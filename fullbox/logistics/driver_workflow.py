from __future__ import annotations

import json
import logging
from datetime import datetime, time
from decimal import Decimal, InvalidOperation
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q, Sum
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime

from employees.models import Employee

from .models import (
    LogisticsTrip,
    ProblemTrip,
    ShippingRoutingState,
    TripAttachment,
    TripHistory,
    TripNotification,
    TripPenalty,
)


MANAGER_ROLES = {"logistician", "manager", "head_manager", "director", "admin", "developer"}
GLOBAL_PROBLEM_ROLES = {"head_manager", "director", "admin", "developer"}
MAX_UPLOAD_BYTES = 15 * 1024 * 1024
DRIVER_ASSIGNMENT_PRELIMINARY = "preliminary"
DRIVER_ASSIGNMENT_CONFIRMED = "confirmed"

logger = logging.getLogger(__name__)


def _user_or_none(user):
    return user if getattr(user, "is_authenticated", False) else None


def _aware_datetime(value: str | None, *, field_label: str) -> datetime:
    raw = str(value or "").strip()
    parsed = parse_datetime(raw)
    if parsed is None:
        parsed_date = parse_date(raw)
        if parsed_date is not None:
            parsed = datetime.combine(parsed_date, time.min)
    if parsed is None:
        raise ValidationError(f"Укажите {field_label}.")
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, timezone.get_current_timezone())
    return parsed


def _optional_datetime(value: str | None, *, field_label: str) -> datetime | None:
    if not str(value or "").strip():
        return None
    return _aware_datetime(value, field_label=field_label)


def _decimal(value: str | None, *, field_label: str) -> Decimal | None:
    raw = str(value or "").strip().replace(" ", "").replace(",", ".")
    if not raw:
        return None
    try:
        parsed = Decimal(raw)
    except InvalidOperation as exc:
        raise ValidationError(f"Поле «{field_label}» должно быть числом.") from exc
    if parsed < 0:
        raise ValidationError(f"Поле «{field_label}» не может быть отрицательным.")
    return parsed


def _coordinate(value: str | None, *, field_label: str, limit: Decimal) -> Decimal | None:
    raw = str(value or "").strip().replace(",", ".")
    if not raw:
        return None
    try:
        parsed = Decimal(raw)
    except InvalidOperation as exc:
        raise ValidationError(f"Поле «{field_label}» должно быть числом.") from exc
    if parsed < -limit or parsed > limit:
        raise ValidationError(f"Некорректное значение поля «{field_label}».")
    return parsed


def _validate_photo(upload, *, label: str) -> None:
    if upload is None:
        raise ValidationError(f"Добавьте {label}.")
    content_type = str(getattr(upload, "content_type", "") or "").lower()
    if content_type and not content_type.startswith("image/"):
        raise ValidationError(f"{label.capitalize()} должна быть изображением.")
    if int(getattr(upload, "size", 0) or 0) > MAX_UPLOAD_BYTES:
        raise ValidationError(f"{label.capitalize()} превышает 15 МБ.")


def log_trip_event(trip: LogisticsTrip, *, event_type: str, description: str, user=None, payload=None) -> TripHistory:
    return TripHistory.objects.create(
        trip=trip,
        event_type=event_type,
        description=description,
        actor=_user_or_none(user),
        payload=payload if isinstance(payload, dict) else {},
    )


def notify_employee(*, recipient: Employee | None, trip: LogisticsTrip, title: str, text: str, detail_url: str, event: str):
    if recipient is None:
        return None
    return TripNotification.objects.create(
        recipient=recipient,
        trip=trip,
        title=title,
        text=text,
        detail_url=detail_url,
        source_key=f"trip:{trip.pk}:{event}:{recipient.pk}:{uuid4().hex[:12]}",
    )


def _trip_schedule_label(trip: LogisticsTrip) -> str:
    scheduled_at = trip.scheduled_at
    if scheduled_at:
        return timezone.localtime(scheduled_at).strftime("%d.%m.%Y, %H:%M")
    if trip.trip_date:
        return trip.trip_date.strftime("%d.%m.%Y")
    return "не указана"


def _trip_marketplace_label(trip: LogisticsTrip) -> str:
    if trip.trip_kind == LogisticsTrip.KIND_EXTERNAL:
        return "Внешний рейс"
    names = []
    for link in trip.orders.select_related("shipping_order__marketplace"):
        marketplace = getattr(link.shipping_order, "marketplace", None)
        value = str(marketplace or "").strip()
        if value and value not in names:
            names.append(value)
    return ", ".join(names) or "—"


