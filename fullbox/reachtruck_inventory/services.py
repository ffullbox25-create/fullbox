from datetime import timedelta

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from employees.access import get_employee_for_user
from inventory.models import Inventory, InventoryLine
from sklad.models import WarehouseStockSnapshot
from sku.models import SKU, SKUBarcode

from .models import InventoryTask


def _user_name(user) -> str:
    employee = get_employee_for_user(user)
    employee_name = str(getattr(employee, "full_name", "") or "").strip()
    return employee_name or str(user.get_full_name() or user.username or "").strip()


def _normalized_scan(value) -> str:
    return "".join(str(value or "").strip().upper().split())


def location_scan_codes(task: InventoryTask) -> set[str]:
    location = task.location
    candidates = {
        str(location.location_code or "").strip(),
        str(location.display_name or "").strip(),
        task.location_code,
        f"{location.zone_code}-{location.row_no}-{location.section_no}-{location.tier_no}-{location.cell_no}",
    }
    try:
        from reachtruck.services.putaway_planner import putaway_location_scan_code

        candidates.add(
            putaway_location_scan_code(
                {
                    "zone": location.zone_code,
                    "row": location.row_no,
                    "section": location.section_no,
                    "tier": location.tier_no,
                    "cell": location.cell_no,
                }
            )
        )
    except (ImportError, ValueError, TypeError):
        pass
    return {_normalized_scan(value) for value in candidates if _normalized_scan(value)}


def _lease_duration() -> timedelta:
    seconds = max(
        int(getattr(settings, "REACHTRUCK_INVENTORY_LEASE_SECONDS", 1200) or 1200),
        60,
    )
    return timedelta(seconds=seconds)


def _lease_expired(task: InventoryTask, *, now=None) -> bool:
    if task.status != InventoryTask.STATUS_IN_PROGRESS:
        return False
    now = now or timezone.now()
    return task.lease_expires_at is None or task.lease_expires_at <= now


def _clear_task_lease(task: InventoryTask, *, now) -> None:
    task.status = InventoryTask.STATUS_CREATED
    task.assigned_to = None
    task.assigned_to_name = ""
    task.location_verified_at = None
    task.last_activity_at = None
    task.lease_expires_at = None
    task.counted_at = None
    task.planned_box_count = 0
    task.actual_box_count = None
    task.planned_box_codes = []
    task.planned_pallet_codes = []
    task.discrepancy_pallet_codes = []
    task.discrepancy_box_codes = []
    task.discrepancy_reported_by = None
    task.discrepancy_reported_by_name = ""
    task.discrepancy_reported_at = None
    task.started_at = None
    task.updated_at = now


def _touch_task_lease(task: InventoryTask, *, now) -> None:
    task.last_activity_at = now
    task.lease_expires_at = now + _lease_duration()
    task.updated_at = now


def _clear_line_counts(*, inventory_id: int, location_id: int) -> None:
    InventoryLine.objects.filter(
        inventory_id=inventory_id,
        location_id=location_id,
    ).update(
        actual_qty=None,
        counted_by=None,
        counted_at=None,
    )


@transaction.atomic
def release_expired_tasks(*, task_id=None, inventory_id=None) -> int:
    now = timezone.now()
    queryset = InventoryTask.objects.filter(status=InventoryTask.STATUS_IN_PROGRESS).filter(
        Q(lease_expires_at__isnull=True) | Q(lease_expires_at__lte=now)
    )
    if task_id is not None:
        queryset = queryset.filter(pk=task_id)
    if inventory_id is not None:
        queryset = queryset.filter(inventory_id=inventory_id)
    expired_tasks = list(
        queryset.select_for_update().values("id", "inventory_id", "location_id")
    )
    if not expired_tasks:
        return 0
    task_ids = [row["id"] for row in expired_tasks]
    updated = InventoryTask.objects.filter(id__in=task_ids).update(
        status=InventoryTask.STATUS_CREATED,
        assigned_to=None,
        assigned_to_name="",
        location_verified_at=None,
        last_activity_at=None,
        lease_expires_at=None,
        counted_at=None,
        planned_box_count=0,
        actual_box_count=None,
        planned_box_codes=[],
        planned_pallet_codes=[],
        discrepancy_pallet_codes=[],
        discrepancy_box_codes=[],
        discrepancy_reported_by=None,
        discrepancy_reported_by_name="",
        discrepancy_reported_at=None,
        started_at=None,
        updated_at=now,
    )
    for row in expired_tasks:
        _clear_line_counts(
            inventory_id=row["inventory_id"],
            location_id=row["location_id"],
        )
    return updated


