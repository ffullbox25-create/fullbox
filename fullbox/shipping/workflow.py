from __future__ import annotations

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from audit.models import OrderAuditEntry, log_order_action
from client_cabinet.portal_access import SECTION_REQUESTS
from client_cabinet.portal_members import resolve_portal_agency_for_user
from employees.access import get_request_role, is_staff_role
from sku.models import Agency
from todo.models import Task

from .models import ShippingOrder
from .services import (
    apply_client_shipping_reserve_on_submit,
    close_logistician_task,
    close_manager_cancel_appeal_task,
    close_manager_review_task,
    close_shipping_act_manager_task,
    close_storekeeper_task,
    close_warehouse_cancel_review_task,
    ensure_warehouse_cancel_review_task,
    create_pick_tasks as create_pick_tasks_for_order,
    ensure_manager_review_task,
    ensure_storekeeper_task,
    mark_storekeeper_task_in_progress,
    order_payload,
    release_client_shipping_reserve_on_cancel,
    release_order_reserves,
    reserve_order,
)

WRITE_ROLES = {
    "admin",
    "director",
    "head_manager",
    "manager",
    "storekeeper",
    "processing_head",
    "developer",
}
MANAGER_ROLES = {
    "admin",
    "director",
    "head_manager",
    "manager",
    "developer",
}
STOREKEEPER_ROLES = {
    "admin",
    "director",
    "storekeeper",
    "developer",
}
SHIPPING_PRIORITY_ROLES = {
    "admin",
    "director",
    "head_manager",
    "storekeeper",
    "developer",
}
SHIPPING_PRIORITY_STATUSES = {
    ShippingOrder.STATUS_RESERVED,
    ShippingOrder.STATUS_STOREKEEPER_ACCEPTED,
}
LOGISTICIAN_ROLES = {
    "admin",
    "director",
    "logistician",
    "developer",
}


def request_scope(request) -> tuple[str | None, str | None, Agency | None]:
    if not request.user.is_authenticated:
        return None, None, None
    role = get_request_role(request)
    if request.user.is_staff or is_staff_role(role):
        return "staff", role, None
    agency = resolve_portal_agency_for_user(request.user, required_section=SECTION_REQUESTS)
    if agency:
        return "client", "client", agency
    return None, role, None


def can_write(scope: str | None, role: str | None) -> bool:
    if scope == "client":
        return True
    if scope == "staff" and (role in WRITE_ROLES or role is None):
        return True
    return False


def is_manager_role(role: str | None) -> bool:
    return role in MANAGER_ROLES or role is None


def is_storekeeper_role(role: str | None) -> bool:
    return role in STOREKEEPER_ROLES or role is None


def is_logistician_role(role: str | None) -> bool:
    return role in LOGISTICIAN_ROLES or role is None


def can_set_shipping_priority(
    scope: str | None,
    role: str | None,
    order: ShippingOrder,
) -> bool:
    return (
        scope == "staff"
        and (role in SHIPPING_PRIORITY_ROLES or role is None)
        and not order.is_closed()
        and order.status in SHIPPING_PRIORITY_STATUSES
    )


def shipping_order_is_manual_priority(order: ShippingOrder) -> bool:
    if not order.pk:
        return False
    return (
        Task.objects.filter(
            route=f"/shipping/{order.pk}/",
            assigned_to__role="storekeeper",
            title=f"Заявка на отгрузку №{order.number}",
            priority="urgent",
        )
        .exclude(status="done")
        .exists()
    )


def can_edit_items(scope: str | None, role: str | None, order: ShippingOrder) -> bool:
    if order.is_closed():
        return False
    editable = {ShippingOrder.STATUS_DRAFT, ShippingOrder.STATUS_SUBMITTED}
    if scope == "client":
        return order.status in editable
    if scope == "staff" and is_manager_role(role):
        return order.status in editable
    return False


