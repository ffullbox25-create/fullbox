from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable

from django.db.models.functions import Lower, Trim

from sku.models import SKUBarcode


def normalize_barcode(value: str) -> str:
    return str(value or "").strip().casefold()


def filter_normalized_barcodes(queryset, barcodes: Iterable[str], *, field_name="barcode"):
    """Filter a queryset by barcode without losing rows to letter-case differences.

    Barcode normalization already treats marketplace prefixes such as ``OZN``
    case-insensitively. Apply the same rule at the database boundary, before
    Python receives and normalizes the matching rows.
    """
    normalized = set()
    for barcode in barcodes:
        value = normalize_barcode(barcode)
        if value:
            normalized.add(value)
    if not normalized:
        return queryset.none()
    alias = "_fbs_normalized_barcode"
    return queryset.alias(**{alias: Lower(Trim(field_name))}).filter(
        **{f"{alias}__in": sorted(normalized)}
    )


def _normalize_size(value: str) -> str:
    normalized = str(value or "").strip().casefold()
    # Marketplace catalogs commonly represent a product without variants as
    # either an empty size (Ozon) or numeric zero (WB). They are the same
    # no-size variant, but real sizes such as 1, 42 or XL stay isolated.
    return "" if normalized in {"", "0"} else normalized


def sku_barcode_alias_map(
    sku_barcode_pairs: Iterable[tuple[int | None, str]],
) -> dict[tuple[int, str], tuple[str, ...]]:
    """Return interchangeable catalog barcodes for the same SKU and size."""
    requested: dict[tuple[int, str], str] = {}
    for raw_sku_id, raw_barcode in sku_barcode_pairs:
        barcode = str(raw_barcode or "").strip()
        normalized = normalize_barcode(barcode)
        if raw_sku_id and normalized:
            requested[(int(raw_sku_id), normalized)] = barcode
    if not requested:
        return {}

    rows = list(
        SKUBarcode.objects.filter(sku_id__in={key[0] for key in requested})
        .values("sku_id", "value", "size", "is_primary", "id")
        .order_by("sku_id", "-is_primary", "id")
    )
    groups: dict[tuple[int, str], list[str]] = defaultdict(list)
    normalized_groups: dict[tuple[int, str], set[str]] = defaultdict(set)
    group_by_barcode: dict[tuple[int, str], tuple[int, str]] = {}
    for row in rows:
        sku_id = int(row["sku_id"])
        value = str(row["value"] or "").strip()
        normalized = normalize_barcode(value)
        if not normalized:
            continue
        group_key = (sku_id, _normalize_size(row["size"]))
        if normalized not in normalized_groups[group_key]:
            groups[group_key].append(value)
            normalized_groups[group_key].add(normalized)
        group_by_barcode[(sku_id, normalized)] = group_key

    result: dict[tuple[int, str], tuple[str, ...]] = {}
    for key, fallback in requested.items():
        group_key = group_by_barcode.get(key)
        result[key] = tuple(groups[group_key]) if group_key else (fallback,)
    return result


def same_sku_barcode_variant(*, sku_id: int, first: str, second: str) -> bool:
    first_value = str(first or "").strip()
    second_value = str(second or "").strip()
    if not first_value or not second_value:
        return False
    if normalize_barcode(first_value) == normalize_barcode(second_value):
        return True
    aliases = sku_barcode_alias_map(((sku_id, first_value),))
    return normalize_barcode(second_value) in {
        normalize_barcode(value)
        for value in aliases.get((int(sku_id), normalize_barcode(first_value)), ())
    }
