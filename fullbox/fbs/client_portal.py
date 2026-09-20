from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from typing import Iterable

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Count, F, Q, Sum
from django.db.models.functions import Lower
from django.utils import timezone

from sklad.models import WarehouseStockSnapshot
from sklad.services.warehouse_transitions import WarehouseTransitionError
from sklad.services.warehouse_write_path import WarehouseWritePathService
from sku.models import Agency, SKUBarcode

from .goods_types import (
    FBS_CLIENT_MOVEMENT_SOURCE_GOODS_TYPE_LABEL,
    FBS_CLIENT_MOVEMENT_AFTER_PROCESSING_STATE,
    fbs_client_movement_source_stock_q,
)
from .models import (
    FbsClientMovementRequest,
    FbsClientMovementRequestLine,
    FbsIntegrationProfile,
    FbsOrder,
    FbsReplenishmentPolicy,
    FbsReplenishmentPreparedBox,
    FbsStockBalance,
)
from .services.client_movements import eligible_mixed_source_boxes, eligible_source_boxes


IN_WORK_STATUSES = {
    FbsOrder.STATUS_RESERVED,
    FbsOrder.STATUS_QUEUED_FOR_PICK,
    FbsOrder.STATUS_PICKING,
    FbsOrder.STATUS_PICKED,
    FbsOrder.STATUS_READY_FOR_HANDOVER,
}
NOT_STARTED_STATUSES = {
    FbsOrder.STATUS_RECEIVED,
    FbsOrder.STATUS_AWAITING_STOCK,
}
COMPLETED_STATUSES = {
    FbsOrder.STATUS_HANDED_OVER,
    FbsOrder.STATUS_DELIVERED,
    FbsOrder.STATUS_RETURNED,
}
PROBLEM_STATUSES = {
    FbsOrder.STATUS_VALIDATION_FAILED,
    FbsOrder.STATUS_AWAITING_STOCK,
    FbsOrder.STATUS_EXCEPTION,
    FbsOrder.STATUS_RETURN_PENDING,
}
UNRESERVED_CLIENT_MOVEMENT_STATUSES = {
    FbsClientMovementRequest.STATUS_SUBMITTED,
    FbsClientMovementRequest.STATUS_APPROVED,
}
OPEN_CLIENT_MOVEMENT_STATUSES = {
    *UNRESERVED_CLIENT_MOVEMENT_STATUSES,
    FbsClientMovementRequest.STATUS_WAREHOUSE_ACCEPTED,
    FbsClientMovementRequest.STATUS_IN_PROGRESS,
    FbsClientMovementRequest.STATUS_MOVED,
    FbsClientMovementRequest.STATUS_AWAITING_MANAGER_CONFIRMATION,
    FbsClientMovementRequest.STATUS_NEEDS_CLARIFICATION,
}
WAREHOUSE_SNAPSHOT_FIELDS = {
    field.name for field in WarehouseStockSnapshot._meta.concrete_fields
}


def _barcode_key(value: object) -> str:
    return str(value or "").strip().casefold()


def _catalog_skus_by_barcode(
    *,
    agency: Agency,
    barcodes: Iterable[str],
) -> dict[str, object]:
    barcode_keys = {
        _barcode_key(value)
        for value in barcodes
        if _barcode_key(value)
    }
    if not barcode_keys:
        return {}
    rows = (
        SKUBarcode.objects.select_related("sku")
        .annotate(normalized_value=Lower("value"))
        .filter(
            normalized_value__in=barcode_keys,
            sku__agency=agency,
            sku__deleted=False,
        )
        .order_by("id")
    )
    return {_barcode_key(row.value): row.sku for row in rows}


def _snapshot_lot_code(snapshot: WarehouseStockSnapshot) -> str:
    if "lot_code" not in WAREHOUSE_SNAPSHOT_FIELDS:
        return ""
    return str(getattr(snapshot, "lot_code", "") or "").strip()


def _snapshot_expiry_date(snapshot: WarehouseStockSnapshot):
    if "expiry_date" not in WAREHOUSE_SNAPSHOT_FIELDS:
        return None
    return getattr(snapshot, "expiry_date", None)


def _as_positive_int(value, *, field: str) -> int:
    try:
        parsed = int(str(value or "").strip())
    except (TypeError, ValueError):
        raise ValidationError(f"{field}: укажите целое число.")
    if parsed <= 0:
        raise ValidationError(f"{field}: значение должно быть больше нуля.")
    return parsed


def _as_non_negative_int(value, *, field: str) -> int:
    try:
        parsed = int(str(value if value not in (None, "") else 0).strip())
    except (TypeError, ValueError):
        raise ValidationError(f"{field}: укажите целое число.")
    if parsed < 0:
        raise ValidationError(f"{field}: значение не может быть отрицательным.")
    return parsed


def _general_stock_queryset(agency: Agency):
    fbs_zone = str(getattr(settings, "FBS_ZONE_CODE", "FBS") or "FBS").strip()
    queryset = (
        WarehouseStockSnapshot.objects.filter(
            agency=agency,
            is_archived=False,
            is_in_vehicle=False,
            qty__gt=0,
        )
        .exclude(zone_code__iexact=fbs_zone)
    )
    if "expiry_date" in WAREHOUSE_SNAPSHOT_FIELDS:
        queryset = queryset.filter(
            Q(expiry_date__isnull=True) | Q(expiry_date__gte=timezone.localdate())
        )
    candidate_snapshots = list(
        queryset.select_related("container__parent_container", "parent_container")
    )
    shipping_blocked_ids = WarehouseWritePathService.shipping_reserved_snapshot_ids(
        agency=agency,
        snapshots=candidate_snapshots,
    )
    if shipping_blocked_ids:
        queryset = queryset.exclude(id__in=shipping_blocked_ids)
    return queryset


def _fbs_movement_source_stock_queryset(agency: Agency):
    return _general_stock_queryset(agency).filter(
        fbs_client_movement_source_stock_q(agency_id=agency.pk),
        active_operation__isnull=True,
    )


def _ordered_fbs_movement_source_stock_queryset(agency: Agency):
    queryset = _fbs_movement_source_stock_queryset(agency).select_for_update()
    if "expiry_date" in WAREHOUSE_SNAPSHOT_FIELDS:
        return queryset.order_by(
            F("expiry_date").asc(nulls_last=True), "created_at", "id"
        )
    return queryset.order_by("created_at", "id")


def _quantity_map(queryset, barcodes: Iterable[str], field: str = "available_qty") -> dict[str, int]:
    values = [str(value or "").strip() for value in barcodes if str(value or "").strip()]
    if not values:
        return {}
    return {
        str(row["barcode"]): int(row["total"] or 0)
        for row in queryset.filter(barcode__in=values)
        .values("barcode")
        .annotate(total=Sum(field))
    }


def _stock_box_size_counts(queryset, barcodes: Iterable[str]) -> dict[str, dict[int, int]]:
    values = [str(value or "").strip() for value in barcodes if str(value or "").strip()]
    if not values:
        return {}
    counts: dict[str, dict[int, int]] = defaultdict(dict)
    rows = (
        queryset.filter(
            barcode__in=values,
            container_id__isnull=False,
            available_qty__gt=0,
        )
        .values("barcode", "container_id")
        .annotate(
            units_per_box=Sum("qty"),
            available_in_container=Sum("available_qty"),
        )
    )
    for row in rows:
        barcode = str(row["barcode"] or "").strip()
        units_per_box = int(row["units_per_box"] or 0)
        available_in_container = int(row["available_in_container"] or 0)
        if not barcode or units_per_box <= 0 or available_in_container <= 0:
            continue
        counts.setdefault(barcode, {})[units_per_box] = (
            counts.get(barcode, {}).get(units_per_box, 0) + 1
        )
    return counts


