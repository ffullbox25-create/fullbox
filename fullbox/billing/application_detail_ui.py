"""Контекст UI страницы расчёта по заявке (ТЗ менеджерского биллинга)."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .charge_status import charge_missing_price, charge_source_label
from .models import ApplicationCharge
from .services import BillingWorkflowService
from .statuses import ActStatus, BillingStatus, InvoiceStatus


OPERATIONAL_STATUS_RU = {
    "draft": "Черновик",
    "submitted": "Новая",
    "reserved": "Зарезервирована",
    "storekeeper_accepted": "Принята складом",
    "picking": "В отборе",
    "packed": "Упакована",
    "shipped": "Отгружена",
    "partial_shipped": "Отгружена частично",
    "canceled": "Отменена",
    "cancelled": "Отменена",
    "done": "Выполнена",
    "completed": "Завершена",
    "in_progress": "В работе",
}

MONTHS_RU = {
    1: "январь",
    2: "февраль",
    3: "март",
    4: "апрель",
    5: "май",
    6: "июнь",
    7: "июль",
    8: "август",
    9: "сентябрь",
    10: "октябрь",
    11: "ноябрь",
    12: "декабрь",
}


def _fmt_day(value: date | None) -> str:
    return value.strftime("%d.%m.%Y") if value else ""


def storage_charge_period_label(charge: ApplicationCharge, storage_days: list | None = None) -> str:
    """Читабельный период строки хранения.

    В заявке хранения начисления создаются по дневным снимкам. Менеджеру важно
    видеть не только услугу «Хранение», но и конкретный день/диапазон, за который
    сформирована строка.
    """
    days = sorted({row.day for row in storage_days or [] if getattr(row, "day", None)})
    if len(days) == 1:
        return f"День хранения: {_fmt_day(days[0])}"
    if len(days) > 1:
        return f"Период строки: {_fmt_day(days[0])}–{_fmt_day(days[-1])}"
    performed_at = getattr(charge, "performed_at", None)
    if performed_at:
        performed_day = timezone.localdate(performed_at) if getattr(performed_at, "tzinfo", None) else performed_at.date()
        return f"День хранения: {_fmt_day(performed_day)}"
    billing_period = getattr(charge, "billing_period", None)
    if billing_period:
        month = MONTHS_RU.get(billing_period.month, billing_period.strftime("%m"))
        return f"Период хранения: {month} {billing_period.year}"
    return ""


def storage_application_period_label(application, storage_days: list | None = None) -> str:
    days = sorted({row.day for row in storage_days or [] if getattr(row, "day", None)})
    if days:
        month = MONTHS_RU.get(days[0].month, days[0].strftime("%m"))
        if days[0] == days[-1]:
            return f"Период хранения: {month} {days[0].year}; день расчёта {_fmt_day(days[0])}"
        return f"Период хранения: {month} {days[0].year}; дни расчёта {_fmt_day(days[0])}–{_fmt_day(days[-1])}"
    source = getattr(application, "source_payload", None) or {}
    period = str(source.get("period") or "").strip()
    if period.startswith("STR-"):
        parts = period.split("-")
        if len(parts) >= 3 and parts[-2].isdigit() and parts[-1].isdigit():
            year = int(parts[-2])
            month_num = int(parts[-1])
            month = MONTHS_RU.get(month_num, f"{month_num:02d}")
            return f"Период хранения: {month} {year}"
    created_at = getattr(application, "created_at_source", None) or getattr(application, "created_at", None)
    if created_at:
        day = timezone.localdate(created_at) if getattr(created_at, "tzinfo", None) else created_at.date()
        month = MONTHS_RU.get(day.month, day.strftime("%m"))
        return f"Период хранения: {month} {day.year}"
    return ""


def _payload_of(application) -> dict:
    payload = getattr(application, "source_payload", None) or {}
    if not isinstance(payload, dict):
        return {}
    nested = payload.get("payload")
    if isinstance(nested, dict):
        merged = dict(payload)
        merged.update(nested)
        return merged
    return payload


def _decimal_or_none(value) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value).replace(",", "."))
    except Exception:
        return None


def _fmt_qty(value) -> str:
    qty = _decimal_or_none(value)
    if qty is None:
        return "—"
    if qty == qty.to_integral_value():
        return f"{int(qty)}"
    text = f"{qty.normalize():f}"
    return text.rstrip("0").rstrip(".") or "0"


def _first_number(data: dict, keys: tuple[str, ...]) -> Decimal | None:
    for key in keys:
        qty = _decimal_or_none(data.get(key))
        if qty is not None:
            return qty
    return None


def _sum_rows(rows, keys: tuple[str, ...]) -> Decimal | None:
    if not isinstance(rows, list):
        return None
    total = Decimal("0")
    has_value = False
    for row in rows:
        if not isinstance(row, dict):
            continue
        qty = _first_number(row, keys)
        if qty is None:
            continue
        total += qty
        has_value = True
    return total if has_value else None


def _count_list_rows(data: dict, keys: tuple[str, ...]) -> Decimal | None:
    for key in keys:
        value = data.get(key)
        if isinstance(value, list) and value:
            return Decimal(len(value))
    return None


def _format_payload_datetime(value) -> str:
    if not value:
        return "—"
    if hasattr(value, "date"):
        dt = value
    else:
        dt = parse_datetime(str(value))
    if not dt:
        return str(value)
    if timezone.is_aware(dt):
        dt = timezone.localtime(dt)
    return dt.strftime("%d.%m.%Y %H:%M")


FACT_LABELS = {
    "receiving": {
        "title": "Факт приёмки",
        "goods": "Товар принято",
        "boxes": "Коробов принято",
        "pallets": "Паллет принято",
        "closed": "принято",
        "empty": "Факт приёмки пока не найден",
    },
    "shipping": {
        "title": "Факт отгрузки",
        "goods": "Товар отгружен",
        "boxes": "Коробов отгружено",
        "pallets": "Паллет отгружено",
        "closed": "отгружено",
        "empty": "Факт отгрузки пока не найден",
    },
    "processing": {
        "title": "Факт обработки",
        "goods": "Товар обработан",
        "boxes": "Коробов",
        "pallets": "Паллет",
        "closed": "завершено",
        "empty": "Факт обработки пока не найден",
    },
    "packing": {
        "title": "Факт упаковки",
        "goods": "Товар упакован",
        "boxes": "Коробов",
        "pallets": "Паллет",
        "closed": "завершено",
        "empty": "Факт упаковки пока не найден",
    },
    "storage": {
        "title": "Факт хранения",
        "goods": "Товар на хранении",
        "boxes": "Коробов",
        "pallets": "Паллет",
        "closed": "рассчитано",
        "empty": "Факт хранения пока не найден",
    },
}


def build_application_fact_summary(
    application,
    warehouse_facts: list | None = None,
    *,
    source_fact_payload: dict | None = None,
) -> dict:
    """Сводка операционного факта заявки для модального окна в биллинге."""
    app_type = str(getattr(application, "application_type", "") or "").strip()
    labels = FACT_LABELS.get(
        app_type,
        {
            "title": "Факт заявки",
            "goods": "Товар по факту",
            "boxes": "Коробов",
            "pallets": "Паллет",
            "closed": "завершено",
            "empty": "Факт заявки пока не найден",
        },
    )

    data = _payload_of(application)
    if isinstance(source_fact_payload, dict):
        data.update(source_fact_payload)
    act_items = data.get("act_items")
    if not isinstance(act_items, list) or not act_items:
        for key in ("items", "rows", "products", "sku_rows"):
            if isinstance(data.get(key), list) and data.get(key):
                act_items = data.get(key)
                break
        else:
            act_items = []

    actual_keys = (
        "actual_qty",
        "accepted_qty",
        "qty_shipped",
        "shipped_qty",
        "processed_qty",
        "packed_qty",
        "picked_qty",
        "done_qty",
        "fact_qty",
        "qty",
        "quantity",
        "count",
    )
    planned_keys = (
        "planned_qty",
        "requested_qty",
        "qty_requested",
        "qty_reserved",
        "expected_qty",
        "qty",
        "quantity",
        "count",
    )
    accepted_goods = _sum_rows(act_items, actual_keys) or _first_number(
        data,
        (
            "actual_qty",
            "accepted_total",
            "qty_shipped",
            "shipped_qty",
            "processed_qty",
            "packed_qty",
            "items_count",
            "products_count",
            "qty_total",
            "quantity",
        ),
    )
    planned_goods = _sum_rows(act_items, planned_keys)
    boxes = (
        _first_number(
            data,
            (
                "actual_boxes",
                "accepted_boxes",
                "shipped_boxes",
                "packed_boxes",
                "box_count",
                "boxes_count",
                "places_count",
                "expected_boxes",
            ),
        )
        or _count_list_rows(data, ("act_boxes", "boxes", "shipping_boxes", "packed_boxes"))
    )
    pallets = (
        _first_number(data, ("actual_pallets", "shipped_pallets", "packed_pallets", "pallet_count", "pallets_count", "expected_pallets"))
        or _count_list_rows(data, ("act_pallets", "pallets", "flow_pallets", "shipping_pallets"))
    )

    fact_rows = []
    for row in act_items:
        if not isinstance(row, dict):
            continue
        planned = _first_number(row, planned_keys)
        actual = _first_number(row, actual_keys)
        fact_rows.append(
            {
                "sku": str(row.get("sku") or row.get("sku_code") or row.get("article") or row.get("barcode") or "").strip() or "—",
                "name": str(row.get("name") or row.get("title") or row.get("product_name") or "").strip() or "—",
                "planned": _fmt_qty(planned),
                "actual": _fmt_qty(actual),
            }
        )

    warehouse_rows = []
    for fact in warehouse_facts or []:
        warehouse_rows.append(
            {
                "service": getattr(fact, "service_name_snapshot", "") or getattr(getattr(fact, "service", None), "name", "") or "Услуга склада",
                "quantity": _fmt_qty(getattr(fact, "quantity", None)),
                "unit": getattr(fact, "unit", "") or getattr(getattr(fact, "service", None), "unit", "") or "",
                "status": fact.get_status_display() if hasattr(fact, "get_status_display") else "",
            }
        )

    closed_at = (
        data.get("act_storekeeper_signed_at")
        or data.get("flow_closed_at")
        or data.get("received_at")
        or data.get("shipped_at")
        or data.get("packed_at")
        or data.get("processed_at")
        or data.get("completed_at")
        or data.get("closed_at")
        or getattr(application, "operations_completed_at", None)
    )
    return {
        "available": True,
        "labels": labels,
        "goods": _fmt_qty(accepted_goods),
        "planned_goods": _fmt_qty(planned_goods),
        "boxes": _fmt_qty(boxes),
        "pallets": _fmt_qty(pallets),
        "sku_count": len(fact_rows),
        "closed_at": _format_payload_datetime(closed_at),
        "rows": fact_rows[:200],
        "rows_more": max(len(fact_rows) - 200, 0),
        "warehouse_rows": warehouse_rows,
        "has_any_fact": any(value not in {"", "—"} for value in (_fmt_qty(accepted_goods), _fmt_qty(boxes), _fmt_qty(pallets))) or bool(fact_rows or warehouse_rows),
    }


def build_receiving_fact_summary(application, warehouse_facts: list | None = None) -> dict:
    return build_application_fact_summary(application, warehouse_facts)


def tariff_order_map(tariff_version) -> dict[int, tuple]:
    if not tariff_version:
        return {}
    result = {}
    items = (
        tariff_version.items.select_related("category", "service")
        .filter(is_active=True)
        .order_by("category__sort_order", "sort_order", "service_name", "id")
    )
    for item in items:
        result.setdefault(
            item.service_id,
            (
                getattr(item.category, "sort_order", 999999),
                item.sort_order or 999999,
                (item.service_name or item.service.name or "").lower(),
                item.id,
            ),
        )
    return result


def charge_display_sort_key(charge: ApplicationCharge, service_order: dict[int, tuple] | None = None) -> tuple:
    service_order = service_order or {}
    order = service_order.get(charge.service_id)
    if order is None and charge.client_tariff_item_id:
        item = charge.client_tariff_item
        category = item.category
        order = (
            getattr(category, "sort_order", 999999),
            item.sort_order or 999999,
            (item.service_name or charge.service_name_snapshot or charge.service.name or "").lower(),
            item.id,
        )
    if order is None:
        order = (
            999999,
            999999,
            (charge.service_name_snapshot or charge.service.name or "").lower(),
            charge.id,
        )
    return (1 if charge.is_excluded else 0, *order, charge.id)


def charge_tariff_display_label(charge: ApplicationCharge) -> str:
    if charge.client_tariff_version_id:
        version = charge.client_tariff_version
        if version and version.valid_from:
            return f"Тариф клиента · действует с {version.valid_from:%d.%m.%Y}"
        return "Тариф клиента"
    return (charge.tariff_source_label or "").strip() or "Индивидуальный тариф"


def operational_status_display(application) -> str:
    label = (application.operational_status_label or "").strip()
    if label and label.lower() not in OPERATIONAL_STATUS_RU:
        # уже русское
        if not label.isascii() or any(ord(c) > 127 for c in label):
            return label
    raw = (application.operational_status or "").strip().lower()
    if raw in OPERATIONAL_STATUS_RU:
        return OPERATIONAL_STATUS_RU[raw]
    return label or application.get_billing_status_display()


def billing_review_status_label(application) -> str:
    status = application.billing_status
    if status in {BillingStatus.NOT_CALCULATED, BillingStatus.CALCULATION_DRAFT}:
        return "Расчёт формируется"
    if status == BillingStatus.CALCULATED:
        return "Проверка менеджером"
    if status in {BillingStatus.ACT_DRAFT, BillingStatus.ACT_SENT, BillingStatus.ACT_DISPUTED, BillingStatus.ACT_CONFIRMED}:
        return "Документы"
    if status in {
        BillingStatus.INVOICE_REQUIRED,
        BillingStatus.INVOICE_DRAFT,
        BillingStatus.INVOICE_ISSUED,
        BillingStatus.INVOICE_SENT,
    }:
        return "Счёт"
    if status in {BillingStatus.PARTIALLY_PAID, BillingStatus.PAID, BillingStatus.FINANCIALLY_CLOSED}:
        return "Оплата"
    if status == BillingStatus.CANCELLED:
        return "Биллинг отменён"
    return application.get_billing_status_display()


def build_billing_steps(application) -> list[dict]:
    """Горизонтальный индикатор этапов (не кликабельный)."""
    status = application.billing_status
    has_act = application.acts.exclude(status=ActStatus.CANCELLED).exists()
    has_invoice = application.invoices.exclude(status=InvoiceStatus.CANCELLED).exists()
    paid = status in {BillingStatus.PAID, BillingStatus.FINANCIALLY_CLOSED} or (
        application.debt_total is not None
        and application.invoice_total
        and application.debt_total <= 0
        and application.paid_total > 0
    )
    volumes_confirmed = (
        application.charges.filter(is_excluded=False, is_included_in_act=False).exists()
        and not application.charges.filter(
            is_excluded=False, is_confirmed=False, is_included_in_act=False, is_disputed=False
        ).exists()
    )
    calc_done = status not in {BillingStatus.NOT_CALCULATED} or application.charges.exists()

    flags = [
        ("calc", "Расчёт сформирован", calc_done or status != BillingStatus.NOT_CALCULATED),
        (
            "review",
            "Проверка менеджером",
            status
            in {
                BillingStatus.CALCULATED,
                BillingStatus.ACT_DRAFT,
                BillingStatus.ACT_SENT,
                BillingStatus.ACT_CONFIRMED,
                BillingStatus.INVOICE_REQUIRED,
                BillingStatus.INVOICE_DRAFT,
                BillingStatus.INVOICE_ISSUED,
                BillingStatus.INVOICE_SENT,
                BillingStatus.PARTIALLY_PAID,
                BillingStatus.PAID,
                BillingStatus.FINANCIALLY_CLOSED,
            }
            or volumes_confirmed,
        ),
        ("volumes", "Объёмы подтверждены", volumes_confirmed or has_act),
        ("act", "Акт создан", has_act or status in {BillingStatus.ACT_DRAFT, BillingStatus.ACT_SENT, BillingStatus.ACT_CONFIRMED, BillingStatus.INVOICE_REQUIRED}),
        (
            "invoice",
            "Счёт выставлен",
            has_invoice
            or status
            in {
                BillingStatus.INVOICE_ISSUED,
                BillingStatus.INVOICE_SENT,
                BillingStatus.PARTIALLY_PAID,
                BillingStatus.PAID,
                BillingStatus.FINANCIALLY_CLOSED,
            },
        ),
        ("paid", "Оплачено", bool(paid)),
    ]
    # current = first incomplete, else last
    current_idx = len(flags) - 1
    for i, (_k, _l, done) in enumerate(flags):
        if not done:
            current_idx = i
            break
    steps = []
    for i, (key, label, done) in enumerate(flags):
        if done and i < current_idx:
            state = "done"
        elif i == current_idx:
            state = "current" if not (done and i == len(flags) - 1) else "done"
            if done and i == len(flags) - 1:
                state = "done"
            elif not done:
                state = "current"
            else:
                state = "done"
        else:
            state = "todo"
        if done and i <= current_idx and state != "current":
            state = "done"
        if i == current_idx and not (done and all(f[2] for f in flags)):
            state = "current" if not done else "done"
        steps.append({"key": key, "label": label, "state": state, "done": done})
    # simplify: mark all completed before first incomplete as done
    first_open = next((i for i, s in enumerate(steps) if not flags[i][2]), len(steps))
    for i, step in enumerate(steps):
        if i < first_open:
            step["state"] = "done"
        elif i == first_open:
            step["state"] = "current"
        else:
            step["state"] = "todo"
    if first_open == len(steps):
        for step in steps:
            step["state"] = "done"
    return steps


def build_required_actions(application, charge_rows: list[dict]) -> list[dict]:
    actions = []
    no_price = sum(1 for r in charge_rows if r.get("missing_price") and not r["charge"].is_excluded)
    unconfirmed = sum(
        1
        for r in charge_rows
        if not r["charge"].is_excluded
        and not r["charge"].is_confirmed
        and not r.get("missing_price")
        and not r["charge"].is_included_in_act
    )
    needs_recalc = sum(
        1
        for r in charge_rows
        if not r["charge"].is_excluded and r.get("status_code") == "needs_recalc"
    )
    if no_price:
        actions.append(
            {
                "key": "tariff",
                "text": f"Пересчитать {no_price} услуг по актуальному тарифу",
                "cta": "Пересчитать тариф",
                "cta_action": "recalc_missing",
            }
        )
    if unconfirmed:
        actions.append(
            {
                "key": "confirm",
                "text": f"Проверить и подтвердить объёмы по {unconfirmed} услугам",
                "cta": "Проверить объёмы",
                "cta_action": "confirm_all",
            }
        )
    if needs_recalc:
        actions.append(
            {
                "key": "recalc",
                "text": f"Требуется пересчёт по {needs_recalc} строкам",
                "cta": "Пересчитать",
                "cta_action": "recalc_all",
            }
        )
    actionable = [
        row
        for row in charge_rows
        if not row["charge"].is_excluded
        and not row["charge"].is_included_in_act
        and not row["charge"].is_included_in_invoice
        and not row["charge"].is_disputed
    ]
    has_documents = (
        application.acts.exclude(status=ActStatus.CANCELLED).exists()
        or application.invoices.exclude(status=InvoiceStatus.CANCELLED).exists()
    )
    if (
        actionable
        and not no_price
        and not unconfirmed
        and not needs_recalc
        and not has_documents
        and application.billing_status not in {BillingStatus.CANCELLED, BillingStatus.FINANCIALLY_CLOSED}
    ):
        actions.append(
            {
                "key": "documents",
                "text": "Начисления подтверждены. Создайте акт и счёт.",
                "cta": "Создать документы",
                "cta_action": "create_documents",
            }
        )
    return actions


def build_finance_summary(application, charges) -> dict:
    active = [c for c in charges if not c.is_excluded]
    excluded = [c for c in charges if c.is_excluded]
    confirmed = [c for c in active if c.is_confirmed or c.is_included_in_act]
    unconfirmed = [c for c in active if not c.is_confirmed and not c.is_included_in_act]

    def _sum(rows):
        return sum((c.total_amount or Decimal("0") for c in rows), Decimal("0"))

    act_total = application.act_total
    invoice_total = application.invoice_total
    return {
        "charged": _sum(active),
        "confirmed": _sum(confirmed),
        "unconfirmed": _sum(unconfirmed),
        "excluded": _sum(excluded),
        "act_total": act_total,
        "act_label": f"{act_total:,.2f} ₽".replace(",", " ").replace(".", ",") if act_total else "Не создан",
        "invoice_total": invoice_total,
        "invoice_label": f"{invoice_total:,.2f} ₽".replace(",", " ").replace(".", ",") if invoice_total else "Не выставлен",
        "paid_total": application.paid_total,
        "paid_label": (
            f"{application.paid_total:,.2f} ₽".replace(",", " ").replace(".", ",")
            if application.paid_total
            else "Нет оплаты"
        ),
        "debt_total": application.debt_total,
        "charges_count": len(active),
        "unconfirmed_count": len(unconfirmed),
        "excluded_count": len(excluded),
    }


def confirm_blockers(application, charge_rows: list[dict]) -> list[str]:
    blockers = []
    for row in charge_rows:
        ch = row["charge"]
        if ch.is_excluded or ch.is_included_in_act or ch.is_included_in_invoice:
            continue
        if row.get("missing_price"):
            blockers.append("есть строки без тарифа")
        if Decimal(str(ch.quantity or "0")) <= 0:
            blockers.append("количество равно нулю или отрицательное")
        if not ch.service_id:
            blockers.append("не определён вид услуги")
    # unique preserve order
    seen = set()
    out = []
    for b in blockers:
        if b not in seen:
            seen.add(b)
            out.append(b)
    return out


def enrich_charge_row(row: dict) -> dict:
    charge = row["charge"]
    row["source_label"] = charge_source_label(charge)
    row["tariff_source_label"] = charge_tariff_display_label(charge)
    row["storage_period_label"] = storage_charge_period_label(charge, row.get("storage_days") or [])
    row["edit_version"] = int(charge.edit_version or 1)
    row["is_excluded"] = bool(charge.is_excluded)
    row["can_edit"] = BillingWorkflowService.charge_editable_by_manager(charge) and not charge_missing_price(charge)
    row["can_confirm"] = (
        not charge.is_excluded
        and not charge.is_confirmed
        and not charge_missing_price(charge)
        and not charge.is_included_in_act
        and Decimal(str(charge.quantity or "0")) > 0
    )
    return row
