from __future__ import annotations

from datetime import date, datetime, time
from decimal import Decimal

from django.db import transaction
from django.db.models import Q, Sum
from django.utils import timezone

from fbs.exceptions import FbsStorageError
from fbs.models import (
    FbsClientStoragePolicy,
    FbsStorageDailyUsage,
)
from marking.codes import marking_code_identity

from .models import (
    BillingApplication,
    BillingService,
    FbsClientRate,
    WarehouseServiceFact,
)
from .services import BillingWorkflowService


FBS_MOVEMENT_ITEM_SERVICE = "fbs_receiving_goods"
FBS_MOVEMENT_BOX_SERVICE = "fbs_movement_box"
FBS_PICK_ITEM_SERVICE = "fbs_pick_item"
FBS_PICK_ORDER_SERVICE = "fbs_pick_order"
FBS_MARKING_LABEL_SERVICE = "fbs_marking_label_58x40"
FBS_CHZ_CHECK_SERVICE = "fbs_honest_sign_check"
FBS_SHIPPING_ORDER_SERVICE = "fbs_shipping_order"
FBS_SHIPPING_BOX_SERVICE = "fbs_shipping_box"
FBS_DELIVERY_SERVICE = "fbs_delivery"
FBS_STORAGE_LITER_SERVICE = "fbs_storage_liter_day"
FBS_STORAGE_PALLET_SERVICE = "fbs_storage_pallet_day"


def _manager_for_agency(agency):
    from employees.models import Employee

    manager_user_id = getattr(agency, "mened_user_id", None)
    if not manager_user_id:
        return None
    return Employee.objects.filter(user_id=manager_user_id).order_by("id").first()


def _service(code: str) -> BillingService:
    service = BillingService.objects.filter(code=code, is_active=True).first()
    if service is None:
        raise FbsStorageError(f"В биллинге не настроена услуга {code}.")
    return service


def _confirmed_chz_summary(*, order):
    """Return unique final KIZ checks for one order, excluding retries."""
    from fbs.models import FbsOrderStockAllocation, FbsOrderTraceability

    rows = list(
        FbsOrderTraceability.objects.filter(
            allocation__order_item__order=order,
            status=FbsOrderTraceability.STATUS_PICKED,
            allocation__status=FbsOrderStockAllocation.STATUS_PICKED,
        )
        .exclude(marking_code="")
        .values(
            "id",
            "marking_code",
            "updated_at",
            "allocation__order_item_id",
            "allocation__order_item__quantity",
        )
        .order_by("id")
    )
    seen = set()
    traceability_ids = []
    billed_by_item = {}
    performed_at = None
    for row in rows:
        identity = marking_code_identity(row["marking_code"])
        if not identity or identity in seen:
            continue
        seen.add(identity)
        item_id = row["allocation__order_item_id"]
        billed_for_item = billed_by_item.get(item_id, 0)
        if billed_for_item >= row["allocation__order_item__quantity"]:
            continue
        billed_by_item[item_id] = billed_for_item + 1
        traceability_ids.append(row["id"])
        if performed_at is None or row["updated_at"] > performed_at:
            performed_at = row["updated_at"]
    return len(traceability_ids), traceability_ids, performed_at


def _chz_billing_enabled(*, client, on_date: date) -> bool:
    return (
        FbsClientRate.objects.filter(
            client=client,
            operation=FbsClientRate.OP_CHZ_CHECK,
            is_active=True,
            valid_from__lte=on_date,
        )
        .filter(Q(valid_to__isnull=True) | Q(valid_to__gte=on_date))
        .exists()
    )


def _upsert_fact(
    *,
    client,
    order_id: str,
    service_code: str,
    quantity,
    planned_quantity=None,
    unit: str = "",
    source: str,
    application=None,
    user=None,
    performed_at=None,
    metadata=None,
    status=None,
) -> WarehouseServiceFact:
    service = _service(service_code)
    defaults = {
        "service_name_snapshot": service.name,
        "planned_quantity": planned_quantity,
        "quantity": Decimal(str(quantity)),
        "unit": unit or service.unit or "шт",
        "status": status or WarehouseServiceFact.STATUS_SENT_TO_BILLING,
        "source": source,
        "is_manual": False,
        "performed_at": performed_at or timezone.now(),
        "metadata": metadata or {},
        "reported_by": user if getattr(user, "is_authenticated", False) else None,
        "application": application,
        "source_key": f"fbs:{order_id}:{service_code}",
    }
    fact, _ = WarehouseServiceFact.objects.update_or_create(
        client=client,
        order_type=WarehouseServiceFact.ORDER_FBS,
        order_id=order_id,
        service=service,
        defaults=defaults,
    )
    return fact


