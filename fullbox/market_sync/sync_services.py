from __future__ import annotations

import json

import requests
from django.db import IntegrityError
from django.http import JsonResponse
from django.utils import timezone

from audit.models import log_sku_change
from fbs.models import FbsOzonCredential
from sku.audit_history import (
    build_sku_audit_snapshot,
    sku_audit_change_rows,
    sku_audit_description,
    sku_audit_snapshot,
)
from sku.models import Agency, Market, MarketCredential, MarketplaceBinding, SKU, SKUBarcode, SKUPhoto

from .http import friendly_network_error, marketplace_request
from .models import MarketSyncReport


MARKETPLACE_PARAMETER_OVERWRITE_FIELDS = frozenset(
    {
        "length_mm",
        "width_mm",
        "height_mm",
        "weight_kg",
        "weight_net_kg",
        "weight_gross_kg",
    }
)


def _views():
    from . import views as market_sync_views

    return market_sync_views


def _report_link(report: MarketSyncReport | None) -> str:
    if not report:
        return ""
    return f"/market-sync/report/{report.id}/"


def _parse_payload(body) -> dict:
    raw_body = body.decode("utf-8") if isinstance(body, (bytes, bytearray)) else str(body or "")
    try:
        payload = json.loads(raw_body or "{}")
    except json.JSONDecodeError:
        payload = {}
    return payload if isinstance(payload, dict) else {}


def _sync_filters(payload: dict) -> tuple[str, str]:
    sku_code = str(payload.get("sku_code") or "").strip()
    external_id = str(payload.get("external_id") or "").strip()
    return sku_code, external_id


def _matches_marketplace_item(*, sku_code: str, external_id: str, sku_code_filter: str, external_id_filter: str) -> bool:
    prepared_sku_code = str(sku_code or "").strip()
    prepared_external_id = str(external_id or "").strip()
    if external_id_filter:
        return prepared_external_id == external_id_filter
    if sku_code_filter:
        return prepared_sku_code == sku_code_filter
    return True


def _ozon_first_image_url(*values) -> str | None:
    """Return the first URL from Ozon image fields, including nested arrays."""
    for value in values:
        if isinstance(value, dict):
            result = _ozon_first_image_url(
                value.get("url"),
                value.get("original"),
                value.get("default"),
                value.get("big"),
            )
            if result:
                return result
            continue
        if isinstance(value, (list, tuple)):
            result = _ozon_first_image_url(*value)
            if result:
                return result
            continue
        if value is None:
            continue
        text = str(value).strip()
        if text.startswith(("https://", "http://")):
            return text
    return None


def _has_meaningful_value(value) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    return True


def _apply_marketplace_updates(*, sku: SKU, update_fields: dict) -> bool:
    changed = False
    for field, value in update_fields.items():
        if value is None:
            continue
        current = getattr(sku, field)
        if (
            field not in MARKETPLACE_PARAMETER_OVERWRITE_FIELDS
            and _has_meaningful_value(current)
        ):
            continue
        if current != value:
            setattr(sku, field, value)
            changed = True
    return changed


def _log_marketplace_sku_audit(
    *,
    sku: SKU,
    before: dict | None,
    marketplace: str,
    is_created: bool,
) -> None:
    snapshot = build_sku_audit_snapshot(
        sku,
        before=before,
        source="marketplace",
        marketplace=marketplace,
    )
    action = "create" if is_created else "update"
    if not is_created and not sku_audit_change_rows(snapshot):
        return
    log_sku_change(
        action,
        sku,
        user=None,
        description=sku_audit_description(action, snapshot),
        snapshot=snapshot,
    )


def _sync_warehouse_sku_code_refs(*, sku: SKU, old_sku_code: str, new_sku_code: str) -> None:
    old_code = str(old_sku_code or "").strip()
    new_code = str(new_sku_code or "").strip()
    if not sku.pk or not old_code or not new_code or old_code == new_code:
        return
    from sklad.models import WarehouseReserve, WarehouseStockSnapshot

    WarehouseStockSnapshot.objects.filter(
        sku_ref=sku,
        sku_code=old_code,
        is_archived=False,
    ).update(sku_code=new_code)
    WarehouseReserve.objects.filter(
        sku_ref=sku,
        sku_code=old_code,
    ).exclude(
        status__in=[WarehouseReserve.STATUS_RELEASED, WarehouseReserve.STATUS_CANCELED],
    ).update(sku_code=new_code)


def _apply_marketplace_sku_code_update(
    *,
    agency: Agency,
    sku: SKU,
    new_sku_code: str,
    marketplace: str,
    external_id: str,
    errors: list,
) -> bool:
    prepared_code = str(new_sku_code or "").strip()
    if not prepared_code:
        return True
    current_code = str(sku.sku_code or "").strip()
    if current_code == prepared_code:
        return True
    conflict = (
        SKU.objects.filter(agency=agency, sku_code=prepared_code, deleted=False)
        .exclude(pk=sku.pk)
        .first()
    )
    if conflict:
        errors.append(
            f"{marketplace}: товар {prepared_code} найден по ШК, но такой артикул уже есть у SKU {conflict.sku_code}."
        )
        return False
    old_code = current_code
    sku.sku_code = prepared_code
    sku.save(update_fields=["sku_code"])
    _sync_warehouse_sku_code_refs(sku=sku, old_sku_code=old_code, new_sku_code=prepared_code)
    return True


