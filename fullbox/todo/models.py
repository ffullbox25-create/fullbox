import os
import re
from datetime import timedelta

from django.conf import settings
from django.db import models
from django.utils import timezone

from audit.models import OrderAuditEntry, get_order_external_number
from employees.models import Employee
from fullbox.order_numbers import replace_order_number_in_title
from orders.title_truth import resolve_order_title


_RECEIVING_ROUTE_RE = re.compile(r"/orders/receiving/([^/]+)/")
_PROCESSING_ROUTE_RE = re.compile(r"/orders/processing/([^/]+)/")
_SHIPPING_ROUTE_RE = re.compile(r"/shipping/(\d+)/")
_STATUS_ONLY_KEYS = {"comment", "message", "status", "status_label", "submit_action"}


def _extract_receiving_order_id(route: str | None) -> str | None:
    if not route:
        return None
    match = _RECEIVING_ROUTE_RE.search(route)
    if not match:
        return None
    return match.group(1)


def _extract_processing_order_id(route: str | None) -> str | None:
    if not route:
        return None
    match = _PROCESSING_ROUTE_RE.search(route)
    if not match:
        return None
    return match.group(1)


def _extract_shipping_order_pk(route: str | None) -> str | None:
    if not route:
        return None
    match = _SHIPPING_ROUTE_RE.search(route)
    if not match:
        return None
    return match.group(1)


def _is_receiving_sign_route(route: str | None) -> bool:
    return bool(route and "/orders/receiving/" in route and "/act/print" in route)


def _parse_qty_value(raw: object | None) -> int | None:
    if raw in (None, ""):
        return None
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return None


def _has_receiving_items(payload: dict) -> bool:
    items = payload.get("items") or []
    for item in items:
        for key in ("sku_code", "name", "qty", "size"):
            if str(item.get(key) or "").strip():
                return True
    return False


def _receiving_has_mismatch(payload: dict | None) -> bool:
    payload = payload or {}
    if payload.get("act_mismatch") is True:
        return True
    act_label = str(payload.get("act_label") or "").strip().lower()
    status_label = str(payload.get("status_label") or "").strip().lower()
    if "расхожд" in act_label or "расхожд" in status_label:
        return True
    act_items = payload.get("act_items") or []
    for item in act_items:
        planned_qty = _parse_qty_value(item.get("planned_qty"))
        if planned_qty is None:
            planned_qty = _parse_qty_value(item.get("qty")) or 0
        actual_qty = _parse_qty_value(item.get("actual_qty"))
        if actual_qty is None:
            actual_qty = _parse_qty_value(item.get("qty")) or 0
        if actual_qty != planned_qty:
            return True
    return False


def _is_receiving_fact_payload(payload: dict | None) -> bool:
    payload = payload or {}
    if not payload:
        return False
    if "act_items" in payload or "act_mismatch" in payload:
        return True
    act_value = str(payload.get("act") or "").strip().lower()
    if act_value == "receiving":
        return True
    act_label = str(payload.get("act_label") or "").strip().lower()
    status_label = str(payload.get("status_label") or "").strip().lower()
    return "акт приемки" in act_label or "товар принят" in status_label


def _latest_receiving_fact_payload(entries) -> dict:
    for entry in reversed(entries or []):
        payload = entry.payload or {}
        if _is_receiving_fact_payload(payload):
            return payload
    return {}


def _latest_payload_from_entries(entries) -> dict:
    for entry in reversed(entries):
        payload = entry.payload or {}
        if not payload:
            continue
        significant_keys = set(payload.keys()) - _STATUS_ONLY_KEYS
        if significant_keys:
            return payload
    return entries[-1].payload or {} if entries else {}


def _planned_receiving_payload_from_entries(entries) -> dict:
    for entry in entries:
        payload = entry.payload or {}
        if "items" in payload:
            return payload
    return _latest_payload_from_entries(entries)


def _receiving_title_from_entries(entries) -> str:
    planned_payload = _planned_receiving_payload_from_entries(entries or [])
    if planned_payload and not _has_receiving_items(planned_payload):
        return "Заявка на приемку без указания товара"
    fact_payload = _latest_receiving_fact_payload(entries)
    if fact_payload and _receiving_has_mismatch(fact_payload):
        return "Заявка на приемку с расхождениями"
    return "Заявка на приемку"