def _current_plan_snapshots(task: InventoryTask):
    inventory = task.inventory
    queryset = WarehouseStockSnapshot.objects.filter(
        location_id=task.location_id,
        location__is_active=True,
        is_archived=False,
        qty__gt=0,
    ).select_related("container", "parent_container").order_by(
        "agency_id", "sku_code", "size", "barcode", "id"
    )
    if inventory.inventory_type == Inventory.TYPE_PARTNER:
        queryset = queryset.filter(agency_id=inventory.agency_id)
    elif inventory.inventory_type == Inventory.TYPE_GOODS:
        sku = inventory.sku
        queryset = queryset.filter(
            Q(sku_ref_id=inventory.sku_id)
            | Q(agency_id=sku.agency_id, sku_code=sku.sku_code)
        )
    return queryset


def _snapshot_line_key(snapshot: WarehouseStockSnapshot) -> tuple:
    return (
        snapshot.agency_id,
        snapshot.sku_ref_id,
        str(snapshot.sku_code or "").strip(),
        str(snapshot.name or "").strip(),
        str(snapshot.size or "").strip(),
        str(snapshot.barcode or "").strip(),
        str(snapshot.goods_type or "").strip(),
    )


def _refresh_task_plan(task: InventoryTask) -> None:
    aggregates: dict[tuple, dict] = {}
    box_codes: set[str] = set()
    pallet_codes: set[str] = set()
    for snapshot in _current_plan_snapshots(task).iterator(chunk_size=500):
        key = _snapshot_line_key(snapshot)
        row = aggregates.setdefault(key, {"planned_qty": 0, "source_snapshot_ids": []})
        row["planned_qty"] += int(snapshot.qty or 0)
        row["source_snapshot_ids"].append(
            {"id": snapshot.id, "version": int(snapshot.snapshot_version or 1)}
        )
        container = snapshot.container
        parent = snapshot.parent_container or getattr(container, "parent_container", None)
        if container and container.container_type == "box":
            box_codes.add(str(container.container_code or snapshot.container_code).strip())
        if container and container.container_type in {"pallet", "mixed_pallet"}:
            pallet_codes.add(str(container.container_code or snapshot.container_code).strip())
        if parent and parent.container_type in {"pallet", "mixed_pallet"}:
            pallet_codes.add(str(parent.container_code or "").strip())

    InventoryLine.objects.filter(
        inventory_id=task.inventory_id,
        location_id=task.location_id,
    ).delete()
    lines = []
    for key, values in aggregates.items():
        agency_id, sku_ref_id, sku_code, name, size, barcode, goods_type = key
        lines.append(
            InventoryLine(
                inventory_id=task.inventory_id,
                location_id=task.location_id,
                agency_id=agency_id,
                sku_ref_id=sku_ref_id,
                sku_code=sku_code,
                name=name,
                size=size,
                barcode=barcode,
                goods_type=goods_type,
                planned_qty=values["planned_qty"],
                source_snapshot_ids=values["source_snapshot_ids"],
            )
        )
    if lines:
        InventoryLine.objects.bulk_create(lines, batch_size=300)
    task.scope_location.planned_qty = sum(
        int(values["planned_qty"] or 0) for values in aggregates.values()
    )
    task.scope_location.save(update_fields=["planned_qty"])
    task.planned_box_codes = sorted(code for code in box_codes if code)
    task.planned_pallet_codes = sorted(code for code in pallet_codes if code)
    task.planned_box_count = len(task.planned_box_codes)
    task.actual_box_count = None


