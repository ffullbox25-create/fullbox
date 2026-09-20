"""Client cabinet UI views and helper logic."""

import json
import re
from io import BytesIO
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.db import models, transaction
from django.db.models import Case, CharField, Count, F, Sum, Value, When
from django.http import HttpResponse, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.generic import ListView, CreateView, UpdateView, TemplateView, FormView
from openpyxl import Workbook
import uuid
from urllib.parse import urlencode, urlsplit

from employees.models import Employee
from employees.access import (
    get_request_employee,
    get_request_role,
    is_staff_role,
    request_has_any_role,
    resolve_cabinet_url,
)
from fbs.models import FbsIntegrationProfile, FbsOzonCredential
from fbs.services.client_profiles import is_client_fbs_enabled
from marking.models import MarkingCode
from orders.title_truth import resolve_order_title
from processing_app.views import _import_marking_codes
from sklad.models import WarehouseStockSnapshot
from sklad.services.warehouse_transitions import WarehouseStateCode
from sku.audit_history import sku_audit_change_rows
from sku.models import Agency, Market, MarketCredential, SKU, SKUBarcode
from sku.views import SKUCreateView, SKUUpdateView, SKUDuplicateView
from todo.models import Task
from .forms import AgencyForm, OzonMarketplaceSettingsForm, WBMarketplaceSettingsForm
from .chat_switch import chats_enabled
from .lk_status_map import is_terminal_cancelled_status, resolve_lk_entry_status
from .lk_requests import build_action_urls, canonical_request_order_id, lk_request_hash
from .request_titles import build_request_display_title
from .services import (
    build_agency_form_context,
    build_client_cabinet_url,
    build_dashboard_context,
    build_client_list_context,
    build_client_list_queryset,
    build_client_sku_duplicate_initial,
    build_client_sku_form_context,
    build_client_sku_list_context,
    build_client_sku_list_queryset,
    build_client_packing_form_context,
    build_client_receiving_form_context,
    build_marking_tools_context,
    fetch_party_by_inn,
    fetch_party_by_inn_response,
    import_marking_codes_response,
    resolve_client_order_redirect_response,
    submit_client_packing_order,
    submit_client_receiving_order,
    toggle_agency_archive_response,
)
from audit.models import OrderAuditEntry, agency_snapshot, log_agency_change, log_order_action


def _staff_allowed(request) -> bool:
    if not request.user.is_authenticated:
        return False
    role = get_request_role(request)
    return request.user.is_staff or is_staff_role(role)


CLIENT_PROFILE_STAFF_ROLES = ("manager", "accountant", "head_manager", "director", "admin")
CLIENT_ACCESS_STAFF_ROLES = ("manager", "head_manager", "director", "admin")


def _staff_can_edit_client_profile(request) -> bool:
    user = getattr(request, "user", None)
    return bool(
        user
        and user.is_authenticated
        and getattr(user, "is_active", False)
        and request_has_any_role(request, CLIENT_PROFILE_STAFF_ROLES)
    )


def _can_edit_client_profile(request, agency) -> bool:
    if _staff_allowed(request):
        return _staff_can_edit_client_profile(request)
    from .portal_members import portal_user_is_admin

    return portal_user_is_admin(getattr(request, "user", None), agency)


def _staff_can_manage_client_access(request) -> bool:
    user = getattr(request, "user", None)
    return bool(
        user
        and user.is_authenticated
        and getattr(user, "is_active", False)
        and request_has_any_role(request, CLIENT_ACCESS_STAFF_ROLES)
    )


def _can_manage_client_access(request, agency) -> bool:
    if _staff_allowed(request):
        return _staff_can_manage_client_access(request)
    from .portal_members import portal_user_is_admin

    return portal_user_is_admin(getattr(request, "user", None), agency)


def _get_client_for_request(request):
    if not request.user.is_authenticated:
        return None, False, False
    from .portal_members import resolve_portal_agency_for_user

    direct_client = resolve_portal_agency_for_user(request.user)
    if direct_client:
        return direct_client, True, True
    staff_allowed = _staff_allowed(request)
    if not staff_allowed:
        return None, False, False
    client_id = request.GET.get("client") or request.GET.get("agency")
    if client_id:
        return Agency.objects.filter(pk=client_id).first(), False, True
    return None, False, True


def _check_agency_access(request, agency) -> bool:
    if not request.user.is_authenticated or not agency:
        return False
    from .portal_members import resolve_portal_agency_for_user

    direct_client = resolve_portal_agency_for_user(request.user)
    if direct_client:
        return direct_client.id == agency.id
    return _staff_allowed(request)


STAFF_CLIENT_PREVIEW_SESSION_KEY = "client_cabinet_staff_preview"
STAFF_CLIENT_PREVIEW_ROLES = {"manager", "logistician", "head_manager", "director", "admin", "developer"}
STAFF_CLIENT_PREVIEW_RETURN_PREFIXES = (
    "/team-manager/",
    "/head-manager/",
    "/cabinet/director/",
    "/dev/",
    "/admin/",
)


def _safe_staff_preview_return_url(value, role: str | None) -> str:
    fallback = resolve_cabinet_url(role)
    if not fallback or fallback.startswith("/client/"):
        fallback = "/team-manager/"
    raw = str(value or "").strip()
    if not raw:
        return fallback
    parsed = urlsplit(raw)
    if parsed.scheme or parsed.netloc or not raw.startswith("/") or raw.startswith("//"):
        return fallback
    if any(raw == prefix.rstrip("/") or raw.startswith(prefix) for prefix in STAFF_CLIENT_PREVIEW_RETURN_PREFIXES):
        return raw
    return fallback


def _staff_preview_agency_allowed(client_id: int) -> bool:
    try:
        from accountant.selectors import manager_visible_agencies

        return manager_visible_agencies(Agency.objects.all()).filter(pk=client_id).exists()
    except Exception:
        return Agency.objects.filter(pk=client_id, archived=False).exists()


def _staff_client_preview_context(request, selected_client, client_view: bool) -> dict:
    empty = {"staff_return_url": "", "staff_return_label": ""}
    if client_view or not selected_client:
        return empty
    role = get_request_role(request)
    if role not in STAFF_CLIENT_PREVIEW_ROLES:
        return empty
    preview = request.session.get(STAFF_CLIENT_PREVIEW_SESSION_KEY) or {}
    if not isinstance(preview, dict):
        return empty
    try:
        preview_client_id = int(preview.get("client_id") or 0)
    except (TypeError, ValueError):
        preview_client_id = 0
    if preview_client_id != selected_client.id:
        return empty
    employee = get_request_employee(request)
    try:
        preview_employee_id = int(preview.get("employee_id") or 0)
    except (TypeError, ValueError):
        preview_employee_id = 0
    if preview_employee_id and getattr(employee, "id", None) != preview_employee_id:
        return empty
    return {
        "staff_return_url": reverse("client-cabinet-preview-return"),
        "staff_return_label": "Вернуться в кабинет менеджера",
    }


def staff_client_preview(request, client_id: int):
    if not request.user.is_authenticated:
        from django.contrib.auth.views import redirect_to_login

        return redirect_to_login(request.get_full_path(), login_url="/login/")
    from .portal_members import resolve_portal_agency_for_user

    if resolve_portal_agency_for_user(request.user):
        return HttpResponseForbidden("Доступ запрещен")
    role = get_request_role(request)
    if role not in STAFF_CLIENT_PREVIEW_ROLES or not _staff_allowed(request):
        return HttpResponseForbidden("Доступ запрещен")
    if not _staff_preview_agency_allowed(client_id):
        return HttpResponseForbidden("Доступ запрещен")
    agency = Agency.objects.filter(pk=client_id).first()
    if not agency:
        return HttpResponseForbidden("Доступ запрещен")
    employee = get_request_employee(request)
    request.session[STAFF_CLIENT_PREVIEW_SESSION_KEY] = {
        "client_id": agency.id,
        "employee_id": getattr(employee, "id", None),
        "role": role,
        "return_url": _safe_staff_preview_return_url(request.GET.get("return"), role),
    }
    request.session.modified = True
    return redirect(build_client_cabinet_url(agency.id))


def staff_client_preview_return(request):
    if not request.user.is_authenticated:
        from django.contrib.auth.views import redirect_to_login

        return redirect_to_login(request.get_full_path(), login_url="/login/")
    role = get_request_role(request)
    if role not in STAFF_CLIENT_PREVIEW_ROLES or not _staff_allowed(request):
        return HttpResponseForbidden("Доступ запрещен")
    preview = request.session.pop(STAFF_CLIENT_PREVIEW_SESSION_KEY, None)
    request.session.modified = True
    return_url = (preview or {}).get("return_url") if isinstance(preview, dict) else ""
    return redirect(_safe_staff_preview_return_url(return_url, role))


_MARKETPLACE_FORM_SPECS = (
    ("WB", "Wildberries", WBMarketplaceSettingsForm, ("WB", "WILDBERRIES")),
)
_OZON_FBS_SLOTS = (1, 2, 3)


def _next_market_credential_id() -> int:
    return (MarketCredential.objects.aggregate(max_id=models.Max("id")).get("max_id") or 0) + 1


def _find_market_by_names(*names: str):
    qs = Market.objects.all()
    for name in names:
        market = qs.filter(name__iexact=name).first()
        if market:
            return market
    for name in names:
        market = qs.filter(name__icontains=name).first()
        if market:
            return market
    return None


def _build_marketplace_settings(*, agency=None, post_data=None) -> list[dict]:
    credentials = {}
    if agency and getattr(agency, "pk", None):
        credentials = {
            item.market_id: item
            for item in MarketCredential.objects.filter(agency=agency).select_related("market")
        }

    settings = []
    for code, label, form_class, market_names in _MARKETPLACE_FORM_SPECS:
        market = _find_market_by_names(*market_names)
        credential = credentials.get(market.id) if market else None
        form = form_class(
            data=post_data,
            initial={
                "market_key": "",
                "client_id": (credential.client_id or "") if credential else "",
            },
            credential_exists=bool(credential and (credential.market_key or "").strip()),
            prefix=code.lower(),
        )
        if code == "WB":
            is_configured = bool(credential and (credential.market_key or "").strip())
        else:
            is_configured = bool(
                credential
                and (credential.market_key or "").strip()
                and (credential.client_id or "").strip()
            )
        settings.append(
            {
                "code": code,
                "label": label,
                "market": market,
                "market_missing": market is None,
                "form": form,
                "is_configured": is_configured,
            }
        )
    return settings


