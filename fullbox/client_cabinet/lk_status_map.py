from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class LkRequestStatus:
    """Display-only status resolved for client/manager LK request lists."""

    bucket: str
    filter_status: str
    status_pill: str
    cancel_policy: str
    manager_label: str
    status_label: str = ""
    next_step: str = ""
    source: str = ""


STATUS_PILLS = {
    "processing": "В обработке",
    "waiting": "Ожидает",
    "completed": "Завершена",
    "clarification": "Требует уточнения",
    "cancelled": "Отменена",
}

BUCKET_MANAGER_LABELS = {
    "client": "У клиента",
    "manager": "На проверке менеджера",
    "warehouse": "В работе склада",
    "done": "Завершена",
}


def _norm(value: str | None) -> str:
    return str(value or "").strip().lower()


def _status_value(order_type: str | None, payload: dict | None) -> str:
    data = payload if isinstance(payload, dict) else {}
    raw_type = _norm(order_type)
    if raw_type == "shipping":
        if _norm(data.get("status")) == "warehouse_cancel_requested":
            return "warehouse_cancel_requested"
        return _norm(data.get("shipping_state") or data.get("status"))
    return _norm(data.get("status") or data.get("submit_action"))


def _has_any(label: str, *tokens: str) -> bool:
    lowered = _norm(label)
    return any(token in lowered for token in tokens)


def is_terminal_cancelled_status(
    status_label: str | None,
    raw_status: str | None = None,
) -> bool:
    """Return True only for a final cancellation shown in LK.

    Warehouse may publish the final receiving event as ``cancelled`` with the
    user-facing label ``Удалена как ошибочная``.  That label contains neither
    ``отмен`` nor ``уточн``, so older LK mapping incorrectly treated it as a
    waiting request.  This helper is display-only and does not mutate the
    underlying request or warehouse status.
    """

    label = _norm(status_label)
    status = _norm(raw_status)
    if status == "cancel_requested" or "отмена на согласовании" in label:
        return False
    deleted_as_error = (
        "ошибоч" in label
        and ("удален" in label or "удалён" in label)
    )
    return status in {"cancelled", "canceled"} or "отмен" in label or deleted_as_error


def _trip_status_for_shipping(order) -> str:
    try:
        from logistics.models import LogisticsTrip, LogisticsTripOrder

        link = (
            LogisticsTripOrder.objects.select_related("trip")
            .filter(
                shipping_order=order,
                trip__status__in=[
                    LogisticsTrip.STATUS_DRAFT,
                    LogisticsTrip.STATUS_PLANNED,
                    LogisticsTrip.STATUS_LOADING,
                    LogisticsTrip.STATUS_DEPARTED,
                    LogisticsTrip.STATUS_COMPLETED,
                ],
            )
            .order_by("-trip__updated_at", "-trip__created_at", "-id")
            .first()
        )
    except Exception:
        return ""
    return str(getattr(getattr(link, "trip", None), "status", "") or "").strip().lower()


def _shipping_order_for_entry(entry):
    raw_order_id = str(getattr(entry, "order_id", "") or "").strip()
    if not raw_order_id:
        return None
    try:
        from shipping.models import ShippingOrder

        qs = ShippingOrder.objects.all()
        agency_id = getattr(entry, "agency_id", None) or getattr(getattr(entry, "agency", None), "id", None)
        if agency_id:
            qs = qs.filter(agency_id=agency_id)
        order = qs.filter(number=raw_order_id).first()
        if order is None and raw_order_id.isdigit():
            order = qs.filter(pk=int(raw_order_id)).first()
        return order
    except Exception:
        return None


def _shipping_bucket(status_value: str, trip_status: str = "") -> str:
    trip = _norm(trip_status)
    status = _norm(status_value)
    if trip in {"departed", "completed"}:
        return "done"
    if status == "draft":
        return "client"
    if status == "submitted":
        return "manager"
    if status in {"reserved", "storekeeper_accepted", "picking", "packed"}:
        return "warehouse"
    if status in {"shipped", "partial_shipped", "canceled", "cancelled"}:
        return "done"
    return "manager"