def _pending_client_movement_quantity_map(
    agency: Agency,
    barcodes: Iterable[str] | None = None,
) -> dict[str, int]:
    queryset = FbsClientMovementRequestLine.objects.filter(
        request__agency=agency,
        request__status__in=UNRESERVED_CLIENT_MOVEMENT_STATUSES,
        request__idempotency_key="",
    )
    values = [
        str(value or "").strip()
        for value in (barcodes or [])
        if str(value or "").strip()
    ]
    if values:
        queryset = queryset.filter(barcode__in=values)
    return {
        str(row["barcode"]): int(row["total"] or 0)
        for row in queryset.values("barcode").annotate(total=Sum("requested_qty"))
    }


def validate_client_movement_lines(
    *,
    agency: Agency,
    mode: str,
    raw_lines: Iterable[dict],
    lock_stock: bool = False,
    item_box_count_is_request_level: bool = False,
) -> tuple[list[dict], list[str]]:
    if mode not in {
        FbsClientMovementRequest.MODE_BOX,
        FbsClientMovementRequest.MODE_ITEM,
    }:
        return [], ["Выберите режим перемещения: коробами или поштучно."]

    prepared = []
    errors = []
    for index, raw in enumerate(list(raw_lines or []), start=1):
        row_number = int(raw.get("row_number") or index)
        barcode = str(raw.get("barcode") or "").strip()
        if not barcode:
            errors.append(f"Строка {row_number}: не указан штрихкод.")
            continue
        try:
            qty = _as_positive_int(raw.get("qty") or raw.get("requested_qty"), field="Количество")
            units_per_box = 1
            if mode == FbsClientMovementRequest.MODE_ITEM:
                if item_box_count_is_request_level:
                    box_count = 0
                else:
                    raw_box_count = raw.get("box_count")
                    if raw_box_count in (None, ""):
                        raw_box_count = raw.get("requested_box_count")
                    if raw_box_count in (None, ""):
                        raw_box_count = 1
                    box_count = _as_positive_int(
                        raw_box_count,
                        field="Количество коробов",
                    )
                    if box_count > qty:
                        raise ValidationError(
                            "Количество коробов не может превышать количество штук."
                        )
            else:
                raw_box_plan = raw.get("box_plan")
                box_plan_counts: dict[int, int] = defaultdict(int)
                if raw_box_plan not in (None, "", []):
                    if not isinstance(raw_box_plan, (list, tuple)):
                        raise ValidationError(
                            "План коробов передан в неверном формате."
                        )
                    for plan_index, plan_row in enumerate(raw_box_plan, start=1):
                        if not isinstance(plan_row, dict):
                            raise ValidationError(
                                f"План коробов, строка {plan_index}: неверный формат."
                            )
                        plan_units = _as_positive_int(
                            plan_row.get("units_per_box"),
                            field=f"План коробов, строка {plan_index}: кратность",
                        )
                        plan_boxes = _as_positive_int(
                            plan_row.get("box_count"),
                            field=f"План коробов, строка {plan_index}: коробов",
                        )
                        box_plan_counts[plan_units] += plan_boxes
                    box_plan = [
                        {
                            "units_per_box": int(plan_units),
                            "box_count": int(plan_boxes),
                        }
                        for plan_units, plan_boxes in sorted(
                            box_plan_counts.items(), reverse=True
                        )
                    ]
                    planned_qty = sum(
                        int(item["units_per_box"]) * int(item["box_count"])
                        for item in box_plan
                    )
                    if qty != planned_qty:
                        raise ValidationError(
                            f"План целых коробов даёт {planned_qty} шт., "
                            f"а в строке указано {qty} шт. Обновите остатки и повторите выбор."
                        )
                    box_count = sum(int(item["box_count"]) for item in box_plan)
                    units_per_box = (
                        int(box_plan[0]["units_per_box"])
                        if len(box_plan) == 1
                        else 1
                    )
                else:
                    units_per_box = _as_positive_int(
                        raw.get("units_per_box"), field="Кратность короба"
                    )
                    if qty % units_per_box:
                        raise ValidationError(
                            "Количество должно делиться на кратность короба без остатка."
                        )
                    box_count = qty // units_per_box
                    box_plan = [
                        {
                            "units_per_box": units_per_box,
                            "box_count": box_count,
                        }
                    ]
        except ValidationError as exc:
            errors.append(f"Строка {row_number}: {'; '.join(exc.messages)}")
            continue
        prepared.append(
            {
                "row_number": row_number,
                "barcode": barcode,
                "qty": qty,
                "units_per_box": units_per_box,
                "box_count": box_count,
                "box_plan": box_plan if mode == FbsClientMovementRequest.MODE_BOX else [],
            }
        )

    if not prepared and not errors:
        errors.append("Добавьте хотя бы одну позицию.")
    if len(prepared) > 2000:
        return [], ["В одной заявке разрешено не более 2000 строк."]

    grouped: dict[str, dict] = {}
    for row in prepared:
        existing = grouped.get(row["barcode"])
        if existing is None:
            grouped[row["barcode"]] = dict(row)
            continue
        existing["qty"] += row["qty"]
        existing["box_count"] += row["box_count"]
        if mode == FbsClientMovementRequest.MODE_BOX:
            plan_counts = defaultdict(int)
            for plan_row in [*existing.get("box_plan", []), *row.get("box_plan", [])]:
                plan_counts[int(plan_row["units_per_box"])] += int(
                    plan_row["box_count"]
                )
            existing["box_plan"] = [
                {"units_per_box": units, "box_count": count}
                for units, count in sorted(plan_counts.items(), reverse=True)
            ]
            existing["units_per_box"] = (
                int(existing["box_plan"][0]["units_per_box"])
                if len(existing["box_plan"]) == 1
                else 1
            )

    if (
        mode == FbsClientMovementRequest.MODE_ITEM
        and not item_box_count_is_request_level
        and sum(int(row["box_count"]) for row in grouped.values()) > 50
    ):
        errors.append("В одной поштучной заявке разрешено не более 50 коробов.")

    barcodes = list(grouped)
    sku_by_barcode = _catalog_skus_by_barcode(
        agency=agency,
        barcodes=barcodes,
    )
    general_stock = _fbs_movement_source_stock_queryset(agency)
    if lock_stock:
        list(
            general_stock.select_for_update()
            .filter(barcode__in=barcodes)
            .values_list("id", flat=True)
        )
    warehouse_fact_by_barcode = _quantity_map(general_stock, barcodes, field="qty")
    warehouse_available_by_barcode = _quantity_map(general_stock, barcodes)
    from sklad.services.fbs_quantity_reserves import available_by_barcode
    quantity_available = available_by_barcode(agency.id)
    warehouse_available_by_barcode = {
        code: min(qty, quantity_available.get(code.casefold(), 0))
        for code, qty in warehouse_available_by_barcode.items()
    }
    pending_by_barcode = _pending_client_movement_quantity_map(agency, barcodes)
    fbs_by_barcode = _quantity_map(
        FbsStockBalance.objects.filter(agency=agency), barcodes
    )
    box_candidate_counts: dict[tuple[str, int], int] = {}
    if mode == FbsClientMovementRequest.MODE_BOX:
        for candidate in eligible_source_boxes(
            agency_id=agency.id,
            barcodes=barcodes,
            lock=lock_stock,
        ):
            key = (candidate.barcode, candidate.units_per_box)
            box_candidate_counts[key] = box_candidate_counts.get(key, 0) + 1
    normalized = []
    for barcode, row in grouped.items():
        sku = sku_by_barcode.get(_barcode_key(barcode))
        if sku is None:
            errors.append(f"ШК {barcode}: не найден в номенклатуре этого клиента.")
            continue
        warehouse_fact = int(warehouse_fact_by_barcode.get(barcode, 0))
        warehouse_available = int(warehouse_available_by_barcode.get(barcode, 0))
        pending_qty = int(pending_by_barcode.get(barcode, 0))
        client_available = max(warehouse_available - pending_qty, 0)
        if client_available < row["qty"]:
            errors.append(
                f"ШК {barcode}: к заявке доступно {client_available}, запрошено {row['qty']} "
                f"(доступный остаток: факт склада {warehouse_fact}, "
                f"доступно складу {warehouse_available}, "
                f"уже в FBS-заявках {pending_qty})."
            )
        if mode == FbsClientMovementRequest.MODE_BOX:
            for plan_row in row.get("box_plan", []):
                plan_units = int(plan_row["units_per_box"])
                plan_boxes = int(plan_row["box_count"])
                physical_box_count = int(
                    box_candidate_counts.get((barcode, plan_units), 0)
                )
                available_box_count = min(
                    physical_box_count,
                    client_available // plan_units,
                )
                if physical_box_count <= 0:
                    errors.append(
                        f"ШК {barcode}: кратность {plan_units} шт. не найдена "
                        "среди доступных физических монокоробов. Выберите доступный "
                        "целый короб или физический микс-короб."
                    )
                elif available_box_count < plan_boxes:
                    errors.append(
                        f"ШК {barcode}: целых коробов по {plan_units} шт. "
                        f"из доступного остатка доступно {available_box_count}, "
                        f"запрошено {plan_boxes} (физически найдено "
                        f"{physical_box_count})."
                    )
        normalized.append(
            {
                **row,
                "sku": sku,
                "sku_id": sku.id,
                "sku_code": str(sku.sku_code or ""),
                "product_name": str(sku.name or ""),
                "goods_type": FBS_CLIENT_MOVEMENT_SOURCE_GOODS_TYPE_LABEL,
                "warehouse_fact_qty": warehouse_fact,
                "warehouse_available_qty": warehouse_available,
                "pending_movement_qty": pending_qty,
                "general_available_qty": client_available,
                "fbs_available_qty": int(fbs_by_barcode.get(barcode, 0)),
            }
        )
    return normalized, errors