def _trip_destination_label(trip: LogisticsTrip) -> str:
    if trip.trip_kind == LogisticsTrip.KIND_EXTERNAL:
        details = getattr(trip, "external_details", None)
        if details:
            return f"{details.pickup_address} → {details.delivery_address}"
    values = []
    for link in trip.orders.select_related("shipping_order"):
        order = link.shipping_order
        value = str(order.destination_warehouse or order.destination_address or "").strip()
        if value and value not in values:
            values.append(value)
    return ", ".join(values) or "—"


def can_manage_driver_assignment(role: str | None) -> bool:
    return str(role or "") in MANAGER_ROLES


@transaction.atomic
def assign_driver(
    *,
    trip: LogisticsTrip,
    driver: Employee,
    actor: Employee | None,
    user=None,
    scheduled_at: str = "",
    assignment_mode: str = DRIVER_ASSIGNMENT_CONFIRMED,
) -> LogisticsTrip:
    if driver is None or driver.role != "driver" or not driver.is_active or not driver.user_id:
        raise ValidationError("Выберите активного водителя с учетной записью.")
    assignment_mode = str(assignment_mode or DRIVER_ASSIGNMENT_CONFIRMED).strip().lower()
    if assignment_mode not in {DRIVER_ASSIGNMENT_PRELIMINARY, DRIVER_ASSIGNMENT_CONFIRMED}:
        raise ValidationError("Некорректный режим назначения водителя.")
    target_status = (
        LogisticsTrip.DRIVER_STATUS_PRELIMINARY
        if assignment_mode == DRIVER_ASSIGNMENT_PRELIMINARY
        else LogisticsTrip.DRIVER_STATUS_ASSIGNED
    )
    locked = (
        LogisticsTrip.objects.select_related("assigned_driver")
        .select_for_update(of=("self",))
        .get(pk=trip.pk)
    )
    if locked.status in {LogisticsTrip.STATUS_COMPLETED, LogisticsTrip.STATUS_CANCELED}:
        raise ValidationError("Нельзя назначить водителя на завершенный или отмененный рейс.")
    old_driver = locked.assigned_driver
    parsed_schedule = _optional_datetime(scheduled_at, field_label="дату и время подачи")
    now = timezone.now()
    same_driver = bool(old_driver and old_driver.pk == driver.pk)
    transitioned = False
    if same_driver:
        if assignment_mode == DRIVER_ASSIGNMENT_PRELIMINARY and locked.driver_status not in {
            LogisticsTrip.DRIVER_STATUS_UNASSIGNED,
            LogisticsTrip.DRIVER_STATUS_PRELIMINARY,
        }:
            raise ValidationError("Подтвержденный или начатый рейс нельзя снова сделать предварительным.")
        update_fields = []
        if parsed_schedule and parsed_schedule != locked.scheduled_at:
            locked.scheduled_at = parsed_schedule
            update_fields.append("scheduled_at")
        if locked.driver_name != driver.full_name:
            locked.driver_name = driver.full_name
            update_fields.append("driver_name")
        driver_phone = str(driver.phone or "").strip()
        if locked.driver_phone != driver_phone:
            locked.driver_phone = driver_phone
            update_fields.append("driver_phone")
        if locked.driver_status != target_status and locked.driver_status in {
            LogisticsTrip.DRIVER_STATUS_UNASSIGNED,
            LogisticsTrip.DRIVER_STATUS_PRELIMINARY,
        }:
            locked.driver_status = target_status
            locked.driver_assigned_at = now
            locked.driver_assigned_by = actor
            update_fields.extend(["driver_status", "driver_assigned_at", "driver_assigned_by"])
            transitioned = True
        if update_fields:
            locked.save(update_fields=[*dict.fromkeys(update_fields), "updated_at"])
        if not transitioned:
            return locked
    else:
        locked.assigned_driver = driver
        locked.driver_assigned_at = now
        locked.driver_assigned_by = actor
        locked.driver_status = target_status
        locked.scheduled_at = parsed_schedule or locked.scheduled_at
        locked.driver_name = driver.full_name
        locked.driver_phone = str(driver.phone or "").strip()
        locked.save(
            update_fields=[
                "assigned_driver",
                "driver_assigned_at",
                "driver_assigned_by",
                "driver_status",
                "scheduled_at",
                "driver_name",
                "driver_phone",
                "updated_at",
            ]
        )
        if old_driver and old_driver.pk != driver.pk:
            notify_employee(
                recipient=old_driver,
                trip=locked,
                title=f"Назначение на рейс {locked.number} отменено",
                text=f"Вы больше не назначены на рейс {locked.number}.",
                detail_url="/logistics/driver/trips/",
                event="assignment-canceled",
            )
            log_trip_event(
                locked,
                event_type="driver_replaced",
                description=f"Водитель {old_driver.full_name} снят с рейса.",
                user=user,
                payload={"previous_driver_id": old_driver.pk, "new_driver_id": driver.pk},
            )
    schedule_label = _trip_schedule_label(locked)
    marketplace_label = _trip_marketplace_label(locked)
    destination_label = _trip_destination_label(locked)
    destination_text = (
        f"Маршрут: {destination_label}"
        if locked.trip_kind == LogisticsTrip.KIND_EXTERNAL
        else f"Маркетплейс: {marketplace_label}. Адрес: {destination_label}"
    )
    if assignment_mode == DRIVER_ASSIGNMENT_PRELIMINARY:
        notification_title = f"Предварительный рейс {locked.number}"
        notification_text = (
            f"Рейс пока не подтвержден. Предварительный план: {schedule_label}. "
            f"{destination_text}"
        )
        notification_event = "preliminary"
        history_event = "driver_preassigned"
        history_description = f"Водитель {driver.full_name} предварительно поставлен на рейс."
    else:
        notification_title = f"Назначен новый рейс {locked.number}"
        notification_text = (
            f"Рейс подтвержден. Дата подачи: {schedule_label}. "
            f"{destination_text}"
        )
        notification_event = "assigned"
        history_event = "driver_assignment_confirmed" if same_driver else "driver_assigned"
        history_description = (
            f"Предварительный рейс подтвержден для водителя {driver.full_name}."
            if same_driver
            else f"Назначен водитель {driver.full_name}."
        )
    notify_employee(
        recipient=driver,
        trip=locked,
        title=notification_title,
        text=notification_text,
        detail_url=f"/logistics/driver/trips/{locked.pk}/",
        event=notification_event,
    )
    log_trip_event(
        locked,
        event_type=history_event,
        description=history_description,
        user=user,
        payload={
            "driver_id": driver.pk,
            "assigned_by_id": getattr(actor, "pk", None),
            "assigned_at": now.isoformat(),
            "scheduled_at": locked.scheduled_at.isoformat() if locked.scheduled_at else "",
            "assignment_mode": assignment_mode,
        },
    )
    return locked