def _sync_application(
    *,
    client,
    application_id: str,
    operational_status: str,
    operational_status_label: str,
    created_at,
    source_payload: dict,
    user=None,
) -> BillingApplication:
    return BillingWorkflowService.sync_application_from_source(
        application_type=BillingApplication.TYPE_FBS,
        application_id=application_id,
        client=client,
        legal_entity=client,
        manager=_manager_for_agency(client),
        operational_status=operational_status,
        operational_status_label=operational_status_label,
        created_at_source=created_at,
        source_payload={"billing_contour": "fbs", **source_payload},
        user=user,
    )


@transaction.atomic
def sync_fbs_movement_to_billing(*, request_row, user=None):
    from fbs.models import FbsClientMovementRequest

    locked = (
        FbsClientMovementRequest.objects.select_for_update()
        .select_related("agency")
        .get(pk=request_row.pk)
    )
    if locked.status != FbsClientMovementRequest.STATUS_COMPLETED:
        raise FbsStorageError("FBS-перемещение еще не подтверждено складом.")
    order_id = locked.number
    application = _sync_application(
        client=locked.agency,
        application_id=order_id,
        operational_status="completed",
        operational_status_label="FBS-перемещение подтверждено складом",
        created_at=locked.created_at,
        source_payload={
            "source": "fbs_movement",
            "request_id": locked.id,
            "mode": locked.mode,
            "requested_qty": locked.requested_qty,
            "actual_moved_qty": locked.actual_moved_qty,
            "actual_moved_box_count": locked.actual_moved_box_count,
        },
        user=user,
    )
    facts = [
        _upsert_fact(
            client=locked.agency,
            order_id=order_id,
            service_code=FBS_MOVEMENT_ITEM_SERVICE,
            quantity=locked.actual_moved_qty,
            planned_quantity=locked.requested_qty,
            unit="шт",
            source="fbs_movement_auto",
            application=application,
            user=user,
            performed_at=locked.warehouse_confirmed_at,
            metadata={"request_id": locked.id, "mode": locked.mode},
            status=WarehouseServiceFact.STATUS_APPROVED,
        )
    ]
    if locked.mode == FbsClientMovementRequest.MODE_BOX and locked.actual_moved_box_count:
        facts.append(
            _upsert_fact(
                client=locked.agency,
                order_id=order_id,
                service_code=FBS_MOVEMENT_BOX_SERVICE,
                quantity=locked.actual_moved_box_count,
                planned_quantity=locked.requested_box_count,
                unit="кор.",
                source="fbs_movement_auto",
                application=application,
                user=user,
                performed_at=locked.warehouse_confirmed_at,
                metadata={"request_id": locked.id, "mode": locked.mode},
                status=WarehouseServiceFact.STATUS_APPROVED,
            )
        )
    if not application.is_operations_completed:
        BillingWorkflowService.mark_operations_completed(
            application,
            completed_at=timezone.now(),
            user=user,
        )
    return application, facts


