"""Shared released-stock rules for warehouse destinations.

Physical zones describe where stock is.  They must not independently decide
whether the same free stock may be sent to shipping, FBS, or processing.
"""

from __future__ import annotations

from django.db.models import Q


DISPATCHABLE_WAREHOUSE_STATE_CODES = (
    "stored",
    "placed_after_processing",
    "in_otg",
    "palletizing",
    "ready_for_loading",
)


def dispatchable_warehouse_state_codes(*, include_receiving: bool = False) -> tuple[str, ...]:
    codes = list(DISPATCHABLE_WAREHOUSE_STATE_CODES)
    if include_receiving:
        codes.insert(0, "placed_in_receiving")
    return tuple(codes)


def dispatchable_warehouse_state_q(
    *,
    state_field: str = "warehouse_state_code",
    snapshot_id_field: str = "pk",
    receiving_allowed_ids=(),
) -> Q:
    query = Q(**{f"{state_field}__in": DISPATCHABLE_WAREHOUSE_STATE_CODES})
    allowed_ids = tuple(receiving_allowed_ids or ())
    if allowed_ids:
        query |= Q(
            **{
                f"{state_field}__iexact": "placed_in_receiving",
                f"{snapshot_id_field}__in": allowed_ids,
            }
        )
    return query


def is_dispatchable_warehouse_state(
    state_code: str | None,
    *,
    snapshot_id: int | None = None,
    receiving_allowed_ids=(),
) -> bool:
    state = str(state_code or "").strip().casefold()
    if state in DISPATCHABLE_WAREHOUSE_STATE_CODES:
        return True
    return (
        state == "placed_in_receiving"
        and snapshot_id is not None
        and int(snapshot_id) in set(receiving_allowed_ids or ())
    )
