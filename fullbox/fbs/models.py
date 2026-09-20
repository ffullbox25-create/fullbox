import re

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import F, Q
from django.urls import reverse

from .file_storage import fbs_label_storage
from sku.models import Agency, SKU

from .barcode_aliases import same_sku_barcode_variant


class FbsIntegrationProfile(models.Model):
    MARKETPLACE_WB = "wb"
    MARKETPLACE_OZON = "ozon"
    MARKETPLACE_CHOICES = [
        (MARKETPLACE_WB, "Wildberries"),
        (MARKETPLACE_OZON, "Ozon"),
    ]

    STOCK_MODE_DISABLED = "disabled"
    STOCK_MODE_MANAGED = "managed"
    STOCK_MODE_CHOICES = [
        (STOCK_MODE_DISABLED, "Не передавать остатки"),
        (STOCK_MODE_MANAGED, "Передавать остатки из Fullbox"),
    ]

    agency = models.ForeignKey(
        Agency,
        on_delete=models.CASCADE,
        related_name="fbs_integration_profiles",
        verbose_name="Клиент",
    )
    marketplace = models.CharField("Маркетплейс", max_length=16, choices=MARKETPLACE_CHOICES)
    name = models.CharField("Название профиля", max_length=128)
    external_account_id = models.CharField("Внешний кабинет", max_length=128, blank=True)
    external_warehouse_id = models.CharField("Внешний склад", max_length=128)
    stock_mode = models.CharField(
        "Передача остатков",
        max_length=16,
        choices=STOCK_MODE_CHOICES,
        default=STOCK_MODE_DISABLED,
    )
    is_active = models.BooleanField("Активен", default=False)
    order_pull_enabled = models.BooleanField("Получать заказы", default=False)
    status_pull_enabled = models.BooleanField("Получать статусы", default=False)
    outbox_enabled = models.BooleanField("Отправлять команды", default=False)
    marking_push_enabled = models.BooleanField("Передавать КИЗы", default=False)
    stock_push_enabled = models.BooleanField("Передавать остатки", default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "fbs_integration_profile"
        verbose_name = "Профиль FBS"
        verbose_name_plural = "Профили FBS"
        ordering = ["agency_id", "marketplace", "name", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["agency", "marketplace", "external_account_id", "external_warehouse_id"],
                name="uniq_fbs_profile_account_warehouse",
            ),
            models.CheckConstraint(
                condition=Q(stock_push_enabled=False) | Q(stock_mode="managed"),
                name="fbs_stock_push_requires_managed_mode",
            ),
        ]
        indexes = [
            models.Index(fields=["agency", "marketplace", "is_active"]),
            models.Index(fields=["marketplace", "external_warehouse_id"]),
        ]

    def clean(self) -> None:
        super().clean()
        if self.stock_push_enabled and self.stock_mode != self.STOCK_MODE_MANAGED:
            raise ValidationError(
                {"stock_push_enabled": "Передача остатков разрешена только в режиме managed."}
            )

    def __str__(self) -> str:
        return f"{self.agency} / {self.get_marketplace_display()} / {self.name}"


class FbsClientStockPolicy(models.Model):
    agency = models.OneToOneField(
        Agency,
        on_delete=models.CASCADE,
        related_name="fbs_stock_policy",
        verbose_name="Клиент",
    )
    safety_stock_qty = models.PositiveIntegerField(
        "Страховой остаток на каждый SKU",
        default=0,
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "fbs_client_stock_policy"
        verbose_name = "Страховой остаток FBS"
        verbose_name_plural = "Страховые остатки FBS"
        ordering = ["agency_id"]
        constraints = [
            models.CheckConstraint(
                condition=Q(safety_stock_qty__lte=1_000_000),
                name="fbs_stock_safety_qty_lte_1000000",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.agency}: {self.safety_stock_qty} шт."


class FbsOzonCredential(models.Model):
    SLOT_CHOICES = [(slot, f"Кабинет {slot}") for slot in range(1, 4)]

    agency = models.ForeignKey(
        Agency,
        on_delete=models.CASCADE,
        related_name="fbs_ozon_credentials",
        verbose_name="Клиент",
    )
    slot = models.PositiveSmallIntegerField("Номер кабинета", choices=SLOT_CHOICES)
    client_id = models.CharField("Client ID Ozon", max_length=128)
    api_key = models.TextField("API-ключ Ozon")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "fbs_ozon_credential"
        verbose_name = "Реквизиты Ozon FBS"
        verbose_name_plural = "Реквизиты Ozon FBS"
        ordering = ["agency_id", "slot", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["agency", "slot"],
                name="uniq_fbs_ozon_credential_slot",
            ),
            models.UniqueConstraint(
                fields=["agency", "client_id"],
                name="uniq_fbs_ozon_credential_client_id",
            ),
            models.CheckConstraint(
                condition=Q(slot__gte=1) & Q(slot__lte=3),
                name="fbs_ozon_credential_slot_1_3",
            ),
        ]

    def clean(self) -> None:
        super().clean()
        self.client_id = str(self.client_id or "").strip()
        self.api_key = str(self.api_key or "").strip()
        if not 1 <= int(self.slot or 0) <= 3:
            raise ValidationError({"slot": "Доступны только кабинеты Ozon 1–3."})
        if not self.client_id.isdigit() or int(self.client_id) <= 0:
            raise ValidationError(
                {"client_id": "Client ID Ozon должен быть положительным числом."}
            )
        if not self.api_key:
            raise ValidationError({"api_key": "Укажите API-ключ Ozon."})

    def __str__(self) -> str:
        return f"{self.agency} / Ozon FBS {self.slot} / {self.client_id}"


class FbsControllerPolicy(models.Model):
    """Persistent FBS control rules enabled for one controller."""

    controller = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="fbs_controller_policy",
        verbose_name="FBS-контролёр",
    )
    skip_repeat_wb_label_scan = models.BooleanField(
        "Не повторять скан этикетки заказа WB",
        default=False,
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="updated_fbs_controller_policies",
        verbose_name="Кем изменено",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "fbs_controller_policy"
        verbose_name = "Настройка FBS-контролёра"
        verbose_name_plural = "Настройки FBS-контролёров"
        ordering = ["controller_id"]

    def __str__(self) -> str:
        return f"FBS-контролёр {self.controller_id}"


class FbsWavePolicy(models.Model):
    """Maximum size of newly created picking waves for one FBS cabinet."""

    profile = models.OneToOneField(
        FbsIntegrationProfile,
        on_delete=models.CASCADE,
        related_name="wave_policy",
        verbose_name="Кабинет FBS",
    )
    max_orders_per_wave = models.PositiveSmallIntegerField(
        "Максимум заказов в волне",
    )
    max_units_per_wave = models.PositiveSmallIntegerField(
        "Максимум единиц в волне",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="updated_fbs_wave_policies",
        verbose_name="Кем изменено",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "fbs_wave_policy"
        verbose_name = "Настройка волны FBS"
        verbose_name_plural = "Настройки волн FBS"
        ordering = ["profile__agency_id", "profile__marketplace", "profile_id"]
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(max_orders_per_wave__gte=1)
                    & Q(max_orders_per_wave__lte=100)
                ),
                name="fbs_wave_policy_orders_1_100",
            ),
            models.CheckConstraint(
                condition=(
                    Q(max_units_per_wave__gte=1)
                    & Q(max_units_per_wave__lte=100)
                ),
                name="fbs_wave_policy_units_1_100",
            ),
        ]

    def clean(self) -> None:
        super().clean()
        if not 1 <= int(self.max_orders_per_wave or 0) <= 100:
            raise ValidationError(
                {"max_orders_per_wave": "Укажите от 1 до 100 заказов."}
            )
        if not 1 <= int(self.max_units_per_wave or 0) <= 100:
            raise ValidationError(
                {"max_units_per_wave": "Укажите от 1 до 100 единиц товара."}
            )

    def __str__(self) -> str:
        return f"{self.profile}: {self.max_orders_per_wave}/{self.max_units_per_wave}"


class FbsMarketplaceQueuePriority(models.Model):
    """Dated marketplace priority used when a picker claims a free wave."""

    marketplace = models.CharField(
        "Маркетплейс",
        max_length=16,
        choices=FbsIntegrationProfile.MARKETPLACE_CHOICES,
        unique=True,
    )
    priority = models.PositiveSmallIntegerField(
        "Приоритет",
        default=100,
        help_text="Меньшее число означает более высокий приоритет.",
    )
    effective_from = models.DateField("Действует с")
    is_active = models.BooleanField("Включен", default=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="updated_fbs_marketplace_queue_priorities",
        verbose_name="Кем изменено",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "fbs_marketplace_queue_priority"
        verbose_name = "Приоритет маркетплейса в очереди FBS"
        verbose_name_plural = "Приоритеты маркетплейсов в очереди FBS"
        ordering = ["priority", "marketplace"]
        constraints = [
            models.CheckConstraint(
                condition=Q(priority__gte=1) & Q(priority__lte=100),
                name="fbs_marketplace_queue_priority_1_100",
            ),
        ]

    def clean(self) -> None:
        super().clean()
        if not 1 <= int(self.priority or 0) <= 100:
            raise ValidationError({"priority": "Укажите приоритет от 1 до 100."})

    def __str__(self) -> str:
        return f"{self.get_marketplace_display()}: {self.priority} с {self.effective_from}"


class FbsEquipmentCodeSequence(models.Model):
    KIND_WORKSTATION = "workstation"
    KIND_CART = "cart"
    KIND_CHOICES = [
        (KIND_WORKSTATION, "Рабочее место"),
        (KIND_CART, "Тележка"),
    ]

    kind = models.CharField(max_length=16, choices=KIND_CHOICES, primary_key=True)
    last_value = models.PositiveIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "fbs_equipment_code_sequence"
        verbose_name = "Счетчик оборудования FBS"
        verbose_name_plural = "Счетчики оборудования FBS"


class FbsWorkstation(models.Model):
    SHIFT_CLOSED = "closed"
    SHIFT_AVAILABLE = "available"
    SHIFT_PAUSED = "paused"
    SHIFT_STATUS_CHOICES = [
        (SHIFT_CLOSED, "Смена закрыта"),
        (SHIFT_AVAILABLE, "Принимает тележки"),
        (SHIFT_PAUSED, "Пауза"),
    ]

    barcode = models.CharField("Штрихкод", max_length=64, unique=True)
    name = models.CharField("Название", max_length=128)
    device_agent = models.ForeignKey(
        "agent.DeviceAgent",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="fbs_workstations",
        verbose_name="Агент рабочего места",
    )
    printer_name = models.CharField("Принтер этикеток", max_length=255, blank=True)
    active_handover_box = models.OneToOneField(
        "FbsHandoverBox",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="active_workstation",
        verbose_name="Активный короб отгрузки",
    )
    max_parallel_waves = models.PositiveSmallIntegerField(
        "Максимум параллельных волн",
        default=7,
    )
    shift_status = models.CharField(
        "Статус смены контролера",
        max_length=16,
        choices=SHIFT_STATUS_CHOICES,
        default=SHIFT_CLOSED,
    )
    shift_controller = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="fbs_controller_workstations",
        verbose_name="Контролер на смене",
    )
    shift_heartbeat_at = models.DateTimeField(
        "Последняя активность контролера",
        null=True,
        blank=True,
    )
    is_active = models.BooleanField("Активно", default=True)
    notes = models.TextField("Примечание", blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_fbs_workstations",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="updated_fbs_workstations",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "fbs_workstation"
        verbose_name = "Рабочее место FBS"
        verbose_name_plural = "Рабочие места FBS"
        ordering = ["name", "id"]
        constraints = [
            models.CheckConstraint(
                condition=Q(max_parallel_waves__gte=1) & Q(max_parallel_waves__lte=7),
                name="fbs_workstation_parallel_waves_1_7",
            )
        ]
        indexes = [
            models.Index(fields=["is_active", "name"]),
            models.Index(fields=["device_agent", "is_active"]),
            models.Index(
                fields=["shift_status", "shift_heartbeat_at"],
                name="fbs_ws_shift_heartbeat_idx",
            ),
        ]

    def clean(self) -> None:
        super().clean()
        self.barcode = str(self.barcode or "").strip().upper()
        self.name = str(self.name or "").strip()
        self.printer_name = str(self.printer_name or "").strip()
        self.notes = str(self.notes or "").strip()
        if not re.fullmatch(r"FBS-WS-\d{6}", self.barcode):
            raise ValidationError(
                {"barcode": "Штрихкод рабочего места должен иметь формат FBS-WS-000001."}
            )
        if not self.name:
            raise ValidationError({"name": "Укажите название рабочего места."})
        if not 1 <= int(self.max_parallel_waves or 0) <= 7:
            raise ValidationError(
                {"max_parallel_waves": "На рабочем месте разрешено от одной до семи тележек."}
            )

    def __str__(self) -> str:
        return f"{self.name} ({self.barcode})"


class FbsPickingCart(models.Model):
    barcode = models.CharField("Штрихкод", max_length=64, unique=True)
    name = models.CharField("Название", max_length=128)
    owner_agency = models.ForeignKey(
        Agency,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="owned_fbs_picking_carts",
        verbose_name="Поставщик-владелец",
    )
    is_active = models.BooleanField("Активна", default=True)
    notes = models.TextField("Примечание", blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_fbs_picking_carts",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="updated_fbs_picking_carts",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "fbs_picking_cart"
        verbose_name = "Тара FBS"
        verbose_name_plural = "Тара FBS"
        ordering = ["name", "id"]
        indexes = [models.Index(fields=["is_active", "name"])]

    def clean(self) -> None:
        super().clean()
        self.barcode = str(self.barcode or "").strip().upper()
        self.name = str(self.name or "").strip()
        self.notes = str(self.notes or "").strip()
        if not re.fullmatch(r"FBS-CART-\d{6}", self.barcode):
            raise ValidationError(
                {"barcode": "Штрихкод тележки должен иметь формат FBS-CART-000001."}
            )
        if not self.name:
            raise ValidationError({"name": "Укажите название тележки."})

    def __str__(self) -> str:
        return f"{self.name} ({self.barcode})"


class FbsToteZone(models.Model):
    KIND_FREE = "free"
    KIND_CONTROLLER = "controller"
    KIND_READY = "ready"
    KIND_TRANSIT = "transit"
    KIND_CHOICES = [
        (KIND_FREE, "Зона свободной тары"),
        (KIND_CONTROLLER, "Зона контролера"),
        (KIND_READY, "Зона готового товара"),
        (KIND_TRANSIT, "Транзитная зона"),
    ]

    barcode = models.CharField("QR зоны", max_length=64, unique=True)
    name = models.CharField("Название", max_length=128)
    kind = models.CharField(max_length=16, choices=KIND_CHOICES)
    is_active = models.BooleanField(default=True)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "fbs_tote_zone"
        ordering = ["kind", "name", "id"]
        indexes = [models.Index(fields=["kind", "is_active"])]

    def clean(self) -> None:
        super().clean()
        self.barcode = str(self.barcode or "").strip().upper()
        self.name = str(self.name or "").strip()
        self.notes = str(self.notes or "").strip()
        if not self.barcode:
            raise ValidationError({"barcode": "Укажите QR зоны тары."})
        if not self.name:
            raise ValidationError({"name": "Укажите название зоны тары."})

    def __str__(self) -> str:
        return f"{self.name} ({self.barcode})"


class FbsToteBinding(models.Model):
    STATE_UNBOUND = "unbound"
    STATE_FREE = "free"
    STATE_PICKING = "picking"
    STATE_WAITING_CONTROL = "waiting_control"
    STATE_AT_CONTROL = "at_control"
    STATE_CHECKING = "checking"
    STATE_UNKNOWN = "unknown"
    STATE_READY = "ready"
    STATE_TRANSIT = "transit"
    STATE_CHOICES = [
        (STATE_UNBOUND, "Не привязана"),
        (STATE_FREE, "Свободна"),
        (STATE_PICKING, "У сборщика"),
        (STATE_WAITING_CONTROL, "Ожидает контролера"),
        (STATE_AT_CONTROL, "У контролера"),
        (STATE_CHECKING, "Тара на проверку"),
        (STATE_UNKNOWN, "Неизвестный товар"),
        (STATE_READY, "Готовый товар"),
        (STATE_TRANSIT, "В перемещении"),
    ]

    tote = models.OneToOneField(
        FbsPickingCart,
        on_delete=models.PROTECT,
        related_name="binding",
    )
    state = models.CharField(max_length=24, choices=STATE_CHOICES, default=STATE_UNBOUND)
    zone = models.ForeignKey(
        FbsToteZone,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="bound_totes",
    )
    workstation = models.ForeignKey(
        FbsWorkstation,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="bound_totes",
    )
    employee = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="bound_fbs_totes",
    )
    pick_batch = models.ForeignKey(
        "FbsPickBatch",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="tote_bindings",
    )
    controller_session = models.ForeignKey(
        "FbsControllerSession",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="tote_bindings",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="updated_fbs_tote_bindings",
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "fbs_tote_binding"
        indexes = [
            models.Index(fields=["state", "updated_at"]),
            models.Index(fields=["zone", "state"]),
            models.Index(fields=["workstation", "state"]),
            models.Index(fields=["employee", "state"]),
        ]

    def clean(self) -> None:
        super().clean()
        destination_count = sum(
            value is not None for value in (self.zone_id, self.workstation_id, self.employee_id)
        )
        if self.state == self.STATE_UNBOUND:
            if destination_count:
                raise ValidationError("Непривязанная тара не может иметь текущее место.")
        elif destination_count != 1:
            raise ValidationError(
                "Тара должна быть привязана ровно к одной зоне, рабочему столу или сотруднику."
            )


class FbsControllerSession(models.Model):
    STATUS_ACTIVE = "active"
    STATUS_CLOSED = "closed"
    STATUS_CHOICES = [
        (STATUS_ACTIVE, "Активна"),
        (STATUS_CLOSED, "Закрыта"),
    ]

    workstation = models.ForeignKey(
        FbsWorkstation,
        on_delete=models.PROTECT,
        related_name="controller_tote_sessions",
    )
    controller = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="fbs_controller_tote_sessions",
    )
    unknown_tote = models.ForeignKey(
        FbsPickingCart,
        on_delete=models.PROTECT,
        related_name="unknown_controller_sessions",
    )
    problem_tote = models.ForeignKey(
        FbsPickingCart,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="problem_controller_sessions",
    )
    canceled_tote = models.ForeignKey(
        FbsPickingCart,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="canceled_controller_sessions",
    )
    free_zone = models.ForeignKey(
        FbsToteZone,
        on_delete=models.PROTECT,
        related_name="controller_sessions",
    )
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_ACTIVE)
    started_at = models.DateTimeField(auto_now_add=True)
    closed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "fbs_controller_session"
        ordering = ["-started_at", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["workstation"],
                condition=Q(status="active"),
                name="uniq_active_fbs_tote_session_workstation",
            ),
        ]


