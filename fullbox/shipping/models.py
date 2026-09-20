import os
import re
from datetime import timedelta

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

from sku.models import Agency, Market, SKU


_ATTACHMENT_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def shipping_attachment_upload_to(instance, filename: str) -> str:
    original = os.path.basename(str(filename or "file"))
    stem, ext = os.path.splitext(original)
    safe_stem = _ATTACHMENT_FILENAME_RE.sub("_", stem).strip("._") or "file"
    safe_ext = _ATTACHMENT_FILENAME_RE.sub("", ext).lower()[:16]
    order_number = _ATTACHMENT_FILENAME_RE.sub("_", str(getattr(instance.order, "number", "") or "shipping"))
    period = timezone.now().strftime("%Y/%m")
    return f"shipping_attachments/{order_number}/{period}/{safe_stem}{safe_ext}"


class ShippingOrder(models.Model):
    STATUS_DRAFT = "draft"
    STATUS_SUBMITTED = "submitted"
    STATUS_RESERVED = "reserved"
    STATUS_STOREKEEPER_ACCEPTED = "storekeeper_accepted"
    STATUS_PICKING = "picking"
    STATUS_PACKED = "packed"
    STATUS_SHIPPED = "shipped"
    STATUS_PARTIAL = "partial_shipped"
    STATUS_CANCELED = "canceled"
    STATUS_CHOICES = [
        (STATUS_DRAFT, "Черновик"),
        (STATUS_SUBMITTED, "Новая"),
        (STATUS_RESERVED, "Зарезервирована"),
        (STATUS_STOREKEEPER_ACCEPTED, "Принята в работу складом"),
        (STATUS_PICKING, "В отборе"),
        (STATUS_PACKED, "Упакована"),
        (STATUS_SHIPPED, "Отгружена"),
        (STATUS_PARTIAL, "Отгружена частично"),
        (STATUS_CANCELED, "Отменена"),
    ]

    DELIVERY_MARKETPLACE = "marketplace"
    DELIVERY_COURIER = "courier"
    DELIVERY_PICKUP = "pickup"
    DELIVERY_TRANSFER = "transfer"
    DELIVERY_OTHER = "other"
    DELIVERY_TYPE_CHOICES = [
        (DELIVERY_MARKETPLACE, "Маркетплейс"),
        (DELIVERY_COURIER, "Курьер"),
        (DELIVERY_PICKUP, "Самовывоз"),
        (DELIVERY_TRANSFER, "Перемещение"),
        (DELIVERY_OTHER, "Другое"),
    ]
    VEHICLE_FULFILLMENT = "fulfillment"
    VEHICLE_CLIENT = "client"
    VEHICLE_CDEK = "cdek"
    VEHICLE_YANDEX = "yandex"
    VEHICLE_TYPE_CHOICES = [
        (VEHICLE_FULFILLMENT, "Транспорт ФуллБокс"),
        (VEHICLE_CLIENT, "Транспорт Клиента"),
        (VEHICLE_CDEK, "СДЭК"),
        (VEHICLE_YANDEX, "Яндекс"),
    ]
    SUPPLY_BOX = "box"
    SUPPLY_MONOPALLET = "monopallet"
    SUPPLY_SUPERSAFE = "supersafe"
    SUPPLY_TYPE_CHOICES = [
        (SUPPLY_BOX, "Короб"),
        (SUPPLY_MONOPALLET, "Монопаллет"),
        (SUPPLY_SUPERSAFE, "Супер сейф"),
    ]
    PLACE_TYPE_PALLET = "pallet"
    PLACE_TYPE_BOX = "box"
    PLACE_TYPE_BAG = "bag"
    PLACE_TYPE_CHOICES = [
        (PLACE_TYPE_PALLET, "Паллет"),
        (PLACE_TYPE_BOX, "Короб"),
        (PLACE_TYPE_BAG, "Мешок"),
    ]

    number = models.CharField("Номер заявки", max_length=32, unique=True, db_index=True)
    agency = models.ForeignKey(
        Agency,
        on_delete=models.PROTECT,
        related_name="shipping_orders",
        verbose_name="Клиент",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="shipping_orders",
        verbose_name="Создал",
    )
    status = models.CharField(
        "Статус",
        max_length=32,
        choices=STATUS_CHOICES,
        default=STATUS_DRAFT,
    )
    delivery_type = models.CharField(
        "Тип отгрузки",
        max_length=32,
        choices=DELIVERY_TYPE_CHOICES,
        default=DELIVERY_MARKETPLACE,
    )
    requires_marking_scan = models.BooleanField(
        "Требуется сканирование Data Matrix",
        default=False,
        help_text=(
            "Только для маркетплейс-отгрузки: маркированные позиции подбираются "
            "и упаковываются парным сканом ШК товара и Data Matrix."
        ),
    )
    marketplace = models.ForeignKey(
        Market,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="shipping_orders",
        verbose_name="Маркетплейс",
    )
    slot_date = models.DateField("Дата слота", null=True, blank=True)
    slot_time = models.TimeField("Время слота", null=True, blank=True)
    destination_address = models.TextField("Адрес отгрузки", blank=True)
    planned_ship_date = models.DateField("Плановая дата отгрузки", null=True, blank=True)
    vehicle_type = models.CharField(
        "Тип авто",
        max_length=32,
        choices=VEHICLE_TYPE_CHOICES,
        blank=True,
        default="",
    )
    wb_supply_barcode = models.CharField("ШК поставки WB", max_length=128, blank=True)
    wb_transit_warehouse = models.BooleanField("Транзитный склад WB", default=False)
    transit_address = models.CharField("Транзитный адрес", max_length=255, blank=True)
    shipping_barcode = models.CharField("ШК поставки", max_length=128, blank=True)
    destination_warehouse = models.CharField("Склад назначения", max_length=255, blank=True)
    supply_type = models.CharField(
        "Тип поставки",
        max_length=32,
        choices=SUPPLY_TYPE_CHOICES,
        blank=True,
        default="",
    )
    eta_at = models.DateTimeField("Плановая дата/время прибытия", null=True, blank=True)
    expected_boxes = models.PositiveIntegerField("Количество мест (план)", default=0)
    place_type = models.CharField(
        "Тип мест",
        max_length=16,
        choices=PLACE_TYPE_CHOICES,
        blank=True,
        default="",
    )
    supply_number = models.CharField(
        "Номер поставки (Ozon)",
        max_length=128,
        blank=True,
        default="",
        help_text="Номер поставки маркетплейса; для Ozon. WB продолжает использовать wb_supply_barcode.",
    )
    DISTRIBUTION_EMPTY = "empty"
    DISTRIBUTION_PARTIAL = "partial"
    DISTRIBUTION_COMPLETE = "complete"
    DISTRIBUTION_MISMATCH = "mismatch"
    DISTRIBUTION_STATUS_CHOICES = [
        (DISTRIBUTION_EMPTY, "Не заполнено"),
        (DISTRIBUTION_PARTIAL, "Заполнено частично"),
        (DISTRIBUTION_COMPLETE, "Заполнено полностью"),
        (DISTRIBUTION_MISMATCH, "Есть расхождения"),
    ]
    distribution_status = models.CharField(
        "Статус распределения Ozon",
        max_length=16,
        choices=DISTRIBUTION_STATUS_CHOICES,
        blank=True,
        default="",
        help_text="Кэш статуса распределения по конечным складам (только Ozon).",
    )
    vehicle_number = models.CharField("Номер авто", max_length=32, blank=True)
    driver_phone = models.CharField("Телефон водителя", max_length=32, blank=True)
    comment = models.TextField("Комментарий", blank=True)
    shipping_discrepancy_status = models.CharField(max_length=32, blank=True, default="")
    shipping_discrepancy_payload = models.JSONField(default=dict, blank=True)
    reserved_at = models.DateTimeField("Резерв подтвержден", null=True, blank=True)
    shipped_at = models.DateTimeField("Отгружена", null=True, blank=True)
    created_at = models.DateTimeField("Создана", auto_now_add=True)
    updated_at = models.DateTimeField("Обновлена", auto_now=True)

    class Meta:
        verbose_name = "Заявка на отгрузку"
        verbose_name_plural = "Заявки на отгрузку"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["status", "created_at"]),
            models.Index(fields=["agency", "status"]),
        ]

    def __str__(self) -> str:
        return self.number

    def is_closed(self) -> bool:
        return self.status in {
            self.STATUS_SHIPPED,
            self.STATUS_PARTIAL,
            self.STATUS_CANCELED,
        }