def _normalized_barcode_values(values) -> list[str]:
    prepared = []
    for value in values or []:
        text = str(value or "").strip()
        if text and text not in prepared:
            prepared.append(text)
    return prepared


def _ozon_marketplace_sku_values(item: dict) -> list[str]:
    """Return the Ozon SKU values used in FBS posting product rows."""
    values = []
    candidates = [item.get("sku")]
    sources = item.get("sources") if isinstance(item.get("sources"), list) else []
    candidates.extend(
        source.get("sku")
        for source in sources
        if isinstance(source, dict)
    )
    for value in candidates:
        prepared = str(value or "").strip()
        if prepared and prepared not in values:
            values.append(prepared[:128])
    return values


def _sync_ozon_sku_bindings(
    *,
    sku: SKU,
    ozon_sku_values: list[str],
    synced_at,
    errors: list,
) -> bool:
    if not ozon_sku_values:
        return True
    existing = {
        binding.external_id: binding
        for binding in MarketplaceBinding.objects.select_related("sku").filter(
            marketplace=MarketplaceBinding.MARKETPLACE_OZON_SKU,
            external_id__in=ozon_sku_values,
        )
    }
    conflicts = [
        binding
        for binding in existing.values()
        if binding.sku_id != sku.id
    ]
    if conflicts:
        errors.append(
            "OZON: SKU Ozon уже привязан к другому товару: "
            + ", ".join(
                f"{binding.external_id} → {binding.sku.sku_code}"
                for binding in conflicts
            )
        )
        return False
    for external_id in ozon_sku_values:
        MarketplaceBinding.objects.update_or_create(
            marketplace=MarketplaceBinding.MARKETPLACE_OZON_SKU,
            external_id=external_id,
            defaults={
                "sku": sku,
                "last_synced_at": synced_at,
            },
        )
    return True


def _resolve_sku_for_marketplace_item(
    *,
    agency: Agency,
    marketplace: str,
    external_id: str,
    sku_code: str,
    barcodes,
    defaults: dict,
    errors: list,
) -> tuple[SKU | None, bool, MarketplaceBinding | None]:
    binding = (
        MarketplaceBinding.objects.select_related("sku")
        .filter(marketplace=marketplace, external_id=external_id)
        .first()
        if external_id
        else None
    )
    barcode_values = _normalized_barcode_values(barcodes)
    barcode_rows = list(
        SKUBarcode.objects.select_related("sku")
        .filter(agency=agency, value__in=barcode_values)
        .order_by("value")
    ) if barcode_values else []
    client_skus = {}
    blocked_values = []
    for barcode in barcode_rows:
        sku = getattr(barcode, "sku", None)
        if sku and sku.agency_id == agency.id and not sku.deleted:
            client_skus[sku.id] = sku
        else:
            blocked_values.append(barcode.value)

    if binding:
        bound_sku = getattr(binding, "sku", None)
        if not bound_sku or bound_sku.deleted or bound_sku.agency_id != agency.id:
            errors.append(
                f"{marketplace}: карточка {external_id} уже привязана к товару другого клиента или удаленному SKU."
            )
            return None, False, binding
        conflicting_skus = [sku for sku_id, sku in client_skus.items() if sku_id != bound_sku.id]
        if blocked_values or conflicting_skus:
            conflict_parts = []
            if conflicting_skus:
                conflict_parts.append(
                    "другие SKU клиента: "
                    + ", ".join(str(getattr(sku, "sku_code", sku.id)) for sku in conflicting_skus)
                )
            if blocked_values:
                conflict_parts.append("ШК удалённого SKU клиента: " + ", ".join(blocked_values))
            errors.append(
                f"{marketplace}: карточка {external_id} привязана к {bound_sku.sku_code}, "
                f"но пришедшие ШК уже заняты ({'; '.join(conflict_parts)})."
            )
            return None, False, binding
        return bound_sku, False, binding

    if blocked_values:
        errors.append(
            f"{marketplace}: товар {sku_code} не синхронизирован, потому что ШК "
            f"{', '.join(blocked_values)} уже есть у удаленного SKU этого клиента."
        )
        return None, False, None
    if len(client_skus) > 1:
        errors.append(
            f"{marketplace}: товар {sku_code} не синхронизирован, потому что его ШК уже привязаны "
            "к разным SKU клиента: "
            + ", ".join(str(getattr(sku, "sku_code", sku.id)) for sku in client_skus.values())
        )
        return None, False, None
    if len(client_skus) == 1:
        return next(iter(client_skus.values())), False, None

    sku, is_created = SKU.objects.get_or_create(
        agency=agency,
        sku_code=sku_code,
        defaults=defaults,
    )
    return sku, is_created, None


