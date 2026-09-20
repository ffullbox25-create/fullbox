from __future__ import annotations

from dataclasses import asdict
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from billing.models import ApplicationCharge, BillingApplication, BillingService
from billing.price_resolver import resolve_client_service_price
from billing.service_catalog import ensure_service_catalog
from billing.services import BillingWorkflowService
from billing.statuses import BillingStatus

from . import logistics, other, processing, receiving, shipping, storage


CALCULATORS = {
    BillingApplication.TYPE_RECEIVING: receiving.calculate,
    BillingApplication.TYPE_PROCESSING: processing.calculate,
    BillingApplication.TYPE_PACKING: processing.calculate,
    BillingApplication.TYPE_SHIPPING: shipping.calculate,
    BillingApplication.TYPE_LOGISTICS: logistics.calculate,
    BillingApplication.TYPE_STORAGE: storage.calculate,
    BillingApplication.TYPE_OTHER: other.calculate,
}


def _performed_at(application):
    return application.operations_completed_at or application.created_at_source or timezone.now()


def _write_billing_meta(application, missing: list[dict], applied: list[dict]) -> None:
    payload = dict(application.source_payload or {})
    billing_meta = dict(payload.get("billing") or {})
    billing_meta["missing_tariffs"] = missing
    billing_meta["missing_tariff_count"] = len(missing)
    billing_meta["applied_prices"] = applied
    payload["billing"] = billing_meta
    application.source_payload = payload
    application.save(update_fields=["source_payload", "updated_at"])


def _strip_ungrounded_prices(application: BillingApplication) -> int:
    """Обнуляет цены начислений без опубликованного тарифа (не в акте/счёте, не override)."""
    from decimal import Decimal

    qs = application.charges.filter(
        is_manual_override=False,
        client_tariff_version__isnull=True,
        client_logistics_tariff__isnull=True,
        is_included_in_act=False,
        is_included_in_invoice=False,
    )
    stripped = 0
    for charge in qs:
        has_money = (
            Decimal(str(charge.tariff or "0")) != 0
            or Decimal(str(charge.amount or "0")) != 0
            or Decimal(str(charge.total_amount or "0")) != 0
        )
        if not has_money and charge.price_source == "missing_agreed_tariff":
            continue
        charge.tariff = Decimal("0")
        charge.tariff_price = None
        charge.amount = Decimal("0")
        charge.vat_amount = Decimal("0")
        charge.total_amount = Decimal("0")
        charge.price_source = "missing_agreed_tariff"
        charge.tariff_source_label = ""
        charge.tariff_basis = ""
        charge.client_tariff_item = None
        charge.is_confirmed = False
        charge.save(
            update_fields=[
                "tariff",
                "tariff_price",
                "amount",
                "vat_amount",
                "total_amount",
                "price_source",
                "tariff_source_label",
                "tariff_basis",
                "client_tariff_item",
                "is_confirmed",
                "updated_at",
            ]
        )
        stripped += 1
    return stripped


