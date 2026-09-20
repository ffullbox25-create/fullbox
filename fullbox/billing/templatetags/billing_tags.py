from django import template

register = template.Library()

_STATUS_BADGE = {
    "not_calculated":    "gray",
    "calculation_draft": "blue",
    "calculated":        "blue",
    "act_draft":         "blue",
    "act_sent":          "orange",
    "act_disputed":      "red",
    "act_confirmed":     "orange",
    "invoice_required":  "orange",
    "invoice_draft":     "blue",
    "invoice_issued":    "blue",
    "invoice_sent":      "blue",
    "partially_paid":    "orange",
    "paid":              "green",
    "financially_closed": "green",
    "cancelled":         "gray",
    # act statuses
    "draft":     "blue",
    "sent":      "orange",
    "disputed":  "red",
    "confirmed": "green",
    # invoice statuses (same keys as billing, handled above)
    "required":  "orange",
    "issued":    "blue",
    # application types
    "receiving":  "blue",
    "processing": "orange",
    "packing":    "orange",
    "shipping":   "green",
    "storage":    "gray",
    "other":      "gray",
}

_APP_TYPE_ABBR = {
    "receiving":  "PR",
    "processing": "OBR",
    "packing":    "OBR",
    "shipping":   "OTG",
    "storage":    "HRN",
    "other":      "DR",
}


@register.filter
def billing_badge(status: str) -> str:
    return _STATUS_BADGE.get(str(status or ""), "gray")


@register.filter
def app_type_abbr(app_type: str) -> str:
    return _APP_TYPE_ABBR.get(str(app_type or ""), "")


@register.filter
def money_fmt(value) -> str:
    try:
        v = float(value or 0)
        if v == 0:
            return "—"
        if v >= 1_000_000:
            return f"{v/1_000_000:.1f} млн ₽"
        if v >= 1_000:
            return f"{v/1_000:.1f} тыс ₽"
        return f"{v:,.2f} ₽".replace(",", " ")
    except Exception:
        return str(value)


@register.filter
def is_overdue(invoice) -> bool:
    """Возвращает True если счёт просрочен (есть долг и дата оплаты прошла)."""
    try:
        from django.utils import timezone
        return bool(invoice.debt_amount) and invoice.due_date < timezone.localdate()
    except Exception:
        return False


@register.filter
def sum_field(queryset, field_name: str):
    """Возвращает сумму поля по queryset (или list)."""
    try:
        total = sum(getattr(item, field_name, 0) or 0 for item in queryset)
        return total
    except Exception:
        return 0


@register.filter
def rub_fmt(value) -> str:
    try:
        from decimal import Decimal, ROUND_HALF_UP

        amount = Decimal(value or "0").quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        raw = f"{amount:,.2f}".replace(",", " ").replace(".", ",")
        return f"{raw} ₽"
    except Exception:
        return str(value)
