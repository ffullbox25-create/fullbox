from __future__ import annotations

from django.db.models import Q
from django.utils import timezone

from .models import BillingApplication, ClientTariff, StandardServicePrice
from .price_resolver import active_contract, default_own_company, resolve_client_service_price
from .serializers import client_contract_dict, client_tariff_dict, own_company_dict, standard_price_dict


def build_client_tariffs_payload(agency) -> dict:
    today = timezone.localdate()
    contract = active_contract(agency, today)
    own_company = contract.own_company if contract else default_own_company()
    # Lightweight synthetic application lets the resolver reuse existing tariff rules.
    application = BillingApplication(
        application_type=BillingApplication.TYPE_OTHER,
        application_id="tariffs-preview",
        client=agency,
        legal_entity=agency,
        own_company=own_company,
        created_at_source=timezone.now(),
    )
    individual_by_service = {
        tariff.service_id: tariff
        for tariff in ClientTariff.objects.select_related("service")
        .filter(client=agency, is_active=True, valid_from__lte=today)
        .filter(Q(valid_to__isnull=True) | Q(valid_to__gte=today))
        .order_by("service_id", "-valid_from", "-id")
    }
    rows = []
    for price in StandardServicePrice.objects.select_related("service").filter(is_active=True).order_by("section_code", "line_no", "service__code"):
        resolved = resolve_client_service_price(application, price.service, performed_at=timezone.now())
        individual = individual_by_service.get(price.service_id)
        rows.append(
            {
                "standard": standard_price_dict(price),
                "effective": {
                    "ok": resolved.ok,
                    "source": resolved.source,
                    "tariff": str(resolved.tariff) if resolved.tariff is not None else None,
                    "unit": resolved.unit,
                    "vat_rate": resolved.vat_rate,
                    "reason": resolved.reason,
                    "note": resolved.note,
                },
                "individual_tariff": client_tariff_dict(individual) if individual else None,
            }
        )
    return {
        "contract": client_contract_dict(contract) if contract else None,
        "own_company": own_company_dict(own_company),
        "rows": rows,
    }