def visible_tasks(user):
    release_expired_tasks()
    return (
        InventoryTask.objects.select_related("inventory", "scope_location", "location", "assigned_to")
        .filter(Q(status=InventoryTask.STATUS_CREATED) | Q(status=InventoryTask.STATUS_IN_PROGRESS, assigned_to=user))
        .order_by("status", "created_at", "id")
    )


def dashboard_counts(user) -> dict[str, int]:
    queryset = visible_tasks(user)
    return {
        "count": queryset.count(),
        "in_progress_count": queryset.filter(status=InventoryTask.STATUS_IN_PROGRESS).count(),
    }


def _assert_owner(task: InventoryTask, user) -> None:
    if task.assigned_to_id != user.id:
        raise PermissionDenied("Задание выполняет другой сотрудник.")


@transaction.atomic
def take_task(task: InventoryTask, user) -> InventoryTask:
    # Lock only the task row. inventory.sku is nullable and PostgreSQL rejects
    # FOR UPDATE when a nullable select_related() join is present.
    task = InventoryTask.objects.select_for_update().get(pk=task.pk)
    now = timezone.now()
    if _lease_expired(task, now=now):
        _clear_task_lease(task, now=now)
    if task.status == InventoryTask.STATUS_CREATED:
        # Older releases could leave an unsubmitted count attached to a task
        # after its lease expired. A fresh claimant must always start clean.
        _clear_task_lease(task, now=now)
        _refresh_task_plan(task)
        task.status = InventoryTask.STATUS_IN_PROGRESS
        task.assigned_to = user
        task.assigned_to_name = _user_name(user)
        task.started_at = now
        _touch_task_lease(task, now=now)
        task.save(
            update_fields=[
                "status",
                "assigned_to",
                "assigned_to_name",
                "started_at",
                "last_activity_at",
                "lease_expires_at",
                "counted_at",
                "planned_box_count",
                "actual_box_count",
                "planned_box_codes",
                "planned_pallet_codes",
                "discrepancy_pallet_codes",
                "discrepancy_box_codes",
                "discrepancy_reported_by",
                "discrepancy_reported_by_name",
                "discrepancy_reported_at",
                "updated_at",
            ]
        )
        inventory = Inventory.objects.select_for_update().get(pk=task.inventory_id)
        if inventory.status == Inventory.STATUS_PENDING:
            inventory.status = Inventory.STATUS_IN_PROGRESS
            inventory.started_at = inventory.started_at or now
            inventory.save(update_fields=["status", "started_at", "updated_at"])
        return task
    if task.status == InventoryTask.STATUS_IN_PROGRESS:
        _assert_owner(task, user)
        _touch_task_lease(task, now=now)
        task.save(update_fields=["last_activity_at", "lease_expires_at", "updated_at"])
        return task
    raise ValidationError("Это задание уже закрыто.")


def verify_location(task: InventoryTask, user, scan_value: str) -> InventoryTask:
    release_expired_tasks(task_id=task.pk)
    with transaction.atomic():
        task = InventoryTask.objects.select_for_update().select_related("location").get(pk=task.pk)
        if task.status != InventoryTask.STATUS_IN_PROGRESS:
            raise ValidationError("Сессия задания истекла. Возьмите его в работу повторно.")
        _assert_owner(task, user)
        normalized = _normalized_scan(scan_value)
        if not normalized:
            raise ValidationError("Скан места пустой.")
        if normalized not in location_scan_codes(task):
            raise ValidationError(f"Ожидается место {task.location_code}.")
        now = timezone.now()
        task.location_verified_at = now
        _touch_task_lease(task, now=now)
        task.save(
            update_fields=[
                "location_verified_at",
                "last_activity_at",
                "lease_expires_at",
                "updated_at",
            ]
        )
    return task


def touch_task_lease(task: InventoryTask, user) -> InventoryTask:
    release_expired_tasks(task_id=task.pk)
    with transaction.atomic():
        task = InventoryTask.objects.select_for_update().get(pk=task.pk)
        if task.status != InventoryTask.STATUS_IN_PROGRESS:
            raise ValidationError("Сессия задания истекла.")
        _assert_owner(task, user)
        now = timezone.now()
        _touch_task_lease(task, now=now)
        task.save(update_fields=["last_activity_at", "lease_expires_at", "updated_at"])
    return task


