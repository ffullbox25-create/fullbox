"""Резолвер цены: только согласованная редакция тарифов клиента (ClientTariffVersion)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_HALF_UP

from django.utils import timezone

from head_manager.models import OwnCompany

from .models import (
    BillingApplication,
    BillingService,
    ClientBillingContract,
    ClientLogisticsTariff,
    ClientLogisticsTariffItem,
    ClientTariffItem,
    ClientTariffVersion,
)
from .tariff_services import active_client_logistics_tariff, get_company_tariff


@dataclass(frozen=True)
class ResolvedServicePrice:
    ok: bool
    source: str
    tariff: Decimal | None
    unit: str
    vat_rate: str
    vat_type: str = ""
    own_company_id: int | None = None
    contract_id: int | None = None
    reason: str = ""
    note: str = ""
    # snapshot / источник согласованного тарифа
    tariff_version: ClientTariffVersion | None = None
    tariff_item: ClientTariffItem | None = None
    tariff_price: Decimal | None = None
    coefficient: Decimal = Decimal("1")
    minimum_amount: Decimal | None = None
    tariff_source_label: str = ""
    tariff_basis: str = ""
    service_name: str = ""
    logistics_tariff: ClientLogisticsTariff | None = None
    logistics_tariff_item: ClientLogisticsTariffItem | None = None


def active_contract(client, on_date: date) -> ClientBillingContract | None:
    from django.db.models import Q

    return (
        ClientBillingContract.objects.select_related("own_company")
        .filter(client=client, is_active=True, valid_from__lte=on_date)
        .filter(Q(valid_to__isnull=True) | Q(valid_to__gte=on_date))
        .order_by("-valid_from", "-id")
        .first()
    )


def default_own_company() -> OwnCompany | None:
    return (
        OwnCompany.objects.filter(is_active=True, is_default=True).first()
        or OwnCompany.objects.filter(is_active=True).order_by("id").first()
    )


def _vat_rate_for_contract(contract: ClientBillingContract | None, service: BillingService, version: ClientTariffVersion | None = None) -> str:
    if version and getattr(version, "vat_type", ""):
        raw = str(version.vat_type).strip().lower()
        if raw in {"0", "none", "без ндс", "no_vat", ClientTariffVersion.VAT_NO}:
            return "0"
        if raw in {ClientTariffVersion.VAT_WITH, ClientTariffVersion.VAT_EXTRA, "vat", "with_vat"}:
            own_company = contract.own_company if contract else default_own_company()
            if own_company and getattr(own_company, "tax_mode", "") != OwnCompany.TAX_MODE_NO_VAT:
                return str(getattr(own_company, "vat_rate", "") or "20")
            return "20"
        if raw.replace("%", "").isdigit():
            return raw.replace("%", "")
    own_company = contract.own_company if contract else default_own_company()
    if own_company:
        if getattr(own_company, "tax_mode", "") == OwnCompany.TAX_MODE_NO_VAT:
            return "0"
        return str(getattr(own_company, "vat_rate", "") or "20")
    return str(service.vat_rate or "20")


def _vat_type_for_version(version: ClientTariffVersion | None) -> str:
    raw = str(getattr(version, "vat_type", "") or "").strip()
    if raw in {ClientTariffVersion.VAT_WITH, ClientTariffVersion.VAT_NO, ClientTariffVersion.VAT_EXTRA}:
        return raw
    return ClientTariffVersion.VAT_EXTRA


def _as_date(value) -> date:
    if hasattr(value, "date"):
        if hasattr(value, "tzinfo"):
            return timezone.localtime(value).date()
        return value.date()
    return value


def _basis_label(version: ClientTariffVersion) -> str:
    parts = []
    if version.additional_agreement_number:
        parts.append(f"Дополнительное соглашение №{version.additional_agreement_number}")
    if version.contract_number:
        parts.append(f"Договор №{version.contract_number}")
    if version.contract_id and version.contract:
        parts.append(str(version.contract))
    return " · ".join(parts) if parts else version.name


def _source_label(version: ClientTariffVersion) -> str:
    start = version.valid_from.strftime("%d.%m.%Y") if version.valid_from else ""
    return f"Тариф клиента · действует с {start}" if start else "Тариф клиента"


def _marketplace_key(marketplace) -> str:
    raw = str(getattr(marketplace, "name", "") or "").strip().lower()
    if raw in {"wb", "wildberries", "вайлдберриз"}:
        return "wb"
    if raw in {"ozon", "озон"}:
        return "ozon"
    return "other"


_LOGISTICS_TARIFF_SERVICE_CODES = {"logistics_pickup_goods_market", "logistics_external_trip"}


def _shipping_order_for_application(application: BillingApplication):
    if application.application_type != BillingApplication.TYPE_SHIPPING:
        return None
    try:
        from shipping.models import ShippingOrder
    except Exception:
        return None
    qs = ShippingOrder.objects.filter(number=application.application_id, agency_id=application.client_id)
    return qs.select_related("marketplace").order_by("-id").first()


def _logistics_direction_for_application(application: BillingApplication) -> str:
    payload = application.source_payload or {}
    values = [
        payload.get("delivery_address"),
        payload.get("destination_warehouse"),
        payload.get("warehouse_name"),
        payload.get("route"),
        application.warehouse_label,
    ]
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _logistics_marketplace_for_application(application: BillingApplication, order=None) -> str:
    if application.application_type == BillingApplication.TYPE_LOGISTICS:
        return "other"
    return _marketplace_key(getattr(order, "marketplace", None) if order else application.marketplace)


def _find_logistics_tariff_item(tariff: ClientLogisticsTariff, *, marketplace: str, destination: str):
    items = tariff.items.filter(is_active=True, marketplace=marketplace)
    item = items.filter(warehouse_name__iexact=destination).order_by("sort_order", "id").first() if destination else None
    if item is None and destination:
        destination_lower = destination.lower()
        item = next(
            (
                row
                for row in items.order_by("sort_order", "id")
                if row.warehouse_name.lower() in destination_lower or destination_lower in row.warehouse_name.lower()
            ),
            None,
        )
    if item is None and marketplace != "other":
        return _find_logistics_tariff_item(tariff, marketplace="other", destination=destination)
    return item


def resolve_client_logistics_price(
    application: BillingApplication,
    service: BillingService,
    *,
    on_date: date,
    quantity: Decimal,
    contract: ClientBillingContract | None,
    own_company: OwnCompany | None,
) -> ResolvedServicePrice | None:
    """Цена доставки до МП из отдельной опубликованной редакции клиента."""
    if service.code not in _LOGISTICS_TARIFF_SERVICE_CODES:
        return None
    tariff = active_client_logistics_tariff(application.client, on_date)
    if tariff is None:
        return None
    order = _shipping_order_for_application(application)
    marketplace = _logistics_marketplace_for_application(application, order=order)
    destination = (
        str(getattr(order, "destination_warehouse", "") or "").strip()
        if order is not None
        else _logistics_direction_for_application(application)
    )
    item = _find_logistics_tariff_item(tariff, marketplace=marketplace, destination=destination)
    if item is None:
        direction = destination or "не указанное направление"
        return ResolvedServicePrice(
            ok=False,
            source="client_logistics_tariff",
            tariff=None,
            unit="паллет",
            vat_rate=_vat_rate_for_contract(contract, service),
            vat_type=ClientTariffVersion.VAT_EXTRA,
            own_company_id=own_company.id if own_company else None,
            contract_id=contract.id if contract else None,
            reason="logistics_direction_missing",
            note=f"В логистическом тарифе клиента нет активной цены для направления «{direction}».",
            service_name=service.name,
            logistics_tariff=tariff,
        )
    return ResolvedServicePrice(
        ok=True,
        source="client_logistics_tariff",
        tariff=Decimal(item.price_per_pallet),
        unit="паллет",
        vat_rate=_vat_rate_for_contract(contract, service),
        vat_type=ClientTariffVersion.VAT_EXTRA,
        own_company_id=own_company.id if own_company else None,
        contract_id=contract.id if contract else None,
        tariff_price=Decimal(item.price_per_pallet),
        tariff_source_label=f"Тариф логистики · действует с {tariff.valid_from:%d.%m.%Y}",
        tariff_basis=f"{tariff.name} · {item.get_marketplace_display()} · {item.warehouse_name}",
        service_name=f"{service.name} · {item.warehouse_name}",
        logistics_tariff=tariff,
        logistics_tariff_item=item,
    )


def apply_quantity_rules(
    price: Decimal,
    quantity: Decimal,
    *,
    coefficient: Decimal,
    minimum_amount: Decimal | None,
    calculation_type: str = ClientTariffItem.CALCULATION_BY_UNIT,
    minimum_quantity: Decimal | None = None,
) -> Decimal:
    """Вернуть эффективную цену за единицу с учётом условий позиции тарифа.

    В начислении хранится количество факта, поэтому фиксированную цену и
    минимальное количество представляем эффективной ценой строки.
    """
    coef = Decimal(coefficient or "1")
    unit_price = (Decimal(price) * coef).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
    qty = Decimal(quantity or "0")
    if qty <= 0:
        return unit_price
    if calculation_type == ClientTariffItem.CALCULATION_FIXED:
        line = unit_price
    else:
        billable_qty = max(qty, Decimal(minimum_quantity or "0"))
        line = (unit_price * billable_qty).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if minimum_amount is not None and qty > 0:
        minimum = Decimal(minimum_amount)
        if line < minimum:
            line = minimum
    return (line / qty).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


def resolve_client_service_price(
    application: BillingApplication,
    service: BillingService,
    *,
    performed_at=None,
    quantity=None,
) -> ResolvedServicePrice:
    """
    Единственный источник цены для биллинга — действующая редакция согласованных тарифов
    на дату оказания услуги (не на дату счёта).
    """
    on_date = _as_date(performed_at or application.created_at_source or application.created_at) or timezone.localdate()
    contract = active_contract(application.client, on_date)
    if application.own_company_id:
        own_company = application.own_company
    else:
        own_company = contract.own_company if contract else default_own_company()

    logistics_result = resolve_client_logistics_price(
        application,
        service,
        on_date=on_date,
        quantity=Decimal(quantity) if quantity is not None else Decimal("1"),
        contract=contract,
        own_company=own_company,
    )
    logistics_direction_missing = None
    if logistics_result is not None and (
        logistics_result.ok or logistics_result.reason != "logistics_direction_missing"
    ):
        return logistics_result
    if logistics_result is not None:
        logistics_direction_missing = logistics_result

    result = get_company_tariff(
        application.client,
        service,
        on_date,
        contract=contract,
        quantity=quantity,
    )
    if not result:
        if logistics_direction_missing is not None:
            return logistics_direction_missing
        return ResolvedServicePrice(
            ok=False,
            source="missing_agreed_tariff",
            tariff=None,
            unit=service.unit,
            vat_rate=_vat_rate_for_contract(contract, service),
            vat_type=ClientTariffVersion.VAT_EXTRA,
            own_company_id=own_company.id if own_company else None,
            contract_id=contract.id if contract else None,
            reason="agreed_tariff_missing",
            note=f'Для услуги «{service.name}» не найден согласованный тариф клиента на дату {on_date.strftime("%d.%m.%Y")}',
            service_name=service.name,
        )

    qty = Decimal(quantity) if quantity is not None else Decimal("1")
    applied = apply_quantity_rules(
        result.price,
        qty,
        coefficient=result.coefficient,
        minimum_amount=result.minimum_amount,
        calculation_type=result.item.calculation_type,
        minimum_quantity=result.item.minimum_quantity,
    )
    unit_name = result.unit.name if result.unit else service.unit
    return ResolvedServicePrice(
        ok=True,
        source="agreed_tariff",
        tariff=applied,
        unit=unit_name,
        vat_rate=_vat_rate_for_contract(contract, service, result.version),
        vat_type=_vat_type_for_version(result.version),
        own_company_id=own_company.id if own_company else None,
        contract_id=(result.version.contract_id or (contract.id if contract else None)),
        tariff_version=result.version,
        tariff_item=result.item,
        tariff_price=Decimal(result.price),
        coefficient=Decimal(result.coefficient or "1"),
        minimum_amount=result.minimum_amount,
        tariff_source_label=_source_label(result.version),
        tariff_basis=_basis_label(result.version),
        service_name=result.item.service_name or service.name,
        note=result.conditions or "",
    )
