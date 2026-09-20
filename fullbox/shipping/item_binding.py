from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone as datetime_timezone

from django.conf import settings

from audit.models import OrderAuditEntry


# Existing shipments keep their current workflow. The strict gate applies only to
# requests created after the rollout boundary.
STRICT_MARKETPLACE_BINDING_START_AT = datetime(
    2026,
    7,
    30,
    8,
    0,
    tzinfo=datetime_timezone.utc,
)


def _ozon_gm_binding_enabled() -> bool:
    """Temporary switch: customer request versus scan fact stays mandatory."""
    return bool(getattr(settings, "SHIPPING_OZON_GM_BINDING_ENABLED", False))


def _clean_barcode(value) -> str:
    return str(value or "").strip()


def _barcode_key(value) -> str:
    return _clean_barcode(value).casefold()


def _catalog_barcode_alias_map(
    order,
    barcode_values,
    sku_codes=None,
) -> dict[str, dict]:
    agency_id = getattr(order, "agency_id", None)
    clean_values = sorted(
        {
            _clean_barcode(value)
            for value in (barcode_values or [])
            if _clean_barcode(value)
        }
    )
    clean_sku_codes = sorted(
        {
            str(value or "").strip()
            for value in (sku_codes or [])
            if str(value or "").strip()
        }
    )
    if not agency_id or (not clean_values and not clean_sku_codes):
        return {}

    from sku.models import SKU, SKUBarcode

    sku_ids = set(
        SKU.objects.filter(
            agency_id=agency_id,
            deleted=False,
            sku_code__in=clean_sku_codes,
        )
        .values_list("id", flat=True)
    )
    if clean_values:
        sku_ids.update(
            SKUBarcode.objects.filter(
                sku__agency_id=agency_id,
                sku__deleted=False,
                value__in=clean_values,
            ).values_list("sku_id", flat=True)
        )
    sku_ids = sorted({int(sku_id) for sku_id in sku_ids})
    if not sku_ids:
        return {}

    aliases = list(
        SKUBarcode.objects.select_related("sku")
        .filter(
            sku_id__in=sku_ids,
            sku__agency_id=agency_id,
            sku__deleted=False,
        )
        .order_by("sku_id", "-is_primary", "value")
    )
    canonical_by_sku: dict[int, str] = {}
    for row in aliases:
        canonical_by_sku.setdefault(int(row.sku_id), _clean_barcode(row.value))

    result: dict[str, dict] = {}
    for row in aliases:
        barcode = _clean_barcode(row.value)
        if not barcode:
            continue
        result[_barcode_key(barcode)] = {
            "identity_key": f"sku:{int(row.sku_id)}",
            "sku_code_key": str(row.sku.sku_code or "").strip().casefold(),
            "canonical_barcode": canonical_by_sku.get(int(row.sku_id), barcode),
        }
    return result


def _catalog_item_identity(
    *,
    barcode,
    sku_code="",
    barcode_aliases: dict[str, dict] | None = None,
) -> tuple[str, str]:
    clean_barcode = _clean_barcode(barcode)
    barcode_key = _barcode_key(clean_barcode)
    if not barcode_key:
        return "", ""

    alias = (barcode_aliases or {}).get(barcode_key)
    if not alias:
        return f"barcode:{barcode_key}", clean_barcode

    clean_sku_code = str(sku_code or "").strip().casefold()
    expected_sku_code = str(alias.get("sku_code_key") or "").strip().casefold()
    if clean_sku_code and expected_sku_code and clean_sku_code != expected_sku_code:
        return (
            f"barcode:{barcode_key}|sku:{clean_sku_code}",
            clean_barcode,
        )
    return (
        str(alias.get("identity_key") or f"barcode:{barcode_key}"),
        _clean_barcode(alias.get("canonical_barcode")) or clean_barcode,
    )


def _positive_int(value) -> int:
    try:
        return max(int(value or 0), 0)
    except (TypeError, ValueError):
        return 0


def marketplace_binding_kind(order) -> str:
    marketplace_name = str(
        getattr(getattr(order, "marketplace", None), "name", "") or ""
    ).strip().casefold()
    if "ozon" in marketplace_name:
        return "ozon"
    if (
        marketplace_name == "wb"
        or marketplace_name.startswith("wb ")
        or "wildberries" in marketplace_name
        or "вайлдберриз" in marketplace_name
        or marketplace_name == "вб"
    ):
        return "wb"
    return ""


def should_enforce_marketplace_item_binding(order) -> bool:
    if not marketplace_binding_kind(order):
        return False
    created_at = getattr(order, "created_at", None)
    if created_at is None:
        return False
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=datetime_timezone.utc)
    return created_at >= STRICT_MARKETPLACE_BINDING_START_AT


