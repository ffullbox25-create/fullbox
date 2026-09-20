from django.conf import settings
from django.db import models

from sku.models import Agency, SKU


class InventoryState(models.Model):
    STATE_PROCESSING = "processing"
    STATE_WAREHOUSE = "warehouse"

    STATE_CHOICES = [
        (STATE_PROCESSING, "В обработке"),
        (STATE_WAREHOUSE, "На складе"),
    ]

    agency = models.ForeignKey(Agency, on_delete=models.CASCADE, related_name="inventory_states")
    order_type = models.CharField(max_length=32, default="processing")
    order_id = models.CharField(max_length=64)
    sku = models.CharField(max_length=64)
    size = models.CharField(max_length=64, blank=True)
    barcode = models.CharField(max_length=64, blank=True)
    goods_type = models.CharField(max_length=64, blank=True)
    qty = models.PositiveIntegerField(default=0)
    state = models.CharField(max_length=32, choices=STATE_CHOICES, default=STATE_PROCESSING)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["agency", "state"]),
            models.Index(fields=["agency", "sku"]),
            models.Index(fields=["order_type", "order_id"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=[
                    "agency",
                    "order_type",
                    "order_id",
                    "sku",
                    "size",
                    "barcode",
                    "goods_type",
                    "state",
                ],
                name="uniq_inventory_state_row",
            )
        ]

    def __str__(self) -> str:
        return f"{self.sku} · {self.size or '-'} · {self.state}"


class StockPalletState(models.Model):
    STATE_WAREHOUSE = "warehouse"

    STATE_CHOICES = [
        (STATE_WAREHOUSE, "На складе"),
    ]

    agency = models.ForeignKey(Agency, on_delete=models.CASCADE, related_name="stock_pallet_states")
    sku_ref = models.ForeignKey(
        "sku.SKU",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="stock_pallet_states",
        verbose_name="SKU",
    )
    order_type = models.CharField(max_length=32, default="receiving")
    order_id = models.CharField(max_length=64)
    sku = models.CharField(max_length=64)
    name = models.CharField(max_length=255, blank=True)
    size = models.CharField(max_length=64, blank=True)
    barcode = models.CharField(max_length=64, blank=True)
    marking_code = models.CharField(max_length=128, blank=True)
    goods_type = models.CharField(max_length=64, blank=True)
    qty = models.PositiveIntegerField(default=0)
    processing_reserved_qty = models.PositiveIntegerField(default=0)
    shipping_reserved_qty = models.PositiveIntegerField(default=0)
    available_qty = models.PositiveIntegerField(default=0)
    box_code = models.CharField(max_length=128, blank=True)
    pallet_code = models.CharField(max_length=128, blank=True)
    zone = models.CharField(max_length=16, blank=True)
    row = models.PositiveIntegerField(default=0)
    section = models.PositiveIntegerField(default=0)
    tier = models.PositiveIntegerField(default=0)
    cell = models.PositiveIntegerField(default=0)
    location = models.CharField(max_length=255, blank=True)
    state = models.CharField(max_length=32, choices=STATE_CHOICES, default=STATE_WAREHOUSE)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["agency", "state"]),
            models.Index(fields=["agency", "sku"]),
            models.Index(fields=["agency", "pallet_code"]),
            models.Index(fields=["order_type", "order_id"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(pallet_code="")
                    | (
                        ~models.Q(pallet_code="")
                        & ~models.Q(zone="")
                        & ~models.Q(location="")
                    )
                ),
                name="stock_pallet_location_required",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.pallet_code or '-'} · {self.sku} · {self.qty}"


