"""Задачи из сообщений чата + статус обращения (без складской логики)."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from django.utils import timezone

from employees.models import Employee
from todo.models import Task

from .chats import _order_url, ensure_order_threads
from .messaging_lk import post_chat_message
from .models import ChatThread, ClientChatMessage


def ensure_task_thread(*, agency, task: Task, user=None) -> ChatThread | None:
    if not agency or not task:
        return None
    title = f"Задача №{task.id}: {(task.title or '')[:180]}"[:255]
    thread = ChatThread.objects.filter(task=task).first()
    if thread:
        if thread.title != title:
            thread.title = title
            thread.save(update_fields=["title", "updated_at"])
        return thread
    return ChatThread.objects.create(
        agency=agency,
        kind=ChatThread.KIND_TASK,
        task=task,
        title=title,
        created_by=user if getattr(user, "is_authenticated", False) else None,
    )


def _default_assignee(user=None, *, assignee_id: int | None = None) -> Employee | None:
    if assignee_id:
        return Employee.objects.filter(pk=assignee_id, is_active=True).first()
    if user and getattr(user, "is_authenticated", False):
        mine = Employee.objects.filter(user=user, is_active=True).first()
        if mine:
            return mine
    return (
        Employee.objects.filter(role="manager", is_active=True)
        .order_by("full_name")
        .first()
    )


def _route_for_thread(thread: ChatThread) -> str:
    url = _order_url(thread) if thread else ""
    if url:
        return url[:255]
    if thread:
        return f"/team-manager/chats/?thread={thread.id}"[:255]
    return "/team-manager/"


def create_task_from_message(
    *,
    message: ClientChatMessage,
    user,
    title: str = "",
    assignee_id: int | None = None,
    priority: str = "normal",
    due_hours: int = 24,
    description_extra: str = "",
) -> dict[str, Any] | None:
    if not message or not message.agency_id:
        return None
    agency = message.agency
    thread = message.thread
    if thread is None:
        from .chats import ensure_client_general_thread

        thread = ensure_client_general_thread(agency, user=user)
    assignee = _default_assignee(user, assignee_id=assignee_id)
    text = (message.text or "").strip() or "Сообщение без текста"
    task_title = (title or f"По чату: {text[:80]}").strip()[:255]
    client_name = getattr(agency, "short_name", None) or getattr(agency, "agn_name", None) or agency.id
    desc_parts = [
        f"Клиент: {client_name}",
        f"Из сообщения #{message.id}",
        text,
    ]
    if thread and thread.order_id:
        desc_parts.insert(1, f"Заявка: {thread.order_type} {thread.order_id}")
    if description_extra:
        desc_parts.append(str(description_extra).strip())
    due = timezone.localtime() + timedelta(hours=max(1, int(due_hours or 24)))
    pri = priority if priority in {"low", "normal", "high", "urgent"} else "normal"
    task = Task.objects.create(
        title=task_title,
        description="\n\n".join(desc_parts)[:4000],
        route=_route_for_thread(thread),
        assigned_to=assignee,
        created_by=user if getattr(user, "is_authenticated", False) else None,
        priority=pri,
        status="backlog",
        due_date=due,
    )
    task_thread = ensure_task_thread(agency=agency, task=task, user=user)
    # Системная запись в исходном чате
    sys_msg = post_chat_message(
        agency=agency,
        user=user,
        text=f"Создана задача №{task.id}: {task_title}",
        author_role=ClientChatMessage.ROLE_SYSTEM,
        thread=thread,
        visibility=(
            ClientChatMessage.VISIBILITY_INTERNAL
            if thread and thread.kind == ChatThread.KIND_ORDER_INTERNAL
            else ClientChatMessage.VISIBILITY_SYSTEM
        ),
    )
    if task_thread:
        post_chat_message(
            agency=agency,
            user=user,
            text=f"Чат задачи №{task.id} открыт. Исходное сообщение: {text[:500]}",
            author_role=ClientChatMessage.ROLE_SYSTEM,
            thread=task_thread,
            visibility=ClientChatMessage.VISIBILITY_INTERNAL,
        )
    return {
        "task_id": task.id,
        "task_title": task.title,
        "assignee_id": assignee.id if assignee else None,
        "assignee_name": assignee.full_name if assignee else "",
        "thread_id": thread.id if thread else None,
        "task_thread_id": task_thread.id if task_thread else None,
        "system_message_id": sys_msg.id if sys_msg else None,
    }


def resolve_conversation(*, thread: ChatThread, user=None) -> ChatThread:
    thread.conversation_status = ChatThread.STATUS_RESOLVED
    thread.save(update_fields=["conversation_status", "updated_at"])
    post_chat_message(
        agency=thread.agency,
        user=user,
        text="Вопрос отмечен как решённый",
        author_role=ClientChatMessage.ROLE_SYSTEM,
        thread=thread,
        visibility=(
            ClientChatMessage.VISIBILITY_INTERNAL
            if thread.kind == ChatThread.KIND_ORDER_INTERNAL
            else ClientChatMessage.VISIBILITY_SYSTEM
        ),
    )
    return thread


def reopen_if_needed(thread: ChatThread | None, *, role: str) -> None:
    """После нового сообщения клиента — вернуть из «Решён»."""
    if not thread:
        return
    if thread.conversation_status == ChatThread.STATUS_RESOLVED and role == ClientChatMessage.ROLE_CLIENT:
        ChatThread.objects.filter(pk=thread.pk).update(
            conversation_status=ChatThread.STATUS_NEEDS_STAFF,
            updated_at=timezone.now(),
        )


def mark_task_done_in_chat(*, task: Task, user=None) -> None:
    """Системное событие при закрытии задачи (вызывать из todo при необходимости)."""
    thread = ChatThread.objects.filter(kind=ChatThread.KIND_TASK, task=task).first()
    if not thread:
        return
    post_chat_message(
        agency=thread.agency,
        user=user,
        text=f"Задача №{task.id} выполнена",
        author_role=ClientChatMessage.ROLE_SYSTEM,
        thread=thread,
        visibility=ClientChatMessage.VISIBILITY_INTERNAL,
    )
    # Также в связанный чат заявки, если есть
    if thread.order_id and thread.order_type:
        client_t, _ = ensure_order_threads(
            agency=thread.agency, order_type=thread.order_type, order_id=thread.order_id, user=user
        )
        if client_t:
            post_chat_message(
                agency=thread.agency,
                user=user,
                text=f"Задача №{task.id} выполнена",
                author_role=ClientChatMessage.ROLE_SYSTEM,
                thread=client_t,
                visibility=ClientChatMessage.VISIBILITY_SYSTEM,
            )
