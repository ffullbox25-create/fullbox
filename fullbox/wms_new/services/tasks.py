from __future__ import annotations

from datetime import datetime, time
from decimal import Decimal, InvalidOperation
from uuid import uuid4

from django.db import transaction
from django.utils import timezone

from ..models import (
    WmsNewEvent,
    WmsNewProduct,
    WmsNewTask,
    WmsNewTaskAttachment,
    WmsNewTaskBox,
    WmsNewTaskItem,
    WmsNewTaskService,
)


class TaskOperationError(Exception):
    pass


TASK_BOARD_STAGES = {
    WmsNewTask.TYPE_ACCEPTANCE: (
        ("new", "Новая"),
        ("allowed", "Обработка разрешена"),
        ("accepting", "Приемка товара"),
        ("accepted", "Приемка завершена"),
        ("placing", "Размещение на хранение"),
    ),
    WmsNewTask.TYPE_PROCESSING: (
        ("new", "Новая"),
        ("allowed", "Обработка разрешена"),
        ("picking", "Подбор товара"),
        ("processing", "Обработка"),
        ("processed", "Обработка завершена"),
        ("placing", "Размещение на хранение"),
    ),
    WmsNewTask.TYPE_SHIPMENT: (
        ("new", "Новая"),
        ("allowed", "Обработка разрешена"),
        ("picking", "Подбор товара"),
        ("processing", "Обработка"),
        ("processed", "Обработка завершена"),
        ("awaiting_shipment", "Ожидает отгрузки"),
        ("delivering", "Доставляется"),
    ),
    WmsNewTask.TYPE_OTHER: (
        ("new", "Новая"),
        ("allowed", "Обработка разрешена"),
        ("processing", "Обработка"),
    ),
}


def board_stage_for(task: WmsNewTask) -> str:
    stages = TASK_BOARD_STAGES.get(task.workflow_type, TASK_BOARD_STAGES[WmsNewTask.TYPE_OTHER])
    stage_keys = tuple(item[0] for item in stages)
    saved = str((task.source_snapshot or {}).get("board_stage") or "")
    if saved in stage_keys:
        return saved
    if task.status == WmsNewTask.STATUS_NEW:
        return stage_keys[0]
    if task.status == WmsNewTask.STATUS_BLOCKED:
        return stage_keys[1] if len(stage_keys) > 1 else stage_keys[0]
    if task.status == WmsNewTask.STATUS_IN_PROGRESS:
        return stage_keys[min(2, len(stage_keys) - 1)]
    return stage_keys[-1]


def _decimal(value, label: str) -> Decimal:
    try:
        result = Decimal(str(value or "0").replace(",", "."))
    except (InvalidOperation, ValueError) as exc:
        raise TaskOperationError(f"Поле «{label}» заполнено неверно.") from exc
    if result < 0:
        raise TaskOperationError(f"Поле «{label}» не может быть отрицательным.")
    return result


def _pilot_task(task_id: int, *, allow_done: bool = False) -> WmsNewTask:
    task = WmsNewTask.objects.select_for_update().get(pk=task_id)
    if not allow_done and task.status == WmsNewTask.STATUS_DONE:
        raise TaskOperationError("Завершенную задачу изменять нельзя.")
    return task


