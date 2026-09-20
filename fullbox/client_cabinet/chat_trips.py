"""Чаты по логистическим рейсам (внутренние, без клиентского ЛК)."""

from __future__ import annotations

from django.utils import timezone

from sku.models import Agency

from .messaging_lk import post_chat_message
from .models import ChatThread, ClientChatMessage

TRIP_ORDER_TYPE = "logistics"


def _trip_anchor_agency(trip) -> Agency | None:
    """Якорный клиент для FK треда: первая заявка рейса (рейс мультиклиентский)."""
    try:
        link = (
            trip.orders.select_related("shipping_order__agency")
            .order_by("id")
            .first()
        )
    except Exception:
        link = None
    order = getattr(link, "shipping_order", None) if link else None
    agency = getattr(order, "agency", None) if order else None
    if agency:
        return agency
    return Agency.objects.filter(archived=False).order_by("id").first()


def _trip_public_number(trip) -> str:
    number = str(getattr(trip, "number", "") or "").strip()
    if number:
        return number
    return f"#{getattr(trip, 'pk', '?')}"


def ensure_trip_thread(trip, *, user=None) -> ChatThread | None:
    from .chat_switch import chats_enabled

    if not chats_enabled():
        return None
    if not trip or not getattr(trip, "pk", None):
        return None
    oid = str(trip.pk)
    existing = ChatThread.objects.filter(
        kind=ChatThread.KIND_TRIP,
        order_type=TRIP_ORDER_TYPE,
        order_id=oid,
    ).first()
    title = f"Рейс {_trip_public_number(trip)}"[:255]
    if existing:
        if existing.title != title:
            existing.title = title
            existing.save(update_fields=["title", "updated_at"])
        return existing

    agency = _trip_anchor_agency(trip)
    if not agency:
        return None
    thread = ChatThread.objects.create(
        agency=agency,
        kind=ChatThread.KIND_TRIP,
        order_type=TRIP_ORDER_TYPE,
        order_id=oid,
        title=title,
        conversation_status=ChatThread.STATUS_IN_PROGRESS,
        created_by=user if getattr(user, "is_authenticated", False) else None,
    )
    post_chat_message(
        agency=agency,
        user=user,
        text=f"Чат рейса {_trip_public_number(trip)} открыт",
        author_role=ClientChatMessage.ROLE_SYSTEM,
        thread=thread,
        visibility=ClientChatMessage.VISIBILITY_INTERNAL,
    )
    return thread


def archive_trip_thread(trip) -> int:
    if not trip or not getattr(trip, "pk", None):
        return 0
    return ChatThread.objects.filter(
        kind=ChatThread.KIND_TRIP,
        order_type=TRIP_ORDER_TYPE,
        order_id=str(trip.pk),
        is_archived=False,
    ).update(is_archived=True, updated_at=timezone.now())


def trip_chat_url(trip) -> str:
    thread = ChatThread.objects.filter(
        kind=ChatThread.KIND_TRIP,
        order_type=TRIP_ORDER_TYPE,
        order_id=str(getattr(trip, "pk", "")),
    ).only("id").first()
    if not thread:
        return "/team-manager/chats/?kind=logistics"
    return f"/team-manager/chats/?thread={thread.id}&kind=logistics"
