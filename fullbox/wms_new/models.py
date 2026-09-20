from __future__ import annotations

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models


class WmsNewOrder(models.Model):
    STATUS_NEW = "new"
    STATUS_AWAITING_STOCK = "awaiting_stock"
    STATUS_RESERVED = "reserved"
    STATUS_QUEUED = "queued"
    STATUS_PICKING = "picking"
    STATUS_PICKED = "picked"
    STATUS_READY = "ready"
    STATUS_HANDED_OVER = "handed_over"
    STATUS_DONE = "done"
    STATUS_CANCELLED = "cancelled"
    STATUS_RETURN_PENDING = "return_pending"
    STATUS_RETURNED = "returned"
    STATUS_EXCEPTION = "exception"
    STATUS_CHOICES = (
        (STATUS_NEW, "Новый"),
        (STATUS_AWAITING_STOCK, "Ожидает товар"),
        (STATUS_RESERVED, "Зарезервирован"),
        (STATUS_QUEUED, "В очереди сборки"),
        (STATUS_PICKING, "Собирается"),
        (STATUS_PICKED, "Отобран"),
        (STATUS_READY, "Готов к передаче"),
        (STATUS_HANDED_OVER, "Передан"),
        (STATUS_DONE, "Выполнен"),
        (STATUS_CANCELLED, "Отменен"),
        (STATUS_RETURN_PENDING, "Ожидается возврат"),
        (STATUS_RETURNED, "Возвращен"),
        (STATUS_EXCEPTION, "Исключение"),
    )

    source_order_id = models.BigIntegerField(null=True, blank=True, unique=True)
    agency = models.ForeignKey(
        "sku.Agency",
        on_delete=models.PROTECT,
        related_name="wms_new_orders",
    )
    source_profile_id = models.BigIntegerField(null=True, blank=True)
    marketplace = models.CharField(max_length=32, blank=True)
    integration_name = models.CharField(max_length=128, blank=True)
    external_order_id = models.CharField(max_length=128)
    delivery_type = models.CharField(max_length=128, blank=True)
    tracking_number = models.CharField(max_length=128, blank=True)
    warehouse_code = models.CharField(max_length=64, default="MSK")
    status = models.CharField(max_length=32, choices=STATUS_CHOICES, default=STATUS_NEW)
    source_status = models.CharField(max_length=64, blank=True)
    ordered_at = models.DateTimeField(null=True, blank=True)
    cutoff_at = models.DateTimeField(null=True, blank=True)
    source_created_at = models.DateTimeField(null=True, blank=True)
    source_updated_at = models.DateTimeField(null=True, blank=True)
    last_synced_at = models.DateTimeField(null=True, blank=True)
    availability_state = models.CharField(max_length=32, default="unavailable")
    availability_snapshot = models.JSONField(default=list, blank=True)
    source_snapshot = models.JSONField(default=dict, blank=True)
    is_manual = models.BooleanField(default=False)
    pilot_revision = models.PositiveIntegerField(default=0)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_wms_new_orders",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "wms_new_order"
        ordering = ("-source_created_at", "-id")
        indexes = (
            models.Index(fields=("status", "source_created_at")),
            models.Index(fields=("agency", "status")),
            models.Index(fields=("marketplace", "status")),
            models.Index(fields=("external_order_id",)),
        )

    def __str__(self) -> str:
        return self.external_order_id


class WmsNewOrderItem(models.Model):
    order = models.ForeignKey(
        WmsNewOrder,
        on_delete=models.CASCADE,
        related_name="items",
    )
    source_item_id = models.BigIntegerField(null=True, blank=True)
    sku = models.ForeignKey(
        "sku.SKU",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="wms_new_order_items",
    )
    external_line_id = models.CharField(max_length=128)
    external_sku = models.CharField(max_length=128, blank=True)
    barcode = models.CharField(max_length=64, blank=True)
    product_name = models.CharField(max_length=255, blank=True)
    quantity = models.PositiveIntegerField(default=1)
    requirements = models.JSONField(default=dict, blank=True)
    source_snapshot = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "wms_new_order_item"
        ordering = ("order_id", "id")
        constraints = (
            models.UniqueConstraint(
                fields=("order", "external_line_id"),
                name="uniq_wms_new_order_line",
            ),
        )


class WmsNewWave(models.Model):
    STATUS_QUEUED = "queued"
    STATUS_IN_PROGRESS = "in_progress"
    STATUS_VERIFICATION = "verification"
    STATUS_DONE = "done"
    STATUS_CANCELLED = "cancelled"
    STATUS_CHOICES = (
        (STATUS_QUEUED, "В очереди"),
        (STATUS_IN_PROGRESS, "В работе"),
        (STATUS_VERIFICATION, "На проверке"),
        (STATUS_DONE, "Завершена"),
        (STATUS_CANCELLED, "Отменена"),
    )

    source_batch_id = models.BigIntegerField(null=True, blank=True, unique=True)
    number = models.CharField(max_length=64, unique=True)
    agency = models.ForeignKey(
        "sku.Agency",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="wms_new_waves",
    )
    status = models.CharField(max_length=32, choices=STATUS_CHOICES, default=STATUS_QUEUED)
    planned_orders = models.PositiveIntegerField(default=0)
    planned_units = models.PositiveIntegerField(default=0)
    picked_units = models.PositiveIntegerField(default=0)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_wms_new_waves",
    )
    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="assigned_wms_new_waves",
    )
    workstation_name = models.CharField(max_length=128, blank=True)
    cart_name = models.CharField(max_length=128, blank=True)
    source_status = models.CharField(max_length=32, blank=True)
    source_created_at = models.DateTimeField(null=True, blank=True)
    source_updated_at = models.DateTimeField(null=True, blank=True)
    last_synced_at = models.DateTimeField(null=True, blank=True)
    source_snapshot = models.JSONField(default=dict, blank=True)
    is_manual = models.BooleanField(default=False)
    pilot_revision = models.PositiveIntegerField(default=0)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "wms_new_wave"
        ordering = ("-created_at", "-id")


class WmsNewWaveOrder(models.Model):
    wave = models.ForeignKey(WmsNewWave, on_delete=models.CASCADE, related_name="wave_orders")
    order = models.ForeignKey(WmsNewOrder, on_delete=models.PROTECT, related_name="wave_orders")
    sequence = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "wms_new_wave_order"
        ordering = ("sequence", "id")
        constraints = (
            models.UniqueConstraint(
                fields=("wave", "order"),
                name="uniq_wms_new_wave_order",
            ),
        )


class WmsNewWavePickLine(models.Model):
    """Planned order line inside an isolated FBS-NEW picking wave."""

    STATUS_PENDING = "pending"
    STATUS_PICKING = "picking"
    STATUS_PICKED = "picked"
    STATUS_SHORTAGE = "shortage"
    STATUS_SKIPPED = "skipped"
    STATUS_CANCELLED = "cancelled"
    STATUS_CHOICES = (
        (STATUS_PENDING, "Ожидает подбор"),
        (STATUS_PICKING, "Подбирается"),
        (STATUS_PICKED, "Подобран"),
        (STATUS_SHORTAGE, "Недостача"),
        (STATUS_SKIPPED, "Пропущен"),
        (STATUS_CANCELLED, "Отменен"),
    )

    wave = models.ForeignKey(
        WmsNewWave,
        on_delete=models.CASCADE,
        related_name="pick_lines",
    )
    order = models.ForeignKey(
        WmsNewOrder,
        on_delete=models.PROTECT,
        related_name="wave_pick_lines",
    )
    order_item = models.ForeignKey(
        WmsNewOrderItem,
        on_delete=models.PROTECT,
        related_name="wave_pick_lines",
    )
    product = models.ForeignKey(
        "WmsNewProduct",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="wave_pick_lines",
    )
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_PENDING)
    planned_quantity = models.PositiveIntegerField(default=1)
    picked_quantity = models.PositiveIntegerField(default=0)
    sequence = models.PositiveIntegerField(default=0)
    source_snapshot = models.JSONField(default=dict, blank=True)
    picked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="picked_wms_new_lines",
    )
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "wms_new_wave_pick_line"
        ordering = ("sequence", "id")
        constraints = (
            models.UniqueConstraint(
                fields=("wave", "order_item"),
                name="uniq_wms_new_wave_pick_item",
            ),
        )
        indexes = (
            models.Index(fields=("wave", "status", "sequence")),
            models.Index(fields=("order", "status")),
        )


