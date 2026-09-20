"""Durable receipts for controller requests whose HTTP response may be lost."""
from uuid import UUID

from django.db import transaction
from django.utils import timezone

from fbs.exceptions import FbsHandoverError
from fbs.models import FbsControllerCheckTote, FbsControllerToteOrder
from sklad.models import WarehouseEvent

SCAN_RECEIPT = 'fbs_controller_scan_receipt'


def operation_token(value):
    try:
        return str(UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        raise FbsHandoverError('Не удалось определить операцию сканирования. Обновите экран.')


def _receipt(check_tote_id, token, actor):
    return WarehouseEvent.objects.filter(
        event_type=SCAN_RECEIPT,
        stock_context_type='fbs_check_tote',
        stock_context_id=str(check_tote_id),
        source_document_id=token,
        performed_by=actor,
    ).first()


def read_composition_operation(*, check_tote_id, operation_id, actor):
    token = operation_token(operation_id)
    # Readback never writes or confirms a scan. Do not expose another table's work.
    check = FbsControllerCheckTote.objects.select_related('session').get(pk=check_tote_id)
    if check.session.controller_id != actor.id:
        raise FbsHandoverError('Эта смена открыта другим контролером.')
    receipt = _receipt(check_tote_id, token, actor)
    if receipt is None:
        return None
    row = FbsControllerToteOrder.objects.select_related('label', 'order', 'transport_box').get(
        pk=receipt.payload['tote_order_id'], check_tote_id=check_tote_id,
    )
    row.composition_request_replayed = True
    row.composition_request_duplicate = receipt.payload.get('duplicate', False)
    return row


@transaction.atomic
def submit_composition_operation(*, check_tote_id, label_scan, operation_id, actor):
    from .totes import (
        _assert_check_tote_operator, _check_tote_session_for_update,
        confirm_check_tote_composition_item, COMPOSITION_ALREADY_PACKED_MESSAGE,
    )
    token = operation_token(operation_id)
    _assert_check_tote_operator(actor)
    _check_tote_session_for_update(check_tote_id=check_tote_id, actor=actor)
    check = FbsControllerCheckTote.objects.select_for_update().get(pk=check_tote_id)
    receipt = _receipt(check_tote_id, token, actor)
    if receipt is not None:
        if receipt.payload['label_scan'] != str(label_scan or '').strip():
            raise FbsHandoverError('Операция уже относится к другому скану.')
        return read_composition_operation(
            check_tote_id=check_tote_id, operation_id=token, actor=actor,
        )
    row = confirm_check_tote_composition_item(
        check_tote_id=check_tote_id, label_scan=label_scan, performed_by=actor,
    )
    duplicate = getattr(row, 'composition_scan_message', '') == COMPOSITION_ALREADY_PACKED_MESSAGE
    # Receipt and physical scan share the transaction and the check-tote lock.
    WarehouseEvent.objects.create(
        agency_id=row.order.profile.agency_id,
        event_type=SCAN_RECEIPT,
        stock_context_type='fbs_check_tote', stock_context_id=str(check_tote_id),
        source_document_type='controller_scan', source_document_id=token,
        qty=0, performed_by=actor, performed_by_role='fbs_controller',
        occurred_at=timezone.now(),
        payload={'label_scan':str(label_scan or '').strip(),
                 'tote_order_id':row.id, 'duplicate':duplicate},
    )
    row.composition_request_replayed = False
    row.composition_request_duplicate = duplicate
    return row