def run_wb_sync_request(*, body) -> JsonResponse:
    views = _views()
    payload = _parse_payload(body)
    sku_code_filter, external_id_filter = _sync_filters(payload)

    client_id = payload.get("client")
    if not client_id:
        return JsonResponse({"ok": False, "errors": ["Не указан клиент."]}, status=400)

    agency = Agency.objects.filter(pk=client_id).first()
    if not agency:
        return JsonResponse({"ok": False, "errors": ["Клиент не найден."]}, status=404)

    wb_market = Market.objects.filter(name__iexact="WB").first()
    if not wb_market:
        return JsonResponse({"ok": False, "errors": ["Маркетплейс WB не найден."]}, status=400)

    credential = MarketCredential.objects.filter(agency=agency, market=wb_market).first()
    token = (credential.market_key or "").strip() if credential else ""
    if not token:
        return JsonResponse({"ok": False, "errors": ["Не указан токен WB."]}, status=400)

    started_at = timezone.now()
    created = 0
    updated = 0
    processed = 0
    barcode_created = 0
    errors = []
    now = timezone.now()
    cursor = {"limit": 100}
    base_url = "https://content-api.wildberries.ru/content/v2/get/cards/list"

    for _ in range(50):
        try:
            response = marketplace_request(
                "POST",
                base_url,
                headers={"Authorization": token, "Content-Type": "application/json"},
                json={"settings": {"cursor": cursor, "filter": {"withPhoto": -1}}},
                timeout=30,
            )
        except requests.RequestException as exc:
            errors.append(friendly_network_error(exc, "WB"))
            break

        if response.status_code != 200:
            errors.append(f"WB API ошибка: {response.status_code}")
            break

        try:
            data = response.json()
        except ValueError:
            errors.append("WB API вернул некорректный JSON.")
            break

        cards = data.get("cards")
        cursor_data = data.get("cursor")
        if cards is None and isinstance(data.get("data"), dict):
            cards = data["data"].get("cards")
            cursor_data = data["data"].get("cursor")

        if not cards:
            break

        for card in cards:
            vendor_code = (card.get("vendorCode") or card.get("vendor_code") or "").strip()
            if not vendor_code:
                continue
            if len(vendor_code) > 64:
                vendor_code = vendor_code[:64]
            nm_id = card.get("nmID") or card.get("nmId") or card.get("nmid")
            if not _matches_marketplace_item(
                sku_code=vendor_code,
                external_id=str(nm_id or "").strip(),
                sku_code_filter=sku_code_filter,
                external_id_filter=external_id_filter,
            ):
                continue
            chars = views._extract_characteristics(card)
            name_raw = views._extract_first([card.get("title"), card.get("name"), card.get("subjectName")]) or vendor_code
            name = views._trim(name_raw, 255) or vendor_code
            brand = views._trim(card.get("brand"), 255)
            color = views._trim(views._extract_color(card) or views._find_char_value(chars, ["цвет"]), 64)
            size = views._trim(views._extract_size(card) or views._find_char_value(chars, ["размер"]), 64)
            subject = views._extract_first([card.get("subjectName"), card.get("subject")]) or views._find_char_value(
                chars, ["предмет", "категория"]
            )
            composition = views._trim(views._find_char_value(chars, ["состав", "материал"]), 255)
            gender = views._trim(views._find_char_value(chars, ["пол"]), 64)
            season = views._trim(views._find_char_value(chars, ["сезон"]), 64)
            made_in = views._trim(
                views._find_char_value(chars, ["страна производства", "страна изготов", "страна"]), 128
            )
            additional_name = views._trim(views._find_char_value(chars, ["доп", "дополн"]), 255)
            description = views._normalize_text(
                views._extract_first([card.get("description"), card.get("descriptionRu")])
                or views._find_char_value(chars, ["описание"])
            )
            tovar_category = views._trim(subject, 128)
            vid_tovar = views._trim(views._find_char_value(chars, ["вид товара", "вид"]), 128)
            type_tovar = views._trim(views._find_char_value(chars, ["тип товара", "тип"]), 128)

            dimensions = card.get("dimensions") if isinstance(card.get("dimensions"), dict) else {}
            length_mm = views._parse_length_mm(
                views._extract_first([dimensions.get("length"), views._find_char_value(chars, ["длина упаков", "длина"])])
            )
            width_mm = views._parse_length_mm(
                views._extract_first([dimensions.get("width"), views._find_char_value(chars, ["ширина упаков", "ширина"])])
            )
            height_mm = views._parse_length_mm(
                views._extract_first([dimensions.get("height"), views._find_char_value(chars, ["высота упаков", "высота"])])
            )
            volume = views._parse_volume(
                views._extract_first([dimensions.get("volume"), views._find_char_value(chars, ["объем", "объём"])])
            )
            weight_gross_kg = views._parse_weight_kg(
                views._extract_first(
                    [
                        dimensions.get("weightBrutto"),
                        dimensions.get("weight_brutto"),
                        card.get("weightGross"),
                        views._find_char_value(chars, ["вес брутто", "масса брутто"]),
                    ]
                )
            )
            weight_net_kg = views._parse_weight_kg(
                views._extract_first(
                    [
                        card.get("weightNetto"),
                        views._find_char_value(chars, ["вес нетто", "масса нетто"]),
                    ]
                )
            )
            weight_generic_kg = views._parse_weight_kg(
                views._extract_first(
                    [
                        dimensions.get("weight"),
                        card.get("weight"),
                        views._find_char_value(chars, ["вес", "масса"]),
                    ]
                )
            )
            weight_gross_effective_kg = weight_gross_kg if weight_gross_kg is not None else weight_generic_kg
            weight_kg = (
                weight_gross_effective_kg
                if weight_gross_effective_kg is not None
                else weight_net_kg
            )
            cr_product_date = views._parse_date(views._find_char_value(chars, ["дата производства", "дата изготовления"]))
            end_product_date = views._parse_date(views._find_char_value(chars, ["срок годности", "годен до"]))
            honest_sign = views._parse_flag(views._find_char_value(chars, ["честный знак", "маркиров"]))
            use_nds = views._parse_flag(views._find_char_value(chars, ["ндс"]))
            sign_akciz = views._parse_flag(views._find_char_value(chars, ["акциз"]))

            photo_urls = views._extract_photos(card)
            primary_photo = photo_urls[0] if photo_urls else None

            size_barcodes = views._extract_size_barcodes(card)
            barcodes = views._flatten_size_barcodes(size_barcodes)
            if size is None:
                size_values = [key for key in size_barcodes.keys() if key]
                if len(size_values) == 1:
                    size = views._trim(size_values[0], 64)
            code_value = views._trim(barcodes[0], 128) if barcodes else None

            update_fields = {
                "name": name,
                "market": wb_market,
                "source": "marketplace",
                "name_print": name,
            }
            if brand is not None:
                update_fields["brand"] = brand
            if color is not None:
                update_fields["color"] = color
            if size is not None:
                update_fields["size"] = size
            if composition is not None:
                update_fields["composition"] = composition
            if gender is not None:
                update_fields["gender"] = gender
            if season is not None:
                update_fields["season"] = season
            if made_in is not None:
                update_fields["made_in"] = made_in
            if additional_name is not None:
                update_fields["additional_name"] = additional_name
            if tovar_category is not None:
                update_fields["tovar_category"] = tovar_category
            if vid_tovar is not None:
                update_fields["vid_tovar"] = vid_tovar
            if type_tovar is not None:
                update_fields["type_tovar"] = type_tovar
            if description is not None:
                update_fields["description"] = description
            if code_value is not None:
                update_fields["code"] = code_value
            if primary_photo is not None:
                update_fields["img"] = primary_photo
            if length_mm is not None:
                update_fields["length_mm"] = length_mm
            if width_mm is not None:
                update_fields["width_mm"] = width_mm
            if height_mm is not None:
                update_fields["height_mm"] = height_mm
            if volume is not None:
                update_fields["volume"] = volume
            if weight_kg is not None:
                update_fields["weight_kg"] = weight_kg
            if weight_net_kg is not None:
                update_fields["weight_net_kg"] = weight_net_kg
            if weight_gross_effective_kg is not None:
                update_fields["weight_gross_kg"] = weight_gross_effective_kg
            if cr_product_date is not None:
                update_fields["cr_product_date"] = cr_product_date
            if end_product_date is not None:
                update_fields["end_product_date"] = end_product_date
            if honest_sign is not None:
                update_fields["honest_sign"] = honest_sign
            if use_nds is not None:
                update_fields["use_nds"] = use_nds
            if sign_akciz is not None:
                update_fields["sign_akciz"] = sign_akciz
            if nm_id:
                update_fields["source_reference"] = str(nm_id)

            sku, is_created, binding = _resolve_sku_for_marketplace_item(
                agency=agency,
                marketplace="WB",
                external_id=str(nm_id or "").strip(),
                sku_code=vendor_code,
                barcodes=barcodes,
                defaults=update_fields,
                errors=errors,
            )
            if sku is None:
                continue
            audit_before = None if is_created else sku_audit_snapshot(sku)
            if not _apply_marketplace_sku_code_update(
                agency=agency,
                sku=sku,
                new_sku_code=vendor_code,
                marketplace="WB",
                external_id=str(nm_id or "").strip(),
                errors=errors,
            ):
                continue
            if not is_created:
                changed = _apply_marketplace_updates(
                    sku=sku,
                    update_fields=update_fields,
                )
                if changed:
                    sku.save()
            if is_created:
                created += 1
            else:
                updated += 1
            processed += 1

            if nm_id:
                MarketplaceBinding.objects.update_or_create(
                    marketplace="WB",
                    external_id=str(nm_id),
                    defaults={
                        "sku": sku,
                        "last_synced_at": now,
                    },
                )

            if photo_urls:
                existing_photos = set(SKUPhoto.objects.filter(sku=sku).values_list("url", flat=True))
                for idx, url in enumerate(photo_urls):
                    if url in existing_photos:
                        continue
                    SKUPhoto.objects.create(sku=sku, url=url, sort_order=idx)

            if barcodes:
                existing_barcodes = {bc.value: bc for bc in SKUBarcode.objects.filter(sku=sku)}
                has_primary = any(bc.is_primary for bc in existing_barcodes.values())
                primary_set = False
                if size_barcodes:
                    for size_value, values in size_barcodes.items():
                        size_label = views._trim(size_value, 64) if size_value else None
                        for value in values:
                            if value in existing_barcodes:
                                existing = existing_barcodes[value]
                                if size_label and existing.size != size_label:
                                    existing.size = size_label
                                    existing.save(update_fields=["size"])
                                continue
                            if SKUBarcode.objects.filter(agency=agency, value=value).exists():
                                continue
                            try:
                                SKUBarcode.objects.create(
                                    sku=sku,
                                    value=value,
                                    size=size_label,
                                    is_primary=not has_primary and not primary_set,
                                )
                            except IntegrityError:
                                continue
                            barcode_created += 1
                            if not has_primary and not primary_set:
                                primary_set = True
                                has_primary = True
                else:
                    for idx, value in enumerate(barcodes):
                        if value in existing_barcodes:
                            continue
                        if SKUBarcode.objects.filter(agency=agency, value=value).exists():
                            continue
                        try:
                            SKUBarcode.objects.create(
                                sku=sku,
                                value=value,
                                is_primary=not has_primary and idx == 0,
                            )
                        except IntegrityError:
                            continue
                        barcode_created += 1
                        if idx == 0:
                            has_primary = True

            _log_marketplace_sku_audit(
                sku=sku,
                before=audit_before,
                marketplace="WB",
                is_created=is_created,
            )

        if cursor_data and cursor_data.get("updatedAt") and cursor_data.get("nmID") is not None:
            cursor = {
                "limit": cursor.get("limit", 100),
                "updatedAt": cursor_data["updatedAt"],
                "nmID": cursor_data["nmID"],
            }
        else:
            break

    finished_at = timezone.now()
    report = MarketSyncReport.objects.create(
        agency=agency,
        marketplace="WB",
        status="ok" if not errors else "error",
        started_at=started_at,
        finished_at=finished_at,
        duration_sec=(finished_at - started_at).total_seconds(),
        processed=processed,
        created=created,
        updated=updated,
        barcodes_created=barcode_created,
        errors=errors,
    )
    return JsonResponse(
        {
            "ok": not errors,
            "processed": processed,
            "created": created,
            "updated": updated,
            "barcodes_created": barcode_created,
            "errors": errors,
            "report_id": report.id,
            "report_url": _report_link(report),
            "duration_sec": report.duration_sec,
        }
    )


