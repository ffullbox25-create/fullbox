from __future__ import annotations

from dataclasses import dataclass, field
import logging
from typing import Callable

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from .models import ShippingOrder, ShippingOrderItem
from .discrepancy import (
    apply_discrepancy_substitution,
    approve_shipping_discrepancy,
    confirm_shipping_discrepancy_pick,
    mark_discrepancy_pick_created,
    reject_shipping_discrepancy,
    request_shipping_discrepancy,
)
from .services import (
    complete_transfer_without_trip,
    refresh_strict_ozon_gm_intake,
    release_order_reserves,
    reserve_order,
    return_order_to_draft,
)
from .workflow import (
    accept_order_by_storekeeper,
    approve_order_by_manager,
    cancel_order,
    reject_warehouse_cancel_confirmation,
    reopen_order_for_rework,
    request_warehouse_cancel_confirmation,
    set_shipping_order_manual_priority,
    start_storekeeper_pick,
    submit_order_for_approval,
)


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ShippingDetailActionPermissions:
    can_edit_items: bool
    can_submit_for_approval: bool
    can_manager_approve: bool
    can_manager_reopen: bool
    can_storekeeper_accept: bool
    can_storekeeper_pick: bool
    can_set_shipping_priority: bool = False
    can_request_shipping_discrepancy: bool = False
    can_approve_shipping_discrepancy: bool = False
    can_add_discrepancy_item: bool = False
    can_confirm_shipping_discrepancy_pick: bool = False
    can_create_otg_supplemental_pick: bool = False
    can_complete_transfer: bool = False
    can_cancel: bool = False
    can_request_warehouse_cancel: bool = False
    can_review_warehouse_cancel: bool = False


@dataclass
class ShippingDetailActionResult:
    redirect_name: str = "shipping:detail"
    redirect_url: str = ""
    messages: list[tuple[str, str]] = field(default_factory=list)

    def add(self, level: str, text: str) -> None:
        self.messages.append((level, text))