@transaction.atomic
def release_task(task: InventoryTask, user) -> InventoryTask:
    task = InventoryTask.objects.select_for_update().get(pk=task.pk)
    if task.status == InventoryTask.STATUS_CREATED:
        return task
    if task.status != InventoryTask.STATUS_IN_PROGRESS:
        raise ValidationError("Закрытое задание нельзя освободить.")
    _assert_owner(task, user)
    now = timezone.now()
    inventory_id = task.inventory_id
    location_id = task.location_id
    _clear_task_lease(task, now=now)
    task.save(
        update_fields=[
            "status",
            "assigned_to",
            "assigned_to_name",
            "location_verified_at",
            "last_activity_at",
            "lease_expires_at",
            "counted_at",
            "planned_box_count",
            "actual_box_count",
            "planned_box_codes",
            "planned_pallet_codes",
            "discrepancy_pallet_codes",
            "discrepancy_box_codes",
            "discrepancy_reported_by",
            "discrepancy_reported_by_name",
            "discrepancy_reported_at",
            "started_at",
            "updated_at",
        ]
    )
    _clear_line_counts(inventory_id=inventory_id, location_id=location_id)
    return task


def _task_lines(task: InventoryTask):
    return InventoryLine.objects.filter(
        inventory_id=task.inventory_id,
        location_id=task.location_id,
    ).select_related("agency", "sku_ref")


def _task_has_discrepancy(task: InventoryTask) -> bool:
    return (
        task.actual_box_count is not None
        and int(task.actual_box_count) != int(task.planned_box_count)
    )


def _task_is_empty_count(task: InventoryTask) -> bool:
    return task.counted_at is not None and int(task.actual_box_count or 0) == 0


def _current_plan_total(task: InventoryTask) -> int:
    return sum(
        int(qty or 0)
        for qty in _current_plan_snapshots(task).values_list("qty", flat=True)
    )


def _current_box_codes(task: InventoryTask) -> list[str]:
    codes = set()
    for snapshot in _current_plan_snapshots(task).iterator(chunk_size=500):
        container = snapshot.container
        if container and container.container_type == "box":
            code = str(container.container_code or snapshot.container_code or "").strip()
            if code:
                codes.add(code)
    return sorted(codes)


def _sync_inventory_completion(inventory: Inventory, completed_by) -> None:
    tasks = inventory.execution_tasks.all()
    if not tasks.exists() or tasks.exclude(status=InventoryTask.STATUS_COMPLETED).exists():
        return
    names = list(
        tasks.exclude(assigned_to_name="")
        .values_list("assigned_to_name", flat=True)
        .distinct()
        .order_by("assigned_to_name")
    )
    inventory.status = Inventory.STATUS_COMPLETED
    inventory.completed_at = timezone.now()
    inventory.performed_by_name = ", ".join(names) or _user_name(completed_by)
    inventory.save(update_fields=["status", "completed_at", "performed_by_name", "updated_at"])


def save_box_count(task: InventoryTask, user, raw_box_count) -> InventoryTask:
    release_expired_tasks(task_id=task.pk)
    with transaction.atomic():
        task = InventoryTask.objects.select_for_update().select_related("inventory", "location").get(pk=task.pk)
        if task.status != InventoryTask.STATUS_IN_PROGRESS:
            raise ValidationError("Сессия задания истекла. Возьмите его в работу повторно.")
        _assert_owner(task, user)
        if task.location_verified_at is None:
            raise ValidationError("Сначала отсканируйте складское место.")
        if task.counted_at is not None:
            raise ValidationError("Количество коробов уже сохранено.")

        try:
            actual_box_count = int(str(raw_box_count).strip())
        except (AttributeError, TypeError, ValueError):
            raise ValidationError("Введите целое количество коробов.")
        if actual_box_count < 0:
            raise ValidationError("Количество коробов не может быть отрицательным.")

        now = timezone.now()
        task.actual_box_count = actual_box_count
        task.counted_at = now
        task.last_activity_at = now
        if _task_has_discrepancy(task):
            _touch_task_lease(task, now=now)
            task.save(
                update_fields=[
                    "actual_box_count",
                    "counted_at",
                    "last_activity_at",
                    "lease_expires_at",
                    "updated_at",
                ]
            )
            return task

        task.status = InventoryTask.STATUS_COMPLETED
        task.completed_at = now
        task.lease_expires_at = None
        task.save(
            update_fields=[
                "status",
                "actual_box_count",
                "counted_at",
                "completed_at",
                "last_activity_at",
                "lease_expires_at",
                "updated_at",
            ]
        )
        inventory = Inventory.objects.select_for_update().get(pk=task.inventory_id)
        _sync_inventory_completion(inventory, user)
    return task