class WmsNewWaveAllocation(models.Model):
    """A route segment and stock reservation contained wholly in FBS-NEW."""

    STATUS_RESERVED = "reserved"
    STATUS_PICKING = "picking"
    STATUS_PICKED = "picked"
    STATUS_SHORTAGE = "shortage"
    STATUS_SKIPPED = "skipped"
    STATUS_RELEASED = "released"
    STATUS_CANCELLED = "cancelled"
    STATUS_CHOICES = (
        (STATUS_RESERVED, "Зарезервировано"),
        (STATUS_PICKING, "Подбирается"),
        (STATUS_PICKED, "Подобрано"),
        (STATUS_SHORTAGE, "Недостача"),
        (STATUS_SKIPPED, "Пропущено"),
        (STATUS_RELEASED, "Резерв снят"),
        (STATUS_CANCELLED, "Отменено"),
    )

    pick_line = models.ForeignKey(
        WmsNewWavePickLine,
        on_delete=models.CASCADE,
        related_name="allocations",
    )
    box_item = models.ForeignKey(
        "WmsNewBoxItem",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="wave_allocations",
    )
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_RESERVED)
    source_box_code = models.CharField(max_length=128, blank=True)
    source_place_code = models.CharField(max_length=128, blank=True)
    source_place_name = models.CharField(max_length=255, blank=True)
    reserved_quantity = models.PositiveIntegerField(default=0)
    picked_quantity = models.PositiveIntegerField(default=0)
    sequence = models.PositiveIntegerField(default=0)
    marking_code = models.CharField(max_length=256, blank=True)
    source_snapshot = models.JSONField(default=dict, blank=True)
    picked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="picked_wms_new_allocations",
    )
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "wms_new_wave_allocation"
        ordering = ("sequence", "id")
        indexes = (
            models.Index(fields=("pick_line", "status", "sequence")),
            models.Index(fields=("source_place_code", "status")),
            models.Index(fields=("box_item", "status")),
        )


class WmsNewAssemblySession(models.Model):
    STATUS_ACTIVE = "active"
    STATUS_COMPLETED = "completed"
    STATUS_CANCELLED = "cancelled"
    STATUS_CHOICES = (
        (STATUS_ACTIVE, "В работе"),
        (STATUS_COMPLETED, "Завершена"),
        (STATUS_CANCELLED, "Отменена"),
    )

    place_code = models.CharField(max_length=128, blank=True)
    place_name = models.CharField(max_length=128)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_ACTIVE)
    planned_items = models.PositiveIntegerField(default=0)
    assembled_items = models.PositiveIntegerField(default=0)
    problem_items = models.PositiveIntegerField(default=0)
    current_order = models.ForeignKey(
        WmsNewOrder,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="current_assembly_sessions",
    )
    started_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="started_wms_new_assembly_sessions",
    )
    completed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="completed_wms_new_assembly_sessions",
    )
    source_snapshot = models.JSONField(default=dict, blank=True)
    pilot_revision = models.PositiveIntegerField(default=0)
    started_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "wms_new_assembly_session"
        ordering = ("-started_at", "-id")


class WmsNewAssemblyLine(models.Model):
    STATUS_PENDING = "pending"
    STATUS_AWAITING_ORDER = "awaiting_order"
    STATUS_ASSEMBLED = "assembled"
    STATUS_PROBLEM = "problem"
    STATUS_CHOICES = (
        (STATUS_PENDING, "Ожидает товар"),
        (STATUS_AWAITING_ORDER, "Ожидает этикетку заказа"),
        (STATUS_ASSEMBLED, "Собран"),
        (STATUS_PROBLEM, "Проблема"),
    )

    session = models.ForeignKey(
        WmsNewAssemblySession,
        on_delete=models.CASCADE,
        related_name="lines",
    )
    wave = models.ForeignKey(
        WmsNewWave,
        on_delete=models.PROTECT,
        related_name="assembly_lines",
    )
    order = models.ForeignKey(
        WmsNewOrder,
        on_delete=models.PROTECT,
        related_name="assembly_lines",
    )
    order_item = models.ForeignKey(
        WmsNewOrderItem,
        on_delete=models.PROTECT,
        related_name="assembly_lines",
    )
    status = models.CharField(max_length=24, choices=STATUS_CHOICES, default=STATUS_PENDING)
    planned_quantity = models.PositiveIntegerField(default=1)
    assembled_quantity = models.PositiveIntegerField(default=0)
    requires_order_scan = models.BooleanField(default=True)
    product_scan = models.CharField(max_length=255, blank=True)
    order_scan = models.CharField(max_length=255, blank=True)
    extra_data = models.JSONField(default=dict, blank=True)
    problem_place = models.CharField(max_length=128, blank=True)
    problem_reason = models.TextField(blank=True)
    assembled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="assembled_wms_new_lines",
    )
    assembled_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "wms_new_assembly_line"
        ordering = ("id",)
        constraints = (
            models.UniqueConstraint(
                fields=("session", "order_item"),
                name="uniq_wms_new_assembly_session_item",
            ),
        )


class WmsNewShipment(models.Model):
    STATUS_NEW = "new"
    STATUS_CHECKING = "checking"
    STATUS_CHECKED = "checked"
    STATUS_IN_TRANSIT = "in_transit"
    STATUS_ACCEPTED = "accepted"
    STATUS_REJECTED = "rejected"
    STATUS_PARTIAL = "partial"
    STATUS_CHOICES = (
        (STATUS_NEW, "Новая"),
        (STATUS_CHECKING, "Проверяется"),
        (STATUS_CHECKED, "Проверена"),
        (STATUS_IN_TRANSIT, "В пути"),
        (STATUS_ACCEPTED, "Принята маркетплейсом"),
        (STATUS_REJECTED, "Отклонена маркетплейсом"),
        (STATUS_PARTIAL, "Принята частично"),
    )

    source_batch_id = models.BigIntegerField(null=True, blank=True, unique=True)
    agency = models.ForeignKey(
        "sku.Agency",
        on_delete=models.PROTECT,
        related_name="wms_new_shipments",
    )
    source_profile_id = models.BigIntegerField(null=True, blank=True)
    delivery_type = models.CharField(max_length=128, blank=True)
    integration_name = models.CharField(max_length=128, blank=True)
    external_supply_id = models.CharField(max_length=128, blank=True)
    external_name = models.CharField(max_length=128, blank=True)
    status = models.CharField(max_length=24, choices=STATUS_CHOICES, default=STATUS_NEW)
    marketplace_state = models.CharField(max_length=32, blank=True)
    order_count = models.PositiveIntegerField(default=0)
    item_count = models.PositiveIntegerField(default=0)
    total_weight_kg = models.DecimalField(max_digits=14, decimal_places=3, default=0)
    total_volume_l = models.DecimalField(max_digits=14, decimal_places=3, default=0)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_wms_new_shipments",
    )
    checked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="checked_wms_new_shipments",
    )
    dispatched_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="dispatched_wms_new_shipments",
    )
    checked_at = models.DateTimeField(null=True, blank=True)
    dispatched_at = models.DateTimeField(null=True, blank=True)
    accepted_at = models.DateTimeField(null=True, blank=True)
    source_created_at = models.DateTimeField(null=True, blank=True)
    source_updated_at = models.DateTimeField(null=True, blank=True)
    last_synced_at = models.DateTimeField(null=True, blank=True)
    source_snapshot = models.JSONField(default=dict, blank=True)
    is_manual = models.BooleanField(default=False)
    pilot_revision = models.PositiveIntegerField(default=0)
    tariff_finalized = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "wms_new_shipment"
        ordering = ("-source_created_at", "-id")


class WmsNewShipmentBox(models.Model):
    shipment = models.ForeignKey(
        WmsNewShipment,
        on_delete=models.CASCADE,
        related_name="boxes",
    )
    source_box_id = models.BigIntegerField(null=True, blank=True, unique=True)
    qr_code = models.CharField(max_length=256)
    external_box_id = models.CharField(max_length=128, blank=True)
    status = models.CharField(max_length=32, default="open")
    problem_reason = models.TextField(blank=True)
    source_snapshot = models.JSONField(default=dict, blank=True)
    is_manual = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "wms_new_shipment_box"
        ordering = ("shipment_id", "id")


class WmsNewShipmentOrder(models.Model):
    VERIFY_NOT_CHECKED = "not_checked"
    VERIFY_CHECKED = "checked"
    VERIFY_ERROR = "error"
    VERIFY_CHOICES = (
        (VERIFY_NOT_CHECKED, "Не проверен"),
        (VERIFY_CHECKED, "Проверен"),
        (VERIFY_ERROR, "Ошибка"),
    )

    shipment = models.ForeignKey(
        WmsNewShipment,
        on_delete=models.CASCADE,
        related_name="shipment_orders",
    )
    order = models.ForeignKey(
        WmsNewOrder,
        on_delete=models.PROTECT,
        related_name="shipment_links",
    )
    box = models.ForeignKey(
        WmsNewShipmentBox,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="shipment_orders",
    )
    source_assignment_id = models.BigIntegerField(null=True, blank=True, unique=True)
    source_handover_order_id = models.BigIntegerField(null=True, blank=True, unique=True)
    assignment_status = models.CharField(max_length=32, default="confirmed")
    verification_status = models.CharField(
        max_length=24,
        choices=VERIFY_CHOICES,
        default=VERIFY_NOT_CHECKED,
    )
    sticker_number = models.CharField(max_length=128, blank=True)
    weight_kg = models.DecimalField(max_digits=12, decimal_places=3, default=0)
    volume_l = models.DecimalField(max_digits=12, decimal_places=3, default=0)
    order_total = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    marketplace_state = models.CharField(max_length=64, blank=True)
    in_supply = models.BooleanField(default=True)
    source_snapshot = models.JSONField(default=dict, blank=True)
    added_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="added_wms_new_shipment_orders",
    )
    verified_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="verified_wms_new_shipment_orders",
    )
    verified_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "wms_new_shipment_order"
        ordering = ("shipment_id", "id")
        constraints = (
            models.UniqueConstraint(
                fields=("shipment", "order"),
                name="uniq_wms_new_shipment_order",
            ),
        )