@transaction.atomic
def create_client_movement_request(
    *,
    agency: Agency,
    mode: str,
    raw_lines: Iterable[dict],
    requested_by=None,
    comment: str = "",
    source_file_name: str = "",
    requested_box_count=None,
    requested_mixed_box_count=0,
    mixed_box_codes=None,
    idempotency_key: str = "",
    edit_request_id: int | None = None,
    expected_updated_at: str = "",
) -> FbsClientMovementRequest:
    mode = str(mode or "").strip().lower()
    if mode != FbsClientMovementRequest.MODE_BOX:
        raise ValidationError(
            "FBS-перемещение создаётся только целыми коробами по кратности. "
            "Поштучное перемещение и сканирование Честного знака отключены."
        )
    edited_request = None
    before_edit = None
    if edit_request_id is not None:
        from .services.movement_edit import lock_movement_for_edit
        edited_request = lock_movement_for_edit(agency=agency, request_id=edit_request_id,
            user=requested_by, expected_updated_at=expected_updated_at)
        if mode != edited_request.mode:
            raise ValidationError("Режим перемещения при редактировании сохраняется.")
        idempotency_key = edited_request.idempotency_key
        before_edit = {
            "comment": edited_request.comment, "requested_qty": edited_request.requested_qty,
            "requested_box_count": edited_request.requested_box_count,
            "requested_mixed_box_count": edited_request.requested_mixed_box_count,
            "lines": list(edited_request.lines.order_by("id").values("id", "barcode", "sku_code", "product_name", "requested_qty", "units_per_box", "requested_box_count")),
        }
    idempotency_key = str(idempotency_key or "").strip()[:64]
    hard_reserve = bool(idempotency_key)
    agency = Agency.objects.select_for_update().get(pk=agency.pk)
    if edited_request is not None:
        from .services.client_movements import _hard_reserve_allocations, _release_hard_reserve
        _hard_reserve_allocations(edited_request, lock=True)
        _release_hard_reserve(edited_request, actor=requested_by, reason="Менеджер изменяет состав FBS-заявки")
    if hard_reserve and edited_request is None:
        existing = (
            FbsClientMovementRequest.objects.select_for_update()
            .filter(agency=agency, idempotency_key=idempotency_key)
            .first()
        )
        if existing is not None:
            return existing
    if mixed_box_codes not in (None, "") and not isinstance(
        mixed_box_codes,
        (list, tuple, set),
    ):
        raise ValidationError("Список микс-коробов передан в неверном формате.")
    selected_mixed_box_codes = []
    for value in list(mixed_box_codes or []):
        code = str(value or "").strip()
        if code and code not in selected_mixed_box_codes:
            selected_mixed_box_codes.append(code)
    if selected_mixed_box_codes and mode != FbsClientMovementRequest.MODE_BOX:
        raise ValidationError(
            "Существующий микс-короб можно выбрать только в режиме перемещения коробами."
        )
    if selected_mixed_box_codes and not hard_reserve:
        raise ValidationError(
            "Для перемещения существующего микс-короба нужен точный складской резерв."
        )
    if len(selected_mixed_box_codes) > 50:
        raise ValidationError("В одной заявке разрешено не более 50 микс-коробов.")
    mixed_box_candidates = (
        eligible_mixed_source_boxes(
            agency_id=agency.id,
            container_codes=selected_mixed_box_codes,
            lock=True,
        )
        if selected_mixed_box_codes
        else []
    )
    mixed_candidates_by_code = {
        candidate.container.container_code: candidate
        for candidate in mixed_box_candidates
    }
    unavailable_mixed_codes = [
        code for code in selected_mixed_box_codes if code not in mixed_candidates_by_code
    ]
    if unavailable_mixed_codes:
        raise ValidationError(
            "Микс-короб недоступен для перемещения целиком: "
            + ", ".join(unavailable_mixed_codes)
            + ". Обновите остатки и повторите выбор."
        )
    mixed_box_candidates = [
        mixed_candidates_by_code[code] for code in selected_mixed_box_codes
    ]
    request_level_box_plan = (
        mode == FbsClientMovementRequest.MODE_ITEM
        and requested_box_count not in (None, "")
    )
    submitted_lines = list(raw_lines or [])
    if submitted_lines:
        lines, errors = validate_client_movement_lines(
            agency=agency,
            mode=mode,
            raw_lines=submitted_lines,
            lock_stock=True,
            item_box_count_is_request_level=request_level_box_plan,
        )
    elif mixed_box_candidates:
        lines, errors = [], []
    else:
        lines, errors = [], ["Добавьте хотя бы одну позицию."]
    if errors:
        raise ValidationError(errors)
    regular_lines_by_barcode = {row["barcode"]: dict(row) for row in lines}
    mixed_qty_by_barcode: dict[str, int] = defaultdict(int)
    for candidate in mixed_box_candidates:
        for snapshot in candidate.snapshots:
            mixed_qty_by_barcode[str(snapshot.barcode or "").strip()] += int(
                snapshot.qty or 0
            )
    if mixed_qty_by_barcode:
        mixed_barcodes = list(mixed_qty_by_barcode)
        sku_by_barcode = _catalog_skus_by_barcode(
            agency=agency,
            barcodes=mixed_barcodes,
        )
        missing_barcodes = [
            value
            for value in mixed_barcodes
            if _barcode_key(value) not in sku_by_barcode
        ]
        if missing_barcodes:
            raise ValidationError(
                "В составе микс-короба есть ШК без номенклатуры клиента: "
                + ", ".join(missing_barcodes)
                + "."
            )
        general_stock = _fbs_movement_source_stock_queryset(agency)
        warehouse_fact_by_barcode = _quantity_map(
            general_stock,
            mixed_barcodes,
            field="qty",
        )
        warehouse_available_by_barcode = _quantity_map(
            general_stock,
            mixed_barcodes,
        )
        pending_by_barcode = _pending_client_movement_quantity_map(
            agency,
            mixed_barcodes,
        )
        fbs_by_barcode = _quantity_map(
            FbsStockBalance.objects.filter(agency=agency),
            mixed_barcodes,
        )
        lines_by_barcode = {row["barcode"]: row for row in lines}
        for row_number, barcode_value in enumerate(mixed_barcodes, start=len(lines) + 1):
            sku = sku_by_barcode[_barcode_key(barcode_value)]
            warehouse_fact = int(warehouse_fact_by_barcode.get(barcode_value, 0))
            warehouse_available = int(
                warehouse_available_by_barcode.get(barcode_value, 0)
            )
            pending_qty = int(pending_by_barcode.get(barcode_value, 0))
            client_available = max(warehouse_available - pending_qty, 0)
            mixed_qty = int(mixed_qty_by_barcode[barcode_value])
            existing = lines_by_barcode.get(barcode_value)
            total_requested = mixed_qty + int(existing["qty"] if existing else 0)
            if total_requested > client_available:
                raise ValidationError(
                    f"ШК {barcode_value}: к заявке доступно {client_available}, "
                    f"с выбранными микс-коробами запрошено {total_requested}."
                )
            if existing is not None:
                existing["qty"] = total_requested
                existing["units_per_box"] = 1
                existing["general_available_qty"] = client_available
                continue
            row = {
                "row_number": row_number,
                "barcode": barcode_value,
                "qty": mixed_qty,
                "units_per_box": 1,
                "box_count": 0,
                "sku": sku,
                "sku_id": sku.id,
                "sku_code": str(sku.sku_code or ""),
                "product_name": str(sku.name or ""),
                "goods_type": FBS_CLIENT_MOVEMENT_SOURCE_GOODS_TYPE_LABEL,
                "warehouse_fact_qty": warehouse_fact,
                "warehouse_available_qty": warehouse_available,
                "pending_movement_qty": pending_qty,
                "general_available_qty": client_available,
                "fbs_available_qty": int(fbs_by_barcode.get(barcode_value, 0)),
            }
            lines.append(row)
            lines_by_barcode[barcode_value] = row
    requested_qty = sum(int(row["qty"]) for row in lines)
    if request_level_box_plan:
        physical_box_count = _as_positive_int(
            requested_box_count,
            field="Физических коробов всего",
        )
        mixed_box_count = _as_non_negative_int(
            requested_mixed_box_count,
            field="Микс-коробов",
        )
        if physical_box_count > 50:
            raise ValidationError("В одной поштучной заявке разрешено не более 50 коробов.")
        if physical_box_count > requested_qty:
            raise ValidationError(
                "Количество физических коробов не может превышать количество штук."
            )
        if mixed_box_count > physical_box_count:
            raise ValidationError(
                "Количество микс-коробов не может превышать общее количество коробов."
            )
        distinct_line_count = len(lines)
        if mixed_box_count and distinct_line_count < 2:
            raise ValidationError(
                "Микс-короб возможен только для заявки с двумя или более разными ШК."
            )
        if distinct_line_count > physical_box_count and mixed_box_count == 0:
            raise ValidationError(
                "Позиций больше, чем физических коробов: укажите хотя бы один микс-короб."
            )
    else:
        physical_box_count = sum(
            int(row["box_count"]) for row in regular_lines_by_barcode.values()
        ) + len(mixed_box_candidates)
        mixed_box_count = _as_non_negative_int(
            requested_mixed_box_count,
            field="Микс-коробов",
        )
        if mixed_box_count not in (0, len(mixed_box_candidates)):
            raise ValidationError(
                "Количество смешанных коробов должно совпадать с выбранными целыми коробами."
            )
        # Count actual selected containers, not a manually entered packing plan.
        # All their current stock is reserved below through the whole-box path.
        mixed_box_count = len(mixed_box_candidates)
    actor = requested_by if getattr(requested_by, "is_authenticated", False) else None
    if edited_request is not None:
        request_row = edited_request
        request_row.comment = str(comment or "").strip()
        request_row.requested_qty = requested_qty
        request_row.requested_box_count = physical_box_count
        request_row.requested_mixed_box_count = mixed_box_count
        request_row.lines.all().delete()
    else:
        request_row = FbsClientMovementRequest(
            agency=agency,
            mode=mode,
            status=(
                FbsClientMovementRequest.STATUS_SUBMITTED
                if hard_reserve
                else FbsClientMovementRequest.STATUS_APPROVED
            ),
            idempotency_key=idempotency_key,
            requested_by=actor,
            comment=str(comment or "").strip(),
            source_file_name=str(source_file_name or "").strip()[:255],
            requested_qty=requested_qty,
            requested_box_count=physical_box_count,
            requested_mixed_box_count=mixed_box_count,
        )
    request_row.full_clean()
    request_row.save()
    saved_lines = []
    for row in lines:
        line = FbsClientMovementRequestLine(
            request=request_row,
            sku=row["sku"],
            barcode=row["barcode"],
            sku_code=row["sku_code"],
            product_name=row["product_name"],
            requested_qty=row["qty"],
            units_per_box=row["units_per_box"],
            requested_box_count=row["box_count"],
            general_available_qty_snapshot=row["general_available_qty"],
            fbs_available_qty_snapshot=row["fbs_available_qty"],
        )
        line.full_clean()
        line.save()
        saved_lines.append(line)
    reserve_allocations = []
    if hard_reserve and mode == FbsClientMovementRequest.MODE_BOX:
        saved_lines_by_barcode = {line.barcode: line for line in saved_lines}
        whole_container_ids = set()
        for candidate in mixed_box_candidates:
            whole_container_ids.add(candidate.container.id)
            for snapshot in candidate.snapshots:
                line = saved_lines_by_barcode[str(snapshot.barcode or "").strip()]
                reserve_allocations.append(
                    {
                        "snapshot_id": snapshot.id,
                        "request_line_id": line.id,
                        "qty": int(snapshot.qty or 0),
                        "container_id": candidate.container.id,
                        "container_code": candidate.container.container_code,
                        "barcode": str(snapshot.barcode or "").strip(),
                        "sku_id": snapshot.sku_ref_id,
                    }
                )
        for barcode_value, regular_row in regular_lines_by_barcode.items():
            line = saved_lines_by_barcode[barcode_value]
            remaining = int(regular_row["qty"] or 0)
            for plan_row in regular_row.get("box_plan", []):
                plan_units = int(plan_row["units_per_box"] or 0)
                plan_boxes = int(plan_row["box_count"] or 0)
                candidates = eligible_source_boxes(
                    agency_id=agency.id,
                    barcodes=(line.barcode,),
                    units_per_box=plan_units,
                    lock=True,
                )
                selected = candidates[:plan_boxes]
                if len(selected) != plan_boxes:
                    raise ValidationError(
                        f"ШК {line.barcode}: коробов по {plan_units} шт. "
                        f"осталось {len(selected)}, запрошено {plan_boxes}. "
                        "Обновите остатки и повторите подачу."
                    )
                for candidate in selected:
                    whole_container_ids.add(candidate.container.id)
                    snapshots = list(
                        WarehouseStockSnapshot.objects.select_for_update()
                        .filter(
                            agency=agency,
                            container=candidate.container,
                            is_archived=False,
                            qty__gt=0,
                        )
                        .order_by("id")
                    )
                    for snapshot in snapshots:
                        reserve_allocations.append(
                            {
                                "snapshot_id": snapshot.id,
                                "request_line_id": line.id,
                                "qty": int(snapshot.qty or 0),
                                "container_id": candidate.container.id,
                                "container_code": candidate.container.container_code,
                                "barcode": str(snapshot.barcode or "").strip(),
                                "sku_id": snapshot.sku_ref_id,
                            }
                        )
                        remaining -= int(snapshot.qty or 0)
            if remaining:
                raise ValidationError(
                    f"ШК {line.barcode}: план физических коробов не совпал "
                    f"с количеством; не хватает {remaining} шт. "
                    "Обновите остатки и повторите подачу."
                )
    elif hard_reserve:
        source_queryset = _ordered_fbs_movement_source_stock_queryset(agency)
        for line in saved_lines:
            remaining = int(line.requested_qty or 0)
            for snapshot in source_queryset.filter(barcode=line.barcode):
                if remaining <= 0:
                    break
                qty = min(remaining, int(snapshot.available_qty or 0))
                if qty <= 0:
                    continue
                reserve_allocations.append(
                    {
                        "snapshot_id": snapshot.id,
                        "request_line_id": line.id,
                        "qty": qty,
                        "container_id": snapshot.container_id,
                        "container_code": str(snapshot.container_code or "").strip(),
                        "barcode": line.barcode,
                        "sku_id": line.sku_id,
                    }
                )
                remaining -= qty
            if remaining:
                raise ValidationError(
                    f"ШК {line.barcode}: точный доступный остаток уже изменился; "
                    f"не хватает {remaining} шт. Обновите остатки и повторите подачу."
                )
    if hard_reserve:
        try:
            if mode == FbsClientMovementRequest.MODE_BOX:
                WarehouseWritePathService.reserve_for_fbs_movement(
                    agency=agency,
                    request_id=request_row.id,
                    allocations=reserve_allocations,
                    whole_container_ids=whole_container_ids,
                    created_by=actor,
                )
            else:
                from sklad.services.fbs_quantity_reserves import reserve_quantity

                reserve_quantity(
                    agency=agency,
                    request_id=request_row.id,
                    allocations=reserve_allocations,
                    created_by=actor,
                )
        except WarehouseTransitionError as exc:
            raise ValidationError(str(exc)) from exc
    from audit.models import log_order_action

    log_order_action(
        "update" if edited_request is not None else "create",
        order_id=request_row.number,
        order_type="fbs_movement",
        user=actor,
        agency=agency,
        description=("Менеджер изменил FBS-заявку; состав и складской резерв пересчитаны" if edited_request is not None else (
            "Клиент подал заявку на перемещение в FBS; "
            + (
                "товар зарезервирован, заявка ожидает подтверждения менеджера"
                if hard_reserve
                else "заявка автоматически передана кладовщику"
            )
        )),
        payload={
            **({"before": before_edit, "after": {"comment": request_row.comment,
                "lines": list(request_row.lines.order_by("id").values("id", "barcode", "sku_code", "product_name", "requested_qty", "units_per_box", "requested_box_count"))}} if edited_request is not None else {}),
            "status": request_row.status,
            "status_label": request_row.get_status_display(),
            "request_id": request_row.id,
            "mode": request_row.mode,
            "requested_qty": request_row.requested_qty,
            "requested_box_count": request_row.requested_box_count,
            "requested_mixed_box_count": request_row.requested_mixed_box_count,
        },
    )
    return request_row


