from __future__ import annotations

import hashlib
from collections import defaultdict
from dataclasses import dataclass

from django.db import transaction
from django.utils import timezone

from audit.models import OrderAuditEntry, log_order_action
from orders.services import ReceivingWorkflowService
from sklad.models import WarehouseContainer, WarehouseStockSnapshot
from sklad.services.warehouse_transitions import WarehouseTransitionError
from sklad.services.warehouse_write_path import WarehouseWritePathService
from sku.models import Agency

from fbs.exceptions import FbsReplenishmentError
from fbs.goods_types import (
    is_fbs_client_movement_source_stock,
    receiving_placement_allowed_snapshot_ids,
)
from fbs.models import (
    FbsClientMovementRequest,
    FbsClientMovementRequestLine,
    FbsReplenishmentLine,
    FbsStockBalance,
)

from .client_movements import (
    RECEIVING_SOURCE_FILE_PREFIX,
    WAREHOUSE_ROLES,
    _require_actor_role,
    accept_client_movement_request,
)
from .receiving_destinations import allocate_selected_receiving_fbs_pallet


ACTIVE_SOURCE_LINE_STATUSES = (
    FbsReplenishmentLine.STATUS_PROPOSED,
    FbsReplenishmentLine.STATUS_RESERVED,
    FbsReplenishmentLine.STATUS_IN_PROGRESS,
)


@dataclass(frozen=True)
class ReceivingPalletArticleOption:
    sku_id: int
    sku_code: str
    product_name: str
    barcode: str
    box_codes: tuple[str, ...]
    box_count: int
    qty: int


@dataclass(frozen=True)
class ReceivingPalletMovementOption:
    agency_id: int
    agency_name: str
    order_id: str
    pallet_code: str
    box_codes: tuple[str, ...]
    box_count: int
    articles: tuple[ReceivingPalletArticleOption, ...]
    article_count: int
    position_count: int
    qty: int
    created_at: object


def _agency_name(agency: Agency) -> str:
    return str(agency.short_name or agency.agn_name or agency.fio_agn or agency)


def _base_snapshot_queryset(*, agency_id=None, order_id: str = ""):
    queryset = (
        WarehouseStockSnapshot.objects.filter(
            source_context_type="receiving",
            zone_code__iexact="PR",
            warehouse_state_code="placed_in_receiving",
            is_archived=False,
            is_in_vehicle=False,
            qty__gt=0,
            available_qty__gt=0,
            processing_reserved_qty=0,
            shipping_reserved_qty=0,
            other_reserved_qty=0,
            active_operation__isnull=True,
            container__container_type=WarehouseContainer.TYPE_BOX,
            container__status=WarehouseContainer.STATUS_ACTIVE,
            container__container_code__iendswith="-gv",
            parent_container__container_type__in=(
                WarehouseContainer.TYPE_PALLET,
                WarehouseContainer.TYPE_MIXED_PALLET,
            ),
            parent_container__status=WarehouseContainer.STATUS_ACTIVE,
        )
        .exclude(source_context_id="")
        .select_related(
            "agency",
            "sku_ref",
            "container",
            "parent_container",
            "location",
        )
        .order_by("agency_id", "source_context_id", "parent_container_id", "container_id", "id")
    )
    if agency_id:
        queryset = queryset.filter(agency_id=int(agency_id))
    if order_id:
        queryset = queryset.filter(source_context_id=str(order_id).strip())
    return queryset