def driver_can_open_trip(*, trip: LogisticsTrip, employee: Employee | None, role: str | None) -> bool:
    if role == "developer":
        return True
    return bool(employee and employee.role == "driver" and trip.assigned_driver_id == employee.pk)


def _ensure_driver_trip(trip: LogisticsTrip, employee: Employee | None, role: str | None) -> None:
    if not driver_can_open_trip(trip=trip, employee=employee, role=role):
        raise PermissionError("Этот рейс не назначен текущему водителю.")


def _submission_already_saved(trip: LogisticsTrip, submission_id: str) -> bool:
    if not submission_id:
        return False
    return trip.driver_history.filter(payload__submission_id=submission_id).exists()


@transaction.atomic
def change_driver_status(*, trip: LogisticsTrip, employee: Employee, role: str, action: str, user=None) -> LogisticsTrip:
    locked = LogisticsTrip.objects.select_for_update().get(pk=trip.pk)
    _ensure_driver_trip(locked, employee, role)
    now = timezone.now()
    transitions = {
        "accept": (LogisticsTrip.DRIVER_STATUS_ASSIGNED, LogisticsTrip.DRIVER_STATUS_ACCEPTED, "driver_accepted_at", "Рейс принят водителем"),
        "depart": (LogisticsTrip.DRIVER_STATUS_ACCEPTED, LogisticsTrip.DRIVER_STATUS_EN_ROUTE, "driver_departed_at", "Водитель выехал"),
        "arrive": (LogisticsTrip.DRIVER_STATUS_EN_ROUTE, LogisticsTrip.DRIVER_STATUS_AWAITING_DELIVERY, "driver_arrived_at", "Водитель прибыл на склад; рейс ожидает сдачи"),
    }
    transition = transitions.get(action)
    if transition is None:
        raise ValidationError("Неизвестное действие рейса.")
    expected, next_status, time_field, label = transition
    if locked.driver_status == next_status:
        return locked
    if locked.driver_status != expected:
        raise ValidationError("Действие недоступно в текущем статусе рейса.")
    locked.driver_status = next_status
    setattr(locked, time_field, now)
    update_fields = ["driver_status", time_field, "updated_at"]
    if action == "depart" and locked.trip_kind == LogisticsTrip.KIND_EXTERNAL:
        locked.status = LogisticsTrip.STATUS_DEPARTED
        update_fields.append("status")
    if action == "arrive" and locked.trip_kind == LogisticsTrip.KIND_EXTERNAL:
        label = "Водитель прибыл в точку Б; рейс ожидает сдачи"
    locked.save(update_fields=update_fields)
    log_trip_event(
        locked,
        event_type=action,
        description=label,
        user=user,
        payload={"status": next_status, "at": now.isoformat()},
    )
    return locked