def handle_shipping_detail_action(
    *,
    action: str,
    order: ShippingOrder,
    request,
    role: str | None,
    stock_rows_for_order: list[dict],
    permissions: ShippingDetailActionPermissions,
    parse_selected_stock_items: Callable,
    selected_stock_rows_with_boxes: Callable,
    parse_box_count_from_comment: Callable[[str | None], int],
    log_update: Callable,
) -> ShippingDetailActionResult:
    result = ShippingDetailActionResult()
    try:
        if action == "add_item":
            _handle_add_item(
                result=result,
                order=order,
                request=request,
                stock_rows_for_order=stock_rows_for_order,
                permissions=permissions,
                parse_selected_stock_items=parse_selected_stock_items,
                selected_stock_rows_with_boxes=selected_stock_rows_with_boxes,
                log_update=log_update,
            )
        elif action == "remove_item":
            _handle_remove_item(
                result=result,
                order=order,
                request=request,
                permissions=permissions,
                parse_box_count_from_comment=parse_box_count_from_comment,
                log_update=log_update,
            )
        elif action == "submit":
            if not permissions.can_submit_for_approval:
                result.add("error", "Отправка на согласование недоступна в текущем статусе.")
            elif order.status == ShippingOrder.STATUS_DRAFT:
                with transaction.atomic():
                    submit_order_for_approval(order, request.user)
                result.add("success", "Заявка отправлена менеджеру на согласование.")
        elif action == "reserve":
            if not permissions.can_manager_approve:
                result.add("error", "Согласование доступно только менеджеру.")
            else:
                approve_order_by_manager(order, request.user)
                result.add("success", "Заявка согласована менеджером и передана кладовщику.")
        elif action == "release_reserve":
            if not permissions.can_manager_reopen:
                result.add(
                    "error",
                    "Вернуть заявку на доработку может только менеджер после согласования.",
                )
            else:
                reopen_order_for_rework(order, request.user)
                result.add("success", "Заявка возвращена на доработку.")
        elif action in {"set_shipping_priority", "clear_shipping_priority"}:
            if not permissions.can_set_shipping_priority:
                result.add(
                    "error",
                    "Менять приоритет может только начальник склада или кладовщик до запуска отбора.",
                )
            else:
                urgent = action == "set_shipping_priority"
                set_shipping_order_manual_priority(
                    order,
                    request.user,
                    role=role,
                    urgent=urgent,
                )
                if urgent and order.status == ShippingOrder.STATUS_STOREKEEPER_ACCEPTED:
                    try:
                        move_ids = start_storekeeper_pick(
                            order,
                            request.user,
                            requested_by_name=request.user.get_full_name() or request.user.username,
                            requested_by_role=role or "",
                        )
                    except Exception as exc:
                        logger.exception(
                            "Failed to dispatch prioritized shipping pick",
                            extra={
                                "shipping_order_pk": order.pk,
                                "shipping_order_number": order.number,
                                "action": action,
                            },
                        )
                        result.add(
                            "warning",
                            "Срочный приоритет установлен, но задание ричтраку пока не создано: "
                            f"{exc}",
                        )
                    else:
                        if move_ids:
                            result.add(
                                "success",
                                "Срочный приоритет установлен. Задания направлены на ТСД: "
                                f"{', '.join(move_ids)}.",
                            )
                        else:
                            order.refresh_from_db(fields=["status"])
                            if order.status == ShippingOrder.STATUS_STOREKEEPER_ACCEPTED:
                                result.add(
                                    "warning",
                                    "Срочный приоритет установлен. OTG-отбор ожидает освобождения "
                                    "товара, паллеты или резерва; система повторит планирование "
                                    "автоматически.",
                                )
                            else:
                                result.add(
                                    "success",
                                    "Срочный приоритет установлен. Товар уже готов в OTG.",
                                )
                else:
                    result.add(
                        "success",
                        "Заявка поставлена первой в очередь OTG."
                        if urgent
                        else "Ручной приоритет заявки снят.",
                    )
        elif action == "accept_storekeeper":
            if not permissions.can_storekeeper_accept:
                result.add(
                    "error",
                    "Принять заявку в работу может только кладовщик после согласования менеджером.",
                )
            else:
                try:
                    refresh_strict_ozon_gm_intake(order, request.user)
                    with transaction.atomic():
                        accept_order_by_storekeeper(order, request.user)
                        move_ids = start_storekeeper_pick(
                            order,
                            request.user,
                            requested_by_name=request.user.get_full_name() or request.user.username,
                            requested_by_role=role or "",
                        )
                except Exception as exc:
                    logger.exception(
                        "Failed to auto-create shipping pick tasks",
                        extra={
                            "shipping_order_pk": order.pk,
                            "shipping_order_number": order.number,
                            "action": action,
                        },
                    )
                    result.add(
                        "error",
                        f"Заявка не принята: не удалось создать задания ричтраку для {order.number}: {exc}",
                    )
                    return result
                if move_ids:
                    result.add("success", f"Заявка принята в работу, задания ричтраку созданы: {', '.join(move_ids)}.")
                else:
                    order.refresh_from_db(fields=["status"])
                    if order.status == ShippingOrder.STATUS_STOREKEEPER_ACCEPTED:
                        result.add(
                            "warning",
                            "Заявка принята. OTG-отбор ожидает освобождения паллет или резерва; "
                            "система повторит планирование автоматически.",
                        )
                    else:
                        result.add("success", "Заявка принята в работу, товар уже готов в OTG.")
        elif action == "create_pick_tasks":
            if not permissions.can_storekeeper_pick:
                result.add(
                    "error",
                    "Создать задания ричтраку может только кладовщик после принятия заявки в работу.",
                )
            else:
                try:
                    refresh_strict_ozon_gm_intake(order, request.user)
                    move_ids = start_storekeeper_pick(
                        order,
                        request.user,
                        requested_by_name=request.user.get_full_name() or request.user.username,
                        requested_by_role=role or "",
                    )
                except Exception as exc:
                    logger.exception(
                        "Failed to create shipping pick tasks",
                        extra={
                            "shipping_order_pk": order.pk,
                            "shipping_order_number": order.number,
                            "action": action,
                        },
                    )
                    result.add(
                        "error",
                        f"Не удалось создать задания ричтраку для заявки {order.number}: {exc}",
                    )
                    return result
                if move_ids:
                    result.add("success", f"Кладовщик передал задания ричтраку: {', '.join(move_ids)}.")
                else:
                    order.refresh_from_db(fields=["status"])
                    if order.status == ShippingOrder.STATUS_STOREKEEPER_ACCEPTED:
                        result.add(
                            "warning",
                            "OTG-отбор ожидает освобождения паллет или резерва; "
                            "система повторит планирование автоматически.",
                        )
                    else:
                        result.add("warning", "Нет позиций для создания заданий ричтраку.")
        elif action == "replace_reachtruck_boxes":
            if not permissions.can_storekeeper_pick:
                result.add("error", "Изменить задание ричтраку может только кладовщик во время отбора.")
            else:
                from otg_reachtruck.services import replace_otg_reachtruck_assignment_boxes

                try:
                    replacement = replace_otg_reachtruck_assignment_boxes(
                        order=order,
                        otg_request_id=int(request.POST.get("otg_request_id") or 0),
                        old_box_codes=request.POST.getlist("old_box_code"),
                        new_box_codes=request.POST.getlist("replacement_box_code"),
                        user=request.user,
                        requested_by_name=request.user.get_full_name() or request.user.username,
                        requested_by_role=role or "",
                    )
                except (TypeError, ValueError, ValidationError) as exc:
                    result.add("error", str(exc))
                    return result
                changes = replacement.get("changes") or []
                change_text = ", ".join(
                    f"{row.get('old')} → {row.get('new')}"
                    for row in changes
                )
                move_ids = replacement.get("move_ids") or []
                log_update(
                    order,
                    request,
                    f"Изменено задание ричтраку: {change_text}",
                    extra={
                        "reachtruck_box_replacements": changes,
                        "reachtruck_move_ids": move_ids,
                    },
                )
                result.add(
                    "success",
                    f"Задание ричтраку перестроено: {change_text}. Новые задания: {', '.join(move_ids)}.",
                )
        elif action == "request_shipping_discrepancy":
            if not permissions.can_request_shipping_discrepancy:
                result.add("error", "Запросить отгрузку с расхождением может только кладовщик.")
            else:
                request_shipping_discrepancy(
                    order,
                    user=request.user,
                    storekeeper_comment=request.POST.get("storekeeper_comment", ""),
                )
                result.add("success", "Отгрузка с расхождением отправлена на согласование.")
        elif action == "approve_shipping_discrepancy":
            if not permissions.can_approve_shipping_discrepancy:
                result.add("error", "Согласование расхождения недоступно.")
            else:
                approve_shipping_discrepancy(order, user=request.user)
                result.add("success", "Отгрузка с расхождением согласована.")
        elif action == "reject_shipping_discrepancy":
            if not permissions.can_approve_shipping_discrepancy:
                result.add(
                    "error",
                    "Решение по расхождению доступно менеджеру или начальнику склада.",
                )
            else:
                payload = reject_shipping_discrepancy(
                    order,
                    user=request.user,
                    correction_mode=request.POST.get("correction_mode") or "",
                )
                mode_label = (
                    "целыми коробами"
                    if payload.get("correction_mode") == "whole_box"
                    else "поштучно"
                )
                result.add(
                    "success",
                    f"Назначен добор {mode_label}. Ожидается подтверждение кладовщика.",
                )
        elif action == "confirm_shipping_discrepancy_pick":
            if not permissions.can_confirm_shipping_discrepancy_pick:
                result.add("error", "Подтверждение добора кладовщиком недоступно.")
            else:
                payload = confirm_shipping_discrepancy_pick(
                    order,
                    user=request.user,
                    requested_by_role=role or "",
                )
                if payload.get("plan_refreshed"):
                    result.add(
                        "warning",
                        "План добора обновлён по текущему складу. "
                        "Проверьте новый короб и нажмите подтверждение повторно.",
                    )
                else:
                    result.add(
                        "success",
                        "Задания ричтраку созданы: "
                        + ", ".join(payload.get("additional_pick_move_ids") or []),
                    )
        elif action == "add_discrepancy_item":
            _handle_add_discrepancy_item(
                result=result,
                order=order,
                request=request,
                role=role,
                stock_rows_for_order=stock_rows_for_order,
                permissions=permissions,
                parse_selected_stock_items=parse_selected_stock_items,
                selected_stock_rows_with_boxes=selected_stock_rows_with_boxes,
            )
        elif action == "create_otg_supplemental_pick":
            _handle_create_otg_supplemental_pick(
                result=result,
                order=order,
                request=request,
                role=role,
                permissions=permissions,
            )
        elif action == "mark_packed":
            result.redirect_name = "shipping:packing"
        elif action == "create_pickup_trip":
            _handle_create_pickup_trip(
                result=result,
                order=order,
                request=request,
                role=role,
            )
        elif action == "complete_transfer":
            if not permissions.can_complete_transfer:
                result.add("error", "Завершение перемещения недоступно в текущем статусе или роли.")
            elif complete_transfer_without_trip(order, request.user):
                result.add("success", "Перемещение завершено без рейса. Товар списан со склада.")
            else:
                result.add("warning", "Перемещение уже завершено.")
        elif action == "request_warehouse_cancel":
            reason = str(request.POST.get("cancel_reason") or "").strip()
            if not permissions.can_request_warehouse_cancel:
                result.add("error", "После принятия заявки складом отмену подтверждает кладовщик.")
            elif not reason:
                result.add("error", "Укажите причину отмены.")
            else:
                request_warehouse_cancel_confirmation(order, request.user, reason=reason)
                result.add("success", "Запрос на отмену отправлен складу.")
        elif action == "approve_warehouse_cancel":
            if not permissions.can_review_warehouse_cancel:
                result.add("error", "Подтвердить отмену может только кладовщик.")
            else:
                reason = str(request.POST.get("cancel_reason") or "").strip()
                cancel_order(order, request.user, reason=reason)
                result.add("success", "Отмена подтверждена складом, резерв снят.")
        elif action == "reject_warehouse_cancel":
            if not permissions.can_review_warehouse_cancel:
                result.add("error", "Отклонить отмену может только кладовщик.")
            else:
                reject_warehouse_cancel_confirmation(order, request.user)
                result.add("success", "Склад не подтвердил отмену. Заявка остаётся в работе.")
        elif action == "cancel":
            reason = str(request.POST.get("cancel_reason") or "").strip()
            if not permissions.can_cancel:
                result.add("error", "Отмена недоступна в текущем статусе/роли.")
            elif order.is_closed():
                result.add("warning", "Заявка уже закрыта.")
            elif not reason:
                result.add("error", "Укажите причину отмены.")
            elif role == "client" and order.status == ShippingOrder.STATUS_SUBMITTED:
                # Client LK: cancel before manager approval → unreserve and back to draft.
                return_order_to_draft(order, request.user)
                result.add("success", "Заявка отменена, товар снят с резерва и возвращён в подготовку.")
            else:
                cancel_order(order, request.user, reason=reason)
                result.add("success", "Заявка отменена, товар снят с резерва.")
    except ValidationError as exc:
        result.add("error", "; ".join(exc.messages))
    except ValueError:
        result.add("error", "Некорректный формат количества.")
    return result