class WmsNewShipmentService(models.Model):
    shipment = models.ForeignKey(
        WmsNewShipment,
        on_delete=models.CASCADE,
        related_name="services",
    )
    name = models.CharField(max_length=255)
    unit_price = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    quantity = models.DecimalField(max_digits=12, decimal_places=3, default=1)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_wms_new_shipment_services",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "wms_new_shipment_service"
        ordering = ("id",)

    @property
    def total(self):
        return self.unit_price * self.quantity


class WmsNewReturn(models.Model):
    STATUS_WAITING = "waiting_marketplace"
    STATUS_QUEUED = "queued"
    STATUS_IN_PROGRESS = "in_progress"
    STATUS_COMPLETED = "completed"
    STATUS_FAILED = "failed"
    STATUS_CANCELLED = "cancelled"
    STATUS_CHOICES = (
        (STATUS_WAITING, "Проверяется маркетплейсом"),
        (STATUS_QUEUED, "Ожидает подборщика"),
        (STATUS_IN_PROGRESS, "Возвращается"),
        (STATUS_COMPLETED, "Возвращено"),
        (STATUS_FAILED, "Ошибка возврата"),
        (STATUS_CANCELLED, "Отменено"),
    )

    source_request_id = models.BigIntegerField(null=True, blank=True, unique=True)
    source_batch_id = models.BigIntegerField(null=True, blank=True)
    order = models.ForeignKey(
        WmsNewOrder,
        on_delete=models.PROTECT,
        related_name="returns",
    )
    status = models.CharField(max_length=24, choices=STATUS_CHOICES, default=STATUS_QUEUED)
    reason_code = models.CharField(max_length=32, blank=True)
    reason = models.TextField(blank=True)
    planned_qty = models.PositiveIntegerField(default=1)
    returned_qty = models.PositiveIntegerField(default=0)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_wms_new_returns",
    )
    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="assigned_wms_new_returns",
    )
    destination_location = models.ForeignKey(
        "sklad.WarehouseLocation",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="wms_new_returns",
    )
    return_box = models.ForeignKey(
        "WmsNewBox",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="received_returns",
    )
    received_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="received_wms_new_returns",
    )
    receiving_started_at = models.DateTimeField(null=True, blank=True)
    shipped_at = models.DateTimeField(null=True, blank=True)
    returned_at = models.DateTimeField(null=True, blank=True)
    source_created_at = models.DateTimeField(null=True, blank=True)
    source_updated_at = models.DateTimeField(null=True, blank=True)
    last_synced_at = models.DateTimeField(null=True, blank=True)
    source_snapshot = models.JSONField(default=dict, blank=True)
    is_manual = models.BooleanField(default=False)
    pilot_revision = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "wms_new_return"
        ordering = ("-source_created_at", "-id")


class WmsNewReturnLine(models.Model):
    CONDITION_GOOD = "good"
    CONDITION_DAMAGED = "damaged"
    CONDITION_MISSING = "missing"
    CONDITION_CHOICES = (
        (CONDITION_GOOD, "Годен к хранению"),
        (CONDITION_DAMAGED, "Поврежден"),
        (CONDITION_MISSING, "Не поступил"),
    )

    return_request = models.ForeignKey(
        WmsNewReturn,
        on_delete=models.CASCADE,
        related_name="lines",
    )
    order_item = models.ForeignKey(
        WmsNewOrderItem,
        on_delete=models.PROTECT,
        related_name="return_lines",
    )
    product = models.ForeignKey(
        "WmsNewProduct",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="return_lines",
    )
    product_name = models.CharField(max_length=255, blank=True)
    article = models.CharField(max_length=128, blank=True)
    barcode = models.CharField(max_length=64, blank=True)
    planned_qty = models.PositiveIntegerField(default=1)
    received_qty = models.PositiveIntegerField(default=0)
    accepted_qty = models.PositiveIntegerField(default=0)
    rejected_qty = models.PositiveIntegerField(default=0)
    condition = models.CharField(
        max_length=16,
        choices=CONDITION_CHOICES,
        default=CONDITION_GOOD,
    )
    marking_codes = models.JSONField(default=list, blank=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="updated_wms_new_return_lines",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "wms_new_return_line"
        ordering = ("return_request_id", "id")
        constraints = (
            models.UniqueConstraint(
                fields=("return_request", "order_item"),
                name="uniq_wms_new_return_order_item",
            ),
        )



class WmsNewTask(models.Model):
    TYPE_ACCEPTANCE = "acceptance"
    TYPE_PROCESSING = "processing"
    TYPE_SHIPMENT = "shipment"
    TYPE_OTHER = "other"
    TYPE_CHOICES = (
        (TYPE_ACCEPTANCE, "Прием поставки"),
        (TYPE_PROCESSING, "Обработка товара"),
        (TYPE_SHIPMENT, "Отгрузка товара"),
        (TYPE_OTHER, "Прочие задачи"),
    )
    STATUS_NEW = "new"
    STATUS_IN_PROGRESS = "in_progress"
    STATUS_BLOCKED = "blocked"
    STATUS_DONE = "done"
    STATUS_CHOICES = (
        (STATUS_NEW, "Новая"),
        (STATUS_IN_PROGRESS, "В работе"),
        (STATUS_BLOCKED, "Приостановлена"),
        (STATUS_DONE, "Выполнена"),
    )
    PRIORITY_LOW = "low"
    PRIORITY_NORMAL = "normal"
    PRIORITY_HIGH = "high"
    PRIORITY_URGENT = "urgent"
    PRIORITY_CHOICES = (
        (PRIORITY_LOW, "Низкий"),
        (PRIORITY_NORMAL, "Средний"),
        (PRIORITY_HIGH, "Высокий"),
        (PRIORITY_URGENT, "Срочный"),
    )

    source_task_id = models.BigIntegerField(null=True, blank=True, unique=True)
    agency = models.ForeignKey(
        "sku.Agency",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="wms_new_tasks",
    )
    workflow_type = models.CharField(max_length=32, choices=TYPE_CHOICES, default=TYPE_OTHER)
    title = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    internal_comment = models.TextField(blank=True)
    delivery_address = models.TextField(blank=True)
    contact_name = models.CharField(max_length=255, blank=True)
    contact_phone = models.CharField(max_length=64, blank=True)
    vehicle_model = models.CharField(max_length=128, blank=True)
    vehicle_number = models.CharField(max_length=64, blank=True)
    driver_name = models.CharField(max_length=255, blank=True)
    driver_phone = models.CharField(max_length=64, blank=True)
    warehouse_code = models.CharField(max_length=64, default="MSK")
    status = models.CharField(max_length=32, choices=STATUS_CHOICES, default=STATUS_NEW)
    priority = models.CharField(max_length=16, choices=PRIORITY_CHOICES, default=PRIORITY_NORMAL)
    assigned_to = models.ForeignKey(
        "employees.Employee",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="wms_new_tasks",
    )
    due_date = models.DateTimeField(null=True, blank=True)
    source_updated_at = models.DateTimeField(null=True, blank=True)
    last_synced_at = models.DateTimeField(null=True, blank=True)
    source_snapshot = models.JSONField(default=dict, blank=True)
    is_manual = models.BooleanField(default=False)
    pilot_revision = models.PositiveIntegerField(default=0)
    tariff_finalized = models.BooleanField(default=False)
    client_confirmed = models.BooleanField(default=False)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_wms_new_tasks",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "wms_new_task"
        ordering = ("status", "due_date", "-id")
        indexes = (
            models.Index(fields=("workflow_type", "status")),
            models.Index(fields=("agency", "status")),
            models.Index(fields=("assigned_to", "status")),
        )

    @property
    def is_overdue(self) -> bool:
        from django.utils import timezone

        return self.status != self.STATUS_DONE and bool(
            self.due_date and self.due_date < timezone.now()
        )


class WmsNewTaskBox(models.Model):
    STATUS_OPEN = "open"
    STATUS_CLOSED = "closed"
    STATUS_CHOICES = ((STATUS_OPEN, "Открыт"), (STATUS_CLOSED, "Закрыт"))

    task = models.ForeignKey(WmsNewTask, on_delete=models.CASCADE, related_name="boxes")
    code = models.CharField(max_length=128)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_OPEN)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_wms_new_task_boxes",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "wms_new_task_box"
        ordering = ("task_id", "id")
        constraints = (
            models.UniqueConstraint(fields=("task", "code"), name="uniq_wms_new_task_box_code"),
        )


