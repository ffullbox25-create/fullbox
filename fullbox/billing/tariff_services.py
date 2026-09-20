from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Max, Q
from django.http import HttpResponse
from django.template.loader import render_to_string
from django.utils import timezone

from employees.access import get_employee_for_user
from sku.models import Agency

from .models import (
    BillingAuditEvent,
    BillingService,
    ClientBillingContract,
    ClientTariffCondition,
    ClientTariffItem,
    ClientTariffVersion,
    ClientLogisticsTariff,
    TariffCategory,
    TariffUnit,
)


@dataclass(frozen=True)
class CompanyTariffResult:
    version: ClientTariffVersion
    item: ClientTariffItem
    price: Decimal
    unit: TariffUnit
    minimum_amount: Decimal | None
    coefficient: Decimal
    conditions: str


def _date_range_filter(on_date):
    return Q(valid_from__lte=on_date) & (Q(valid_to__isnull=True) | Q(valid_to__gte=on_date))


def active_tariff_version(client: Agency, on_date=None) -> ClientTariffVersion | None:
    on_date = on_date or timezone.localdate()
    return (
        ClientTariffVersion.objects.select_related("client", "contract", "manager", "manager__user")
        # Завершённая редакция остаётся источником цены для исторической даты
        # оказания услуги. Иначе после публикации новой редакции биллинг теряет
        # тариф за последний день предыдущего периода, хотя valid_to включителен.
        .filter(
            client=client,
            status__in=[
                ClientTariffVersion.STATUS_ACTIVE,
                ClientTariffVersion.STATUS_SCHEDULED,
                ClientTariffVersion.STATUS_EXPIRED,
            ],
        )
        .filter(_date_range_filter(on_date))
        .order_by("-valid_from", "-version_number", "-id")
        .first()
    )


def tariff_history(client: Agency):
    return (
        ClientTariffVersion.objects.select_related("contract", "manager")
        .filter(client=client)
        .order_by("-valid_from", "-version_number", "-id")
    )


def next_version_number(client: Agency) -> int:
    value = ClientTariffVersion.objects.filter(client=client).aggregate(value=Max("version_number")).get("value") or 0
    return int(value) + 1


def _manager_for_client(client: Agency):
    user_id = getattr(client, "mened_user_id", None)
    if not user_id:
        return None
    try:
        from django.contrib.auth import get_user_model

        user = get_user_model().objects.filter(pk=user_id).first()
    except Exception:
        user = None
    return get_employee_for_user(user) if user else None


def create_tariff_version(
    *,
    client: Agency,
    contract: ClientBillingContract | None = None,
    name: str = "",
    valid_from=None,
    valid_to=None,
    status: str = ClientTariffVersion.STATUS_DRAFT,
    user=None,
) -> ClientTariffVersion:
    valid_from = valid_from or timezone.localdate()
    version = ClientTariffVersion.objects.create(
        client=client,
        contract=contract,
        name=name or f"Тарифы с {valid_from:%d.%m.%Y}",
        version_number=next_version_number(client),
        status=status,
        valid_from=valid_from,
        valid_to=valid_to,
        vat_type=_vat_type_from_contract(contract),
        contract_number=getattr(client, "contract_numb", "") or "",
        manager=_manager_for_client(client),
        created_by=user if getattr(user, "is_authenticated", False) else None,
    )
    from .models import StorageCalculationRule

    StorageCalculationRule.objects.get_or_create(
        tariff_version=version,
        defaults={
            "billing_mode": StorageCalculationRule.MODE_PALLET_DAY,
            "volume_level": StorageCalculationRule.LEVEL_PALLET,
            "missing_dims_policy": StorageCalculationRule.MISSING_SKIP,
        },
    )
    _audit_tariff("tariff_version_created", version, user=user, new_value={"status": version.status})
    return version


def _vat_type_from_contract(contract: ClientBillingContract | None) -> str:
    own_company = contract.own_company if contract else None
    if own_company and getattr(own_company, "tax_mode", "") == "no_vat":
        return ClientTariffVersion.VAT_NO
    return ClientTariffVersion.VAT_EXTRA


