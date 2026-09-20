from __future__ import annotations

from audit.models import OrderAuditEntry, log_order_action

PROCESSING_STAGE_DRAFT = "draft"
PROCESSING_STAGE_AWAITING_APPROVAL = "awaiting_approval"
PROCESSING_STAGE_MANAGER_APPROVED = "manager_approved"
PROCESSING_STAGE_OBR_MOVE_CREATED = "obr_move_created"
PROCESSING_STAGE_OBR_ARRIVED = "obr_arrived"
PROCESSING_STAGE_TAKEN_INTO_PROCESSING = "taken_into_processing"
PROCESSING_STAGE_UNBOXING_OPENED = "unboxing_opened"
PROCESSING_STAGE_UNBOXING_COMPLETED = "unboxing_completed"
PROCESSING_STAGE_QUALITY_CONTROL = "quality_control"
PROCESSING_STAGE_REWORK = "rework"
PROCESSING_STAGE_QUALITY_APPROVED = "quality_approved"
PROCESSING_STAGE_RETURN_TO_STOCK_SENT = "return_to_stock_sent"
PROCESSING_STAGE_RETURNED_TO_STOCK = "returned_to_stock"
PROCESSING_STAGE_DONE = "done"
PROCESSING_STAGE_CANCELLED = "cancelled"

PROCESSING_STAGE_LABELS: dict[str, str] = {
    PROCESSING_STAGE_DRAFT: "Черновик",
    PROCESSING_STAGE_AWAITING_APPROVAL: "Ждет подтверждения",
    PROCESSING_STAGE_MANAGER_APPROVED: "утверждено менеджером",
    PROCESSING_STAGE_OBR_MOVE_CREATED: "создано перемещение в OBR",
    PROCESSING_STAGE_OBR_ARRIVED: "товар прибыл в OBR",
    PROCESSING_STAGE_TAKEN_INTO_PROCESSING: "взято в обработку",
    PROCESSING_STAGE_UNBOXING_OPENED: "открыта раскоробовка",
    PROCESSING_STAGE_UNBOXING_COMPLETED: "раскоробовка завершена",
    PROCESSING_STAGE_QUALITY_CONTROL: "на проверке качества",
    PROCESSING_STAGE_REWORK: "возвращено на доработку",
    PROCESSING_STAGE_QUALITY_APPROVED: "проверка качества пройдена",
    PROCESSING_STAGE_RETURN_TO_STOCK_SENT: "отправлено обратно на склад",
    PROCESSING_STAGE_RETURNED_TO_STOCK: "возвращено на склад",
    PROCESSING_STAGE_DONE: "заявка завершена",
    PROCESSING_STAGE_CANCELLED: "Отменена",
}

PROCESSING_STAGE_RANKS: dict[str, int] = {
    PROCESSING_STAGE_DRAFT: 0,
    PROCESSING_STAGE_AWAITING_APPROVAL: 10,
    PROCESSING_STAGE_MANAGER_APPROVED: 20,
    PROCESSING_STAGE_OBR_MOVE_CREATED: 30,
    PROCESSING_STAGE_OBR_ARRIVED: 40,
    PROCESSING_STAGE_TAKEN_INTO_PROCESSING: 50,
    PROCESSING_STAGE_UNBOXING_OPENED: 60,
    PROCESSING_STAGE_UNBOXING_COMPLETED: 70,
    PROCESSING_STAGE_QUALITY_CONTROL: 72,
    PROCESSING_STAGE_REWORK: 74,
    PROCESSING_STAGE_QUALITY_APPROVED: 76,
    PROCESSING_STAGE_RETURN_TO_STOCK_SENT: 80,
    PROCESSING_STAGE_RETURNED_TO_STOCK: 90,
    PROCESSING_STAGE_DONE: 100,
    PROCESSING_STAGE_CANCELLED: 100,
}

