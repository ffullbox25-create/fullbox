from __future__ import annotations

from dataclasses import dataclass

from django.contrib import messages
from django.db import transaction
from django.db.models import Count, Prefetch, Q
from django.http import HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme

from audit.models import OrderAuditEntry, get_order_external_number, log_order_action
from employees.access import get_request_employee, get_request_roles
from employees.models import Employee
from logistics.models import LogisticsTrip, LogisticsTripOrder
from shipping.selectors import shipping_ui_status_label
from .models import Task, TaskAttachment, TaskChecklistItem, TaskComment


INTERNAL_TASK_CREATE_ROLES = {
    "admin",
    "director",
    "head_manager",
    "manager",
    "developer",
}
INTERNAL_TASK_OVERSIGHT_ROLES = {
    "admin",
    "director",
    "head_manager",
    "developer",
}
DAILY_PROBLEM_CONTROL_ROUTE_PREFIX = "/sklad/journal/?daily_problem_check="
READ_ONLY_HTTP_METHODS = {"GET", "HEAD"}


def _views():
    from . import views as todo_views

    return todo_views


def build_trip_context(task: Task) -> dict | None:
    trip_pk = _views()._extract_logistics_trip_pk(task.route)
    if trip_pk is None:
        return None
    trip = (
        LogisticsTrip.objects.select_related("assigned_logistician", "created_by")
        .prefetch_related(
            Prefetch(
                "orders",
                queryset=LogisticsTripOrder.objects.select_related(
                    "shipping_order",
                    "shipping_order__agency",
                    "shipping_order__marketplace",
                ).order_by("loading_sequence", "delivery_sequence", "id"),
            )
        )
        .filter(pk=trip_pk)
        .first()
    )
    if trip is None:
        return None
    trip_orders = list(trip.orders.all())
    order_numbers = [item.shipping_order.number for item in trip_orders if item.shipping_order and item.shipping_order.number]
    packing_payloads = _views()._trip_packing_payload_map(order_numbers)
    rows: list[dict] = []
    participants: list[str] = []
    if trip.assigned_logistician:
        participants.append(f"Логист: {trip.assigned_logistician.full_name}")
    storekeeper_name = task.assigned_to.full_name if task.assigned_to else ""
    if storekeeper_name:
        participants.append(f"Кладовщик: {storekeeper_name}")
    creator_name = getattr(task.created_by, "get_full_name", lambda: "")() or getattr(task.created_by, "username", "")
    if creator_name:
        participants.append(f"Постановщик: {creator_name}")
    for index, item in enumerate(trip_orders, start=1):
        order = item.shipping_order
        packing_payload = packing_payloads.get(order.number) or {}
        rows.append(
            {
                "route_position": index,
                "display_number": get_order_external_number("shipping", order.number),
                "agency_name": _views()._short_agency_name(getattr(order.agency, "agn_name", "")) or "-",
                "destination_label": str(order.destination_warehouse or "").strip() or "-",
                "slot_date_label": order.slot_date.strftime("%d.%m.%Y") if order.slot_date else "-",
                "pallet_count": int(packing_payload.get("pallet_count") or 0),
                "box_count": int(packing_payload.get("delivered_box_count") or order.expected_boxes or 0),
                "status_label": shipping_ui_status_label(order),
                "comment": item.comment or "-",
            }
        )
    return {
        "trip": trip,
        "trip_display_number": _views()._trip_public_number(trip),
        "status_label": _views()._trip_status_label(trip),
        "participants": participants,
        "orders": rows,
        "total_pallets": sum(row["pallet_count"] for row in rows),
        "total_boxes": sum(row["box_count"] for row in rows),
        "vehicle_name": trip.vehicle_name or "-",
        "vehicle_number": trip.vehicle_number or "-",
        "driver_name": trip.driver_name or "-",
        "driver_phone": trip.driver_phone or "-",
        "route_comment": trip.route_comment or "-",
        "loading_comment": trip.loading_comment or "-",
    }