class FbsControllerCheckTote(models.Model):
    STATUS_OPEN = "open"
    STATUS_WAITING_KIZ = "waiting_kiz"
    STATUS_READY = "ready"
    STATUS_COMPOSITION = "composition"
    STATUS_CLOSED = "closed"
    STATUS_PROBLEM = "problem"
    STATUS_CHOICES = [
        (STATUS_OPEN, "Наполняется"),
        (STATUS_WAITING_KIZ, "Ожидает КИЗ"),
        (STATUS_READY, "Готова к проверке состава"),
        (STATUS_COMPOSITION, "Проверка состава"),
        (STATUS_CLOSED, "Закрыта"),
        (STATUS_PROBLEM, "Проблема"),
    ]

    session = models.ForeignKey(
        FbsControllerSession,
        on_delete=models.PROTECT,
        related_name="check_totes",
    )
    tote = models.ForeignKey(
        FbsPickingCart,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="controller_check_uses",
    )
    agency = models.ForeignKey(
        "sku.Agency",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="fbs_controller_check_totes",
    )
    profile = models.ForeignKey(
        "FbsIntegrationProfile",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="controller_check_totes",
    )
    handover_batch = models.OneToOneField(
        "FbsHandoverBatch",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="controller_check_tote",
    )
    status = models.CharField(max_length=24, choices=STATUS_CHOICES, default=STATUS_OPEN)
    item_qty = models.PositiveIntegerField(default=0)
    labeled_qty = models.PositiveIntegerField(default=0)
    composition_qty = models.PositiveIntegerField(default=0)
    opened_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="opened_fbs_check_totes",
    )
    closed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="closed_fbs_check_totes",
    )
    opened_at = models.DateTimeField(auto_now_add=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "fbs_controller_check_tote"
        ordering = ["opened_at", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["tote"],
                condition=Q(
                    status__in=("open", "waiting_kiz", "ready", "composition")
                ),
                name="uniq_active_fbs_controller_check_tote",
            )
        ]
        indexes = [
            models.Index(fields=["session", "status"]),
            models.Index(fields=["agency", "status"]),
        ]


class FbsControllerPickTote(models.Model):
    STATUS_PROCESSING = "processing"
    STATUS_AWAITING_EMPTY = "awaiting_empty"
    STATUS_CLOSED = "closed"
    STATUS_PROBLEM = "problem"
    STATUS_CHOICES = [
        (STATUS_PROCESSING, "Обрабатывается"),
        (STATUS_AWAITING_EMPTY, "Подтвердите пустоту"),
        (STATUS_CLOSED, "Освобождена"),
        (STATUS_PROBLEM, "Проблема"),
    ]

    session = models.ForeignKey(
        FbsControllerSession,
        on_delete=models.PROTECT,
        related_name="pick_totes",
    )
    check_tote = models.ForeignKey(
        FbsControllerCheckTote,
        on_delete=models.PROTECT,
        related_name="pick_totes",
    )
    pick_batch = models.OneToOneField(
        "FbsPickBatch",
        on_delete=models.PROTECT,
        related_name="controller_pick_tote",
    )
    tote = models.ForeignKey(
        FbsPickingCart,
        on_delete=models.PROTECT,
        related_name="controller_pick_uses",
    )
    status = models.CharField(max_length=24, choices=STATUS_CHOICES, default=STATUS_PROCESSING)
    planned_qty = models.PositiveIntegerField(default=0)
    processed_qty = models.PositiveIntegerField(default=0)
    unknown_qty = models.PositiveIntegerField(default=0)
    attached_at = models.DateTimeField(auto_now_add=True)
    empty_confirmed_at = models.DateTimeField(null=True, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "fbs_controller_pick_tote"
        ordering = ["attached_at", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["tote"],
                condition=Q(status__in=("processing", "awaiting_empty")),
                name="uniq_active_fbs_controller_pick_tote",
            )
        ]
        indexes = [
            models.Index(fields=["session", "status"]),
            models.Index(fields=["check_tote", "status"]),
        ]


class FbsControllerToteOrder(models.Model):
    STATUS_LABELED = "labeled"
    STATUS_COMPOSITION = "composition"
    STATUS_PACKED = "packed"
    STATUS_REMOVED = "removed"
    STATUS_CHOICES = [
        (STATUS_LABELED, "В таре проверки"),
        (STATUS_COMPOSITION, "Состав подтвержден"),
        (STATUS_PACKED, "В транспортном коробе"),
        (STATUS_REMOVED, "Исключен"),
    ]

    check_tote = models.ForeignKey(
        FbsControllerCheckTote,
        on_delete=models.PROTECT,
        related_name="orders",
    )
    pick_tote = models.ForeignKey(
        FbsControllerPickTote,
        on_delete=models.PROTECT,
        related_name="orders",
    )
    order = models.ForeignKey(
        "FbsOrder",
        on_delete=models.PROTECT,
        related_name="controller_tote_orders",
    )
    label = models.ForeignKey(
        "FbsOrderLabel",
        on_delete=models.PROTECT,
        related_name="controller_tote_orders",
    )
    transport_box = models.ForeignKey(
        "FbsHandoverBox",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="controller_tote_orders",
    )
    status = models.CharField(max_length=24, choices=STATUS_CHOICES, default=STATUS_LABELED)
    units = models.PositiveIntegerField(default=1)
    label_confirmed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="confirmed_fbs_tote_order_labels",
    )
    label_confirmed_at = models.DateTimeField(auto_now_add=True)
    composition_checked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="checked_fbs_tote_order_compositions",
    )
    composition_checked_at = models.DateTimeField(null=True, blank=True)
    primary_order_label_scan_reused = models.BooleanField(
        "Первичный скан этикетки использован для проверки состава",
        default=False,
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "fbs_controller_tote_order"
        ordering = ["label_confirmed_at", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["order"],
                condition=Q(status__in=("labeled", "composition", "packed")),
                name="uniq_active_fbs_controller_tote_order",
            )
        ]
        indexes = [
            models.Index(fields=["check_tote", "status"]),
            models.Index(fields=["pick_tote", "status"]),
        ]


class FbsUnknownToteItem(models.Model):
    STATUS_WAITING = "waiting"
    STATUS_PLACED = "placed"
    STATUS_CHOICES = [
        (STATUS_WAITING, "Ожидает размещения"),
        (STATUS_PLACED, "Размещен"),
    ]

    session = models.ForeignKey(
        FbsControllerSession,
        on_delete=models.PROTECT,
        related_name="unknown_items",
    )
    unknown_tote = models.ForeignKey(
        FbsPickingCart,
        on_delete=models.PROTECT,
        related_name="unknown_items",
    )
    source_pick_tote = models.ForeignKey(
        FbsControllerPickTote,
        on_delete=models.PROTECT,
        related_name="unknown_items",
    )
    scanned_value = models.CharField(max_length=255)
    comment = models.TextField(blank=True)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_WAITING)
    reported_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="reported_fbs_unknown_tote_items",
    )
    placed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="placed_fbs_unknown_tote_items",
    )
    reported_at = models.DateTimeField(auto_now_add=True)
    placed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "fbs_unknown_tote_item"
        ordering = ["reported_at", "id"]
        indexes = [models.Index(fields=["status", "reported_at"])]


class FbsProblemToteItem(models.Model):
    SEVERITY_NONCRITICAL = "noncritical"
    SEVERITY_CRITICAL = "critical"
    SEVERITY_CHOICES = [
        (SEVERITY_NONCRITICAL, "Некритическая проблема"),
        (SEVERITY_CRITICAL, "Критическая проблема"),
    ]

    STATUS_IN_TOTE = "in_tote"
    STATUS_RETURNED = "returned"
    STATUS_CHOICES = [
        (STATUS_IN_TOTE, "В проблемной таре"),
        (STATUS_RETURNED, "Возвращен в тару проверки"),
    ]

    session = models.ForeignKey(
        FbsControllerSession,
        on_delete=models.PROTECT,
        related_name="problem_items",
    )
    problem_tote = models.ForeignKey(
        FbsPickingCart,
        on_delete=models.PROTECT,
        related_name="problem_items",
    )
    source_pick_tote = models.ForeignKey(
        FbsControllerPickTote,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="problem_items",
    )
    source_check_tote = models.ForeignKey(
        FbsControllerCheckTote,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="problem_items",
    )
    order = models.ForeignKey(
        "FbsOrder",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="problem_tote_items",
    )
    order_item = models.ForeignKey(
        "FbsOrderItem",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="problem_tote_items",
    )
    scanned_value = models.CharField(max_length=512)
    reason = models.TextField()
    severity = models.CharField(
        max_length=16,
        choices=SEVERITY_CHOICES,
        default=SEVERITY_NONCRITICAL,
    )
    quantity = models.PositiveIntegerField(default=1)
    status = models.CharField(
        max_length=16,
        choices=STATUS_CHOICES,
        default=STATUS_IN_TOTE,
    )
    reported_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="reported_fbs_problem_tote_items",
    )
    resolved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="resolved_fbs_problem_tote_items",
    )
    reported_at = models.DateTimeField(auto_now_add=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "fbs_problem_tote_item"
        ordering = ["reported_at", "id"]
        constraints = [
            models.CheckConstraint(
                condition=Q(quantity__gt=0),
                name="fbs_problem_tote_item_qty_gt_zero",
            )
        ]
        indexes = [
            models.Index(
                fields=["problem_tote", "status", "severity"],
                name="fbs_problem_tote_status_idx",
            ),
            models.Index(
                fields=["scanned_value", "status"],
                name="fbs_problem_item_scan_idx",
            ),
            models.Index(
                fields=["order_item", "status"],
                name="fbs_problem_item_order_idx",
            ),
        ]


class FbsToteMovement(models.Model):
    ACTION_ASSIGN = "assign"
    ACTION_TAKE = "take"
    ACTION_PLACE = "place"
    ACTION_HANDOVER = "handover"
    ACTION_CHECK = "check"
    ACTION_RELEASE = "release"
    ACTION_UNKNOWN = "unknown"
    ACTION_CLOSE = "close"
    ACTION_CHOICES = [
        (ACTION_ASSIGN, "Привязка"),
        (ACTION_TAKE, "Взята"),
        (ACTION_PLACE, "Оставлена"),
        (ACTION_HANDOVER, "Передана"),
        (ACTION_CHECK, "Проверка"),
        (ACTION_RELEASE, "Освобождена"),
        (ACTION_UNKNOWN, "Неизвестный товар"),
        (ACTION_CLOSE, "Закрытие"),
    ]

    tote = models.ForeignKey(
        FbsPickingCart,
        on_delete=models.PROTECT,
        related_name="movement_events",
    )
    action = models.CharField(max_length=16, choices=ACTION_CHOICES)
    source_kind = models.CharField(max_length=16, blank=True)
    source_code = models.CharField(max_length=128, blank=True)
    target_kind = models.CharField(max_length=16, blank=True)
    target_code = models.CharField(max_length=128, blank=True)
    pick_batch = models.ForeignKey(
        "FbsPickBatch",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="tote_movements",
    )
    controller_session = models.ForeignKey(
        FbsControllerSession,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="movements",
    )
    handover_batch = models.ForeignKey(
        "FbsHandoverBatch",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="tote_movements",
    )
    quantity = models.PositiveIntegerField(default=0)
    details = models.JSONField(default=dict, blank=True)
    performed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="performed_fbs_tote_movements",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "fbs_tote_movement"
        ordering = ["created_at", "id"]
        indexes = [
            models.Index(fields=["tote", "created_at"]),
            models.Index(fields=["action", "created_at"]),
        ]

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationError("Событие движения тары нельзя изменять.")
        return super().save(*args, **kwargs)


class FbsSyncCursor(models.Model):
    STREAM_ORDERS = "orders"
    STREAM_STATUSES = "statuses"
    STREAM_STOCKS = "stocks"
    STREAM_MARKING = "marking"
    STREAM_CHOICES = [
        (STREAM_ORDERS, "Заказы"),
        (STREAM_STATUSES, "Статусы"),
        (STREAM_STOCKS, "Остатки"),
        (STREAM_MARKING, "Маркировка"),
    ]

    profile = models.ForeignKey(
        FbsIntegrationProfile,
        on_delete=models.CASCADE,
        related_name="sync_cursors",
    )
    stream = models.CharField(max_length=32, choices=STREAM_CHOICES)
    cursor_key = models.CharField(max_length=64, default="default")
    cursor = models.JSONField(default=dict, blank=True)
    last_polled_at = models.DateTimeField(null=True, blank=True)
    last_success_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True)
    lease_token = models.CharField(max_length=64, blank=True)
    lease_expires_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "fbs_sync_cursor"
        constraints = [
            models.UniqueConstraint(
                fields=["profile", "stream", "cursor_key"],
                name="uniq_fbs_sync_cursor",
            )
        ]
        indexes = [models.Index(fields=["profile", "stream"])]


class FbsMarketplaceEvent(models.Model):
    STATUS_RECEIVED = "received"
    STATUS_PROCESSED = "processed"
    STATUS_RETRY = "retry"
    STATUS_FAILED = "failed"
    STATUS_DEAD = "dead"
    STATUS_CHOICES = [
        (STATUS_RECEIVED, "Получено"),
        (STATUS_PROCESSED, "Обработано"),
        (STATUS_RETRY, "Повтор"),
        (STATUS_FAILED, "Ошибка"),
        (STATUS_DEAD, "Остановлено"),
    ]

    profile = models.ForeignKey(
        FbsIntegrationProfile,
        on_delete=models.CASCADE,
        related_name="marketplace_events",
    )
    event_type = models.CharField(max_length=64)
    external_id = models.CharField(max_length=128, blank=True)
    payload = models.JSONField(default=dict)
    payload_hash = models.CharField(max_length=64)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_RECEIVED)
    attempt_count = models.PositiveIntegerField(default=0)
    error = models.TextField(blank=True)
    received_at = models.DateTimeField(auto_now_add=True)
    processed_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "fbs_marketplace_event"
        ordering = ["received_at", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["profile", "event_type", "external_id", "payload_hash"],
                name="uniq_fbs_marketplace_event_payload",
            )
        ]
        indexes = [
            models.Index(fields=["status", "received_at"]),
            models.Index(fields=["profile", "event_type", "external_id"]),
        ]


class FbsOrder(models.Model):
    STATUS_RECEIVED = "received"
    STATUS_VALIDATION_FAILED = "validation_failed"
    STATUS_AWAITING_STOCK = "awaiting_stock"
    STATUS_RESERVED = "reserved"
    STATUS_QUEUED_FOR_PICK = "queued_for_pick"
    STATUS_PICKING = "picking"
    STATUS_PICKED = "picked"
    STATUS_READY_FOR_HANDOVER = "ready_for_handover"
    STATUS_HANDED_OVER = "handed_over"
    STATUS_DELIVERED = "delivered"
    STATUS_CANCELLED = "cancelled"
    STATUS_RETURN_PENDING = "return_pending"
    STATUS_RETURNED = "returned"
    STATUS_EXCEPTION = "exception"
    STATUS_CHOICES = [
        (STATUS_RECEIVED, "Получен"),
        (STATUS_VALIDATION_FAILED, "Ошибка проверки"),
        (STATUS_AWAITING_STOCK, "Ожидает товар"),
        (STATUS_RESERVED, "Зарезервирован"),
        (STATUS_QUEUED_FOR_PICK, "В очереди сборки"),
        (STATUS_PICKING, "Собирается"),
        (STATUS_PICKED, "Отобран"),
        (STATUS_READY_FOR_HANDOVER, "Готов к передаче"),
        (STATUS_HANDED_OVER, "Передан"),
        (STATUS_DELIVERED, "Доставлен"),
        (STATUS_CANCELLED, "Отменен"),
        (STATUS_RETURN_PENDING, "Ожидается возврат"),
        (STATUS_RETURNED, "Возвращен"),
        (STATUS_EXCEPTION, "Исключение"),
    ]

    profile = models.ForeignKey(
        FbsIntegrationProfile,
        on_delete=models.PROTECT,
        related_name="orders",
    )
    external_order_id = models.CharField(max_length=128)
    internal_status = models.CharField(max_length=32, choices=STATUS_CHOICES, default=STATUS_RECEIVED)
    marketplace_status = models.CharField(max_length=64, blank=True)
    marketplace_substatus = models.CharField(max_length=64, blank=True)
    hold_reason = models.CharField(max_length=64, blank=True)
    problem_reason = models.TextField(blank=True)
    raw_payload = models.JSONField(default=dict, blank=True)
    ordered_at = models.DateTimeField(null=True, blank=True)
    cutoff_at = models.DateTimeField(null=True, blank=True)
    imported_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "fbs_order"
        ordering = ["cutoff_at", "imported_at", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["profile", "external_order_id"],
                name="uniq_fbs_external_order",
            )
        ]
        indexes = [
            models.Index(fields=["profile", "internal_status", "cutoff_at"]),
            models.Index(fields=["profile", "marketplace_status"]),
        ]

    def __str__(self) -> str:
        return f"{self.profile_id}:{self.external_order_id}"