def client_movement_source_stock_payload(
    *,
    agency: Agency,
    search: str = "",
    article: str = "",
    brand: str = "",
    barcode: str = "",
    include_box_options: bool = False,
) -> dict:
    queryset = _fbs_movement_source_stock_queryset(agency)
    needle = str(search or "").strip()
    if needle:
        queryset = queryset.filter(
            Q(barcode__icontains=needle)
            | Q(sku_code__icontains=needle)
            | Q(name__icontains=needle)
        )
    catalog_filters = {
        "article": str(article or "").strip(),
        "brand": str(brand or "").strip(),
        "barcode": str(barcode or "").strip(),
    }
    if any(catalog_filters.values()):
        matching_barcodes = SKUBarcode.objects.filter(
            sku__agency=agency,
            sku__deleted=False,
        )
        if catalog_filters["article"]:
            matching_barcodes = matching_barcodes.filter(
                sku__sku_code__icontains=catalog_filters["article"]
            )
        if catalog_filters["brand"]:
            matching_barcodes = matching_barcodes.filter(
                sku__brand__icontains=catalog_filters["brand"]
            )
        if catalog_filters["barcode"]:
            matching_barcodes = matching_barcodes.filter(
                value__icontains=catalog_filters["barcode"]
            )
        matching_barcode_keys = matching_barcodes.annotate(
            normalized_value=Lower("value")
        ).values("normalized_value")
        queryset = queryset.annotate(
            normalized_barcode=Lower("barcode")
        ).filter(normalized_barcode__in=matching_barcode_keys)
    grouped_rows = list(
        queryset.values("barcode")
        .annotate(
            warehouse_fact_qty=Sum("qty"),
            warehouse_available_qty=Sum("available_qty"),
            location_count=Count("location_id", distinct=True),
            container_count=Count("container_code", distinct=True),
        )
        .filter(warehouse_available_qty__gt=0)
        .exclude(barcode="")
        .order_by("barcode")[:500]
    )
    barcodes = [str(row["barcode"] or "").strip() for row in grouped_rows]
    # Explain readiness without treating completed processing as loose goods.
    source_labels_by_barcode: dict[str, list[str]] = defaultdict(list)
    if barcodes:
        for source in (
            queryset.filter(barcode__in=barcodes, available_qty__gt=0)
            .values("barcode", "warehouse_state_code")
            .order_by("barcode", "warehouse_state_code")
            .distinct()
        ):
            label = {
                FBS_CLIENT_MOVEMENT_AFTER_PROCESSING_STATE: "Готово после обработки",
                "placed_in_receiving": "Разрешено размещение из приёмки",
            }.get(str(source["warehouse_state_code"] or "").strip().casefold(), "Готово на хранении")
            labels = source_labels_by_barcode[source["barcode"]]
            if label not in labels:
                labels.append(label)
    unavailable_by_barcode: dict[str, dict[str, int]] = defaultdict(dict)
    unavailable_by_zone: dict[str, int] = defaultdict(int)
    if barcodes:
        unavailable_rows = (
            queryset.filter(
                barcode__in=barcodes,
                qty__gt=F("available_qty"),
            )
            .values("barcode", "zone_code")
            .annotate(total=Sum(F("qty") - F("available_qty")))
        )
        for unavailable_row in unavailable_rows:
            unavailable_barcode = str(
                unavailable_row["barcode"] or ""
            ).strip()
            zone_code = str(unavailable_row["zone_code"] or "").strip() or "—"
            unavailable_qty = int(unavailable_row["total"] or 0)
            if not unavailable_barcode or unavailable_qty <= 0:
                continue
            unavailable_by_barcode.setdefault(unavailable_barcode, {})[
                zone_code
            ] = unavailable_qty
            unavailable_by_zone[zone_code] += unavailable_qty
    mixed_box_candidates = (
        eligible_mixed_source_boxes(agency_id=agency.id)
        if include_box_options
        else []
    )
    mixed_candidate_barcodes = {
        str(snapshot.barcode or "").strip()
        for candidate in mixed_box_candidates
        for snapshot in candidate.snapshots
        if str(snapshot.barcode or "").strip()
    }
    payload_barcodes = list(dict.fromkeys([*barcodes, *sorted(mixed_candidate_barcodes)]))
    sku_by_barcode = _catalog_skus_by_barcode(
        agency=agency,
        barcodes=payload_barcodes,
    )
    pending_by_barcode = _pending_client_movement_quantity_map(agency, payload_barcodes)
    fbs_by_barcode = _quantity_map(
        FbsStockBalance.objects.filter(agency=agency), payload_barcodes
    )
    box_candidates_by_barcode: dict[str, dict[int, int]] = defaultdict(dict)
    if include_box_options:
        for candidate in eligible_source_boxes(
            agency_id=agency.id,
            barcodes=barcodes,
        ):
            counts = box_candidates_by_barcode[candidate.barcode]
            counts[candidate.units_per_box] = counts.get(candidate.units_per_box, 0) + 1
    mixed_warehouse_available = _quantity_map(
        _fbs_movement_source_stock_queryset(agency),
        mixed_candidate_barcodes,
    )
    mixed_boxes = []
    mixed_box_qty_by_barcode: dict[str, int] = defaultdict(int)
    article_needle = catalog_filters["article"].casefold()
    brand_needle = catalog_filters["brand"].casefold()
    barcode_needle = catalog_filters["barcode"].casefold()
    search_needle = needle.casefold()
    for candidate in mixed_box_candidates:
        qty_by_barcode: dict[str, int] = defaultdict(int)
        for snapshot in candidate.snapshots:
            qty_by_barcode[str(snapshot.barcode or "").strip()] += int(
                snapshot.qty or 0
            )
        if any(
            _barcode_key(value) not in sku_by_barcode
            for value in qty_by_barcode
        ):
            continue
        items = []
        for item_barcode, item_qty in qty_by_barcode.items():
            sku = sku_by_barcode[_barcode_key(item_barcode)]
            items.append(
                {
                    "barcode": item_barcode,
                    "sku_code": str(sku.sku_code or ""),
                    "name": str(sku.name or ""),
                    "brand": str(sku.brand or ""),
                    "size": str(sku.size or ""),
                    "qty": item_qty,
                }
            )
        container_code = str(candidate.container.container_code or "").strip()
        if search_needle and not any(
            search_needle
            in " ".join(
                [
                    container_code,
                    item["barcode"],
                    item["sku_code"],
                    item["name"],
                ]
            ).casefold()
            for item in items
        ):
            continue
        if article_needle and not any(
            article_needle in item["sku_code"].casefold() for item in items
        ):
            continue
        if brand_needle and not any(
            brand_needle in item["brand"].casefold() for item in items
        ):
            continue
        if barcode_needle and barcode_needle not in container_code.casefold() and not any(
            barcode_needle in item["barcode"].casefold() for item in items
        ):
            continue
        if any(
            item["qty"]
            > max(
                int(mixed_warehouse_available.get(item["barcode"], 0))
                - int(pending_by_barcode.get(item["barcode"], 0)),
                0,
            )
            for item in items
        ):
            continue
        mixed_boxes.append(
            {
                "container_code": container_code,
                "total_qty": candidate.total_qty,
                "position_count": len(items),
                "items": items,
            }
        )
        for item in items:
            mixed_box_qty_by_barcode[item["barcode"]] += int(item["qty"] or 0)

    from sklad.services.fbs_quantity_reserves import available_by_barcode
    quantity_available = available_by_barcode(agency.id)
    rows = []
    for row in grouped_rows:
        barcode = str(row["barcode"] or "").strip()
        sku = sku_by_barcode.get(_barcode_key(barcode))
        if sku is None:
            continue
        warehouse_fact = int(row["warehouse_fact_qty"] or 0)
        warehouse_available = min(int(row["warehouse_available_qty"] or 0), quantity_available.get(barcode.casefold(), 0))
        pending_qty = int(pending_by_barcode.get(barcode, 0))
        client_available = max(warehouse_available - pending_qty, 0)
        if client_available <= 0:
            continue
        box_options = []
        box_units = sorted(box_candidates_by_barcode.get(barcode, {}))
        for units_per_box in box_units:
            physical_box_count = int(
                box_candidates_by_barcode.get(barcode, {}).get(units_per_box, 0)
            )
            available_box_count = min(
                physical_box_count,
                client_available // units_per_box,
            )
            box_options.append(
                {
                    "units_per_box": units_per_box,
                    "physical_box_count": physical_box_count,
                    "available_box_count": available_box_count,
                    "available_qty": available_box_count * units_per_box,
                }
            )
        regular_box_available_qty = sum(
            int(option["available_qty"] or 0) for option in box_options
        )
        regular_box_available_count = sum(
            int(option["available_box_count"] or 0) for option in box_options
        )
        mixed_box_available_qty = int(mixed_box_qty_by_barcode.get(barcode, 0))
        whole_box_available_qty = min(
            regular_box_available_qty + mixed_box_available_qty,
            client_available,
        )
        if include_box_options and whole_box_available_qty <= 0:
            continue
        unavailable_breakdown = [
            {"zone_code": zone_code, "qty": unavailable_qty}
            for zone_code, unavailable_qty in sorted(
                unavailable_by_barcode.get(barcode, {}).items()
            )
            if unavailable_qty > 0
        ]
        rows.append(
            {
                "barcode": barcode,
                "sku_id": sku.id,
                "sku_code": str(sku.sku_code or ""),
                "name": str(sku.name or ""),
                "brand": str(sku.brand or ""),
                "size": str(sku.size or ""),
                "goods_type": " · ".join(source_labels_by_barcode.get(barcode, []))
                or FBS_CLIENT_MOVEMENT_SOURCE_GOODS_TYPE_LABEL,
                "warehouse_fact_qty": warehouse_fact,
                "warehouse_available_qty": warehouse_available,
                "warehouse_reserved_qty": max(warehouse_fact - warehouse_available, 0),
                "warehouse_unavailable_qty": max(
                    warehouse_fact - warehouse_available,
                    0,
                ),
                "unavailable_breakdown": unavailable_breakdown,
                "pending_movement_qty": pending_qty,
                "client_available_qty": client_available,
                "fbs_available_qty": int(fbs_by_barcode.get(barcode, 0)),
                "location_count": int(row["location_count"] or 0),
                "container_count": int(row["container_count"] or 0),
                "box_options": box_options,
                "regular_box_available_count": regular_box_available_count,
                "regular_box_available_qty": regular_box_available_qty,
                "mixed_box_available_qty": mixed_box_available_qty,
                "whole_box_available_qty": whole_box_available_qty,
                "non_box_available_qty": max(
                    client_available - whole_box_available_qty,
                    0,
                ),
            }
        )
    return {
        "results": rows,
        "mixed_boxes": mixed_boxes,
        "total": len(rows),
        "summary": {
            "goods_type": FBS_CLIENT_MOVEMENT_SOURCE_GOODS_TYPE_LABEL,
            "warehouse_fact_qty": sum(row["warehouse_fact_qty"] for row in rows),
            "warehouse_available_qty": sum(
                row["warehouse_available_qty"] for row in rows
            ),
            "warehouse_unavailable_qty": sum(
                row["warehouse_unavailable_qty"] for row in rows
            ),
            "unavailable_breakdown": [
                {"zone_code": zone_code, "qty": unavailable_qty}
                for zone_code, unavailable_qty in sorted(
                    unavailable_by_zone.items()
                )
                if unavailable_qty > 0
            ],
            "pending_movement_qty": sum(row["pending_movement_qty"] for row in rows),
            "client_available_qty": sum(row["client_available_qty"] for row in rows),
            "fbs_available_qty": sum(row["fbs_available_qty"] for row in rows),
            "source_box_count": sum(
                row["regular_box_available_count"] for row in rows
            ),
            "source_box_qty": sum(
                row["regular_box_available_qty"] for row in rows
            ),
            "source_mixed_box_count": len(mixed_boxes),
            "source_mixed_box_qty": sum(
                int(row["total_qty"] or 0) for row in mixed_boxes
            ),
        },
    }


