"""Client-facing visibility rules for LK (display-only, no warehouse writes).

Clients must see coarse lifecycle: draft → waiting manager → in work → act/done.
Warehouse internals and storekeeper/employee names stay out of LK.
"""

from __future__ import annotations

from typing import Any


# Coarse status shown while the request is on the warehouse side.
CLIENT_IN_WORK_LABEL = "В работе"
CLIENT_ACCEPTED_BY_MANAGER = "Менеджер принял заявку в работу"

# Labels that stay as-is for the client (not warehouse internals).
_KEEP_STATUS_TOKENS = (
    "черновик",
    "ждет подтверждения",
    "ждёт подтверждения",
    "на согласовании",
    "ожидает подписи",
    "акт отправлен",
    "выполн",
    "заверш",
    "отмен",
    "разноглас",
    "уточн",
)

# Warehouse-internal status fragments → collapse to «В работе».
_WAREHOUSE_STATUS_TOKENS = (
    "склад",
    "кладовщик",
    "ричтрак",
    "размещен",
    "размещён",
    "размещение",
    "ожидании поставки",
    "в ожидании поставки",
    "взята в работу",
    "принята в работу складом",
    "передана в работу",
    "подготовлена складом",
    "зона отгрузки",
    "otg",
    "приемк",
    "приёмк",
    "обработ",
    "сборк",
    "упаков",
    "погруз",
    "доставка в зону",
)

# Audit descriptions that must never appear in client history/notifications.
_HIDE_DESCRIPTION_TOKENS = (
    "акт размещения",
    "открыт акт",
    "тип товара",
    "режим приемки",
    "режим приёмки",
    "повторное открытие",
    "кладовщик",
    "ричтрак",
    "перемещ",
    "putaway",
    "размещение на складе",
    "обновлен режим",
    "обновлён режим",
)

# Milestones: accepted into work (show once, rewritten).
_ACCEPTED_DESCRIPTION_TOKENS = (
    "отправлено на склад",
    "передана в работу",
    "передано в работу",
    "взята в работу",
    "взял заявку в работу",
    "склад взял",
    "подтверждено менеджером",
)

_MILESTONE_DESCRIPTION_TOKENS = (
    "создан",
    "создана",
    "черновик",
    "подтверждени",
    "согласован",
    "отправлен клиенту",
    "акт отправлен",
    "выполн",
    "заверш",
    "отмен",
    "комментар",
    "вложени",
    "расхождени",
)


def _norm(value: Any) -> str:
    return str(value or "").strip().lower()


def _has_any(text: str, tokens: tuple[str, ...] | list[str]) -> bool:
    return any(token in text for token in tokens)


def client_facing_status_label(status_label: str, *, bucket: str = "") -> str:
    """Collapse warehouse-side labels to a single client-facing status."""
    raw = str(status_label or "").strip()
    if not raw:
        return raw
    label = _norm(raw)
    bucket_key = _norm(bucket)

    if _has_any(label, ("отмена на согласовании",)):
        return "Отмена на согласовании менеджера"
    if "отмен" in label:
        return "ОТМЕНЕНА" if raw.isupper() or raw == "ОТМЕНЕНА" else "Отменена"
    if _has_any(label, ("выполн", "заверш", "закрыт")) and "прием" not in label and "приём" not in label:
        return "Выполнена"
    if _has_any(label, _KEEP_STATUS_TOKENS) and not _has_any(
        label,
        (
            "кладовщик",
            "ричтрак",
            "размещен",
            "размещён",
            "размещение",
            "передана в работу кладовщик",
            "принята в работу складом",
            "подготовлена складом",
            "зона отгрузки",
        ),
    ):
        # Keep manager/client milestones, but still rewrite storekeeper-flavored keepers.
        if _has_any(label, ("ожидает подписи", "акт отправлен", "ждет подтверждения", "ждёт подтверждения", "черновик", "на согласовании", "уточн", "разноглас")):
            return raw
        if _has_any(label, ("выполн", "заверш", "отмен")):
            return raw

    if bucket_key == "warehouse" or _has_any(label, _WAREHOUSE_STATUS_TOKENS):
        return CLIENT_IN_WORK_LABEL
    return raw


