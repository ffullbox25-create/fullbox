from __future__ import annotations

from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.db.models import Max, Min, Sum
from django.utils import timezone

from billing.models import BillingStorageDay
from sku.models import Agency
from wms_new.models import (
    WmsNewBillingItem,
    WmsNewEvent,
    WmsNewInvoice,
    WmsNewPartnerProfile,
    WmsNewPrimaryDocument,
    WmsNewShipment,
    WmsNewTask,
)


class PartnerBillingOperationError(ValueError):
    pass


def _money(value) -> Decimal:
    try:
        return Decimal(str(value or 0)).quantize(Decimal("0.01"))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise PartnerBillingOperationError("Некорректная сумма.") from exc


def _event(instance, action: str, *, actor=None, before=None, metadata=None):
    WmsNewEvent.objects.create(
        entity_type=instance._meta.model_name,
        entity_id=instance.pk,
        action=action,
        actor=actor,
        before=before or {},
        after={
            "status": getattr(instance, "status", ""),
            "pilot_revision": getattr(instance, "pilot_revision", 0),
        },
        metadata=metadata or {},
    )


def ensure_partner_profiles():
    """Mirror partner identity/capabilities into the isolated contour, never back."""

    profiles = []
    for agency in Agency.objects.filter(archived=False).prefetch_related("market_credentials__market"):
        market_names = {
            str(item.market.name or "").lower()
            for item in agency.market_credentials.all()
            if str(item.market_key or "").strip()
        }
        wb = any("wild" in name or name == "wb" for name in market_names)
        ozon = any("ozon" in name or "озон" in name for name in market_names)
        defaults = {
            "name": str(agency),
            "is_active": True,
            "wb_products_enabled": wb,
            "wb_orders_enabled": wb,
            "ozon_products_enabled": ozon,
            "ozon_orders_enabled": ozon,
            "requisites": " · ".join(
                value for value in (agency.inn and f"ИНН {agency.inn}", agency.kpp and f"КПП {agency.kpp}") if value
            ),
        }
        profile, created = WmsNewPartnerProfile.objects.get_or_create(
            source_agency=agency,
            defaults=defaults,
        )
        if not created and profile.pilot_revision == 0:
            changed = []
            for field, value in defaults.items():
                if getattr(profile, field) != value:
                    setattr(profile, field, value)
                    changed.append(field)
            if changed:
                profile.save(update_fields=(*changed, "updated_at"))
        profiles.append(profile)
    return profiles


@transaction.atomic
def create_partner(*, name: str, actor=None) -> WmsNewPartnerProfile:
    value = str(name or "").strip()
    if not value:
        raise PartnerBillingOperationError("Укажите название нового партнера.")
    if WmsNewPartnerProfile.objects.filter(name__iexact=value, is_active=True).exists():
        raise PartnerBillingOperationError("Партнер с таким названием уже существует в FBS-NEW.")
    profile = WmsNewPartnerProfile.objects.create(
        name=value,
        is_manual=True,
        pilot_revision=1,
        created_by=actor,
    )
    _event(profile, "partner_created", actor=actor)
    return profile


@transaction.atomic
def update_partner(*, profile_id: int, actor=None, **values) -> WmsNewPartnerProfile:
    profile = WmsNewPartnerProfile.objects.select_for_update().get(pk=profile_id)
    before = {
        "name": profile.name,
        "balance": str(profile.balance),
        "is_active": profile.is_active,
    }
    name = str(values.get("name") or "").strip()
    if not name:
        raise PartnerBillingOperationError("Название партнера не может быть пустым.")
    profile.name = name
    profile.balance = _money(values.get("balance"))
    profile.requisites = str(values.get("requisites") or "").strip()[:255]
    for field in (
        "is_active",
        "wb_products_enabled",
        "wb_orders_enabled",
        "ozon_products_enabled",
        "ozon_orders_enabled",
    ):
        setattr(profile, field, bool(values.get(field)))
    profile.pilot_revision += 1
    profile.save()
    _event(profile, "partner_updated", actor=actor, before=before)
    return profile


