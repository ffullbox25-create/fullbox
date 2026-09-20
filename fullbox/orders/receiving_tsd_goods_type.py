"""Goods-type selection for TSD receiving; accepted containers stay unchanged."""
from django.db import transaction

from audit.models import OrderAuditEntry
from orders.services import ReceivingActionResult, ReceivingStatusUpdateResult, ReceivingWorkflowService as Workflow
from sklad.services.warehouse_commands import WarehouseCommandService


def goods_type_choices(payload):
    if Workflow.normalize_receiving_route(
        (payload or {}).get("receiving_route"),
        default="fbo",
    ) == "fbs":
        return [("gv", Workflow.RECEIVING_GOODS_TYPE_LABELS["gv"])]
    # Shipping returns need a source shipment, selected in the existing desktop flow.
    return [(code, label) for code, label in Workflow.RECEIVING_GOODS_TYPE_LABELS.items()
            if code in {"gv", "no", "op", "br", "vz", "rh"}
            or code == (payload or {}).get("goods_type")]


def _entries(order_id):
    return list(OrderAuditEntry.objects.select_for_update().filter(
        order_id=order_id, order_type="receiving").order_by("created_at", "id"))


def configure_tsd_goods_type(*, order_id, goods_type, role, user):
    """Validate and change only the default for future containers, under the order lock."""
    def reject(message, payload=None):
        return ReceivingStatusUpdateResult(applied=False, payload=payload or {}, description=message)

    if role != "storekeeper" or not order_id:
        return reject("Изменять тип товара может только кладовщик.")
    selected = str(goods_type or "").strip().lower()
    with transaction.atomic():
        WarehouseCommandService.acquire_receiving_order_lock(str(order_id))
        entries = _entries(order_id)
        if not entries:
            return reject("Заявка не найдена.")
        status = Workflow._current_status_entry(entries)
        payload = dict(status.payload or {}) if status else {}
        payload["receiving_route"] = Workflow.receiving_route_from_entries(entries)
        if Workflow._flow_closed(entries):
            return reject("Приемка уже завершена.", payload)
        if (Workflow.receiving_work_access(entries=entries, user=user).status != "allowed"
                or not Workflow._receiving_work_owner(entries)):
            return reject("Сначала возьмите заявку в работу под своим именем.", payload)
        if selected not in dict(goods_type_choices(payload)):
            return reject("Выберите тип товара из списка.", payload)
        previous = str(payload.get("goods_type") or "").strip().lower()
        if selected == previous:
            return ReceivingStatusUpdateResult(applied=True, payload=payload, description="Тип товара без изменений.")
        if previous and previous not in {"gv", "no", "op", "br", "vz", "rh", "na"}:
            return reject("Тип связанного возврата меняется в карточке заявки, вместе с исходной отгрузкой.", payload)
        state = Workflow.find_receiving_flow_state(entries) or {}
        active = str(state.get("activeBox") or "")
        for box in state.get("boxes") or []:
            if (isinstance(box, dict) and str(box.get("code") or "") == active
                    and not box.get("sealed") and Workflow.receiving_flow_has_items({"boxes": [box]})):
                return reject("Сначала закройте текущий короб. Новый тип применяется только к следующим коробам.", payload)
        mode = Workflow.receiving_mode_required_by_client(payload) or Workflow.effective_receiving_mode(payload)
        if mode == "cz" and selected == "no":
            return reject("Необработанный товар несовместим с включенным Честным знаком. Тип не изменен.", payload)
        result = Workflow.configure_receiving_act(
            order_id=order_id, entries=entries, role=role, goods_type=selected,
            receiving_mode=mode, user=user,
        )
        if result.applied and not previous:
            # Older TSD requests issued '-na' while the request type was unset.
            # The existing migration only changes unused active containers.
            Workflow._migrate_receiving_flow_draft_goods_type(
                entries=entries, order_id=order_id,
                previous_goods_type="na", next_goods_type=selected,
            )
        return result


def claim_tsd_receiving(*, order_id, entries, role, user, goods_type):
    """Claim and configure atomically: a failed selection must not assign the order."""
    if role != "storekeeper" or not order_id:
        return ReceivingActionResult(status="forbidden")
    with transaction.atomic():
        WarehouseCommandService.acquire_receiving_order_lock(str(order_id))
        locked = _entries(order_id)
        # Repeated claims and transfers must never silently retype accepted goods.
        if Workflow._receiving_work_owner(locked):
            return Workflow.start_receiving_work(order_id=order_id, entries=locked, role=role, user=user)
        result = Workflow.start_receiving_work(order_id=order_id, entries=locked, role=role, user=user)
        if result.status != "started":
            return result
        configured = configure_tsd_goods_type(
            order_id=order_id, goods_type=goods_type, role=role, user=user,
        )
        if not configured.applied:
            transaction.set_rollback(True)
            return ReceivingActionResult(status="invalid_goods_type", reason=configured.description)
        return ReceivingActionResult(status="started", payload=configured.payload)
