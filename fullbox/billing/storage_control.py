"""Контроль биллинга хранения: периоды, корректировки, ошибки, выгрузки."""
from __future__ import annotations

import csv
import io
from datetime import date
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from sku.models import Agency

from .models import (
    ApplicationCharge,
    BillingStorageDay,
    StorageAdjustment,
    StorageBillingError,
    StorageBillingPeriod,
)
from .services import BillingWorkflowService
from .storage_billing import StorageBillingService
from .storage_engine import money_quantize


def get_or_open_period(client: Agency, *, year: int, month: int) -> StorageBillingPeriod:
    period, _ = StorageBillingPeriod.objects.get_or_create(
        client=client,
        legal_entity=client,
        year=year,
        month=month,
        defaults={"status": StorageBillingPeriod.STATUS_OPEN},
    )
    return period


def assert_period_editable(period: StorageBillingPeriod) -> None:
    if period.status == StorageBillingPeriod.STATUS_CLOSED:
        raise ValidationError("Период закрыт — изменения запрещены.")


@transaction.atomic
def close_storage_period(period: StorageBillingPeriod, *, user=None, checklist: dict | None = None) -> StorageBillingPeriod:
    if period.status == StorageBillingPeriod.STATUS_CLOSED:
        return period
    open_errors = StorageBillingError.objects.filter(
        client=period.client,
        day__year=period.year,
        day__month=period.month,
        resolved_at__isnull=True,
        severity=StorageBillingError.SEVERITY_ERROR,
    ).count()
    pending_adj = StorageAdjustment.objects.filter(
        client=period.client,
        period=period,
        status__in=[StorageAdjustment.STATUS_DRAFT, StorageAdjustment.STATUS_PENDING],
    ).count()
    flags = dict(checklist or period.checklist or {})
    flags.setdefault("errors_reviewed", open_errors == 0)
    flags.setdefault("adjustments_done", pending_adj == 0)
    if not flags.get("force") and (open_errors or pending_adj):
        raise ValidationError(
            f"Нельзя закрыть период: открытых ошибок={open_errors}, незакрытых корректировок={pending_adj}."
        )
    totals = BillingStorageDay.objects.filter(
        client=period.client, day__year=period.year, day__month=period.month
    ).aggregate(amount=Sum("amount"), vat=Sum("vat_amount"))
    period.total_amount = money_quantize(totals.get("amount") or 0)
    period.total_vat = money_quantize(totals.get("vat") or 0)
    period.checklist = flags
    period.status = StorageBillingPeriod.STATUS_CLOSED
    period.closed_by = user if getattr(user, "is_authenticated", False) else None
    period.closed_at = timezone.now()
    period.save()
    notify_storage_event(
        period.client,
        title="Закрыт период хранения",
        body=f"Период {period.year}-{period.month:02d} закрыт. Сумма без НДС: {period.total_amount}.",
    )
    return period


@transaction.atomic
def reopen_storage_period(period: StorageBillingPeriod, *, user=None) -> StorageBillingPeriod:
    period.status = StorageBillingPeriod.STATUS_OPEN
    period.closed_by = None
    period.closed_at = None
    checklist = dict(period.checklist or {})
    checklist["reopened_at"] = timezone.now().isoformat()
    checklist["reopened_by"] = getattr(user, "id", None)
    period.checklist = checklist
    period.save()
    return period


@transaction.atomic
def create_adjustment(
    *,
    client: Agency,
    year: int,
    month: int,
    delta_amount: Decimal,
    reason: str,
    comment: str = "",
    delta_volume_l: Decimal | None = None,
    source_day: BillingStorageDay | None = None,
    user=None,
) -> StorageAdjustment:
    period = get_or_open_period(client, year=year, month=month)
    assert_period_editable(period)
    return StorageAdjustment.objects.create(
        client=client,
        period=period,
        source_day=source_day,
        source_charge=source_day.charge if source_day else None,
        delta_volume_l=delta_volume_l or Decimal("0"),
        delta_amount=money_quantize(delta_amount),
        reason=reason or StorageAdjustment.REASON_OTHER,
        comment=comment or "",
        status=StorageAdjustment.STATUS_PENDING,
        created_by=user if getattr(user, "is_authenticated", False) else None,
    )