PROCESSING_STAGE_STATUS_VALUES: dict[str, str] = {
    PROCESSING_STAGE_DRAFT: "draft",
    PROCESSING_STAGE_AWAITING_APPROVAL: "sent_unconfirmed",
    PROCESSING_STAGE_MANAGER_APPROVED: "processing_head",
    PROCESSING_STAGE_OBR_MOVE_CREATED: "processing_head",
    PROCESSING_STAGE_OBR_ARRIVED: "processing_head",
    PROCESSING_STAGE_TAKEN_INTO_PROCESSING: "processing_in_work",
    PROCESSING_STAGE_UNBOXING_OPENED: "processing_in_work",
    PROCESSING_STAGE_UNBOXING_COMPLETED: "processing_in_work",
    PROCESSING_STAGE_QUALITY_CONTROL: "processing_in_work",
    PROCESSING_STAGE_REWORK: "processing_in_work",
    PROCESSING_STAGE_QUALITY_APPROVED: "processing_in_work",
    PROCESSING_STAGE_RETURN_TO_STOCK_SENT: "processing_in_work",
    PROCESSING_STAGE_RETURNED_TO_STOCK: "processing_in_work",
    PROCESSING_STAGE_DONE: "done",
    PROCESSING_STAGE_CANCELLED: "cancelled",
}

PROCESSING_STAGE_INITIAL_TRANSITIONS: frozenset[str] = frozenset(
    {
        PROCESSING_STAGE_DRAFT,
        PROCESSING_STAGE_AWAITING_APPROVAL,
    }
)

PROCESSING_STAGE_TRANSITIONS: dict[str, frozenset[str]] = {
    PROCESSING_STAGE_DRAFT: frozenset(
        {
            PROCESSING_STAGE_AWAITING_APPROVAL,
            PROCESSING_STAGE_CANCELLED,
        }
    ),
    PROCESSING_STAGE_AWAITING_APPROVAL: frozenset(
        {
            PROCESSING_STAGE_MANAGER_APPROVED,
            PROCESSING_STAGE_CANCELLED,
        }
    ),
    PROCESSING_STAGE_MANAGER_APPROVED: frozenset(
        {
            PROCESSING_STAGE_OBR_MOVE_CREATED,
            PROCESSING_STAGE_OBR_ARRIVED,
            PROCESSING_STAGE_TAKEN_INTO_PROCESSING,
            PROCESSING_STAGE_UNBOXING_OPENED,
        }
    ),
    PROCESSING_STAGE_OBR_MOVE_CREATED: frozenset(
        {
            PROCESSING_STAGE_OBR_ARRIVED,
            PROCESSING_STAGE_TAKEN_INTO_PROCESSING,
            PROCESSING_STAGE_UNBOXING_OPENED,
        }
    ),
    PROCESSING_STAGE_OBR_ARRIVED: frozenset(
        {
            PROCESSING_STAGE_TAKEN_INTO_PROCESSING,
            PROCESSING_STAGE_UNBOXING_OPENED,
        }
    ),
    PROCESSING_STAGE_TAKEN_INTO_PROCESSING: frozenset(
        {
            PROCESSING_STAGE_UNBOXING_OPENED,
        }
    ),
    PROCESSING_STAGE_UNBOXING_OPENED: frozenset(
        {
            PROCESSING_STAGE_UNBOXING_COMPLETED,
        }
    ),
    PROCESSING_STAGE_UNBOXING_COMPLETED: frozenset(
        {
            PROCESSING_STAGE_QUALITY_CONTROL,
        }
    ),
    PROCESSING_STAGE_QUALITY_CONTROL: frozenset(
        {
            PROCESSING_STAGE_REWORK,
            PROCESSING_STAGE_QUALITY_APPROVED,
        }
    ),
    PROCESSING_STAGE_REWORK: frozenset(
        {
            PROCESSING_STAGE_QUALITY_CONTROL,
        }
    ),
    PROCESSING_STAGE_QUALITY_APPROVED: frozenset(
        {
            PROCESSING_STAGE_RETURN_TO_STOCK_SENT,
            PROCESSING_STAGE_RETURNED_TO_STOCK,
            PROCESSING_STAGE_DONE,
        }
    ),
    PROCESSING_STAGE_RETURN_TO_STOCK_SENT: frozenset(
        {
            PROCESSING_STAGE_RETURNED_TO_STOCK,
        }
    ),
    PROCESSING_STAGE_RETURNED_TO_STOCK: frozenset(
        {
            PROCESSING_STAGE_DONE,
        }
    ),
    PROCESSING_STAGE_DONE: frozenset(),
    PROCESSING_STAGE_CANCELLED: frozenset(),
}

