"""Shipping-wide driver ownership, recorded in the existing order audit.

The queue mutex serializes assignment decisions (including one driver's two
concurrent terminals); shipping row locks serialize them with order changes.
No stock facts or pallet claims are changed by assigning/releasing an order.
"""
from functools import wraps

from django.db import connection, transaction
from django.db.models import Q
from audit.models import OrderAuditEntry
from employees.access import get_employee_for_user
from reachtruck.models import MoveTask, MoveRequest
from shipping.models import ShippingOrder
from .models import OtgDeliveryRequest

ASSIGNMENT_DESCRIPTION = "Закрепление отгрузки за водителем ОТГ"
FINAL_STATUSES = {MoveTask.STATUS_DONE, MoveTask.STATUS_CANCELED, MoveTask.STATUS_FAILED}
MANAGER_ROLES = {"manager", "storekeeper", "processing_head", "head_manager", "director", "admin"}



def _assignment_result(*, ok, error="", message=""):
    from .execution import MoveTaskCommandResult
    result = MoveTaskCommandResult(ok=ok)
    # These are known UTF-8 application strings; preserve them verbatim. The
    # shared legacy text repair can mistake valid Russian words for mojibake.
    result.error = error
    result.message = message
    return result

def lock_assignment_queue():
    # Transaction-scoped PostgreSQL mutex, reserved for this OTG assignment flow.
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(%s, %s)", [1178750808, 20260908])


def order_for_task(task):
    linked = OtgDeliveryRequest.objects.filter(move_request_id=task.request_id).order_by().values_list("shipping_order_id", flat=True).distinct()
    order_ids = list(linked[:2])
    if len(order_ids) == 1:
        return ShippingOrder.objects.filter(pk=order_ids[0], agency_id=task.request.agency_id).first()
    if order_ids:
        raise ValueError("Задание связано с несколькими отгрузками. Требуется проверка руководителем.")
    number = str((task.payload or {}).get("shipping_order_id") or "").removeprefix("shipping:").strip()
    if number:
        return ShippingOrder.objects.filter(number=number, agency_id=task.request.agency_id).first()
    context_id = str(task.request.context_id or "")
    if task.request.context_type == MoveRequest.CONTEXT_MANUAL and context_id.isdigit():
        return ShippingOrder.objects.filter(pk=int(context_id), agency_id=task.request.agency_id).first()
    return None  # Standalone legacy move, outside the shipping-order queue.


def order_tasks(order):
    request_ids = OtgDeliveryRequest.objects.filter(shipping_order=order).exclude(move_request_id=None).values_list("move_request_id", flat=True)
    return MoveTask.objects.select_related("request", "assigned_to").filter(
        request__agency_id=order.agency_id, to_zone="OTG",
    ).filter(
        Q(request_id__in=request_ids)
        | Q(request__context_type=MoveRequest.CONTEXT_MANUAL, request__context_id=str(order.pk), request__destination_zone="OTG")
        | Q(payload__shipping_order_id=order.number)
        | Q(payload__shipping_order_id="shipping:" + order.number)
    ).order_by("id")


def _task_owner(task):
    from .execution import _otg_task_assignee_employee_id
    employee_id = _otg_task_assignee_employee_id(task, dict(task.payload or {}))
    name = str(task.assigned_to_name or (task.payload or {}).get("assigned_to_name") or "")
    return employee_id, name