@transaction.atomic
def approve_adjustment(adj: StorageAdjustment, *, user=None, apply_charge: bool = True) -> StorageAdjustment:
    if adj.status not in {StorageAdjustment.STATUS_PENDING, StorageAdjustment.STATUS_DRAFT}:
        raise ValidationError("Корректировку нельзя согласовать в текущем статусе.")
    if adj.period_id:
        assert_period_editable(adj.period)
    adj.status = StorageAdjustment.STATUS_APPROVED
    adj.approved_by = user if getattr(user, "is_authenticated", False) else None
    adj.approved_at = timezone.now()
    adj.save()
    if apply_charge and adj.delta_amount != 0:
        day = adj.source_day.day if adj.source_day_id else date(adj.period.year, adj.period.month, 1)
        application = StorageBillingService.ensure_month_application(
            adj.client, year=day.year, month=day.month, user=user
        )
        from .storage_billing import ensure_storage_service

        service = ensure_storage_service("storage_pallet_day")
        charge = BillingWorkflowService.create_or_update_charge(
            application,
            service=service,
            quantity=Decimal("1"),
            tariff=adj.delta_amount,
            unit="корр.",
            source_type=ApplicationCharge.SOURCE_MANUAL,
            source_id=f"adj-{adj.id}",
            source_key=f"storage-adj:{adj.id}",
            performed_at=timezone.now(),
            billing_period=date(day.year, day.month, 1),
            operation_type="storage_adjustment",
            operation_id=str(adj.id),
            comment=f"Корректировка хранения #{adj.id}: {adj.get_reason_display()}. {adj.comment}",
            user=user,
            resolve_from_agreed_tariff=False,
            is_manual_override=True,
            override_reason=adj.comment or adj.get_reason_display(),
        )
        if adj.source_charge_id:
            charge.correction_of = adj.source_charge
            charge.save(update_fields=["correction_of", "updated_at"])
        adj.source_charge = charge
        adj.status = StorageAdjustment.STATUS_APPLIED
        adj.save(update_fields=["source_charge", "status", "updated_at"])
    return adj


def resolve_error(err: StorageBillingError, *, user=None) -> StorageBillingError:
    err.resolved_at = timezone.now()
    err.resolved_by = user if getattr(user, "is_authenticated", False) else None
    err.save(update_fields=["resolved_at", "resolved_by"])
    return err


def storage_days_for_client(client: Agency, *, year: int, month: int):
    return (
        BillingStorageDay.objects.filter(client=client, day__year=year, day__month=month)
        .select_related("charge", "calculation_rule", "tariff_version")
        .prefetch_related("lines")
        .order_by("day")
    )


def export_storage_days_csv(client: Agency, *, year: int, month: int) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=";")
    writer.writerow(
        [
            "date",
            "billing_mode",
            "pallet_count",
            "box_count",
            "physical_volume_l",
            "billable_volume_l",
            "billable_volume_m3",
            "amount",
            "vat_amount",
            "status",
        ]
    )
    for row in storage_days_for_client(client, year=year, month=month):
        writer.writerow(
            [
                row.day.isoformat(),
                row.billing_mode,
                row.pallet_count,
                row.box_count,
                row.physical_volume_l,
                row.billable_volume_l,
                row.billable_volume_m3,
                row.amount,
                row.vat_amount,
                row.status,
            ]
        )
    return buf.getvalue()


def notify_storage_event(client: Agency, *, title: str, body: str) -> None:
    """Best-effort уведомление в ЛК клиента."""
    try:
        from client_cabinet.messaging_lk import create_notification

        create_notification(agency=client, title=title, text=body, source_key=f"storage:{title[:40]}")
    except Exception:
        pass