def send_receiving_to_warehouse(task: Task, request) -> bool:
    order_id = _views()._extract_receiving_order_id(task.route)
    if not order_id:
        return False
    latest = (
        OrderAuditEntry.objects.filter(order_id=order_id, order_type="receiving")
        .select_related("agency")
        .order_by("-created_at")
        .first()
    )
    if not latest:
        return False
    payload = dict(latest.payload or {})
    status_value = (payload.get("status") or payload.get("submit_action") or "").lower()
    status_label = (payload.get("status_label") or "").lower()
    if (
        status_value in {"warehouse", "on_warehouse"}
        or "склад" in status_label
        or "ожидании поставки" in status_label
    ):
        return True
    payload["status"] = "warehouse"
    payload["status_label"] = "В ожидании поставки товара"
    log_order_action(
        "status",
        order_id=order_id,
        order_type="receiving",
        user=request.user if request.user.is_authenticated else None,
        agency=latest.agency,
        description="Подтверждено и отправлено на склад",
        payload=payload,
    )
    Task.objects.filter(
        route=f"/orders/receiving/{order_id}/",
        assigned_to__role="manager",
    ).exclude(status="done").update(status="done")
    storekeeper = (
        Employee.objects.filter(role="storekeeper", is_active=True)
        .order_by("full_name")
        .first()
    )
    if storekeeper:
        observer = Employee.objects.filter(
            user=request.user, is_active=True
        ).first()
        description = f"Клиент: {latest.agency.agn_name or latest.agency.inn or latest.agency.id}"
        Task.objects.create(
            title=f"Принять заявку на приемку товара №{order_id}",
            description=description,
            route=f"/orders/receiving/{order_id}/",
            assigned_to=storekeeper,
            observer=observer,
            created_by=request.user if request.user.is_authenticated else None,
            due_date=timezone.localtime() + _views().timedelta(days=1),
        )
    return True


def status_entry_from_list(entries):
    for entry in reversed(entries):
        payload = entry.payload or {}
        if entry.action == "status":
            return entry
        if payload.get("status") or payload.get("status_label") or payload.get("submit_action"):
            return entry
    return entries[-1] if entries else None


def act_entry_from_entries(entries, act_type: str):
    for entry in reversed(entries or []):
        if (entry.payload or {}).get("act") == act_type:
            return entry
    return None


