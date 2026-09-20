"""Единый модуль чатов: треды клиента / заявки / внутренние / задачи."""

from __future__ import annotations

from typing import Any

from django.db.models import Count, Q, QuerySet
from django.utils import timezone

from sku.models import Agency

from .models import ChatThread, ClientChatMessage


ORDER_TYPE_LABELS = {
    "receiving": "Приёмка",
    "processing": "Обработка",
    "shipping": "Отгрузка",
    "other": "Прочая заявка",
    "logistics": "Рейс",
}


def _agency_title(agency: Agency) -> str:
    return str(getattr(agency, "short_name", None) or getattr(agency, "agn_name", None) or f"Клиент {agency.id}")


def ensure_client_general_thread(agency: Agency, *, user=None) -> ChatThread | None:
    from .chat_switch import chats_enabled

    if not chats_enabled():
        return None
    if not agency:
        return None
    title = f"Клиент — FullBox · {_agency_title(agency)}"[:255]
    thread, created = ChatThread.objects.get_or_create(
        agency=agency,
        kind=ChatThread.KIND_CLIENT_GENERAL,
        defaults={"title": title, "created_by": user if getattr(user, "is_authenticated", False) else None},
    )
    if not created and not thread.title:
        thread.title = title
        thread.save(update_fields=["title", "updated_at"])
    return thread


def ensure_order_threads(
    *,
    agency: Agency,
    order_type: str,
    order_id: str,
    user=None,
) -> tuple[ChatThread | None, ChatThread | None]:
    """Клиентский + внутренний тред заявки. Складскую логику не трогает."""
    from .chat_switch import chats_enabled

    if not chats_enabled():
        return None, None
    if not agency:
        return None, None
    oid = str(order_id or "").strip()
    otype = str(order_type or "").strip().lower()
    if not oid or not otype:
        return None, None
    type_label = ORDER_TYPE_LABELS.get(otype, otype)
    client_title = f"{type_label} №{oid}"[:255]
    internal_title = f"Внутренний · {type_label} №{oid}"[:255]
    author = user if getattr(user, "is_authenticated", False) else None
    client_thread, _ = ChatThread.objects.get_or_create(
        agency=agency,
        kind=ChatThread.KIND_ORDER_CLIENT,
        order_type=otype,
        order_id=oid,
        defaults={"title": client_title, "created_by": author},
    )
    internal_thread, _ = ChatThread.objects.get_or_create(
        agency=agency,
        kind=ChatThread.KIND_ORDER_INTERNAL,
        order_type=otype,
        order_id=oid,
        defaults={"title": internal_title, "created_by": author},
    )
    return client_thread, internal_thread


def archive_order_threads(*, agency: Agency, order_type: str, order_id: str) -> int:
    oid = str(order_id or "").strip()
    otype = str(order_type or "").strip().lower()
    if not agency or not oid:
        return 0
    return ChatThread.objects.filter(
        agency=agency,
        order_type=otype,
        order_id=oid,
        kind__in=[ChatThread.KIND_ORDER_CLIENT, ChatThread.KIND_ORDER_INTERNAL],
        is_archived=False,
    ).update(is_archived=True, updated_at=timezone.now())


