"""Optional Ozon distribution helpers (safe stub when models/migrations absent)."""

from __future__ import annotations

DISTRIBUTION_LABELS = {
    "": "—",
    "empty": "Не заполнено",
    "partial": "Заполнено частично",
    "complete": "Заполнено полностью",
    "mismatch": "Есть расхождения",
}


class _MissingDistribution(RuntimeError):
    pass


def _require_models():
    try:
        from .models import ShippingDestination, ShippingDestinationItem  # noqa: F401
    except Exception as exc:  # pragma: no cover
        raise _MissingDistribution(str(exc)) from exc


def compute_distribution_status(order):
    _require_models()
    raise _MissingDistribution("full distribution module unavailable")


def refresh_order_distribution_status(order, *, save: bool = True) -> str:
    try:
        _require_models()
    except _MissingDistribution:
        return str(getattr(order, "distribution_status", "") or "")
    return str(getattr(order, "distribution_status", "") or "")


def validate_ozon_distribution_for_submit(order) -> None:
    try:
        _require_models()
    except _MissingDistribution:
        return


def destination_summary_rows(order):
    _require_models()
    return []


def distribution_matrix(order):
    _require_models()
    return None
