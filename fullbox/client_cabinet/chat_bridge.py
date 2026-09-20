"""Мост audit/заявок → единые чат-треды (без изменения складской логики)."""

from __future__ import annotations

import logging
from datetime import timedelta

from django.utils import timezone

from .chats import archive_order_threads, ensure_order_threads
from .models import ClientChatMessage

logger = logging.getLogger(__name__)

_TERMINAL_STATUSES = {
    "done",
    "completed",
    "closed",
    "cancelled",
    "canceled",
    "otkaz",
}

_LIFECYCLE_ACTIONS = {
    "status",
    "update",
    "close",
    "cancel",
    "attachment",
    "act_sent",
}


def _safe_agency(agency):
    if agency is None:
        return None
    if hasattr(agency, "id"):
        return agency
    return None


def on_order_audit(
    *,
    action: str,
    order_id: str,
    order_type: str = "receiving",
    user=None,
    agency=None,
    description: str = "",
    payload: dict | None = None,
) -> None:
    """Side-effect после OrderAuditEntry: треды, зеркало комментариев, ЖЦ, архив.

    Никогда не должен ломать запись аудита — вызывающий код ловит исключения.
    """
    from .chat_switch import chats_enabled

    if not chats_enabled():
        return
    agency = _safe_agency(agency)
    oid = str(order_id or "").strip()
    otype = str(order_type or "").strip().lower() or "receiving"
    if not agency or not oid:
        return
    if otype == "stock_move":
        return
    data = payload if isinstance(payload, dict) else {}
    if data.get("from_chat") or data.get("skip_chat_bridge"):
        return

    action_key = str(action or "").strip().lower()
    if action_key in {"create", "submit"}:
        _ensure_and_announce(
            agency=agency, order_type=otype, order_id=oid, user=user, description=description
        )
        return

    if action_key == "comment":
        _mirror_comment(
            agency=agency,
            order_type=otype,
            order_id=oid,
            user=user,
            description=description,
            payload=data,
        )
        return

    if action_key in _LIFECYCLE_ACTIONS or action_key.startswith("shipping_disc"):
        _announce_lifecycle(
            agency=agency,
            order_type=otype,
            order_id=oid,
            user=user,
            action=action_key,
            description=description,
            payload=data,
        )
        status = str(data.get("status") or data.get("shipping_state") or "").strip().lower()
        if status in _TERMINAL_STATUSES or any(
            tok in str(description or "").lower() for tok in ("закрыт", "завершен", "завершён", "отмен")
        ):
            archive_order_threads(agency=agency, order_type=otype, order_id=oid)


def _ensure_and_announce(*, agency, order_type: str, order_id: str, user=None, description: str = "") -> None:
    client_thread, internal_thread = ensure_order_threads(
        agency=agency, order_type=order_type, order_id=order_id, user=user
    )
    if not client_thread:
        return
    if ClientChatMessage.objects.filter(
        thread=client_thread,
        author_role=ClientChatMessage.ROLE_SYSTEM,
        text__startswith="Заявка создана",
    ).exists():
        return
    from .messaging_lk import post_chat_message

    text = (description or "").strip() or f"Заявка создана: {client_thread.title}"
    if not text.lower().startswith("заявка"):
        text = f"Заявка создана. {text}"
    post_chat_message(
        agency=agency,
        user=user,
        text=text[:2000],
        author_role=ClientChatMessage.ROLE_SYSTEM,
        thread=client_thread,
        visibility=ClientChatMessage.VISIBILITY_SYSTEM,
    )
    # внутренняя системная метка для склада
    if internal_thread and not ClientChatMessage.objects.filter(
        thread=internal_thread, author_role=ClientChatMessage.ROLE_SYSTEM
    ).exists():
        post_chat_message(
            agency=agency,
            user=user,
            text=f"Внутренний чат заявки {order_id} открыт",
            author_role=ClientChatMessage.ROLE_SYSTEM,
            thread=internal_thread,
            visibility=ClientChatMessage.VISIBILITY_INTERNAL,
        )


def _recent_system_has(thread, text: str, *, minutes: int = 10) -> bool:
    if not thread or not text:
        return False
    since = timezone.now() - timedelta(minutes=minutes)
    return ClientChatMessage.objects.filter(
        thread=thread,
        author_role=ClientChatMessage.ROLE_SYSTEM,
        text=text,
        created_at__gte=since,
        is_deleted=False,
    ).exists()