def _photo_metadata(request) -> dict:
    return {
        "photo_created_at": _optional_datetime(request.POST.get("photo_created_at"), field_label="время создания фотографии"),
        "latitude": _coordinate(request.POST.get("latitude"), field_label="широта", limit=Decimal("90")),
        "longitude": _coordinate(request.POST.get("longitude"), field_label="долгота", limit=Decimal("180")),
    }


def _save_submitted_logistics_facts(request, trip: LogisticsTrip) -> None:
    raw = str(request.POST.get("logistics_service_facts") or "").strip()
    if not raw:
        return
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ValidationError("Не удалось прочитать перечень услуг рейса.") from exc
    if not isinstance(payload, list):
        raise ValidationError("Некорректный перечень услуг рейса.")

    from billing.warehouse_services import logistics_trip_clients, replace_warehouse_facts

    clients = {client.pk: client for client in logistics_trip_clients(trip)}
    seen_client_ids: set[int] = set()
    for item in payload:
        if not isinstance(item, dict):
            raise ValidationError("Некорректный перечень услуг рейса.")
        try:
            client_id = int(item.get("client_id") or 0)
        except (TypeError, ValueError):
            client_id = 0
        client = clients.get(client_id)
        if client is None:
            raise ValidationError("Клиент не входит в состав этого рейса.")
        if client_id in seen_client_ids:
            raise ValidationError("Услуги одного клиента не могут быть переданы дважды.")
        seen_client_ids.add(client_id)
        lines = item.get("lines") or []
        if not isinstance(lines, list):
            raise ValidationError("Некорректный перечень услуг рейса.")
        replace_warehouse_facts(
            client=client,
            order_type="logistics",
            order_id=trip.number,
            lines=lines,
            user=request.user,
        )


@transaction.atomic
def confirm_delivery(*, request, trip: LogisticsTrip, employee: Employee, role: str) -> LogisticsTrip:
    locked = LogisticsTrip.objects.select_for_update().get(pk=trip.pk)
    _ensure_driver_trip(locked, employee, role)
    submission_id = str(request.POST.get("submission_id") or "").strip()
    if _submission_already_saved(locked, submission_id):
        return locked
    if locked.driver_status != LogisticsTrip.DRIVER_STATUS_AWAITING_DELIVERY:
        destination = "в точку Б" if locked.trip_kind == LogisticsTrip.KIND_EXTERNAL else "на склад"
        raise ValidationError(f"Подтверждение сдачи доступно только после прибытия {destination}.")
    if str(request.POST.get("result_confirmed") or "") not in {"1", "on", "true"}:
        raise ValidationError("Подтвердите результат сдачи.")
    gate_photo = request.FILES.get("gate_photo")
    photo_label = "фотографию точки сдачи" if locked.trip_kind == LogisticsTrip.KIND_EXTERNAL else "фотографию ворот"
    _validate_photo(gate_photo, label=photo_label)
    handover_at = _aware_datetime(request.POST.get("actual_handover_at"), field_label="фактические дату и время сдачи")
    system_at = timezone.now()
    change_reason = str(request.POST.get("handover_time_change_reason") or "").strip()
    changed_manually = abs((handover_at - system_at).total_seconds()) > 60
    if changed_manually and not change_reason:
        raise ValidationError("Укажите причину изменения фактического времени сдачи.")
    metadata = _photo_metadata(request)
    from billing.warehouse_services import require_logistics_trip_completion_facts

    _save_submitted_logistics_facts(request, locked)
    require_logistics_trip_completion_facts(locked)
    TripAttachment.objects.create(
        trip=locked,
        attachment_type=TripAttachment.TYPE_GATE,
        file=gate_photo,
        uploaded_by=_user_or_none(request.user),
        **metadata,
    )
    for upload in request.FILES.getlist("receipt_photos"):
        _validate_photo(upload, label="фотографию документа")
        TripAttachment.objects.create(
            trip=locked,
            attachment_type=TripAttachment.TYPE_RECEIPT,
            file=upload,
            uploaded_by=_user_or_none(request.user),
        )
    if locked.trip_kind != LogisticsTrip.KIND_EXTERNAL:
        from .routing_services import mark_order_delivered
        from .services import _ship_single_trip_order

        for link in locked.orders.select_related("shipping_order"):
            _ship_single_trip_order(link.shipping_order, user=request.user)
            mark_order_delivered(link.shipping_order, user=request.user)
    locked.refresh_from_db()
    locked.driver_status = LogisticsTrip.DRIVER_STATUS_DELIVERED
    locked.actual_handover_at = handover_at
    locked.handover_system_at = system_at
    locked.handover_time_change_reason = change_reason
    locked.gate_number = str(request.POST.get("gate_number") or "").strip()
    locked.waiting_started_at = _optional_datetime(request.POST.get("waiting_started_at"), field_label="время начала ожидания")
    locked.unloading_finished_at = _optional_datetime(request.POST.get("unloading_finished_at"), field_label="время окончания разгрузки")
    locked.driver_result_comment = str(request.POST.get("driver_comment") or "").strip()
    locked.delivery_receipt_number = str(request.POST.get("receipt_number") or "").strip()
    locked.closed_at = system_at
    if locked.status != LogisticsTrip.STATUS_COMPLETED:
        locked.status = LogisticsTrip.STATUS_COMPLETED
    locked.save(
        update_fields=[
            "driver_status", "actual_handover_at", "handover_system_at", "handover_time_change_reason",
            "gate_number", "waiting_started_at", "unloading_finished_at", "driver_result_comment",
            "delivery_receipt_number", "closed_at", "status", "updated_at",
        ]
    )
    log_trip_event(
        locked,
        event_type="delivery_confirmed",
        description="Груз сдан; рейс автоматически закрыт.",
        user=request.user,
        payload={
            "submission_id": submission_id,
            "system_at": system_at.isoformat(),
            "actual_handover_at": handover_at.isoformat(),
            "time_changed_manually": changed_manually,
            "time_change_reason": change_reason,
        },
    )
    notify_employee(
        recipient=locked.assigned_logistician,
        trip=locked,
        title=f"Рейс {locked.number} успешно сдан",
        text=f"Время сдачи: {timezone.localtime(handover_at):%d.%m.%Y %H:%M}. Водитель: {employee.full_name}.",
        detail_url=(
            f"/logistics/trips/external/{locked.pk}/"
            if locked.trip_kind == LogisticsTrip.KIND_EXTERNAL
            else f"/logistics/trips/{locked.pk}/driver-assignment/"
        ),
        event="delivered",
    )
    from billing.warehouse_services import sync_logistics_trip_facts_to_billing

    sync_logistics_trip_facts_to_billing(trip=locked, user=_user_or_none(request.user))
    return locked