def _requested_totals(
    order,
    *,
    barcode_aliases: dict[str, dict] | None = None,
) -> tuple[Counter, list[str]]:
    totals: Counter[str] = Counter()
    labels: dict[str, str] = {}
    errors: list[str] = []
    for item in order.items.order_by("id"):
        qty = _positive_int(getattr(item, "qty_requested", 0))
        barcode = _clean_barcode(getattr(item, "barcode", ""))
        label = str(
            getattr(item, "sku_code", "")
            or getattr(item, "name", "")
            or getattr(item, "pk", "")
            or "-"
        ).strip()
        if qty <= 0:
            errors.append(f"В позиции {label} указано нулевое количество.")
            continue
        if not barcode:
            errors.append(f"В позиции {label} не указан товарный штрих-код.")
            continue
        key, display_barcode = _catalog_item_identity(
            barcode=barcode,
            sku_code=getattr(item, "sku_code", ""),
            barcode_aliases=barcode_aliases,
        )
        totals[key] += qty
        labels.setdefault(key, display_barcode or barcode)
    return Counter({labels.get(key, key): qty for key, qty in totals.items()}), errors


def _fact_totals_and_signatures(
    boxes: list[dict] | None,
    *,
    barcode_aliases: dict[str, dict] | None = None,
) -> tuple[Counter, Counter, dict[tuple, list[str]], list[str]]:
    totals_by_key: Counter[str] = Counter()
    labels: dict[str, str] = {}
    signature_counts: Counter[tuple] = Counter()
    boxes_by_signature: defaultdict[tuple, list[str]] = defaultdict(list)
    errors: list[str] = []
    seen_box_codes: set[str] = set()

    for index, source_box in enumerate(boxes or [], start=1):
        if not isinstance(source_box, dict):
            continue
        box_code = str(
            source_box.get("box_code") or source_box.get("code") or ""
        ).strip()
        box_label = box_code or f"строка {index}"
        box_key = box_code.casefold()
        if not box_code:
            errors.append(f"У фактического короба в строке {index} отсутствует номер.")
        elif box_key in seen_box_codes:
            errors.append(f"Фактический короб {box_code} указан повторно.")
        else:
            seen_box_codes.add(box_key)

        box_totals: Counter[str] = Counter()
        for source_item in source_box.get("items") or []:
            if not isinstance(source_item, dict):
                continue
            qty = _positive_int(
                source_item.get("qty")
                or source_item.get("actual_qty")
                or source_item.get("quantity")
                or source_item.get("count")
            )
            barcode = _clean_barcode(source_item.get("barcode"))
            if qty <= 0:
                continue
            if not barcode:
                item_label = str(
                    source_item.get("sku_code")
                    or source_item.get("sku")
                    or source_item.get("name")
                    or "-"
                ).strip()
                errors.append(
                    f"В фактическом коробе {box_label} у позиции {item_label} "
                    "не указан товарный штрих-код."
                )
                continue
            key, display_barcode = _catalog_item_identity(
                barcode=barcode,
                sku_code=source_item.get("sku_code") or source_item.get("sku"),
                barcode_aliases=barcode_aliases,
            )
            box_totals[key] += qty
            totals_by_key[key] += qty
            labels.setdefault(key, display_barcode or barcode)

        signature = tuple(
            sorted((key, int(qty)) for key, qty in box_totals.items() if int(qty) > 0)
        )
        if not signature:
            errors.append(f"У фактического короба {box_label} отсутствует состав товара.")
            continue
        signature_counts[signature] += 1
        boxes_by_signature[signature].append(box_code)

    totals = Counter({labels.get(key, key): qty for key, qty in totals_by_key.items()})
    return totals, signature_counts, dict(boxes_by_signature), errors


def _latest_ozon_gm_payload(order) -> dict:
    entries = (
        OrderAuditEntry.objects.filter(
            order_type="shipping",
            order_id=str(getattr(order, "number", "") or ""),
        )
        .order_by("-id")[:100]
    )
    for entry in entries:
        payload = entry.payload if isinstance(entry.payload, dict) else {}
        cargoes = payload.get("gm_cargoes")
        if isinstance(cargoes, list) and cargoes:
            return payload
    return {}


def _latest_ozon_gm_cargoes(order) -> list[dict]:
    payload = _latest_ozon_gm_payload(order)
    cargoes = payload.get("gm_cargoes")
    if isinstance(cargoes, list):
        return [dict(row) for row in cargoes if isinstance(row, dict)]
    return []


