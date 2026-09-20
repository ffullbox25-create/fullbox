from __future__ import annotations

from functools import cached_property
import re

from django.apps import apps
from django.db.models import Q

from audit.models import OrderAuditEntry
from sklad.models import WarehouseContainer, WarehouseStockSnapshot
from sklad.services.warehouse_transitions import WarehouseStateCode

from .models import ShippingOrder


_SHIPPING_WAREHOUSE_FLOW_STATES = [
    WarehouseStateCode.IN_OTG.value,
    WarehouseStateCode.PALLETIZING.value,
    WarehouseStateCode.READY_FOR_LOADING.value,
    WarehouseStateCode.ASSIGNED_TO_TRIP.value,
    WarehouseStateCode.LOADING_IN_PROGRESS.value,
    WarehouseStateCode.LOADED_TO_VEHICLE.value,
    WarehouseStateCode.SHIPPED.value,
]

_SHIPPING_PACKED_STATES = [
    WarehouseStateCode.READY_FOR_LOADING.value,
    WarehouseStateCode.ASSIGNED_TO_TRIP.value,
    WarehouseStateCode.LOADING_IN_PROGRESS.value,
    WarehouseStateCode.LOADED_TO_VEHICLE.value,
    WarehouseStateCode.SHIPPED.value,
]

_PACKING_PALLET_REMOVAL_ACT = "shipping_packing_pallet_removal"


def _normalize_code(value: str | None) -> str:
    return str(value or "").strip()


def _normalize_key(value: str | None) -> str:
    return _normalize_code(value).lower()


def _to_int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _parse_comment_number(comment: str | None, label: str) -> int:
    match = re.search(rf"{re.escape(label)}\s*:\s*(\d+)", str(comment or ""), re.IGNORECASE)
    return max(_to_int(match.group(1)) if match else 0, 0)


def _item_box_numbers(item) -> tuple[int, int]:
    box_count = _parse_comment_number(getattr(item, "comment", ""), "Коробов")
    box_qty = _parse_comment_number(getattr(item, "comment", ""), "кратность")
    requested_qty = max(_to_int(getattr(item, "qty_requested", 0)), 0)
    if box_count <= 0 and box_qty > 0 and requested_qty % box_qty == 0:
        box_count = requested_qty // box_qty
    if box_qty <= 0 and box_count > 0 and requested_qty % box_count == 0:
        box_qty = requested_qty // box_count
    return box_count, box_qty


def _snapshot_box_code(snapshot: WarehouseStockSnapshot) -> str:
    container = snapshot.container
    if container is not None and str(container.container_type or "").strip() == WarehouseContainer.TYPE_BOX:
        return _normalize_code(container.container_code)
    return _normalize_code(snapshot.container_code)


def _packing_summary_box_codes(packing_summary: dict | None) -> set[str]:
    if not isinstance(packing_summary, dict):
        return set()
    codes: set[str] = set()
    for pallet in packing_summary.get("pallets") or []:
        if not isinstance(pallet, dict):
            continue
        for box in pallet.get("boxes") or []:
            if not isinstance(box, dict):
                continue
            code_key = _normalize_key(box.get("box_code") or box.get("code"))
            if code_key:
                codes.add(code_key)
    return codes