@transaction.atomic
def sync_fbs_storage_usage_to_billing(*, agency, usage_date: date):
    rows = list(
        FbsStorageDailyUsage.objects.select_for_update()
        .filter(agency=agency, usage_date=usage_date)
        .order_by("id")
    )
    if not rows:
        return None, []
    modes = {row.billing_mode for row in rows}
    if len(modes) != 1:
        raise FbsStorageError(
            "За один день нельзя одновременно начислять FBS-хранение в литрах и палетах."
        )
    mode = next(iter(modes))
    if mode == FbsClientStoragePolicy.BILLING_LITERS:
        if any(not row.dimensions_complete for row in rows):
            return None, []
        quantity = sum((Decimal(row.volume_liters or 0) for row in rows), Decimal("0"))
        service_code = FBS_STORAGE_LITER_SERVICE
        unit = "л"
    else:
        quantity = sum((Decimal(row.pallet_places or 0) for row in rows), Decimal("0"))
        service_code = FBS_STORAGE_PALLET_SERVICE
        unit = "пал."
    if quantity <= 0:
        return None, []
    order_id = f"FBS-STORAGE-{usage_date.isoformat()}"
    performed_at = timezone.make_aware(
        datetime.combine(usage_date, time.max),
        timezone.get_current_timezone(),
    )
    application = _sync_application(
        client=agency,
        application_id=order_id,
        operational_status="completed",
        operational_status_label="Суточное FBS-хранение рассчитано",
        created_at=performed_at,
        source_payload={
            "source": "fbs_storage_daily_usage",
            "usage_date": usage_date.isoformat(),
            "billing_mode": mode,
            "usage_row_ids": [row.id for row in rows],
        },
    )
    fact = _upsert_fact(
        client=agency,
        order_id=order_id,
        service_code=service_code,
        quantity=quantity,
        planned_quantity=quantity,
        unit=unit,
        source="fbs_storage_auto",
        application=application,
        performed_at=performed_at,
        metadata={"usage_date": usage_date.isoformat(), "billing_mode": mode},
    )
    if not application.is_operations_completed:
        BillingWorkflowService.mark_operations_completed(
            application,
            completed_at=performed_at,
        )
    return application, [fact]


@transaction.atomic
def sync_fbs_order_to_billing(*, order, user=None):
    from fbs.models import FbsHandoverOrder, FbsOrder, FbsOrderLabel

    order = FbsOrder.objects.select_related("profile__agency").get(pk=order.pk)
    picked_statuses = {
        FbsOrder.STATUS_PICKED,
        FbsOrder.STATUS_READY_FOR_HANDOVER,
        FbsOrder.STATUS_HANDED_OVER,
        FbsOrder.STATUS_DELIVERED,
    }
    shipped_statuses = {FbsOrder.STATUS_HANDED_OVER, FbsOrder.STATUS_DELIVERED}
    if order.internal_status not in picked_statuses:
        return None, []
    client = order.profile.agency
    order_id = f"FBS-ORDER-{order.id}"
    item_qty = int(order.items.aggregate(total=Sum("quantity"))["total"] or 0)
    application = _sync_application(
        client=client,
        application_id=order_id,
        operational_status=order.internal_status,
        operational_status_label=order.get_internal_status_display(),
        created_at=order.imported_at,
        source_payload={
            "source": "fbs_order",
            "fbs_order_id": order.id,
            "external_order_id": order.external_order_id,
            "marketplace": order.profile.marketplace,
        },
        user=user,
    )
    facts = [
        _upsert_fact(
            client=client,
            order_id=order_id,
            service_code=FBS_PICK_ITEM_SERVICE,
            quantity=item_qty,
            planned_quantity=item_qty,
            unit="шт",
            source="fbs_order_auto",
            application=application,
            user=user,
            metadata={"fbs_order_id": order.id},
        ),
        _upsert_fact(
            client=client,
            order_id=order_id,
            service_code=FBS_PICK_ORDER_SERVICE,
            quantity=1,
            planned_quantity=1,
            unit="заказ",
            source="fbs_order_auto",
            application=application,
            user=user,
            metadata={"fbs_order_id": order.id},
        ),
    ]
    applied_label = (
        FbsOrderLabel.objects.filter(
            order=order,
            status=FbsOrderLabel.STATUS_APPLIED,
        )
        .order_by("-applied_at", "-id")
        .first()
    )
    if applied_label is not None:
        facts.append(
            _upsert_fact(
                client=client,
                order_id=order_id,
                service_code=FBS_MARKING_LABEL_SERVICE,
                quantity=1,
                planned_quantity=1,
                unit="шт",
                source="fbs_order_label_auto",
                application=application,
                user=user,
                performed_at=applied_label.applied_at or timezone.now(),
                metadata={
                    "fbs_order_id": order.id,
                    "fbs_order_label_id": applied_label.id,
                    "label_format": applied_label.label_format,
                },
            )
        )
    if FbsClientRate.objects.filter(
        client=client,
        operation=FbsClientRate.OP_CHZ_CHECK,
        is_active=True,
    ).exists():
        chz_quantity, traceability_ids, chz_performed_at = _confirmed_chz_summary(
            order=order
        )
        chz_on_date = (
            timezone.localtime(chz_performed_at).date()
            if chz_performed_at is not None and timezone.is_aware(chz_performed_at)
            else (
                chz_performed_at.date()
                if chz_performed_at is not None
                else timezone.localdate()
            )
        )
        if chz_quantity and _chz_billing_enabled(client=client, on_date=chz_on_date):
            facts.append(
                _upsert_fact(
                    client=client,
                    order_id=order_id,
                    service_code=FBS_CHZ_CHECK_SERVICE,
                    quantity=chz_quantity,
                    planned_quantity=chz_quantity,
                    unit="шт",
                    source="fbs_order_chz_auto",
                    application=application,
                    user=user,
                    performed_at=chz_performed_at or timezone.now(),
                    metadata={
                        "fbs_order_id": order.id,
                        "traceability_ids": traceability_ids,
                        "deduplication": "order_and_marking_code_identity",
                    },
                )
            )
    if order.internal_status in shipped_statuses:
        box_count = (
            FbsHandoverOrder.objects.filter(order=order)
            .values("box_id")
            .distinct()
            .count()
        )
        facts.append(
            _upsert_fact(
                client=client,
                order_id=order_id,
                service_code=FBS_SHIPPING_ORDER_SERVICE,
                quantity=1,
                planned_quantity=1,
                unit="заказ",
                source="fbs_order_auto",
                application=application,
                user=user,
                metadata={"fbs_order_id": order.id},
            )
        )
        if box_count:
            facts.append(
                _upsert_fact(
                    client=client,
                    order_id=order_id,
                    service_code=FBS_SHIPPING_BOX_SERVICE,
                    quantity=box_count,
                    planned_quantity=box_count,
                    unit="кор.",
                    source="fbs_order_auto",
                    application=application,
                    user=user,
                    metadata={"fbs_order_id": order.id},
                )
            )
        if not application.is_operations_completed:
            BillingWorkflowService.mark_operations_completed(
                application,
                completed_at=timezone.now(),
                user=user,
            )
    return application, facts