def _is_problem_order(order: FbsOrder) -> bool:
    return bool(
        order.internal_status in PROBLEM_STATUSES
        or str(order.hold_reason or "").strip()
        or str(order.problem_reason or "").strip()
    )


def _problem_filter() -> Q:
    return (
        Q(internal_status__in=PROBLEM_STATUSES)
        | ~Q(hold_reason="")
        | ~Q(problem_reason="")
    )


def _clean_status_filter(statuses: set[str]) -> Q:
    return Q(internal_status__in=statuses, hold_reason="", problem_reason="")


def _order_group(order: FbsOrder) -> str:
    if _is_problem_order(order):
        return "problem"
    if order.internal_status in IN_WORK_STATUSES:
        return "in_work"
    if order.internal_status in COMPLETED_STATUSES:
        return "completed"
    if order.internal_status == FbsOrder.STATUS_CANCELLED:
        return "canceled"
    return "not_started"


def _format_dt(value) -> str:
    if not value:
        return ""
    return timezone.localtime(value).strftime("%d.%m.%Y %H:%M")


def serialize_order(order: FbsOrder, *, include_items: bool = False) -> dict:
    items = list(order.items.all()) if include_items else []
    payload = {
        "id": order.id,
        "external_order_id": order.external_order_id,
        "marketplace": order.profile.marketplace,
        "marketplace_label": order.profile.get_marketplace_display(),
        "warehouse": order.profile.external_warehouse_id,
        "status": order.internal_status,
        "status_label": order.get_internal_status_display(),
        "status_group": _order_group(order),
        "marketplace_status": order.marketplace_status,
        "marketplace_substatus": order.marketplace_substatus,
        "hold_reason": order.hold_reason,
        "problem_reason": order.problem_reason,
        "ordered_at": _format_dt(order.ordered_at),
        "cutoff_at": _format_dt(order.cutoff_at),
        "imported_at": _format_dt(order.imported_at),
        "updated_at": _format_dt(order.updated_at),
        "line_count": int(getattr(order, "line_count", 0) or len(items)),
        "unit_count": int(getattr(order, "unit_count", 0) or sum(item.quantity for item in items)),
    }
    if include_items:
        payload["items"] = [
            {
                "id": item.id,
                "external_sku": item.external_sku,
                "sku_code": item.sku.sku_code if item.sku_id else "",
                "barcode": item.barcode,
                "product_name": item.product_name or (item.sku.name if item.sku_id else ""),
                "quantity": item.quantity,
                "requirements": item.requirements or {},
            }
            for item in items
        ]
    return payload