class ShippingTruthService:
    def __init__(self, order: ShippingOrder):
        self.order = order
        self.order_key = _normalize_code(order.number)

    @classmethod
    def for_order(cls, order: ShippingOrder) -> "ShippingTruthService":
        cache_key = "_shipping_truth_service_cache"
        cached = getattr(order, cache_key, None)
        if cached is None:
            cached = cls(order)
            setattr(order, cache_key, cached)
        return cached

    @cached_property
    def delivered_claim_box_codes(self) -> set[str]:
        if not self.order_key:
            return set()
        try:
            BoxClaim = apps.get_model("reachtruck", "BoxClaim")
        except LookupError:
            return set()
        codes: set[str] = set()
        for raw in (
            BoxClaim.objects.filter(
                agency=self.order.agency,
                status=BoxClaim.STATUS_DELIVERED,
                claim_kind=BoxClaim.KIND_BOX,
            )
            .filter(Q(shipping_order_id=self.order_key) | Q(shipping_order_pk=self.order.pk))
            .exclude(box_code__isnull=True)
            .values_list("box_code", flat=True)
        ):
            code = _normalize_code(raw)
            if code:
                codes.add(code)
        return codes

    @cached_property
    def warehouse_snapshots(self) -> list[WarehouseStockSnapshot]:
        if not self.order_key:
            return []
        base_qs = WarehouseStockSnapshot.objects.select_related(
            "container", "parent_container", "last_event"
        ).filter(
            agency=self.order.agency,
            is_archived=False,
            warehouse_state_code__in=_SHIPPING_WAREHOUSE_FLOW_STATES,
        )
        snapshots = list(
            base_qs.filter(
                last_event__stock_context_type="shipping",
                last_event__stock_context_id=self.order_key,
            ).order_by("id")
        )
        claim_codes = self.delivered_claim_box_codes
        if claim_codes:
            claim_snapshots = list(
                base_qs.filter(
                    Q(container_code__in=claim_codes) | Q(container__container_code__in=claim_codes)
                ).order_by("id")
            )
            for snapshot in claim_snapshots:
                last_event = snapshot.last_event
                current_context_type = _normalize_key(
                    getattr(last_event, "stock_context_type", "")
                )
                current_context_id = _normalize_code(
                    getattr(last_event, "stock_context_id", "")
                )
                if (
                    current_context_type == "shipping"
                    and current_context_id
                    and current_context_id != self.order_key
                    and self.order.status != ShippingOrder.STATUS_CANCELED
                ):
                    continue
                snapshots.append(snapshot)
        seen: set[int] = set()
        unique: list[WarehouseStockSnapshot] = []
        for snapshot in snapshots:
            if snapshot.id in seen:
                continue
            seen.add(snapshot.id)
            unique.append(snapshot)
        return unique

    @cached_property
    def warehouse_box_codes(self) -> set[str]:
        codes: set[str] = set()
        for snapshot in self.warehouse_snapshots:
            code_key = _normalize_key(_snapshot_box_code(snapshot))
            if code_key:
                codes.add(code_key)
        return codes

    @cached_property
    def packed_box_codes(self) -> set[str]:
        codes: set[str] = set()
        for snapshot in self.warehouse_snapshots:
            if str(snapshot.warehouse_state_code or "").strip() not in _SHIPPING_PACKED_STATES:
                continue
            parent = snapshot.parent_container
            if parent is None:
                continue
            if str(parent.source_context_type or "").strip() != "shipping":
                continue
            if _normalize_code(parent.source_context_id) != self.order_key:
                continue
            code_key = _normalize_key(_snapshot_box_code(snapshot))
            if code_key:
                codes.add(code_key)
        return codes

    @cached_property
    def approved_final_box_count(self) -> int:
        payload = self.order.shipping_discrepancy_payload
        if not isinstance(payload, dict):
            return 0
        status = _normalize_key(self.order.shipping_discrepancy_status) or _normalize_key(payload.get("status"))
        if status not in {"approved", "resolved"}:
            return 0
        for key in ("final_otg_box_count", "new_expected_boxes"):
            count = _to_int(payload.get(key))
            if count > 0:
                return count
        return 0

    @cached_property
    def approved_removed_box_codes(self) -> set[str]:
        if not self.order_key:
            return set()
        codes: set[str] = set()
        entries = (
            OrderAuditEntry.objects.filter(order_type="shipping", order_id=self.order_key)
            .only("payload")
            .order_by("created_at", "id")
        )
        for entry in entries:
            payload = entry.payload if isinstance(entry.payload, dict) else {}
            if _normalize_key(payload.get("act")) != _PACKING_PALLET_REMOVAL_ACT:
                continue
            if _normalize_key(payload.get("removal_status")) != "approved":
                continue
            for raw_code in payload.get("box_codes") or []:
                code_key = _normalize_key(raw_code)
                if code_key:
                    codes.add(code_key)
            for box in payload.get("boxes") or []:
                if not isinstance(box, dict):
                    continue
                code_key = _normalize_key(box.get("box_code") or box.get("code"))
                if code_key:
                    codes.add(code_key)
        return codes

    @property
    def expected_box_count(self) -> int:
        approved_count = self.approved_final_box_count
        if approved_count > 0:
            return approved_count
        try:
            return int(self.order.expected_boxes or 0)
        except (TypeError, ValueError):
            return 0

    @cached_property
    def delivered_box_counts_by_pattern(self) -> dict[tuple[str, str, str, int], int]:
        counts: dict[tuple[str, str, str, int], int] = {}
        for snapshot in self.warehouse_snapshots:
            box_qty = _to_int(getattr(snapshot, "qty", 0))
            if box_qty <= 0:
                continue
            key = (
                _normalize_key(getattr(snapshot, "barcode", "")),
                _normalize_key(getattr(snapshot, "size", "")),
                _normalize_key(getattr(snapshot, "goods_type", "")),
                box_qty,
            )
            if not key[0]:
                continue
            counts[key] = counts.get(key, 0) + 1
        return counts

    @cached_property
    def quantity_mismatch_rows(self) -> list[dict]:
        rows_by_barcode: dict[str, dict] = {}
        for item in self.order.items.all().order_by("id"):
            barcode = _normalize_code(getattr(item, "barcode", ""))
            key = _normalize_key(barcode)
            if not key:
                continue
            row = rows_by_barcode.setdefault(
                key,
                {
                    "shipping_item_id": _to_int(getattr(item, "id", 0)),
                    "sku_code": _normalize_code(getattr(item, "sku_code", "")),
                    "name": _normalize_code(getattr(item, "name", "")),
                    "size": _normalize_code(getattr(item, "size", "")),
                    "barcode": barcode,
                    "goods_type": _normalize_code(getattr(item, "goods_type", "")),
                    "requested_qty": 0,
                    "fact_qty": 0,
                },
            )
            row["requested_qty"] += max(_to_int(getattr(item, "qty_requested", 0)), 0)

        for snapshot in self.warehouse_snapshots:
            barcode = _normalize_code(getattr(snapshot, "barcode", ""))
            key = _normalize_key(barcode)
            if not key:
                continue
            row = rows_by_barcode.setdefault(
                key,
                {
                    "shipping_item_id": 0,
                    "sku_code": _normalize_code(getattr(snapshot, "sku_code", "")),
                    "name": _normalize_code(getattr(snapshot, "name", "")),
                    "size": _normalize_code(getattr(snapshot, "size", "")),
                    "barcode": barcode,
                    "goods_type": _normalize_code(getattr(snapshot, "goods_type", "")),
                    "requested_qty": 0,
                    "fact_qty": 0,
                },
            )
            row["fact_qty"] += max(_to_int(getattr(snapshot, "qty", 0)), 0)

        mismatches: list[dict] = []
        for row in rows_by_barcode.values():
            requested_qty = max(_to_int(row.get("requested_qty")), 0)
            fact_qty = max(_to_int(row.get("fact_qty")), 0)
            if requested_qty == fact_qty:
                continue
            missing_qty = max(requested_qty - fact_qty, 0)
            excess_qty = max(fact_qty - requested_qty, 0)
            result = dict(row)
            result.update(
                {
                    "marketplace_qty": requested_qty,
                    "missing_qty": missing_qty,
                    "excess_qty": excess_qty,
                    "mismatch_label": (
                        f"Не хватает {missing_qty} шт."
                        if missing_qty > 0
                        else f"В OTG больше на {excess_qty} шт."
                    ),
                }
            )
            mismatches.append(result)
        return mismatches

    @cached_property
    def missing_box_rows(self) -> list[dict]:
        expected_by_pattern: dict[tuple[str, str, str, int], dict] = {}
        for item in self.order.items.all().order_by("id"):
            expected_boxes, box_qty = _item_box_numbers(item)
            if expected_boxes <= 0 or box_qty <= 0:
                continue
            key = (
                _normalize_key(getattr(item, "barcode", "")),
                _normalize_key(getattr(item, "size", "")),
                _normalize_key(getattr(item, "goods_type", "")),
                box_qty,
            )
            row = expected_by_pattern.setdefault(
                key,
                {
                    "sku_code": _normalize_code(getattr(item, "sku_code", "")),
                    "name": _normalize_code(getattr(item, "name", "")),
                    "barcode": _normalize_code(getattr(item, "barcode", "")),
                    "goods_type": _normalize_code(getattr(item, "goods_type", "")),
                    "box_qty": box_qty,
                    "expected_boxes": 0,
                },
            )
            row["expected_boxes"] += expected_boxes

        rows: list[dict] = []
        for key, row in expected_by_pattern.items():
            delivered_boxes = self.delivered_box_counts_by_pattern.get(key, 0)
            missing_boxes = max(_to_int(row.get("expected_boxes")) - delivered_boxes, 0)
            if missing_boxes <= 0:
                continue
            result = dict(row)
            result["missing_boxes"] = missing_boxes
            result["missing_qty"] = missing_boxes * _to_int(row.get("box_qty"))
            rows.append(result)
        return rows

    def unassigned_for_packing(self, assigned_box_codes: set[str]) -> set[str]:
        assigned = {_normalize_key(code) for code in assigned_box_codes if _normalize_key(code)}
        return self.warehouse_box_codes - assigned

    def integrity_warnings(self, *, packing_summary: dict | None = None) -> list[str]:
        warnings: list[str] = []
        if self.order.status == ShippingOrder.STATUS_SHIPPED:
            return warnings

        warehouse_codes = set(self.warehouse_box_codes)

        if warehouse_codes and self.quantity_mismatch_rows:
            mismatch_details = "; ".join(
                (
                    f"ШК {row['barcode']}: в заявке {row['requested_qty']} шт, "
                    f"факт OTG {row['fact_qty']} шт ({row['mismatch_label']})"
                )
                for row in self.quantity_mismatch_rows[:5]
            )
            warnings.append(
                "Расхождение отгрузки по ШК и количеству: "
                f"{mismatch_details}."
            )

        summary_codes = _packing_summary_box_codes(packing_summary)
        if summary_codes:
            missing_from_packing = sorted(warehouse_codes - summary_codes)
            extra_in_packing = sorted(summary_codes - warehouse_codes)
            if missing_from_packing:
                warnings.append(
                    "\u041d\u0435\u043f\u043e\u043b\u043d\u0430\u044f \u043f\u0430\u043b\u043b\u0435\u0442\u0438\u0437\u0430\u0446\u0438\u044f: "
                    f"\u0432 OTG {len(warehouse_codes)} \u043a\u043e\u0440\u043e\u0431\u043e\u0432, "
                    f"\u0432 \u0430\u043a\u0442\u0435 {len(summary_codes)}, "
                    f"\u043d\u0435 \u0440\u0430\u0437\u043b\u043e\u0436\u0435\u043d\u043e {len(missing_from_packing)}."
                )
            if extra_in_packing:
                warnings.append(
                    "\u0420\u0430\u0441\u0445\u043e\u0436\u0434\u0435\u043d\u0438\u0435 \u043f\u0430\u043b\u043b\u0435\u0442\u0438\u0437\u0430\u0446\u0438\u0438: "
                    f"{len(extra_in_packing)} \u043a\u043e\u0440\u043e\u0431\u043e\u0432 \u0435\u0441\u0442\u044c \u0432 \u0430\u043a\u0442\u0435, "
                    "\u043d\u043e \u043d\u0435\u0442 \u0432 OTG."
                )
        return warnings
