import re
from uuid import uuid4

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models

from employees.models import Employee
from head_manager.models import Carrier


def trip_attachment_upload_to(instance, filename: str) -> str:
    trip_id = getattr(instance, "trip_id", None)
    if not trip_id and getattr(instance, "problem_id", None):
        trip_id = getattr(instance.problem, "trip_id", None)
    trip_id = trip_id or "new"
    safe_name = re.sub(r"[^0-9A-Za-zА-Яа-я._-]+", "_", str(filename or "file")).strip("._") or "file"
    return f"logistics/trips/{trip_id}/{uuid4().hex}_{safe_name}"


class LogisticsTrip(models.Model):
    KIND_INTERNAL = "internal"
    KIND_EXTERNAL = "external"
    KIND_CHOICES = [
        (KIND_INTERNAL, "По внутренним заявкам"),
        (KIND_EXTERNAL, "Внешний рейс"),
    ]

    STATUS_DRAFT = "draft"
    STATUS_PLANNED = "planned"
    STATUS_LOADING = "loading"
    STATUS_DEPARTED = "departed"
    STATUS_COMPLETED = "completed"
    STATUS_CANCELED = "canceled"
    STATUS_CHOICES = [
        (STATUS_DRAFT, "Черновик"),
        (STATUS_PLANNED, "Спланирован"),
        (STATUS_LOADING, "Погрузка"),
        (STATUS_DEPARTED, "В рейсе"),
        (STATUS_COMPLETED, "Завершен"),
        (STATUS_CANCELED, "Отменен"),
    ]

    DRIVER_STATUS_UNASSIGNED = "unassigned"
    DRIVER_STATUS_PRELIMINARY = "preliminary"
    DRIVER_STATUS_ASSIGNED = "assigned"
    DRIVER_STATUS_ACCEPTED = "accepted"
    DRIVER_STATUS_EN_ROUTE = "en_route"
    DRIVER_STATUS_AWAITING_DELIVERY = "awaiting_delivery"
    DRIVER_STATUS_DELIVERED = "delivered"
    DRIVER_STATUS_PROBLEM = "problem"
    DRIVER_STATUS_CHOICES = [
        (DRIVER_STATUS_UNASSIGNED, "Водитель не назначен"),
        (DRIVER_STATUS_PRELIMINARY, "Предварительный рейс"),
        (DRIVER_STATUS_ASSIGNED, "Водитель назначен"),
        (DRIVER_STATUS_ACCEPTED, "Рейс принят водителем"),
        (DRIVER_STATUS_EN_ROUTE, "Водитель выехал"),
        (DRIVER_STATUS_AWAITING_DELIVERY, "Ожидает сдачи"),
        (DRIVER_STATUS_DELIVERED, "Груз сдан"),
        (DRIVER_STATUS_PROBLEM, "Проблемный рейс"),
    ]

    VEHICLE_FULFILLMENT = "fulfillment"
    VEHICLE_CLIENT = "client"
    VEHICLE_HIRED = "hired"
    VEHICLE_OTHER = "other"
    VEHICLE_CHOICES = [
        (VEHICLE_FULFILLMENT, "Транспорт Fullbox"),
        (VEHICLE_CLIENT, "Транспорт клиента"),
        (VEHICLE_HIRED, "Наемный транспорт"),
        (VEHICLE_OTHER, "Другое"),
    ]

    number = models.CharField("Номер рейса", max_length=32, unique=True, db_index=True)
    trip_kind = models.CharField(
        "Тип рейса",
        max_length=16,
        choices=KIND_CHOICES,
        default=KIND_INTERNAL,
        db_index=True,
    )
    trip_date = models.DateField("Дата рейса", null=True, blank=True)
    status = models.CharField("Статус", max_length=32, choices=STATUS_CHOICES, default=STATUS_DRAFT)
    vehicle_type = models.CharField("Тип транспорта", max_length=32, choices=VEHICLE_CHOICES, blank=True, default="")
    vehicle_name = models.CharField("Транспорт", max_length=128, blank=True)
    vehicle_number = models.CharField("Номер авто", max_length=32, blank=True)
    driver_name = models.CharField("Водитель", max_length=255, blank=True)
    driver_phone = models.CharField("Телефон водителя", max_length=32, blank=True)
    assigned_driver = models.ForeignKey(
        Employee,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="assigned_delivery_trips",
        verbose_name="Водитель в личном кабинете",
        limit_choices_to={"role": "driver"},
    )
    driver_assigned_at = models.DateTimeField("Водитель назначен", null=True, blank=True)
    driver_assigned_by = models.ForeignKey(
        Employee,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="delivery_driver_assignments",
        verbose_name="Кто назначил водителя",
    )
    driver_status = models.CharField(
        "Статус водителя",
        max_length=32,
        choices=DRIVER_STATUS_CHOICES,
        default=DRIVER_STATUS_UNASSIGNED,
        db_index=True,
    )
    scheduled_at = models.DateTimeField("Плановая подача", null=True, blank=True)
    driver_accepted_at = models.DateTimeField("Рейс принят", null=True, blank=True)
    driver_departed_at = models.DateTimeField("Водитель выехал", null=True, blank=True)
    driver_arrived_at = models.DateTimeField("Водитель прибыл", null=True, blank=True)
    actual_handover_at = models.DateTimeField("Фактическая сдача", null=True, blank=True)
    handover_system_at = models.DateTimeField("Системное время подтверждения", null=True, blank=True)
    handover_time_change_reason = models.TextField("Причина изменения времени сдачи", blank=True)
    gate_number = models.CharField("Номер ворот", max_length=64, blank=True)
    waiting_started_at = models.DateTimeField("Начало ожидания", null=True, blank=True)
    unloading_finished_at = models.DateTimeField("Окончание разгрузки", null=True, blank=True)
    delivery_receipt_number = models.CharField("Номер документа приемки", max_length=128, blank=True)
    driver_result_comment = models.TextField("Комментарий водителя", blank=True)
    closed_at = models.DateTimeField("Рейс закрыт", null=True, blank=True)
    route_comment = models.TextField("Комментарий по маршруту", blank=True)
    loading_comment = models.TextField("Комментарий по погрузке", blank=True)
    assigned_logistician = models.ForeignKey(
        Employee,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="logistics_trips",
        verbose_name="Логист",
        limit_choices_to={"role": "logistician"},
    )
    carrier = models.ForeignKey(
        Carrier,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="logistics_trips",
        verbose_name="Перевозчик",
    )
    max_weight_kg = models.DecimalField(
        "Грузоподъёмность, кг",
        max_digits=12,
        decimal_places=3,
        null=True,
        blank=True,
    )
    max_volume_m3 = models.DecimalField(
        "Вместимость, м³",
        max_digits=12,
        decimal_places=3,
        null=True,
        blank=True,
    )
    max_pallets = models.PositiveIntegerField("Макс. паллет", null=True, blank=True)
    edit_version = models.PositiveIntegerField(
        "Версия редактирования",
        default=1,
        db_index=True,
        help_text="Optimistic lock: увеличивается при каждом сохранении рейса",
    )
    last_edited_by = models.ForeignKey(
        Employee,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="logistics_trips_last_edited",
        verbose_name="Последний редактор",
    )
    editing_by = models.ForeignKey(
        Employee,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="logistics_trips_editing",
        verbose_name="Сейчас на карточке",
    )
    editing_heartbeat_at = models.DateTimeField(
        "Активность на карточке",
        null=True,
        blank=True,
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_logistics_trips",
        verbose_name="Создал",
    )
    created_at = models.DateTimeField("Создан", auto_now_add=True)
    updated_at = models.DateTimeField("Обновлен", auto_now=True)

    class Meta:
        verbose_name = "Рейс логистики"
        verbose_name_plural = "Рейсы логистики"
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["status", "trip_date"])]

    def __str__(self) -> str:
        return self.number