def _handle_add_item(
    *,
    result: ShippingDetailActionResult,
    order: ShippingOrder,
    request,
    stock_rows_for_order: list[dict],
    permissions: ShippingDetailActionPermissions,
    parse_selected_stock_items: Callable,
    selected_stock_rows_with_boxes: Callable,
    log_update: Callable,
) -> None:
    if not permissions.can_edit_items:
        result.add("error", "Редактирование позиций доступно только до согласования менеджером.")
        return
    selected_rows, selection_errors = parse_selected_stock_items(
        request,
        stock_rows_for_order,
        multiple=True,
    )
    if selection_errors:
        result.add("error", "; ".join(selection_errors))
        return
    _expanded_rows, added_box_count, _selection_errors = selected_stock_rows_with_boxes(
        request,
        stock_rows_for_order,
        multiple=False,
    )
    with transaction.atomic():
        row = selected_rows[0]
        item = ShippingOrderItem(**row)
        item.order = order
        item.save()
        order.expected_boxes = int(order.expected_boxes or 0) + int(max(added_box_count, 0))
        order.save(update_fields=["expected_boxes", "updated_at"])
        if order.status == ShippingOrder.STATUS_SUBMITTED:
            reserve_order(
                order,
                request.user,
                target_status=ShippingOrder.STATUS_SUBMITTED,
                log_description="Резерв обновлен после изменения позиций заявки",
            )
    log_update(order, request, f"Добавлена позиция {item.sku_code} ({item.qty_requested})")
    result.add("success", "Позиция добавлена.")