def _build_ozon_fbs_settings(*, agency=None, post_data=None) -> list[dict]:
    credentials = {}
    if agency and getattr(agency, "pk", None):
        credentials = {
            item.slot: item
            for item in FbsOzonCredential.objects.filter(agency=agency).order_by("slot")
        }
        if not credentials:
            ozon_market = _find_market_by_names("OZON")
            legacy = (
                MarketCredential.objects.filter(agency=agency, market=ozon_market).first()
                if ozon_market
                else None
            )
            if legacy and str(legacy.market_key or "").strip():
                credentials[1] = legacy

    settings = []
    for slot in _OZON_FBS_SLOTS:
        credential = credentials.get(slot)
        api_key = str(
            getattr(credential, "api_key", None)
            or getattr(credential, "market_key", None)
            or ""
        ).strip()
        client_id = str(getattr(credential, "client_id", None) or "").strip()
        form = OzonMarketplaceSettingsForm(
            data=post_data,
            initial={"market_key": "", "client_id": client_id},
            credential_exists=bool(api_key),
            existing_client_id=client_id,
            prefix=f"ozon_fbs_{slot}",
        )
        settings.append(
            {
                "slot": slot,
                "label": f"Ozon кабинет {slot}",
                "form": form,
                "is_configured": bool(api_key and client_id),
                "existing_client_id": client_id,
                "existing_api_key": api_key,
            }
        )
    return settings


def _save_marketplace_settings(*, agency, settings: list[dict]) -> None:
    changed = False
    for item in settings:
        market = item.get("market")
        if market is None:
            continue
        cleaned = item["form"].cleaned_data
        market_key = str(cleaned.get("market_key") or "").strip()
        credential = MarketCredential.objects.filter(agency=agency, market=market).first()
        if item["code"] == "WB":
            if not market_key:
                continue
            if credential is None:
                credential = MarketCredential(id=_next_market_credential_id(), agency=agency, market=market)
            credential.market_key = market_key
            credential.client_id = None
            credential.save()
            changed = True
    if changed:
        from .marketplace_lk import invalidate_marketplace_auth_cache

        invalidate_marketplace_auth_cache(getattr(agency, "id", None))


@transaction.atomic
def _save_ozon_fbs_settings(*, agency, settings: list[dict]) -> None:
    existing = {
        item.slot: item
        for item in FbsOzonCredential.objects.select_for_update()
        .filter(agency=agency)
        .order_by("slot")
    }
    changed = False
    for item in settings:
        slot = int(item["slot"])
        cleaned = item["form"].cleaned_data
        client_id = str(cleaned.get("client_id") or "").strip()
        api_key = str(cleaned.get("market_key") or "").strip()
        credential = existing.get(slot)
        if not client_id and not api_key:
            if credential is not None:
                credential.delete()
                changed = True
            continue
        if credential is None:
            credential = FbsOzonCredential(
                agency=agency,
                slot=slot,
                client_id=client_id,
                api_key=api_key or str(item.get("existing_api_key") or "").strip(),
            )
        else:
            if credential.client_id != client_id:
                credential.client_id = client_id
                changed = True
            if api_key and credential.api_key != api_key:
                credential.api_key = api_key
                changed = True
        credential.full_clean(exclude={"id"})
        credential.save()
        changed = True

    ozon_market = _find_market_by_names("OZON")
    if ozon_market is not None:
        primary = FbsOzonCredential.objects.filter(agency=agency).order_by("slot").first()
        legacy = MarketCredential.objects.filter(agency=agency, market=ozon_market).first()
        if primary is None:
            if legacy is not None:
                legacy.delete()
                changed = True
        else:
            if legacy is None:
                legacy = MarketCredential(
                    id=_next_market_credential_id(),
                    agency=agency,
                    market=ozon_market,
                )
            legacy.client_id = primary.client_id
            legacy.market_key = primary.api_key
            legacy.save()
            changed = True

    if changed:
        from .marketplace_lk import invalidate_marketplace_auth_cache

        invalidate_marketplace_auth_cache(getattr(agency, "id", None))


def _build_client_username(*, agency_id: int | None = None) -> str:
    User = get_user_model()
    used_numbers: set[int] = set()
    for username in User.objects.filter(username__startswith="client").values_list("username", flat=True):
        match = re.fullmatch(r"client(\d+)", str(username or ""))
        if match:
            used_numbers.add(int(match.group(1)))
    next_number = max(used_numbers, default=0) + 1
    if agency_id:
        next_number = max(next_number, int(agency_id))
    while next_number in used_numbers:
        next_number += 1
    return f"client{next_number}"


def _save_client_portal_user(*, agency, raw_login: str, raw_password: str) -> None:
    login = str(raw_login or "").strip()
    password = str(raw_password or "")
    if not getattr(agency, "pk", None):
        return
    portal_user = agency.portal_user
    if portal_user is None:
        if not password:
            return
        resolved_login = _build_client_username(agency_id=agency.id)
        User = get_user_model()
        portal_user = User.objects.create_user(
            username=resolved_login,
            password=password,
            email=agency.email or "",
        )
        agency.portal_user = portal_user
        agency.save(update_fields=["portal_user"])
        return
    resolved_login = login or portal_user.username
    update_fields = []
    if portal_user.username != resolved_login:
        portal_user.username = resolved_login
        update_fields.append("username")
    email = agency.email or ""
    if portal_user.email != email:
        portal_user.email = email
        update_fields.append("email")
    if password:
        portal_user.set_password(password)
        update_fields.append("password")
    if update_fields:
        portal_user.save(update_fields=update_fields)


def _manager_due_date(now):
    cutoff = now.replace(hour=15, minute=0, second=0, microsecond=0)
    if now < cutoff:
        return now
    return now + timedelta(days=1)


def _format_agency_value(value):
    if value is None or value == "":
        return "-"
    if isinstance(value, bool):
        return "Да" if value else "Нет"
    return str(value).strip() or "-"


def _describe_agency_changes(old_snapshot: dict, new_snapshot: dict) -> str:
    fields = [
        ("agn_name", "Название"),
        ("short_name", "Сокращенное название"),
        ("pref", "Префикс для кодов"),
        ("inn", "ИНН"),
        ("kpp", "КПП"),
        ("ogrn", "ОГРН"),
        ("phone", "Телефон"),
        ("email", "Email"),
        ("adres", "Юр. адрес"),
        ("fakt_adres", "Факт. адрес"),
        ("fio_agn", "Контактное лицо"),
        ("sign_oferta", "Оферта"),
        ("use_nds", "НДС"),
        ("contract_numb", "Номер договора"),
        ("contract_link", "Ссылка на договор"),
        ("archived", "Архив"),
    ]
    changes = []
    for key, label in fields:
        old_val = _format_agency_value(old_snapshot.get(key))
        new_val = _format_agency_value(new_snapshot.get(key))
        if old_val != new_val:
            changes.append(f"{label}: {old_val} -> {new_val}")
    if not changes:
        return "Изменений нет"
    return "Изменены реквизиты: " + "; ".join(changes)


def _create_manager_task(order_id, agency, request, submitted_at):
    if not agency:
        return
    manager = (
        Employee.objects.filter(role="manager", is_active=True)
        .order_by("full_name")
        .first()
    )
    if not manager:
        return
    description = f"Клиент: {agency.agn_name or agency.inn or agency.id}"
    Task.objects.create(
        title=f"Подтвердите заявку на приемку товара №{order_id}",
        description=description,
        route=f"/orders/receiving/{order_id}/",
        assigned_to=manager,
        created_by=request.user if request.user.is_authenticated else None,
        due_date=_manager_due_date(submitted_at),
    )


def _order_type_label(order_type: str) -> str:
    if not order_type:
        return "-"
    labels = {"receiving": "PR", "packing": "ЗУ", "processing": "OBR", "shipping": "OTG"}
    return labels.get(order_type, order_type)


def _entry_status_value(order_type: str | None, payload: dict | None) -> str:
    data = payload or {}
    if order_type == "shipping":
        return (data.get("shipping_state") or "").lower()
    return (data.get("status") or data.get("submit_action") or "").lower()


def _repair_mojibake_text(value) -> str:
    """Fix UTF-8 text that was decoded as cp1251 (incl. mixed good+broken strings)."""
    text = str(value or "")
    if not text:
        return ""

    def _score(raw: str) -> int:
        return (
            raw.count("Р")
            + raw.count("С")
            + raw.count("в„")
            + raw.count("вЂ")
            + sum(3 for char in raw if 0x80 <= ord(char) <= 0x9F)
        )

    def _cp1251_mojibake_bytes(raw: str) -> bytes | None:
        chunks: list[bytes] = []
        for char in raw:
            code = ord(char)
            if 0x80 <= code <= 0x9F:
                chunks.append(bytes([code]))
                continue
            try:
                chunks.append(char.encode("cp1251"))
            except UnicodeEncodeError:
                return None
        return b"".join(chunks)

    def _try_repair_whole(current: str) -> str:
        for _ in range(4):
            raw_bytes = _cp1251_mojibake_bytes(current)
            if raw_bytes is None:
                break
            try:
                repaired = raw_bytes.decode("utf-8")
            except UnicodeDecodeError:
                break
            if _score(repaired) >= _score(current):
                break
            current = repaired
        return current

    whole = _try_repair_whole(text)
    if _score(whole) < 3 and not any(0x80 <= ord(char) <= 0x9F for char in whole):
        return whole

    # Mixed strings: mojibake prefix + already-correct Cyrillic/punctuation.
    # Greedily take the longest prefix that successfully re-decodes with a better score.
    parts: list[str] = []
    index = 0
    length = len(text)
    while index < length:
        best_end = None
        best_repaired = None
        limit = min(length, index + 240)
        for end in range(index + 1, limit + 1):
            chunk = text[index:end]
            raw_bytes = _cp1251_mojibake_bytes(chunk)
            if raw_bytes is None:
                break
            try:
                repaired = raw_bytes.decode("utf-8")
            except UnicodeDecodeError:
                continue
            if _score(repaired) < _score(chunk):
                best_end = end
                best_repaired = repaired
        if best_end is not None and best_repaired is not None and best_end > index + 1:
            parts.append(_try_repair_whole(best_repaired))
            index = best_end
            continue
        parts.append(text[index])
        index += 1

    mixed = "".join(parts)
    return mixed if _score(mixed) <= _score(whole) else whole


def _is_sent_to_manager(payload: dict) -> bool:
    status_value = _entry_status_value("receiving", payload)
    status_label = _repair_mojibake_text(payload.get("status_label") or "").lower()
    if status_value in {"sent_unconfirmed", "send", "submitted"}:
        return True
    return "подтверждени" in status_label


def _order_title_label(
    order_type: str,
    order_id: str,
    payload: dict | None = None,
    *,
    entries=None,
    created_at=None,
    variant: str = "full",
) -> str:
    return resolve_order_title(
        order_type,
        order_id,
        payload=payload,
        entries=entries,
        created_at=created_at,
        variant=variant,
    )