class LogisticsExternalTrip(models.Model):
    trip = models.OneToOneField(
        LogisticsTrip,
        on_delete=models.CASCADE,
        related_name="external_details",
        verbose_name="Рейс",
    )
    client = models.ForeignKey(
        "sku.Agency",
        on_delete=models.PROTECT,
        related_name="external_logistics_trips",
        verbose_name="Клиент для биллинга",
    )
    pickup_address = models.TextField("Точка А — откуда забрать")
    delivery_address = models.TextField("Точка Б — куда доставить")
    cargo_description = models.TextField("Описание груза", blank=True)
    pickup_contact = models.CharField("Контакт в точке А", max_length=255, blank=True)
    pickup_phone = models.CharField("Телефон в точке А", max_length=32, blank=True)
    delivery_contact = models.CharField("Контакт в точке Б", max_length=255, blank=True)
    delivery_phone = models.CharField("Телефон в точке Б", max_length=32, blank=True)
    created_at = models.DateTimeField("Создано", auto_now_add=True)
    updated_at = models.DateTimeField("Обновлено", auto_now=True)

    class Meta:
        verbose_name = "Детали внешнего рейса"
        verbose_name_plural = "Детали внешних рейсов"
        constraints = [
            models.CheckConstraint(
                condition=~models.Q(pickup_address="") & ~models.Q(delivery_address=""),
                name="log_ext_route_nonempty",
            ),
        ]

    def clean(self):
        super().clean()
        if self.trip_id and self.trip.trip_kind != LogisticsTrip.KIND_EXTERNAL:
            raise ValidationError("Внешние точки можно указать только для внешнего рейса.")
        if not str(self.pickup_address or "").strip():
            raise ValidationError({"pickup_address": "Укажите точку А."})
        if not str(self.delivery_address or "").strip():
            raise ValidationError({"delivery_address": "Укажите точку Б."})

    def save(self, *args, **kwargs):
        self.pickup_address = str(self.pickup_address or "").strip()
        self.delivery_address = str(self.delivery_address or "").strip()
        self.full_clean()
        return super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"{self.trip.number}: {self.pickup_address} → {self.delivery_address}"