class FbsOrderItem(models.Model):
    order = models.ForeignKey(FbsOrder, on_delete=models.CASCADE, related_name="items")
    external_line_id = models.CharField(max_length=128)
    external_sku = models.CharField(max_length=128)
    barcode = models.CharField(max_length=64, blank=True)
    sku = models.ForeignKey(
        SKU,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="fbs_order_items",
    )
    product_name = models.CharField(max_length=255, blank=True)
    quantity = models.PositiveIntegerField(default=1)
    requirements = models.JSONField(default=dict, blank=True)
    raw_payload = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "fbs_order_item"
        ordering = ["order_id", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["order", "external_line_id"],
                name="uniq_fbs_order_line",
            ),
            models.CheckConstraint(condition=Q(quantity__gt=0), name="fbs_order_item_qty_gt_zero"),
        ]
        indexes = [
            models.Index(fields=["order", "external_sku"]),
            models.Index(fields=["order", "barcode"]),
            models.Index(fields=["sku"]),
        ]


class FbsMarketplaceCommand(models.Model):
    SOURCE_AUTOMATIC = "automatic"
    SOURCE_METADATA_CONTROL = "metadata_control"
    SOURCE_CHOICES = [
        (SOURCE_AUTOMATIC, "Автоматически"),
        (SOURCE_METADATA_CONTROL, "Экран контроля передач"),
    ]

    METHOD_GET = "GET"
    METHOD_POST = "POST"
    METHOD_PATCH = "PATCH"
    METHOD_PUT = "PUT"
    METHOD_CHOICES = [
        (METHOD_GET, "GET"),
        (METHOD_POST, "POST"),
        (METHOD_PATCH, "PATCH"),
        (METHOD_PUT, "PUT"),
    ]

    STATUS_PENDING = "pending"
    STATUS_SENT = "sent"
    STATUS_CONFIRMED = "confirmed"
    STATUS_RETRY = "retry"
    STATUS_FAILED = "failed"
    STATUS_CONFLICT = "conflict"
    STATUS_CANCELLED = "cancelled"
    STATUS_CHOICES = [
        (STATUS_PENDING, "Ожидает"),
        (STATUS_SENT, "Отправлено"),
        (STATUS_CONFIRMED, "Подтверждено"),
        (STATUS_RETRY, "Повтор"),
        (STATUS_FAILED, "Ошибка"),
        (STATUS_CONFLICT, "Конфликт"),
        (STATUS_CANCELLED, "Отменено"),
    ]

    profile = models.ForeignKey(
        FbsIntegrationProfile,
        on_delete=models.CASCADE,
        related_name="marketplace_commands",
    )
    order = models.ForeignKey(
        FbsOrder,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="marketplace_commands",
    )
    handover_batch = models.ForeignKey(
        "FbsHandoverBatch",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="marketplace_commands",
    )
    handover_box = models.ForeignKey(
        "FbsHandoverBox",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="marketplace_commands",
    )
    command_type = models.CharField(max_length=64)
    http_method = models.CharField(max_length=8, choices=METHOD_CHOICES, default=METHOD_POST)
    endpoint = models.CharField(max_length=255)
    endpoint_version = models.CharField(max_length=32, blank=True)
    idempotency_key = models.CharField(max_length=128)
    payload = models.JSONField(default=dict)
    payload_hash = models.CharField(max_length=64)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_PENDING)
    attempt_count = models.PositiveIntegerField(default=0)
    next_attempt_at = models.DateTimeField(null=True, blank=True)
    http_status = models.PositiveIntegerField(null=True, blank=True)
    response_payload = models.JSONField(default=dict, blank=True)
    error = models.TextField(blank=True)
    request_source = models.CharField(
        max_length=32,
        choices=SOURCE_CHOICES,
        default=SOURCE_AUTOMATIC,
    )
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="requested_fbs_marketplace_commands",
    )
    confirmed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "fbs_marketplace_command"
        ordering = ["created_at", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["profile", "idempotency_key"],
                name="uniq_fbs_command_idempotency",
            )
        ]
        indexes = [
            models.Index(fields=["status", "next_attempt_at"]),
            models.Index(fields=["profile", "command_type", "status"]),
        ]


class FbsStorageCell(models.Model):
    PURPOSE_PICK = "pick"
    PURPOSE_RESERVE = "reserve"
    PURPOSE_FLEX = "flex"
    PURPOSE_CHOICES = [
        (PURPOSE_PICK, "Нижний ярус отбора"),
        (PURPOSE_RESERVE, "Верхний ярус резерва"),
        (PURPOSE_FLEX, "Универсальная"),
    ]

    cell_code = models.CharField(max_length=64, unique=True)
    location = models.OneToOneField(
        "sklad.WarehouseLocation",
        on_delete=models.PROTECT,
        related_name="fbs_storage_cell",
    )
    purpose = models.CharField(max_length=16, choices=PURPOSE_CHOICES, default=PURPOSE_FLEX)
    client_cluster = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "fbs_storage_cell"
        ordering = ["cell_code", "id"]

    @property
    def warehouse_location_code(self) -> str:
        location = self.location
        coordinates = (
            int(location.row_no or 0),
            int(location.section_no or 0),
            int(location.tier_no or 0),
            int(location.cell_no or 0),
        )
        if str(location.zone_code or "").strip().upper() == "OS" and all(
            value > 0 for value in coordinates
        ):
            from sklad.topology import os_location_code

            return os_location_code(
                row=coordinates[0],
                section=coordinates[1],
                tier=coordinates[2],
                cell=coordinates[3],
            )
        return str(location.location_code or self.cell_code)

    @property
    def warehouse_location_label(self) -> str:
        location = self.location
        coordinates = (
            int(location.row_no or 0),
            int(location.section_no or 0),
            int(location.tier_no or 0),
            int(location.cell_no or 0),
        )
        if str(location.zone_code or "").strip().upper() == "OS" and all(
            value > 0 for value in coordinates
        ):
            from sklad.topology import os_location_label

            return os_location_label(
                row=coordinates[0],
                section=coordinates[1],
                tier=coordinates[2],
                cell=coordinates[3],
            )
        return str(location.display_name or location.location_code or self.cell_code)

    def clean(self) -> None:
        super().clean()
        if not self.location_id:
            return
        expected_zone = str(getattr(settings, "FBS_ZONE_CODE", "FBS") or "FBS").strip().upper()
        allowed_zones = {expected_zone, "OS"}
        if (
            str(self.location.zone_code or "").strip().upper() == "PR"
            and self.location.is_fbs_visible
            and not self.location.is_topology_visible
        ):
            allowed_zones.add("PR")
        if str(self.location.zone_code or "").strip().upper() not in allowed_zones:
            raise ValidationError(
                {
                    "location": (
                        f"FBS-ячейка должна находиться в общей зоне OS "
                        f"или в совместимой зоне {expected_zone}."
                    )
                }
            )

    def __str__(self) -> str:
        return self.cell_code


class FbsRack(models.Model):
    location = models.OneToOneField(
        "sklad.WarehouseLocation",
        on_delete=models.PROTECT,
        related_name="fbs_rack",
    )
    is_active = models.BooleanField(default=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_fbs_racks",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "fbs_rack"
        ordering = ["location__location_code", "id"]

    def clean(self) -> None:
        super().clean()
        if not self.location_id:
            return
        if str(self.location.zone_code or "").strip().upper() != "PR":
            raise ValidationError({"location": "FBS-стеллаж можно создать только в зоне PR."})
        if self.location.is_topology_visible or not self.location.is_fbs_visible:
            raise ValidationError(
                {"location": "FBS-стеллаж должен быть дополнительным местом PR, видимым в FBS."}
            )

    @property
    def rack_code(self) -> str:
        return str(self.location.location_code or "").strip()

    def __str__(self) -> str:
        return self.rack_code or f"FBS rack {self.pk}"


class FbsRackCell(models.Model):
    rack = models.ForeignKey(FbsRack, on_delete=models.PROTECT, related_name="cells")
    storage_cell = models.OneToOneField(
        FbsStorageCell,
        on_delete=models.PROTECT,
        related_name="rack_cell",
    )
    position = models.PositiveSmallIntegerField()
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "fbs_rack_cell"
        ordering = ["rack_id", "position", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["rack", "position"],
                name="uniq_fbs_rack_cell_position",
            ),
            models.CheckConstraint(
                condition=Q(position__gt=0),
                name="fbs_rack_cell_position_gt_zero",
            ),
        ]
        indexes = [
            models.Index(
                fields=["rack", "is_active", "position"],
                name="fbs_rack_act_pos_idx",
            )
        ]

    @property
    def location(self):
        return self.storage_cell.location

    @property
    def cell_code(self) -> str:
        return str(self.storage_cell.location.location_code or self.storage_cell.cell_code)

    def clean(self) -> None:
        super().clean()
        if self.storage_cell_id and str(self.storage_cell.location.zone_code or "").strip().upper() != "PR":
            raise ValidationError({"storage_cell": "Ячейка FBS-стеллажа должна находиться в PR."})

    def __str__(self) -> str:
        return self.cell_code


class FbsPallet(models.Model):
    STATUS_PLANNED = "planned"
    STATUS_ACTIVE = "active"
    STATUS_ARCHIVED = "archived"
    STATUS_CHOICES = [
        (STATUS_PLANNED, "Запланирована"),
        (STATUS_ACTIVE, "Активна"),
        (STATUS_ARCHIVED, "Архив"),
    ]

    agency = models.ForeignKey(Agency, on_delete=models.PROTECT, related_name="fbs_pallets")
    pallet_code = models.CharField(max_length=128)
    cell = models.ForeignKey(
        FbsStorageCell,
        on_delete=models.PROTECT,
        related_name="pallets",
    )
    warehouse_container = models.OneToOneField(
        "sklad.WarehouseContainer",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="fbs_pallet",
    )
    max_boxes = models.PositiveSmallIntegerField(default=10)
    is_rack_binding = models.BooleanField(default=False)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_PLANNED)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "fbs_pallet"
        ordering = ["cell__cell_code", "pallet_code", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["agency", "pallet_code"],
                name="uniq_fbs_pallet_code_per_agency",
            ),
            models.UniqueConstraint(
                fields=["cell", "agency"],
                condition=Q(
                    status__in=["planned", "active"],
                    is_rack_binding=True,
                ),
                name="uniq_active_fbs_rack_pallet_cell_agency",
            ),
            models.CheckConstraint(condition=Q(max_boxes__gt=0), name="fbs_pallet_max_boxes_gt_zero"),
        ]
        indexes = [
            models.Index(fields=["agency", "status"]),
            models.Index(fields=["cell", "status"]),
        ]

    def clean(self) -> None:
        super().clean()
        if self.warehouse_container_id and self.warehouse_container.agency_id != self.agency_id:
            raise ValidationError(
                {"warehouse_container": "Паллета склада и FBS должны принадлежать одному клиенту."}
            )
        if self.cell_id and self.status in {self.STATUS_PLANNED, self.STATUS_ACTIVE}:
            is_rack_cell = FbsRackCell.objects.filter(
                storage_cell_id=self.cell_id,
                is_active=True,
            ).exists()
            if self.is_rack_binding and not is_rack_cell:
                raise ValidationError(
                    {"cell": "Системную паллету PR-стеллажа нельзя создать в обычной FBS-ячейке."}
                )
            if not self.is_rack_binding and is_rack_cell:
                raise ValidationError(
                    {"cell": "В ячейке PR-стеллажа разрешены только системные клиентские паллеты."}
                )
            has_other_client = FbsPallet.objects.filter(
                cell_id=self.cell_id,
                status__in=(self.STATUS_PLANNED, self.STATUS_ACTIVE),
            ).exclude(pk=self.pk).exclude(agency_id=self.agency_id).exists()
            allows_mixed_clients = bool(
                self.cell.location.allow_mixed_client_pallets
            )
            if has_other_client and not is_rack_cell and not allows_mixed_clients:
                raise ValidationError(
                    {
                        "cell": (
                            "Для этого места не разрешены FBS-паллеты разных клиентов."
                        )
                    }
                )

    def __str__(self) -> str:
        return f"{self.pallet_code} / {self.agency}"


class FbsBox(models.Model):
    STATUS_PLANNED = "planned"
    STATUS_ACTIVE = "active"
    STATUS_ARCHIVED = "archived"
    STATUS_CHOICES = [
        (STATUS_PLANNED, "Запланирован"),
        (STATUS_ACTIVE, "Активен"),
        (STATUS_ARCHIVED, "Архив"),
    ]

    agency = models.ForeignKey(Agency, on_delete=models.PROTECT, related_name="fbs_boxes")
    pallet = models.ForeignKey(FbsPallet, on_delete=models.PROTECT, related_name="boxes")
    box_code = models.CharField(max_length=128)
    source_container = models.OneToOneField(
        "sklad.WarehouseContainer",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="fbs_box",
    )
    width_mm = models.PositiveIntegerField(null=True, blank=True)
    height_mm = models.PositiveIntegerField(null=True, blank=True)
    depth_mm = models.PositiveIntegerField(null=True, blank=True)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_PLANNED)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "fbs_box"
        ordering = ["pallet_id", "box_code", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["agency", "box_code"],
                name="uniq_fbs_box_code_per_agency",
            )
        ]
        indexes = [
            models.Index(fields=["pallet", "status"]),
            models.Index(fields=["agency", "status"]),
        ]

    def clean(self) -> None:
        super().clean()
        if self.pallet_id and self.pallet.agency_id != self.agency_id:
            raise ValidationError({"pallet": "В одной FBS-паллете разрешен только один клиент."})
        if self.source_container_id and self.source_container.agency_id != self.agency_id:
            raise ValidationError(
                {"source_container": "Исходный короб и FBS-короб должны принадлежать одному клиенту."}
            )

    def __str__(self) -> str:
        return f"{self.box_code} / {self.pallet.pallet_code}"


class FbsStockBalance(models.Model):
    agency = models.ForeignKey(Agency, on_delete=models.PROTECT, related_name="fbs_stock_balances")
    box = models.ForeignKey(FbsBox, on_delete=models.PROTECT, related_name="stock_balances")
    sku_ref = models.ForeignKey(
        SKU,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="fbs_stock_balances",
    )
    identity_key = models.CharField(max_length=64)
    sku_code = models.CharField(max_length=64)
    name = models.CharField(max_length=255, blank=True)
    size = models.CharField(max_length=64, blank=True)
    barcode = models.CharField(max_length=64, blank=True)
    goods_type = models.CharField(max_length=64, blank=True)
    marking_code = models.CharField(max_length=128, blank=True)
    lot_code = models.CharField(max_length=128, blank=True)
    expiry_date = models.DateField(null=True, blank=True)
    qty = models.PositiveIntegerField(default=0)
    available_qty = models.PositiveIntegerField(default=0)
    reserved_qty = models.PositiveIntegerField(default=0)
    external_reserved_qty = models.PositiveIntegerField(default=0, db_default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "fbs_stock_balance"
        ordering = ["box_id", "sku_code", "size", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["box", "identity_key"],
                name="uniq_fbs_stock_identity_per_box",
            ),
            models.UniqueConstraint(
                fields=["agency", "marking_code"],
                condition=~Q(marking_code=""),
                name="uniq_fbs_marking_code_per_agency",
            ),
            models.CheckConstraint(condition=Q(qty__gte=0), name="fbs_stock_qty_gte_zero"),
            models.CheckConstraint(
                condition=Q(available_qty__gte=0),
                name="fbs_stock_available_gte_zero",
            ),
            models.CheckConstraint(
                condition=Q(reserved_qty__gte=0),
                name="fbs_stock_reserved_gte_zero",
            ),
            models.CheckConstraint(
                condition=Q(qty__gte=F("available_qty") + F("reserved_qty")),
                name="fbs_stock_qty_covers_available_reserved",
            ),
            models.CheckConstraint(
                condition=Q(qty__gte=F("available_qty") + F("reserved_qty") + F("external_reserved_qty")),
                name="fbs_stock_covers_external_hold",
            ),
        ]
        indexes = [
            models.Index(fields=["agency", "sku_code"]),
            models.Index(fields=["agency", "barcode"]),
            models.Index(fields=["expiry_date"]),
            models.Index(fields=["box", "available_qty"]),
        ]

    def clean(self) -> None:
        super().clean()
        if self.box_id and self.box.agency_id != self.agency_id:
            raise ValidationError({"box": "Остаток и FBS-короб должны принадлежать одному клиенту."})
        if self.sku_ref_id and self.sku_ref.agency_id not in {None, self.agency_id}:
            raise ValidationError({"sku_ref": "SKU принадлежит другому клиенту."})


class FbsRackCellBinding(models.Model):
    """Hidden per-client FBS containers backing one shared physical PR rack cell."""

    rack_cell = models.ForeignKey(
        FbsRackCell,
        on_delete=models.PROTECT,
        related_name="bindings",
    )
    agency = models.ForeignKey(
        Agency,
        on_delete=models.PROTECT,
        related_name="fbs_rack_cell_bindings",
    )
    pallet = models.OneToOneField(
        FbsPallet,
        on_delete=models.PROTECT,
        related_name="rack_cell_binding",
    )
    box = models.OneToOneField(
        FbsBox,
        on_delete=models.PROTECT,
        related_name="rack_cell_binding",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_fbs_rack_cell_bindings",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "fbs_rack_cell_binding"
        ordering = ["rack_cell_id", "agency_id", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["rack_cell", "agency"],
                name="uniq_fbs_rack_cell_binding_agency",
            )
        ]
        indexes = [
            models.Index(
                fields=["agency", "rack_cell"],
                name="fbs_rbind_ag_cell_idx",
            )
        ]

    def clean(self) -> None:
        super().clean()
        if self.pallet_id:
            if self.pallet.agency_id != self.agency_id:
                raise ValidationError({"pallet": "Паллета ячейки принадлежит другому клиенту."})
            if self.pallet.cell_id != self.rack_cell.storage_cell_id:
                raise ValidationError({"pallet": "Паллета привязана к другой физической ячейке."})
            if not self.pallet.is_rack_binding:
                raise ValidationError({"pallet": "Паллета не помечена как системная паллета PR-стеллажа."})
        if self.box_id:
            if self.box.agency_id != self.agency_id:
                raise ValidationError({"box": "Короб ячейки принадлежит другому клиенту."})
            if self.pallet_id and self.box.pallet_id != self.pallet_id:
                raise ValidationError({"box": "Короб и паллета ячейки не совпадают."})

    def __str__(self) -> str:
        return f"{self.rack_cell.cell_code} / {self.agency}"


