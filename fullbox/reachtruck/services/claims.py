from __future__ import annotations

from datetime import datetime

from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from reachtruck.models import BoxClaim, MoveTask, PalletLock
from sklad.models import WarehouseStockSnapshot


_FINAL_TASK_STATUSES = {
    MoveTask.STATUS_DONE,
    MoveTask.STATUS_CANCELED,
    MoveTask.STATUS_FAILED,
}

_STORAGE_ZONE_CODES = {"OS", "PR"}


def _normalize_code(value) -> str:
    return str(value or "").strip()


def _ordered_codes(values) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for raw_value in values or []:
        code = _normalize_code(raw_value)
        key = code.lower()
        if not code or key in seen:
            continue
        seen.add(key)
        result.append(code)
    return result


def _authenticated_user(user):
    return user if getattr(user, "is_authenticated", False) else None


def _confirmed_delivery_box_keys(payload: dict | None) -> tuple[set[str], bool]:
    """Return final moved box codes and whether the task recorded final facts.

    ``mobile_execution.boxes_scanned`` is deliberately not a final fact: a driver
    can scan an unsuitable box before completing the task with different boxes.
    """
    task_payload = payload if isinstance(payload, dict) else {}
    result: set[str] = set()
    has_final_facts = False

    for field_name in (
        "picked_boxes",
        "moved_boxes",
        "actual_box_codes",
        "confirmed_box_codes",
    ):
        if field_name not in task_payload:
            continue
        has_final_facts = True
        raw_codes = task_payload.get(field_name) or []
        if isinstance(raw_codes, str):
            raw_codes = [raw_codes]
        for raw_code in raw_codes:
            code = _normalize_code(raw_code)
            if code:
                result.add(code.lower())

    if "picked_rows" in task_payload:
        has_final_facts = True
        for row in task_payload.get("picked_rows") or []:
            if not isinstance(row, dict):
                continue
            code = _normalize_code(row.get("box_code"))
            if code:
                result.add(code.lower())

    arrival = task_payload.get("shipping_arrival")
    if isinstance(arrival, dict) and "box_codes" in arrival:
        has_final_facts = True
        raw_codes = arrival.get("box_codes") or []
        if isinstance(raw_codes, str):
            raw_codes = [raw_codes]
        for raw_code in raw_codes:
            code = _normalize_code(raw_code)
            if code:
                result.add(code.lower())

    return result, has_final_facts


def active_box_claim_codes(*, agency_id: int | None) -> set[str]:
    qs = BoxClaim.objects.filter(status=BoxClaim.STATUS_CLAIMED).exclude(
        move_task__status__in=_FINAL_TASK_STATUSES,
    )
    if agency_id:
        qs = qs.filter(agency_id=agency_id)
    return {
        str(code or "").strip()
        for code in qs.values_list("box_code", flat=True)
        if str(code or "").strip()
    }


def unavailable_box_claim_codes(*, agency_id: int | None) -> set[str]:
    unavailable = active_box_claim_codes(agency_id=agency_id)
    delivered_qs = BoxClaim.objects.filter(
        status=BoxClaim.STATUS_DELIVERED,
        claim_kind=BoxClaim.KIND_BOX,
    )
    if agency_id:
        delivered_qs = delivered_qs.filter(agency_id=agency_id)

    delivered_rows = list(delivered_qs.values_list(
        "box_code",
        "delivered_at",
        "released_at",
        "updated_at",
        "move_task_id",
    ))
    task_facts_by_id = {
        int(task_id): {
            "payload": payload,
            "status": status,
            "to_zone": str(to_zone or "").strip().upper(),
        }
        for task_id, payload, status, to_zone in MoveTask.objects.filter(
            pk__in={int(row[4]) for row in delivered_rows if row[4]}
        ).values_list("id", "payload", "status", "to_zone")
    }

    delivered_by_key: dict[str, tuple[str, datetime]] = {}
    for code, delivered_at, released_at, updated_at, move_task_id in delivered_rows:
        normalized = _normalize_code(code)
        occurred_at = delivered_at or released_at or updated_at
        key = normalized.lower()
        if not key or occurred_at is None:
            continue
        task_facts = task_facts_by_id.get(int(move_task_id or 0), {})
        if (
            task_facts.get("status") == MoveTask.STATUS_DONE
            and task_facts.get("to_zone") in _STORAGE_ZONE_CODES
        ):
            # A completed move into OS/PR returns the box to warehouse storage.
            # Its delivered claim records completion of that move, not delivery
            # into processing, and must not block a later warehouse task.
            continue
        confirmed_keys, has_final_facts = _confirmed_delivery_box_keys(task_facts.get("payload"))
        if has_final_facts and key not in confirmed_keys:
            # The box was scanned during the route but the final warehouse move
            # contains different boxes. Such a historical claim must not block
            # a box that is still physically stored in OS/PR.
            continue
        current = delivered_by_key.get(key)
        if current is None or occurred_at > current[1]:
            delivered_by_key[key] = (normalized, occurred_at)

    if not delivered_by_key:
        return unavailable

    delivered_codes = [value[0] for value in delivered_by_key.values()]
    snapshot_qs = WarehouseStockSnapshot.objects.select_related("container").filter(
        is_archived=False,
        available_qty__gt=0,
        zone_code__in=_STORAGE_ZONE_CODES,
    )
    if agency_id:
        snapshot_qs = snapshot_qs.filter(agency_id=agency_id)
    snapshot_qs = snapshot_qs.filter(
        Q(container_code__in=delivered_codes)
        | Q(container__container_code__in=delivered_codes)
    )
    for snapshot in snapshot_qs:
        code = _normalize_code(
            getattr(snapshot.container, "container_code", "")
            or snapshot.container_code
        )
        delivered = delivered_by_key.get(code.lower())
        if delivered and snapshot.updated_at <= delivered[1]:
            unavailable.add(delivered[0])
    return unavailable