def _resolve_sku(inventory: Inventory, code: str) -> SKU:
    normalized = str(code or "").strip()
    if not normalized:
        raise ValidationError("Укажите штрихкод или артикул товара.")
    barcode_queryset = SKUBarcode.objects.select_related("sku", "sku__agency").filter(
        value__iexact=normalized,
        sku__deleted=False,
    )
    if inventory.inventory_type == Inventory.TYPE_PARTNER and inventory.agency_id:
        barcode_queryset = barcode_queryset.filter(agency_id=inventory.agency_id)
    elif inventory.inventory_type == Inventory.TYPE_GOODS and inventory.sku_id:
        barcode_queryset = barcode_queryset.filter(sku_id=inventory.sku_id)
    barcode_matches = list(barcode_queryset[:2])
    if len(barcode_matches) > 1:
        raise ValidationError(
            "Штрихкод используется у нескольких партнеров. "
            "Для поштучного сканирования выберите инвентаризацию по партнеру или товару."
        )
    if barcode_matches:
        sku = barcode_matches[0].sku
    else:
        queryset = SKU.objects.filter(deleted=False, sku_code__iexact=normalized).select_related("agency")
        if inventory.agency_id:
            queryset = queryset.filter(agency_id=inventory.agency_id)
        matches = list(queryset[:2])
        if not matches:
            raise ValidationError("Товар не найден.")
        if len(matches) > 1:
            raise ValidationError("Артикул найден у нескольких партнеров. Сканируйте уникальный штрихкод.")
        sku = matches[0]
    if inventory.inventory_type == Inventory.TYPE_PARTNER and sku.agency_id != inventory.agency_id:
        raise ValidationError("Товар не относится к партнеру этой инвентаризации.")
    if inventory.inventory_type == Inventory.TYPE_GOODS and sku.id != inventory.sku_id:
        raise ValidationError("Этот товар не входит в инвентаризацию.")
    if sku.agency_id is None:
        raise ValidationError("У товара не указан партнер.")
    return sku


def add_unexpected_line(task: InventoryTask, user, *, code: str, actual_qty) -> InventoryLine:
    release_expired_tasks(task_id=task.pk)
    with transaction.atomic():
        task = InventoryTask.objects.select_for_update().select_related("inventory", "location").get(pk=task.pk)
        if task.status != InventoryTask.STATUS_IN_PROGRESS or task.location_verified_at is None:
            raise ValidationError("Сначала возьмите задание и отсканируйте место.")
        _assert_owner(task, user)
        if task.counted_at is not None:
            raise ValidationError("Пересчет уже сохранен. Дополнительный товар изменить нельзя.")
        try:
            qty = int(str(actual_qty).strip())
        except (TypeError, ValueError):
            raise ValidationError("Введите целое фактическое количество.")
        if qty < 0:
            raise ValidationError("Количество не может быть отрицательным.")
        sku = _resolve_sku(task.inventory, code)
        now = timezone.now()
        primary_barcode = sku.barcodes.filter(is_primary=True).values_list("value", flat=True).first() or ""
        line = InventoryLine.objects.filter(
            inventory_id=task.inventory_id,
            location_id=task.location_id,
            agency_id=sku.agency_id,
            sku_code=sku.sku_code,
            size=str(sku.size or ""),
        ).first()
        if line is None:
            line = InventoryLine.objects.create(
                inventory_id=task.inventory_id,
                location_id=task.location_id,
                agency_id=sku.agency_id,
                sku_ref=sku,
                sku_code=sku.sku_code,
                name=sku.name,
                size=str(sku.size or ""),
                barcode=primary_barcode,
                planned_qty=0,
                actual_qty=qty,
                counted_by=user,
                counted_at=now,
            )
        else:
            line.actual_qty = qty
            line.counted_by = user
            line.counted_at = now
            line.save(update_fields=["actual_qty", "counted_by", "counted_at", "updated_at"])
        _touch_task_lease(task, now=now)
        task.save(update_fields=["last_activity_at", "lease_expires_at", "updated_at"])
    return line


