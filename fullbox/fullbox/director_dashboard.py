"""Mock context for director cabinet dashboard MVP (TZ §29)."""
from __future__ import annotations

from typing import Any


PERIOD_CHOICES = (
    ("today", "Сегодня"),
    ("week", "Неделя"),
    ("month", "Месяц"),
    ("quarter", "Квартал"),
)


def director_nav_items(*, active_key: str = "home") -> list[dict[str, Any]]:
    """Общая навигация панели директора."""
    items: list[dict[str, Any]] = [
        {"key": "home", "label": "Главная", "href": "/cabinet/director/", "badge": None},
        {"key": "finance", "label": "Финансы", "href": "/team-manager/billing/", "badge": None},
        {"key": "sales", "label": "Продажи", "href": "#sales", "badge": None, "soon": True},
        {"key": "clients", "label": "Клиенты", "href": "/team-manager/clients/", "badge": None},
        {"key": "orders", "label": "Заявки", "href": "/orders/", "badge": 4},
        {"key": "inventory", "label": "Остатки", "href": "/cabinet/director/inventory/", "badge": None},
        {"key": "processing", "label": "Обработка", "href": "/orders/processing/", "badge": None},
        {"key": "shipping", "label": "Отгрузки", "href": "/shipping/", "badge": None},
        {"key": "logistics", "label": "Логистика", "href": "/logistics/", "badge": None},
        {"key": "billing", "label": "Биллинг", "href": "/team-manager/billing/", "badge": None},
        {"key": "employees", "label": "Сотрудники", "href": "/employees/", "badge": None},
        {"key": "marketing", "label": "Маркетинг", "href": "#marketing", "badge": None, "soon": True},
        {"key": "risks", "label": "Риски", "href": "#attention", "badge": 7},
        {"key": "reports", "label": "Отчеты", "href": "#reports", "badge": None, "soon": True},
        {
            "key": "settings",
            "label": "Настройки",
            "href": "/cabinet/director/integrations/telegram/",
            "badge": None,
        },
    ]
    for item in items:
        item["active"] = item["key"] == active_key
    return items


def _money(value: int) -> str:
    return f"{value:,}".replace(",", " ") + " ₽"