def can_edit_order_form(scope: str | None, role: str | None, order: ShippingOrder) -> bool:
    if order.is_closed():
        return False
    editable = {ShippingOrder.STATUS_DRAFT, ShippingOrder.STATUS_SUBMITTED}
    if scope == "client":
        return order.status in editable
    if scope == "staff" and is_manager_role(role):
        return order.status in editable
    return False


def can_submit_for_approval(scope: str | None, role: str | None, order: ShippingOrder) -> bool:
    if order.status != ShippingOrder.STATUS_DRAFT or order.is_closed():
        return False
    if scope == "client":
        return True
    if scope == "staff" and is_manager_role(role):
        return True
    return False


def is_order_returned_for_rework(order: ShippingOrder) -> bool:
    """Distinguish a manager return from an ordinary client draft.

    The workflow intentionally keeps returned requests in ``draft`` so the
    client can edit them.  The latest status audit entry carries the semantic
    state used by the UI without adding another persisted order status.
    """
    if (
        order.status != ShippingOrder.STATUS_DRAFT
        or not str(getattr(order, "number", "") or "").strip()
    ):
        return False
    entry = (
        OrderAuditEntry.objects.filter(
            order_type="shipping",
            order_id=order.number,
            action="status",
        )
        .only("description", "payload")
        .order_by("-created_at", "-id")
        .first()
    )
    if entry is None:
        return False
    payload = entry.payload if isinstance(entry.payload, dict) else {}
    return bool(
        payload.get("returned_for_rework")
        or "возвращена клиенту на доработку" in str(entry.description or "").casefold()
    )


def can_manager_approve(scope: str | None, role: str | None, order: ShippingOrder) -> bool:
    return (
        scope == "staff"
        and is_manager_role(role)
        and not order.is_closed()
        and order.status == ShippingOrder.STATUS_SUBMITTED
    )


def can_manager_reopen(scope: str | None, role: str | None, order: ShippingOrder) -> bool:
    return (
        scope == "staff"
        and is_manager_role(role)
        and not order.is_closed()
        and order.status in {ShippingOrder.STATUS_RESERVED, ShippingOrder.STATUS_STOREKEEPER_ACCEPTED}
    )


def can_storekeeper_accept(scope: str | None, role: str | None, order: ShippingOrder) -> bool:
    return (
        scope == "staff"
        and is_storekeeper_role(role)
        and not order.is_closed()
        and order.status == ShippingOrder.STATUS_RESERVED
    )


def can_storekeeper_pick(scope: str | None, role: str | None, order: ShippingOrder) -> bool:
    return (
        scope == "staff"
        and is_storekeeper_role(role)
        and not order.is_closed()
        and order.status in {ShippingOrder.STATUS_STOREKEEPER_ACCEPTED, ShippingOrder.STATUS_PICKING}
    )


def can_storekeeper_pack(scope: str | None, role: str | None, order: ShippingOrder) -> bool:
    return (
        scope == "staff"
        and is_storekeeper_role(role)
        and not order.is_closed()
        and order.status == ShippingOrder.STATUS_PICKING
    )


def can_storekeeper_manage_packing(
    scope: str | None,
    role: str | None,
    order: ShippingOrder,
    *,
    has_manageable_boxes: bool,
) -> bool:
    allowed_statuses = {ShippingOrder.STATUS_PICKING, ShippingOrder.STATUS_PACKED}
    if order.status == ShippingOrder.STATUS_STOREKEEPER_ACCEPTED and has_manageable_boxes:
        allowed_statuses.add(ShippingOrder.STATUS_STOREKEEPER_ACCEPTED)
    if order.status == ShippingOrder.STATUS_CANCELED and has_manageable_boxes:
        # A canceled order can still own stock already delivered to OTG. The
        # storekeeper must palletize that stock before it can be returned.
        allowed_statuses.add(ShippingOrder.STATUS_CANCELED)
    canceled_return = order.status == ShippingOrder.STATUS_CANCELED and has_manageable_boxes
    if not (
        scope == "staff"
        and is_storekeeper_role(role)
        and (not order.is_closed() or canceled_return)
        and order.status in allowed_statuses
    ):
        return False
    return has_manageable_boxes


