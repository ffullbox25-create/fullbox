import threading

from django.db import transaction
from django.db.models import Q
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from audit.models import OrderAuditEntry
from employees.models import Employee
from logistics.models import LogisticsTrip, LogisticsTripOrder, ShippingRoutingState
from reachtruck.models import MoveTask
from shipping.models import ShippingOrder
from sklad.models import WarehouseEvent, WarehouseOperation, WarehouseReserve

from .models import Task, TaskPanelSnapshot


KNOWN_ROLE_KEYS = {role for role, _label in Employee.ROLE_CHOICES}
_SYNC_STATE = threading.local()


def _roles_for_task(instance: Task) -> set[str]:
    roles = set()
    assigned_to = getattr(instance, "assigned_to", None)
    observer = getattr(instance, "observer", None)
    created_by = getattr(instance, "created_by", None)
    if assigned_to and assigned_to.role:
        roles.add(assigned_to.role)
    if observer and observer.role:
        roles.add(observer.role)
    username = str(getattr(created_by, "username", "") or "").strip()
    if username in KNOWN_ROLE_KEYS:
        roles.add(username)
    if roles:
        from .templatetags.todo_panel import ALL_ROLES_KEY

        roles.add(ALL_ROLES_KEY)
    return roles


def _schedule_role_sync(role_keys) -> None:
    normalized_roles = {str(role_key).strip() for role_key in role_keys or [] if str(role_key or "").strip()}
    if not normalized_roles:
        return

    pending_roles = getattr(_SYNC_STATE, "role_keys", None)
    if pending_roles is not None:
        pending_roles.update(normalized_roles)
        return

    _SYNC_STATE.role_keys = set(normalized_roles)

    def _run():
        roles_to_sync = set(getattr(_SYNC_STATE, "role_keys", set()))
        if hasattr(_SYNC_STATE, "role_keys"):
            delattr(_SYNC_STATE, "role_keys")
        if roles_to_sync:
            TaskPanelSnapshot.objects.filter(role_key__in=roles_to_sync).exclude(task__status="done").delete()

    transaction.on_commit(_run)


def _schedule_role_sync_for_route_fragment(fragment: str) -> None:
    normalized_fragment = str(fragment or "").strip()
    if not normalized_fragment:
        return

    pending_fragments = getattr(_SYNC_STATE, "route_fragments", None)
    if pending_fragments is not None:
        pending_fragments.add(normalized_fragment)
        return

    _SYNC_STATE.route_fragments = {normalized_fragment}

    def _run():
        fragments = set(getattr(_SYNC_STATE, "route_fragments", set()))
        if hasattr(_SYNC_STATE, "route_fragments"):
            delattr(_SYNC_STATE, "route_fragments")
        task_ids = set()
        for route_fragment in fragments:
            tasks = Task.objects.select_related("assigned_to", "observer", "created_by").filter(
                route__contains=route_fragment
            )
            for task in tasks:
                task_ids.add(task.id)
        if task_ids:
            TaskPanelSnapshot.objects.filter(task_id__in=task_ids).delete()

    transaction.on_commit(_run)


def _schedule_role_sync_for_context(context_type, context_id) -> None:
    raw_type = str(context_type or "").strip().lower()
    raw_id = str(context_id or "").strip()
    if not raw_id:
        return
    if raw_type == "receiving":
        _schedule_role_sync_for_route_fragment(f"/orders/receiving/{raw_id}/")
        return
    if raw_type == "processing":
        _schedule_role_sync_for_route_fragment(f"/orders/processing/{raw_id}/")
        return
    if raw_type == "shipping":
        query = Q(number=raw_id)
        if raw_id.isdigit():
            query |= Q(pk=int(raw_id))
        order_pks = set(
            ShippingOrder.objects.filter(query).values_list("pk", flat=True)
        )
        if not order_pks and raw_id.isdigit():
            order_pks.add(int(raw_id))
        for order_pk in order_pks:
            _schedule_role_sync_for_route_fragment(f"/shipping/{order_pk}/")
        return
    if raw_type in {"logistics", "logistics_trip"}:
        _schedule_role_sync_for_route_fragment(f"/logistics/trips/{raw_id}/")


def _move_task_shipping_pk(instance: MoveTask) -> int | None:
    payload = instance.payload if isinstance(instance.payload, dict) else {}
    for key in ("shipping_order_pk", "order_pk"):
        value = str(payload.get(key) or "").strip()
        if value.isdigit():
            return int(value)
    shipping_number = str(payload.get("shipping_order_id") or payload.get("order_id") or "").strip()
    if not shipping_number:
        return None
    return ShippingOrder.objects.filter(number=shipping_number).values_list("pk", flat=True).first()