def _eligible_receiving_groups(*, agency_id=None, order_id: str = "", lock: bool = False):
    queryset = _base_snapshot_queryset(agency_id=agency_id, order_id=order_id)
    if lock:
        # ``_base_snapshot_queryset`` joins nullable relations (for example,
        # ``location``) for the read model.  PostgreSQL cannot apply a blanket
        # FOR UPDATE to the nullable side of those outer joins, so lock only
        # the inventory rows that this command is going to reserve.
        queryset = queryset.select_for_update(of=("self",))
    snapshots = list(queryset)
    if not snapshots:
        return {}

    allowed_ids = receiving_placement_allowed_snapshot_ids(snapshots)
    shipping_blocked_ids = set()
    snapshots_by_agency = defaultdict(list)
    for snapshot in snapshots:
        snapshots_by_agency[int(snapshot.agency_id)].append(snapshot)
    for owner_id, owner_snapshots in snapshots_by_agency.items():
        shipping_blocked_ids.update(
            WarehouseWritePathService.shipping_reserved_snapshot_ids(
                agency=owner_id,
                snapshots=owner_snapshots,
            )
        )
    busy_container_ids = set(
        FbsReplenishmentLine.objects.filter(
            source_container_id__in={row.container_id for row in snapshots},
            status__in=ACTIVE_SOURCE_LINE_STATUSES,
        ).values_list("source_container_id", flat=True)
    )

    grouped_by_container: dict[int, list[WarehouseStockSnapshot]] = defaultdict(list)
    for snapshot in snapshots:
        grouped_by_container[int(snapshot.container_id)].append(snapshot)

    all_positive_by_container: dict[int, list[WarehouseStockSnapshot]] = defaultdict(list)
    all_positive = WarehouseStockSnapshot.objects.filter(
        container_id__in=grouped_by_container,
        is_archived=False,
        qty__gt=0,
    ).select_related("container", "parent_container", "location", "sku_ref")
    if lock:
        all_positive = all_positive.select_for_update(of=("self",))
    for snapshot in all_positive.order_by("container_id", "id"):
        all_positive_by_container[int(snapshot.container_id)].append(snapshot)

    eligible_containers: dict[int, tuple[WarehouseStockSnapshot, ...]] = {}
    for container_id, candidate_rows in grouped_by_container.items():
        rows = tuple(all_positive_by_container.get(container_id) or ())
        if not rows or container_id in busy_container_ids:
            continue
        agency_ids = {int(row.agency_id) for row in rows}
        order_ids = {str(row.source_context_id or "").strip() for row in rows}
        parent_ids = {int(row.parent_container_id or 0) for row in rows}
        if len(agency_ids) != 1 or len(order_ids) != 1 or len(parent_ids) != 1 or 0 in parent_ids:
            continue
        exact = all(
            row.id in allowed_ids
            and row.id not in shipping_blocked_ids
            and str(row.source_context_type or "").strip().casefold() == "receiving"
            and str(row.zone_code or "").strip().upper() == "PR"
            and str(row.warehouse_state_code or "").strip() == "placed_in_receiving"
            and int(row.available_qty or 0) == int(row.qty or 0)
            and int(row.processing_reserved_qty or 0) == 0
            and int(row.shipping_reserved_qty or 0) == 0
            and int(row.other_reserved_qty or 0) == 0
            and row.active_operation_id is None
            and not row.is_in_vehicle
            and row.sku_ref_id is not None
            and row.sku_ref.agency_id == row.agency_id
            and is_fbs_client_movement_source_stock(
                snapshot=row,
                receiving_allowed_ids=allowed_ids,
                goods_type=row.goods_type,
                zone_kind=row.zone_kind,
                warehouse_state_code=row.warehouse_state_code,
                container_type=row.container.container_type,
                container_code=row.container.container_code,
                container_status=row.container.status,
            )
            and str(row.container.container_code or "").strip().casefold().endswith("-gv")
            for row in rows
        )
        if exact:
            eligible_containers[container_id] = rows

    grouped = defaultdict(list)
    for container_id, rows in eligible_containers.items():
        first = rows[0]
        grouped[
            (
                int(first.agency_id),
                str(first.source_context_id or "").strip(),
                int(first.parent_container_id),
            )
        ].append((first.container, rows))
    return grouped