def _announce_lifecycle(
    *,
    agency,
    order_type: str,
    order_id: str,
    user=None,
    action: str,
    description: str = "",
    payload: dict,
) -> None:
    """Системные события ЖЦ: клиенту — безопасный текст, складу — полный статус."""
    from .client_visibility import (
        CLIENT_ACCEPTED_BY_MANAGER,
        client_facing_status_label,
        client_notification_text_for_audit,
        is_accepted_into_work_audit,
        is_warehouse_internal_audit,
    )
    from .messaging_lk import post_chat_message

    client_thread, internal_thread = ensure_order_threads(
        agency=agency, order_type=order_type, order_id=order_id, user=user
    )
    if not client_thread and not internal_thread:
        return

    data = dict(payload or {})
    data.setdefault("_order_type", order_type)

    client_text = client_notification_text_for_audit(
        action=action, description=description, payload=data
    )
    if client_text and client_thread and not _recent_system_has(client_thread, client_text):
        post_chat_message(
            agency=agency,
            user=user,
            text=str(client_text).strip()[:2000],
            author_role=ClientChatMessage.ROLE_SYSTEM,
            thread=client_thread,
            visibility=ClientChatMessage.VISIBILITY_SYSTEM,
        )

    # Внутренний чат: складские шаги и всё, что клиент не видит «как есть».
    status_raw = str(data.get("status_label") or data.get("status") or "").strip()
    status_label = client_facing_status_label(status_raw) if status_raw else ""
    desc = str(description or "").strip()

    if is_accepted_into_work_audit(action=action, description=description, payload=data):
        internal_text = f"{CLIENT_ACCEPTED_BY_MANAGER}"
        if status_raw and status_raw != CLIENT_ACCEPTED_BY_MANAGER:
            internal_text = f"{CLIENT_ACCEPTED_BY_MANAGER} · {status_raw}"
    elif is_warehouse_internal_audit(action=action, description=description, payload=data):
        internal_text = desc or (f"Статус: {status_raw}" if status_raw else "")
        if status_raw and desc and status_raw.lower() not in desc.lower():
            internal_text = f"{desc} · {status_raw}"
    else:
        # Клиентский milestone — во внутреннем тоже фиксируем (для менеджера/склада).
        internal_text = client_text or desc or (f"Статус: {status_label or status_raw}" if (status_label or status_raw) else "")
        if status_raw and client_text and status_raw not in client_text:
            internal_text = f"{client_text} · {status_raw}"

    if action == "attachment" and not internal_text:
        internal_text = "Добавлено вложение к заявке"

    internal_text = str(internal_text or "").strip()
    if internal_text and internal_thread and not _recent_system_has(internal_thread, internal_text):
        post_chat_message(
            agency=agency,
            user=user,
            text=internal_text[:2000],
            author_role=ClientChatMessage.ROLE_SYSTEM,
            thread=internal_thread,
            visibility=ClientChatMessage.VISIBILITY_INTERNAL,
        )


def _mirror_comment(
    *,
    agency,
    order_type: str,
    order_id: str,
    user=None,
    description: str = "",
    payload: dict,
) -> None:
    text = str(
        payload.get("comment")
        or payload.get("text")
        or payload.get("message")
        or description
        or ""
    ).strip()
    if not text:
        return
    client_thread, internal_thread = ensure_order_threads(
        agency=agency, order_type=order_type, order_id=order_id, user=user
    )
    from .client_visibility import is_warehouse_internal_audit
    from .messaging_lk import post_chat_message, resolve_chat_author_role

    role = resolve_chat_author_role(user, agency)
    internal = bool(payload.get("internal") or payload.get("staff_only"))
    if payload.get("notify_client") or payload.get("visible_to_client") or payload.get("client_visible"):
        internal = False
    elif not internal:
        internal = is_warehouse_internal_audit(
            action="comment", description=description or text, payload=payload
        )
    # Клиент всегда пишет только в клиентский тред заявки.
    if role == ClientChatMessage.ROLE_CLIENT:
        internal = False
    # Сотрудник без явного «клиенту» → внутренний чат (склад/операционка).
    elif role == ClientChatMessage.ROLE_STAFF and not (
        payload.get("notify_client")
        or payload.get("visible_to_client")
        or payload.get("client_visible")
    ):
        internal = True

    thread = internal_thread if internal else client_thread
    if not thread:
        return
    recent = (
        ClientChatMessage.objects.filter(thread=thread, text=text)
        .order_by("-id")
        .first()
    )
    if recent and recent.author_id == getattr(user, "id", None):
        return

    visibility = (
        ClientChatMessage.VISIBILITY_INTERNAL
        if internal
        else ClientChatMessage.VISIBILITY_CLIENT
    )
    post_chat_message(
        agency=agency,
        user=user,
        text=text[:4000],
        author_role=role,
        thread=thread,
        visibility=visibility,
    )


def publish_system_event(
    *,
    agency,
    order_type: str,
    order_id: str,
    text: str,
    visible_to_client: bool = True,
    user=None,
) -> ClientChatMessage | None:
    """Публикация системного события в чат заявки (для явных вызовов из ЛК)."""
    from .chat_switch import chats_enabled

    if not chats_enabled():
        return None
    agency = _safe_agency(agency)
    if not agency or not order_id or not str(text or "").strip():
        return None
    client_thread, internal_thread = ensure_order_threads(
        agency=agency, order_type=order_type, order_id=order_id, user=user
    )
    from .messaging_lk import post_chat_message

    thread = client_thread if visible_to_client else internal_thread
    if not thread:
        return None
    return post_chat_message(
        agency=agency,
        user=user,
        text=str(text).strip()[:2000],
        author_role=ClientChatMessage.ROLE_SYSTEM,
        thread=thread,
        visibility=(
            ClientChatMessage.VISIBILITY_SYSTEM
            if visible_to_client
            else ClientChatMessage.VISIBILITY_INTERNAL
        ),
    )
