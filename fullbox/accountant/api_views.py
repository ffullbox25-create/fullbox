from __future__ import annotations

import json
import re
import uuid

from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection, transaction
from django.db.models import Max
from django.db.models.functions import Trim, Upper
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from billing.models import BillingService, ClientTariffItem, ClientTariffVersion, TariffCategory, TariffUnit
from billing.serializers import client_tariff_version_dict
from billing.tariff_services import archive_tariff_version, copy_tariff_version, tariff_history
from employees.access import get_request_effective_role
from head_manager.models import OwnCompany
from sku.models import Agency

from .inn_lookup import lookup_bank_by_bik, lookup_party_by_inn
from .models import ClientChangeLog, ClientLifecycle
from .selectors import ACCOUNTANT_ROLES, accountant_clients_queryset, ensure_lifecycle
from .services import (
    activate_client,
    apply_standard_prices_to_tariff,
    create_logistics_tariff_from_price_list,
    create_tariff_from_catalog,
    normalize_client_prefix,
    publish_tariff,
    update_lifecycle_fields,
)


def _ok(data=None, status=200):
    return JsonResponse({"ok": True, "data": data}, status=status)


def _error(exc, status=400):
    if isinstance(exc, ValidationError):
        if hasattr(exc, "message_dict"):
            return JsonResponse({"ok": False, "error": exc.message_dict}, status=status)
        messages = getattr(exc, "messages", None) or [str(exc)]
        return JsonResponse({"ok": False, "error": "; ".join(str(m) for m in messages)}, status=status)
    return JsonResponse({"ok": False, "error": str(exc)}, status=status)


def _guard_accountant(request):
    role = get_request_effective_role(
        request,
        preferred_roles=("admin", "director", "accountant", "head_manager"),
    )
    if request.user.is_superuser:
        return
    if role not in ACCOUNTANT_ROLES:
        raise PermissionDenied("Доступ только для бухгалтера")


def _json_body(request):
    if not request.body:
        return {}
    return json.loads(request.body.decode("utf-8"))


def _normalize_client_inn(value) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    if any(ch for ch in raw if not (ch.isdigit() or ch.isspace() or ch in "-./")):
        raise ValidationError("ИНН должен содержать только цифры.")
    inn = "".join(ch for ch in raw if ch.isdigit())
    if len(inn) not in (10, 12):
        raise ValidationError("ИНН должен быть 10 или 12 цифр.")
    return inn


def _normalize_bank_bik(value) -> str:
    bik = "".join(ch for ch in str(value or "") if ch.isdigit())
    if len(bik) != 9:
        raise ValidationError("БИК должен содержать 9 цифр.")
    return bik


def _ensure_unique_client_inn(inn: str, *, exclude_pk: int | None = None) -> None:
    if not inn:
        return
    qs = Agency.objects.filter(inn=inn)
    if exclude_pk:
        qs = qs.exclude(pk=exclude_pk)
    existing = qs.order_by("id").first()
    if existing:
        label = existing.short_name or existing.agn_name or f"клиент #{existing.id}"
        raise ValidationError(f"Клиент с ИНН {inn} уже есть в системе: {label} (ID {existing.id}).")


def _lock_client_prefix_uniqueness(prefix: str) -> None:
    """Serialize accountant writes for one prefix on PostgreSQL."""
    if not prefix or connection.vendor != "postgresql":
        return
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            [f"fullbox:agency-prefix:{prefix}"],
        )


def _ensure_unique_client_prefix(prefix: str, *, exclude_pk: int | None = None) -> None:
    if not prefix:
        return
    qs = Agency.objects.annotate(
        normalized_prefix=Upper(Trim("pref")),
    ).filter(normalized_prefix=prefix)
    if exclude_pk:
        qs = qs.exclude(pk=exclude_pk)
    existing = qs.order_by("id").first()
    if existing:
        label = existing.short_name or existing.agn_name or f"клиент #{existing.id}"
        raise ValidationError(
            f"Префикс {prefix} уже используется клиентом: {label} (ID {existing.id}). "
            "Укажите другой префикс."
        )


def _normalize_portal_login(value) -> str:
    login = str(value or "").strip()
    if not login:
        return ""
    if len(login) < 3:
        raise ValidationError("Логин клиента должен быть не короче 3 символов.")
    if not re.fullmatch(r"[A-Za-z0-9_.@+-]+", login):
        raise ValidationError("Логин клиента может содержать только латиницу, цифры и символы . _ @ + -")
    return login


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


