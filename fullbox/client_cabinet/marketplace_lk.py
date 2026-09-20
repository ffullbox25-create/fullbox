from __future__ import annotations

import json
import sys
import time
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlparse

import requests
import urllib3
from django.core.cache import cache
from django.db.models import Q, Sum
from django.http import JsonResponse
from django.utils import timezone

from fullbox.file_locks import acquire_file_lock_nonblocking, release_file_lock

from market_sync.http import (
    auth_error_message,
    friendly_network_error,
    is_auth_http_status,
    marketplace_request,
)
from market_sync.models import MarketSyncReport
from market_sync.sync_services import run_ozon_sync_request, run_wb_sync_request
from shipping.models import ShippingOrder
from sklad.models import WarehouseStockSnapshot
from sku.models import MarketCredential, MarketplaceBinding, SKU, SKUBarcode

SHIPPING_DONE_STATUSES = {
    ShippingOrder.STATUS_SHIPPED,
    ShippingOrder.STATUS_PARTIAL,
    ShippingOrder.STATUS_CANCELED,
    "delivered",
    "completed",
    "closed",
    "done",
    "cancelled",
    "canceled",
}

SHIPPING_LK_FIELDS = (
    "id",
    "agency_id",
    "marketplace_id",
    "number",
    "status",
    "slot_date",
    "slot_time",
    "delivery_type",
    "destination_warehouse",
    "wb_supply_barcode",
    "shipping_barcode",
)

STOCK_CACHE_TTL_SECONDS = 12 * 60
AUTH_CACHE_TTL_SECONDS = 30 * 60
DNS_CACHE_TTL_SECONDS = 5 * 60
DOH_URLS = ("https://1.1.1.1/dns-query", "https://8.8.8.8/resolve")
DIRECT_API_HOSTS = {
    "api-seller.ozon.ru",
    "content-api.wildberries.ru",
    "marketplace-api.wildberries.ru",
    "statistics-api.wildberries.ru",
}
_DNS_CACHE: dict[str, dict[str, Any]] = {}

MARKETPLACES = (
    {"id": "wb", "name": "Wildberries", "short_name": "WB", "market_names": ("WB", "WILDBERRIES")},
    {"id": "ozon", "name": "Ozon", "short_name": "OZON", "market_names": ("OZON",)},
)

_RU_MONTHS = {
    1: "янв.",
    2: "февр.",
    3: "мар.",
    4: "апр.",
    5: "мая",
    6: "июн.",
    7: "июл.",
    8: "авг.",
    9: "сент.",
    10: "окт.",
    11: "нояб.",
    12: "дек.",
}


def format_ru_date(value) -> str:
    if not value:
        return ""
    try:
        local = timezone.localtime(value) if timezone.is_aware(value) else value
    except Exception:
        local = value
    try:
        return f"{local.day} {_RU_MONTHS.get(local.month, '')} {local.year} г."
    except Exception:
        return str(value)[:16]


def _market_label(order) -> str:
    market = getattr(order, "marketplace", None)
    if market is None:
        return ""
    return str(getattr(market, "name", market) or "").strip()


def _is_market(label: str, *needles: str) -> bool:
    upper = (label or "").upper()
    return any(needle in upper for needle in needles)


def _credential_for(agency, market_id: str) -> MarketCredential | None:
    names = {"wb": ("WB", "WILDBERRIES"), "ozon": ("OZON",)}.get(market_id, ())
    qs = MarketCredential.objects.filter(agency=agency).select_related("market")
    for cred in qs:
        name = (getattr(cred.market, "name", "") or "").upper()
        if any(n in name for n in names):
            return cred
    return None


def _sku_count_for(agency, market_id: str) -> int:
    market_names = {"wb": ("WB", "WILDBERRIES"), "ozon": ("OZON",)}.get(market_id, ())
    binding_count = (
        MarketplaceBinding.objects.filter(sku__agency=agency, sku__deleted=False)
        .filter(marketplace__iregex=r"|".join(market_names))
        .values("sku_id")
        .distinct()
        .count()
    )
    if binding_count:
        return binding_count
    return (
        SKU.objects.filter(agency=agency, deleted=False, market__isnull=False)
        .filter(market__name__iregex=r"|".join(market_names))
        .count()
    )


def _latest_report(agency, market_id: str) -> MarketSyncReport | None:
    market_names = {"wb": ("WB", "WILDBERRIES"), "ozon": ("OZON",)}.get(market_id, ())
    for report in MarketSyncReport.objects.filter(agency=agency).order_by("-finished_at", "-started_at"):
        name = (report.marketplace or "").upper()
        if any(n in name for n in market_names):
            return report
    return None


def _credential_configured(cred: MarketCredential | None, market_id: str) -> bool:
    if not cred:
        return False
    token = (cred.market_key or "").strip()
    if not token:
        return False
    if market_id == "ozon":
        return bool((cred.client_id or "").strip())
    return True


def _stocks_cache_key(agency_id: int) -> str:
    return f"lk:mp_stocks:v1:{agency_id}"


def _auth_cache_key(agency_id: int, market_id: str) -> str:
    return f"lk:mp_auth:v1:{agency_id}:{market_id}"


class _DirectResponse:
    def __init__(self, *, status_code: int, data: bytes):
        self.status_code = int(status_code)
        self.content = data or b""
        self.text = self.content.decode("utf-8", "ignore")

    def json(self):
        return json.loads(self.text or "{}")