class ShippingOrderItem(models.Model):
    order = models.ForeignKey(
        ShippingOrder,
        on_delete=models.CASCADE,
        related_name="items",
        verbose_name="Заявка",
    )
    sku = models.ForeignKey(
        SKU,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="shipping_items",
        verbose_name="SKU",
    )
    sku_code = models.CharField("Артикул", max_length=64)
    name = models.CharField("Наименование", max_length=255)
    size = models.CharField("Размер", max_length=64, blank=True)
    barcode = models.CharField("Штрихкод", max_length=64, blank=True)
    goods_type = models.CharField("Тип товара", max_length=64, blank=True)
    qty_requested = models.PositiveIntegerField("Запрошено", default=0)
    qty_reserved = models.PositiveIntegerField("Зарезервировано", default=0)
    qty_shipped = models.PositiveIntegerField("Отгружено", default=0)
    comment = models.TextField("Комментарий", blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Позиция отгрузки"
        verbose_name_plural = "Позиции отгрузки"
        ordering = ["id"]
        indexes = [
            models.Index(fields=["order", "sku_code", "size"]),
            models.Index(fields=["sku_code", "barcode"]),
        ]

    def __str__(self) -> str:
        return f"{self.sku_code} ({self.qty_requested})"

    def clean(self):
        if self.qty_requested <= 0:
            raise ValidationError("Количество по позиции должно быть больше нуля.")
        if self.qty_reserved > self.qty_requested:
            raise ValidationError("Резерв не может быть больше запрошенного количества.")
        if self.qty_shipped > self.qty_requested:
            raise ValidationError("Отгруженное количество не может быть больше запрошенного.")

    @property
    def qty_remaining(self) -> int:
        return max(int(self.qty_requested or 0) - int(self.qty_shipped or 0), 0)


class ShippingReservationUnit(models.Model):
    MODE_FULL_PALLET = "full_pallet"
    MODE_PALLET_QUOTA = "pallet_quota"
    MODE_FIXED_BOX = "fixed_box"
    MODE_LOOSE_QTY = "loose_qty"
    MODE_CHOICES = [
        (MODE_FULL_PALLET, "Full pallet"),
        (MODE_PALLET_QUOTA, "Pallet quota"),
        (MODE_FIXED_BOX, "Fixed box"),
        (MODE_LOOSE_QTY, "Loose qty"),
    ]

    STATUS_RESERVED = "reserved"
    STATUS_TASK_CREATED = "task_created"
    STATUS_PICKED = "picked"
    STATUS_IN_OTG = "in_otg"
    STATUS_PALLETIZED = "palletized"
    STATUS_SHIPPED = "shipped"
    STATUS_RELEASED = "released"
    STATUS_SUBSTITUTED = "substituted"
    STATUS_CHOICES = [
        (STATUS_RESERVED, "Reserved"),
        (STATUS_TASK_CREATED, "Task created"),
        (STATUS_PICKED, "Picked"),
        (STATUS_IN_OTG, "In OTG"),
        (STATUS_PALLETIZED, "Palletized"),
        (STATUS_SHIPPED, "Shipped"),
        (STATUS_RELEASED, "Released"),
        (STATUS_SUBSTITUTED, "Substituted"),
    ]

    order = models.ForeignKey(
        ShippingOrder,
        on_delete=models.CASCADE,
        related_name="reservation_units",
    )
    item = models.ForeignKey(
        ShippingOrderItem,
        on_delete=models.CASCADE,
        related_name="reservation_units",
    )
    agency = models.ForeignKey(
        Agency,
        on_delete=models.CASCADE,
        related_name="shipping_reservation_units",
    )
    reserve_mode = models.CharField(max_length=24, choices=MODE_CHOICES, default=MODE_LOOSE_QTY)
    status = models.CharField(max_length=24, choices=STATUS_CHOICES, default=STATUS_RESERVED)
    sku_code = models.CharField(max_length=64)
    name = models.CharField(max_length=255, blank=True, default="")
    size = models.CharField(max_length=64, blank=True, default="")
    barcode = models.CharField(max_length=64, blank=True, default="")
    goods_type = models.CharField(max_length=32, blank=True, default="gv")
    qty_per_box = models.PositiveIntegerField(default=0)
    boxes_required = models.PositiveIntegerField(default=0)
    qty_required = models.PositiveIntegerField(default=0)
    pallet_code = models.CharField(max_length=128, blank=True, default="")
    box_code = models.CharField(max_length=128, blank=True, default="")
    snapshot_id = models.PositiveIntegerField(null=True, blank=True)
    source_location = models.CharField(max_length=255, blank=True, default="")
    source_zone = models.CharField(max_length=32, blank=True, default="")
    payload = models.JSONField(default=dict, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_shipping_reservation_units",
    )
    released_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="released_shipping_reservation_units",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["order", "status"], name="shipping_sh_order__5c3341_idx"),
            models.Index(fields=["agency", "status"], name="shipping_sh_agency__805bb6_idx"),
            models.Index(fields=["pallet_code", "status"], name="shipping_sh_pallet__6fdc17_idx"),
            models.Index(fields=["box_code", "status"], name="shipping_sh_box_cod_b364f4_idx"),
            models.Index(fields=["snapshot_id"], name="shipping_sh_snapsho_75cf78_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.order.number}: {self.sku_code} ({self.qty_required})"



class ShippingDestination(models.Model):
    """Конечный склад распределения внутри одной заявки Ozon (не отдельный SO)."""

    order = models.ForeignKey(ShippingOrder, on_delete=models.CASCADE, related_name="destinations", verbose_name="Заявка")
    warehouse_name = models.CharField("Конечный склад", max_length=255)
    warehouse_code = models.CharField("Код склада Ozon", max_length=100, blank=True, default="")
    region = models.CharField("Регион", max_length=255, blank=True, default="")
    address = models.TextField("Адрес", blank=True, default="")
    planned_units = models.PositiveIntegerField("Единиц (план)", default=0)
    planned_boxes = models.PositiveIntegerField("Коробов (план)", default=0)
    comment = models.TextField("Комментарий", blank=True, default="")
    sort_order = models.PositiveIntegerField("Порядок", default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Конечный склад Ozon"
        verbose_name_plural = "Конечные склады Ozon"
        ordering = ["sort_order", "id"]
        constraints = [models.UniqueConstraint(fields=["order", "warehouse_name"], name="shipping_destination_unique_name_per_order")]
        indexes = [models.Index(fields=["order", "sort_order"])]

    def __str__(self) -> str:
        return f"{self.warehouse_name} ({self.order.number})"


class ShippingDestinationItem(models.Model):
    """Распределение количества позиции заявки на конечный склад Ozon."""

    destination = models.ForeignKey(ShippingDestination, on_delete=models.CASCADE, related_name="items", verbose_name="Конечный склад")
    order_item = models.ForeignKey(ShippingOrderItem, on_delete=models.CASCADE, related_name="destination_items", verbose_name="Позиция заявки")
    quantity = models.PositiveIntegerField("Количество", default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Распределение позиции Ozon"
        verbose_name_plural = "Распределения позиций Ozon"
        ordering = ["id"]
        constraints = [models.UniqueConstraint(fields=["destination", "order_item"], name="shipping_destination_item_unique")]
        indexes = [models.Index(fields=["order_item", "destination"])]

    def __str__(self) -> str:
        return f"{self.order_item.sku_code} -> {self.destination.warehouse_name}: {self.quantity}"


class ShippingTransportNote(models.Model):
    order = models.OneToOneField(
        ShippingOrder,
        on_delete=models.CASCADE,
        related_name="transport_note",
        verbose_name="Заявка",
    )
    document_number = models.CharField("Номер ТН", max_length=64, blank=True)
    document_date = models.DateField("Дата ТН", null=True, blank=True)

    shipper_name = models.CharField("Грузоотправитель", max_length=255, blank=True)
    shipper_inn = models.CharField("ИНН грузоотправителя", max_length=32, blank=True)
    shipper_address = models.TextField("Адрес грузоотправителя", blank=True)
    shipper_phone = models.CharField("Телефон грузоотправителя", max_length=32, blank=True)

    consignee_name = models.CharField("Грузополучатель", max_length=255, blank=True)
    consignee_inn = models.CharField("ИНН грузополучателя", max_length=32, blank=True)
    consignee_address = models.TextField("Адрес грузополучателя", blank=True)
    consignee_phone = models.CharField("Телефон грузополучателя", max_length=32, blank=True)

    carrier_name = models.CharField("Перевозчик", max_length=255, blank=True)
    carrier_inn = models.CharField("ИНН перевозчика", max_length=32, blank=True)
    carrier_address = models.TextField("Адрес перевозчика", blank=True)
    carrier_phone = models.CharField("Телефон перевозчика", max_length=32, blank=True)

    loading_address = models.TextField("Адрес погрузки", blank=True)
    unloading_address = models.TextField("Адрес выгрузки", blank=True)
    cargo_name = models.TextField("Наименование груза", blank=True)
    cargo_package_count = models.PositiveIntegerField("Количество мест", default=0)
    cargo_package_type = models.CharField("Тип мест", max_length=64, blank=True)
    cargo_weight_kg = models.DecimalField("Вес, кг", max_digits=10, decimal_places=3, null=True, blank=True)
    cargo_declared_value = models.DecimalField(
        "Объявленная стоимость",
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
    )

    accompanying_documents = models.TextField("Сопроводительные документы", blank=True)
    special_instructions = models.TextField("Указания грузоотправителя", blank=True)
    transportation_conditions = models.TextField("Условия перевозки", blank=True)
    delivery_notes = models.TextField("Оговорки и замечания", blank=True)

    driver_name = models.CharField("Водитель", max_length=255, blank=True)
    driver_phone = models.CharField("Телефон водителя", max_length=32, blank=True)
    vehicle_number = models.CharField("Номер автомобиля", max_length=32, blank=True)
    trailer_number = models.CharField("Номер прицепа", max_length=32, blank=True)
    service_cost = models.DecimalField("Стоимость услуг", max_digits=12, decimal_places=2, null=True, blank=True)

    created_at = models.DateTimeField("Создано", auto_now_add=True)
    updated_at = models.DateTimeField("Обновлено", auto_now=True)

    class Meta:
        verbose_name = "Транспортная накладная"
        verbose_name_plural = "Транспортные накладные"
        ordering = ["-updated_at", "-id"]

    def __str__(self) -> str:
        return self.document_number or f"ТН по заявке {self.order.number}"


class ShippingReserve(models.Model):
    order = models.ForeignKey(
        ShippingOrder,
        on_delete=models.CASCADE,
        related_name="reserves",
        verbose_name="Заявка",
    )
    item = models.ForeignKey(
        ShippingOrderItem,
        on_delete=models.CASCADE,
        related_name="reserves",
        verbose_name="Позиция",
    )
    agency = models.ForeignKey(
        Agency,
        on_delete=models.CASCADE,
        related_name="shipping_reserves",
        verbose_name="Клиент",
    )
    sku_code = models.CharField("Артикул", max_length=64)
    size = models.CharField("Размер", max_length=64, blank=True)
    barcode = models.CharField("Штрихкод", max_length=64, blank=True)
    goods_type = models.CharField("Тип товара", max_length=64, blank=True)
    qty = models.PositiveIntegerField("Количество", default=0)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="shipping_reserves",
        verbose_name="Зарезервировал",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Резерв отгрузки"
        verbose_name_plural = "Резервы отгрузки"
        indexes = [
            models.Index(fields=["agency", "sku_code", "size"]),
            models.Index(fields=["order", "item"]),
        ]

    def __str__(self) -> str:
        return f"{self.order.number}: {self.sku_code} ({self.qty})"


class ShippingOrderAttachment(models.Model):
    RETENTION_DAYS = 60

    order = models.ForeignKey(
        ShippingOrder,
        on_delete=models.CASCADE,
        related_name="attachments",
        verbose_name="Заявка",
    )
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="shipping_order_attachments",
        verbose_name="Кто загрузил",
    )
    file = models.FileField("Файл", upload_to=shipping_attachment_upload_to)
    uploaded_at = models.DateTimeField("Загружен", auto_now_add=True)

    class Meta:
        verbose_name = "Файл заявки на отгрузку"
        verbose_name_plural = "Файлы заявок на отгрузку"
        ordering = ["-uploaded_at"]

    def __str__(self) -> str:
        return f"{self.order.number}: {self.filename}"

    @property
    def filename(self) -> str:
        return os.path.basename(self.file.name)

    @property
    def expires_at(self):
        return self.uploaded_at + timedelta(days=self.RETENTION_DAYS)

    @property
    def is_expired(self) -> bool:
        if not self.uploaded_at:
            return False
        return timezone.now() >= self.expires_at