def receiving_pallet_movement_options(
    *,
    agency_id=None,
    order_id: str = "",
) -> tuple[ReceivingPalletMovementOption, ...]:
    groups = _eligible_receiving_groups(agency_id=agency_id, order_id=order_id)
    options = []
    for (_owner_id, source_order_id, _pallet_id), boxes in groups.items():
        rows = [snapshot for _box, snapshots in boxes for snapshot in snapshots]
        first = rows[0]
        article_groups: dict[tuple[int, str], list[WarehouseStockSnapshot]] = defaultdict(list)
        for row in rows:
            article_groups[(int(row.sku_ref_id), str(row.barcode or "").strip())].append(row)
        articles = []
        for (_sku_id, barcode), article_rows in article_groups.items():
            article_first = article_rows[0]
            sku = article_first.sku_ref
            article_box_codes = tuple(
                sorted(
                    {
                        str(row.container.container_code or "").strip()
                        for row in article_rows
                    },
                    key=str.casefold,
                )
            )
            articles.append(
                ReceivingPalletArticleOption(
                    sku_id=int(sku.id),
                    sku_code=str(sku.sku_code or article_first.sku_code or "—").strip(),
                    product_name=str(sku.name or article_first.name or "—").strip(),
                    barcode=barcode or "—",
                    box_codes=article_box_codes,
                    box_count=len(article_box_codes),
                    qty=sum(int(row.qty or 0) for row in article_rows),
                )
            )
        articles = tuple(
            sorted(
                articles,
                key=lambda row: (
                    row.sku_code.casefold(),
                    row.product_name.casefold(),
                    row.barcode.casefold(),
                ),
            )
        )
        options.append(
            ReceivingPalletMovementOption(
                agency_id=first.agency_id,
                agency_name=_agency_name(first.agency),
                order_id=source_order_id,
                pallet_code=str(first.parent_container.container_code or "").strip(),
                box_codes=tuple(sorted(str(box.container_code or "").strip() for box, _rows in boxes)),
                box_count=len(boxes),
                articles=articles,
                article_count=len({row.sku_id for row in articles}),
                position_count=len(articles),
                qty=sum(int(row.qty or 0) for row in rows),
                created_at=min(row.created_at for row in rows),
            )
        )
    return tuple(
        sorted(
            options,
            key=lambda row: (
                row.agency_name.casefold(),
                row.order_id.casefold(),
                row.pallet_code.casefold(),
            ),
        )
    )


def _flow_pallet_is_ready(*, agency_id: int, order_id: str, pallet_code: str) -> bool:
    entries = list(
        OrderAuditEntry.objects.filter(
            agency_id=agency_id,
            order_type="receiving",
            order_id=order_id,
        ).order_by("created_at", "id")
    )
    if not entries:
        return False
    flow_state = ReceivingWorkflowService.find_receiving_flow_state(entries)
    statuses = ReceivingWorkflowService.build_receiving_flow_pallet_takeout_statuses(
        order_id,
        flow_state.get("pallets") if isinstance(flow_state, dict) else [],
    )
    status = statuses.get(pallet_code) or next(
        (
            value
            for code, value in statuses.items()
            if str(code or "").strip().casefold() == pallet_code.casefold()
        ),
        {},
    )
    return str(status.get("status") or "").strip().casefold() == "ready"