def _shipment_amount(shipment: WmsNewShipment) -> Decimal:
    return sum((item.total for item in shipment.services.all()), Decimal("0.00"))


def ensure_task_item(task: WmsNewTask, *, actor=None) -> WmsNewBillingItem:
    if task.agency_id is None:
        raise PartnerBillingOperationError("У задачи не указан партнер.")
    item, _ = WmsNewBillingItem.objects.get_or_create(
        kind=WmsNewBillingItem.KIND_TASK,
        source_key=str(task.pk),
        defaults={
            "agency": task.agency,
            "source_task": task,
            "title": task.title,
            "occurred_at": task.updated_at,
            "quantity": 1,
            "amount": 0,
            "created_by": actor,
            "source_snapshot": {"workflow_type": task.workflow_type, "source_task_id": task.source_task_id},
        },
    )
    return item


def ensure_fbs_item(shipment: WmsNewShipment, *, actor=None) -> WmsNewBillingItem:
    amount = _shipment_amount(shipment)
    item, created = WmsNewBillingItem.objects.get_or_create(
        kind=WmsNewBillingItem.KIND_FBS,
        source_key=str(shipment.pk),
        defaults={
            "agency": shipment.agency,
            "source_shipment": shipment,
            "title": f"Отгрузка №{shipment.source_batch_id or shipment.pk}",
            "occurred_at": shipment.dispatched_at or shipment.source_created_at or shipment.created_at,
            "marketplace": shipment.delivery_type,
            "integration": shipment.integration_name,
            "quantity": shipment.item_count or 1,
            "amount": amount,
            "unit_price": amount / Decimal(shipment.item_count or 1),
            "created_by": actor,
            "source_snapshot": {"source_batch_id": shipment.source_batch_id},
        },
    )
    if not created and item.pilot_revision == 0 and item.status == WmsNewBillingItem.STATUS_UNPRICED:
        item.amount = amount
        item.unit_price = amount / Decimal(shipment.item_count or 1)
        item.save(update_fields=("amount", "unit_price", "updated_at"))
    return item


def ensure_storage_items(*, actor=None):
    """Clone storage facts into independent billing rows; legacy rows remain read-only."""

    aggregates = (
        BillingStorageDay.objects.exclude(status=BillingStorageDay.STATUS_CANCELLED)
        .values("client_id")
        .annotate(period_start=Min("day"), period_end=Max("day"), amount=Sum("amount"))
    )
    result = []
    agencies = Agency.objects.in_bulk(item["client_id"] for item in aggregates)
    for row in aggregates:
        agency = agencies.get(row["client_id"])
        if not agency or not row["period_start"] or not row["period_end"]:
            continue
        source_key = f"{agency.pk}:{row['period_start']}:{row['period_end']}"
        defaults = {
            "agency": agency,
            "title": "По объему",
            "period_start": row["period_start"],
            "period_end": row["period_end"],
            "quantity": 1,
            "amount": _money(row["amount"]),
            "unit_price": _money(row["amount"]),
            "status": WmsNewBillingItem.STATUS_PRICED,
            "created_by": actor,
            "source_snapshot": {"source": "billing_storage_day_read_only"},
        }
        item, created = WmsNewBillingItem.objects.get_or_create(
            kind=WmsNewBillingItem.KIND_STORAGE,
            source_key=source_key,
            defaults=defaults,
        )
        if not created and item.pilot_revision == 0 and item.status == WmsNewBillingItem.STATUS_PRICED:
            item.amount = defaults["amount"]
            item.unit_price = defaults["unit_price"]
            item.save(update_fields=("amount", "unit_price", "updated_at"))
        result.append(item)
    return result


