"""Audited correction of excess scans in the currently open TSD box."""
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import Q
from django.http import JsonResponse
from django.views.decorators.http import require_POST

from audit.models import OrderAuditEntry, log_order_action
from receiving_cz.services import accepted_units, delete_unit, normalize_marking_code
from sklad.models import WarehouseStockSnapshot
from .services import ReceivingWorkflowService
from .receiving_tsd import (
    _TsdStateError, _active_box, _active_pallet, _assert_owned, _current_state,
    _deny_unless_storekeeper, _json_body, _locked_context, _persist_state,
    _plan_qty, _state_summary,
)

EVENT = "receiving_tsd_box_corrected"
IDENTITY_FIELDS = ("sku_code", "name", "size", "barcode")


def _integer(value):
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError("Укажите целое неотрицательное количество.")
    text = str(value)
    if not text.isascii() or not text.isdigit() or len(text) > 9:
        raise ValueError("Укажите целое неотрицательное количество.")
    return int(text)


@transaction.atomic
def correct_open_box(*, request, order_id, payload):
    """Validate ownership and warehouse locks before any quantity/mark mutation."""
    context = _locked_context(order_id)
    _assert_owned(context, request)
    if ReceivingWorkflowService._flow_closed(context.entries):
        raise ValueError("Приёмка уже завершена. Исправление короба недоступно.")
    event_id = str(payload.get("event_id") or "").strip()
    box_code = str(payload.get("box_code") or "").strip()
    if not event_id or len(event_id) > 96 or not box_code:
        raise ValueError("Не указан короб или номер исправления. Обновите страницу.")
    state = _current_state(context)
    result = lambda: {"ok": True, "state": state, "summary": _state_summary(
        state, planned_qty=_plan_qty(context.status_payload))}
    previous = OrderAuditEntry.objects.filter(
        order_type="receiving", order_id=context.order_id,
        payload__event=EVENT, payload__event_id=event_id,
    ).first()
    if previous:
        if previous.payload.get("box_code") != box_code:
            raise ValueError("Номер исправления уже использован для другого короба.")
        return {**result(), "duplicate_ignored": True, "message": "Исправление уже сохранено."}
    box = _active_box(state)
    pallet = _active_pallet(state)
    if not box or str(box.get("code")) != box_code:
        raise ValueError("Можно исправить только текущий открытый короб. Обновите страницу.")
    if not pallet or box_code not in (pallet.get("boxes") or []):
        raise ValueError("Палета короба закрыта или изменилась. Обновите страницу.")
    pallet_code = str(pallet.get("code") or "")
    permissions = ReceivingWorkflowService._pallet_placement_permissions_from_entries(context.entries)
    materialized = ReceivingWorkflowService.materialized_pallet_codes(
        order_id=context.order_id, entries=context.entries)
    has_stock = WarehouseStockSnapshot.objects.filter(
        source_context_type="receiving", source_context_id=context.order_id,
    ).filter(Q(container_code__in=[box_code, pallet_code])
             | Q(container__container_code__in=[box_code, pallet_code])
             | Q(parent_container__container_code=pallet_code)).exists()
    if permissions.get(pallet_code.casefold()) or pallet_code.casefold() in materialized or has_stock:
        raise ValueError("Палета уже передана на размещение или оприходована. Состав заблокирован.")

    is_marked = ReceivingWorkflowService.effective_receiving_mode(context.status_payload) == "cz"
    audit = {"event": EVENT, "event_id": event_id, "box_code": box_code,
             "pallet_code": pallet_code, "reason": "Лишнее сканирование", "marked": is_marked}
    if is_marked:
        code = normalize_marking_code(str(payload.get("marking_code") or ""))
        if not code:
            raise ValueError("Для Честного знака отсканируйте конкретный лишний Data Matrix.")
        unit = accepted_units(context.order_id).select_related(None).select_for_update().filter(
            box_code=box_code, marking_code=code).first()
        if unit is None:
            raise ValueError("Этот Data Matrix не найден в текущем коробе.")
        audit["removed_unit"] = {"id": unit.id, "marking_code": code,
                                 "sku_code": unit.sku_code, "barcode": unit.barcode}
        deleted = delete_unit(context=context, unit_id=unit.id)
        if deleted.status != "ok":
            raise ValueError(deleted.error or "Не удалось убрать Data Matrix.")
        state = _current_state(context)
        message = "Лишний Data Matrix удалён из короба. Количество пересчитано."
    else:
        index = _integer(payload.get("row_index"))
        expected = _integer(payload.get("expected_qty"))
        quantity = _integer(payload.get("quantity"))
        items = box.get("items") or []
        identity = payload.get("item")
        item = items[index] if index < len(items) else None
        if (not isinstance(item, dict) or not isinstance(identity, dict)
                or any(str(item.get(k) or "") != str(identity.get(k) or "") for k in IDENTITY_FIELDS)
                or int(item.get("qty") or 0) != expected):
            return {**result(), "ok": False, "status": "stale_box", "error":
                    "Состав короба изменился. Показаны актуальные количества; проверьте и повторите."}
        if quantity >= expected:
            raise ValueError("Введите количество меньше текущего. Для добавления сканируйте товар.")
        audit.update({"item": {k: item.get(k) for k in IDENTITY_FIELDS},
                      "quantity_before": expected, "quantity_after": quantity})
        if quantity:
            item["qty"] = quantity
        else:
            items.pop(index)
        message = f"Короб исправлен: {expected} → {quantity} шт."
    state, version = _persist_state(context=context, state=state, request=request)
    log_order_action("update", order_id=context.order_id, order_type="receiving",
                     agency=context.agency, user=request.user,
                     description=f"Исправлен лишний скан в коробе {box_code}", payload=audit)
    return {**result(), "flow_version": version, "message": message}


@login_required
@require_POST
def receiving_tsd_correct_box(request, order_id):
    _, denied = _deny_unless_storekeeper(request)
    if denied:
        return denied
    payload = _json_body(request)
    if payload is None:
        return JsonResponse({"ok": False, "error": "Некорректный запрос."}, status=400)
    try:
        data = correct_open_box(request=request, order_id=order_id, payload=payload)
        return JsonResponse(data, status=200 if data.get("ok") else 409)
    except _TsdStateError as exc:
        return JsonResponse({"ok": False, "error": exc.message, "status": exc.status}, status=exc.http_status)
    except ValueError as exc:
        return JsonResponse({"ok": False, "error": str(exc)}, status=409)