_SHIPPING_ORDER_CACHE: dict[tuple, object] = {}
_SHIPPING_TRIP_CACHE: dict[tuple, str] = {}


def clear_lk_shipping_status_caches() -> None:
    """Drop per-request caches used while building LK request lists."""
    _SHIPPING_ORDER_CACHE.clear()
    _SHIPPING_TRIP_CACHE.clear()


def preload_lk_shipping_status_caches(agency_id, order_numbers: list[str] | tuple[str, ...] | None) -> None:
    """Bulk-fill shipping order/trip caches for LK list (avoids N+1)."""
    numbers = [str(n).strip() for n in (order_numbers or []) if str(n).strip()]
    if not numbers:
        return
    try:
        from logistics.models import LogisticsTripOrder
        from shipping.models import ShippingOrder
    except Exception:
        return

    qs = ShippingOrder.objects.filter(number__in=numbers)
    if agency_id:
        qs = qs.filter(agency_id=agency_id)
    for order in qs.only("id", "number", "status", "agency_id"):
        _SHIPPING_ORDER_CACHE[(agency_id, order.number)] = order
    for num in numbers:
        key = (agency_id, num)
        if key not in _SHIPPING_ORDER_CACHE:
            _SHIPPING_ORDER_CACHE[key] = None

    trip_qs = (
        LogisticsTripOrder.objects.select_related("trip", "shipping_order")
        .filter(
            shipping_order__number__in=numbers,
            trip__status__in={"draft", "planned", "loading", "departed", "completed"},
        )
        .order_by("-trip__updated_at", "-trip__created_at", "-id")
    )
    if agency_id:
        trip_qs = trip_qs.filter(shipping_order__agency_id=agency_id)
    best: dict[str, str] = {}
    for link in trip_qs:
        num = str(getattr(getattr(link, "shipping_order", None), "number", "") or "").strip()
        if not num or num in best:
            continue
        best[num] = str(getattr(getattr(link, "trip", None), "status", "") or "").strip()
    for num in numbers:
        _SHIPPING_TRIP_CACHE[(agency_id, num)] = best.get(num, "")


def _shipping_trip_status(order_id: str | None, agency_id=None) -> str:
    raw_number = str(order_id or "").strip()
    if not raw_number:
        return ""
    cache_key = (agency_id, raw_number)
    if cache_key in _SHIPPING_TRIP_CACHE:
        return _SHIPPING_TRIP_CACHE[cache_key]
    try:
        from logistics.models import LogisticsTripOrder

        qs = LogisticsTripOrder.objects.select_related("trip").filter(
            shipping_order__number=raw_number,
            trip__status__in={
                "draft",
                "planned",
                "loading",
                "departed",
                "completed",
            },
        )
        if agency_id:
            qs = qs.filter(shipping_order__agency_id=agency_id)
        trip_link = qs.order_by("-trip__updated_at", "-trip__created_at", "-id").first()
    except Exception:
        _SHIPPING_TRIP_CACHE[cache_key] = ""
        return ""
    value = str(getattr(getattr(trip_link, "trip", None), "status", "") or "").strip()
    _SHIPPING_TRIP_CACHE[cache_key] = value
    return value


_SHIPPING_FALLBACK_LABELS = {
    "draft": "Черновик клиента",
    "submitted": "На согласовании менеджера",
    "reserved": "Согласована и передана в работу кладовщику",
    "storekeeper_accepted": "Принята в работу складом",
    "picking": "Доставка в зону отгрузки (ричтрак)",
    "packed": "Подготовлена складом, ожидает логиста",
    "shipped": "Отгружена",
    "partial_shipped": "Отгружена частично",
    "canceled": "Отменена",
}


def _resolve_shipping_order_for_entry(entry):
    """Load live ShippingOrder for a dashboard/audit entry when DB is available."""
    raw = str(getattr(entry, "order_id", "") or "").strip()
    if not raw:
        return None
    agency_id = getattr(entry, "agency_id", None)
    if agency_id is None:
        agency_id = getattr(getattr(entry, "agency", None), "id", None)
    cache_key = (agency_id, raw)
    if cache_key in _SHIPPING_ORDER_CACHE:
        return _SHIPPING_ORDER_CACHE[cache_key]
    order = None
    try:
        from shipping.models import ShippingOrder

        qs = ShippingOrder.objects.all()
        if agency_id:
            qs = qs.filter(agency_id=agency_id)
        order = qs.filter(number=raw).first()
        if order is None and raw.isdigit():
            order = qs.filter(pk=int(raw)).first()
    except Exception:
        order = None
    _SHIPPING_ORDER_CACHE[cache_key] = order
    return order


def _shipping_bucket_from_state(status_value: str, trip_status: str = "") -> str:
    trip = str(trip_status or "").strip().lower()
    status = str(status_value or "").strip().lower()
    if trip in {"departed", "completed"}:
        return "done"
    if status == "draft":
        return "client"
    if status == "submitted":
        return "manager"
    if status in {"reserved", "storekeeper_accepted", "picking", "packed"}:
        return "warehouse"
    if status in {"shipped", "partial_shipped", "canceled"}:
        return "done"
    return "manager"


def _shipping_fallback_label(status_value: str, trip_status: str = "") -> str:
    trip = str(trip_status or "").strip().lower()
    status = str(status_value or "").strip().lower()
    if trip == "departed":
        return "Загружено в машину"
    if trip == "completed":
        return "Рейс завершен"
    return _SHIPPING_FALLBACK_LABELS.get(status, status or "-")


def _shipping_list_label(order, trip_status: str = "") -> str:
    """Client-list label from order/trip without WarehouseGoodsStateResolver."""
    trip = str(trip_status or "").strip().lower()
    status = str(getattr(order, "status", "") or "").strip().lower()
    if trip == "completed":
        return "Рейс завершен"
    if trip == "departed":
        return "Загружено в машину"
    if trip == "loading" and status == "packed":
        return "Логист сформировал рейс, ожидается погрузка"
    return _shipping_fallback_label(status, trip_status)


def _shipping_kanban_status(entry) -> tuple[str, str]:
    """Return (status_label, bucket) for LK/dashboard lists (fast path)."""
    payload = entry.payload if isinstance(getattr(entry, "payload", None), dict) else {}
    status_value = _entry_status_value("shipping", payload)
    agency_id = getattr(entry, "agency_id", None)
    if agency_id is None:
        agency_id = getattr(getattr(entry, "agency", None), "id", None)
    order = _resolve_shipping_order_for_entry(entry)
    if order is not None:
        trip_status = _shipping_trip_status(
            getattr(order, "number", None) or getattr(entry, "order_id", ""),
            agency_id=agency_id or getattr(order, "agency_id", None),
        )
        label = _shipping_list_label(order, trip_status)
        bucket = _shipping_bucket_from_state(order.status, trip_status)
        return label, bucket

    trip_status = _shipping_trip_status(getattr(entry, "order_id", ""), agency_id=agency_id)
    return (
        _shipping_fallback_label(status_value, trip_status),
        _shipping_bucket_from_state(status_value, trip_status),
    )


def _processing_list_label(payload: dict, status_value: str) -> str:
    status_label = _repair_mojibake_text(payload.get("status_label") or "")
    status_label_l = status_label.lower()
    if status_value == "draft" or "черновик" in status_label_l:
        return "Черновик"
    if status_value == "cancel_requested" or "отмена на согласовании" in status_label_l:
        return "Отмена на согласовании менеджера"
    completed_label = "выполн" in status_label_l and "выполняется" not in status_label_l
    if status_value in {"done", "completed", "closed", "finished"} or any(
        token in status_label_l for token in ("заверш", "закрыт")
    ) or completed_label:
        return "Выполнена"
    if status_label:
        return status_label
    return status_value or "-"


def _processing_list_bucket(payload: dict, status_value: str) -> str:
    status_label = _repair_mojibake_text(payload.get("status_label") or "").lower()
    if status_value == "draft" or "черновик" in status_label:
        return "client"
    if status_value == "cancel_requested" or "отмена на согласовании" in status_label:
        return "manager"
    completed_label = "выполн" in status_label and "выполняется" not in status_label
    if status_value in {"done", "completed", "closed", "finished"} or any(
        token in status_label for token in ("заверш", "закрыт", "размещ")
    ):
        return "done"
    if completed_label:
        return "done"
    if status_value in {
        "processing_in_work",
        "reserved",
        "in_progress",
        "warehouse",
        "on_warehouse",
    } or any(
        token in status_label
        for token in ("взята в работу", "в работе", "обработ", "сборк", "упаков")
    ):
        return "warehouse"
    return "manager"


def _order_status_label(entry) -> str:
    payload = entry.payload or {}
    status_value = _entry_status_value(entry.order_type, payload)
    status_label_raw = _repair_mojibake_text(payload.get("status_label") or "")
    if entry.order_type == "shipping":
        # Dashboard/list path must stay lightweight: the live warehouse resolver
        # can traverse movement state for many orders and time out the client LK.
        label, _bucket = _shipping_kanban_status(entry)
        return label
    if entry.order_type == "processing":
        # List path: payload/status only. Detail pages still use live resolver.
        return _processing_list_label(payload if isinstance(payload, dict) else {}, status_value)
    try:
        resolved = resolve_lk_entry_status(entry, audience="client")
        if resolved.status_label:
            return resolved.status_label
    except Exception:
        pass
    if status_value == "cancel_requested" or "отмена на согласовании" in status_label_raw.lower():
        return "Отмена на согласовании менеджера"
    if (payload.get("act_client_response") or "").lower() == "confirmed":
        return "Выполнена"
    if status_value == "draft":
        return "Черновик"
    if status_value in {"done", "completed", "closed", "finished"}:
        return "Выполнена"
    if payload.get("act_sent") and (payload.get("act_client_response") or "").lower() != "confirmed":
        return "Акт отправлен клиенту"
    if payload.get("act") == "placement":
        state = (payload.get("act_state") or "closed").lower()
        return "Размещение на складе" if state == "open" else "Товар принят и размещен на складе"
    status_label = _repair_mojibake_text(payload.get("status_label") or "").lower()
    if "взята в работу" in status_label:
        return "Взята в работу"
    if "товар принят" in status_label:
        return "Товар принят и размещен на складе"
    if status_value in {"sent_unconfirmed", "send", "submitted"} or "подтверж" in status_label:
        return "Ждет подтверждения"
    if status_value in {"warehouse", "on_warehouse"} or "ожидании поставки" in status_label or "на складе" in status_label:
        return "В ожидании поставки товара"
    return _repair_mojibake_text(payload.get("status_label") or payload.get("status") or "-")


def _is_status_entry(entry) -> bool:
    payload = entry.payload or {}
    if entry.action == "status":
        return True
    return bool(
        payload.get("status")
        or payload.get("status_label")
        or payload.get("submit_action")
        or payload.get("shipping_state")
    )


