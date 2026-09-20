"""Запросы менеджера на дополнительные услуги (этап 4)."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_date

from sku.models import Agency

from .charge_status import NO_PRICE_MESSAGE
from .manager_billing import can_access_client
from .models import ApplicationCharge, BillingApplication, BillingService, ExtraServiceRequest
from .permissions import filter_agencies_for_user, get_employee
from .price_resolver import resolve_client_service_price
from .services import BillingWorkflowService
from .statuses import BillingStatus


def _parse_service_date(value):
    if hasattr(value, "year"):
        return value
    parsed = parse_date(str(value or "").strip())
    if not parsed:
        raise ValidationError("Укажите дату оказания услуги.")
    return parsed


def _ensure_application(client: Agency, *, user=None, warehouse_label: str = "") -> BillingApplication:
    """Отдельная заявка TYPE_OTHER для доп. услуг клиента в текущем месяце."""
    today = timezone.localdate()
    app_id = f"EXTRA-{client.id}-{today.strftime('%Y%m')}"
    employee = get_employee(user) if user else None
    application, _ = BillingApplication.objects.get_or_create(
        application_type=BillingApplication.TYPE_OTHER,
        application_id=app_id,
        client=client,
        defaults={
            "legal_entity": client,
            "manager": employee if employee and getattr(employee, "role", "") == "manager" else None,
            "warehouse_label": warehouse_label or "",
            "operational_status": "done",
            "operational_status_label": "Доп. услуги",
            "is_operations_completed": True,
            "operations_completed_at": timezone.now(),
            "billing_status": BillingStatus.CALCULATION_DRAFT,
            "created_at_source": timezone.now(),
        },
    )
    return application


@transaction.atomic
def create_extra_service_request(
    *,
    user,
    client: Agency,
    service: BillingService,
    quantity,
    service_date,
    application: BillingApplication | None = None,
    unit: str = "",
    warehouse_label: str = "",
    description: str = "",
    reason: str = "",
    performer: str = "",
    internal_comment: str = "",
    client_comment: str = "",
    attachment=None,
) -> ExtraServiceRequest:
    if not can_access_client(user, client):
        raise ValidationError("Нет доступа к клиенту.")
    qty = Decimal(str(quantity or "0"))
    if qty <= 0:
        raise ValidationError("Количество должно быть больше нуля.")
    day = _parse_service_date(service_date)
    if application is None:
        application = _ensure_application(client, user=user, warehouse_label=warehouse_label)
    elif application.client_id != client.id:
        raise ValidationError("Заявка принадлежит другому клиенту.")

    req = ExtraServiceRequest(
        client=client,
        application=application,
        service=service,
        service_date=day,
        quantity=qty,
        unit=(unit or service.unit or "шт")[:32],
        warehouse_label=warehouse_label or application.warehouse_label or "",
        description=description or "",
        reason=reason or "",
        performer=performer or "",
        internal_comment=internal_comment or "",
        client_comment=client_comment or "",
        created_by=user if getattr(user, "is_authenticated", False) else None,
        status=ExtraServiceRequest.STATUS_NEEDS_PRICE,
    )
    if attachment is not None:
        req.attachment = attachment
    req.save()
    return try_materialize_extra_service_request(req, user=user)


@transaction.atomic
def try_materialize_extra_service_request(req: ExtraServiceRequest, *, user=None) -> ExtraServiceRequest:
    """Создать ApplicationCharge, если есть договорная цена; иначе needs_price."""
    if req.status == ExtraServiceRequest.STATUS_CANCELLED:
        return req
    if req.charge_id:
        req.status = ExtraServiceRequest.STATUS_CHARGED
        req.save(update_fields=["status", "updated_at"])
        return req

    application = req.application
    if application is None:
        application = _ensure_application(req.client, user=user, warehouse_label=req.warehouse_label)
        req.application = application
        req.save(update_fields=["application", "updated_at"])

    performed_at = timezone.make_aware(datetime.combine(req.service_date, datetime.min.time()))
    resolved = resolve_client_service_price(
        application,
        req.service,
        performed_at=performed_at,
        quantity=req.quantity,
    )
    if not resolved.ok or resolved.tariff is None:
        req.status = ExtraServiceRequest.STATUS_NEEDS_PRICE
        req.price_note = resolved.note or NO_PRICE_MESSAGE
        req.save(update_fields=["status", "price_note", "updated_at"])
        return req

    comment_parts = [p for p in (req.description, req.reason, req.client_comment) if p]
    charge = BillingWorkflowService.create_or_update_charge(
        application,
        service=req.service,
        quantity=req.quantity,
        unit=req.unit or resolved.unit or req.service.unit,
        source_type=ApplicationCharge.SOURCE_MANUAL,
        source_id=f"extra-req:{req.pk}",
        source_key=f"extra-req:{req.pk}",
        performed_at=performed_at,
        billing_period=req.service_date.replace(day=1),
        comment="\n".join(comment_parts),
        user=user,
        resolve_from_agreed_tariff=True,
    )
    req.charge = charge
    req.status = ExtraServiceRequest.STATUS_CHARGED
    req.price_note = resolved.tariff_source_label or ""
    req.save(update_fields=["charge", "status", "price_note", "updated_at"])
    if application.billing_status in {BillingStatus.NOT_CALCULATED, BillingStatus.CALCULATION_DRAFT}:
        application.billing_status = BillingStatus.CALCULATED
        application.save(update_fields=["billing_status", "updated_at"])
    BillingWorkflowService.audit(
        action="extra_service_charged",
        application=application,
        user=user,
        obj=charge,
        new_value={"request_id": req.id, "total_amount": str(charge.total_amount)},
    )
    return req


def cancel_extra_service_request(req: ExtraServiceRequest, *, user=None) -> ExtraServiceRequest:
    if req.status == ExtraServiceRequest.STATUS_CHARGED and req.charge_id:
        charge = req.charge
        if charge and (charge.is_included_in_act or charge.is_included_in_invoice):
            raise ValidationError("Нельзя отменить: начисление уже в акте или счёте.")
        if charge:
            application = charge.application
            charge.delete()
            BillingWorkflowService.recalculate_application_totals(application)
    req.status = ExtraServiceRequest.STATUS_CANCELLED
    req.charge = None
    req.save(update_fields=["status", "charge", "updated_at"])
    return req


def list_extra_service_requests(user_or_request, *, status: str = "", client_id: str = ""):
    portfolio = filter_agencies_for_user(Agency.objects.all(), user_or_request)
    qs = (
        ExtraServiceRequest.objects.filter(client__in=portfolio)
        .select_related("client", "service", "application", "charge", "created_by")
        .order_by("-service_date", "-id")
    )
    if status:
        qs = qs.filter(status=status)
    if client_id:
        qs = qs.filter(client_id=client_id)
    return qs