def active_pallet_lock_codes(*, agency_id: int | None) -> set[str]:
    qs = PalletLock.objects.filter(status=PalletLock.STATUS_ACTIVE).exclude(
        move_task__status__in=_FINAL_TASK_STATUSES,
    )
    if agency_id:
        qs = qs.filter(agency_id=agency_id)
    return {
        str(code or "").strip()
        for code in qs.values_list("pallet_code", flat=True)
        if str(code or "").strip()
    }


def lock_pallet_for_task(locked_task: MoveTask, *, locked_by=None, payload: dict | None = None) -> PalletLock | None:
    agency = getattr(locked_task.request, "agency", None)
    pallet_code = _normalize_code(locked_task.pallet_code)
    if agency is None or not pallet_code:
        return None
    now = timezone.now()
    PalletLock.objects.select_for_update().filter(
        agency=agency,
        pallet_code=pallet_code,
        status=PalletLock.STATUS_ACTIVE,
        move_task__status__in=_FINAL_TASK_STATUSES,
    ).update(
        status=PalletLock.STATUS_RELEASED,
        released_at=now,
        updated_at=now,
    )
    authenticated = _authenticated_user(locked_by)
    conflict = (
        PalletLock.objects.select_for_update()
        .filter(
            agency=agency,
            pallet_code=pallet_code,
            status=PalletLock.STATUS_ACTIVE,
        )
        .exclude(move_task=locked_task)
        .order_by("id")
        .first()
    )
    if conflict:
        conflict_task = conflict.move_task
        same_request = (
            getattr(conflict_task, "request_id", None)
            and getattr(locked_task, "request_id", None)
            and conflict_task.request_id == locked_task.request_id
        )
        same_actor = bool(authenticated and conflict.locked_by_id == authenticated.id)
        if same_request and same_actor:
            return conflict
        raise ValueError(f"Паллета {pallet_code} уже взята в работу по другому заданию.")
    lock = (
        PalletLock.objects.select_for_update()
        .filter(
            agency=agency,
            move_task=locked_task,
            pallet_code=pallet_code,
            status=PalletLock.STATUS_ACTIVE,
        )
        .order_by("id")
        .first()
    )
    lock_payload = dict(payload or {})
    if lock is None:
        try:
            lock = PalletLock.objects.create(
                agency=agency,
                move_task=locked_task,
                pallet_code=pallet_code,
                payload=lock_payload,
                locked_by=authenticated,
            )
        except IntegrityError as exc:
            raise ValueError(f"Паллета {pallet_code} уже взята в работу по другому заданию.") from exc
    else:
        update_fields = ["updated_at"]
        if lock_payload and lock.payload != lock_payload:
            lock.payload = lock_payload
            update_fields.append("payload")
        if authenticated and lock.locked_by_id != authenticated.id:
            lock.locked_by = authenticated
            update_fields.append("locked_by")
        if len(update_fields) > 1:
            lock.save(update_fields=update_fields)
    return lock