@transaction.atomic
def copy_tariff_version(source: ClientTariffVersion, *, valid_from=None, user=None) -> ClientTariffVersion:
    target = create_tariff_version(
        client=source.client,
        contract=source.contract,
        name=f"{source.name} (новая редакция)",
        valid_from=valid_from or timezone.localdate(),
        status=ClientTariffVersion.STATUS_DRAFT,
        user=user,
    )
    target.vat_type = source.vat_type
    target.currency = source.currency
    target.contract_number = source.contract_number
    target.contract_date = source.contract_date
    target.additional_agreement_number = source.additional_agreement_number
    target.additional_agreement_date = source.additional_agreement_date
    target.general_comment = source.general_comment
    target.save()
    for item in source.items.select_related("category", "service", "unit").all():
        ClientTariffItem.objects.create(
            tariff_version=target,
            category=item.category,
            service=item.service,
            service_name=item.service_name,
            description=item.description,
            unit=item.unit,
            price=item.price,
            minimum_amount=item.minimum_amount,
            minimum_quantity=item.minimum_quantity,
            included_materials=item.included_materials,
            conditions=item.conditions,
            calculation_type=item.calculation_type,
            coefficient=item.coefficient,
            sort_order=item.sort_order,
            is_active=item.is_active,
        )
    for condition in source.conditions.select_related("unit").all():
        ClientTariffCondition.objects.create(
            tariff_version=target,
            condition_type=condition.condition_type,
            name=condition.name,
            description=condition.description,
            value=condition.value,
            unit=condition.unit,
            valid_from=condition.valid_from,
            valid_to=condition.valid_to,
            sort_order=condition.sort_order,
            is_active=condition.is_active,
        )
    source_rule = getattr(source, "storage_rule", None)
    if source_rule is not None:
        target_rule = getattr(target, "storage_rule", None)
        if target_rule is None:
            from .models import StorageCalculationRule

            target_rule = StorageCalculationRule(tariff_version=target)
        target_rule.billing_mode = source_rule.billing_mode
        target_rule.charge_basis = getattr(source_rule, "charge_basis", None) or StorageCalculationRule.BASIS_PALLET
        target_rule.charge_period = getattr(source_rule, "charge_period", None) or StorageCalculationRule.PERIOD_DAY
        target_rule.month_mode = source_rule.month_mode
        target_rule.day_counting = source_rule.day_counting
        target_rule.free_period_type = source_rule.free_period_type
        target_rule.free_period_value = source_rule.free_period_value
        target_rule.volume_level = source_rule.volume_level
        target_rule.space_coefficient_default = source_rule.space_coefficient_default
        target_rule.rounding_mode = source_rule.rounding_mode
        target_rule.min_billable_volume = source_rule.min_billable_volume
        target_rule.min_amount_day = source_rule.min_amount_day
        target_rule.min_amount_month = source_rule.min_amount_month
        target_rule.missing_dims_policy = source_rule.missing_dims_policy
        target_rule.snapshot_hour = source_rule.snapshot_hour
        target_rule.timezone_name = source_rule.timezone_name
        target_rule.accountant_comment = source_rule.accountant_comment
        target_rule.save()
    _audit_tariff(
        "tariff_version_copied",
        target,
        user=user,
        old_value={"source_id": source.id},
        new_value={"target_id": target.id},
    )
    return target