@transaction.atomic
def create_receiving_fbs_movement_request(
    *,
    agency_id: int,
    order_id: str,
    pallet_codes,
    created_by=None,
) -> FbsClientMovementRequest:
    actor = _require_actor_role(
        created_by,
        WAREHOUSE_ROLES,
        "Создать FBS-перемещение из приемки может только сотрудник склада.",
    )
    order_key = str(order_id or "").strip()
    selected_codes = sorted(
        {
            str(code or "").strip()
            for code in (pallet_codes or [])
            if str(code or "").strip()
        },
        key=str.casefold,
    )
    if not order_key:
        raise FbsReplenishmentError("Выберите заявку приемки.")
    if not selected_codes:
        raise FbsReplenishmentError("Выберите хотя бы одну разрешенную палету приемки.")

    agency = Agency.objects.select_for_update().get(pk=int(agency_id))
    selected_token = "|".join(code.casefold() for code in selected_codes)
    digest = hashlib.sha256(
        f"{agency.id}|{order_key.casefold()}|{selected_token}".encode("utf-8")
    ).hexdigest()[:32]
    idempotency_key = f"receiving-{digest}"
    existing = (
        FbsClientMovementRequest.objects.select_for_update()
        .filter(agency=agency, idempotency_key=idempotency_key)
        .first()
    )
    if existing is not None:
        return existing

    groups = _eligible_receiving_groups(
        agency_id=agency.id,
        order_id=order_key,
        lock=True,
    )
    selected_groups = {}
    for (_owner_id, source_order_id, _pallet_id), boxes in groups.items():
        if source_order_id != order_key or not boxes:
            continue
        pallet_code = str(boxes[0][1][0].parent_container.container_code or "").strip()
        selected_groups[pallet_code.casefold()] = boxes
    missing = [code for code in selected_codes if code.casefold() not in selected_groups]
    if missing:
        raise FbsReplenishmentError(
            "Палеты уже недоступны для FBS-перемещения: " + ", ".join(missing) + "."
        )
    for code in selected_codes:
        if not _flow_pallet_is_ready(
            agency_id=agency.id,
            order_id=order_key,
            pallet_code=code,
        ):
            raise FbsReplenishmentError(
                f"Палета {code} не закрыта, не разрешена к размещению или уже перемещается."
            )

    selected_boxes = []
    selected_snapshots = []
    for code in selected_codes:
        for box, rows in selected_groups[code.casefold()]:
            selected_boxes.append(box)
            selected_snapshots.extend(rows)
    if not selected_boxes or not selected_snapshots:
        raise FbsReplenishmentError("В выбранных палетах нет доступных GV-коробов.")

    snapshots_by_barcode: dict[str, list[WarehouseStockSnapshot]] = defaultdict(list)
    boxes_by_barcode: dict[str, set[int]] = defaultdict(set)
    box_barcodes: dict[int, set[str]] = defaultdict(set)
    for snapshot in selected_snapshots:
        barcode = str(snapshot.barcode or "").strip()
        if not barcode:
            raise FbsReplenishmentError(
                f"В коробе {snapshot.container.container_code} есть товар без штрихкода."
            )
        snapshots_by_barcode[barcode].append(snapshot)
        boxes_by_barcode[barcode].add(int(snapshot.container_id))
        box_barcodes[int(snapshot.container_id)].add(barcode)

    mixed_box_ids = {container_id for container_id, barcodes in box_barcodes.items() if len(barcodes) > 1}
    request_row = FbsClientMovementRequest(
        agency=agency,
        mode=FbsClientMovementRequest.MODE_BOX,
        status=FbsClientMovementRequest.STATUS_APPROVED,
        requested_by=actor,
        reviewed_by=actor,
        reviewed_at=timezone.now(),
        source_file_name=f"{RECEIVING_SOURCE_FILE_PREFIX}{order_key}"[:255],
        idempotency_key=idempotency_key,
        comment=(
            f"Из приемки {order_key}; разрешенные палеты: "
            + ", ".join(selected_codes)
        ),
        requested_qty=sum(int(row.qty or 0) for row in selected_snapshots),
        requested_box_count=len(selected_boxes),
        # Box-mode movements transfer every selected physical box as-is.  The
        # legacy request field is reserved for item-mode packing plans.
        requested_mixed_box_count=0,
    )
    request_row.full_clean()
    request_row.save()

    fbs_by_barcode = {
        str(row["barcode"]): int(row["qty"] or 0)
        for row in FbsStockBalance.objects.filter(
            agency=agency,
            barcode__in=snapshots_by_barcode,
        ).values("barcode", "qty")
    }
    lines_by_barcode = {}
    for barcode, rows in sorted(snapshots_by_barcode.items()):
        sku_ids = {int(row.sku_ref_id or 0) for row in rows}
        if len(sku_ids) != 1 or 0 in sku_ids:
            raise FbsReplenishmentError(
                f"ШК {barcode} в приемке сопоставлен неоднозначно. Исправьте номенклатуру."
            )
        sku = rows[0].sku_ref
        monobox_ids = boxes_by_barcode[barcode] - mixed_box_ids
        monobox_sizes = {
            sum(
                int(item.qty or 0)
                for item in selected_snapshots
                if int(item.container_id) == container_id and str(item.barcode or "").strip() == barcode
            )
            for container_id in monobox_ids
        }
        requested_qty = sum(int(row.qty or 0) for row in rows)
        has_mixed_source = bool(boxes_by_barcode[barcode] & mixed_box_ids)
        units_per_box = (
            next(iter(monobox_sizes))
            if len(monobox_sizes) == 1 and not has_mixed_source
            else 1
        )
        line = FbsClientMovementRequestLine(
            request=request_row,
            sku=sku,
            barcode=barcode,
            sku_code=str(sku.sku_code or ""),
            product_name=str(sku.name or ""),
            requested_qty=requested_qty,
            units_per_box=max(int(units_per_box or 1), 1),
            requested_box_count=len(monobox_ids),
            general_available_qty_snapshot=requested_qty,
            fbs_available_qty_snapshot=int(fbs_by_barcode.get(barcode, 0)),
        )
        line.full_clean()
        line.save()
        lines_by_barcode[barcode] = line

    allocations = []
    for snapshot in selected_snapshots:
        barcode = str(snapshot.barcode or "").strip()
        allocations.append(
            {
                "snapshot_id": snapshot.id,
                "request_line_id": lines_by_barcode[barcode].id,
                "qty": int(snapshot.qty or 0),
                "container_id": snapshot.container_id,
                "container_code": str(snapshot.container.container_code or "").strip(),
                "barcode": barcode,
                "sku_id": snapshot.sku_ref_id,
            }
        )
    try:
        WarehouseWritePathService.reserve_for_fbs_movement(
            agency=agency,
            request_id=request_row.id,
            whole_container_ids=[box.id for box in selected_boxes],
            allocations=allocations,
            created_by=actor,
        )
    except WarehouseTransitionError as exc:
        raise FbsReplenishmentError(str(exc)) from exc

    log_order_action(
        "create",
        order_id=request_row.number,
        order_type="fbs_movement",
        user=actor,
        agency=agency,
        description=(
            f"Кладовщик создал FBS-перемещение из частично принятой заявки {order_key}; "
            "зарезервированы только выбранные разрешенные палеты"
        ),
        payload={
            "status": request_row.status,
            "status_label": request_row.get_status_display(),
            "request_id": request_row.id,
            "source_receiving_order_id": order_key,
            "source_pallet_codes": selected_codes,
            "source_box_codes": sorted(
                str(box.container_code or "").strip() for box in selected_boxes
            ),
            "requested_qty": request_row.requested_qty,
            "requested_box_count": request_row.requested_box_count,
        },
    )
    return request_row