def is_warehouse_internal_audit(*, action: str, description: str, payload: dict | None) -> bool:
    """True when the audit row is an internal warehouse step (hide from client)."""
    data = payload if isinstance(payload, dict) else {}
    desc = _norm(description)
    status_label = _norm(data.get("status_label"))
    act = _norm(data.get("act"))
    action_key = _norm(action)

    # Intake milestones are rewritten for the client, not dropped.
    if _has_any(desc, _ACCEPTED_DESCRIPTION_TOKENS):
        return False
    if act == "placement":
        return True
    if data.get("act_sent") and action_key in {"update", "status"}:
        # Act sent to client is visible — not internal.
        return False
    if _has_any(desc, _HIDE_DESCRIPTION_TOKENS):
        return True
    if _has_any(status_label, ("размещение", "размещен", "размещён")):
        return True
    if action_key == "update" and not data.get("notify_client") and not data.get("act_sent"):
        # Generic warehouse updates are not client milestones.
        if _has_any(desc, _ACCEPTED_DESCRIPTION_TOKENS + _MILESTONE_DESCRIPTION_TOKENS):
            return False
        return True
    return False


def is_accepted_into_work_audit(*, action: str, description: str, payload: dict | None) -> bool:
    data = payload if isinstance(payload, dict) else {}
    desc = _norm(description)
    status = _norm(data.get("status") or data.get("submit_action") or data.get("shipping_state"))
    status_label = _norm(data.get("status_label"))
    if _has_any(desc, _ACCEPTED_DESCRIPTION_TOKENS):
        return True
    if status in {"warehouse", "in_work", "processing_in_work", "reserved", "storekeeper_accepted"}:
        return True
    if _has_any(status_label, ("взята в работу", "в работе", "на складе", "передана в работу", "ожидании поставки")):
        # Only treat as "accepted" milestone when description/status suggests intake, not later internals.
        if is_warehouse_internal_audit(action=action, description=description, payload=payload):
            return False
        return True
    return False


def is_client_milestone_audit(*, action: str, description: str, payload: dict | None) -> bool:
    if is_warehouse_internal_audit(action=action, description=description, payload=payload):
        return False
    if is_accepted_into_work_audit(action=action, description=description, payload=payload):
        return True
    data = payload if isinstance(payload, dict) else {}
    desc = _norm(description)
    status = _norm(data.get("status") or data.get("submit_action"))
    action_key = _norm(action)
    if data.get("act_sent"):
        return True
    if status in {"draft", "submitted", "sent_unconfirmed", "cancel_requested", "cancelled", "canceled", "done", "completed", "closed"}:
        return True
    if action_key in {"comment", "attachment"}:
        return True
    if action_key.startswith("shipping_disc") or "расхождени" in desc:
        return True
    if data.get("notify_client") and _has_any(desc, _MILESTONE_DESCRIPTION_TOKENS + _ACCEPTED_DESCRIPTION_TOKENS):
        return True
    if _has_any(desc, _MILESTONE_DESCRIPTION_TOKENS):
        return True
    return False


def build_client_visible_history(entries: list, *, repair_text, format_date) -> list[dict[str, Any]]:
    """Filter/rewrite audit entries for LK request history (no staff names)."""
    rows: list[dict[str, Any]] = []
    seen_accepted = False
    for entry in entries:
        payload = entry.payload if isinstance(getattr(entry, "payload", None), dict) else {}
        action = str(getattr(entry, "action", "") or "")
        description = repair_text(getattr(entry, "description", "") or "")
        if not is_client_milestone_audit(action=action, description=description, payload=payload):
            continue

        status_label = repair_text(payload.get("status_label") or "")
        if is_accepted_into_work_audit(action=action, description=description, payload=payload):
            if seen_accepted:
                continue
            seen_accepted = True
            description = CLIENT_ACCEPTED_BY_MANAGER
            status_label = CLIENT_IN_WORK_LABEL
        else:
            status_label = client_facing_status_label(status_label)

        rows.append(
            {
                "id": getattr(entry, "id", None),
                "action": action,
                "description": description,
                "status_label": status_label,
                "created_at": format_date(getattr(entry, "created_at", None)),
                "user": "",  # never expose storekeeper/warehouse employee names
            }
        )
    return rows


