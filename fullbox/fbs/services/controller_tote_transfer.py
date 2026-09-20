"""Explicit reassignment of delivered totes that have not entered control yet."""
from django.db import transaction
from fbs.exceptions import FbsPickingError
from fbs.models import (FbsControllerSession, FbsControllerPickTote, FbsPickBatch,
    FbsPickingCart, FbsToteBinding, FbsToteMovement, FbsWorkstation, FbsPickRestockRequest)
from .totes import (_require_writes, _actor, _assert_controller_tote_transfer_operator,
    _assert_tote_not_service_reserved, _move_tote)
from .controller_shift import controller_shift_is_live
from .picking import _assert_controller_workstation_capacity


@transaction.atomic
def transfer_waiting_controller_tote(*, binding_id, expected_workstation_id,
                                     target_session_id, performed_by):
    _require_writes()
    actor = _actor(performed_by)
    from .totes import get_employee_for_user, get_employee_roles
    employee = get_employee_for_user(actor)
    if not (getattr(actor, "is_active", False)
            and getattr(employee, "is_active", False)
            and "fbs_controller" in get_employee_roles(employee)):
        _assert_controller_tote_transfer_operator(actor)
    # Use the same batch-first lock order as delivery and verification claim.
    initial = FbsToteBinding.objects.filter(pk=binding_id).first()
    if initial is None or not initial.pick_batch_id:
        raise FbsPickingError("Тара ожидающей волны не найдена.")
    batch = FbsPickBatch.objects.select_for_update().get(pk=initial.pick_batch_id)
    binding = FbsToteBinding.objects.select_for_update().get(pk=binding_id)
    if (binding.state != FbsToteBinding.STATE_WAITING_CONTROL
        or binding.workstation_id != expected_workstation_id
        or binding.pick_batch_id != batch.id
        or binding.controller_session_id is not None):
        raise FbsPickingError("Тару уже приняли или перенесли. Обновите страницу.")
    if (batch.status != FbsPickBatch.STATUS_VERIFICATION
        or batch.cart_id != binding.tote_id or batch.workstation_id != binding.workstation_id
        or batch.completed_at or batch.cart_released_at or batch.verification_started_at
        or batch.verification_assigned_to_id
        or FbsControllerPickTote.objects.filter(pick_batch=batch).exists()):
        raise FbsPickingError("Волна уже принята на проверку или завершена.")
    target = (FbsControllerSession.objects.select_for_update().select_related("controller")
              .filter(pk=target_session_id, status=FbsControllerSession.STATUS_ACTIVE).first())
    if target is None or not target.controller.is_active:
        raise FbsPickingError("На целевом столе нет активного контролера.")
    desks = {w.id: w for w in FbsWorkstation.objects.select_for_update().filter(
        pk__in=[binding.workstation_id, target.workstation_id]).order_by("pk")}
    desk = desks.get(target.workstation_id)
    if (not desk or not desk.is_active or desk.id == binding.workstation_id
        or desk.shift_controller_id != target.controller_id or not controller_shift_is_live(desk)):
        raise FbsPickingError("Выберите другой стол с активной сменой контролера.")
    if FbsPickRestockRequest.objects.filter(batch=batch, status__in=(
        "waiting_marketplace", "queued", "in_progress", "failed")).exists():
        raise FbsPickingError("По таре уже выполняется возврат. Сначала завершите его.")
    _assert_controller_workstation_capacity(workstation=desk, batch_id=batch.id)
    tote = FbsPickingCart.objects.select_for_update().get(pk=binding.tote_id)
    _assert_tote_not_service_reserved(tote)
    source_id = binding.workstation_id
    source = FbsControllerSession.objects.filter(workstation_id=source_id, status="active").first()
    batch.workstation = desk
    batch.save(update_fields=["workstation", "updated_at"])
    return _move_tote(tote=tote, state=FbsToteBinding.STATE_WAITING_CONTROL,
        workstation=desk, performed_by=actor, action=FbsToteMovement.ACTION_HANDOVER,
        pick_batch=batch, quantity=batch.picked_qty,
        details={"operation": "controller_tote_transfer", "waiting_control": True,
            "source_session_id": source.id if source else None,
            "source_workstation_id": source_id, "target_session_id": target.id,
            "target_controller_id": target.controller_id, "target_workstation_id": desk.id})