class WmsNewTaskItem(models.Model):
    task = models.ForeignKey(WmsNewTask, on_delete=models.CASCADE, related_name="items")
    product = models.ForeignKey(
        "WmsNewProduct",
        on_delete=models.PROTECT,
        related_name="task_items",
    )
    box = models.ForeignKey(
        WmsNewTaskBox,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="items",
    )
    planned_qty = models.PositiveIntegerField(default=1)
    processed_qty = models.PositiveIntegerField(default=0)
    unit_price = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    technical_requirement = models.TextField(blank=True)
    additional_source = models.CharField(max_length=128, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "wms_new_task_item"
        ordering = ("task_id", "id")
        constraints = (
            models.UniqueConstraint(fields=("task", "product"), name="uniq_wms_new_task_product"),
        )


class WmsNewTaskService(models.Model):
    task = models.ForeignKey(WmsNewTask, on_delete=models.CASCADE, related_name="services")
    item = models.ForeignKey(
        WmsNewTaskItem,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="services",
    )
    name = models.CharField(max_length=255)
    unit_price = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    quantity = models.DecimalField(max_digits=12, decimal_places=3, default=1)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_wms_new_task_services",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "wms_new_task_service"
        ordering = ("task_id", "id")

    @property
    def total(self):
        return self.unit_price * self.quantity


class WmsNewTaskAttachment(models.Model):
    task = models.ForeignKey(WmsNewTask, on_delete=models.CASCADE, related_name="attachments")
    file = models.FileField(upload_to="wms_new/task_attachments/%Y/%m/")
    original_name = models.CharField(max_length=255)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="uploaded_wms_new_task_attachments",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "wms_new_task_attachment"
        ordering = ("task_id", "id")


class WmsNewProduct(models.Model):
    """Independent WMS NEW product card populated by one-way source sync."""

    source_sku_id = models.BigIntegerField(null=True, blank=True, unique=True)
    agency = models.ForeignKey(
        "sku.Agency",
        on_delete=models.PROTECT,
        related_name="wms_new_products",
    )
    name = models.CharField(max_length=255)
    article = models.CharField(max_length=128)
    barcode = models.CharField(max_length=64, blank=True)
    image_url = models.URLField(max_length=500, blank=True)
    color = models.CharField(max_length=64, blank=True)
    size = models.CharField(max_length=64, blank=True)
    category = models.CharField(max_length=128, blank=True)
    weight_grams = models.DecimalField(max_digits=12, decimal_places=3, default=0)
    width_cm = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    depth_cm = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    height_cm = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    marking_required = models.BooleanField(default=False)
    marking_count = models.PositiveIntegerField(default=0)
    stock_on_hand = models.PositiveIntegerField(default=0)
    stock_free = models.PositiveIntegerField(default=0)
    fbo_reserved = models.PositiveIntegerField(default=0)
    fbs_reserved = models.PositiveIntegerField(default=0)
    internal_reserved = models.PositiveIntegerField(default=0)
    expected_qty = models.PositiveIntegerField(default=0)
    is_bundle = models.BooleanField(default=False)
    internal_notes = models.TextField(blank=True)
    description = models.TextField(blank=True)
    source_updated_at = models.DateTimeField(null=True, blank=True)
    last_synced_at = models.DateTimeField(null=True, blank=True)
    source_snapshot = models.JSONField(default=dict, blank=True)
    is_manual = models.BooleanField(default=False)
    is_archived = models.BooleanField(default=False)
    pilot_revision = models.PositiveIntegerField(default=0)
    merged_into = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="merged_products",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_wms_new_products",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "wms_new_product"
        ordering = ("id",)
        indexes = (
            models.Index(fields=("agency", "article")),
            models.Index(fields=("barcode",)),
            models.Index(fields=("category", "is_archived")),
            models.Index(fields=("stock_on_hand", "stock_free")),
        )
        constraints = (
            models.UniqueConstraint(
                fields=("agency", "article"),
                condition=models.Q(is_archived=False),
                name="uniq_wms_new_product_article",
            ),
        )

    def __str__(self) -> str:
        return f"{self.article} - {self.name}"

    @property
    def dimensions(self) -> str:
        values = (self.width_cm, self.depth_cm, self.height_cm)
        return "*".join(format(value.normalize(), "f") for value in values)


class WmsNewBundleComponent(models.Model):
    bundle = models.ForeignKey(
        WmsNewProduct,
        on_delete=models.PROTECT,
        related_name="bundle_components",
    )
    component = models.ForeignKey(
        WmsNewProduct,
        on_delete=models.PROTECT,
        related_name="used_in_bundles",
    )
    quantity = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "wms_new_bundle_component"
        ordering = ("bundle_id", "id")
        constraints = (
            models.UniqueConstraint(
                fields=("bundle", "component"),
                name="uniq_wms_new_bundle_component",
            ),
        )


class WmsNewBundleStock(models.Model):
    bundle = models.ForeignKey(
        WmsNewProduct,
        on_delete=models.PROTECT,
        related_name="bundle_stocks",
    )
    location = models.ForeignKey(
        "sklad.WarehouseLocation",
        on_delete=models.PROTECT,
        related_name="wms_new_bundle_stocks",
    )
    quantity = models.PositiveIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "wms_new_bundle_stock"
        ordering = ("location_id", "bundle_id")
        constraints = (
            models.UniqueConstraint(
                fields=("bundle", "location"),
                name="uniq_wms_new_bundle_location",
            ),
        )


class WmsNewBundleOperation(models.Model):
    ACTION_ASSEMBLE = "assemble"
    ACTION_DISASSEMBLE = "disassemble"
    ACTION_DEFINE = "define"
    ACTION_CHOICES = (
        (ACTION_ASSEMBLE, "Собрать набор"),
        (ACTION_DISASSEMBLE, "Разобрать набор"),
        (ACTION_DEFINE, "Создать новый набор"),
    )

    bundle = models.ForeignKey(
        WmsNewProduct,
        on_delete=models.PROTECT,
        related_name="bundle_operations",
    )
    action = models.CharField(max_length=16, choices=ACTION_CHOICES)
    quantity = models.PositiveIntegerField(default=1)
    location = models.ForeignKey(
        "sklad.WarehouseLocation",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="wms_new_bundle_operations",
    )
    component_snapshot = models.JSONField(default=list, blank=True)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="wms_new_bundle_operations",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "wms_new_bundle_operation"
        ordering = ("-created_at", "-id")


class WmsNewMovement(models.Model):
    ACTION_ADJUSTMENT = "adjustment"
    ACTION_RECEIPT = "receipt"
    ACTION_WRITEOFF = "writeoff"
    ACTION_MOVEMENT = "movement"
    ACTION_CHOICES = (
        (ACTION_ADJUSTMENT, "Изменение остатка"),
        (ACTION_RECEIPT, "Поступление товара"),
        (ACTION_WRITEOFF, "Списание"),
        (ACTION_MOVEMENT, "Перемещение"),
    )

    source_event_id = models.BigIntegerField(null=True, blank=True, unique=True)
    agency = models.ForeignKey(
        "sku.Agency",
        on_delete=models.PROTECT,
        related_name="wms_new_movements",
    )
    product = models.ForeignKey(
        WmsNewProduct,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="movements",
    )
    product_name = models.CharField(max_length=255, blank=True)
    article = models.CharField(max_length=128, blank=True)
    action = models.CharField(max_length=16, choices=ACTION_CHOICES)
    source_location = models.ForeignKey(
        "sklad.WarehouseLocation",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="wms_new_movements_from",
    )
    source_location_name = models.CharField(max_length=255, blank=True)
    target_location = models.ForeignKey(
        "sklad.WarehouseLocation",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="wms_new_movements_to",
    )
    target_location_name = models.CharField(max_length=255, blank=True)
    quantity = models.IntegerField(default=0)
    balance_after = models.IntegerField(default=0)
    boxes_count = models.PositiveIntegerField(default=0)
    information = models.TextField(blank=True)
    source_event_type = models.CharField(max_length=64, blank=True)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="wms_new_movements",
    )
    actor_name = models.CharField(max_length=255, blank=True)
    occurred_at = models.DateTimeField()
    payload = models.JSONField(default=dict, blank=True)
    is_manual = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "wms_new_movement"
        ordering = ("-occurred_at", "-id")
        indexes = (
            models.Index(fields=("agency", "action", "occurred_at")),
            models.Index(fields=("product", "occurred_at")),
            models.Index(fields=("source_location", "occurred_at")),
            models.Index(fields=("target_location", "occurred_at")),
        )


class WmsNewLogisticsOrder(models.Model):
    STATUS_OK = "ok"
    STATUS_UNMEASURED = "unmeasured"
    STATUS_NO_DIMENSIONS = "no_dimensions"
    STATUS_ERROR = "error"
    STATUS_CARRIER_CANCELLED = "carrier_cancelled"
    STATUS_CHOICES = (
        (STATUS_OK, "OK"),
        (STATUS_UNMEASURED, "Не измерен"),
        (STATUS_NO_DIMENSIONS, "Нет габаритов"),
        (STATUS_ERROR, "Ошибка"),
        (STATUS_CARRIER_CANCELLED, "Отменён в ТК"),
    )
    SOURCE_SHIPPING = "shipping"
    SOURCE_FBS = "fbs"
    SOURCE_MANUAL = "manual"
    SOURCE_CHOICES = (
        (SOURCE_SHIPPING, "Fullbox"),
        (SOURCE_FBS, "FBS"),
        (SOURCE_MANUAL, "Ручная"),
    )

    source_shipping_order_id = models.BigIntegerField(null=True, blank=True, unique=True)
    source_fbs_order = models.OneToOneField(
        WmsNewOrder,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="logistics_order",
    )
    source_fbs_shipment = models.ForeignKey(
        WmsNewShipment,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="logistics_orders",
    )
    agency = models.ForeignKey(
        "sku.Agency",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="wms_new_logistics_orders",
    )
    number = models.CharField(max_length=128, unique=True)
    tracking_number = models.CharField(max_length=128, blank=True)
    source_type = models.CharField(max_length=16, choices=SOURCE_CHOICES)
    source_label = models.CharField(max_length=128, blank=True)
    weight_g = models.PositiveIntegerField(default=0)
    width_mm = models.PositiveIntegerField(default=0)
    height_mm = models.PositiveIntegerField(default=0)
    depth_mm = models.PositiveIntegerField(default=0)
    item_count = models.PositiveIntegerField(default=0)
    status = models.CharField(max_length=24, choices=STATUS_CHOICES, default=STATUS_UNMEASURED)
    delivery_status = models.CharField(max_length=128, blank=True)
    error_text = models.TextField(blank=True)
    source_updated_at = models.DateTimeField(null=True, blank=True)
    last_checked_at = models.DateTimeField(null=True, blank=True)
    last_synced_at = models.DateTimeField(null=True, blank=True)
    source_snapshot = models.JSONField(default=dict, blank=True)
    is_manual = models.BooleanField(default=False)
    pilot_revision = models.PositiveIntegerField(default=0)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_wms_new_logistics_orders",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "wms_new_logistics_order"
        ordering = ("-created_at", "-id")
        indexes = (
            models.Index(fields=("status", "created_at")),
            models.Index(fields=("source_type", "created_at")),
            models.Index(fields=("tracking_number",)),
        )