def client_orders_payload(
    *, agency: Agency, group: str = "all", marketplace: str = "", search: str = "", page: int = 1
) -> dict:
    queryset = (
        FbsOrder.objects.select_related("profile")
        .filter(profile__agency=agency)
        .annotate(line_count=Count("items"), unit_count=Sum("items__quantity"))
        .order_by("-imported_at", "-id")
    )
    if marketplace in {FbsIntegrationProfile.MARKETPLACE_WB, FbsIntegrationProfile.MARKETPLACE_OZON}:
        queryset = queryset.filter(profile__marketplace=marketplace)
    search = str(search or "").strip()
    if search:
        queryset = queryset.filter(
            Q(external_order_id__icontains=search)
            | Q(items__barcode__icontains=search)
            | Q(items__external_sku__icontains=search)
        ).distinct()
    if group == "problem":
        queryset = queryset.filter(_problem_filter())
    elif group == "in_work":
        queryset = queryset.filter(_clean_status_filter(IN_WORK_STATUSES))
    elif group == "completed":
        queryset = queryset.filter(_clean_status_filter(COMPLETED_STATUSES))
    elif group == "canceled":
        queryset = queryset.filter(
            internal_status=FbsOrder.STATUS_CANCELLED,
            hold_reason="",
            problem_reason="",
        )
    elif group == "not_started":
        queryset = queryset.filter(_clean_status_filter(NOT_STARTED_STATUSES)).exclude(
            internal_status__in=PROBLEM_STATUSES
        )
    paginator = Paginator(queryset, 50)
    page_obj = paginator.get_page(page)
    return {
        "results": [serialize_order(order) for order in page_obj.object_list],
        "page": page_obj.number,
        "pages": paginator.num_pages,
        "total": paginator.count,
    }


