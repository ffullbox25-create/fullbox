from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from fbs.exceptions import FbsFeatureDisabled, FbsStorageError
from fbs.flags import feature_enabled
from fbs.models import (
    FbsBox,
    FbsClientStoragePolicy,
    FbsPallet,
    FbsStockBalance,
    FbsStorageDailyUsage,
)
from fbs.signals import storage_usage_captured


@dataclass(frozen=True)
class FbsStorageUsageResult:
    usage_date: date
    agencies: int
    rows: int
    incomplete_dimension_rows: int


def _require_module() -> None:
    if not feature_enabled("module"):
        raise FbsFeatureDisabled("Модуль FBS выключен.")


def _unit_volume_liters(sku) -> Decimal | None:
    if sku is None:
        return None
    dimensions = (sku.length_mm, sku.width_mm, sku.height_mm)
    if not all(value is not None and Decimal(value) > 0 for value in dimensions):
        return None
    return (
        Decimal(dimensions[0])
        * Decimal(dimensions[1])
        * Decimal(dimensions[2])
        / Decimal(1_000_000)
    )


@transaction.atomic
def capture_daily_storage_usage(
    *,
    usage_date: date | None = None,
    agency=None,
) -> FbsStorageUsageResult:
    """Create idempotent physical FBS usage rows without calculating prices."""
    _require_module()
    usage_date = usage_date or timezone.localdate()
    policies = FbsClientStoragePolicy.objects.select_related("agency").filter(is_active=True)
    if agency is not None:
        policies = policies.filter(agency=agency)
    rows = 0
    incomplete = 0
    agency_count = 0
    processed_agencies = []
    for policy in policies:
        policy = FbsClientStoragePolicy.objects.select_for_update().select_related(
            "agency"
        ).get(pk=policy.pk)
        processed_agencies.append(policy.agency)
        FbsStorageDailyUsage.objects.filter(
            usage_date=usage_date,
            agency=policy.agency,
        ).delete()
        agency_count += 1
        if policy.billing_mode == FbsClientStoragePolicy.BILLING_LITERS:
            balances = (
                FbsStockBalance.objects.select_related("sku_ref")
                .filter(
                    agency=policy.agency,
                    qty__gt=0,
                    box__status__in=(FbsBox.STATUS_PLANNED, FbsBox.STATUS_ACTIVE),
                    box__pallet__status__in=(
                        FbsPallet.STATUS_PLANNED,
                        FbsPallet.STATUS_ACTIVE,
                    ),
                )
                .order_by("sku_ref_id", "id")
            )
            totals: dict[int | None, dict[str, object]] = defaultdict(
                lambda: {"qty": 0, "liters": Decimal("0"), "complete": True, "sku": None}
            )
            for balance in balances:
                row = totals[balance.sku_ref_id]
                row["sku"] = balance.sku_ref
                row["qty"] = int(row["qty"]) + int(balance.qty or 0)
                unit_liters = _unit_volume_liters(balance.sku_ref)
                if unit_liters is None:
                    row["complete"] = False
                else:
                    row["liters"] = Decimal(row["liters"]) + unit_liters * int(
                        balance.qty or 0
                    )
            for values in totals.values():
                FbsStorageDailyUsage.objects.create(
                    usage_date=usage_date,
                    agency=policy.agency,
                    billing_mode=policy.billing_mode,
                    sku=values["sku"],
                    pallet=None,
                    quantity=values["qty"],
                    volume_liters=Decimal(values["liters"]).quantize(Decimal("0.001")),
                    pallet_places=Decimal("0"),
                    dimensions_complete=bool(values["complete"]),
                )
                rows += 1
                if not values["complete"]:
                    incomplete += 1
        elif policy.billing_mode == FbsClientStoragePolicy.BILLING_PALLETS:
            pallets = FbsPallet.objects.filter(
                agency=policy.agency,
                status=FbsPallet.STATUS_ACTIVE,
                boxes__stock_balances__qty__gt=0,
            ).distinct()
            for pallet in pallets:
                quantity = int(
                    FbsStockBalance.objects.filter(box__pallet=pallet).aggregate(
                        total=Sum("qty")
                    )["total"]
                    or 0
                )
                FbsStorageDailyUsage.objects.create(
                    usage_date=usage_date,
                    agency=policy.agency,
                    billing_mode=policy.billing_mode,
                    sku=None,
                    pallet=pallet,
                    quantity=quantity,
                    volume_liters=Decimal("0"),
                    pallet_places=Decimal("1.000"),
                    dimensions_complete=True,
                )
                rows += 1
        else:
            raise FbsStorageError("Неизвестный режим тарификации хранения FBS.")
    for processed_agency in processed_agencies:
        storage_usage_captured.send_robust(
            sender=capture_daily_storage_usage,
            agency=processed_agency,
            usage_date=usage_date,
        )
    return FbsStorageUsageResult(
        usage_date=usage_date,
        agencies=agency_count,
        rows=rows,
        incomplete_dimension_rows=incomplete,
    )
