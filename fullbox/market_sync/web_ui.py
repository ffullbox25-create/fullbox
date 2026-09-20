"""Marketplace sync UI views and helper logic."""

import json
import datetime
import re
from decimal import Decimal, InvalidOperation

import requests
from django.db import IntegrityError
from django.http import JsonResponse
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.http import require_POST

from employees.access import role_required

from sku.models import Agency, Market, MarketCredential, SKU, SKUBarcode, SKUPhoto, MarketplaceBinding


OZON_API_BASE = "https://api-seller.ozon.ru"

from .models import MarketSyncReport
from .http import friendly_network_error, marketplace_request
from .services import (
    build_dashboard_context,
    build_report_detail_response,
    prepare_ozon_settings_page,
    prepare_wb_settings_page,
    submit_ozon_settings,
    submit_wb_settings,
)
from .sync_services import run_ozon_sync_request, run_wb_sync_request
from .sync_all import (
    configured_global_sync_targets as _configured_global_sync_targets,
    marketplace_sync_code as _marketplace_sync_code,
    run_sync_all_clients,
)


MARKET_SYNC_ROLES = ("admin", "director", "head_manager", "manager", "developer")


@role_required(*MARKET_SYNC_ROLES)
def dashboard(request):
    return render(
        request,
        "market_sync/dashboard.html",
        build_dashboard_context(client_id=request.GET.get("client")),
    )


@role_required(*MARKET_SYNC_ROLES)
def wb_settings(request):
    if request.method == "POST":
        response, context = submit_wb_settings(
            client_id=request.POST.get("client"),
            post_data=request.POST,
        )
    else:
        response, context = prepare_wb_settings_page(
            client_id=request.GET.get("client"),
        )
    if response is not None:
        return response
    return render(
        request,
        "market_sync/wb_settings.html",
        context,
    )


@role_required(*MARKET_SYNC_ROLES)
def ozon_settings(request):
    if request.method == "POST":
        response, context = submit_ozon_settings(
            client_id=request.POST.get("client"),
            post_data=request.POST,
        )
    else:
        response, context = prepare_ozon_settings_page(
            client_id=request.GET.get("client"),
        )
    if response is not None:
        return response
    return render(
        request,
        "market_sync/ozon_settings.html",
        context,
    )


@role_required(*MARKET_SYNC_ROLES)
def report_detail(request, report_id: int):
    return build_report_detail_response(report_id=report_id)


def _configured_client_sync_target(agency) -> dict | None:
    markets = []
    credentials = (
        MarketCredential.objects.select_related("market")
        .filter(agency=agency)
        .order_by("market__name", "id")
    )
    for credential in credentials:
        code = _marketplace_sync_code(getattr(getattr(credential, "market", None), "name", ""))
        if code == "WB":
            if not (credential.market_key or "").strip():
                continue
        elif code == "OZON":
            if not (credential.market_key or "").strip() or not (credential.client_id or "").strip():
                continue
        else:
            continue
        if code not in markets:
            markets.append(code)
    if not markets:
        return None
    return {
        "agency": agency,
        "markets": markets,
    }


def _decode_sync_response(response) -> dict:
    try:
        payload = json.loads(response.content.decode("utf-8") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError):
        payload = {}
    return payload if isinstance(payload, dict) else {}


def _extract_first(values):
    for value in values:
        if value:
            return value
    return None


def _extract_color(card):
    colors = card.get("colors")
    if isinstance(colors, list) and colors:
        first = colors[0]
        if isinstance(first, dict):
            return first.get("name") or first.get("value")
        return str(first)
    return None


def _extract_size(card):
    sizes = card.get("sizes")
    if not isinstance(sizes, list):
        return None
    for size in sizes:
        if not isinstance(size, dict):
            continue
        value = _extract_first([size.get("techSize"), size.get("wbSize"), size.get("size")])
        if value:
            return value
    return None


def _extract_size_barcodes(card):
    size_map = {}
    sizes = card.get("sizes")
    if not isinstance(sizes, list):
        return size_map
    for size in sizes:
        if not isinstance(size, dict):
            continue
        size_value = _normalize_text(
            _extract_first([size.get("techSize"), size.get("wbSize"), size.get("size")])
        )
        skus = size.get("skus") or []
        if isinstance(skus, list):
            for sku in skus:
                if sku:
                    key = size_value or ""
                    size_map.setdefault(key, []).append(str(sku))
    for key, values in size_map.items():
        size_map[key] = list(dict.fromkeys(values))
    return size_map


def _flatten_size_barcodes(size_map):
    barcodes = []
    for values in size_map.values():
        barcodes.extend(values)
    return list(dict.fromkeys(barcodes))


def _extract_barcodes(card):
    return _flatten_size_barcodes(_extract_size_barcodes(card))


def _normalize_text(value):
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        parts = [str(v).strip() for v in value if v]
        text = ", ".join([part for part in parts if part])
        return text or None
    text = str(value).strip()
    return text or None