def can_storekeeper_ship(scope: str | None, role: str | None, order: ShippingOrder) -> bool:
    return (
        scope == "staff"
        and is_storekeeper_role(role)
        and not order.is_closed()
        and order.status == ShippingOrder.STATUS_PACKED
    )


def can_cancel(scope: str | None, role: str | None, order: ShippingOrder) -> bool:
    if order.is_closed():
        return False
    if scope == "client":
        return order.status in {ShippingOrder.STATUS_DRAFT, ShippingOrder.STATUS_SUBMITTED}
    if scope == "staff" and is_manager_role(role):
        return not requires_warehouse_cancel_confirmation(order)
    return False


def requires_warehouse_cancel_confirmation(order: ShippingOrder) -> bool:
    """Once the warehouse accepted an order, only the warehouse may release it."""
    return order.status in {
        ShippingOrder.STATUS_STOREKEEPER_ACCEPTED,
        ShippingOrder.STATUS_PICKING,
        ShippingOrder.STATUS_PACKED,
    }


def can_access_order(scope: str | None, client_agency: Agency | None, order: ShippingOrder) -> bool:
    if scope is None:
        return False
    if scope == "client" and client_agency is not None and order.agency_id != client_agency.id:
        return False
    return True


def _log_workflow_update(
    order: ShippingOrder,
    user,
    description: str,
    *,
    action: str = "update",
    extra: dict | None = None,
) -> None:
    log_order_action(
        action=action,
        order_id=order.number,
        order_type="shipping",
        user=user if getattr(user, "is_authenticated", False) else None,
        agency=order.agency,
        description=description,
        payload=order_payload(order, extra=extra),
    )


def submit_order_for_approval(order: ShippingOrder, user) -> None:
    order.status = ShippingOrder.STATUS_SUBMITTED
    order.save(update_fields=["status", "updated_at"])
    # Client LK: reserve appears only after send to manager.
    apply_client_shipping_reserve_on_submit(order, user)
    ensure_manager_review_task(order, user)
    _log_workflow_update(
        order,
        user,
        "Заявка отправлена менеджеру на согласование",
        action="status",
    )


def approve_order_by_manager(order: ShippingOrder, user) -> None:
    reserve_order(order, user)
    close_manager_review_task(order)
    ensure_storekeeper_task(order, user)


def reopen_order_for_rework(order: ShippingOrder, user) -> None:
    release_order_reserves(order, user)
    order.status = ShippingOrder.STATUS_DRAFT
    order.reserved_at = None
    order.save(update_fields=["status", "reserved_at", "updated_at"])
    close_manager_review_task(order)
    close_storekeeper_task(order)
    close_logistician_task(order)
    close_shipping_act_manager_task(order)
    _log_workflow_update(
        order,
        user,
        "Заявка возвращена клиенту на доработку",
        action="status",
        extra={"returned_for_rework": True},
    )


def accept_order_by_storekeeper(order: ShippingOrder, user) -> None:
    order.status = ShippingOrder.STATUS_STOREKEEPER_ACCEPTED
    order.save(update_fields=["status", "updated_at"])
    mark_storekeeper_task_in_progress(order)
    _log_workflow_update(
        order,
        user,
        "Кладовщик принял заявку в работу",
        action="status",
    )