def apply_board_action(*, task_id: int, action: str, actor=None) -> WmsNewTask:
    """Advance/cancel a pilot board card without touching the source task."""

    with transaction.atomic():
        task = WmsNewTask.objects.select_for_update().get(pk=task_id)
        stages = TASK_BOARD_STAGES.get(task.workflow_type)
        if not stages:
            raise TaskOperationError("Для этого типа задачи не настроена доска.")
        stage_keys = tuple(item[0] for item in stages)
        current_stage = board_stage_for(task)
        before = {
            "status": task.status,
            "board_stage": current_stage,
            "pilot_revision": task.pilot_revision,
        }
        snapshot = dict(task.source_snapshot or {})
        if action == "cancel":
            task.status = WmsNewTask.STATUS_DONE
            snapshot["board_cancelled"] = True
            snapshot["board_stage"] = current_stage
        elif action == "complete":
            if task.workflow_type != WmsNewTask.TYPE_OTHER and not task.items.exists():
                raise TaskOperationError("В задаче нет товаров.")
            if task.workflow_type in {
                WmsNewTask.TYPE_PROCESSING,
                WmsNewTask.TYPE_SHIPMENT,
            } and not task.tariff_finalized:
                raise TaskOperationError("Сначала завершите тарификацию задачи.")
            task.status = WmsNewTask.STATUS_DONE
            task.completed_at = timezone.now()
            snapshot["board_cancelled"] = False
            snapshot["board_stage"] = stage_keys[-1]
        elif action == "advance":
            try:
                position = stage_keys.index(current_stage)
            except ValueError as exc:
                raise TaskOperationError("Неизвестная стадия карточки.") from exc
            if position >= len(stage_keys) - 1:
                if task.workflow_type != WmsNewTask.TYPE_OTHER and not task.items.exists():
                    raise TaskOperationError("В задаче нет товаров.")
                if task.workflow_type in {
                    WmsNewTask.TYPE_PROCESSING,
                    WmsNewTask.TYPE_SHIPMENT,
                } and not task.tariff_finalized:
                    raise TaskOperationError("Сначала завершите тарификацию задачи.")
                task.status = WmsNewTask.STATUS_DONE
                task.completed_at = timezone.now()
                snapshot["board_stage"] = stage_keys[-1]
            else:
                snapshot["board_stage"] = stage_keys[position + 1]
                task.status = WmsNewTask.STATUS_IN_PROGRESS
            snapshot["board_cancelled"] = False
        else:
            raise TaskOperationError("Неизвестное действие с карточкой.")
        task.source_snapshot = snapshot
        task.pilot_revision += 1
        task.save(update_fields=("status", "source_snapshot", "pilot_revision", "completed_at", "updated_at"))
        WmsNewEvent.objects.create(
            entity_type="task",
            entity_id=task.id,
            action=f"board_{action}",
            actor=actor,
            before=before,
            after={
                "status": task.status,
                "board_stage": snapshot.get("board_stage"),
                "board_cancelled": snapshot.get("board_cancelled", False),
                "pilot_revision": task.pilot_revision,
            },
        )
    return task


@transaction.atomic
def save_task_details(*, task_id: int, actor=None, **values) -> WmsNewTask:
    task = _pilot_task(task_id)
    before = {
        "description": task.description,
        "internal_comment": task.internal_comment,
        "delivery_address": task.delivery_address,
    }
    for field in (
        "title", "description", "internal_comment", "delivery_address", "contact_name",
        "contact_phone", "vehicle_model", "vehicle_number", "driver_name",
        "driver_phone", "warehouse_code",
    ):
        if field in values:
            setattr(task, field, str(values.get(field) or "").strip())
    if not task.title:
        raise TaskOperationError("Укажите название задачи.")
    due_date = values.get("due_date")
    if due_date is not None:
        task.due_date = timezone.make_aware(datetime.combine(due_date, time(23, 59, 59)))
    task.pilot_revision += 1
    task.save()
    WmsNewEvent.objects.create(
        entity_type="task", entity_id=task.id, action="details_update", actor=actor,
        before=before,
        after={
            "title": task.title,
            "delivery_address": task.delivery_address,
            "contact_name": task.contact_name,
            "due_date": task.due_date.isoformat() if task.due_date else None,
        },
    )
    return task