class WmsNewLogisticsManifest(models.Model):
    STATUS_NEW = "new"
    STATUS_SENT = "sent"
    STATUS_COMPLETED = "completed"
    STATUS_CANCELLED = "cancelled"
    STATUS_CHOICES = (
        (STATUS_NEW, "Новый"),
        (STATUS_SENT, "Отправлен"),
        (STATUS_COMPLETED, "Завершен"),
        (STATUS_CANCELLED, "Отменен"),
    )

    source_trip_id = models.BigIntegerField(null=True, blank=True, unique=True)
    name = models.CharField(max_length=128)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_NEW)
    logistics_company = models.CharField(max_length=255, blank=True)
    total_items = models.PositiveIntegerField(default=0)
    total_weight_g = models.PositiveIntegerField(default=0)
    departure_airport = models.CharField(max_length=128, blank=True)
    destination_airport = models.CharField(max_length=128, blank=True)
    expected_arrival_date = models.DateField(null=True, blank=True)
    source_updated_at = models.DateTimeField(null=True, blank=True)
    last_synced_at = models.DateTimeField(null=True, blank=True)
    source_snapshot = models.JSONField(default=dict, blank=True)
    is_manual = models.BooleanField(default=False)
    pilot_revision = models.PositiveIntegerField(default=0)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_wms_new_logistics_manifests",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "wms_new_logistics_manifest"
        ordering = ("-created_at", "-id")
        indexes = (models.Index(fields=("status", "created_at")),)


class WmsNewLogisticsPackage(models.Model):
    CARRIER_TANAIS = "tanais"
    CARRIER_GBS = "gbs"
    CARRIER_MANUAL = "manual"
    CARRIER_CDEK = "cdek"
    CARRIER_RUSSIAN_POST = "russian_post"
    CARRIER_CHOICES = (
        (CARRIER_TANAIS, "TANAIS"),
        (CARRIER_GBS, "GBS"),
        (CARRIER_MANUAL, "Ручная"),
        (CARRIER_CDEK, "СДЭК"),
        (CARRIER_RUSSIAN_POST, "Почта России"),
    )
    STATUS_NEW = "new"
    STATUS_SENT = "sent"
    STATUS_CHOICES = ((STATUS_NEW, "Новая"), (STATUS_SENT, "Отправлено"))

    number = models.CharField(max_length=128, unique=True)
    tracking_number = models.CharField(max_length=128, blank=True)
    carrier_code = models.CharField(max_length=32, choices=CARRIER_CHOICES)
    delivery_service = models.CharField(max_length=128)
    warehouse_code = models.CharField(max_length=128, blank=True)
    manifest = models.ForeignKey(
        WmsNewLogisticsManifest,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="packages",
    )
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_NEW)
    weight_g = models.PositiveIntegerField(default=0)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_wms_new_logistics_packages",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "wms_new_logistics_package"
        ordering = ("-created_at", "-id")
        indexes = (
            models.Index(fields=("carrier_code", "status")),
            models.Index(fields=("manifest", "status")),
        )