def build_director_dashboard_context(request) -> dict[str, Any]:
    period = (request.GET.get("period") or "month").strip().lower()
    if period not in {p for p, _ in PERIOD_CHOICES}:
        period = "month"

    employee = getattr(request, "employee", None)
    full_name = (
        getattr(employee, "full_name", None)
        or request.session.get("employee_name")
        or getattr(request.user, "get_full_name", lambda: "")()
        or request.user.get_username()
    )
    initials = "".join(part[:1] for part in str(full_name).split()[:2]).upper() or "Д"

    kpis = [
        {
            "id": "revenue",
            "label": "Выручка",
            "value": _money(9_420_000),
            "delta": "↑ 12,4% к прошлому месяцу",
            "delta_class": "up",
            "extra": "План выполнен на 94%",
            "progress": 94,
            "icon": "₽",
            "source": "Биллинг / выставленные счета",
            "formula": "Сумма выставленных и оплаченных услуг за выбранный период.",
            "updated": "2 минуты назад",
        },
        {
            "id": "profit",
            "label": "Операционная прибыль",
            "value": _money(1_740_000),
            "delta": "↑ 8,1% к прошлому месяцу",
            "delta_class": "up",
            "extra": "Маржа 18,5%",
            "progress": 82,
            "icon": "%",
            "source": "Финансовая модель FullBox",
            "formula": "Выручка − прямые операционные затраты склада и логистики.",
            "updated": "2 минуты назад",
        },
        {
            "id": "cash",
            "label": "Остаток денег",
            "value": _money(4_260_000),
            "delta": "↓ 3,2% за неделю",
            "delta_class": "down",
            "extra": "На расчётных счетах",
            "progress": 0,
            "icon": "₸",
            "source": "Банк / выписка",
            "formula": "Сумма остатков по активным счетам компании.",
            "updated": "15 минут назад",
        },
        {
            "id": "receivables",
            "label": "Дебиторская задолженность",
            "value": _money(1_180_000),
            "delta": "↑ 5,0% к прошлому месяцу",
            "delta_class": "down",
            "extra": "4 счёта просрочены",
            "progress": 0,
            "icon": "!",
            "source": "Биллинг",
            "formula": "Неоплаченные счета со сроком оплаты ≤ сегодня.",
            "updated": "2 минуты назад",
        },
        {
            "id": "clients",
            "label": "Активные клиенты",
            "value": "25",
            "delta": "+2 за месяц",
            "delta_class": "up",
            "extra": "3 требуют внимания",
            "progress": 0,
            "icon": "◎",
            "source": "CRM / клиентский контур",
            "formula": "Клиенты с операциями или оплатами за последние 30 дней.",
            "updated": "2 минуты назад",
        },
        {
            "id": "orders",
            "label": "Заявки в работе",
            "value": "48",
            "delta": "4 просрочены",
            "delta_class": "down",
            "extra": "Среднее SLA 93,4%",
            "progress": 0,
            "icon": "☰",
            "source": "WMS",
            "formula": "Открытые заявки приёмки, обработки и отгрузки.",
            "updated": "1 минуту назад",
        },
        {
            "id": "sla",
            "label": "Выполнение SLA",
            "value": "93,4%",
            "delta": "↓ 1,1 п.п.",
            "delta_class": "down",
            "extra": "Цель 95%",
            "progress": 93,
            "icon": "✓",
            "source": "WMS",
            "formula": "Доля завершённых заявок в установленный срок.",
            "updated": "2 минуты назад",
        },
        {
            "id": "warehouse",
            "label": "Загрузка склада",
            "value": "78%",
            "delta": "702 / 900 палетомест",
            "delta_class": "up",
            "extra": "Свободно 198",
            "progress": 78,
            "icon": "▦",
            "source": "WMS / карта склада",
            "formula": "Занятые палетоместа / ёмкость склада.",
            "updated": "3 минуты назад",
        },
    ]

    nav_items = director_nav_items(active_key="home")

    chart_raw = [
        ("01", 280, 48),
        ("05", 310, 55),
        ("09", 295, 50),
        ("13", 340, 62),
        ("17", 360, 70),
        ("21", 390, 74),
        ("25", 410, 78),
        ("28", 430, 82),
    ]
    chart_max = 450
    chart_days = [
        {
            "label": label,
            "revenue": rev,
            "profit": profit,
            "revenue_pct": max(8, int(rev * 100 / chart_max)),
            "profit_pct": max(6, int(profit * 100 / 90)),
        }
        for label, rev, profit in chart_raw
    ]

    attention = [
        {
            "level": "critical",
            "title": "4 счета просрочены более чем на 10 дней",
            "object": "Биллинг",
            "owner": "Бухгалтерия",
            "due": "сегодня",
            "impact": "380 000 ₽",
            "href": "/team-manager/billing/overdue/",
        },
        {
            "level": "critical",
            "title": "Заявка WB-1058 нарушает SLA",
            "object": "Отгрузка",
            "owner": "Иванюк М.",
            "due": "2 ч",
            "impact": "SLA −1,1 п.п.",
            "href": "/shipping/",
        },
        {
            "level": "warn",
            "title": "Клиент «Компания А» снизил объём на 42%",
            "object": "Клиенты",
            "owner": "Менеджер портфеля",
            "due": "на неделе",
            "impact": "риск churn",
            "href": "/team-manager/clients/",
        },
        {
            "level": "warn",
            "title": "Маршрут №218 не подтверждён",
            "object": "Логистика",
            "owner": "Логист",
            "due": "сегодня 18:00",
            "impact": "рейс",
            "href": "/logistics/",
        },
        {
            "level": "warn",
            "title": "Загрузка зоны А достигла 96%",
            "object": "Склад",
            "owner": "Кладовщик",
            "due": "сейчас",
            "impact": "ёмкость",
            "href": "/sklad/journal/",
        },
    ]

    funnel = [
        {"label": "Подтверждены", "count": 12, "avg": "1,2 ч", "overdue": 0, "pct": 100},
        {"label": "Комплектуются", "count": 14, "avg": "4,5 ч", "overdue": 2, "pct": 85},
        {"label": "Готовы", "count": 8, "avg": "2,1 ч", "overdue": 1, "pct": 55},
        {"label": "В пути", "count": 7, "avg": "6,0 ч", "overdue": 1, "pct": 40},
        {"label": "Сданы", "count": 5, "avg": "—", "overdue": 0, "pct": 28},
        {"label": "Закрыты", "count": 18, "avg": "—", "overdue": 0, "pct": 100},
    ]

    zones = [
        {"code": "A", "load": 96, "hot": True},
        {"code": "B", "load": 74, "hot": False},
        {"code": "C", "load": 61, "hot": False},
        {"code": "Обр.", "load": 82, "hot": False},
        {"code": "ГП", "load": 55, "hot": False},
        {"code": "Брак", "load": 18, "hot": False},
    ]

    clients = [
        {"name": "ИП Талеев", "revenue": "1 240 000 ₽", "profit": "210 000 ₽", "debt": "0 ₽", "orders": 18, "status": "ok", "status_label": "Стабильный", "manager": "Демина Д."},
        {"name": "ООО Норд", "revenue": "980 000 ₽", "profit": "150 000 ₽", "debt": "120 000 ₽", "orders": 14, "status": "warn", "status_label": "Требует внимания", "manager": "Иванюк М."},
        {"name": "Компания А", "revenue": "720 000 ₽", "profit": "90 000 ₽", "debt": "260 000 ₽", "orders": 9, "status": "bad", "status_label": "Высокий риск", "manager": "Иванюк М."},
        {"name": "ООО Восток", "revenue": "640 000 ₽", "profit": "110 000 ₽", "debt": "0 ₽", "orders": 11, "status": "ok", "status_label": "Стабильный", "manager": "Демина Д."},
        {"name": "ИП Смирнов", "revenue": "510 000 ₽", "profit": "85 000 ₽", "debt": "45 000 ₽", "orders": 7, "status": "warn", "status_label": "Требует внимания", "manager": "Петрова А."},
        {"name": "ООО Юг", "revenue": "480 000 ₽", "profit": "70 000 ₽", "debt": "0 ₽", "orders": 6, "status": "ok", "status_label": "Стабильный", "manager": "Петрова А."},
        {"name": "ООО Центр", "revenue": "390 000 ₽", "profit": "55 000 ₽", "debt": "80 000 ₽", "orders": 5, "status": "warn", "status_label": "Требует внимания", "manager": "Демина Д."},
    ]

    employees = [
        {"name": "Иванюк М.", "role": "Менеджер", "metric": "План 108%", "tone": "ok"},
        {"name": "Демина Д.", "role": "Менеджер", "metric": "План 96%", "tone": "ok"},
        {"name": "Кладовщик смены А", "role": "Склад", "metric": "2 просрочки", "tone": "warn"},
        {"name": "Логист дежурный", "role": "Логистика", "metric": "Маршрут 218", "tone": "warn"},
    ]

    events = [
        {"time": "01:52", "title": "Акт ACT-2026-000157 создан", "meta": "Отгрузка SO-000001 · Демина Д.", "href": "/team-manager/billing/applications/202/"},
        {"time": "01:40", "title": "Тариф клиента опубликован", "meta": "ИП Талеев · Бухгалтерия", "href": "/team-manager/billing/tariffs/"},
        {"time": "01:12", "title": "Рейс №218 ожидает подтверждения", "meta": "Логистика", "href": "/logistics/"},
        {"time": "00:55", "title": "Зона A загружена на 96%", "meta": "Склад", "href": "/sklad/journal/"},
        {"time": "00:40", "title": "Счёт просрочен 12 дней", "meta": "Клиент Норд · 90 000 ₽", "href": "/team-manager/billing/overdue/"},
        {"time": "00:22", "title": "Заявка отгрузки отправлена клиенту", "meta": "WB-1058", "href": "/shipping/"},
        {"time": "23:58", "title": "Приёмка закрыта", "meta": "RCV-094 · Кладовщик", "href": "/orders/receiving/"},
        {"time": "23:40", "title": "Новый клиент активирован", "meta": "ООО Центр", "href": "/team-manager/clients/"},
        {"time": "23:15", "title": "Оплата поступила", "meta": "ИП Смирнов · 45 000 ₽", "href": "/team-manager/billing/payments/"},
        {"time": "22:50", "title": "Синхронизация 1С: 3 ошибки", "meta": "Интеграции", "href": "#integrations"},
    ]

    return {
        "period": period,
        "period_choices": PERIOD_CHOICES,
        "director_name": full_name,
        "director_initials": initials,
        "nav_items": nav_items,
        "integrations": [
            {"label": "WMS подключена", "state": "ok"},
            {"label": "1С подключена", "state": "ok"},
            {"label": "Банк подключен", "state": "ok"},
            {"label": "CRM подключена", "state": "ok"},
            {"label": "3 ошибки синхронизации", "state": "warn"},
        ],
        "updated_label": "данные обновлены 2 минуты назад",
        "kpis": kpis,
        "chart_days": chart_days,
        "plan": {
            "target": _money(10_000_000),
            "fact": _money(9_420_000),
            "forecast": _money(10_320_000),
            "pct": 94,
            "left": _money(580_000),
            "workdays_left": 8,
        },
        "attention": attention,
        "attention_critical_count": 7,
        "funnel": funnel,
        "warehouse": {
            "occupied": 702,
            "free": 198,
            "load_pct": 78,
            "boxes": "12 480",
            "units": "186 200",
            "stale_skus": 34,
        },
        "zones": zones,
        "client_health": [
            {"label": "Стабильные", "count": 18, "pct": 72, "color": "var(--color-success)"},
            {"label": "Требуют внимания", "count": 5, "pct": 20, "color": "var(--color-warning)"},
            {"label": "Высокий риск", "count": 2, "pct": 8, "color": "var(--color-danger)"},
        ],
        "clients": clients,
        "employees": employees,
        "events": events,
        "notifications_count": 7,
    }