def client_notification_text_for_audit(*, action: str, description: str, payload: dict | None) -> str | None:
    """Return client-safe notification text, or None to skip creating a notification."""
    data = payload if isinstance(payload, dict) else {}
    desc = str(description or "").strip()
    action_key = _norm(action)
    order_type = _norm(data.get("_order_type"))  # optional hint

    if is_warehouse_internal_audit(action=action, description=desc, payload=data):
        return None

    # Shipping discrepancy: never leak storekeeper comments.
    if action_key in {
        "shipping_discrepancy_requested",
        "shipping_disc_substitution",
        "shipping_disc_pick_created",
    } or action_key.startswith("shipping_disc"):
        reason = data.get("missing_text") or data.get("reason") or ""
        reason = str(reason).strip()
        if reason:
            return f"Есть расхождения по отгрузке. {reason}"
        return "Есть расхождения по отгрузке."

    if data.get("act_sent"):
        return "Акт приёмки отправлен вам на подтверждение."

    if is_accepted_into_work_audit(action=action, description=desc, payload=data):
        return CLIENT_ACCEPTED_BY_MANAGER

    status = _norm(data.get("status") or data.get("submit_action"))
    if status in {"cancelled", "canceled"} or "отмен" in _norm(desc):
        if "согласован" in _norm(desc) or status == "cancel_requested":
            return "Запрос на отмену отправлен менеджеру."
        return "Заявка отменена."
    if status in {"done", "completed", "closed"} or _has_any(_norm(desc), ("выполн", "заверш")):
        return "Заявка выполнена."
    if status in {"draft", "submitted", "sent_unconfirmed"} or _has_any(_norm(desc), ("подтверждени", "согласован", "создан")):
        return desc or "Статус заявки обновлён."

    if action_key in {"comment", "attachment"}:
        return desc or ("Добавлен комментарий." if action_key == "comment" else "Добавлено вложение.")

    if data.get("notify_client"):
        # Only pass through if not warehouse-flavored.
        if _has_any(_norm(desc), _HIDE_DESCRIPTION_TOKENS + ("кладовщик", "ричтрак", "склад взял")):
            return CLIENT_ACCEPTED_BY_MANAGER if is_accepted_into_work_audit(action=action, description=desc, payload=data) else None
        return desc or "Статус заявки обновлён."

    # Default: do not notify on arbitrary process status noise.
    if action_key == "status" and is_client_milestone_audit(action=action, description=desc, payload=data):
        return desc or "Статус заявки обновлён."
    return None


def should_create_client_notification_from_audit(
    *,
    order_type: str,
    action: str,
    description: str,
    payload: dict | None,
) -> bool:
    data = dict(payload or {}) if isinstance(payload, dict) else {}
    text = client_notification_text_for_audit(action=action, description=description, payload=data)
    if text is None:
        return False
    action_key = _norm(action)
    otype = _norm(order_type)
    if action_key.startswith("shipping_disc") and otype == "shipping":
        return True
    if data.get("act_sent") and otype == "receiving":
        return True
    if otype == "other" and (
        data.get("notify_client")
        or _norm(data.get("status")) in {"warehouse", "in_work", "completed", "done", "cancelled", "canceled"}
        or action_key in {"attachment", "comment"}
    ):
        return True
    if action_key == "status" and otype in {"receiving", "processing", "packing", "shipping", "other"}:
        return is_client_milestone_audit(action=action, description=description, payload=data)
    if data.get("notify_client"):
        return True
    return False


def is_legacy_warehouse_notification(text: str, title: str = "") -> bool:
    """Hide already-materialized warehouse-noise notifications from the LK feed."""
    blob = _norm(f"{title} {text}")
    if not blob:
        return False
    if _has_any(blob, ("менеджер принял", "принята в работу", "акт приёмки отправлен", "акт приемки отправлен", "расхождени", "выполнена", "отменена", "подтверждени")):
        return False
    return _has_any(
        blob,
        (
            "кладовщик",
            "ричтрак",
            "акт размещения",
            "тип товара",
            "режим приемки",
            "режим приёмки",
            "открыт акт",
            "склад взял",
            "размещение на складе",
        ),
    )


def client_safe_other_status_description(status: str, description: str = "") -> str:
    """Rewrite other-request status texts for client notifications."""
    key = _norm(status)
    if key in {"warehouse", "in_work"}:
        return CLIENT_ACCEPTED_BY_MANAGER
    if key in {"completed", "done"}:
        return "Заявка выполнена."
    if key in {"cancelled", "canceled"}:
        return "Заявка отменена."
    text = str(description or "").strip()
    if _has_any(_norm(text), ("склад", "кладовщик")):
        return CLIENT_ACCEPTED_BY_MANAGER
    return text