def _format_receiving_date(value) -> str:
    if not value:
        return ""
    dt_value = value
    if timezone.is_naive(dt_value):
        dt_value = timezone.make_aware(dt_value, timezone.get_current_timezone())
    return timezone.localtime(dt_value).strftime("%d.%m.%Y")


def _payload_for_receiving_order(order_id: str) -> dict:
    entries = list(
        OrderAuditEntry.objects.filter(order_id=order_id, order_type="receiving")
        .only("payload", "created_at")
        .order_by("created_at")
    )
    return _latest_payload_from_entries(entries)


def build_receiving_display_context(order_id: str, entries=None) -> dict:
    normalized_order_id = str(order_id or "").strip()
    if entries is None:
        entries = list(
            OrderAuditEntry.objects.filter(order_id=normalized_order_id, order_type="receiving")
            .only("payload", "created_at", "id")
            .order_by("created_at", "id")
        )
    else:
        entries = sorted(
            list(entries),
            key=lambda entry: (
                getattr(entry, "created_at", None) or timezone.localtime(),
                getattr(entry, "id", 0),
            ),
        )
    created_at = entries[0].created_at if entries else None
    updated_at = entries[-1].created_at if entries else None
    latest_payload = entries[-1].payload if entries else {}
    title = resolve_order_title(
        "receiving",
        normalized_order_id,
        payload=latest_payload,
        entries=entries,
        created_at=created_at,
        variant="base",
    )
    display_title = resolve_order_title(
        "receiving",
        normalized_order_id,
        payload=latest_payload,
        entries=entries,
        created_at=created_at,
        variant="full",
    )
    display_number = get_order_external_number("receiving", normalized_order_id)
    return {
        "title": display_title,
        "title_base": title,
        "created_at": created_at,
        "updated_at": updated_at,
        "display_number": display_number,
    }


def _display_shipping_number(number: str | None) -> str:
    return get_order_external_number("shipping", number)


def _shipping_task_title_with_display_id(
    title: str | None,
    raw_number: str | None,
    *,
    delivery_type: str | None = None,
) -> str:
    prefix = "Заявка на отгрузку самовывозом" if str(delivery_type or "").strip() == "pickup" else "Заявка на отгрузку"
    return f"{prefix} №{_display_shipping_number(raw_number)}"


def default_due_date():
    return timezone.now() + timedelta(days=1)