def _is_draft_entry(entry) -> bool:
    payload = entry.payload or {}
    status_value = _entry_status_value(entry.order_type, payload)
    status_label = _repair_mojibake_text(payload.get("status_label") or "").lower()
    return status_value == "draft" or "черновик" in status_label


def _order_detail_url(entry, client_id: int | None, client_view: bool) -> str:
    order_type = str(getattr(entry, "order_type", "") or "").strip().lower()
    if order_type in {"stock_move", "stockmove"}:
        # Internal warehouse moves are not client cards.
        return build_client_cabinet_url(client_id) if client_view and client_id else "/orders/"
    if client_view and client_id and _is_draft_entry(entry):
        from .client_drafts import continue_url_for_entry

        return continue_url_for_entry(
            agency_id=client_id,
            order_type=entry.order_type,
            order_id=str(entry.order_id or ""),
        )
    if entry.order_type == "shipping":
        from shipping.models import ShippingOrder

        raw_number = canonical_request_order_id("shipping", str(entry.order_id or "").strip())
        qs = ShippingOrder.objects.filter(number=raw_number).only("id")
        agency_id = getattr(getattr(entry, "agency", None), "id", None) or client_id
        if agency_id:
            qs = qs.filter(agency_id=agency_id)
        shipping_order = qs.first()
        suffix = f"?client={client_id}" if client_view and client_id else ""
        if shipping_order:
            return f"/shipping/{shipping_order.id}/{suffix}"
        return f"/shipping/{suffix}"
    if order_type == "other":
        oid = str(entry.order_id or "").strip()
        # В ЛК клиента — hash-карточка; менеджер/склад открывают WMS /orders/other/.
        if client_view and client_id:
            return f"{build_client_cabinet_url(client_id)}#/request/other/{oid}"
        return f"/orders/other/{oid}/"
    suffix = f"?client={client_id}" if client_view and client_id else ""
    return f"/orders/{entry.order_type}/{entry.order_id}/{suffix}"


def _order_bucket(entry) -> str:
    payload = entry.payload or {}
    status_value = _entry_status_value(entry.order_type, payload)
    status_label = _repair_mojibake_text(payload.get("status_label") or "").lower()
    if entry.order_type == "shipping":
        _label, bucket = _shipping_kanban_status(entry)
        return bucket
    if entry.order_type == "processing":
        return _processing_list_bucket(payload if isinstance(payload, dict) else {}, status_value)
    try:
        resolved = resolve_lk_entry_status(entry, audience="client")
        if resolved.bucket:
            return resolved.bucket
    except Exception:
        pass
    if status_value == "cancel_requested" or "отмена на согласовании" in status_label:
        return "manager"
    if status_value == "draft" or "черновик" in status_label:
        return "client"
    if (
        (payload.get("act_client_response") or "").lower() == "confirmed"
        or status_value in {"done", "completed", "closed", "finished"}
        or any(
        token in status_label for token in ("выполн", "заверш", "закрыт", "утвержден")
        )
    ):
        return "done"
    if payload.get("act_sent"):
        return "client"
    if payload.get("act") == "placement":
        act_state = (payload.get("act_state") or "closed").lower()
        return "warehouse" if act_state == "open" else "done"
    if status_value in {"processing_in_work"} or any(
        token in status_label for token in ("взята в работу", "в работе")
    ):
        return "warehouse"
    if status_value in {"warehouse", "on_warehouse"} or any(token in status_label for token in ("склад", "прием", "приём", "ожидании поставки")):
        return "warehouse"
    return "manager"


def _receiving_act_needs_client_attention(act_entry, status_entry) -> bool:
    act_payload = dict(act_entry.payload or {}) if act_entry and isinstance(act_entry.payload, dict) else {}
    status_payload = dict(status_entry.payload or {}) if status_entry and isinstance(status_entry.payload, dict) else {}
    if not act_payload.get("act_sent"):
        return False
    client_response = str(
        status_payload.get("act_client_response")
        or act_payload.get("act_client_response")
        or ""
    ).strip().lower()
    if client_response in {"confirmed", "dispute"}:
        return False
    # Просмотр акта не снимает внимание: клиенту всё ещё нужно подтвердить/скачать акт.
    return True


def _shipping_act_needs_client_attention(act_entry, status_entry) -> bool:
    """Show shipping act card when manager signed/sent and client has not viewed it yet."""
    act_payload = dict(act_entry.payload or {}) if act_entry and isinstance(act_entry.payload, dict) else {}
    status_payload = dict(status_entry.payload or {}) if status_entry and isinstance(status_entry.payload, dict) else {}
    if not (act_payload.get("act_sent") or act_payload.get("act_manager_signed")):
        return False
    if bool(status_payload.get("act_viewed") or act_payload.get("act_viewed")):
        return False
    return True


_ISO_DATETIME_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?\b")


def _format_message_text(text: str) -> str:
    if not text:
        return ""
    text = _repair_mojibake_text(text)

    def _replace(match):
        raw = match.group(0)
        try:
            parsed = timezone.datetime.fromisoformat(raw)
        except ValueError:
            return raw
        return parsed.strftime("%d.%m.%Y, %H:%M")

    return _ISO_DATETIME_RE.sub(_replace, text)


def _verification_qty(value) -> int:
    try:
        return max(int(str(value).strip()), 0)
    except (TypeError, ValueError):
        return 0


def _received_total_for_agency(agency: Agency) -> tuple[int, int]:
    latest_receiving_payloads: dict[str, dict] = {}
    qs = (
        OrderAuditEntry.objects.filter(
            agency=agency,
            order_type="receiving",
            payload__act="receiving",
        )
        .only("order_id", "payload", "created_at")
        .order_by("-created_at")
    )
    for entry in qs:
        if entry.order_id in latest_receiving_payloads:
            continue
        latest_receiving_payloads[entry.order_id] = entry.payload or {}

    total = 0
    for payload in latest_receiving_payloads.values():
        for item in payload.get("act_items") or []:
            qty = _verification_qty(item.get("actual_qty"))
            if qty <= 0:
                qty = _verification_qty(item.get("qty"))
            total += qty
    return total, len(latest_receiving_payloads)


def _shipped_total_for_agency(agency: Agency) -> tuple[int, int]:
    from shipping.models import ShippingOrder, ShippingOrderItem

    shipped_statuses = [
        ShippingOrder.STATUS_SHIPPED,
        ShippingOrder.STATUS_PARTIAL,
    ]
    shipped_orders_qs = ShippingOrder.objects.filter(agency=agency, status__in=shipped_statuses)
    shipped_total = (
        ShippingOrderItem.objects.filter(order__in=shipped_orders_qs)
        .aggregate(total=Sum("qty_shipped"))
        .get("total")
        or 0
    )
    shipped_orders_count = shipped_orders_qs.count()
    return int(shipped_total), int(shipped_orders_count)


def _stock_total_for_agency(agency: Agency) -> int:
    stock_total = (
        WarehouseStockSnapshot.objects.filter(
            agency=agency,
            is_archived=False,
        )
        .exclude(warehouse_state_code=WarehouseStateCode.PROCESSING_IN_PROGRESS.value)
        .aggregate(total=Sum("qty"))
        .get("total")
        or 0
    )
    return int(stock_total)


def _processing_in_progress_total_for_agency(agency: Agency) -> int:
    total = (
        WarehouseStockSnapshot.objects.filter(
            agency=agency,
            warehouse_state_code=WarehouseStateCode.PROCESSING_IN_PROGRESS.value,
            is_archived=False,
        )
        .aggregate(total=Sum("processing_reserved_qty"))
        .get("total")
        or 0
    )
    if total:
        return int(total)
    fallback_total = (
        WarehouseStockSnapshot.objects.filter(
            agency=agency,
            warehouse_state_code=WarehouseStateCode.PROCESSING_IN_PROGRESS.value,
            is_archived=False,
        )
        .aggregate(total=Sum("qty"))
        .get("total")
        or 0
    )
    return int(fallback_total)


def _inventory_check_for_agency(agency: Agency | None) -> dict | None:
    if not agency:
        return None
    received_total, receiving_orders_count = _received_total_for_agency(agency)
    shipped_total, shipped_orders_count = _shipped_total_for_agency(agency)
    stock_total = _stock_total_for_agency(agency)
    processing_in_progress_total = _processing_in_progress_total_for_agency(agency)
    owned_total = int(stock_total) + int(processing_in_progress_total)
    expected_stock = int(received_total) - int(shipped_total)
    discrepancy = int(owned_total) - expected_stock
    is_ok = discrepancy == 0
    return {
        "received_total": int(received_total),
        "receiving_orders_count": int(receiving_orders_count),
        "shipped_total": int(shipped_total),
        "shipped_orders_count": int(shipped_orders_count),
        "expected_stock": int(expected_stock),
        "stock_total": int(stock_total),
        "processing_in_progress_total": int(processing_in_progress_total),
        "owned_total": int(owned_total),
        "discrepancy": int(discrepancy),
        "status_label": "Сходится" if is_ok else "Есть расхождение",
        "status_tone": "ok" if is_ok else "warn",
        "computed_at": timezone.localtime(),
    }


def dashboard(request):
    """Entry point for client cabinet — always the new LK."""
    if not request.user.is_authenticated:
        return HttpResponseForbidden("Доступ запрещен")
    params = request.GET.copy()
    params.pop("legacy", None)
    target = "/client/dashboard/lk/"
    if params:
        target = f"{target}?{params.urlencode()}"
    return redirect(target)


def dashboard_new(request):
    """Deprecated preview cabinet — redirect to primary LK."""
    if not request.user.is_authenticated:
        return HttpResponseForbidden("Доступ запрещен")
    params = request.GET.copy()
    params.pop("legacy", None)
    target = "/client/dashboard/lk/"
    if params:
        target = f"{target}?{params.urlencode()}"
    return redirect(target)


def _lk_request_type_key(order_type: str) -> str:
    raw = str(order_type or "").strip().lower()
    if raw in {"receiving", "shipping", "processing"}:
        return raw
    return "other"


def _lk_request_type_label(order_type: str) -> str:
    raw = str(order_type or "").strip().lower()
    if raw == "packing":
        raw = "processing"
    labels = {
        "receiving": "Приёмка",
        "shipping": "Отгрузка",
        "processing": "Обработка",
        "other": "Прочая заявка",
    }
    return labels.get(_lk_request_type_key(raw), labels["other"])


def _lk_request_type_badge(order_type: str) -> str:
    badges = {
        "receiving": "ПР",
        "shipping": "OT",
        "processing": "ОБ",
        "packing": "ОБ",
        "other": "ПРЧ",
    }
    key = str(order_type or "").strip().lower()
    return badges.get(key, "ДР")