@transaction.atomic
def sync_fbs_delivery_to_billing(*, batch, user=None):
    from fbs.models import FbsHandoverBatch, FbsHandoverOrder

    batch = (
        FbsHandoverBatch.objects.select_for_update()
        .select_related("profile__agency")
        .get(pk=batch.pk)
    )
    if batch.status != FbsHandoverBatch.STATUS_ACCEPTED:
        raise FbsStorageError("FBS-поставка еще не принята маркетплейсом.")
    client = batch.profile.agency
    order_id = f"FBS-DELIVERY-{batch.id}"
    accepted_at = batch.accepted_at or timezone.now()
    application = _sync_application(
        client=client,
        application_id=order_id,
        operational_status="accepted",
        operational_status_label="FBS-поставка принята маркетплейсом",
        created_at=batch.created_at,
        source_payload={
            "source": "fbs_handover_batch",
            "handover_batch_id": batch.id,
            "profile_id": batch.profile_id,
            "marketplace": batch.profile.marketplace,
            "external_supply_id": batch.external_supply_id,
            "box_count": batch.boxes.count(),
            "order_count": FbsHandoverOrder.objects.filter(box__batch=batch).count(),
        },
        user=user,
    )
    fact = _upsert_fact(
        client=client,
        order_id=order_id,
        service_code=FBS_DELIVERY_SERVICE,
        quantity=1,
        planned_quantity=1,
        unit="рейс",
        source="fbs_delivery_auto",
        application=application,
        user=user,
        performed_at=accepted_at,
        metadata={
            "handover_batch_id": batch.id,
            "external_supply_id": batch.external_supply_id,
        },
        status=WarehouseServiceFact.STATUS_APPROVED,
    )
    if not application.is_operations_completed:
        BillingWorkflowService.mark_operations_completed(
            application,
            completed_at=accepted_at,
            user=user,
        )
    return application, [fact]
