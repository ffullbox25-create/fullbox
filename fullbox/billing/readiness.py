"""Read-only readiness checks. This is NOT an invoice calculator or approval."""
from decimal import Decimal, InvalidOperation

from django.db.models import Q

from .models import BillingService, WarehouseServiceFact
from .price_resolver import resolve_client_service_price
from .warehouse_services import candidates_from_warehouse_facts, compatible_warehouse_order_types


def _unresolved_fact_count(application):
    return WarehouseServiceFact.objects.filter(
        client_id=application.client_id,
        order_id=str(application.application_id),
        order_type__in=compatible_warehouse_order_types(application.application_type),
    ).filter(Q(application__isnull=True) | Q(application=application)).exclude(
        status=WarehouseServiceFact.STATUS_CANCELLED
    ).filter(
        Q(service__isnull=True) | ~Q(status__in={
            WarehouseServiceFact.STATUS_SENT_TO_BILLING,
            WarehouseServiceFact.STATUS_APPROVED,
            WarehouseServiceFact.STATUS_CHARGED,
        })
    ).count()


def preview_application_readiness(application):
    """Inspect reported facts without fallback quantities or billing writes.

    Callers must enforce a read-only DB transaction. Tariff resolution is shared
    with billing; no catalog seeding, sync, charge creation or confirmation runs.
    Specialized FBS/storage flows are deliberately not approved by this check.
    """
    result = {
        "application_pk": application.pk,
        "application_id": application.application_id,
        "client_id": application.client_id,
        "application_type": application.application_type,
        "status": "needs_review",
        "rows": [],
        "issues": [],
        "creates_charges": False,
    }
    if application.application_type in {"fbs", "storage"}:
        result["issues"].append("specialized_preview_required")
        return result
    unresolved = _unresolved_fact_count(application)
    result["unresolved_fact_count"] = unresolved
    if unresolved:
        result["issues"].append("unresolved_warehouse_facts")
    candidates = candidates_from_warehouse_facts(application)
    if candidates is None:
        result["issues"].append("warehouse_facts_missing")
        return result
    if not candidates:
        result["issues"].append("no_eligible_warehouse_facts")
        return result
    keys = set()
    for candidate in candidates:
        row = {
            "service_code": candidate.service_code,
            "quantity": str(candidate.quantity),
            "performed_at": candidate.performed_at.isoformat() if candidate.performed_at else None,
            "source_key": candidate.source_key,
            "issues": [],
        }
        result["rows"].append(row)
        try:
            quantity = Decimal(str(candidate.quantity))
            valid_quantity = quantity.is_finite() and quantity > 0
        except (InvalidOperation, ValueError, TypeError):
            valid_quantity = False
        if not valid_quantity:
            row["issues"].append("invalid_fact_quantity")
        if not candidate.performed_at:
            row["issues"].append("performed_at_missing")
        if not candidate.source_key:
            row["issues"].append("source_key_missing")
        elif candidate.source_key in keys:
            row["issues"].append("duplicate_source_key")
        keys.add(candidate.source_key)
        service = BillingService.objects.filter(code=candidate.service_code, is_active=True).first()
        if service is None:
            row["issues"].append("service_missing_or_inactive")
        if row["issues"]:
            continue
        resolved = resolve_client_service_price(
            application, service, performed_at=candidate.performed_at, quantity=quantity
        )
        version = getattr(resolved, "tariff_version", None)
        logistics = getattr(resolved, "logistics_tariff", None)
        if not resolved.ok or resolved.tariff is None or (version is None and logistics is None):
            row["issues"].append("agreed_tariff_missing")
            row["reason"] = resolved.note or resolved.reason
            continue
        row["tariff_version_id"] = version.pk if version else None
        row["logistics_tariff_id"] = logistics.pk if logistics else None
        row["unit"] = resolved.unit or service.unit
        try:
            tariff = Decimal(str(resolved.tariff))
            valid_tariff = tariff.is_finite() and tariff >= 0
        except (InvalidOperation, ValueError, TypeError):
            valid_tariff = False
        if not valid_tariff:
            row["issues"].append("invalid_tariff")
            continue
        # Zero may be a contractual inclusion/free service. Do not silently
        # equate it with missing price, or approve it for invoicing here.
        if tariff == 0:
            row["issues"].append("zero_tariff_requires_contract_review")
    if not result["issues"] and all(not row["issues"] for row in result["rows"]):
        result["status"] = "facts_and_tariffs_found"
    return result