def _exact_route_payload(request_row, plans) -> dict:
    targets = []
    seen = set()
    for plan in plans or ():
        pallet = getattr(plan, "target_pallet", None)
        cell = getattr(pallet, "cell", None)
        location = getattr(cell, "location", None)
        if pallet is None or cell is None or location is None:
            continue
        target_key = int(pallet.id)
        if target_key in seen:
            continue
        seen.add(target_key)
        targets.append(
            {
                "pallet_id": target_key,
                "cell_id": int(cell.id),
                "pallet_code": str(pallet.pallet_code or "").strip(),
                "cell_code": str(cell.warehouse_location_code or "").strip(),
                "cell_label": str(cell.warehouse_location_label or "").strip(),
                "zone_code": str(location.zone_code or "").strip().upper(),
                "exact": str(location.zone_kind or "").strip()
                != str(location.ZONE_KIND_VIRTUAL),
            }
        )
    exact_targets = [target for target in targets if target.get("exact")]
    return {
        "status": "assigned" if targets and len(exact_targets) == len(targets) else "blocked",
        "request_id": int(request_row.id),
        "request_number": str(request_row.number or "").strip(),
        "targets": targets,
        "message": (
            "Точное FBS-место назначено."
            if targets and len(exact_targets) == len(targets)
            else "Точное FBS-место пока не назначено."
        ),
    }