def _resolve_host_ips(host: str) -> list[str]:
    now = time.monotonic()
    cached = _DNS_CACHE.get(host)
    if cached and cached.get("expires_at", 0) > now:
        return list(cached.get("ips") or [])

    last_error: Exception | None = None
    for url in DOH_URLS:
        try:
            session = requests.Session()
            session.trust_env = False
            response = session.get(
                url,
                params={"name": host, "type": "A"},
                headers={"Accept": "application/dns-json"},
                timeout=4,
            )
            if response.status_code != 200:
                continue
            payload = response.json()
            ips: list[str] = []
            ttls: list[int] = []
            for row in payload.get("Answer") or []:
                if not isinstance(row, dict) or int(row.get("type") or 0) != 1:
                    continue
                ip = str(row.get("data") or "").strip()
                if ip and ip not in ips:
                    ips.append(ip)
                try:
                    ttls.append(int(row.get("TTL") or DNS_CACHE_TTL_SECONDS))
                except (TypeError, ValueError):
                    pass
            if ips:
                ttl = max(30, min(min(ttls or [DNS_CACHE_TTL_SECONDS]), DNS_CACHE_TTL_SECONDS))
                _DNS_CACHE[host] = {"ips": ips, "expires_at": now + ttl}
                return ips
        except Exception as exc:
            last_error = exc
            continue
    if last_error:
        raise requests.RequestException(str(last_error))
    raise requests.RequestException(f"DNS-over-HTTPS did not return IP for {host}")


def _direct_marketplace_request(method: str, url: str, **kwargs):
    parsed = urlparse(url)
    host = parsed.hostname or ""
    if parsed.scheme != "https" or host not in DIRECT_API_HOSTS:
        return marketplace_request(method, url, **kwargs)

    params = kwargs.pop("params", None)
    json_body = kwargs.pop("json", None)
    timeout = int(kwargs.pop("timeout", 40) or 40)
    headers = dict(kwargs.pop("headers", {}) or {})
    if kwargs or "test" in sys.argv:
        return marketplace_request(method, url, params=params, json=json_body, timeout=timeout, headers=headers, **kwargs)

    path = parsed.path or "/"
    query = parsed.query or ""
    if params:
        encoded = urlencode(params, doseq=True)
        query = f"{query}&{encoded}" if query else encoded
    if query:
        path = f"{path}?{query}"

    body = None
    if json_body is not None:
        body = json.dumps(json_body).encode("utf-8")
        headers.setdefault("Content-Type", "application/json")
    headers["Host"] = host

    last_error: Exception | None = None
    for ip in _resolve_host_ips(host)[:3]:
        try:
            pool = urllib3.HTTPSConnectionPool(
                ip,
                port=443,
                assert_hostname=host,
                server_hostname=host,
            )
            response = pool.request(
                method.upper(),
                path,
                body=body,
                headers=headers,
                timeout=urllib3.Timeout(connect=min(timeout, 5), read=timeout),
                retries=False,
            )
            return _DirectResponse(status_code=int(response.status), data=response.data)
        except Exception as exc:
            last_error = exc
            continue
    raise requests.RequestException(str(last_error or f"{host} unavailable"))


def invalidate_marketplace_auth_cache(agency_id: int | None) -> None:
    """Drop cached WB/Ozon auth probes after credentials change."""
    if not agency_id:
        return
    for market_id in ("wb", "ozon"):
        cache.delete(_auth_cache_key(int(agency_id), market_id))
    cache.delete(_stocks_cache_key(int(agency_id)))


def _remember_auth_health(agency, market_id: str, *, auth_ok: bool, message: str = "") -> dict[str, Any]:
    payload = {
        "auth_ok": bool(auth_ok),
        "message": message or "",
        "checked_at": timezone.localtime().isoformat(),
    }
    if agency:
        cache.set(_auth_cache_key(agency.id, market_id), payload, AUTH_CACHE_TTL_SECONDS)
    return payload


def probe_marketplace_auth(agency, market_id: str, *, force_refresh: bool = False) -> dict[str, Any]:
    """Lightweight token health check (cached)."""
    if not agency:
        return {"auth_ok": None, "message": "", "checked_at": ""}
    cache_key = _auth_cache_key(agency.id, market_id)
    if not force_refresh:
        cached = cache.get(cache_key)
        if isinstance(cached, dict) and "auth_ok" in cached:
            return cached

    cred = _credential_for(agency, market_id)
    if not _credential_configured(cred, market_id):
        return _remember_auth_health(agency, market_id, auth_ok=False, message="Ключ не задан")

    token = (cred.market_key or "").strip()
    client_id = (cred.client_id or "").strip()
    label = "WB" if market_id == "wb" else "Ozon"
    try:
        if market_id == "wb":
            # Validate the Content API directly. Fullbox credentials must also include
            # Marketplace scope with read/write access, as documented in the client guide.
            response = _direct_marketplace_request(
                "GET",
                "https://content-api.wildberries.ru/ping",
                headers={"Authorization": token, "Content-Type": "application/json"},
                timeout=20,
            )
        else:
            response = _direct_marketplace_request(
                "POST",
                "https://api-seller.ozon.ru/v2/warehouse/list",
                headers={
                    "Client-Id": client_id,
                    "Api-Key": token,
                    "Content-Type": "application/json",
                },
                json={"limit": 1, "cursor": ""},
                timeout=20,
            )
    except requests.RequestException as exc:
        # Network issues are not auth failures
        return {
            "auth_ok": None,
            "message": friendly_network_error(exc, label),
            "checked_at": timezone.localtime().isoformat(),
        }

    if is_auth_http_status(response.status_code):
        detail = ""
        try:
            payload = response.json() if response.content else {}
            if isinstance(payload, dict):
                detail = str(payload.get("detail") or payload.get("title") or "").strip()
        except Exception:
            detail = ""
        message = auth_error_message(label, response.status_code)
        if "scope" in detail.lower():
            message = (
                f"{label}: у токена нет нужных прав доступа ({detail}). "
                "Создайте новый API-токен WB под владельцем аккаунта с обязательными "
                "категориями «Контент» и «Маркетплейс» и уровнем «Чтение и запись». "
                "Токен «Только чтение» и токен тестового контура не подходят."
            )
        return _remember_auth_health(
            agency,
            market_id,
            auth_ok=False,
            message=message,
        )
    if response.status_code >= 400:
        return {
            "auth_ok": None,
            "message": f"{label}: проверка ключа вернула код {response.status_code}",
            "checked_at": timezone.localtime().isoformat(),
        }
    return _remember_auth_health(agency, market_id, auth_ok=True, message="")