def scan_discrepancy_container(
    task: InventoryTask,
    user,
    *,
    container_type: str,
    scan_code: str,
) -> InventoryTask:
    if container_type not in {"pallet", "box"}:
        raise ValidationError("Неизвестный тип складского места.")
    normalized_code = str(scan_code or "").strip()
    if not normalized_code:
        label = "паллеты" if container_type == "pallet" else "короба"
        raise ValidationError(f"Скан {label} пустой.")
    release_expired_tasks(task_id=task.pk)
    with transaction.atomic():
        task = InventoryTask.objects.select_for_update().get(pk=task.pk)
        if task.status != InventoryTask.STATUS_IN_PROGRESS:
            raise ValidationError("Сессия задания истекла. Возьмите его в работу повторно.")
        _assert_owner(task, user)
        if task.location_verified_at is None:
            raise ValidationError("Сначала повторно подтвердите складское место.")
        if task.counted_at is None or not _task_has_discrepancy(task):
            raise ValidationError("По заданию нет сохраненного расхождения.")

        field = (
            "discrepancy_pallet_codes"
            if container_type == "pallet"
            else "discrepancy_box_codes"
        )
        codes = [str(value or "").strip() for value in (getattr(task, field) or [])]
        is_new_code = normalized_code.casefold() not in {
            value.casefold() for value in codes
        }
        if (
            container_type == "box"
            and is_new_code
            and len(codes) >= int(task.actual_box_count or 0)
        ):
            raise ValidationError(
                "Уже отсканировано указанное фактическое количество коробов."
            )
        if is_new_code:
            codes.append(normalized_code)
        setattr(task, field, codes)
        now = timezone.now()
        _touch_task_lease(task, now=now)
        task.save(
            update_fields=[field, "last_activity_at", "lease_expires_at", "updated_at"]
        )
    return task


def clear_discrepancy_containers(task: InventoryTask, user) -> InventoryTask:
    release_expired_tasks(task_id=task.pk)
    with transaction.atomic():
        task = InventoryTask.objects.select_for_update().get(pk=task.pk)
        if task.status != InventoryTask.STATUS_IN_PROGRESS:
            raise ValidationError("Сессия задания истекла. Возьмите его в работу повторно.")
        _assert_owner(task, user)
        if task.counted_at is None or not _task_has_discrepancy(task):
            raise ValidationError("По заданию нет сохраненного расхождения.")
        task.discrepancy_pallet_codes = []
        task.discrepancy_box_codes = []
        now = timezone.now()
        _touch_task_lease(task, now=now)
        task.save(
            update_fields=[
                "discrepancy_pallet_codes",
                "discrepancy_box_codes",
                "last_activity_at",
                "lease_expires_at",
                "updated_at",
            ]
        )
    return task