@transaction.atomic
def confirm_delivery_failure(*, request, trip: LogisticsTrip, employee: Employee, role: str) -> ProblemTrip:
    locked = LogisticsTrip.objects.select_for_update().get(pk=trip.pk)
    _ensure_driver_trip(locked, employee, role)
    submission_id = str(request.POST.get("submission_id") or "").strip()
    if _submission_already_saved(locked, submission_id):
        return ProblemTrip.objects.get(trip=locked)
    if locked.driver_status != LogisticsTrip.DRIVER_STATUS_AWAITING_DELIVERY:
        raise ValidationError("Несдачу можно зафиксировать только после прибытия на склад.")
    reason_code = str(request.POST.get("reason_code") or "").strip()
    valid_reasons = {value for value, _label in ProblemTrip.REASON_CHOICES}
    if reason_code not in valid_reasons:
        raise ValidationError("Выберите причину несдачи.")
    comment = str(request.POST.get("comment") or "").strip()
    if not comment:
        raise ValidationError("Добавьте подробный комментарий.")
    refusal_at = _aware_datetime(request.POST.get("refusal_at"), field_label="дату и время отказа")
    evidence = request.FILES.getlist("evidence_photos")
    if not evidence:
        raise ValidationError("Добавьте минимум одну фотографию подтверждения.")
    for upload in evidence:
        _validate_photo(upload, label="фотографию подтверждения")
    responsible_party = str(request.POST.get("responsible_party") or ProblemTrip.PARTY_UNKNOWN).strip()
    if responsible_party not in {value for value, _label in ProblemTrip.PARTY_CHOICES}:
        responsible_party = ProblemTrip.PARTY_UNKNOWN
    problem = ProblemTrip.objects.create(
        trip=locked,
        reason_code=reason_code,
        comment=comment,
        refusal_at=refusal_at,
        rejecting_employee=str(request.POST.get("rejecting_employee") or "").strip(),
        gate_number=str(request.POST.get("gate_number") or "").strip(),
        support_case_number=str(request.POST.get("support_case_number") or "").strip(),
        responsible_party=responsible_party,
        assigned_to=locked.assigned_logistician,
    )
    for upload in evidence:
        TripAttachment.objects.create(
            trip=locked,
            attachment_type=TripAttachment.TYPE_PROBLEM,
            file=upload,
            uploaded_by=_user_or_none(request.user),
        )
    for upload in request.FILES.getlist("problem_documents"):
        TripAttachment.objects.create(
            trip=locked,
            attachment_type=TripAttachment.TYPE_DOCUMENT,
            file=upload,
            uploaded_by=_user_or_none(request.user),
        )
    for link in locked.orders.select_related("shipping_order"):
        state, _created = ShippingRoutingState.objects.get_or_create(shipping_order=link.shipping_order)
        state.status = ShippingRoutingState.STATUS_FAILED
        state.save(update_fields=["status", "updated_at"])
    locked.driver_status = LogisticsTrip.DRIVER_STATUS_PROBLEM
    locked.gate_number = problem.gate_number or locked.gate_number
    locked.driver_result_comment = comment
    locked.save(update_fields=["driver_status", "gate_number", "driver_result_comment", "updated_at"])
    log_trip_event(
        locked,
        event_type="delivery_failed",
        description=f"Груз не сдан: {problem.get_reason_code_display()}.",
        user=request.user,
        payload={
            "submission_id": submission_id,
            "problem_id": problem.pk,
            "reason_code": reason_code,
            "comment": comment,
            "refusal_at": refusal_at.isoformat(),
        },
    )
    notify_employee(
        recipient=locked.assigned_logistician,
        trip=locked,
        title=f"Проблемный рейс {locked.number}",
        text=f"Груз не сдан. Причина: {problem.get_reason_code_display()}. Водитель: {employee.full_name}.",
        detail_url=f"/logistics/problems/{problem.pk}/",
        event="failed",
    )
    return problem