def _handle_add_discrepancy_item(
    *,
    result: ShippingDetailActionResult,
    order: ShippingOrder,
    request,
    role: str | None,
    stock_rows_for_order: list[dict],
    permissions: ShippingDetailActionPermissions,
    parse_selected_stock_items: Callable,
    selected_stock_rows_with_boxes: Callable,
) -> None:
    if not permissions.can_add_discrepancy_item:
        result.add("error", "Добор по расхождению недоступен.")
        return
    selected_rows, selection_errors = parse_selected_stock_items(
        request,
        stock_rows_for_order,
        multiple=False,
    )
    if selection_errors:
        result.add("error", "; ".join(selection_errors))
        return
    _expanded_rows, added_box_count, selection_errors = selected_stock_rows_with_boxes(
        request,
        stock_rows_for_order,
        multiple=True,
    )
    if selection_errors:
        result.add("error", "; ".join(selection_errors))
        return
    with transaction.atomic():
        payload = apply_discrepancy_substitution(
            order,
            selected_items=selected_rows,
            added_box_count=added_box_count,
            user=request.user,
        )
        added_item_ids = [
            int(item.get("item_id") or 0)
            for item in list(payload.get("added_items") or [])
            if int(item.get("item_id") or 0) > 0
        ]
        pick_items = list(order.items.filter(id__in=added_item_ids).order_by("id"))
        if not pick_items:
            raise ValidationError("Не найдены строки добора для задания ричтраку OTG.")
        from otg_reachtruck.services import create_otg_shipping_pick_request

        _move_request, move_ids, shortage_qty = create_otg_shipping_pick_request(
            order=order,
            user=request.user,
            requested_by_name=request.user.get_full_name() or request.user.username,
            requested_by_role=role or "",
            allow_partial=False,
            demand_items=pick_items,
            cancel_existing=False,
            request_reason="shipping_discrepancy_pickup",
        )
        if not move_ids and shortage_qty > 0:
            raise ValidationError("Не удалось создать задание ричтраку OTG на согласованный добор.")
        mark_discrepancy_pick_created(order, move_ids=move_ids, user=request.user)
    if move_ids:
        result.add("success", f"Добор добавлен, ричтраку OTG созданы задания: {', '.join(move_ids)}.")
    else:
        result.add("warning", "Добор добавлен, но задания ричтраку не созданы.")
    result.add(
        "success",
        f"План пересчитан по warehouse: было {payload.get('old_expected_boxes')}, стало {payload.get('new_expected_boxes')}.",
    )


