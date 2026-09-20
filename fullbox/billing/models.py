from __future__ import annotations

import os
import re
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.utils import timezone

from employees.models import Employee
from head_manager.models import OwnCompany
from sku.models import Agency, Market, Store

from .statuses import (
    ActStatus,
    BillingStatus,
    DiscrepancyStatus,
    DocumentReviewStatus,
    InvoiceStatus,
    PaymentPromiseStatus,
    PaymentStatus,
    ReconciliationStatus,
    SequenceKind,
)


MONEY_ZERO = Decimal("0.00")
_ATTACHMENT_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def billing_document_upload_to(instance, filename: str) -> str:
    original = os.path.basename(str(filename or "file"))
    stem, ext = os.path.splitext(original)
    safe_stem = _ATTACHMENT_FILENAME_RE.sub("_", stem).strip("._") or "file"
    safe_ext = _ATTACHMENT_FILENAME_RE.sub("", ext).lower()[:16]
    kind = _ATTACHMENT_FILENAME_RE.sub("_", instance.__class__.__name__.lower())
    period = timezone.now().strftime("%Y/%m")
    return f"billing/{kind}/{period}/{safe_stem}{safe_ext}"


class BillingApplication(models.Model):
    TYPE_RECEIVING = "receiving"
    TYPE_PROCESSING = "processing"
    TYPE_PACKING = "packing"
    TYPE_SHIPPING = "shipping"
    TYPE_LOGISTICS = "logistics"
    TYPE_STORAGE = "storage"
    TYPE_OTHER = "other"
    TYPE_FBS = "fbs"
    APPLICATION_TYPE_CHOICES = [
        (TYPE_RECEIVING, "Приемка"),
        (TYPE_PROCESSING, "Обработка"),
        (TYPE_PACKING, "Упаковка"),
        (TYPE_SHIPPING, "Отгрузка"),
        (TYPE_LOGISTICS, "Логистика"),
        (TYPE_STORAGE, "Хранение"),
        (TYPE_OTHER, "Другая заявка"),
        (TYPE_FBS, "FBS"),
    ]

    application_type = models.CharField("Тип заявки", max_length=32, choices=APPLICATION_TYPE_CHOICES)
    application_id = models.CharField("Номер заявки", max_length=128)
    client = models.ForeignKey(Agency, on_delete=models.PROTECT, related_name="billing_applications", verbose_name="Клиент")
    legal_entity = models.ForeignKey(
        Agency,
        on_delete=models.PROTECT,
        related_name="billing_legal_entity_applications",
        verbose_name="Юридическое лицо клиента",
    )
    own_company = models.ForeignKey(
        OwnCompany,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="billing_applications",
        verbose_name="Юрлицо FullBox",
    )
    manager = models.ForeignKey(
        Employee,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="billing_applications",
        verbose_name="Ответственный менеджер",
    )
    marketplace = models.ForeignKey(
        Market,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="billing_applications",
        verbose_name="Маркетплейс",
    )
    warehouse = models.ForeignKey(
        Store,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="billing_applications",
        verbose_name="Склад",
    )
    warehouse_label = models.CharField("Склад", max_length=255, blank=True)
    operational_status = models.CharField("Операционный статус", max_length=128, blank=True)
    operational_status_label = models.CharField("Операционный статус (текст)", max_length=255, blank=True)
    billing_status = models.CharField(
        "Статус биллинга",
        max_length=32,
        choices=BillingStatus.choices,
        default=BillingStatus.NOT_CALCULATED,
        db_index=True,
    )
    created_at_source = models.DateTimeField("Дата заявки", null=True, blank=True)
    operations_completed_at = models.DateTimeField("Дата завершения складских работ", null=True, blank=True)
    financially_closed_at = models.DateTimeField("Дата финансового закрытия", null=True, blank=True)
    is_operations_completed = models.BooleanField("Складские работы завершены", default=False, db_index=True)
    is_financially_closed = models.BooleanField("Финансово закрыта", default=False, db_index=True)
    act_confirmed_at = models.DateTimeField("Дата подтверждения акта", null=True, blank=True)
    invoice_required_at = models.DateTimeField("Дата требования счета", null=True, blank=True)
    charges_total = models.DecimalField("Начисления", max_digits=14, decimal_places=2, default=MONEY_ZERO)
    act_total = models.DecimalField("Сумма акта", max_digits=14, decimal_places=2, default=MONEY_ZERO)
    invoice_total = models.DecimalField("Сумма счета", max_digits=14, decimal_places=2, default=MONEY_ZERO)
    paid_total = models.DecimalField("Оплачено", max_digits=14, decimal_places=2, default=MONEY_ZERO)
    debt_total = models.DecimalField("Задолженность", max_digits=14, decimal_places=2, default=MONEY_ZERO)
    source_payload = models.JSONField("Снимок источника", default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Биллинг заявки"
        verbose_name_plural = "Биллинг заявок"
        ordering = ["-created_at_source", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["application_type", "application_id", "client"],
                name="uniq_billing_application_source",
            )
        ]
        indexes = [
            models.Index(fields=["client", "billing_status"]),
            models.Index(fields=["manager", "billing_status"]),
            models.Index(fields=["application_type", "application_id"]),
            models.Index(fields=["operations_completed_at"]),
            models.Index(fields=["financially_closed_at"]),
            models.Index(fields=["act_confirmed_at"]),
            models.Index(fields=["invoice_required_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.get_application_type_display()} {self.application_id}"

    @property
    def requires_invoice(self) -> bool:
        return self.billing_status == BillingStatus.INVOICE_REQUIRED


class BillingService(models.Model):
    code = models.SlugField("Код услуги", max_length=64, unique=True)
    name = models.CharField("Название", max_length=255)
    unit = models.CharField("Единица измерения", max_length=32, default="шт")
    vat_rate = models.CharField("Ставка НДС", max_length=16, default="20")
    category = models.ForeignKey(
        "TariffCategory",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="billing_services",
        verbose_name="Категория",
    )
    default_price = models.DecimalField("Цена по умолчанию", max_digits=14, decimal_places=4, null=True, blank=True)
    description = models.TextField("Описание", blank=True)
    sort_order = models.PositiveIntegerField("Порядок сортировки", default=100)
    used_in_billing = models.BooleanField("Используется в биллинге", default=True)
    is_active = models.BooleanField("Активна", default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Услуга биллинга"
        verbose_name_plural = "Услуги биллинга"
        ordering = ["sort_order", "name"]

    def __str__(self) -> str:
        return self.name


class ClientTariff(models.Model):
    client = models.ForeignKey(Agency, on_delete=models.CASCADE, related_name="billing_tariffs", verbose_name="Клиент")
    legal_entity = models.ForeignKey(
        Agency,
        on_delete=models.CASCADE,
        related_name="billing_legal_entity_tariffs",
        verbose_name="Юридическое лицо",
    )
    service = models.ForeignKey(BillingService, on_delete=models.PROTECT, related_name="client_tariffs", verbose_name="Услуга")
    unit = models.CharField("Единица измерения", max_length=32, default="шт")
    tariff = models.DecimalField("Тариф", max_digits=14, decimal_places=4)
    vat_rate = models.CharField("Ставка НДС", max_length=16, default="20")
    valid_from = models.DateField("Действует с")
    valid_to = models.DateField("Действует до", null=True, blank=True)
    is_active = models.BooleanField("Активен", default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Тариф клиента"
        verbose_name_plural = "Тарифы клиентов"
        ordering = ["client", "service", "-valid_from"]
        indexes = [
            models.Index(fields=["client", "service", "valid_from"]),
            models.Index(fields=["legal_entity", "is_active"]),
        ]

    def __str__(self) -> str:
        return f"{self.client} · {self.service}: {self.tariff}"


class StandardServicePrice(models.Model):
    service = models.OneToOneField(
        BillingService,
        on_delete=models.CASCADE,
        related_name="standard_price",
        verbose_name="Услуга",
    )
    section_code = models.CharField("Код раздела", max_length=16)
    section_name = models.CharField("Раздел", max_length=255)
    line_no = models.PositiveIntegerField("№ строки", default=0)
    name = models.CharField("Название из прайса", max_length=255)
    unit = models.CharField("Единица", max_length=32, default="шт")
    base_price = models.DecimalField("Базовая цена", max_digits=14, decimal_places=4, null=True, blank=True)
    price_note = models.CharField("Примечание к цене", max_length=255, blank=True)
    is_active = models.BooleanField("Активна", default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Стандартная цена"
        verbose_name_plural = "Стандартные цены"
        ordering = ["section_code", "line_no", "service__code"]
        indexes = [
            models.Index(fields=["section_code", "is_active"]),
            models.Index(fields=["is_active"]),
        ]

    def __str__(self) -> str:
        return f"{self.section_code}.{self.line_no} {self.name}"


class ClientBillingContract(models.Model):
    PRICING_BASE = "base"
    PRICING_MARKUP_5 = "markup_5"
    PRICING_INDIVIDUAL = "individual"
    PRICING_MODE_CHOICES = [
        (PRICING_BASE, "Стандартный прайс"),
        (PRICING_MARKUP_5, "Стандартный прайс + 5%"),
        (PRICING_INDIVIDUAL, "Индивидуальные тарифы"),
    ]

    client = models.ForeignKey(Agency, on_delete=models.CASCADE, related_name="billing_contracts", verbose_name="Клиент")
    own_company = models.ForeignKey(OwnCompany, on_delete=models.PROTECT, related_name="billing_contracts", verbose_name="Юрлицо FullBox")
    pricing_mode = models.CharField("Режим тарификации", max_length=32, choices=PRICING_MODE_CHOICES, default=PRICING_BASE)
    valid_from = models.DateField("Действует с", default=timezone.localdate)
    valid_to = models.DateField("Действует до", null=True, blank=True)
    is_active = models.BooleanField("Активен", default=True)
    comment = models.TextField("Комментарий", blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Договор биллинга клиента"
        verbose_name_plural = "Договоры биллинга клиентов"
        ordering = ["client", "-valid_from", "-id"]
        indexes = [
            models.Index(fields=["client", "is_active", "valid_from"]),
            models.Index(fields=["own_company", "is_active"]),
        ]

    def __str__(self) -> str:
        return f"{self.client} · {self.own_company} · {self.get_pricing_mode_display()}"


class TariffCategory(models.Model):
    code = models.SlugField("Код", max_length=64, unique=True)
    name = models.CharField("Название", max_length=255)
    description = models.TextField("Описание", blank=True)
    sort_order = models.PositiveIntegerField("Порядок", default=100)
    is_active = models.BooleanField("Активна", default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Категория тарифа"
        verbose_name_plural = "Категории тарифов"
        ordering = ["sort_order", "name"]
        indexes = [
            models.Index(fields=["is_active", "sort_order"]),
        ]

    def __str__(self) -> str:
        return self.name


class TariffUnit(models.Model):
    code = models.SlugField("Код", max_length=64, unique=True)
    name = models.CharField("Название", max_length=255)
    short_name = models.CharField("Сокращение", max_length=64)
    is_active = models.BooleanField("Активна", default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Единица расчёта тарифа"
        verbose_name_plural = "Единицы расчёта тарифов"
        ordering = ["name"]
        indexes = [
            models.Index(fields=["is_active", "code"]),
        ]

    def __str__(self) -> str:
        return self.short_name or self.name


class ClientTariffVersion(models.Model):
    STATUS_DRAFT = "draft"
    STATUS_SCHEDULED = "scheduled"
    STATUS_ACTIVE = "active"
    STATUS_EXPIRED = "expired"
    STATUS_ARCHIVED = "archived"
    STATUS_CHOICES = [
        (STATUS_DRAFT, "Черновик"),
        (STATUS_SCHEDULED, "Будут действовать"),
        (STATUS_ACTIVE, "Действуют"),
        (STATUS_EXPIRED, "Истекли"),
        (STATUS_ARCHIVED, "Архив"),
    ]

    VAT_WITH = "vat"
    VAT_NO = "no_vat"
    VAT_EXTRA = "vat_extra"
    VAT_TYPE_CHOICES = [
        (VAT_WITH, "С НДС"),
        (VAT_NO, "Без НДС"),
        (VAT_EXTRA, "НДС начисляется дополнительно"),
    ]

    client = models.ForeignKey(Agency, on_delete=models.CASCADE, related_name="tariff_versions", verbose_name="Клиент")
    contract = models.ForeignKey(
        ClientBillingContract,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="tariff_versions",
        verbose_name="Договор биллинга",
    )
    name = models.CharField("Название редакции", max_length=255)
    version_number = models.PositiveIntegerField("Номер версии", default=1)
    status = models.CharField("Статус", max_length=32, choices=STATUS_CHOICES, default=STATUS_DRAFT, db_index=True)
    valid_from = models.DateField("Действует с")
    valid_to = models.DateField("Действует до", null=True, blank=True)
    vat_type = models.CharField("Система налогообложения", max_length=32, choices=VAT_TYPE_CHOICES, default=VAT_WITH)
    currency = models.CharField("Валюта", max_length=8, default="RUB")
    contract_number = models.CharField("Номер договора", max_length=128, blank=True)
    contract_date = models.DateField("Дата договора", null=True, blank=True)
    additional_agreement_number = models.CharField("Номер доп. соглашения", max_length=128, blank=True)
    additional_agreement_date = models.DateField("Дата доп. соглашения", null=True, blank=True)
    manager = models.ForeignKey(
        Employee,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="client_tariff_versions",
        verbose_name="Ответственный менеджер",
    )
    general_comment = models.TextField("Общий комментарий", blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_client_tariff_versions",
        verbose_name="Кто создал",
    )
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="approved_client_tariff_versions",
        verbose_name="Кто утвердил",
    )
    approved_at = models.DateTimeField("Дата утверждения", null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Редакция тарифов клиента"
        verbose_name_plural = "Редакции тарифов клиентов"
        ordering = ["client", "-valid_from", "-version_number"]
        constraints = [
            models.UniqueConstraint(fields=["client", "version_number"], name="uniq_client_tariff_version_number"),
        ]
        indexes = [
            models.Index(fields=["client", "status"]),
            models.Index(fields=["client", "valid_from"]),
            models.Index(fields=["client", "valid_to"]),
            models.Index(fields=["status", "valid_from"]),
        ]

    @property
    def effective_status(self) -> str:
        today = timezone.localdate()
        if self.status == self.STATUS_ARCHIVED:
            return self.STATUS_ARCHIVED
        if self.valid_from > today:
            return self.STATUS_SCHEDULED
        if self.valid_to and self.valid_to < today:
            return self.STATUS_EXPIRED
        if self.status in {self.STATUS_ACTIVE, self.STATUS_SCHEDULED, self.STATUS_EXPIRED}:
            return self.STATUS_ACTIVE
        return self.status

    def clean(self):
        if self.valid_to and self.valid_to < self.valid_from:
            raise ValidationError({"valid_to": "Дата окончания не может быть раньше даты начала."})
        active_statuses = {self.STATUS_ACTIVE, self.STATUS_SCHEDULED}
        if self.status in active_statuses:
            qs = type(self).objects.filter(client=self.client, status__in=active_statuses).exclude(pk=self.pk)
            qs = qs.filter(Q(valid_to__isnull=True) | Q(valid_to__gte=self.valid_from))
            if self.valid_to:
                qs = qs.filter(valid_from__lte=self.valid_to)
            if qs.exists():
                raise ValidationError("Для клиента уже есть активная редакция тарифов с пересекающимся периодом.")

    def billing_usage_summary(self) -> dict:
        if not self.pk:
            return {"charges_count": 0, "act_count": 0, "invoice_count": 0, "storage_charge_days_count": 0}
        charges = self.charges.all()
        storage_days = self.storage_days.filter(charge__isnull=False)
        return {
            "charges_count": charges.count(),
            "act_count": charges.filter(is_included_in_act=True).count(),
            "invoice_count": charges.filter(is_included_in_invoice=True).count(),
            "storage_charge_days_count": storage_days.count(),
        }

    def is_used_for_billing(self) -> bool:
        if not self.pk:
            return False
        return self.charges.exists()

    def can_edit_directly(self) -> bool:
        if self.status == self.STATUS_DRAFT:
            return True
        if self.status == self.STATUS_ARCHIVED:
            return False
        return not self.is_used_for_billing()

    def edit_block_reason(self) -> str:
        if self.status == self.STATUS_ARCHIVED:
            return "Архивную редакцию тарифа нельзя редактировать. Создайте новую версию."
        usage = self.billing_usage_summary()
        if usage["charges_count"]:
            return (
                "Тарифы уже используются в начислениях: "
                f"{usage['charges_count']} строк услуг"
                f", в актах: {usage['act_count']}"
                f", в счетах: {usage['invoice_count']}. "
                "Редактирование этой редакции невозможно — создайте новую версию."
            )
        return ""

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"{self.client} · {self.name}"


class ClientTariffItem(models.Model):
    CALCULATION_FIXED = "fixed"
    CALCULATION_BY_UNIT = "by_unit"
    CALCULATION_COEFFICIENT = "coefficient"
    CALCULATION_CHOICES = [
        (CALCULATION_BY_UNIT, "За единицу"),
        (CALCULATION_FIXED, "Фиксированная стоимость"),
        (CALCULATION_COEFFICIENT, "Коэффициент"),
    ]

    tariff_version = models.ForeignKey(ClientTariffVersion, on_delete=models.CASCADE, related_name="items", verbose_name="Редакция тарифов")
    category = models.ForeignKey(TariffCategory, on_delete=models.PROTECT, related_name="tariff_items", verbose_name="Категория")
    service = models.ForeignKey(BillingService, on_delete=models.PROTECT, related_name="tariff_items", verbose_name="Услуга")
    service_name = models.CharField("Название услуги в редакции", max_length=255)
    description = models.TextField("Описание", blank=True)
    unit = models.ForeignKey(TariffUnit, on_delete=models.PROTECT, related_name="tariff_items", verbose_name="Единица расчёта")
    price = models.DecimalField("Цена", max_digits=14, decimal_places=4)
    minimum_amount = models.DecimalField("Минимальная сумма", max_digits=14, decimal_places=2, null=True, blank=True)
    minimum_quantity = models.DecimalField("Минимальное количество", max_digits=14, decimal_places=3, null=True, blank=True)
    included_materials = models.TextField("Материалы", blank=True)
    conditions = models.TextField("Условия применения", blank=True)
    calculation_type = models.CharField("Тип расчёта", max_length=32, choices=CALCULATION_CHOICES, default=CALCULATION_BY_UNIT)
    coefficient = models.DecimalField("Коэффициент", max_digits=8, decimal_places=4, default=Decimal("1.0000"))
    sort_order = models.PositiveIntegerField("Порядок", default=100)
    is_active = models.BooleanField("Активен", default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Позиция тарифов клиента"
        verbose_name_plural = "Позиции тарифов клиентов"
        ordering = ["category__sort_order", "sort_order", "service_name"]
        constraints = [
            models.UniqueConstraint(fields=["tariff_version", "service", "conditions"], name="uniq_client_tariff_item_service_condition"),
        ]
        indexes = [
            models.Index(fields=["tariff_version", "is_active"]),
            models.Index(fields=["service", "is_active"]),
            models.Index(fields=["category", "is_active"]),
        ]

    def clean(self):
        if self.price < 0:
            raise ValidationError({"price": "Цена не может быть отрицательной."})
        if self.minimum_amount is not None and self.minimum_amount < 0:
            raise ValidationError({"minimum_amount": "Минимальная сумма не может быть отрицательной."})
        if self.minimum_quantity is not None and self.minimum_quantity < 0:
            raise ValidationError({"minimum_quantity": "Минимальное количество не может быть отрицательным."})
        if self.coefficient <= 0:
            raise ValidationError({"coefficient": "Коэффициент должен быть больше нуля."})
        # После первого начисления позиции не переписываются, но бухгалтер может
        # добавить отдельную новую услугу. У новой строки ещё нет pk.
        if self.tariff_version_id and not self.tariff_version.can_edit_directly() and self.pk:
            raise ValidationError(self.tariff_version.edit_block_reason() or "Нельзя менять позиции этой редакции тарифа.")

    def save(self, *args, **kwargs):
        if not self.service_name and self.service_id:
            self.service_name = self.service.name
        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"{self.tariff_version}: {self.service_name}"


class FbsClientRate(models.Model):
    """Client-specific FBS price band, independent from warehouse write paths."""

    OP_RECEIVING = "receiving"
    OP_PICKING = "picking"
    OP_MARKING = "marking"
    OP_CHZ_CHECK = "chz_check"
    OP_SHIPPING = "shipping"
    OP_STORAGE = "storage"
    OPERATION_CHOICES = [
        (OP_RECEIVING, "Приемка"),
        (OP_PICKING, "Подбор"),
        (OP_MARKING, "Маркировка"),
        (OP_CHZ_CHECK, "Проверка ЧЗ"),
        (OP_SHIPPING, "Отгрузка FBS"),
        (OP_STORAGE, "Хранение"),
    ]

    client = models.ForeignKey(
        Agency,
        on_delete=models.CASCADE,
        related_name="fbs_client_rates",
        verbose_name="Клиент",
    )
    operation = models.CharField("Операция FBS", max_length=16, choices=OPERATION_CHOICES)
    liters_from = models.DecimalField(
        "Больше, чем литров", max_digits=12, decimal_places=3, default=Decimal("0")
    )
    liters_to = models.DecimalField(
        "До литров включительно", max_digits=12, decimal_places=3, null=True, blank=True
    )
    price = models.DecimalField("Цена за единицу", max_digits=14, decimal_places=4)
    unit = models.CharField("Единица расчета", max_length=16, default="шт")
    valid_from = models.DateField("Действует с")
    valid_to = models.DateField("Действует до", null=True, blank=True)
    vat_rate = models.CharField("Ставка НДС", max_length=16, default="5")
    vat_type = models.CharField(
        "Режим НДС", max_length=32, default=ClientTariffVersion.VAT_EXTRA
    )
    is_active = models.BooleanField("Активна", default=True)
    comment = models.CharField("Комментарий", max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Ставка FBS клиента"
        verbose_name_plural = "Ставки FBS клиентов"
        ordering = ["client", "operation", "valid_from", "liters_from", "id"]
        indexes = [
            models.Index(fields=["client", "operation", "valid_from"]),
            models.Index(fields=["client", "is_active"]),
        ]

    def clean(self):
        if self.liters_from < 0:
            raise ValidationError({"liters_from": "Граница литража не может быть отрицательной."})
        if self.liters_to is not None and self.liters_to <= self.liters_from:
            raise ValidationError({"liters_to": "Верхняя граница должна быть больше нижней."})
        if self.price < 0:
            raise ValidationError({"price": "Цена не может быть отрицательной."})
        if self.valid_to and self.valid_to < self.valid_from:
            raise ValidationError({"valid_to": "Дата окончания не может быть раньше даты начала."})

    def __str__(self) -> str:
        upper = str(self.liters_to) if self.liters_to is not None else "∞"
        return f"{self.client}: {self.get_operation_display()} ({self.liters_from}; {upper}]"


class ClientTariffCondition(models.Model):
    TYPE_FREE_STORAGE = "free_storage"
    TYPE_MIN_MONTHLY = "min_monthly"
    TYPE_MATERIALS = "materials"
    TYPE_URGENT_COEFFICIENT = "urgent_coefficient"
    TYPE_VOLUME_THRESHOLD = "volume_threshold"
    TYPE_DELIVERY_SEPARATE = "delivery_separate"
    TYPE_MIN_ORDER = "min_order"
    TYPE_OTHER = "other"
    CONDITION_TYPE_CHOICES = [
        (TYPE_FREE_STORAGE, "Бесплатное хранение"),
        (TYPE_MIN_MONTHLY, "Минимальный ежемесячный платёж"),
        (TYPE_MATERIALS, "Материалы"),
        (TYPE_URGENT_COEFFICIENT, "Срочность"),
        (TYPE_VOLUME_THRESHOLD, "Порог объёма"),
        (TYPE_DELIVERY_SEPARATE, "Доставка отдельно"),
        (TYPE_MIN_ORDER, "Минимальная стоимость заявки"),
        (TYPE_OTHER, "Другое"),
    ]

    tariff_version = models.ForeignKey(ClientTariffVersion, on_delete=models.CASCADE, related_name="conditions", verbose_name="Редакция тарифов")
    condition_type = models.CharField("Тип условия", max_length=64, choices=CONDITION_TYPE_CHOICES, default=TYPE_OTHER)
    name = models.CharField("Название", max_length=255)
    description = models.TextField("Описание", blank=True)
    value = models.CharField("Значение", max_length=128, blank=True)
    unit = models.ForeignKey(TariffUnit, on_delete=models.SET_NULL, null=True, blank=True, related_name="tariff_conditions", verbose_name="Единица")
    valid_from = models.DateField("Действует с", null=True, blank=True)
    valid_to = models.DateField("Действует до", null=True, blank=True)
    sort_order = models.PositiveIntegerField("Порядок", default=100)
    is_active = models.BooleanField("Активно", default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Специальное условие тарифа"
        verbose_name_plural = "Специальные условия тарифов"
        ordering = ["sort_order", "name"]
        indexes = [
            models.Index(fields=["tariff_version", "is_active"]),
            models.Index(fields=["condition_type", "is_active"]),
        ]

    def clean(self):
        if self.valid_from and self.valid_to and self.valid_to < self.valid_from:
            raise ValidationError({"valid_to": "Дата окончания не может быть раньше даты начала."})
        if self.tariff_version_id and not self.tariff_version.can_edit_directly():
            raise ValidationError(self.tariff_version.edit_block_reason() or "Нельзя менять условия этой редакции тарифа.")

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return self.name


class ApplicationCharge(models.Model):
    SOURCE_MANUAL = "manual"
    SOURCE_LEGACY_CHARGE = "client_service_charge"
    SOURCE_SHIPPING_TN = "shipping_transport_note"
    SOURCE_STORAGE_DAY = "storage_day"
    SOURCE_WAREHOUSE_APPLICATION = "warehouse_application"
    SOURCE_LOGISTICS_TRIP = "logistics_trip"
    SOURCE_CHOICES = [
        (SOURCE_MANUAL, "Ручное начисление"),
        (SOURCE_LEGACY_CHARGE, "Начисление ЛК клиента"),
        (SOURCE_SHIPPING_TN, "Транспортная накладная"),
        (SOURCE_STORAGE_DAY, "Хранение палето-день"),
        (SOURCE_WAREHOUSE_APPLICATION, "Складская заявка"),
        (SOURCE_LOGISTICS_TRIP, "Внешний рейс"),
    ]

    EXCLUDE_NOT_PERFORMED = "not_performed"
    EXCLUDE_WRONG_AUTO = "wrong_auto"
    EXCLUDE_DUPLICATE = "duplicate"
    EXCLUDE_INCLUDED_IN_OTHER = "included_in_other"
    EXCLUDE_OTHER = "other"
    EXCLUDE_REASON_CHOICES = [
        (EXCLUDE_NOT_PERFORMED, "Услуга фактически не оказывалась"),
        (EXCLUDE_WRONG_AUTO, "Автоматика определила неправильную операцию"),
        (EXCLUDE_DUPLICATE, "Дублирующее начисление"),
        (EXCLUDE_INCLUDED_IN_OTHER, "Услуга включена в другую услугу"),
        (EXCLUDE_OTHER, "Другое"),
    ]

    QTY_BASIS_WAREHOUSE = "warehouse"
    QTY_BASIS_FACT = "fact"
    QTY_BASIS_BOXES = "boxes"
    QTY_BASIS_PALLETS = "pallets"
    QTY_BASIS_UNITS = "units"
    QTY_BASIS_MANUAL = "manual"
    QTY_BASIS_OTHER = "other"
    QTY_BASIS_CHOICES = [
        (QTY_BASIS_WAREHOUSE, "Данные склада"),
        (QTY_BASIS_FACT, "Фактическое количество"),
        (QTY_BASIS_BOXES, "Количество коробов"),
        (QTY_BASIS_PALLETS, "Количество палет"),
        (QTY_BASIS_UNITS, "Количество единиц товара"),
        (QTY_BASIS_MANUAL, "Ручная корректировка"),
        (QTY_BASIS_OTHER, "Другое"),
    ]

    application = models.ForeignKey(BillingApplication, on_delete=models.CASCADE, related_name="charges", verbose_name="Заявка")
    client = models.ForeignKey(Agency, on_delete=models.PROTECT, related_name="billing_charges", verbose_name="Клиент")
    legal_entity = models.ForeignKey(
        Agency,
        on_delete=models.PROTECT,
        related_name="billing_legal_entity_charges",
        verbose_name="Юридическое лицо",
    )
    service = models.ForeignKey(BillingService, on_delete=models.PROTECT, related_name="charges", verbose_name="Услуга")
    operation_type = models.CharField("Источник операции", max_length=64, blank=True)
    operation_id = models.CharField("ID операции", max_length=128, blank=True)
    quantity = models.DecimalField("Количество", max_digits=14, decimal_places=3)
    unit = models.CharField("Единица", max_length=32, default="шт")
    tariff = models.DecimalField("Применённая цена", max_digits=14, decimal_places=4)
    tariff_price = models.DecimalField("Цена по согласованному тарифу", max_digits=14, decimal_places=4, null=True, blank=True)
    coefficient = models.DecimalField("Коэффициент", max_digits=8, decimal_places=4, default=Decimal("1.0000"))
    minimum_amount = models.DecimalField("Минимальная сумма", max_digits=14, decimal_places=2, null=True, blank=True)
    amount = models.DecimalField("Сумма без НДС", max_digits=14, decimal_places=2, default=MONEY_ZERO)
    vat_rate = models.CharField("Ставка НДС", max_length=16, default="20")
    vat_amount = models.DecimalField("Сумма НДС", max_digits=14, decimal_places=2, default=MONEY_ZERO)
    total_amount = models.DecimalField("Итого", max_digits=14, decimal_places=2, default=MONEY_ZERO)
    # Snapshot согласованного тарифа — счёт не меняется после новой редакции
    client_tariff_version = models.ForeignKey(
        "ClientTariffVersion",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="charges",
        verbose_name="Редакция тарифов",
    )
    client_tariff_item = models.ForeignKey(
        "ClientTariffItem",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="charges",
        verbose_name="Позиция тарифа",
    )
    client_logistics_tariff = models.ForeignKey(
        "ClientLogisticsTariff",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="charges",
        verbose_name="Редакция логистического тарифа",
    )
    client_logistics_tariff_item = models.ForeignKey(
        "ClientLogisticsTariffItem",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="charges",
        verbose_name="Направление логистического тарифа",
    )
    service_name_snapshot = models.CharField("Название услуги (снимок)", max_length=255, blank=True)
    previous_service_name = models.CharField("Предыдущая услуга", max_length=255, blank=True)
    service_changed_at = models.DateTimeField("Услуга изменена менеджером", null=True, blank=True, db_index=True)
    tariff_source_label = models.CharField("Источник цены", max_length=255, blank=True)
    tariff_basis = models.CharField("Основание тарифа", max_length=255, blank=True)
    own_company = models.ForeignKey(
        OwnCompany,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="billing_charges",
        verbose_name="Компания обслуживания FullBox",
    )
    vat_type_snapshot = models.CharField("Формат НДС (снимок)", max_length=32, blank=True)
    price_source = models.CharField("Источник цены (код)", max_length=64, blank=True, default="client_tariff")
    source_type = models.CharField("Тип источника", max_length=64, choices=SOURCE_CHOICES, default=SOURCE_MANUAL)
    source_id = models.CharField("ID источника", max_length=128, blank=True)
    source_key = models.CharField("Ключ источника", max_length=160, blank=True, db_index=True)
    performed_at = models.DateTimeField("Дата выполнения", null=True, blank=True)
    billing_period = models.DateField("Период биллинга")
    is_confirmed = models.BooleanField("Подтверждено", default=False)
    is_included_in_act = models.BooleanField("В акте", default=False)
    is_included_in_invoice = models.BooleanField("В счете", default=False)
    is_disputed = models.BooleanField("Спорная строка", default=False)
    is_excluded = models.BooleanField("Исключено из расчёта", default=False, db_index=True)
    excluded_at = models.DateTimeField("Исключено когда", null=True, blank=True)
    excluded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="excluded_billing_charges",
        verbose_name="Кто исключил",
    )
    exclude_reason = models.CharField(
        "Причина исключения",
        max_length=32,
        choices=EXCLUDE_REASON_CHOICES,
        blank=True,
    )
    exclude_comment = models.TextField("Комментарий к исключению", blank=True)
    original_quantity = models.DecimalField(
        "Первоначальное количество",
        max_digits=14,
        decimal_places=3,
        null=True,
        blank=True,
    )
    qty_change_basis = models.CharField(
        "Основание изменения количества",
        max_length=32,
        choices=QTY_BASIS_CHOICES,
        blank=True,
    )
    qty_change_comment = models.TextField("Комментарий к изменению количества", blank=True)
    edit_version = models.PositiveIntegerField("Версия для оптимистичной блокировки", default=1)
    is_manual_override = models.BooleanField("Цена изменена вручную", default=False, db_index=True)
    override_reason = models.TextField("Причина ручного изменения цены", blank=True)
    overridden_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="overridden_billing_charges",
        verbose_name="Кто изменил цену",
    )
    overridden_at = models.DateTimeField("Когда изменена цена", null=True, blank=True)
    comment = models.TextField("Комментарий", blank=True)
    correction_of = models.ForeignKey(
        "self",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="corrections",
        verbose_name="Корректирует строку",
    )
    correction_reason = models.TextField("Причина корректировки", blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_billing_charges",
        verbose_name="Кто создал",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Начисление по заявке"
        verbose_name_plural = "Начисления по заявкам"
        ordering = ["-performed_at", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["application", "source_key"],
                condition=~Q(source_key=""),
                name="uniq_billing_charge_source",
            )
        ]
        indexes = [
            models.Index(fields=["application", "is_included_in_act"]),
            models.Index(fields=["application", "is_excluded"]),
            models.Index(fields=["client", "billing_period"]),
            models.Index(fields=["source_type", "source_id"]),
            models.Index(fields=["is_included_in_invoice"]),
        ]

    def __str__(self) -> str:
        return f"{self.application}: {self.service} {self.total_amount}"


class ApplicationChargeHistory(models.Model):
    CHANGE_SERVICE = "service"
    CHANGE_QTY = "qty"
    CHANGE_TARIFF = "tariff"
    CHANGE_EXCLUDE = "exclude"
    CHANGE_RESTORE = "restore"
    CHANGE_ADD = "add"
    CHANGE_CONFIRM = "confirm"
    CHANGE_UNCONFIRM = "unconfirm"
    CHANGE_RECALC = "recalc"
    CHANGE_TYPE_CHOICES = [
        (CHANGE_SERVICE, "Вид услуги"),
        (CHANGE_QTY, "Количество"),
        (CHANGE_TARIFF, "Тариф"),
        (CHANGE_EXCLUDE, "Исключение"),
        (CHANGE_RESTORE, "Восстановление"),
        (CHANGE_ADD, "Добавление"),
        (CHANGE_CONFIRM, "Подтверждение"),
        (CHANGE_UNCONFIRM, "Снятие подтверждения"),
        (CHANGE_RECALC, "Пересчёт"),
    ]

    charge = models.ForeignKey(
        ApplicationCharge,
        on_delete=models.CASCADE,
        related_name="history_entries",
        verbose_name="Начисление",
    )
    application = models.ForeignKey(
        BillingApplication,
        on_delete=models.CASCADE,
        related_name="charge_history",
        verbose_name="Заявка",
    )
    change_type = models.CharField("Тип изменения", max_length=32, choices=CHANGE_TYPE_CHOICES, db_index=True)
    old_service_name = models.CharField("Старая услуга", max_length=255, blank=True)
    new_service_name = models.CharField("Новая услуга", max_length=255, blank=True)
    old_quantity = models.DecimalField("Старое количество", max_digits=14, decimal_places=3, null=True, blank=True)
    new_quantity = models.DecimalField("Новое количество", max_digits=14, decimal_places=3, null=True, blank=True)
    old_tariff = models.DecimalField("Старый тариф", max_digits=14, decimal_places=4, null=True, blank=True)
    new_tariff = models.DecimalField("Новый тариф", max_digits=14, decimal_places=4, null=True, blank=True)
    old_total = models.DecimalField("Старая сумма", max_digits=14, decimal_places=2, null=True, blank=True)
    new_total = models.DecimalField("Новая сумма", max_digits=14, decimal_places=2, null=True, blank=True)
    reason = models.CharField("Причина", max_length=64, blank=True)
    comment = models.TextField("Комментарий", blank=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="billing_charge_history",
        verbose_name="Пользователь",
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = "История начисления"
        verbose_name_plural = "История начислений"
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["charge", "-created_at"]),
            models.Index(fields=["application", "-created_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.charge_id}: {self.change_type} @ {self.created_at}"


class BillingAct(models.Model):
    application = models.ForeignKey(BillingApplication, on_delete=models.CASCADE, related_name="acts", verbose_name="Заявка")
    client = models.ForeignKey(Agency, on_delete=models.PROTECT, related_name="billing_acts", verbose_name="Клиент")
    legal_entity = models.ForeignKey(
        Agency,
        on_delete=models.PROTECT,
        related_name="billing_legal_entity_acts",
        verbose_name="Юридическое лицо",
    )
    number = models.CharField("Номер акта", max_length=64, db_index=True)
    version = models.PositiveIntegerField("Версия", default=1)
    act_date = models.DateField("Дата акта", default=timezone.localdate)
    status = models.CharField("Статус", max_length=32, choices=ActStatus.choices, default=ActStatus.DRAFT, db_index=True)
    subtotal = models.DecimalField("Сумма без НДС", max_digits=14, decimal_places=2, default=MONEY_ZERO)
    vat_amount = models.DecimalField("НДС", max_digits=14, decimal_places=2, default=MONEY_ZERO)
    total_amount = models.DecimalField("Итого", max_digits=14, decimal_places=2, default=MONEY_ZERO)
    sent_at = models.DateTimeField("Отправлен клиенту", null=True, blank=True)
    confirmed_at = models.DateTimeField("Подтвержден клиентом", null=True, blank=True)
    confirmed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="confirmed_billing_acts",
        verbose_name="Кто подтвердил",
    )
    confirmed_amount = models.DecimalField("Подтвержденная сумма", max_digits=14, decimal_places=2, default=MONEY_ZERO)
    client_comment = models.TextField("Комментарий клиента", blank=True)
    client_ip = models.GenericIPAddressField("IP клиента", null=True, blank=True)
    client_user_agent = models.TextField("User-Agent клиента", blank=True)
    manager_comment = models.TextField("Комментарий менеджера", blank=True)
    file = models.FileField("Файл акта", upload_to=billing_document_upload_to, null=True, blank=True)
    review_status = models.CharField(
        "Проверка бухгалтером",
        max_length=32,
        choices=DocumentReviewStatus.choices,
        default=DocumentReviewStatus.LOCAL,
        db_index=True,
    )
    submitted_at = models.DateTimeField("Передан бухгалтеру", null=True, blank=True)
    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="submitted_billing_acts",
    )
    reviewed_at = models.DateTimeField("Проверен бухгалтером", null=True, blank=True)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reviewed_billing_acts",
    )
    accountant_comment = models.TextField("Комментарий бухгалтера", blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_billing_acts",
        verbose_name="Кто создал",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Акт биллинга"
        verbose_name_plural = "Акты биллинга"
        ordering = ["-act_date", "-id"]
        constraints = [
            models.UniqueConstraint(fields=["number", "version"], name="uniq_billing_act_number_version"),
        ]
        indexes = [
            models.Index(fields=["application", "status"]),
            models.Index(fields=["client", "status"]),
            models.Index(fields=["confirmed_at"]),
            models.Index(fields=["review_status", "submitted_at"]),
        ]

    def __str__(self) -> str:
        return f"Акт {self.number} v{self.version}"


class BillingActLine(models.Model):
    act = models.ForeignKey(BillingAct, on_delete=models.CASCADE, related_name="lines", verbose_name="Акт")
    charge = models.ForeignKey(ApplicationCharge, on_delete=models.PROTECT, related_name="act_lines", verbose_name="Начисление")
    service_name = models.CharField("Услуга", max_length=255)
    quantity = models.DecimalField("Количество", max_digits=14, decimal_places=3)
    unit = models.CharField("Единица", max_length=32)
    tariff = models.DecimalField("Тариф", max_digits=14, decimal_places=4)
    amount = models.DecimalField("Сумма", max_digits=14, decimal_places=2)
    vat_amount = models.DecimalField("НДС", max_digits=14, decimal_places=2, default=MONEY_ZERO)
    total_amount = models.DecimalField("Итого", max_digits=14, decimal_places=2)

    class Meta:
        verbose_name = "Строка акта"
        verbose_name_plural = "Строки акта"
        constraints = [
            models.UniqueConstraint(fields=["act", "charge"], name="uniq_billing_act_charge"),
        ]


class BillingActDispute(models.Model):
    act = models.ForeignKey(BillingAct, on_delete=models.CASCADE, related_name="disputes", verbose_name="Акт")
    line = models.ForeignKey(
        BillingActLine,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="disputes",
        verbose_name="Строка акта",
    )
    expected_quantity = models.DecimalField("Ожидаемое количество", max_digits=14, decimal_places=3, null=True, blank=True)
    expected_amount = models.DecimalField("Ожидаемая сумма", max_digits=14, decimal_places=2, null=True, blank=True)
    comment = models.TextField("Комментарий")
    file = models.FileField("Файл", upload_to=billing_document_upload_to, null=True, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "Разногласие по акту"
        verbose_name_plural = "Разногласия по актам"
        ordering = ["-created_at"]


class ClientInvoice(models.Model):
    TYPE_REGULAR = "regular"
    TYPE_CORRECTION = "correction"
    INVOICE_TYPE_CHOICES = [
        (TYPE_REGULAR, "Обычный"),
        (TYPE_CORRECTION, "Корректировка"),
    ]

    CONTOUR_FBO = "fbo"
    CONTOUR_FBS = "fbs"
    BILLING_CONTOUR_CHOICES = [
        (CONTOUR_FBO, "FBO / основные услуги"),
        (CONTOUR_FBS, "FBS"),
    ]

    application = models.ForeignKey(BillingApplication, on_delete=models.CASCADE, related_name="invoices", verbose_name="Заявка")
    client = models.ForeignKey(Agency, on_delete=models.PROTECT, related_name="billing_invoices", verbose_name="Клиент")
    legal_entity = models.ForeignKey(
        Agency,
        on_delete=models.PROTECT,
        related_name="billing_legal_entity_invoices",
        verbose_name="Юридическое лицо",
    )
    act = models.ForeignKey(BillingAct, on_delete=models.PROTECT, related_name="invoices", verbose_name="Основной акт")
    linked_acts = models.ManyToManyField(
        BillingAct,
        through="ClientInvoiceAct",
        related_name="grouped_invoices",
        verbose_name="Акты в счёте",
        blank=True,
    )
    number = models.CharField("Номер счета", max_length=64, db_index=True)
    invoice_type = models.CharField("Тип счета", max_length=32, choices=INVOICE_TYPE_CHOICES, default=TYPE_REGULAR)
    billing_contour = models.CharField(
        "Контур биллинга",
        max_length=16,
        choices=BILLING_CONTOUR_CHOICES,
        default=CONTOUR_FBO,
        db_index=True,
    )
    billing_period = models.DateField("Период биллинга")
    invoice_date = models.DateField("Дата счета", default=timezone.localdate)
    due_date = models.DateField("Срок оплаты")
    subtotal = models.DecimalField("Сумма без НДС", max_digits=14, decimal_places=2, default=MONEY_ZERO)
    vat_amount = models.DecimalField("НДС", max_digits=14, decimal_places=2, default=MONEY_ZERO)
    total_amount = models.DecimalField("Итого", max_digits=14, decimal_places=2, default=MONEY_ZERO)
    paid_amount = models.DecimalField("Оплачено", max_digits=14, decimal_places=2, default=MONEY_ZERO)
    debt_amount = models.DecimalField("Задолженность", max_digits=14, decimal_places=2, default=MONEY_ZERO)
    status = models.CharField("Статус", max_length=32, choices=InvoiceStatus.choices, default=InvoiceStatus.DRAFT, db_index=True)
    sent_at = models.DateTimeField("Отправлен", null=True, blank=True)
    paid_at = models.DateTimeField("Оплачен", null=True, blank=True)
    external_system = models.CharField("Внешняя система", max_length=64, blank=True)
    external_id = models.CharField("Внешний ID", max_length=128, blank=True)
    external_url = models.URLField("Внешняя ссылка", blank=True)
    supplier_snapshot = models.JSONField("Снимок реквизитов поставщика", default=dict, blank=True)
    vat_rate_snapshot = models.CharField("Ставка НДС в счёте", max_length=16, blank=True)
    file = models.FileField("Файл счета", upload_to=billing_document_upload_to, null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_client_invoices",
    )
    checked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="checked_client_invoices",
    )
    review_status = models.CharField(
        "Проверка бухгалтером",
        max_length=32,
        choices=DocumentReviewStatus.choices,
        default=DocumentReviewStatus.LOCAL,
        db_index=True,
    )
    submitted_at = models.DateTimeField("Передан бухгалтеру", null=True, blank=True)
    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="submitted_client_invoices",
    )
    reviewed_at = models.DateTimeField("Проверен бухгалтером", null=True, blank=True)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reviewed_client_invoices",
    )
    accountant_comment = models.TextField("Комментарий бухгалтера", blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Счет клиента"
        verbose_name_plural = "Счета клиентов"
        ordering = ["-invoice_date", "-id"]
        constraints = [
            models.UniqueConstraint(fields=["number"], name="uniq_billing_invoice_number"),
            models.UniqueConstraint(
                fields=["application", "legal_entity", "billing_period", "invoice_type"],
                condition=~Q(status=InvoiceStatus.CANCELLED),
                name="uniq_active_billing_invoice",
            ),
        ]
        indexes = [
            models.Index(fields=["client", "status"]),
            models.Index(fields=["status", "due_date"]),
            models.Index(fields=["act", "status"]),
            models.Index(fields=["billing_period"]),
            models.Index(fields=["review_status", "submitted_at"]),
        ]

    def __str__(self) -> str:
        return f"Счет {self.number}"

    def get_linked_acts(self):
        """Акты, входящие в счёт (через M2M или основной act для старых записей)."""
        qs = (
            BillingAct.objects.filter(invoice_links__invoice=self)
            .select_related("application", "client", "legal_entity")
            .prefetch_related("lines__charge__service")
            .order_by("invoice_links__sort_order", "act_date", "id")
            .distinct()
        )
        if qs.exists():
            return qs
        if self.act_id:
            return (
                BillingAct.objects.filter(pk=self.act_id)
                .select_related("application", "client", "legal_entity")
                .prefetch_related("lines__charge__service")
            )
        return BillingAct.objects.none()

    def linked_applications(self):
        app_ids = list(self.get_linked_acts().values_list("application_id", flat=True))
        if self.application_id and self.application_id not in app_ids:
            app_ids.append(self.application_id)
        return BillingApplication.objects.filter(pk__in=app_ids).select_related("client")

    def clean(self):
        super().clean()
        if not self.application_id:
            return
        expected = (
            self.CONTOUR_FBS
            if self.application.application_type == BillingApplication.TYPE_FBS
            else self.CONTOUR_FBO
        )
        if self.billing_contour != expected:
            raise ValidationError(
                {"billing_contour": "Контур счета не совпадает с типом заявки биллинга."}
            )

    @property
    def acts_count(self) -> int:
        count = self.invoice_acts.count()
        if count:
            return count
        return 1 if self.act_id else 0


class ClientInvoiceAct(models.Model):
    """Связь счёта с одним или несколькими подтверждёнными актами."""

    invoice = models.ForeignKey(
        ClientInvoice,
        on_delete=models.CASCADE,
        related_name="invoice_acts",
        verbose_name="Счёт",
    )
    act = models.ForeignKey(
        BillingAct,
        on_delete=models.PROTECT,
        related_name="invoice_links",
        verbose_name="Акт",
    )
    sort_order = models.PositiveIntegerField("Порядок", default=0)

    class Meta:
        verbose_name = "Акт в счёте"
        verbose_name_plural = "Акты в счетах"
        ordering = ["sort_order", "id"]
        constraints = [
            models.UniqueConstraint(fields=["invoice", "act"], name="uniq_billing_invoice_act"),
        ]
        indexes = [
            models.Index(fields=["act"]),
            models.Index(fields=["invoice", "sort_order"]),
        ]

    def __str__(self) -> str:
        return f"{self.invoice_id} ← {self.act_id}"


class InvoicePayment(models.Model):
    invoice = models.ForeignKey(ClientInvoice, on_delete=models.CASCADE, related_name="payments", verbose_name="Счет")
    amount = models.DecimalField("Сумма", max_digits=14, decimal_places=2)
    paid_at = models.DateTimeField("Дата оплаты", default=timezone.now)
    status = models.CharField("Статус", max_length=32, choices=PaymentStatus.choices, default=PaymentStatus.CONFIRMED)
    source = models.CharField("Источник", max_length=64, default="manual")
    external_id = models.CharField("Внешний ID", max_length=128, blank=True)
    comment = models.TextField("Комментарий", blank=True)
    registered_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Оплата счета"
        verbose_name_plural = "Оплаты счетов"
        ordering = ["-paid_at", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["invoice", "external_id"],
                condition=~Q(external_id=""),
                name="uniq_billing_payment_external_id",
            ),
        ]
        indexes = [
            models.Index(fields=["invoice", "status"]),
            models.Index(fields=["paid_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.invoice.number}: {self.amount}"


class BillingAuditEvent(models.Model):
    application = models.ForeignKey(
        BillingApplication,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="audit_events",
        verbose_name="Заявка",
    )
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    action = models.CharField("Действие", max_length=64, db_index=True)
    object_type = models.CharField("Тип объекта", max_length=64, blank=True)
    object_id = models.CharField("ID объекта", max_length=128, blank=True)
    old_value = models.JSONField("Старое значение", null=True, blank=True)
    new_value = models.JSONField("Новое значение", null=True, blank=True)
    comment = models.TextField("Комментарий", blank=True)
    source = models.CharField("Источник", max_length=64, default="wms")
    ip_address = models.GenericIPAddressField("IP", null=True, blank=True)
    user_agent = models.TextField("User-Agent", blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Аудит биллинга"
        verbose_name_plural = "Аудит биллинга"
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["application", "created_at"]),
            models.Index(fields=["object_type", "object_id"]),
            models.Index(fields=["action", "created_at"]),
        ]


class BillingSequence(models.Model):
    kind = models.CharField("Тип", max_length=32, choices=SequenceKind.choices)
    year = models.PositiveIntegerField("Год")
    last_number = models.PositiveIntegerField("Последний номер", default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Счетчик биллинга"
        verbose_name_plural = "Счетчики биллинга"
        constraints = [
            models.UniqueConstraint(fields=["kind", "year"], name="uniq_billing_sequence_kind_year"),
        ]

    def __str__(self) -> str:
        return f"{self.kind}/{self.year}: {self.last_number}"


class StorageCalculationRule(models.Model):
    """Правила расчёта хранения, привязанные к редакции тарифа клиента."""

    MODE_LITER_DAY = "liter_day"
    MODE_LITER_WEEK = "liter_week"
    MODE_LITER_MONTH = "liter_month"
    MODE_M3_DAY = "m3_day"
    MODE_M3_WEEK = "m3_week"
    MODE_M3_MONTH = "m3_month"
    MODE_PALLET_DAY = "pallet_day"
    MODE_PALLET_WEEK = "pallet_week"
    MODE_PALLET_MONTH = "pallet_month"
    MODE_CUSTOM = "custom"
    MODE_CHOICES = [
        (MODE_LITER_DAY, "За 1 литр в сутки"),
        (MODE_LITER_WEEK, "За 1 литр в неделю"),
        (MODE_LITER_MONTH, "За 1 литр в месяц"),
        (MODE_M3_DAY, "За 1 м³ в сутки"),
        (MODE_M3_WEEK, "За 1 м³ в неделю"),
        (MODE_M3_MONTH, "За 1 м³ в месяц"),
        (MODE_PALLET_DAY, "За палетоместо в сутки"),
        (MODE_PALLET_WEEK, "За палетоместо в неделю"),
        (MODE_PALLET_MONTH, "За палетоместо в месяц"),
        (MODE_CUSTOM, "Индивидуальная схема"),
    ]

    BASIS_PALLET = "pallet"
    BASIS_LITER = "liter"
    BASIS_M3 = "m3"
    CHARGE_BASIS_CHOICES = [
        (BASIS_PALLET, "Палетоместо"),
        (BASIS_LITER, "Литр"),
        (BASIS_M3, "м³"),
    ]

    PERIOD_DAY = "day"
    PERIOD_WEEK = "week"
    PERIOD_MONTH = "month"
    CHARGE_PERIOD_CHOICES = [
        (PERIOD_DAY, "За сутки"),
        (PERIOD_WEEK, "За неделю"),
        (PERIOD_MONTH, "За месяц"),
    ]

    MONTH_CALENDAR_PRORATE = "calendar_prorate"
    MONTH_FULL = "full_month"
    MONTH_AVG_DAILY = "avg_daily_volume"
    MONTH_MAX = "max_volume"
    MONTH_MODE_CHOICES = [
        (MONTH_CALENDAR_PRORATE, "Пропорционально календарным дням"),
        (MONTH_FULL, "Полная стоимость за месяц"),
        (MONTH_AVG_DAILY, "По среднесуточному объёму"),
        (MONTH_MAX, "По максимальному объёму за месяц"),
    ]

    DAY_INCLUDE_BOTH = "include_both"
    DAY_INCLUDE_RECEIVE = "include_receive"
    DAY_INCLUDE_SHIP = "include_ship"
    DAY_EXCLUDE_RECEIVE = "exclude_receive"
    DAY_EXCLUDE_SHIP = "exclude_ship"
    DAY_AFTER_PLACEMENT = "after_placement"
    DAY_AFTER_FREE = "after_free_period"
    DAY_COUNTING_CHOICES = [
        (DAY_INCLUDE_BOTH, "День приёмки и отгрузки включаются"),
        (DAY_INCLUDE_RECEIVE, "День приёмки включается"),
        (DAY_INCLUDE_SHIP, "День отгрузки включается"),
        (DAY_EXCLUDE_RECEIVE, "День приёмки не включается"),
        (DAY_EXCLUDE_SHIP, "День отгрузки не включается"),
        (DAY_AFTER_PLACEMENT, "После размещения в ячейку"),
        (DAY_AFTER_FREE, "После бесплатного периода"),
    ]

    FREE_NONE = "none"
    FREE_HOURS = "hours"
    FREE_CALENDAR_DAYS = "calendar_days"
    FREE_WORK_DAYS = "work_days"
    FREE_UNTIL_EVENT = "until_event"
    FREE_UNTIL_PROCESSING_DONE = "until_processing_done"
    FREE_PERIOD_CHOICES = [
        (FREE_NONE, "Без бесплатного периода"),
        (FREE_HOURS, "Часы"),
        (FREE_CALENDAR_DAYS, "Календарные дни"),
        (FREE_WORK_DAYS, "Рабочие дни"),
        (FREE_UNTIL_EVENT, "До события"),
        (FREE_UNTIL_PROCESSING_DONE, "До завершения обработки партии"),
    ]

    LEVEL_UNIT = "unit"
    LEVEL_BOX = "box"
    LEVEL_PALLET = "pallet"
    LEVEL_PLACE = "place"
    VOLUME_LEVEL_CHOICES = [
        (LEVEL_UNIT, "По единице товара"),
        (LEVEL_BOX, "По коробу"),
        (LEVEL_PALLET, "По палете"),
        (LEVEL_PLACE, "По занимаемому месту"),
    ]

    ROUND_NONE = "none"
    ROUND_LITER = "liter"
    ROUND_10_L = "10_l"
    ROUND_50_L = "50_l"
    ROUND_100_L = "100_l"
    ROUND_001_M3 = "0.01_m3"
    ROUND_01_M3 = "0.1_m3"
    ROUND_1_M3 = "1_m3"
    ROUND_CEIL = "ceil"
    ROUND_MATH = "math"
    ROUNDING_CHOICES = [
        (ROUND_NONE, "Без округления"),
        (ROUND_LITER, "До целого литра"),
        (ROUND_10_L, "До 10 литров"),
        (ROUND_50_L, "До 50 литров"),
        (ROUND_100_L, "До 100 литров"),
        (ROUND_001_M3, "До 0,01 м³"),
        (ROUND_01_M3, "До 0,1 м³"),
        (ROUND_1_M3, "До 1 м³"),
        (ROUND_CEIL, "Всегда в большую сторону"),
        (ROUND_MATH, "Математическое округление"),
    ]

    MISSING_SKIP = "skip"
    MISSING_FALLBACK = "fallback_tariff"
    MISSING_ERROR = "error_only"
    MISSING_DIMS_CHOICES = [
        (MISSING_SKIP, "Не начислять"),
        (MISSING_FALLBACK, "Резервный тариф"),
        (MISSING_ERROR, "Только ошибка"),
    ]

    tariff_version = models.OneToOneField(
        ClientTariffVersion,
        on_delete=models.CASCADE,
        related_name="storage_rule",
        verbose_name="Редакция тарифов",
    )
    billing_mode = models.CharField("Способ тарификации", max_length=32, choices=MODE_CHOICES, default=MODE_PALLET_DAY)
    charge_basis = models.CharField(
        "Единица тарифа",
        max_length=16,
        choices=CHARGE_BASIS_CHOICES,
        default=BASIS_PALLET,
        help_text="За что берётся цена: палета, литр или м³.",
    )
    charge_period = models.CharField(
        "Период тарифа",
        max_length=16,
        choices=CHARGE_PERIOD_CHOICES,
        default=PERIOD_DAY,
        help_text="За какой период указана цена в тарифе: сутки, неделя или месяц.",
    )
    month_mode = models.CharField("Месячный режим", max_length=32, choices=MONTH_MODE_CHOICES, default=MONTH_CALENDAR_PRORATE)
    day_counting = models.CharField("Учёт дней", max_length=32, choices=DAY_COUNTING_CHOICES, default=DAY_INCLUDE_BOTH)
    free_period_type = models.CharField("Бесплатный период", max_length=32, choices=FREE_PERIOD_CHOICES, default=FREE_NONE)
    free_period_value = models.DecimalField("Значение бесплатного периода", max_digits=12, decimal_places=3, default=MONEY_ZERO)
    volume_level = models.CharField("Уровень объёма", max_length=16, choices=VOLUME_LEVEL_CHOICES, default=LEVEL_PALLET)
    space_coefficient_default = models.DecimalField(
        "Коэффициент хранения по умолчанию",
        max_digits=10,
        decimal_places=6,
        default=Decimal("1.000000"),
    )
    rounding_mode = models.CharField("Округление объёма", max_length=16, choices=ROUNDING_CHOICES, default=ROUND_NONE)
    min_billable_volume = models.DecimalField("Мин. тарифицируемый объём", max_digits=16, decimal_places=6, default=MONEY_ZERO)
    min_amount_day = models.DecimalField("Мин. сумма за день", max_digits=14, decimal_places=2, default=MONEY_ZERO)
    min_amount_month = models.DecimalField("Мин. сумма за месяц", max_digits=14, decimal_places=2, default=MONEY_ZERO)
    missing_dims_policy = models.CharField(
        "Нет габаритов",
        max_length=32,
        choices=MISSING_DIMS_CHOICES,
        default=MISSING_ERROR,
    )
    snapshot_hour = models.PositiveSmallIntegerField("Час снимка (локальное время склада)", default=23)
    timezone_name = models.CharField("Часовой пояс склада", max_length=64, default="Europe/Moscow")
    accountant_comment = models.TextField("Комментарий бухгалтера", blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Правило расчёта хранения"
        verbose_name_plural = "Правила расчёта хранения"

    def __str__(self) -> str:
        return f"{self.tariff_version_id}: {self.get_billing_mode_display()}"

    @classmethod
    def compose_billing_mode(cls, basis: str, period: str) -> str:
        basis = (basis or cls.BASIS_PALLET).strip()
        period = (period or cls.PERIOD_DAY).strip()
        mapping = {
            (cls.BASIS_PALLET, cls.PERIOD_DAY): cls.MODE_PALLET_DAY,
            (cls.BASIS_PALLET, cls.PERIOD_WEEK): cls.MODE_PALLET_WEEK,
            (cls.BASIS_PALLET, cls.PERIOD_MONTH): cls.MODE_PALLET_MONTH,
            (cls.BASIS_LITER, cls.PERIOD_DAY): cls.MODE_LITER_DAY,
            (cls.BASIS_LITER, cls.PERIOD_WEEK): cls.MODE_LITER_WEEK,
            (cls.BASIS_LITER, cls.PERIOD_MONTH): cls.MODE_LITER_MONTH,
            (cls.BASIS_M3, cls.PERIOD_DAY): cls.MODE_M3_DAY,
            (cls.BASIS_M3, cls.PERIOD_WEEK): cls.MODE_M3_WEEK,
            (cls.BASIS_M3, cls.PERIOD_MONTH): cls.MODE_M3_MONTH,
        }
        return mapping.get((basis, period), cls.MODE_PALLET_DAY)

    @classmethod
    def split_billing_mode(cls, billing_mode: str) -> tuple[str, str]:
        mode = (billing_mode or cls.MODE_PALLET_DAY).strip()
        mapping = {
            cls.MODE_PALLET_DAY: (cls.BASIS_PALLET, cls.PERIOD_DAY),
            cls.MODE_PALLET_WEEK: (cls.BASIS_PALLET, cls.PERIOD_WEEK),
            cls.MODE_PALLET_MONTH: (cls.BASIS_PALLET, cls.PERIOD_MONTH),
            cls.MODE_LITER_DAY: (cls.BASIS_LITER, cls.PERIOD_DAY),
            cls.MODE_LITER_WEEK: (cls.BASIS_LITER, cls.PERIOD_WEEK),
            cls.MODE_LITER_MONTH: (cls.BASIS_LITER, cls.PERIOD_MONTH),
            cls.MODE_M3_DAY: (cls.BASIS_M3, cls.PERIOD_DAY),
            cls.MODE_M3_WEEK: (cls.BASIS_M3, cls.PERIOD_WEEK),
            cls.MODE_M3_MONTH: (cls.BASIS_M3, cls.PERIOD_MONTH),
            cls.MODE_CUSTOM: (cls.BASIS_PALLET, cls.PERIOD_DAY),
        }
        return mapping.get(mode, (cls.BASIS_PALLET, cls.PERIOD_DAY))

    def sync_mode_from_basis_period(self) -> None:
        self.billing_mode = self.compose_billing_mode(self.charge_basis, self.charge_period)

    def sync_basis_period_from_mode(self) -> None:
        self.charge_basis, self.charge_period = self.split_billing_mode(self.billing_mode)


class BillingStorageDay(models.Model):
    """Дневной снимок хранения клиента (склад только читаем)."""

    STATUS_PRELIMINARY = "preliminary"
    STATUS_CALCULATED = "calculated"
    STATUS_NEEDS_REVIEW = "needs_review"
    STATUS_CONFIRMED = "confirmed"
    STATUS_IN_INVOICE = "in_invoice"
    STATUS_CANCELLED = "cancelled"
    STATUS_CHOICES = [
        (STATUS_PRELIMINARY, "Предварительное"),
        (STATUS_CALCULATED, "Рассчитано"),
        (STATUS_NEEDS_REVIEW, "Требует проверки"),
        (STATUS_CONFIRMED, "Подтверждено"),
        (STATUS_IN_INVOICE, "Включено в счёт"),
        (STATUS_CANCELLED, "Отменено"),
    ]

    client = models.ForeignKey(
        Agency,
        on_delete=models.CASCADE,
        related_name="billing_storage_days",
        verbose_name="Клиент",
    )
    application = models.ForeignKey(
        BillingApplication,
        on_delete=models.CASCADE,
        related_name="storage_days",
        verbose_name="Заявка хранения",
        null=True,
        blank=True,
    )
    day = models.DateField("Дата", db_index=True)
    pallet_count = models.PositiveIntegerField("Палет", default=0)
    box_count = models.PositiveIntegerField("Коробов", default=0)
    sku_unit_count = models.PositiveIntegerField("Единиц товара", default=0)
    zone_counts = models.JSONField("По зонам", default=dict, blank=True)
    physical_volume_l = models.DecimalField("Физ. объём, л", max_digits=16, decimal_places=6, default=MONEY_ZERO)
    physical_volume_m3 = models.DecimalField("Физ. объём, м³", max_digits=16, decimal_places=6, default=MONEY_ZERO)
    billable_volume_l = models.DecimalField("Тариф. объём, л", max_digits=16, decimal_places=6, default=MONEY_ZERO)
    billable_volume_m3 = models.DecimalField("Тариф. объём, м³", max_digits=16, decimal_places=6, default=MONEY_ZERO)
    coefficient_applied = models.DecimalField(
        "Коэффициент",
        max_digits=10,
        decimal_places=6,
        default=Decimal("1.000000"),
    )
    billing_mode = models.CharField("Способ тарификации", max_length=32, blank=True)
    amount = models.DecimalField("Сумма без НДС", max_digits=14, decimal_places=2, default=MONEY_ZERO)
    vat_amount = models.DecimalField("НДС", max_digits=14, decimal_places=2, default=MONEY_ZERO)
    status = models.CharField("Статус", max_length=32, choices=STATUS_CHOICES, default=STATUS_PRELIMINARY, db_index=True)
    tariff_version = models.ForeignKey(
        ClientTariffVersion,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="storage_days",
        verbose_name="Редакция тарифа",
    )
    calculation_rule = models.ForeignKey(
        StorageCalculationRule,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="storage_days",
        verbose_name="Правило расчёта",
    )
    payload = models.JSONField("Аудит расчёта", default=dict, blank=True)
    charge = models.ForeignKey(
        ApplicationCharge,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="storage_days",
        verbose_name="Начисление",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "День хранения"
        verbose_name_plural = "Дни хранения"
        ordering = ["-day", "-id"]
        constraints = [
            models.UniqueConstraint(fields=["client", "day"], name="uniq_billing_storage_day_client"),
        ]
        indexes = [
            models.Index(fields=["day"]),
            models.Index(fields=["client", "day"]),
            models.Index(fields=["application", "day"]),
            models.Index(fields=["status", "day"]),
        ]

    def __str__(self) -> str:
        return f"{self.client_id} {self.day}: {self.pallet_count} пал."


class StorageSnapshotLine(models.Model):
    """Строка детализации дневного снимка хранения."""

    STATUS_OK = "ok"
    STATUS_NO_DIMS = "no_dims"
    STATUS_CHOICES = [
        (STATUS_OK, "OK"),
        (STATUS_NO_DIMS, "Нет габаритов"),
    ]

    day = models.ForeignKey(
        BillingStorageDay,
        on_delete=models.CASCADE,
        related_name="lines",
        verbose_name="День хранения",
    )
    sku = models.ForeignKey(
        "sku.SKU",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="storage_snapshot_lines",
        verbose_name="SKU",
    )
    sku_code = models.CharField("Код SKU", max_length=128, blank=True, db_index=True)
    name = models.CharField("Наименование", max_length=255, blank=True)
    barcode = models.CharField("Штрихкод", max_length=128, blank=True)
    box_code = models.CharField("Короб", max_length=128, blank=True, db_index=True)
    pallet_code = models.CharField("Палета", max_length=128, blank=True, db_index=True)
    zone_code = models.CharField("Зона", max_length=64, blank=True)
    cell_code = models.CharField("Ячейка", max_length=128, blank=True)
    quantity = models.DecimalField("Количество", max_digits=14, decimal_places=3, default=MONEY_ZERO)
    length_mm = models.PositiveIntegerField("Длина, мм", null=True, blank=True)
    width_mm = models.PositiveIntegerField("Ширина, мм", null=True, blank=True)
    height_mm = models.PositiveIntegerField("Высота, мм", null=True, blank=True)
    unit_volume_l = models.DecimalField("Объём ед., л", max_digits=16, decimal_places=6, default=MONEY_ZERO)
    total_volume_l = models.DecimalField("Объём всего, л", max_digits=16, decimal_places=6, default=MONEY_ZERO)
    total_volume_m3 = models.DecimalField("Объём всего, м³", max_digits=16, decimal_places=6, default=MONEY_ZERO)
    coefficient = models.DecimalField("Коэффициент", max_digits=10, decimal_places=6, default=Decimal("1.000000"))
    billable_volume_l = models.DecimalField("Тариф. объём, л", max_digits=16, decimal_places=6, default=MONEY_ZERO)
    billable_volume_m3 = models.DecimalField("Тариф. объём, м³", max_digits=16, decimal_places=6, default=MONEY_ZERO)
    volume_level = models.CharField("Уровень", max_length=16, blank=True)
    dims_source = models.CharField("Источник размеров", max_length=64, blank=True)
    status = models.CharField("Статус", max_length=16, choices=STATUS_CHOICES, default=STATUS_OK, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Строка снимка хранения"
        verbose_name_plural = "Строки снимков хранения"
        ordering = ["id"]
        indexes = [
            models.Index(fields=["day", "status"]),
            models.Index(fields=["sku_code"]),
            models.Index(fields=["pallet_code"]),
        ]

    def __str__(self) -> str:
        return f"{self.day_id}: {self.sku_code or self.pallet_code or self.box_code}"


class StorageBillingPeriod(models.Model):
    STATUS_OPEN = "open"
    STATUS_CLOSING = "closing"
    STATUS_CLOSED = "closed"
    STATUS_CHOICES = [
        (STATUS_OPEN, "Открыт"),
        (STATUS_CLOSING, "Закрывается"),
        (STATUS_CLOSED, "Закрыт"),
    ]

    client = models.ForeignKey(Agency, on_delete=models.CASCADE, related_name="storage_billing_periods", verbose_name="Клиент")
    legal_entity = models.ForeignKey(
        Agency,
        on_delete=models.PROTECT,
        related_name="storage_billing_legal_periods",
        verbose_name="Юрлицо клиента",
    )
    year = models.PositiveIntegerField("Год")
    month = models.PositiveIntegerField("Месяц")
    status = models.CharField("Статус", max_length=16, choices=STATUS_CHOICES, default=STATUS_OPEN, db_index=True)
    total_amount = models.DecimalField("Итого без НДС", max_digits=14, decimal_places=2, default=MONEY_ZERO)
    total_vat = models.DecimalField("НДС", max_digits=14, decimal_places=2, default=MONEY_ZERO)
    checklist = models.JSONField("Чек-лист закрытия", default=dict, blank=True)
    closed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="closed_storage_periods",
    )
    closed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Период биллинга хранения"
        verbose_name_plural = "Периоды биллинга хранения"
        ordering = ["-year", "-month", "-id"]
        constraints = [
            models.UniqueConstraint(fields=["client", "legal_entity", "year", "month"], name="uniq_storage_billing_period"),
        ]

    def __str__(self) -> str:
        return f"{self.client_id} {self.year}-{self.month:02d} ({self.status})"


class StorageAdjustment(models.Model):
    REASON_BAD_DIMS = "bad_dims"
    REASON_BAD_QTY = "bad_qty"
    REASON_BAD_PLACEMENT = "bad_placement"
    REASON_BAD_TARIFF = "bad_tariff"
    REASON_CLIENT_AGREEMENT = "client_agreement"
    REASON_FREE_STORAGE = "free_storage"
    REASON_TECH = "tech_failure"
    REASON_COMPENSATION = "compensation"
    REASON_OTHER = "other"
    REASON_CHOICES = [
        (REASON_BAD_DIMS, "Неверные габариты"),
        (REASON_BAD_QTY, "Ошибка в количестве"),
        (REASON_BAD_PLACEMENT, "Ошибка размещения"),
        (REASON_BAD_TARIFF, "Неверный тариф"),
        (REASON_CLIENT_AGREEMENT, "Договорённость с клиентом"),
        (REASON_FREE_STORAGE, "Бесплатное хранение"),
        (REASON_TECH, "Технический сбой"),
        (REASON_COMPENSATION, "Компенсация"),
        (REASON_OTHER, "Иная причина"),
    ]

    STATUS_DRAFT = "draft"
    STATUS_PENDING = "pending"
    STATUS_APPROVED = "approved"
    STATUS_REJECTED = "rejected"
    STATUS_APPLIED = "applied"
    STATUS_CHOICES = [
        (STATUS_DRAFT, "Черновик"),
        (STATUS_PENDING, "На согласовании"),
        (STATUS_APPROVED, "Согласовано"),
        (STATUS_REJECTED, "Отклонено"),
        (STATUS_APPLIED, "Применено"),
    ]

    client = models.ForeignKey(Agency, on_delete=models.CASCADE, related_name="storage_adjustments", verbose_name="Клиент")
    period = models.ForeignKey(
        StorageBillingPeriod,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="adjustments",
        verbose_name="Период",
    )
    source_day = models.ForeignKey(
        BillingStorageDay,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="adjustments",
        verbose_name="Исходный день",
    )
    source_charge = models.ForeignKey(
        ApplicationCharge,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="storage_adjustments",
        verbose_name="Исходное начисление",
    )
    delta_volume_l = models.DecimalField("Корр. объём, л", max_digits=16, decimal_places=6, default=MONEY_ZERO)
    delta_amount = models.DecimalField("Корр. сумма", max_digits=14, decimal_places=2, default=MONEY_ZERO)
    reason = models.CharField("Причина", max_length=32, choices=REASON_CHOICES, default=REASON_OTHER)
    comment = models.TextField("Комментарий", blank=True)
    status = models.CharField("Статус", max_length=16, choices=STATUS_CHOICES, default=STATUS_DRAFT, db_index=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_storage_adjustments",
    )
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="approved_storage_adjustments",
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Корректировка хранения"
        verbose_name_plural = "Корректировки хранения"
        ordering = ["-created_at", "-id"]

    def __str__(self) -> str:
        return f"{self.client_id}: {self.delta_amount} ({self.reason})"


class StorageBillingError(models.Model):
    TYPE_NO_DIMS = "no_dims"
    TYPE_ZERO_DIMS = "zero_dims"
    TYPE_VOLUME_TOO_LARGE = "volume_too_large"
    TYPE_VOLUME_TOO_SMALL = "volume_too_small"
    TYPE_NO_TARIFF = "no_tariff"
    TYPE_MULTI_TARIFF = "multi_tariff"
    TYPE_DOUBLE_COUNT = "double_count"
    TYPE_NEGATIVE_STOCK = "negative_stock"
    TYPE_AFTER_SHIP = "after_ship"
    TYPE_BEFORE_RECEIVE = "before_receive"
    TYPE_CLOSED_PERIOD = "closed_period"
    TYPE_VOLUME_SPIKE = "volume_spike"
    TYPE_WMS_MISMATCH = "wms_mismatch"
    TYPE_OTHER = "other"
    TYPE_CHOICES = [
        (TYPE_NO_DIMS, "Нет габаритов"),
        (TYPE_ZERO_DIMS, "Нулевые габариты"),
        (TYPE_VOLUME_TOO_LARGE, "Подозрительно большой объём"),
        (TYPE_VOLUME_TOO_SMALL, "Подозрительно маленький объём"),
        (TYPE_NO_TARIFF, "Нет тарифа"),
        (TYPE_MULTI_TARIFF, "Несколько тарифов"),
        (TYPE_DOUBLE_COUNT, "Двойной учёт"),
        (TYPE_NEGATIVE_STOCK, "Отрицательный остаток"),
        (TYPE_AFTER_SHIP, "Начисление после отгрузки"),
        (TYPE_BEFORE_RECEIVE, "Начисление до приёмки"),
        (TYPE_CLOSED_PERIOD, "Изменение закрытого периода"),
        (TYPE_VOLUME_SPIKE, "Резкое изменение объёма"),
        (TYPE_WMS_MISMATCH, "Расхождение WMS и биллинга"),
        (TYPE_OTHER, "Прочее"),
    ]

    SEVERITY_INFO = "info"
    SEVERITY_WARN = "warn"
    SEVERITY_ERROR = "error"
    SEVERITY_CHOICES = [
        (SEVERITY_INFO, "Инфо"),
        (SEVERITY_WARN, "Предупреждение"),
        (SEVERITY_ERROR, "Ошибка"),
    ]

    error_type = models.CharField("Тип", max_length=32, choices=TYPE_CHOICES, db_index=True)
    severity = models.CharField("Критичность", max_length=16, choices=SEVERITY_CHOICES, default=SEVERITY_ERROR)
    client = models.ForeignKey(Agency, on_delete=models.CASCADE, related_name="storage_billing_errors", verbose_name="Клиент")
    day = models.DateField("Дата", null=True, blank=True, db_index=True)
    storage_day = models.ForeignKey(
        BillingStorageDay,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="errors",
    )
    sku_code = models.CharField("SKU", max_length=128, blank=True)
    pallet_code = models.CharField("Палета", max_length=128, blank=True)
    box_code = models.CharField("Короб", max_length=128, blank=True)
    message = models.TextField("Сообщение")
    payload = models.JSONField("Детали", default=dict, blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="resolved_storage_billing_errors",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Ошибка биллинга хранения"
        verbose_name_plural = "Ошибки биллинга хранения"
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["client", "error_type", "resolved_at"]),
            models.Index(fields=["day", "severity"]),
        ]

    def __str__(self) -> str:
        return f"{self.error_type}: {self.client_id}"

class ExtraServiceRequest(models.Model):
    """Запрос менеджера на доп. услугу вне автопотока WMS → ApplicationCharge."""

    STATUS_NEEDS_PRICE = "needs_price"
    STATUS_CHARGED = "charged"
    STATUS_CANCELLED = "cancelled"
    STATUS_CHOICES = [
        (STATUS_NEEDS_PRICE, "Требуется настройка цены"),
        (STATUS_CHARGED, "Начисление создано"),
        (STATUS_CANCELLED, "Отменён"),
    ]

    client = models.ForeignKey(
        Agency,
        on_delete=models.PROTECT,
        related_name="extra_service_requests",
        verbose_name="Клиент",
    )
    application = models.ForeignKey(
        BillingApplication,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="extra_service_requests",
        verbose_name="Заявка биллинга",
    )
    service = models.ForeignKey(
        BillingService,
        on_delete=models.PROTECT,
        related_name="extra_service_requests",
        verbose_name="Услуга",
    )
    service_date = models.DateField("Дата оказания", db_index=True)
    quantity = models.DecimalField("Количество", max_digits=14, decimal_places=3)
    unit = models.CharField("Единица", max_length=32, blank=True)
    warehouse_label = models.CharField("Склад", max_length=128, blank=True)
    description = models.TextField("Описание", blank=True)
    reason = models.TextField("Причина запроса", blank=True)
    performer = models.CharField("Исполнитель", max_length=255, blank=True)
    internal_comment = models.TextField("Внутренний комментарий", blank=True)
    client_comment = models.TextField("Комментарий для клиента", blank=True)
    attachment = models.FileField(
        "Подтверждение",
        upload_to=billing_document_upload_to,
        null=True,
        blank=True,
    )
    status = models.CharField(
        "Статус",
        max_length=32,
        choices=STATUS_CHOICES,
        default=STATUS_NEEDS_PRICE,
        db_index=True,
    )
    price_note = models.TextField("Примечание по цене", blank=True)
    charge = models.ForeignKey(
        ApplicationCharge,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="extra_service_requests",
        verbose_name="Начисление",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_extra_service_requests",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Запрос доп. услуги"
        verbose_name_plural = "Запросы доп. услуг"
        ordering = ["-service_date", "-id"]
        indexes = [
            models.Index(fields=["client", "status"]),
            models.Index(fields=["status", "service_date"]),
        ]

    def __str__(self) -> str:
        return f"{self.client_id}: {self.service_id} × {self.quantity} ({self.status})"


class WarehouseServiceFact(models.Model):
    """Факт услуги от кладовщика при закрытии заявки: услуга + qty, без цены.

    Цена подтягивается из согласованного тарифа только в биллинге менеджера.
    Клиенту и складу цены не показываются.
    """

    ORDER_RECEIVING = "receiving"
    ORDER_PROCESSING = "processing"
    ORDER_PACKING = "packing"
    ORDER_SHIPPING = "shipping"
    ORDER_FBS = "fbs"
    ORDER_TYPE_CHOICES = [
        (ORDER_RECEIVING, "Приёмка"),
        (ORDER_PROCESSING, "Обработка"),
        (ORDER_PACKING, "Упаковка"),
        (ORDER_SHIPPING, "Отгрузка"),
        (ORDER_FBS, "FBS"),
    ]
    STATUS_SENT_TO_BILLING = "sent_to_billing"
    STATUS_WAITING_MANAGER_REVIEW = "waiting_manager_review"
    STATUS_APPROVED = "approved"
    STATUS_NEEDS_CLARIFICATION = "needs_clarification"
    STATUS_CHARGED = "charged"
    STATUS_CANCELLED = "cancelled"
    STATUS_CHOICES = [
        (STATUS_SENT_TO_BILLING, "Передано в биллинг"),
        (STATUS_WAITING_MANAGER_REVIEW, "На проверке менеджером"),
        (STATUS_APPROVED, "Одобрено"),
        (STATUS_NEEDS_CLARIFICATION, "Требует уточнения"),
        (STATUS_CHARGED, "Начислено"),
        (STATUS_CANCELLED, "Отменено"),
    ]

    client = models.ForeignKey(
        Agency,
        on_delete=models.PROTECT,
        related_name="warehouse_service_facts",
        verbose_name="Клиент",
    )
    order_type = models.CharField("Тип заявки", max_length=32, choices=ORDER_TYPE_CHOICES, db_index=True)
    order_id = models.CharField("Номер заявки", max_length=128, db_index=True)
    service = models.ForeignKey(
        BillingService,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="warehouse_service_facts",
        verbose_name="Услуга",
    )
    service_name_snapshot = models.CharField("Название услуги", max_length=255, blank=True)
    planned_quantity = models.DecimalField(
        "Плановое количество",
        max_digits=14,
        decimal_places=3,
        null=True,
        blank=True,
    )
    quantity = models.DecimalField("Количество", max_digits=14, decimal_places=3)
    unit = models.CharField("Единица", max_length=32, blank=True)
    comment = models.CharField("Комментарий", max_length=255, blank=True)
    discrepancy_reason = models.TextField("Причина расхождения", blank=True)
    status = models.CharField(
        "Статус проверки",
        max_length=32,
        choices=STATUS_CHOICES,
        default=STATUS_SENT_TO_BILLING,
        db_index=True,
    )
    source = models.CharField("Источник", max_length=64, blank=True, default="warehouse_manual")
    is_manual = models.BooleanField("Введено вручную", default=True)
    warehouse_label = models.CharField("Склад / участок", max_length=255, blank=True)
    performed_at = models.DateTimeField("Когда выполнено", null=True, blank=True, db_index=True)
    metadata = models.JSONField("Технические данные", default=dict, blank=True)
    reported_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reported_warehouse_service_facts",
        verbose_name="Кто указал",
    )
    reported_at = models.DateTimeField("Когда указано", auto_now_add=True, db_index=True)
    application = models.ForeignKey(
        BillingApplication,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="warehouse_service_facts",
        verbose_name="Заявка биллинга",
    )
    charge = models.ForeignKey(
        "ApplicationCharge",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="warehouse_service_facts",
        verbose_name="Начисление",
    )
    source_key = models.CharField("Ключ источника", max_length=160, blank=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Факт услуги склада"
        verbose_name_plural = "Факты услуг склада"
        ordering = ["order_type", "order_id", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["client", "order_type", "order_id", "service"],
                name="uniq_warehouse_service_fact_order_service",
            ),
        ]
        indexes = [
            models.Index(fields=["client", "order_type", "order_id"]),
            models.Index(fields=["order_type", "order_id"]),
            models.Index(fields=["status", "reported_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.order_type}:{self.order_id} · {self.service_id} × {self.quantity}"


class WarehouseServiceFactAttachment(models.Model):
    """Вложение к факту выполненной складской услуги."""

    fact = models.ForeignKey(
        WarehouseServiceFact,
        on_delete=models.CASCADE,
        related_name="attachments",
        verbose_name="Факт услуги",
    )
    file = models.FileField("Файл", upload_to="billing/warehouse-service-facts/%Y/%m/")
    comment = models.CharField("Комментарий", max_length=255, blank=True)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="uploaded_warehouse_service_fact_attachments",
        verbose_name="Кто загрузил",
    )
    created_at = models.DateTimeField("Когда загружено", auto_now_add=True)

    class Meta:
        verbose_name = "Вложение к факту услуги склада"
        verbose_name_plural = "Вложения к фактам услуг склада"
        ordering = ["-created_at", "-id"]

    def __str__(self) -> str:
        return f"{self.fact_id}: {self.file.name}"


class UpdRequest(models.Model):
    """Запрос менеджера на формирование УПД (бухгалтер исполняет)."""

    STATUS_REQUESTED = "requested"
    STATUS_IN_PROGRESS = "in_progress"
    STATUS_ISSUED = "issued"
    STATUS_REJECTED = "rejected"
    STATUS_CANCELLED = "cancelled"
    STATUS_CHOICES = [
        (STATUS_REQUESTED, "Запрошен"),
        (STATUS_IN_PROGRESS, "В работе"),
        (STATUS_ISSUED, "Выпущен"),
        (STATUS_REJECTED, "Отклонён"),
        (STATUS_CANCELLED, "Отменён"),
    ]

    client = models.ForeignKey(
        Agency,
        on_delete=models.PROTECT,
        related_name="upd_requests",
        verbose_name="Клиент",
    )
    application = models.ForeignKey(
        BillingApplication,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="upd_requests",
    )
    invoice = models.ForeignKey(
        ClientInvoice,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="upd_requests",
    )
    act = models.ForeignKey(
        BillingAct,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="upd_requests",
    )
    period_from = models.DateField("Период с", null=True, blank=True)
    period_to = models.DateField("Период по", null=True, blank=True)
    status = models.CharField(
        "Статус",
        max_length=32,
        choices=STATUS_CHOICES,
        default=STATUS_REQUESTED,
        db_index=True,
    )
    comment = models.TextField("Комментарий менеджера", blank=True)
    accountant_comment = models.TextField("Комментарий бухгалтера", blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_upd_requests",
    )
    processed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="processed_upd_requests",
    )
    processed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Запрос УПД"
        verbose_name_plural = "Запросы УПД"
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["client", "status"]),
            models.Index(fields=["status", "created_at"]),
        ]

    def __str__(self) -> str:
        return f"УПД {self.client_id} ({self.status})"


class PaymentPromise(models.Model):
    """Обещание оплаты — не уменьшает задолженность."""

    client = models.ForeignKey(
        Agency,
        on_delete=models.PROTECT,
        related_name="payment_promises",
        verbose_name="Клиент",
    )
    invoice = models.ForeignKey(
        ClientInvoice,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="payment_promises",
        verbose_name="Счёт",
    )
    amount = models.DecimalField("Сумма", max_digits=14, decimal_places=2)
    promised_date = models.DateField("Обещанная дата", db_index=True)
    contact_person = models.CharField("Контактное лицо", max_length=255, blank=True)
    client_comment = models.TextField("Комментарий клиента", blank=True)
    manager_comment = models.TextField("Комментарий менеджера", blank=True)
    status = models.CharField(
        "Статус",
        max_length=32,
        choices=PaymentPromiseStatus.choices,
        default=PaymentPromiseStatus.PENDING,
        db_index=True,
    )
    result_note = models.TextField("Фактический результат", blank=True)
    manager = models.ForeignKey(
        Employee,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="payment_promises",
        verbose_name="Менеджер",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_payment_promises",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Обещание оплаты"
        verbose_name_plural = "Обещания оплаты"
        ordering = ["promised_date", "-id"]
        indexes = [
            models.Index(fields=["client", "status"]),
            models.Index(fields=["status", "promised_date"]),
        ]

    def __str__(self) -> str:
        return f"{self.client_id}: {self.amount} до {self.promised_date}"

    def refresh_overdue(self) -> None:
        if self.status == PaymentPromiseStatus.PENDING and self.promised_date < timezone.localdate():
            self.status = PaymentPromiseStatus.OVERDUE
            self.save(update_fields=["status", "updated_at"])


class ReconciliationRequest(models.Model):
    """Запрос/черновик акта сверки менеджера → бухгалтер."""

    client = models.ForeignKey(
        Agency,
        on_delete=models.PROTECT,
        related_name="reconciliation_requests",
        verbose_name="Клиент",
    )
    period_from = models.DateField("Период с")
    period_to = models.DateField("Период по")
    opening_balance = models.DecimalField(
        "Начальное сальдо", max_digits=14, decimal_places=2, default=MONEY_ZERO
    )
    closing_balance = models.DecimalField(
        "Конечное сальдо", max_digits=14, decimal_places=2, null=True, blank=True
    )
    status = models.CharField(
        "Статус",
        max_length=32,
        choices=ReconciliationStatus.choices,
        default=ReconciliationStatus.DRAFT,
        db_index=True,
    )
    manager_comment = models.TextField("Комментарий менеджера", blank=True)
    accountant_comment = models.TextField("Комментарий бухгалтера", blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_reconciliation_requests",
    )
    submitted_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Акт сверки (запрос)"
        verbose_name_plural = "Акты сверки (запросы)"
        ordering = ["-period_to", "-id"]
        indexes = [
            models.Index(fields=["client", "status"]),
            models.Index(fields=["status", "period_to"]),
        ]

    def __str__(self) -> str:
        return f"Сверка {self.client_id} {self.period_from}–{self.period_to}"


class BillingDiscrepancy(models.Model):
    """Единый реестр расхождений менеджера."""

    TYPE_QTY = "quantity"
    TYPE_SERVICE = "service"
    TYPE_PRICE = "price"
    TYPE_MISSING_OP = "missing_operation"
    TYPE_DUPLICATE = "duplicate"
    TYPE_PERIOD = "period"
    TYPE_DOCUMENT = "document"
    TYPE_RECONCILIATION = "reconciliation"
    TYPE_PAYMENT = "payment"
    TYPE_OTHER = "other"
    TYPE_CHOICES = [
        (TYPE_QTY, "Количество"),
        (TYPE_SERVICE, "Услуга"),
        (TYPE_PRICE, "Цена"),
        (TYPE_MISSING_OP, "Нет операции"),
        (TYPE_DUPLICATE, "Дубль"),
        (TYPE_PERIOD, "Период"),
        (TYPE_DOCUMENT, "Документ"),
        (TYPE_RECONCILIATION, "Акт сверки"),
        (TYPE_PAYMENT, "Оплата"),
        (TYPE_OTHER, "Прочее"),
    ]

    client = models.ForeignKey(
        Agency,
        on_delete=models.PROTECT,
        related_name="billing_discrepancies",
        verbose_name="Клиент",
    )
    application = models.ForeignKey(
        BillingApplication,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="discrepancies",
    )
    charge = models.ForeignKey(
        ApplicationCharge,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="discrepancies",
    )
    act = models.ForeignKey(
        BillingAct,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="discrepancies",
    )
    invoice = models.ForeignKey(
        ClientInvoice,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="discrepancies",
    )
    act_dispute = models.ForeignKey(
        BillingActDispute,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="discrepancies",
    )
    discrepancy_type = models.CharField("Тип", max_length=32, choices=TYPE_CHOICES, default=TYPE_OTHER)
    description = models.TextField("Описание")
    disputed_amount = models.DecimalField(
        "Сумма спора", max_digits=14, decimal_places=2, null=True, blank=True
    )
    status = models.CharField(
        "Статус",
        max_length=32,
        choices=DiscrepancyStatus.choices,
        default=DiscrepancyStatus.NEW,
        db_index=True,
    )
    manager_comment = models.TextField("Комментарий менеджера", blank=True)
    result_note = models.TextField("Результат", blank=True)
    attachment = models.FileField(
        "Файл",
        upload_to=billing_document_upload_to,
        null=True,
        blank=True,
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_billing_discrepancies",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    closed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "Расхождение биллинга"
        verbose_name_plural = "Расхождения биллинга"
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["client", "status"]),
            models.Index(fields=["status", "created_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.client_id}: {self.discrepancy_type} ({self.status})"


class BillingStaffNotification(models.Model):
    """Уведомления менеджера/бухгалтера по биллингу (не клиентский ЛК)."""

    AUDIENCE_MANAGER = "manager"
    AUDIENCE_ACCOUNTANT = "accountant"
    AUDIENCE_CHOICES = [
        (AUDIENCE_MANAGER, "Менеджер"),
        (AUDIENCE_ACCOUNTANT, "Бухгалтер"),
    ]

    KIND_TARIFF_PUBLISHED = "tariff_published"
    KIND_DOC_RETURNED = "doc_returned"
    KIND_DOC_ACCEPTED = "doc_accepted"
    KIND_DOC_SUBMITTED = "doc_submitted"
    KIND_PAYMENT = "payment"
    KIND_OVERDUE = "overdue"
    KIND_NO_PRICE = "no_price"
    KIND_DISCREPANCY = "discrepancy"
    KIND_UPD = "upd"
    KIND_PROMISE = "promise"
    KIND_SERVICE_CHANGED = "service_changed"
    KIND_OTHER = "other"
    KIND_CHOICES = [
        (KIND_TARIFF_PUBLISHED, "Новый тариф"),
        (KIND_DOC_RETURNED, "Документ возвращён"),
        (KIND_DOC_ACCEPTED, "Документ принят"),
        (KIND_DOC_SUBMITTED, "Документ на проверке"),
        (KIND_PAYMENT, "Оплата"),
        (KIND_OVERDUE, "Просрочка"),
        (KIND_NO_PRICE, "Нет цены"),
        (KIND_DISCREPANCY, "Расхождение"),
        (KIND_UPD, "УПД"),
        (KIND_PROMISE, "Обещание"),
        (KIND_SERVICE_CHANGED, "Услуга изменена"),
        (KIND_OTHER, "Прочее"),
    ]

    recipient = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="billing_staff_notifications",
        verbose_name="Получатель",
    )
    audience = models.CharField("Аудитория", max_length=32, choices=AUDIENCE_CHOICES, default=AUDIENCE_MANAGER)
    client = models.ForeignKey(
        Agency,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="billing_staff_notifications",
        verbose_name="Клиент",
    )
    kind = models.CharField("Тип", max_length=32, choices=KIND_CHOICES, default=KIND_OTHER, db_index=True)
    title = models.CharField("Заголовок", max_length=255)
    message = models.TextField("Текст", blank=True)
    link_url = models.CharField("Ссылка", max_length=512, blank=True)
    is_read = models.BooleanField("Прочитано", default=False, db_index=True)
    read_at = models.DateTimeField(null=True, blank=True)
    source_key = models.CharField("Ключ идемпотентности", max_length=191, unique=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_billing_staff_notifications",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Уведомление биллинга (сотрудник)"
        verbose_name_plural = "Уведомления биллинга (сотрудники)"
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["recipient", "is_read", "created_at"]),
            models.Index(fields=["kind", "created_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.recipient_id}: {self.title}"


class LogisticsPriceLine(models.Model):
    """Базовый прайс доставки; сам по себе не меняет складской учёт и биллинг."""

    MARKETPLACE_WB = "wb"
    MARKETPLACE_OZON = "ozon"
    MARKETPLACE_OTHER = "other"
    MARKETPLACE_CHOICES = [
        (MARKETPLACE_WB, "Wildberries"),
        (MARKETPLACE_OZON, "Ozon"),
        (MARKETPLACE_OTHER, "Другое направление"),
    ]

    marketplace = models.CharField("Маркетплейс", max_length=16, choices=MARKETPLACE_CHOICES, default=MARKETPLACE_OTHER)
    warehouse_name = models.CharField("Склад / направление", max_length=255)
    pickup_days = models.CharField("Дни забора", max_length=128, blank=True)
    delivery_days = models.CharField("Дни сдачи", max_length=128, blank=True)
    price_per_pallet = models.DecimalField("Цена за паллету", max_digits=14, decimal_places=2)
    minimum_pallets = models.PositiveIntegerField("От паллет", default=1)
    discount_percent = models.DecimalField("Скидка, %", max_digits=5, decimal_places=2, default=Decimal("0.00"))
    comment = models.CharField("Комментарий", max_length=255, blank=True)
    sort_order = models.PositiveIntegerField("Порядок", default=100)
    is_active = models.BooleanField("Активно", default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Строка прайса логистики"
        verbose_name_plural = "Прайс логистики"
        ordering = ["marketplace", "sort_order", "warehouse_name"]
        indexes = [models.Index(fields=["marketplace", "is_active"])]

    def clean(self):
        if self.price_per_pallet < 0:
            raise ValidationError({"price_per_pallet": "Цена не может быть отрицательной."})
        if self.discount_percent < 0 or self.discount_percent > 100:
            raise ValidationError({"discount_percent": "Скидка должна быть от 0 до 100%."})

    def __str__(self) -> str:
        return f"{self.get_marketplace_display()} · {self.warehouse_name}"


class ClientLogisticsTariff(models.Model):
    """Отдельная версия логистического прайса конкретного клиента."""

    STATUS_DRAFT = "draft"
    STATUS_ACTIVE = "active"
    STATUS_ARCHIVED = "archived"
    STATUS_CHOICES = [(STATUS_DRAFT, "Черновик"), (STATUS_ACTIVE, "Действует"), (STATUS_ARCHIVED, "Архив")]

    client = models.ForeignKey(Agency, on_delete=models.CASCADE, related_name="logistics_tariffs", verbose_name="Клиент")
    name = models.CharField("Название редакции", max_length=255)
    version_number = models.PositiveIntegerField("Номер версии", default=1)
    status = models.CharField("Статус", max_length=16, choices=STATUS_CHOICES, default=STATUS_DRAFT, db_index=True)
    valid_from = models.DateField("Действует с", default=timezone.localdate)
    valid_to = models.DateField("Действует до", null=True, blank=True)
    comment = models.TextField("Комментарий", blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="created_client_logistics_tariffs")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Редакция логистического тарифа клиента"
        verbose_name_plural = "Редакции логистических тарифов клиентов"
        ordering = ["client", "-valid_from", "-version_number"]
        constraints = [models.UniqueConstraint(fields=["client", "version_number"], name="uniq_client_logistics_tariff_version")]
        indexes = [models.Index(fields=["client", "status"])]

    def clean(self):
        if self.valid_to and self.valid_to < self.valid_from:
            raise ValidationError({"valid_to": "Дата окончания не может быть раньше даты начала."})

    def __str__(self) -> str:
        return f"{self.client} · {self.name}"


class ClientLogisticsTariffItem(models.Model):
    tariff = models.ForeignKey(ClientLogisticsTariff, on_delete=models.CASCADE, related_name="items", verbose_name="Редакция тарифа")
    source_line = models.ForeignKey(LogisticsPriceLine, on_delete=models.SET_NULL, null=True, blank=True, related_name="client_tariff_items", verbose_name="Строка основного прайса")
    marketplace = models.CharField("Маркетплейс", max_length=16, choices=LogisticsPriceLine.MARKETPLACE_CHOICES, default=LogisticsPriceLine.MARKETPLACE_OTHER)
    warehouse_name = models.CharField("Склад / направление", max_length=255)
    pickup_days = models.CharField("Дни забора", max_length=128, blank=True)
    delivery_days = models.CharField("Дни сдачи", max_length=128, blank=True)
    price_per_pallet = models.DecimalField("Цена клиента за паллету", max_digits=14, decimal_places=2)
    minimum_pallets = models.PositiveIntegerField("От паллет", default=1)
    discount_percent = models.DecimalField("Скидка, %", max_digits=5, decimal_places=2, default=Decimal("0.00"))
    comment = models.CharField("Комментарий", max_length=255, blank=True)
    sort_order = models.PositiveIntegerField("Порядок", default=100)
    is_active = models.BooleanField("Активно", default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Строка логистического тарифа клиента"
        verbose_name_plural = "Строки логистических тарифов клиентов"
        ordering = ["marketplace", "sort_order", "warehouse_name"]
        indexes = [models.Index(fields=["tariff", "is_active"])]

    def clean(self):
        if self.price_per_pallet < 0:
            raise ValidationError({"price_per_pallet": "Цена не может быть отрицательной."})
        if self.discount_percent < 0 or self.discount_percent > 100:
            raise ValidationError({"discount_percent": "Скидка должна быть от 0 до 100%."})

    def __str__(self) -> str:
        return f"{self.tariff}: {self.warehouse_name}"