@transaction.atomic
def update_billing_sources(*, kind: str, source_ids, action: str, actor=None) -> int:
    ids = {int(value) for value in source_ids if str(value).isdigit()}
    if not ids:
        raise PartnerBillingOperationError("Выберите строки.")
    if kind == WmsNewBillingItem.KIND_TASK:
        sources = WmsNewTask.objects.select_related("agency").filter(pk__in=ids)
        items = [ensure_task_item(source, actor=actor) for source in sources]
    elif kind == WmsNewBillingItem.KIND_FBS:
        sources = WmsNewShipment.objects.select_related("agency").prefetch_related("services").filter(pk__in=ids)
        items = [ensure_fbs_item(source, actor=actor) for source in sources]
    else:
        items = list(WmsNewBillingItem.objects.filter(kind=kind, pk__in=ids))
    if not items:
        raise PartnerBillingOperationError("Выбранные строки не найдены.")
    if action == "split":
        if kind != WmsNewBillingItem.KIND_STORAGE or len(items) != 1:
            raise PartnerBillingOperationError("Для разделения выберите одну строку хранения.")
        item = items[0]
        if not item.period_start or not item.period_end or item.period_start >= item.period_end:
            raise PartnerBillingOperationError("Период хранения нельзя разделить.")
        days = (item.period_end - item.period_start).days
        split_day = item.period_start + timedelta(days=max(1, days // 2))
        original_end = item.period_end
        second_amount = (item.amount / Decimal("2")).quantize(Decimal("0.01"))
        first_amount = item.amount - second_amount
        before = {"period_end": str(item.period_end), "amount": str(item.amount)}
        item.period_end = split_day
        item.amount = first_amount
        item.unit_price = first_amount
        item.pilot_revision += 1
        item.save(update_fields=("period_end", "amount", "unit_price", "pilot_revision", "updated_at"))
        second = WmsNewBillingItem.objects.create(
            agency=item.agency,
            kind=item.kind,
            source_key=f"{item.source_key}:split:{item.pk}:{item.pilot_revision}",
            title=item.title,
            period_start=split_day + timedelta(days=1),
            period_end=original_end,
            quantity=1,
            unit_price=second_amount,
            amount=second_amount,
            status=item.status,
            source_snapshot={**item.source_snapshot, "split_from": item.pk},
            pilot_revision=1,
            created_by=actor,
        )
        _event(item, "billing_split", actor=actor, before=before, metadata={"new_item_id": second.pk})
        return 2
    status_by_action = {
        "price": WmsNewBillingItem.STATUS_PRICED,
        "archive": WmsNewBillingItem.STATUS_ARCHIVED,
        "restore": (
            WmsNewBillingItem.STATUS_PRICED
            if kind == WmsNewBillingItem.KIND_STORAGE
            else WmsNewBillingItem.STATUS_UNPRICED
        ),
    }
    target = status_by_action.get(action)
    if not target:
        raise PartnerBillingOperationError("Неизвестное действие тарификации.")
    changed = 0
    for item in items:
        if item.invoice_id and target != WmsNewBillingItem.STATUS_INVOICED:
            continue
        before = {"status": item.status}
        item.status = target
        item.pilot_revision += 1
        item.save(update_fields=("status", "pilot_revision", "updated_at"))
        _event(item, f"billing_{action}", actor=actor, before=before)
        changed += 1
    return changed


@transaction.atomic
def create_invoice(*, agency: Agency, invoice_type: str, requisites: str = "", amount=None, actor=None) -> WmsNewInvoice:
    if invoice_type not in dict(WmsNewInvoice.TYPE_CHOICES):
        raise PartnerBillingOperationError("Некорректный тип счета.")
    ensure_storage_items(actor=actor)
    invoice_date = timezone.localdate()
    due_date = invoice_date + timedelta(days=10)
    invoice = WmsNewInvoice.objects.create(
        agency=agency,
        number=f"FBN-TMP-{timezone.now().timestamp()}",
        invoice_type=invoice_type,
        requisites=str(requisites or "").strip()[:255],
        invoice_date=invoice_date,
        due_date=due_date,
        created_by=actor,
    )
    invoice.number = f"FBN-{invoice_date:%Y%m}-{invoice.pk:05d}"
    if invoice_type == WmsNewInvoice.TYPE_SERVICES:
        items = list(
            WmsNewBillingItem.objects.select_for_update().filter(
                agency=agency,
                status=WmsNewBillingItem.STATUS_PRICED,
                invoice__isnull=True,
            )
        )
        invoice.total_amount = sum((item.amount for item in items), Decimal("0.00"))
        for item in items:
            item.invoice = invoice
            item.status = WmsNewBillingItem.STATUS_INVOICED
            item.pilot_revision += 1
            item.save(update_fields=("invoice", "status", "pilot_revision", "updated_at"))
    else:
        invoice.total_amount = _money(amount)
        if invoice.total_amount <= 0:
            raise PartnerBillingOperationError("Для пополнения укажите сумму больше нуля.")
    invoice.save(update_fields=("number", "total_amount", "updated_at"))
    if invoice_type == WmsNewInvoice.TYPE_SERVICES:
        period = invoice_date.replace(day=1)
        WmsNewPrimaryDocument.objects.create(
            agency=agency,
            invoice=invoice,
            number=f"ACT-FBN-{invoice.pk:05d}",
            document_type=WmsNewPrimaryDocument.TYPE_ACT,
            period=period,
            amount=invoice.total_amount,
            status=WmsNewPrimaryDocument.STATUS_READY,
            created_by=actor,
            source_snapshot={"billing_item_ids": [item.pk for item in items]},
        )
    _event(invoice, "invoice_created", actor=actor, metadata={"type": invoice_type})
    return invoice


@transaction.atomic
def update_invoice(*, invoice_id: int, action: str, actor=None, amount=None) -> WmsNewInvoice:
    invoice = WmsNewInvoice.objects.select_for_update().select_related("agency").get(pk=invoice_id)
    before = {"status": invoice.status, "paid_amount": str(invoice.paid_amount)}
    if action == "ready":
        if invoice.status == WmsNewInvoice.STATUS_CANCELLED:
            raise PartnerBillingOperationError("Отмененный счет нельзя сделать готовым.")
        invoice.status = WmsNewInvoice.STATUS_READY
    elif action == "payment":
        value = _money(amount)
        if value <= 0:
            raise PartnerBillingOperationError("Сумма оплаты должна быть больше нуля.")
        invoice.paid_amount += value
        if invoice.invoice_type == WmsNewInvoice.TYPE_BALANCE:
            profile = WmsNewPartnerProfile.objects.select_for_update().filter(source_agency=invoice.agency).first()
            if profile:
                profile.balance += value
                profile.pilot_revision += 1
                profile.save(update_fields=("balance", "pilot_revision", "updated_at"))
    elif action == "cancel":
        if invoice.paid_amount:
            raise PartnerBillingOperationError("Нельзя отменить счет с зарегистрированной оплатой.")
        invoice.status = WmsNewInvoice.STATUS_CANCELLED
        for item in invoice.items.select_for_update():
            item.invoice = None
            item.status = WmsNewBillingItem.STATUS_PRICED
            item.pilot_revision += 1
            item.save(update_fields=("invoice", "status", "pilot_revision", "updated_at"))
        invoice.primary_documents.update(status=WmsNewPrimaryDocument.STATUS_DRAFT)
    else:
        raise PartnerBillingOperationError("Неизвестное действие со счетом.")
    invoice.pilot_revision += 1
    invoice.save(update_fields=("status", "paid_amount", "pilot_revision", "updated_at"))
    _event(invoice, f"invoice_{action}", actor=actor, before=before)
    return invoice


@transaction.atomic
def create_invoice_from_document(*, document_id: int, actor=None) -> WmsNewInvoice:
    document = WmsNewPrimaryDocument.objects.select_for_update().select_related("agency").get(pk=document_id)
    if document.invoice_id:
        return document.invoice
    invoice_date = timezone.localdate()
    invoice = WmsNewInvoice.objects.create(
        agency=document.agency,
        number=f"FBN-TMP-{timezone.now().timestamp()}",
        invoice_type=WmsNewInvoice.TYPE_SERVICES,
        invoice_date=invoice_date,
        due_date=invoice_date + timedelta(days=10),
        total_amount=document.amount,
        created_by=actor,
    )
    invoice.number = f"FBN-{invoice_date:%Y%m}-{invoice.pk:05d}"
    invoice.save(update_fields=("number", "updated_at"))
    document.invoice = invoice
    document.save(update_fields=("invoice", "updated_at"))
    _event(invoice, "invoice_created_from_primary_document", actor=actor, metadata={"document_id": document.pk})
    return invoice