def _save_client_portal_user(
    agency: Agency,
    *,
    raw_login=None,
    raw_password=None,
    login_present: bool = False,
    password_present: bool = False,
) -> None:
    if not login_present and not password_present:
        return
    login = _normalize_portal_login(raw_login)
    password = str(raw_password or "")
    portal_user = agency.portal_user
    if portal_user is None and not login and not password:
        return
    if portal_user is None and login and not password:
        raise ValidationError("Укажите пароль, чтобы создать доступ клиента.")
    if portal_user is not None and not login_present:
        resolved_login = portal_user.username
    elif portal_user is not None:
        resolved_login = login or portal_user.username
    else:
        resolved_login = _build_client_username(agency_id=agency.id)
    User = get_user_model()
    qs = User.objects.filter(username=resolved_login)
    if portal_user is not None:
        qs = qs.exclude(pk=portal_user.pk)
    if qs.exists():
        raise ValidationError("Такой логин клиента уже занят.")
    if portal_user is None:
        portal_user = User.objects.create_user(
            username=resolved_login,
            password=password,
            email=agency.email or "",
        )
        agency.portal_user = portal_user
        agency.save(update_fields=["portal_user"])
        return
    update_fields = []
    if portal_user.username != resolved_login:
        portal_user.username = resolved_login
        update_fields.append("username")
    email = agency.email or ""
    if portal_user.email != email:
        portal_user.email = email
        update_fields.append("email")
    if password_present and password:
        portal_user.set_password(password)
        update_fields.append("password")
    if update_fields:
        portal_user.save(update_fields=update_fields)


def _unit_for_billing_service(service: BillingService) -> TariffUnit:
    raw = (service.unit or "шт").strip() or "шт"
    code = "".join(ch if ch.isalnum() else "-" for ch in raw.lower())[:64] or "sht"
    unit, _ = TariffUnit.objects.get_or_create(
        code=code,
        defaults={"name": raw, "short_name": raw[:64]},
    )
    return unit


def _category_for_billing_service(service: BillingService) -> TariffCategory:
    if service.category_id:
        return service.category
    category, _ = TariffCategory.objects.get_or_create(
        code="other",
        defaults={"name": "Прочее", "sort_order": 900},
    )
    return category


def _custom_tariff_unit(raw_value) -> TariffUnit:
    raw = str(raw_value or "шт").strip() or "шт"
    code = "".join(ch if ch.isalnum() else "-" for ch in raw.lower())[:64].strip("-") or "sht"
    unit, _ = TariffUnit.objects.get_or_create(
        code=code,
        defaults={"name": raw, "short_name": raw[:64], "is_active": True},
    )
    return unit


def _custom_tariff_category(raw_category_id=None) -> TariffCategory:
    if raw_category_id:
        category = TariffCategory.objects.filter(pk=raw_category_id, is_active=True).first()
        if category:
            return category
    category, _ = TariffCategory.objects.get_or_create(
        code="client_custom",
        defaults={"name": "Индивидуальные услуги клиента", "sort_order": 890, "is_active": True},
    )
    return category


def _custom_service_code(version: ClientTariffVersion) -> str:
    while True:
        code = f"client_{version.client_id}_{uuid.uuid4().hex[:12]}"
        if not BillingService.objects.filter(code=code).exists():
            return code