def threads_for_manager(
    *,
    agency_ids: list[int] | None = None,
    kind: str = "",
    q: str = "",
    unread_only: bool = False,
    archived: bool | None = False,
    limit: int = 100,
) -> list[ChatThread]:
    qs: QuerySet = ChatThread.objects.select_related("agency", "pinned_message").annotate(
        unread_staff=Count(
            "messages",
            filter=Q(
                messages__author_role=ClientChatMessage.ROLE_CLIENT,
                messages__is_read_by_staff=False,
                messages__is_deleted=False,
            ),
        )
    )
    if agency_ids is not None:
        # Рейсы мультиклиентские — показываем менеджеру/логисту независимо от якоря agency.
        qs = qs.filter(Q(agency_id__in=agency_ids) | Q(kind=ChatThread.KIND_TRIP))
    if archived is True:
        qs = qs.filter(is_archived=True)
    elif archived is False:
        qs = qs.filter(is_archived=False)
    kind = str(kind or "").strip()
    if kind == "clients":
        qs = qs.filter(kind=ChatThread.KIND_CLIENT_GENERAL)
    elif kind == "orders":
        qs = qs.filter(kind__in=[ChatThread.KIND_ORDER_CLIENT, ChatThread.KIND_ORDER_INTERNAL])
    elif kind == "internal":
        qs = qs.filter(kind=ChatThread.KIND_ORDER_INTERNAL)
    elif kind == "tasks":
        qs = qs.filter(kind=ChatThread.KIND_TASK)
    elif kind in {"logistics", "trips", "trip"}:
        qs = qs.filter(kind=ChatThread.KIND_TRIP)
    elif kind in {"receiving", "processing", "shipping"}:
        qs = qs.filter(order_type=kind)
    elif kind and kind != "all":
        qs = qs.filter(kind=kind)
    query = str(q or "").strip()
    if query:
        qs = qs.filter(
            Q(title__icontains=query)
            | Q(order_id__icontains=query)
            | Q(agency__agn_name__icontains=query)
            | Q(agency__short_name__icontains=query)
            | Q(agency__inn__icontains=query)
            | Q(messages__text__icontains=query)
        ).distinct()
    if unread_only:
        qs = qs.filter(unread_staff__gt=0)
    return list(qs.order_by("-last_message_at", "-updated_at", "-id")[: max(1, int(limit or 100))])


def serialize_thread_card(thread: ChatThread) -> dict[str, Any]:
    last = (
        thread.messages.filter(is_deleted=False)
        .select_related("author")
        .order_by("-created_at", "-id")
        .first()
    )
    unread = int(getattr(thread, "unread_staff", 0) or getattr(thread, "unread_client", 0) or 0)
    if not unread and hasattr(thread, "unread_client"):
        unread = int(thread.unread_client or 0)
    if not unread:
        # Для менеджера — непрочитанные от клиента; для клиента считаем отдельно в threads_for_client.
        unread = thread.messages.filter(
            author_role=ClientChatMessage.ROLE_CLIENT,
            is_read_by_staff=False,
            is_deleted=False,
        ).count()
    agency = thread.agency
    preview = ""
    author = ""
    when = ""
    if last:
        preview = (last.text or "Вложение").strip()[:120]
        if last.is_deleted:
            preview = "Сообщение удалено"
        if last.author_role == ClientChatMessage.ROLE_CLIENT:
            author = "Клиент"
        elif last.author_role == ClientChatMessage.ROLE_SYSTEM:
            author = "Система"
        else:
            author = "FullBox"
        when = timezone.localtime(last.created_at).strftime("%d.%m %H:%M") if last.created_at else ""
    return {
        "id": thread.id,
        "title": thread.title or thread.get_kind_display(),
        "kind": thread.kind,
        "kind_label": thread.get_kind_display(),
        "agency_id": thread.agency_id,
        "agency_name": _agency_title(agency) if agency else "—",
        "order_type": thread.order_type,
        "order_id": thread.order_id,
        "is_archived": thread.is_archived,
        "is_internal": thread.kind in {
            ChatThread.KIND_ORDER_INTERNAL,
            ChatThread.KIND_TRIP,
        },
        "conversation_status": getattr(thread, "conversation_status", "") or "",
        "conversation_status_label": (
            thread.get_conversation_status_display()
            if hasattr(thread, "get_conversation_status_display")
            else ""
        ),
        "unread": unread,
        "preview": preview,
        "preview_author": author,
        "preview_at": when,
        "order_url": _order_url(thread),
        "pinned_message_id": getattr(thread, "pinned_message_id", None),
        "pinned_preview": _pinned_preview(thread),
    }