class CarrierVehicle(models.Model):
    carrier = models.ForeignKey(
        Carrier,
        on_delete=models.CASCADE,
        related_name="vehicles",
        verbose_name="Перевозчик",
    )
    vehicle_name = models.CharField("Марка / модель", max_length=128, blank=True)
    vehicle_number = models.CharField("Госномер", max_length=32)
    driver_name = models.CharField("ФИО водителя", max_length=255)
    driver_phone = models.CharField("Телефон водителя", max_length=32)
    max_weight_kg = models.DecimalField(
        "Грузоподъёмность, кг",
        max_digits=12,
        decimal_places=3,
        null=True,
        blank=True,
    )
    max_volume_m3 = models.DecimalField(
        "Вместимость, м³",
        max_digits=12,
        decimal_places=3,
        null=True,
        blank=True,
    )
    max_pallets = models.PositiveIntegerField("Максимум паллет", null=True, blank=True)
    is_default = models.BooleanField("По умолчанию", default=False)
    is_active = models.BooleanField("Активен", default=True, db_index=True)
    comment = models.CharField("Комментарий", max_length=255, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_carrier_vehicles",
        verbose_name="Создал",
    )
    created_at = models.DateTimeField("Создано", auto_now_add=True)
    updated_at = models.DateTimeField("Обновлено", auto_now=True)

    class Meta:
        ordering = ("carrier__name", "-is_default", "vehicle_number", "id")
        verbose_name = "Автомобиль перевозчика"
        verbose_name_plural = "Автомобили перевозчиков"
        constraints = [
            models.UniqueConstraint(
                fields=("carrier", "vehicle_number"),
                name="uniq_carrier_vehicle_number",
            ),
            models.UniqueConstraint(
                fields=("carrier",),
                condition=models.Q(is_default=True, is_active=True),
                name="uniq_active_default_vehicle_per_carrier",
            ),
        ]
        indexes = [
            models.Index(fields=("carrier", "is_active"), name="log_carrier_active_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.carrier} · {self.vehicle_number}"


class TripAttachment(models.Model):
    TYPE_GATE = "gate"
    TYPE_RECEIPT = "receipt"
    TYPE_PROBLEM = "problem"
    TYPE_DOCUMENT = "document"
    TYPE_CHOICES = [
        (TYPE_GATE, "Фотография ворот"),
        (TYPE_RECEIPT, "Документ о приемке"),
        (TYPE_PROBLEM, "Подтверждение проблемы"),
        (TYPE_DOCUMENT, "Документ рейса"),
    ]

    trip = models.ForeignKey(
        LogisticsTrip,
        on_delete=models.CASCADE,
        related_name="driver_attachments",
        verbose_name="Рейс",
    )
    attachment_type = models.CharField("Тип", max_length=24, choices=TYPE_CHOICES)
    file = models.FileField("Файл", upload_to=trip_attachment_upload_to)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="uploaded_trip_attachments",
        verbose_name="Загрузил",
    )
    photo_created_at = models.DateTimeField("Время создания фото", null=True, blank=True)
    latitude = models.DecimalField("Широта", max_digits=9, decimal_places=6, null=True, blank=True)
    longitude = models.DecimalField("Долгота", max_digits=9, decimal_places=6, null=True, blank=True)
    created_at = models.DateTimeField("Загружено", auto_now_add=True)

    class Meta:
        ordering = ("created_at", "id")
        verbose_name = "Файл рейса"
        verbose_name_plural = "Файлы рейсов"

    def __str__(self) -> str:
        return f"{self.trip} · {self.get_attachment_type_display()}"


class ProblemTrip(models.Model):
    REASON_WAREHOUSE_REJECTS = "warehouse_rejects"
    REASON_LATE_SLOT = "late_slot"
    REASON_DOCUMENT_ERROR = "document_error"
    REASON_BAD_BARCODE = "bad_barcode"
    REASON_MARKING_ERROR = "marking_error"
    REASON_PACKAGING = "packaging"
    REASON_QUANTITY = "quantity"
    REASON_DAMAGE = "damage"
    REASON_NO_MARKETPLACE_REQUEST = "no_marketplace_request"
    REASON_WRONG_ADDRESS = "wrong_address"
    REASON_QUEUE_CLOSED = "queue_closed"
    REASON_MARKETPLACE_TECH = "marketplace_tech"
    REASON_EMPLOYEE_REFUSAL = "employee_refusal"
    REASON_TRANSPORT = "transport"
    REASON_DRIVER = "driver"
    REASON_CLIENT = "client"
    REASON_OTHER = "other"
    REASON_CHOICES = [
        (REASON_WAREHOUSE_REJECTS, "Склад не принимает поставку"),
        (REASON_LATE_SLOT, "Опоздание на временной слот"),
        (REASON_DOCUMENT_ERROR, "Ошибка в документах"),
        (REASON_BAD_BARCODE, "Неверный штрихкод"),
        (REASON_MARKING_ERROR, "Ошибка в маркировке или КИЗах"),
        (REASON_PACKAGING, "Неправильная упаковка"),
        (REASON_QUANTITY, "Расхождение по количеству"),
        (REASON_DAMAGE, "Повреждение товара"),
        (REASON_NO_MARKETPLACE_REQUEST, "Отсутствие заявки в системе маркетплейса"),
        (REASON_WRONG_ADDRESS, "Неправильный адрес или склад"),
        (REASON_QUEUE_CLOSED, "Очередь и завершение времени приемки"),
        (REASON_MARKETPLACE_TECH, "Техническая ошибка маркетплейса"),
        (REASON_EMPLOYEE_REFUSAL, "Отказ сотрудника склада"),
        (REASON_TRANSPORT, "Проблема с транспортом"),
        (REASON_DRIVER, "Проблема по вине водителя"),
        (REASON_CLIENT, "Проблема по вине клиента"),
        (REASON_OTHER, "Другая причина"),
    ]

    PARTY_UNKNOWN = "unknown"
    PARTY_MARKETPLACE = "marketplace"
    PARTY_CLIENT = "client"
    PARTY_DRIVER = "driver"
    PARTY_CARRIER = "carrier"
    PARTY_FULLBOX = "fullbox"
    PARTY_CHOICES = [
        (PARTY_UNKNOWN, "Не определена"),
        (PARTY_MARKETPLACE, "Маркетплейс"),
        (PARTY_CLIENT, "Клиент"),
        (PARTY_DRIVER, "Водитель"),
        (PARTY_CARRIER, "Перевозчик"),
        (PARTY_FULLBOX, "Fullbox"),
    ]

    STATUS_NEW = "new"
    STATUS_REVIEW = "review"
    STATUS_WAIT_MARKETPLACE = "wait_marketplace"
    STATUS_REDELIVERY = "redelivery"
    STATUS_PENALTY_CONFIRMED = "penalty_confirmed"
    STATUS_PENALTY_DISPUTED = "penalty_disputed"
    STATUS_RESOLVED = "resolved"
    STATUS_CLOSED = "closed"
    STATUS_CHOICES = [
        (STATUS_NEW, "Новый"),
        (STATUS_REVIEW, "На проверке"),
        (STATUS_WAIT_MARKETPLACE, "Ожидает ответа маркетплейса"),
        (STATUS_REDELIVERY, "Назначена повторная доставка"),
        (STATUS_PENALTY_CONFIRMED, "Штраф подтвержден"),
        (STATUS_PENALTY_DISPUTED, "Штраф оспаривается"),
        (STATUS_RESOLVED, "Проблема решена"),
        (STATUS_CLOSED, "Закрыт"),
    ]

    trip = models.OneToOneField(
        LogisticsTrip,
        on_delete=models.PROTECT,
        related_name="problem",
        verbose_name="Рейс",
    )
    reason_code = models.CharField("Причина несдачи", max_length=40, choices=REASON_CHOICES, db_index=True)
    comment = models.TextField("Подробный комментарий")
    refusal_at = models.DateTimeField("Дата и время отказа")
    rejecting_employee = models.CharField("Кто отказал в приемке", max_length=255, blank=True)
    gate_number = models.CharField("Номер ворот", max_length=64, blank=True)
    support_case_number = models.CharField("Обращение в поддержку", max_length=128, blank=True)
    responsible_party = models.CharField(
        "Ответственная сторона",
        max_length=24,
        choices=PARTY_CHOICES,
        default=PARTY_UNKNOWN,
        db_index=True,
    )
    status = models.CharField("Статус разбирательства", max_length=32, choices=STATUS_CHOICES, default=STATUS_NEW, db_index=True)
    assigned_to = models.ForeignKey(
        Employee,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="assigned_problem_trips",
        verbose_name="Ответственный сотрудник",
    )
    next_action_at = models.DateTimeField("Дата следующего действия", null=True, blank=True)
    created_at = models.DateTimeField("Создан", auto_now_add=True)
    updated_at = models.DateTimeField("Обновлен", auto_now=True)

    class Meta:
        ordering = ("-created_at", "-id")
        verbose_name = "Проблемный рейс"
        verbose_name_plural = "Проблемные рейсы"

    def __str__(self) -> str:
        return f"Проблема · {self.trip}"


class TripPenalty(models.Model):
    STATUS_EXPECTED = "expected"
    STATUS_CHARGED = "charged"
    STATUS_REVIEW = "review"
    STATUS_DISPUTED = "disputed"
    STATUS_CANCELED = "canceled"
    STATUS_CONFIRMED = "confirmed"
    STATUS_PAID = "paid"
    STATUS_CHOICES = [
        (STATUS_EXPECTED, "Ожидается"),
        (STATUS_CHARGED, "Начислен"),
        (STATUS_REVIEW, "На проверке"),
        (STATUS_DISPUTED, "Оспаривается"),
        (STATUS_CANCELED, "Отменен"),
        (STATUS_CONFIRMED, "Подтвержден"),
        (STATUS_PAID, "Оплачен"),
    ]

    problem = models.ForeignKey(
        ProblemTrip,
        on_delete=models.CASCADE,
        related_name="penalties",
        verbose_name="Проблемный рейс",
    )
    marketplace_name = models.CharField("Маркетплейс", max_length=128)
    penalty_type = models.CharField("Тип штрафа", max_length=128)
    reason = models.TextField("Причина штрафа", blank=True)
    charged_at = models.DateField("Дата начисления", null=True, blank=True)
    estimated_amount = models.DecimalField("Предполагаемая сумма", max_digits=14, decimal_places=2, null=True, blank=True)
    actual_amount = models.DecimalField("Фактическая сумма", max_digits=14, decimal_places=2, null=True, blank=True)
    document_number = models.CharField("Номер документа", max_length=128, blank=True)
    support_reference = models.CharField("Ссылка или номер обращения", max_length=255, blank=True)
    responsible_party = models.CharField(
        "Ответственная сторона",
        max_length=24,
        choices=ProblemTrip.PARTY_CHOICES,
        default=ProblemTrip.PARTY_UNKNOWN,
    )
    status = models.CharField("Статус штрафа", max_length=24, choices=STATUS_CHOICES, default=STATUS_EXPECTED, db_index=True)
    comment = models.TextField("Комментарий", blank=True)
    document = models.FileField("Документ", upload_to=trip_attachment_upload_to, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_trip_penalties",
        verbose_name="Создал",
    )
    created_at = models.DateTimeField("Создан", auto_now_add=True)
    updated_at = models.DateTimeField("Обновлен", auto_now=True)

    class Meta:
        ordering = ("-created_at", "-id")
        verbose_name = "Штраф рейса"
        verbose_name_plural = "Штрафы рейсов"


class TripHistory(models.Model):
    trip = models.ForeignKey(
        LogisticsTrip,
        on_delete=models.PROTECT,
        related_name="driver_history",
        verbose_name="Рейс",
    )
    event_type = models.CharField("Тип события", max_length=64, db_index=True)
    description = models.TextField("Описание")
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="trip_history_entries",
        verbose_name="Пользователь",
    )
    payload = models.JSONField("Данные", default=dict, blank=True)
    created_at = models.DateTimeField("Когда", auto_now_add=True, db_index=True)

    class Meta:
        ordering = ("-created_at", "-id")
        verbose_name = "История рейса"
        verbose_name_plural = "История рейсов"

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValueError("Записи истории рейса нельзя изменять.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError("Записи истории рейса нельзя удалять.")


class TripNotification(models.Model):
    recipient = models.ForeignKey(
        Employee,
        on_delete=models.CASCADE,
        related_name="trip_notifications",
        verbose_name="Получатель",
    )
    trip = models.ForeignKey(
        LogisticsTrip,
        on_delete=models.CASCADE,
        related_name="notifications",
        verbose_name="Рейс",
    )
    title = models.CharField("Заголовок", max_length=255)
    text = models.TextField("Текст")
    detail_url = models.CharField("Ссылка", max_length=500)
    source_key = models.CharField("Ключ события", max_length=160, unique=True)
    read_at = models.DateTimeField("Прочитано", null=True, blank=True)
    created_at = models.DateTimeField("Создано", auto_now_add=True, db_index=True)

    class Meta:
        ordering = ("-created_at", "-id")
        verbose_name = "Уведомление о рейсе"
        verbose_name_plural = "Уведомления о рейсах"


class LogisticsTripOrder(models.Model):
    trip = models.ForeignKey(
        LogisticsTrip,
        on_delete=models.CASCADE,
        related_name="orders",
        verbose_name="Рейс",
    )
    shipping_order = models.ForeignKey(
        "shipping.ShippingOrder",
        on_delete=models.PROTECT,
        related_name="logistics_links",
        verbose_name="Заявка на отгрузку",
    )
    loading_sequence = models.PositiveIntegerField("Очередь погрузки", default=0)
    delivery_sequence = models.PositiveIntegerField("Очередь доставки", default=0)
    comment = models.CharField("Комментарий", max_length=255, blank=True)
    created_at = models.DateTimeField("Создан", auto_now_add=True)
    updated_at = models.DateTimeField("Обновлен", auto_now=True)

    class Meta:
        verbose_name = "Заявка в рейсе"
        verbose_name_plural = "Заявки в рейсе"
        ordering = ["loading_sequence", "delivery_sequence", "id"]
        constraints = [
            models.UniqueConstraint(fields=["trip", "shipping_order"], name="uniq_logistics_trip_shipping_order")
        ]

    def __str__(self) -> str:
        return f"{self.trip.number} / {self.shipping_order.number}"

    def clean(self):
        super().clean()
        if self.trip_id and self.trip.trip_kind == LogisticsTrip.KIND_EXTERNAL:
            raise ValidationError("К внешнему рейсу нельзя привязывать внутренние заявки на отгрузку.")

    def save(self, *args, **kwargs):
        self.clean()
        return super().save(*args, **kwargs)


_TRIP_NUMBER_RE = re.compile(r"^TRIP-(\d+)$", re.IGNORECASE)
_TRIP_RS_RE = re.compile(r"^(\d+)_RS$", re.IGNORECASE)
_TRIP_DRAFT_RE = re.compile(r"^DRAFT-[0-9A-F]{12,}$", re.IGNORECASE)


def trip_number_sequence_value(value: str | None) -> int:
    raw = str(value or "").strip()
    if not raw:
        return 0
    match = _TRIP_NUMBER_RE.match(raw)
    if match:
        return int(match.group(1))
    match = _TRIP_RS_RE.match(raw)
    if match:
        return int(match.group(1))
    return 0


def is_draft_trip_number(value: str | None) -> bool:
    return bool(_TRIP_DRAFT_RE.match(str(value or "").strip()))


def display_trip_number(value: str | None) -> str:
    sequence = trip_number_sequence_value(value)
    if sequence > 0:
        return f"{sequence}_RS"
    return str(value or "").strip()


def next_draft_trip_number() -> str:
    return f"DRAFT-{uuid4().hex[:16].upper()}"


def next_trip_number() -> str:
    numbers = LogisticsTrip.objects.values_list("number", flat=True)
    max_number = 0
    for raw in numbers:
        max_number = max(max_number, trip_number_sequence_value(raw))
    return f"{max_number + 1}_RS"


class ShippingRoutingState(models.Model):
    """
    Логистический этап отгрузки отдельно от складского ShippingOrder.status.
    Не дублирует Task — только состояние маршрутизации и уточнений.
    """

    STATUS_READY = "ready_for_routing"
    STATUS_CLARIFY = "needs_clarification"
    STATUS_ROUTED = "routed"
    STATUS_IN_TRANSIT = "in_transit"
    STATUS_DELIVERED = "delivered"
    STATUS_FAILED = "delivery_failed"
    STATUS_CHOICES = [
        (STATUS_READY, "Готова к маршрутизации"),
        (STATUS_CLARIFY, "Требуется уточнение"),
        (STATUS_ROUTED, "Рейс назначен"),
        (STATUS_IN_TRANSIT, "В пути"),
        (STATUS_DELIVERED, "Доставка завершена"),
        (STATUS_FAILED, "Не сдана на МП"),
    ]
    TERMINAL_DELIVERY_STATUSES = frozenset({STATUS_DELIVERED, STATUS_FAILED})

    REASON_NO_ADDRESS = "no_address"
    REASON_BAD_WINDOW = "bad_time_window"
    REASON_NO_VOLUME = "no_volume"
    REASON_NO_WEIGHT = "no_weight"
    REASON_NO_CONTACTS = "no_contacts"
    REASON_NOT_READY = "goods_not_ready"
    REASON_NO_DOCS = "no_documents"
    REASON_SPECIAL_VEHICLE = "special_vehicle"
    REASON_OTHER = "other"
    REASON_CHOICES = [
        (REASON_NO_ADDRESS, "Отсутствует адрес"),
        (REASON_BAD_WINDOW, "Неверное временное окно"),
        (REASON_NO_VOLUME, "Не указан объём"),
        (REASON_NO_WEIGHT, "Не указан вес"),
        (REASON_NO_CONTACTS, "Отсутствуют контакты"),
        (REASON_NOT_READY, "Товар не готов"),
        (REASON_NO_DOCS, "Отсутствуют документы"),
        (REASON_SPECIAL_VEHICLE, "Требуется специальный транспорт"),
        (REASON_OTHER, "Другое"),
    ]

    shipping_order = models.OneToOneField(
        "shipping.ShippingOrder",
        on_delete=models.CASCADE,
        related_name="routing_state",
        verbose_name="Заявка на отгрузку",
    )
    status = models.CharField(
        max_length=32,
        choices=STATUS_CHOICES,
        default=STATUS_READY,
        db_index=True,
    )
    reason_code = models.CharField(max_length=32, choices=REASON_CHOICES, blank=True, default="")
    reason_text = models.TextField(blank=True, default="")
    returned_by = models.ForeignKey(
        Employee,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="routing_returns",
        verbose_name="Вернул на уточнение",
    )
    returned_at = models.DateTimeField(null=True, blank=True)
    resolved_by = models.ForeignKey(
        Employee,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="routing_resolves",
        verbose_name="Снял уточнение",
    )
    resolved_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Статус маршрутизации отгрузки"
        verbose_name_plural = "Статусы маршрутизации отгрузок"
        indexes = [
            models.Index(fields=["status", "-updated_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.shipping_order_id}: {self.status}"

    @property
    def status_label(self) -> str:
        return dict(self.STATUS_CHOICES).get(self.status, self.status)

    @property
    def reason_label(self) -> str:
        if not self.reason_code:
            return ""
        return dict(self.REASON_CHOICES).get(self.reason_code, self.reason_code)