def _status_for(
    configured: bool,
    report: MarketSyncReport | None,
    errors: list[str],
    *,
    auth_ok: bool | None = None,
) -> tuple[str, str]:
    if not configured:
        return "setup", "Не подключён"
    if auth_ok is False:
        return "error", "Ключ недействителен"
    if errors or (report and report.status == "error"):
        return "warning", "Каталог с замечаниями"
    if report:
        return "ok", "Каталог актуален"
    return "ok", "Подключён"


def build_marketplaces_payload(
    agency,
    *,
    check_auth: bool = True,
    force_auth_refresh: bool = False,
) -> list[dict[str, Any]]:
    rows = []
    credentials = list(
        MarketCredential.objects.filter(agency=agency).select_related("market")
    )
    reports = list(
        MarketSyncReport.objects.filter(agency=agency).order_by(
            "-finished_at",
            "-started_at",
        )
    )
    for spec in MARKETPLACES:
        market_id = spec["id"]
        market_names = {
            "wb": ("WB", "WILDBERRIES"),
            "ozon": ("OZON",),
        }.get(market_id, ())
        cred = next(
            (
                item
                for item in credentials
                if any(
                    name in (getattr(item.market, "name", "") or "").upper()
                    for name in market_names
                )
            ),
            None,
        )
        configured = _credential_configured(cred, market_id)
        report = next(
            (
                item
                for item in reports
                if any(
                    name in (item.marketplace or "").upper()
                    for name in market_names
                )
            ),
            None,
        )
        errors: list[str] = []
        if report:
            raw_errors = report.errors or []
            if isinstance(raw_errors, str):
                errors = [raw_errors] if raw_errors else []
            elif isinstance(raw_errors, list):
                errors = [str(item) for item in raw_errors if item][:8]
        auth = {"auth_ok": None, "message": "", "checked_at": ""}
        if configured and check_auth:
            auth = probe_marketplace_auth(agency, market_id, force_refresh=force_auth_refresh)
            if auth.get("auth_ok") is False and auth.get("message"):
                errors = [str(auth["message"])] + [e for e in errors if e != auth["message"]]
        status, status_label = _status_for(
            configured,
            report,
            errors,
            auth_ok=auth.get("auth_ok"),
        )
        last_sync = ""
        if report and (report.finished_at or report.started_at):
            last_sync = timezone.localtime(report.finished_at or report.started_at).isoformat()
        settings_url = f"/client/{agency.id}/edit/#marketplaces" if agency else "/client/"
        reconnect_url = settings_url
        if not configured or auth.get("auth_ok") is False:
            # Portal clients can't edit keys — send them to chat with manager
            reconnect_url = f"/client/dashboard/lk/?client={agency.id}#/chat" if agency else "#/chat"
        if not configured:
            setup_hint = (
                "Маркетплейс не подключён. Передайте API-ключ менеджеру FullBox или откройте чат."
                if market_id == "wb"
                else "Ozon не подключён. Передайте API-ключ и Client ID менеджеру FullBox."
            )
            action_label = "Подключить"
        elif auth.get("auth_ok") is False:
            setup_hint = auth.get("message") or "Ключ API недействителен. Нужно переподключить доступ."
            action_label = "Переподключить"
        elif errors:
            setup_hint = "Замечания не блокируют работу на складе — менеджер поможет сверить каталог."
            action_label = "Обновить каталог"
        else:
            setup_hint = "Нажмите «Обновить каталог», чтобы подтянуть новые карточки и штрихкоды."
            action_label = "Обновить каталог"
        rows.append(
            {
                "id": market_id,
                "name": spec["name"],
                "short_name": spec["short_name"],
                "sku_count": _sku_count_for(agency, market_id) if configured else 0,
                "status_label": status_label,
                "status": status,
                "last_sync": last_sync,
                "last_sync_display": format_ru_date(report.finished_at or report.started_at) if report else "",
                "errors": errors[:8],
                "configured": configured,
                "auth_ok": auth.get("auth_ok"),
                "auth_checked_at": auth.get("checked_at") or "",
                "token_health": (
                    "ok" if auth.get("auth_ok") is True
                    else "error" if auth.get("auth_ok") is False
                    else "unknown" if configured
                    else "missing"
                ),
                "settings_url": settings_url,
                "reconnect_url": reconnect_url,
                "action_label": action_label,
                "setup_hint": setup_hint,
            }
        )
    return rows


def _serialize_supply(order, *, agency_id: int, today) -> dict[str, Any]:
    label = _market_label(order)
    market_id = "wb" if _is_market(label, "WB", "WILDBERRIES") else "ozon" if _is_market(label, "OZON") else (label or "other").lower()
    detail_url = f"/shipping/{order.id}/?client={agency_id}"
    return {
        "id": order.id,
        "number": order.number,
        "marketplace": market_id,
        "marketplace_name": label or market_id.upper(),
        "status": order.status,
        "status_label": order.get_status_display() if hasattr(order, "get_status_display") else order.status,
        "slot_date": order.slot_date.isoformat() if order.slot_date else "",
        "slot_time": order.slot_time.strftime("%H:%M") if order.slot_time else "",
        "destination_warehouse": order.destination_warehouse or "",
        "detail_url": detail_url,
        "wms_url": detail_url,
        "open_label": "Открыть отгрузку в WMS",
        "is_today": bool(order.slot_date and order.slot_date == today),
        "supply_barcode": getattr(order, "wb_supply_barcode", "") or getattr(order, "shipping_barcode", "") or "",
    }