@transaction.atomic
def claim_boxes_for_task(
    task: MoveTask,
    box_codes,
    *,
    claimed_by=None,
    claim_kind: str = BoxClaim.KIND_BOX,
    payload: dict | None = None,
    lock_pallet: bool = True,
) -> list[BoxClaim]:
    codes = _ordered_codes(box_codes)
    if not codes:
        return []
    if not task.pk:
        raise ValueError("Задание не найдено.")
    locked_task = MoveTask.objects.select_for_update().get(pk=task.pk)
    if locked_task.status in {
        MoveTask.STATUS_DONE,
        MoveTask.STATUS_CANCELED,
        MoveTask.STATUS_FAILED,
    }:
        raise ValueError("Задание уже закрыто.")
    agency = getattr(locked_task.request, "agency", None)
    if agency is None:
        raise ValueError("Не найдена организация задания.")

    own_claim_keys = {
        str(code or "").strip().lower()
        for code in BoxClaim.objects.filter(
            agency=agency,
            move_task=locked_task,
            status=BoxClaim.STATUS_CLAIMED,
        ).values_list("box_code", flat=True)
        if str(code or "").strip()
    }
    unavailable_keys = {
        str(code or "").strip().lower()
        for code in unavailable_box_claim_codes(agency_id=agency.id)
        if str(code or "").strip()
    }
    unavailable_code = next(
        (
            code
            for code in codes
            if code.lower() in unavailable_keys and code.lower() not in own_claim_keys
        ),
        "",
    )
    if unavailable_code:
        raise ValueError(
            f"Короб {unavailable_code} уже занят другим заданием или доставлен в обработку."
        )

    if lock_pallet:
        lock_pallet_for_task(locked_task, locked_by=claimed_by, payload=payload)

    conflict = (
        BoxClaim.objects.select_for_update()
        .filter(
            agency=agency,
            status=BoxClaim.STATUS_CLAIMED,
            box_code__in=codes,
        )
        .exclude(move_task=locked_task)
        .order_by("id")
        .first()
    )
    if conflict:
        raise ValueError(f"Короб {conflict.box_code} уже взят в работу по другому заданию.")

    claim_payload = dict(payload or {})
    authenticated = _authenticated_user(claimed_by)
    existing = {
        claim.box_code.lower(): claim
        for claim in BoxClaim.objects.select_for_update().filter(
            agency=agency,
            move_task=locked_task,
            status=BoxClaim.STATUS_CLAIMED,
            box_code__in=codes,
        )
    }
    result: list[BoxClaim] = []
    for code in codes:
        claim = existing.get(code.lower())
        if claim is None:
            try:
                claim = BoxClaim.objects.create(
                    agency=agency,
                    move_task=locked_task,
                    box_code=code,
                    pallet_code=_normalize_code(locked_task.pallet_code),
                    claim_kind=claim_kind if claim_kind in {BoxClaim.KIND_BOX, BoxClaim.KIND_PARTIAL} else BoxClaim.KIND_BOX,
                    shipping_order_id=str((locked_task.payload or {}).get("shipping_order_id") or "").strip(),
                    shipping_order_pk=(locked_task.payload or {}).get("shipping_order_pk") or None,
                    payload=claim_payload,
                    claimed_by=authenticated,
                )
            except IntegrityError as exc:
                raise ValueError(f"Короб {code} уже взят в работу по другому заданию.") from exc
        else:
            update_fields = ["updated_at"]
            if claim_payload and claim.payload != claim_payload:
                claim.payload = claim_payload
                update_fields.append("payload")
            if authenticated and claim.claimed_by_id != authenticated.id:
                claim.claimed_by = authenticated
                update_fields.append("claimed_by")
            if claim.claim_kind != claim_kind and claim_kind in {BoxClaim.KIND_BOX, BoxClaim.KIND_PARTIAL}:
                claim.claim_kind = claim_kind
                update_fields.append("claim_kind")
            if len(update_fields) > 1:
                claim.save(update_fields=update_fields)
        result.append(claim)
    return result


@transaction.atomic
def release_claims_for_task(task: MoveTask, *, delivered: bool = False) -> None:
    if not task.pk:
        return
    now = timezone.now()
    claims = list(BoxClaim.objects.select_for_update().filter(
        move_task_id=task.pk,
        status=BoxClaim.STATUS_CLAIMED,
    ))
    if delivered:
        confirmed_keys, has_final_facts = _confirmed_delivery_box_keys(task.payload)
        if has_final_facts:
            delivered_ids = [claim.pk for claim in claims if claim.box_code.lower() in confirmed_keys]
            cancelled_ids = [claim.pk for claim in claims if claim.box_code.lower() not in confirmed_keys]
        else:
            delivered_ids = [claim.pk for claim in claims]
            cancelled_ids = []
        if delivered_ids:
            BoxClaim.objects.filter(pk__in=delivered_ids).update(
                status=BoxClaim.STATUS_DELIVERED,
                delivered_at=now,
                released_at=now,
                updated_at=now,
            )
        if cancelled_ids:
            BoxClaim.objects.filter(pk__in=cancelled_ids).update(
                status=BoxClaim.STATUS_CANCELLED,
                released_at=now,
                updated_at=now,
            )
    elif claims:
        BoxClaim.objects.filter(pk__in=[claim.pk for claim in claims]).update(
            status=BoxClaim.STATUS_CANCELLED,
            released_at=now,
            updated_at=now,
        )
    lock_status = PalletLock.STATUS_RELEASED if delivered else PalletLock.STATUS_CANCELLED
    PalletLock.objects.filter(
        move_task_id=task.pk,
        status=PalletLock.STATUS_ACTIVE,
    ).update(
        status=lock_status,
        released_at=now,
        updated_at=now,
    )