def _delivery_slot_label(slot_date, slot_time) -> str:
    parts = []
    if slot_date:
        parts.append(slot_date.strftime("%d.%m.%Y"))
    if slot_time:
        parts.append(slot_time.strftime("%H:%M"))
    return " · ".join(parts) or "Не указан"


def trip_summary(trip: LogisticsTrip) -> dict:
    orders = list(
        trip.orders.select_related("shipping_order__agency", "shipping_order__marketplace")
        .prefetch_related("shipping_order__attachments")
        .order_by("delivery_sequence", "id")
    )
    rows = []
    total_boxes = 0
    delivery_slots = []
    for link in orders:
        order = link.shipping_order
        total_boxes += int(order.expected_boxes or 0)
        slot_label = _delivery_slot_label(order.slot_date, order.slot_time)
        if slot_label != "Не указан" and slot_label not in delivery_slots:
            delivery_slots.append(slot_label)
        rows.append(
            {
                "order": order,
                "number": order.number,
                "client": getattr(order.agency, "agn_name", "") or "—",
                "marketplace": str(order.marketplace or "—"),
                "supply_type": order.get_supply_type_display() if order.supply_type else "—",
                "scheduled_at": order.eta_at,
                "slot_date": order.slot_date,
                "slot_time": order.slot_time,
                "slot_label": slot_label,
                "address": order.destination_address or order.destination_warehouse or "—",
                "warehouse": order.destination_warehouse or "—",
                "supply_number": order.supply_number or order.wb_supply_barcode or order.shipping_barcode or "—",
                "boxes": int(order.expected_boxes or 0),
                "documents": list(order.attachments.all()),
                "comment": order.comment or "",
            }
        )
    is_external = trip.trip_kind == LogisticsTrip.KIND_EXTERNAL
    details = getattr(trip, "external_details", None) if is_external else None
    return {
        "orders": rows,
        "marketplaces": _trip_marketplace_label(trip),
        "destinations": _trip_destination_label(trip),
        "total_boxes": total_boxes,
        "total_pallets": int(trip.max_pallets or 0),
        "delivery_slots_label": "; ".join(delivery_slots) or "Не указан",
        "is_external": is_external,
        "external_client": str(details.client) if details else "—",
        "pickup_address": str(getattr(details, "pickup_address", "") or "—"),
        "delivery_address": str(getattr(details, "delivery_address", "") or "—"),
        "cargo_description": str(getattr(details, "cargo_description", "") or "Не указано"),
        "pickup_contact": str(getattr(details, "pickup_contact", "") or ""),
        "pickup_phone": str(getattr(details, "pickup_phone", "") or ""),
        "delivery_contact": str(getattr(details, "delivery_contact", "") or ""),
        "delivery_phone": str(getattr(details, "delivery_phone", "") or ""),
    }


def driver_trip_queryset(employee: Employee | None, role: str | None):
    queryset = LogisticsTrip.objects.select_related(
        "assigned_driver", "assigned_logistician", "external_details__client"
    ).prefetch_related(
        "orders__shipping_order__agency", "orders__shipping_order__marketplace"
    )
    if role != "developer":
        queryset = queryset.filter(assigned_driver=employee)
    return queryset.order_by("-scheduled_at", "-trip_date", "-id")