class FbsRackStagingBox(models.Model):
    STATUS_AWAITING = "awaiting_placement"
    STATUS_PLACED = "placed"
    STATUS_CANCELED = "canceled"
    STATUS_CHOICES = [
        (STATUS_AWAITING, "Ожидает размещения в ячейку"),
        (STATUS_PLACED, "Размещен в ячейке"),
        (STATUS_CANCELED, "Отменен"),
    ]

    box = models.ForeignKey(
        FbsBox,
        on_delete=models.PROTECT,
        related_name="rack_staging_records",
    )
    rack = models.ForeignKey(
        FbsRack,
        on_delete=models.PROTECT,
        related_name="staging_boxes",
    )
    arrival_operation = models.OneToOneField(
        "sklad.WarehouseOperation",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="fbs_rack_arrival",
    )
    status = models.CharField(
        max_length=24,
        choices=STATUS_CHOICES,
        default=STATUS_AWAITING,
    )
    arrived_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="arrived_fbs_rack_staging_boxes",
    )
    placed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="placed_fbs_rack_staging_boxes",
    )
    arrived_at = models.DateTimeField(auto_now_add=True)
    placed_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "fbs_rack_staging_box"
        ordering = ["arrived_at", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["box"],
                condition=Q(status="awaiting_placement"),
                name="uniq_awaiting_fbs_rack_staging_box",
            )
        ]
        indexes = [
            models.Index(
                fields=["rack", "status", "arrived_at"],
                name="fbs_stage_rack_time_idx",
            ),
            models.Index(
                fields=["box", "status"],
                name="fbs_stage_box_status_idx",
            ),
        ]

    def clean(self) -> None:
        super().clean()
        if self.box_id and self.rack_id:
            box_location_id = getattr(self.box.source_container, "current_location_id", None)
            if self.status == self.STATUS_AWAITING and box_location_id != self.rack.location_id:
                raise ValidationError(
                    {"box": "Ожидающий размещения FBS-короб должен находиться у этого PR-стеллажа."}
                )

    def __str__(self) -> str:
        return f"{self.box.box_code} -> {self.rack.rack_code} ({self.status})"


class FbsRackContentMovement(models.Model):
    """Immutable batch audit for a free all-contents move into one rack cell."""

    idempotency_key = models.CharField(max_length=64, unique=True)
    agency = models.ForeignKey(
        Agency,
        on_delete=models.PROTECT,
        related_name="fbs_rack_content_movements",
    )
    source_box = models.ForeignKey(
        FbsBox,
        on_delete=models.PROTECT,
        related_name="outgoing_rack_content_movements",
    )
    target_cell = models.ForeignKey(
        FbsRackCell,
        on_delete=models.PROTECT,
        related_name="incoming_content_movements",
    )
    target_binding = models.ForeignKey(
        FbsRackCellBinding,
        on_delete=models.PROTECT,
        related_name="content_movements",
    )
    operation = models.OneToOneField(
        "sklad.WarehouseOperation",
        on_delete=models.PROTECT,
        related_name="fbs_rack_content_movement",
    )
    moved_qty = models.PositiveIntegerField()
    performed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="performed_fbs_rack_content_movements",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "fbs_rack_content_movement"
        ordering = ["-created_at", "-id"]
        constraints = [
            models.CheckConstraint(
                condition=Q(moved_qty__gt=0),
                name="fbs_rack_content_moved_qty_gt_zero",
            )
        ]
        indexes = [
            models.Index(
                fields=["agency", "created_at"],
                name="fbs_rmove_ag_time_idx",
            ),
            models.Index(
                fields=["target_cell", "created_at"],
                name="fbs_rmove_cell_time_idx",
            ),
        ]

    def clean(self) -> None:
        super().clean()
        if self.source_box_id and self.source_box.agency_id != self.agency_id:
            raise ValidationError({"source_box": "Исходный короб принадлежит другому клиенту."})
        if self.target_binding_id:
            if self.target_binding.agency_id != self.agency_id:
                raise ValidationError({"target_binding": "Ячейка назначения принадлежит другому клиенту."})
            if self.target_binding.rack_cell_id != self.target_cell_id:
                raise ValidationError({"target_binding": "Привязка относится к другой ячейке."})

    def __str__(self) -> str:
        return f"{self.source_box.box_code} -> {self.target_cell.cell_code}: {self.moved_qty}"


class FbsStockExportState(models.Model):
    STATUS_PENDING = "pending"
    STATUS_SENDING = "sending"
    STATUS_SYNCED = "synced"
    STATUS_RETRY = "retry"
    STATUS_BLOCKED = "blocked"
    STATUS_CHOICES = [
        (STATUS_PENDING, "Ожидает выгрузки"),
        (STATUS_SENDING, "Отправляется"),
        (STATUS_SYNCED, "Выгружено"),
        (STATUS_RETRY, "Ожидает повтора"),
        (STATUS_BLOCKED, "Ошибка данных"),
    ]

    profile = models.ForeignKey(
        FbsIntegrationProfile,
        on_delete=models.CASCADE,
        related_name="stock_export_states",
    )
    sku_ref = models.ForeignKey(
        SKU,
        on_delete=models.PROTECT,
        related_name="fbs_stock_export_states",
    )
    barcode = models.CharField(max_length=64, blank=True)
    external_item_id = models.CharField(max_length=128)
    external_product_id = models.CharField(max_length=128, blank=True)
    desired_qty = models.PositiveIntegerField(default=0)
    last_sent_qty = models.PositiveIntegerField(null=True, blank=True)
    generation = models.PositiveBigIntegerField(default=0)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_PENDING)
    attempt_count = models.PositiveIntegerField(default=0)
    next_attempt_at = models.DateTimeField(null=True, blank=True)
    last_attempt_at = models.DateTimeField(null=True, blank=True)
    last_success_at = models.DateTimeField(null=True, blank=True)
    last_http_status = models.PositiveIntegerField(null=True, blank=True)
    response_payload = models.JSONField(default=dict, blank=True)
    error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "fbs_stock_export_state"
        ordering = ["profile_id", "external_item_id", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["profile", "external_item_id"],
                name="uniq_fbs_stock_export_item_per_profile",
            ),
        ]
        indexes = [
            models.Index(fields=["profile", "status", "next_attempt_at"]),
            models.Index(fields=["profile", "sku_ref"]),
        ]

    def clean(self) -> None:
        super().clean()
        if self.profile_id and self.sku_ref_id:
            if self.sku_ref.agency_id != self.profile.agency_id:
                raise ValidationError(
                    {"sku_ref": "Профиль выгрузки и SKU принадлежат разным клиентам."}
                )


class FbsReplenishmentPlan(models.Model):
    CLIENT_BOX_ITEM_FALLBACK_MARKER = "[client_box_item_fallback]"
    CLIENT_ITEM_WHOLE_BOX_MARKER = "[client_item_whole_box]"

    MODE_BOX = "box"
    MODE_ITEM = "item"
    MODE_CHOICES = [
        (MODE_BOX, "Коробами"),
        (MODE_ITEM, "Штучный подсорт"),
    ]

    STATUS_PROPOSED = "proposed"
    STATUS_CONFIRMED = "confirmed"
    STATUS_IN_PROGRESS = "in_progress"
    STATUS_AWAITING_PACK = "awaiting_pack"
    STATUS_DONE = "done"
    STATUS_BLOCKED = "blocked"
    STATUS_CANCELED = "canceled"
    STATUS_CHOICES = [
        (STATUS_PROPOSED, "Предложен программой"),
        (STATUS_CONFIRMED, "Подтвержден кладовщиком"),
        (STATUS_IN_PROGRESS, "В работе"),
        (STATUS_AWAITING_PACK, "Ожидает укладки в короб"),
        (STATUS_DONE, "Выполнен"),
        (STATUS_BLOCKED, "Заблокирован"),
        (STATUS_CANCELED, "Отменен"),
    ]

    agency = models.ForeignKey(
        Agency,
        on_delete=models.PROTECT,
        related_name="fbs_replenishment_plans",
    )
    client_movement_request = models.ForeignKey(
        "FbsClientMovementRequest",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="replenishment_plans",
    )
    mode = models.CharField(max_length=16, choices=MODE_CHOICES)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_PROPOSED)
    target_cell = models.ForeignKey(
        FbsStorageCell,
        on_delete=models.PROTECT,
        related_name="replenishment_plans",
    )
    target_pallet = models.ForeignKey(
        FbsPallet,
        on_delete=models.PROTECT,
        related_name="replenishment_plans",
    )
    target_box = models.ForeignKey(
        FbsBox,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="replenishment_plans",
    )
    staging_location = models.ForeignKey(
        "sklad.WarehouseLocation",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="fbs_staging_replenishment_plans",
    )
    warehouse_operation = models.OneToOneField(
        "sklad.WarehouseOperation",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="fbs_replenishment_plan",
    )
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="requested_fbs_replenishments",
    )
    confirmed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="confirmed_fbs_replenishments",
    )
    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="assigned_fbs_replenishments",
    )
    requested_by_role = models.CharField(max_length=32, blank=True)
    comment = models.TextField(blank=True)
    planned_qty = models.PositiveIntegerField(default=0)
    moved_qty = models.PositiveIntegerField(default=0)
    confirmed_at = models.DateTimeField(null=True, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    canceled_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "fbs_replenishment_plan"
        ordering = ["-created_at", "-id"]
        constraints = [
            models.CheckConstraint(
                condition=Q(planned_qty__gte=F("moved_qty")),
                name="fbs_plan_moved_lte_planned",
            )
        ]
        indexes = [
            models.Index(fields=["agency", "status"]),
            models.Index(fields=["status", "created_at"]),
            models.Index(fields=["assigned_to", "status"]),
        ]

    def clean(self) -> None:
        super().clean()
        if self.client_movement_request_id:
            if self.client_movement_request.agency_id != self.agency_id:
                raise ValidationError(
                    {"client_movement_request": "Заявка на перемещение принадлежит другому клиенту."}
                )
            is_box_item_fallback = (
                self.client_movement_request.mode
                == FbsClientMovementRequest.MODE_BOX
                and self.mode == self.MODE_ITEM
                and str(self.comment or "").startswith(
                    self.CLIENT_BOX_ITEM_FALLBACK_MARKER
                )
            )
            is_item_whole_box = (
                self.client_movement_request.mode
                == FbsClientMovementRequest.MODE_ITEM
                and self.mode == self.MODE_BOX
                and self.CLIENT_ITEM_WHOLE_BOX_MARKER
                in str(self.comment or "")[:160]
            )
            if (
                self.client_movement_request.mode != self.mode
                and not is_box_item_fallback
                and not is_item_whole_box
            ):
                raise ValidationError(
                    {"client_movement_request": "Режим заявки не совпадает с режимом плана."}
                )
        if self.target_pallet_id and self.target_pallet.agency_id != self.agency_id:
            raise ValidationError({"target_pallet": "Паллета принадлежит другому клиенту."})
        if self.target_pallet_id and self.target_pallet.cell_id != self.target_cell_id:
            raise ValidationError({"target_cell": "Паллета находится в другой FBS-ячейке."})
        if self.target_box_id:
            if self.target_box.agency_id != self.agency_id:
                raise ValidationError({"target_box": "Короб принадлежит другому клиенту."})
            if self.target_box.pallet_id != self.target_pallet_id:
                raise ValidationError({"target_box": "Короб находится на другой FBS-паллете."})


class FbsReplenishmentLine(models.Model):
    STATUS_PROPOSED = "proposed"
    STATUS_RESERVED = "reserved"
    STATUS_IN_PROGRESS = "in_progress"
    STATUS_DONE = "done"
    STATUS_CANCELED = "canceled"
    STATUS_CHOICES = [
        (STATUS_PROPOSED, "Предложена"),
        (STATUS_RESERVED, "Зарезервирована"),
        (STATUS_IN_PROGRESS, "В работе"),
        (STATUS_DONE, "Выполнена"),
        (STATUS_CANCELED, "Отменена"),
    ]

    plan = models.ForeignKey(FbsReplenishmentPlan, on_delete=models.CASCADE, related_name="lines")
    client_movement_line = models.ForeignKey(
        "FbsClientMovementRequestLine",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="replenishment_lines",
    )
    sku_ref = models.ForeignKey(
        SKU,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="fbs_replenishment_lines",
    )
    sku_code = models.CharField(max_length=64, blank=True)
    barcode = models.CharField(max_length=64, blank=True)
    source_container = models.ForeignKey(
        "sklad.WarehouseContainer",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="fbs_replenishment_lines",
    )
    target_box = models.ForeignKey(
        FbsBox,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="replenishment_lines",
    )
    qty_requested = models.PositiveIntegerField()
    qty_planned = models.PositiveIntegerField(default=0)
    qty_moved = models.PositiveIntegerField(default=0)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_PROPOSED)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "fbs_replenishment_line"
        ordering = ["plan_id", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["source_container"],
                condition=Q(
                    source_container__isnull=False,
                    status__in=["proposed", "reserved", "in_progress"],
                ),
                name="uniq_open_fbs_line_per_source_container",
            ),
            models.CheckConstraint(
                condition=Q(qty_requested__gt=0),
                name="fbs_line_requested_gt_zero",
            ),
            models.CheckConstraint(
                condition=Q(qty_planned__gte=F("qty_moved")),
                name="fbs_line_moved_lte_planned",
            ),
        ]
        indexes = [
            models.Index(fields=["plan", "status"]),
            models.Index(fields=["plan", "barcode"]),
            models.Index(fields=["sku_ref"]),
            models.Index(fields=["source_container"]),
        ]

    def clean(self) -> None:
        super().clean()
        if self.client_movement_line_id:
            if self.plan.client_movement_request_id != self.client_movement_line.request_id:
                raise ValidationError(
                    {"client_movement_line": "Строка заявки не относится к заявке плана."}
                )
            if self.plan.mode == FbsReplenishmentPlan.MODE_ITEM:
                if str(self.barcode or "").strip() != str(
                    self.client_movement_line.barcode or ""
                ).strip():
                    raise ValidationError(
                        {"client_movement_line": "Штрихкод строки заявки не совпадает с планом."}
                    )
                if (
                    self.client_movement_line.request.mode
                    == FbsClientMovementRequest.MODE_BOX
                    and (
                        int(self.qty_requested or 0)
                        % int(self.client_movement_line.units_per_box or 1)
                        != 0
                        or int(self.qty_requested or 0)
                        > int(self.client_movement_line.requested_qty or 0)
                    )
                ):
                    raise ValidationError(
                        {
                            "qty_requested": (
                                "Поштучный добор коробной заявки должен соответствовать "
                                "кратности и количеству исходной строки."
                            )
                        }
                    )
        if self.target_box_id and self.target_box.agency_id != self.plan.agency_id:
            raise ValidationError({"target_box": "Короб назначения принадлежит другому клиенту."})
        if self.plan.mode == FbsReplenishmentPlan.MODE_ITEM and not self.sku_ref_id:
            raise ValidationError({"sku_ref": "Для штучного подсорта необходимо указать SKU."})
        if self.plan.mode == FbsReplenishmentPlan.MODE_ITEM and not str(
            self.barcode or ""
        ).strip():
            raise ValidationError(
                {"barcode": "Для штучного подсорта необходимо указать штрихкод."}
            )
        if self.plan.mode == FbsReplenishmentPlan.MODE_BOX and not self.source_container_id:
            raise ValidationError(
                {"source_container": "Для подсорта коробами необходимо указать исходный короб."}
            )
        if self.sku_ref_id and self.sku_ref.agency_id not in {None, self.plan.agency_id}:
            raise ValidationError({"sku_ref": "SKU принадлежит другому клиенту."})
        if self.source_container_id and self.source_container.agency_id != self.plan.agency_id:
            raise ValidationError({"source_container": "Исходный короб принадлежит другому клиенту."})


class FbsReplenishmentAllocation(models.Model):
    STATUS_RESERVED = "reserved"
    STATUS_IN_PROGRESS = "in_progress"
    STATUS_STAGED = "staged"
    STATUS_DONE = "done"
    STATUS_CANCELED = "canceled"
    STATUS_CHOICES = [
        (STATUS_RESERVED, "Зарезервировано"),
        (STATUS_IN_PROGRESS, "В работе"),
        (STATUS_STAGED, "Передано кладовщику"),
        (STATUS_DONE, "Перемещено"),
        (STATUS_CANCELED, "Отменено"),
    ]

    line = models.ForeignKey(
        FbsReplenishmentLine,
        on_delete=models.CASCADE,
        related_name="allocations",
    )
    source_snapshot = models.ForeignKey(
        "sklad.WarehouseStockSnapshot",
        on_delete=models.PROTECT,
        related_name="fbs_replenishment_allocations",
    )
    target_box = models.ForeignKey(
        FbsBox,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="replenishment_allocations",
    )
    warehouse_reserve = models.OneToOneField(
        "sklad.WarehouseReserve",
        on_delete=models.PROTECT,
        related_name="fbs_replenishment_allocation",
    )
    warehouse_task = models.ForeignKey(
        "sklad.WarehouseOperationTask",
        on_delete=models.PROTECT,
        related_name="fbs_replenishment_allocations",
    )
    qty_planned = models.PositiveIntegerField()
    qty_staged = models.PositiveIntegerField(default=0)
    qty_moved = models.PositiveIntegerField(default=0)
    source_snapshot_version = models.PositiveIntegerField()
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_RESERVED)
    staged_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="staged_fbs_replenishment_allocations",
    )
    staged_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "fbs_replenishment_allocation"
        ordering = ["line_id", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["line", "source_snapshot"],
                name="uniq_fbs_replenishment_allocation",
            ),
            models.CheckConstraint(
                condition=Q(qty_planned__gt=0),
                name="fbs_allocation_planned_gt_zero",
            ),
            models.CheckConstraint(
                condition=Q(qty_planned__gte=F("qty_staged")),
                name="fbs_allocation_staged_lte_planned",
            ),
            models.CheckConstraint(
                condition=Q(qty_planned__gte=F("qty_moved")),
                name="fbs_allocation_moved_lte_planned",
            ),
        ]
        indexes = [
            models.Index(fields=["status", "created_at"]),
            models.Index(fields=["source_snapshot"]),
            models.Index(fields=["target_box", "status"]),
        ]