_LEGACY_LABEL_STAGE_PAIRS: tuple[tuple[str, str], ...] = (
    ("разноглас", ""),
    ("заявка завершена", PROCESSING_STAGE_DONE),
    ("выполн", PROCESSING_STAGE_DONE),
    ("отменен", PROCESSING_STAGE_CANCELLED),
    ("отменена", PROCESSING_STAGE_CANCELLED),
    ("проверка качества пройдена", PROCESSING_STAGE_QUALITY_APPROVED),
    ("возвращено на доработку", PROCESSING_STAGE_REWORK),
    ("на проверке качества", PROCESSING_STAGE_QUALITY_CONTROL),
    ("возвращено на склад", PROCESSING_STAGE_RETURNED_TO_STOCK),
    ("перемещено на склад", PROCESSING_STAGE_RETURNED_TO_STOCK),
    ("отправлено обратно на склад", PROCESSING_STAGE_RETURN_TO_STOCK_SENT),
    ("раскоробовка завершена", PROCESSING_STAGE_UNBOXING_COMPLETED),
    ("открыта раскоробовка", PROCESSING_STAGE_UNBOXING_OPENED),
    ("взято в обработку", PROCESSING_STAGE_TAKEN_INTO_PROCESSING),
    ("взята в работу", PROCESSING_STAGE_TAKEN_INTO_PROCESSING),
    ("товар прибыл в obr", PROCESSING_STAGE_OBR_ARRIVED),
    ("создано перемещение в obr", PROCESSING_STAGE_OBR_MOVE_CREATED),
    ("утверждено менеджером", PROCESSING_STAGE_MANAGER_APPROVED),
    ("ждет подтверждения", PROCESSING_STAGE_AWAITING_APPROVAL),
    ("на подтверждении", PROCESSING_STAGE_AWAITING_APPROVAL),
    ("черновик", PROCESSING_STAGE_DRAFT),
)

_LEGACY_STATUS_STAGE_MAP: dict[str, str] = {
    "draft": PROCESSING_STAGE_DRAFT,
    "sent_unconfirmed": PROCESSING_STAGE_AWAITING_APPROVAL,
    "send": PROCESSING_STAGE_AWAITING_APPROVAL,
    "submitted": PROCESSING_STAGE_AWAITING_APPROVAL,
    "processing_head": PROCESSING_STAGE_MANAGER_APPROVED,
    "processing_in_work": PROCESSING_STAGE_TAKEN_INTO_PROCESSING,
    "done": PROCESSING_STAGE_DONE,
    "completed": PROCESSING_STAGE_DONE,
    "closed": PROCESSING_STAGE_DONE,
    "finished": PROCESSING_STAGE_DONE,
    "cancelled": PROCESSING_STAGE_CANCELLED,
    "canceled": PROCESSING_STAGE_CANCELLED,
}


def processing_stage_rank(stage: str | None) -> int:
    return PROCESSING_STAGE_RANKS.get(str(stage or "").strip(), -1)


def processing_stage_from_payload(payload: dict | None) -> str:
    source = payload if isinstance(payload, dict) else {}
    stage_value = str(source.get("processing_stage") or "").strip()
    if stage_value:
        return stage_value
    stage_label = str(source.get("processing_stage_label") or "").strip().lower()
    if stage_label:
        for label_text, candidate_stage in _LEGACY_LABEL_STAGE_PAIRS:
            if candidate_stage and label_text in stage_label:
                return candidate_stage
    status_value = str(source.get("status") or source.get("submit_action") or "").strip().lower()
    if status_value in _LEGACY_STATUS_STAGE_MAP:
        return _LEGACY_STATUS_STAGE_MAP[status_value]
    status_label = str(source.get("status_label") or "").strip().lower()
    for label_text, candidate_stage in _LEGACY_LABEL_STAGE_PAIRS:
        if candidate_stage and label_text in status_label:
            return candidate_stage
    return ""


def processing_is_cancelled(payload: dict | None) -> bool:
    source = payload if isinstance(payload, dict) else {}
    stage_value = processing_stage_from_payload(source)
    if stage_value == PROCESSING_STAGE_CANCELLED:
        return True
    status_value = str(source.get("status") or source.get("submit_action") or "").strip().lower()
    status_label_value = str(source.get("status_label") or "").strip().lower()
    return status_value in {"cancelled", "canceled"} or "отменен" in status_label_value