def _shipping_fallback_label(status_value: str, trip_status: str = "") -> str:
    trip = _norm(trip_status)
    status = _norm(status_value)
    if trip == "departed":
        return "Загружено в машину"
    if trip == "completed":
        return "Рейс завершен"
    return {
        "draft": "Черновик клиента",
        "submitted": "На согласовании менеджера",
        "reserved": "Согласована и передана в работу кладовщику",
        "storekeeper_accepted": "Принята в работу складом",
        "picking": "Доставка в зону отгрузки (ричтрак)",
        "packed": "Подготовлена складом, ожидает логиста",
        "shipped": "Отгружена",
        "partial_shipped": "Отгружена частично",
        "canceled": "Отменена",
        "cancelled": "Отменена",
    }.get(status, status or "-")


def _bucket_from_resolved_label(
    *,
    order_type: str,
    status_value: str,
    label: str,
    default_bucket: str = "manager",
    is_terminal: bool = False,
    is_ready: bool = False,
) -> str:
    if status_value == "draft" or _has_any(label, "черновик"):
        return "client"
    if status_value == "cancel_requested" or _has_any(label, "отмена на согласовании"):
        return "manager"
    if status_value == "warehouse_cancel_requested" or _has_any(label, "отмена ожидает подтверждения склада"):
        return "warehouse"
    if _has_any(label, "отмен") and status_value != "cancel_requested":
        return "done"
    if is_terminal or status_value in {"done", "completed", "closed", "finished"}:
        return "done"
    if _has_any(label, "выполн", "заверш", "закрыт", "размещен", "размещён"):
        return "done"
    if order_type == "processing" and is_ready:
        return "done"
    if _has_any(label, "отправлен менеджеру", "ожидает подписи менеджера"):
        return "manager"
    if _has_any(label, "акт отправлен клиенту", "отправлен клиенту"):
        return "client"
    if _has_any(label, "ждет подтверждения", "ждёт подтверждения", "согласован"):
        return "manager"
    if _has_any(label, "склад", "прием", "приём", "обработ", "ричтрак", "отбор", "погруз", "доставка", "работ"):
        return "warehouse"
    return default_bucket


def _fallback_label(order_type: str, payload: dict, status_value: str) -> str:
    label = str(payload.get("status_label") or "").strip()
    if status_value == "draft" or _has_any(label, "черновик"):
        return "Черновик"
    if status_value == "cancel_requested" or _has_any(label, "отмена на согласовании"):
        return "Отмена на согласовании менеджера"
    if status_value == "warehouse_cancel_requested" or _has_any(label, "отмена ожидает подтверждения склада"):
        return "Отмена ожидает подтверждения склада"
    if _has_any(label, "отмен"):
        return "Отменена"
    if status_value in {"done", "completed", "closed", "finished"} or _has_any(label, "выполн", "заверш"):
        return "Выполнена"
    if status_value in {"sent_unconfirmed", "send", "submitted"} or _has_any(label, "подтверждени"):
        return "Ждет подтверждения"
    return label or status_value or "-"