def _gm_totals_and_signatures(
    cargoes: list[dict],
    *,
    barcode_aliases: dict[str, dict] | None = None,
) -> tuple[Counter, Counter, dict[tuple, list[str]], list[str]]:
    totals_by_key: Counter[str] = Counter()
    labels: dict[str, str] = {}
    signature_counts: Counter[tuple] = Counter()
    gm_by_signature: defaultdict[tuple, list[str]] = defaultdict(list)
    errors: list[str] = []
    seen_gm_codes: set[str] = set()

    if not cargoes:
        errors.append("Ozon не передал состав ШК ГМ.")
        return Counter(), Counter(), {}, errors

    for index, source_cargo in enumerate(cargoes, start=1):
        gm_code = str(source_cargo.get("gm_barcode") or "").strip()
        gm_label = gm_code or f"строка {index}"
        gm_key = gm_code.casefold()
        if not gm_code:
            errors.append(f"У ШК ГМ в строке {index} отсутствует номер.")
        elif gm_key in seen_gm_codes:
            errors.append(f"ШК ГМ {gm_code} указан повторно.")
        else:
            seen_gm_codes.add(gm_key)

        cargo_totals: Counter[str] = Counter()
        for source_item in source_cargo.get("items") or []:
            if not isinstance(source_item, dict):
                continue
            qty = _positive_int(
                source_item.get("quantity")
                or source_item.get("qty")
                or source_item.get("count")
            )
            barcode = _clean_barcode(source_item.get("barcode"))
            if qty <= 0:
                continue
            if not barcode:
                item_label = str(
                    source_item.get("offer_id")
                    or source_item.get("ozon_sku")
                    or source_item.get("name")
                    or "-"
                ).strip()
                errors.append(
                    f"В составе ШК ГМ {gm_label} у позиции {item_label} "
                    "не указан товарный штрих-код."
                )
                continue
            key, display_barcode = _catalog_item_identity(
                barcode=barcode,
                sku_code=source_item.get("offer_id"),
                barcode_aliases=barcode_aliases,
            )
            cargo_totals[key] += qty
            totals_by_key[key] += qty
            labels.setdefault(key, display_barcode or barcode)

        signature = tuple(
            sorted((key, int(qty)) for key, qty in cargo_totals.items() if int(qty) > 0)
        )
        if not signature:
            errors.append(f"У ШК ГМ {gm_label} отсутствует состав товара.")
            continue
        signature_counts[signature] += 1
        gm_by_signature[signature].append(gm_code)

    totals = Counter({labels.get(key, key): qty for key, qty in totals_by_key.items()})
    return totals, signature_counts, dict(gm_by_signature), errors


def _totals_by_key(totals: Counter) -> Counter:
    return Counter({_barcode_key(barcode): int(qty) for barcode, qty in totals.items()})


def _totals_difference_errors(
    *,
    expected: Counter,
    actual: Counter,
    actual_label: str,
) -> list[str]:
    expected_by_key = _totals_by_key(expected)
    actual_by_key = _totals_by_key(actual)
    labels: dict[str, str] = {}
    for source in (expected, actual):
        for barcode in source:
            labels.setdefault(_barcode_key(barcode), str(barcode))

    errors: list[str] = []
    for key in sorted(set(expected_by_key) | set(actual_by_key)):
        requested = int(expected_by_key.get(key, 0))
        found = int(actual_by_key.get(key, 0))
        if requested == found:
            continue
        delta_label = "не хватает" if found < requested else "лишнее количество"
        errors.append(
            f"{actual_label}: ШК {labels.get(key, key)} — заявлено {requested}, "
            f"найдено {found} ({delta_label})."
        )
    return errors


def validate_ozon_request_binding(
    order,
    *,
    gm_cargoes: list[dict],
    expected_boxes: int | None = None,
    ozon_boxes_total: int | None = None,
    compare_source_box_count: bool = True,
) -> dict:
    if marketplace_binding_kind(order) != "ozon":
        return {
            "applies": False,
            "ready": True,
            "errors": [],
            "request_totals": {},
            "gm_totals": {},
        }

    cargoes = [dict(row) for row in (gm_cargoes or []) if isinstance(row, dict)]
    if not cargoes:
        return {
            "applies": True,
            "ready": True,
            "errors": [],
            "request_totals": {},
            "gm_totals": {},
        }

    barcode_values = [getattr(item, "barcode", "") for item in order.items.order_by("id")]
    sku_codes = [getattr(item, "sku_code", "") for item in order.items.order_by("id")]
    for source_cargo in cargoes:
        barcode_values.extend(
            source_item.get("barcode")
            for source_item in (source_cargo.get("items") or [])
            if isinstance(source_item, dict)
        )
        sku_codes.extend(
            source_item.get("offer_id")
            for source_item in (source_cargo.get("items") or [])
            if isinstance(source_item, dict)
        )
    barcode_aliases = _catalog_barcode_alias_map(
        order,
        barcode_values,
        sku_codes,
    )
    requested, request_errors = _requested_totals(
        order,
        barcode_aliases=barcode_aliases,
    )
    gm_totals, _gm_signatures, _gm_by_signature, gm_errors = (
        _gm_totals_and_signatures(
            cargoes,
            barcode_aliases=barcode_aliases,
        )
    )
    errors = [*request_errors, *gm_errors]

    request_box_count = _positive_int(
        getattr(order, "expected_boxes", 0)
        if expected_boxes is None
        else expected_boxes
    )
    ozon_box_count = _positive_int(ozon_boxes_total) or len(cargoes)
    # Fullbox boxes are source warehouse containers, while Ozon boxes are
    # destination cargo places. Several source boxes may be consolidated into
    # fewer Ozon cargoes, so workflows that preserve the exact Ozon GM
    # composition must not compare those counts as if they were one entity.
    if compare_source_box_count and request_box_count != ozon_box_count:
        errors.append(
            f"Количество коробов не совпадает: Ozon {ozon_box_count}, "
            f"в заявке Fullbox {request_box_count}."
        )

    cargoes_are_complete = ozon_box_count <= 0 or len(cargoes) == ozon_box_count
    if cargoes_are_complete:
        requested_by_key = _totals_by_key(requested)
        gm_by_key = _totals_by_key(gm_totals)
        labels: dict[str, str] = {}
        for source in (requested, gm_totals):
            for barcode in source:
                labels.setdefault(_barcode_key(barcode), str(barcode))
        for key in sorted(set(requested_by_key) | set(gm_by_key)):
            request_qty = int(requested_by_key.get(key, 0))
            ozon_qty = int(gm_by_key.get(key, 0))
            if request_qty == ozon_qty:
                continue
            errors.append(
                f"Состав заявки не совпадает: ШК {labels.get(key, key)} — "
                f"Ozon {ozon_qty} шт., в заявке Fullbox {request_qty} шт."
            )

    return {
        "applies": True,
        "ready": not errors,
        "errors": errors,
        "request_totals": dict(requested),
        "gm_totals": dict(gm_totals),
    }