@transaction.atomic
def add_task_item(
    *, task_id: int, product_id: int, planned_qty, unit_price=0,
    technical_requirement: str = "", actor=None
) -> WmsNewTaskItem:
    task = _pilot_task(task_id)
    product = WmsNewProduct.objects.get(pk=product_id, is_archived=False)
    if task.agency_id and product.agency_id != task.agency_id:
        raise TaskOperationError("Товар принадлежит другому партнеру.")
    try:
        quantity = int(planned_qty)
    except (TypeError, ValueError) as exc:
        raise TaskOperationError("Количество должно быть целым числом.") from exc
    if quantity <= 0:
        raise TaskOperationError("Количество должно быть больше нуля.")
    item, created = WmsNewTaskItem.objects.select_for_update().get_or_create(
        task=task,
        product=product,
        defaults={
            "planned_qty": quantity,
            "unit_price": _decimal(unit_price, "Цена"),
            "technical_requirement": str(technical_requirement or "").strip(),
        },
    )
    if not created:
        item.planned_qty += quantity
        item.unit_price = _decimal(unit_price, "Цена")
        item.technical_requirement = str(technical_requirement or item.technical_requirement).strip()
        item.save()
    task.pilot_revision += 1
    task.save(update_fields=("pilot_revision", "updated_at"))
    WmsNewEvent.objects.create(
        entity_type="task", entity_id=task.id, action="item_add", actor=actor,
        after={"item_id": item.id, "product_id": product.id, "planned_qty": item.planned_qty},
    )
    return item


@transaction.atomic
def remove_task_item(*, task_id: int, item_id: int, actor=None) -> None:
    task = _pilot_task(task_id)
    item = WmsNewTaskItem.objects.select_for_update().get(pk=item_id, task=task)
    if item.processed_qty:
        raise TaskOperationError("Товар уже участвовал в обработке и не может быть удален.")
    item.delete()
    task.pilot_revision += 1
    task.save(update_fields=("pilot_revision", "updated_at"))
    WmsNewEvent.objects.create(
        entity_type="task", entity_id=task.id, action="item_remove", actor=actor,
        before={"item_id": item_id},
    )


@transaction.atomic
def create_task_box(*, task_id: int, code: str = "", actor=None) -> WmsNewTaskBox:
    task = _pilot_task(task_id)
    clean_code = str(code or "").strip() or f"FBN-TASK-{task.id}-{uuid4().hex[:6].upper()}"
    if task.boxes.filter(code__iexact=clean_code).exists():
        raise TaskOperationError("Короб с таким кодом уже есть в задаче.")
    box = WmsNewTaskBox.objects.create(task=task, code=clean_code, created_by=actor)
    task.pilot_revision += 1
    task.save(update_fields=("pilot_revision", "updated_at"))
    WmsNewEvent.objects.create(
        entity_type="task", entity_id=task.id, action="box_create", actor=actor,
        after={"box_id": box.id, "code": box.code},
    )
    return box


@transaction.atomic
def assign_task_items_to_box(
    *, task_id: int, box_id: int | None, item_ids: list[int], actor=None
) -> int:
    task = _pilot_task(task_id)
    box = None
    if box_id:
        box = WmsNewTaskBox.objects.get(pk=box_id, task=task, status=WmsNewTaskBox.STATUS_OPEN)
    ids = sorted({int(value) for value in item_ids if int(value) > 0})
    items = list(WmsNewTaskItem.objects.select_for_update().filter(task=task, id__in=ids))
    if not ids or len(items) != len(ids):
        raise TaskOperationError("Выберите товары этой задачи.")
    for item in items:
        item.box = box
        item.save(update_fields=("box", "updated_at"))
    task.pilot_revision += 1
    task.save(update_fields=("pilot_revision", "updated_at"))
    WmsNewEvent.objects.create(
        entity_type="task", entity_id=task.id, action="items_box_assign", actor=actor,
        after={"box_id": box.id if box else None, "item_ids": ids},
    )
    return len(items)