def resolve_lk_entry_status(
    entry,
    *,
    audience: str = "client",
    attention: bool = False,
    use_live_warehouse: bool = True,
) -> LkRequestStatus:
    """Resolve a live display status for client and manager LK from one audit entry.

    This is intentionally read-only: it may inspect ShippingOrder and warehouse
    snapshots through existing selectors/resolvers, but never writes stock,
    reserves, tasks, or process statuses.

    For list/kanban paths pass ``use_live_warehouse=False`` to avoid N+1 warehouse
    resolver calls; detail pages keep the live resolver.
    """

    order_type = _norm(getattr(entry, "order_type", "")) or "other"
    if order_type == "packing":
        order_type = "processing"
    payload = getattr(entry, "payload", None)
    payload = payload if isinstance(payload, dict) else {}
    status_value = _status_value(order_type, payload)
    raw_label = str(payload.get("status_label") or "").strip()
    if status_value == "cancel_requested" or _has_any(raw_label, "отмена на согласовании"):
        return resolve_lk_request_status(
            bucket="manager",
            status_label="Отмена на согласовании менеджера",
            attention=attention,
            payload=payload,
            source="audit:cancel_request",
        )
    if status_value == "warehouse_cancel_requested" or _has_any(raw_label, "отмена ожидает подтверждения склада"):
        return resolve_lk_request_status(
            bucket="warehouse",
            status_label="Отмена ожидает подтверждения склада",
            attention=attention,
            payload=payload,
            source="audit:warehouse_cancel_request",
        )
    if status_value == "draft" or _has_any(raw_label, "черновик"):
        return resolve_lk_request_status(
            bucket="client",
            status_label="Черновик",
            attention=attention,
            payload=payload,
            source="audit:draft",
        )

    client_response = _norm(payload.get("act_client_response"))
    act_sent = bool(payload.get("act_sent"))
    storekeeper_signed = bool(payload.get("act_storekeeper_signed"))
    manager_signed = bool(payload.get("act_manager_signed"))
    logistician_signed = bool(payload.get("act_logistician_signed"))
    awaiting_manager_act = (
        (storekeeper_signed or logistician_signed or _has_any(raw_label, "отправлен менеджеру"))
        and not manager_signed
        and not act_sent
        and client_response not in {"confirmed", "dispute"}
    )
    if awaiting_manager_act:
        label = raw_label or "Акт ожидает подписи менеджера"
        mapped = resolve_lk_request_status(
            bucket="manager",
            status_label=label,
            attention=attention,
            payload=payload,
            source="audit:act_manager",
        )
        object.__setattr__(mapped, "next_step", "Подписать акт")
        return mapped

    if client_response == "dispute":
        return resolve_lk_request_status(
            bucket="manager",
            status_label=raw_label or "Разногласия по акту",
            attention=True,
            payload=payload,
            source="audit:act_dispute",
        )

    if act_sent and client_response != "confirmed":
        # Акт у клиента: не помечаем заявку «Выполнена», пока клиент не подтвердил.
        # Для клиента всегда явный статус шага; складской raw_label («Завершена», «В обработке») маскирует действие.
        if audience == "client":
            act_label = "Акт отправлен клиенту"
        else:
            act_label = raw_label or "Акт отправлен клиенту"
            if _has_any(act_label, "выполн", "заверш", "закрыт", "done"):
                act_label = "Акт отправлен клиенту"
        mapped = resolve_lk_request_status(
            bucket="client",
            status_label=act_label,
            attention=attention or audience == "client",
            payload=payload,
            source="audit:act_sent",
        )
        if audience == "client":
            object.__setattr__(mapped, "next_step", "Подтвердить акт")
        return mapped

    if (
        client_response == "confirmed"
        or status_value in {"done", "completed", "closed", "finished"}
        or _has_any(raw_label, "выполн", "заверш", "закрыт")
    ):
        return resolve_lk_request_status(
            bucket="done",
            status_label="Выполнена",
            attention=attention,
            payload=payload,
            source="audit:terminal",
        )
    if order_type == "shipping":
        order = _shipping_order_for_entry(entry)
        if order is not None:
            trip_status = _trip_status_for_shipping(order)
            try:
                from shipping.selectors import shipping_ui_status_label

                label = shipping_ui_status_label(order)
            except Exception:
                label = _shipping_fallback_label(getattr(order, "status", ""), trip_status)
            return resolve_lk_request_status(
                bucket=_shipping_bucket(getattr(order, "status", ""), trip_status),
                status_label=label,
                attention=attention,
                payload={"status": getattr(order, "status", ""), **payload},
                source="shipping:live",
            )
        trip_status = ""
        try:
            raw_order_id = str(getattr(entry, "order_id", "") or "").strip()
            if raw_order_id:
                from . import web_ui as client_web_ui

                agency_id = getattr(entry, "agency_id", None) or getattr(getattr(entry, "agency", None), "id", None)
                trip_status = client_web_ui._shipping_trip_status(raw_order_id, agency_id=agency_id)
        except Exception:
            trip_status = ""
        return resolve_lk_request_status(
            bucket=_shipping_bucket(status_value, trip_status),
            status_label=_shipping_fallback_label(status_value, trip_status),
            attention=attention,
            payload=payload,
            source="shipping:audit",
        )

    if order_type in {"receiving", "processing"} and use_live_warehouse:
        try:
            from sklad.services import WarehouseGoodsStateResolver

            resolver = (
                WarehouseGoodsStateResolver.resolve_for_receiving_order
                if order_type == "receiving"
                else WarehouseGoodsStateResolver.resolve_for_processing_order
            )
            resolved = resolver(
                order_id=str(getattr(entry, "order_id", "") or ""),
                agency=getattr(entry, "agency", None),
                payload=payload,
            )
            label = str(resolved.label_for(audience) or resolved.label_for("default") or "").strip()
            bucket = _bucket_from_resolved_label(
                order_type=order_type,
                status_value=status_value,
                label=label,
                default_bucket="warehouse",
                is_terminal=bool(getattr(resolved, "is_terminal", False)),
                is_ready=bool(getattr(resolved, "is_ready_for_next_step", False)),
            )
            mapped = resolve_lk_request_status(
                bucket=bucket,
                status_label=label,
                attention=attention,
                payload=payload,
                source=f"warehouse:{order_type}",
            )
            object.__setattr__(
                mapped,
                "next_step",
                str(resolved.next_step_for(audience) or resolved.next_step_for("default") or ""),
            )
            return mapped
        except Exception:
            pass

    label = _fallback_label(order_type, payload, status_value)
    bucket = _bucket_from_resolved_label(
        order_type=order_type,
        status_value=status_value,
        label=label,
        default_bucket="manager",
    )
    return resolve_lk_request_status(
        bucket=bucket,
        status_label=label,
        attention=attention,
        payload=payload,
        source="audit:fallback",
    )


