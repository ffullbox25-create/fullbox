from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal


@dataclass(frozen=True)
class ChargeCandidate:
    service_code: str
    quantity: Decimal
    operation_type: str
    operation_id: str
    source_key: str
    comment: str = ""
    # У факта склада дата выполнения важнее даты закрытия всей заявки: на неё
    # выбирается редакция тарифа и период начисления.
    performed_at: datetime | None = None


def decimal_qty(value, *, default: str = "0") -> Decimal:
    try:
        return Decimal(str(value if value not in (None, "") else default))
    except Exception:
        return Decimal(default)