def _run_ozon_sync_request_single(*, body) -> JsonResponse:
    views = _views()
    payload = _parse_payload(body)
    sku_code_filter, external_id_filter = _sync_filters(payload)

    client_id = payload.get("client")
    if not client_id:
        return JsonResponse(
            {
                "ok": False,
                "errors": [
                    "Не указан клиент.",
                    "Передайте ID клиента в JSON-теле запроса: {\"client\": <id>}.",
                ],
            },
            status=400,
        )

    agency = Agency.objects.filter(pk=client_id).first()
    if not agency:
        return JsonResponse(
            {
                "ok": False,
                "errors": [f"Клиент с ID {client_id} не найден в базе."],
            },
            status=404,
        )

    ozon_market = Market.objects.filter(name__iexact="OZON").first()
    if not ozon_market:
        return JsonResponse(
            {
                "ok": False,
                "errors": ["Маркетплейс Ozon не найден в справочнике (Market.name=OZON)."],
            },
            status=400,
        )

    requested_ozon_client_id = views._normalize_ozon_client_id(payload.get("ozon_client_id"))
    slot_credential = None
    if requested_ozon_client_id:
        slot_credential = FbsOzonCredential.objects.filter(
            agency=agency,
            client_id=requested_ozon_client_id,
        ).first()
        if not slot_credential:
            return JsonResponse(
                {
                    "ok": False,
                    "errors": [
                        f"Кабинет Ozon с Client ID {requested_ozon_client_id} не найден у клиента."
                    ],
                },
                status=400,
            )

    credential = None
    if slot_credential:
        token = (slot_credential.api_key or "").strip()
        client_id_value = views._normalize_ozon_client_id(slot_credential.client_id)
    else:
        credential = MarketCredential.objects.filter(agency=agency, market=ozon_market).first()
        token = (credential.market_key or "").strip() if credential else ""
        client_id_value = views._normalize_ozon_client_id(credential.client_id) if credential else ""
    if not token or not client_id_value:
        missing = []
        if not client_id_value:
            missing.append("Не указан Client ID Ozon для клиента.")
        if not token:
            missing.append("Не указан API ключ Ozon для клиента.")
        return JsonResponse(
            {"ok": False, "errors": missing or ["Не указан Client ID или API ключ Ozon."]},
            status=400,
        )
    if not client_id_value.isdigit() or int(client_id_value) <= 0:
        return JsonResponse(
            {
                "ok": False,
                "errors": [
                    "Client ID Ozon должен быть положительным числом.",
                    "Проверьте значение Client ID в настройках Ozon.",
                ],
            },
            status=400,
        )

    started_at = timezone.now()
    created = 0
    updated = 0
    processed = 0
    barcode_created = 0
    errors = []
    now = timezone.now()

    list_items = []
    seen_product_keys = set()
    for visibility in ("ALL", "ARCHIVED"):
        last_id = ""
        for _ in range(50):
            list_payload = {
                "filter": {"visibility": visibility},
                "last_id": last_id,
                "limit": 1000,
            }
            data, error = views._ozon_post("/v3/product/list", client_id_value, token, list_payload)
            if error:
                errors.append(f"{visibility}: {error}")
                break
            result = (data or {}).get("result") or {}
            items = result.get("items") or []
            if not items:
                break
            for item in items:
                product_id = item.get("product_id")
                offer_id = str(item.get("offer_id") or item.get("offerId") or "").strip()
                product_key = (
                    f"product:{product_id}"
                    if product_id is not None
                    else f"offer:{offer_id}"
                )
                if product_key in seen_product_keys:
                    continue
                seen_product_keys.add(product_key)
                list_items.append(item)
            next_last_id = result.get("last_id") or ""
            if not next_last_id or next_last_id == last_id:
                break
            last_id = next_last_id
        if errors:
            break

    if not errors and not list_items:
        finished_at = timezone.now()
        report = MarketSyncReport.objects.create(
            agency=agency,
            marketplace="OZON",
            status="ok",
            started_at=started_at,
            finished_at=finished_at,
            duration_sec=(finished_at - started_at).total_seconds(),
            processed=0,
            created=0,
            updated=0,
            barcodes_created=0,
            errors=[],
        )
        return JsonResponse(
            {
                "ok": True,
                "processed": 0,
                "created": 0,
                "updated": 0,
                "barcodes_created": 0,
                "errors": [],
                "report_id": report.id,
                "report_url": _report_link(report),
                "duration_sec": report.duration_sec,
            }
        )

    product_ids = []
    offer_by_product = {}
    for item in list_items:
        product_id = item.get("product_id")
        offer_id = item.get("offer_id") or item.get("offerId")
        if not _matches_marketplace_item(
            sku_code=str(offer_id or "").strip(),
            external_id=str(product_id or "").strip(),
            sku_code_filter=sku_code_filter,
            external_id_filter=external_id_filter,
        ):
            continue
        if product_id is None:
            continue
        product_ids.append(product_id)
        if offer_id:
            offer_by_product[product_id] = str(offer_id)

    def _chunked(values, size):
        for idx in range(0, len(values), size):
            yield values[idx : idx + size]

    attr_item_by_product = {}
    info_items = []
    if not errors:
        for batch in _chunked(product_ids, 100):
            info_payload = {"product_id": batch}
            data, error = views._ozon_post("/v3/product/info/list", client_id_value, token, info_payload)
            if error:
                errors.append(error)
                break
            info_items.extend((data or {}).get("items") or [])

            attr_payload = {
                "filter": {"product_id": batch},
                "limit": 1000,
            }
            attr_data, attr_error = views._ozon_post(
                "/v4/product/info/attributes", client_id_value, token, attr_payload
            )
            if attr_error:
                errors.append(attr_error)
                continue
            attr_result = (attr_data or {}).get("result")
            if isinstance(attr_result, dict):
                entries = attr_result.get("items") or []
            elif isinstance(attr_result, list):
                entries = attr_result
            else:
                entries = []
            for entry in entries:
                entry_product_id = entry.get("product_id") or entry.get("id")
                if entry_product_id is not None:
                    attr_item_by_product[entry_product_id] = entry

    for item in info_items:
        product_id = item.get("product_id") or item.get("id")
        offer_id = item.get("offer_id") or offer_by_product.get(product_id)
        if not _matches_marketplace_item(
            sku_code=str(offer_id or "").strip(),
            external_id=str(product_id or "").strip(),
            sku_code_filter=sku_code_filter,
            external_id_filter=external_id_filter,
        ):
            continue
        if not offer_id:
            continue
        offer_id = str(offer_id).strip()
        if not offer_id:
            continue
        if len(offer_id) > 64:
            offer_id = offer_id[:64]

        attr_item = attr_item_by_product.get(product_id) or {}
        attributes = item.get("attributes") or attr_item.get("attributes") or []
        name = views._trim(item.get("name") or item.get("title"), 255) or offer_id
        brand = views._trim(item.get("brand") or views._ozon_find_attr(attributes, ["бренд"]), 255)
        color = views._trim(views._ozon_find_attr(attributes, ["цвет"]), 64)
        size = views._trim(views._ozon_find_attr(attributes, ["размер"]), 64)
        composition = views._trim(views._ozon_find_attr(attributes, ["состав", "материал"]), 255)
        gender = views._trim(views._ozon_find_attr(attributes, ["пол"]), 64)
        season = views._trim(views._ozon_find_attr(attributes, ["сезон"]), 64)
        made_in = views._trim(views._ozon_find_attr(attributes, ["страна"]), 128)
        tovar_category = views._trim(
            views._ozon_find_attr(attributes, ["категория", "тип товара", "предмет", "назначение"]),
            128,
        )
        description = views._normalize_text(item.get("description"))

        raw_weight = views._extract_first(
            [
                attr_item.get("weight"),
                item.get("weight"),
                item.get("weight_g"),
                item.get("weight_kg"),
            ]
        )
        weight_unit = views._extract_first(
            [attr_item.get("weight_unit"), item.get("weight_unit")]
        )
        weight_kg = (
            views._parse_weight_kg(f"{raw_weight} {weight_unit}")
            if raw_weight is not None and weight_unit
            else views._ozon_weight_kg(raw_weight)
        )
        dimensions = item.get("dimensions") if isinstance(item.get("dimensions"), dict) else {}
        attr_dimensions = (
            attr_item.get("dimensions")
            if isinstance(attr_item.get("dimensions"), dict)
            else {}
        )
        dimension_unit = views._extract_first(
            [attr_item.get("dimension_unit"), item.get("dimension_unit")]
        ) or "mm"
        length_mm = views._parse_length_mm(
            views._extract_first(
                [
                    attr_item.get("depth"),
                    attr_item.get("length"),
                    attr_dimensions.get("length"),
                    item.get("depth"),
                    item.get("length"),
                    dimensions.get("length"),
                ]
            ),
            default_unit=dimension_unit,
        )
        width_mm = views._parse_length_mm(
            views._extract_first(
                [
                    attr_item.get("width"),
                    attr_dimensions.get("width"),
                    item.get("width"),
                    dimensions.get("width"),
                ]
            ),
            default_unit=dimension_unit,
        )
        height_mm = views._parse_length_mm(
            views._extract_first(
                [
                    attr_item.get("height"),
                    attr_dimensions.get("height"),
                    item.get("height"),
                    dimensions.get("height"),
                ]
            ),
            default_unit=dimension_unit,
        )
        volume = views._parse_volume(
            attr_item.get("volume")
            or attr_dimensions.get("volume")
            or item.get("volume")
            or dimensions.get("volume")
        )

        raw_images = item.get("images") or []
        if not isinstance(raw_images, (list, tuple)):
            raw_images = [raw_images]
        images = []
        for raw_image in raw_images:
            image_url = _ozon_first_image_url(raw_image)
            if image_url and image_url not in images:
                images.append(image_url)
        primary_image = _ozon_first_image_url(item.get("primary_image"), images)

        barcodes = item.get("barcodes") or item.get("barcode") or []
        if isinstance(barcodes, str):
            barcodes = [barcodes]
        barcodes = [str(value) for value in barcodes if value]
        ozon_sku_values = _ozon_marketplace_sku_values(item)
        code_value = views._trim(barcodes[0], 128) if barcodes else None

        update_fields = {
            "name": name,
            "market": ozon_market,
            "source": "marketplace",
            "name_print": name,
        }
        if brand is not None:
            update_fields["brand"] = brand
        if color is not None:
            update_fields["color"] = color
        if size is not None:
            update_fields["size"] = size
        if composition is not None:
            update_fields["composition"] = composition
        if gender is not None:
            update_fields["gender"] = gender
        if season is not None:
            update_fields["season"] = season
        if made_in is not None:
            update_fields["made_in"] = made_in
        if tovar_category is not None:
            update_fields["tovar_category"] = tovar_category
        if description is not None:
            update_fields["description"] = description
        if code_value is not None:
            update_fields["code"] = code_value
        if primary_image is not None:
            update_fields["img"] = primary_image
        if length_mm is not None:
            update_fields["length_mm"] = length_mm
        if width_mm is not None:
            update_fields["width_mm"] = width_mm
        if height_mm is not None:
            update_fields["height_mm"] = height_mm
        if volume is not None:
            update_fields["volume"] = volume
        if weight_kg is not None:
            update_fields["weight_kg"] = weight_kg
            update_fields["weight_gross_kg"] = weight_kg
        if product_id is not None:
            update_fields["source_reference"] = str(product_id)

        sku, is_created, binding = _resolve_sku_for_marketplace_item(
            agency=agency,
            marketplace="OZON",
            external_id=str(product_id) if product_id is not None else "",
            sku_code=offer_id,
            barcodes=barcodes,
            defaults=update_fields,
            errors=errors,
        )
        if sku is None:
            continue
        audit_before = None if is_created else sku_audit_snapshot(sku)
        if not _apply_marketplace_sku_code_update(
            agency=agency,
            sku=sku,
            new_sku_code=offer_id,
            marketplace="OZON",
            external_id=str(product_id or "").strip(),
            errors=errors,
        ):
            continue
        if not is_created:
            changed = _apply_marketplace_updates(
                sku=sku,
                update_fields=update_fields,
            )
            if changed:
                sku.save()
        if product_id is not None:
            MarketplaceBinding.objects.update_or_create(
                marketplace=MarketplaceBinding.MARKETPLACE_OZON,
                external_id=str(product_id),
                defaults={
                    "sku": sku,
                    "last_synced_at": now,
                },
            )
        if not _sync_ozon_sku_bindings(
            sku=sku,
            ozon_sku_values=ozon_sku_values,
            synced_at=now,
            errors=errors,
        ):
            continue
        if is_created:
            created += 1
        else:
            updated += 1
        processed += 1

        if images:
            existing_photos = set(SKUPhoto.objects.filter(sku=sku).values_list("url", flat=True))
            for idx, url in enumerate(images):
                if url in existing_photos:
                    continue
                SKUPhoto.objects.create(sku=sku, url=url, sort_order=idx)

        if barcodes:
            existing_barcodes = {bc.value: bc for bc in SKUBarcode.objects.filter(sku=sku)}
            has_primary = any(bc.is_primary for bc in existing_barcodes.values())
            for idx, value in enumerate(barcodes):
                if value in existing_barcodes:
                    existing = existing_barcodes[value]
                    if size and existing.size != size:
                        existing.size = size
                        existing.save(update_fields=["size"])
                    continue
                if SKUBarcode.objects.filter(agency=agency, value=value).exists():
                    continue
                try:
                    SKUBarcode.objects.create(
                        sku=sku,
                        value=value,
                        size=size,
                        is_primary=not has_primary and idx == 0,
                    )
                except IntegrityError:
                    continue
                barcode_created += 1
                if idx == 0:
                    has_primary = True

        _log_marketplace_sku_audit(
            sku=sku,
            before=audit_before,
            marketplace="OZON",
            is_created=is_created,
        )

    finished_at = timezone.now()
    report = MarketSyncReport.objects.create(
        agency=agency,
        marketplace="OZON",
        status="ok" if not errors else "error",
        started_at=started_at,
        finished_at=finished_at,
        duration_sec=(finished_at - started_at).total_seconds(),
        processed=processed,
        created=created,
        updated=updated,
        barcodes_created=barcode_created,
        errors=errors,
    )
    return JsonResponse(
        {
            "ok": not errors,
            "processed": processed,
            "created": created,
            "updated": updated,
            "barcodes_created": barcode_created,
            "errors": errors,
            "report_id": report.id,
            "report_url": _report_link(report),
            "duration_sec": report.duration_sec,
        }
    )