def build_supplies_payload(agency) -> dict[str, Any]:
    today = timezone.localdate()
    horizon = today + timedelta(days=14)
    qs = (
        ShippingOrder.objects.filter(agency=agency)
        .filter(
            Q(delivery_type=ShippingOrder.DELIVERY_MARKETPLACE)
            | Q(marketplace__isnull=False)
        )
        .exclude(status__in=SHIPPING_DONE_STATUSES)
        .filter(slot_date__gte=today, slot_date__lte=horizon)
        .select_related("marketplace")
        .only(*SHIPPING_LK_FIELDS, "marketplace__id", "marketplace__name")
        .order_by("slot_date", "slot_time", "id")
    )
    today_wb: list[dict[str, Any]] = []
    today_ozon: list[dict[str, Any]] = []
    upcoming: list[dict[str, Any]] = []
    for order in qs:
        row = _serialize_supply(order, agency_id=agency.id, today=today)
        upcoming.append(row)
        if row["is_today"]:
            if row["marketplace"] == "wb":
                today_wb.append(row)
            elif row["marketplace"] == "ozon":
                today_ozon.append(row)
    return {"today": {"wb": today_wb, "ozon": today_ozon}, "upcoming": upcoming}


def build_live_dashboard(
    agency,
    availability_rows: list[dict[str, Any]] | None = None,
    marketplaces_rows: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Aggregate WMS LIVE strip metrics for the client LK home.

    Pass ``availability_rows`` when the caller already loaded stock availability
    (e.g. ``api_dashboard``) to avoid a second heavy scan.
    """
    now = timezone.localtime()
    stock_qs = WarehouseStockSnapshot.objects.filter(agency=agency, is_archived=False)
    # Ready-to-ship: physical qty staged for outbound (not free available_qty).
    active_processing_states = (
        "processing_in_progress",
        "in_processing_zone",
        "reserved_for_processing",
        "moving_to_processing",
        "palletizing",
        "placed_after_processing",
    )
    stock_totals = stock_qs.aggregate(
        ready=Sum(
            "qty",
            filter=(
                Q(zone_kind="otg")
                | Q(zone_kind="shipping")
                | Q(warehouse_state_code__icontains="otg")
                | Q(warehouse_state_code__icontains="shipping")
                | Q(warehouse_state_code="ready_for_loading")
            )
            & ~Q(warehouse_state_code="processing_consumed"),
        ),
        in_processing=Sum("qty", filter=Q(warehouse_state_code__in=active_processing_states)),
    )
    ready = stock_totals.get("ready") or 0
    in_processing = stock_totals.get("in_processing") or 0

    if availability_rows is None:
        from sklad.services.stock_availability import StockAvailabilityService

        availability_rows = StockAvailabilityService.stock_rows_with_availability(agency=agency)
    from sklad.ui_services import _JOURNAL_HIDDEN_WAREHOUSE_STATES

    stock_units = 0
    for row in availability_rows:
        if int(row.get("available_qty") or 0) <= 0:
            continue
        state_code = str(row.get("warehouse_state_code") or "").strip()
        if state_code in _JOURNAL_HIDDEN_WAREHOUSE_STATES:
            continue
        if int(row.get("qty") or 0) <= 0:
            continue
        stock_units += int(row.get("available_qty") or 0)

    box_codes = set()
    pallet_ids = set()
    for code_value, parent_id in stock_qs.filter(available_qty__gt=0).values_list(
        "container_code",
        "parent_container_id",
    )[:5000]:
        code = str(code_value or "").strip()
        if code:
            box_codes.add(code)
        if parent_id:
            pallet_ids.add(parent_id)

    today = timezone.localdate()
    today_orders = list(
        ShippingOrder.objects.filter(agency=agency, slot_date=today)
        .exclude(status__in=SHIPPING_DONE_STATUSES)
        .select_related("marketplace")
        .only("id", "agency_id", "marketplace_id", "number", "status", "slot_time", "marketplace__id", "marketplace__name")
        .order_by("slot_time", "id")
    )

    def pick(*needles):
        for order in today_orders:
            if _is_market(_market_label(order), *needles):
                return order
        return None

    mp_rows = (
        list(marketplaces_rows)
        if marketplaces_rows is not None
        else build_marketplaces_payload(agency, check_auth=False)
    )
    mp_by_id = {str(row.get("id") or "").lower(): row for row in mp_rows}
    upcoming_orders_cache = None

    def upcoming_orders():
        nonlocal upcoming_orders_cache
        if upcoming_orders_cache is None:
            upcoming_orders_cache = list(
                ShippingOrder.objects.filter(agency=agency, slot_date__gt=today)
                .exclude(status__in=SHIPPING_DONE_STATUSES)
                .select_related("marketplace")
                .order_by("slot_date", "slot_time", "id")[:40]
            )
        return upcoming_orders_cache

    def delivery_payload(market_key: str, order, needles: tuple[str, ...]) -> dict[str, Any]:
        mp = mp_by_id.get(market_key) or {}
        connected = bool(mp.get("configured"))
        lk_hash = "#/marketplaces"
        # Warehouse slot for today wins over MP cabinet connection status.
        if order:
            slot_time = order.slot_time
            if slot_time:
                status_text = f"Сегодня, {slot_time.strftime('%H:%M')}"
            else:
                status_text = f"Назначена · {order.number}"
            return {
                "connected": connected,
                "status": "slot_today",
                "status_text": status_text,
                "tone": "ok",
                "slot_start": slot_time.isoformat() if slot_time else None,
                "slot_end": None,
                "url": f"#/request/shipping/{order.number}" if order.number else lk_hash,
                "order_id": str(order.number or ""),
                "order_number": str(order.number or ""),
            }
        auth_ok = mp.get("auth_ok")
        if connected and (
            auth_ok is False or str(mp.get("status") or "").lower() in {"error", "auth_error"}
        ):
            return {
                "connected": True,
                "status": "sync_error",
                "status_text": "Ошибка синхронизации",
                "tone": "danger",
                "slot_start": None,
                "slot_end": None,
                "url": lk_hash,
                "order_id": "",
                "order_number": "",
            }
        # Upcoming slot (not today)
        for item in upcoming_orders():
            if not _is_market(_market_label(item), *needles):
                continue
            day = item.slot_date.strftime("%d.%m") if item.slot_date else ""
            tm = item.slot_time.strftime("%H:%M") if item.slot_time else ""
            status_text = f"Назначена на {day}" + (f", {tm}" if tm else "")
            return {
                "connected": connected,
                "status": "slot_scheduled",
                "status_text": status_text,
                "tone": "ok",
                "slot_start": None,
                "slot_end": None,
                "url": f"#/request/shipping/{item.number}" if item.number else lk_hash,
                "order_id": str(item.number or ""),
                "order_number": str(item.number or ""),
            }
        if not connected:
            return {
                "connected": False,
                "status": "not_connected",
                "status_text": "Нет слота на сегодня",
                "tone": "muted",
                "slot_start": None,
                "slot_end": None,
                "url": lk_hash,
                "order_id": "",
                "order_number": "",
            }
        return {
            "connected": True,
            "status": "no_slot_today",
            "status_text": "Нет слота на сегодня",
            "tone": "warn",
            "slot_start": None,
            "slot_end": None,
            "url": lk_hash,
            "order_id": "",
            "order_number": "",
        }

    wb = pick("WB", "WILDBERRIES")
    ozon = pick("OZON")
    wb_delivery = delivery_payload("wb", wb, ("WB", "WILDBERRIES"))
    ozon_delivery = delivery_payload("ozon", ozon, ("OZON",))

    return {
        "status": "online",
        "updated_at": now.isoformat(),
        "updated_at_label": now.strftime("%H:%M"),
        "stock": {"units": int(stock_units or 0)},
        "processing": {
            "units": int(in_processing or 0),
            "requests_count": 0,
        },
        "ready_to_ship": {
            "units": int(ready or 0),
            "requests_count": 0,
        },
        # Backward-compatible flat fields used by existing tests/UI.
        "ready_for_shipping_units": int(ready or 0),
        "in_processing_units": int(in_processing or 0),
        "supply_wb_label": wb_delivery["status_text"],
        "supply_ozon_label": ozon_delivery["status_text"],
        "marketplace_deliveries": {
            "wildberries": wb_delivery,
            "ozon": ozon_delivery,
        },
        "packaging": {
            "boxes": len(box_codes),
            "pallets": len(pallet_ids),
            "sku_count": SKU.objects.filter(agency=agency, deleted=False).count(),
        },
    }


def build_live_history(
    agency,
    *,
    limit: int = 5,
    notification_rows: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Last N client-safe events for home live-history strip."""
    from .messaging_lk import list_notifications

    rows = []
    row_limit = max(1, min(int(limit or 5), 20))
    source_rows = (
        list(notification_rows)[:row_limit]
        if notification_rows is not None
        else list_notifications(agency, limit=row_limit)
    )
    for item in source_rows:
        text = str(item.get("text") or item.get("message") or "").strip()
        title = str(item.get("title") or "").strip() or text or "Событие"
        if text and text != title:
            display = f"{title}: {text}" if len(text) < 120 else title
        else:
            display = title
        blob = f"{title} {text}".lower()
        tone = "info"
        if any(token in blob for token in ("ошиб", "расхожд", "отклон")):
            tone = "danger"
        elif any(token in blob for token in ("требует", "согласован", "подтверд", "оплат", "акт")):
            tone = "warn" if "требует" in blob or "не оплач" in blob else "success"
        elif any(token in blob for token in ("принята в работу", "менеджер принял", "выполн", "заверш")):
            tone = "success"
        elif any(token in blob for token in ("обработ", "склад", "короб", "палет")):
            tone = "process"
        created = item.get("created_at")
        created_label = ""
        if created:
            try:
                local = timezone.localtime(created)
                created_label = local.strftime("%H:%M")
                created_iso = local.isoformat()
            except Exception:
                created_iso = str(created)
                created_label = str(created)[:16]
        else:
            created_iso = ""
        rows.append(
            {
                "id": item.get("db_id") or item.get("id"),
                "type": item.get("type") or "notification",
                "title": display[:180],
                "created_at": created_iso,
                "created_at_label": created_label,
                "tone": tone,
                "requires_action": bool(item.get("priority") == "high" and not item.get("is_read")),
                "url": item.get("detail_url") or "#/notifications",
            }
        )
        if len(rows) >= max(1, int(limit or 5)):
            break
    return rows


def _fullbox_qty_by_sku(agency) -> dict[str, int]:
    rows = (
        WarehouseStockSnapshot.objects.filter(agency=agency, is_archived=False)
        .values("sku_code")
        .annotate(qty=Sum("available_qty"))
    )
    result: dict[str, int] = {}
    for row in rows:
        code = (row.get("sku_code") or "").strip()
        if not code:
            continue
        result[code] = int(row.get("qty") or 0)
    return result


def _sku_names(agency) -> dict[str, str]:
    return {
        (sku.sku_code or "").strip(): (sku.name or sku.sku_code or "")
        for sku in SKU.objects.filter(agency=agency, deleted=False).only("sku_code", "name")
        if (sku.sku_code or "").strip()
    }


def _barcodes_by_sku(agency) -> dict[str, list[str]]:
    mapping: dict[str, list[str]] = {}
    for row in SKUBarcode.objects.filter(sku__agency=agency, sku__deleted=False).select_related("sku"):
        code = (row.sku.sku_code or "").strip()
        barcode = (row.value or "").strip()
        if not code or not barcode:
            continue
        mapping.setdefault(code, []).append(barcode)
    return mapping


def _fetch_wb_stock_map(token: str, barcodes: list[str] | None = None) -> tuple[dict[str, int], str]:
    """Return qty keyed by vendorCode/barcode via statistics or marketplace API."""
    headers = {"Authorization": token, "Content-Type": "application/json"}
    totals: dict[str, int] = {}
    errors: list[str] = []

    try:
        response = _direct_marketplace_request(
            "GET",
            "https://statistics-api.wildberries.ru/api/v1/supplier/stocks",
            headers={"Authorization": token},
            params={"dateFrom": "2019-01-01"},
            timeout=40,
        )
        if response.status_code == 200:
            payload = response.json()
            if isinstance(payload, list):
                for item in payload:
                    if not isinstance(item, dict):
                        continue
                    qty = int(item.get("quantity") or item.get("quantityFull") or item.get("Quantity") or 0)
                    keys = [
                        item.get("supplierArticle"),
                        item.get("vendorCode"),
                        item.get("sa_name"),
                        item.get("barcode"),
                        item.get("Barcode"),
                    ]
                    for key in keys:
                        code = str(key or "").strip()
                        if not code:
                            continue
                        totals[code] = totals.get(code, 0) + qty
                if totals:
                    return totals, ""
        elif is_auth_http_status(response.status_code):
            errors.append(f"statistics API {response.status_code}")
        else:
            errors.append(f"statistics API {response.status_code}")
    except requests.RequestException as exc:
        errors.append(friendly_network_error(exc, "WB"))
    except ValueError:
        errors.append("statistics API: bad JSON")

    # Fallback: seller warehouses + barcode stocks (marketplace token).
    try:
        wh_response = _direct_marketplace_request(
            "GET",
            "https://marketplace-api.wildberries.ru/api/v3/warehouses",
            headers=headers,
            timeout=30,
        )
    except requests.RequestException as exc:
        return {}, friendly_network_error(exc, "WB")
    if wh_response.status_code != 200:
        if is_auth_http_status(wh_response.status_code):
            return {}, auth_error_message("WB", wh_response.status_code)
        detail = "; ".join(errors) if errors else f"код {wh_response.status_code}"
        return {}, f"WB остатки: не удалось получить склады ({detail})"
    try:
        warehouses = wh_response.json()
    except ValueError:
        return {}, "WB остатки: некорректный список складов"
    if not isinstance(warehouses, list) or not warehouses:
        return {}, "WB остатки: список складов пуст"

    sku_list = [code for code in (barcodes or []) if code][:900]
    if not sku_list:
        return {}, "WB остатки: нет штрихкодов для сверки. Обновите каталог."

    for warehouse in warehouses[:8]:
        if not isinstance(warehouse, dict):
            continue
        warehouse_id = warehouse.get("id") or warehouse.get("warehouseId")
        if not warehouse_id:
            continue
        for offset in range(0, len(sku_list), 100):
            chunk = sku_list[offset : offset + 100]
            try:
                stock_response = _direct_marketplace_request(
                    "POST",
                    f"https://marketplace-api.wildberries.ru/api/v3/stocks/{warehouse_id}",
                    headers=headers,
                    json={"skus": chunk},
                    timeout=30,
                )
            except requests.RequestException:
                continue
            if stock_response.status_code != 200:
                continue
            try:
                payload = stock_response.json()
            except ValueError:
                continue
            items = payload.get("stocks") if isinstance(payload, dict) else payload
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict):
                    continue
                sku = str(item.get("sku") or item.get("barcode") or "").strip()
                qty = int(item.get("amount") or item.get("quantity") or 0)
                if sku:
                    totals[sku] = totals.get(sku, 0) + qty
    if not totals:
        return {}, "WB остатки: по складам продавца остатков не найдено"
    return totals, ""