def _exact_ozon_processed_barcode_errors(order, cargoes: list[dict]) -> list[str]:
    """Reject replacing an OZN product barcode with another SKU alias.

    Catalog aliases are useful for ordinary product barcodes, but an ``OZN``
    barcode identifies the processed marketplace unit.  Comparing only the
    catalog SKU would allow a raw box with the same article to satisfy the
    request.  Aggregate by offer and exact case-insensitive barcode so splits
    across several Fullbox rows remain valid while cross-barcode substitution
    is blocked.
    """
    expected: Counter[str] = Counter()
    labels: dict[str, tuple[str, str]] = {}
    offers_by_barcode: defaultdict[str, set[str]] = defaultdict(set)
    for source_cargo in cargoes or []:
        if not isinstance(source_cargo, dict):
            continue
        for source_item in source_cargo.get("items") or []:
            if not isinstance(source_item, dict):
                continue
            offer_label = str(source_item.get("offer_id") or "").strip()
            barcode_label = _clean_barcode(source_item.get("barcode"))
            offer_key = offer_label.casefold()
            barcode_key = _barcode_key(barcode_label)
            quantity = _positive_int(
                source_item.get("quantity")
                or source_item.get("qty")
                or source_item.get("count")
            )
            if (
                not offer_key
                or not barcode_key.startswith("ozn")
                or quantity <= 0
            ):
                continue
            expected[barcode_key] += quantity
            offers_by_barcode[barcode_key].add(offer_key)
            labels.setdefault(
                barcode_key,
                (offer_label, barcode_label),
            )

    if not expected:
        return []

    requested: Counter[str] = Counter()
    requested_by_offer: defaultdict[str, Counter[str]] = defaultdict(Counter)
    for item in order.items.order_by("id"):
        offer_key = str(getattr(item, "sku_code", "") or "").strip().casefold()
        barcode_key = _barcode_key(getattr(item, "barcode", ""))
        quantity = _positive_int(getattr(item, "qty_requested", 0))
        requested[barcode_key] += quantity
        requested_by_offer[offer_key][barcode_key] += quantity

    errors: list[str] = []
    for barcode_key, expected_qty in expected.items():
        found_qty = int(requested.get(barcode_key, 0))
        if found_qty == int(expected_qty):
            continue
        offer_label, barcode_label = labels[barcode_key]
        substituted_qty = 0
        for offer_key in offers_by_barcode.get(barcode_key, set()):
            request_barcodes = requested_by_offer.get(offer_key, Counter())
            substituted_qty += sum(
                max(int(qty) - int(expected.get(key, 0)), 0)
                for key, qty in request_barcodes.items()
                if key != barcode_key
            )
        suffix = (
            f"; {substituted_qty} шт. подставлены с другим ШК"
            if substituted_qty > 0
            else ""
        )
        errors.append(
            f"Позиция Ozon {offer_label}: требуется ШК {barcode_label} — "
            f"Ozon {int(expected_qty)} шт., в заявке по этому ШК {found_qty} шт."
            f"{suffix}. Необработанный товар использовать нельзя."
        )
    return errors


def enforced_ozon_request_binding_errors(
    order,
    *,
    gm_cargoes: list[dict] | None = None,
    expected_boxes: int | None = None,
    ozon_boxes_total: int | None = None,
    compare_source_box_count: bool = True,
) -> list[str]:
    if not should_enforce_marketplace_item_binding(order):
        return []
    if marketplace_binding_kind(order) != "ozon":
        return []

    cargoes = gm_cargoes
    if cargoes is None:
        payload = _latest_ozon_gm_payload(order)
        payload_cargoes = payload.get("gm_cargoes")
        cargoes = payload_cargoes if isinstance(payload_cargoes, list) else []
        if ozon_boxes_total is None:
            ozon_boxes_total = _positive_int(payload.get("ozon_boxes_total"))
    if not cargoes:
        return []

    exact_barcode_errors = _exact_ozon_processed_barcode_errors(order, cargoes)
    if not _ozon_gm_binding_enabled():
        return exact_barcode_errors

    result = validate_ozon_request_binding(
        order,
        gm_cargoes=cargoes,
        expected_boxes=expected_boxes,
        ozon_boxes_total=ozon_boxes_total,
        compare_source_box_count=compare_source_box_count,
    )
    return list(dict.fromkeys([*exact_barcode_errors, *(result.get("errors") or [])]))