def _handle_create_otg_supplemental_pick(
    *,
    result: ShippingDetailActionResult,
    order: ShippingOrder,
    request,
    role: str | None,
    permissions: ShippingDetailActionPermissions,
) -> None:
    if not permissions.can_create_otg_supplemental_pick:
        result.add("error", "Создание добора ричтраку недоступно.")
        return

    from otg_reachtruck.services import create_otg_shipping_supplemental_pick

    _move_request, move_ids, preview = create_otg_shipping_supplemental_pick(
        order=order,
        user=request.user,
        requested_by_name=request.user.get_full_name() or request.user.username,
        requested_by_role=role or "",
    )
    result.add(
        "success",
        "Создан добор ричтраку: "
        f"{preview['requested_boxes']} короб., {preview['requested_qty']} шт. "
        f"Задания: {', '.join(move_ids)}.",
    )


def _handle_remove_item(
    *,
    result: ShippingDetailActionResult,
    order: ShippingOrder,
    request,
    permissions: ShippingDetailActionPermissions,
    parse_box_count_from_comment: Callable[[str | None], int],
    log_update: Callable,
) -> None:
    if not permissions.can_edit_items:
        result.add("error", "Редактирование позиций доступно только до согласования менеджером.")
        return
    item_id = int(request.POST.get("item_id") or 0)
    item = order.items.filter(id=item_id).first()
    if not item:
        return
    with transaction.atomic():
        item_label = f"{item.sku_code}/{item.size or '-'}"
        removed_box_count = parse_box_count_from_comment(item.comment)
        item.delete()
        order.expected_boxes = max(int(order.expected_boxes or 0) - int(removed_box_count or 0), 0)
        order.save(update_fields=["expected_boxes", "updated_at"])
        if order.status == ShippingOrder.STATUS_SUBMITTED:
            if order.items.exists():
                reserve_order(
                    order,
                    request.user,
                    target_status=ShippingOrder.STATUS_SUBMITTED,
                    log_description="Резерв обновлен после изменения позиций заявки",
                )
            else:
                release_order_reserves(order, request.user)
    log_update(order, request, f"Удалена позиция {item_label}")
    result.add("success", "Позиция удалена.")