@transaction.atomic
def set_shipping_order_manual_priority(
    order: ShippingOrder,
    user,
    *,
    role: str | None,
    urgent: bool,
) -> bool:
    """Change only the queue priority of an unstarted warehouse shipment."""

    if role not in SHIPPING_PRIORITY_ROLES and role is not None:
        raise ValidationError("Менять приоритет может только начальник склада или кладовщик.")
    order = (
        ShippingOrder.objects.select_for_update()
        .select_related("agency")
        .get(pk=order.pk)
    )
    if order.status not in SHIPPING_PRIORITY_STATUSES or order.is_closed():
        raise ValidationError(
            "Приоритет можно менять только у согласованной заявки до запуска отбора."
        )

    title = f"Заявка на отгрузку №{order.number}"
    tasks = list(
        Task.objects.select_for_update()
        .filter(
            route=f"/shipping/{order.pk}/",
            assigned_to__role="storekeeper",
            title=title,
        )
        .exclude(status="done")
        .order_by("id")
    )
    if not tasks:
        task = ensure_storekeeper_task(order, user)
        if task is not None:
            tasks = [Task.objects.select_for_update().get(pk=task.pk)]
    if not tasks:
        raise ValidationError("Не найдена активная задача кладовщика для этой отгрузки.")

    priority = "urgent" if urgent else "normal"
    task_ids = [int(task.pk) for task in tasks]
    Task.objects.filter(pk__in=task_ids).update(
        priority=priority,
        updated_at=timezone.now(),
    )
    _log_workflow_update(
        order,
        user,
        "Отгрузка поставлена в ручной приоритет"
        if urgent
        else "Ручной приоритет отгрузки снят",
        action="priority",
        extra={
            "manual_shipping_priority": priority,
            "storekeeper_task_ids": task_ids,
        },
    )
    return urgent


def start_storekeeper_pick(
    order: ShippingOrder,
    user,
    *,
    requested_by_name: str = "",
    requested_by_role: str = "",
) -> list[str]:
    return create_pick_tasks_for_order(
        order,
        user,
        requested_by_name=requested_by_name,
        requested_by_role=requested_by_role,
    )


def cancel_order_reachtruck_tasks(order: ShippingOrder, user) -> int:
    from django.db.models import Q
    from reachtruck.models import MoveRequest, MoveTask
    from reachtruck.services.move_requests import sync_task_status_by_legacy_order_id

    request_ids = set(
        MoveRequest.objects.filter(
            context_type=MoveRequest.CONTEXT_MANUAL,
            context_id=str(order.pk),
            destination_zone="OTG",
        ).values_list("id", flat=True)
    )
    request_ids.update(
        MoveTask.objects.filter(
            Q(payload__shipping_order_id=order.number)
            | Q(payload__shipping_order_number=order.number)
            | Q(payload__shipping_order_pk=order.pk)
        ).values_list("request_id", flat=True)
    )
    request_ids = {int(request_id) for request_id in request_ids if int(request_id or 0) > 0}
    if not request_ids:
        return 0

    canceled_count = 0
    open_tasks = (
        MoveTask.objects.filter(request_id__in=request_ids)
        .exclude(status__in=[MoveTask.STATUS_DONE, MoveTask.STATUS_CANCELED])
        .order_by("id")
    )
    for task in open_tasks:
        updated = sync_task_status_by_legacy_order_id(
            task.legacy_order_id,
            status=MoveTask.STATUS_CANCELED,
        )
        if not updated:
            continue
        payload = dict(updated.payload or {})
        payload["status"] = MoveTask.STATUS_CANCELED
        payload["status_label"] = "Отменено вместе с заявкой на отгрузку"
        payload["shipping_order_canceled"] = True
        updated.payload = payload
        updated.save(update_fields=["payload", "updated_at"])
        canceled_count += 1

    for move_request in MoveRequest.objects.filter(id__in=request_ids):
        has_open_tasks = move_request.tasks.exclude(
            status__in=[MoveTask.STATUS_DONE, MoveTask.STATUS_CANCELED]
        ).exists()
        if not has_open_tasks and move_request.status != MoveRequest.STATUS_CANCELED:
            move_request.status = MoveRequest.STATUS_CANCELED
            move_request.save(update_fields=["status", "updated_at"])

    if canceled_count:
        _log_workflow_update(
            order,
            user,
            f"Отменены задания ричтрака по отгрузке: {canceled_count}",
            action="reachtruck_cancel",
        )
    return canceled_count