@transaction.atomic
def activate_tariff_version(version: ClientTariffVersion, *, user=None) -> ClientTariffVersion:
    if version.status == ClientTariffVersion.STATUS_ARCHIVED:
        raise ValidationError("Архивную редакцию нельзя активировать.")
    old_status = version.status
    previous = (
        ClientTariffVersion.objects.filter(
            client=version.client,
            status__in=[ClientTariffVersion.STATUS_ACTIVE, ClientTariffVersion.STATUS_SCHEDULED],
        )
        .exclude(pk=version.pk)
        .filter(Q(valid_to__isnull=True) | Q(valid_to__gte=version.valid_from))
    )
    for item in previous:
        item.status = ClientTariffVersion.STATUS_EXPIRED
        if item.valid_to is None or item.valid_to >= version.valid_from:
            item.valid_to = version.valid_from - timedelta(days=1)
        item.save()
    version.status = ClientTariffVersion.STATUS_ACTIVE if version.valid_from <= timezone.localdate() else ClientTariffVersion.STATUS_SCHEDULED
    version.approved_by = user if getattr(user, "is_authenticated", False) else version.approved_by
    version.approved_at = version.approved_at or timezone.now()
    version.save()
    _audit_tariff(
        "tariff_version_activated",
        version,
        user=user,
        old_value={"status": old_status},
        new_value={"status": version.status},
    )
    return version


def archive_tariff_version(version: ClientTariffVersion, *, user=None) -> ClientTariffVersion:
    old_status = version.status
    version.status = ClientTariffVersion.STATUS_ARCHIVED
    version.save()
    _audit_tariff(
        "tariff_version_archived",
        version,
        user=user,
        old_value={"status": old_status},
        new_value={"status": version.status},
    )
    return version


def get_company_tariff(company: Agency, service: BillingService, operation_date, **_kwargs) -> CompanyTariffResult | None:
    version = active_tariff_version(company, operation_date)
    if not version:
        return None
    item = (
        version.items.select_related("unit", "category", "service")
        .filter(service=service, is_active=True)
        .order_by("sort_order", "id")
        .first()
    )
    if not item:
        return None
    return CompanyTariffResult(
        version=version,
        item=item,
        price=item.price,
        unit=item.unit,
        minimum_amount=item.minimum_amount,
        coefficient=item.coefficient,
        conditions=item.conditions,
    )


def list_agreed_billing_services(client: Agency, *, on_date=None) -> list[dict]:
    """Услуги и цены из действующей редакции согласованного тарифа клиента."""
    version = active_tariff_version(client, on_date=on_date)
    if not version:
        return []
    items = (
        version.items.filter(is_active=True)
        .select_related("service", "unit")
        .order_by("category__sort_order", "sort_order", "service_name", "id")
    )
    rows: list[dict] = []
    seen: set[int] = set()
    for item in items:
        service = item.service
        if not service or not getattr(service, "is_active", True) or service.id in seen:
            continue
        seen.add(service.id)
        unit = getattr(item.unit, "short_name", None) or getattr(item.unit, "name", None) or service.unit or "шт"
        rows.append(
            {
                "id": service.id,
                "code": service.code or "",
                "name": (item.service_name or service.name or "").strip() or service.name,
                "unit": str(unit),
                "price": Decimal(item.price or "0"),
            }
        )
    return rows


class TariffService:
    """Единая точка доступа к опубликованным ценам (обёртка над существующим резолвером)."""

    @staticmethod
    def get_active_tariff(company_id, operation_date=None):
        client = Agency.objects.filter(pk=company_id).first()
        if not client:
            return None
        return active_tariff_version(client, operation_date)

    @staticmethod
    def get_service_price(
        company_id,
        service_id,
        operation_date,
        quantity=None,
        project_id=None,
        contract_id=None,
        client_legal_entity_id=None,
        fullbox_legal_entity_id=None,
    ) -> dict | None:
        del project_id, contract_id, client_legal_entity_id, fullbox_legal_entity_id  # зарезервировано
        client = Agency.objects.filter(pk=company_id).first()
        service = BillingService.objects.filter(pk=service_id).first()
        if not client or not service:
            return None
        result = get_company_tariff(client, service, operation_date, quantity=quantity)
        if not result:
            return None
        from .price_resolver import apply_quantity_rules

        qty = Decimal(quantity) if quantity is not None else Decimal("1")
        unit_price = apply_quantity_rules(
            result.price,
            qty,
            coefficient=result.coefficient,
            minimum_amount=result.minimum_amount,
            calculation_type=result.item.calculation_type,
            minimum_quantity=result.item.minimum_quantity,
        )
        return {
            "tariff_plan_id": None,
            "tariff_version_id": result.version.id,
            "tariff_item_id": result.item.id,
            "service_id": service.id,
            "unit_price": unit_price,
            "tax_rate": result.version.vat_type or "",
            "price_without_tax": unit_price,
            "price_with_tax": None,
            "currency": "RUB",
            "calculation_type": result.item.calculation_type,
            "range_from": None,
            "range_to": None,
            "minimum_amount": result.minimum_amount,
            "source_document_id": result.version.contract_id,
            "valid_from": result.version.valid_from,
            "valid_to": result.version.valid_to,
        }