def _fetch_ozon_stock_map(client_id: str, api_key: str) -> tuple[dict[str, int], str]:
    headers = {
        "Client-Id": str(client_id).strip(),
        "Api-Key": str(api_key).strip(),
        "Content-Type": "application/json",
    }
    totals: dict[str, int] = {}
    last_id = ""
    for _ in range(40):
        body = {"filter": {"visibility": "ALL"}, "limit": 1000}
        if last_id:
            body["last_id"] = last_id
        try:
            response = _direct_marketplace_request(
                "POST",
                "https://api-seller.ozon.ru/v4/product/info/stocks",
                headers=headers,
                json=body,
                timeout=40,
            )
        except requests.RequestException as exc:
            return {}, friendly_network_error(exc, "Ozon")
        if response.status_code != 200:
            if is_auth_http_status(response.status_code):
                return {}, auth_error_message("Ozon", response.status_code)
            # fallback older endpoint
            try:
                response = _direct_marketplace_request(
                    "POST",
                    "https://api-seller.ozon.ru/v3/product/info/stocks",
                    headers=headers,
                    json={"filter": {"visibility": "ALL"}, "limit": 1000, "cursor": last_id or ""},
                    timeout=40,
                )
            except requests.RequestException as exc:
                return {}, friendly_network_error(exc, "Ozon")
            if response.status_code != 200:
                if is_auth_http_status(response.status_code):
                    return {}, auth_error_message("Ozon", response.status_code)
                return {}, f"Ozon остатки: ошибка API {response.status_code}"
        try:
            payload = response.json()
        except ValueError:
            return {}, "Ozon остатки: некорректный ответ API"
        result = payload.get("result") if isinstance(payload, dict) else None
        items = None
        if isinstance(result, dict):
            items = result.get("items") or result.get("products")
            last_id = str(result.get("last_id") or result.get("cursor") or "")
        elif isinstance(payload, dict):
            items = payload.get("items")
            last_id = str(payload.get("last_id") or payload.get("cursor") or "")
        if not items:
            break
        for item in items:
            if not isinstance(item, dict):
                continue
            offer = str(item.get("offer_id") or item.get("offerId") or "").strip()
            present = 0
            stocks = item.get("stocks") or []
            if isinstance(stocks, list):
                for stock in stocks:
                    if not isinstance(stock, dict):
                        continue
                    present += int(stock.get("present") or stock.get("quantity") or 0)
            else:
                present = int(item.get("present") or item.get("stock") or 0)
            if offer:
                totals[offer] = totals.get(offer, 0) + present
        if not last_id:
            break
    return totals, ""


