"""Реестр секций кабинета: stub / bridge / alias на существующие страницы."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class SectionMeta:
    path: str
    title: str
    group: str
    kind: str  # stub | bridge | alias
    description: str = ""
    # bridge: ссылка на существующий реестр (team-manager billing и т.п.)
    bridge_url: str = ""
    bridge_label: str = "Открыть реестр"
    # alias: внутренний redirect на готовый URL кабинета
    alias_url: str = ""
    permission_key: str = ""
    related: list[tuple[str, str]] = field(default_factory=list)


def _perm_key(path: str, explicit: str = "") -> str:
    if explicit:
        return explicit
    return path.replace("/", "_").replace("-", "_").strip("_")


def _stub(path: str, title: str, group: str, description: str = "", permission_key: str = "", related: list | None = None) -> SectionMeta:
    return SectionMeta(
        path=path,
        title=title,
        group=group,
        kind="stub",
        description=description
        or "Раздел готовится. Данные и операции появятся после подключения соответствующих моделей и процессов.",
        permission_key=_perm_key(path, permission_key),
        related=related or [],
    )


def _bridge(path: str, title: str, group: str, bridge_url: str, description: str = "", permission_key: str = "") -> SectionMeta:
    return SectionMeta(
        path=path,
        title=title,
        group=group,
        kind="bridge",
        description=description
        or "Реестр уже работает в биллинге менеджера. Откройте его из кабинета бухгалтера без дублирования страницы.",
        bridge_url=bridge_url,
        bridge_label="Открыть в биллинге",
        permission_key=_perm_key(path, permission_key),
        related=[("/accountant/storage/", "Хранение в кабинете"), ("/accountant/billing/", "Обзор биллинга")],
    )


def _alias(path: str, title: str, group: str, alias_url: str, permission_key: str = "") -> SectionMeta:
    return SectionMeta(
        path=path,
        title=title,
        group=group,
        kind="alias",
        alias_url=alias_url,
        permission_key=_perm_key(path, permission_key),
    )


SECTION_LIST: list[SectionMeta] = [
    # Клиенты
    _stub("clients/legal-entities", "Юридические лица клиентов", "Клиенты", "Карточки юрлиц клиентов появятся поверх текущих реквизитов Agency.", "clients_legal", [("/accountant/clients/", "Все клиенты")]),
    _stub("clients/requisites", "Реквизиты и ответственные", "Клиенты", "Сводный реестр реквизитов. Сейчас данные ведутся в карточке клиента.", "clients_requisites", [("/accountant/clients/", "Клиенты")]),
    _stub("clients/finance-settings", "Финансовые настройки", "Клиенты", "Отсрочка, лимиты, условия оплаты — раздел готовится.", "clients_finance", [("/accountant/clients/", "Клиенты")]),
    # Договоры
    _stub("contracts/addenda", "Дополнительные соглашения", "Договоры", related=[("/accountant/contracts/", "Договоры / КП")]),
    _stub("contracts/expiring", "Истекающие договоры", "Договоры", "Срок действия договоров пока не хранится отдельным полем — счётчик будет после модели сроков.", related=[("/accountant/contracts/", "Договоры")]),
    _stub("contracts/signing", "Документы на подписание", "Договоры", related=[("/accountant/contracts/", "Договоры")]),
    _stub("contracts/archive", "Архив договоров", "Договоры", related=[("/accountant/contracts/", "Договоры")]),
    # Тарифы (catalog / no-price — отдельные views в urls)
    _stub("tariffs/approval", "Тарифы на согласовании", "Тарифы и услуги", related=[("/accountant/tariff-check/", "Проверка тарифов")]),
    _stub("tariffs/history", "История версий тарифов", "Тарифы и услуги", "История доступна в карточке тарифа клиента.", related=[("/accountant/tariffs/", "Тарифы клиентов")]),
    _stub("tariffs/templates", "Шаблоны тарифов", "Тарифы и услуги"),
    # Начисления / биллинг bridges
    _bridge("charges", "Все начисления", "Начисления", "/team-manager/billing/charges/"),
    _alias("charges/applications", "Начисления по заявкам", "Начисления", "/accountant/charges/?scope=applications", "charges_apps"),
    _alias("charges/storage", "Хранение", "Начисления", "/accountant/storage/", "charges_storage"),
    _stub("charges/materials", "Расходные материалы", "Начисления"),
    _stub("charges/extra", "Дополнительные услуги", "Начисления"),
    _alias("charges/manual", "Ручные начисления", "Начисления", "/accountant/charges/?scope=manual", "charges_manual"),
    _alias("charges/no-price", "Начисления без цены", "Начисления", "/accountant/charges/?no_price=1", "charges_no_price"),
    _alias("charges/errors", "Ошибки расчёта", "Начисления", "/accountant/storage/", "charges_errors"),
    _alias("charges/adjustments", "Корректировки начислений", "Начисления", "/accountant/charges/adjustments/", "charges_adjustments"),
    _bridge("billing", "Обзор биллинга", "Биллинг", "/team-manager/billing/", description="Сводка и реестры биллинга доступны менеджеру; бухгалтер открывает их из кабинета."),
    _bridge("billing/applications", "Заявки к выставлению", "Биллинг", "/team-manager/billing/requests/"),
    _bridge("billing/charges-check", "Проверка начислений", "Биллинг", "/team-manager/billing/charges/"),
    _stub("billing/drafts", "Черновики документов", "Биллинг", related=[("/team-manager/billing/invoices/", "Счета"), ("/team-manager/billing/acts/", "Акты")]),
    _bridge("billing/ready", "Готово к выставлению", "Биллинг", "/team-manager/billing/requests/?ready=1"),
    _alias("billing/errors", "Ошибки биллинга", "Биллинг", "/accountant/storage/"),
    _stub("billing/period-close", "Закрытие расчётного периода", "Биллинг", related=[("/accountant/storage/", "Хранение / периоды")]),
    # Документы
    _bridge("documents/invoices", "Счета", "Документы", "/team-manager/billing/invoices/"),
    _bridge("documents/acts", "Акты", "Документы", "/team-manager/billing/acts/"),
    _stub("documents/upd", "УПД", "Документы"),
    _stub("documents/invoices-fact", "Счета-фактуры", "Документы"),
    _stub("documents/corrections", "Корректировочные документы", "Документы"),
    _stub("documents/reconciliations", "Акты сверки", "Документы"),
    _stub("documents/attachments", "Приложения и детализации", "Документы"),
    _stub("documents/to-send", "Документы на отправку", "Документы", related=[("/team-manager/billing/invoices/", "Счета")]),
    _stub("documents/to-sign", "Документы на подписание", "Документы"),
    _stub("documents/archive", "Архив документов", "Документы"),
    # Оплаты
    _bridge("payments", "Поступления от клиентов", "Оплаты и задолженность", "/team-manager/billing/payments/"),
    _stub("payments/allocation", "Распределение оплат", "Оплаты и задолженность"),
    _stub("payments/unidentified", "Неопознанные платежи", "Оплаты и задолженность", "Модель неопознанных платежей пока не подключена."),
    _stub("payments/partial", "Частичные оплаты", "Оплаты и задолженность", related=[("/team-manager/billing/invoices/", "Счета")]),
    _stub("payments/advances", "Авансы и переплаты", "Оплаты и задолженность"),
    _stub("payments/receivables", "Дебиторская задолженность", "Оплаты и задолженность", related=[("/team-manager/billing/overdue/", "Просрочка")]),
    _bridge("payments/overdue", "Просроченная задолженность", "Оплаты и задолженность", "/team-manager/billing/overdue/"),
    _stub("payments/promises", "Обещания оплаты", "Оплаты и задолженность"),
    # Поставщики и далее — stubs
    _stub("suppliers", "Все поставщики", "Поставщики"),
    _stub("suppliers/requisites", "Реквизиты поставщиков", "Поставщики"),
    _stub("suppliers/contracts", "Договоры поставщиков", "Поставщики"),
    _stub("suppliers/tariffs", "Тарифы поставщиков", "Поставщики"),
    _stub("suppliers/incoming", "Входящие документы", "Поставщики"),
    _stub("suppliers/closing", "Закрывающие документы", "Поставщики"),
    _stub("suppliers/payables", "Кредиторская задолженность", "Поставщики"),
    _stub("suppliers/check", "Проверка поставщиков", "Поставщики"),
    _stub("suppliers/archive", "Архив поставщиков", "Поставщики"),
    _stub("payment-requests", "Заявки на оплату", "Заявки на оплату"),
    _stub("payment-calendar", "Календарь платежей", "Платёжный календарь"),
    _stub("payment-calendar/inflows", "Плановые поступления", "Платёжный календарь"),
    _stub("payment-calendar/outflows", "Плановые расходы", "Платёжный календарь"),
    _stub("payment-calendar/regular", "Регулярные платежи", "Платёжный календарь"),
    _stub("payment-calendar/plan-fact", "План-факт платежей", "Платёжный календарь"),
    _stub("payment-calendar/forecast", "Прогноз остатков", "Платёжный календарь"),
    _stub("payment-calendar/cash-gaps", "Кассовые разрывы", "Платёжный календарь"),
    _stub("banks", "Банковские счета", "Банки"),
    _stub("banks/balances", "Остатки на счетах", "Банки"),
    _stub("banks/statements", "Банковские выписки", "Банки"),
    _stub("banks/operations", "Банковские операции", "Банки"),
    _stub("banks/unidentified", "Неопознанные операции", "Банки"),
    _stub("banks/payment-orders", "Платёжные поручения", "Банки"),
    _stub("banks/reconciliation", "Банковская сверка", "Банки"),
    _stub("banks/connections", "Подключения к банкам", "Банки"),
    _stub("banks/sync-errors", "Ошибки синхронизации банка", "Банки"),
    _stub("periods", "Открытые периоды", "Финансовые периоды"),
    _stub("periods/review", "Периоды на проверке", "Финансовые периоды"),
    _stub("periods/ready", "Готовые к закрытию", "Финансовые периоды"),
    _stub("periods/closed", "Закрытые периоды", "Финансовые периоды"),
    _stub("periods/reopened", "Переоткрытые периоды", "Финансовые периоды"),
    _stub("periods/errors", "Ошибки закрытия периода", "Финансовые периоды"),
    _stub("reports/revenue-clients", "Выручка по клиентам", "Отчёты"),
    _stub("reports/revenue-services", "Выручка по услугам", "Отчёты"),
    _stub("reports/charges-applications", "Начисления по заявкам", "Отчёты", related=[("/team-manager/billing/report/", "Отчёт биллинга")]),
    _stub("reports/storage", "Отчёт по хранению", "Отчёты", related=[("/accountant/storage/", "Хранение")]),
    _stub("reports/accrued-not-invoiced", "Начислено, но не выставлено", "Отчёты"),
    _stub("reports/invoiced-unpaid", "Выставлено, но не оплачено", "Отчёты", related=[("/team-manager/billing/overdue/", "Просрочка")]),
    _stub("reports/receivables", "Дебиторская задолженность", "Отчёты"),
    _stub("reports/payables", "Кредиторская задолженность", "Отчёты"),
    _stub("reports/cashflow", "Движение денежных средств", "Отчёты"),
    _stub("reports/vat", "НДС", "Отчёты"),
    _stub("reports/plan-fact", "План-факт", "Отчёты"),
    _stub("reports/pnl", "P&L", "Отчёты"),
    _stub("reports/client-profitability", "Прибыльность клиентов", "Отчёты"),
    _stub("reports/reconcile-wms", "Сверка WMS и биллинга", "Отчёты"),
    _stub("reports/reconcile-billing-docs", "Сверка биллинга и документов", "Отчёты"),
    _stub("reports/reconcile-docs-bank", "Сверка документов и банка", "Отчёты"),
    _alias("legal-entities", "Юридические лица FULLBOX", "Юридические лица FULLBOX", "/accountant/own-companies/", "le_all"),
    _stub("legal-entities/requisites", "Реквизиты компаний FullBox", "Юридические лица FULLBOX", related=[("/accountant/own-companies/", "Все компании")]),
    _stub("legal-entities/tax", "Налоговые настройки", "Юридические лица FULLBOX", related=[("/accountant/own-companies/", "Все компании")]),
    _stub("legal-entities/banks", "Банковские счета компаний", "Юридические лица FULLBOX"),
    _stub("legal-entities/signers", "Подписанты", "Юридические лица FULLBOX"),
    _stub("legal-entities/seals", "Печати и подписи", "Юридические лица FULLBOX"),
    _stub("legal-entities/templates", "Шаблоны документов", "Юридические лица FULLBOX"),
    _stub("legal-entities/numbering", "Правила нумерации", "Юридические лица FULLBOX"),
    _stub("integrations/wms", "Интеграция WMS", "Интеграции"),
    _stub("integrations/1c", "Интеграция 1С", "Интеграции"),
    _stub("integrations/banks", "Интеграция с банками", "Интеграции"),
    _stub("integrations/edo", "ЭДО", "Интеграции"),
    _stub("integrations/diadoc", "Диадок", "Интеграции"),
    _stub("integrations/sbis", "СБИС", "Интеграции"),
    _stub("integrations/email", "Email", "Интеграции"),
    _stub("integrations/telegram", "Telegram", "Интеграции"),
    _stub("integrations/max", "MAX", "Интеграции"),
    _stub("integrations/history", "История обмена", "Интеграции"),
    _stub("integrations/errors", "Ошибки синхронизации", "Интеграции"),
    _stub("settings/categories", "Категории услуг", "Настройки", related=[("/accountant/tariffs/catalog/", "Справочник услуг")]),
    _stub("settings/units", "Единицы измерения", "Настройки"),
    _stub("settings/vat-rates", "Ставки НДС", "Настройки"),
    _stub("settings/income-items", "Статьи доходов", "Настройки"),
    _stub("settings/expense-items", "Статьи расходов", "Настройки"),
    _stub("settings/cashflow-items", "Статьи ДДС", "Настройки"),
    _stub("settings/statuses", "Статусы", "Настройки"),
    _stub("settings/templates", "Шаблоны документов", "Настройки"),
    _stub("settings/approval-routes", "Маршруты согласования", "Настройки"),
    _stub("settings/roles", "Роли и права", "Настройки", "Расширение ролей (аудитор / финдир) — отдельное согласование жёлтой зоны employees."),
    _stub("settings/notifications", "Настройки уведомлений", "Настройки"),
    _alias("audit", "Все действия", "История и аудит", "/accountant/history/", "audit_all"),
    _stub("audit/requisites", "Изменения реквизитов", "История и аудит", related=[("/accountant/history/", "Вся история")]),
    _stub("audit/contracts", "Изменения договоров", "История и аудит", related=[("/accountant/history/", "Вся история")]),
    _stub("audit/tariffs", "Изменения тарифов", "История и аудит", related=[("/accountant/history/", "Вся история")]),
    _stub("audit/charges", "Изменения начислений", "История и аудит"),
    _stub("audit/documents", "Финансовые документы", "История и аудит"),
    _stub("audit/banks", "Банковские действия", "История и аудит"),
    _stub("audit/users", "Действия пользователей", "История и аудит"),
]

SECTIONS: dict[str, SectionMeta] = {s.path.strip("/"): s for s in SECTION_LIST}


def get_section(path: str) -> SectionMeta | None:
    key = (path or "").strip("/")
    return SECTIONS.get(key)