def grouped_tariff_items(version: ClientTariffVersion | None):
    if not version:
        return []
    categories = []
    qs = (
        version.items.select_related("category", "service", "unit")
        .filter(is_active=True)
        .order_by("category__sort_order", "sort_order", "service_name")
    )
    current_code = None
    current = None
    for item in qs:
        if item.category.code != current_code:
            current = {"category": item.category, "items": []}
            categories.append(current)
            current_code = item.category.code
        current["items"].append(item)
    return categories


def active_client_logistics_tariff(client: Agency, on_date=None) -> ClientLogisticsTariff | None:
    """Текущий опубликованный прайс доставки клиента для его личного кабинета."""
    on_date = on_date or timezone.localdate()
    return (
        ClientLogisticsTariff.objects.filter(client=client, status=ClientLogisticsTariff.STATUS_ACTIVE)
        .filter(_date_range_filter(on_date))
        .prefetch_related("items")
        .order_by("-valid_from", "-version_number", "-id")
        .first()
    )


def build_profile_tariff_context(client: Agency) -> dict:
    current = active_tariff_version(client)
    logistics_current = active_client_logistics_tariff(client)
    history = list(tariff_history(client)[:20])
    future = (
        ClientTariffVersion.objects.filter(client=client, valid_from__gt=timezone.localdate())
        .exclude(status=ClientTariffVersion.STATUS_ARCHIVED)
        .order_by("valid_from", "version_number")
        .first()
    )
    return {
        "tariff_current": current,
        "tariff_future": future,
        "tariff_groups": grouped_tariff_items(current),
        "tariff_conditions": list(current.conditions.select_related("unit").filter(is_active=True)) if current else [],
        "tariff_history": history,
        "tariff_today": timezone.localdate(),
        "logistics_tariff_current": logistics_current,
        "logistics_tariff_items": list(logistics_current.items.filter(is_active=True)) if logistics_current else [],
    }


def render_tariff_print_response(version: ClientTariffVersion, *, as_attachment: bool = False) -> HttpResponse:
    html = render_to_string(
        "billing/tariff_print.html",
        {
            "version": version,
            "groups": grouped_tariff_items(version),
            "conditions": version.conditions.select_related("unit").filter(is_active=True),
            "generated_at": timezone.localtime(),
        },
    )
    response = HttpResponse(html, content_type="text/html; charset=utf-8")
    filename = f"tariffs_{version.client_id}_v{version.version_number}.html"
    disposition = "attachment" if as_attachment else "inline"
    response["Content-Disposition"] = f'{disposition}; filename="{filename}"'
    return response


def _audit_tariff(action: str, version: ClientTariffVersion, *, user=None, old_value=None, new_value=None, comment: str = ""):
    return BillingAuditEvent.objects.create(
        application=None,
        user=user if getattr(user, "is_authenticated", False) else None,
        action=action,
        object_type=version.__class__.__name__,
        object_id=str(version.pk),
        old_value=old_value,
        new_value=new_value,
        comment=comment,
    )


def ensure_draft(version: ClientTariffVersion):
    if version.status != ClientTariffVersion.STATUS_DRAFT:
        raise PermissionDenied("Редактировать можно только черновик тарифов.")