def processing_stage_label(payload: dict | None, fallback: str | None = None) -> str:
    source = payload if isinstance(payload, dict) else {}
    status_label_value = str(source.get("status_label") or "").strip()
    if processing_is_cancelled(source):
        return PROCESSING_STAGE_LABELS[PROCESSING_STAGE_CANCELLED]
    if "разноглас" in status_label_value.lower():
        return status_label_value
    explicit_label = str(source.get("processing_stage_label") or "").strip()
    if explicit_label:
        return explicit_label
    stage_value = processing_stage_from_payload(source)
    if stage_value:
        return PROCESSING_STAGE_LABELS.get(stage_value, explicit_label or stage_value)
    if status_label_value:
        return status_label_value
    return str(fallback or "").strip()


def apply_processing_stage(payload: dict | None, stage: str, *, status_value: str | None = None) -> dict:
    updated = dict(payload or {})
    updated["processing_stage"] = stage
    updated["processing_stage_label"] = PROCESSING_STAGE_LABELS.get(stage, stage)
    updated["status"] = status_value or PROCESSING_STAGE_STATUS_VALUES.get(stage, updated.get("status") or "")
    updated["status_label"] = updated["processing_stage_label"]
    if stage == PROCESSING_STAGE_DRAFT:
        updated["submit_action"] = "draft"
    elif stage == PROCESSING_STAGE_AWAITING_APPROVAL:
        updated["submit_action"] = "submitted"
    elif stage == PROCESSING_STAGE_CANCELLED:
        updated["submit_action"] = "cancelled"
    return updated


def processing_stage_transition_allowed(payload: dict | None, stage: str) -> bool:
    target_stage = str(stage or "").strip()
    if target_stage not in PROCESSING_STAGE_LABELS:
        return False
    current_stage = processing_stage_from_payload(payload)
    if not current_stage:
        return target_stage in PROCESSING_STAGE_INITIAL_TRANSITIONS
    if current_stage == target_stage:
        return False
    return target_stage in PROCESSING_STAGE_TRANSITIONS.get(current_stage, frozenset())


def processing_stage_transition_error(payload: dict | None, stage: str) -> str:
    target_stage = str(stage or "").strip()
    if target_stage not in PROCESSING_STAGE_LABELS:
        return f"Неизвестный этап обработки: {target_stage or 'не указан'}."
    current_stage = processing_stage_from_payload(payload)
    if not current_stage:
        if target_stage in PROCESSING_STAGE_INITIAL_TRANSITIONS:
            return ""
        return (
            f"Нельзя установить этап «{PROCESSING_STAGE_LABELS[target_stage]}»: "
            "у заявки не определён начальный этап."
        )
    if current_stage == target_stage:
        return ""
    if target_stage in PROCESSING_STAGE_TRANSITIONS.get(current_stage, frozenset()):
        return ""
    current_label = PROCESSING_STAGE_LABELS.get(current_stage, current_stage)
    target_label = PROCESSING_STAGE_LABELS[target_stage]
    return f"Переход «{current_label}» → «{target_label}» запрещён."


def should_advance_processing_stage(payload: dict | None, stage: str) -> bool:
    return processing_stage_transition_allowed(payload, stage)


def log_processing_stage(
    *,
    order_id: str,
    payload: dict | None,
    stage: str,
    description: str,
    user=None,
    agency=None,
    order_type: str = "processing",
    status_value: str | None = None,
    extra_payload: dict | None = None,
) -> tuple[bool, dict]:
    if not should_advance_processing_stage(payload, stage):
        return False, dict(payload or {})
    next_payload = apply_processing_stage(payload, stage, status_value=status_value)
    if extra_payload:
        next_payload.update(extra_payload)
    log_order_action(
        "status",
        order_id=order_id,
        order_type=order_type,
        user=user,
        agency=agency,
        description=description,
        payload=next_payload,
    )
    return True, next_payload


def processing_is_done(payload: dict | None) -> bool:
    if processing_is_cancelled(payload):
        return False
    stage_value = processing_stage_from_payload(payload)
    if stage_value:
        return stage_value == PROCESSING_STAGE_DONE
    status_value = str((payload or {}).get("status") or (payload or {}).get("submit_action") or "").strip().lower()
    status_label_value = str((payload or {}).get("status_label") or "").strip().lower()
    return status_value in {"done", "completed", "closed", "finished"} or "выполн" in status_label_value