def run_ozon_sync_request(*, body) -> JsonResponse:
    """Synchronize every configured Ozon cabinet for an agency."""
    payload = _parse_payload(body)
    if payload.get("ozon_client_id"):
        return _run_ozon_sync_request_single(body=body)

    agency_id = payload.get("client")
    if not agency_id:
        return _run_ozon_sync_request_single(body=body)
    try:
        agency_id = int(agency_id)
    except (TypeError, ValueError):
        return _run_ozon_sync_request_single(body=body)

    credentials = list(
        FbsOzonCredential.objects.filter(agency_id=agency_id)
        .exclude(client_id="")
        .order_by("slot", "id")
    )
    if not credentials:
        return _run_ozon_sync_request_single(body=body)

    summary = {
        "ok": True,
        "processed": 0,
        "created": 0,
        "updated": 0,
        "barcodes_created": 0,
        "errors": [],
        "report_ids": [],
        "report_urls": [],
        "duration_sec": 0.0,
        "accounts": [],
    }
    for credential in credentials:
        account_payload = dict(payload)
        account_payload["ozon_client_id"] = credential.client_id
        response = _run_ozon_sync_request_single(
            body=json.dumps(account_payload).encode("utf-8")
        )
        try:
            result = json.loads(response.content.decode("utf-8") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError):
            result = {}
        account_errors = [str(error) for error in (result.get("errors") or [])]
        account_ok = bool(response.status_code < 400 and result.get("ok"))
        account_result = {
            "slot": credential.slot,
            "client_id": credential.client_id,
            "ok": account_ok,
            "processed": int(result.get("processed") or 0),
            "created": int(result.get("created") or 0),
            "updated": int(result.get("updated") or 0),
            "barcodes_created": int(result.get("barcodes_created") or 0),
            "errors": account_errors,
            "report_id": result.get("report_id"),
            "report_url": result.get("report_url") or "",
        }
        summary["accounts"].append(account_result)
        for field in ("processed", "created", "updated", "barcodes_created"):
            summary[field] += account_result[field]
        summary["duration_sec"] += float(result.get("duration_sec") or 0)
        if account_result["report_id"]:
            summary["report_ids"].append(account_result["report_id"])
        if account_result["report_url"]:
            summary["report_urls"].append(account_result["report_url"])
        if not account_ok:
            summary["ok"] = False
            if account_errors:
                summary["errors"].extend(
                    [
                        f"Кабинет {credential.slot} (Client ID {credential.client_id}): {error}"
                        for error in account_errors
                    ]
                )
            else:
                summary["errors"].append(
                    f"Кабинет {credential.slot} (Client ID {credential.client_id}): синхронизация завершилась с ошибкой."
                )

    return JsonResponse(summary, status=200 if summary["ok"] else 207)
