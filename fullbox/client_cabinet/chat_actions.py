"""Редактирование / soft-delete / закрепы / @упоминания (без складской логики)."""

from __future__ import annotations

import re
from datetime import timedelta
from typing import Any

from django.contrib.auth import get_user_model
from django.db.models import Q
from django.utils import timezone

from employees.models import Employee

from .models import ChatMessageAudit, ChatMessageMention, ChatThread, ClientChatMessage

User = get_user_model()

MENTION_RE = re.compile(r"(?<![\w.@])@([\w.\-а-яА-ЯёЁ]{2,40})", re.UNICODE)
CLIENT_EDIT_WINDOW = timedelta(minutes=30)
STAFF_MENTION_ROLES = {
    "manager",
    "logistician",
    "head_manager",
    "director",
    "admin",
    "developer",
    "storekeeper",
}


class ChatActionError(ValueError):
    pass


def _is_staff_user(user) -> bool:
    if not user or not getattr(user, "is_authenticated", False):
        return False
    if getattr(user, "is_staff", False) or getattr(user, "is_superuser", False):
        return True
    return Employee.objects.filter(user=user, is_active=True).exists()


def can_edit_message(message: ClientChatMessage, user) -> bool:
    if not message or message.is_deleted or message.author_role == ClientChatMessage.ROLE_SYSTEM:
        return False
    if not user or not getattr(user, "is_authenticated", False):
        return False
    if message.author_id != getattr(user, "id", None):
        return False
    if message.author_role == ClientChatMessage.ROLE_CLIENT:
        return bool(message.created_at and timezone.now() - message.created_at <= CLIENT_EDIT_WINDOW)
    return True


def can_delete_message(message: ClientChatMessage, user) -> bool:
    if not message or message.is_deleted or message.author_role == ClientChatMessage.ROLE_SYSTEM:
        return False
    if not user or not getattr(user, "is_authenticated", False):
        return False
    if message.author_id == getattr(user, "id", None):
        return True
    # Сотрудник может удалить чужое клиентское/staff сообщение (модерация).
    return _is_staff_user(user)


def can_pin_message(message: ClientChatMessage, user) -> bool:
    if not message or message.is_deleted or not message.thread_id:
        return False
    return _is_staff_user(user)


def _write_audit(*, thread, message, actor, action: str, old_text: str = "", new_text: str = "") -> None:
    ChatMessageAudit.objects.create(
        thread=thread,
        message=message,
        actor=actor if getattr(actor, "is_authenticated", False) else None,
        action=action,
        old_text=(old_text or "")[:8000],
        new_text=(new_text or "")[:8000],
    )


def edit_message(*, message: ClientChatMessage, user, text: str) -> ClientChatMessage:
    if not can_edit_message(message, user):
        raise ChatActionError("Нельзя редактировать это сообщение")
    new_text = str(text or "").strip()
    if not new_text:
        raise ChatActionError("Текст не может быть пустым")
    if new_text == (message.text or ""):
        return message
    old = message.text or ""
    message.text = new_text[:8000]
    message.edited_at = timezone.now()
    message.edited_by = user
    message.save(update_fields=["text", "edited_at", "edited_by"])
    _write_audit(
        thread=message.thread,
        message=message,
        actor=user,
        action=ChatMessageAudit.ACTION_EDIT,
        old_text=old,
        new_text=new_text,
    )
    sync_mentions(message)
    _broadcast(message, "message.edited")
    return message


def soft_delete_message(*, message: ClientChatMessage, user) -> ClientChatMessage:
    if not can_delete_message(message, user):
        raise ChatActionError("Нельзя удалить это сообщение")
    if message.is_deleted:
        return message
    old = message.text or ""
    message.is_deleted = True
    message.deleted_at = timezone.now()
    message.deleted_by = user
    message.save(update_fields=["is_deleted", "deleted_at", "deleted_by"])
    if message.thread_id and message.thread and message.thread.pinned_message_id == message.id:
        pin_message(thread=message.thread, message=None, user=user, silent=True)
    _write_audit(
        thread=message.thread,
        message=message,
        actor=user,
        action=ChatMessageAudit.ACTION_DELETE,
        old_text=old,
        new_text="",
    )
    _broadcast(message, "message.deleted")
    return message