@transaction.atomic
def cancel_order(order: ShippingOrder, user, *, reason: str = "") -> None:
    # Take stock out of reserve, then cancel order/tasks.
    release_client_shipping_reserve_on_cancel(order, user)
    from sklad.services.warehouse_write_path import WarehouseWritePathService

    return_box_count = WarehouseWritePathService.release_canceled_shipping_stock_to_available(
        agency=order.agency,
        order_id=order.number,
        performed_by=user if getattr(user, "is_authenticated", False) else None,
    )
    cancel_order_reachtruck_tasks(order, user)
    order.status = ShippingOrder.STATUS_CANCELED
    order.reserved_at = None
    order.save(update_fields=["status", "reserved_at", "updated_at"])
    order.items.update(qty_reserved=0)
    close_manager_review_task(order)
    close_manager_cancel_appeal_task(order)
    close_warehouse_cancel_review_task(order)
    close_storekeeper_task(order)
    from todo.models import Task

    Task.objects.filter(
        route=f"/shipping/{order.pk}/packing/loose/",
    ).exclude(status="done").update(
        title=f"Упаковать возврат отмененной отгрузки №{order.number}",
        description=(
            "Заявка отменена. Упакуйте находящийся в OTG товар в возвратные короба "
            "и паллеты для размещения на основном складе. Повторная проверка ЧЗ не требуется."
        ),
    )
    if return_box_count > 0:
        return_task = ensure_storekeeper_task(order, user)
        if return_task is not None:
            return_task.title = f"Возврат товара по отмененной отгрузке №{order.number}"
            return_task.description = (
                f"Отгрузка отменена. Разложите короба из OTG по возвратным паллетам "
                f"и передайте их на свободное размещение. Коробов: {return_box_count}."
            )
            return_task.save(update_fields=["title", "description", "updated_at"])
    close_logistician_task(order)
    close_shipping_act_manager_task(order)
    cancel_text = str(reason or "").strip()
    cancel_payload = {
        "status": "cancelled",
        "status_label": "Отменена",
        "warehouse_return_required": return_box_count > 0,
        "warehouse_return_box_count": return_box_count,
        "stock_available_immediately": True,
    }
    if cancel_text:
        cancel_payload["cancel_reason"] = cancel_text
    _log_workflow_update(
        order,
        user,
        f"Заявка отменена, резерв снят. Причина: {cancel_text}" if cancel_text else "Заявка отменена, резерв снят",
        action="status",
        extra=cancel_payload,
    )


def request_warehouse_cancel_confirmation(order: ShippingOrder, user, *, reason: str) -> None:
    if not requires_warehouse_cancel_confirmation(order):
        raise ValueError("Подтверждение склада для этой заявки не требуется")
    cancel_text = str(reason or "").strip()
    if not cancel_text:
        raise ValueError("Укажите причину отмены")
    ensure_warehouse_cancel_review_task(order, user, reason=cancel_text)
    close_manager_cancel_appeal_task(order)
    _log_workflow_update(
        order,
        user,
        "Менеджер запросил подтверждение отмены у склада",
        action="warehouse_cancel_request",
        extra={
            "status": "warehouse_cancel_requested",
            "status_label": "Отмена ожидает подтверждения склада",
            "cancel_reason": cancel_text,
            "cancel_requested_by_manager": True,
        },
    )


def reject_warehouse_cancel_confirmation(order: ShippingOrder, user) -> None:
    close_warehouse_cancel_review_task(order)
    _log_workflow_update(
        order,
        user,
        "Склад не подтвердил отмену; заявка остаётся в работе",
        action="warehouse_cancel_rejected",
        extra={"cancel_request_rejected_by_warehouse": True},
    )