@transaction.atomic
def add_task_service(
    *, task_id: int, name: str, unit_price, quantity, item_id: int | None = None, actor=None
) -> WmsNewTaskService:
    task = _pilot_task(task_id)
    if task.tariff_finalized:
        raise TaskOperationError("Тарификация задачи уже завершена.")
    clean_name = str(name or "").strip()
    if not clean_name:
        raise TaskOperationError("Укажите услугу.")
    item = None
    if item_id:
        item = WmsNewTaskItem.objects.get(pk=item_id, task=task)
    clean_quantity = _decimal(quantity, "Количество")
    if clean_quantity <= 0:
        raise TaskOperationError("Количество услуги должно быть больше нуля.")
    service = WmsNewTaskService.objects.create(
        task=task,
        item=item,
        name=clean_name,
        unit_price=_decimal(unit_price, "Цена"),
        quantity=clean_quantity,
        created_by=actor,
    )
    task.pilot_revision += 1
    task.save(update_fields=("pilot_revision", "updated_at"))
    WmsNewEvent.objects.create(
        entity_type="task", entity_id=task.id, action="service_add", actor=actor,
        after={"service_id": service.id, "name": service.name},
    )
    return service


@transaction.atomic
def finalize_task_tariff(*, task_id: int, actor=None) -> WmsNewTask:
    task = _pilot_task(task_id)
    if not task.items.exists():
        raise TaskOperationError("Добавьте в задачу хотя бы один товар.")
    task.tariff_finalized = True
    task.pilot_revision += 1
    task.save(update_fields=("tariff_finalized", "pilot_revision", "updated_at"))
    WmsNewEvent.objects.create(
        entity_type="task", entity_id=task.id, action="tariff_finalize", actor=actor,
        after={"services": task.services.count()},
    )
    return task


@transaction.atomic
def attach_task_file(*, task_id: int, uploaded_file, actor=None) -> WmsNewTaskAttachment:
    task = _pilot_task(task_id)
    if uploaded_file is None:
        raise TaskOperationError("Выберите файл.")
    attachment = WmsNewTaskAttachment.objects.create(
        task=task,
        file=uploaded_file,
        original_name=str(getattr(uploaded_file, "name", "файл"))[:255],
        uploaded_by=actor,
    )
    task.pilot_revision += 1
    task.save(update_fields=("pilot_revision", "updated_at"))
    WmsNewEvent.objects.create(
        entity_type="task", entity_id=task.id, action="file_upload", actor=actor,
        after={"attachment_id": attachment.id, "name": attachment.original_name},
    )
    return attachment


@transaction.atomic
def record_task_item(*, task_id: int, item_id: int, processed_qty, actor=None) -> WmsNewTaskItem:
    task = _pilot_task(task_id)
    item = WmsNewTaskItem.objects.select_for_update().get(pk=item_id, task=task)
    try:
        quantity = int(processed_qty)
    except (TypeError, ValueError) as exc:
        raise TaskOperationError("Обработанное количество должно быть целым числом.") from exc
    if quantity < 0 or quantity > item.planned_qty:
        raise TaskOperationError("Обработанное количество должно быть от нуля до планового.")
    before = item.processed_qty
    item.processed_qty = quantity
    item.save(update_fields=("processed_qty", "updated_at"))
    task.pilot_revision += 1
    task.save(update_fields=("pilot_revision", "updated_at"))
    WmsNewEvent.objects.create(
        entity_type="task", entity_id=task.id, action="item_process", actor=actor,
        before={"item_id": item.id, "processed_qty": before},
        after={"item_id": item.id, "processed_qty": item.processed_qty},
    )
    return item


@transaction.atomic
def set_task_box_status(*, task_id: int, box_id: int, status: str, actor=None) -> WmsNewTaskBox:
    task = _pilot_task(task_id)
    if status not in dict(WmsNewTaskBox.STATUS_CHOICES):
        raise TaskOperationError("Неизвестный статус короба.")
    box = WmsNewTaskBox.objects.select_for_update().get(pk=box_id, task=task)
    if status == WmsNewTaskBox.STATUS_CLOSED and not box.items.exists():
        raise TaskOperationError("Пустой короб нельзя закрыть.")
    before = box.status
    box.status = status
    box.save(update_fields=("status", "updated_at"))
    task.pilot_revision += 1
    task.save(update_fields=("pilot_revision", "updated_at"))
    WmsNewEvent.objects.create(
        entity_type="task", entity_id=task.id, action="box_status", actor=actor,
        before={"box_id": box.id, "status": before},
        after={"box_id": box.id, "status": box.status},
    )
    return box