def _extract_characteristics(card):
    chars = (
        card.get("characteristics")
        or card.get("characteristicsFull")
        or card.get("characteristics_short")
    )
    if not isinstance(chars, list):
        return []
    items = []
    for item in chars:
        if not isinstance(item, dict):
            continue
        name = (item.get("name") or item.get("charName") or "").strip()
        value = item.get("value")
        if value is None:
            value = item.get("values")
        if value is None:
            value = item.get("valueName")
        if value is None:
            value = item.get("valueId")
        value_text = _normalize_text(value)
        if name and value_text:
            items.append((name.lower(), value_text))
    return items


def _find_char_value(chars, names):
    for needle in names:
        needle = needle.lower()
        for char_name, value in chars:
            if needle in char_name:
                return value
    return None


def _parse_decimal(value):
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    text = str(value).strip().replace(",", ".")
    match = re.search(r"([0-9]+(?:\.[0-9]+)?)", text)
    if not match:
        return None
    try:
        return Decimal(match.group(1))
    except InvalidOperation:
        return None


def _parse_length_mm(value, default_unit="cm"):
    if value is None:
        return None
    if isinstance(value, (int, float, Decimal)):
        number = Decimal(str(value))
        unit = default_unit
    else:
        text = str(value).strip().lower().replace(",", ".")
        match = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*([a-zа-я]+)?", text)
        if not match:
            return None
        number = Decimal(match.group(1))
        unit = match.group(2) or default_unit
    if "мм" in unit or "mm" in unit:
        return number
    if "см" in unit or "cm" in unit:
        return number * Decimal("10")
    if unit in ("м", "m") or "метр" in unit:
        return number * Decimal("1000")
    return number * Decimal("10") if default_unit == "cm" else number


def _parse_weight_kg(value):
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    text = str(value).strip().lower().replace(",", ".")
    match = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*([a-zа-я]+)?", text)
    if not match:
        return None
    number = Decimal(match.group(1))
    unit = match.group(2) or ""
    if "кг" in unit or "kg" in unit:
        return number
    if ("г" in unit and "кг" not in unit) or unit == "g":
        return number / Decimal("1000")
    return number


def _parse_volume(value):
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    text = str(value).strip().lower().replace(",", ".")
    match = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*([a-zа-я0-9³^]+)?", text)
    if not match:
        return None
    number = Decimal(match.group(1))
    unit = (match.group(2) or "").strip()
    if "см3" in unit or "см³" in unit:
        return number / Decimal("1000")
    if "м3" in unit or "м³" in unit:
        return number * Decimal("1000")
    return number


def _parse_date(value):
    if value is None:
        return None
    if isinstance(value, datetime.date):
        return value
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y"):
        try:
            return datetime.datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _parse_flag(value):
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float, Decimal)):
        return value > 0
    text = str(value).strip().lower()
    if any(token in text for token in ("нет", "без", "no", "false", "0")):
        return False
    if any(token in text for token in ("да", "yes", "true", "1", "есть")):
        return True
    if re.search(r"\d", text):
        return True
    return None


def _extract_photos(card):
    photos = card.get("photos") or []
    urls = []
    if isinstance(photos, list):
        for photo in photos:
            if isinstance(photo, dict):
                url = _extract_first(
                    [
                        photo.get("big"),
                        photo.get("square"),
                        photo.get("tm"),
                        photo.get("c246x328"),
                        photo.get("c516x688"),
                    ]
                )
            else:
                url = str(photo)
            if url:
                urls.append(url)
    return list(dict.fromkeys(urls))


def _trim(value, max_len):
    text = _normalize_text(value)
    if not text:
        return None
    return text[:max_len]


def _ozon_headers(client_id: str, api_key: str) -> dict:
    return {
        "Client-Id": client_id,
        "Api-Key": api_key,
        "Content-Type": "application/json",
    }


def _normalize_ozon_client_id(value: str) -> str:
    text = (value or "").strip()
    if not text:
        return ""
    match = re.fullmatch(r"(\d+)(?:\.0+)?", text)
    if match:
        return match.group(1)
    return text


def _ozon_post(path: str, client_id: str, api_key: str, payload: dict, timeout: int = 30):
    url = f"{OZON_API_BASE}{path}"
    try:
        response = marketplace_request(
            "POST",
            url,
            headers=_ozon_headers(client_id, api_key),
            json=payload,
            timeout=timeout,
        )
    except requests.RequestException as exc:
        return None, friendly_network_error(exc, "Ozon")
    if response.status_code != 200:
        snippet = (response.text or "").strip()
        if len(snippet) > 200:
            snippet = f"{snippet[:200]}..."
        detail = f": {snippet}" if snippet else ""
        return None, f"Ozon API ошибка {response.status_code}{detail}"
    try:
        data = response.json()
    except ValueError:
        return None, "Ozon API вернул некорректный JSON."
    return data, None


def _ozon_attr_value(attr: dict):
    values = attr.get("values")
    if isinstance(values, list) and values:
        first = values[0]
        if isinstance(first, dict):
            return _normalize_text(first.get("value") or first.get("dictionary_value_id"))
        return _normalize_text(first)
    return _normalize_text(attr.get("value") or attr.get("value_name"))