def resolve_lk_request_status(
    *,
    bucket: str,
    status_label: str,
    attention: bool = False,
    payload: dict | None = None,
    source: str = "",
) -> LkRequestStatus:
    """Resolve how an existing internal request status is shown in LK.

    This helper does not mutate warehouse/order state and deliberately works only
    with already-computed bucket/label values.
    """

    bucket_key = _norm(bucket) or "manager"
    label = _norm(status_label)
    data = payload if isinstance(payload, dict) else {}
    raw_status = _norm(data.get("status") or data.get("submit_action") or data.get("shipping_state"))

    cancel_pending = raw_status == "cancel_requested" or "отмена на согласовании" in label
    is_cancelled = is_terminal_cancelled_status(label, raw_status)
    is_completed = bucket_key == "done" or any(token in label for token in ("выполн", "заверш", "закрыт"))

    if is_cancelled:
        filter_status = "cancelled"
    elif attention or "уточн" in label:
        filter_status = "clarification"
    elif is_completed:
        filter_status = "completed"
    elif bucket_key == "warehouse":
        filter_status = "processing"
    else:
        filter_status = "waiting"

    # После приёмки складом / на подписи акта клиент не отменяет сам —
    # только через менеджера (кнопку в ЛК скрываем).
    locked_for_client_cancel = _has_any(
        label,
        "ожидает подписи",
        "отправлен менеджеру",
        "принято складом",
        "принята складом",
    )

    if cancel_pending or is_cancelled or is_completed or locked_for_client_cancel:
        cancel_policy = "none"
    elif bucket_key == "warehouse":
        cancel_policy = "manager_approval"
    elif bucket_key in {"client", "manager"}:
        cancel_policy = "direct"
    else:
        cancel_policy = "none"

    return LkRequestStatus(
        bucket=bucket_key,
        filter_status=filter_status,
        status_pill=STATUS_PILLS.get(filter_status, STATUS_PILLS["waiting"]),
        cancel_policy=cancel_policy,
        manager_label=BUCKET_MANAGER_LABELS.get(bucket_key, BUCKET_MANAGER_LABELS["manager"]),
        status_label=str(status_label or "").strip(),
        source=source,
    )