def processing_is_waiting_approval(payload: dict | None) -> bool:
    if processing_is_cancelled(payload):
        return False
    return processing_stage_from_payload(payload) == PROCESSING_STAGE_AWAITING_APPROVAL


def processing_is_locked_for_edit(payload: dict | None) -> bool:
    if processing_is_cancelled(payload):
        return True
    stage_value = processing_stage_from_payload(payload)
    return bool(stage_value) and processing_stage_rank(stage_value) >= processing_stage_rank(PROCESSING_STAGE_MANAGER_APPROVED)


def processing_client_can_edit(payload: dict | None) -> bool:
    """Client may directly edit only a draft processing request."""
    if processing_is_cancelled(payload) or processing_is_done(payload):
        return False
    if processing_is_locked_for_edit(payload):
        return False
    stage_value = processing_stage_from_payload(payload)
    if stage_value:
        return stage_value == PROCESSING_STAGE_DRAFT
    data = payload or {}
    status_value = str(data.get("status") or data.get("submit_action") or "").strip().lower()
    status_label = str(data.get("status_label") or "").strip().lower()
    return status_value in {"draft", "client_draft"} or "черновик" in status_label


def processing_is_in_work(payload: dict | None) -> bool:
    if processing_is_cancelled(payload):
        return False
    stage_value = processing_stage_from_payload(payload)
    if stage_value:
        return (
            stage_value != PROCESSING_STAGE_DONE
            and stage_value != PROCESSING_STAGE_CANCELLED
            and processing_stage_rank(stage_value) >= processing_stage_rank(PROCESSING_STAGE_TAKEN_INTO_PROCESSING)
        )
    status_value = str((payload or {}).get("status") or "").strip().lower()
    status_label_value = str((payload or {}).get("status_label") or "").strip().lower()
    return status_value == "processing_in_work" or "взята" in status_label_value


def processing_stage_at_least(payload: dict | None, stage: str) -> bool:
    current_stage = processing_stage_from_payload(payload)
    if not current_stage:
        return False
    return processing_stage_rank(current_stage) >= processing_stage_rank(stage)


def processing_return_to_stock_completed(order_id: str) -> bool:
    order_key = str(order_id or "").strip()
    if not order_key:
        return False
    pallet_codes = _processing_placement_pallet_codes(order_key)
    if not pallet_codes:
        return False
    latest_by_pallet: dict[str, tuple[str, str]] = {}
    entries = OrderAuditEntry.objects.filter(order_type="stock_move").order_by("-created_at", "-id")
    for entry in entries:
        payload = entry.payload or {}
        if str(payload.get("processing_order_id") or "").strip() != order_key:
            continue
        pallet_code = str(payload.get("pallet_code") or "").strip()
        if not pallet_code or pallet_code in latest_by_pallet or pallet_code not in pallet_codes:
            continue
        from_zone = _extract_zone_code(payload.get("from_location"), payload.get("from_zone"))
        to_zone = _extract_zone_code(payload.get("to_location"), payload.get("to_zone"))
        if from_zone and from_zone != "OBR":
            continue
        latest_by_pallet[pallet_code] = (
            str(payload.get("status") or "").strip().lower(),
            to_zone,
        )
        if len(latest_by_pallet) >= len(pallet_codes):
            break
    if len(latest_by_pallet) < len(pallet_codes):
        return False
    return all(status == "done" and zone and zone != "OBR" for status, zone in latest_by_pallet.values())


def _processing_placement_pallet_codes(order_id: str) -> set[str]:
    entries = (
        OrderAuditEntry.objects.filter(order_id=order_id, order_type="processing")
        .order_by("-created_at", "-id")
    )
    for entry in entries:
        payload = entry.payload or {}
        pallets = payload.get("act_pallets") or []
        if str(payload.get("act") or "").strip().lower() != "placement":
            continue
        if not isinstance(pallets, list):
            continue
        codes = {
            str((pallet or {}).get("code") or "").strip()
            for pallet in pallets
            if isinstance(pallet, dict) and str((pallet or {}).get("code") or "").strip()
        }
        if codes:
            return codes
    return set()


def _extract_zone_code(raw_location, raw_zone) -> str:
    if isinstance(raw_location, dict):
        zone_value = raw_location.get("zone") or raw_zone
    else:
        zone_value = raw_zone
    return str(zone_value or "").strip().upper()