def _empty_market_stock(*, configured: bool, error: str = "", client_message: str = "", auth_error: bool = False) -> dict[str, Any]:
    return {
        "configured": configured,
        "ok": configured and not error,
        "error": error,
        "client_message": client_message or error,
        "auth_error": bool(auth_error),
        "total_present": 0,
        "rows": [],
    }


def build_mp_stocks_cached_payload(agency) -> dict[str, Any]:
    """Fast client-LK marketplace stock payload without live API calls.

    The marketplace page must open immediately. Live WB/Ozon stock checks are
    only performed on explicit refresh, because seller APIs/DNS can be slow.
    """
    if not agency:
        return {
            "fetched_at": "",
            "from_cache": False,
            "cache_ttl_seconds": STOCK_CACHE_TTL_SECONDS,
            "wb": _empty_market_stock(configured=False),
            "ozon": _empty_market_stock(configured=False),
            "rows": [],
        }
    cached = cache.get(_stocks_cache_key(agency.id))
    if isinstance(cached, dict) and "rows" in cached:
        payload = dict(cached)
        payload["from_cache"] = True
        return payload

    by_market: dict[str, dict[str, Any]] = {}
    for spec in MARKETPLACES:
        market_id = spec["id"]
        configured = _credential_configured(_credential_for(agency, market_id), market_id)
        by_market[market_id] = _empty_market_stock(
            configured=configured,
            client_message=(
                "Нажмите «Обновить», чтобы подтянуть остатки Ozon."
                if market_id == "ozon"
                else "Нажмите «Обновить», чтобы подтянуть остатки Wildberries."
            ),
        )
    return {
        "fetched_at": "",
        "from_cache": False,
        "cache_ttl_seconds": STOCK_CACHE_TTL_SECONDS,
        "wb": by_market.get("wb") or _empty_market_stock(configured=False),
        "ozon": by_market.get("ozon") or _empty_market_stock(configured=False),
        "rows": [],
    }