class WmsNewLogisticsPackageOrder(models.Model):
    package = models.ForeignKey(
        WmsNewLogisticsPackage,
        on_delete=models.CASCADE,
        related_name="package_orders",
    )
    order = models.OneToOneField(
        WmsNewLogisticsOrder,
        on_delete=models.PROTECT,
        related_name="package_link",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "wms_new_logistics_package_order"
        ordering = ("package_id", "id")


class WmsNewLogisticsRouteRule(models.Model):
    delivery_service = models.CharField(max_length=128, unique=True)
    international_carrier = models.CharField(max_length=32, blank=True)
    local_carrier = models.CharField(max_length=32, blank=True)
    is_active = models.BooleanField(default=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="updated_wms_new_logistics_rules",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "wms_new_logistics_route_rule"
        ordering = ("delivery_service",)


class WmsNewAcceptance(models.Model):
    STATUS_CREATED = "created"
    STATUS_IN_PROGRESS = "in_progress"
    STATUS_DONE = "done"
    STATUS_CANCELLED = "cancelled"
    STATUS_CHOICES = (
        (STATUS_CREATED, "Создана"),
        (STATUS_IN_PROGRESS, "В работе"),
        (STATUS_DONE, "Завершена"),
        (STATUS_CANCELLED, "Отменена"),
    )
    TYPE_SCAN = "scan"
    TYPE_MANUAL = "manual"
    TYPE_CHOICES = (
        (TYPE_SCAN, "Сканирование"),
        (TYPE_MANUAL, "Ручной ввод"),
    )

    source_order_key = models.CharField(max_length=128, unique=True)
    agency = models.ForeignKey(
        "sku.Agency",
        on_delete=models.PROTECT,
        related_name="wms_new_acceptances",
    )
    task_number = models.CharField(max_length=128, blank=True)
    title = models.CharField(max_length=255, blank=True)
    received_qty = models.PositiveIntegerField(default=0)
    expected_qty = models.PositiveIntegerField(default=0)
    acceptance_type = models.CharField(max_length=16, choices=TYPE_CHOICES, default=TYPE_MANUAL)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_CREATED)
    source_status = models.CharField(max_length=128, blank=True)
    source_created_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    source_updated_at = models.DateTimeField(null=True, blank=True)
    last_synced_at = models.DateTimeField(null=True, blank=True)
    source_snapshot = models.JSONField(default=dict, blank=True)
    pilot_revision = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "wms_new_acceptance"
        ordering = ("-source_created_at", "-id")
        indexes = (
            models.Index(fields=("agency", "status")),
            models.Index(fields=("status", "source_created_at")),
        )


class WmsNewMarkingCode(models.Model):
    """Independent marking-code registry used only by the WMS NEW pilot."""

    TYPE_UNIT = "unit"
    TYPE_GROUP = "group"
    TYPE_CHOICES = (
        (TYPE_UNIT, "КИЗ товара"),
        (TYPE_GROUP, "Код групповой упаковки"),
    )
    SOURCE_SCAN = "scan"
    SOURCE_IMPORT = "import"
    SOURCE_MANUAL = "manual"
    SOURCE_CHOICES = (
        (SOURCE_SCAN, "Сканер"),
        (SOURCE_IMPORT, "Импорт"),
        (SOURCE_MANUAL, "Ручной ввод"),
    )

    source_code_id = models.BigIntegerField(null=True, blank=True, unique=True)
    agency = models.ForeignKey(
        "sku.Agency",
        on_delete=models.PROTECT,
        related_name="wms_new_marking_codes",
    )
    product = models.ForeignKey(
        WmsNewProduct,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="marking_codes",
    )
    product_name = models.CharField(max_length=255, blank=True)
    article = models.CharField(max_length=128, blank=True)
    barcode = models.CharField(max_length=128, blank=True)
    code_type = models.CharField(max_length=16, choices=TYPE_CHOICES, default=TYPE_UNIT)
    code = models.TextField(unique=True)
    source = models.CharField(max_length=16, choices=SOURCE_CHOICES, default=SOURCE_MANUAL)
    received_reference = models.CharField(max_length=128, blank=True)
    retired_reference = models.CharField(max_length=128, blank=True)
    received_at = models.DateTimeField(null=True, blank=True)
    retired_at = models.DateTimeField(null=True, blank=True)
    processed_at = models.DateTimeField(null=True, blank=True)
    printed_at = models.DateTimeField(null=True, blank=True)
    print_count = models.PositiveIntegerField(default=0)
    file_name = models.CharField(max_length=255, blank=True)
    is_returned = models.BooleanField(default=False)
    source_updated_at = models.DateTimeField(null=True, blank=True)
    last_synced_at = models.DateTimeField(null=True, blank=True)
    source_snapshot = models.JSONField(default=dict, blank=True)
    pilot_revision = models.PositiveIntegerField(default=0)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_wms_new_marking_codes",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "wms_new_marking_code"
        ordering = ("-created_at", "-id")
        indexes = (
            models.Index(fields=("agency", "product")),
            models.Index(fields=("retired_at", "printed_at")),
            models.Index(fields=("source", "created_at")),
        )


class WmsNewExtraFieldDefinition(models.Model):
    TYPE_TEXT = "text"
    TYPE_NUMBER = "number"
    TYPE_BOOLEAN = "boolean"
    TYPE_DATE = "date"
    TYPE_CHOICES = (
        (TYPE_TEXT, "Текст"),
        (TYPE_NUMBER, "Число"),
        (TYPE_BOOLEAN, "Да/нет"),
        (TYPE_DATE, "Дата"),
    )

    source_definition_id = models.BigIntegerField(null=True, blank=True, unique=True)
    name = models.CharField(max_length=128)
    code = models.SlugField(max_length=96, unique=True, allow_unicode=True)
    field_type = models.CharField(max_length=16, choices=TYPE_CHOICES, default=TYPE_TEXT)
    is_active = models.BooleanField(default=True)
    sort_order = models.PositiveIntegerField(default=100)
    options = models.JSONField(default=list, blank=True)
    source_updated_at = models.DateTimeField(null=True, blank=True)
    last_synced_at = models.DateTimeField(null=True, blank=True)
    source_snapshot = models.JSONField(default=dict, blank=True)
    pilot_revision = models.PositiveIntegerField(default=0)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_wms_new_extra_fields",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "wms_new_extra_field_definition"
        ordering = ("sort_order", "name", "id")
        indexes = (
            models.Index(fields=("is_active", "field_type")),
        )


class WmsNewExtraFieldValue(models.Model):
    source_value_id = models.BigIntegerField(null=True, blank=True, unique=True)
    definition = models.ForeignKey(
        WmsNewExtraFieldDefinition,
        on_delete=models.PROTECT,
        related_name="values",
    )
    product = models.ForeignKey(
        WmsNewProduct,
        on_delete=models.PROTECT,
        related_name="extra_field_values",
    )
    value = models.JSONField(null=True, blank=True)
    source_updated_at = models.DateTimeField(null=True, blank=True)
    last_synced_at = models.DateTimeField(null=True, blank=True)
    source_snapshot = models.JSONField(default=dict, blank=True)
    pilot_revision = models.PositiveIntegerField(default=0)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="updated_wms_new_extra_field_values",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "wms_new_extra_field_value"
        ordering = ("definition_id", "product_id")
        constraints = (
            models.UniqueConstraint(
                fields=("definition", "product"),
                name="uniq_wms_new_extra_field_value",
            ),
        )
        indexes = (
            models.Index(fields=("product", "definition")),
        )


class WmsNewInventorySession(models.Model):
    """Independent inventory workflow for the WMS NEW pilot."""

    SCOPE_ALL = "all"
    SCOPE_AGENCY = "agency"
    SCOPE_CELL = "cell"
    SCOPE_PALLET = "pallet"
    SCOPE_BOX = "box"
    SCOPE_PRODUCT = "product"
    SCOPE_CHOICES = (
        (SCOPE_ALL, "Весь склад"),
        (SCOPE_AGENCY, "Партнер"),
        (SCOPE_CELL, "Ячейка"),
        (SCOPE_PALLET, "Паллета"),
        (SCOPE_BOX, "Короб"),
        (SCOPE_PRODUCT, "Товар"),
    )

    MODE_AUDIT = "audit"
    MODE_DRAIN = "drain"
    MODE_IMMEDIATE = "immediate"
    MODE_CHOICES = (
        (MODE_AUDIT, "Контрольный пересчет"),
        (MODE_DRAIN, "После завершения операций"),
        (MODE_IMMEDIATE, "Немедленная блокировка WMS NEW"),
    )

    SCAN_MODE_KIZ = "kiz"
    SCAN_MODE_BARCODE = "barcode"
    SCAN_MODE_CHOICES = (
        (SCAN_MODE_KIZ, "По КИЗам"),
        (SCAN_MODE_BARCODE, "По штрихкодам"),
    )

    STATUS_PLANNED = "planned"
    STATUS_DRAINING = "draining"
    STATUS_COUNTING = "counting"
    STATUS_RECOUNT = "recount"
    STATUS_APPROVAL = "approval"
    STATUS_DONE = "done"
    STATUS_CANCELLED = "cancelled"
    STATUS_CHOICES = (
        (STATUS_PLANNED, "Запланирована"),
        (STATUS_DRAINING, "Ожидает завершения операций"),
        (STATUS_COUNTING, "Первый пересчет"),
        (STATUS_RECOUNT, "Повторный пересчет"),
        (STATUS_APPROVAL, "Ожидает утверждения"),
        (STATUS_DONE, "Завершена"),
        (STATUS_CANCELLED, "Отменена"),
    )

    source_session_id = models.BigIntegerField(null=True, blank=True, unique=True)
    number = models.CharField(max_length=64, unique=True)
    scope_type = models.CharField(max_length=16, choices=SCOPE_CHOICES)
    mode = models.CharField(max_length=16, choices=MODE_CHOICES, default=MODE_AUDIT)
    scan_mode = models.CharField(
        max_length=16,
        choices=SCAN_MODE_CHOICES,
        default=SCAN_MODE_BARCODE,
    )
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_PLANNED)
    agency = models.ForeignKey(
        "sku.Agency",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="wms_new_inventory_sessions",
    )
    cell = models.ForeignKey(
        "fbs.FbsStorageCell",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="wms_new_inventory_sessions",
    )
    pallet = models.ForeignKey(
        "fbs.FbsPallet",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="wms_new_inventory_sessions",
    )
    box = models.ForeignKey(
        "fbs.FbsBox",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="wms_new_inventory_sessions",
    )
    product = models.ForeignKey(
        WmsNewProduct,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="inventory_sessions",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_wms_new_inventory_sessions",
    )
    first_counter = models.ForeignKey(
        "employees.Employee",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="first_wms_new_inventory_sessions",
    )
    second_counter = models.ForeignKey(
        "employees.Employee",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="second_wms_new_inventory_sessions",
    )
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="approved_wms_new_inventory_sessions",
    )
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    source_updated_at = models.DateTimeField(null=True, blank=True)
    last_synced_at = models.DateTimeField(null=True, blank=True)
    source_snapshot = models.JSONField(default=dict, blank=True)
    pilot_revision = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "wms_new_inventory_session"
        ordering = ("-created_at", "-id")
        indexes = (
            models.Index(fields=("status", "created_at")),
            models.Index(fields=("agency", "status")),
            models.Index(fields=("scope_type", "status")),
        )

    def clean(self) -> None:
        super().clean()
        required_field = {
            self.SCOPE_AGENCY: "agency_id",
            self.SCOPE_CELL: "cell_id",
            self.SCOPE_PALLET: "pallet_id",
            self.SCOPE_BOX: "box_id",
            self.SCOPE_PRODUCT: "product_id",
        }.get(self.scope_type)
        if required_field and not getattr(self, required_field):
            raise ValidationError(
                {required_field.removesuffix("_id"): "Не выбран объект инвентаризации."}
            )
        if self.scope_type == self.SCOPE_PRODUCT and self.product_id:
            if self.agency_id and self.product.agency_id != self.agency_id:
                raise ValidationError({"product": "Товар принадлежит другому партнеру."})

    @property
    def has_discrepancies(self) -> bool:
        return self.lines.exclude(first_count_qty=models.F("expected_qty")).exists()


class WmsNewInventoryLine(models.Model):
    session = models.ForeignKey(
        WmsNewInventorySession,
        on_delete=models.CASCADE,
        related_name="lines",
    )
    source_line_id = models.BigIntegerField(null=True, blank=True, unique=True)
    source_balance_id = models.BigIntegerField(null=True, blank=True)
    product = models.ForeignKey(
        WmsNewProduct,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="inventory_lines",
    )
    agency = models.ForeignKey(
        "sku.Agency",
        on_delete=models.PROTECT,
        related_name="wms_new_inventory_lines",
    )
    location_code = models.CharField(max_length=128, blank=True)
    cell_code = models.CharField(max_length=64, blank=True)
    pallet_code = models.CharField(max_length=128, blank=True)
    box_code = models.CharField(max_length=128, blank=True)
    sku_code = models.CharField(max_length=128)
    product_name = models.CharField(max_length=255, blank=True)
    barcode = models.CharField(max_length=128, blank=True)
    marking_code = models.CharField(max_length=256, blank=True)
    expected_qty = models.PositiveIntegerField(default=0)
    first_count_qty = models.PositiveIntegerField(null=True, blank=True)
    second_count_qty = models.PositiveIntegerField(null=True, blank=True)
    final_qty = models.PositiveIntegerField(null=True, blank=True)
    approved_delta = models.IntegerField(default=0)
    source_snapshot = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "wms_new_inventory_line"
        ordering = ("location_code", "box_code", "sku_code", "id")
        constraints = (
            models.UniqueConstraint(
                fields=("session", "source_balance_id"),
                name="uniq_wms_new_inventory_balance",
            ),
        )
        indexes = (
            models.Index(fields=("session", "product")),
            models.Index(fields=("barcode",)),
            models.Index(fields=("marking_code",)),
        )