def _handle_create_pickup_trip(
    *,
    result: ShippingDetailActionResult,
    order: ShippingOrder,
    request,
    role: str | None,
) -> None:
    allowed_roles = {"storekeeper", "manager", "head_manager", "director", "admin", "developer"}
    if role not in allowed_roles:
        result.add("error", "\u0424\u043e\u0440\u043c\u0438\u0440\u043e\u0432\u0430\u043d\u0438\u0435 \u0440\u0435\u0439\u0441\u0430 \u043d\u0435\u0434\u043e\u0441\u0442\u0443\u043f\u043d\u043e \u0432 \u0432\u0430\u0448\u0435\u0439 \u0440\u043e\u043b\u0438.")
        return
    if order.delivery_type != ShippingOrder.DELIVERY_PICKUP:
        result.add("error", "\u042d\u0442\u043e \u0434\u0435\u0439\u0441\u0442\u0432\u0438\u0435 \u0434\u043e\u0441\u0442\u0443\u043f\u043d\u043e \u0442\u043e\u043b\u044c\u043a\u043e \u0434\u043b\u044f \u0441\u0430\u043c\u043e\u0432\u044b\u0432\u043e\u0437\u0430.")
        return
    if order.status != ShippingOrder.STATUS_PACKED:
        result.add("error", "\u0420\u0435\u0439\u0441 \u043c\u043e\u0436\u043d\u043e \u0441\u0444\u043e\u0440\u043c\u0438\u0440\u043e\u0432\u0430\u0442\u044c \u0442\u043e\u043b\u044c\u043a\u043e \u043f\u043e\u0441\u043b\u0435 \u0443\u043f\u0430\u043a\u043e\u0432\u043a\u0438 \u043f\u0430\u043b\u043b\u0435\u0442.")
        return

    from logistics.models import LogisticsTrip, LogisticsTripOrder
    from logistics.services import _assign_public_trip_number, _ensure_trip_storekeeper_task, next_draft_trip_number
    from .dispatch import shipping_dispatch_trip_link

    existing_link = shipping_dispatch_trip_link(order)
    if existing_link is not None:
        trip = existing_link.trip
        if trip.status == LogisticsTrip.STATUS_LOADING:
            result.redirect_url = f"/logistics/trips/{trip.pk}/loading/"
        else:
            result.redirect_url = f"/logistics/trips/{trip.pk}/"
        result.add("warning", f"\u0414\u043b\u044f \u0437\u0430\u044f\u0432\u043a\u0438 {order.number} \u0440\u0435\u0439\u0441 \u0443\u0436\u0435 \u0441\u043e\u0437\u0434\u0430\u043d.")
        return

    trip_date = order.slot_date or order.planned_ship_date
    if trip_date is None and order.eta_at is not None:
        eta_at = order.eta_at
        if timezone.is_naive(eta_at):
            eta_at = timezone.make_aware(eta_at, timezone.get_current_timezone())
        trip_date = timezone.localtime(eta_at).date()
    if trip_date is None:
        trip_date = timezone.localdate()

    agency_name = str(getattr(order.agency, "agn_name", "") or "").strip()
    raw_items = list(order.items.order_by("id").values_list("name", "sku_code", "size")[:3])
    total_items = order.items.count()
    item_labels: list[str] = []
    for name, sku_code, size in raw_items:
        label = str(name or sku_code or "").strip()
        size_value = str(size or "").strip()
        if size_value and size_value not in {"-", "0"}:
            label = f"{label} {size_value}".strip()
        if label:
            item_labels.append(label)
    item_summary = ", ".join(item_labels)
    if total_items > len(item_labels):
        extra_count = total_items - len(item_labels)
        suffix = f" (+{extra_count})"
        item_summary = f"{item_summary}{suffix}" if item_summary else suffix.strip()

    eta_label = ""
    if order.eta_at is not None:
        eta_at = order.eta_at
        if timezone.is_naive(eta_at):
            eta_at = timezone.make_aware(eta_at, timezone.get_current_timezone())
        eta_label = timezone.localtime(eta_at).strftime("%d.%m.%Y")
    elif trip_date is not None:
        eta_label = timezone.localtime().strftime("%d.%m.%Y %H:%M")

    route_comment = " | ".join(
        bit
        for bit in [
            f"\u0417\u0430\u044f\u0432\u043a\u0430 {order.number}",
            agency_name,
            item_summary,
            eta_label,
        ]
        if bit
    )

    with transaction.atomic():
        trip = LogisticsTrip.objects.create(
            number=next_draft_trip_number(),
            trip_date=trip_date,
            status=LogisticsTrip.STATUS_LOADING,
            vehicle_type=LogisticsTrip.VEHICLE_CLIENT,
            vehicle_name="\u0421\u0430\u043c\u043e\u0432\u044b\u0432\u043e\u0437",
            route_comment=route_comment,
            created_by=request.user if getattr(request.user, "is_authenticated", False) else None,
        )
        trip.number = _assign_public_trip_number(trip)
        trip.save(update_fields=["number", "updated_at"])
        LogisticsTripOrder.objects.create(
            trip=trip,
            shipping_order=order,
            loading_sequence=1,
            delivery_sequence=1,
        )
        _ensure_trip_storekeeper_task(trip, request.user)

    result.redirect_url = f"/logistics/trips/{trip.pk}/loading/"
    result.add("success", f"\u0420\u0435\u0439\u0441 {trip.number} \u0441\u0444\u043e\u0440\u043c\u0438\u0440\u043e\u0432\u0430\u043d. \u041c\u043e\u0436\u043d\u043e \u0433\u0440\u0443\u0437\u0438\u0442\u044c \u043f\u0430\u043b\u043b\u0435\u0442\u044b.")
