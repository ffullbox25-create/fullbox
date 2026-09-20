"""SLA-метрики клиентских чатов для руководителя (без складской логики)."""

from __future__ import annotations

from collections import defaultdict
from datetime import timedelta
from typing import Any

from django.contrib.auth import get_user_model
from django.db.models import Count
from django.utils import timezone

from sku.models import Agency

from .models import ChatThread, ClientChatMessage

# Порог «нарушение SLA» для первого ответа FullBox (минуты).
DEFAULT_SLA_MINUTES = 120


def _agency_title(agency) -> str:
    return str(
        getattr(agency, "short_name", None)
        or getattr(agency, "agn_name", None)
        or f"Клиент {getattr(agency, 'id', '?')}"
    )


def _manager_label_map(agencies: list) -> dict[int, str]:
    """agency_id → ФИО менеджера (или «Без менеджера»)."""
    user_ids = {getattr(a, "mened_user_id", None) for a in agencies if getattr(a, "mened_user_id", None)}
    names: dict[int, str] = {}
    if user_ids:
        try:
            from employees.models import Employee

            for emp in Employee.objects.filter(user_id__in=user_ids, is_active=True).only(
                "user_id", "full_name"
            ):
                names[emp.user_id] = emp.full_name
        except Exception:
            pass
        missing = user_ids - set(names)
        if missing:
            User = get_user_model()
            for u in User.objects.filter(pk__in=missing).only("id", "username"):
                names[u.id] = u.username
    out: dict[int, str] = {}
    for a in agencies:
        mid = getattr(a, "mened_user_id", None)
        out[a.id] = names.get(mid, "Без менеджера") if mid else "Без менеджера"
    return out


def _client_visible_threads_qs(*, agency_ids: list[int] | None = None):
    qs = (
        ChatThread.objects.filter(
            kind__in=[ChatThread.KIND_CLIENT_GENERAL, ChatThread.KIND_ORDER_CLIENT],
            is_archived=False,
        )
        .exclude(
            conversation_status__in=[
                ChatThread.STATUS_CLOSED,
                ChatThread.STATUS_ARCHIVED,
            ]
        )
        .select_related("agency")
    )
    if agency_ids is not None:
        qs = qs.filter(agency_id__in=agency_ids)
    return qs