def _store_task_snapshot(instance: Task) -> None:
    if instance.status != "done" or instance.kind == Task.KIND_WAREHOUSE_INTERNAL:
        return
    role_keys = _roles_for_task(instance)
    if not role_keys:
        return

    def _run():
        try:
            task = (
                Task.objects.select_related("assigned_to", "observer", "created_by")
                .get(pk=instance.pk)
            )
        except Task.DoesNotExist:
            return
        from .templatetags.todo_panel import _snapshot_defaults_for_task, _snapshot_role_value

        defaults = _snapshot_defaults_for_task(task, hidden=False)
        route = str(task.route or "")
        requires_storekeeper_enrichment = any(
            fragment in route
            for fragment in (
                "/orders/receiving/",
                "/orders/other/",
                "/shipping/",
                "/logistics/trips/",
            )
        )
        for role_key in role_keys:
            if role_key == "storekeeper" and requires_storekeeper_enrichment:
                continue
            TaskPanelSnapshot.objects.update_or_create(
                task=task,
                role_key=_snapshot_role_value(role_key),
                defaults=defaults,
            )

    transaction.on_commit(_run)


@receiver(post_save, sender=Task)
@receiver(post_delete, sender=Task)
def invalidate_task_snapshot(sender, instance, **kwargs):
    TaskPanelSnapshot.objects.filter(task=instance).delete()
    if kwargs.get("signal") is post_save and instance.status == "done":
        _store_task_snapshot(instance)


@receiver(post_save, sender=Task)
def preserve_rescheduled_warehouse_deadline(sender, instance, **kwargs):
    if instance.status == "done":
        return
    from .deadlines import apply_latest_deadline_to_task

    apply_latest_deadline_to_task(instance)


@receiver(post_save, sender=OrderAuditEntry)
@receiver(post_delete, sender=OrderAuditEntry)
def invalidate_order_audit_snapshot(sender, instance, **kwargs):
    _schedule_role_sync_for_context(instance.order_type, instance.order_id)


@receiver(post_save, sender=ShippingOrder)
@receiver(post_delete, sender=ShippingOrder)
def invalidate_shipping_snapshot(sender, instance, **kwargs):
    _schedule_role_sync_for_route_fragment(f"/shipping/{instance.pk}/")


@receiver(post_save, sender=MoveTask)
@receiver(post_delete, sender=MoveTask)
def invalidate_move_task_snapshot(sender, instance, **kwargs):
    shipping_pk = _move_task_shipping_pk(instance)
    if shipping_pk:
        _schedule_role_sync_for_context("shipping", shipping_pk)


@receiver(post_save, sender=LogisticsTrip)
@receiver(post_delete, sender=LogisticsTrip)
def invalidate_logistics_snapshot(sender, instance, **kwargs):
    _schedule_role_sync_for_context("logistics_trip", instance.pk)
    if kwargs.get("signal") is post_save:
        for shipping_order_pk in instance.orders.values_list("shipping_order_id", flat=True):
            _schedule_role_sync_for_context("shipping", shipping_order_pk)


@receiver(post_save, sender=LogisticsTripOrder)
@receiver(post_delete, sender=LogisticsTripOrder)
def invalidate_logistics_trip_order_snapshot(sender, instance, **kwargs):
    _schedule_role_sync_for_context("shipping", instance.shipping_order_id)


@receiver(post_save, sender=ShippingRoutingState)
@receiver(post_delete, sender=ShippingRoutingState)
def invalidate_shipping_routing_snapshot(sender, instance, **kwargs):
    _schedule_role_sync_for_context("shipping", instance.shipping_order_id)


@receiver(post_save, sender=WarehouseReserve)
@receiver(post_delete, sender=WarehouseReserve)
def invalidate_warehouse_reserve_snapshot(sender, instance, **kwargs):
    _schedule_role_sync_for_context(instance.context_type, instance.context_id)


@receiver(post_save, sender=WarehouseOperation)
@receiver(post_delete, sender=WarehouseOperation)
def invalidate_warehouse_operation_snapshot(sender, instance, **kwargs):
    _schedule_role_sync_for_context(instance.context_type, instance.context_id)


@receiver(post_save, sender=WarehouseEvent)
@receiver(post_delete, sender=WarehouseEvent)
def invalidate_warehouse_event_snapshot(sender, instance, **kwargs):
    _schedule_role_sync_for_context(instance.stock_context_type, instance.stock_context_id)