@transaction.atomic
def auto_route_receiving_pallet_to_fbs(
    *,
    agency_id: int,
    order_id: str,
    pallet_code: str,
    fbs_cell_id=None,
    user=None,
) -> dict:
    """Route to the storekeeper's choice; retained name for existing callers."""
    try:
        selected_cell_id = int(fbs_cell_id)
    except (TypeError, ValueError):
        raise FbsReplenishmentError("Кладовщик должен выбрать конечное FBS-место для паллеты.")
    if selected_cell_id <= 0:
        raise FbsReplenishmentError("Выберите конечное FBS-место для паллеты.")
    request_row = create_receiving_fbs_movement_request(
        agency_id=int(agency_id),
        order_id=str(order_id or "").strip(),
        pallet_codes=[str(pallet_code or "").strip()],
        created_by=user,
    )
    existing_plans = tuple(
        request_row.replenishment_plans.select_related(
            "target_pallet__cell__location"
        ).order_by("id")
    )
    if existing_plans:
        if any(plan.target_pallet.cell_id != selected_cell_id for plan in existing_plans):
            raise FbsReplenishmentError(
                "Для паллеты уже создано перемещение в другое FBS-место. "
                "Существующее задание не изменено; изменение выполняется через перемещения FBS."
            )
        return _exact_route_payload(request_row, existing_plans)

    target_pallet = allocate_selected_receiving_fbs_pallet(
        agency=request_row.agency,
        cell_id=selected_cell_id,
        required_box_slots=max(int(request_row.requested_box_count or 0), 1),
    )
    approval = accept_client_movement_request(
        request_id=int(request_row.id),
        accepted_by=user,
        target_pallet_id=int(target_pallet.id),
    )
    # Reload with target relations without relying on the result object's cache.
    plans = tuple(
        request_row.replenishment_plans.select_related(
            "target_pallet__cell__location"
        ).order_by("id")
    )
    return _exact_route_payload(approval.request, plans)


def receiving_fbs_route_rows(*, agency_id: int, order_id: str) -> list[dict]:
    requests = (
        FbsClientMovementRequest.objects.filter(
            agency_id=int(agency_id),
            source_file_name=f"{RECEIVING_SOURCE_FILE_PREFIX}{str(order_id or '').strip()}",
        )
        .prefetch_related("replenishment_plans__target_pallet__cell__location")
        .order_by("id")
    )
    rows = []
    seen = set()
    for request_row in requests:
        comment = str(request_row.comment or "")
        source_codes = []
        marker = "разрешенные палеты:"
        if marker in comment.casefold():
            source_text = comment[comment.casefold().index(marker) + len(marker):]
            source_codes = [code.strip() for code in source_text.split(",") if code.strip()]
        for target in _exact_route_payload(
            request_row,
            request_row.replenishment_plans.all(),
        ).get("targets", []):
            key = (int(request_row.id), int(target.get("pallet_id") or 0))
            if key in seen:
                continue
            seen.add(key)
            rows.append(
                {
                    **target,
                    "request_id": int(request_row.id),
                    "request_number": str(request_row.number or "").strip(),
                    "request_status": str(request_row.status or "").strip(),
                    "source_pallet_code": source_codes[0] if len(source_codes) == 1 else "",
                }
            )
    return rows