class FbsReplenishmentPreparedBox(models.Model):
    STATUS_PRINTED = "printed"
    STATUS_FILLING = "filling"
    STATUS_CLOSED = "closed"
    STATUS_PLACED = "placed"
    STATUS_UNUSED = "unused"
    STATUS_CHOICES = [
        (STATUS_PRINTED, "Напечатан"),
        (STATUS_FILLING, "Заполняется"),
        (STATUS_CLOSED, "Закрыт"),
        (STATUS_PLACED, "Размещен"),
        (STATUS_UNUSED, "Не использован"),
    ]

    plan = models.ForeignKey(
        FbsReplenishmentPlan,
        on_delete=models.PROTECT,
        related_name="prepared_boxes",
    )
    agency = models.ForeignKey(
        Agency,
        on_delete=models.PROTECT,
        related_name="fbs_replenishment_prepared_boxes",
    )
    sequence_no = models.PositiveSmallIntegerField()
    box_code = models.CharField(max_length=128)
    physical_container = models.OneToOneField(
        "sklad.WarehouseContainer",
        on_delete=models.PROTECT,
        related_name="fbs_replenishment_prepared_box",
    )
    fbs_box = models.OneToOneField(
        FbsBox,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="replenishment_prepared_box",
    )
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_PRINTED)
    scanned_qty = models.PositiveIntegerField(default=0)
    placed_qty = models.PositiveIntegerField(default=0)
    printed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="printed_fbs_replenishment_boxes",
    )
    closed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="closed_fbs_replenishment_boxes",
    )
    placed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="placed_fbs_replenishment_boxes",
    )
    printed_at = models.DateTimeField(auto_now_add=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    placed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "fbs_replenishment_prepared_box"
        ordering = ["plan_id", "sequence_no", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["plan", "sequence_no"],
                name="uniq_fbs_prepared_box_sequence",
            ),
            models.UniqueConstraint(
                fields=["agency", "box_code"],
                name="uniq_fbs_prepared_box_code_per_agency",
            ),
            models.CheckConstraint(
                condition=Q(sequence_no__gt=0),
                name="fbs_prepared_box_sequence_gt_zero",
            ),
            models.CheckConstraint(
                condition=Q(scanned_qty__gte=F("placed_qty")),
                name="fbs_prepared_box_scanned_covers_placed",
            ),
        ]
        indexes = [
            models.Index(fields=["plan", "status"]),
            models.Index(fields=["agency", "status"]),
            models.Index(fields=["box_code"]),
        ]

    def clean(self) -> None:
        super().clean()
        if self.plan_id and self.plan.agency_id != self.agency_id:
            raise ValidationError({"agency": "Подготовленный короб принадлежит другому клиенту."})
        if self.physical_container_id and self.physical_container.agency_id != self.agency_id:
            raise ValidationError(
                {"physical_container": "Физический короб принадлежит другому клиенту."}
            )
        if self.fbs_box_id and self.fbs_box.agency_id != self.agency_id:
            raise ValidationError({"fbs_box": "Размещенный FBS-короб принадлежит другому клиенту."})


class FbsReplenishmentPreparedBoxItem(models.Model):
    prepared_box = models.ForeignKey(
        FbsReplenishmentPreparedBox,
        on_delete=models.PROTECT,
        related_name="items",
    )
    allocation = models.ForeignKey(
        FbsReplenishmentAllocation,
        on_delete=models.PROTECT,
        related_name="prepared_box_items",
    )
    qty_scanned = models.PositiveIntegerField(default=0)
    qty_placed = models.PositiveIntegerField(default=0)
    target_balance = models.ForeignKey(
        FbsStockBalance,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="prepared_box_items",
    )
    warehouse_event = models.OneToOneField(
        "sklad.WarehouseEvent",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="fbs_prepared_box_item",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "fbs_replenishment_prepared_box_item"
        ordering = ["prepared_box_id", "allocation_id", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["prepared_box", "allocation"],
                name="uniq_fbs_prepared_box_allocation",
            ),
            models.CheckConstraint(
                condition=Q(qty_scanned__gt=0),
                name="fbs_prepared_box_item_scanned_gt_zero",
            ),
            models.CheckConstraint(
                condition=Q(qty_scanned__gte=F("qty_placed")),
                name="fbs_prepared_item_scanned_covers_placed",
            ),
        ]
        indexes = [
            models.Index(fields=["allocation", "prepared_box"]),
            models.Index(fields=["prepared_box", "qty_placed"]),
        ]

    def clean(self) -> None:
        super().clean()
        if self.prepared_box_id and self.allocation_id:
            if self.prepared_box.plan_id != self.allocation.line.plan_id:
                raise ValidationError(
                    {"allocation": "Товар и подготовленный короб относятся к разным планам."}
                )


class FbsStockMovement(models.Model):
    allocation = models.OneToOneField(
        FbsReplenishmentAllocation,
        on_delete=models.PROTECT,
        related_name="stock_movement",
    )
    source_snapshot = models.ForeignKey(
        "sklad.WarehouseStockSnapshot",
        on_delete=models.PROTECT,
        related_name="fbs_stock_movements",
    )
    target_balance = models.ForeignKey(
        FbsStockBalance,
        on_delete=models.PROTECT,
        related_name="movements",
    )
    warehouse_event = models.OneToOneField(
        "sklad.WarehouseEvent",
        on_delete=models.PROTECT,
        related_name="fbs_stock_movement",
    )
    qty = models.PositiveIntegerField()
    performed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="performed_fbs_stock_movements",
    )
    occurred_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "fbs_stock_movement"
        ordering = ["occurred_at", "id"]
        constraints = [
            models.CheckConstraint(condition=Q(qty__gt=0), name="fbs_stock_movement_qty_gt_zero")
        ]
        indexes = [
            models.Index(fields=["occurred_at"]),
            models.Index(fields=["source_snapshot"]),
            models.Index(fields=["target_balance"]),
        ]


class FbsPickBatch(models.Model):
    STATUS_QUEUED = "queued"
    STATUS_IN_PROGRESS = "in_progress"
    STATUS_VERIFICATION = "verification"
    STATUS_DONE = "done"
    STATUS_CANCELED = "canceled"
    STATUS_CHOICES = [
        (STATUS_QUEUED, "В очереди"),
        (STATUS_IN_PROGRESS, "В работе"),
        (STATUS_VERIFICATION, "Проверка"),
        (STATUS_DONE, "Завершена"),
        (STATUS_CANCELED, "Отменена"),
    ]

    agency = models.ForeignKey(
        Agency,
        on_delete=models.PROTECT,
        related_name="fbs_pick_batches",
    )
    status = models.CharField(max_length=24, choices=STATUS_CHOICES, default=STATUS_QUEUED)
    planned_qty = models.PositiveIntegerField(default=0)
    picked_qty = models.PositiveIntegerField(default=0)
    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="assigned_fbs_pick_batches",
    )
    workstation = models.ForeignKey(
        FbsWorkstation,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="pick_batches",
    )
    cart = models.ForeignKey(
        FbsPickingCart,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="pick_batches",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_fbs_pick_batches",
    )
    verification_assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="controlled_fbs_pick_batches",
    )
    started_at = models.DateTimeField(null=True, blank=True)
    claimed_at = models.DateTimeField(null=True, blank=True)
    picking_completed_at = models.DateTimeField(null=True, blank=True)
    verification_started_at = models.DateTimeField(null=True, blank=True)
    cart_released_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    canceled_at = models.DateTimeField(null=True, blank=True)
    manual_queue_rank = models.PositiveIntegerField(
        "Ручное место в очереди",
        null=True,
        blank=True,
    )
    manual_queue_date = models.DateField(
        "Дата ручного порядка очереди",
        null=True,
        blank=True,
    )
    queue_order_updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reordered_fbs_pick_batches",
        verbose_name="Кем изменен порядок очереди",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "fbs_pick_batch"
        ordering = ["created_at", "id"]
        constraints = [
            models.CheckConstraint(
                condition=Q(planned_qty__gte=F("picked_qty")),
                name="fbs_pick_batch_picked_lte_planned",
            ),
            models.UniqueConstraint(
                fields=["cart"],
                condition=Q(
                    status__in=("in_progress", "verification"),
                    picking_completed_at__isnull=True,
                    cart__isnull=False,
                ),
                name="uniq_active_fbs_wave_per_cart",
            ),
        ]
        indexes = [
            models.Index(fields=["agency", "status"]),
            models.Index(fields=["status", "created_at"]),
            models.Index(fields=["manual_queue_date", "manual_queue_rank"]),
            models.Index(fields=["assigned_to", "status"]),
            models.Index(fields=["workstation", "status"]),
            models.Index(fields=["verification_assigned_to", "status"]),
        ]


class FbsPickTask(models.Model):
    STATUS_QUEUED = "queued"
    STATUS_IN_PROGRESS = "in_progress"
    STATUS_PICKED = "picked"
    STATUS_EXCEPTION = "exception"
    STATUS_CANCELED = "canceled"
    STATUS_CHOICES = [
        (STATUS_QUEUED, "В очереди"),
        (STATUS_IN_PROGRESS, "В работе"),
        (STATUS_PICKED, "Отобран"),
        (STATUS_EXCEPTION, "Исключение"),
        (STATUS_CANCELED, "Отменен"),
    ]

    batch = models.ForeignKey(FbsPickBatch, on_delete=models.PROTECT, related_name="tasks")
    order = models.ForeignKey(FbsOrder, on_delete=models.PROTECT, related_name="pick_tasks")
    status = models.CharField(max_length=24, choices=STATUS_CHOICES, default=STATUS_QUEUED)
    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="assigned_fbs_pick_tasks",
    )
    sort_order = models.PositiveIntegerField(default=0)
    planned_qty = models.PositiveIntegerField(default=0)
    picked_qty = models.PositiveIntegerField(default=0)
    claimed_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    canceled_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "fbs_pick_task"
        ordering = ["batch_id", "sort_order", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["batch", "order"],
                name="uniq_fbs_pick_task_batch_order",
            ),
            models.UniqueConstraint(
                fields=["order"],
                condition=Q(status__in=["queued", "in_progress"]),
                name="uniq_active_fbs_pick_task_per_order",
            ),
            models.CheckConstraint(
                condition=Q(planned_qty__gt=0),
                name="fbs_pick_task_planned_gt_zero",
            ),
            models.CheckConstraint(
                condition=Q(planned_qty__gte=F("picked_qty")),
                name="fbs_pick_task_picked_lte_planned",
            ),
        ]
        indexes = [
            models.Index(fields=["status", "sort_order", "created_at"]),
            models.Index(fields=["assigned_to", "status"]),
            models.Index(fields=["order", "status"]),
        ]

    def clean(self) -> None:
        super().clean()
        if self.batch_id and self.order_id:
            if self.batch.agency_id != self.order.profile.agency_id:
                raise ValidationError({"order": "Заказ принадлежит другому клиенту."})


class FbsOrderStockAllocation(models.Model):
    STATUS_RESERVED = "reserved"
    STATUS_PICKING = "picking"
    STATUS_PICKED = "picked"
    STATUS_RELEASED = "released"
    STATUS_CANCELED = "canceled"
    STATUS_CHOICES = [
        (STATUS_RESERVED, "Зарезервировано"),
        (STATUS_PICKING, "Отбирается"),
        (STATUS_PICKED, "Отобрано"),
        (STATUS_RELEASED, "Освобождено"),
        (STATUS_CANCELED, "Отменено"),
    ]

    order_item = models.ForeignKey(
        FbsOrderItem,
        on_delete=models.PROTECT,
        related_name="stock_allocations",
    )
    balance = models.ForeignKey(
        FbsStockBalance,
        on_delete=models.PROTECT,
        related_name="order_allocations",
    )
    pick_task = models.ForeignKey(
        FbsPickTask,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="allocations",
    )
    reserved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reserved_fbs_order_allocations",
    )
    picked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="picked_fbs_order_allocations",
    )
    released_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="released_fbs_order_allocations",
    )
    qty_reserved = models.PositiveIntegerField()
    qty_picked = models.PositiveIntegerField(default=0)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_RESERVED)
    reserved_at = models.DateTimeField(auto_now_add=True)
    picked_at = models.DateTimeField(null=True, blank=True)
    released_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "fbs_order_stock_allocation"
        ordering = ["order_item__order_id", "order_item_id", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["order_item", "balance"],
                condition=Q(status__in=["reserved", "picking"]),
                name="uniq_active_fbs_order_balance_allocation",
            ),
            models.CheckConstraint(
                condition=Q(qty_reserved__gt=0),
                name="fbs_order_allocation_reserved_gt_zero",
            ),
            models.CheckConstraint(
                condition=Q(qty_reserved__gte=F("qty_picked")),
                name="fbs_order_allocation_picked_lte_reserved",
            ),
        ]
        indexes = [
            models.Index(fields=["status", "reserved_at"]),
            models.Index(fields=["order_item", "status"]),
            models.Index(fields=["balance", "status"]),
            models.Index(fields=["pick_task", "status"]),
        ]

    def clean(self) -> None:
        super().clean()
        if not self.order_item_id or not self.balance_id:
            return
        order_agency_id = self.order_item.order.profile.agency_id
        if self.balance.agency_id != order_agency_id:
            raise ValidationError({"balance": "Остаток принадлежит другому клиенту."})
        order_barcode = str(self.order_item.barcode or "").strip()
        balance_barcode = str(self.balance.barcode or "").strip()
        if not order_barcode:
            raise ValidationError({"order_item": "В строке заказа отсутствует штрихкод."})
        if order_barcode != balance_barcode:
            same_variant = bool(
                self.order_item.sku_id
                and self.balance.sku_ref_id == self.order_item.sku_id
                and same_sku_barcode_variant(
                    sku_id=self.order_item.sku_id,
                    first=order_barcode,
                    second=balance_barcode,
                )
            )
            if not same_variant:
                raise ValidationError(
                    {
                        "balance": (
                            "Штрихкод остатка не совпадает с заказом и не является "
                            "штрихкодом того же SKU и размера."
                        )
                    }
                )
        if self.pick_task_id and self.pick_task.order_id != self.order_item.order_id:
            raise ValidationError({"pick_task": "Задание относится к другому заказу."})


class FbsOrderTraceability(models.Model):
    STATUS_RESERVED = "reserved"
    STATUS_PICKED = "picked"
    STATUS_RELEASED = "released"
    STATUS_CANCELED = "canceled"
    STATUS_CHOICES = [
        (STATUS_RESERVED, "Зарезервировано"),
        (STATUS_PICKED, "Отобрано"),
        (STATUS_RELEASED, "Освобождено"),
        (STATUS_CANCELED, "Отменено"),
    ]

    allocation = models.OneToOneField(
        FbsOrderStockAllocation,
        on_delete=models.PROTECT,
        related_name="traceability",
    )
    marking_code = models.CharField(max_length=128, blank=True)
    lot_code = models.CharField(max_length=128, blank=True)
    expiry_date = models.DateField(null=True, blank=True)
    qty = models.PositiveIntegerField()
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_RESERVED)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "fbs_order_traceability"
        ordering = ["allocation_id"]
        constraints = [
            models.CheckConstraint(condition=Q(qty__gt=0), name="fbs_traceability_qty_gt_zero"),
            models.CheckConstraint(
                condition=Q(marking_code="") | Q(qty=1),
                name="fbs_traceability_marking_qty_one",
            ),
        ]
        indexes = [
            models.Index(fields=["status", "created_at"]),
            models.Index(fields=["lot_code", "expiry_date"]),
            models.Index(fields=["marking_code"]),
        ]

    def clean(self) -> None:
        super().clean()
        if self.allocation_id and self.marking_code:
            balance_code = str(self.allocation.balance.marking_code or "").strip()
            marketplace = str(
                self.allocation.order_item.order.profile.marketplace or ""
            ).strip()
            if (
                marketplace != FbsIntegrationProfile.MARKETPLACE_WB
                and balance_code
                and balance_code != str(self.marking_code or "").strip()
            ):
                raise ValidationError({"marking_code": "КИЗ не совпадает с FBS-остатком."})


class FbsMarketplaceMetadataTransfer(models.Model):
    TYPE_MARKING_CODE = "marking_code"
    TYPE_EXPIRATION = "expiration"
    TYPE_CHOICES = [
        (TYPE_MARKING_CODE, "КИЗ"),
        (TYPE_EXPIRATION, "Срок годности"),
    ]

    STATUS_PREPARED = "prepared"
    STATUS_QUEUED = "queued"
    STATUS_SENT = "sent"
    STATUS_CONFIRMED = "confirmed"
    STATUS_RETRY = "retry"
    STATUS_FAILED = "failed"
    STATUS_CONFLICT = "conflict"
    STATUS_UNSUPPORTED = "unsupported"
    STATUS_CANCELED = "canceled"
    STATUS_CHOICES = [
        (STATUS_PREPARED, "Подготовлено"),
        (STATUS_QUEUED, "В очереди"),
        (STATUS_SENT, "Отправлено"),
        (STATUS_CONFIRMED, "Подтверждено"),
        (STATUS_RETRY, "Повтор"),
        (STATUS_FAILED, "Ошибка"),
        (STATUS_CONFLICT, "Конфликт"),
        (STATUS_UNSUPPORTED, "Не поддерживается API"),
        (STATUS_CANCELED, "Отменено"),
    ]

    order_item = models.ForeignKey(
        FbsOrderItem,
        on_delete=models.PROTECT,
        related_name="metadata_transfers",
    )
    traceability = models.ForeignKey(
        FbsOrderTraceability,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="metadata_transfers",
    )
    metadata_type = models.CharField(max_length=32, choices=TYPE_CHOICES)
    value = models.TextField()
    is_required = models.BooleanField(default=False)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_PREPARED)
    idempotency_key = models.CharField(max_length=64, unique=True)
    attempt_count = models.PositiveIntegerField(default=0)
    external_status = models.CharField(max_length=128, blank=True)
    last_error = models.TextField(blank=True)
    prepared_at = models.DateTimeField(auto_now_add=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    confirmed_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "fbs_marketplace_metadata_transfer"
        ordering = ["order_item__order_id", "order_item_id", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["traceability", "metadata_type"],
                condition=Q(traceability__isnull=False),
                name="uniq_fbs_traceability_metadata_type",
            )
        ]
        indexes = [
            models.Index(fields=["order_item", "status"]),
            models.Index(fields=["metadata_type", "status"]),
            models.Index(fields=["status", "prepared_at"]),
        ]

    def clean(self) -> None:
        super().clean()
        if self.traceability_id:
            trace_item_id = self.traceability.allocation.order_item_id
            if trace_item_id != self.order_item_id:
                raise ValidationError({"traceability": "Трассировка относится к другой строке заказа."})