def _build_mp_stocks_payload_uncached(agency) -> dict[str, Any]:
    fullbox = _fullbox_qty_by_sku(agency)
    names = _sku_names(agency)
    barcodes = _barcodes_by_sku(agency)
    fetched_at = timezone.localtime().isoformat()
    combined_rows: list[dict[str, Any]] = []
    by_market: dict[str, dict[str, Any]] = {}

    for spec in MARKETPLACES:
        market_id = spec["id"]
        cred = _credential_for(agency, market_id)
        configured = _credential_configured(cred, market_id)
        if not configured:
            by_market[market_id] = _empty_market_stock(
                configured=False,
                client_message=(
                    "Подключите API-ключ Ozon и Client ID через менеджера FullBox."
                    if market_id == "ozon"
                    else "Подключите API-ключ Wildberries через менеджера FullBox."
                ),
            )
            continue

        token = (cred.market_key or "").strip()
        client_id = (cred.client_id or "").strip()
        if market_id == "wb":
            all_barcodes = [code for codes in barcodes.values() for code in codes]
            mp_map, error = _fetch_wb_stock_map(token, barcodes=all_barcodes)
        else:
            mp_map, error = _fetch_ozon_stock_map(client_id, token)

        auth_error = bool(error and ("недействителен" in error.lower() or "истёк" in error.lower() or "истек" in error.lower()))
        if auth_error:
            _remember_auth_health(agency, market_id, auth_ok=False, message=error)
        elif not error:
            _remember_auth_health(agency, market_id, auth_ok=True, message="")

        market_rows: list[dict[str, Any]] = []
        seen: set[str] = set()

        # Prefer SKUs bound / market-linked.
        linked_codes = set(
            (sku.sku_code or "").strip()
            for sku in SKU.objects.filter(agency=agency, deleted=False, market__isnull=False)
            if (sku.sku_code or "").strip()
            and _is_market(getattr(sku.market, "name", ""), *spec["market_names"])
        )
        linked_codes.update(
            (binding.sku.sku_code or "").strip()
            for binding in MarketplaceBinding.objects.filter(sku__agency=agency, sku__deleted=False).select_related("sku")
            if (binding.sku.sku_code or "").strip()
            and _is_market(binding.marketplace, *spec["market_names"])
        )
        if not linked_codes:
            linked_codes = set(fullbox.keys()) | set(mp_map.keys())

        for sku_code in sorted(linked_codes):
            if not sku_code or sku_code in seen:
                continue
            seen.add(sku_code)
            mp_qty = int(mp_map.get(sku_code) or 0)
            if mp_qty == 0:
                for barcode in barcodes.get(sku_code, []):
                    mp_qty += int(mp_map.get(barcode) or 0)
            fullbox_qty = int(fullbox.get(sku_code) or 0)
            if mp_qty == 0 and fullbox_qty == 0:
                continue
            row = {
                "marketplace": market_id,
                "sku": sku_code,
                "name": names.get(sku_code) or sku_code,
                "mp_qty": mp_qty,
                "fullbox_qty": fullbox_qty,
            }
            market_rows.append(row)
            combined_rows.append(row)

        by_market[market_id] = {
            "configured": True,
            "ok": not error,
            "error": error,
            "client_message": error,
            "auth_error": auth_error,
            "total_present": sum(row["mp_qty"] for row in market_rows),
            "rows": market_rows,
        }

    return {
        "fetched_at": fetched_at,
        "cache_ttl_seconds": STOCK_CACHE_TTL_SECONDS,
        "wb": by_market.get("wb") or _empty_market_stock(configured=False),
        "ozon": by_market.get("ozon") or _empty_market_stock(configured=False),
        "rows": combined_rows,
    }