@transaction.atomic
def calculate_application(application: BillingApplication, *, user=None) -> dict:
    services = ensure_service_catalog()
    from billing.warehouse_services import (
        candidates_from_warehouse_facts,
        exclude_stale_warehouse_fact_charges,
        link_facts_to_charges,
    )

    # Факты кладовщика (услуга+qty) имеют приоритет над эвристикой калькулятора.
    fact_candidates = candidates_from_warehouse_facts(application)
    if fact_candidates is not None:
        candidates = list(fact_candidates)
        # Доставка до маркетплейса не является складским фактом: её цена и
        # количество берутся из заявки/паллетизации и тарифов логистики.
        if application.application_type == BillingApplication.TYPE_SHIPPING:
            candidates.extend(
                candidate
                for candidate in shipping.calculate(application)
                if candidate.service_code == "logistics_pickup_goods_market"
            )
    else:
        calculator = CALCULATORS.get(application.application_type, other.calculate)
        candidates = calculator(application)
    created = []
    missing = []
    applied = []
    default_performed_at = _performed_at(application)

    for candidate in candidates:
        performed_at = candidate.performed_at or default_performed_at
        service = services.get(candidate.service_code) or BillingService.objects.filter(code=candidate.service_code).first()
        if not service:
            missing.append({**asdict(candidate), "reason": "service_missing"})
            continue
        resolved = resolve_client_service_price(
            application, service, performed_at=performed_at, quantity=candidate.quantity
        )
        if not resolved.ok or resolved.tariff is None or (
            resolved.tariff_version is None and resolved.logistics_tariff is None
        ):
            missing.append(
                {
                    "service_code": service.code,
                    "service_name": service.name,
                    "quantity": str(candidate.quantity),
                    "unit": resolved.unit or service.unit,
                    "reason": resolved.reason or "agreed_tariff_missing",
                    "note": resolved.note
                    or (
                        f'Для услуги «{service.name}» не найден согласованный тариф клиента '
                        f'на дату {performed_at.date().strftime("%d.%m.%Y")}'
                    ),
                    "status": "requires_tariff_setup",
                }
            )
            continue
        applied.append(
            {
                "service_code": service.code,
                "service_name": resolved.service_name or service.name,
                "source": resolved.source,
                "tariff": str(resolved.tariff),
                "tariff_price": str(resolved.tariff_price) if resolved.tariff_price is not None else "",
                "vat_rate": resolved.vat_rate,
                "contract_id": resolved.contract_id,
                "own_company_id": resolved.own_company_id,
                "tariff_version_id": resolved.tariff_version.id if resolved.tariff_version else None,
                "tariff_item_id": resolved.tariff_item.id if resolved.tariff_item else None,
                "logistics_tariff_id": resolved.logistics_tariff.id if resolved.logistics_tariff else None,
                "logistics_tariff_item_id": resolved.logistics_tariff_item.id if resolved.logistics_tariff_item else None,
                "tariff_source_label": resolved.tariff_source_label,
                "tariff_basis": resolved.tariff_basis,
            }
        )
        existing_excluded = (
            ApplicationCharge.objects.filter(
                application=application,
                source_key=candidate.source_key,
                is_excluded=True,
            ).first()
            if candidate.source_key
            else None
        )
        if existing_excluded is not None:
            continue
        charge = BillingWorkflowService.create_or_update_charge(
            application,
            service=service,
            quantity=candidate.quantity,
            tariff=resolved.tariff,
            unit=resolved.unit or service.unit,
            vat_rate=resolved.vat_rate,
            vat_type=resolved.vat_type,
            source_type=(
                ApplicationCharge.SOURCE_LOGISTICS_TRIP
                if application.application_type == BillingApplication.TYPE_LOGISTICS
                else ApplicationCharge.SOURCE_WAREHOUSE_APPLICATION
            ),
            source_id=candidate.operation_id,
            source_key=candidate.source_key,
            performed_at=performed_at,
            billing_period=performed_at.date().replace(day=1),
            operation_type=candidate.operation_type,
            operation_id=candidate.operation_id,
            comment=candidate.comment,
            user=user,
            client_tariff_version=resolved.tariff_version,
            client_tariff_item=resolved.tariff_item,
            client_logistics_tariff=resolved.logistics_tariff,
            client_logistics_tariff_item=resolved.logistics_tariff_item,
            tariff_price=resolved.tariff_price,
            coefficient=resolved.coefficient,
            minimum_amount=resolved.minimum_amount,
            tariff_source_label=resolved.tariff_source_label,
            tariff_basis=resolved.tariff_basis,
            service_name_snapshot=resolved.service_name or service.name,
            resolve_from_agreed_tariff=False,
        )
        created.append(charge)

    excluded_stale_facts = 0
    if fact_candidates is not None:
        excluded_stale_facts = exclude_stale_warehouse_fact_charges(
            application,
            source_keys={candidate.source_key for candidate in candidates if candidate.source_key},
            user=user,
        )

    stripped = _strip_ungrounded_prices(application)
    if stripped:
        BillingWorkflowService.recalculate_application_totals(application)
    _write_billing_meta(application, missing, applied)
    application.refresh_from_db()
    # Кандидаты без тарифа (missing) не блокируют CALCULATED, если все уже
    # созданные открытые начисления имеют договорную цену — иначе менеджер
    # после подтверждения объёмов не видит «Создать акт».
    from billing.charge_status import charge_missing_price

    open_charges = list(
        application.charges.filter(is_included_in_act=False, is_disputed=False, is_excluded=False)
    )
    has_priced = any(not charge_missing_price(c) for c in open_charges)
    has_unpriced = any(charge_missing_price(c) for c in open_charges)
    if has_priced and not has_unpriced:
        application.billing_status = BillingStatus.CALCULATED
    else:
        application.billing_status = BillingStatus.CALCULATION_DRAFT
    application.save(update_fields=["billing_status", "updated_at"])
    linked_facts = link_facts_to_charges(application)
    BillingWorkflowService.audit(
        action="warehouse_charges_calculated",
        application=application,
        user=user,
        obj=application,
        new_value={
            "created": len(created),
            "missing_tariffs": len(missing),
            "applied_prices": len(applied),
            "stripped_ungrounded": stripped,
            "from_warehouse_facts": fact_candidates is not None,
            "linked_facts": linked_facts,
            "excluded_stale_facts": excluded_stale_facts,
        },
    )
    return {
        "charges": created,
        "missing_tariffs": missing,
        "stripped_ungrounded": stripped,
        "from_warehouse_facts": fact_candidates is not None,
        "excluded_stale_facts": excluded_stale_facts,
    }
