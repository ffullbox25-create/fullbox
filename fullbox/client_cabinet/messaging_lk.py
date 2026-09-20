"""Client LK notifications + chat helpers."""

from __future__ import annotations

from typing import Any

from django.db.models import Q
from django.utils import timezone

from audit.models import OrderAuditEntry

from .client_visibility import (
    client_notification_text_for_audit,
    is_legacy_warehouse_notification,
    should_create_client_notification_from_audit,
)
from .lk_requests import notification_detail_url
from .chats import ensure_client_general_thread
from .models import (
    ChatMessageReaction,
    ChatMessageReadReceipt,
    ChatNotificationPreference,
    ChatThread,
    ClientChatAttachment,
    ClientChatMessage,
    ClientNotification,
)
from .other_requests import ORDER_TYPE as OTHER_ORDER_TYPE


SHIPPING_DISCREPANCY_ACTIONS = {
    "shipping_discrepancy_requested",
    "shipping_disc_substitution",
    "shipping_disc_pick_created",
}


def create_notification(
    *,
    agency,
    title: str,
    text: str = "",
    notif_type: str = ClientNotification.TYPE_SYSTEM,
    detail_url: str = "",
    priority: str = "normal",
    source_key: str = "",
) -> ClientNotification | None:
    if not agency:
        return None
    key = str(source_key or "").strip()
    if key:
        existing = ClientNotification.objects.filter(agency=agency, source_key=key).first()
        if existing:
            return existing
    return ClientNotification.objects.create(
        agency=agency,
        title=(title or "Уведомление")[:255],
        text=str(text or "").strip(),
        notif_type=notif_type or ClientNotification.TYPE_SYSTEM,
        detail_url=str(detail_url or "")[:512],
        priority=priority or "normal",
        source_key=key,
    )


def sync_notifications_from_audit(agency, *, limit: int = 80) -> int:
    """Materialize client-safe audit milestones into ClientNotification rows."""
    from . import web_ui as client_web_ui

    if not agency:
        return 0
    created = 0
    entries = list(
        OrderAuditEntry.objects.filter(agency=agency)
        .exclude(description="")
        .order_by("-created_at")[:limit]
    )
    source_keys = [f"audit:{entry.id}" for entry in entries]
    existing_source_keys = set(
        ClientNotification.objects.filter(
            agency=agency,
            source_key__in=source_keys,
        ).values_list("source_key", flat=True)
    )
    for entry in entries:
        payload = dict(entry.payload) if isinstance(entry.payload, dict) else {}
        action = str(entry.action or "").lower()
        order_type = str(entry.order_type or "").strip().lower()
        if not should_create_client_notification_from_audit(
            order_type=order_type,
            action=action,
            description=entry.description or "",
            payload=payload,
        ):
            continue
        text = client_notification_text_for_audit(
            action=action,
            description=entry.description or "",
            payload=payload,
        )
        if not text:
            continue
        source_key = f"audit:{entry.id}"
        if source_key in existing_source_keys:
            continue

        status = str(payload.get("status") or "").lower()
        notify = bool(payload.get("notify_client"))
        is_act = order_type == "receiving" and bool(payload.get("act_sent"))
        is_shipping_discrepancy = order_type == "shipping" and action in SHIPPING_DISCREPANCY_ACTIONS

        if is_shipping_discrepancy:
            title = client_web_ui._repair_mojibake_text(
                payload.get("order_display_title")
                or payload.get("order_title")
                or f"Заявка на отгрузку №{entry.order_id}"
            )
            notif_type = ClientNotification.TYPE_STATUS
            priority = "high"
        else:
            act_sent = payload.get("act_sent")
            act_title = ""
            if isinstance(act_sent, str) and act_sent.strip() and act_sent.strip().lower() not in {"true", "1", "yes"}:
                act_title = act_sent.strip()
            type_titles = {
                "receiving": f"Заявка на приёмку №{entry.order_id}",
                "processing": f"Заявка на обработку №{entry.order_id}",
                "packing": f"Заявка на обработку №{entry.order_id}",
                "shipping": f"Заявка на отгрузку №{entry.order_id}",
                OTHER_ORDER_TYPE: f"Обращение №{entry.order_id}",
            }
            title = client_web_ui._repair_mojibake_text(
                payload.get("order_title")
                or payload.get("order_display_title")
                or payload.get("category_label")
                or act_title
                or type_titles.get(order_type)
                or f"Заявка {entry.order_id}"
            )
            text = client_web_ui._format_message_text(text) or client_web_ui._repair_mojibake_text(text)
            notif_type = (
                ClientNotification.TYPE_STATUS
                if action == "status" or is_act or is_shipping_discrepancy
                else ClientNotification.TYPE_COMMENT
            )
            priority = "high" if notify or status in {"in_work", "completed", "done"} or is_act else "normal"

        create_notification(
            agency=agency,
            title=title,
            text=text,
            notif_type=notif_type,
            detail_url=notification_detail_url(
                order_type=entry.order_type,
                order_id=entry.order_id,
                agency_id=agency.id,
            ),
            priority=priority,
            source_key=source_key,
        )
        existing_source_keys.add(source_key)
        created += 1
    return created