class Task(models.Model):
    KIND_SYSTEM = "system"
    KIND_WAREHOUSE_INTERNAL = "warehouse_internal"
    KIND_CHOICES = [
        (KIND_SYSTEM, "Системная задача"),
        (KIND_WAREHOUSE_INTERNAL, "Внутренняя задача склада"),
    ]

    INTERNAL_STATUS_LABELS = {
        "backlog": "Новая",
        "in_progress": "В работе",
        "blocked": "Приостановлена",
        "done": "Выполнена",
    }

    STATUS_CHOICES = [
        ("backlog", "Просрочены"),
        ("in_progress", "Сегодня"),
        ("done", "Готово"),
        ("blocked", "Скоро"),
    ]

    PRIORITY_CHOICES = [
        ("low", "Низкий"),
        ("normal", "Средний"),
        ("high", "Высокий"),
        ("urgent", "Срочный"),
    ]

    title = models.CharField("Название задачи", max_length=255)
    description = models.TextField("Описание", blank=True)
    route = models.CharField("Маршрут", max_length=255, blank=True)
    kind = models.CharField(
        "Тип задачи",
        max_length=32,
        choices=KIND_CHOICES,
        default=KIND_SYSTEM,
        db_index=True,
    )
    assigned_to = models.ForeignKey(
        Employee,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="tasks",
        verbose_name="Исполнитель",
    )
    observer = models.ForeignKey(
        Employee,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="observed_tasks",
        verbose_name="Наблюдатель",
    )
    participants = models.ManyToManyField(
        Employee,
        blank=True,
        related_name="participating_tasks",
        verbose_name="Соисполнители",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="created_tasks",
        verbose_name="Постановщик",
    )
    status = models.CharField("Статус", max_length=32, choices=STATUS_CHOICES, default="backlog")
    priority = models.CharField("Приоритет", max_length=16, choices=PRIORITY_CHOICES, default="normal")
    due_date = models.DateTimeField("Дедлайн", default=default_due_date)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["status", "priority", "due_date", "-created_at"]
        verbose_name = "Задача"
        verbose_name_plural = "Задачи"

    def __str__(self) -> str:
        return self.title

    @property
    def is_warehouse_internal(self) -> bool:
        return self.kind == self.KIND_WAREHOUSE_INTERNAL

    @property
    def workflow_status_label(self) -> str:
        if self.is_warehouse_internal:
            return self.INTERNAL_STATUS_LABELS.get(self.status, self.get_status_display())
        return self.get_status_display()

    @property
    def checklist_total(self) -> int:
        annotated = getattr(self, "checklist_total_count", None)
        if annotated is not None:
            return int(annotated)
        return self.checklist_items.count()

    @property
    def checklist_completed(self) -> int:
        annotated = getattr(self, "checklist_completed_count", None)
        if annotated is not None:
            return int(annotated)
        return self.checklist_items.filter(is_completed=True).count()

    @property
    def checklist_percent(self) -> int:
        total = self.checklist_total
        if total <= 0:
            return 0
        return round(self.checklist_completed * 100 / total)

    @property
    def is_overdue(self) -> bool:
        return self.status != "done" and bool(self.due_date and self.due_date < timezone.now())

    def display_title(self) -> str:
        cached = getattr(self, "_display_title_cache", None)
        if cached:
            return cached
        order_id = _extract_receiving_order_id(self.route)
        if order_id:
            raw_title = str(self.title or "").strip()
            if _is_receiving_sign_route(self.route) and raw_title:
                self._display_title_cache = replace_order_number_in_title(
                    raw_title,
                    "receiving",
                    order_id,
                    default_title=raw_title,
                )
                return self._display_title_cache
            display_context = build_receiving_display_context(order_id)
            self._display_title_cache = display_context.get("title") or f"Заявка на приемку №{get_order_external_number('receiving', order_id)}"
            return self._display_title_cache
        order_id = _extract_processing_order_id(self.route)
        if order_id:
            raw_title = str(self.title or "").strip()
            default_title = f"Заявка на обработку №{get_order_external_number('processing', order_id)}"
            self._display_title_cache = replace_order_number_in_title(
                raw_title,
                "processing",
                order_id,
                default_title=default_title,
            )
            return self._display_title_cache
        if self.route and "/packing/pallet-removal/" in self.route:
            self._display_title_cache = self.title
            return self._display_title_cache
        shipping_pk = _extract_shipping_order_pk(self.route)
        if shipping_pk:
            shipping_order = getattr(self, "_shipping_order_cache", None)
            if shipping_order is None:
                try:
                    from shipping.models import ShippingOrder

                    shipping_order = (
                        ShippingOrder.objects.filter(pk=int(shipping_pk))
                        .only("number", "delivery_type")
                        .first()
                    )
                except Exception:
                    shipping_order = None
            if shipping_order:
                self._display_title_cache = _shipping_task_title_with_display_id(
                    self.title,
                    shipping_order.number,
                    delivery_type=getattr(shipping_order, "delivery_type", ""),
                )
                return self._display_title_cache
        self._display_title_cache = self.title
        return self._display_title_cache


class TaskAttention(models.Model):
    task = models.ForeignKey(
        Task,
        on_delete=models.CASCADE,
        related_name="attention_states",
        verbose_name="Задача",
    )
    employee = models.ForeignKey(
        Employee,
        on_delete=models.CASCADE,
        related_name="task_attention_states",
        verbose_name="Получатель",
    )
    delivered_at = models.DateTimeField("Когда доставлено", default=timezone.now)
    viewed_at = models.DateTimeField("Когда просмотрено", blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-delivered_at", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["task", "employee"],
                name="uniq_todo_task_attention_employee",
            )
        ]
        indexes = [
            models.Index(
                fields=["employee", "viewed_at"],
                name="todo_attn_employee_viewed_idx",
            )
        ]
        verbose_name = "Уведомление о задаче"
        verbose_name_plural = "Уведомления о задачах"

    def __str__(self) -> str:
        return f"{self.task_id}:{self.employee_id}"