def pin_message(*, thread: ChatThread, message: ClientChatMessage | None, user, silent: bool = False) -> ChatThread:
    if not thread:
        raise ChatActionError("Чат не найден")
    if message is not None:
        if message.thread_id != thread.id:
            raise ChatActionError("Сообщение из другого чата")
        if not can_pin_message(message, user):
            raise ChatActionError("Нельзя закрепить сообщение")
        thread.pinned_message = message
        action = ChatMessageAudit.ACTION_PIN
        new_text = (message.text or "")[:500]
    else:
        if not _is_staff_user(user) and not silent:
            raise ChatActionError("Нельзя открепить")
        thread.pinned_message = None
        action = ChatMessageAudit.ACTION_UNPIN
        new_text = ""
        message = None
    thread.save(update_fields=["pinned_message", "updated_at"])
    if not silent:
        _write_audit(
            thread=thread,
            message=message,
            actor=user,
            action=action,
            old_text="",
            new_text=new_text,
        )
        try:
            from .chat_realtime import broadcast_thread_event
            from .messaging_lk import serialize_chat_message

            payload = {
                "thread_id": thread.id,
                "pinned_message_id": thread.pinned_message_id,
                "pinned": serialize_chat_message(thread.pinned_message, agency_id=thread.agency_id)
                if thread.pinned_message_id
                else None,
            }
            broadcast_thread_event(thread.id, "message.pin", payload)
        except Exception:
            pass
    return thread


def mention_candidates(*, q: str = "", agency=None, limit: int = 20) -> list[dict[str, Any]]:
    query = str(q or "").strip().lstrip("@")
    rows: list[dict[str, Any]] = []
    seen: set[int] = set()

    # Якорь: менеджер клиента
    if agency and getattr(agency, "mened_user_id", None):
        emp = (
            Employee.objects.filter(user_id=agency.mened_user_id, is_active=True)
            .select_related("user")
            .first()
        )
        if emp and emp.user_id:
            label = emp.full_name or emp.user.username
            if not query or query.lower() in label.lower() or query.lower() in "менеджер":
                rows.append(
                    {
                        "user_id": emp.user_id,
                        "label": label,
                        "handle": _handle_from_label(label),
                        "role": "manager",
                    }
                )
                seen.add(emp.user_id)

    qs = Employee.objects.filter(is_active=True, role__in=STAFF_MENTION_ROLES, user__isnull=False).select_related(
        "user"
    )
    if query:
        qs = qs.filter(Q(full_name__icontains=query) | Q(user__username__icontains=query))
    for emp in qs.order_by("full_name")[: max(1, int(limit or 20))]:
        if emp.user_id in seen:
            continue
        label = emp.full_name or emp.user.username
        rows.append(
            {
                "user_id": emp.user_id,
                "label": label,
                "handle": _handle_from_label(label),
                "role": emp.role,
            }
        )
        seen.add(emp.user_id)
    return rows[: max(1, int(limit or 20))]


def _handle_from_label(label: str) -> str:
    raw = str(label or "").strip()
    first = raw.split()[0] if raw else "user"
    return re.sub(r"[^\w.\-а-яА-ЯёЁ]", "", first, flags=re.UNICODE) or "user"


def resolve_mention_token(token: str, *, agency=None) -> User | None:
    key = str(token or "").strip().lstrip("@")
    if not key:
        return None
    low = key.lower()
    if low in {"менеджер", "manager"} and agency and getattr(agency, "mened_user_id", None):
        return User.objects.filter(pk=agency.mened_user_id).first()

    emp = (
        Employee.objects.filter(is_active=True, user__isnull=False)
        .filter(Q(full_name__iexact=key) | Q(user__username__iexact=key) | Q(full_name__istartswith=key + " "))
        .select_related("user")
        .first()
    )
    if emp:
        return emp.user
    # Первое слово ФИО
    for emp in Employee.objects.filter(is_active=True, user__isnull=False).select_related("user")[:300]:
        handle = _handle_from_label(emp.full_name or emp.user.username)
        if handle.lower() == low:
            return emp.user
    return User.objects.filter(username__iexact=key).first()


def sync_mentions(message: ClientChatMessage) -> list[ChatMessageMention]:
    if not message or message.is_deleted:
        return []
    text = message.text or ""
    tokens = {m.group(1) for m in MENTION_RE.finditer(text)}
    ChatMessageMention.objects.filter(message=message).delete()
    created: list[ChatMessageMention] = []
    for token in tokens:
        user = resolve_mention_token(token, agency=message.agency)
        if not user or user.id == message.author_id:
            continue
        obj, _ = ChatMessageMention.objects.get_or_create(
            message=message,
            user=user,
            defaults={"mention_text": token[:64]},
        )
        created.append(obj)
    return created


def _broadcast(message: ClientChatMessage, event_type: str) -> None:
    try:
        from .chat_realtime import broadcast_thread_event
        from .messaging_lk import serialize_chat_message

        if message.thread_id:
            broadcast_thread_event(
                message.thread_id,
                event_type,
                serialize_chat_message(message, agency_id=message.agency_id),
            )
    except Exception:
        pass