def _pinned_preview(thread: ChatThread) -> str:
    if not getattr(thread, "pinned_message_id", None):
        return ""
    try:
        text = thread.pinned_message.text if thread.pinned_message else ""
    except Exception:
        text = ""
    return (text or "")[:140]


def _order_url(thread: ChatThread) -> str:
    if not thread.order_id:
        return ""
    otype = thread.order_type or "receiving"
    oid = thread.order_id
    cid = thread.agency_id
    if thread.kind == ChatThread.KIND_TRIP or otype == "logistics":
        return f"/logistics/trips/{oid}/"
    if otype == "shipping":
        return f"/shipping/?client={cid}&q={oid}"
    if otype == "receiving":
        return f"/orders/receiving/{oid}/?client={cid}"
    if otype == "processing":
        return f"/orders/processing/{oid}/?client={cid}"
    return f"/team-manager/orders/?q={oid}"


def get_thread_for_staff(*, thread_id: int, agency_ids: list[int] | None = None) -> ChatThread | None:
    qs = ChatThread.objects.select_related("agency", "pinned_message").filter(pk=thread_id)
    thread = qs.first()
    if not thread:
        return None
    if thread.kind == ChatThread.KIND_TRIP:
        return thread
    if agency_ids is not None and thread.agency_id not in agency_ids:
        return None
    return thread


def threads_for_client(agency, *, limit: int = 50) -> list[ChatThread]:
    """Клиенту доступны только общий чат и клиентские треды заявок."""
    if not agency:
        return []
    ensure_client_general_thread(agency)
    qs = (
        ChatThread.objects.filter(
            agency=agency,
            kind__in=[ChatThread.KIND_CLIENT_GENERAL, ChatThread.KIND_ORDER_CLIENT],
            is_archived=False,
        )
        .annotate(
            unread_client=Count(
                "messages",
                filter=Q(
                    messages__author_role=ClientChatMessage.ROLE_STAFF,
                    messages__is_read_by_client=False,
                    messages__is_deleted=False,
                    messages__visibility__in=[
                        ClientChatMessage.VISIBILITY_CLIENT,
                        ClientChatMessage.VISIBILITY_SYSTEM,
                    ],
                ),
            )
        )
        .order_by("-last_message_at", "-updated_at", "-id")
    )
    return list(qs[: max(1, int(limit or 50))])


def get_thread_for_client(*, agency, thread_id: int) -> ChatThread | None:
    if not agency or not thread_id:
        return None
    return (
        ChatThread.objects.select_related("agency")
        .filter(
            pk=thread_id,
            agency=agency,
            kind__in=[ChatThread.KIND_CLIENT_GENERAL, ChatThread.KIND_ORDER_CLIENT],
        )
        .first()
    )


def threads_for_warehouse(*, limit: int = 120, q: str = "") -> list[ChatThread]:
    """Склад видит внутренние треды заявок и чаты рейсов."""
    qs = ChatThread.objects.select_related("agency", "pinned_message").filter(
        kind__in=[ChatThread.KIND_ORDER_INTERNAL, ChatThread.KIND_TRIP],
        is_archived=False,
    ).annotate(
        unread_staff=Count(
            "messages",
            filter=Q(
                messages__author_role=ClientChatMessage.ROLE_CLIENT,
                messages__is_read_by_staff=False,
                messages__is_deleted=False,
            ),
        )
    )
    query = str(q or "").strip()
    if query:
        qs = qs.filter(
            Q(title__icontains=query)
            | Q(order_id__icontains=query)
            | Q(agency__agn_name__icontains=query)
            | Q(agency__short_name__icontains=query)
            | Q(messages__text__icontains=query)
        ).distinct()
    return list(qs.order_by("-last_message_at", "-updated_at", "-id")[: max(1, int(limit or 120))])