def client_order_detail(*, agency: Agency, order_id: int) -> dict | None:
    order = (
        FbsOrder.objects.select_related("profile")
        .prefetch_related("items__sku")
        .filter(pk=order_id, profile__agency=agency)
        .first()
    )
    return serialize_order(order, include_items=True) if order else None


def _stock_rows(agency: Agency) -> list[dict]:
    fbs_rows = list(
        FbsStockBalance.objects.filter(agency=agency, qty__gt=0)
        .values("barcode", "sku_ref_id", "sku_code", "name", "size")
        .annotate(
            qty=Sum("qty"),
            available_qty=Sum("available_qty"),
            reserved_qty=Sum("reserved_qty"),
            box_count=Count("box_id", distinct=True),
        )
        .order_by("sku_code", "barcode")
    )
    barcodes = [str(row["barcode"] or "") for row in fbs_rows if row["barcode"]]
    general_map = _quantity_map(_general_stock_queryset(agency), barcodes)
    policies = {
        row.sku_id: row
        for row in FbsReplenishmentPolicy.objects.filter(agency=agency, is_active=True)
    }
    rows = []
    for row in fbs_rows:
        policy = policies.get(row["sku_ref_id"])
        minimum = int(policy.minimum_qty or 0) if policy else 0
        available = int(row["available_qty"] or 0)
        rows.append(
            {
                **row,
                "barcode": str(row["barcode"] or ""),
                "qty": int(row["qty"] or 0),
                "available_qty": available,
                "reserved_qty": int(row["reserved_qty"] or 0),
                "box_count": int(row["box_count"] or 0),
                "general_available_qty": int(general_map.get(str(row["barcode"] or ""), 0)),
                "minimum_qty": minimum,
                "target_qty": int(policy.target_qty or 0) if policy else 0,
                "needs_replenishment": bool(policy and available <= minimum),
            }
        )
    return rows


def client_stock_payload(*, agency: Agency, search: str = "") -> dict:
    rows = _stock_rows(agency)
    needle = str(search or "").strip().casefold()
    if needle:
        rows = [
            row
            for row in rows
            if needle in " ".join(
                [row.get("barcode", ""), row.get("sku_code", ""), row.get("name", "")]
            ).casefold()
        ]
    return {
        "results": rows[:500],
        "total": len(rows),
        "summary": {
            "qty": sum(row["qty"] for row in rows),
            "available_qty": sum(row["available_qty"] for row in rows),
            "reserved_qty": sum(row["reserved_qty"] for row in rows),
            "box_count": sum(row["box_count"] for row in rows),
            "needs_replenishment": sum(1 for row in rows if row["needs_replenishment"]),
        },
    }


def client_stock_detail(*, agency: Agency, barcode: str) -> dict | None:
    barcode = str(barcode or "").strip()
    if not barcode:
        return None
    fbs_rows = list(
        FbsStockBalance.objects.select_related("box__pallet__cell")
        .filter(agency=agency, barcode=barcode, qty__gt=0)
        .order_by("box__pallet__cell__cell_code", "box__box_code", "expiry_date", "id")
    )
    common_order = ["location__location_code", "container_code", "id"]
    if "expiry_date" in WAREHOUSE_SNAPSHOT_FIELDS:
        common_order.insert(0, "expiry_date")
    common_rows = list(
        _general_stock_queryset(agency)
        .select_related("location", "container")
        .filter(barcode=barcode)
        .order_by(*common_order)
    )
    if not fbs_rows and not common_rows:
        return None
    sample = fbs_rows[0] if fbs_rows else common_rows[0]
    return {
        "barcode": barcode,
        "sku_code": str(sample.sku_code or ""),
        "name": str(sample.name or ""),
        "size": str(sample.size or ""),
        "fbs_locations": [
            {
                "cell": row.box.pallet.cell.cell_code,
                "pallet": row.box.pallet.pallet_code,
                "box": row.box.box_code,
                "qty": row.qty,
                "available_qty": row.available_qty,
                "reserved_qty": row.reserved_qty,
                "lot_code": row.lot_code,
                "expiry_date": row.expiry_date.isoformat() if row.expiry_date else "",
            }
            for row in fbs_rows
        ],
        "general_locations": [
            {
                "location": str(getattr(row.location, "location_code", "") or row.zone_code or ""),
                "container": str(row.container_code or getattr(row.container, "container_code", "") or ""),
                "available_qty": row.available_qty,
                "lot_code": _snapshot_lot_code(row),
                "expiry_date": (
                    _snapshot_expiry_date(row).isoformat()
                    if _snapshot_expiry_date(row)
                    else ""
                ),
            }
            for row in common_rows
        ],
    }


