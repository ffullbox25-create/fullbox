from __future__ import annotations

from datetime import timedelta

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from ..exceptions import FbsEquipmentError
from ..models import (
    FbsControllerPickTote,
    FbsControllerSession,
    FbsPickBatch,
    FbsWorkstation,
)


CONTROLLER_SHIFT_HEARTBEAT_TTL = timedelta(seconds=45)


def controller_shift_is_live(
    workstation: FbsWorkstation,
    *,
    now=None,
) -> bool:
    heartbeat_at = workstation.shift_heartbeat_at
    if heartbeat_at is None or workstation.shift_controller_id is None:
        return False
    now = now or timezone.now()
    return heartbeat_at >= now - CONTROLLER_SHIFT_HEARTBEAT_TTL


def _assert_shift_owner(workstation: FbsWorkstation, user) -> None:
    if workstation.shift_controller_id != user.id:
        raise FbsEquipmentError("Смена этого рабочего места открыта другим контролером.")


@transaction.atomic
def start_controller_shift(*, workstation_id: int, controller) -> FbsWorkstation:
    # A controller owns only the operator session, never the physical desk or
    # its work. Lock all desks involved in a switch in a stable order so the
    # takeover is atomic and concurrent logins cannot leave two owners behind.
    locked_workstations = list(
        FbsWorkstation.objects.select_for_update()
        .filter(Q(pk=workstation_id) | Q(shift_controller=controller))
        .order_by("pk")
    )
    workstation = next(
        (item for item in locked_workstations if item.pk == workstation_id),
        None,
    )
    if workstation is None:
        raise FbsEquipmentError("Рабочее место не найдено.")
    if not workstation.is_active:
        raise FbsEquipmentError("Рабочее место выключено.")
    now = timezone.now()
    # Release only the operator binding on the previous desk. Its waves,
    # controller totes, printer, scanner and queued print jobs stay untouched.
    previous_workstations = [
        item for item in locked_workstations if item.pk != workstation.pk
    ]
    if previous_workstations:
        FbsWorkstation.objects.filter(
            pk__in=[item.pk for item in previous_workstations]
        ).update(
            shift_status=FbsWorkstation.SHIFT_CLOSED,
            shift_controller=None,
            shift_heartbeat_at=None,
            updated_by=controller,
            updated_at=now,
        )
    workstation.shift_status = FbsWorkstation.SHIFT_AVAILABLE
    workstation.shift_controller = controller
    workstation.shift_heartbeat_at = now
    workstation.updated_by = controller
    workstation.save(
        update_fields=[
            "shift_status",
            "shift_controller",
            "shift_heartbeat_at",
            "updated_by",
            "updated_at",
        ]
    )
    # The tote session and every unfinished verification already accepted at
    # the desk are one operator context. Transfer them together: otherwise the
    # dashboard belongs to the new controller while the verification page is
    # still authorized only for the previous controller.
    active_sessions = list(
        FbsControllerSession.objects.select_for_update().filter(
            workstation=workstation,
            status=FbsControllerSession.STATUS_ACTIVE,
        )
    )
    active_session_ids = [session.id for session in active_sessions]
    active_pick_contexts = list(
        FbsControllerPickTote.objects.select_for_update().filter(
            session_id__in=active_session_ids,
            status__in=(
                FbsControllerPickTote.STATUS_PROCESSING,
                FbsControllerPickTote.STATUS_AWAITING_EMPTY,
            ),
        )
    )
    active_batch_ids = [context.pick_batch_id for context in active_pick_contexts]
    active_batches = list(
        FbsPickBatch.objects.select_for_update().filter(
            pk__in=active_batch_ids,
            workstation=workstation,
            status=FbsPickBatch.STATUS_VERIFICATION,
            cart_released_at__isnull=True,
        )
    )
    batches_to_transfer = [
        batch.id
        for batch in active_batches
        if batch.verification_assigned_to_id != controller.id
    ]
    if batches_to_transfer:
        FbsPickBatch.objects.filter(pk__in=batches_to_transfer).update(
            verification_assigned_to=controller,
            updated_at=now,
        )
    FbsControllerSession.objects.filter(
        pk__in=active_session_ids,
    ).exclude(controller=controller).update(
        controller=controller,
    )
    return workstation


@transaction.atomic
def pause_controller_shift(*, workstation_id: int, controller) -> FbsWorkstation:
    workstation = FbsWorkstation.objects.select_for_update().get(pk=workstation_id)
    _assert_shift_owner(workstation, controller)
    workstation.shift_status = FbsWorkstation.SHIFT_PAUSED
    workstation.shift_heartbeat_at = timezone.now()
    workstation.updated_by = controller
    workstation.save(
        update_fields=[
            "shift_status",
            "shift_heartbeat_at",
            "updated_by",
            "updated_at",
        ]
    )
    return workstation


@transaction.atomic
def close_controller_shift(*, workstation_id: int, controller) -> FbsWorkstation:
    workstation = FbsWorkstation.objects.select_for_update().get(pk=workstation_id)
    _assert_shift_owner(workstation, controller)
    workstation.shift_status = FbsWorkstation.SHIFT_CLOSED
    workstation.shift_controller = None
    workstation.shift_heartbeat_at = None
    workstation.updated_by = controller
    workstation.save(
        update_fields=[
            "shift_status",
            "shift_controller",
            "shift_heartbeat_at",
            "updated_by",
            "updated_at",
        ]
    )
    return workstation


@transaction.atomic
def heartbeat_controller_shift(*, workstation_id: int, controller) -> FbsWorkstation:
    workstation = FbsWorkstation.objects.select_for_update().get(pk=workstation_id)
    _assert_shift_owner(workstation, controller)
    if workstation.shift_status == FbsWorkstation.SHIFT_CLOSED:
        raise FbsEquipmentError("Смена рабочего места закрыта.")
    workstation.shift_heartbeat_at = timezone.now()
    workstation.save(update_fields=["shift_heartbeat_at", "updated_at"])
    return workstation