def list_notifications(
    agency,
    *,
    limit: int = 40,
    unread_only: bool = False,
    sync: bool = True,
) -> list[dict[str, Any]]:
    if not agency:
        return []
    if sync:
        sync_notifications_from_audit(agency)
    qs = ClientNotification.objects.filter(agency=agency).order_by("-created_at", "-id")
    if unread_only:
        qs = qs.filter(is_read=False)
    rows = []
    for item in qs:
        if is_legacy_warehouse_notification(item.text or "", item.title or ""):
            continue
        rows.append(
            {
                "id": f"notif-{item.id}",
                "db_id": item.id,
                "created_at": item.created_at,
                "title": item.title,
                "text": item.text,
                "message": item.text,
                "type": item.notif_type,
                "priority": item.priority,
                "read": item.is_read,
                "is_read": item.is_read,
                "detail_url": item.detail_url or "#/notifications",
            }
        )
        if len(rows) >= limit:
            break
    return rows


def unread_notifications_count(agency, *, sync: bool = True) -> int:
    if not agency:
        return 0
    if sync:
        sync_notifications_from_audit(agency, limit=40)
    count = 0
    for item in ClientNotification.objects.filter(agency=agency, is_read=False).only("title", "text")[:200]:
        if is_legacy_warehouse_notification(item.text or "", item.title or ""):
            continue
        count += 1
    return count


def mark_notifications_read(agency, *, ids: list[int] | None = None, all_items: bool = False) -> int:
    if not agency:
        return 0
    qs = ClientNotification.objects.filter(agency=agency, is_read=False)
    if all_items:
        pass
    elif ids:
        qs = qs.filter(id__in=ids)
    else:
        return 0
    return qs.update(is_read=True, read_at=timezone.now())


def _author_label(message: ClientChatMessage) -> str:
    if message.author_role == ClientChatMessage.ROLE_STAFF:
        return "FullBox"
    if message.author_id and getattr(message.author, "username", None):
        return str(message.author.username)
    return "Клиент"


def _delivery_status(message: ClientChatMessage) -> str:
    if message.is_deleted:
        return "deleted"
    if message.author_role == ClientChatMessage.ROLE_CLIENT:
        if message.is_read_by_staff:
            return "read"
        if message.delivered_at:
            return "delivered"
        return "sent"
    if message.author_role == ClientChatMessage.ROLE_STAFF:
        if message.visibility == ClientChatMessage.VISIBILITY_INTERNAL:
            if message.is_read_by_staff:
                return "read"
            return "delivered" if message.delivered_at else "sent"
        if message.is_read_by_client:
            return "read"
        if message.delivered_at:
            return "delivered"
        return "sent"
    return "sent"