def filter_driver_trips(queryset, *, bucket: str):
    today = timezone.localdate()
    if bucket == "new":
        return queryset.filter(
            driver_status__in=[
                LogisticsTrip.DRIVER_STATUS_PRELIMINARY,
                LogisticsTrip.DRIVER_STATUS_ASSIGNED,
            ]
        )
    if bucket == "today":
        return queryset.filter(Q(scheduled_at__date=today) | Q(scheduled_at__isnull=True, trip_date=today))
    if bucket == "work":
        return queryset.filter(driver_status__in=[LogisticsTrip.DRIVER_STATUS_ACCEPTED, LogisticsTrip.DRIVER_STATUS_EN_ROUTE, LogisticsTrip.DRIVER_STATUS_AWAITING_DELIVERY])
    if bucket == "completed":
        return queryset.filter(driver_status=LogisticsTrip.DRIVER_STATUS_DELIVERED)
    if bucket == "problem":
        return queryset.filter(driver_status=LogisticsTrip.DRIVER_STATUS_PROBLEM)
    return queryset


def problem_queryset(*, role: str, employee: Employee | None):
    queryset = ProblemTrip.objects.select_related(
        "trip", "trip__assigned_driver", "trip__assigned_logistician", "assigned_to"
    ).prefetch_related("trip__orders__shipping_order__agency", "trip__orders__shipping_order__marketplace", "penalties")
    if role == "logistician" and employee:
        queryset = queryset.filter(Q(trip__assigned_logistician=employee) | Q(assigned_to=employee))
    return queryset


def apply_problem_filters(queryset, params):
    if params.get("date_from"):
        queryset = queryset.filter(trip__trip_date__gte=params.get("date_from"))
    if params.get("date_to"):
        queryset = queryset.filter(trip__trip_date__lte=params.get("date_to"))
    if params.get("driver"):
        queryset = queryset.filter(trip__assigned_driver_id=params.get("driver"))
    if params.get("marketplace"):
        queryset = queryset.filter(trip__orders__shipping_order__marketplace__name__icontains=params.get("marketplace"))
    if params.get("client"):
        queryset = queryset.filter(trip__orders__shipping_order__agency__agn_name__icontains=params.get("client"))
    if params.get("reason"):
        queryset = queryset.filter(reason_code=params.get("reason"))
    if params.get("party"):
        queryset = queryset.filter(responsible_party=params.get("party"))
    if params.get("status"):
        queryset = queryset.filter(status=params.get("status"))
    if params.get("has_penalty") == "yes":
        queryset = queryset.filter(penalties__isnull=False)
    if params.get("has_penalty") == "no":
        queryset = queryset.filter(penalties__isnull=True)
    if params.get("q"):
        query = str(params.get("q") or "").strip()
        queryset = queryset.filter(
            Q(trip__number__icontains=query)
            | Q(trip__orders__shipping_order__number__icontains=query)
            | Q(trip__orders__shipping_order__agency__agn_name__icontains=query)
            | Q(trip__orders__shipping_order__marketplace__name__icontains=query)
        )
    return queryset.distinct()


@transaction.atomic
def update_problem(*, problem: ProblemTrip, request, employee: Employee | None) -> ProblemTrip:
    valid_statuses = {value for value, _label in ProblemTrip.STATUS_CHOICES}
    status = str(request.POST.get("status") or problem.status).strip()
    if status not in valid_statuses:
        raise ValidationError("Некорректный статус разбирательства.")
    responsible_party = str(request.POST.get("responsible_party") or problem.responsible_party).strip()
    if responsible_party not in {value for value, _label in ProblemTrip.PARTY_CHOICES}:
        raise ValidationError("Некорректная ответственная сторона.")
    assigned_id = str(request.POST.get("assigned_to") or "").strip()
    assigned_to = Employee.objects.filter(pk=assigned_id, is_active=True).first() if assigned_id else None
    problem.status = status
    problem.responsible_party = responsible_party
    problem.assigned_to = assigned_to
    problem.next_action_at = _optional_datetime(request.POST.get("next_action_at"), field_label="дату следующего действия")
    problem.save(update_fields=["status", "responsible_party", "assigned_to", "next_action_at", "updated_at"])
    log_trip_event(
        problem.trip,
        event_type="problem_updated",
        description=f"Проблемный рейс: статус «{problem.get_status_display()}».",
        user=request.user,
        payload={"problem_id": problem.pk, "status": problem.status, "assigned_to_id": getattr(assigned_to, "pk", None)},
    )
    notify_employee(
        recipient=assigned_to or problem.trip.assigned_logistician,
        trip=problem.trip,
        title=f"Изменен статус проблемного рейса {problem.trip.number}",
        text=f"Новый статус: {problem.get_status_display()}.",
        detail_url=f"/logistics/problems/{problem.pk}/",
        event="problem-status",
    )
    return problem