def validate_marketplace_item_binding(
    order,
    *,
    boxes: list[dict] | None,
    gm_cargoes: list[dict] | None = None,
) -> dict:
    kind = marketplace_binding_kind(order)
    if not kind:
        return {
            "applies": False,
            "ready": True,
            "kind": "",
            "errors": [],
            "request_totals": {},
            "fact_totals": {},
            "gm_totals": {},
            "gm_box_assignments": {},
            "gm_binding_enabled": False,
        }

    gm_binding_enabled = kind == "ozon" and _ozon_gm_binding_enabled()
    cargoes = (
        list(gm_cargoes)
        if gm_binding_enabled and gm_cargoes is not None
        else (_latest_ozon_gm_cargoes(order) if gm_binding_enabled else [])
    )
    barcode_values = [
        getattr(item, "barcode", "")
        for item in order.items.order_by("id")
    ]
    sku_codes = [
        getattr(item, "sku_code", "")
        for item in order.items.order_by("id")
    ]
    for source_box in boxes or []:
        if not isinstance(source_box, dict):
            continue
        barcode_values.extend(
            source_item.get("barcode")
            for source_item in (source_box.get("items") or [])
            if isinstance(source_item, dict)
        )
        sku_codes.extend(
            source_item.get("sku_code") or source_item.get("sku")
            for source_item in (source_box.get("items") or [])
            if isinstance(source_item, dict)
        )
    for source_cargo in cargoes:
        if not isinstance(source_cargo, dict):
            continue
        barcode_values.extend(
            source_item.get("barcode")
            for source_item in (source_cargo.get("items") or [])
            if isinstance(source_item, dict)
        )
        sku_codes.extend(
            source_item.get("offer_id")
            for source_item in (source_cargo.get("items") or [])
            if isinstance(source_item, dict)
        )
    barcode_aliases = (
        _catalog_barcode_alias_map(
            order,
            barcode_values,
            sku_codes,
        )
        if kind == "ozon"
        else {}
    )

    requested, request_errors = _requested_totals(
        order,
        barcode_aliases=barcode_aliases,
    )
    fact, fact_signatures, boxes_by_signature, fact_errors = (
        _fact_totals_and_signatures(
            boxes,
            barcode_aliases=barcode_aliases,
        )
    )
    errors = [*request_errors, *fact_errors]
    errors.extend(
        _totals_difference_errors(
            expected=requested,
            actual=fact,
            actual_label="Фактическая отгрузка",
        )
    )

    gm_totals: Counter = Counter()
    gm_box_assignments: dict[str, str] = {}
    if gm_binding_enabled:
        gm_totals, gm_signatures, gm_by_signature, gm_errors = (
            _gm_totals_and_signatures(
                cargoes,
                barcode_aliases=barcode_aliases,
            )
        )
        errors.extend(gm_errors)
        errors.extend(
            _totals_difference_errors(
                expected=requested,
                actual=gm_totals,
                actual_label="Состав ШК ГМ Ozon",
            )
        )
        if gm_signatures == fact_signatures:
            for signature, gm_codes in gm_by_signature.items():
                box_codes = sorted(
                    boxes_by_signature.get(signature, []),
                    key=str.casefold,
                )
                for gm_code, box_code in zip(sorted(gm_codes, key=str.casefold), box_codes):
                    gm_box_assignments[gm_code] = box_code

    return {
        "applies": True,
        "ready": not errors,
        "kind": kind,
        "errors": errors,
        "request_totals": dict(requested),
        "fact_totals": dict(fact),
        "gm_totals": dict(gm_totals),
        "gm_box_assignments": gm_box_assignments,
        "gm_binding_enabled": gm_binding_enabled,
    }