def serialize_chat_message(message: ClientChatMessage, *, agency_id=None) -> dict[str, Any]:
    from .chat_files import serialize_chat_attachment

    attachments = []
    if not message.is_deleted:
        for attachment in message.attachments.all():
            attachments.append(serialize_chat_attachment(attachment, agency_id=agency_id))
    reactions: dict[str, list[str]] = {}
    for reaction in message.reactions.all():
        reactions.setdefault(reaction.emoji, [])
        label = getattr(reaction.user, "username", None) or str(reaction.user_id)
        reactions[reaction.emoji].append(str(label))
    text = "Сообщение удалено" if message.is_deleted else message.text
    reply = None
    if message.reply_to_id and message.reply_to:
        reply = {
            "id": message.reply_to_id,
            "text": (message.reply_to.text or "")[:160],
            "author": _author_label(message.reply_to),
        }
    mentions = []
    if not message.is_deleted:
        for mention in message.mentions.select_related("user").all():
            mentions.append(
                {
                    "user_id": mention.user_id,
                    "label": getattr(mention.user, "username", "") or str(mention.user_id),
                    "mention_text": mention.mention_text,
                }
            )
    pinned = bool(
        message.thread_id
        and getattr(message.thread, "pinned_message_id", None) == message.id
    )
    return {
        "id": message.id,
        "public_id": str(getattr(message, "public_id", "") or ""),
        "thread_id": message.thread_id,
        "author": _author_label(message),
        "author_id": message.author_id,
        "author_role": message.author_role,
        "visibility": getattr(message, "visibility", ClientChatMessage.VISIBILITY_CLIENT),
        "is_internal": getattr(message, "visibility", "") == ClientChatMessage.VISIBILITY_INTERNAL,
        "text": text,
        "is_deleted": bool(message.is_deleted),
        "is_edited": bool(getattr(message, "edited_at", None)),
        "edited_at": (
            timezone.localtime(message.edited_at).strftime("%d.%m.%Y, %H:%M")
            if getattr(message, "edited_at", None)
            else ""
        ),
        "is_pinned": pinned,
        "mentions": mentions,
        "created_at": timezone.localtime(message.created_at).strftime("%d.%m.%Y, %H:%M") if message.created_at else "",
        "created_at_iso": message.created_at.isoformat() if message.created_at else "",
        "is_read_by_client": message.is_read_by_client,
        "is_read_by_staff": message.is_read_by_staff,
        "delivery_status": _delivery_status(message),
        "reactions": [{"emoji": emoji, "count": len(users), "users": users} for emoji, users in reactions.items()],
        "reply_to": reply,
        "attachments": attachments,
        "detail_url": f"#/chat?thread={message.thread_id}" if message.thread_id else "#/chat",
    }


def list_chat_messages(
    agency,
    *,
    limit: int = 100,
    thread: ChatThread | None = None,
    for_client: bool = True,
    since_id: int | None = None,
) -> list[dict[str, Any]]:
    if not agency:
        return []
    thread = thread or ensure_client_general_thread(agency)
    qs = (
        ClientChatMessage.objects.filter(agency=agency)
        .select_related("author", "reply_to", "reply_to__author", "thread")
        .prefetch_related("attachments", "reactions__user", "mentions__user")
    )
    if thread:
        # Общий чат клиента: подхватываем старые сообщения без thread_id.
        if thread.kind == ChatThread.KIND_CLIENT_GENERAL:
            qs = qs.filter(Q(thread=thread) | Q(thread__isnull=True))
        else:
            qs = qs.filter(thread=thread)
    if for_client:
        qs = qs.filter(
            visibility__in=[
                ClientChatMessage.VISIBILITY_CLIENT,
                ClientChatMessage.VISIBILITY_SYSTEM,
            ]
        )
    if since_id:
        rows = list(qs.filter(id__gt=int(since_id)).order_by("created_at", "id")[: max(1, int(limit or 100))])
        return [serialize_chat_message(item, agency_id=agency.id) for item in rows]
    # Берём последние N сообщений, затем отдаём в хронологическом порядке.
    recent = list(qs.order_by("-created_at", "-id")[: max(1, int(limit or 100))])
    recent.reverse()
    return [serialize_chat_message(item, agency_id=agency.id) for item in recent]


def unread_chat_count_for_client(agency) -> int:
    if not agency:
        return 0
    return ClientChatMessage.objects.filter(
        agency=agency,
        author_role=ClientChatMessage.ROLE_STAFF,
        is_read_by_client=False,
        is_deleted=False,
        visibility__in=[ClientChatMessage.VISIBILITY_CLIENT, ClientChatMessage.VISIBILITY_SYSTEM],
    ).filter(
        Q(thread__isnull=True)
        | Q(thread__kind__in=[ChatThread.KIND_CLIENT_GENERAL, ChatThread.KIND_ORDER_CLIENT])
    ).count()


def _write_receipts(messages, user) -> None:
    if not user or not getattr(user, "is_authenticated", False) or not messages:
        return
    now = timezone.now()
    for msg in messages:
        ChatMessageReadReceipt.objects.get_or_create(message=msg, user=user, defaults={"read_at": now})
        if not msg.delivered_at:
            ClientChatMessage.objects.filter(pk=msg.pk, delivered_at__isnull=True).update(delivered_at=now)