def assignment_snapshots(orders, *, tasks_by_order=None):
    """Batch audit read, with read-only adoption of work started before deploy."""
    orders = list(orders)
    states = {order.pk: {"employee_id": None, "employee_name": "", "conflict": False, "recorded": False} for order in orders}
    identities = {(order.agency_id, order.number): order.pk for order in orders}
    if not orders:
        return states
    entries = OrderAuditEntry.objects.filter(
        order_type="shipping", description=ASSIGNMENT_DESCRIPTION,
        order_id__in=[order.number for order in orders], agency_id__in={order.agency_id for order in orders},
    ).order_by("-created_at", "-id").values("agency_id", "order_id", "payload")
    for entry in entries:
        pk = identities.get((entry["agency_id"], entry["order_id"]))
        if pk is None or states[pk]["recorded"]:
            continue
        record = dict((entry["payload"] or {}).get("otg_driver_assignment") or {})
        states[pk].update(employee_id=record.get("employee_id"), employee_name=record.get("employee_name") or "", recorded=True)
    for order in orders:
        state = states[order.pk]
        tasks = list(tasks_by_order.get(order.pk, [])) if tasks_by_order is not None else list(order_tasks(order))
        active_owners = {}
        for task in tasks:
            if task.status == MoveTask.STATUS_IN_PROGRESS:
                pk, name = _task_owner(task)
                if pk:
                    active_owners[pk] = name
        if state["employee_id"]:
            active_owners.setdefault(state["employee_id"], state["employee_name"])
        if len(active_owners) > 1:
            state.update(conflict=True, employee_ids=list(active_owners), employee_name=", ".join(active_owners.values()))
        elif active_owners:
            pk, name = next(iter(active_owners.items()))
            state.update(employee_id=pk, employee_name=name)
        elif not state["recorded"]:
            # Keep the driver between pallets of an existing unfinished route.
            active_request_ids = {task.request_id for task in tasks if task.status not in FINAL_STATUSES}
            completed = [task for task in tasks if task.status == MoveTask.STATUS_DONE and task.request_id in active_request_ids]
            for task in sorted(completed, key=lambda row: (row.updated_at, row.pk), reverse=True):
                pk, name = _task_owner(task)
                if pk:
                    state.update(employee_id=pk, employee_name=name)
                    break
    return states


def assignment_error(state, employee_id):
    if state.get("conflict"):
        return "В заявке уже работают несколько водителей. Завершите взятые паллеты и передайте заявку одному водителю через руководителя."
    if state.get("employee_id") and state["employee_id"] != employee_id:
        return f"Заявка закреплена за водителем {state['employee_name'] or state['employee_id']}. Возьмите следующую свободную заявку."
    return ""


def _record_assignment(order, *, employee_id, employee_name, user, event, previous):
    OrderAuditEntry.objects.create(
        order_type="shipping", order_id=order.number, agency=order.agency,
        action="update", user=user if getattr(user, "is_authenticated", False) else None,
        description=ASSIGNMENT_DESCRIPTION,
        payload={"otg_driver_assignment": {"employee_id": employee_id, "employee_name": employee_name,
                  "event": event, "previous_employee_id": previous.get("employee_id"),
                  "previous_employee_name": previous.get("employee_name")}},
    )


def _other_owned_order(employee_id, current_order_id):
    # Resolve live work from OTG relations, independently of UI grouping/payload
    # labels. A missing display number must not let a driver take two orders.
    active_tasks = list(MoveTask.objects.select_related("request", "assigned_to")
                        .filter(to_zone="OTG").exclude(status__in=FINAL_STATUSES))
    by_request, orders, tasks_by_order = {}, {}, {}
    for task in active_tasks:
        if task.request_id not in by_request:
            by_request[task.request_id] = order_for_task(task)
        order = by_request[task.request_id]
        if order is not None and order.pk != current_order_id:
            orders[order.pk] = order
            tasks_by_order.setdefault(order.pk, []).append(task)
    completed = MoveTask.objects.select_related("request", "assigned_to").filter(
        request_id__in=[pk for pk, order in by_request.items() if order and order.pk in orders],
        status=MoveTask.STATUS_DONE,
    )
    for task in completed:
        tasks_by_order[by_request[task.request_id].pk].append(task)
    states = assignment_snapshots(orders.values(), tasks_by_order=tasks_by_order)
    for pk, state in states.items():
        if state.get("employee_id") == employee_id or employee_id in state.get("employee_ids", []):
            return orders[pk]
    return None


