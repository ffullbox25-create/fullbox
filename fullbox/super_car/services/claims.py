from __future__ import annotations

from django.db import IntegrityError, transaction
from django.utils import timezone

from super_car.models import BoxClaim, MoveTask, PalletLock


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


def active_box_claim_codes(*, agency_id: int | None) -> set[str]:
    qs = BoxClaim.objects.filter(status=BoxClaim.STATUS_CLAIMED)
    if agency_id:
        qs = qs.filter(agency_id=agency_id)
    return {
        str(code or "").strip()
        for code in qs.values_list("box_code", flat=True)
        if str(code or "").strip()
    }


def active_pallet_lock_codes(*, agency_id: int | None) -> set[str]:
    qs = PalletLock.objects.filter(status=PalletLock.STATUS_ACTIVE)
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
    authenticated = _authenticated_user(locked_by)
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
    claim_status = BoxClaim.STATUS_DELIVERED if delivered else BoxClaim.STATUS_CANCELLED
    claim_updates = {
        "status": claim_status,
        "released_at": now,
        "updated_at": now,
    }
    if delivered:
        claim_updates["delivered_at"] = now
    BoxClaim.objects.filter(
        move_task_id=task.pk,
        status=BoxClaim.STATUS_CLAIMED,
    ).update(**claim_updates)
    lock_status = PalletLock.STATUS_RELEASED if delivered else PalletLock.STATUS_CANCELLED
    PalletLock.objects.filter(
        move_task_id=task.pk,
        status=PalletLock.STATUS_ACTIVE,
    ).update(
        status=lock_status,
        released_at=now,
        updated_at=now,
    )