def mark_chat_read_for_client(agency, *, thread: ChatThread | None = None, user=None) -> int:
    if not agency:
        return 0
    qs = ClientChatMessage.objects.filter(
        agency=agency,
        author_role=ClientChatMessage.ROLE_STAFF,
        is_read_by_client=False,
        visibility__in=[ClientChatMessage.VISIBILITY_CLIENT, ClientChatMessage.VISIBILITY_SYSTEM],
    )
    if thread is not None:
        if thread.kind == ChatThread.KIND_CLIENT_GENERAL:
            qs = qs.filter(Q(thread=thread) | Q(thread__isnull=True))
        else:
            qs = qs.filter(thread=thread)
    else:
        qs = qs.filter(
            Q(thread__isnull=True)
            | Q(thread__kind__in=[ChatThread.KIND_CLIENT_GENERAL, ChatThread.KIND_ORDER_CLIENT])
        )
    rows = list(qs[:500])
    _write_receipts(rows, user)
    count = qs.update(is_read_by_client=True)
    if count and thread:
        from .chat_realtime import broadcast_thread_event

        broadcast_thread_event(
            thread.id,
            "message.read",
            {"thread_id": thread.id, "count": count, "by": "client"},
        )
    return count


def mark_chat_read_for_staff(agency=None, *, thread: ChatThread | None = None, user=None) -> int:
    qs = ClientChatMessage.objects.filter(
        author_role=ClientChatMessage.ROLE_CLIENT,
        is_read_by_staff=False,
        is_deleted=False,
    )
    if thread is not None:
        qs = qs.filter(thread=thread)
    elif agency is not None:
        qs = qs.filter(agency=agency)
    else:
        return 0
    rows = list(qs[:500])
    _write_receipts(rows, user)
    count = qs.update(is_read_by_staff=True)
    if count and thread:
        from .chat_realtime import broadcast_thread_event

        broadcast_thread_event(
            thread.id,
            "message.read",
            {"thread_id": thread.id, "count": count, "by": "staff"},
        )
    return count


def resolve_chat_author_role(user, agency) -> str:
    if not user or not getattr(user, "is_authenticated", False):
        return ClientChatMessage.ROLE_CLIENT
    if getattr(agency, "portal_user_id", None) and agency.portal_user_id == user.id:
        return ClientChatMessage.ROLE_CLIENT
    if getattr(user, "is_staff", False) or getattr(user, "is_superuser", False):
        return ClientChatMessage.ROLE_STAFF
    try:
        from employees.models import Employee

        if Employee.objects.filter(user=user, is_active=True).exists():
            return ClientChatMessage.ROLE_STAFF
    except Exception:
        pass
    return ClientChatMessage.ROLE_CLIENT


def _update_conversation_status(thread: ChatThread | None, *, role: str, visibility: str) -> None:
    if not thread or role == ClientChatMessage.ROLE_SYSTEM:
        return
    if thread.kind == ChatThread.KIND_ORDER_INTERNAL or visibility == ClientChatMessage.VISIBILITY_INTERNAL:
        status = ChatThread.STATUS_WAIT_WAREHOUSE if role == ClientChatMessage.ROLE_STAFF else ChatThread.STATUS_IN_PROGRESS
    elif role == ClientChatMessage.ROLE_CLIENT:
        status = ChatThread.STATUS_NEEDS_STAFF
    else:
        status = ChatThread.STATUS_WAIT_CLIENT
    ChatThread.objects.filter(pk=thread.pk).update(
        conversation_status=status,
        is_archived=False,
        updated_at=timezone.now(),
    )