@transaction.atomic
def add_penalty(*, problem: ProblemTrip, request) -> TripPenalty:
    marketplace_name = str(request.POST.get("marketplace_name") or "").strip()
    penalty_type = str(request.POST.get("penalty_type") or "").strip()
    if not marketplace_name or not penalty_type:
        raise ValidationError("Укажите маркетплейс и тип штрафа.")
    status = str(request.POST.get("penalty_status") or TripPenalty.STATUS_EXPECTED).strip()
    if status not in {value for value, _label in TripPenalty.STATUS_CHOICES}:
        raise ValidationError("Некорректный статус штрафа.")
    responsible_party = str(request.POST.get("penalty_party") or ProblemTrip.PARTY_UNKNOWN).strip()
    if responsible_party not in {value for value, _label in ProblemTrip.PARTY_CHOICES}:
        raise ValidationError("Некорректная ответственная сторона штрафа.")
    charged_raw = str(request.POST.get("charged_at") or "").strip()
    charged_at = parse_date(charged_raw) if charged_raw else None
    if charged_raw and charged_at is None:
        raise ValidationError("Некорректная дата начисления штрафа.")
    penalty = TripPenalty.objects.create(
        problem=problem,
        marketplace_name=marketplace_name,
        penalty_type=penalty_type,
        reason=str(request.POST.get("penalty_reason") or "").strip(),
        charged_at=charged_at,
        estimated_amount=_decimal(request.POST.get("estimated_amount"), field_label="предполагаемая сумма"),
        actual_amount=_decimal(request.POST.get("actual_amount"), field_label="фактическая сумма"),
        document_number=str(request.POST.get("document_number") or "").strip(),
        support_reference=str(request.POST.get("support_reference") or "").strip(),
        responsible_party=responsible_party,
        status=status,
        comment=str(request.POST.get("penalty_comment") or "").strip(),
        document=request.FILES.get("penalty_document"),
        created_by=_user_or_none(request.user),
    )
    log_trip_event(
        problem.trip,
        event_type="penalty_added",
        description=f"Добавлен штраф: {marketplace_name}, {penalty_type}.",
        user=request.user,
        payload={"problem_id": problem.pk, "penalty_id": penalty.pk, "status": penalty.status},
    )
    notify_employee(
        recipient=problem.assigned_to or problem.trip.assigned_logistician,
        trip=problem.trip,
        title=f"Добавлен штраф по рейсу {problem.trip.number}",
        text=f"{marketplace_name}: {penalty_type}, статус «{penalty.get_status_display()}».",
        detail_url=f"/logistics/problems/{problem.pk}/",
        event="penalty-added",
    )
    return penalty


@transaction.atomic
def update_penalty(*, problem: ProblemTrip, request) -> TripPenalty:
    penalty = problem.penalties.select_for_update().filter(pk=request.POST.get("penalty_id")).first()
    if penalty is None:
        raise ValidationError("Штраф не найден в этом проблемном рейсе.")
    status = str(request.POST.get("penalty_status") or penalty.status).strip()
    if status not in {value for value, _label in TripPenalty.STATUS_CHOICES}:
        raise ValidationError("Некорректный статус штрафа.")
    penalty.status = status
    penalty.actual_amount = _decimal(request.POST.get("actual_amount"), field_label="фактическая сумма")
    penalty.comment = str(request.POST.get("penalty_comment") or penalty.comment or "").strip()
    penalty.save(update_fields=["status", "actual_amount", "comment", "updated_at"])
    log_trip_event(
        problem.trip,
        event_type="penalty_updated",
        description=f"Штраф «{penalty.penalty_type}»: статус «{penalty.get_status_display()}».",
        user=request.user,
        payload={"problem_id": problem.pk, "penalty_id": penalty.pk, "status": penalty.status},
    )
    notify_employee(
        recipient=problem.assigned_to or problem.trip.assigned_logistician,
        trip=problem.trip,
        title=f"Изменен штраф по рейсу {problem.trip.number}",
        text=f"{penalty.penalty_type}: {penalty.get_status_display()}.",
        detail_url=f"/logistics/problems/{problem.pk}/",
        event="penalty-updated",
    )
    return penalty


def penalty_totals(problem: ProblemTrip) -> dict:
    totals = problem.penalties.aggregate(estimated=Sum("estimated_amount"), actual=Sum("actual_amount"))
    return {"estimated": totals.get("estimated") or Decimal("0"), "actual": totals.get("actual") or Decimal("0")}
