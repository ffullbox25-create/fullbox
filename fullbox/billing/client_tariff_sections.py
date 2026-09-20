"""Three accounting tariff sections for one client.

The models remain intentionally separate because each calculation contour has
its own resolver.  This module only presents them as one client tariff card:
general warehouse/FBO services, logistics and FBS.
"""
from __future__ import annotations

from collections import OrderedDict

from django.utils import timezone

from .models import FbsClientRate
from .tariff_services import active_client_logistics_tariff, grouped_tariff_items


FBS_CATEGORY_CODES = {"fbs_processing"}
LOGISTICS_CATEGORY_CODES = {"logistics"}


def _is_fbs_item(item) -> bool:
    return item.category.code in FBS_CATEGORY_CODES or str(item.service.code or "").startswith("fbs_")


def _is_logistics_item(item) -> bool:
    return item.category.code in LOGISTICS_CATEGORY_CODES


def _filtered_groups(version, predicate) -> list[dict]:
    result = []
    for group in grouped_tariff_items(version):
        items = [item for item in group["items"] if predicate(item)]
        if items:
            result.append({"category": group["category"], "items": items})
    return result


def _fbs_rate_groups(client, on_date) -> list[dict]:
    rates = list(
        FbsClientRate.objects.filter(
            client=client,
            is_active=True,
            valid_from__lte=on_date,
        )
        .filter(valid_to__isnull=True)
        .order_by("operation", "liters_from", "liters_to", "id")
    )
    # Include rows ending after the selected date without widening the primary
    # query to a union that would make ordering harder to audit.
    rates.extend(
        FbsClientRate.objects.filter(
            client=client,
            is_active=True,
            valid_from__lte=on_date,
            valid_to__gte=on_date,
        ).order_by("operation", "liters_from", "liters_to", "id")
    )
    by_operation = OrderedDict()
    for rate in sorted(rates, key=lambda row: (row.operation, row.liters_from, row.id)):
        group = by_operation.setdefault(
            rate.operation,
            {
                "operation": rate.operation,
                "label": rate.get_operation_display(),
                "rates": [],
            },
        )
        group["rates"].append(rate)
    return list(by_operation.values())


def build_client_tariff_sections(client, tariff_version, *, on_date=None) -> dict:
    """Return the three user-facing tariff sections without changing prices."""
    on_date = on_date or timezone.localdate()
    general_groups = _filtered_groups(
        tariff_version,
        lambda item: not _is_fbs_item(item) and not _is_logistics_item(item),
    )
    logistics_service_groups = _filtered_groups(tariff_version, _is_logistics_item)
    fbs_service_groups = _filtered_groups(tariff_version, _is_fbs_item)
    logistics_tariff = active_client_logistics_tariff(client, on_date)
    logistics_items = (
        list(logistics_tariff.items.filter(is_active=True).order_by("marketplace", "sort_order", "warehouse_name", "id"))
        if logistics_tariff
        else []
    )
    fbs_rate_groups = _fbs_rate_groups(client, on_date)
    fbs_rates_count = sum(len(group["rates"]) for group in fbs_rate_groups)
    fbs_storage_configured = any(
        group["operation"] == FbsClientRate.OP_STORAGE for group in fbs_rate_groups
    )
    return {
        "general": {
            "key": "general",
            "label": "Общие тарифы",
            "description": "Единый тариф FBO и складских услуг.",
            "groups": general_groups,
            "items_count": sum(len(group["items"]) for group in general_groups),
            "version": tariff_version,
        },
        "logistics": {
            "key": "logistics",
            "label": "Логистика",
            "description": "Отдельные направления и цены доставки клиента.",
            "tariff": logistics_tariff,
            "items": logistics_items,
            "service_groups": logistics_service_groups,
            "items_count": len(logistics_items)
            + sum(len(group["items"]) for group in logistics_service_groups),
        },
        "fbs": {
            "key": "fbs",
            "label": "FBS",
            "description": "Приёмка, подбор, маркировка, отгрузка и хранение FBS.",
            "rate_groups": fbs_rate_groups,
            "service_groups": fbs_service_groups,
            "items_count": fbs_rates_count
            + sum(len(group["items"]) for group in fbs_service_groups),
            "storage_configured": fbs_storage_configured,
        },
    }