def _lk_request_filter_status(*, bucket: str, status_label: str, attention: bool = False) -> str:
    label = str(status_label or "").strip().lower()
    if "отмена на согласовании" in label:
        return "waiting"
    if is_terminal_cancelled_status(label):
        return "cancelled"
    if attention or "уточн" in label:
        return "clarification"
    if bucket == "done":
        return "completed"
    if bucket == "warehouse":
        return "processing"
    return "waiting"


def _lk_request_status_pill(filter_status: str) -> str:
    return {
        "processing": "В обработке",
        "waiting": "Ожидает",
        "completed": "Завершена",
        "clarification": "Требует уточнения",
        "cancelled": "Отменена",
    }.get(filter_status, "Ожидает")


def _lk_format_request_date(value) -> str:
    if not value:
        return ""
    months = {
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
    try:
        local = timezone.localtime(value)
    except Exception:
        local = value
    try:
        return f"{local.day} {months.get(local.month, '')} {local.year} г."
    except Exception:
        return str(value)


def dashboard_lk(request):
    if not request.user.is_authenticated:
        from django.contrib.auth.views import redirect_to_login

        return redirect_to_login(request.get_full_path(), login_url="/login/")
    selected_client, client_view, allowed = _get_client_for_request(request)
    if not allowed:
        return HttpResponseForbidden("Доступ запрещен")
    chats_are_enabled = chats_enabled()
    from .portal_members import portal_sections_for_user

    portal_allowed_sections = (
        portal_sections_for_user(request.user, selected_client)
        if selected_client and client_view
        else None
    )
    # Portal client shell: skip stock/finance/SKU/live on SSR (~4–5s).
    # KPI and WMS LIVE hydrate via API; «Мои заявки» uses lightweight request payloads.
    # Preview gets the light shell too; client_view still controls permissions.
    fast_shell = bool(selected_client)

    stock_rows = []
    sku_items = []
    sku_context = {}
    stock_total = 0
    stock_available = 0
    stock_reserved = 0
    processing_reserved = 0
    shipping_reserved = 0
    other_reserved = 0
    low_stock_count = 0
    marketplace_count = 0
    live_dashboard = {
        "ready_for_shipping_units": 0,
        "supply_wb_label": "нет слота на сегодня",
        "supply_ozon_label": "нет слота на сегодня",
    }
    marking_stats = {"total": 0, "free": 0, "reserved": 0, "used": 0}
    finance_summary = {
        "documents": [],
        "document_counts": {"invoice": 0, "upd": 0, "act": 0, "all": 0},
        "billing": None,
        "billing_available": True,
        "months": [],
        "recent_documents": [],
        "recent_services": [],
    }
    live_history: list = []
    marketplace_home: list = []
    orders_list: list = []
    now_local = timezone.localtime()

    if fast_shell:
        empty_columns = [
            {"status": "client", "label": "У клиента", "orders": [], "count": 0},
            {"status": "manager", "label": "У менеджера", "orders": [], "count": 0},
            {"status": "warehouse", "label": "В работе", "orders": [], "count": 0},
            {"status": "done", "label": "Выполнена", "orders": [], "count": 0},
        ]
        context = {
            "selected_client": selected_client,
            "client_filter_param": f"?agency={selected_client.id}",
            "client_view": client_view,
            "orders_panel_columns": empty_columns,
            "orders_panel_stats": [
                {"status": c["status"], "label": c["label"], "count": 0} for c in empty_columns
            ],
            "orders_panel_total": 0,
            "client_messages": [],
            "act_attention_cards": [],
            "act_attention_count": 0,
            "act_attention_first_url": "",
            "inventory_check": None,
            "run_inventory_check": False,
            "notifications_unread": 0,
            "chat_unread_count": 0,
            "chats_enabled": chats_are_enabled,
        }
        from .api_views import _act_attention_payload
        from .lk_requests import build_client_notifications
        from .messaging_lk import unread_notifications_count

        # Shell first: unread + short notifications. Act cards + заявки — из лёгких билдеров.
        context["client_messages"] = build_client_notifications(selected_client, limit=12)
        context["notifications_unread"] = unread_notifications_count(selected_client)
        if chats_are_enabled:
            from .messaging_lk import unread_chat_count_for_client

            context["chat_unread_count"] = unread_chat_count_for_client(selected_client)

        act_attention_cards = []
        for card in _act_attention_payload(selected_client):
            order_id = str(card.get("order_id") or "")
            title = str(card.get("title") or "Акт")
            if order_id and f"№{order_id}" not in title:
                title = f"{title} по заявке №{order_id}"
            act_attention_cards.append(
                {
                    "bucket": "client",
                    "order_id": order_id,
                    "order_type": card.get("order_type") or "",
                    "type_label": "Акт",
                    "title": title,
                    "status_label": card.get("status_label") or "Акт ожидает действия",
                    "detail_url": card.get("detail_url") or "#/requests",
                    "attention": True,
                }
            )
        context["act_attention_cards"] = act_attention_cards
        context["act_attention_count"] = len(act_attention_cards)
        if act_attention_cards:
            context["act_attention_first_url"] = str(act_attention_cards[0].get("detail_url") or "")

        # Fast shell: не тащим весь журнал заявок в HTML.
        # Критичные акты оставляем на домашнем экране, полный список грузится
        # через /client/api/v1/requests/ при открытии раздела «Мои заявки».
        orders_list = []
        for card in act_attention_cards:
            order_type = str(card.get("order_type") or "")
            order_id = str(card.get("order_id") or "")
            type_key = "processing" if order_type == "packing" else _lk_request_type_key(order_type)
            orders_list.append(
                {
                    "id": f"{order_type}-{order_id}",
                    "number": order_id,
                    "order_id": order_id,
                    "order_type": order_type,
                    "title": card.get("title") or f"Акт по заявке №{order_id}",
                    "display_title": card.get("title") or f"Акт по заявке №{order_id}",
                    "type": type_key,
                    "type_label": _lk_request_type_label(order_type),
                    "type_badge": _lk_request_type_badge(order_type),
                    "subtitle": card.get("status_label") or "Акт ожидает действия",
                    "date": "",
                    "status": "clarification",
                    "status_pill": "Уточнение",
                    "attention": True,
                    "detail_url": card.get("detail_url") or "#/requests",
                    "wms_url": "",
                    "status_tone": "attention",
                    "bucket": "client",
                    "is_draft": False,
                }
            )
        marketplace_home = [
            {
                "id": "yandex",
                "name": "Яндекс Маркет",
                "configured": False,
                "status_label": "Требуется авторизация",
                "status": "not_connected",
            }
        ]
    else:
        context = build_dashboard_context(
            request=request,
            selected_client=selected_client,
            client_view=client_view,
            run_inventory_check=request.GET.get("verify") == "1",
        )
        context.setdefault("notifications_unread", 0)
        context.setdefault("chat_unread_count", 0)
        context["chats_enabled"] = chats_are_enabled
        if selected_client:
            from .lk_requests import build_client_notifications

            context["client_messages"] = build_client_notifications(selected_client, limit=30)
            from .messaging_lk import unread_notifications_count

            context["notifications_unread"] = unread_notifications_count(selected_client)
            if chats_are_enabled:
                from .messaging_lk import unread_chat_count_for_client

                context["chat_unread_count"] = unread_chat_count_for_client(selected_client)
            from sklad.services.stock_availability import StockAvailabilityService

            stock_availability_rows = StockAvailabilityService.stock_rows_with_availability(
                agency=selected_client
            )
            stock_available = sum(int(row.get("available_qty") or 0) for row in stock_availability_rows)
            # ЛК клиента: KPI «на складе» = только доступное к отгрузке.
            stock_total = stock_available
            from .marketplace_lk import build_live_dashboard, build_marketplaces_payload

            live_dashboard = build_live_dashboard(selected_client)
            # Process-stage units for WMS LIVE (not mixed into available stock KPI).
            processing_reserved = int(live_dashboard.get("in_processing_units") or 0)
            shipping_reserved = int(live_dashboard.get("ready_for_shipping_units") or 0)
            other_reserved = 0
            stock_reserved = 0
            grouped_stock = {}
            for row in stock_availability_rows:
                available_qty = int(row.get("available_qty") or 0)
                if available_qty <= 0:
                    continue
                key = (
                    row.get("sku_code") or row.get("sku") or "",
                    row.get("name") or "",
                    row.get("barcode") or "",
                    row.get("size") or "",
                )
                item = grouped_stock.setdefault(
                    key,
                    {
                        "sku_code": key[0],
                        "name": key[1],
                        "barcode": key[2],
                        "size": key[3],
                        "available_qty": 0,
                    },
                )
                item["available_qty"] += available_qty
            grouped_stock_rows = sorted(
                grouped_stock.values(),
                key=lambda row: (str(row.get("name") or "").lower(), str(row.get("sku_code") or "").lower()),
            )[:180]
            for row in grouped_stock_rows:
                available_qty = int(row.get("available_qty") or 0)
                if 0 < available_qty <= 2:
                    low_stock_count += 1
                stock_rows.append({
                    "sku_code": row.get("sku_code") or "",
                    "name": row.get("name") or "",
                    "barcode": row.get("barcode") or "",
                    "size": row.get("size") or "",
                    "qty": available_qty,
                    "available_qty": available_qty,
                    "reserved_qty": 0,
                })
            sku_items = list(build_client_sku_list_queryset(request=request, agency=selected_client)[:180])
            sku_context = build_client_sku_list_context(
                request=request,
                agency=selected_client,
                items=sku_items,
                view_modes={"table", "cards"},
            )
            marketplace_count = sum(
                1 for item in build_marketplaces_payload(selected_client, check_auth=False) if item.get("configured")
            )
            marking_qs = MarkingCode.objects.filter(agency=selected_client)
            marking_total = marking_qs.count()
            marking_used = marking_qs.exclude(used_at__isnull=True).count()
            marking_printed = marking_qs.exclude(printed_at__isnull=True).count()
            marking_stats = {
                "total": marking_total,
                "free": max(marking_total - marking_used, 0),
                "reserved": marking_printed,
                "used": marking_used,
            }
            from .finance_lk import build_finance_summary

            finance_summary = build_finance_summary(selected_client)

        # Rebuild list with stable created_at sort from panel columns.
        # Prefer attention cards when the same order appears twice (act + status row).
        from .lk_requests import request_attention

        dated_by_key: dict[tuple, tuple] = {}
        for column in context.get("orders_panel_columns") or []:
            bucket = str(column.get("status") or "")
            for order in column.get("orders") or []:
                order_type = str(order.get("order_type") or "")
                order_id = str(order.get("order_id") or "")
                key = (order_type, order_id)
                status_label = _repair_mojibake_text(order.get("status_label") or "")
                attention = bool(order.get("attention"))
                if selected_client and not attention:
                    attention = request_attention(
                        agency=selected_client,
                        order_type=order_type,
                        order_id=order_id,
                        payload={},
                    )
                filter_status = _lk_request_filter_status(
                    bucket=bucket,
                    status_label=status_label,
                    attention=attention,
                )
                type_key = "processing" if order_type == "packing" else _lk_request_type_key(order_type)
                is_draft = "черновик" in str(status_label or "").lower()
                # Карточка в ЛК; форма черновика — по кнопке «Продолжить» (wms_url).
                detail_url = lk_request_hash(type_key, order_id)
                wms_url = ""
                if is_draft:
                    from .client_drafts import continue_url_for_entry

                    wms_url = continue_url_for_entry(
                        agency_id=getattr(selected_client, "id", None),
                        order_type=order_type,
                        order_id=order_id,
                    ) or (order.get("detail_url") or "")
                title = _repair_mojibake_text(order.get("title") or f"Заявка {order_id}")
                row = {
                    "id": f"{order_type}-{order_id}",
                    "number": order_id,
                    "order_id": order_id,
                    "order_type": order_type,
                    "title": title,
                    "display_title": build_request_display_title(
                        order_type=order_type,
                        order_id=order_id,
                        payload=order.get("payload") if isinstance(order.get("payload"), dict) else {},
                        base_title=title,
                    ),
                    "type": type_key,
                    "type_label": _lk_request_type_label(order_type),
                    "type_badge": _lk_request_type_badge(order_type),
                    "subtitle": status_label,
                    "date": _lk_format_request_date(order.get("created_at")),
                    "status": filter_status,
                    "status_pill": _lk_request_status_pill(filter_status),
                    "attention": attention,
                    "detail_url": detail_url,
                    "wms_url": wms_url,
                    "status_tone": order.get("status_tone") or "",
                    "bucket": bucket,
                    "is_draft": is_draft,
                }
                existing = dated_by_key.get(key)
                if existing is None:
                    dated_by_key[key] = (order.get("created_at"), row)
                    continue
                # Upgrade to attention/clarification row; otherwise keep first (status card).
                if attention and not existing[1].get("attention"):
                    dated_by_key[key] = (existing[0] or order.get("created_at"), row)
        dated_rows = list(dated_by_key.values())
        dated_rows.sort(key=lambda item: item[0] or timezone.now(), reverse=True)
        orders_list = [row for _created, row in dated_rows]

        from .marketplace_lk import build_live_history, build_marketplaces_payload as _mp_payload_home

        live_history = build_live_history(selected_client, limit=5) if selected_client else []
        marketplace_home = (
            _mp_payload_home(selected_client, check_auth=False) if selected_client else []
        )
        # Ensure Yandex appears as stub if missing
        mp_ids = {str(item.get("id") or "").lower() for item in marketplace_home}
        if "ym" not in mp_ids and "yandex" not in mp_ids:
            marketplace_home = list(marketplace_home) + [
                {
                    "id": "yandex",
                    "name": "Яндекс Маркет",
                    "configured": False,
                    "status_label": "Требуется авторизация",
                    "status": "not_connected",
                }
            ]

    orders_active_count = sum(
        1 for row in orders_list if row.get("status") not in ("completed", "cancelled")
    )
    from .home_progress import enrich_request_row

    home_active_requests = []
    active_by_type = {"receiving": 0, "processing": 0, "shipping": 0, "other": 0}
    for row in orders_list:
        if row.get("status") in ("completed", "cancelled"):
            continue
        enriched = enrich_request_row(row)
        key = str(enriched.get("type") or enriched.get("order_type") or "other")
        if key == "packing":
            key = "processing"
        if key not in active_by_type:
            key = "other"
        active_by_type[key] += 1
        if len(home_active_requests) < 5:
            home_active_requests.append(enriched)

    packaging = (live_dashboard or {}).get("packaging") or {}
    unpaid_invoices_count = sum(
        1
        for doc in (finance_summary.get("documents") or [])
        if str(doc.get("doc_kind") or "") == "invoice"
        and "оплачен" not in str(doc.get("status_label") or "").lower()
        and str(doc.get("status") or "").lower() not in {"paid", "cancelled", "canceled"}
    )
    month_names = {
        1: "январь", 2: "февраль", 3: "март", 4: "апрель", 5: "май", 6: "июнь",
        7: "июль", 8: "август", 9: "сентябрь", 10: "октябрь", 11: "ноябрь", 12: "декабрь",
    }
    finance_month_title = f"Финансы за {month_names.get(now_local.month, '')}"

    context.update({
        "stock_rows": stock_rows,
        "stock_total": stock_total,
        "stock_available": stock_available,
        "stock_reserved": stock_reserved,
        "processing_reserved": processing_reserved,
        "shipping_reserved": shipping_reserved,
        "other_reserved": other_reserved,
        "low_stock_count": low_stock_count,
        "stock_box_count": int(packaging.get("boxes") or 0),
        "stock_pallet_count": int(packaging.get("pallets") or 0),
        "sku_items": sku_items,
        "sku_total": (
            0
            if fast_shell
            else (SKU.objects.filter(agency=selected_client, deleted=False).count() if selected_client else 0)
        ),
        "marketplace_count": marketplace_count,
        "marketplace_home": marketplace_home,
        "marking_stats": marking_stats,
        "finance_documents": finance_summary.get("documents") or [],
        "finance_documents_json": json.dumps(
            finance_summary.get("documents") or [],
            ensure_ascii=False,
            default=str,
        ),
        "finance_document_counts": finance_summary.get("document_counts") or {},
        "finance_recent_documents": finance_summary.get("recent_documents") or [],
        "finance_month_title": finance_month_title,
        "unpaid_invoices_count": unpaid_invoices_count,
        "billing": finance_summary.get("billing"),
        "billing_json": json.dumps(
            finance_summary.get("billing"),
            ensure_ascii=False,
            default=str,
        ),
        "billing_available": bool(finance_summary.get("billing_available")),
        "billing_months": finance_summary.get("months") or [],
        "billing_recent_services": finance_summary.get("recent_services") or [],
        "brand_options": sku_context.get("brand_options", []),
        "market_options": sku_context.get("market_options", []),
        "size_options": sku_context.get("size_options", []),
        "orders_list": orders_list,
        "orders_active_count": orders_active_count,
        "home_active_requests": home_active_requests,
        "active_requests_by_type": active_by_type,
        "live_dashboard": live_dashboard,
        "live_history": live_history,
        "wms_live_updated_at": (live_dashboard or {}).get("updated_at_label") or now_local.strftime("%H:%M"),
        "supply_wb_label": live_dashboard.get("supply_wb_label") or "Нет слота на сегодня",
        "supply_ozon_label": live_dashboard.get("supply_ozon_label") or "Нет слота на сегодня",
        "supply_wb_tone": ((live_dashboard or {}).get("marketplace_deliveries") or {}).get("wildberries", {}).get("tone") or "warn",
        "supply_ozon_tone": ((live_dashboard or {}).get("marketplace_deliveries") or {}).get("ozon", {}).get("tone") or "warn",
        "action_urls": build_action_urls(selected_client) if selected_client else {
            "receiving_new": "#",
            "shipping_new": "#",
            "processing_new": "#",
            "other_new": "#/other-requests",
            "other_journal": "/orders/other/",
            "sku_new": "#",
            "sku_list": "#",
            "sku_template": "/orders/templates/sku-upload/",
            "sku_template_upload": "/client/api/v1/nomenclature/upload/",
            "client_edit": "#",
            "market_sync": "/market-sync/",
            "legacy_dashboard": "/client/dashboard/lk/",
            "marking": "/client/marking/",
            "dashboard": "/client/dashboard/lk/",
            "orders_journal": "/orders/",
            "shipping_journal": "/shipping/",
            "stock_journal": "/sklad/journal/",
        },
        "portal_allowed_sections": portal_allowed_sections,
        "client_fbs_enabled": is_client_fbs_enabled(selected_client),
        "lk_fast_shell": fast_shell,
    })
    context.update(_staff_client_preview_context(request, selected_client, client_view))
    return render(request, "client_cabinet/dashboard_lk_react.html", context)


def marking_tools(request):
    if not request.user.is_authenticated:
        return HttpResponseForbidden("Доступ запрещен")
    selected_client, _client_view, allowed = _get_client_for_request(request)
    if not allowed:
        return HttpResponseForbidden("Доступ запрещен")
    if not selected_client:
        return redirect("/client/")
    context = build_marking_tools_context(selected_client=selected_client)
    return render(
        request,
        "client_cabinet/marking_tools.html",
        context,
    )


def marking_import(request):
    if request.method != "POST":
        return HttpResponseForbidden("Доступ запрещен")
    if not request.user.is_authenticated:
        return HttpResponseForbidden("Доступ запрещен")
    selected_client, _client_view, allowed = _get_client_for_request(request)
    if not allowed or not selected_client:
        return HttpResponseForbidden("Доступ запрещен")
    return import_marking_codes_response(request=request, selected_client=selected_client)


def receiving_redirect(request, pk: int):
    if not request.user.is_authenticated:
        return HttpResponseForbidden("Доступ запрещен")
    return resolve_client_order_redirect_response(request=request, pk=pk, destination="receiving")


def packing_redirect(request, pk: int):
    if not request.user.is_authenticated:
        return HttpResponseForbidden("Доступ запрещен")
    return resolve_client_order_redirect_response(request=request, pk=pk, destination="packing")


def shipping_redirect(request, pk: int):
    if not request.user.is_authenticated:
        return HttpResponseForbidden("Доступ запрещен")
    return resolve_client_order_redirect_response(request=request, pk=pk, destination="shipping")


class ClientListView(ListView):
    model = Agency
    paginate_by = 20
    template_name = "client_cabinet/clients_list.html"
    context_object_name = "items"
    view_modes = ("table", "cards")
    sort_fields = {
        "name": "agn_name",
        "short_name": "short_name",
        "pref": "pref",
        "inn": "inn",
        "email": "email",
        "phone": "phone",
        "use_nds": "use_nds",
        "sign_oferta": "sign_oferta",
        "id": "id",
    }
    filter_fields = {
        "agn_name": "agn_name",
        "short_name": "short_name",
        "inn": "inn",
        "pref": "pref",
        "email": "email",
        "phone": "phone",
    }
    default_sort = "name"

    def get_queryset(self):
        return build_client_list_queryset(
            request=self.request,
            base_queryset=super().get_queryset(),
            sort_fields=self.sort_fields,
            filter_fields=self.filter_fields,
            default_sort=self.default_sort,
        )

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx.update(
            build_client_list_context(
                request=self.request,
                items=ctx.get("items"),
                view_modes=self.view_modes,
                sort_fields=self.sort_fields,
                default_sort=self.default_sort,
            )
        )
        ctx["can_edit_client_profile"] = _staff_can_edit_client_profile(self.request)
        ctx["can_export_client_logins"] = _staff_can_manage_client_access(self.request)
        return ctx

    def dispatch(self, request, *args, **kwargs):
        if not _staff_allowed(request):
            return HttpResponseForbidden("Доступ запрещен")
        return super().dispatch(request, *args, **kwargs)


def export_client_logins(request):
    if not _staff_can_manage_client_access(request):
        return HttpResponseForbidden("Доступ запрещен")
    queryset = build_client_list_queryset(
        request=request,
        base_queryset=Agency.objects.select_related("portal_user"),
        sort_fields=ClientListView.sort_fields,
        filter_fields=ClientListView.filter_fields,
        default_sort=ClientListView.default_sort,
    )

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Клиенты"
    sheet.append(
        [
            "ID",
            "Название организации",
            "Краткое наименование",
            "ИНН",
            "ФИО контактного",
            "Телефон",
            "Email",
            "Логин",
        ]
    )
    for agency in queryset:
        sheet.append(
            [
                agency.id,
                agency.agn_name or "",
                agency.short_name or "",
                agency.inn or "",
                agency.fio_agn or "",
                agency.phone or "",
                agency.email or "",
                agency.portal_user.username if agency.portal_user else "",
            ]
        )

    output = BytesIO()
    workbook.save(output)
    output.seek(0)
    response = HttpResponse(
        output.getvalue(),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    response["Content-Disposition"] = (
        f'attachment; filename="client_logins_{timezone.localdate().isoformat()}.xlsx"'
    )
    return response


class AgencyFormMixin:
    model = Agency
    form_class = AgencyForm
    template_name = "client_cabinet/clients_form.html"
    success_url = "/client/"

    def get_form(self, form_class=None):
        form = super().get_form(form_class)
        if not _can_manage_client_access(self.request, getattr(form, "instance", None)):
            form.fields.pop("portal_login", None)
            form.fields.pop("portal_password", None)
        if not _staff_allowed(self.request) and "archived" in form.fields:
            del form.fields["archived"]
        # Менеджер не меняет юр.реквизиты после создания — зона бухгалтера
        role = get_request_role(self.request)
        is_edit = bool(getattr(form.instance, "pk", None))
        if role == "manager" and is_edit:
            locked = {
                "agn_name",
                "short_name",
                "inn",
                "kpp",
                "ogrn",
                "adres",
                "fakt_adres",
                "use_nds",
                "contract_numb",
                "contract_link",
                "sign_oferta",
                "pref",
            }
            for name in locked:
                field = form.fields.get(name)
                if field:
                    field.disabled = True
        return form

    def get_success_url(self):
        from client_cabinet.services import _safe_staff_next_url

        if _staff_allowed(self.request):
            return _safe_staff_next_url(self.request, default="/client/")
        return build_client_cabinet_url(getattr(getattr(self, "object", None), "id", None))

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        agency = getattr(self, "object", None)
        ctx.update(
            build_agency_form_context(
                request=self.request,
                mode=getattr(self, "mode", "edit"),
                title=getattr(self, "title", "Клиент"),
                submit_label=getattr(self, "submit_label", "Сохранить"),
                agency=agency,
            )
        )
        can_edit_tokens = _can_manage_client_access(self.request, agency)
        marketplace_settings = getattr(self, "_marketplace_settings", None)
        if marketplace_settings is None and can_edit_tokens:
            marketplace_settings = _build_marketplace_settings(
                agency=agency,
                post_data=self.request.POST if self.request.method == "POST" else None,
            )
        ozon_fbs_settings = getattr(self, "_ozon_fbs_settings", None)
        if ozon_fbs_settings is None and can_edit_tokens:
            ozon_fbs_settings = _build_ozon_fbs_settings(
                agency=agency,
                post_data=self.request.POST if self.request.method == "POST" else None,
            )
        ctx["marketplace_settings"] = marketplace_settings or []
        ctx["ozon_fbs_settings"] = ozon_fbs_settings or []
        ctx["show_marketplace_settings"] = bool(
            ctx["marketplace_settings"] or ctx["ozon_fbs_settings"]
        )
        ctx["can_manage_portal_access"] = _can_manage_client_access(
            self.request, agency
        )
        return ctx

    def _validate_marketplace_settings(self) -> bool:
        agency = getattr(self, "object", None)
        can_edit_tokens = _can_manage_client_access(self.request, agency)
        if not can_edit_tokens:
            self._marketplace_settings = []
            self._ozon_fbs_settings = []
            return True
        self._marketplace_settings = _build_marketplace_settings(
            agency=agency,
            post_data=self.request.POST,
        )
        is_valid = True
        for item in self._marketplace_settings:
            if item["market_missing"]:
                continue
            if not item["form"].is_valid():
                is_valid = False
        self._ozon_fbs_settings = _build_ozon_fbs_settings(
            agency=agency,
            post_data=self.request.POST,
        )
        seen_client_ids = {}
        for item in self._ozon_fbs_settings:
            form = item["form"]
            if not form.is_valid():
                is_valid = False
                continue
            client_id = str(form.cleaned_data.get("client_id") or "").strip()
            existing_client_id = str(item.get("existing_client_id") or "").strip()
            if (
                existing_client_id
                and client_id != existing_client_id
                and FbsIntegrationProfile.objects.filter(
                    agency=agency,
                    marketplace=FbsIntegrationProfile.MARKETPLACE_OZON,
                    external_account_id=existing_client_id,
                    is_active=True,
                ).exists()
            ):
                form.add_error(
                    "client_id",
                    "Сначала отключите активные FBS-склады этого кабинета.",
                )
                is_valid = False
                continue
            if not client_id:
                continue
            previous = seen_client_ids.get(client_id)
            if previous is not None:
                form.add_error(
                    "client_id",
                    f"Этот Client ID уже указан в кабинете {previous}.",
                )
                is_valid = False
            else:
                seen_client_ids[client_id] = item["slot"]
        return is_valid


class ClientCreateView(AgencyFormMixin, CreateView):
    mode = "create"
    title = "Создание клиента"
    submit_label = "Создать"

    def dispatch(self, request, *args, **kwargs):
        if not _staff_can_edit_client_profile(request):
            return HttpResponseForbidden("Доступ запрещен")
        return super().dispatch(request, *args, **kwargs)

    def form_valid(self, form):
        if not self._validate_marketplace_settings():
            return self.form_invalid(form)
        response = super().form_valid(form)
        if _can_manage_client_access(self.request, self.object):
            _save_client_portal_user(
                agency=self.object,
                raw_login=form.cleaned_data.get("portal_login"),
                raw_password=form.cleaned_data.get("portal_password"),
            )
        _save_marketplace_settings(agency=self.object, settings=getattr(self, "_marketplace_settings", []))
        _save_ozon_fbs_settings(
            agency=self.object,
            settings=getattr(self, "_ozon_fbs_settings", []),
        )
        log_agency_change(
            "create",
            self.object,
            user=self.request.user if self.request.user.is_authenticated else None,
            description=f"Создан клиент: {self.object.agn_name or self.object.inn or self.object.id}",
            snapshot=agency_snapshot(self.object),
        )
        # Менеджер создаёт черновик: он виден в справочнике клиентов,
        # но закрыт для заявок/биллинга до активации бухгалтером.
        try:
            from accountant.models import ClientLifecycle
            from accountant.selectors import ensure_lifecycle
            from employees.access import get_request_role

            role = get_request_role(self.request)
            status = (
                ClientLifecycle.STATUS_DRAFT
                if role == "manager"
                else ClientLifecycle.STATUS_DRAFT
            )
            # Бухгалтер тоже создаёт как draft, затем активирует осознанно
            if role in {"accountant", "admin", "director", "head_manager"}:
                status = ClientLifecycle.STATUS_DRAFT
            ensure_lifecycle(self.object, status=status, user=self.request.user)
            if role == "manager":
                from django.contrib import messages

                messages.info(
                    self.request,
                    "Черновик клиента создан. Он уже виден в разделе клиентов, а в заявках и биллинге появится после активации бухгалтером.",
                )
        except Exception:
            pass
        return response


class ClientUpdateView(AgencyFormMixin, UpdateView):
    mode = "edit"
    title = "Редактирование клиента"
    submit_label = "Сохранить"

    def dispatch(self, request, *args, **kwargs):
        agency = Agency.objects.filter(pk=kwargs.get("pk")).first()
        if not agency:
            return redirect("/client/")
        if not _can_edit_client_profile(request, agency):
            return HttpResponseForbidden("Доступ запрещен")
        return super().dispatch(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        self.object = self.get_object()
        action = str(request.POST.get("portal_action") or "").strip()
        if action in {"add_employee", "deactivate_employee", "activate_employee", "delete_employee"}:
            return self._handle_portal_employee_action(action)
        return super().post(request, *args, **kwargs)

    def _employees_redirect(self):
        return redirect(f"{self.request.path}?tab=employees")

    def _handle_portal_employee_action(self, action: str):
        from django.contrib import messages

        from .models import AgencyPortalMember
        from .portal_members import create_portal_employee, delete_member, set_member_active

        agency = self.object
        if not _can_manage_client_access(self.request, agency):
            return HttpResponseForbidden("Доступ запрещен")
        try:
            if action == "add_employee":
                custom_sections = self.request.POST.getlist("portal_sections")
                member, temp_password = create_portal_employee(
                    agency=agency,
                    last_name=self.request.POST.get("portal_last_name", ""),
                    first_name=self.request.POST.get("portal_first_name", ""),
                    email=self.request.POST.get("portal_email", ""),
                    position=self.request.POST.get("portal_position", ""),
                    role=self.request.POST.get("portal_role", ""),
                    custom_sections=custom_sections,
                    created_by=self.request.user,
                )
                login_name = member.user.username if member.user_id else member.email
                messages.success(
                    self.request,
                    (
                        f"Сотрудник {member.full_name} добавлен. "
                        f"Логин: {login_name}. Временный пароль: {temp_password}"
                    ),
                    extra_tags="portal-credentials",
                )
            elif action == "deactivate_employee":
                member_id = int(self.request.POST.get("member_id") or 0)
                set_member_active(agency=agency, member_id=member_id, is_active=False)
                messages.success(self.request, "Сотрудник деактивирован.")
            elif action == "activate_employee":
                member_id = int(self.request.POST.get("member_id") or 0)
                set_member_active(agency=agency, member_id=member_id, is_active=True)
                messages.success(self.request, "Сотрудник активирован.")
            elif action == "delete_employee":
                member_id = int(self.request.POST.get("member_id") or 0)
                delete_member(agency=agency, member_id=member_id)
                messages.success(self.request, "Сотрудник удалён.")
        except ValueError as exc:
            messages.error(self.request, str(exc))
        except AgencyPortalMember.DoesNotExist:
            messages.error(self.request, "Сотрудник не найден.")
        return self._employees_redirect()

    def get_context_data(self, **kwargs):
        from .portal_members import build_employees_context

        ctx = super().get_context_data(**kwargs)
        can_manage_portal_access = _can_manage_client_access(self.request, self.object)
        if not _staff_allowed(self.request):
            ctx["title"] = "Профиль компании"
            ctx["card_title"] = "Карточка компании"
        tab = str(self.request.GET.get("tab") or "company").strip().lower()
        if tab not in {"company", "employees", "tariffs"}:
            tab = "company"
        if tab == "employees" and not can_manage_portal_access:
            tab = "company"
        ctx["profile_tab"] = tab
        if can_manage_portal_access:
            ctx.update(build_employees_context(agency=self.object))
        if tab == "tariffs":
            from billing.tariff_services import build_profile_tariff_context

            ctx.update(build_profile_tariff_context(self.object))
        return ctx

    def form_valid(self, form):
        if not self._validate_marketplace_settings():
            return self.form_invalid(form)
        old_snapshot = agency_snapshot(self.get_object())
        response = super().form_valid(form)
        if _can_manage_client_access(self.request, self.object):
            _save_client_portal_user(
                agency=self.object,
                raw_login=form.cleaned_data.get("portal_login"),
                raw_password=form.cleaned_data.get("portal_password"),
            )
        _save_marketplace_settings(agency=self.object, settings=getattr(self, "_marketplace_settings", []))
        _save_ozon_fbs_settings(
            agency=self.object,
            settings=getattr(self, "_ozon_fbs_settings", []),
        )
        new_snapshot = agency_snapshot(self.object)
        description = _describe_agency_changes(old_snapshot, new_snapshot)
        log_agency_change(
            "update",
            self.object,
            user=self.request.user if self.request.user.is_authenticated else None,
            description=description,
            snapshot=new_snapshot,
        )
        return response

def archive_toggle(request, pk: int):
    return toggle_agency_archive_response(request=request, pk=pk)


def fetch_by_inn(request):
    return fetch_party_by_inn_response(request=request)


class ClientSKUListView(ListView):
    model = SKU
    paginate_by = 20
    template_name = "client_cabinet/client_sku_list.html"
    context_object_name = "items"
    view_modes = ("table", "cards")

    def dispatch(self, request, *args, **kwargs):
        self.agency = Agency.objects.filter(pk=self.kwargs.get("pk")).first()
        if not self.agency:
            return redirect("/client/")
        if not _check_agency_access(request, self.agency):
            return HttpResponseForbidden("Доступ запрещен")
        return super().dispatch(request, *args, **kwargs)

    def get_queryset(self):
        return build_client_sku_list_queryset(request=self.request, agency=self.agency)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx.update(
            build_client_sku_list_context(
                request=self.request,
                agency=self.agency,
                items=ctx.get("items"),
                view_modes=self.view_modes,
            )
        )
        return ctx


class ClientSKUFormMixin:
    template_name = "client_cabinet/client_sku_form.html"

    def get_form(self, form_class=None):
        form = super().get_form(form_class)
        form.instance.agency = self.agency
        agency_field = form.fields.get("agency")
        if agency_field is not None:
            agency_field.queryset = Agency.objects.filter(pk=self.agency.pk)
            agency_field.initial = self.agency
            agency_field.disabled = True
        return form

    def form_valid(self, form):
        # The URL/access check defines the client. Never trust the hidden POST
        # field: a portal user must not move or clone an SKU into another client.
        form.instance.agency = self.agency
        return super().form_valid(form)

    @staticmethod
    def _sku_history_actor(user, *, missing_label="Не зафиксирован"):
        if user is None:
            return missing_label
        full_name = str(user.get_full_name() or "").strip()
        return full_name or str(user.get_username() or "").strip() or f"Пользователь #{user.pk}"

    def get_barcode_formset(self, *args, **kwargs):
        formset = super().get_barcode_formset(*args, **kwargs)
        for barcode_form in formset.forms:
            if not barcode_form.instance.pk:
                continue
            delete_field = barcode_form.fields.get("DELETE")
            if delete_field is not None:
                delete_field.disabled = True
        return formset

    def get_success_url(self):
        client_id = self.kwargs.get("pk")
        return f"/client/{client_id}/sku/"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx.update(build_client_sku_form_context(agency=getattr(self, "agency", None)))
        sku_object = getattr(self, "object", None)
        if getattr(sku_object, "pk", None):
            history_entries = list(
                sku_object.audit_entries.select_related("user")
                .order_by("-created_at", "-id")[:100]
            )
            creation_entry = next(
                (
                    entry
                    for entry in reversed(history_entries)
                    if entry.action in {"create", "clone"}
                ),
                None,
            )
            latest_entry = history_entries[0] if history_entries else None
            latest_entry_at = latest_entry.created_at if latest_entry else None
            has_unlogged_update = bool(
                sku_object.updated_at
                and (
                    latest_entry_at is None
                    or sku_object.updated_at > latest_entry_at + timedelta(seconds=1)
                )
            )
            ctx.update(
                {
                    "sku_created_at": sku_object.created_at,
                    "sku_created_by": self._sku_history_actor(
                        creation_entry.user if creation_entry else None
                    ),
                    "sku_updated_at": sku_object.updated_at,
                    "sku_updated_by": (
                        "Системное изменение, детали не зафиксированы"
                        if has_unlogged_update
                        else self._sku_history_actor(latest_entry.user if latest_entry else None)
                    ),
                    "sku_has_unlogged_update": has_unlogged_update,
                    "sku_history_entries": [
                        {
                            "created_at": entry.created_at,
                            "action": entry.get_action_display(),
                            "actor": self._sku_history_actor(
                                entry.user,
                                missing_label="Системное изменение",
                            ),
                            "description": entry.description,
                            "changes": sku_audit_change_rows(entry.snapshot),
                            "details_missing": bool(
                                entry.action == "update"
                                and not sku_audit_change_rows(entry.snapshot)
                            ),
                        }
                        for entry in history_entries
                    ],
                }
            )
        return ctx


class ClientSKUCreateView(ClientSKUFormMixin, SKUCreateView):
    def dispatch(self, request, *args, **kwargs):
        self.agency = Agency.objects.filter(pk=self.kwargs.get("pk")).first()
        if not self.agency:
            return redirect("/client/")
        if not _check_agency_access(request, self.agency):
            return HttpResponseForbidden("Доступ запрещен")
        return super().dispatch(request, *args, **kwargs)

    def get_initial(self):
        initial = super().get_initial()
        initial["agency"] = self.agency.id
        return initial

    def form_valid(self, form):
        form.instance.agency = self.agency
        return super().form_valid(form)


class ClientSKUUpdateView(ClientSKUFormMixin, SKUUpdateView):
    pk_url_kwarg = "sku_id"

    def dispatch(self, request, *args, **kwargs):
        self.agency = Agency.objects.filter(pk=self.kwargs.get("pk")).first()
        if not self.agency:
            return redirect("/client/")
        if not _check_agency_access(request, self.agency):
            return HttpResponseForbidden("Доступ запрещен")
        return super().dispatch(request, *args, **kwargs)

    def get_queryset(self):
        qs = super().get_queryset()
        return qs.filter(agency=self.agency)


class ClientSKUDuplicateView(ClientSKUFormMixin, SKUDuplicateView):
    pk_url_kwarg = "sku_id"

    def dispatch(self, request, *args, **kwargs):
        self.agency = Agency.objects.filter(pk=self.kwargs.get("pk")).first()
        if not self.agency:
            return redirect("/client/")
        if not _check_agency_access(request, self.agency):
            return HttpResponseForbidden("Доступ запрещен")
        return super().dispatch(request, *args, **kwargs)

    def get_initial(self):
        return build_client_sku_duplicate_initial(
            agency=self.agency,
            sku_id=self.kwargs.get("sku_id"),
        )

    def get_queryset(self):
        qs = super().get_queryset()
        return qs.filter(agency=self.agency)


class ClientOrderFormView(TemplateView):
    template_name = "client_cabinet/client_order_form.html"

    def dispatch(self, request, *args, **kwargs):
        self.agency = Agency.objects.filter(pk=self.kwargs.get("pk")).first()
        if not self.agency:
            return redirect("/client/")
        if not _check_agency_access(request, self.agency):
            return HttpResponseForbidden("Доступ запрещен")
        return super().dispatch(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        # Пока без сохранения: имитация отправки.
        return self.get(request, submitted=True)

    def get(self, request, *args, **kwargs):
        submitted = kwargs.get("submitted") or request.GET.get("ok") == "1"
        ctx = self.get_context_data(submitted=submitted)
        return self.render_to_response(ctx)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["agency"] = self.agency
        ctx["submitted"] = kwargs.get("submitted", False)
        ctx["client_view"] = True
        return ctx


class ClientPackingCreateView(TemplateView):
    template_name = "client_cabinet/client_packing_form.html"

    def dispatch(self, request, *args, **kwargs):
        self.agency = Agency.objects.filter(pk=self.kwargs.get("pk")).first()
        if not self.agency:
            return redirect("/client/")
        if not _check_agency_access(request, self.agency):
            return HttpResponseForbidden("Доступ запрещен")
        return super().dispatch(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        submit_client_packing_order(request=request, agency=self.agency)
        return self.get(request, submitted=True)

    def get(self, request, *args, **kwargs):
        submitted = kwargs.get("submitted") or request.GET.get("ok") == "1"
        ctx = self.get_context_data(submitted=submitted)
        return self.render_to_response(ctx)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx.update(
            build_client_packing_form_context(
                agency=self.agency,
                submitted=kwargs.get("submitted", False),
            )
        )
        return ctx


class ClientReceivingCreateView(TemplateView):
    template_name = "client_cabinet/client_receiving_form.html"

    def dispatch(self, request, *args, **kwargs):
        self.agency = Agency.objects.filter(pk=self.kwargs.get("pk")).first()
        if not self.agency:
            return redirect("/client/")
        if not _check_agency_access(request, self.agency):
            return HttpResponseForbidden("Доступ запрещен")
        return super().dispatch(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        result = submit_client_receiving_order(request=request, agency=self.agency)
        if result["redirect_to_dashboard"]:
            return redirect(build_client_cabinet_url(self.agency.id))
        return self.get(request, submitted=True)

    def get(self, request, *args, **kwargs):
        submitted = kwargs.get("submitted") or request.GET.get("ok") == "1"
        ctx = self.get_context_data(submitted=submitted)
        return self.render_to_response(ctx)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx.update(
            build_client_receiving_form_context(
                agency=self.agency,
                submitted=kwargs.get("submitted", False),
            )
        )
        return ctx