def resolve_return_url(request) -> str:
    candidate = request.POST.get("next") or request.GET.get("next") or ""
    if candidate and url_has_allowed_host_and_scheme(
        url=candidate,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return candidate
    cabinet_url = _views().resolve_cabinet_url(_views().get_request_role(request))
    if cabinet_url != "/":
        return cabinet_url
    return reverse("todo:list")


def can_create_receiving_act(task) -> tuple[bool, str | None, list]:
    order_id = _views()._extract_receiving_order_id(task.route)
    if not order_id:
        return False, None, []
    entries = list(
        OrderAuditEntry.objects.filter(order_id=order_id, order_type="receiving")
        .select_related("agency")
        .order_by("created_at")
    )
    if not entries:
        return False, order_id, []
    if any((entry.payload or {}).get("act") == "receiving" for entry in entries):
        return False, order_id, entries
    status_entry = status_entry_from_list(entries)
    payload = status_entry.payload or {} if status_entry else {}
    status_value = (payload.get("status") or payload.get("submit_action") or "").lower()
    status_label = (payload.get("status_label") or "").lower()
    if (
        status_value in {"warehouse", "on_warehouse"}
        or "склад" in status_label
        or "ожидании поставки" in status_label
    ):
        return True, order_id, entries
    return False, order_id, entries


def build_task_list_context(tasks) -> dict:
    return {"tasks": tasks}


def build_task_list_queryset(*, request, role):
    kind = str(request.GET.get("kind") or Task.KIND_SYSTEM).strip()
    if kind == Task.KIND_WAREHOUSE_INTERNAL:
        return build_internal_task_list_queryset(request=request)

    tasks = Task.objects.exclude(kind=Task.KIND_WAREHOUSE_INTERNAL)
    if role and not request.user.is_staff:
        tasks = tasks.filter(
            Q(assigned_to__role=role)
            | Q(observer__role=role)
            | Q(created_by=request.user)
        )
    return tasks


def internal_task_can_create(request) -> bool:
    if not getattr(request.user, "is_authenticated", False):
        return False
    if getattr(request.user, "is_superuser", False):
        return True
    return bool(get_request_roles(request).intersection(INTERNAL_TASK_CREATE_ROLES))


def internal_task_has_oversight(request) -> bool:
    if getattr(request.user, "is_superuser", False):
        return True
    return bool(get_request_roles(request).intersection(INTERNAL_TASK_OVERSIGHT_ROLES))


def build_internal_task_list_queryset(*, request):
    tasks = Task.objects.filter(kind=Task.KIND_WAREHOUSE_INTERNAL)
    employee = get_request_employee(request)
    if not internal_task_has_oversight(request):
        access_filter = Q(created_by=request.user)
        if employee:
            access_filter |= (
                Q(assigned_to=employee)
                | Q(observer=employee)
                | Q(participants=employee)
            )
        tasks = tasks.filter(access_filter)

    status = str(request.GET.get("status") or "active").strip()
    if status == "active":
        tasks = tasks.exclude(status="done")
    elif status in {"backlog", "in_progress", "blocked", "done"}:
        tasks = tasks.filter(status=status)

    query = str(request.GET.get("q") or "").strip()
    if query:
        tasks = tasks.filter(
            Q(title__icontains=query)
            | Q(description__icontains=query)
            | Q(assigned_to__full_name__icontains=query)
            | Q(participants__full_name__icontains=query)
        )

    return (
        tasks.select_related("assigned_to", "observer", "created_by")
        .prefetch_related("participants")
        .annotate(
            checklist_total_count=Count("checklist_items", distinct=True),
            checklist_completed_count=Count(
                "checklist_items",
                filter=Q(checklist_items__is_completed=True),
                distinct=True,
            ),
        )
        .distinct()
        .order_by("status", "due_date", "-created_at")
    )


def build_internal_task_list_context(*, request, tasks) -> dict:
    base_tasks = Task.objects.filter(kind=Task.KIND_WAREHOUSE_INTERNAL)
    employee = get_request_employee(request)
    if not internal_task_has_oversight(request):
        access_filter = Q(created_by=request.user)
        if employee:
            access_filter |= (
                Q(assigned_to=employee)
                | Q(observer=employee)
                | Q(participants=employee)
            )
        base_tasks = base_tasks.filter(access_filter)
    return {
        "tasks": tasks,
        "can_create_internal_task": internal_task_can_create(request),
        "selected_status": str(request.GET.get("status") or "active").strip(),
        "search_query": str(request.GET.get("q") or "").strip(),
        "return_url": _views().resolve_cabinet_url(_views().get_request_role(request)),
        "status_counts": {
            "active": base_tasks.exclude(status="done").distinct().count(),
            "backlog": base_tasks.filter(status="backlog").distinct().count(),
            "in_progress": base_tasks.filter(status="in_progress").distinct().count(),
            "blocked": base_tasks.filter(status="blocked").distinct().count(),
            "done": base_tasks.filter(status="done").distinct().count(),
        },
    }


def _is_daily_problem_control_manager_view(*, task: Task, role: str | None) -> bool:
    return bool(
        role == "manager"
        and task.kind == Task.KIND_SYSTEM
        and str(task.route or "").startswith(DAILY_PROBLEM_CONTROL_ROUTE_PREFIX)
    )


def can_access_task(*, request, task: Task, role=None) -> bool:
    if task.kind == Task.KIND_WAREHOUSE_INTERNAL:
        return internal_task_can_access(request=request, task=task)
    current_role = role if role is not None else _views().get_request_role(request)
    if _is_daily_problem_control_manager_view(task=task, role=current_role):
        return str(getattr(request, "method", "GET") or "GET").upper() in READ_ONLY_HTTP_METHODS
    if request.user.is_staff:
        return True
    if current_role:
        if task.assigned_to and task.assigned_to.role == current_role:
            return True
        if task.observer and task.observer.role == current_role:
            return True
    return task.created_by_id == request.user.id


def internal_task_can_access(*, request, task: Task) -> bool:
    if task.kind != Task.KIND_WAREHOUSE_INTERNAL:
        return False
    if task.created_by_id == getattr(request.user, "id", None):
        return True
    if internal_task_has_oversight(request):
        return True
    employee = get_request_employee(request)
    if not employee:
        return False
    if employee.id in {task.assigned_to_id, task.observer_id}:
        return True
    return task.participants.filter(pk=employee.pk).exists()


def internal_task_can_edit(*, request, task: Task) -> bool:
    if task.status == "done":
        return False
    return (
        task.created_by_id == getattr(request.user, "id", None)
        or internal_task_has_oversight(request)
    )


def internal_task_can_work(*, request, task: Task) -> bool:
    if task.status == "done":
        return False
    employee = get_request_employee(request)
    if not employee:
        return False
    if task.assigned_to_id == employee.id:
        return True
    return task.participants.filter(pk=employee.pk).exists()


def internal_task_can_complete(*, request, task: Task) -> bool:
    employee = get_request_employee(request)
    if not employee or task.status == "done" or task.assigned_to_id != employee.id:
        return False
    return not task.checklist_items.filter(is_completed=False).exists()


def internal_task_can_return(*, request, task: Task) -> bool:
    if task.status != "done":
        return False
    return (
        task.created_by_id == getattr(request.user, "id", None)
        or internal_task_has_oversight(request)
    )


def save_checklist_formset(formset) -> None:
    position = 0
    for form in formset.forms:
        cleaned_data = getattr(form, "cleaned_data", {})
        if not cleaned_data or cleaned_data.get("DELETE"):
            if form.instance.pk:
                form.instance.delete()
            continue
        title = str(cleaned_data.get("title") or "").strip()
        if not title:
            continue
        item = form.save(commit=False)
        item.task = formset.instance
        item.title = title
        item.position = position
        item.save()
        position += 1


def handle_task_create(request):
    form = _views().TaskForm(request.POST)
    if form.is_valid():
        task = form.save(commit=False)
        if request.user.is_authenticated:
            task.created_by = request.user
        task.save()
        messages.success(request, "Задача создана")
        return redirect(resolve_return_url(request)), form
    return None, form


def handle_task_update(request, task: Task):
    form = _views().TaskForm(request.POST, instance=task)
    if form.is_valid():
        form.save()
        messages.success(request, "Задача обновлена")
        return redirect(resolve_return_url(request)), form
    return None, form


def handle_task_delete(request, task: Task):
    if task.kind == Task.KIND_WAREHOUSE_INTERNAL:
        can_delete = (
            task.status == "backlog"
            and (
                task.created_by_id == getattr(request.user, "id", None)
                or internal_task_has_oversight(request)
            )
        )
        if not can_delete:
            messages.error(request, "Удалить можно только новую задачу до начала работы")
            return redirect("todo:detail", pk=task.pk)
    task.delete()
    messages.success(request, "Задача удалена")
    return redirect("todo:list")


@dataclass
class TaskDetailState:
    can_complete: bool
    can_create_receiving_act: bool
    receiving_act_url: str | None
    can_create_placement_act: bool
    placement_act_url: str | None
    placement_act_exists: bool
    receiving_act_exists: bool
    receiving_act_label: str
    placement_act_label: str
    receiving_act_open_url: str | None
    placement_act_open_url: str | None
    can_send_act_to_client: bool
    can_edit: bool
    return_url: str
    order_context: dict | None
    trip_context: dict | None


def build_task_detail_state(*, request, task: Task) -> TaskDetailState:
    role = _views().get_request_role(request)
    can_complete = bool(
        role
        and task.assigned_to
        and role == task.assigned_to.role
    )
    can_create_receiving_act = False
    receiving_act_url = None
    can_create_placement_act = False
    placement_act_url = None
    placement_act_exists = False
    receiving_act_exists = False
    receiving_act_label = ""
    placement_act_label = ""
    receiving_act_open_url = None
    placement_act_open_url = None
    can_send_act_to_client = False
    if can_complete and task.assigned_to and task.assigned_to.role == "storekeeper":
        can_create_receiving_act, order_id, entries = can_create_receiving_act_helper(task)
        if order_id:
            receiving_act = act_entry_from_entries(entries, "receiving")
            placement_act = act_entry_from_entries(entries, "placement")
            placement_act_exists = placement_act is not None
            if receiving_act and not placement_act:
                can_create_placement_act = True
                placement_act_url = f"/orders/receiving/{order_id}/placement/"
        if can_create_receiving_act and order_id:
            receiving_act_url = f"/orders/receiving/{order_id}/flow/"

    comment_form = _views().TaskCommentForm()
    attachment_form = _views().TaskAttachmentForm()
    can_edit = request.user.is_authenticated and task.created_by_id == request.user.id
    employee_role = _views().get_request_role(request)
    return_url = "/"
    if employee_role == "storekeeper":
        return_url = "/sklad/"
    elif employee_role == "manager":
        return_url = "/team-manager/"
    elif employee_role:
        return_url = f"/cabinet/{employee_role}/"

    trip_context = build_trip_context(task)
    order_context = None
    order_id = _views()._extract_receiving_order_id(task.route)
    if order_id:
        entries = list(
            OrderAuditEntry.objects.filter(order_id=order_id, order_type="receiving")
            .select_related("user", "agency")
            .order_by("created_at")
        )
        if entries:
            latest = entries[-1]
            status_entry = _views().order_views._current_status_entry(entries)
            payload = _views().order_views._latest_payload_from_entries(entries)
            client_label = "-"
            if latest and latest.agency:
                name = latest.agency.agn_name or latest.agency.fio_agn or str(latest.agency)
                client_label = _views().order_views._shorten_ip_name(name)
            participants = []
            seen = set()
            for entry in entries:
                label = _views().order_views._actor_label(entry.user, entry.agency, client_view=False)
                if label in seen:
                    continue
                seen.add(label)
                participants.append(label)

            def find_act_entry(act_type, label_hint):
                entry = _views().order_views._act_entry_from_entries(entries, act_type)
                if entry:
                    return entry
                for candidate in reversed(entries):
                    label = ((candidate.payload or {}).get("act_label") or "").lower()
                    if label_hint in label:
                        return candidate
                return None

            receiving_entry = find_act_entry("receiving", "акт приемки")
            placement_entry = find_act_entry("placement", "акт размещения")
            receiving_act_exists = bool(receiving_entry)
            placement_act_exists = bool(placement_entry)
            receiving_act_label = (
                (receiving_entry.payload or {}).get("act_label") if receiving_entry else ""
            ) or "Акт приемки"
            placement_act_label = (
                (placement_entry.payload or {}).get("act_label") if placement_entry else ""
            ) or "Акт размещения"
            receiving_act_open_url = f"/orders/receiving/{order_id}/act/"
            placement_act_open_url = f"/orders/receiving/{order_id}/placement/"
            can_send_act_to_client = bool(
                role in {"manager", "head_manager", "director", "admin"}
                and receiving_entry
                and placement_entry
                and not _views().order_views._is_done_status(status_entry)
            )

            order_context = {
                "order_id": order_id,
                "status_label": _views().order_views._status_label_from_entry(status_entry)
                if status_entry
                else "-",
                "responsible": _views().order_views._current_responsible_label(status_entry)
                if status_entry
                else "-",
                "client_label": client_label,
                "meta": {
                    "eta_at": _views().order_views._format_datetime_value(payload.get("eta_at")),
                    "expected_boxes": payload.get("expected_boxes"),
                    "place_type": _views().order_views._place_type_label(payload.get("place_type")),
                    "vehicle_number": payload.get("vehicle_number"),
                    "driver_phone": payload.get("driver_phone"),
                    "comment": payload.get("comment"),
                },
                "items": payload.get("items") or [],
                "participants": participants,
                "history": [
                    {
                        "created_at": entry.created_at,
                        "action_label": _views().order_views._history_action_label(entry),
                        "status_label": _views().order_views._status_label_from_entry(entry),
                        "description": _views().order_views._format_message_text(entry.description),
                        "actor_label": _views().order_views._history_actor_label(
                            entry,
                            client_view=False,
                            client_label=client_label,
                        ),
                    }
                    for entry in reversed(entries)
                    if entry.action != "comment"
                ],
            }
            if order_context["status_label"] in ("-", "", None):
                order_context["status_label"] = "В ожидании поставки товара"

    return TaskDetailState(
        can_complete=can_complete,
        can_create_receiving_act=can_create_receiving_act,
        receiving_act_url=receiving_act_url,
        can_create_placement_act=can_create_placement_act,
        placement_act_url=placement_act_url,
        placement_act_exists=placement_act_exists,
        receiving_act_exists=receiving_act_exists,
        receiving_act_label=receiving_act_label,
        placement_act_label=placement_act_label,
        receiving_act_open_url=receiving_act_open_url,
        placement_act_open_url=placement_act_open_url,
        can_send_act_to_client=can_send_act_to_client,
        can_edit=can_edit,
        return_url=return_url,
        order_context=order_context,
        trip_context=trip_context,
    )


def can_create_receiving_act_helper(task):
    return can_create_receiving_act(task)


def handle_task_detail_post(*, request, task: Task, state: TaskDetailState):
    role = _views().get_request_role(request)
    comment_form = _views().TaskCommentForm()
    attachment_form = _views().TaskAttachmentForm()
    action = request.POST.get("action")
    if action == "complete":
        if task.route and "/orders/other/" in task.route:
            messages.error(request, "Заполните результат в заявке перед завершением задачи")
            return HttpResponseRedirect(task.route), comment_form, attachment_form
        if state.can_complete:
            sent = False
            if task.assigned_to and task.assigned_to.role == "manager":
                sent = send_receiving_to_warehouse(task, request)
            task.status = "done"
            task.save(update_fields=["status", "updated_at"])
            if sent:
                messages.success(request, "Заявка отправлена на склад")
            else:
                messages.success(request, "Задача отмечена как выполненная")
        else:
            messages.error(request, "Недостаточно прав для завершения задачи")
        return redirect("todo:detail", pk=task.pk), comment_form, attachment_form
    if action == "send_act_to_client":
        order_id = _views()._extract_receiving_order_id(task.route)
        if order_id:
            entries = list(
                OrderAuditEntry.objects.filter(order_id=order_id, order_type="receiving")
                .select_related("agency")
                .order_by("created_at")
            )
        else:
            entries = []
        if entries and role in {"manager", "head_manager", "director", "admin"}:
            if _views().order_views._send_act_to_client(order_id, entries, request.user):
                messages.success(request, "Акт отправлен клиенту")
            else:
                messages.error(request, "Не удалось отправить акт клиенту")
        else:
            messages.error(request, "Недостаточно прав для отправки акта")
        return redirect("todo:detail", pk=task.pk), comment_form, attachment_form
    if action == "rework":
        if not state.can_edit:
            messages.error(request, "Вернуть на доработку может только постановщик")
            return redirect("todo:detail", pk=task.pk), comment_form, attachment_form
        if task.status != "done":
            messages.error(request, "Вернуть на доработку можно только выполненную задачу")
            return redirect("todo:detail", pk=task.pk), comment_form, attachment_form
        comment_form = _views().TaskCommentForm(request.POST)
        if comment_form.is_valid():
            comment = comment_form.save(commit=False)
            comment.task = task
            if request.user.is_authenticated:
                comment.author = request.user
            comment.save()
            task.status = "in_progress"
            task.save(update_fields=["status", "updated_at"])
            messages.success(request, "Задача возвращена на доработку")
            return redirect("todo:detail", pk=task.pk), comment_form, attachment_form
        return None, comment_form, attachment_form
    if action == "comment":
        comment_form = _views().TaskCommentForm(request.POST)
        if comment_form.is_valid():
            comment = comment_form.save(commit=False)
            comment.task = task
            if request.user.is_authenticated:
                comment.author = request.user
            comment.save()
            messages.success(request, "Комментарий добавлен")
            return redirect("todo:detail", pk=task.pk), comment_form, attachment_form
        return None, comment_form, attachment_form
    if action == "attach":
        attachment_form = _views().TaskAttachmentForm(request.POST, request.FILES)
        if attachment_form.is_valid():
            files = request.FILES.getlist("files")
            if not files:
                messages.error(request, "Файлы не выбраны")
            else:
                for uploaded_file in files:
                    TaskAttachment.objects.create(
                        task=task,
                        uploaded_by=request.user if request.user.is_authenticated else None,
                        file=uploaded_file,
                    )
                messages.success(request, "Файлы загружены")
                return redirect("todo:detail", pk=task.pk), comment_form, attachment_form
        return None, comment_form, attachment_form
    messages.error(request, "Неизвестное действие")
    return None, comment_form, attachment_form


def build_task_detail_context(*, request, task: Task, comment_form=None, attachment_form=None) -> dict:
    state = build_task_detail_state(request=request, task=task)
    read_only = _is_daily_problem_control_manager_view(
        task=task,
        role=_views().get_request_role(request),
    )
    comments = task.comments.select_related("author").order_by("-created_at")
    attachments = task.attachments.select_related("uploaded_by").order_by("-uploaded_at")
    return {
        "task": task,
        "comments": comments,
        "attachments": attachments,
        "comment_form": comment_form or _views().TaskCommentForm(),
        "attachment_form": attachment_form or _views().TaskAttachmentForm(),
        "read_only": read_only,
        "can_complete": state.can_complete and not read_only,
        "can_create_receiving_act": state.can_create_receiving_act,
        "receiving_act_url": state.receiving_act_url,
        "can_create_placement_act": state.can_create_placement_act,
        "placement_act_url": state.placement_act_url,
        "placement_act_exists": state.placement_act_exists,
        "receiving_act_exists": state.receiving_act_exists,
        "receiving_act_label": state.receiving_act_label,
        "placement_act_label": state.placement_act_label,
        "receiving_act_open_url": state.receiving_act_open_url,
        "placement_act_open_url": state.placement_act_open_url,
        "can_send_act_to_client": state.can_send_act_to_client,
        "can_edit": state.can_edit and not read_only,
        "return_url": state.return_url,
        "order_context": state.order_context,
        "trip_context": state.trip_context,
    }


def _record_internal_task_event(*, task: Task, request, text: str) -> None:
    TaskComment.objects.create(
        task=task,
        author=request.user if request.user.is_authenticated else None,
        body=f"Событие: {text}",
    )


def build_internal_task_detail_context(
    *,
    request,
    task: Task,
    comment_form=None,
    attachment_form=None,
) -> dict:
    checklist_items = list(
        task.checklist_items.select_related("completed_by").order_by("position", "id")
    )
    checklist_completed = sum(1 for item in checklist_items if item.is_completed)
    can_work = internal_task_can_work(request=request, task=task)
    can_complete = internal_task_can_complete(request=request, task=task)
    return {
        "task": task,
        "participants": task.participants.filter(is_active=True).order_by("full_name"),
        "checklist_items": checklist_items,
        "checklist_total": len(checklist_items),
        "checklist_completed": checklist_completed,
        "checklist_percent": (
            round(checklist_completed * 100 / len(checklist_items))
            if checklist_items
            else 0
        ),
        "comments": task.comments.select_related("author").order_by("-created_at"),
        "attachments": task.attachments.select_related("uploaded_by").order_by("-uploaded_at"),
        "comment_form": comment_form or _views().TaskCommentForm(),
        "attachment_form": attachment_form or _views().TaskAttachmentForm(),
        "can_work": can_work,
        "can_start": can_work and task.status == "backlog",
        "can_pause": can_work and task.status == "in_progress",
        "can_resume": can_work and task.status == "blocked",
        "can_complete": can_complete,
        "completion_blocked": (
            can_work
            and task.assigned_to_id == getattr(get_request_employee(request), "id", None)
            and not can_complete
        ),
        "can_edit": internal_task_can_edit(request=request, task=task),
        "can_return": internal_task_can_return(request=request, task=task),
        "can_delete": (
            task.status == "backlog"
            and (
                task.created_by_id == getattr(request.user, "id", None)
                or internal_task_has_oversight(request)
            )
        ),
        "return_url": _views().resolve_cabinet_url(_views().get_request_role(request)),
    }


def handle_internal_task_detail_post(*, request, task: Task):
    comment_form = _views().TaskCommentForm()
    attachment_form = _views().TaskAttachmentForm()
    action = str(request.POST.get("action") or "").strip()

    if action == "comment":
        comment_form = _views().TaskCommentForm(request.POST)
        if comment_form.is_valid():
            comment = comment_form.save(commit=False)
            comment.task = task
            comment.author = request.user if request.user.is_authenticated else None
            comment.save()
            messages.success(request, "Комментарий добавлен")
            return redirect("todo:detail", pk=task.pk), comment_form, attachment_form
        return None, comment_form, attachment_form

    if action == "attach":
        attachment_form = _views().TaskAttachmentForm(request.POST, request.FILES)
        if attachment_form.is_valid():
            files = request.FILES.getlist("files")
            if not files:
                messages.error(request, "Файлы не выбраны")
            else:
                for uploaded_file in files:
                    TaskAttachment.objects.create(
                        task=task,
                        uploaded_by=request.user if request.user.is_authenticated else None,
                        file=uploaded_file,
                    )
                messages.success(request, "Файлы загружены")
                return redirect("todo:detail", pk=task.pk), comment_form, attachment_form
        return None, comment_form, attachment_form

    if action == "toggle_checklist":
        with transaction.atomic():
            locked_task = Task.objects.select_for_update().get(pk=task.pk)
            if not internal_task_can_work(request=request, task=locked_task):
                messages.error(request, "Изменять чек-лист могут только исполнители задачи")
            else:
                item = get_object_or_404(
                    TaskChecklistItem.objects.select_for_update(),
                    pk=request.POST.get("item_id"),
                    task=locked_task,
                )
                item.is_completed = not item.is_completed
                item.completed_by = request.user if item.is_completed else None
                item.completed_at = timezone.now() if item.is_completed else None
                item.save(update_fields=["is_completed", "completed_by", "completed_at"])
                if locked_task.status == "backlog":
                    locked_task.status = "in_progress"
                    locked_task.save(update_fields=["status", "updated_at"])
                messages.success(request, "Чек-лист обновлён")
        return redirect("todo:detail", pk=task.pk), comment_form, attachment_form

    if action in {"start", "pause", "resume", "complete"}:
        with transaction.atomic():
            locked_task = Task.objects.select_for_update().get(pk=task.pk)
            if action == "complete":
                if not internal_task_can_complete(request=request, task=locked_task):
                    if locked_task.checklist_items.filter(is_completed=False).exists():
                        messages.error(request, "Сначала выполните все пункты чек-листа")
                    else:
                        messages.error(request, "Завершить задачу может только основной ответственный")
                else:
                    locked_task.status = "done"
                    locked_task.save(update_fields=["status", "updated_at"])
                    _record_internal_task_event(
                        task=locked_task,
                        request=request,
                        text="задача выполнена.",
                    )
                    messages.success(request, "Задача выполнена")
            elif not internal_task_can_work(request=request, task=locked_task):
                messages.error(request, "Действие доступно только исполнителям задачи")
            else:
                transitions = {
                    "start": ("backlog", "in_progress", "задача взята в работу."),
                    "pause": ("in_progress", "blocked", "задача приостановлена."),
                    "resume": ("blocked", "in_progress", "работа по задаче продолжена."),
                }
                expected_status, next_status, event_text = transitions[action]
                if locked_task.status != expected_status:
                    messages.error(request, "Статус задачи уже изменился. Обновите страницу.")
                else:
                    locked_task.status = next_status
                    locked_task.save(update_fields=["status", "updated_at"])
                    _record_internal_task_event(
                        task=locked_task,
                        request=request,
                        text=event_text,
                    )
                    messages.success(request, "Статус задачи обновлён")
        return redirect("todo:detail", pk=task.pk), comment_form, attachment_form

    if action == "rework":
        if not internal_task_can_return(request=request, task=task):
            messages.error(request, "Вернуть задачу может только постановщик")
            return redirect("todo:detail", pk=task.pk), comment_form, attachment_form
        comment_form = _views().TaskCommentForm(request.POST)
        if not comment_form.is_valid():
            messages.error(request, "Укажите причину возврата")
            return None, comment_form, attachment_form
        with transaction.atomic():
            locked_task = Task.objects.select_for_update().get(pk=task.pk)
            if locked_task.status != "done":
                messages.error(request, "Статус задачи уже изменился. Обновите страницу.")
            else:
                comment = comment_form.save(commit=False)
                comment.task = locked_task
                comment.author = request.user if request.user.is_authenticated else None
                comment.save()
                locked_task.status = "in_progress"
                locked_task.save(update_fields=["status", "updated_at"])
                messages.success(request, "Задача возвращена на доработку")
        return redirect("todo:detail", pk=task.pk), comment_form, attachment_form

    messages.error(request, "Неизвестное действие")
    return None, comment_form, attachment_form
