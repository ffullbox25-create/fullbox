"""Scanner-confirmed actions for the controller's shipment problem window."""
from django.db import transaction
from fbs.exceptions import FbsPickingError
from fbs.models import (FbsHandoverBatch, FbsHandoverOrderAssignment, FbsOrder,
                        FbsOrderLabel, FbsPickRestockRequest)
from .totes import _assert_check_tote_operator, _require_writes
from .pick_restock import (ensure_cancelled_order_pick_restock,
                          order_is_client_canceled_by_marketplace,
                          release_order_pick_restock_to_queue)


@transaction.atomic
def confirm_problem_order_to_tote(*, batch_id, order_id, order_scan, tote_scan, actor):
    _require_writes()
    _assert_check_tote_operator(actor)
    batch = FbsHandoverBatch.objects.select_for_update().get(pk=batch_id)
    if batch.status not in {FbsHandoverBatch.STATUS_OPEN, FbsHandoverBatch.STATUS_READY}:
        raise FbsPickingError('Эта отгрузка уже передана; убрать из неё товар нельзя.')
    assignment = FbsHandoverOrderAssignment.objects.select_for_update().filter(
        batch=batch, order_id=order_id).first()
    if assignment is None:
        raise FbsPickingError('Заказ не относится к этой отгрузке.')
    order = FbsOrder.objects.select_for_update().select_related('profile').get(pk=order_id)
    label = FbsOrderLabel.objects.filter(order=order).order_by('-requested_at', '-id').first()
    if label is None or not label.barcode or str(order_scan or '').strip() != label.barcode.strip():
        raise FbsPickingError('Отсканируйте этикетку выбранного заказа. Это другой код.')
    restock = FbsPickRestockRequest.objects.select_for_update().filter(
        order=order, handover_assignment=assignment,
    ).exclude(status=FbsPickRestockRequest.STATUS_CANCELED).order_by('-id').first()
    if restock is None:
        if not order_is_client_canceled_by_marketplace(order):
            raise FbsPickingError('Заказ не отменён и не назначен к возврату. Убирать его нельзя.')
        restock = ensure_cancelled_order_pick_restock(order_id=order.id)
    if restock is None:
        raise FbsPickingError('Возврат пока не подготовлен. Обратитесь к руководителю смены.')
    return release_order_pick_restock_to_queue(
        request_id=restock.id, order_scan=order_scan, canceled_tote_scan=tote_scan,
        comment=restock.reason, confirm_physical=True, handover_batch_id=batch.id,
        performed_by=actor,
    )


@transaction.atomic
def confirm_problem_order_to_tote_without_label(
    *, batch_id, order_id, product_scans, tote_scan, actor
):
    """Identify a canceled order by every physical unit when its label is absent."""
    _require_writes()
    _assert_check_tote_operator(actor)
    batch = FbsHandoverBatch.objects.select_for_update().get(pk=batch_id)
    if batch.status not in {FbsHandoverBatch.STATUS_OPEN, FbsHandoverBatch.STATUS_READY}:
        raise FbsPickingError('Эта отгрузка уже передана; убрать из неё товар нельзя.')
    assignment = FbsHandoverOrderAssignment.objects.select_for_update().filter(
        batch=batch, order_id=order_id).first()
    if assignment is None:
        raise FbsPickingError('Заказ не относится к этой отгрузке.')
    order = FbsOrder.objects.select_for_update().select_related('profile').get(pk=order_id)
    if not order_is_client_canceled_by_marketplace(order):
        raise FbsPickingError(
            'Ручная проверка товара доступна только для заказа, отменённого маркетплейсом.'
        )
    restock = FbsPickRestockRequest.objects.select_for_update().filter(
        order=order, handover_assignment=assignment,
    ).exclude(status=FbsPickRestockRequest.STATUS_CANCELED).order_by('-id').first()
    if restock is None:
        restock = ensure_cancelled_order_pick_restock(order_id=order.id)
    if restock is None:
        raise FbsPickingError('Возврат пока не подготовлен. Обратитесь к руководителю смены.')
    return release_order_pick_restock_to_queue(
        request_id=restock.id,
        product_scans=product_scans,
        canceled_tote_scan=tote_scan,
        comment=restock.reason,
        confirm_physical=True,
        handover_batch_id=batch.id,
        performed_by=actor,
    )