def marketplace_binding_mismatch_rows(result: dict) -> list[dict]:
    sources = (
        result.get("request_totals") or {},
        result.get("fact_totals") or {},
        result.get("gm_totals") or {},
    )
    labels: dict[str, str] = {}
    normalized: list[dict[str, int]] = []
    for source in sources:
        values: dict[str, int] = {}
        for barcode, raw_qty in source.items():
            label = _clean_barcode(barcode)
            key = _barcode_key(label)
            if not key:
                continue
            labels.setdefault(key, label)
            values[key] = _positive_int(raw_qty)
        normalized.append(values)

    requested, fact, marketplace = normalized
    use_marketplace_target = bool(
        result.get("kind") == "ozon"
        and result.get("gm_binding_enabled", True)
        and marketplace
    )
    rows: list[dict] = []
    for key in sorted(set(requested) | set(fact) | set(marketplace)):
        requested_qty = int(requested.get(key, 0))
        fact_qty = int(fact.get(key, 0))
        marketplace_qty = int(
            marketplace.get(key, 0)
            if use_marketplace_target
            else requested_qty
        )
        if requested_qty == fact_qty == marketplace_qty:
            continue

        fact_delta = marketplace_qty - fact_qty
        request_delta = marketplace_qty - requested_qty
        if fact_delta > 0:
            mismatch_label = f"Не хватает {fact_delta} шт."
        elif fact_delta < 0:
            mismatch_label = f"Лишнее количество {abs(fact_delta)} шт."
        elif request_delta > 0:
            mismatch_label = f"В заявке меньше на {request_delta} шт."
        else:
            mismatch_label = f"В заявке больше на {abs(request_delta)} шт."
        rows.append(
            {
                "barcode": labels.get(key, key),
                "marketplace_qty": marketplace_qty,
                "requested_qty": requested_qty,
                "fact_qty": fact_qty,
                "missing_qty": max(fact_delta, 0),
                "excess_qty": max(-fact_delta, 0),
                "mismatch_label": mismatch_label,
            }
        )
    return rows


def marketplace_binding_snapshot(result: dict) -> dict:
    def normalized_totals(source) -> list[dict]:
        rows = [
            {
                "barcode": _clean_barcode(barcode),
                "qty": _positive_int(qty),
            }
            for barcode, qty in (source or {}).items()
            if _clean_barcode(barcode)
        ]
        return sorted(rows, key=lambda row: row["barcode"].casefold())

    return {
        "kind": str(result.get("kind") or "").strip(),
        "gm_binding_enabled": bool(result.get("gm_binding_enabled", True)),
        "request_totals": normalized_totals(result.get("request_totals")),
        "fact_totals": normalized_totals(result.get("fact_totals")),
        "gm_totals": normalized_totals(result.get("gm_totals")),
        "mismatch_rows": marketplace_binding_mismatch_rows(result),
    }


def _snapshot_totals_by_barcode(snapshot: dict, key: str) -> dict[str, int]:
    totals: dict[str, int] = {}
    for row in snapshot.get(key) or []:
        if not isinstance(row, dict):
            continue
        barcode = _clean_barcode(row.get("barcode"))
        barcode_key = _barcode_key(barcode)
        if not barcode_key:
            continue
        totals[barcode_key] = totals.get(barcode_key, 0) + _positive_int(row.get("qty"))
    return totals


def _supplement_pick_tasks_are_done(order, payload: dict) -> bool:
    raw_move_ids = payload.get("additional_pick_move_ids")
    if not isinstance(raw_move_ids, (list, tuple)):
        return False
    move_ids = [str(value or "").strip() for value in raw_move_ids]
    move_ids = [value for value in move_ids if value]
    if not move_ids:
        return False

    try:
        from django.apps import apps
        from django.db.models import Q

        MoveTask = apps.get_model("reachtruck", "MoveTask")
    except (ImportError, LookupError):
        return False

    numeric_ids = [int(value) for value in move_ids if value.isdigit()]
    query = Q(legacy_order_id__in=move_ids)
    if numeric_ids:
        query |= Q(pk__in=numeric_ids)
    rows = list(
        MoveTask.objects.filter(
            request__context_id=str(getattr(order, "pk", "") or ""),
        )
        .filter(query)
        .values("id", "legacy_order_id", "status")
    )
    done_status = str(getattr(MoveTask, "STATUS_DONE", "done") or "done")
    return all(
        any(
            str(row.get("status") or "") == done_status
            and (
                str(row.get("legacy_order_id") or "") == move_id
                or str(row.get("id") or "") == move_id
            )
            for row in rows
        )
        for move_id in move_ids
    )