class FbsOrderLabel(models.Model):
    STATUS_REQUESTED = "requested"
    STATUS_READY = "ready"
    STATUS_APPLIED = "applied"
    STATUS_ERROR = "error"
    STATUS_CANCELED = "canceled"
    STATUS_CHOICES = [
        (STATUS_REQUESTED, "Запрошена"),
        (STATUS_READY, "Готова"),
        (STATUS_APPLIED, "Подтверждена"),
        (STATUS_ERROR, "Ошибка"),
        (STATUS_CANCELED, "Отменена"),
    ]

    FORMAT_PDF = "pdf"
    FORMAT_PNG = "png"
    FORMAT_ZPL = "zpl"
    FORMAT_OTHER = "other"
    FORMAT_CHOICES = [
        (FORMAT_PDF, "PDF"),
        (FORMAT_PNG, "PNG"),
        (FORMAT_ZPL, "ZPL"),
        (FORMAT_OTHER, "Другой"),
    ]

    order = models.ForeignKey(
        FbsOrder,
        on_delete=models.PROTECT,
        related_name="marketplace_labels",
    )
    marketplace = models.CharField(
        max_length=16,
        choices=FbsIntegrationProfile.MARKETPLACE_CHOICES,
    )
    external_label_id = models.CharField(max_length=256, blank=True)
    barcode = models.CharField(max_length=512, blank=True)
    label_format = models.CharField(
        max_length=16,
        choices=FORMAT_CHOICES,
        default=FORMAT_PDF,
    )
    file = models.FileField(
        upload_to="order-labels/%Y/%m/%d/",
        storage=fbs_label_storage,
        blank=True,
    )
    file_url = models.URLField(max_length=1000, blank=True)
    payload = models.JSONField(default=dict, blank=True)
    content_hash = models.CharField(max_length=64, blank=True)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_REQUESTED)
    error = models.TextField(blank=True)
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="requested_fbs_order_labels",
    )
    applied_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="applied_fbs_order_labels",
    )
    requested_at = models.DateTimeField(auto_now_add=True)
    ready_at = models.DateTimeField(null=True, blank=True)
    applied_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "fbs_order_label"
        ordering = ["requested_at", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["order"],
                condition=Q(status__in=["requested", "ready"]),
                name="uniq_active_fbs_label_per_order",
            ),
            models.UniqueConstraint(
                fields=["order", "external_label_id"],
                condition=~Q(external_label_id=""),
                name="uniq_fbs_order_external_label",
            ),
        ]
        indexes = [
            models.Index(fields=["status", "requested_at"]),
            models.Index(fields=["marketplace", "status"]),
            models.Index(fields=["order", "status"]),
        ]

    def clean(self) -> None:
        super().clean()
        if self.order_id and self.marketplace != self.order.profile.marketplace:
            raise ValidationError(
                {"marketplace": "Маркетплейс этикетки не совпадает с профилем заказа."}
            )
        if self.status in {self.STATUS_READY, self.STATUS_APPLIED}:
            if not str(self.external_label_id or "").strip():
                raise ValidationError({"external_label_id": "Не указан идентификатор этикетки."})
            if not str(self.barcode or "").strip():
                raise ValidationError({"barcode": "Не указан штрихкод этикетки."})

    @property
    def document_url(self) -> str:
        if self.file:
            return reverse("fbs:tsd_label_file", kwargs={"label_id": self.id})
        return self.file_url


class FbsClientStoragePolicy(models.Model):
    BILLING_LITERS = "liters"
    BILLING_PALLETS = "pallets"
    BILLING_CHOICES = [
        (BILLING_LITERS, "Хранение в литрах"),
        (BILLING_PALLETS, "Паллетное хранение"),
    ]

    agency = models.OneToOneField(
        Agency,
        on_delete=models.CASCADE,
        related_name="fbs_storage_policy",
    )
    billing_mode = models.CharField(max_length=16, choices=BILLING_CHOICES)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "fbs_client_storage_policy"
        ordering = ["agency_id"]


class FbsReplenishmentPolicy(models.Model):
    agency = models.ForeignKey(
        Agency,
        on_delete=models.CASCADE,
        related_name="fbs_replenishment_policies",
    )
    sku = models.ForeignKey(
        SKU,
        on_delete=models.CASCADE,
        related_name="fbs_replenishment_policies",
    )
    minimum_qty = models.PositiveIntegerField(default=0)
    target_qty = models.PositiveIntegerField(default=0)
    auto_suggest = models.BooleanField(default=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "fbs_replenishment_policy"
        ordering = ["agency_id", "sku_id"]
        constraints = [
            models.UniqueConstraint(
                fields=["agency", "sku"],
                name="uniq_fbs_replenishment_policy",
            ),
            models.CheckConstraint(
                condition=Q(target_qty__gte=F("minimum_qty")),
                name="fbs_replenishment_target_gte_minimum",
            ),
        ]
        indexes = [models.Index(fields=["agency", "is_active"])]

    def clean(self) -> None:
        super().clean()
        if self.sku_id and self.sku.agency_id not in {None, self.agency_id}:
            raise ValidationError({"sku": "SKU принадлежит другому клиенту."})


class FbsPickException(models.Model):
    inventory_session = models.ForeignKey(
        "FbsInventorySession", on_delete=models.PROTECT, null=True, blank=True,
        related_name="pick_issues",
    )
    TYPE_NOT_FOUND = "not_found"
    TYPE_DAMAGED = "damaged"
    TYPE_BARCODE = "barcode"
    TYPE_OTHER = "other"
    TYPE_CHOICES = [
        (TYPE_NOT_FOUND, "Товар не найден"),
        (TYPE_DAMAGED, "Товар поврежден"),
        (TYPE_BARCODE, "Штрихкод не читается"),
        (TYPE_OTHER, "Другая проблема"),
    ]
    STATUS_OPEN = "open"
    STATUS_RESOLVED = "resolved"
    STATUS_CANCELED = "canceled"
    STATUS_CHOICES = [
        (STATUS_OPEN, "Открыта"),
        (STATUS_RESOLVED, "Решена"),
        (STATUS_CANCELED, "Отменена"),
    ]

    task = models.ForeignKey(FbsPickTask, on_delete=models.PROTECT, related_name="exceptions")
    allocation = models.ForeignKey(
        FbsOrderStockAllocation,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="exceptions",
    )
    exception_type = models.CharField(max_length=32, choices=TYPE_CHOICES)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_OPEN)
    reason = models.TextField(blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_fbs_pick_exceptions",
    )
    resolved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="resolved_fbs_pick_exceptions",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "fbs_pick_exception"
        ordering = ["-created_at", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["allocation"],
                condition=Q(status="open", allocation__isnull=False),
                name="uniq_open_fbs_exception_per_allocation",
            )
        ]
        indexes = [
            models.Index(fields=["status", "created_at"]),
            models.Index(fields=["task", "status"]),
        ]

    def clean(self) -> None:
        super().clean()
        if self.allocation_id and self.allocation.pick_task_id != self.task_id:
            raise ValidationError({"allocation": "Позиция относится к другому заданию."})


class FbsPickVerificationProgress(models.Model):
    allocation = models.OneToOneField(
        FbsOrderStockAllocation,
        on_delete=models.PROTECT,
        related_name="verification_progress",
    )
    qty_verified = models.PositiveIntegerField(default=0)
    verified_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="verified_fbs_pick_allocations",
    )
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "fbs_pick_verification_progress"
        ordering = ["allocation_id"]
        indexes = [
            models.Index(
                fields=["completed_at", "allocation"],
                name="fbs_verify_completed_idx",
            )
        ]

    def clean(self) -> None:
        super().clean()
        if self.allocation_id and int(self.qty_verified or 0) > int(
            self.allocation.qty_picked or 0
        ):
            raise ValidationError(
                {"qty_verified": "Нельзя проверить больше фактически отобранного количества."}
            )


class FbsPickScanEventQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ValidationError("События сканирования нельзя изменять.")

    def delete(self):
        raise ValidationError("События сканирования нельзя удалять.")


class FbsPickScanEvent(models.Model):
    STAGE_WORKSTATION = "workstation"
    STAGE_CART = "cart"
    STAGE_CELL = "cell"
    STAGE_BOX = "box"
    STAGE_PICK_ITEM = "pick_item"
    STAGE_VERIFY_ITEM = "verify_item"
    STAGE_VERIFY_MARKING = "verify_marking"
    STAGE_VERIFY_EXPIRY = "verify_expiry"
    STAGE_ORDER_LABEL = "order_label"
    STAGE_WAVE_HANDOVER = "wave_handover"
    STAGE_CHOICES = [
        (STAGE_WORKSTATION, "Рабочее место"),
        (STAGE_CART, "Тележка"),
        (STAGE_CELL, "Ячейка"),
        (STAGE_BOX, "Короб FBS"),
        (STAGE_PICK_ITEM, "Отбор товара"),
        (STAGE_VERIFY_ITEM, "Проверка товара"),
        (STAGE_VERIFY_MARKING, "Проверка КИЗ"),
        (STAGE_VERIFY_EXPIRY, "Срок годности"),
        (STAGE_ORDER_LABEL, "Этикетка заказа"),
        (STAGE_WAVE_HANDOVER, "Сдача волны на проверку"),
    ]
    RESULT_SUCCESS = "success"
    RESULT_ERROR = "error"
    RESULT_CHOICES = [
        (RESULT_SUCCESS, "Успешно"),
        (RESULT_ERROR, "Ошибка"),
    ]

    batch = models.ForeignKey(
        FbsPickBatch,
        on_delete=models.PROTECT,
        related_name="scan_events",
    )
    task = models.ForeignKey(
        FbsPickTask,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="scan_events",
    )
    allocation = models.ForeignKey(
        FbsOrderStockAllocation,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="scan_events",
    )
    stage = models.CharField(max_length=24, choices=STAGE_CHOICES)
    result = models.CharField(max_length=16, choices=RESULT_CHOICES)
    scan_value = models.CharField(max_length=512, blank=True)
    expected_value = models.CharField(max_length=512, blank=True)
    quantity_after = models.PositiveIntegerField(null=True, blank=True)
    message = models.TextField(blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="fbs_pick_scan_events",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    objects = models.Manager.from_queryset(FbsPickScanEventQuerySet)()

    class Meta:
        db_table = "fbs_pick_scan_event"
        ordering = ["created_at", "id"]
        indexes = [
            models.Index(
                fields=["batch", "created_at"],
                name="fbs_scan_batch_created_idx",
            ),
            models.Index(
                fields=["allocation", "stage", "result"],
                name="fbs_scan_alloc_stage_idx",
            ),
        ]

    def clean(self) -> None:
        super().clean()
        if self.task_id and self.task.batch_id != self.batch_id:
            raise ValidationError({"task": "Задание относится к другой волне."})
        if self.allocation_id:
            if self.allocation.pick_task_id is None:
                raise ValidationError({"allocation": "Позиция не включена в волну."})
            if self.allocation.pick_task.batch_id != self.batch_id:
                raise ValidationError({"allocation": "Позиция относится к другой волне."})
            if self.task_id and self.allocation.pick_task_id != self.task_id:
                raise ValidationError({"allocation": "Позиция относится к другому заданию."})

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationError("Событие сканирования нельзя изменить.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Событие сканирования нельзя удалить.")


class FbsPickRestockRequest(models.Model):
    STATUS_WAITING_MARKETPLACE = "waiting_marketplace"
    STATUS_QUEUED = "queued"
    STATUS_IN_PROGRESS = "in_progress"
    STATUS_COMPLETED = "completed"
    STATUS_FAILED = "failed"
    STATUS_CANCELED = "canceled"
    STATUS_CHOICES = [
        (STATUS_WAITING_MARKETPLACE, "Проверяется маркетплейсом"),
        (STATUS_QUEUED, "Ожидает подборщика"),
        (STATUS_IN_PROGRESS, "Возвращается"),
        (STATUS_COMPLETED, "Возвращено"),
        (STATUS_FAILED, "Ошибка исключения"),
        (STATUS_CANCELED, "Отменено"),
    ]

    REASON_CLIENT_CANCELED = "client_canceled"
    REASON_DAMAGED = "damaged"
    REASON_WRONG_PRODUCT = "wrong_product"
    REASON_METADATA = "metadata_error"
    REASON_MARKETPLACE = "marketplace_error"
    REASON_OTHER = "other"
    REASON_CHOICES = [
        (REASON_CLIENT_CANCELED, "Отменен клиентом или покупателем"),
        (REASON_DAMAGED, "Товар поврежден"),
        (REASON_WRONG_PRODUCT, "Несоответствие товара"),
        (REASON_METADATA, "Ошибка КИЗ или срока годности"),
        (REASON_MARKETPLACE, "Ошибка маркетплейса"),
        (REASON_OTHER, "Другая причина"),
    ]

    MARKETPLACE_ACTION_NONE = "none"
    MARKETPLACE_ACTION_VERIFY_CANCEL = "verify_cancel"
    MARKETPLACE_ACTION_SELLER_CANCEL = "seller_cancel"
    MARKETPLACE_ACTION_CHOICES = [
        (MARKETPLACE_ACTION_NONE, "Не требуется"),
        (MARKETPLACE_ACTION_VERIFY_CANCEL, "Проверить отмену маркетплейса"),
        (MARKETPLACE_ACTION_SELLER_CANCEL, "Отменить продавцом"),
    ]

    batch = models.ForeignKey(
        FbsPickBatch,
        on_delete=models.PROTECT,
        related_name="pick_restock_request",
    )
    order = models.ForeignKey(
        FbsOrder,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="pick_restock_requests",
    )
    handover_assignment = models.ForeignKey(
        "FbsHandoverOrderAssignment",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="pick_restock_requests",
    )
    source_tote = models.ForeignKey(
        FbsPickingCart,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="pick_restock_requests",
    )
    quarantine_box = models.ForeignKey(
        FbsBox,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="quarantine_pick_restock_requests",
    )
    status = models.CharField(max_length=24, choices=STATUS_CHOICES, default=STATUS_QUEUED)
    reason_code = models.CharField(max_length=32, choices=REASON_CHOICES, blank=True)
    reason = models.TextField()
    marketplace_action = models.CharField(
        max_length=24,
        choices=MARKETPLACE_ACTION_CHOICES,
        default=MARKETPLACE_ACTION_NONE,
    )
    marketplace_confirmed_at = models.DateTimeField(null=True, blank=True)
    planned_qty = models.PositiveIntegerField()
    returned_qty = models.PositiveIntegerField(default=0)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_fbs_pick_restock_requests",
    )
    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="assigned_fbs_pick_restock_requests",
    )
    claimed_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    canceled_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "fbs_pick_restock_request"
        ordering = ["created_at", "id"]
        constraints = [
            models.CheckConstraint(
                condition=Q(planned_qty__gt=0),
                name="fbs_pick_restock_planned_gt_zero",
            ),
            models.CheckConstraint(
                condition=Q(planned_qty__gte=F("returned_qty")),
                name="fbs_pick_restock_returned_lte_planned",
            ),
            models.UniqueConstraint(
                fields=["order"],
                condition=Q(
                    order__isnull=False,
                    status__in=[
                        "waiting_marketplace",
                        "queued",
                        "in_progress",
                        "failed",
                    ],
                ),
                name="uniq_active_fbs_order_restock",
            ),
        ]
        indexes = [
            models.Index(
                fields=["status", "created_at"],
                name="fbs_restock_status_created_idx",
            ),
            models.Index(
                fields=["assigned_to", "status"],
                name="fbs_restock_actor_stat_idx",
            ),
        ]


class FbsPickRestockLine(models.Model):
    STATUS_PENDING = "pending"
    STATUS_IN_PROGRESS = "in_progress"
    STATUS_COMPLETED = "completed"
    STATUS_CANCELED = "canceled"
    STATUS_CHOICES = [
        (STATUS_PENDING, "Ожидает"),
        (STATUS_IN_PROGRESS, "Возвращается"),
        (STATUS_COMPLETED, "Возвращено"),
        (STATUS_CANCELED, "Отменено"),
    ]

    request = models.ForeignKey(
        FbsPickRestockRequest,
        on_delete=models.PROTECT,
        related_name="lines",
    )
    allocation = models.OneToOneField(
        FbsOrderStockAllocation,
        on_delete=models.PROTECT,
        related_name="pick_restock_line",
    )
    source_balance = models.ForeignKey(
        FbsStockBalance,
        on_delete=models.PROTECT,
        related_name="pick_restock_lines",
    )
    source_box = models.ForeignKey(
        FbsBox,
        on_delete=models.PROTECT,
        related_name="pick_restock_lines",
    )
    source_cell = models.ForeignKey(
        FbsStorageCell,
        on_delete=models.PROTECT,
        related_name="pick_restock_lines",
    )
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_PENDING)
    planned_qty = models.PositiveIntegerField()
    returned_qty = models.PositiveIntegerField(default=0)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "fbs_pick_restock_line"
        ordering = ["request_id", "source_cell_id", "source_box_id", "id"]
        constraints = [
            models.CheckConstraint(
                condition=Q(planned_qty__gt=0),
                name="fbs_pick_restock_line_planned_gt_zero",
            ),
            models.CheckConstraint(
                condition=Q(planned_qty__gte=F("returned_qty")),
                name="fbs_pick_restock_line_returned_lte_planned",
            ),
        ]
        indexes = [
            models.Index(
                fields=["request", "status"],
                name="fbs_restock_line_req_stat_idx",
            ),
            models.Index(
                fields=["source_box", "status"],
                name="fbs_restock_line_box_stat_idx",
            ),
        ]

    def clean(self) -> None:
        super().clean()
        if not self.allocation_id:
            return
        if self.source_balance_id != self.allocation.balance_id:
            raise ValidationError({"source_balance": "Остаток не совпадает с отбором."})
        if self.source_box_id != self.allocation.balance.box_id:
            raise ValidationError({"source_box": "Короб не совпадает с исходным отбором."})
        if self.source_cell_id != self.allocation.balance.box.pallet.cell_id:
            raise ValidationError({"source_cell": "Ячейка не совпадает с исходным отбором."})
        if self.allocation.pick_task.batch_id != self.request.batch_id:
            raise ValidationError({"allocation": "Позиция относится к другой волне."})


class FbsPickRestockScanQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ValidationError("События возврата отбора нельзя изменять.")

    def delete(self):
        raise ValidationError("События возврата отбора нельзя удалять.")


class FbsPickRestockScan(models.Model):
    STAGE_WORKSTATION = "workstation"
    STAGE_PICKUP_ITEM = "pickup_item"
    STAGE_CELL = "cell"
    STAGE_BOX = "box"
    STAGE_ITEM = "item"
    STAGE_CHOICES = [
        (STAGE_WORKSTATION, "Исходный рабочий стол"),
        (STAGE_PICKUP_ITEM, "Снятие товара со стола"),
        (STAGE_CELL, "Исходная ячейка"),
        (STAGE_BOX, "Исходный короб"),
        (STAGE_ITEM, "Возврат товара"),
    ]
    RESULT_SUCCESS = "success"
    RESULT_ERROR = "error"
    RESULT_CHOICES = [
        (RESULT_SUCCESS, "Успешно"),
        (RESULT_ERROR, "Ошибка"),
    ]

    request = models.ForeignKey(
        FbsPickRestockRequest,
        on_delete=models.PROTECT,
        related_name="scans",
    )
    line = models.ForeignKey(
        FbsPickRestockLine,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="scans",
    )
    stage = models.CharField(max_length=16, choices=STAGE_CHOICES)
    result = models.CharField(max_length=16, choices=RESULT_CHOICES)
    scan_value = models.CharField(max_length=512, blank=True)
    expected_value = models.CharField(max_length=512, blank=True)
    quantity_after = models.PositiveIntegerField(null=True, blank=True)
    message = models.TextField(blank=True)
    request_token = models.UUIDField(null=True, blank=True, unique=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="fbs_pick_restock_scans",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    objects = models.Manager.from_queryset(FbsPickRestockScanQuerySet)()

    class Meta:
        db_table = "fbs_pick_restock_scan"
        ordering = ["created_at", "id"]
        indexes = [
            models.Index(
                fields=["request", "created_at"],
                name="fbs_restock_scan_req_time_idx",
            ),
            models.Index(
                fields=["line", "stage", "result"],
                name="fbs_restock_scan_line_idx",
            ),
        ]

    def clean(self) -> None:
        super().clean()
        if self.line_id and self.line.request_id != self.request_id:
            raise ValidationError({"line": "Строка относится к другому заданию возврата."})

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationError("Событие возврата отбора нельзя изменить.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Событие возврата отбора нельзя удалить.")


class FbsInventorySession(models.Model):
    @property
    def workflow_target_label(self):
        if self.box_id:
            return self.box.box_code
        if self.pallet_id:
            return self.pallet.pallet_code
        return self.get_scope_type_display()

    @property
    def workflow_place_label(self):
        cell = self.box.pallet.cell if self.box_id else self.pallet.cell if self.pallet_id else self.cell
        return cell.warehouse_location_label if cell else ""

    @property
    def workflow_agency_label(self):
        return str(self.agency or (self.box.agency if self.box_id else self.pallet.agency if self.pallet_id else ""))

    @property
    def workflow_assignee_label(self):
        user = self.recount_assigned_to if self.status == "recount" else self.assigned_to
        if user is None:
            return "Не назначен"
        employee = getattr(user, "employee_profile", None)
        return employee.full_name if employee else user.get_full_name() or user.username

    managed_workflow = models.BooleanField(default=False)
    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True,
        related_name="assigned_fbs_inventories",
    )
    recount_assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True,
        related_name="assigned_fbs_inventory_recounts",
    )
    count_started_at = models.DateTimeField(null=True, blank=True)
    recount_started_at = models.DateTimeField(null=True, blank=True)
    SCOPE_ALL = "all"
    SCOPE_AGENCY = "agency"
    SCOPE_CELL = "cell"
    SCOPE_PALLET = "pallet"
    SCOPE_BOX = "box"
    SCOPE_SKU = "sku"
    SCOPE_CHOICES = [
        (SCOPE_ALL, "Весь FBS"),
        (SCOPE_AGENCY, "Клиент"),
        (SCOPE_CELL, "Ячейка"),
        (SCOPE_PALLET, "Паллета"),
        (SCOPE_BOX, "Короб"),
        (SCOPE_SKU, "SKU клиента"),
    ]
    MODE_AUDIT = "audit"
    MODE_DRAIN = "drain"
    MODE_IMMEDIATE = "immediate"
    MODE_CHOICES = [
        (MODE_AUDIT, "Аудит без блокировки"),
        (MODE_DRAIN, "После завершения активных операций"),
        (MODE_IMMEDIATE, "Немедленная блокировка"),
    ]
    SCAN_MODE_KIZ = "kiz"
    SCAN_MODE_BARCODE = "barcode"
    SCAN_MODE_CHOICES = [
        (SCAN_MODE_KIZ, "С КИЗами"),
        (SCAN_MODE_BARCODE, "Без КИЗов"),
    ]
    STATUS_PLANNED = "planned"
    STATUS_DRAINING = "draining"
    STATUS_COUNTING = "counting"
    STATUS_RECOUNT = "recount"
    STATUS_APPROVAL = "approval"
    STATUS_DONE = "done"
    STATUS_CANCELED = "canceled"
    STATUS_CHOICES = [
        (STATUS_PLANNED, "Запланирована"),
        (STATUS_DRAINING, "Ожидает завершения операций"),
        (STATUS_COUNTING, "Первый пересчет"),
        (STATUS_RECOUNT, "Повторный пересчет"),
        (STATUS_APPROVAL, "Ожидает утверждения"),
        (STATUS_DONE, "Завершена"),
        (STATUS_CANCELED, "Отменена"),
    ]

    scope_type = models.CharField(max_length=16, choices=SCOPE_CHOICES)
    mode = models.CharField(max_length=16, choices=MODE_CHOICES, default=MODE_DRAIN)
    scan_mode = models.CharField(
        max_length=16,
        choices=SCAN_MODE_CHOICES,
        default=SCAN_MODE_BARCODE,
    )
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_PLANNED)
    agency = models.ForeignKey(
        Agency,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="fbs_inventory_sessions",
    )
    cell = models.ForeignKey(
        FbsStorageCell,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="inventory_sessions",
    )
    pallet = models.ForeignKey(
        FbsPallet,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="inventory_sessions",
    )
    box = models.ForeignKey(
        FbsBox,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="inventory_sessions",
    )
    sku = models.ForeignKey(
        SKU,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="fbs_inventory_sessions",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_fbs_inventory_sessions",
    )
    first_counter = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="first_fbs_inventory_sessions",
    )
    second_counter = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="second_fbs_inventory_sessions",
    )
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="approved_fbs_inventory_sessions",
    )
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "fbs_inventory_session"
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["status", "created_at"]),
            models.Index(fields=["agency", "status"]),
            models.Index(fields=["cell", "status"]),
        ]

    def clean(self) -> None:
        super().clean()
        required_field = {
            self.SCOPE_AGENCY: "agency_id",
            self.SCOPE_CELL: "cell_id",
            self.SCOPE_PALLET: "pallet_id",
            self.SCOPE_BOX: "box_id",
            self.SCOPE_SKU: "sku_id",
        }.get(self.scope_type)
        if required_field and not getattr(self, required_field):
            raise ValidationError({required_field[:-3]: "Не заполнен объект инвентаризации."})
        if self.scope_type == self.SCOPE_SKU and not self.agency_id:
            raise ValidationError({"agency": "Для инвентаризации SKU укажите клиента."})


class FbsInventoryWorkEvent(models.Model):
    @property
    def action_label(self):
        return {
            "shortage_reported": "Сообщение о недостаче", "assigned": "Назначен исполнитель",
            "count_started": "Подтвержден адрес, начат пересчет", "count_finished": "Пересчет завершен",
            "first_count_confirmed": "Первый пересчет подтвержден ответственным сотрудником",
            "approved": "Результат утвержден", "reservation_released": "Освобожден неподобранный резерв",
            "order_followup": "Проверен повторный резерв", "order_review_required": "Требуется разбор заказа",
        }.get(self.action, self.action)

    session = models.ForeignKey(FbsInventorySession, on_delete=models.PROTECT, related_name="work_events")
    action = models.CharField(max_length=40)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True)
    payload = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "fbs_inventory_work_event"
        ordering = ["created_at", "id"]


class FbsStorageLock(models.Model):
    session = models.OneToOneField(
        FbsInventorySession,
        on_delete=models.CASCADE,
        related_name="storage_lock",
    )
    scope_type = models.CharField(max_length=16, choices=FbsInventorySession.SCOPE_CHOICES)
    agency = models.ForeignKey(Agency, on_delete=models.CASCADE, null=True, blank=True)
    cell = models.ForeignKey(FbsStorageCell, on_delete=models.CASCADE, null=True, blank=True)
    pallet = models.ForeignKey(FbsPallet, on_delete=models.CASCADE, null=True, blank=True)
    box = models.ForeignKey(FbsBox, on_delete=models.CASCADE, null=True, blank=True)
    sku = models.ForeignKey(SKU, on_delete=models.CASCADE, null=True, blank=True)
    block_new_reservations = models.BooleanField(default=True)
    block_execution = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    released_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "fbs_storage_lock"
        indexes = [
            models.Index(fields=["is_active", "block_new_reservations"]),
            models.Index(fields=["is_active", "block_execution"]),
            models.Index(fields=["agency", "is_active"]),
            models.Index(fields=["cell", "is_active"]),
            models.Index(fields=["pallet", "is_active"]),
            models.Index(fields=["box", "is_active"]),
            models.Index(fields=["sku", "is_active"]),
        ]


class FbsInventoryLine(models.Model):
    protected_qty = models.PositiveIntegerField(default=0)
    session = models.ForeignKey(
        FbsInventorySession,
        on_delete=models.CASCADE,
        related_name="lines",
    )
    balance = models.ForeignKey(
        FbsStockBalance,
        on_delete=models.PROTECT,
        related_name="inventory_lines",
    )
    expected_qty = models.PositiveIntegerField()
    first_count_qty = models.PositiveIntegerField(null=True, blank=True)
    second_count_qty = models.PositiveIntegerField(null=True, blank=True)
    final_qty = models.PositiveIntegerField(null=True, blank=True)
    approved_delta = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "fbs_inventory_line"
        ordering = ["balance__box_id", "balance_id"]
        constraints = [
            models.UniqueConstraint(
                fields=["session", "balance"],
                name="uniq_fbs_inventory_session_balance",
            )
        ]
        indexes = [models.Index(fields=["session", "balance"])]


class FbsInventoryScan(models.Model):
    ROUND_FIRST = 1
    ROUND_SECOND = 2
    ROUND_CHOICES = [(ROUND_FIRST, "Первый"), (ROUND_SECOND, "Повторный")]

    line = models.ForeignKey(FbsInventoryLine, on_delete=models.CASCADE, related_name="scans")
    count_round = models.PositiveSmallIntegerField(choices=ROUND_CHOICES)
    scan_code = models.CharField(max_length=256)
    qty = models.PositiveIntegerField(default=1)
    counted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="fbs_inventory_scans",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "fbs_inventory_scan"
        ordering = ["created_at", "id"]
        constraints = [
            models.CheckConstraint(condition=Q(qty__gt=0), name="fbs_inventory_scan_qty_gt_zero")
        ]
        indexes = [
            models.Index(fields=["line", "count_round"]),
            models.Index(fields=["scan_code"]),
        ]


class FbsClientMovementRequest(models.Model):
    MODE_BOX = "box"
    MODE_ITEM = "item"
    MODE_CHOICES = [
        (MODE_BOX, "Коробами"),
        (MODE_ITEM, "Поштучно"),
    ]

    STATUS_SUBMITTED = "submitted"
    STATUS_APPROVED = "approved"
    STATUS_WAREHOUSE_ACCEPTED = "warehouse_accepted"
    STATUS_IN_PROGRESS = "in_progress"
    STATUS_MOVED = "moved"
    STATUS_AWAITING_MANAGER_CONFIRMATION = "awaiting_manager_confirmation"
    STATUS_NEEDS_CLARIFICATION = "needs_clarification"
    STATUS_COMPLETED = "completed"
    STATUS_REJECTED = "rejected"
    STATUS_CANCELED = "canceled"
    STATUS_CHOICES = [
        (STATUS_SUBMITTED, "Ожидает менеджера"),
        (STATUS_APPROVED, "Передана на склад"),
        (STATUS_WAREHOUSE_ACCEPTED, "Принята складом"),
        (STATUS_IN_PROGRESS, "В работе"),
        (STATUS_MOVED, "Перемещена"),
        (STATUS_AWAITING_MANAGER_CONFIRMATION, "Ожидает подтверждения менеджером"),
        (STATUS_NEEDS_CLARIFICATION, "Требует уточнения"),
        (STATUS_COMPLETED, "Выполнена"),
        (STATUS_REJECTED, "Отклонена"),
        (STATUS_CANCELED, "Отменена"),
    ]

    agency = models.ForeignKey(
        Agency,
        on_delete=models.PROTECT,
        related_name="fbs_client_movement_requests",
    )
    mode = models.CharField(max_length=16, choices=MODE_CHOICES)
    status = models.CharField(max_length=32, choices=STATUS_CHOICES, default=STATUS_SUBMITTED)
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_fbs_client_movement_requests",
    )
    source_file_name = models.CharField(max_length=255, blank=True)
    idempotency_key = models.CharField(max_length=64, blank=True, default="")
    comment = models.TextField(blank=True)
    requested_qty = models.PositiveIntegerField(default=0)
    requested_box_count = models.PositiveIntegerField(default=0)
    requested_mixed_box_count = models.PositiveIntegerField(default=0)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reviewed_fbs_client_movement_requests",
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    warehouse_accepted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="accepted_fbs_client_movement_requests",
    )
    warehouse_accepted_at = models.DateTimeField(null=True, blank=True)
    warehouse_confirmed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="warehouse_confirmed_fbs_client_movement_requests",
    )
    warehouse_confirmed_at = models.DateTimeField(null=True, blank=True)
    manager_confirmed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="manager_confirmed_fbs_client_movement_requests",
    )
    manager_confirmed_at = models.DateTimeField(null=True, blank=True)
    actual_moved_qty = models.PositiveIntegerField(default=0)
    actual_moved_box_count = models.PositiveIntegerField(default=0)
    clarification_reason = models.TextField(blank=True)
    billing_synced_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "fbs_client_movement_request"
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["agency", "status", "created_at"]),
            models.Index(fields=["status", "created_at"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["agency", "idempotency_key"],
                condition=~Q(idempotency_key=""),
                name="uniq_fbs_mov_agency_idempotency",
            ),
            models.CheckConstraint(
                condition=Q(requested_mixed_box_count__lte=F("requested_box_count")),
                name="fbs_mov_mix_lte_boxes",
            ),
        ]

    def clean(self) -> None:
        super().clean()
        if self.requested_mixed_box_count > self.requested_box_count:
            raise ValidationError(
                {
                    "requested_mixed_box_count": (
                        "Количество микс-коробов не может превышать общее количество коробов."
                    )
                }
            )
        if (
            self.mode == self.MODE_ITEM
            and self.requested_box_count > self.requested_qty
        ):
            raise ValidationError(
                {
                    "requested_box_count": (
                        "Количество физических коробов не может превышать количество штук."
                    )
                }
            )

    @property
    def number(self) -> str:
        return f"FBS-MOV-{self.pk:06d}" if self.pk else "FBS-MOV"

    @property
    def uses_hard_reserve(self) -> bool:
        return bool(str(self.idempotency_key or "").strip())


class FbsClientMovementRequestLine(models.Model):
    request = models.ForeignKey(
        FbsClientMovementRequest,
        on_delete=models.CASCADE,
        related_name="lines",
    )
    sku = models.ForeignKey(
        SKU,
        on_delete=models.PROTECT,
        related_name="fbs_client_movement_request_lines",
    )
    barcode = models.CharField(max_length=64)
    sku_code = models.CharField(max_length=64)
    product_name = models.CharField(max_length=255, blank=True)
    requested_qty = models.PositiveIntegerField()
    units_per_box = models.PositiveIntegerField(default=1)
    requested_box_count = models.PositiveIntegerField(default=0)
    general_available_qty_snapshot = models.PositiveIntegerField(default=0)
    fbs_available_qty_snapshot = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "fbs_client_movement_request_line"
        ordering = ["request_id", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["request", "barcode"],
                name="uniq_fbs_client_movement_request_barcode",
            ),
            models.CheckConstraint(
                condition=Q(requested_qty__gt=0),
                name="fbs_client_movement_requested_qty_gt_zero",
            ),
            models.CheckConstraint(
                condition=Q(units_per_box__gt=0),
                name="fbs_client_movement_units_per_box_gt_zero",
            ),
        ]
        indexes = [
            models.Index(fields=["request", "barcode"]),
            models.Index(fields=["sku", "created_at"]),
        ]

    def clean(self) -> None:
        super().clean()
        if self.sku_id and self.sku.agency_id not in {None, self.request.agency_id}:
            raise ValidationError({"sku": "SKU принадлежит другому клиенту."})
        if self.request.mode == FbsClientMovementRequest.MODE_ITEM:
            if self.units_per_box != 1:
                raise ValidationError(
                    {"units_per_box": "Для поштучной заявки кратность должна быть равна 1."}
                )
            if self.requested_box_count > self.requested_qty:
                raise ValidationError(
                    {
                        "requested_box_count": (
                            "Количество коробов не может превышать количество штук."
                        )
                    }
                )
        elif self.requested_qty % self.units_per_box != 0:
            raise ValidationError(
                {"requested_qty": "Количество должно быть кратно количеству штук в коробе."}
            )