def _create_head_manager_discrepancy_task(task: InventoryTask, user) -> None:
    from employees.models import Employee
    from todo.models import Task

    manager = None
    if task.inventory.created_by_id:
        manager = Employee.objects.filter(
            user_id=task.inventory.created_by_id,
            role="head_manager",
            is_active=True,
        ).first()
    if manager is None:
        manager = (
            Employee.objects.filter(role="head_manager", is_active=True)
            .order_by("full_name", "id")
            .first()
        )
    if manager is None:
        raise ValidationError(
            "Не найден активный начальник склада. Отчет о расхождении не отправлен."
        )

    lines = list(_task_lines(task))
    planned_qty = sum(int(line.planned_qty or 0) for line in lines)
    current_db_qty = _current_plan_total(task)
    current_box_codes = _current_box_codes(task)
    actual_box_count = int(task.actual_box_count or 0)
    difference = actual_box_count - int(task.planned_box_count or 0)
    empty_place = actual_box_count == 0
    product_lines = [
        (
            f"- {line.sku_code}"
            f"{f' · {line.size}' if line.size else ''}: {int(line.planned_qty or 0)} шт."
            f"{f' · {line.name}' if line.name else ''}"
        )
        for line in lines
    ]
    observer = Employee.objects.filter(user=user, is_active=True).first()
    title = f"Проверить расхождение инвентаризации №{task.inventory_id}"
    route = f"/inventory/{task.inventory_id}/?inventory_task={task.id}"
    description = "\n".join(
        [
            f"Место: {task.location_code}",
            f"Коробов по базе на начало: {task.planned_box_count}",
            f"Коробов насчитано: {actual_box_count}",
            f"Разница по коробам: {difference:+d}",
            f"Короба по базе на начало: {', '.join(task.planned_box_codes or []) or 'нет'}",
            f"Паллеты по базе на начало: {', '.join(task.planned_pallet_codes or []) or 'нет'}",
            f"Текущая база при отправке: {len(current_box_codes)} коробов, {current_db_qty} шт. товара",
            f"Текущие коды коробов: {', '.join(current_box_codes) or 'нет'}",
            (
                "Место подтверждено пустым; паллеты и короба не обнаружены."
                if empty_place
                else "Отсканированные паллеты: " + ", ".join(task.discrepancy_pallet_codes or [])
            ),
            (
                "Отсканированные короба: " + ", ".join(task.discrepancy_box_codes or [])
                if not empty_place
                else ""
            ),
            f"Товарных единиц по плану на начало: {planned_qty}",
            "Товары по плану:",
            *(product_lines or ["- нет"]),
            f"Передал: {_user_name(user)}",
        ]
    ).replace("\n\n", "\n")
    Task.objects.get_or_create(
        title=title,
        route=route,
        status="backlog",
        defaults={
            "description": description,
            "kind": Task.KIND_WAREHOUSE_INTERNAL,
            "assigned_to": manager,
            "observer": observer,
            "created_by": user if getattr(user, "is_authenticated", False) else None,
            "priority": "high",
            "due_date": timezone.now(),
        },
    )


def submit_discrepancy_report(task: InventoryTask, user) -> InventoryTask:
    release_expired_tasks(task_id=task.pk)
    with transaction.atomic():
        task = (
            InventoryTask.objects.select_for_update()
            .get(pk=task.pk)
        )
        if task.status != InventoryTask.STATUS_IN_PROGRESS:
            raise ValidationError("Сессия задания истекла. Возьмите его в работу повторно.")
        _assert_owner(task, user)
        if task.location_verified_at is None:
            raise ValidationError("Сначала повторно подтвердите складское место.")
        if task.counted_at is None or not _task_has_discrepancy(task):
            raise ValidationError("По заданию нет расхождения для отправки.")
        empty_count = _task_is_empty_count(task)
        now = timezone.now()

        if not empty_count and not task.discrepancy_pallet_codes:
            raise ValidationError("Отсканируйте хотя бы одну паллету.")
        if not empty_count and len(task.discrepancy_box_codes or []) != int(
            task.actual_box_count or 0
        ):
            raise ValidationError(
                f"Отсканируйте все короба: {len(task.discrepancy_box_codes or [])} "
                f"из {task.actual_box_count}."
            )

        task.discrepancy_reported_by = user
        task.discrepancy_reported_by_name = _user_name(user)
        task.discrepancy_reported_at = now
        task.status = InventoryTask.STATUS_COMPLETED
        task.completed_at = now
        task.last_activity_at = now
        task.lease_expires_at = None
        task.save(
            update_fields=[
                "discrepancy_reported_by",
                "discrepancy_reported_by_name",
                "discrepancy_reported_at",
                "status",
                "completed_at",
                "last_activity_at",
                "lease_expires_at",
                "updated_at",
            ]
        )
        inventory = Inventory.objects.select_for_update().get(pk=task.inventory_id)
        _sync_inventory_completion(inventory, user)
        _create_head_manager_discrepancy_task(task, user)
    return task
