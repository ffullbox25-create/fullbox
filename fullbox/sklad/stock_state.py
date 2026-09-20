from __future__ import annotations

from django.db.models import Sum

from sku.models import Agency

from .models import WarehouseReserve, WarehouseStockSnapshot


def _normalize_goods_type(value: str | None) -> str:
    text = str(value or "").strip().lower()
    aliases = {
        "op": "оптовый",
        "gv": "готовый",
        "no": "не обработанный",
        "br": "брак",
        "vz": "возврат",
        "votg": "возврат с отгрузки",
        "rh": "расходный",
    }
    return aliases.get(text, text)


def _goods_type_label(order_type: str, payload: dict | None) -> str:
    payload = payload if isinstance(payload, dict) else {}
    value = str(payload.get("goods_type_label") or payload.get("goods_type") or "").strip()
    if value:
        return value
    return "Готовый" if str(order_type or "").strip() == "processing" else "Оптовый"


def _inventory_key(sku: str | None, size: str | None, goods_type: str | None) -> tuple[str, str, str]:
    return (
        str(sku or "").strip().lower(),
        str(size or "").strip().lower(),
        _normalize_goods_type(goods_type),
    )


def _normalize_location(location: dict | None) -> dict:
    location = location if isinstance(location, dict) else {}
    return {
        "zone": str(location.get("zone") or "").strip().upper(),
        "row": int(location.get("row") or 0),
        "section": int(location.get("section") or 0),
        "tier": int(location.get("tier") or 0),
        "cell": int(location.get("cell") or 0),
    }


def _ensure_pallet_location(pallet: dict | None, default_zone: str = "PR") -> dict:
    pallet = pallet if isinstance(pallet, dict) else {}
    location = pallet.get("location") if isinstance(pallet.get("location"), dict) else pallet
    normalized = _normalize_location(location)
    if not normalized["zone"]:
        normalized["zone"] = str(default_zone or "PR").strip().upper()
    return normalized


def _receiving_removed_qty_map(entries) -> dict:
    return {}


def _shipping_shipped_qty_map(entries) -> dict:
    return {}


def _apply_processing_source_deductions(*args, **kwargs):
    return None


def _warehouse_core_summary_for_agency(agency: Agency | None) -> dict:
    if agency is None:
        return {
            "agency_id": 0,
            "snapshot_count": 0,
            "qty": 0,
            "available_qty": 0,
            "processing_reserved_qty": 0,
            "shipping_reserved_qty": 0,
            "reserve_count": 0,
            "source": "warehouse_core",
        }
    snapshot_qs = WarehouseStockSnapshot.objects.filter(agency=agency, is_archived=False)
    totals = snapshot_qs.aggregate(
        qty=Sum("qty"),
        available_qty=Sum("available_qty"),
        processing_reserved_qty=Sum("processing_reserved_qty"),
        shipping_reserved_qty=Sum("shipping_reserved_qty"),
    )
    reserve_count = (
        WarehouseReserve.objects.filter(agency=agency)
        .exclude(status__in=[WarehouseReserve.STATUS_RELEASED, WarehouseReserve.STATUS_CANCELED])
        .count()
    )
    return {
        "agency_id": int(agency.id or 0),
        "snapshot_count": int(snapshot_qs.count()),
        "qty": int(totals.get("qty") or 0),
        "available_qty": int(totals.get("available_qty") or 0),
        "processing_reserved_qty": int(totals.get("processing_reserved_qty") or 0),
        "shipping_reserved_qty": int(totals.get("shipping_reserved_qty") or 0),
        "reserve_count": int(reserve_count),
        "source": "warehouse_core",
    }


def rebuild_stock_snapshot_for_agency(agency: Agency | None) -> dict:
    return _warehouse_core_summary_for_agency(agency)


def refresh_materialized_stock_state_for_agency(agency: Agency | None) -> dict:
    return _warehouse_core_summary_for_agency(agency)


def refresh_materialized_stock_state_for_keys(
    agency: Agency | None,
    keys: set[tuple[str, str, str]] | list[tuple[str, str, str]] | tuple[tuple[str, str, str], ...],
) -> dict:
    result = _warehouse_core_summary_for_agency(agency)
    result["keys_count"] = len(keys or [])
    return result


def rebuild_stock_snapshot() -> dict:
    agencies = list(Agency.objects.all().order_by("id"))
    results = [rebuild_stock_snapshot_for_agency(agency) for agency in agencies]
    return {
        "agency_count": len(results),
        "snapshot_count": sum(int(result.get("snapshot_count") or 0) for result in results),
        "qty": sum(int(result.get("qty") or 0) for result in results),
        "available_qty": sum(int(result.get("available_qty") or 0) for result in results),
        "processing_reserved_qty": sum(int(result.get("processing_reserved_qty") or 0) for result in results),
        "shipping_reserved_qty": sum(int(result.get("shipping_reserved_qty") or 0) for result in results),
        "source": "warehouse_core",
        "agencies": results,
    }
