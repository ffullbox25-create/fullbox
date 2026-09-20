from __future__ import annotations

from django.conf import settings
from django.db import models

from head_manager.models import OwnCompany
from sku.models import Agency


class ClientLifecycle(models.Model):
    STATUS_DRAFT = "draft"
    STATUS_ACCOUNTANT_REVIEW = "accountant_review"
    STATUS_REQUISITES_READY = "requisites_ready"
    STATUS_TARIFFS_READY = "tariffs_ready"
    STATUS_ACTIVE = "active"
    STATUS_BLOCKED = "blocked"
    STATUS_ARCHIVED = "archived"
    STATUS_CHOICES = [
        (STATUS_DRAFT, "Черновик"),
        (STATUS_ACCOUNTANT_REVIEW, "На проверке бухгалтера"),
        (STATUS_REQUISITES_READY, "Реквизиты заполнены"),
        (STATUS_TARIFFS_READY, "Тарифы заполнены"),
        (STATUS_ACTIVE, "Активен"),
        (STATUS_BLOCKED, "Заблокирован"),
        (STATUS_ARCHIVED, "Архивный"),
    ]

    TYPE_IP = "ip"
    TYPE_OOO = "ooo"
    TYPE_AO = "ao"
    TYPE_SELF = "self_employed"
    TYPE_PERSON = "person"
    TYPE_CHOICES = [
        (TYPE_IP, "ИП"),
        (TYPE_OOO, "ООО"),
        (TYPE_AO, "Акционерное общество"),
        (TYPE_SELF, "Самозанятый"),
        (TYPE_PERSON, "Физлицо"),
    ]
    VAT_NO = "no_vat"
    VAT_INCLUDED = "vat"
    VAT_EXTRA = "vat_extra"
    VAT_TYPE_CHOICES = [
        (VAT_NO, "Без НДС"),
        (VAT_INCLUDED, "НДС в том числе"),
        (VAT_EXTRA, "НДС сверху"),
    ]

    agency = models.OneToOneField(
        Agency,
        on_delete=models.CASCADE,
        related_name="lifecycle",
        verbose_name="Клиент",
    )
    status = models.CharField("Статус", max_length=32, choices=STATUS_CHOICES, default=STATUS_DRAFT, db_index=True)
    client_type = models.CharField("Тип клиента", max_length=32, choices=TYPE_CHOICES, blank=True)
    vat_type = models.CharField("НДС клиента", max_length=32, choices=VAT_TYPE_CHOICES, default=VAT_NO)
    postal_address = models.CharField("Почтовый адрес", max_length=512, blank=True)
    email_documents = models.EmailField("Email для документов", blank=True)
    email_notifications = models.EmailField("Email для уведомлений", blank=True)
    accountant_comment = models.TextField("Комментарий бухгалтера", blank=True)
    serving_company = models.ForeignKey(
        OwnCompany,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="served_clients",
        verbose_name="Компания обслуживания FullBox",
    )
    commercial_offer_number = models.CharField("Номер КП", max_length=128, blank=True)
    contract_date = models.DateField("Дата договора", null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_client_lifecycles",
        verbose_name="Кто создал",
    )
    activated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="activated_client_lifecycles",
        verbose_name="Кто активировал",
    )
    activated_at = models.DateTimeField("Активирован", null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Жизненный цикл клиента"
        verbose_name_plural = "Жизненные циклы клиентов"
        indexes = [
            models.Index(fields=["status"]),
        ]

    def __str__(self) -> str:
        return f"{self.agency_id}: {self.get_status_display()}"

    @property
    def is_manager_visible(self) -> bool:
        return self.status == self.STATUS_ACTIVE and not self.agency.archived


class ClientChangeLog(models.Model):
    agency = models.ForeignKey(Agency, on_delete=models.CASCADE, related_name="accountant_change_logs")
    lifecycle = models.ForeignKey(
        ClientLifecycle,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="change_logs",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="client_accountant_changes",
    )
    field_name = models.CharField("Поле", max_length=128)
    old_value = models.TextField("Было", blank=True)
    new_value = models.TextField("Стало", blank=True)
    comment = models.TextField("Комментарий", blank=True)
    changed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "История изменений клиента"
        verbose_name_plural = "История изменений клиентов"
        ordering = ["-changed_at", "-id"]