class WarehouseTemporaryNomenclature(models.Model):
    agency = models.ForeignKey(
        Agency,
        on_delete=models.CASCADE,
        related_name="warehouse_temporary_nomenclature",
        verbose_name="Клиент",
    )
    identity_key = models.CharField(max_length=512)
    item_code = models.CharField("Временный артикул", max_length=64, blank=True)
    name = models.CharField("Наименование", max_length=255)
    brand = models.CharField("Бренд", max_length=255, blank=True)
    color = models.CharField("Цвет", max_length=64, blank=True)
    size = models.CharField("Размер", max_length=64, blank=True)
    barcode = models.CharField("Штрихкод", max_length=64, blank=True)
    goods_type = models.CharField("Тип товара", max_length=64, default="op", blank=True)
    first_context_type = models.CharField(max_length=32, blank=True)
    first_context_id = models.CharField(max_length=64, blank=True)
    last_context_type = models.CharField(max_length=32, blank=True)
    last_context_id = models.CharField(max_length=64, blank=True)
    normalized_sku_ref = models.ForeignKey(
        SKU,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="temporary_nomenclature_sources",
        verbose_name="Нормализованный SKU",
    )
    normalized_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "warehouse_temporary_nomenclature"
        verbose_name = "Временная номенклатура склада"
        verbose_name_plural = "Временная номенклатура склада"
        ordering = ["item_code", "name", "size", "id"]
        indexes = [
            models.Index(fields=["agency", "item_code"]),
            models.Index(fields=["agency", "normalized_at"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["agency", "identity_key"],
                name="uniq_warehouse_temp_nomenclature_identity",
            )
        ]

    def __str__(self) -> str:
        code = self.item_code or f"TEMP-{self.pk or '-'}"
        brand = f" · {self.brand}" if self.brand else ""
        color = f" · {self.color}" if self.color else ""
        size = f" · {self.size}" if self.size else ""
        return f"{code} · {self.name}{brand}{color}{size}"


class WarehouseLocation(models.Model):
    ZONE_KIND_RECEIVING = "receiving"
    ZONE_KIND_STORAGE = "storage"
    ZONE_KIND_PROCESSING = "processing"
    ZONE_KIND_SHIPPING = "shipping"
    ZONE_KIND_LOADING = "loading"
    ZONE_KIND_TRANSIT = "transit"
    ZONE_KIND_VEHICLE = "vehicle"
    ZONE_KIND_VIRTUAL = "virtual"

    ZONE_KIND_CHOICES = [
        (ZONE_KIND_RECEIVING, "Приемка"),
        (ZONE_KIND_STORAGE, "Хранение"),
        (ZONE_KIND_PROCESSING, "Обработка"),
        (ZONE_KIND_SHIPPING, "Отгрузка"),
        (ZONE_KIND_LOADING, "Погрузка"),
        (ZONE_KIND_TRANSIT, "Транзит"),
        (ZONE_KIND_VEHICLE, "Транспорт"),
        (ZONE_KIND_VIRTUAL, "Виртуальная зона"),
    ]

    warehouse_code = models.CharField(max_length=32, default="MSK")
    zone_code = models.CharField(max_length=16)
    zone_kind = models.CharField(max_length=32, choices=ZONE_KIND_CHOICES, default=ZONE_KIND_STORAGE)
    row_no = models.PositiveIntegerField(default=0)
    section_no = models.PositiveIntegerField(default=0)
    tier_no = models.PositiveIntegerField(default=0)
    cell_no = models.PositiveIntegerField(default=0)
    location_code = models.CharField(max_length=64, blank=True)
    display_name = models.CharField(max_length=255, blank=True)
    capacity_containers = models.PositiveIntegerField(
        default=0,
        help_text="0 означает, что вместимость места не ограничена.",
    )
    is_topology_visible = models.BooleanField(default=True)
    is_fbs_visible = models.BooleanField(default=False)
    allow_mixed_client_pallets = models.BooleanField(
        default=False,
        help_text=(
            "Разрешает размещать в этом операционном месте FBS-паллеты "
            "разных клиентов."
        ),
    )
    is_active = models.BooleanField(default=True)
    is_pickable = models.BooleanField(default=False)
    is_storage = models.BooleanField(default=False)
    is_processing = models.BooleanField(default=False)
    is_shipping = models.BooleanField(default=False)
    is_loading = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "warehouse_location"
        indexes = [
            models.Index(fields=["zone_code"]),
            models.Index(fields=["zone_kind"]),
            models.Index(fields=["warehouse_code", "zone_code"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["warehouse_code", "zone_code", "row_no", "section_no", "tier_no", "cell_no"],
                condition=models.Q(is_topology_visible=True),
                name="uniq_topology_location_slot",
            ),
            models.UniqueConstraint(
                fields=["warehouse_code", "location_code"],
                condition=~models.Q(location_code=""),
                name="uniq_warehouse_location_code",
            ),
        ]

    def __str__(self) -> str:
        return self.display_name or self.location_code or f"{self.zone_code}"


class WarehouseContainer(models.Model):
    TYPE_BOX = "box"
    TYPE_PALLET = "pallet"
    TYPE_MIXED_PALLET = "mixed_pallet"

    TYPE_CHOICES = [
        (TYPE_BOX, "Короб"),
        (TYPE_PALLET, "Паллета"),
        (TYPE_MIXED_PALLET, "Смешанная паллета"),
    ]

    STATUS_ACTIVE = "active"
    STATUS_MERGED = "merged"
    STATUS_SPLIT = "split"
    STATUS_ARCHIVED = "archived"

    STATUS_CHOICES = [
        (STATUS_ACTIVE, "Активен"),
        (STATUS_MERGED, "Объединен"),
        (STATUS_SPLIT, "Разделен"),
        (STATUS_ARCHIVED, "Архив"),
    ]

    agency = models.ForeignKey(Agency, on_delete=models.CASCADE, related_name="warehouse_containers")
    container_type = models.CharField(max_length=32, choices=TYPE_CHOICES)
    container_code = models.CharField(max_length=128)
    parent_container = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="child_containers",
    )
    current_location = models.ForeignKey(
        WarehouseLocation,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="containers",
    )
    status = models.CharField(max_length=32, choices=STATUS_CHOICES, default=STATUS_ACTIVE)
    gross_weight_g = models.PositiveIntegerField(null=True, blank=True)
    width_mm = models.PositiveIntegerField(null=True, blank=True)
    height_mm = models.PositiveIntegerField(null=True, blank=True)
    depth_mm = models.PositiveIntegerField(null=True, blank=True)
    source_context_type = models.CharField(max_length=32, blank=True)
    source_context_id = models.CharField(max_length=64, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_warehouse_containers",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "warehouse_container"
        indexes = [
            models.Index(fields=["agency", "container_type"]),
            models.Index(fields=["current_location"]),
            models.Index(fields=["parent_container"]),
        ]
        constraints = [
            models.UniqueConstraint(fields=["agency", "container_code"], name="uniq_warehouse_container_code"),
        ]

    def __str__(self) -> str:
        return f"{self.container_code} ({self.container_type})"


class WarehouseReserve(models.Model):
    TYPE_PROCESSING = "processing"
    TYPE_SHIPPING = "shipping"
    TYPE_FBS_MOVEMENT = "fbs_movement"
    TYPE_MANUAL = "manual"

    TYPE_CHOICES = [
        (TYPE_PROCESSING, "Под обработку"),
        (TYPE_SHIPPING, "Под отгрузку"),
        (TYPE_FBS_MOVEMENT, "Под перемещение в FBS"),
        (TYPE_MANUAL, "Ручной"),
    ]

    STATUS_ACTIVE = "active"
    STATUS_PARTIALLY_ALLOCATED = "partially_allocated"
    STATUS_ALLOCATED = "allocated"
    STATUS_PARTIALLY_SATISFIED = "partially_satisfied"
    STATUS_SATISFIED = "satisfied"
    STATUS_RELEASED = "released"
    STATUS_CANCELED = "canceled"

    STATUS_CHOICES = [
        (STATUS_ACTIVE, "Активен"),
        (STATUS_PARTIALLY_ALLOCATED, "Частично аллоцирован"),
        (STATUS_ALLOCATED, "Аллоцирован"),
        (STATUS_PARTIALLY_SATISFIED, "Частично выполнен"),
        (STATUS_SATISFIED, "Выполнен"),
        (STATUS_RELEASED, "Снят"),
        (STATUS_CANCELED, "Отменен"),
    ]

    agency = models.ForeignKey(Agency, on_delete=models.CASCADE, related_name="warehouse_reserves")
    reserve_type = models.CharField(max_length=32, choices=TYPE_CHOICES)
    context_type = models.CharField(max_length=32)
    context_id = models.CharField(max_length=64)
    sku_ref = models.ForeignKey(
        SKU,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="warehouse_reserves",
    )
    sku_code = models.CharField(max_length=64)
    size = models.CharField(max_length=64, blank=True)
    barcode = models.CharField(max_length=64, blank=True)
    goods_type = models.CharField(max_length=64, blank=True)
    marking_code = models.CharField(max_length=128, blank=True)
    qty_reserved = models.PositiveIntegerField(default=0)
    qty_allocated = models.PositiveIntegerField(default=0)
    qty_satisfied = models.PositiveIntegerField(default=0)
    status = models.CharField(max_length=32, choices=STATUS_CHOICES, default=STATUS_ACTIVE)
    source_document_type = models.CharField(max_length=32, blank=True)
    source_document_id = models.CharField(max_length=64, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_warehouse_reserves",
    )
    released_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="released_warehouse_reserves",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "warehouse_reserve"
        indexes = [
            models.Index(fields=["agency", "reserve_type", "context_type", "context_id"]),
            models.Index(fields=["agency", "sku_code", "size", "barcode", "goods_type"]),
            models.Index(fields=["status"]),
        ]
        constraints = [
            models.CheckConstraint(condition=models.Q(qty_reserved__gt=0), name="warehouse_reserve_qty_reserved_gt_zero"),
            models.CheckConstraint(condition=models.Q(qty_allocated__gte=0), name="warehouse_reserve_qty_allocated_gte_zero"),
            models.CheckConstraint(condition=models.Q(qty_satisfied__gte=0), name="warehouse_reserve_qty_satisfied_gte_zero"),
            models.CheckConstraint(
                condition=models.Q(qty_allocated__lte=models.F("qty_reserved")),
                name="warehouse_reserve_allocated_lte_reserved",
            ),
            models.CheckConstraint(
                condition=models.Q(qty_satisfied__lte=models.F("qty_reserved")),
                name="warehouse_reserve_satisfied_lte_reserved",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.reserve_type} · {self.sku_code} · {self.qty_reserved}"


class WarehouseOperation(models.Model):
    TYPE_PUTAWAY = "putaway"
    TYPE_PROCESSING = "processing"
    TYPE_MOVE_TO_PROCESSING = "move_to_processing"
    TYPE_MOVE_TO_OTG = "move_to_otg"
    TYPE_PALLETIZATION = "palletization"
    TYPE_MOVE_TO_LOADING = "move_to_loading"
    TYPE_LOAD_TO_VEHICLE = "load_to_vehicle"
    TYPE_RETURN_TO_STORAGE = "return_to_storage"
    TYPE_INTERNAL_RELOCATION = "internal_relocation"

    TYPE_CHOICES = [
        (TYPE_PUTAWAY, "Размещение в хранение"),
        (TYPE_PROCESSING, "Обработка"),
        (TYPE_MOVE_TO_PROCESSING, "Перемещение в обработку"),
        (TYPE_MOVE_TO_OTG, "Перемещение в OTG"),
        (TYPE_PALLETIZATION, "Паллетизация"),
        (TYPE_MOVE_TO_LOADING, "Перемещение в погрузку"),
        (TYPE_LOAD_TO_VEHICLE, "Погрузка в машину"),
        (TYPE_RETURN_TO_STORAGE, "Возврат в хранение"),
        (TYPE_INTERNAL_RELOCATION, "Внутреннее перемещение"),
    ]

    STATUS_CREATED = "created"
    STATUS_PLANNED = "planned"
    STATUS_IN_PROGRESS = "in_progress"
    STATUS_PARTIAL = "partial"
    STATUS_DONE = "done"
    STATUS_BLOCKED = "blocked"
    STATUS_CANCELED = "canceled"

    STATUS_CHOICES = [
        (STATUS_CREATED, "Создана"),
        (STATUS_PLANNED, "Запланирована"),
        (STATUS_IN_PROGRESS, "В работе"),
        (STATUS_PARTIAL, "Частично выполнена"),
        (STATUS_DONE, "Выполнена"),
        (STATUS_BLOCKED, "Заблокирована"),
        (STATUS_CANCELED, "Отменена"),
    ]

    agency = models.ForeignKey(Agency, on_delete=models.CASCADE, related_name="warehouse_operations")
    operation_type = models.CharField(max_length=32, choices=TYPE_CHOICES)
    context_type = models.CharField(max_length=32)
    context_id = models.CharField(max_length=64)
    reserve = models.ForeignKey(
        WarehouseReserve,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="operations",
    )
    source_document_type = models.CharField(max_length=32, blank=True)
    source_document_id = models.CharField(max_length=64, blank=True)
    source_location = models.ForeignKey(
        WarehouseLocation,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="source_operations",
    )
    destination_location = models.ForeignKey(
        WarehouseLocation,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="destination_operations",
    )
    source_zone_code = models.CharField(max_length=16, blank=True)
    destination_zone_code = models.CharField(max_length=16, blank=True)
    status = models.CharField(max_length=32, choices=STATUS_CHOICES, default=STATUS_CREATED)
    priority = models.PositiveSmallIntegerField(default=0)
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="requested_warehouse_operations",
    )
    requested_by_role = models.CharField(max_length=32, blank=True)
    assigned_executor_role = models.CharField(max_length=32, blank=True)
    comment = models.TextField(blank=True)
    planned_qty = models.PositiveIntegerField(default=0)
    done_qty = models.PositiveIntegerField(default=0)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "warehouse_operation"
        indexes = [
            models.Index(fields=["context_type", "context_id"]),
            models.Index(fields=["operation_type"]),
            models.Index(fields=["status"]),
            models.Index(fields=["agency", "destination_zone_code"]),
            models.Index(fields=["reserve"]),
        ]

    def __str__(self) -> str:
        return f"{self.operation_type} · {self.context_type}:{self.context_id}"


class WarehouseOperationTask(models.Model):
    TYPE_PALLET_MOVE = "pallet_move"
    TYPE_BOX_MOVE = "box_move"
    TYPE_PARTIAL_PICK = "partial_pick"
    TYPE_PALLETIZATION_STEP = "palletization_step"
    TYPE_LOADING_STEP = "loading_step"

    TYPE_CHOICES = [
        (TYPE_PALLET_MOVE, "Перемещение паллеты"),
        (TYPE_BOX_MOVE, "Перемещение короба"),
        (TYPE_PARTIAL_PICK, "Частичный отбор"),
        (TYPE_PALLETIZATION_STEP, "Шаг паллетизации"),
        (TYPE_LOADING_STEP, "Шаг погрузки"),
    ]

    STATUS_CREATED = "created"
    STATUS_IN_PROGRESS = "in_progress"
    STATUS_DONE = "done"
    STATUS_FAILED = "failed"
    STATUS_CANCELED = "canceled"

    STATUS_CHOICES = [
        (STATUS_CREATED, "Создана"),
        (STATUS_IN_PROGRESS, "В работе"),
        (STATUS_DONE, "Выполнена"),
        (STATUS_FAILED, "Ошибка"),
        (STATUS_CANCELED, "Отменена"),
    ]

    operation = models.ForeignKey(WarehouseOperation, on_delete=models.CASCADE, related_name="tasks")
    task_type = models.CharField(max_length=32, choices=TYPE_CHOICES)
    container = models.ForeignKey(
        WarehouseContainer,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="operation_tasks",
    )
    from_location = models.ForeignKey(
        WarehouseLocation,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="from_operation_tasks",
    )
    to_location = models.ForeignKey(
        WarehouseLocation,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="to_operation_tasks",
    )
    from_zone_code = models.CharField(max_length=16, blank=True)
    to_zone_code = models.CharField(max_length=16, blank=True)
    qty_planned = models.PositiveIntegerField(default=0)
    qty_done = models.PositiveIntegerField(default=0)
    status = models.CharField(max_length=32, choices=STATUS_CHOICES, default=STATUS_CREATED)
    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="warehouse_operation_tasks",
    )
    assigned_to_name = models.CharField(max_length=255, blank=True)
    executor_role = models.CharField(max_length=32, blank=True)
    payload = models.JSONField(default=dict, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "warehouse_operation_task"
        indexes = [
            models.Index(fields=["operation"]),
            models.Index(fields=["status"]),
            models.Index(fields=["assigned_to"]),
            models.Index(fields=["container"]),
        ]

    def __str__(self) -> str:
        return f"{self.task_type} · {self.status}"


class WarehouseEvent(models.Model):
    agency = models.ForeignKey(Agency, on_delete=models.CASCADE, related_name="warehouse_events")
    event_type = models.CharField(max_length=64)
    stock_context_type = models.CharField(max_length=32, blank=True)
    stock_context_id = models.CharField(max_length=64, blank=True)
    container = models.ForeignKey(
        WarehouseContainer,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="events",
    )
    operation = models.ForeignKey(
        WarehouseOperation,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="events",
    )
    operation_task = models.ForeignKey(
        WarehouseOperationTask,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="events",
    )
    reserve = models.ForeignKey(
        WarehouseReserve,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="events",
    )
    source_document_type = models.CharField(max_length=32, blank=True)
    source_document_id = models.CharField(max_length=64, blank=True)
    from_location = models.ForeignKey(
        WarehouseLocation,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="events_from",
    )
    to_location = models.ForeignKey(
        WarehouseLocation,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="events_to",
    )
    from_zone_code = models.CharField(max_length=16, blank=True)
    to_zone_code = models.CharField(max_length=16, blank=True)
    qty = models.PositiveIntegerField(default=0)
    payload = models.JSONField(default=dict, blank=True)
    performed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="performed_warehouse_events",
    )
    performed_by_role = models.CharField(max_length=32, blank=True)
    occurred_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "warehouse_event"
        indexes = [
            models.Index(fields=["event_type"]),
            models.Index(fields=["stock_context_type", "stock_context_id"]),
            models.Index(fields=["operation"]),
            models.Index(fields=["reserve"]),
            models.Index(fields=["container"]),
            models.Index(fields=["occurred_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.event_type} · {self.occurred_at:%Y-%m-%d %H:%M:%S}"


class WarehouseStockSnapshot(models.Model):
    agency = models.ForeignKey(Agency, on_delete=models.CASCADE, related_name="warehouse_snapshots")
    stock_unit_type = models.CharField(max_length=32, blank=True, default="item")
    source_context_type = models.CharField(max_length=32, blank=True)
    source_context_id = models.CharField(max_length=64, blank=True)
    sku_ref = models.ForeignKey(
        SKU,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="warehouse_snapshots",
    )
    sku_code = models.CharField(max_length=64)
    name = models.CharField(max_length=255, blank=True)
    size = models.CharField(max_length=64, blank=True)
    barcode = models.CharField(max_length=64, blank=True)
    goods_type = models.CharField(max_length=64, blank=True)
    marking_code = models.CharField(max_length=128, blank=True)
    qty = models.PositiveIntegerField(default=0)
    available_qty = models.PositiveIntegerField(default=0)
    processing_reserved_qty = models.PositiveIntegerField(default=0)
    shipping_reserved_qty = models.PositiveIntegerField(default=0)
    other_reserved_qty = models.PositiveIntegerField(default=0)
    container = models.ForeignKey(
        WarehouseContainer,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="snapshots",
    )
    container_code = models.CharField(max_length=128, blank=True)
    parent_container = models.ForeignKey(
        WarehouseContainer,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="child_snapshots",
    )
    location = models.ForeignKey(
        WarehouseLocation,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="snapshots",
    )
    zone_code = models.CharField(max_length=16, blank=True)
    zone_kind = models.CharField(max_length=32, blank=True)
    warehouse_state_code = models.CharField(max_length=64, blank=True)
    active_operation = models.ForeignKey(
        WarehouseOperation,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="active_snapshots",
    )
    active_operation_type = models.CharField(max_length=32, blank=True)
    current_trip_id = models.CharField(max_length=64, blank=True)
    is_in_vehicle = models.BooleanField(default=False)
    is_archived = models.BooleanField(default=False)
    snapshot_version = models.PositiveIntegerField(default=1)
    last_event = models.ForeignKey(
        WarehouseEvent,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="snapshots",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "warehouse_stock_snapshot"
        indexes = [
            models.Index(fields=["agency", "sku_code", "size", "barcode", "goods_type"]),
            models.Index(fields=["warehouse_state_code"]),
            models.Index(fields=["zone_code"]),
            models.Index(fields=["location"]),
            models.Index(fields=["container"]),
            models.Index(fields=["active_operation"]),
            models.Index(fields=["current_trip_id"]),
            models.Index(
                fields=["created_at", "id"],
                name="wh_live_created_idx",
                condition=models.Q(is_archived=False, qty__gt=0),
            ),
            models.Index(
                fields=["agency", "created_at", "id"],
                name="wh_live_ag_created_idx",
                condition=models.Q(is_archived=False, qty__gt=0),
            ),
            models.Index(
                fields=["container_code"],
                name="wh_live_code_idx",
                condition=models.Q(is_archived=False, qty__gt=0),
            ),
        ]
        constraints = [
            models.CheckConstraint(condition=models.Q(available_qty__gte=0), name="warehouse_snapshot_available_gte_zero"),
            models.CheckConstraint(
                condition=models.Q(processing_reserved_qty__gte=0),
                name="warehouse_snapshot_processing_reserved_gte_zero",
            ),
            models.CheckConstraint(
                condition=models.Q(shipping_reserved_qty__gte=0),
                name="warehouse_snapshot_shipping_reserved_gte_zero",
            ),
            models.CheckConstraint(condition=models.Q(other_reserved_qty__gte=0), name="warehouse_snapshot_other_reserved_gte_zero"),
            models.CheckConstraint(condition=models.Q(qty__gte=0), name="warehouse_snapshot_qty_gte_zero"),
            models.CheckConstraint(condition=models.Q(qty__gte=models.F("available_qty")), name="warehouse_snapshot_qty_gte_available"),
        ]

    def __str__(self) -> str:
        return f"{self.sku_code} · {self.qty} · {self.zone_code or '-'}"