def _bool_from_row(value, *, default=True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in {"0", "false", "no", "нет", "off"}


def _agency_dict(agency: Agency) -> dict:
    lifecycle = ensure_lifecycle(agency)
    company = lifecycle.serving_company
    return {
        "id": agency.id,
        "legal_name": agency.agn_name or "",
        "short_name": agency.short_name or "",
        "pref": agency.pref or "",
        "inn": agency.inn or "",
        "kpp": agency.kpp or "",
        "ogrn": agency.ogrn or "",
        "legal_address": agency.adres or "",
        "actual_address": agency.fakt_adres or "",
        "postal_address": lifecycle.postal_address or "",
        "phone": agency.phone or "",
        "email": agency.email or "",
        "contact_person": agency.fio_agn or "",
        "bank_name": agency.bank_name or "",
        "bank_bik": agency.bank_bik or "",
        "account_number": agency.bank_itog_account or "",
        "correspondent_account": agency.bank_koresp_account or "",
        "contract_number": agency.contract_numb or "",
        "contract_link": agency.contract_link or "",
        "portal_login": agency.portal_user.username if agency.portal_user_id else "",
        "portal_has_user": bool(agency.portal_user_id),
        "portal_has_password": bool(agency.portal_user_id and agency.portal_user.has_usable_password()),
        "status": lifecycle.status,
        "status_label": lifecycle.get_status_display(),
        "client_type": lifecycle.client_type,
        "client_type_label": lifecycle.get_client_type_display() if lifecycle.client_type else "",
        "vat_type": lifecycle.vat_type,
        "vat_type_label": lifecycle.get_vat_type_display(),
        "email_documents": lifecycle.email_documents,
        "email_notifications": lifecycle.email_notifications,
        "accountant_comment": lifecycle.accountant_comment,
        "commercial_offer_number": lifecycle.commercial_offer_number,
        "contract_date": lifecycle.contract_date.isoformat() if lifecycle.contract_date else None,
        "serving_company_id": lifecycle.serving_company_id,
        "serving_company": (
            {
                "id": company.id,
                "name": company.short_name or company.name,
                "tax_mode": company.tax_mode,
                "vat_rate": company.vat_rate,
                "vat_label": "Без НДС" if company.tax_mode == "no_vat" else f"НДС {company.vat_rate}%",
            }
            if company
            else None
        ),
        "manager_visible": lifecycle.is_manager_visible,
        "archived": agency.archived,
    }


@login_required
@require_GET
def api_clients(request):
    try:
        _guard_accountant(request)
        qs = accountant_clients_queryset(status=request.GET.get("status") or None, q=request.GET.get("q") or "")
        return _ok([_agency_dict(a) for a in qs[:300]])
    except PermissionDenied as exc:
        return _error(exc, status=403)


@login_required
@require_GET
def api_client_lookup_inn(request):
    """Lookup a new client's requisites by INN for the accountant modal."""
    try:
        _guard_accountant(request)
        inn = _normalize_client_inn(request.GET.get("inn"))
        result = lookup_party_by_inn(inn)
        if not result:
            raise ValidationError("По этому ИНН организация не найдена.")
        return _ok(result)
    except PermissionDenied as exc:
        return _error(exc, status=403)
    except ValidationError as exc:
        return _error(exc, status=400)


@login_required
@require_GET
def api_client_lookup_bik(request):
    """Lookup bank requisites by BIK for the accountant client-create modal."""
    try:
        _guard_accountant(request)
        result = lookup_bank_by_bik(_normalize_bank_bik(request.GET.get("bik")))
        if not result:
            raise ValidationError("По этому БИК банк не найден.")
        return _ok(result)
    except PermissionDenied as exc:
        return _error(exc, status=403)
    except ValidationError as exc:
        return _error(exc, status=400)


@login_required
@require_http_methods(["POST"])
def api_clients_create(request):
    try:
        _guard_accountant(request)
        data = _json_body(request)
        legal_name = str(data.get("legal_name") or data.get("agn_name") or "").strip()
        inn = _normalize_client_inn(data.get("inn"))
        _ensure_unique_client_inn(inn)
        if not legal_name:
            raise ValidationError("Укажите юридическое название")
        prefix = normalize_client_prefix(data.get("pref") or data.get("prefix"))
        if not prefix:
            raise ValidationError("Укажите префикс клиента")
        with transaction.atomic():
            _lock_client_prefix_uniqueness(prefix)
            _ensure_unique_client_prefix(prefix)
            agency = Agency.objects.create(
                agn_name=legal_name,
                short_name=str(data.get("short_name") or "")[:128],
                pref=prefix,
                inn=inn,
                kpp=str(data.get("kpp") or ""),
                ogrn=str(data.get("ogrn") or ""),
                phone=str(data.get("phone") or ""),
                email=str(data.get("email") or ""),
                adres=str(data.get("legal_address") or ""),
                fakt_adres=str(data.get("actual_address") or ""),
                fio_agn=str(data.get("contact_person") or ""),
                bank_name=str(data.get("bank_name") or ""),
                bank_bik=str(data.get("bank_bik") or ""),
                bank_itog_account=str(data.get("account_number") or ""),
                bank_koresp_account=str(data.get("correspondent_account") or ""),
                contract_numb=str(data.get("contract_number") or ""),
                contract_link=str(data.get("contract_link") or ""),
            )
            lifecycle = ensure_lifecycle(agency, status=ClientLifecycle.STATUS_DRAFT, user=request.user)
            update_lifecycle_fields(
                agency,
                {
                    "client_type": data.get("client_type") or "",
                    "postal_address": data.get("postal_address") or "",
                    "email_documents": data.get("email_documents") or agency.email or "",
                    "email_notifications": data.get("email_notifications") or agency.email or "",
                    "accountant_comment": data.get("accountant_comment") or "",
                    "commercial_offer_number": data.get("commercial_offer_number") or "",
                    "contract_date": data.get("contract_date") or None,
                    "vat_type": data.get("vat_type") or ClientLifecycle.VAT_NO,
                    "serving_company_id": data.get("serving_company_id"),
                },
                user=request.user,
            )
            _save_client_portal_user(
                agency,
                raw_login=data.get("portal_login"),
                raw_password=data.get("portal_password"),
                login_present="portal_login" in data,
                password_present="portal_password" in data,
            )
            # Только новый клиент получает полный черновик из актуального прайса.
            # Старые клиенты и их согласованные тарифы не изменяются.
            initial_tariff = create_tariff_from_catalog(
                client=agency,
                items_active=False,
                user=request.user,
            )
            initial_logistics_tariff = create_logistics_tariff_from_price_list(
                client=agency,
                items_active=False,
                user=request.user,
            )
        response_data = _agency_dict(agency)
        response_data["initial_tariff_id"] = initial_tariff.id
        response_data["initial_tariff_status"] = initial_tariff.status
        response_data["initial_logistics_tariff_id"] = initial_logistics_tariff.id
        response_data["initial_logistics_tariff_status"] = initial_logistics_tariff.status
        return _ok(response_data, status=201)
    except (PermissionDenied, ValidationError, json.JSONDecodeError) as exc:
        status = 403 if isinstance(exc, PermissionDenied) else 400
        return _error(exc, status=status)


@login_required
@require_GET
def api_client_detail(request, pk: int):
    try:
        _guard_accountant(request)
        agency = get_object_or_404(Agency, pk=pk)
        return _ok(_agency_dict(agency))
    except PermissionDenied as exc:
        return _error(exc, status=403)


@login_required
@require_http_methods(["PATCH", "POST"])
def api_client_patch(request, pk: int):
    try:
        _guard_accountant(request)
        agency = get_object_or_404(Agency, pk=pk)
        data = _json_body(request)
        agency_fields = {
            "legal_name": "agn_name",
            "short_name": "short_name",
            "pref": "pref",
            "inn": "inn",
            "kpp": "kpp",
            "ogrn": "ogrn",
            "legal_address": "adres",
            "actual_address": "fakt_adres",
            "phone": "phone",
            "email": "email",
            "contact_person": "fio_agn",
            "bank_name": "bank_name",
            "bank_bik": "bank_bik",
            "account_number": "bank_itog_account",
            "correspondent_account": "bank_koresp_account",
            "contract_number": "contract_numb",
            "contract_link": "contract_link",
        }
        with transaction.atomic():
            changed = []
            for src, dst in agency_fields.items():
                if src not in data:
                    continue
                old = getattr(agency, dst)
                if src == "inn":
                    new = _normalize_client_inn(data[src])
                elif src == "pref":
                    new = normalize_client_prefix(data[src])
                else:
                    new = data[src]
                if src == "inn":
                    _ensure_unique_client_inn(new, exclude_pk=agency.id)
                elif src == "pref":
                    _lock_client_prefix_uniqueness(new)
                    _ensure_unique_client_prefix(new, exclude_pk=agency.id)
                if str(old or "") != str(new or ""):
                    setattr(agency, dst, new or "")
                    changed.append(dst)
            if changed:
                agency.save(update_fields=changed)
            update_lifecycle_fields(agency, data, user=request.user)
            _save_client_portal_user(
                agency,
                raw_login=data.get("portal_login"),
                raw_password=data.get("portal_password"),
                login_present="portal_login" in data,
                password_present="portal_password" in data,
            )
        return _ok(_agency_dict(agency))
    except (PermissionDenied, ValidationError, json.JSONDecodeError) as exc:
        status = 403 if isinstance(exc, PermissionDenied) else 400
        return _error(exc, status=status)


@login_required
@require_POST
def api_client_activate(request, pk: int):
    try:
        _guard_accountant(request)
        agency = get_object_or_404(Agency, pk=pk)
        activate_client(agency, user=request.user)
        return _ok(_agency_dict(agency))
    except (PermissionDenied, ValidationError) as exc:
        status = 403 if isinstance(exc, PermissionDenied) else 400
        return _error(exc, status=status)


@login_required
@require_GET
def api_services(request):
    try:
        _guard_accountant(request)
        qs = BillingService.objects.filter(is_active=True).select_related("category").order_by("sort_order", "name")
        return _ok(
            [
                {
                    "id": s.id,
                    "code": s.code,
                    "name": s.name,
                    "unit": s.unit,
                    "default_price": str(s.default_price) if s.default_price is not None else None,
                    "category": s.category.name if s.category_id else "",
                    "category_code": s.category.code if s.category_id else "",
                    "description": s.description,
                    "used_in_billing": s.used_in_billing,
                    "sort_order": s.sort_order,
                }
                for s in qs
            ]
        )
    except PermissionDenied as exc:
        return _error(exc, status=403)


@login_required
@require_GET
def api_serving_companies(request):
    try:
        _guard_accountant(request)
        qs = OwnCompany.objects.filter(is_active=True).order_by("short_name", "name")
        return _ok(
            [
                {
                    "id": c.id,
                    "name": c.short_name or c.name,
                    "tax_mode": c.tax_mode,
                    "vat_rate": c.vat_rate,
                    "vat_label": "Без НДС" if c.tax_mode == "no_vat" else f"НДС {c.vat_rate}%",
                }
                for c in qs
            ]
        )
    except PermissionDenied as exc:
        return _error(exc, status=403)


@login_required
@require_GET
def api_client_tariffs(request):
    try:
        _guard_accountant(request)
        qs = ClientTariffVersion.objects.select_related("client", "contract").order_by("-valid_from", "-id")
        client_id = request.GET.get("client")
        if client_id:
            qs = qs.filter(client_id=client_id)
        status = request.GET.get("status")
        if status:
            qs = qs.filter(status=status)
        return _ok([client_tariff_version_dict(v) for v in qs[:200]])
    except PermissionDenied as exc:
        return _error(exc, status=403)


@login_required
@require_GET
def api_client_tariff_detail(request, pk: int):
    try:
        _guard_accountant(request)
        version = get_object_or_404(ClientTariffVersion.objects.select_related("client"), pk=pk)
        return _ok(client_tariff_version_dict(version, include_children=True))
    except PermissionDenied as exc:
        return _error(exc, status=403)


@login_required
@require_POST
def api_client_tariffs_create(request):
    try:
        _guard_accountant(request)
        data = _json_body(request)
        client = get_object_or_404(Agency, pk=data.get("client_id"))
        version = create_tariff_from_catalog(
            client=client,
            valid_from=data.get("valid_from") or None,
            name=str(data.get("name") or ""),
            commercial_offer_number=str(data.get("commercial_offer_number") or ""),
            user=request.user,
        )
        return _ok(client_tariff_version_dict(version, include_children=True), status=201)
    except (PermissionDenied, ValidationError, json.JSONDecodeError) as exc:
        status = 403 if isinstance(exc, PermissionDenied) else 400
        return _error(exc, status=status)


@login_required
@require_http_methods(["PATCH", "POST"])
@transaction.atomic
def api_client_tariff_patch(request, pk: int):
    try:
        _guard_accountant(request)
        version = get_object_or_404(ClientTariffVersion, pk=pk)
        data = _json_body(request)
        new_items = data.get("new_items") or []
        existing_item_edits = data.get("items") or []
        can_edit_existing = version.can_edit_directly()
        if not can_edit_existing:
            if version.status == ClientTariffVersion.STATUS_ARCHIVED:
                raise ValidationError("В архивный тариф нельзя добавлять услуги.")
            protected_fields = {
                "name", "general_comment", "contract_number", "additional_agreement_number", "valid_from", "valid_to"
            }
            if not new_items:
                raise ValidationError(
                    "Тариф уже используется. Существующие услуги и цены изменять нельзя; "
                    "можно только добавить новую услугу отдельной строкой."
                )
            if existing_item_edits or any(field in data for field in protected_fields):
                raise ValidationError(
                    "Тариф уже используется. Изменение существующих строк, дат и реквизитов невозможно; "
                    "добавьте только новую услугу отдельной строкой."
                )
        else:
            for field in ("name", "general_comment", "contract_number", "additional_agreement_number"):
                if field in data:
                    setattr(version, field, data[field] or "")
            if "valid_from" in data and data["valid_from"]:
                version.valid_from = data["valid_from"]
            if "valid_to" in data:
                version.valid_to = data["valid_to"] or None
            version.save()
        for row in new_items:
            service_name = str(row.get("service_name") or row.get("name") or "").strip()
            if not service_name:
                raise ValidationError("Укажите название новой услуги.")
            category = _custom_tariff_category(row.get("category_id"))
            unit = _custom_tariff_unit(row.get("unit") or row.get("unit_name"))
            max_sort = version.items.aggregate(max_sort=Max("sort_order")).get("max_sort") or 0
            service = BillingService.objects.create(
                code=_custom_service_code(version),
                name=service_name,
                unit=unit.short_name or unit.name or "шт",
                category=category,
                default_price=None,
                description=f"Индивидуальная услуга клиента #{version.client_id} в тарифе #{version.id}",
                sort_order=9000,
                used_in_billing=True,
                is_active=True,
            )
            ClientTariffItem.objects.create(
                tariff_version=version,
                category=category,
                service=service,
                service_name=service_name,
                description=str(row.get("description") or ""),
                unit=unit,
                price=row.get("client_price", row.get("price")) or "0",
                minimum_amount=row.get("minimum_amount") or None,
                conditions=str(row.get("comment") or ""),
                sort_order=max_sort + 10,
                is_active=_bool_from_row(row.get("is_active"), default=True),
            )
        # У использованного тарифа можно только добавить новую строку выше.
        # Ранее согласованные строки и их цены остаются финансовым фактом.
        items = existing_item_edits if can_edit_existing else []
        for row in items:
            item = ClientTariffItem.objects.filter(pk=row.get("id"), tariff_version=version).first()
            if not item:
                continue
            if row.get("delete") is True or row.get("_delete") is True:
                item.delete()
                continue
            if "service_id" in row:
                service = BillingService.objects.filter(pk=row.get("service_id"), is_active=True, used_in_billing=True).first()
                if not service:
                    raise ValidationError("Выбранная услуга не найдена или отключена.")
                item.service = service
                item.category = _category_for_billing_service(service)
                item.unit = _unit_for_billing_service(service)
                if "service_name" not in row:
                    item.service_name = service.name
            if "service_name" in row:
                item.service_name = str(row.get("service_name") or "").strip() or item.service.name
            if "client_price" in row or "price" in row:
                item.price = row.get("client_price", row.get("price"))
            if "minimum_amount" in row:
                item.minimum_amount = row.get("minimum_amount") or None
            if "is_active" in row:
                item.is_active = bool(row["is_active"])
            if "comment" in row:
                item.conditions = str(row.get("comment") or item.conditions)
            item.save()
        return _ok(client_tariff_version_dict(version, include_children=True))
    except (PermissionDenied, ValidationError, json.JSONDecodeError) as exc:
        status = 403 if isinstance(exc, PermissionDenied) else 400
        return _error(exc, status=status)


@login_required
@require_POST
def api_client_tariff_apply_price_list(request, pk: int):
    try:
        _guard_accountant(request)
        version = get_object_or_404(ClientTariffVersion, pk=pk)
        data = _json_body(request)
        overwrite = bool(data.get("overwrite", True))
        result = apply_standard_prices_to_tariff(version, overwrite=overwrite, user=request.user)
        return _ok({"result": result, "version": client_tariff_version_dict(version, include_children=True)})
    except (PermissionDenied, ValidationError, json.JSONDecodeError) as exc:
        status = 403 if isinstance(exc, PermissionDenied) else 400
        return _error(exc, status=status)


@login_required
@require_POST
def api_client_tariff_publish(request, pk: int):
    try:
        _guard_accountant(request)
        version = get_object_or_404(ClientTariffVersion, pk=pk)
        version = publish_tariff(version, user=request.user)
        lifecycle = ensure_lifecycle(version.client)
        if lifecycle.status in {
            ClientLifecycle.STATUS_DRAFT,
            ClientLifecycle.STATUS_ACCOUNTANT_REVIEW,
            ClientLifecycle.STATUS_REQUISITES_READY,
        }:
            lifecycle.status = ClientLifecycle.STATUS_TARIFFS_READY
            lifecycle.save(update_fields=["status", "updated_at"])
        return _ok(client_tariff_version_dict(version, include_children=True))
    except (PermissionDenied, ValidationError) as exc:
        status = 403 if isinstance(exc, PermissionDenied) else 400
        return _error(exc, status=status)


@login_required
@require_POST
def api_client_tariff_archive(request, pk: int):
    try:
        _guard_accountant(request)
        version = get_object_or_404(ClientTariffVersion, pk=pk)
        return _ok(client_tariff_version_dict(archive_tariff_version(version, user=request.user)))
    except (PermissionDenied, ValidationError) as exc:
        status = 403 if isinstance(exc, PermissionDenied) else 400
        return _error(exc, status=status)


@login_required
@require_POST
def api_client_tariff_duplicate(request, pk: int):
    try:
        _guard_accountant(request)
        version = get_object_or_404(ClientTariffVersion, pk=pk)
        data = _json_body(request) if request.body else {}
        copied = copy_tariff_version(version, valid_from=data.get("valid_from") or None, user=request.user)
        return _ok(client_tariff_version_dict(copied, include_children=True), status=201)
    except (PermissionDenied, ValidationError, json.JSONDecodeError) as exc:
        status = 403 if isinstance(exc, PermissionDenied) else 400
        return _error(exc, status=status)


@login_required
@require_GET
def api_client_tariff_history(request, pk: int):
    try:
        _guard_accountant(request)
        version = get_object_or_404(ClientTariffVersion, pk=pk)
        return _ok([client_tariff_version_dict(v) for v in tariff_history(version.client)[:50]])
    except PermissionDenied as exc:
        return _error(exc, status=403)


@login_required
@require_GET
def api_client_history(request, pk: int):
    try:
        _guard_accountant(request)
        agency = get_object_or_404(Agency, pk=pk)
        rows = ClientChangeLog.objects.filter(agency=agency).select_related("user").order_by("-changed_at")[:100]
        return _ok(
            [
                {
                    "id": r.id,
                    "field_name": r.field_name,
                    "old_value": r.old_value,
                    "new_value": r.new_value,
                    "comment": r.comment,
                    "changed_at": r.changed_at.isoformat(),
                    "user": r.user.get_username() if r.user_id else "",
                }
                for r in rows
            ]
        )
    except PermissionDenied as exc:
        return _error(exc, status=403)


@login_required
@require_GET
def api_storage_rule(request, pk: int):
    """Правило хранения для редакции тарифа."""
    try:
        _guard_accountant(request)
        from billing.models import StorageCalculationRule
        from billing.storage_serializers import rule_for_tariff_version, storage_rule_choices, storage_rule_dict

        version = get_object_or_404(ClientTariffVersion, pk=pk)
        rule = rule_for_tariff_version(version)
        return _ok({"rule": storage_rule_dict(rule), "choices": storage_rule_choices()})
    except PermissionDenied as exc:
        return _error(exc, status=403)


@login_required
@require_http_methods(["PATCH", "POST"])
def api_storage_rule_patch(request, pk: int):
    try:
        _guard_accountant(request)
        from billing.storage_serializers import apply_rule_patch, rule_for_tariff_version, storage_rule_dict

        version = get_object_or_404(ClientTariffVersion, pk=pk)
        if not version.can_edit_directly():
            raise ValidationError(version.edit_block_reason() or "Правило хранения нельзя менять в этой редакции тарифа.")
        data = _json_body(request)
        rule = apply_rule_patch(rule_for_tariff_version(version), data)
        return _ok(storage_rule_dict(rule))
    except (PermissionDenied, ValidationError, json.JSONDecodeError) as exc:
        status = 403 if isinstance(exc, PermissionDenied) else 400
        return _error(exc, status=status)


@login_required
@require_GET
def api_storage_days(request):
    try:
        _guard_accountant(request)
        from billing.models import BillingStorageDay
        from billing.storage_serializers import storage_day_dict

        client_id = request.GET.get("client_id")
        year = int(request.GET.get("year") or timezone_year())
        month = int(request.GET.get("month") or timezone_month())
        qs = BillingStorageDay.objects.select_related("client", "charge").order_by("-day")
        if client_id:
            qs = qs.filter(client_id=client_id)
        qs = qs.filter(day__year=year, day__month=month)
        return _ok([storage_day_dict(d) for d in qs[:500]])
    except PermissionDenied as exc:
        return _error(exc, status=403)
    except (TypeError, ValueError) as exc:
        return _error(exc)


def timezone_year():
    from django.utils import timezone

    return timezone.localdate().year


def timezone_month():
    from django.utils import timezone

    return timezone.localdate().month


@login_required
@require_GET
def api_storage_day_detail(request, pk: int):
    try:
        _guard_accountant(request)
        from billing.models import BillingStorageDay
        from billing.storage_serializers import storage_day_dict

        day = get_object_or_404(BillingStorageDay.objects.prefetch_related("lines"), pk=pk)
        return _ok(storage_day_dict(day, with_lines=True))
    except PermissionDenied as exc:
        return _error(exc, status=403)


@login_required
@require_GET
def api_storage_errors(request):
    try:
        _guard_accountant(request)
        from billing.models import StorageBillingError
        from billing.storage_serializers import storage_error_dict

        qs = StorageBillingError.objects.select_related("client").order_by("-created_at")
        if request.GET.get("open") == "1":
            qs = qs.filter(resolved_at__isnull=True)
        if request.GET.get("client_id"):
            qs = qs.filter(client_id=request.GET["client_id"])
        return _ok([storage_error_dict(e) for e in qs[:300]])
    except PermissionDenied as exc:
        return _error(exc, status=403)


@login_required
@require_POST
def api_storage_error_resolve(request, pk: int):
    try:
        _guard_accountant(request)
        from billing.models import StorageBillingError
        from billing.storage_control import resolve_error
        from billing.storage_serializers import storage_error_dict

        err = get_object_or_404(StorageBillingError, pk=pk)
        return _ok(storage_error_dict(resolve_error(err, user=request.user)))
    except PermissionDenied as exc:
        return _error(exc, status=403)


@login_required
@require_http_methods(["GET", "POST"])
def api_storage_periods(request):
    try:
        _guard_accountant(request)
        from billing.models import StorageBillingPeriod
        from billing.storage_control import close_storage_period, get_or_open_period, reopen_storage_period
        from billing.storage_serializers import storage_period_dict
        from sku.models import Agency

        if request.method == "GET":
            qs = StorageBillingPeriod.objects.select_related("client").order_by("-year", "-month")
            if request.GET.get("client_id"):
                qs = qs.filter(client_id=request.GET["client_id"])
            return _ok([storage_period_dict(p) for p in qs[:200]])

        data = _json_body(request)
        action = data.get("action") or "open"
        client = get_object_or_404(Agency, pk=data.get("client_id"))
        year = int(data.get("year"))
        month = int(data.get("month"))
        period = get_or_open_period(client, year=year, month=month)
        if action == "close":
            period = close_storage_period(period, user=request.user, checklist=data.get("checklist"))
        elif action == "reopen":
            period = reopen_storage_period(period, user=request.user)
        return _ok(storage_period_dict(period))
    except (PermissionDenied, ValidationError, json.JSONDecodeError, TypeError, ValueError) as exc:
        status = 403 if isinstance(exc, PermissionDenied) else 400
        return _error(exc, status=status)


@login_required
@require_http_methods(["GET", "POST"])
def api_storage_adjustments(request):
    try:
        _guard_accountant(request)
        from decimal import Decimal

        from billing.models import StorageAdjustment
        from billing.storage_control import approve_adjustment, create_adjustment
        from billing.storage_serializers import storage_adjustment_dict
        from sku.models import Agency

        if request.method == "GET":
            qs = StorageAdjustment.objects.select_related("client", "period").order_by("-created_at")
            if request.GET.get("client_id"):
                qs = qs.filter(client_id=request.GET["client_id"])
            return _ok([storage_adjustment_dict(a) for a in qs[:200]])

        data = _json_body(request)
        action = data.get("action") or "create"
        if action == "approve":
            adj = get_object_or_404(StorageAdjustment, pk=data.get("id"))
            return _ok(storage_adjustment_dict(approve_adjustment(adj, user=request.user)))
        client = get_object_or_404(Agency, pk=data.get("client_id"))
        adj = create_adjustment(
            client=client,
            year=int(data.get("year")),
            month=int(data.get("month")),
            delta_amount=Decimal(str(data.get("delta_amount") or "0")),
            reason=data.get("reason") or StorageAdjustment.REASON_OTHER,
            comment=data.get("comment") or "",
            delta_volume_l=Decimal(str(data.get("delta_volume_l") or "0")),
            user=request.user,
        )
        return _ok(storage_adjustment_dict(adj), status=201)
    except (PermissionDenied, ValidationError, json.JSONDecodeError, TypeError, ValueError) as exc:
        status = 403 if isinstance(exc, PermissionDenied) else 400
        return _error(exc, status=status)


@login_required
@require_GET
def api_storage_export_csv(request):
    try:
        _guard_accountant(request)
        from django.http import HttpResponse

        from billing.storage_control import export_storage_days_csv
        from sku.models import Agency

        client = get_object_or_404(Agency, pk=request.GET.get("client_id"))
        year = int(request.GET.get("year") or timezone_year())
        month = int(request.GET.get("month") or timezone_month())
        content = export_storage_days_csv(client, year=year, month=month)
        resp = HttpResponse(content, content_type="text/csv; charset=utf-8")
        resp["Content-Disposition"] = f'attachment; filename="storage_{client.id}_{year}-{month:02d}.csv"'
        return resp
    except PermissionDenied as exc:
        return _error(exc, status=403)
    except (TypeError, ValueError) as exc:
        return _error(exc)