def post_chat_message(
    *,
    agency,
    user=None,
    text: str = "",
    files=None,
    author_role: str | None = None,
    thread: ChatThread | None = None,
    visibility: str | None = None,
    idempotency_key: str = "",
    reply_to_id: int | None = None,
) -> ClientChatMessage | None:
    from .chat_switch import chats_enabled

    if not chats_enabled():
        return None
    if not agency:
        return None
    from .chat_files import ChatFileError, validate_chat_uploads

    message_text = str(text or "").strip()
    try:
        files = validate_chat_uploads(files)
    except ChatFileError:
        raise
    if not message_text and not files:
        return None
    role = author_role or resolve_chat_author_role(user, agency)
    thread = thread or ensure_client_general_thread(agency, user=user)
    key = str(idempotency_key or "").strip()[:64]
    if key and thread and user and getattr(user, "is_authenticated", False):
        existing = ClientChatMessage.objects.filter(
            thread=thread, author=user, idempotency_key=key
        ).first()
        if existing:
            return existing
    if visibility is None:
        if role == ClientChatMessage.ROLE_SYSTEM:
            visibility = ClientChatMessage.VISIBILITY_SYSTEM
        elif thread and thread.kind == ChatThread.KIND_ORDER_INTERNAL:
            visibility = ClientChatMessage.VISIBILITY_INTERNAL
        else:
            visibility = ClientChatMessage.VISIBILITY_CLIENT
    # Клиент не может писать во внутренний тред и не может ставить internal.
    if role == ClientChatMessage.ROLE_CLIENT:
        if thread and thread.kind == ChatThread.KIND_ORDER_INTERNAL:
            return None
        visibility = ClientChatMessage.VISIBILITY_CLIENT
    reply_to = None
    if reply_to_id:
        reply_to = ClientChatMessage.objects.filter(pk=reply_to_id, agency=agency).first()
    message = ClientChatMessage.objects.create(
        agency=agency,
        thread=thread,
        author=user if getattr(user, "is_authenticated", False) else None,
        author_role=role,
        visibility=visibility,
        text=message_text,
        idempotency_key=key,
        reply_to=reply_to,
        delivered_at=timezone.now(),
        is_read_by_client=role == ClientChatMessage.ROLE_CLIENT
        or visibility == ClientChatMessage.VISIBILITY_INTERNAL,
        is_read_by_staff=role == ClientChatMessage.ROLE_STAFF,
    )
    for uploaded in files:
        ClientChatAttachment.objects.create(message=message, file=uploaded)
    try:
        from .chat_actions import sync_mentions

        sync_mentions(message)
    except Exception:
        pass
    try:
        from .chat_telegram import notify_telegram_about_message

        notify_telegram_about_message(message)
    except Exception:
        pass
    if thread is not None:
        ChatThread.objects.filter(pk=thread.pk).update(
            last_message_at=message.created_at or timezone.now(),
            updated_at=timezone.now(),
        )
        try:
            from .chat_tasks import reopen_if_needed

            reopen_if_needed(thread, role=role)
        except Exception:
            pass
        _update_conversation_status(thread, role=role, visibility=visibility)
    # Notify the other side (только клиент-видимые)
    if role == ClientChatMessage.ROLE_STAFF and visibility == ClientChatMessage.VISIBILITY_CLIENT:
        create_notification(
            agency=agency,
            title="Новое сообщение от FullBox",
            text=message_text or "Во вложении файл",
            notif_type=ClientNotification.TYPE_CHAT,
            detail_url=f"#/chat?thread={thread.id}" if thread else "#/chat",
            priority="high",
            source_key=f"chat-msg:{message.id}",
        )
    try:
        from .chat_realtime import broadcast_agency_event, broadcast_thread_event

        payload = serialize_chat_message(message, agency_id=agency.id)
        if thread:
            broadcast_thread_event(thread.id, "message.new", payload)
        client_visible = visibility in {
            ClientChatMessage.VISIBILITY_CLIENT,
            ClientChatMessage.VISIBILITY_SYSTEM,
        }
        if client_visible:
            broadcast_agency_event(agency.id, client_side=True, event_type="message.new", payload=payload)
        broadcast_agency_event(agency.id, client_side=False, event_type="message.new", payload=payload)
    except Exception:
        pass
    return message


def toggle_message_reaction(*, message: ClientChatMessage, user, emoji: str) -> dict[str, Any]:
    emoji = str(emoji or "").strip()[:16]
    if not message or not user or not emoji:
        return {"reactions": []}
    existing = ChatMessageReaction.objects.filter(message=message, user=user, emoji=emoji).first()
    if existing:
        existing.delete()
    else:
        ChatMessageReaction.objects.create(message=message, user=user, emoji=emoji)
    message = (
        ClientChatMessage.objects.filter(pk=message.pk)
        .prefetch_related("reactions__user", "attachments")
        .select_related("author", "reply_to")
        .first()
    )
    data = serialize_chat_message(message, agency_id=message.agency_id)
    if message.thread_id:
        from .chat_realtime import broadcast_thread_event

        broadcast_thread_event(message.thread_id, "reaction.set", data)
    return data


def get_or_create_chat_prefs(user) -> ChatNotificationPreference | None:
    if not user or not getattr(user, "is_authenticated", False):
        return None
    pref, _ = ChatNotificationPreference.objects.get_or_create(user=user)
    return pref


def get_chat_attachment(*, attachment_id: int, agency=None):
    attachment = (
        ClientChatAttachment.objects.select_related("message", "message__agency")
        .filter(pk=attachment_id)
        .first()
    )
    if not attachment:
        return None
    if agency and attachment.message.agency_id != agency.id:
        return None
    # Клиент не скачивает internal вложения.
    if agency and attachment.message.visibility == ClientChatMessage.VISIBILITY_INTERNAL:
        return None
    return attachment