class WmsNewInventoryScan(models.Model):
    ROUND_FIRST = 1
    ROUND_SECOND = 2
    ROUND_CHOICES = ((ROUND_FIRST, "Первый"), (ROUND_SECOND, "Повторный"))

    source_scan_id = models.BigIntegerField(null=True, blank=True, unique=True)
    line = models.ForeignKey(
        WmsNewInventoryLine,
        on_delete=models.CASCADE,
        related_name="scans",
    )
    count_round = models.PositiveSmallIntegerField(choices=ROUND_CHOICES)
    scan_code = models.CharField(max_length=256)
    qty = models.PositiveIntegerField(default=1)
    counted_by = models.ForeignKey(
        "employees.Employee",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="wms_new_inventory_scans",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "wms_new_inventory_scan"
        ordering = ("created_at", "id")
        indexes = (
            models.Index(fields=("line", "count_round")),
            models.Index(fields=("scan_code",)),
        )
        constraints = (
            models.CheckConstraint(
                condition=models.Q(qty__gt=0),
                name="wms_new_inventory_scan_qty_gt_zero",
            ),
        )


class WmsNewInventoryLock(models.Model):
    """A lock observed only by new WMS services; legacy work is never blocked."""

    session = models.OneToOneField(
        WmsNewInventorySession,
        on_delete=models.CASCADE,
        related_name="storage_lock",
    )
    block_new_operations = models.BooleanField(default=True)
    block_execution = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    released_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "wms_new_inventory_lock"
        indexes = (
            models.Index(fields=("is_active", "block_new_operations")),
            models.Index(fields=("is_active", "block_execution")),
        )


class WmsNewBox(models.Model):
    STATUS_ACTIVE = "active"
    STATUS_MERGED = "merged"
    STATUS_SPLIT = "split"
    STATUS_ARCHIVED = "archived"
    STATUS_CHOICES = (
        (STATUS_ACTIVE, "Активен"),
        (STATUS_MERGED, "Объединен"),
        (STATUS_SPLIT, "Разделен"),
        (STATUS_ARCHIVED, "Архив"),
    )

    source_container_id = models.BigIntegerField(null=True, blank=True, unique=True)
    agency = models.ForeignKey(
        "sku.Agency",
        on_delete=models.PROTECT,
        related_name="wms_new_boxes",
    )
    code = models.CharField(max_length=128)
    source_parent_container_id = models.BigIntegerField(null=True, blank=True)
    parent_code = models.CharField(max_length=128, blank=True)
    location = models.ForeignKey(
        "sklad.WarehouseLocation",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="wms_new_boxes",
    )
    location_code = models.CharField(max_length=128, blank=True)
    zone_code = models.CharField(max_length=32, blank=True)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_ACTIVE)
    gross_weight_g = models.PositiveIntegerField(null=True, blank=True)
    width_mm = models.PositiveIntegerField(null=True, blank=True)
    height_mm = models.PositiveIntegerField(null=True, blank=True)
    depth_mm = models.PositiveIntegerField(null=True, blank=True)
    sku_count = models.PositiveIntegerField(default=0)
    stock_on_hand = models.PositiveIntegerField(default=0)
    stock_free = models.PositiveIntegerField(default=0)
    reserved_qty = models.PositiveIntegerField(default=0)
    marking_count = models.PositiveIntegerField(default=0)
    source_context_type = models.CharField(max_length=32, blank=True)
    source_context_id = models.CharField(max_length=64, blank=True)
    source_updated_at = models.DateTimeField(null=True, blank=True)
    last_synced_at = models.DateTimeField(null=True, blank=True)
    source_snapshot = models.JSONField(default=dict, blank=True)
    is_manual = models.BooleanField(default=False)
    pilot_revision = models.PositiveIntegerField(default=0)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_wms_new_boxes",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "wms_new_box"
        ordering = ("code", "id")
        constraints = (
            models.UniqueConstraint(
                fields=("agency", "code"),
                name="uniq_wms_new_box_code",
            ),
        )
        indexes = (
            models.Index(fields=("agency", "status")),
            models.Index(fields=("location", "status")),
            models.Index(fields=("zone_code", "status")),
            models.Index(fields=("stock_on_hand", "status")),
        )


class WmsNewBoxItem(models.Model):
    source_snapshot_id = models.BigIntegerField(null=True, blank=True, unique=True)
    box = models.ForeignKey(WmsNewBox, on_delete=models.CASCADE, related_name="items")
    product = models.ForeignKey(
        WmsNewProduct,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="box_items",
    )
    agency = models.ForeignKey(
        "sku.Agency",
        on_delete=models.PROTECT,
        related_name="wms_new_box_items",
    )
    sku_code = models.CharField(max_length=128)
    product_name = models.CharField(max_length=255, blank=True)
    size = models.CharField(max_length=64, blank=True)
    barcode = models.CharField(max_length=128, blank=True)
    goods_type = models.CharField(max_length=64, blank=True)
    marking_code = models.CharField(max_length=256, blank=True)
    qty = models.PositiveIntegerField(default=0)
    available_qty = models.PositiveIntegerField(default=0)
    reserved_qty = models.PositiveIntegerField(default=0)
    warehouse_state_code = models.CharField(max_length=64, blank=True)
    source_updated_at = models.DateTimeField(null=True, blank=True)
    last_synced_at = models.DateTimeField(null=True, blank=True)
    source_snapshot = models.JSONField(default=dict, blank=True)
    pilot_revision = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "wms_new_box_item"
        ordering = ("sku_code", "size", "id")
        indexes = (
            models.Index(fields=("box", "sku_code")),
            models.Index(fields=("product", "box")),
            models.Index(fields=("barcode",)),
            models.Index(fields=("marking_code",)),
        )


class WmsNewDocument(models.Model):
    TYPE_RECEIPT = "receipt"
    TYPE_WRITEOFF = "writeoff"
    TYPE_CHOICES = (
        (TYPE_RECEIPT, "Приход"),
        (TYPE_WRITEOFF, "Расход"),
    )
    STATUS_DRAFT = "draft"
    STATUS_POSTED = "posted"
    STATUS_CANCELLED = "cancelled"
    STATUS_CHOICES = (
        (STATUS_DRAFT, "Черновик"),
        (STATUS_POSTED, "Проведен"),
        (STATUS_CANCELLED, "Отменен"),
    )

    source_key = models.CharField(max_length=160, null=True, blank=True, unique=True)
    source_acceptance = models.ForeignKey(
        WmsNewAcceptance,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="documents",
    )
    source_shipment = models.ForeignKey(
        WmsNewShipment,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="documents",
    )
    agency = models.ForeignKey(
        "sku.Agency",
        on_delete=models.PROTECT,
        related_name="wms_new_documents",
    )
    document_type = models.CharField(max_length=16, choices=TYPE_CHOICES)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_DRAFT)
    basis = models.CharField(max_length=255, blank=True)
    warehouse_name = models.CharField(max_length=128, default="Основной склад")
    comment = models.TextField(blank=True)
    internal_comment = models.TextField(blank=True)
    sku_count = models.PositiveIntegerField(default=0)
    unit_count = models.PositiveIntegerField(default=0)
    source_created_at = models.DateTimeField(null=True, blank=True)
    source_updated_at = models.DateTimeField(null=True, blank=True)
    last_synced_at = models.DateTimeField(null=True, blank=True)
    posted_at = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    source_snapshot = models.JSONField(default=dict, blank=True)
    is_manual = models.BooleanField(default=False)
    pilot_revision = models.PositiveIntegerField(default=0)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_wms_new_documents",
    )
    posted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="posted_wms_new_documents",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "wms_new_document"
        ordering = ("-source_created_at", "-created_at", "-id")
        indexes = (
            models.Index(fields=("document_type", "status", "source_created_at")),
            models.Index(fields=("agency", "document_type", "status")),
        )


class WmsNewDocumentItem(models.Model):
    document = models.ForeignKey(
        WmsNewDocument,
        on_delete=models.CASCADE,
        related_name="items",
    )
    product = models.ForeignKey(
        WmsNewProduct,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="document_items",
    )
    location = models.ForeignKey(
        "sklad.WarehouseLocation",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="wms_new_document_items",
    )
    product_name = models.CharField(max_length=255)
    article = models.CharField(max_length=128, blank=True)
    barcode = models.CharField(max_length=128, blank=True)
    location_name = models.CharField(max_length=255, blank=True)
    quantity = models.PositiveIntegerField(default=1)
    unit_price = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    source_line_key = models.CharField(max_length=160, blank=True)
    source_snapshot = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "wms_new_document_item"
        ordering = ("id",)
        indexes = (
            models.Index(fields=("document", "product")),
            models.Index(fields=("document", "article")),
        )