def build_mp_stocks_payload(agency, *, force_refresh: bool = False) -> dict[str, Any]:
    if not agency:
        return {
            "fetched_at": "",
            "from_cache": False,
            "cache_ttl_seconds": STOCK_CACHE_TTL_SECONDS,
            "wb": _empty_market_stock(configured=False),
            "ozon": _empty_market_stock(configured=False),
            "rows": [],
        }
    cache_key = _stocks_cache_key(agency.id)
    if not force_refresh:
        cached = cache.get(cache_key)
        if isinstance(cached, dict) and "rows" in cached:
            payload = dict(cached)
            payload["from_cache"] = True
            return payload
    payload = _build_mp_stocks_payload_uncached(agency)
    payload["from_cache"] = False
    cache.set(cache_key, payload, STOCK_CACHE_TTL_SECONDS)
    return payload


def sync_marketplace(*, agency, marketplace: str) -> JsonResponse:
    with marketplace_sync_lock(agency.id) as acquired:
        if not acquired:
            response = JsonResponse(
                {
                    "ok": False,
                    "error": "Синхронизация маркетплейса для клиента уже выполняется. Дождитесь завершения.",
                },
                status=409,
            )
            response["Retry-After"] = "10"
            return response
        return _sync_marketplace_unlocked(agency=agency, marketplace=marketplace)


def _sync_marketplace_unlocked(*, agency, marketplace: str) -> JsonResponse:
    market_id = (marketplace or "").strip().lower()
    if market_id not in {"wb", "ozon", ""}:
        return JsonResponse({"ok": False, "error": "Неизвестный маркетплейс"}, status=400)
    body = json.dumps({"client": agency.id}).encode("utf-8")

    def _can_sync(mid: str) -> bool:
        return _credential_configured(_credential_for(agency, mid), mid)

    if market_id == "wb":
        if not _can_sync("wb"):
            return JsonResponse({"ok": False, "error": "Wildberries не подключён"}, status=400)
        auth = probe_marketplace_auth(agency, "wb", force_refresh=True)
        if auth.get("auth_ok") is False:
            return JsonResponse({"ok": False, "error": auth.get("message") or "Ключ WB недействителен"}, status=400)
        response = run_wb_sync_request(body=body)
        cache.delete(_stocks_cache_key(agency.id))
        return response
    if market_id == "ozon":
        if not _can_sync("ozon"):
            return JsonResponse({"ok": False, "error": "Ozon не подключён"}, status=400)
        auth = probe_marketplace_auth(agency, "ozon", force_refresh=True)
        if auth.get("auth_ok") is False:
            return JsonResponse({"ok": False, "error": auth.get("message") or "Ключ Ozon недействителен"}, status=400)
        response = run_ozon_sync_request(body=body)
        cache.delete(_stocks_cache_key(agency.id))
        return response

    results = []
    errors: list[str] = []
    for mid, runner in (("wb", run_wb_sync_request), ("ozon", run_ozon_sync_request)):
        if not _can_sync(mid):
            continue
        auth = probe_marketplace_auth(agency, mid, force_refresh=True)
        if auth.get("auth_ok") is False:
            errors.append(str(auth.get("message") or f"Ключ {mid} недействителен"))
            continue
        response = runner(body=body)
        try:
            data = json.loads(response.content.decode("utf-8") or "{}")
        except Exception:
            data = {}
        if response.status_code >= 400 or data.get("ok") is False:
            err = data.get("error")
            items = data.get("errors") or ([err] if err else [f"Ошибка синхронизации {mid}"])
            errors.extend(str(item) for item in items if item)
        results.append({"marketplace": mid, "response": data})
    cache.delete(_stocks_cache_key(agency.id))
    if not results and errors:
        return JsonResponse({"ok": False, "error": errors[0], "errors": errors}, status=400)
    if not results:
        return JsonResponse({"ok": False, "error": "Нет подключённых маркетплейсов"}, status=400)
    return JsonResponse({"ok": not errors, "errors": errors, "results": results}, status=200 if not errors else 400)


@contextmanager
def marketplace_sync_lock(agency_id: int):
    lock_path = Path("/tmp") / f"fullbox_marketplace_sync_{int(agency_id)}.lock"
    handle = lock_path.open("a+")
    acquired = False
    try:
        try:
            acquire_file_lock_nonblocking(handle)
        except BlockingIOError:
            yield False
            return
        acquired = True
        yield True
    finally:
        if acquired:
            release_file_lock(handle)
        handle.close()