def shipping_driver_command(*, taking=False):
    """Guard every public OTG take/scan path, including single-pallet calls."""
    def decorate(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            employee_id = kwargs.get("employee_id")
            ids = kwargs.get("legacy_order_ids", args[0] if args else None)
            if ids is None:
                ids = [kwargs.get("legacy_order_id")]
            elif isinstance(ids, str):
                ids = [ids]
            ids = [str(value) for value in ids if value]
            seeds = list(MoveTask.objects.select_related("request").filter(legacy_order_id__in=ids).exclude(status__in=FINAL_STATUSES))
            try:
                by_request = {}
                for task in seeds:
                    if task.request_id not in by_request:
                        by_request[task.request_id] = order_for_task(task)
                orders = {order.pk: order for order in by_request.values() if order is not None}
            except ValueError as exc:
                return _assignment_result(ok=False, error=str(exc))
            if len(orders) > 1:
                return _assignment_result(ok=False, error="За одно действие можно взять только одну отгрузку.")
            if not orders:
                return fn(*args, **kwargs)
            if not employee_id:
                return _assignment_result(ok=False, error="Профиль сотрудника не найден.")
            with transaction.atomic():
                if taking:
                    lock_assignment_queue()
                order = ShippingOrder.objects.select_for_update().get(pk=next(iter(orders)))
                state = assignment_snapshots([order])[order.pk]
                error = assignment_error(state, employee_id)
                # Existing multiple-driver work must be allowed to finish the
                # already-taken pallet; no new pallet may be assigned in conflict.
                if error and not taking and state.get("conflict"):
                    if any(task.status == MoveTask.STATUS_IN_PROGRESS and _task_owner(task)[0] == employee_id for task in seeds):
                        error = ""
                if error:
                    return _assignment_result(ok=False, error=error)
                if taking:
                    other = _other_owned_order(employee_id, order.pk)
                    if other:
                        return _assignment_result(ok=False, error=f"Сначала завершите свою заявку {other.number} или передайте её через руководителя.")
                result = fn(*args, **kwargs)
                if result.ok and taking and not transaction.get_rollback() and (not state.get("recorded") or not state.get("employee_id")):
                    _record_assignment(order, employee_id=employee_id, employee_name=kwargs.get("employee_name", ""),
                                       user=kwargs.get("user"), event="assigned", previous=state)
                return result
        return wrapped
    return decorate


@transaction.atomic
def release_shipping_driver_assignment(*, order_id, user, employee_id, employee_name):
    manager = get_employee_for_user(user)
    if not manager or manager.pk != employee_id or manager.role not in MANAGER_ROLES:
        return _assignment_result(ok=False, error="Передать заявку может только руководитель.")
    lock_assignment_queue()
    order = ShippingOrder.objects.select_for_update().get(pk=order_id)
    state = assignment_snapshots([order])[order.pk]
    if order_tasks(order).filter(status=MoveTask.STATUS_IN_PROGRESS).exists():
        return _assignment_result(ok=False, error="Сначала завершите взятые паллеты. Не начатое задание можно вернуть в очередь существующей кнопкой после проверки.")
    from .execution import _otg_task_has_protected_scan_facts
    pending = order_tasks(order).exclude(status__in=FINAL_STATUSES)
    if any(_otg_task_has_protected_scan_facts(dict(task.payload or {})) for task in pending):
        return _assignment_result(ok=False, error="В незавершённых паллетах уже есть сканы. Сначала завершите перемещение.")
    if pending.filter(Q(box_claims__status="claimed") | Q(pallet_locks__status="active")).exists():
        return _assignment_result(ok=False, error="Незавершённая паллета ещё закреплена для перемещения. Сначала завершите задание.")
    if not state.get("employee_id") and not state.get("conflict"):
        return _assignment_result(ok=False, error="Заявка уже свободна.")
    _record_assignment(order, employee_id=None, employee_name="", user=user, event="released", previous=state)
    return _assignment_result(ok=True, message="Заявка освобождена и доступна следующему водителю. Выполненные паллеты сохранены.")