class FbsInternalMovement(models.Model):
    MODE_BOX = "box"
    MODE_ITEM = "item"
    MODE_CHOICES = [(MODE_BOX, "Короб"), (MODE_ITEM, "Штучно")]
    STATUS_PROPOSED = "proposed"
    STATUS_IN_PROGRESS = "in_progress"
    STATUS_DONE = "done"
    STATUS_BLOCKED = "blocked"
    STATUS_CANCELED = "canceled"
    STATUS_CHOICES = [
        (STATUS_PROPOSED, "Предложено"),
        (STATUS_IN_PROGRESS, "В работе"),
        (STATUS_DONE, "Выполнено"),
        (STATUS_BLOCKED, "Заблокировано"),
        (STATUS_CANCELED, "Отменено"),
    ]

    agency = models.ForeignKey(Agency, on_delete=models.PROTECT, related_name="fbs_movements")
    mode = models.CharField(max_length=16, choices=MODE_CHOICES)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_PROPOSED)
    source_box = models.ForeignKey(
        FbsBox,
        on_delete=models.PROTECT,
        related_name="outgoing_fbs_movements",
    )
    target_pallet = models.ForeignKey(
        FbsPallet,
        on_delete=models.PROTECT,
        related_name="incoming_fbs_movements",
    )
    target_box = models.ForeignKey(
        FbsBox,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="incoming_item_fbs_movements",
    )
    source_balance = models.ForeignKey(
        FbsStockBalance,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="internal_movements",
    )
    planned_qty = models.PositiveIntegerField(default=0)
    moved_qty = models.PositiveIntegerField(default=0)
    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="assigned_fbs_internal_movements",
    )
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="requested_fbs_internal_movements",
    )
    comment = models.TextField(blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "fbs_internal_movement"
        ordering = ["created_at", "id"]
        constraints = [
            models.CheckConstraint(
                condition=Q(planned_qty__gte=F("moved_qty")),
                name="fbs_internal_movement_moved_lte_planned",
            )
        ]
        indexes = [
            models.Index(fields=["agency", "status"]),
            models.Index(fields=["assigned_to", "status"]),
            models.Index(fields=["source_box", "status"]),
        ]

    def clean(self) -> None:
        super().clean()
        if self.source_box_id and self.source_box.agency_id != self.agency_id:
            raise ValidationError({"source_box": "Исходный короб принадлежит другому клиенту."})
        if self.target_pallet_id and self.target_pallet.agency_id != self.agency_id:
            raise ValidationError({"target_pallet": "Паллета назначения принадлежит другому клиенту."})
        if self.target_box_id and self.target_box.agency_id != self.agency_id:
            raise ValidationError({"target_box": "Короб назначения принадлежит другому клиенту."})
        if self.mode == self.MODE_ITEM and not self.source_balance_id:
            raise ValidationError({"source_balance": "Для штучного перемещения нужен остаток."})
        if self.mode == self.MODE_ITEM and not self.target_box_id:
            raise ValidationError({"target_box": "Для штучного перемещения нужен короб назначения."})


class FbsHandoverBatch(models.Model):
    STATUS_OPEN = "open"
    STATUS_READY = "ready"
    STATUS_DISPATCHED = "dispatched"
    STATUS_ACCEPTED = "accepted"
    STATUS_PROBLEM = "problem"
    STATUS_ARCHIVED = "archived"
    STATUS_CHOICES = [
        (STATUS_OPEN, "Сканирование"),
        (STATUS_READY, "Готова к отгрузке"),
        (STATUS_DISPATCHED, "Передана водителю"),
        (STATUS_ACCEPTED, "Принята маркетплейсом"),
        (STATUS_PROBLEM, "Проблема"),
        (STATUS_ARCHIVED, "Архив"),
    ]

    MARKETPLACE_DRAFT = "draft"
    MARKETPLACE_CREATING = "creating"
    MARKETPLACE_OPEN = "open"
    MARKETPLACE_DELIVERY_PENDING = "delivery_pending"
    MARKETPLACE_COMPLETE = "complete"
    MARKETPLACE_ERROR = "error"
    MARKETPLACE_STATE_CHOICES = [
        (MARKETPLACE_DRAFT, "Черновик Fullbox"),
        (MARKETPLACE_CREATING, "Создается на маркетплейсе"),
        (MARKETPLACE_OPEN, "Открыта на маркетплейсе"),
        (MARKETPLACE_DELIVERY_PENDING, "Передается в доставку"),
        (MARKETPLACE_COMPLETE, "Передана в доставку"),
        (MARKETPLACE_ERROR, "Ошибка маркетплейса"),
    ]

    profile = models.ForeignKey(
        FbsIntegrationProfile,
        on_delete=models.PROTECT,
        related_name="handover_batches",
    )
    external_supply_id = models.CharField(max_length=128, blank=True)
    external_name = models.CharField(max_length=128, blank=True)
    compatibility_key = models.CharField(max_length=255, blank=True)
    marketplace_state = models.CharField(
        max_length=24,
        choices=MARKETPLACE_STATE_CHOICES,
        default=MARKETPLACE_DRAFT,
    )
    marketplace_payload = models.JSONField(default=dict, blank=True)
    supply_qr_code = models.CharField(max_length=512, blank=True)
    supply_label_file = models.FileField(
        upload_to="handover-labels/%Y/%m/%d/",
        storage=fbs_label_storage,
        blank=True,
    )
    supply_label_format = models.CharField(max_length=16, blank=True)
    supply_label_hash = models.CharField(max_length=64, blank=True)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_OPEN)
    dispatch_location = models.ForeignKey(
        "sklad.WarehouseLocation",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="fbs_handover_batches",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_fbs_handover_batches",
    )
    dispatched_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="dispatched_fbs_handover_batches",
    )
    dispatched_at = models.DateTimeField(null=True, blank=True)
    accepted_at = models.DateTimeField(null=True, blank=True)
    archived_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="archived_fbs_handover_batches",
    )
    archived_at = models.DateTimeField(null=True, blank=True)
    archive_reason = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "fbs_handover_batch"
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["profile", "status"]),
            models.Index(fields=["status", "created_at"]),
            models.Index(fields=["profile", "marketplace_state"]),
            models.Index(
                fields=["dispatch_location", "status"],
                name="fbs_hand_dispatch_status_idx",
            ),
        ]


class FbsHandoverBox(models.Model):
    STATUS_OPEN = "open"
    STATUS_CLOSED = "closed"
    STATUS_SCANNED = "scanned"
    STATUS_DISPATCHED = "dispatched"
    STATUS_ACCEPTED = "accepted"
    STATUS_PROBLEM = "problem"
    STATUS_CHOICES = [
        (STATUS_OPEN, "Открыт"),
        (STATUS_CLOSED, "Закрыт"),
        (STATUS_SCANNED, "Просканирован кладовщиком"),
        (STATUS_DISPATCHED, "Передан водителю"),
        (STATUS_ACCEPTED, "Принят маркетплейсом"),
        (STATUS_PROBLEM, "Проблема"),
    ]

    batch = models.ForeignKey(FbsHandoverBatch, on_delete=models.PROTECT, related_name="boxes")
    qr_code = models.CharField(max_length=256, unique=True)
    external_box_id = models.CharField(max_length=128, blank=True)
    label_file = models.FileField(
        upload_to="handover-box-labels/%Y/%m/%d/",
        storage=fbs_label_storage,
        blank=True,
    )
    label_format = models.CharField(max_length=16, blank=True)
    label_hash = models.CharField(max_length=64, blank=True)
    label_ready_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_OPEN)
    scanned_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="scanned_fbs_handover_boxes",
    )
    scanned_at = models.DateTimeField(null=True, blank=True)
    accepted_at = models.DateTimeField(null=True, blank=True)
    problem_reason = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "fbs_handover_box"
        ordering = ["batch_id", "id"]
        indexes = [models.Index(fields=["batch", "status"])]


class FbsHandoverOrderAssignment(models.Model):
    STATUS_PENDING = "pending"
    STATUS_CONFIRMED = "confirmed"
    STATUS_ERROR = "error"
    STATUS_CANCELED = "canceled"
    STATUS_CHOICES = [
        (STATUS_PENDING, "Добавляется в поставку"),
        (STATUS_CONFIRMED, "Добавлен в поставку"),
        (STATUS_ERROR, "Ошибка добавления"),
        (STATUS_CANCELED, "Отменено"),
    ]

    batch = models.ForeignKey(
        FbsHandoverBatch,
        on_delete=models.PROTECT,
        related_name="order_assignments",
    )
    order = models.OneToOneField(
        FbsOrder,
        on_delete=models.PROTECT,
        related_name="handover_assignment",
    )
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_PENDING)
    error = models.TextField(blank=True)
    assigned_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="assigned_fbs_handover_orders",
    )
    confirmed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "fbs_handover_order_assignment"
        ordering = ["batch_id", "id"]
        indexes = [models.Index(fields=["batch", "status"])]

    def clean(self) -> None:
        super().clean()
        if self.batch_id and self.order_id and self.batch.profile_id != self.order.profile_id:
            raise ValidationError({"order": "Заказ относится к другому кабинету маркетплейса."})


class FbsHandoverOrder(models.Model):
    STATUS_ACTIVE = "active"
    STATUS_RETURN_PENDING = "return_pending"
    STATUS_EXCLUDED = "excluded"
    STATUS_CHOICES = [
        (STATUS_ACTIVE, "В отгрузке"),
        (STATUS_RETURN_PENDING, "Ожидает возврата"),
        (STATUS_EXCLUDED, "Исключен"),
    ]

    box = models.ForeignKey(FbsHandoverBox, on_delete=models.PROTECT, related_name="orders")
    order = models.OneToOneField(
        FbsOrder,
        on_delete=models.PROTECT,
        related_name="handover_order",
    )
    added_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="added_fbs_handover_orders",
    )
    added_at = models.DateTimeField(auto_now_add=True)
    status = models.CharField(
        max_length=16,
        choices=STATUS_CHOICES,
        default=STATUS_ACTIVE,
    )
    exclusion_reason = models.TextField(blank=True)
    excluded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="excluded_fbs_handover_orders",
    )
    excluded_at = models.DateTimeField(null=True, blank=True)
    verified_label = models.ForeignKey(
        FbsOrderLabel,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="verified_fbs_handover_orders",
    )
    verified_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="verified_fbs_handover_orders",
    )
    verified_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "fbs_handover_order"
        ordering = ["box_id", "id"]
        indexes = [
            models.Index(fields=["box", "status"], name="fbs_handov_box_status_idx")
        ]

    def clean(self) -> None:
        super().clean()
        if self.box_id and self.order_id and self.box.batch.profile_id != self.order.profile_id:
            raise ValidationError({"order": "Заказ относится к другому кабинету маркетплейса."})


class FbsHandoverVerificationOverride(models.Model):
    batch = models.ForeignKey(
        FbsHandoverBatch,
        on_delete=models.PROTECT,
        related_name="verification_overrides",
    )
    reason = models.TextField()
    snapshot = models.JSONField(default=dict)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="approved_fbs_handover_verification_overrides",
    )
    is_active = models.BooleanField(default=True)
    approved_at = models.DateTimeField(auto_now_add=True)
    used_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="used_fbs_handover_verification_overrides",
    )
    used_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "fbs_handover_verification_override"
        ordering = ["-approved_at", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["batch"],
                condition=Q(is_active=True),
                name="uniq_active_fbs_handover_verify_override",
            )
        ]
        indexes = [
            models.Index(
                fields=["batch", "is_active"],
                name="fbs_hov_batch_active_idx",
            ),
            models.Index(fields=["approved_at"], name="fbs_hov_approved_at_idx"),
        ]

    def clean(self) -> None:
        super().clean()
        if not str(self.reason or "").strip():
            raise ValidationError({"reason": "Укажите причину аварийной отгрузки."})


class FbsStorageDailyUsage(models.Model):
    usage_date = models.DateField()
    agency = models.ForeignKey(
        Agency,
        on_delete=models.PROTECT,
        related_name="fbs_storage_daily_usage",
    )
    billing_mode = models.CharField(max_length=16, choices=FbsClientStoragePolicy.BILLING_CHOICES)
    sku = models.ForeignKey(
        SKU,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="fbs_storage_daily_usage",
    )
    pallet = models.ForeignKey(
        FbsPallet,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="daily_usage",
    )
    quantity = models.PositiveIntegerField(default=0)
    volume_liters = models.DecimalField(max_digits=16, decimal_places=3, default=0)
    pallet_places = models.DecimalField(max_digits=10, decimal_places=3, default=0)
    dimensions_complete = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "fbs_storage_daily_usage"
        ordering = ["-usage_date", "agency_id", "sku_id", "pallet_id"]
        constraints = [
            models.UniqueConstraint(
                fields=["usage_date", "agency", "billing_mode", "sku", "pallet"],
                name="uniq_fbs_daily_storage_usage",
            )
        ]
        indexes = [
            models.Index(fields=["usage_date", "agency"]),
            models.Index(fields=["agency", "billing_mode", "usage_date"]),
        ]


class FbsComplianceOverride(models.Model):
    order = models.ForeignKey(
        FbsOrder,
        on_delete=models.PROTECT,
        related_name="compliance_overrides",
    )
    metadata_type = models.CharField(
        max_length=32,
        choices=FbsMarketplaceMetadataTransfer.TYPE_CHOICES,
    )
    reason = models.TextField()
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="approved_fbs_compliance_overrides",
    )
    is_active = models.BooleanField(default=True)
    approved_at = models.DateTimeField(auto_now_add=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "fbs_compliance_override"
        ordering = ["-approved_at", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["order", "metadata_type"],
                condition=Q(is_active=True),
                name="uniq_active_fbs_compliance_override",
            )
        ]
        indexes = [
            models.Index(fields=["order", "is_active"]),
            models.Index(fields=["metadata_type", "is_active"]),
        ]

    def clean(self) -> None:
        super().clean()
        if not str(self.reason or "").strip():
            raise ValidationError({"reason": "Укажите причину разрешения отгрузки."})


class FbsExternalIssue(models.Model):
    """External custody handoff, independent of marketplace orders."""
    agency = models.ForeignKey(Agency, on_delete=models.PROTECT)
    reference = models.CharField(max_length=128)
    recipient = models.CharField(max_length=255)
    purpose = models.CharField(max_length=20, choices=[("owner", "Возврат владельцу"), ("external", "Сторонняя отгрузка")])
    basis = models.TextField()
    shipping_details = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=16, default="reserved", choices=[("reserved", "К отбору"), ("picking", "В отборе"), ("completed", "Выдано"), ("canceled", "Отменено")])
    request_key = models.UUIDField(unique=True)
    request_hash = models.CharField(max_length=64)
    historical = models.BooleanField(default=False)
    occurred_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="fbs_external_issues_created")
    completed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="fbs_external_issues_completed", null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    class Meta:
        ordering = ["-id"]
        constraints = [models.UniqueConstraint(fields=["agency", "reference"], name="fbs_external_issue_reference")]
    @property
    def number(self):
        return f"FBS-OUT-{self.pk:06d}"


class FbsExternalIssueLine(models.Model):
    issue = models.ForeignKey(FbsExternalIssue, on_delete=models.PROTECT, related_name="lines")
    balance = models.ForeignKey(FbsStockBalance, on_delete=models.PROTECT, related_name="external_issue_lines")
    item_snapshot = models.JSONField(default=dict)
    requested_qty = models.PositiveIntegerField()
    picked_qty = models.PositiveIntegerField(default=0)
    returned_qty = models.PositiveIntegerField(default=0)
    shipped_qty = models.PositiveIntegerField(default=0)
    class Meta:
        ordering = ["id"]
        constraints = [
            models.UniqueConstraint(fields=["issue", "balance"], name="fbs_external_line_balance"),
            models.CheckConstraint(condition=Q(requested_qty__gt=0) & Q(requested_qty__gte=F("picked_qty")) & Q(picked_qty__gte=F("returned_qty")) & Q(shipped_qty__lte=F("picked_qty") - F("returned_qty")), name="fbs_external_line_quantities"),
        ]
    @property
    def in_hand_qty(self):
        return self.picked_qty - self.returned_qty - self.shipped_qty
    @property
    def unpicked_qty(self):
        return self.requested_qty - self.picked_qty


class FbsExternalIssueEvent(models.Model):
    issue = models.ForeignKey(FbsExternalIssue, on_delete=models.PROTECT, related_name="events")
    request_key = models.UUIDField(unique=True)
    request_hash = models.CharField(max_length=64)
    action = models.CharField(max_length=24)
    payload = models.JSONField(default=dict)
    performed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)
    class Meta:
        ordering = ["id"]


class FbsStorekeeperResponsible(models.Model):
    """Employees who receive mandatory FBS control alerts."""

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="fbs_storekeeper_responsibility",
    )
    is_active = models.BooleanField(default=True)
    assigned_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="assigned_fbs_storekeeper_responsibilities",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "fbs_storekeeper_responsible"
        ordering = ["user_id"]


class FbsStorekeeperAlertAcknowledgement(models.Model):
    """Shared claim of one still-active FBS alert case."""

    alert_key = models.CharField(max_length=128, unique=True)
    alert_kind = models.CharField(max_length=32)
    responsible = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="acknowledged_fbs_storekeeper_alerts",
    )
    acknowledged_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "fbs_storekeeper_alert_ack"
        ordering = ["-acknowledged_at", "-id"]
        indexes = [
            models.Index(
                fields=["alert_kind", "acknowledged_at"],
                name="fbs_store_alert_kind_ack_idx",
            )
        ]