def serialize_movement_request(request_row: FbsClientMovementRequest, *, include_lines=False) -> dict:
    lines = list(request_row.lines.select_related("sku").all()) if include_lines else []
    payload = {
        "id": request_row.id,
        "number": request_row.number,
        "mode": request_row.mode,
        "mode_label": request_row.get_mode_display(),
        "status": request_row.status,
        "status_label": request_row.get_status_display(),
        "requested_qty": request_row.requested_qty,
        "requested_box_count": request_row.requested_box_count,
        "requested_mixed_box_count": request_row.requested_mixed_box_count,
        "actual_moved_qty": request_row.actual_moved_qty,
        "actual_moved_box_count": request_row.actual_moved_box_count,
        "line_count": int(getattr(request_row, "line_count", 0) or len(lines)),
        "plan_count": int(
            getattr(request_row, "plan_count", 0)
            or request_row.replenishment_plans.count()
        ),
        "moved_qty": int(
            getattr(request_row, "moved_qty", 0)
            or request_row.replenishment_plans.aggregate(total=Sum("moved_qty"))["total"]
            or 0
        ),
        "comment": request_row.comment,
        "clarification_reason": request_row.clarification_reason,
        "source_file_name": request_row.source_file_name,
        "warehouse_confirmed_at": _format_dt(request_row.warehouse_confirmed_at),
        "manager_confirmed_at": _format_dt(request_row.manager_confirmed_at),
        "created_at": _format_dt(request_row.created_at),
        "updated_at": _format_dt(request_row.updated_at),
        "uses_hard_reserve": request_row.uses_hard_reserve,
        "can_cancel": bool(
            request_row.uses_hard_reserve
            and request_row.status
            in {
                FbsClientMovementRequest.STATUS_SUBMITTED,
                FbsClientMovementRequest.STATUS_APPROVED,
            }
            and not request_row.replenishment_plans.exists()
        ),
    }
    if include_lines:
        used_prepared_boxes = list(
            FbsReplenishmentPreparedBox.objects.filter(
                Q(
                    status__in=(
                        FbsReplenishmentPreparedBox.STATUS_CLOSED,
                        FbsReplenishmentPreparedBox.STATUS_PLACED,
                    )
                )
                | Q(scanned_qty__gt=0),
                plan__client_movement_request=request_row,
            )
            .annotate(
                distinct_barcode_count=Count(
                    "items__allocation__line__barcode",
                    distinct=True,
                )
            )
        )
        payload["actual_packed_box_count"] = len(used_prepared_boxes)
        payload["actual_mixed_box_count"] = sum(
            int(box.distinct_barcode_count or 0) > 1
            for box in used_prepared_boxes
        )
        payload["lines"] = [
            {
                "id": line.id,
                "barcode": line.barcode,
                "sku_code": line.sku_code,
                "product_name": line.product_name,
                "requested_qty": line.requested_qty,
                "units_per_box": line.units_per_box,
                "requested_box_count": line.requested_box_count,
                "general_available_qty_snapshot": line.general_available_qty_snapshot,
                "fbs_available_qty_snapshot": line.fbs_available_qty_snapshot,
            }
            for line in lines
        ]
    return payload


def client_movements_payload(*, agency: Agency) -> dict:
    rows = (
        FbsClientMovementRequest.objects.filter(agency=agency)
        .annotate(
            line_count=Count("lines", distinct=True),
            plan_count=Count("replenishment_plans", distinct=True),
            moved_qty=Sum("replenishment_plans__moved_qty"),
        )
        .order_by("-created_at", "-id")[:200]
    )
    return {"results": [serialize_movement_request(row) for row in rows]}


def client_movement_detail(*, agency: Agency, request_id: int) -> dict | None:
    row = FbsClientMovementRequest.objects.filter(pk=request_id, agency=agency).first()
    return serialize_movement_request(row, include_lines=True) if row else None


def client_reports_payload(*, agency: Agency) -> dict:
    orders = FbsOrder.objects.filter(profile__agency=agency)
    today = timezone.localdate()
    today_start = timezone.make_aware(datetime.combine(today, datetime.min.time()))
    problem_filter = _problem_filter()
    in_work_filter = _clean_status_filter(IN_WORK_STATUSES)
    completed_filter = _clean_status_filter(COMPLETED_STATUSES)
    not_started_filter = _clean_status_filter(NOT_STARTED_STATUSES) & ~Q(
        internal_status__in=PROBLEM_STATUSES
    )

    summary = orders.aggregate(
        total=Count("id"),
        today=Count("id", filter=Q(imported_at__date=today)),
        in_work=Count("id", filter=in_work_filter),
        not_started=Count("id", filter=not_started_filter),
        held_today=Count("id", filter=problem_filter & Q(updated_at__gte=today_start)),
        completed=Count("id", filter=completed_filter),
    )
    marketplace_counts = list(
        orders.values("profile__marketplace")
        .annotate(
            total=Count("id"),
            in_work=Count("id", filter=in_work_filter),
            problem=Count("id", filter=problem_filter),
            completed=Count("id", filter=completed_filter),
        )
        .order_by("profile__marketplace")
    )
    status_counts = list(
        orders.values("internal_status")
        .annotate(count=Count("id"))
        .order_by("-count", "internal_status")
    )

    trend = []
    for offset in range(6, -1, -1):
        day = today - timedelta(days=offset)
        imported = orders.filter(imported_at__date=day).count()
        held = orders.filter(problem_filter, updated_at__date=day).count()
        trend.append({"date": day.strftime("%d.%m"), "orders": imported, "held": held})

    return {
        "summary": {
            key: int(summary.get(key) or 0)
            for key in ("total", "today", "in_work", "not_started", "held_today", "completed")
        },
        "marketplaces": [
            {
                "marketplace": row["profile__marketplace"],
                "label": dict(FbsIntegrationProfile.MARKETPLACE_CHOICES).get(
                    row["profile__marketplace"], row["profile__marketplace"]
                ),
                "total": int(row["total"] or 0),
                "in_work": int(row["in_work"] or 0),
                "problem": int(row["problem"] or 0),
                "completed": int(row["completed"] or 0),
            }
            for row in marketplace_counts
        ],
        "statuses": [
            {
                "label": dict(FbsOrder.STATUS_CHOICES).get(
                    row["internal_status"], row["internal_status"]
                ),
                "count": int(row["count"] or 0),
            }
            for row in status_counts
        ],
        "trend": trend,
        "held_definition": (
            "Ожидает товар, ошибка проверки, исключение, ожидаемый возврат "
            "или заказ с указанной причиной задержки."
        ),
    }


def client_overview_payload(*, agency: Agency) -> dict:
    reports = client_reports_payload(agency=agency)
    stock = client_stock_payload(agency=agency)
    profiles = list(
        FbsIntegrationProfile.objects.filter(agency=agency)
        .order_by("marketplace", "name", "id")
    )
    return {
        "enabled": any(profile.is_active for profile in profiles),
        "profiles": [
            {
                "id": profile.id,
                "marketplace": profile.marketplace,
                "marketplace_label": profile.get_marketplace_display(),
                "name": profile.name,
                "warehouse": profile.external_warehouse_id,
                "is_active": profile.is_active,
                "order_pull_enabled": profile.order_pull_enabled,
            }
            for profile in profiles
        ],
        "orders": reports["summary"],
        "stock": stock["summary"],
        "movement_requests_open": FbsClientMovementRequest.objects.filter(
            agency=agency, status__in=OPEN_CLIENT_MOVEMENT_STATUSES
        ).count(),
    }