def shipping_supplement_fact_is_complete(order, result: dict) -> bool:
    payload = (
        order.shipping_discrepancy_payload
        if isinstance(getattr(order, "shipping_discrepancy_payload", None), dict)
        else {}
    )
    status = str(getattr(order, "shipping_discrepancy_status", "") or "").strip()
    if not (
        status in {"pickup_required", "resolved"}
        and _positive_int(payload.get("workflow_version")) >= 2
        and str(payload.get("decision") or "").strip() == "supplement"
    ):
        return False

    # Non-marketplace transfers do not have an Ozon/WB binding snapshot. Their
    # supplement is complete when the live warehouse fact matches the request,
    # every positive-quantity row is physically inside a box, and all supplement
    # reachtruck tasks are done. Requiring kind="ozon" here used to leave a
    # successfully packed transfer forever in awaiting_supplement_packing.
    if str(result.get("kind") or "").strip() != "ozon":
        from .truth import ShippingTruthService

        truth = ShippingTruthService.for_order(order)
        if truth.quantity_mismatch_rows or not truth.warehouse_box_codes:
            return False
        if any(
            _positive_int(getattr(snapshot, "qty", 0)) > 0
            and not (
                getattr(snapshot, "container_id", None)
                or str(getattr(snapshot, "container_code", "") or "").strip()
            )
            for snapshot in truth.warehouse_snapshots
        ):
            return False
        return _supplement_pick_tasks_are_done(order, payload)

    stored_snapshot = payload.get("binding_snapshot")
    if not isinstance(stored_snapshot, dict):
        return False
    live_snapshot = marketplace_binding_snapshot(result)
    plan_key = (
        "gm_totals"
        if bool(result.get("gm_binding_enabled", True))
        else "request_totals"
    )
    stored_plan_totals = _snapshot_totals_by_barcode(stored_snapshot, plan_key)
    live_plan_totals = _snapshot_totals_by_barcode(live_snapshot, plan_key)
    live_fact_totals = _snapshot_totals_by_barcode(live_snapshot, "fact_totals")
    if not stored_plan_totals or stored_plan_totals != live_plan_totals:
        return False
    if live_fact_totals != live_plan_totals:
        return False

    allowed_request_mismatch_prefixes = ["Фактическая отгрузка:"]
    if bool(result.get("gm_binding_enabled", True)):
        allowed_request_mismatch_prefixes.append("Состав ШК ГМ Ozon:")
    if any(
        not str(error or "").startswith(allowed_request_mismatch_prefixes)
        for error in result.get("errors") or []
    ):
        return False
    return _supplement_pick_tasks_are_done(order, payload)


def shipping_discrepancy_allows_current_mismatch(order, result: dict) -> bool:
    payload = (
        order.shipping_discrepancy_payload
        if isinstance(getattr(order, "shipping_discrepancy_payload", None), dict)
        else {}
    )
    stored_snapshot = payload.get("binding_snapshot")
    live_snapshot = marketplace_binding_snapshot(result)
    if (
        isinstance(stored_snapshot, dict)
        and not bool(result.get("gm_binding_enabled", True))
    ):
        snapshot_matches = all(
            _snapshot_totals_by_barcode(stored_snapshot, key)
            == _snapshot_totals_by_barcode(live_snapshot, key)
            for key in ("request_totals", "fact_totals")
        )
    else:
        snapshot_matches = stored_snapshot == live_snapshot
    approved_as_is = bool(
        str(getattr(order, "shipping_discrepancy_status", "") or "").strip()
        == "approved"
        and int(payload.get("workflow_version") or 0) >= 2
        and str(payload.get("decision") or "").strip() == "approve_mismatch"
        and isinstance(stored_snapshot, dict)
        and snapshot_matches
    )
    return approved_as_is or shipping_supplement_fact_is_complete(order, result)


def _has_v2_binding_snapshot(order) -> bool:
    payload = (
        order.shipping_discrepancy_payload
        if isinstance(getattr(order, "shipping_discrepancy_payload", None), dict)
        else {}
    )
    return bool(
        _positive_int(payload.get("workflow_version")) >= 2
        and isinstance(payload.get("binding_snapshot"), dict)
    )


def build_shipping_final_truth_snapshot(order, *, result: dict) -> dict:
    """Build the immutable customer-plan versus scanned-fact shipment result."""
    if not (
        should_enforce_marketplace_item_binding(order)
        or _has_v2_binding_snapshot(order)
    ):
        return {}
    if not result.get("applies"):
        return {}
    if not (result.get("ready") or shipping_discrepancy_allows_current_mismatch(order, result)):
        return {}

    payload = (
        order.shipping_discrepancy_payload
        if isinstance(getattr(order, "shipping_discrepancy_payload", None), dict)
        else {}
    )
    live_snapshot = marketplace_binding_snapshot(result)
    stored_snapshot = payload.get("binding_snapshot")
    source_snapshot = (
        stored_snapshot
        if _positive_int(payload.get("workflow_version")) >= 2
        and isinstance(stored_snapshot, dict)
        else live_snapshot
    )
    kind = str(result.get("kind") or source_snapshot.get("kind") or "").strip()
    plan_key = (
        "gm_totals"
        if kind == "ozon" and bool(result.get("gm_binding_enabled", True))
        else "request_totals"
    )
    plan_totals = _snapshot_totals_by_barcode(source_snapshot, plan_key)
    fact_totals = _snapshot_totals_by_barcode(live_snapshot, "fact_totals")
    if not plan_totals:
        return {}

    labels: dict[str, str] = {}
    for snapshot, key in (
        (source_snapshot, plan_key),
        (live_snapshot, "fact_totals"),
    ):
        for row in snapshot.get(key) or []:
            if not isinstance(row, dict):
                continue
            barcode = _clean_barcode(row.get("barcode"))
            barcode_key = _barcode_key(barcode)
            if barcode_key:
                labels.setdefault(barcode_key, barcode)

    rows: list[dict] = []
    for barcode_key in sorted(set(plan_totals) | set(fact_totals)):
        planned_qty = int(plan_totals.get(barcode_key, 0) or 0)
        fact_qty = int(fact_totals.get(barcode_key, 0) or 0)
        rows.append(
            {
                "barcode": labels.get(barcode_key, barcode_key),
                "planned_qty": planned_qty,
                "fact_qty": fact_qty,
                "shortage_qty": max(planned_qty - fact_qty, 0),
                "excess_qty": max(fact_qty - planned_qty, 0),
            }
        )

    return {
        "version": 1,
        "source": "marketplace_barcode_quantity",
        "kind": kind,
        "decision": str(payload.get("decision") or "exact_match").strip(),
        "matches_plan": plan_totals == fact_totals,
        "planned_total_qty": sum(plan_totals.values()),
        "fact_total_qty": sum(fact_totals.values()),
        "shortage_total_qty": sum(row["shortage_qty"] for row in rows),
        "excess_total_qty": sum(row["excess_qty"] for row in rows),
        "rows": rows,
        "internal_request_totals": live_snapshot.get("request_totals") or [],
    }


