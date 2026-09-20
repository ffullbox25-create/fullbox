"""Агрегаты для обзора и счётчиков меню кабинета бухгалтера."""
from __future__ import annotations

from dataclasses import dataclass

from django.db.models import Exists, OuterRef, Q
from django.utils import timezone

from billing.models import (
    ApplicationCharge,
    BillingApplication,
    BillingService,
    ClientInvoice,
    ClientTariffItem,
    ClientTariffVersion,
    StorageBillingError,
)
from billing.statuses import BillingStatus, InvoiceStatus

from .models import ClientLifecycle


@dataclass(frozen=True)
class AttentionItem:
    key: str
    title: str
    count: int
    href: str
    tone: str = "warn"  # warn | danger | info


@dataclass(frozen=True)
class KpiCard:
    key: str
    title: str
    value: int | str
    href: str
    hint: str = ""
    tone: str = "default"


class AccountantDashboardService:
    """Только aggregate/count — без N+1 по карточкам."""

    def counters(self) -> dict[str, int]:
        today = timezone.localdate()
        unpaid_statuses = [
            InvoiceStatus.ISSUED,
            InvoiceStatus.SENT,
            InvoiceStatus.PARTIALLY_PAID,
            InvoiceStatus.REQUIRED,
        ]
        data = {
            "drafts": ClientLifecycle.objects.filter(status=ClientLifecycle.STATUS_DRAFT).count(),
            "review": ClientLifecycle.objects.filter(
                status__in=[
                    ClientLifecycle.STATUS_ACCOUNTANT_REVIEW,
                    ClientLifecycle.STATUS_REQUISITES_READY,
                    ClientLifecycle.STATUS_TARIFFS_READY,
                ]
            ).count(),
            "active": ClientLifecycle.objects.filter(status=ClientLifecycle.STATUS_ACTIVE).count(),
            "tariff_drafts": ClientTariffVersion.objects.filter(status=ClientTariffVersion.STATUS_DRAFT).count(),
            "tariff_review": ClientLifecycle.objects.filter(
                status__in=[
                    ClientLifecycle.STATUS_ACCOUNTANT_REVIEW,
                    ClientLifecycle.STATUS_TARIFFS_READY,
                ]
            ).count(),
            "services_no_price": ClientTariffItem.objects.filter(
                tariff_version__status=ClientTariffVersion.STATUS_DRAFT,
                price=0,
            ).count(),
            "storage_errors": StorageBillingError.objects.filter(resolved_at__isnull=True).count(),
            "charges_review": ApplicationCharge.objects.filter(is_confirmed=False, is_disputed=False).count(),
            "charges_no_price": ApplicationCharge.objects.filter(Q(tariff__isnull=True) | Q(tariff=0) | Q(amount=0)).count(),
            "ready_to_invoice": BillingApplication.objects.filter(
                billing_status__in=[BillingStatus.INVOICE_REQUIRED, BillingStatus.ACT_CONFIRMED]
            ).count(),
            "unpaid_invoices": ClientInvoice.objects.filter(status__in=unpaid_statuses).exclude(debt_amount=0).count(),
            "overdue_invoices": ClientInvoice.objects.filter(
                status__in=unpaid_statuses,
                due_date__lt=today,
            )
            .exclude(debt_amount=0)
            .count(),
            "partial_payments": ClientInvoice.objects.filter(status=InvoiceStatus.PARTIALLY_PAID).count(),
            # Пока нет моделей — явно 0, без фейковых данных
            "unidentified_payments": 0,
            "contracts_expiring": 0,
            "without_contract": ClientLifecycle.objects.filter(
                status__in=[ClientLifecycle.STATUS_ACTIVE, ClientLifecycle.STATUS_TARIFFS_READY]
            )
            .filter(Q(agency__contract_numb__isnull=True) | Q(agency__contract_numb=""))
            .filter(Q(commercial_offer_number__isnull=True) | Q(commercial_offer_number=""))
            .count(),
            "without_tariff": 0,
            "catalog_services": BillingService.objects.filter(is_active=True).count(),
        }
        active_tariff_exists = ClientTariffVersion.objects.filter(
            status=ClientTariffVersion.STATUS_ACTIVE,
            client_id=OuterRef("agency_id"),
        )
        data["without_tariff"] = (
            ClientLifecycle.objects.filter(status=ClientLifecycle.STATUS_ACTIVE)
            .annotate(has_active_tariff=Exists(active_tariff_exists))
            .filter(has_active_tariff=False)
            .count()
        )
        return data

    def kpi_cards(self, counters: dict[str, int] | None = None) -> list[KpiCard]:
        c = counters or self.counters()
        return [
            KpiCard("drafts", "Клиенты-черновики", c["drafts"], "/accountant/clients/?status=draft", "Нужно заполнить реквизиты"),
            KpiCard("review", "На проверке", c["review"], "/accountant/tariff-check/", "Реквизиты / тарифы"),
            KpiCard("without_contract", "Без договора / КП", c["without_contract"], "/accountant/contracts/", "Активные без номера"),
            KpiCard("without_tariff", "Без тарифа", c["without_tariff"], "/accountant/tariff-check/", "Активные без опубликованного тарифа"),
            KpiCard("services_no_price", "Услуги без цены", c["services_no_price"], "/accountant/tariffs/no-price/", "В черновиках тарифов"),
            KpiCard("charges_review", "Начисления на проверке", c["charges_review"], "/accountant/billing/charges-check/", "Не подтверждены"),
            KpiCard("ready_to_invoice", "Готово к выставлению", c["ready_to_invoice"], "/accountant/billing/ready/", "Очередь к счёту"),
            KpiCard("unpaid_invoices", "Неоплаченные счета", c["unpaid_invoices"], "/accountant/documents/invoices/", "С задолженностью"),
            KpiCard("overdue_invoices", "Просроченные счета", c["overdue_invoices"], "/accountant/payments/overdue/", "Срок оплаты прошёл", "danger"),
            KpiCard("storage_errors", "Ошибки хранения", c["storage_errors"], "/accountant/storage/", "Открытые ошибки расчёта", "danger"),
            KpiCard("planned_payments", "Плановые платежи", "—", "/accountant/payment-calendar/", "Раздел готовится"),
            KpiCard("bank_balance", "Остатки на счетах", "—", "/accountant/banks/balances/", "Раздел готовится"),
            KpiCard("cash_gap", "Кассовый разрыв", "—", "/accountant/payment-calendar/cash-gaps/", "Раздел готовится"),
        ]

    def attention_queue(self, counters: dict[str, int] | None = None) -> list[AttentionItem]:
        c = counters or self.counters()
        items = [
            AttentionItem("overdue", "Просроченные счета", c["overdue_invoices"], "/accountant/payments/overdue/", "danger"),
            AttentionItem("storage_errors", "Ошибки хранения", c["storage_errors"], "/accountant/storage/", "danger"),
            AttentionItem("services_no_price", "Услуги без цены", c["services_no_price"], "/accountant/tariffs/no-price/", "warn"),
            AttentionItem("charges_review", "Начисления на проверке", c["charges_review"], "/accountant/billing/charges-check/", "warn"),
            AttentionItem("ready", "Готово к выставлению", c["ready_to_invoice"], "/accountant/billing/ready/", "info"),
            AttentionItem("tariff_drafts", "Черновики тарифов", c["tariff_drafts"], "/accountant/tariffs/?status=draft", "info"),
            AttentionItem("without_tariff", "Клиенты без тарифа", c["without_tariff"], "/accountant/tariff-check/", "warn"),
            AttentionItem("partial", "Частичные оплаты", c["partial_payments"], "/accountant/payments/partial/", "info"),
        ]
        return [i for i in items if i.count > 0]

    def quick_start_steps(self) -> list[dict]:
        return [
            {"n": 1, "title": "Создать клиента и заполнить реквизиты", "href": "/accountant/clients/"},
            {"n": 2, "title": "Выбрать компанию обслуживания FullBox", "href": "/accountant/own-companies/"},
            {"n": 3, "title": "Указать договор или КП", "href": "/accountant/contracts/"},
            {"n": 4, "title": "Создать и опубликовать тариф", "href": "/accountant/tariffs/"},
            {"n": 5, "title": "Активировать клиента для менеджеров", "href": "/accountant/clients/?status=draft"},
        ]

    def quick_actions(self) -> list[dict]:
        return [
            {"title": "Новый клиент", "href": "/accountant/clients/", "primary": True},
            {"title": "Новый тариф", "href": "/accountant/tariffs/", "primary": False},
            {"title": "Проверить тарифы", "href": "/accountant/tariff-check/", "primary": False},
            {"title": "Хранение", "href": "/accountant/storage/", "primary": False},
            {"title": "Счета", "href": "/accountant/documents/invoices/", "primary": False},
            {"title": "Биллинг", "href": "/accountant/billing/", "primary": False},
        ]