def build_chat_sla_report(
    *,
    agency_ids: list[int] | None = None,
    sla_minutes: int = DEFAULT_SLA_MINUTES,
    limit_rows: int = 80,
) -> dict[str, Any]:
    now = timezone.now()
    sla_delta = timedelta(minutes=max(15, int(sla_minutes or DEFAULT_SLA_MINUTES)))
    threads = list(_client_visible_threads_qs(agency_ids=agency_ids)[:1500])
    thread_ids = [t.id for t in threads]
    agencies = [t.agency for t in threads if t.agency_id]
    # Дополним агентствами из недельной статистики
    week_ago = now - timedelta(days=7)
    client_msg_qs = ClientChatMessage.objects.filter(
        author_role=ClientChatMessage.ROLE_CLIENT,
        is_deleted=False,
        created_at__gte=week_ago,
        visibility=ClientChatMessage.VISIBILITY_CLIENT,
        thread__kind__in=[ChatThread.KIND_CLIENT_GENERAL, ChatThread.KIND_ORDER_CLIENT],
    )
    if agency_ids is not None:
        client_msg_qs = client_msg_qs.filter(agency_id__in=agency_ids)
    week_by_agency = {
        row["agency_id"]: int(row["c"] or 0)
        for row in client_msg_qs.values("agency_id").annotate(c=Count("id"))
    }
    extra_agency_ids = set(week_by_agency) - {a.id for a in agencies}
    if extra_agency_ids:
        agencies.extend(list(Agency.objects.filter(pk__in=extra_agency_ids)))
    mgr_map = _manager_label_map(agencies)
    agency_by_id = {a.id: a for a in agencies}

    by_manager: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "manager": "",
            "unanswered": 0,
            "overdue": 0,
            "open": 0,
            "client_messages": 0,
            "agencies": set(),
        }
    )
    for aid, cnt in week_by_agency.items():
        agency = agency_by_id.get(aid)
        label = mgr_map.get(aid, "—")
        by_manager[label]["manager"] = label
        by_manager[label]["client_messages"] += cnt
        if agency:
            by_manager[label]["agencies"].add(agency.id)

    # Все клиент/staff сообщения по тредам одним запросом
    msgs = list(
        ClientChatMessage.objects.filter(
            thread_id__in=thread_ids,
            is_deleted=False,
            visibility__in=[
                ClientChatMessage.VISIBILITY_CLIENT,
                ClientChatMessage.VISIBILITY_SYSTEM,
            ],
        )
        .exclude(author_role=ClientChatMessage.ROLE_SYSTEM)
        .order_by("thread_id", "created_at", "id")
        .only("id", "thread_id", "author_role", "text", "created_at", "visibility")
    )
    by_thread: dict[int, list[ClientChatMessage]] = defaultdict(list)
    for m in msgs:
        by_thread[m.thread_id].append(m)

    unanswered_rows: list[dict[str, Any]] = []
    overdue_rows: list[dict[str, Any]] = []
    response_samples: list[float] = []
    open_questions = 0
    messages_per_order: dict[str, int] = defaultdict(int)

    open_statuses = {
        ChatThread.STATUS_NEEDS_STAFF,
        ChatThread.STATUS_WAIT_WAREHOUSE,
        ChatThread.STATUS_IN_PROGRESS,
        ChatThread.STATUS_NEW,
    }

    for thread in threads:
        agency = thread.agency
        mgr = mgr_map.get(thread.agency_id, "—") if thread.agency_id else "—"
        by_manager[mgr]["manager"] = mgr
        if agency:
            by_manager[mgr]["agencies"].add(agency.id)

        status = thread.conversation_status or ""
        if status in open_statuses:
            open_questions += 1
            by_manager[mgr]["open"] += 1

        thread_msgs = by_thread.get(thread.id, [])
        client_count = sum(1 for m in thread_msgs if m.author_role == ClientChatMessage.ROLE_CLIENT)
        if thread.order_id and client_count:
            messages_per_order[f"{thread.order_type}:{thread.order_id}"] += client_count

        last = thread_msgs[-1] if thread_msgs else None
        if last and last.author_role == ClientChatMessage.ROLE_CLIENT:
            wait_min = int((now - last.created_at).total_seconds() // 60)
            row = {
                "thread_id": thread.id,
                "title": thread.title or thread.get_kind_display(),
                "agency_id": thread.agency_id,
                "agency_name": _agency_title(agency) if agency else "—",
                "manager": mgr,
                "order_id": thread.order_id or "",
                "order_type": thread.order_type or "",
                "status": status,
                "status_label": thread.get_conversation_status_display(),
                "waiting_minutes": wait_min,
                "preview": (last.text or "")[:140],
                "chat_url": f"/team-manager/chats/?thread={thread.id}&kind=clients",
                "is_overdue": last.created_at <= now - sla_delta,
            }
            unanswered_rows.append(row)
            by_manager[mgr]["unanswered"] += 1
            if row["is_overdue"]:
                overdue_rows.append(row)
                by_manager[mgr]["overdue"] += 1

        first_client = next(
            (m for m in thread_msgs if m.author_role == ClientChatMessage.ROLE_CLIENT),
            None,
        )
        if first_client:
            reply = next(
                (
                    m
                    for m in thread_msgs
                    if m.author_role == ClientChatMessage.ROLE_STAFF
                    and m.created_at > first_client.created_at
                ),
                None,
            )
            if reply:
                response_samples.append(
                    (reply.created_at - first_client.created_at).total_seconds() / 60.0
                )

    unanswered_rows.sort(key=lambda r: (-int(r["is_overdue"]), -r["waiting_minutes"]))
    overdue_rows.sort(key=lambda r: -r["waiting_minutes"])

    avg_response = (
        round(sum(response_samples) / len(response_samples), 1) if response_samples else None
    )

    manager_rows = []
    for item in by_manager.values():
        manager_rows.append(
            {
                "manager": item["manager"],
                "unanswered": item["unanswered"],
                "overdue": item["overdue"],
                "open": item["open"],
                "client_messages_7d": item["client_messages"],
                "clients": len(item["agencies"]),
            }
        )
    manager_rows.sort(key=lambda r: (-r["overdue"], -r["unanswered"], -r["client_messages_7d"]))

    order_rows = [
        {
            "order_type": key.split(":", 1)[0] if ":" in key else "",
            "order_id": key.split(":", 1)[1] if ":" in key else key,
            "client_messages": cnt,
        }
        for key, cnt in sorted(messages_per_order.items(), key=lambda x: -x[1])[:40]
    ]

    return {
        "sla_minutes": int(sla_delta.total_seconds() // 60),
        "kpi": {
            "unanswered": len(unanswered_rows),
            "overdue": len(overdue_rows),
            "open_questions": open_questions,
            "avg_first_response_min": avg_response,
            "response_samples": len(response_samples),
            "threads_total": len(threads),
        },
        "unanswered": unanswered_rows[:limit_rows],
        "overdue": overdue_rows[:limit_rows],
        "managers": manager_rows,
        "orders": order_rows,
        "generated_at": timezone.localtime(now).strftime("%d.%m.%Y %H:%M"),
    }