def shipping_final_truth_rows(
    order,
    *,
    result: dict | None = None,
    boxes: list[dict] | None = None,
) -> list[dict]:
    """Return document rows from the persisted final snapshot or current scan fact."""
    payload = (
        order.shipping_discrepancy_payload
        if isinstance(getattr(order, "shipping_discrepancy_payload", None), dict)
        else {}
    )
    final_truth = payload.get("final_shipping_truth")
    if not isinstance(final_truth, dict) or not isinstance(final_truth.get("rows"), list):
        if not (
            should_enforce_marketplace_item_binding(order)
            or _has_v2_binding_snapshot(order)
        ):
            return []
        if boxes is None:
            from .packing import _shipping_delivered_boxes

            boxes = _shipping_delivered_boxes(order)
        result = result or validate_marketplace_item_binding(order, boxes=boxes)
        final_truth = build_shipping_final_truth_snapshot(order, result=result)
    if not final_truth:
        return []
    document_rows = final_truth.get("document_rows")
    if isinstance(document_rows, list):
        return [dict(row) for row in document_rows if isinstance(row, dict)]

    item_meta: dict[str, dict] = {}
    reserve_totals: Counter = Counter()
    for item in order.items.order_by("id"):
        barcode = _clean_barcode(getattr(item, "barcode", ""))
        barcode_key = _barcode_key(barcode)
        if not barcode_key:
            continue
        item_meta.setdefault(
            barcode_key,
            {
                "sku_code": str(getattr(item, "sku_code", "") or "").strip(),
                "name": str(getattr(item, "name", "") or "").strip(),
                "size": str(getattr(item, "size", "") or "").strip(),
                "goods_type": str(getattr(item, "goods_type", "") or "").strip(),
            },
        )
        reserve_totals[barcode_key] += _positive_int(getattr(item, "qty_reserved", 0))

    for box in boxes or []:
        if not isinstance(box, dict):
            continue
        for source_item in box.get("items") or []:
            if not isinstance(source_item, dict):
                continue
            barcode = _clean_barcode(source_item.get("barcode"))
            barcode_key = _barcode_key(barcode)
            if not barcode_key or barcode_key in item_meta:
                continue
            item_meta[barcode_key] = {
                "sku_code": str(source_item.get("sku_code") or source_item.get("sku") or "").strip(),
                "name": str(source_item.get("name") or "").strip(),
                "size": str(source_item.get("size") or "").strip(),
                "goods_type": str(source_item.get("goods_type") or "").strip(),
            }

    rows: list[dict] = []
    for source_row in final_truth.get("rows") or []:
        if not isinstance(source_row, dict):
            continue
        barcode = _clean_barcode(source_row.get("barcode"))
        barcode_key = _barcode_key(barcode)
        meta = item_meta.get(barcode_key, {})
        planned_qty = _positive_int(source_row.get("planned_qty"))
        fact_qty = _positive_int(source_row.get("fact_qty"))
        rows.append(
            {
                "sku_code": meta.get("sku_code") or "-",
                "name": meta.get("name") or "-",
                "size": meta.get("size") or "",
                "barcode": barcode or "-",
                "goods_type": meta.get("goods_type") or "",
                "qty_requested": planned_qty,
                "qty_reserved": int(reserve_totals.get(barcode_key, 0) or 0),
                "qty_shipped": fact_qty,
                "shortage_qty": _positive_int(source_row.get("shortage_qty")),
                "excess_qty": _positive_int(source_row.get("excess_qty")),
            }
        )
    return rows


def enforced_marketplace_item_binding_errors(
    order,
    *,
    boxes: list[dict] | None,
) -> list[str]:
    if not (
        should_enforce_marketplace_item_binding(order)
        or _has_v2_binding_snapshot(order)
    ):
        return []
    result = validate_marketplace_item_binding(order, boxes=boxes)
    if result["ready"]:
        return []
    if shipping_discrepancy_allows_current_mismatch(order, result):
        return []
    return [
        "Нельзя завершить отгрузку: товарный ШК и количество должны точно "
        "совпадать с заявкой клиента"
        + (
            " и составом ШК ГМ Ozon."
            if result["kind"] == "ozon" and result.get("gm_binding_enabled", True)
            else "."
        ),
        *result["errors"],
    ]
