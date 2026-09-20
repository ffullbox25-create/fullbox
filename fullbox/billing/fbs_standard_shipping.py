"""FBS calculation over existing shipping orders.

This module is intentionally read-only to the warehouse domain.  It creates
only billing applications and charge snapshots after a manager presses the
calculation button in billing.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from shipping.models import ShippingOrder
from sku.models import SKU

from .models import (
    BillingApplication,
    BillingService,
    ClientTariffVersion,
    FbsClientRate,
)
from .services import BillingWorkflowService


LITERS_PER_CUBIC_MILLIMETER = Decimal("1000000")
FBS_MARKING_LABEL = "Маркировка ШК 58×40"
SERVICE_BY_OPERATION = {
    FbsClientRate.OP_RECEIVING: "fbs_receiving_goods",
    FbsClientRate.OP_PICKING: "fbs_pick_item",
    FbsClientRate.OP_MARKING: "fbs_marking_label_58x40",
    FbsClientRate.OP_SHIPPING: "fbs_shipping_item",
    FbsClientRate.OP_STORAGE: "fbs_storage_liter_day",
}
OPERATIONS_FROM_SHIPPING = (
    FbsClientRate.OP_PICKING,
    FbsClientRate.OP_SHIPPING,
)


@dataclass(frozen=True)
class FbsCalculationRow:
    order: ShippingOrder
    operation: str
    rate: FbsClientRate
    quantity: Decimal
    service_date: date
    liters: Decimal | None = None
    sku_count: int = 0


def order_service_date(order: ShippingOrder) -> date | None:
    """Use the shipping date specified in the request; actual date is fallback."""
    if order.planned_ship_date:
        return order.planned_ship_date
    if order.shipped_at:
        return timezone.localtime(order.shipped_at).date()
    return None


def sku_liters(sku) -> Decimal | None:
    dimensions = (getattr(sku, "length_mm", None), getattr(sku, "width_mm", None), getattr(sku, "height_mm", None))
    if any(value is None or Decimal(str(value)) <= 0 for value in dimensions):
        return None
    return (Decimal(str(dimensions[0])) * Decimal(str(dimensions[1])) * Decimal(str(dimensions[2]))) / LITERS_PER_CUBIC_MILLIMETER


def _normalize_sku_lookup(value) -> str:
    return str(value or "").strip().casefold()


def _shipping_item_sku_maps(*, client, items) -> tuple[dict[str, SKU], dict[str, SKU]]:
    """Resolve legacy shipping rows that were saved without their SKU foreign key."""
    sku_codes = {
        str(item.sku_code or "").strip()
        for item in items
        if not item.sku_id and str(item.sku_code or "").strip()
    }
    barcodes = {
        str(item.barcode or "").strip()
        for item in items
        if not item.sku_id and str(item.barcode or "").strip()
    }
    if not sku_codes and not barcodes:
        return {}, {}

    candidate_filter = Q()
    if sku_codes:
        candidate_filter |= Q(sku_code__in=sku_codes)
    if barcodes:
        candidate_filter |= Q(barcodes__value__in=barcodes)
    candidates = (
        SKU.objects.filter(agency=client, deleted=False)
        .filter(candidate_filter)
        .prefetch_related("barcodes")
        .distinct()
        .order_by("id")
    )
    by_code: dict[str, SKU] = {}
    by_barcode: dict[str, SKU] = {}
    for sku in candidates:
        code_key = _normalize_sku_lookup(sku.sku_code)
        if code_key:
            by_code.setdefault(code_key, sku)
        for barcode in sku.barcodes.all():
            barcode_key = _normalize_sku_lookup(barcode.value)
            if barcode_key:
                by_barcode.setdefault(barcode_key, sku)
    return by_code, by_barcode


def _shipping_item_sku(*, item, client, by_code: dict[str, SKU], by_barcode: dict[str, SKU]):
    linked_sku = item.sku
    if linked_sku is not None and linked_sku.agency_id == client.pk and not linked_sku.deleted:
        return linked_sku
    return by_code.get(_normalize_sku_lookup(item.sku_code)) or by_barcode.get(
        _normalize_sku_lookup(item.barcode)
    )


def fbs_rate_billable_quantity(
    *,
    operation: str,
    rate: FbsClientRate,
    item_quantity: Decimal,
    liters: Decimal | None,
) -> Decimal:
    """Use liters as the charge quantity when delivery is priced per liter."""
    unit = str(rate.unit or "").strip().casefold().replace(".", "")
    if operation == FbsClientRate.OP_SHIPPING and unit in {"л", "литр", "литры", "литров"}:
        if liters is None:
            raise ValidationError("Для доставки FBS по литрам нужны габариты товара.")
        return item_quantity * liters
    return item_quantity


def _rate_for(*, client, operation: str, liters: Decimal, on_date: date) -> FbsClientRate | None:
    candidates = (
        FbsClientRate.objects.filter(
            client=client,
            operation=operation,
            is_active=True,
            valid_from__lte=on_date,
        )
        .filter(Q(valid_to__isnull=True) | Q(valid_to__gte=on_date))
        .filter(liters_from__lt=liters)
        .filter(Q(liters_to__isnull=True) | Q(liters_to__gte=liters))
        .order_by("-valid_from", "-liters_from", "-id")
    )
    return candidates.first()


def _rate_for_flat_operation(*, client, operation: str, on_date: date) -> FbsClientRate | None:
    return (
        FbsClientRate.objects.filter(
            client=client,
            operation=operation,
            is_active=True,
            valid_from__lte=on_date,
        )
        .filter(Q(valid_to__isnull=True) | Q(valid_to__gte=on_date))
        .order_by("-valid_from", "liters_from", "-id")
        .first()
    )


def _selected_orders(*, client, date_from: date, date_to: date):
    orders = (
        ShippingOrder.objects.filter(
            agency=client,
            status__in=(ShippingOrder.STATUS_SHIPPED, ShippingOrder.STATUS_PARTIAL),
        )
        .select_related("agency", "marketplace")
        .prefetch_related("items__sku")
        .order_by("planned_ship_date", "shipped_at", "id")
    )
    # planned_ship_date is the explicit date in the request.  Rows without it
    # are handled below via shipped_at so legacy requests remain billable.
    return [
        order
        for order in orders
        if (service_date := order_service_date(order)) and date_from <= service_date <= date_to
    ]


def build_standard_shipping_preview(*, client, date_from: date, date_to: date) -> dict:
    rows: list[FbsCalculationRow] = []
    errors: list[str] = []
    orders = _selected_orders(client=client, date_from=date_from, date_to=date_to)
    all_items = [item for order in orders for item in order.items.all()]
    sku_by_code, sku_by_barcode = _shipping_item_sku_maps(client=client, items=all_items)
    for order in orders:
        service_date = order_service_date(order)
        assert service_date is not None
        grouped: dict[tuple[str, int], dict] = {}
        for item in order.items.all():
            quantity = Decimal(str(item.qty_shipped or 0))
            if quantity <= 0:
                continue
            sku = _shipping_item_sku(
                item=item,
                client=client,
                by_code=sku_by_code,
                by_barcode=sku_by_barcode,
            )
            liters = sku_liters(sku)
            if liters is None:
                errors.append(f"{order.number}: у артикула {item.sku_code} нет полных габаритов, строка не рассчитана.")
                continue
            for operation in OPERATIONS_FROM_SHIPPING:
                rate = _rate_for(client=client, operation=operation, liters=liters, on_date=service_date)
                if rate is None:
                    errors.append(
                        f"{order.number}: нет ставки «{dict(FbsClientRate.OPERATION_CHOICES)[operation]}» для {liters:.3f} л на {service_date:%d.%m.%Y}."
                    )
                    continue
                billable_quantity = fbs_rate_billable_quantity(
                    operation=operation,
                    rate=rate,
                    item_quantity=quantity,
                    liters=liters,
                )
                key = (operation, rate.pk)
                group = grouped.setdefault(key, {"rate": rate, "quantity": Decimal("0"), "sku_count": 0, "liters": liters})
                group["quantity"] += billable_quantity
                group["sku_count"] += 1

        for (operation, _rate_id), group in grouped.items():
            rows.append(
                FbsCalculationRow(
                    order=order,
                    operation=operation,
                    rate=group["rate"],
                    quantity=group["quantity"],
                    service_date=service_date,
                    liters=group["liters"],
                    sku_count=group["sku_count"],
                )
            )

    total = sum((row.quantity * row.rate.price for row in rows), Decimal("0"))
    return {"orders": orders, "rows": rows, "errors": errors, "amount": total.quantize(Decimal("0.01"))}


def _active_tariff_version(*, client, on_date: date) -> ClientTariffVersion | None:
    return (
        ClientTariffVersion.objects.filter(
            client=client,
            status__in=(ClientTariffVersion.STATUS_ACTIVE, ClientTariffVersion.STATUS_SCHEDULED),
            valid_from__lte=on_date,
        )
        .filter(Q(valid_to__isnull=True) | Q(valid_to__gte=on_date))
        .order_by("-valid_from", "-version_number", "-id")
        .first()
    )


@transaction.atomic
def calculate_standard_shipping_fbs(*, client, date_from: date, date_to: date, user=None) -> dict:
    """Persist only FBS billing snapshots for the already completed shipment."""
    preview = build_standard_shipping_preview(client=client, date_from=date_from, date_to=date_to)
    applications: dict[int, BillingApplication] = {}
    created_or_updated = 0
    for row in preview["rows"]:
        tariff_version = _active_tariff_version(client=client, on_date=row.service_date)
        if tariff_version is None:
            preview["errors"].append(
                f"{row.order.number}: на {row.service_date:%d.%m.%Y} нет опубликованной редакции тарифа клиента."
            )
            continue
        application = applications.get(row.order.pk)
        if application is None:
            application = BillingWorkflowService.sync_application_from_source(
                application_type=BillingApplication.TYPE_FBS,
                application_id=f"FBS-SHIP-{row.order.number}",
                client=client,
                legal_entity=client,
                manager=None,
                marketplace=row.order.marketplace,
                operational_status=row.order.status,
                operational_status_label="FBS: отгрузка подтверждена",
                created_at_source=timezone.make_aware(datetime.combine(row.service_date, time.min)),
                source_payload={
                    "source": "shipping_order_fbs_calculation",
                    "shipping_order_id": row.order.pk,
                    "shipping_order_number": row.order.number,
                    "service_date": row.service_date.isoformat(),
                },
                user=user,
            )
            applications[row.order.pk] = application
        service_code = SERVICE_BY_OPERATION[row.operation]
        service = BillingService.objects.filter(code=service_code, is_active=True).first()
        if service is None:
            raise ValidationError(f"В биллинге не настроена FBS-услуга {service_code}.")
        operation_label = (
            FBS_MARKING_LABEL
            if row.operation == FbsClientRate.OP_MARKING
            else dict(FbsClientRate.OPERATION_CHOICES)[row.operation]
        )
        lower = f"> {row.rate.liters_from:g} л"
        upper = f"до {row.rate.liters_to:g} л" if row.rate.liters_to is not None else "без верхней границы"
        BillingWorkflowService.create_or_update_charge(
            application,
            service=service,
            quantity=row.quantity,
            tariff=row.rate.price,
            tariff_price=row.rate.price,
            unit=row.rate.unit,
            vat_rate=row.rate.vat_rate,
            vat_type=row.rate.vat_type,
            source_type="shipping_order_fbs",
            source_id=str(row.order.pk),
            source_key=f"fbs-standard-shipping:{row.order.pk}:{row.operation}:{row.rate.pk}",
            performed_at=timezone.make_aware(datetime.combine(row.service_date, time.min)),
            billing_period=row.service_date.replace(day=1),
            operation_type="shipping",
            operation_id=row.order.number,
            comment=f"FBS {operation_label}: {lower}, {upper}.",
            user=user,
            client_tariff_version=tariff_version,
            tariff_source_label="Индивидуальная ставка FBS клиента",
            tariff_basis=f"{operation_label} · ({lower}; {upper}]",
            service_name_snapshot=operation_label,
            resolve_from_agreed_tariff=False,
        )
        created_or_updated += 1
    for application in applications.values():
        if not application.is_operations_completed:
            BillingWorkflowService.mark_operations_completed(application, user=user)
    preview["applications"] = list(applications.values())
    preview["created_or_updated"] = created_or_updated
    return preview