class WmsNewMarketplaceStock(models.Model):
    """Independent marketplace warehouse stock snapshot for FBS-NEW analytics."""

    MARKETPLACE_WB = "wb"
    MARKETPLACE_OZON = "ozon"
    MARKETPLACE_CHOICES = (
        (MARKETPLACE_WB, "Wildberries"),
        (MARKETPLACE_OZON, "Ozon"),
    )

    agency = models.ForeignKey(
        "sku.Agency",
        on_delete=models.PROTECT,
        related_name="wms_new_marketplace_stocks",
    )
    product = models.ForeignKey(
        WmsNewProduct,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="marketplace_stocks",
    )
    marketplace = models.CharField(max_length=16, choices=MARKETPLACE_CHOICES)
    credential_source_id = models.PositiveIntegerField(null=True, blank=True)
    warehouse_code = models.CharField(max_length=128)
    warehouse_name = models.CharField(max_length=255)
    sku_code = models.CharField(max_length=128)
    barcode = models.CharField(max_length=128, blank=True)
    product_name = models.CharField(max_length=255, blank=True)
    category = models.CharField(max_length=128, blank=True)
    quantity = models.PositiveIntegerField(default=0)
    fullbox_qty = models.PositiveIntegerField(default=0)
    sales_30d = models.PositiveIntegerField(default=0)
    days_cover = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    fetched_at = models.DateTimeField()
    source_snapshot = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "wms_new_marketplace_stock"
        ordering = ("agency_id", "marketplace", "warehouse_name", "product_name", "sku_code")
        constraints = (
            models.UniqueConstraint(
                fields=("agency", "marketplace", "warehouse_code", "sku_code"),
                name="uniq_wms_new_market_stock_row",
            ),
        )
        indexes = (
            models.Index(fields=("agency", "marketplace", "fetched_at")),
            models.Index(fields=("marketplace", "warehouse_code")),
            models.Index(fields=("quantity", "days_cover")),
        )


class WmsNewPartnerProfile(models.Model):
    """Independent FBS-NEW partner settings layered over factual Fullbox clients."""

    source_agency = models.OneToOneField(
        "sku.Agency",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="wms_new_partner_profile",
    )
    name = models.CharField(max_length=255)
    balance = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    is_active = models.BooleanField(default=True)
    wb_products_enabled = models.BooleanField(default=False)
    wb_orders_enabled = models.BooleanField(default=False)
    ozon_products_enabled = models.BooleanField(default=False)
    ozon_orders_enabled = models.BooleanField(default=False)
    requisites = models.CharField(max_length=255, blank=True)
    is_manual = models.BooleanField(default=False)
    pilot_revision = models.PositiveIntegerField(default=0)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_wms_new_partner_profiles",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "wms_new_partner_profile"
        ordering = ("name", "id")
        indexes = (
            models.Index(fields=("is_active", "name")),
        )

    def __str__(self) -> str:
        return self.name


class WmsNewInvoice(models.Model):
    TYPE_SERVICES = "services"
    TYPE_BALANCE = "balance"
    TYPE_CHOICES = (
        (TYPE_SERVICES, "Счет на услуги"),
        (TYPE_BALANCE, "Счет на пополнение баланса"),
    )
    STATUS_DRAFT = "draft"
    STATUS_READY = "ready"
    STATUS_CANCELLED = "cancelled"
    STATUS_CHOICES = (
        (STATUS_DRAFT, "Черновик"),
        (STATUS_READY, "Готовый счет"),
        (STATUS_CANCELLED, "Отменен"),
    )

    agency = models.ForeignKey(
        "sku.Agency",
        on_delete=models.PROTECT,
        related_name="wms_new_invoices",
    )
    number = models.CharField(max_length=64, unique=True)
    invoice_type = models.CharField(max_length=16, choices=TYPE_CHOICES, default=TYPE_SERVICES)
    requisites = models.CharField(max_length=255, blank=True)
    invoice_date = models.DateField()
    due_date = models.DateField()
    total_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    paid_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_DRAFT)
    source_snapshot = models.JSONField(default=dict, blank=True)
    pilot_revision = models.PositiveIntegerField(default=0)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_wms_new_invoices",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "wms_new_invoice"
        ordering = ("-invoice_date", "-id")
        indexes = (
            models.Index(fields=("agency", "status")),
            models.Index(fields=("status", "due_date")),
        )

    def __str__(self) -> str:
        return f"Счет {self.number}"


class WmsNewBillingItem(models.Model):
    KIND_TASK = "task"
    KIND_STORAGE = "storage"
    KIND_FBS = "fbs"
    KIND_CHOICES = (
        (KIND_TASK, "Задача"),
        (KIND_STORAGE, "Хранение"),
        (KIND_FBS, "FBS"),
    )
    STATUS_UNPRICED = "unpriced"
    STATUS_PRICED = "priced"
    STATUS_INVOICED = "invoiced"
    STATUS_ARCHIVED = "archived"
    STATUS_CHOICES = (
        (STATUS_UNPRICED, "Не протарифицировано"),
        (STATUS_PRICED, "Протарифицировано, не в счете"),
        (STATUS_INVOICED, "В счете"),
        (STATUS_ARCHIVED, "Архив"),
    )

    agency = models.ForeignKey(
        "sku.Agency",
        on_delete=models.PROTECT,
        related_name="wms_new_billing_items",
    )
    kind = models.CharField(max_length=16, choices=KIND_CHOICES)
    source_key = models.CharField(max_length=160)
    source_task = models.ForeignKey(
        WmsNewTask,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="billing_items",
    )
    source_shipment = models.ForeignKey(
        WmsNewShipment,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="billing_items",
    )
    title = models.CharField(max_length=255)
    occurred_at = models.DateTimeField(null=True, blank=True)
    period_start = models.DateField(null=True, blank=True)
    period_end = models.DateField(null=True, blank=True)
    marketplace = models.CharField(max_length=64, blank=True)
    integration = models.CharField(max_length=128, blank=True)
    quantity = models.DecimalField(max_digits=14, decimal_places=3, default=1)
    unit_price = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_UNPRICED)
    invoice = models.ForeignKey(
        WmsNewInvoice,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="items",
    )
    source_snapshot = models.JSONField(default=dict, blank=True)
    pilot_revision = models.PositiveIntegerField(default=0)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_wms_new_billing_items",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "wms_new_billing_item"
        ordering = ("-occurred_at", "-id")
        constraints = (
            models.UniqueConstraint(
                fields=("kind", "source_key"),
                name="uniq_wms_new_billing_source",
            ),
        )
        indexes = (
            models.Index(fields=("kind", "status", "agency")),
            models.Index(fields=("invoice", "status")),
        )


class WmsNewPrimaryDocument(models.Model):
    TYPE_ACT = "act"
    TYPE_GOODS_INVOICE = "goods_invoice"
    TYPE_CHOICES = (
        (TYPE_ACT, "Акт"),
        (TYPE_GOODS_INVOICE, "Товарная накладная"),
    )
    STATUS_DRAFT = "draft"
    STATUS_READY = "ready"
    STATUS_CHOICES = (
        (STATUS_DRAFT, "Черновик"),
        (STATUS_READY, "Готов"),
    )

    agency = models.ForeignKey(
        "sku.Agency",
        on_delete=models.PROTECT,
        related_name="wms_new_primary_documents",
    )
    invoice = models.ForeignKey(
        WmsNewInvoice,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="primary_documents",
    )
    number = models.CharField(max_length=64, unique=True)
    document_type = models.CharField(max_length=24, choices=TYPE_CHOICES, default=TYPE_ACT)
    period = models.DateField()
    amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_READY)
    source_snapshot = models.JSONField(default=dict, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_wms_new_primary_documents",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "wms_new_primary_document"
        ordering = ("-period", "agency_id", "id")
        indexes = (
            models.Index(fields=("period", "document_type")),
            models.Index(fields=("agency", "period")),
        )


class WmsNewRecord(models.Model):
    """Versioned shadow record for donor modules not yet promoted to typed models."""

    module = models.CharField(max_length=64)
    entity_type = models.CharField(max_length=64)
    source_key = models.CharField(max_length=128)
    title = models.CharField(max_length=255, blank=True)
    status = models.CharField(max_length=64, blank=True)
    agency = models.ForeignKey(
        "sku.Agency",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="wms_new_records",
    )
    payload = models.JSONField(default=dict, blank=True)
    source_updated_at = models.DateTimeField(null=True, blank=True)
    last_synced_at = models.DateTimeField(null=True, blank=True)
    pilot_revision = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "wms_new_record"
        constraints = (
            models.UniqueConstraint(
                fields=("module", "entity_type", "source_key"),
                name="uniq_wms_new_shadow_record",
            ),
        )
        indexes = (
            models.Index(fields=("module", "entity_type", "status")),
        )


class WmsNewEvent(models.Model):
    entity_type = models.CharField(max_length=64)
    entity_id = models.BigIntegerField()
    action = models.CharField(max_length=64)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="wms_new_events",
    )
    before = models.JSONField(default=dict, blank=True)
    after = models.JSONField(default=dict, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "wms_new_event"
        ordering = ("-created_at", "-id")
        indexes = (
            models.Index(fields=("entity_type", "entity_id", "created_at")),
            models.Index(fields=("action", "created_at")),
        )


class WmsNewSyncRun(models.Model):
    STATUS_RUNNING = "running"
    STATUS_DONE = "done"
    STATUS_FAILED = "failed"
    STATUS_CHOICES = (
        (STATUS_RUNNING, "Выполняется"),
        (STATUS_DONE, "Завершена"),
        (STATUS_FAILED, "Ошибка"),
    )

    scope = models.CharField(max_length=64)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_RUNNING)
    imported = models.PositiveIntegerField(default=0)
    updated = models.PositiveIntegerField(default=0)
    error = models.TextField(blank=True)
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "wms_new_sync_run"
        ordering = ("-started_at", "-id")