@transaction.atomic
def remove_task_service(*, task_id: int, service_id: int, actor=None) -> None:
    task = _pilot_task(task_id)
    if task.tariff_finalized:
        raise TaskOperationError("Тарификация задачи уже завершена.")
    service = WmsNewTaskService.objects.select_for_update().get(pk=service_id, task=task)
    before = {"service_id": service.id, "name": service.name}
    service.delete()
    task.pilot_revision += 1
    task.save(update_fields=("pilot_revision", "updated_at"))
    WmsNewEvent.objects.create(
        entity_type="task", entity_id=task.id, action="service_remove", actor=actor,
        before=before,
    )


@transaction.atomic
def set_task_client_confirmation(*, task_id: int, confirmed: bool, actor=None) -> WmsNewTask:
    task = _pilot_task(task_id)
    before = task.client_confirmed
    task.client_confirmed = bool(confirmed)
    task.pilot_revision += 1
    task.save(update_fields=("client_confirmed", "pilot_revision", "updated_at"))
    WmsNewEvent.objects.create(
        entity_type="task", entity_id=task.id, action="client_confirmation", actor=actor,
        before={"confirmed": before}, after={"confirmed": task.client_confirmed},
    )
    return task


def create_task(
    *,
    agency,
    workflow_type: str,
    title: str,
    due_date,
    description: str = "",
    priority: str = WmsNewTask.PRIORITY_NORMAL,
    actor=None,
) -> WmsNewTask:
    if workflow_type not in dict(WmsNewTask.TYPE_CHOICES):
        raise TaskOperationError("Укажите тип задачи.")
    title = str(title or "").strip() or dict(WmsNewTask.TYPE_CHOICES)[workflow_type]
    if priority not in dict(WmsNewTask.PRIORITY_CHOICES):
        priority = WmsNewTask.PRIORITY_NORMAL
    due_at = timezone.make_aware(datetime.combine(due_date, time(23, 59, 59)))
    with transaction.atomic():
        task = WmsNewTask.objects.create(
            agency=agency,
            workflow_type=workflow_type,
            title=title,
            description=str(description or "").strip(),
            status=WmsNewTask.STATUS_NEW,
            priority=priority,
            due_date=due_at,
            is_manual=True,
            pilot_revision=1,
            created_by=actor,
        )
        WmsNewEvent.objects.create(
            entity_type="task",
            entity_id=task.id,
            action="create",
            actor=actor,
            after={
                "status": task.status,
                "workflow_type": task.workflow_type,
                "agency_id": task.agency_id,
                "due_date": task.due_date.isoformat(),
            },
        )
    return task


def update_task(
    *,
    task_id: int,
    status: str,
    priority: str,
    assigned_to,
    actor=None,
) -> WmsNewTask:
    if status not in dict(WmsNewTask.STATUS_CHOICES):
        raise TaskOperationError("Неизвестный статус задачи.")
    if priority not in dict(WmsNewTask.PRIORITY_CHOICES):
        raise TaskOperationError("Неизвестный приоритет задачи.")
    with transaction.atomic():
        task = WmsNewTask.objects.select_for_update().get(pk=task_id)
        before = {
            "status": task.status,
            "priority": task.priority,
            "assigned_to_id": task.assigned_to_id,
            "pilot_revision": task.pilot_revision,
        }
        task.status = status
        task.priority = priority
        task.assigned_to = assigned_to
        task.pilot_revision += 1
        task.save(
            update_fields=("status", "priority", "assigned_to", "pilot_revision", "updated_at")
        )
        WmsNewEvent.objects.create(
            entity_type="task",
            entity_id=task.id,
            action="update",
            actor=actor,
            before=before,
            after={
                "status": task.status,
                "priority": task.priority,
                "assigned_to_id": task.assigned_to_id,
                "pilot_revision": task.pilot_revision,
            },
        )
    return task