def _ozon_find_attr(attributes: list, names: list[str]):
    for attr in attributes:
        name = (attr.get("attribute_name") or attr.get("name") or "").strip().lower()
        if not name:
            continue
        for needle in names:
            if needle in name:
                return _ozon_attr_value(attr)
    return None


def _ozon_weight_kg(value):
    if value is None:
        return None
    if isinstance(value, (int, float, Decimal)):
        number = Decimal(str(value))
        if number > 50:
            return number / Decimal("1000")
        return number
    return _parse_weight_kg(value)


def _report_link(report: MarketSyncReport | None) -> str:
    if not report:
        return ""
    return f"/market-sync/report/{report.id}/"


@role_required(*MARKET_SYNC_ROLES)
@require_POST
def wb_sync_run(request):
    return run_wb_sync_request(body=request.body)


@role_required(*MARKET_SYNC_ROLES)
@require_POST
def ozon_sync_run(request):
    return run_ozon_sync_request(body=request.body)


@role_required(*MARKET_SYNC_ROLES)
@require_POST
def sync_client_run(request):
    try:
        payload = json.loads(request.body.decode("utf-8") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError):
        payload = {}
    client_id = payload.get("client")
    if not client_id:
        return JsonResponse({"ok": False, "errors": ["Не указан клиент для синхронизации."]}, status=400)
    try:
        client_id = int(client_id)
    except (TypeError, ValueError):
        return JsonResponse({"ok": False, "errors": ["Некорректный идентификатор клиента."]}, status=400)

    agency = Agency.objects.filter(pk=client_id, archived=False).first()
    if not agency:
        return JsonResponse({"ok": False, "errors": ["Клиент не найден."]}, status=404)

    target = _configured_client_sync_target(agency)
    if not target:
        return JsonResponse(
            {
                "ok": False,
                "errors": ["Для клиента не настроены поддерживаемые маркетплейсы WB или Ozon."],
            },
            status=400,
        )

    agency_name = str(getattr(agency, "agn_name", "") or getattr(agency, "fio_agn", "") or agency.id)
    summary = {
        "agency_id": agency.id,
        "agency_name": agency_name,
        "markets": [],
        "processed": 0,
        "created": 0,
        "updated": 0,
        "barcodes_created": 0,
        "errors": [],
    }
    successful_markets = 0

    for market_code in target["markets"]:
        body = json.dumps({"client": agency.id}).encode("utf-8")
        response = run_wb_sync_request(body=body) if market_code == "WB" else run_ozon_sync_request(body=body)
        data = _decode_sync_response(response)
        market_errors = [str(item) for item in (data.get("errors") or []) if str(item).strip()]
        processed_count = int(data.get("processed") or 0)
        created_count = int(data.get("created") or 0)
        updated_count = int(data.get("updated") or 0)
        barcodes_count = int(data.get("barcodes_created") or 0)
        market_has_data = bool(response.status_code < 400 and (data.get("ok") or processed_count > 0))
        market_ok = bool(market_has_data and data.get("ok") is not False and not market_errors)
        if market_has_data:
            successful_markets += 1
        summary["markets"].append(
            {
                "marketplace": market_code,
                "ok": market_ok,
                "processed": processed_count,
                "created": created_count,
                "updated": updated_count,
                "barcodes_created": barcodes_count,
                "errors": market_errors,
            }
        )
        summary["processed"] += processed_count
        summary["created"] += created_count
        summary["updated"] += updated_count
        summary["barcodes_created"] += barcodes_count
        if response.status_code >= 400 or market_errors or data.get("ok") is False:
            fallback_errors = market_errors or [f"Ошибка синхронизации {market_code}."]
            summary["errors"].extend([f"{market_code}: {error}" for error in fallback_errors])

    partial = successful_markets > 0 and bool(summary["errors"])
    ok = bool(successful_markets) and not summary["errors"]
    return JsonResponse(
        {
            "ok": ok,
            "partial": partial,
            **summary,
        },
        status=200 if (ok or partial) else 400,
    )


@role_required(*MARKET_SYNC_ROLES)
@require_POST
def sync_all_run(request):
    summary = run_sync_all_clients(pause_seconds=0)
    if summary.get("clients_total") == 0:
        return JsonResponse(
            {
                "ok": False,
                "errors": summary.get("errors")
                or ["Нет клиентов с настроенными маркетплейсами WB или Ozon."],
                "clients_total": 0,
                "clients_processed": 0,
            },
            status=400,
        )
    return JsonResponse(
        {
            "ok": bool(summary.get("ok")),
            "partial": bool(summary.get("partial")),
            "clients_total": summary.get("clients_total"),
            "clients_processed": summary.get("clients_processed"),
            "clients_with_errors": summary.get("clients_with_errors"),
            "processed": summary.get("processed"),
            "created": summary.get("created"),
            "updated": summary.get("updated"),
            "barcodes_created": summary.get("barcodes_created"),
            "errors": summary.get("errors") or [],
            "results": summary.get("results") or [],
        },
        status=200,
    )