class TaskComment(models.Model):
    task = models.ForeignKey(
        Task,
        on_delete=models.CASCADE,
        related_name="comments",
        verbose_name="Задача",
    )
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="task_comments",
        verbose_name="Автор",
    )
    body = models.TextField("Комментарий")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Комментарий"
        verbose_name_plural = "Комментарии"

    def __str__(self) -> str:
        return f"Комментарий #{self.pk}"


class WarehouseRequestDeadlineChange(models.Model):
    request_route = models.CharField(
        "Маршрут складской заявки",
        max_length=255,
        db_index=True,
    )
    previous_due_date = models.DateTimeField("Предыдущий срок")
    due_date = models.DateTimeField("Новый срок")
    reason = models.TextField("Причина переноса")
    changed_by = models.ForeignKey(
        Employee,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="warehouse_deadline_changes",
        verbose_name="Кто изменил",
    )
    created_at = models.DateTimeField("Когда изменено", auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(
                fields=["request_route", "-created_at"],
                name="todo_wh_due_route_created_idx",
            )
        ]
        verbose_name = "Перенос срока складской заявки"
        verbose_name_plural = "Переносы сроков складских заявок"

    def __str__(self) -> str:
        return f"{self.request_route}: {self.previous_due_date} → {self.due_date}"


class TaskAttachment(models.Model):
    task = models.ForeignKey(
        Task,
        on_delete=models.CASCADE,
        related_name="attachments",
        verbose_name="Задача",
    )
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="task_attachments",
        verbose_name="Кто загрузил",
    )
    file = models.FileField("Файл", upload_to="task_files/")
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-uploaded_at"]
        verbose_name = "Файл задачи"
        verbose_name_plural = "Файлы задач"

    def __str__(self) -> str:
        return f"Файл #{self.pk}"


class TaskChecklistItem(models.Model):
    task = models.ForeignKey(
        Task,
        on_delete=models.CASCADE,
        related_name="checklist_items",
        verbose_name="Задача",
    )
    title = models.CharField("Пункт чек-листа", max_length=255)
    position = models.PositiveIntegerField("Порядок", default=0)
    is_completed = models.BooleanField("Выполнен", default=False)
    completed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="completed_task_checklist_items",
        verbose_name="Кто выполнил",
    )
    completed_at = models.DateTimeField("Когда выполнен", blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["position", "id"]
        indexes = [
            models.Index(
                fields=["task", "is_completed"],
                name="todo_ch_task_done_idx",
            )
        ]
        verbose_name = "Пункт чек-листа"
        verbose_name_plural = "Пункты чек-листа"

    def __str__(self) -> str:
        return self.title

    @property
    def filename(self) -> str:
        return os.path.basename(self.file.name)


class TaskPanelSnapshot(models.Model):
    task = models.ForeignKey(
        Task,
        on_delete=models.CASCADE,
        related_name="panel_snapshots",
    )
    role_key = models.CharField(max_length=64, blank=True, db_index=True)
    task_route = models.CharField(max_length=255, blank=True)
    task_status = models.CharField(max_length=32, blank=True)
    filter_type = models.CharField(max_length=32, blank=True)
    panel_title = models.TextField(blank=True)
    panel_url = models.CharField(max_length=255, blank=True)
    order_client_id = models.IntegerField(null=True, blank=True)
    order_client_label = models.TextField(blank=True)
    order_status_label = models.TextField(blank=True)
    order_status_tone = models.CharField(max_length=32, blank=True)
    order_notice_label = models.TextField(blank=True)
    order_notice_tone = models.CharField(max_length=32, blank=True)
    executor_label = models.TextField(blank=True)
    processing_packers_label = models.TextField(blank=True)
    worker_title = models.TextField(blank=True)
    panel_updated_at_label = models.CharField(max_length=32, blank=True)
    is_hidden = models.BooleanField(default=False)
    snapshot_updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "todo_task_panel_snapshot"
        constraints = [
            models.UniqueConstraint(fields=["task", "role_key"], name="uniq_todo_task_panel_snapshot"),
        ]

    def __str__(self) -> str:
        return f"{self.task_id}:{self.role_key or '-'}"
