"""Конфигурация бокового меню кабинета бухгалтера."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class MenuItem:
    key: str
    title: str
    url_name: str | None = None
    url: str | None = None
    icon: str = ""
    permission: str = "accountant.access"
    counter_key: str | None = None
    order: int = 100
    children: list["MenuItem"] = field(default_factory=list)
    query: str = ""
    external: bool = False
    description: str = ""


def accountant_menu_tree() -> list[MenuItem]:
    return [
        MenuItem(key="overview", title="Обзор", url_name="accountant-overview", order=10),
        MenuItem(
            key="clients",
            title="Клиенты",
            order=20,
            children=[
                MenuItem(key="clients_all", title="Все клиенты", url_name="accountant-clients", order=10),
                MenuItem(key="clients_legal", title="Юридические лица клиентов", url="/accountant/clients/legal-entities/", order=20),
                MenuItem(key="clients_requisites", title="Реквизиты и ответственные", url="/accountant/clients/requisites/", order=30),
                MenuItem(key="clients_finance", title="Финансовые настройки", url="/accountant/clients/finance-settings/", order=40),
                MenuItem(
                    key="clients_archive",
                    title="Архив клиентов",
                    url_name="accountant-clients",
                    query="?status=archived",
                    order=50,
                ),
            ],
        ),
        MenuItem(
            key="contracts",
            title="Договоры",
            order=30,
            children=[
                MenuItem(key="contracts_list", title="Договоры клиентов", url_name="accountant-contracts", order=10),
                MenuItem(key="contracts_kp", title="Коммерческие предложения", url_name="accountant-contracts", query="?tab=kp", order=20),
                MenuItem(key="contracts_addenda", title="Дополнительные соглашения", url="/accountant/contracts/addenda/", order=30),
                MenuItem(key="contracts_expiring", title="Истекающие договоры", url="/accountant/contracts/expiring/", order=40, counter_key="contracts_expiring"),
                MenuItem(key="contracts_signing", title="Документы на подписание", url="/accountant/contracts/signing/", order=50),
                MenuItem(key="contracts_archive", title="Архив договоров", url="/accountant/contracts/archive/", order=60),
            ],
        ),
        MenuItem(
            key="tariffs",
            title="Тарифы и услуги",
            order=40,
            children=[
                MenuItem(key="tariffs_clients", title="Тарифы клиентов — общие (FBO и склад)", url_name="accountant-tariffs", order=10),
                MenuItem(key="tariffs_logistics", title="Логистика", url_name="accountant-client-logistics-tariffs", order=20),
                MenuItem(key="tariffs_fbs", title="FBS", url_name="accountant-fbs-tariffs", order=30),
                MenuItem(key="tariffs_catalog", title="Справочник услуг FULLBOX", url="/accountant/tariffs/catalog/", order=40),
                MenuItem(key="logistics_price_list", title="Основной прайс логистики", url_name="accountant-logistics-price-list", order=50),
                MenuItem(key="tariffs_drafts", title="Черновики общих тарифов", url_name="accountant-tariffs", query="?status=draft", order=60, counter_key="tariff_drafts"),
                MenuItem(key="tariffs_review", title="На проверке", url_name="accountant-tariff-check", order=70, counter_key="tariff_review"),
                MenuItem(key="tariffs_approval", title="На согласовании", url="/accountant/tariffs/approval/", order=80),
                MenuItem(key="tariffs_published", title="Опубликованные общие тарифы", url_name="accountant-tariffs", query="?status=active", order=90),
                MenuItem(key="tariffs_no_price", title="Услуги без цены", url_name="accountant-tariffs-no-price", order=100, counter_key="services_no_price"),
                MenuItem(key="tariffs_history", title="История версий", url="/accountant/tariffs/history/", order=110),
                MenuItem(key="tariffs_templates", title="Шаблоны тарифов", url="/accountant/tariffs/templates/", order=120),
            ],
        ),
        MenuItem(
            key="charges",
            title="Начисления",
            order=50,
            children=[
                MenuItem(key="charges_all", title="Все начисления", url="/accountant/charges/", order=10),
                MenuItem(key="charges_apps", title="Начисления по заявкам", url="/accountant/charges/applications/", order=20),
                MenuItem(key="charges_storage", title="Хранение", url_name="accountant-storage", order=30, counter_key="storage_errors"),
                MenuItem(key="charges_materials", title="Расходные материалы", url="/accountant/charges/materials/", order=40),
                MenuItem(key="charges_extra", title="Дополнительные услуги", url="/accountant/charges/extra/", order=50),
                MenuItem(key="charges_manual", title="Ручные начисления", url="/accountant/charges/manual/", order=60),
                MenuItem(key="charges_no_price", title="Начисления без цены", url="/accountant/charges/no-price/", order=70, counter_key="charges_no_price"),
                MenuItem(key="charges_errors", title="Ошибки расчёта", url="/accountant/charges/errors/", order=80, counter_key="storage_errors"),
                MenuItem(key="charges_adjustments", title="Корректировки", url="/accountant/charges/adjustments/", order=90),
            ],
        ),
        MenuItem(
            key="billing",
            title="Биллинг",
            order=60,
            children=[
                MenuItem(key="billing_overview", title="Обзор биллинга", url="/accountant/billing/", order=10),
                MenuItem(key="billing_apps", title="Заявки к выставлению", url="/accountant/billing/applications/", order=20, counter_key="ready_to_invoice"),
                MenuItem(key="billing_check", title="Проверка начислений", url="/accountant/billing/charges-check/", order=30, counter_key="charges_review"),
                MenuItem(key="billing_drafts", title="Черновики документов", url="/accountant/billing/drafts/", order=40),
                MenuItem(key="billing_ready", title="Готово к выставлению", url="/accountant/billing/ready/", order=50, counter_key="ready_to_invoice", description="Очередь: можно выставлять / не трогать"),
                MenuItem(key="billing_errors", title="Ошибки биллинга", url="/accountant/billing/errors/", order=60, counter_key="storage_errors"),
                MenuItem(key="billing_close", title="Закрытие расчётного периода", url="/accountant/billing/period-close/", order=70),
            ],
        ),
        MenuItem(
            key="documents",
            title="Документы",
            order=70,
            children=[
                MenuItem(key="documents_invoices", title="Счета", url="/accountant/documents/invoices/", order=10, counter_key="unpaid_invoices"),
                MenuItem(key="documents_acts", title="Акты", url="/accountant/documents/acts/", order=20),
                MenuItem(key="documents_upd", title="УПД", url="/accountant/documents/upd/", order=30),
                MenuItem(key="documents_sf", title="Счета-фактуры", url="/accountant/documents/invoices-fact/", order=40),
                MenuItem(key="documents_corrections", title="Корректировочные документы", url="/accountant/documents/corrections/", order=50),
                MenuItem(key="documents_reconcile", title="Акты сверки", url="/accountant/documents/reconciliations/", order=60),
                MenuItem(key="documents_attachments", title="Приложения и детализации", url="/accountant/documents/attachments/", order=70),
                MenuItem(key="documents_send", title="Документы на отправку", url="/accountant/documents/to-send/", order=80),
                MenuItem(key="documents_sign", title="Документы на подписание", url="/accountant/documents/to-sign/", order=90),
                MenuItem(key="documents_archive", title="Архив документов", url="/accountant/documents/archive/", order=100),
            ],
        ),
        MenuItem(
            key="payments",
            title="Оплаты и задолженность",
            order=80,
            children=[
                MenuItem(key="payments_incoming", title="Поступления от клиентов", url="/accountant/payments/", order=10),
                MenuItem(key="payments_allocate", title="Распределение оплат", url="/accountant/payments/allocation/", order=20),
                MenuItem(key="payments_unidentified", title="Неопознанные платежи", url="/accountant/payments/unidentified/", order=30, counter_key="unidentified_payments"),
                MenuItem(key="payments_partial", title="Частичные оплаты", url="/accountant/payments/partial/", order=40),
                MenuItem(key="payments_advances", title="Авансы и переплаты", url="/accountant/payments/advances/", order=50),
                MenuItem(key="payments_ar", title="Дебиторская задолженность", url="/accountant/payments/receivables/", order=60, counter_key="unpaid_invoices"),
                MenuItem(key="payments_overdue", title="Просроченная задолженность", url="/accountant/payments/overdue/", order=70, counter_key="overdue_invoices"),
                MenuItem(key="payments_promises", title="Обещания оплаты", url="/accountant/payments/promises/", order=80),
            ],
        ),
        MenuItem(
            key="suppliers",
            title="Поставщики",
            order=90,
            children=[
                MenuItem(key="suppliers_all", title="Все поставщики", url="/accountant/suppliers/", order=10),
                MenuItem(key="suppliers_requisites", title="Реквизиты поставщиков", url="/accountant/suppliers/requisites/", order=20),
                MenuItem(key="suppliers_contracts", title="Договоры поставщиков", url="/accountant/suppliers/contracts/", order=30),
                MenuItem(key="suppliers_tariffs", title="Тарифы поставщиков", url="/accountant/suppliers/tariffs/", order=40),
                MenuItem(key="suppliers_incoming", title="Входящие документы", url="/accountant/suppliers/incoming/", order=50),
                MenuItem(key="suppliers_closing", title="Закрывающие документы", url="/accountant/suppliers/closing/", order=60),
                MenuItem(key="suppliers_ap", title="Кредиторская задолженность", url="/accountant/suppliers/payables/", order=70),
                MenuItem(key="suppliers_check", title="Проверка поставщиков", url="/accountant/suppliers/check/", order=80),
                MenuItem(key="suppliers_archive", title="Архив поставщиков", url="/accountant/suppliers/archive/", order=90),
            ],
        ),
        MenuItem(
            key="payment_requests",
            title="Заявки на оплату",
            order=100,
            children=[
                MenuItem(key="pr_new", title="Новые", url="/accountant/payment-requests/?status=new", order=10),
                MenuItem(key="pr_approval", title="На согласовании", url="/accountant/payment-requests/?status=approval", order=20),
                MenuItem(key="pr_clarify", title="Требуют уточнения", url="/accountant/payment-requests/?status=clarify", order=30),
                MenuItem(key="pr_approved", title="Согласованные", url="/accountant/payment-requests/?status=approved", order=40),
                MenuItem(key="pr_ready", title="Готовые к оплате", url="/accountant/payment-requests/?status=ready", order=50),
                MenuItem(key="pr_bank", title="Переданные в банк", url="/accountant/payment-requests/?status=bank", order=60),
                MenuItem(key="pr_paid", title="Оплаченные", url="/accountant/payment-requests/?status=paid", order=70),
                MenuItem(key="pr_rejected", title="Отклонённые", url="/accountant/payment-requests/?status=rejected", order=80),
            ],
        ),
        MenuItem(
            key="payment_calendar",
            title="Платёжный календарь",
            order=110,
            children=[
                MenuItem(key="pc_calendar", title="Календарь платежей", url="/accountant/payment-calendar/", order=10),
                MenuItem(key="pc_in", title="Плановые поступления", url="/accountant/payment-calendar/inflows/", order=20),
                MenuItem(key="pc_out", title="Плановые расходы", url="/accountant/payment-calendar/outflows/", order=30),
                MenuItem(key="pc_regular", title="Регулярные платежи", url="/accountant/payment-calendar/regular/", order=40),
                MenuItem(key="pc_planfact", title="План-факт", url="/accountant/payment-calendar/plan-fact/", order=50),
                MenuItem(key="pc_forecast", title="Прогноз остатков", url="/accountant/payment-calendar/forecast/", order=60),
                MenuItem(key="pc_gaps", title="Кассовые разрывы", url="/accountant/payment-calendar/cash-gaps/", order=70),
            ],
        ),
        MenuItem(
            key="banks",
            title="Банки",
            order=120,
            children=[
                MenuItem(key="banks_accounts", title="Банковские счета", url="/accountant/banks/", order=10),
                MenuItem(key="banks_balances", title="Остатки", url="/accountant/banks/balances/", order=20),
                MenuItem(key="banks_statements", title="Банковские выписки", url="/accountant/banks/statements/", order=30),
                MenuItem(key="banks_ops", title="Банковские операции", url="/accountant/banks/operations/", order=40),
                MenuItem(key="banks_unidentified", title="Неопознанные операции", url="/accountant/banks/unidentified/", order=50, counter_key="unidentified_payments"),
                MenuItem(key="banks_orders", title="Платёжные поручения", url="/accountant/banks/payment-orders/", order=60),
                MenuItem(key="banks_reconcile", title="Банковская сверка", url="/accountant/banks/reconciliation/", order=70),
                MenuItem(key="banks_connections", title="Подключения к банкам", url="/accountant/banks/connections/", order=80),
                MenuItem(key="banks_errors", title="Ошибки синхронизации", url="/accountant/banks/sync-errors/", order=90),
            ],
        ),
        MenuItem(
            key="periods",
            title="Финансовые периоды",
            order=130,
            children=[
                MenuItem(key="periods_open", title="Открытые периоды", url="/accountant/periods/", order=10),
                MenuItem(key="periods_review", title="На проверке", url="/accountant/periods/review/", order=20),
                MenuItem(key="periods_ready", title="Готовые к закрытию", url="/accountant/periods/ready/", order=30),
                MenuItem(key="periods_closed", title="Закрытые периоды", url="/accountant/periods/closed/", order=40),
                MenuItem(key="periods_reopened", title="Переоткрытые периоды", url="/accountant/periods/reopened/", order=50),
                MenuItem(key="periods_errors", title="Ошибки закрытия", url="/accountant/periods/errors/", order=60),
            ],
        ),
        MenuItem(
            key="reports",
            title="Отчёты",
            order=140,
            children=[
                MenuItem(key="rep_rev_clients", title="Выручка по клиентам", url="/accountant/reports/revenue-clients/", order=10),
                MenuItem(key="rep_rev_services", title="Выручка по услугам", url="/accountant/reports/revenue-services/", order=20),
                MenuItem(key="rep_charges_apps", title="Начисления по заявкам", url="/accountant/reports/charges-applications/", order=30),
                MenuItem(key="rep_storage", title="Отчёт по хранению", url="/accountant/reports/storage/", order=40),
                MenuItem(key="rep_not_invoiced", title="Начислено, но не выставлено", url="/accountant/reports/accrued-not-invoiced/", order=50),
                MenuItem(key="rep_unpaid", title="Выставлено, но не оплачено", url="/accountant/reports/invoiced-unpaid/", order=60),
                MenuItem(key="rep_ar", title="Дебиторская задолженность", url="/accountant/reports/receivables/", order=70),
                MenuItem(key="rep_ap", title="Кредиторская задолженность", url="/accountant/reports/payables/", order=80),
                MenuItem(key="rep_cashflow", title="Движение денежных средств", url="/accountant/reports/cashflow/", order=90),
                MenuItem(key="rep_vat", title="НДС", url="/accountant/reports/vat/", order=100),
                MenuItem(key="rep_planfact", title="План-факт", url="/accountant/reports/plan-fact/", order=110),
                MenuItem(key="rep_pl", title="P&L", url="/accountant/reports/pnl/", order=120),
                MenuItem(key="rep_profit", title="Прибыльность клиентов", url="/accountant/reports/client-profitability/", order=130),
                MenuItem(key="rep_wms", title="Сверка WMS и биллинга", url="/accountant/reports/reconcile-wms/", order=140),
                MenuItem(key="rep_billing_docs", title="Сверка биллинга и документов", url="/accountant/reports/reconcile-billing-docs/", order=150),
                MenuItem(key="rep_docs_bank", title="Сверка документов и банка", url="/accountant/reports/reconcile-docs-bank/", order=160),
            ],
        ),
        MenuItem(
            key="legal_entities",
            title="Юридические лица FULLBOX",
            order=150,
            children=[
                MenuItem(key="le_all", title="Все компании", url_name="accountant-own-companies", order=10),
                MenuItem(key="le_requisites", title="Реквизиты", url="/accountant/legal-entities/requisites/", order=20),
                MenuItem(key="le_tax", title="Налоговые настройки", url="/accountant/legal-entities/tax/", order=30),
                MenuItem(key="le_banks", title="Банковские счета", url="/accountant/legal-entities/banks/", order=40),
                MenuItem(key="le_signers", title="Подписанты", url="/accountant/legal-entities/signers/", order=50),
                MenuItem(key="le_seals", title="Печати и подписи", url="/accountant/legal-entities/seals/", order=60),
                MenuItem(key="le_templates", title="Шаблоны документов", url="/accountant/legal-entities/templates/", order=70),
                MenuItem(key="le_numbering", title="Правила нумерации", url="/accountant/legal-entities/numbering/", order=80),
            ],
        ),
        MenuItem(
            key="carriers",
            title="Перевозчики",
            order=155,
            children=[
                MenuItem(key="carriers_all", title="Все перевозчики", url_name="accountant-carriers", order=10),
                MenuItem(key="carriers_new", title="Добавить перевозчика", url_name="accountant-carrier-create", order=20),
            ],
        ),
        MenuItem(
            key="integrations",
            title="Интеграции",
            order=160,
            children=[
                MenuItem(key="int_wms", title="WMS", url="/accountant/integrations/wms/", order=10),
                MenuItem(key="int_1c", title="1С", url="/accountant/integrations/1c/", order=20),
                MenuItem(key="int_banks", title="Банки", url="/accountant/integrations/banks/", order=30),
                MenuItem(key="int_edo", title="ЭДО", url="/accountant/integrations/edo/", order=40),
                MenuItem(key="int_diadoc", title="Диадок", url="/accountant/integrations/diadoc/", order=50),
                MenuItem(key="int_sbis", title="СБИС", url="/accountant/integrations/sbis/", order=60),
                MenuItem(key="int_email", title="Email", url="/accountant/integrations/email/", order=70),
                MenuItem(key="int_tg", title="Telegram", url="/accountant/integrations/telegram/", order=80),
                MenuItem(key="int_max", title="MAX", url="/accountant/integrations/max/", order=90),
                MenuItem(key="int_history", title="История обмена", url="/accountant/integrations/history/", order=100),
                MenuItem(key="int_errors", title="Ошибки синхронизации", url="/accountant/integrations/errors/", order=110),
            ],
        ),
        MenuItem(
            key="settings",
            title="Настройки и справочники",
            order=170,
            children=[
                MenuItem(key="set_categories", title="Категории услуг", url="/accountant/settings/categories/", order=10),
                MenuItem(key="set_units", title="Единицы измерения", url="/accountant/settings/units/", order=20),
                MenuItem(key="set_vat", title="Ставки НДС", url="/accountant/settings/vat-rates/", order=30),
                MenuItem(key="set_income", title="Статьи доходов", url="/accountant/settings/income-items/", order=40),
                MenuItem(key="set_expense", title="Статьи расходов", url="/accountant/settings/expense-items/", order=50),
                MenuItem(key="set_cashflow", title="Статьи ДДС", url="/accountant/settings/cashflow-items/", order=60),
                MenuItem(key="set_statuses", title="Статусы", url="/accountant/settings/statuses/", order=70),
                MenuItem(key="set_templates", title="Шаблоны документов", url="/accountant/settings/templates/", order=80),
                MenuItem(key="set_routes", title="Маршруты согласования", url="/accountant/settings/approval-routes/", order=90),
                MenuItem(key="set_roles", title="Роли и права", url="/accountant/settings/roles/", order=100),
                MenuItem(key="set_notify", title="Настройки уведомлений", url="/accountant/settings/notifications/", order=110),
            ],
        ),
        MenuItem(
            key="audit",
            title="История и аудит",
            order=180,
            children=[
                MenuItem(key="audit_all", title="Все действия", url_name="accountant-history", order=10),
                MenuItem(key="audit_requisites", title="Изменения реквизитов", url="/accountant/audit/requisites/", order=20),
                MenuItem(key="audit_contracts", title="Изменения договоров", url="/accountant/audit/contracts/", order=30),
                MenuItem(key="audit_tariffs", title="Изменения тарифов", url="/accountant/audit/tariffs/", order=40),
                MenuItem(key="audit_charges", title="Изменения начислений", url="/accountant/audit/charges/", order=50),
                MenuItem(key="audit_docs", title="Финансовые документы", url="/accountant/audit/documents/", order=60),
                MenuItem(key="audit_banks", title="Банковские действия", url="/accountant/audit/banks/", order=70),
                MenuItem(key="audit_users", title="Действия пользователей", url="/accountant/audit/users/", order=80),
            ],
        ),
    ]


def resolve_item_href(item: MenuItem) -> str:
    if item.url:
        return item.url + (item.query or "")
    if item.url_name:
        from django.urls import NoReverseMatch, reverse

        try:
            base = reverse(item.url_name)
        except NoReverseMatch:
            base = "#"
        return base + (item.query or "")
    return "#"


def _normalize_path(path: str) -> str:
    p = (path or "").split("?")[0].rstrip("/")
    return p or "/"


def path_is_active(href_path: str, request_path: str) -> bool:
    """Активность пункта: точное совпадение или detail `/…/<id>/` под списком."""
    if not href_path or href_path == "#":
        return False
    h = _normalize_path(href_path)
    p = _normalize_path(request_path)
    if p == h:
        return True
    if h == "/accountant":
        return False
    if p.startswith(h + "/"):
        rest = p[len(h) + 1 :]
        first = rest.split("/")[0]
        return first.isdigit()
    return False


def build_menu_context(*, request_path: str, counters: dict[str, int] | None = None, role: str | None = None) -> list[dict[str, Any]]:
    """Превращает дерево меню в dict для шаблона с active/open/counters."""
    from .section_permissions import can_see_menu_key

    counters = counters or {}
    path = (request_path or "").split("?")[0]
    rows: list[dict[str, Any]] = []

    for group in accountant_menu_tree():
        if not can_see_menu_key(role, group.key):
            continue
        children_out = []
        group_active = False
        for child in sorted(group.children, key=lambda c: c.order):
            if not can_see_menu_key(role, child.key):
                continue
            href = resolve_item_href(child)
            href_path = href.split("?")[0]
            active = path_is_active(href_path, path)
            # Совместимость старых URL с новыми пунктами меню
            if not active:
                p = _normalize_path(path)
                if child.key == "charges_storage" and p.endswith("/accountant/storage"):
                    active = True
                elif child.key == "audit_all" and p.endswith("/accountant/history"):
                    active = True
                elif child.key == "le_all" and "/accountant/own-companies" in p:
                    active = True
                elif child.key in {"carriers_all", "carriers_new"} and "/accountant/carriers" in p:
                    active = True
                elif child.key == "tariffs_review" and p.endswith("/accountant/tariff-check"):
                    active = True
            if active:
                group_active = True
            children_out.append(
                {
                    "key": child.key,
                    "title": child.title,
                    "href": href,
                    "active": active,
                    "counter": int(counters.get(child.counter_key or "", 0) or 0) if child.counter_key else 0,
                    "external": child.external,
                }
            )
        if group.children and not children_out:
            continue
        href = resolve_item_href(group) if not group.children else ""
        if not group.children:
            group_active = path_is_active(href.split("?")[0], path) if href else False
            if _normalize_path(href.split("?")[0] if href else "") == "/accountant":
                group_active = _normalize_path(path) == "/accountant"
        # Специальные привязки старых URL к группам
        if not group_active:
            if group.key == "charges" and path.rstrip("/").endswith("/accountant/storage"):
                group_active = True
            if group.key == "audit" and "/accountant/history" in path:
                group_active = True
            if group.key == "legal_entities" and "/accountant/own-companies" in path:
                group_active = True
            if group.key == "carriers" and "/accountant/carriers" in path:
                group_active = True
            if group.key == "tariffs" and "/accountant/tariff-check" in path:
                group_active = True
        rows.append(
            {
                "key": group.key,
                "title": group.title,
                "href": href,
                "active": group_active,
                "open": group_active,
                "counter": sum(c["counter"] for c in children_out),
                "children": children_out,
                "has_children": bool(children_out),
            }
        )
    return rows
