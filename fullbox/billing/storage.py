"""Read-only остатки клиента для биллинга хранения (склад не меняем)."""

from __future__ import annotations

from typing import Any

STORAGE_ZONES = frozenset({"PR", "OS", "OBR", "OTG"})
NON_STORAGE_STATE_CODES = frozenset(
    {
        "loaded_to_vehicle",
        "shipped",
        "partially_shipped",
        "canceled",
        "processing_consumed",
    }
)


def _zone_of_row(row: dict[str, Any]) -> str:
    zone = str(row.get("zone") or row.get("zone_code") or "").strip().upper()
    if zone in STORAGE_ZONES:
        return zone
    if int(row.get("stock_otg_qty") or 0) > 0:
        return "OTG"
    if int(row.get("stock_processing_qty") or 0) > 0:
        return "OBR"
    if int(row.get("stock_main_qty") or 0) > 0:
        return "OS"
    return zone


def _state_of_row(row: dict[str, Any]) -> str:
    return str(row.get("warehouse_state_code") or "").strip().lower()


def _truthy_row_flag(row: dict[str, Any], key: str) -> bool:
    value = row.get(key)
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "да"}


def _is_storage_billable_row(row: dict[str, Any]) -> bool:
    if _state_of_row(row) in NON_STORAGE_STATE_CODES:
        return False
    if _truthy_row_flag(row, "is_in_vehicle"):
        return False
    return _zone_of_row(row) in STORAGE_ZONES


def _enrich_row_dims(row: dict[str, Any], sku_cache: dict[int, Any], container_cache: dict[int, Any]) -> dict[str, Any]:
    """Добавить mm-габариты из SKU / WarehouseContainer (read-only)."""
    out = dict(row)
    sku_id = int(row.get("sku_ref_id") or row.get("sku_id") or 0)
    if sku_id and sku_id in sku_cache:
        sku = sku_cache[sku_id]
        out["sku_id"] = sku_id
        out["sku_code"] = out.get("sku_code") or out.get("sku") or getattr(sku, "sku_code", "")
        out["sku_name"] = out.get("sku_name") or out.get("name") or getattr(sku, "name", "")
        for attr, key in (("length_mm", "sku_length_mm"), ("width_mm", "sku_width_mm"), ("height_mm", "sku_height_mm")):
            val = getattr(sku, attr, None)
            if val is not None:
                try:
                    out[key] = int(val)
                    out.setdefault(attr, int(val))
                except (TypeError, ValueError):
                    pass
        vol = getattr(sku, "volume", None)
        if vol is not None:
            out["sku_volume"] = str(vol)
        for attr, key in (
            ("weight_kg", "sku_weight_kg"),
            ("weight_gross_kg", "sku_weight_gross_kg"),
            ("weight_net_kg", "sku_weight_net_kg"),
        ):
            val = getattr(sku, attr, None)
            if val is not None:
                out[key] = str(val)
                out.setdefault(attr, str(val))

    for cid_key, prefix in (("container_id", "box_"), ("parent_container_id", "pallet_")):
        cid = int(row.get(cid_key) or 0)
        if not cid or cid not in container_cache:
            continue
        cont = container_cache[cid]
        w = getattr(cont, "width_mm", None)
        h = getattr(cont, "height_mm", None)
        d = getattr(cont, "depth_mm", None)
        if w and h and d:
            out[f"{prefix}width_mm"] = int(w)
            out[f"{prefix}height_mm"] = int(h)
            out[f"{prefix}depth_mm"] = int(d)
            out[f"{prefix}length_mm"] = int(d)
            # Prefer box dims on the row for volume calc
            if prefix == "box_" or not out.get("length_mm"):
                out["width_mm"] = int(w)
                out["height_mm"] = int(h)
                out["length_mm"] = int(d)
                out["depth_mm"] = int(d)
        gross_weight_g = getattr(cont, "gross_weight_g", None)
        if gross_weight_g:
            out[f"{prefix}gross_weight_g"] = int(gross_weight_g)
            if prefix == "box_":
                out["gross_weight_g"] = int(gross_weight_g)
    return out


def client_storage_stock_rows(agency) -> list[dict[str, Any]]:
    """Строки остатков в зонах хранения + габариты SKU/контейнера."""
    from sklad.models import WarehouseContainer
    from sklad.services import StockAvailabilityService
    from sku.models import SKU

    rows = StockAvailabilityService.stock_rows_with_availability(agency=agency)
    filtered: list[dict[str, Any]] = []
    sku_ids: set[int] = set()
    container_ids: set[int] = set()
    for row in rows:
        qty = int(row.get("qty") or 0)
        if qty <= 0:
            continue
        if not _is_storage_billable_row(row):
            continue
        zone = _zone_of_row(row)
        row = dict(row)
        row["zone"] = zone
        filtered.append(row)
        sid = int(row.get("sku_ref_id") or 0)
        if sid:
            sku_ids.add(sid)
        for key in ("container_id", "parent_container_id"):
            cid = int(row.get(key) or 0)
            if cid:
                container_ids.add(cid)

    sku_cache = {s.id: s for s in SKU.objects.filter(id__in=sku_ids)} if sku_ids else {}
    container_cache = (
        {c.id: c for c in WarehouseContainer.objects.filter(id__in=container_ids)} if container_ids else {}
    )
    return [_enrich_row_dims(r, sku_cache, container_cache) for r in filtered]


def count_client_pallets(agency) -> dict[str, Any]:
    """Уникальные pallet_code клиента в зонах OS+OBR+OTG (qty > 0)."""
    rows = client_storage_stock_rows(agency)
    by_zone: dict[str, set[str]] = {z: set() for z in STORAGE_ZONES}
    all_codes: set[str] = set()

    for row in rows:
        code = str(row.get("pallet_code") or "").strip()
        if not code:
            continue
        zone = _zone_of_row(row)
        if zone not in STORAGE_ZONES:
            continue
        by_zone[zone].add(code)
        all_codes.add(code)

    zone_counts = {z: len(codes) for z, codes in by_zone.items()}
    return {
        "total": len(all_codes),
        "by_zone": zone_counts,
        "pallet_codes": sorted(all_codes),
        "rows": rows,
    }


def agencies_with_pallets():
    """Клиенты, у которых есть хотя бы одна палета в OS/OBR/OTG."""
    from sku.models import Agency
    from sklad.services.warehouse_stock_rows import snapshot_stock_rows

    agency_ids: set[int] = set()
    for row in snapshot_stock_rows(require_pallet=True):
        qty = int(row.get("qty") or 0)
        if qty <= 0:
            continue
        code = str(row.get("pallet_code") or "").strip()
        if not code:
            continue
        if not _is_storage_billable_row(row):
            continue
        aid = row.get("agency_id")
        if aid:
            agency_ids.add(int(aid))
    return list(Agency.objects.filter(id__in=agency_ids).order_by("id"))


def agencies_with_storage_stock():
    """Клиенты с любым положительным остатком в тарифицируемых зонах.

    В объёмной схеме товар может храниться без pallet_code, поэтому выборка
    только палет пропускала литровые и м³-тарифы.
    """
    from sku.models import Agency
    from sklad.services.warehouse_stock_rows import snapshot_stock_rows

    agency_ids: set[int] = set()
    for row in snapshot_stock_rows():
        if int(row.get("qty") or 0) <= 0:
            continue
        if not _is_storage_billable_row(row):
            continue
        agency_id = row.get("agency_id")
        if agency_id:
            agency_ids.add(int(agency_id))
    return list(Agency.objects.filter(id__in=agency_ids).order_by("id"))
