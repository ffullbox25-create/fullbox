import json
import re
from collections import Counter, defaultdict
from datetime import timedelta
from pathlib import Path

from django.contrib import messages
from django.core.paginator import Paginator
from django.db import connection, transaction
from django.db.models import F, Q, Window
from django.db.models.functions import RowNumber
from django.http import FileResponse, Http404, HttpResponseForbidden, JsonResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views import View
from django.views.generic import TemplateView

from audit.models import OrderAuditEntry
from client_cabinet.services import (
    build_client_cabinet_url,
    build_staff_client_cabinet_url,
    build_client_list_context,
    build_client_list_queryset,
)
from client_cabinet.lk_status_map import resolve_lk_entry_status
from employees.access import (
    RoleRequiredMixin,
    employee_has_role,
    get_request_effective_role,
    get_request_employee,
)
from fullbox.order_numbers import format_order_number
from head_manager.models import Carrier
from logistics.models import CarrierVehicle
from shipping.models import ShippingOrder
from sku.models import Agency
from sklad.ui_services import build_inventory_journal_page
from sklad.views import _export_inventory_excel, inventory_pagination_links
from todo.deadlines import (
    DEADLINE_MANAGER_ROLES,
    WAREHOUSE_EXECUTOR_ROLES,
    attach_warehouse_deadline_context,
    canonical_warehouse_request_route,
    reschedule_warehouse_request,
)
from todo.models import Task, TaskComment

from .handoff import (
    TRANSFER_REASONS,
    bulk_transfer_tasks,
    cabinet_colleagues,
    can_reassign,
    claim_task,
    covered_principal_ids,
    deactivate_coverage,
    list_coverages_for_settings,
    transfer_task,
    upsert_coverage,
)
from .forms import CarrierVehicleForm
from .models import EmployeeCoverage
from .roles import (
    CABINET_ROLES,
    SCOPE_CHOICES,
    default_lane,
    default_scope,
)

try:
    from orders.web_ui import _repair_mojibake_text as _repair_order_mojibake_text
except Exception:
    def _repair_order_mojibake_text(value) -> str:
        return str(value or "")


def _team_request_role(request):
    return get_request_effective_role(
        request,
        preferred_roles=("admin", "director", "head_manager", "manager", "logistician"),
    )


def _repair_manager_mojibake_text(value) -> str:
    text = str(value or "")
    if not text:
        return ""

    def _looks_broken(fragment: str) -> bool:
        return any(
            (0x80 <= ord(char) <= 0xFF and char not in {"\u00a0", "\u00b7"})
            or (
                0x0400 <= ord(char) <= 0x04FF
                and not ("А" <= char <= "я")
                and char not in {"Ё", "ё"}
            )
            for char in fragment
        )

    def _decode_broken(fragment: str) -> str:
        chunks: list[bytes] = []
        for char in fragment:
            code = ord(char)
            if 0x80 <= code <= 0x9F:
                chunks.append(bytes([code]))
                continue
            try:
                chunks.append(char.encode("cp1251"))
            except UnicodeEncodeError:
                return _repair_order_mojibake_text(fragment)
        try:
            return b"".join(chunks).decode("utf-8")
        except UnicodeDecodeError:
            return _repair_order_mojibake_text(fragment)

    for _ in range(4):
        direct_repair = _decode_broken(text) if _looks_broken(text) else text
        if direct_repair != text and not _looks_broken(direct_repair):
            repaired = direct_repair
            if repaired == text:
                break
            text = repaired
            continue

        repaired = text
        parts = re.split(r"(\s+)", repaired)
        rebuilt: list[str] = []
        broken_parts: list[str] = []
        for part in parts:
            if part.isspace() and broken_parts:
                broken_parts.append(part)
                continue
            if _looks_broken(part):
                broken_parts.append(part)
                continue
            if broken_parts:
                rebuilt.append(_decode_broken("".join(broken_parts)))
                broken_parts = []
            rebuilt.append(part)
        if broken_parts:
            rebuilt.append(_decode_broken("".join(broken_parts)))
        repaired = "".join(rebuilt)
        if repaired == text:
            break
        text = repaired
    return text

CLIENT_SORT_FIELDS = {
    "name": "agn_name",
    "short_name": "short_name",
    "pref": "pref",
    "inn": "inn",
    "email": "email",
    "phone": "phone",
    "id": "id",
}
CLIENT_FILTER_FIELDS = {
    "agn_name": "agn_name",
    "short_name": "short_name",
    "inn": "inn",
    "pref": "pref",
    "email": "email",
    "phone": "phone",
}

MANAGER_REQUEST_TYPES = {"receiving", "processing", "packing", "shipping", "other", "fbs_movement"}
MANAGER_WAITING_STATUSES = {"sent_unconfirmed", "submitted", "waiting", "new", "created"}
TERMINAL_STATUS_TOKENS = ("выполн", "заверш", "закрыт", "done", "completed", "canceled", "cancelled")
CANCEL_REQUESTED_TOKENS = ("cancel_requested", "отмена на согласовании")
MANAGER_DASHBOARD_REQUEST_LIMIT = 500
MANAGER_DASHBOARD_TASK_LIMIT = 1000
MANAGER_TASK_PAGE_SIZE = 20
MANAGER_TASK_PAGE_SIZE_OPTIONS = (10, 20, 50, 100)
MANAGER_DUE_OPTIONS = (
    ("all", "Любой срок"),
    ("today", "Сегодня"),
    ("tomorrow", "Завтра"),
    ("week", "7 дней"),
    ("overdue", "Просрочено"),
)

TASK_TYPE_META = {
    # css classes map to lk-theme accents (no indigo/blue SaaS chips)
    "receiving": {"label": "Приемка", "tab": "Приемка", "code": "PR", "css": "accent"},
    "processing": {"label": "Обработка", "tab": "Обработка", "code": "OBR", "css": "sun"},
    "shipping": {"label": "Отгрузка", "tab": "Отгрузки", "code": "OTG", "css": "green"},
    "fbs_movement": {"label": "FBS перемещение", "tab": "FBS", "code": "FBS", "css": "sun"},
    "logistics": {"label": "Рейс", "tab": "Рейсы", "code": "TRIP", "css": "gray"},
    "other": {"label": "Другая заявка", "tab": "Другие", "code": "OTH", "css": "gray"},
}
TASK_STATUS_LABELS = {
    "backlog": "Новая",
    "in_progress": "В работе",
    "blocked": "Требует внимания",
    "done": "Выполнена",
}
TASK_STATUS_CSS = {
    "backlog": "neutral",
    "in_progress": "blue",
    "blocked": "orange",
    "done": "green",
}
PRIORITY_LABELS = {
    "low": "Низкий",
    "normal": "Средний",
    "high": "Высокий",
    "urgent": "Срочный",
}
PRIORITY_CSS = {
    "low": "green",
    "normal": "orange",
    "high": "red",
    "urgent": "red",
}

_RECEIVING_ROUTE_RE = re.compile(r"/orders/receiving/([^/]+)/")
_PROCESSING_ROUTE_RE = re.compile(r"/orders/processing/([^/]+)/")
_SHIPPING_ROUTE_RE = re.compile(r"/shipping/(\d+)/")
_OTHER_ROUTE_RE = re.compile(r"/orders/other/([^/]+)/")
_NUMBER_IN_TITLE_RE = re.compile(r"№\s*([A-Za-zА-Яа-я0-9_\-]+)")


def _payload(entry) -> dict:
    if entry is None:
        return {}
    return dict(entry.payload) if isinstance(entry.payload, dict) else {}


def _model_relation_exists(model, name: str) -> bool:
    try:
        model._meta.get_field(name)
        return True
    except Exception:
        return False


def _shipping_prefetches(*names: str) -> list[str]:
    return [name for name in names if _model_relation_exists(ShippingOrder, name)]


def _normalize_order_type(order_type: str) -> str:
    value = str(order_type or "").strip().lower()
    return "processing" if value == "packing" else value


def _type_label(order_type: str) -> str:
    return {
        "receiving": "Приёмка",
        "processing": "Обработка",
        "shipping": "Отгрузка",
        "fbs_movement": "FBS перемещение",
        "other": "Другая заявка",
    }.get(_normalize_order_type(order_type), "Заявка")


def _agency_display_name(agency) -> str:
    """Short client label for dense manager tables (prefer short_name)."""
    if agency is None:
        return "—"
    short = str(getattr(agency, "short_name", None) or "").strip()
    if short:
        return short
    full = str(getattr(agency, "agn_name", None) or getattr(agency, "name", None) or "").strip()
    return full or "—"


def _display_order_number(order_type: str, order_id: str) -> str:
    return format_order_number(_normalize_order_type(order_type), order_id)


def _request_url(entry, *, shipping_order: ShippingOrder | None = None) -> str:
    order_type = _normalize_order_type(entry.order_type)
    order_id = str(entry.order_id or "").strip()
    agency_id = getattr(entry, "agency_id", None)
    if order_type == "receiving":
        return f"/orders/receiving/{order_id}/?client={agency_id}" if agency_id else f"/orders/receiving/{order_id}/"
    if order_type == "processing":
        return f"/orders/processing/{order_id}/?client={agency_id}" if agency_id else f"/orders/processing/{order_id}/"
    if order_type == "other":
        # Кабинет менеджера: отдельный модуль «Другие заявки».
        return f"/team-manager/other-requests/{order_id}/"
    if order_type == "fbs_movement":
        request_id = _payload(entry).get("request_id")
        if request_id:
            return f"/team-manager/fbs/movements/{request_id}/"
        return "/team-manager/fbs/movements/"
    if order_type == "shipping":
        if shipping_order is None:
            shipping_qs = ShippingOrder.objects.filter(number=order_id).only("id", "agency_id")
            if agency_id:
                shipping_qs = shipping_qs.filter(agency_id=agency_id)
            shipping_order = shipping_qs.order_by("-id").first()
        if shipping_order:
            target_client_id = agency_id or shipping_order.agency_id
            suffix = f"?client={target_client_id}" if target_client_id else ""
            return f"/shipping/{shipping_order.id}/{suffix}"
    if agency_id:
        return f"{build_client_cabinet_url(agency_id)}#/request/{order_type}/{order_id}"
    return "/orders/"


def _is_draft(entry) -> bool:
    data = _payload(entry)
    status = str(data.get("status") or data.get("submit_action") or "").strip().lower()
    label = str(data.get("status_label") or "").strip().lower()
    return status in {"draft", "client_draft"} or "черновик" in label


def _is_cancel_requested(entry) -> bool:
    """A client cancellation appeal takes precedence over the warehouse stage."""
    data = _payload(entry)
    status = str(data.get("status") or data.get("submit_action") or "").strip().lower()
    label = str(data.get("status_label") or "").strip().lower()
    return status == "cancel_requested" or any(token in label for token in CANCEL_REQUESTED_TOKENS)


def _is_warehouse_cancel_requested(entry) -> bool:
    data = _payload(entry)
    status = str(data.get("status") or data.get("submit_action") or "").strip().lower()
    label = str(data.get("status_label") or "").strip().lower()
    return status == "warehouse_cancel_requested" or "отмена ожидает подтверждения склада" in label


def _quick_entry_status(entry, *, payload: dict | None = None) -> dict:
    """Cheap display status for large manager lists.

    Live warehouse/shipping status resolution is intentionally limited to the
    rows that are rendered on screen. Counters and filters use audit payloads so
    the manager LK does not perform hundreds of warehouse resolver calls during
    page load.
    """

    data = payload if isinstance(payload, dict) else _payload(entry)
    status = str(data.get("status") or data.get("submit_action") or data.get("shipping_state") or "").strip().lower()
    label = str(data.get("status_label") or "").strip().lower()
    order_type = _normalize_order_type(getattr(entry, "order_type", ""))
    raw_label = str(data.get("status_label") or "").strip()

    if status in {"draft", "client_draft"} or "черновик" in label:
        return {"bucket": "client", "filter_status": "waiting", "label": raw_label or "Черновик"}
    # Отмена на согласовании — не terminal: иначе KPI/фильтры показывают «Завершена».
    if _is_cancel_requested(entry):
        return {
            "bucket": "manager",
            "filter_status": "waiting",
            "label": raw_label or "Отмена на согласовании менеджера",
        }
    if _is_warehouse_cancel_requested(entry):
        return {
            "bucket": "warehouse",
            "filter_status": "processing",
            "label": raw_label or "Отмена ожидает подтверждения склада",
        }
    if status in {"canceled", "cancelled"} or any(
        token in status or token in label for token in TERMINAL_STATUS_TOKENS
    ):
        return {"bucket": "done", "filter_status": "completed", "label": raw_label or "Завершена"}
    if any(token in status or token in label for token in ("отмен",)):
        # Прочие «отмен*» без cancel_requested — считаем завершёнными.
        return {"bucket": "done", "filter_status": "completed", "label": raw_label or "Завершена"}
    if order_type == "shipping":
        if status in {"reserved", "storekeeper_accepted", "picking", "packed"}:
            return {"bucket": "warehouse", "filter_status": "processing", "label": raw_label or status or "В работе склада"}
        if status in {"shipped", "partial_shipped", "canceled", "cancelled"}:
            return {"bucket": "done", "filter_status": "completed", "label": raw_label or status or "Завершена"}
        if status == "submitted":
            return {"bucket": "manager", "filter_status": "waiting", "label": raw_label or "На согласовании менеджера"}
    if order_type == "processing" and (
        status in {"processing_head", "processing_in_work", "processing_worker", "in_work"}
        or any(token in label for token in ("раскороб", "товар прибыл в obr"))
    ):
        return {"bucket": "warehouse", "filter_status": "processing", "label": raw_label or status or "В работе склада"}
    if any(token in status or token in label for token in ("склад", "передан", "кладов", "работ", "прием", "приём", "обработ")):
        return {"bucket": "warehouse", "filter_status": "processing", "label": raw_label or status or "В работе склада"}
    if (
        status in MANAGER_WAITING_STATUSES
        or "ждет подтверждения" in label
        or "ждёт подтверждения" in label
        or "согласован" in label
        or "провер" in label
    ):
        return {"bucket": "manager", "filter_status": "waiting", "label": raw_label or status or "Ждет подтверждения"}
    return {"bucket": "manager", "filter_status": "waiting", "label": raw_label or status or "—"}


def _is_waiting_manager(entry, resolved=None) -> bool:
    if resolved is not None and getattr(resolved, "bucket", ""):
        return resolved.bucket == "manager" and resolved.filter_status == "waiting"
    quick = _quick_entry_status(entry)
    return quick["bucket"] == "manager" and quick["filter_status"] == "waiting"


def _entry_key(entry) -> tuple[str, str]:
    return (_normalize_order_type(entry.order_type), str(entry.order_id or "").strip())


def _request_identity(entry) -> tuple[int | None, str, str]:
    return (
        getattr(entry, "agency_id", None),
        _normalize_order_type(entry.order_type),
        str(entry.order_id or "").strip(),
    )


def _is_manager_status_source(entry) -> bool:
    data = _payload(entry)
    status = str(data.get("status") or data.get("submit_action") or data.get("shipping_state") or "").strip().lower()
    label = str(data.get("status_label") or "").strip().lower()
    has_act_state = bool(data.get("act_sent") or data.get("act_client_response") or data.get("act"))
    if entry.action == "status":
        # Технические складские события иногда записываются с action=status,
        # но без статуса. Они не должны перекрывать полноценный этап заявки.
        return bool(status or label or has_act_state)
    if has_act_state:
        return True
    if status in {"draft", "client_draft"} or "черновик" in label:
        # Editing a returned/client draft is an event, but not always the
        # current process status. Keep it as status only when it is explicitly
        # logged as a status event (handled above) or when no better source
        # exists and we fall back to the latest entry.
        return False
    return bool(status or label)


def _is_submission_event(entry) -> bool:
    """A non-draft status event marks the request entering the workflow."""
    if _is_draft(entry):
        return False
    data = _payload(entry)
    draft_values = {"", "draft", "client_draft", "save", "save_draft"}
    return any(
        str(data.get(key) or "").strip().lower() not in draft_values
        for key in ("status", "submit_action", "shipping_state")
    )


def _display_status_entry(entry: OrderAuditEntry, *, allow_query: bool = False) -> OrderAuditEntry:
    # A client/warehouse cancellation appeal is itself the effective status.
    # Do not replace it with an older operational event such as "picking".
    if _is_cancel_requested(entry) or _is_warehouse_cancel_requested(entry):
        return entry
    cached = getattr(entry, "_manager_status_entry_cache", None)
    if cached is not None:
        return cached
    if not allow_query:
        return entry
    qs = OrderAuditEntry.objects.filter(
        order_type=entry.order_type,
        order_id=str(entry.order_id or ""),
    )
    if entry.agency_id:
        qs = qs.filter(agency_id=entry.agency_id)
    for candidate in qs.select_related("agency").order_by("-created_at", "-id")[:80]:
        if _is_manager_status_source(candidate):
            return candidate
    return entry


def _request_history_meta(
    selected_keys: set[tuple[int | None, str, str]],
) -> tuple[
    dict[tuple[int | None, str, str], int],
    dict[tuple[int | None, str, str], object],
    dict[tuple[int | None, str, str], object],
]:
    """Return status/timestamp metadata without materializing full audit history.

    A VALUES join keeps the composite audit index usable for each selected
    request. The previous large OR query returned tens of thousands of JSON
    rows to Python on every manager dashboard refresh.
    """
    if not selected_keys:
        return {}, {}, {}
    ordered_keys = sorted(
        selected_keys,
        key=lambda key: (key[0] is None, int(key[0] or 0), key[1], key[2]),
    )
    lookup_keys = [
        (agency_id, request_type, audit_type, order_id)
        for agency_id, request_type, order_id in ordered_keys
        for audit_type in (
            ("processing", "packing")
            if request_type == "processing"
            else (request_type,)
        )
    ]
    values_sql = ", ".join(
        ["(%s::integer, %s::text, %s::text, %s::text)"] * len(lookup_keys)
    )
    params = [value for key in lookup_keys for value in key]
    sql = f"""
        WITH selected(agency_id, request_type, audit_type, order_id) AS (
            VALUES {values_sql}
        ), per_type AS (
            SELECT
                s.agency_id,
                s.request_type,
                s.order_id,
                first_event.created_at AS first_event_at,
                submission.created_at AS submitted_at,
                status_source.id AS status_id,
                status_source.created_at AS status_created_at
            FROM selected AS s
            LEFT JOIN LATERAL (
                SELECT audit.created_at
                FROM audit_orderauditentry AS audit
                WHERE audit.agency_id IS NOT DISTINCT FROM s.agency_id
                  AND audit.order_type = s.audit_type
                  AND audit.order_id = s.order_id
                ORDER BY audit.created_at, audit.id
                LIMIT 1
            ) AS first_event ON TRUE
            LEFT JOIN LATERAL (
                SELECT audit.created_at
                FROM audit_orderauditentry AS audit
                CROSS JOIN LATERAL (
                    SELECT
                        LOWER(BTRIM(COALESCE(audit.payload ->> 'status', ''))) AS status_value,
                        LOWER(BTRIM(COALESCE(audit.payload ->> 'submit_action', ''))) AS submit_value,
                        LOWER(BTRIM(COALESCE(audit.payload ->> 'shipping_state', ''))) AS shipping_value,
                        LOWER(BTRIM(COALESCE(audit.payload ->> 'status_label', ''))) AS status_label
                ) AS state
                WHERE audit.agency_id IS NOT DISTINCT FROM s.agency_id
                  AND audit.order_type = s.audit_type
                  AND audit.order_id = s.order_id
                  AND NOT (
                        COALESCE(NULLIF(state.status_value, ''), NULLIF(state.submit_value, ''), '')
                            IN ('draft', 'client_draft')
                        OR state.status_label LIKE '%%черновик%%'
                  )
                  AND (
                        state.status_value NOT IN ('', 'draft', 'client_draft', 'save', 'save_draft')
                        OR state.submit_value NOT IN ('', 'draft', 'client_draft', 'save', 'save_draft')
                        OR state.shipping_value NOT IN ('', 'draft', 'client_draft', 'save', 'save_draft')
                  )
                ORDER BY audit.created_at, audit.id
                LIMIT 1
            ) AS submission ON TRUE
            LEFT JOIN LATERAL (
                SELECT audit.id, audit.created_at
                FROM audit_orderauditentry AS audit
                CROSS JOIN LATERAL (
                    SELECT
                        LOWER(BTRIM(COALESCE(audit.payload ->> 'status', ''))) AS status_value,
                        LOWER(BTRIM(COALESCE(audit.payload ->> 'submit_action', ''))) AS submit_value,
                        LOWER(BTRIM(COALESCE(audit.payload ->> 'shipping_state', ''))) AS shipping_value,
                        LOWER(BTRIM(COALESCE(audit.payload ->> 'status_label', ''))) AS status_label,
                        audit.payload -> 'act_sent' AS act_sent,
                        audit.payload -> 'act_client_response' AS act_response,
                        audit.payload -> 'act' AS act_value
                ) AS state
                CROSS JOIN LATERAL (
                    SELECT
                        (
                            COALESCE(NULLIF(state.status_value, ''), NULLIF(state.submit_value, ''), '')
                                IN ('draft', 'client_draft')
                            OR state.status_label LIKE '%%черновик%%'
                        ) AS is_draft,
                        (
                            (
                                state.act_sent IS NOT NULL
                                AND state.act_sent NOT IN (
                                    'null'::jsonb, 'false'::jsonb, '0'::jsonb,
                                    '""'::jsonb, '[]'::jsonb, '{{}}'::jsonb
                                )
                            )
                            OR (
                                state.act_response IS NOT NULL
                                AND state.act_response NOT IN (
                                    'null'::jsonb, 'false'::jsonb, '0'::jsonb,
                                    '""'::jsonb, '[]'::jsonb, '{{}}'::jsonb
                                )
                            )
                            OR (
                                state.act_value IS NOT NULL
                                AND state.act_value NOT IN (
                                    'null'::jsonb, 'false'::jsonb, '0'::jsonb,
                                    '""'::jsonb, '[]'::jsonb, '{{}}'::jsonb
                                )
                            )
                        ) AS has_act_state,
                        COALESCE(
                            NULLIF(state.status_value, ''),
                            NULLIF(state.submit_value, ''),
                            NULLIF(state.shipping_value, ''),
                            ''
                        ) AS effective_status
                ) AS classified
                WHERE audit.agency_id IS NOT DISTINCT FROM s.agency_id
                  AND audit.order_type = s.audit_type
                  AND audit.order_id = s.order_id
                  AND (
                        (
                            audit.action = 'status'
                            AND (
                                classified.effective_status <> ''
                                OR state.status_label <> ''
                                OR classified.has_act_state
                            )
                        )
                        OR (
                            audit.action IS DISTINCT FROM 'status'
                            AND (
                                classified.has_act_state
                                OR (
                                    (
                                        classified.effective_status <> ''
                                        OR state.status_label <> ''
                                    )
                                    AND NOT classified.is_draft
                                )
                            )
                        )
                  )
                ORDER BY audit.created_at DESC, audit.id DESC
                LIMIT 1
            ) AS status_source ON TRUE
        )
        SELECT
            agency_id,
            request_type,
            order_id,
            MIN(first_event_at) AS first_event_at,
            MIN(submitted_at) AS submitted_at,
            (
                ARRAY_AGG(status_id ORDER BY status_created_at DESC, status_id DESC)
                    FILTER (WHERE status_id IS NOT NULL)
            )[1] AS latest_status_id
        FROM per_type
        GROUP BY agency_id, request_type, order_id
    """
    latest_status_ids: dict[tuple[int | None, str, str], int] = {}
    first_event_at: dict[tuple[int | None, str, str], object] = {}
    submitted_at: dict[tuple[int | None, str, str], object] = {}
    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        for agency_id, request_type, order_id, first_at, submitted, status_id in cursor.fetchall():
            key = (agency_id, str(request_type), str(order_id))
            if first_at is not None:
                first_event_at[key] = first_at
            if submitted is not None:
                submitted_at[key] = submitted
            if status_id is not None:
                latest_status_ids[key] = int(status_id)
    return latest_status_ids, first_event_at, submitted_at


def _preload_audit_payloads(entries) -> None:
    missing_by_id = {
        int(entry.id): entry
        for entry in entries
        if getattr(entry, "id", None) and "payload" not in entry.__dict__
    }
    if not missing_by_id:
        return
    for entry_id, payload in OrderAuditEntry.objects.filter(
        pk__in=missing_by_id
    ).values_list("id", "payload"):
        missing_by_id[int(entry_id)].payload = payload


def _latest_request_entries(
    limit: int = 1000,
    *,
    order_type: str | None = None,
    include_latest_payload: bool = True,
) -> list[OrderAuditEntry]:
    limit = max(int(limit or 0), 0)
    if not limit:
        return []
    order_types = set(MANAGER_REQUEST_TYPES)
    normalized = _normalize_order_type(order_type or "")
    if normalized and normalized != "all":
        order_types = {"processing", "packing"} if normalized == "processing" else {normalized}
    result = []
    seen_keys: set[tuple[int | None, str, str]] = set()
    candidate_offset = 0
    candidate_batch_size = max(
        limit + min(limit, 100),
        min(limit * 3, 600),
        200,
    )
    candidates = (
        OrderAuditEntry.objects.filter(order_type__in=order_types)
        .annotate(
            manager_row_number=Window(
                expression=RowNumber(),
                partition_by=(F("agency_id"), F("order_type"), F("order_id")),
                order_by=(F("created_at").desc(), F("id").desc()),
            )
        )
        .filter(manager_row_number=1)
        .select_related("agency")
        .order_by("-created_at", "-id")
    )
    if not include_latest_payload:
        candidates = candidates.defer("payload")

    while len(result) < limit:
        batch = list(candidates[candidate_offset:candidate_offset + candidate_batch_size])
        if not batch:
            break
        candidate_offset += len(batch)

        latest: dict[tuple[int | None, str, str], OrderAuditEntry] = {}
        for entry in batch:
            key = _request_identity(entry)
            if key[2] and key not in seen_keys and key not in latest:
                latest[key] = entry
        seen_keys.update(latest)
        selected_keys = set(latest)

        latest_status_ids, first_event_at, submitted_at = _request_history_meta(selected_keys)

        status_ids = set(latest_status_ids.values())
        status_entries = {
            entry.id: entry
            for entry in batch
            if entry.id in status_ids and "payload" in entry.__dict__
        }
        missing_status_ids = status_ids.difference(status_entries)
        if missing_status_ids:
            status_entries.update(
                {
                    entry.id: entry
                    for entry in OrderAuditEntry.objects.filter(
                        pk__in=missing_status_ids
                    ).select_related("agency")
                }
            )
        latest_status = {
            key: status_entries[entry_id]
            for key, entry_id in latest_status_ids.items()
            if entry_id in status_entries
        }
        _preload_audit_payloads(
            entry
            for key, entry in latest.items()
            if key not in latest_status
        )

        shipping_keys = {
            (key[0], key[2])
            for key in selected_keys
            if key[0] is not None and key[1] == "shipping"
        }
        draft_shipping_keys: set[tuple[int, str]] = set()
        if shipping_keys:
            draft_shipping_keys = set(
                ShippingOrder.objects.filter(
                    agency_id__in={agency_id for agency_id, _number in shipping_keys},
                    number__in={number for _agency_id, number in shipping_keys},
                    status=ShippingOrder.STATUS_DRAFT,
                ).values_list("agency_id", "number")
            )

        for key, entry in latest.items():
            entry._manager_status_entry_cache = latest_status.get(key, entry)
            entry._manager_submitted_at = (
                submitted_at.get(key) or first_event_at.get(key) or entry.created_at
            )
            if str(entry.order_id or "").strip().lower().startswith("draft-"):
                continue
            if key[1] == "shipping" and (key[0], key[2]) in draft_shipping_keys:
                continue
            if _is_draft(entry._manager_status_entry_cache):
                continue
            result.append(entry)
            if len(result) >= limit:
                break

        if len(batch) < candidate_batch_size:
            break
    # Черновик существует только в личном кабинете клиента. В менеджерский
    # контур он не должен попадать ни как строка, ни в KPI/уведомления.
    return result


def _task_order_ref(task) -> tuple[str, str] | None:
    route = str(task.route or "")
    match = _RECEIVING_ROUTE_RE.search(route)
    if match:
        return ("receiving", match.group(1))
    match = _PROCESSING_ROUTE_RE.search(route)
    if match:
        return ("processing", match.group(1))
    match = _SHIPPING_ROUTE_RE.search(route)
    if match:
        return ("shipping", match.group(1))
    match = _OTHER_ROUTE_RE.search(route)
    if match:
        return ("other", match.group(1))
    lowered = f"{task.title or ''} {route}".lower()
    if "рейс" in lowered or "logistics" in lowered or "trip" in lowered:
        return ("logistics", str(task.pk))
    return None


def _short_location_label(value) -> str:
    """Короткое имя склада: «СТАРАЯ_КУПАВНА_28 (Россия…)» → «СТАРАЯ_КУПАВНА_28»."""
    text = str(value or "").strip()
    if not text or text == "—":
        return ""
    if " (" in text:
        text = text.split(" (", 1)[0].strip()
    return text[:80]


def _shipping_order_payload(order: ShippingOrder | None) -> dict:
    if order is None:
        return {}
    marketplace_id = getattr(order, "marketplace_id", None)
    marketplace = str(getattr(getattr(order, "marketplace", None), "name", "") or "").strip()
    if not marketplace and marketplace_id:
        marketplace = _market_name_by_id(marketplace_id)
    status = str(getattr(order, "status", "") or "")
    submitted_status = getattr(ShippingOrder, "STATUS_SUBMITTED", "submitted")
    status_label = (
        "На согласовании менеджера"
        if status == submitted_status
        else (order.get_status_display() if hasattr(order, "get_status_display") else status)
    )
    destination_warehouse = str(getattr(order, "destination_warehouse", "") or "").strip()
    transit_address = str(getattr(order, "transit_address", "") or "").strip()
    destination_address = str(getattr(order, "destination_address", "") or "").strip()
    delivery_type = str(getattr(order, "delivery_type", "") or "").strip()
    transfer_destination = (
        destination_address
        if delivery_type == getattr(ShippingOrder, "DELIVERY_TRANSFER", "transfer")
        else ""
    )
    destination_names: list[str] = []
    try:
        destination_names = [
            str(getattr(dest, "warehouse_name", "") or "").strip()
            for dest in order.destinations.all()
            if str(getattr(dest, "warehouse_name", "") or "").strip()
        ]
    except Exception:
        destination_names = []
    warehouse = (
        destination_warehouse
        or (destination_names[0] if destination_names else "")
        or transit_address
        or transfer_destination
    )
    return {
        "status": status,
        "shipping_state": status,
        "status_label": status_label,
        "marketplace": marketplace,
        "marketplace_id": marketplace_id,
        "delivery_type": delivery_type,
        "destination_address": destination_address,
        "transfer_destination": transfer_destination,
        "destination_warehouse": destination_warehouse,
        "transit_address": transit_address,
        "warehouse": warehouse,
        "shipping_barcode": getattr(order, "shipping_barcode", ""),
        "wb_supply_barcode": getattr(order, "wb_supply_barcode", ""),
        "supply_number": getattr(order, "supply_number", ""),
        "comment": getattr(order, "comment", ""),
        "source": "Портал клиента",
    }


def _attach_shipping_task_orders(tasks: list[Task], latest_by_key: dict[tuple[str, str], OrderAuditEntry]) -> None:
    """Link /shipping/<pk>/ manager tasks to shipping audit rows keyed by SO-*."""

    shipping_pks: set[int] = set()
    for task in tasks:
        match = _SHIPPING_ROUTE_RE.search(str(task.route or ""))
        if not match:
            continue
        try:
            shipping_pks.add(int(match.group(1)))
        except (TypeError, ValueError):
            continue
    if not shipping_pks:
        return
    orders_by_pk = {
        order.pk: order
        for order in ShippingOrder.objects.filter(pk__in=shipping_pks)
        .select_related("agency", "marketplace")
        .prefetch_related(*_shipping_prefetches("items", "destinations"))
    }
    for task in tasks:
        match = _SHIPPING_ROUTE_RE.search(str(task.route or ""))
        if not match:
            continue
        try:
            shipping_pk = int(match.group(1))
        except (TypeError, ValueError):
            continue
        order = orders_by_pk.get(shipping_pk)
        if order is None:
            continue
        task._shipping_order_cache = order
        public_number = str(order.number or "").strip()
        if not public_number:
            continue
        entry = latest_by_key.get(("shipping", public_number))
        if entry is not None:
            latest_by_key.setdefault(("shipping", str(shipping_pk)), entry)


def _task_type(task, ref: tuple[str, str] | None = None) -> str:
    if ref:
        return ref[0] if ref[0] in TASK_TYPE_META else "other"
    lowered = f"{task.title or ''} {task.route or ''}".lower()
    if "прием" in lowered or "приём" in lowered:
        return "receiving"
    if "обработ" in lowered:
        return "processing"
    if "отгруз" in lowered:
        return "shipping"
    if "рейс" in lowered or "logistics" in lowered or "trip" in lowered:
        return "logistics"
    return "other"


def _payload_first(data: dict, *keys, default: str = "—") -> str:
    for key in keys:
        value = data.get(key)
        if value not in (None, "", False):
            # Булевы флаги вроде wb_transit_warehouse не являются названием склада.
            if isinstance(value, bool):
                continue
            text = str(value).strip()
            if text and text != "—":
                return text
    return default


_MARKET_NAME_CACHE: dict[int, str] | None = None
_DELIVERY_TYPE_LABELS = {
    "marketplace": "Маркетплейс",
    "courier": "Курьер",
    "pickup": "Самовывоз",
    "other": "Другое",
}


def _market_name_by_id(market_id) -> str:
    """Resolve Market.id → name with a request-scoped module cache."""
    global _MARKET_NAME_CACHE
    if market_id in (None, "", 0, "0"):
        return ""
    try:
        mid = int(market_id)
    except (TypeError, ValueError):
        return ""
    if _MARKET_NAME_CACHE is None:
        try:
            from sku.models import Market

            _MARKET_NAME_CACHE = {
                int(row.id): str(row.name or "").strip()
                for row in Market.objects.only("id", "name")
            }
        except Exception:
            _MARKET_NAME_CACHE = {}
    return str(_MARKET_NAME_CACHE.get(mid) or "").strip()


def _marketplace_label(data: dict) -> str:
    value = _payload_first(
        data,
        "marketplace",
        "market",
        "marketplace_name",
        "market_name",
        "marketplace_label",
        default="",
    ).strip()
    if not value:
        value = _market_name_by_id(data.get("marketplace_id"))
    lowered = value.lower()
    if "ozon" in lowered or "озон" in lowered:
        return "OZON"
    if lowered in {"wb", "wildberries"} or "wildberries" in lowered:
        return "WB"
    if value:
        return value.upper()
    # Нет МП в заявке — покажем тип доставки, чтобы колонка не была пустой.
    delivery = str(data.get("delivery_type") or "").strip().lower()
    return _DELIVERY_TYPE_LABELS.get(delivery, "—")


def _warehouse_label(data: dict) -> str:
    value = _payload_first(
        data,
        "destination_warehouse",
        "warehouse",
        "warehouse_name",
        "store_name",
        "store",
        "transit_address",
        default="",
    )
    short = _short_location_label(value)
    if short:
        return short
    delivery = str(data.get("delivery_type") or "").strip().lower()
    if delivery == "pickup":
        return "Самовывоз"
    return "—"


_ACT_FLAG_KEYS = (
    "act_storekeeper_signed",
    "act_manager_signed",
    "act_logistician_signed",
    "act_sent",
    "act_client_response",
    "act_viewed",
)


def _merge_payload_meta(*payloads: dict) -> dict:
    """Собрать marketplace/склад и мета приёмки из нескольких audit/shipping payload."""
    merged: dict = {}
    market_keys = ("marketplace", "market", "marketplace_name", "market_name", "marketplace_label")
    warehouse_keys = (
        "destination_warehouse",
        "warehouse",
        "warehouse_name",
        "store_name",
        "store",
        "transit_address",
    )
    extra_keys = ("place_type", "goods_type_label", "goods_type", "delivery_type")
    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        for key in market_keys:
            if merged.get("marketplace") not in (None, "", "—"):
                break
            value = payload.get(key)
            if value not in (None, "", False) and not isinstance(value, bool):
                merged["marketplace"] = value
        if merged.get("marketplace_id") in (None, "", 0, "0"):
            mp_id = payload.get("marketplace_id")
            if mp_id not in (None, "", 0, "0", False):
                merged["marketplace_id"] = mp_id
        for key in warehouse_keys:
            if merged.get("warehouse") not in (None, "", "—"):
                break
            value = payload.get(key)
            if value not in (None, "", False) and not isinstance(value, bool):
                merged["warehouse"] = value
                if key == "destination_warehouse":
                    merged["destination_warehouse"] = value
                if key == "transit_address":
                    merged["transit_address"] = value
        for key in extra_keys:
            if merged.get(key) not in (None, "", "—"):
                continue
            value = payload.get(key)
            if value not in (None, "", False) and not isinstance(value, bool):
                merged[key] = value
        # Флаги акта: берём первое истинное значение из истории (OR).
        for key in _ACT_FLAG_KEYS:
            if merged.get(key):
                continue
            value = payload.get(key)
            if value not in (None, "", False):
                merged[key] = value
        label = str(payload.get("status_label") or "").strip()
        if label and not merged.get("status_label"):
            merged["status_label"] = label
        elif label and "акт" in label.lower() and "акт" not in str(merged.get("status_label") or "").lower():
            # Предпочитаем подпись со словом «акт», если текущий label без него.
            merged["status_label"] = label
    return merged


def _looks_like_question_noise(value) -> bool:
    text = str(value or "").strip()
    if text.count("?") < 6:
        return False
    meaningful = [char for char in text if not char.isspace()]
    if not meaningful:
        return False
    cyrillic = sum(0x0400 <= ord(char) <= 0x04FF for char in text)
    letters = sum(char.isalpha() for char in text)
    question_ratio = text.count("?") / max(len(meaningful), 1)
    return cyrillic == 0 and (question_ratio >= 0.35 or letters == 0)


def _shipping_event_fallback(data: dict, shipping_order: ShippingOrder | None) -> str:
    status = str(
        data.get("shipping_state")
        or data.get("status")
        or getattr(shipping_order, "status", "")
        or ""
    ).strip().lower()
    if status == ShippingOrder.STATUS_SHIPPED:
        return "Заявка отгружена"
    if status == ShippingOrder.STATUS_PARTIAL:
        return "Заявка отгружена частично"
    if status == ShippingOrder.STATUS_PACKED:
        return "Кладовщик подготовил заявку к отгрузке"
    if status == ShippingOrder.STATUS_PICKING:
        shortage = data.get("reachtruck_shortage_qty")
        if shortage not in (None, "", 0, "0"):
            return f"Созданы задания ричтраку на отбор в OTG (частично, дефицит {shortage} шт.)"
        return "Созданы задания ричтраку на отбор в OTG"
    if status == ShippingOrder.STATUS_RESERVED:
        return "Резерв подтвержден"
    if status == ShippingOrder.STATUS_STOREKEEPER_ACCEPTED:
        return "Кладовщик принял заявку в работу"
    if status == ShippingOrder.STATUS_SUBMITTED:
        return "Заявка ожидает подтверждения менеджера"
    if status == ShippingOrder.STATUS_DRAFT:
        return "Заявка отредактирована менеджером"
    if status == ShippingOrder.STATUS_CANCELED:
        return "Заявка отменена"
    return ""


def _manager_event_description(
    entry: OrderAuditEntry,
    *,
    data: dict,
    status_label: str,
    next_step: str,
    shipping_order: ShippingOrder | None = None,
) -> str:
    description = _repair_manager_mojibake_text(entry.description or "").strip()
    if description and not _looks_like_question_noise(description):
        return description

    order_type = _normalize_order_type(entry.order_type)
    if order_type == "shipping":
        fallback = _shipping_event_fallback(data, shipping_order)
        if fallback:
            return fallback

    payload = entry.payload if isinstance(entry.payload, dict) else {}
    for key in ("message", "detail", "title", "status_label", "comment"):
        candidate = _repair_manager_mojibake_text(payload.get(key) or "").strip()
        if candidate and not _looks_like_question_noise(candidate):
            return candidate
    if status_label and status_label != "—":
        return status_label
    if next_step:
        return next_step
    return "" if _looks_like_question_noise(description) else description


def _act_needs_manager_sign(data: dict, order_type: str) -> bool:
    """Акт уже у менеджера, подпись менеджера ещё не поставлена."""
    order_type = _normalize_order_type(order_type)
    label = str(data.get("status_label") or "").strip().lower()
    if order_type == "receiving":
        if data.get("act_manager_signed") or data.get("act_sent"):
            return False
        if data.get("act_storekeeper_signed"):
            return True
        return "акт" in label and "менеджер" in label
    if order_type == "shipping":
        if data.get("act_manager_signed") or data.get("act_sent"):
            return False
        if data.get("act_logistician_signed"):
            return True
        return "акт" in label and "менеджер" in label
    return False


def _act_url_for_row(
    *,
    order_type: str,
    order_id: str,
    data: dict,
    shipping_order: ShippingOrder | None = None,
) -> str:
    """Ссылка на печатную форму акта, если акт уже доступен менеджеру/клиенту."""
    order_type = _normalize_order_type(order_type)
    order_id = str(order_id or "").strip()
    label = str(data.get("status_label") or "").strip().lower()
    if order_type == "receiving":
        has_act = bool(
            data.get("act_storekeeper_signed")
            or data.get("act_manager_signed")
            or data.get("act_sent")
            or ("акт" in label and ("менеджер" in label or "клиент" in label))
        )
        if has_act and order_id:
            return f"/orders/receiving/{order_id}/act/print/"
        return ""
    if order_type == "shipping":
        status = str(
            data.get("status")
            or data.get("shipping_state")
            or getattr(shipping_order, "status", "")
            or ""
        ).strip().lower()
        has_act = bool(
            data.get("act_logistician_signed")
            or data.get("act_manager_signed")
            or data.get("act_sent")
            or status
            in {
                ShippingOrder.STATUS_PACKED,
                ShippingOrder.STATUS_SHIPPED,
                ShippingOrder.STATUS_PARTIAL,
                "partial",
            }
        )
        pk = getattr(shipping_order, "pk", None)
        if has_act and pk:
            return f"/shipping/{pk}/act/"
        return ""
    return ""


def _audit_meta_by_order(entries: list[OrderAuditEntry]) -> dict[tuple[int | None, str, str], dict]:
    """По истории заявок вытащить marketplace/склад, даже если в latest-status их нет."""
    if not entries:
        return {}
    selected_keys = {
        _request_identity(entry)
        for entry in entries
        if str(entry.order_id or "").strip()
    }
    lookup_keys = [
        (agency_id, request_type, request_type, order_id)
        for agency_id, request_type, order_id in sorted(
            selected_keys,
            key=lambda key: (key[0] is None, int(key[0] or 0), key[1], key[2]),
        )
    ]
    if not lookup_keys:
        return {}
    values_sql = ", ".join(
        ["(%s::integer, %s::text, %s::text, %s::text)"] * len(lookup_keys)
    )
    params = [value for key in lookup_keys for value in key]
    sql = f"""
        WITH selected(agency_id, request_type, audit_type, order_id) AS (
            VALUES {values_sql}
        )
        SELECT
            selected.agency_id,
            selected.request_type,
            selected.order_id,
            audit.payload
        FROM selected
        JOIN audit_orderauditentry AS audit
          ON audit.agency_id IS NOT DISTINCT FROM selected.agency_id
         AND audit.order_type = selected.audit_type
         AND audit.order_id = selected.order_id
        ORDER BY audit.id
    """
    buckets: dict[tuple[int | None, str, str], list[dict]] = {}
    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        for agency_id, request_type, order_id, payload in cursor.fetchall():
            if not isinstance(payload, dict) and payload not in (None, ""):
                try:
                    payload = json.loads(
                        bytes(payload) if isinstance(payload, memoryview) else payload
                    )
                except (TypeError, ValueError):
                    payload = {}
            if isinstance(payload, dict) and payload:
                key = (agency_id, str(request_type), str(order_id))
                buckets.setdefault(key, []).append(payload)
    return {key: _merge_payload_meta(*payloads) for key, payloads in buckets.items()}


_OZON_GM_BLOCK_RE = re.compile(r"ШК\s*ГМ(?:\s*Ozon)?\s*:\s*([^\n]+)", re.IGNORECASE)
_OZON_GM_LINE_RE = re.compile(r"(?:^|\n)\s*-?\s*ГМ\s+([^\s,;→]+)", re.IGNORECASE)


def _append_unique(target: list[str], value) -> None:
    text = str(value or "").strip().strip(" .;,\u00a0")
    if not text or text == "—" or text in target:
        return
    target.append(text)


def _extract_ozon_gm_codes(*values) -> list[str]:
    result: list[str] = []

    def walk(value) -> None:
        if value in (None, ""):
            return
        if isinstance(value, (list, tuple, set)):
            for item in value:
                walk(item)
            return
        if isinstance(value, dict):
            for key in ("gm_barcode", "ozon_gm_comment", "comment", "gm_barcodes", "ozon_gm_barcodes"):
                if key in value:
                    walk(value.get(key))
            return
        text = str(value or "").strip()
        for match in _OZON_GM_BLOCK_RE.finditer(text):
            for token in re.split(r"[,;]", match.group(1)):
                _append_unique(result, token)
        for match in _OZON_GM_LINE_RE.finditer(text):
            _append_unique(result, match.group(1))

    for value in values:
        walk(value)
    return result


def _join_codes(codes: list[str], *, limit: int = 12) -> str:
    visible = list(codes[:limit])
    suffix = f" +{len(codes) - limit}" if len(codes) > limit else ""
    return ", ".join(visible) + suffix


def _shipping_order_for_entry(entry: OrderAuditEntry, *, details: bool = False) -> ShippingOrder | None:
    if _normalize_order_type(entry.order_type) != "shipping":
        return None
    order_id = str(entry.order_id or "").strip()
    if not order_id:
        return None
    qs = ShippingOrder.objects.filter(number=order_id).select_related("marketplace")
    if entry.agency_id:
        qs = qs.filter(agency_id=entry.agency_id)
    if details:
        qs = qs.prefetch_related(*_shipping_prefetches("items", "destinations"))
    return qs.order_by("-id").first()


def _shipping_ozon_summary(data: dict, order: ShippingOrder | None = None) -> dict[str, str]:
    marketplace = _marketplace_label(data)
    is_ozon = marketplace == "OZON" or bool(
        data.get("supply_number") or data.get("ozon_batch_id") or data.get("is_ozon_distribution")
    )
    supply = str(
        (getattr(order, "supply_number", "") if order else "")
        or data.get("supply_number")
        or data.get("shipping_barcode")
        or data.get("wb_supply_barcode")
        or ""
    ).strip()
    values = [
        getattr(order, "comment", "") if order else "",
        data.get("comment"),
        data.get("ozon_gm_comment"),
        data.get("gm_barcodes"),
        data.get("items"),
    ]
    if order is not None:
        try:
            values.extend(dest.comment for dest in order.destinations.all())
        except Exception:
            pass
        try:
            values.extend(item.comment for item in order.items.all())
        except Exception:
            pass
    gm_codes = _extract_ozon_gm_codes(*values)
    if not is_ozon and not gm_codes:
        return {"ozon_supply_label": "", "ozon_gm_summary": ""}
    return {
        "ozon_supply_label": f"Поставка Ozon: {supply}" if supply else "",
        "ozon_gm_summary": f"ШК ГМ: {_join_codes(gm_codes)}" if gm_codes else "",
    }


def _display_task_number(task, task_type: str, ref: tuple[str, str] | None) -> str:
    if ref and ref[0] in {"receiving", "processing", "shipping", "other"}:
        return _display_order_number(ref[0], ref[1])
    title = task.display_title() if hasattr(task, "display_title") else str(task.title or "")
    match = _NUMBER_IN_TITLE_RE.search(title)
    if match:
        return match.group(1)
    return f"{TASK_TYPE_META.get(task_type, TASK_TYPE_META['other'])['code']}-{task.pk}"


_FINANCE_NEXT_STEP_MARKERS = (
    "исправить тариф",
    "рассчитать начисления",
    "проверить начисления",
    "сформировать акт",
    "получить подпись",
    "исправить акт",
    "выставить счёт",
    "выставить счет",
    "отправить счёт",
    "отправить счет",
    "проверить оплату",
    "закрыть заявку",
)


def _is_finance_next_step(next_step: str) -> bool:
    step = (next_step or "").strip().lower()
    return any(marker in step for marker in _FINANCE_NEXT_STEP_MARKERS)


def _billing_application_url(
    *,
    task_type: str,
    order_id: str,
    client_id: int | None,
    cache: dict | None = None,
) -> str:
    """URL карточки биллинга для финансового следующего действия."""
    aid = str(order_id or "").strip()
    if not task_type or not aid:
        return "/team-manager/billing/applications/"
    cache_key = (str(task_type), aid, int(client_id or 0))
    if cache is not None and cache_key in cache:
        application_id = cache[cache_key]
        if application_id:
            return f"/team-manager/billing/applications/{application_id}/"
        return "/team-manager/billing/applications/"
    try:
        from billing.models import BillingApplication

        qs = BillingApplication.objects.filter(application_type=task_type, application_id=aid)
        if client_id:
            qs = qs.filter(client_id=client_id)
        app = qs.order_by("-updated_at", "-id").first()
        if app:
            return f"/team-manager/billing/applications/{app.id}/"
    except Exception:
        pass
    return "/team-manager/billing/applications/"


def _document_point(next_step: str, task_status: str = "") -> str:
    step = (next_step or "").lower()
    if "счёт" in step or "счет" in step:
        if "отправ" in step:
            return "счёт отправлен"
        if "выстав" in step:
            return "счёт не выставлен"
        return "счёт"
    if "упд" in step:
        return "УПД отправлен" if "отправ" in step else "УПД"
    if "акт" in step:
        if "подпис" in step or "получен" in step:
            return "акт ждёт подписи"
        if "отправ" in step:
            return "акт отправлен"
        if "сформир" in step:
            return "акт не сформирован"
        return "акт"
    if "документ" in step:
        return "проверить документы"
    if task_status == "done":
        return "документы завершены"
    return "—"


def _is_document_action(next_step: str) -> bool:
    step = (next_step or "").lower()
    keys = ("счёт", "счет", "акт", "упд", "подпис", "начислен", "тариф", "оплат")
    return any(k in step for k in keys)


def _is_waiting_client_row(row: dict) -> bool:
    """Same predicate as «Ждём клиента» in manager_control rail."""
    text = " ".join(
        str(row.get(key) or "").lower()
        for key in ("next_step", "status", "document_point", "stuck_reason")
    )
    return any(token in text for token in ("клиент", "ждём", "ждем", "ожидает клиента"))


def _stuck_reason(task, now, *, is_overdue: bool, has_executor: bool, next_step: str, waiting_manager: bool) -> str:
    """Explicit stuck reason or empty if not stuck."""
    if is_overdue or not has_executor:
        return ""
    updated = timezone.localtime(task.updated_at) if task.updated_at else None
    created = timezone.localtime(task.created_at) if task.created_at else None
    age_ref = updated or created
    age_minutes = int((now - age_ref).total_seconds() // 60) if age_ref else 0
    if task.status == "blocked":
        return f"Зависло {_human_duration(age_minutes)} — разобрать проблему"
    if waiting_manager and age_minutes >= 30:
        return f"Зависло {_human_duration(age_minutes)} — ждёт действия менеджера"
    if next_step == "Назначить исполнителя" and age_minutes >= 30:
        return f"Зависло {_human_duration(age_minutes)} — не назначен исполнитель"
    if age_minutes >= 6 * 60 and task.status in {"backlog", "in_progress", "todo", "open"}:
        return f"Зависло {_human_duration(age_minutes)} — нет обязательного движения"
    return ""


def _human_duration(total_minutes: int) -> str:
    hours = max(0, total_minutes) // 60
    minutes = max(0, total_minutes) % 60
    if hours and minutes:
        return f"{hours} ч. {minutes} мин."
    if hours:
        return f"{hours} ч."
    return f"{minutes} мин."


def _assign_manager_category(row: dict) -> str:
    """
    Mutually exclusive focus bucket for KPI cards.
    Priority: overdue → no_owner → stuck → docs → today → ''
    """
    if row.get("is_overdue"):
        return "overdue"
    if row.get("executor") in {"—", "", None}:
        return "no_owner"
    if row.get("stuck_reason"):
        return "stuck"
    if _is_document_action(row.get("next_step") or ""):
        return "docs"
    deadline = row.get("deadline")
    if deadline and timezone.localtime(deadline).date() == timezone.localtime().date():
        return "today"
    return ""


def _ops_complete(*, task_type: str, shipping_order, status_bucket: str) -> bool:
    """Операционный этап завершён — SLA отгрузки/приёмки больше не «просрочено»."""
    if task_type == "shipping" and shipping_order is not None:
        return str(getattr(shipping_order, "status", "") or "") in {
            "packed",
            "shipped",
            "partial_shipped",
        }
    return status_bucket == "done"


def _finance_sla(next_step: str) -> dict:
    step = (next_step or "").lower()
    if "подпис" in step:
        return {"label": next_step, "css": "purple", "is_overdue": False}
    if _is_document_action(next_step):
        return {"label": next_step, "css": "orange", "is_overdue": False}
    return {"label": "Операция завершена", "css": "green", "is_overdue": False}


def _sla_context(task, now):
    if task.status == "done" or not task.due_date:
        return {"label": "—", "css": "neutral", "is_overdue": False}
    due_date = timezone.localtime(task.due_date)
    delta = due_date - now
    if delta.total_seconds() < 0:
        total_minutes = int(abs(delta.total_seconds()) // 60)
        hours = total_minutes // 60
        minutes = total_minutes % 60
        label = f"Просрочено {hours} ч {minutes} мин" if hours else f"Просрочено {minutes} мин"
        return {"label": label, "css": "red", "is_overdue": True}
    days = delta.days
    hours = delta.seconds // 3600
    if days:
        label = f"{days} д {hours} ч"
    else:
        minutes = (delta.seconds % 3600) // 60
        label = f"{hours} ч {minutes} мин"
    return {"label": label, "css": "green" if days else "orange", "is_overdue": False}


def _next_step(task, task_type: str, entry, resolved=None) -> str:
    # The operational shipping status may still be "reserved" or "picking",
    # but an active client appeal must lead the manager to the cancellation
    # decision instead of advancing the shipment.
    if _is_cancel_requested(entry):
        request_payload = _payload(entry)
        if task_type == "receiving":
            previous_status = str(
                request_payload.get("manager_cancel_previous_status") or ""
            ).strip().lower()
            if previous_status in {"warehouse", "on_warehouse"}:
                return "Запросить подтверждение склада"
        if task_type == "processing":
            processing_stage = str(
                request_payload.get("processing_stage") or ""
            ).strip().lower()
            if processing_stage not in {
                "",
                "draft",
                "awaiting_approval",
                "cancelled",
                "done",
            }:
                return "Запросить подтверждение склада"
        shipping_order = getattr(task, "_shipping_order_cache", None)
        if task_type == "shipping" and shipping_order is not None:
            try:
                from shipping.workflow import requires_warehouse_cancel_confirmation

                if requires_warehouse_cancel_confirmation(shipping_order):
                    return "Запросить подтверждение склада"
            except Exception:
                pass
        return "Подтвердить отмену"
    if _is_warehouse_cancel_requested(entry):
        return "Ожидать подтверждения склада"
    task_title = str(getattr(task, "title", "") or "").strip().lower()
    if task_type == "processing" and "акт обработки" in task_title:
        return "Открыть и подтвердить акт"
    if resolved is not None and getattr(resolved, "next_step", ""):
        base = resolved.next_step
    else:
        base = ""
    routing_cache = getattr(task, "_routing_cache", None)
    billing_cache = getattr(task, "_billing_cache", None)
    # Финансовый этап после операционного завершения (отдельный SLA/действие).
    try:
        from billing.manager_billing import billing_next_action_for_wms

        order_id = ""
        client_id = None
        if entry is not None:
            order_id = str(getattr(entry, "order_id", "") or "")
            client_id = getattr(entry, "agency_id", None)
        shipping_order = getattr(task, "_shipping_order_cache", None)
        if task_type == "shipping" and shipping_order is not None:
            order_id = str(getattr(shipping_order, "number", "") or order_id)
            client_id = getattr(shipping_order, "agency_id", None) or client_id
            status = str(getattr(shipping_order, "status", "") or "")
            try:
                from logistics.routing_services import routing_snapshot

                snap = routing_snapshot(shipping_order, cache=routing_cache)
                if snap.get("needs_clarification"):
                    reason = snap.get("reason_label") or ""
                    return f"Уточнить данные для логистики{(' — ' + reason) if reason else ''}"
                if status in {"packed", "shipped", "partial_shipped"} and snap.get("status") == "ready_for_routing":
                    if not snap.get("trip_number"):
                        # после складской готовности — логистический этап, не операционный SLA
                        pass
            except Exception:
                pass
            if status in {
                "packed",
                "shipped",
                "partial_shipped",
            }:
                finance = billing_next_action_for_wms(
                    application_type="shipping",
                    application_id=order_id,
                    client_id=client_id,
                    cache=billing_cache,
                )
                if finance:
                    return finance
                try:
                    from logistics.routing_services import routing_snapshot

                    snap = routing_snapshot(shipping_order, cache=routing_cache)
                    routing_status = str(snap.get("status") or "")
                    if routing_status == "ready_for_routing" and not snap.get("trip_number"):
                        return "Включить в рейс"
                    if routing_status == "routed":
                        return "Контроль рейса"
                    if routing_status == "in_transit":
                        return "Контроль доставки"
                    if routing_status == "delivered":
                        return "Операция завершена"
                    if routing_status == "failed":
                        return "Разобрать результат доставки"
                except Exception:
                    return "Передать в логистику"
        elif task_type in {"receiving", "processing"} and order_id:
            bucket = getattr(resolved, "bucket", "") if resolved is not None else ""
            if bucket == "done":
                finance = billing_next_action_for_wms(
                    application_type=task_type,
                    application_id=order_id,
                    client_id=client_id,
                    cache=billing_cache,
                )
                if finance:
                    return finance
    except Exception:
        pass

    if base:
        return base
    if not task.assigned_to_id:
        return "Назначить исполнителя"
    if entry and _is_waiting_manager(entry, resolved):
        return {
            "receiving": "Подтвердить приемку",
            "processing": "Проверить обработку",
            "shipping": "Подтвердить отгрузку",
            "other": "Подтвердить и отправить на склад",
        }.get(task_type, "Проверить заявку")
    if task.status == "blocked":
        return "Разобрать проблему"
    if task.status == "done":
        return "Архивировать"
    return {
        "receiving": "Проверить документы",
        "processing": "Проверить расхождения",
        "shipping": "Передать на склад",
        "logistics": "Проверить маршрут",
        "other": "Подтвердить и отправить на склад",
    }.get(task_type, "Связаться с клиентом")


def _task_row(task, latest_by_key: dict[tuple[str, str], OrderAuditEntry], now, *, live_status: bool = True) -> dict:
    ref = _task_order_ref(task)
    task_type = _task_type(task, ref)
    entry = latest_by_key.get(ref) if ref else None
    status_entry = _display_status_entry(entry, allow_query=live_status) if entry is not None else None
    cancellation_appeal = bool(
        status_entry is not None
        and (_is_cancel_requested(status_entry) or _is_warehouse_cancel_requested(status_entry))
    )
    shipping_order = getattr(task, "_shipping_order_cache", None) if task_type == "shipping" else None
    data = {**_shipping_order_payload(shipping_order), **(_payload(entry) if entry else {})}
    status_data = {**(_payload(status_entry) if status_entry else {})}
    if shipping_order is not None:
        status_data = {**status_data, **_shipping_order_payload(shipping_order)}
    resolved_status = None
    quick_status = (
        _quick_entry_status(status_entry, payload=status_data)
        if status_entry is not None
        else {
            "bucket": "manager" if shipping_order else "",
            "filter_status": "waiting" if shipping_order else "",
            "label": data.get("status_label", ""),
        }
    )
    if entry is not None and live_status and not cancellation_appeal:
        try:
            resolved_status = resolve_lk_entry_status(status_entry, audience="default")
        except Exception:
            resolved_status = None
    agency = getattr(entry, "agency", None) if entry else getattr(shipping_order, "agency", None)
    sla = _sla_context(task, now)
    comments_total = getattr(task, "comments_total", 0) or 0
    title = task.display_title() if hasattr(task, "display_title") else task.title
    if cancellation_appeal:
        appeal_status = _quick_entry_status(status_entry, payload=_payload(status_entry))
        entry_status_label = str(appeal_status["label"] or "").strip()
        status_bucket = appeal_status["bucket"]
    else:
        entry_status_label = str(
            getattr(resolved_status, "status_label", "")
            or status_data.get("status_label")
            or quick_status["label"]
            or ""
        ).strip()
        status_bucket = getattr(resolved_status, "bucket", "") or quick_status["bucket"]
    status_label = entry_status_label if entry_status_label else TASK_STATUS_LABELS.get(task.status, task.status or "Новая")
    if status_bucket == "done":
        status_css = "green"
    elif status_bucket == "warehouse":
        status_css = "accent"
    elif entry_status_label and (
        status_bucket == "manager"
        or (status_entry is not None and _is_waiting_manager(status_entry, resolved_status))
    ):
        status_css = "orange"
    else:
        status_css = TASK_STATUS_CSS.get(task.status, "neutral")
    display_order_id = str(getattr(entry, "order_id", "") or getattr(shipping_order, "number", "") or "").strip()
    display_number = (
        _display_order_number(task_type, display_order_id)
        if display_order_id and task_type in {"receiving", "processing", "shipping", "other"}
        else _display_task_number(task, task_type, ref)
    )
    ozon_summary = _shipping_ozon_summary(data, shipping_order) if task_type == "shipping" else {
        "ozon_supply_label": "",
        "ozon_gm_summary": "",
    }
    next_step = _next_step(task, task_type, status_entry or entry, resolved_status)
    logistics_status = ""
    logistics_trip = ""
    if task_type == "shipping" and shipping_order is not None:
        try:
            from logistics.routing_services import routing_snapshot

            snap = routing_snapshot(shipping_order, cache=getattr(task, "_routing_cache", None))
            logistics_status = snap.get("status_label") or ""
            logistics_trip = snap.get("trip_number") or ""
            if snap.get("needs_clarification") and status_css != "red":
                status_css = "orange"
        except Exception:
            pass
    # Завершённая отгрузка/операция: операционный SLA закрыт, дальше только фин. этап.
    if _ops_complete(task_type=task_type, shipping_order=shipping_order, status_bucket=status_bucket):
        if "Уточнить данные" in (next_step or ""):
            sla = {"label": next_step, "css": "orange", "is_overdue": False}
        else:
            sla = _finance_sla(next_step)
        if status_bucket != "done" and "Уточнить данные" not in (next_step or ""):
            status_css = "green"
    waiting_manager = bool(
        status_entry is not None and _is_waiting_manager(status_entry, resolved_status)
    )
    has_executor = bool(getattr(task, "assigned_to_id", None))
    stuck_reason = _stuck_reason(
        task,
        now,
        is_overdue=sla["is_overdue"],
        has_executor=has_executor,
        next_step=next_step,
        waiting_manager=waiting_manager,
    )
    if (
        not stuck_reason
        and not sla["is_overdue"]
        and has_executor
        and _is_document_action(next_step)
        and _ops_complete(task_type=task_type, shipping_order=shipping_order, status_bucket=status_bucket)
    ):
        updated = timezone.localtime(task.updated_at) if task.updated_at else None
        age_minutes = int((now - updated).total_seconds() // 60) if updated else 0
        if age_minutes >= 6 * 60:
            stuck_reason = f"Зависло {_human_duration(age_minutes)} — {next_step.lower()}"
    entry_submitted_at = getattr(entry, "_manager_submitted_at", None) if entry is not None else None
    submitted_value = entry_submitted_at or task.created_at
    submitted_at = timezone.localtime(submitted_value) if submitted_value else None
    row = {
        "_task_obj": task,
        "id": task.id,
        "number": display_number,
        "title": title,
        "type": task_type,
        "type_label": TASK_TYPE_META.get(task_type, TASK_TYPE_META["other"])["label"],
        "type_css": TASK_TYPE_META.get(task_type, TASK_TYPE_META["other"])["css"],
        "process_label": "Биллинг" if _is_finance_next_step(next_step) else TASK_TYPE_META.get(task_type, TASK_TYPE_META["other"])["label"],
        "process_css": "purple" if _is_finance_next_step(next_step) else TASK_TYPE_META.get(task_type, TASK_TYPE_META["other"])["css"],
        "client_name": _agency_display_name(agency),
        "client_inn": getattr(agency, "inn", None) or "",
        "client_id": getattr(agency, "id", None),
        "warehouse": _warehouse_label(data),
        "marketplace": _marketplace_label(data),
        "executor": getattr(task.assigned_to, "full_name", None) or "—",
        "executor_id": getattr(task, "assigned_to_id", None),
        "assignee_role": getattr(task.assigned_to, "role", None) or "",
        "priority": PRIORITY_LABELS.get(task.priority, task.priority or "Средний"),
        "priority_css": PRIORITY_CSS.get(task.priority, "orange"),
        "status": status_label,
        "status_css": status_css,
        "task_status": TASK_STATUS_LABELS.get(task.status, task.status or "Новая"),
        "task_status_css": TASK_STATUS_CSS.get(task.status, "neutral"),
        "deadline": timezone.localtime(task.due_date) if task.due_date else None,
        "deadline_input": (
            timezone.localtime(task.due_date).strftime("%Y-%m-%dT%H:%M")
            if task.due_date
            else ""
        ),
        "sla": sla["label"],
        "sla_css": sla["css"],
        "is_overdue": sla["is_overdue"],
        "needs_attention": task.status == "blocked" or task.priority in {"high", "urgent"} or sla["is_overdue"],
        "updated_at": timezone.localtime(task.updated_at) if task.updated_at else None,
        "next_step": next_step,
        "document_point": _document_point(next_step, task.status),
        "stuck_reason": stuck_reason,
        "logistics_status": logistics_status,
        "logistics_trip": logistics_trip,
        "comments_total": comments_total,
        "channel": _payload_first(data, "source", "channel", default="Портал клиента"),
        "created_at": submitted_at,
        "submitted_at": submitted_at,
        "is_submitted_today": bool(submitted_at and submitted_at.date() == now.date()),
        "ozon_supply_label": ozon_summary["ozon_supply_label"],
        "ozon_gm_summary": ozon_summary["ozon_gm_summary"],
        # Stay inside manager LK — never fall back to legacy /todo/ panel.
        "url": task.route or "/team-manager/?section=tasks",
        "process_url": task.route or "/team-manager/?section=tasks",
        "boxes_url": f"/shipping/{shipping_order.pk}/boxes.xlsx" if task_type == "shipping" and shipping_order is not None else "",
    }
    # Фин. этап: кнопка «следующее действие» должна открывать биллинг, а не карточку склада
    if _is_finance_next_step(next_step) and task_type in {"shipping", "receiving", "processing"}:
        order_ref = display_order_id or str(getattr(shipping_order, "number", "") or "")
        row["url"] = _billing_application_url(
            task_type=task_type,
            order_id=order_ref,
            client_id=getattr(agency, "id", None),
            cache=getattr(task, "_billing_url_cache", None),
        )
    row["focus"] = _assign_manager_category(row)
    return row


def _resolve_task_filters(request) -> dict:
    """Эффективные фильтры с дефолтами по роли (без сброса явного GET)."""
    role = _team_request_role(request)
    section = str(request.GET.get("section") or "desk").strip().lower()
    if section not in {"desk", "tasks"}:
        section = "desk"
    scope = request.GET.get("scope")
    lane = request.GET.get("lane")
    if scope is None:
        if section == "desk" and role == "manager":
            scope = "all"
        elif section == "desk" and role == "logistician":
            scope = "mine_or_unassigned"
        else:
            scope = default_scope(role)
    if lane is None:
        lane = default_lane(role)
    return {
        "section": section,
        "type": request.GET.get("type") or "all",
        "status": request.GET.get("status") or "all",
        "client": request.GET.get("client") or "all",
        "warehouse": request.GET.get("warehouse") or "all",
        "due": request.GET.get("due") or "all",
        "focus": request.GET.get("focus") or ("desk" if section == "desk" else "all"),
        "q": request.GET.get("q") or "",
        "overdue": request.GET.get("overdue") == "1",
        "attention": request.GET.get("attention") == "1",
        "scope": scope,
        "lane": lane,
        "role": role,
    }


def _resolve_task_page_size(request) -> int:
    try:
        value = int(request.GET.get("page_size") or MANAGER_TASK_PAGE_SIZE)
    except (TypeError, ValueError):
        value = MANAGER_TASK_PAGE_SIZE
    return value if value in MANAGER_TASK_PAGE_SIZE_OPTIONS else MANAGER_TASK_PAGE_SIZE


def _task_list_page(request, filtered_total: int) -> tuple[int, int, int, int]:
    """Return (page, num_pages, start, end) for task table pagination."""
    page_size = _resolve_task_page_size(request)
    num_pages = max(1, (int(filtered_total) + page_size - 1) // page_size) if filtered_total else 1
    try:
        page = int(request.GET.get("page") or 1)
    except (TypeError, ValueError):
        page = 1
    page = max(1, min(page, num_pages))
    start = (page - 1) * page_size
    end = start + page_size
    return page, num_pages, start, end


def _task_pager_query(filters: dict, *, page: int, page_size: int | None = None) -> str:
    """Build query string for pager links, preserving filters."""
    from urllib.parse import urlencode

    params = {
        "section": filters.get("section") or "tasks",
        "type": filters.get("type") or "all",
        "status": filters.get("status") or "all",
        "client": filters.get("client") or "all",
        "scope": filters.get("scope") or "all",
        "lane": filters.get("lane") or "all",
        "page": str(page),
    }
    warehouse = filters.get("warehouse") or "all"
    if warehouse and warehouse != "all":
        params["warehouse"] = warehouse
    due = filters.get("due") or "all"
    if due and due != "all":
        params["due"] = due
    if page_size and page_size != MANAGER_TASK_PAGE_SIZE:
        params["page_size"] = str(page_size)
    focus = filters.get("focus") or "all"
    if focus and focus != "all":
        params["focus"] = focus
    q = (filters.get("q") or "").strip()
    if q:
        params["q"] = q
    if filters.get("overdue"):
        params["overdue"] = "1"
    if filters.get("attention"):
        params["attention"] = "1"
    return urlencode(params)


def _task_focus_query(filters: dict, focus: str) -> str:
    """Ссылка на список ровно с теми фильтрами, по которым посчитан счётчик."""
    from urllib.parse import urlencode

    params = {"section": filters.get("section") or "tasks"}
    for key in ("type", "status", "client", "warehouse", "due", "scope", "lane"):
        value = filters.get(key)
        if value and value != "all":
            params[key] = value
    query = str(filters.get("q") or "").strip()
    if query:
        params["q"] = query
    if focus and focus != "all":
        params["focus"] = focus
    return urlencode(params)


def _filtered_task_rows(
    rows: list[dict],
    request,
    *,
    skip_filters: set[str] | frozenset[str] | None = None,
    filters: dict | None = None,
    employee=None,
    covered_ids: set[int] | None = None,
) -> list[dict]:
    filters = filters or _resolve_task_filters(request)
    skip = set(skip_filters or ())
    selected_type = filters["type"]
    selected_status = filters["status"]
    selected_client = filters["client"]
    selected_warehouse = filters.get("warehouse") or "all"
    selected_due = filters.get("due") or "all"
    selected_focus = filters["focus"]
    selected_scope = filters["scope"]
    selected_lane = filters["lane"]
    query = (filters["q"] or "").strip().lower()
    now_local = timezone.localtime()
    today_local = now_local.date()
    only_overdue = filters["overdue"]
    only_attention = filters["attention"]
    if covered_ids is None:
        employee = employee or get_request_employee(request)
        covered_ids = set(covered_principal_ids(employee)) if employee else set()
    employee_id = getattr(employee, "id", None)
    employee_role = filters["role"] or getattr(employee, "role", None)
    result = []
    for row in rows:
        if "type" not in skip and selected_type != "all" and row["type"] != selected_type:
            continue
        if "lane" not in skip and selected_lane == "ops_logistics" and row["type"] not in {"shipping", "logistics"}:
            continue
        if "status" not in skip and selected_status != "all" and row["status"] != selected_status:
            continue
        if "client" not in skip and selected_client != "all" and str(row.get("client_id") or "") != selected_client:
            continue
        if "warehouse" not in skip and selected_warehouse != "all" and str(row.get("warehouse") or "") != selected_warehouse:
            continue
        if "due" not in skip and selected_due != "all":
            deadline = row.get("deadline")
            if selected_due == "overdue":
                if not row.get("is_overdue"):
                    continue
            elif not deadline:
                continue
            elif selected_due == "today" and timezone.localtime(deadline).date() != today_local:
                continue
            elif selected_due == "tomorrow" and timezone.localtime(deadline).date() != today_local + timedelta(days=1):
                continue
            elif selected_due == "week" and not (today_local <= timezone.localtime(deadline).date() <= today_local + timedelta(days=7)):
                continue
        if "focus" in skip:
            pass
        elif selected_focus == "waiting_client":
            if not _is_waiting_client_row(row):
                continue
        elif selected_focus == "waiting_warehouse":
            if row.get("status_css") != "accent":
                continue
        elif selected_focus == "acts_sign":
            # Акты к подписи: документный шаг с подписью/актом.
            step = str(row.get("next_step") or "").lower()
            if not (_is_document_action(step) and any(k in step for k in ("акт", "подпис"))):
                continue
        elif selected_focus == "desk":
            pass
        elif selected_focus == "today":
            if not row.get("is_submitted_today"):
                continue
        elif selected_focus == "attention":
            if not (
                row.get("is_overdue")
                or row.get("stuck_reason")
                or row.get("executor") in {"—", "", None}
            ):
                continue
        elif selected_focus != "all" and row.get("focus") != selected_focus:
            continue
        if "overdue" not in skip and only_overdue and not row["is_overdue"]:
            continue
        if "attention" not in skip and only_attention and not row["needs_attention"]:
            continue
        assignee_id = row.get("executor_id")
        assignee_role = row.get("assignee_role") or ""
        if "scope" in skip:
            pass
        elif selected_scope == "mine_or_unassigned":
            if assignee_id and assignee_id != employee_id and assignee_id not in covered_ids:
                continue
        elif selected_scope == "mine":
            if assignee_id != employee_id and assignee_id not in covered_ids:
                continue
        elif selected_scope == "department":
            if assignee_role and assignee_role != employee_role:
                continue
            if not assignee_id and employee_role not in {"manager", "logistician", "head_manager"}:
                continue
        elif selected_scope == "unassigned":
            if row.get("executor") not in {"—", "", None} and assignee_id:
                continue
        elif selected_scope == "role_manager":
            if assignee_role and assignee_role != "manager":
                continue
            if not assignee_id and row["type"] not in {"receiving", "processing", "shipping", "other"}:
                continue
        elif selected_scope == "role_logistician":
            if assignee_role and assignee_role != "logistician":
                continue
            if not assignee_id and row["type"] not in {"shipping", "logistics"}:
                continue
        elif selected_scope == "lane_billing":
            if not _is_document_action(row.get("next_step") or ""):
                continue
        elif selected_scope == "lane_warehouse":
            if row["type"] not in {"receiving", "processing", "shipping"}:
                continue
        elif selected_scope == "needs_reassign":
            if row.get("focus") not in {"stuck", "no_owner", "overdue"}:
                continue
        if "q" not in skip and query:
            haystack = " ".join(
                str(row.get(key) or "")
                for key in (
                    "number",
                    "title",
                    "client_name",
                    "client_inn",
                    "warehouse",
                    "marketplace",
                    "executor",
                    "next_step",
                    "document_point",
                    "ozon_supply_label",
                    "ozon_gm_summary",
                )
            ).lower()
            if query not in haystack:
                continue
        result.append(row)
    return result


def _preload_task_row_caches(
    tasks: list[Task],
    latest_by_key: dict[tuple[str, str], OrderAuditEntry] | None = None,
) -> tuple[dict, dict]:
    """Пакетно подгружает routing/billing для строк задач (убирает N+1)."""
    shipping_orders = [
        getattr(task, "_shipping_order_cache", None)
        for task in tasks
        if getattr(task, "_shipping_order_cache", None) is not None
    ]
    routing_cache: dict = {}
    try:
        from logistics.routing_services import routing_snapshots_for_orders

        routing_cache = routing_snapshots_for_orders([o.pk for o in shipping_orders if getattr(o, "pk", None)])
    except Exception:
        routing_cache = {}

    billing_refs: list[tuple[str, str, int | None]] = []
    for task in tasks:
        order = getattr(task, "_shipping_order_cache", None)
        if order is not None:
            billing_refs.append(("shipping", str(order.number or ""), getattr(order, "agency_id", None)))
            continue
        ref = _task_order_ref(task)
        if ref and ref[0] in {"receiving", "processing"}:
            entry = (latest_by_key or {}).get(ref)
            billing_refs.append((ref[0], str(ref[1]), getattr(entry, "agency_id", None)))
    billing_cache: dict = {}
    try:
        from billing.manager_billing import billing_next_actions_bulk

        billing_cache = billing_next_actions_bulk(billing_refs)
    except Exception:
        billing_cache = {}

    billing_url_cache: dict[tuple[str, str, int], int | None] = {}
    for application_type, application_id, client_id in billing_refs:
        key = (str(application_type), str(application_id), int(client_id or 0))
        billing_url_cache.setdefault(key, None)
    if billing_url_cache:
        try:
            from billing.models import BillingApplication

            application_filter = Q(pk__in=[])
            for application_type, application_id, client_id in billing_refs:
                item_filter = Q(
                    application_type=application_type,
                    application_id=str(application_id),
                )
                if client_id:
                    item_filter &= Q(client_id=client_id)
                application_filter |= item_filter
            applications = BillingApplication.objects.filter(application_filter).only(
                "id",
                "application_type",
                "application_id",
                "client_id",
                "updated_at",
            ).order_by("-updated_at", "-id")
            for application in applications:
                key = (
                    str(application.application_type),
                    str(application.application_id),
                    int(application.client_id or 0),
                )
                if key in billing_url_cache and billing_url_cache[key] is None:
                    billing_url_cache[key] = int(application.id)
        except Exception:
            billing_url_cache = {}

    for task in tasks:
        task._routing_cache = routing_cache
        task._billing_cache = billing_cache
        task._billing_url_cache = billing_url_cache
    return routing_cache, billing_cache


def _build_manager_task_rows(entries: list[OrderAuditEntry], request, *, live_top: bool = True) -> dict:
    now = timezone.localtime()
    latest_by_key = {_entry_key(entry): entry for entry in entries}
    # Активные задачи менеджерской зоны (как task_panel): без done, роли manager/logistician.
    tasks = list(
        Task.objects.select_related("assigned_to")
        .exclude(status="done")
        .filter(Q(assigned_to__role__in=("manager", "logistician")) | Q(assigned_to__isnull=True))
        .order_by("status", "due_date", "-updated_at")[:MANAGER_DASHBOARD_TASK_LIMIT]
    )
    _attach_shipping_task_orders(tasks, latest_by_key)
    _preload_audit_payloads(
        entry
        for task in tasks
        for ref in [_task_order_ref(task)]
        for entry in [latest_by_key.get(ref) if ref else None]
        if entry is not None
    )
    _preload_task_row_caches(tasks, latest_by_key)
    # Все статусы shipping/routing уже подгружены пакетно. Повторный live-resolve
    # на каждую видимую строку создаёт N+1 и не даёт более точного статуса.
    all_rows = [_task_row(task, latest_by_key, now, live_status=False) for task in tasks]
    filters = _resolve_task_filters(request)
    is_desk = filters["section"] == "desk"
    billing_rows = [
        row for row in all_rows if _is_finance_next_step(str(row.get("next_step") or ""))
    ]
    rows = [
        row for row in all_rows if not _is_finance_next_step(str(row.get("next_step") or ""))
    ] if is_desk else all_rows
    if is_desk:
        # Новые операционные заявки текущего дня всегда находятся в начале стола.
        rows.sort(key=lambda row: 0 if row.get("is_submitted_today") else 1)
    employee = get_request_employee(request)
    covered_ids = set(covered_principal_ids(employee)) if employee else set()
    filtered_source = _filtered_task_rows(
        rows,
        request,
        filters=filters,
        employee=employee,
        covered_ids=covered_ids,
    )
    metric_source = _filtered_task_rows(
        rows,
        request,
        skip_filters={"focus", "overdue", "attention"},
        filters=filters,
        employee=employee,
        covered_ids=covered_ids,
    )
    type_source = _filtered_task_rows(
        rows,
        request,
        skip_filters={"type", "focus", "overdue", "attention"},
        filters=filters,
        employee=employee,
        covered_ids=covered_ids,
    )
    option_source = _filtered_task_rows(
        rows,
        request,
        skip_filters={
            "type",
            "status",
            "client",
            "warehouse",
            "due",
            "focus",
            "overdue",
            "attention",
            "q",
        },
        filters=filters,
        employee=employee,
        covered_ids=covered_ids,
    )
    employee_id = getattr(employee, "id", None)
    can_see_all_billing = bool(
        employee
        and any(
            employee_has_role(employee, role)
            for role in {"head_manager", "director", "admin", "developer"}
        )
    )
    billing_attention_count = sum(
        1
        for row in billing_rows
        if can_see_all_billing
        or not row.get("executor_id")
        or row.get("executor_id") == employee_id
        or row.get("executor_id") in covered_ids
    )
    page_size = _resolve_task_page_size(request)
    page, num_pages, start, end = _task_list_page(request, len(filtered_source))
    page_source = filtered_source[start:end]
    filtered_rows = []
    can_edit_manager_deadline = bool(
        employee and _team_request_role(request) in DEADLINE_MANAGER_ROLES
    )
    for row in page_source:
        task_obj = row.get("_task_obj")
        if task_obj is not None:
            live_row = dict(row)
            live_row["can_edit_deadline"] = bool(
                can_edit_manager_deadline
                and task_obj.status != "done"
                and task_obj.kind != Task.KIND_WAREHOUSE_INTERNAL
                and (
                    not task_obj.assigned_to_id
                    or str(getattr(task_obj.assigned_to, "role", "") or "") in CABINET_ROLES
                )
            )
            live_row["can_transfer"] = bool(
                employee
                and (
                    (not task_obj.assigned_to_id)
                    or task_obj.assigned_to_id == employee.id
                    or any(
                        employee_has_role(employee, role)
                        for role in {"head_manager", "director", "admin", "developer"}
                    )
                    or task_obj.assigned_to_id in covered_ids
                )
            )
            live_row["can_claim"] = bool(
                employee and (not task_obj.assigned_to_id or task_obj.assigned_to_id == employee.id)
            )
            live_row.pop("_task_obj", None)
            filtered_rows.append(live_row)
        else:
            row["can_transfer"] = bool(employee) and (
                not row.get("executor_id")
                or row.get("executor_id") == getattr(employee, "id", None)
                or any(
                    employee_has_role(employee, role)
                    for role in {"head_manager", "director", "admin", "developer"}
                )
                or row.get("executor_id") in covered_ids
            )
            row["can_claim"] = not row.get("executor_id")
            row.pop("_task_obj", None)
            filtered_rows.append(row)
    for row in rows:
        row.pop("_task_obj", None)
    type_counts = {
        code: sum(1 for row in type_source if row["type"] == code)
        for code in ("receiving", "processing", "shipping", "logistics")
    }
    status_values = sorted({row["status"] for row in option_source})
    client_options = []
    seen_clients = set()
    for row in option_source:
        client_id = row.get("client_id")
        if not client_id or client_id in seen_clients:
            continue
        seen_clients.add(client_id)
        client_options.append({"id": client_id, "name": row["client_name"]})
        if len(client_options) >= 50:
            break
    warehouse_options = []
    seen_warehouses = set()
    for row in option_source:
        label = str(row.get("warehouse") or "").strip()
        if not label or label == "—" or label in seen_warehouses:
            continue
        seen_warehouses.add(label)
        warehouse_options.append(label)
        if len(warehouse_options) >= 50:
            break
    warehouse_options.sort()
    focus_counts = {
        "today": sum(1 for row in metric_source if row.get("is_submitted_today")),
        "overdue": sum(1 for row in metric_source if row.get("focus") == "overdue"),
        "stuck": sum(1 for row in metric_source if row.get("focus") == "stuck"),
        "no_owner": sum(1 for row in metric_source if row.get("focus") == "no_owner"),
        "docs": sum(1 for row in metric_source if row.get("focus") == "docs"),
        "waiting_client": sum(1 for row in metric_source if _is_waiting_client_row(row)),
        "waiting_warehouse": sum(1 for row in metric_source if row.get("status_css") == "accent"),
    }
    desk_summary = {
        "mine": sum(
            1
            for row in metric_source
            if row.get("executor_id") == employee_id or row.get("executor_id") in covered_ids
        ),
        "unassigned": sum(1 for row in metric_source if not row.get("executor_id")),
        "attention": sum(
            1
            for row in metric_source
            if row.get("is_overdue")
            or row.get("stuck_reason")
            or row.get("executor") in {"—", "", None}
        ),
    }
    range_start = start + 1 if filtered_source else 0
    range_end = min(start + len(page_source), len(filtered_source))
    manager_focus_links = {
        key: _task_focus_query(filters, key)
        for key in (
            "desk",
            "today",
            "overdue",
            "stuck",
            "no_owner",
            "docs",
            "attention",
            "waiting_client",
            "waiting_warehouse",
            "all",
        )
    }
    for key, scope in (("scope_mine", "mine"), ("scope_unassigned", "unassigned")):
        scoped_filters = {**filters, "scope": scope}
        manager_focus_links[key] = _task_focus_query(scoped_filters, "desk")
    return {
        "manager_task_source_truncated": len(tasks) >= MANAGER_DASHBOARD_TASK_LIMIT,
        "manager_task_source_limit": MANAGER_DASHBOARD_TASK_LIMIT,
        "manager_focus_links": manager_focus_links,
        "manager_task_rows": filtered_rows,
        "manager_task_total": len(metric_source),
        "manager_task_filtered_total": len(filtered_source),
        "manager_task_range_start": range_start,
        "manager_task_range_end": range_end,
        "manager_task_page": page,
        "manager_task_num_pages": num_pages,
        "manager_task_page_size": page_size,
        "manager_task_page_size_options": MANAGER_TASK_PAGE_SIZE_OPTIONS,
        "manager_task_has_prev": page > 1,
        "manager_task_has_next": page < num_pages,
        "manager_task_page_numbers": list(range(1, num_pages + 1)),
        "manager_task_prev_qs": _task_pager_query(filters, page=page - 1, page_size=page_size) if page > 1 else "",
        "manager_task_next_qs": _task_pager_query(filters, page=page + 1, page_size=page_size) if page < num_pages else "",
        "manager_task_page_links": [
            {"number": n, "qs": _task_pager_query(filters, page=n, page_size=page_size), "active": n == page}
            for n in range(1, num_pages + 1)
        ],
        "manager_task_type_tabs": [
            {"key": "all", "label": "Все процессы", "count": len(type_source)},
            {"key": "receiving", "label": TASK_TYPE_META["receiving"]["tab"], "count": type_counts["receiving"]},
            {"key": "processing", "label": TASK_TYPE_META["processing"]["tab"], "count": type_counts["processing"]},
            {"key": "shipping", "label": TASK_TYPE_META["shipping"]["tab"], "count": type_counts["shipping"]},
            {"key": "logistics", "label": TASK_TYPE_META["logistics"]["tab"], "count": type_counts["logistics"]},
        ],
        "manager_status_options": status_values,
        "manager_client_options": client_options,
        "manager_warehouse_options": warehouse_options,
        "manager_due_options": [{"key": k, "label": v} for k, v in MANAGER_DUE_OPTIONS],
        "manager_filters": {
            "type": filters["type"],
            "status": filters["status"],
            "client": filters["client"],
            "warehouse": filters.get("warehouse") or "all",
            "due": filters.get("due") or "all",
            "q": filters["q"],
            "focus": filters["focus"],
            "overdue": filters["overdue"],
            "attention": filters["attention"],
            "scope": filters["scope"],
            "lane": filters["lane"],
        },
        "manager_scope_options": (
            [{"key": "mine_or_unassigned", "label": "Мои и свободные"}]
            if is_desk and filters["role"] in {"manager", "logistician"}
            else []
        ) + [{"key": k, "label": v} for k, v in SCOPE_CHOICES],
        "manager_desk_summary": desk_summary,
        "manager_billing_attention_count": billing_attention_count,
        "manager_lane_options": [
            {"key": "all", "label": "Все типы"},
            {"key": "ops_logistics", "label": "Отгрузки и рейсы"},
        ],
        "manager_task_kpi": {
            "today": focus_counts["today"],
            "overdue": focus_counts["overdue"],
            "stuck": focus_counts["stuck"],
            "without_executor": focus_counts["no_owner"],
            "docs": focus_counts["docs"],
            "waiting_client": focus_counts["waiting_client"],
            "waiting_warehouse": focus_counts["waiting_warehouse"],
            # backward-compatible aliases used by older template bits / tests
            "attention": focus_counts["stuck"],
            "review": focus_counts["docs"],
        },
        "_manager_task_rows_all": rows,
    }


def _manager_notifications(
    waiting_rows: list[dict],
    task_rows: list[dict],
    *,
    dismissed_ids: set[str] | None = None,
) -> list[dict]:
    """New events only — no repeating overdue spam."""
    notices = []
    seen = set()
    dismissed_ids = dismissed_ids or set()

    def add_notice(notice: dict) -> bool:
        if notice["id"] in dismissed_ids:
            return False
        notices.append(notice)
        return len(notices) >= 5

    for row in waiting_rows:
        key = f"waiting:{row.get('number')}"
        if key in seen:
            continue
        seen.add(key)
        if add_notice(
            {
                "id": key,
                "title": f"Новая на проверке: №{row['number']}",
                "subtitle": row.get("type_label") or "Заявка",
                "url": row.get("url") or "/team-manager/",
                "tone": "blue",
            }
        ):
            return notices
    for row in task_rows:
        step = str(row.get("next_step") or "")
        if step.startswith("Уточнить данные"):
            key = f"clarify:{row.get('id') or row.get('number')}"
            if key in seen:
                continue
            seen.add(key)
            if add_notice(
                {
                    "id": key,
                    "title": f"Уточнение логистики: №{row['number']}",
                    "subtitle": step,
                    "url": row.get("url") or "/team-manager/",
                    "tone": "orange",
                }
            ):
                return notices
    for row in task_rows:
        if row.get("focus") != "stuck":
            continue
        key = f"stuck:{row.get('id') or row.get('number')}"
        if key in seen:
            continue
        seen.add(key)
        if add_notice(
            {
                "id": key,
                "title": f"Зависло: №{row['number']}",
                "subtitle": row.get("stuck_reason") or row.get("next_step") or "",
                "url": row.get("url") or "/team-manager/",
                "tone": "orange",
            }
        ):
            break
    return notices


def _dismissed_manager_notification_ids(request) -> set[str]:
    stored = request.session.get("team_manager_dismissed_notifications", [])
    if not isinstance(stored, list):
        return set()
    return {str(item) for item in stored if isinstance(item, str)}


def _manager_notifications_for_request(request, waiting_rows: list[dict], task_rows: list[dict]) -> list[dict]:
    return _manager_notifications(
        waiting_rows,
        task_rows,
        dismissed_ids=_dismissed_manager_notification_ids(request),
    )


def build_manager_dashboard_context(user, entries: list[OrderAuditEntry] | None = None) -> dict:
    entries = entries if entries is not None else _latest_request_entries()
    waiting_entries = [entry for entry in entries if _is_waiting_manager(_display_status_entry(entry))]
    draft_entries = [entry for entry in entries if _is_draft(_display_status_entry(entry))]

    request_rows = []
    top_waiting_entries = sorted(
        waiting_entries,
        key=lambda item: item.created_at,
        reverse=True,
    )[:8]
    _preload_audit_payloads(top_waiting_entries)
    for entry in top_waiting_entries:
        data = _payload(entry)
        status_entry = _display_status_entry(entry)
        agency = getattr(entry, "agency", None)
        quick_status = _quick_entry_status(status_entry)
        request_rows.append(
            {
                "number": _display_order_number(entry.order_type, entry.order_id),
                "type_label": _type_label(entry.order_type),
                "client_name": _agency_display_name(agency),
                "status_label": quick_status["label"] or data.get("status_label") or "Ждет подтверждения",
                "created_at": entry.created_at,
                "url": _request_url(entry),
                "client_url": build_staff_client_cabinet_url(entry.agency_id) if entry.agency_id else "#",
            }
        )

    return {
        "manager_stats": {"client_drafts": len(draft_entries)},
        "manager_request_rows": request_rows,
    }


def build_manager_workspace_context(user, request) -> dict:
    entries = _latest_request_entries(
        limit=MANAGER_DASHBOARD_REQUEST_LIMIT,
        include_latest_payload=False,
    )
    context = build_manager_dashboard_context(user, entries)
    task_context = _build_manager_task_rows(entries, request)
    all_task_rows = task_context.pop("_manager_task_rows_all", None) or []
    context.update(task_context)
    context["manager_stats"].update(task_context["manager_task_kpi"])
    employee = getattr(user, "employee_profile", None)
    kpi = task_context.get("manager_task_kpi") or {}
    context["manager_control"] = {
        "waiting_client": int(kpi.get("waiting_client") or 0),
        "waiting_warehouse": int(kpi.get("waiting_warehouse") or 0),
    }
    # Уведомления по полному KPI-набору, не только по текущей странице таблицы.
    context["manager_notifications"] = _manager_notifications_for_request(
        request,
        context.get("manager_request_rows") or [],
        all_task_rows or context.get("manager_task_rows") or [],
    )
    emp = get_request_employee(request) or employee
    context["cabinet_colleagues"] = [
        {"id": e.id, "name": e.full_name or f"#{e.id}", "role": e.role}
        for e in cabinet_colleagues(exclude_id=getattr(emp, "id", None))
    ]
    context["transfer_reasons"] = [{"key": k, "label": v} for k, v in TRANSFER_REASONS]
    context["can_bulk_reassign"] = bool(emp and emp.role in {"head_manager", "director", "admin", "developer", "manager", "logistician"})
    return context


class TeamManagerDashboard(RoleRequiredMixin, TemplateView):
    template_name = "teammanager/dashboard.html"
    allowed_roles = CABINET_ROLES

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        role = _team_request_role(self.request)
        section = str(self.request.GET.get("section") or "desk").strip().lower()
        if section not in {"desk", "tasks"}:
            section = "desk"
        ctx["role"] = role or "manager"
        ctx["title"] = "Team Manager"
        ctx["manager_section"] = section
        ctx["active_nav"] = "desk" if section == "desk" else "todo"
        ctx["employee_name"] = (
            getattr(getattr(self.request.user, "employee_profile", None), "full_name", None)
            or self.request.session.get("employee_name")
            or self.request.user.get_full_name()
            or self.request.user.username
        )
        ctx.update(build_manager_workspace_context(self.request.user, self.request))
        return ctx


class TeamManagerTaskEventsApi(RoleRequiredMixin, View):
    """Lightweight poll payload for tasks screen (60s). Preserves filters via query string."""

    allowed_roles = CABINET_ROLES

    def get(self, request, *args, **kwargs):
        # Не пересобираем полный workspace (audit 500 + dashboard stats) — только задачи.
        entries = _latest_request_entries(
            limit=MANAGER_DASHBOARD_REQUEST_LIMIT,
            include_latest_payload=False,
        )
        task_context = _build_manager_task_rows(entries, request, live_top=False)
        all_task_rows = task_context.pop("_manager_task_rows_all", None) or []
        notifications = _manager_notifications_for_request(request, [], all_task_rows)
        kpi = task_context.get("manager_task_kpi") or {}
        control = {
            "waiting_client": int(kpi.get("waiting_client") or 0),
            "waiting_warehouse": int(kpi.get("waiting_warehouse") or 0),
        }
        return JsonResponse(
            {
                "ok": True,
                "data": {
                    "kpi": {
                        "today": kpi.get("today", 0),
                        "overdue": kpi.get("overdue", 0),
                        "stuck": kpi.get("stuck", 0),
                        "without_executor": kpi.get("without_executor", 0),
                        "docs": kpi.get("docs", 0),
                    },
                    "control": control,
                    "notifications": notifications,
                    "filtered_total": task_context.get("manager_task_filtered_total", 0),
                    "total": task_context.get("manager_task_total", 0),
                },
            }
        )


class TeamManagerNotificationReadApi(RoleRequiredMixin, View):
    """Stores dismissed workspace notifications in the current manager session."""

    allowed_roles = CABINET_ROLES

    def post(self, request, *args, **kwargs):
        try:
            payload = json.loads(request.body.decode("utf-8") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError):
            return JsonResponse({"ok": False, "error": "Некорректные данные"}, status=400)
        event_id = str(payload.get("id") or "").strip()
        if not event_id.startswith(("waiting:", "clarify:", "stuck:")):
            return JsonResponse({"ok": False, "error": "Неизвестное уведомление"}, status=400)
        dismissed = _dismissed_manager_notification_ids(request)
        dismissed.add(event_id)
        request.session["team_manager_dismissed_notifications"] = sorted(dismissed)[-200:]
        request.session.modified = True
        return JsonResponse({"ok": True})


class TeamManagerClientsView(RoleRequiredMixin, TemplateView):
    """Clients directory inside manager cabinet (LK shell)."""

    template_name = "teammanager/clients.html"
    allowed_roles = CABINET_ROLES
    paginate_by = 30

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        request = self.request
        from accountant.selectors import manager_visible_agencies

        queryset = build_client_list_queryset(
            request=request,
            base_queryset=manager_visible_agencies(Agency.objects.select_related("portal_user", "lifecycle")),
            sort_fields=CLIENT_SORT_FIELDS,
            filter_fields=CLIENT_FILTER_FIELDS,
            default_sort="name",
        )
        paginator = Paginator(queryset, self.paginate_by)
        page_obj = paginator.get_page(request.GET.get("page") or 1)
        list_ctx = build_client_list_context(
            request=request,
            items=page_obj.object_list,
            view_modes=("table",),
            sort_fields=CLIENT_SORT_FIELDS,
            default_sort="name",
        )
        client_rows = []
        for agency in page_obj.object_list:
            client_rows.append(
                {
                    "agency": agency,
                    "lk_url": build_staff_client_cabinet_url(agency.id, return_url="/team-manager/clients/"),
                    "edit_url": f"/client/{agency.id}/edit/",
                    "archive_url": f"/client/{agency.id}/archive/?next=/team-manager/clients/",
                }
            )
        ctx.update(list_ctx)
        ctx.update(
            {
                "role": "manager",
                "title": "Клиенты",
                "active_nav": "clients",
                "page_obj": page_obj,
                "client_rows": client_rows,
                "total_count": paginator.count,
            }
        )
        return ctx


def _manager_request_row(
    entry: OrderAuditEntry,
    *,
    live_status: bool = True,
    shipping_order: ShippingOrder | None = None,
    audit_meta: dict | None = None,
) -> dict:
    data = _payload(entry)
    agency = getattr(entry, "agency", None)
    order_type = _normalize_order_type(entry.order_type)
    allow_status_query = bool(live_status and order_type != "shipping")
    status_entry = _display_status_entry(entry, allow_query=allow_status_query)
    status_data = _payload(status_entry)
    if order_type == "shipping" and shipping_order is None:
        shipping_order = _shipping_order_for_entry(entry, details=False)
    shipping_payload = _shipping_order_payload(shipping_order) if shipping_order is not None else {}
    history_meta = audit_meta or {}
    # Непустое: ShippingOrder → история audit → latest entry.
    display_meta = _merge_payload_meta(shipping_payload, history_meta, data)
    data = {**data, **shipping_payload, **display_meta}
    if shipping_order is not None:
        if _is_cancel_requested(status_entry) or _is_warehouse_cancel_requested(status_entry):
            status_data = {**shipping_payload, **status_data}
        else:
            status_data = {**status_data, **shipping_payload}
    # Для приёмки в колонке «Склад» показываем тип мест / тип товара, если склада нет.
    if order_type == "receiving" and _warehouse_label(data) == "—":
        place = str(data.get("place_type") or "").strip().lower()
        place_label = {"pallet": "Паллет", "box": "Короб", "bag": "Мешок"}.get(place, "")
        goods_label = str(data.get("goods_type_label") or "").strip()
        fallback_wh = " · ".join(part for part in (place_label, goods_label) if part)
        if fallback_wh:
            data["warehouse"] = fallback_wh
    quick_status = _quick_entry_status(status_entry, payload=status_data)
    resolved = None
    if live_status and order_type != "shipping":
        try:
            resolved = resolve_lk_entry_status(status_entry, audience="default")
        except Exception:
            resolved = None
    status_label = (
        str(getattr(resolved, "status_label", "") or "").strip()
        or str(status_data.get("status_label") or "").strip()
        or str(quick_status["label"] or "").strip()
    )
    status_label = _repair_manager_mojibake_text(status_label).strip()
    if not status_label or status_label in {"—", "-"} or status_label.lower() in {
        "shipped", "packed", "picking", "reserved", "submitted", "draft", "partial_shipped", "canceled", "cancelled",
    }:
        # Не оставляем пустой/сырой код статуса: берём display ShippingOrder или русский fallback.
        if shipping_order is not None and hasattr(shipping_order, "get_status_display"):
            status_label = str(shipping_order.get_status_display() or "").strip() or status_label
        elif order_type == "shipping":
            status_label = {
                "draft": "Черновик",
                "submitted": "На согласовании менеджера",
                "reserved": "Согласована и передана в работу кладовщику",
                "storekeeper_accepted": "Принята в работу складом",
                "picking": "Доставка в зону отгрузки (ричтрак)",
                "packed": "Подготовлена складом, ожидает логиста",
                "shipped": "Отгружена",
                "partial_shipped": "Отгружена частично",
                "canceled": "Отменена",
                "cancelled": "Отменена",
            }.get(str(status_data.get("status") or status_data.get("shipping_state") or "").strip().lower(), status_label)
        if not status_label or status_label in {"—", "-"}:
            status_label = "—"
    bucket = str(getattr(resolved, "bucket", "") or quick_status["bucket"]).strip()
    act_url = _act_url_for_row(
        order_type=order_type,
        order_id=str(entry.order_id or ""),
        data={**status_data, **data},
        shipping_order=shipping_order,
    )
    needs_act_sign = _act_needs_manager_sign({**status_data, **data}, order_type)
    if needs_act_sign:
        bucket = "manager"
        next_step = "Подписать акт"
        if not status_label or status_label == "—":
            status_label = "Акт ожидает подписи менеджера"
    else:
        next_step = str(getattr(resolved, "next_step", "") or "").strip()
        next_step = _repair_manager_mojibake_text(next_step).strip()
        if not next_step:
            if order_type == "other" and bucket == "manager":
                next_step = "Подтвердить и отправить на склад"
            else:
                next_step = "Проверить заявку"
    ozon_summary = _shipping_ozon_summary(data, shipping_order) if order_type == "shipping" and live_status else {
        "ozon_supply_label": "",
        "ozon_gm_summary": "",
    }
    submitted_at = getattr(entry, "_manager_submitted_at", None) or entry.created_at
    warehouse_request_route = ""
    if order_type == "shipping" and shipping_order is not None:
        warehouse_request_route = f"/shipping/{shipping_order.pk}/"
    elif order_type in {"receiving", "processing", "other"}:
        warehouse_request_route = f"/orders/{order_type}/{entry.order_id}/"
    return {
        "_entry_obj": entry,
        "_warehouse_request_route": warehouse_request_route,
        "number": _display_order_number(order_type, entry.order_id),
        "order_id": str(entry.order_id or ""),
        "type": order_type,
        "type_label": _type_label(order_type),
        "type_css": TASK_TYPE_META.get(order_type, TASK_TYPE_META["other"])["css"],
        "client_name": _agency_display_name(agency),
        "client_inn": getattr(agency, "inn", None) or "",
        "client_id": getattr(agency, "id", None),
        "marketplace": _marketplace_label(data),
        "warehouse": _warehouse_label(data),
        "status": status_label,
        "status_css": (
            "green" if bucket == "done" else
            "accent" if bucket == "warehouse" else
            "sun" if bucket == "manager" else
            "gray"
        ),
        "bucket": bucket or "manager",
        "next_step": next_step,
        "created_at": timezone.localtime(entry.created_at) if entry.created_at else None,
        "updated_at": timezone.localtime(entry.created_at) if entry.created_at else None,
        "submitted_at": timezone.localtime(submitted_at) if submitted_at else None,
        "url": _request_url(entry, shipping_order=shipping_order),
        "act_url": act_url,
        "boxes_url": f"/shipping/{shipping_order.pk}/boxes.xlsx" if order_type == "shipping" and shipping_order is not None else "",
        "client_url": build_staff_client_cabinet_url(entry.agency_id) if entry.agency_id else "#",
        "description": _manager_event_description(
            entry,
            data={**status_data, **data},
            status_label=status_label,
            next_step=next_step,
            shipping_order=shipping_order,
        ),
        "ozon_supply_label": ozon_summary["ozon_supply_label"],
        "ozon_gm_summary": ozon_summary["ozon_gm_summary"],
    }


def _shipping_orders_by_number(
    entries: list[OrderAuditEntry],
    *,
    include_items: bool = False,
) -> dict[tuple[int, str], ShippingOrder]:
    numbers = {
        str(entry.order_id or "").strip()
        for entry in entries
        if _normalize_order_type(entry.order_type) == "shipping" and str(entry.order_id or "").strip()
    }
    if not numbers:
        return {}
    agency_ids = {
        entry.agency_id
        for entry in entries
        if _normalize_order_type(entry.order_type) == "shipping" and entry.agency_id
    }
    qs = ShippingOrder.objects.filter(
        number__in=numbers,
        agency_id__in=agency_ids,
    ).select_related("marketplace", "agency")
    prefetches = ["destinations"]
    if include_items:
        prefetches.append("items")
    qs = qs.prefetch_related(*_shipping_prefetches(*prefetches))
    return {
        (order.agency_id, str(order.number or "").strip()): order
        for order in qs
        if str(order.number or "").strip()
    }


ORDERS_PAGE_SIZE = 20
ORDERS_PAGE_SIZE_OPTIONS = (10, 20, 50, 100)
ORDERS_PERIOD_OPTIONS = (
    ("all", "Любой срок"),
    ("today", "Сегодня"),
    ("yesterday", "Вчера"),
    ("week", "7 дней"),
    ("older", "Старше 7 дней"),
)

REPORT_PERIOD_OPTIONS = (
    ("today", "Сегодня"),
    ("week", "7 дней"),
    ("month", "30 дней"),
    ("all", "Всё время"),
)

REPORT_TYPE_ORDER = ("receiving", "shipping", "processing", "other")
REPORT_BUCKET_ORDER = ("manager", "warehouse", "client", "done")
REPORT_BUCKET_LABELS = {
    "manager": "У менеджера",
    "warehouse": "На складе",
    "client": "У клиента",
    "done": "Завершено",
}
REPORT_BUCKET_CSS = {
    "manager": "sun",
    "warehouse": "accent",
    "client": "gray",
    "done": "green",
}


def _orders_page_size(request) -> int:
    try:
        value = int(request.GET.get("page_size") or ORDERS_PAGE_SIZE)
    except (TypeError, ValueError):
        value = ORDERS_PAGE_SIZE
    return value if value in ORDERS_PAGE_SIZE_OPTIONS else ORDERS_PAGE_SIZE


def _percent(value: int, total: int) -> int:
    if not total:
        return 0
    return max(0, min(100, round(int(value) * 100 / int(total))))


def _period_date(value):
    if not value:
        return None
    try:
        return timezone.localtime(value).date()
    except Exception:
        return getattr(value, "date", lambda: None)()


def _matches_report_period(value, period: str, today) -> bool:
    if period == "all":
        return True
    value_date = _period_date(value)
    if value_date is None:
        return False
    age_days = (today - value_date).days
    if period == "today":
        return age_days == 0
    if period == "month":
        return 0 <= age_days <= 30
    return 0 <= age_days <= 7


def _orders_pager_query(filters: dict, *, page: int, page_size: int) -> str:
    from urllib.parse import urlencode

    params = {
        "type": filters.get("type") or "all",
        "status": filters.get("status") or "all",
        "client": filters.get("client") or "all",
        "page": str(page),
    }
    warehouse = filters.get("warehouse") or "all"
    if warehouse != "all":
        params["warehouse"] = warehouse
    period = filters.get("period") or "all"
    if period != "all":
        params["period"] = period
    q = (filters.get("q") or "").strip()
    if q:
        params["q"] = q
    if page_size != ORDERS_PAGE_SIZE:
        params["page_size"] = str(page_size)
    return urlencode(params)


def _order_notifications(rows: list[dict], *, limit: int = 5) -> list[dict]:
    """Заявки, где мяч сейчас у менеджера — та же логика, что и KPI «Требует менеджера»."""
    candidates = [row for row in rows if row.get("bucket") == "manager" and row.get("updated_at")]
    candidates.sort(key=lambda row: row["updated_at"], reverse=True)
    notices = []
    for row in candidates[:limit]:
        notices.append(
            {
                "id": f"order:{row.get('number')}",
                "title": row.get("next_step") or "Требует внимания менеджера",
                "subtitle": f"№{row.get('number')}",
                "time": row["updated_at"],
                "url": row.get("url") or "/team-manager/orders/",
                "tone": "orange",
            }
        )
    return notices


def _manager_orders_context(request) -> dict:
    selected_type = request.GET.get("type") or "all"
    entries = sorted(
        _latest_request_entries(limit=1500, order_type=selected_type),
        key=lambda item: item.created_at,
        reverse=True,
    )
    # Клиентские черновики (draft-*) не показываем как «заявки» менеджеру —
    # иначе после отправки сверху висит ложный «Черновик».
    entries = [
        entry
        for entry in entries
        if not str(entry.order_id or "").strip().lower().startswith("draft-")
        and not _is_draft(_display_status_entry(entry))
    ]
    shipping_map = _shipping_orders_by_number(entries)
    # Full audit history meta is expensive — skip on list materialization.
    # Latest payload + ShippingOrder cover filters/KPI; page rows get history meta.
    rows = [
        _manager_request_row(
            entry,
            live_status=False,
            shipping_order=shipping_map.get((entry.agency_id, str(entry.order_id or "").strip())),
            audit_meta=None,
        )
        for entry in entries
    ]
    selected_status = request.GET.get("status") or "all"
    selected_client = request.GET.get("client") or "all"
    selected_warehouse = request.GET.get("warehouse") or "all"
    selected_period = request.GET.get("period") or "all"
    query = (request.GET.get("q") or "").strip().lower()
    now_local = timezone.localtime()
    today_local = now_local.date()
    filtered = []
    for row in rows:
        if selected_type != "all" and row["type"] != selected_type:
            continue
        if selected_status != "all" and row["bucket"] != selected_status:
            continue
        if selected_client != "all" and str(row.get("client_id") or "") != selected_client:
            continue
        if selected_warehouse != "all" and str(row.get("warehouse") or "") != selected_warehouse:
            continue
        if selected_period != "all":
            submitted = row.get("submitted_at")
            if not submitted:
                continue
            submitted_date = timezone.localtime(submitted).date()
            age_days = (today_local - submitted_date).days
            if selected_period == "today" and age_days != 0:
                continue
            if selected_period == "yesterday" and age_days != 1:
                continue
            if selected_period == "week" and not (0 <= age_days <= 7):
                continue
            if selected_period == "older" and age_days <= 7:
                continue
        if query:
            haystack = " ".join(
                str(row.get(key) or "")
                for key in (
                    "number",
                    "order_id",
                    "type_label",
                    "client_name",
                    "client_inn",
                    "marketplace",
                    "warehouse",
                    "status",
                    "ozon_supply_label",
                    "ozon_gm_summary",
                )
            ).lower()
            if query not in haystack:
                continue
        filtered.append(row)

    page_size = _orders_page_size(request)
    paginator = Paginator(filtered, page_size)
    page_obj = paginator.get_page(request.GET.get("page") or 1)
    page_entries = [row["_entry_obj"] for row in page_obj.object_list]
    audit_meta_map = _audit_meta_by_order(page_entries)
    page_rows = [
        _manager_request_row(
            row["_entry_obj"],
            live_status=True,
            shipping_order=shipping_map.get(
                (row["_entry_obj"].agency_id, str(row.get("order_id") or "").strip())
            ),
            audit_meta=audit_meta_map.get(_request_identity(row["_entry_obj"])),
        )
        for row in page_obj.object_list
    ]
    route_set = {
        str(row.get("_warehouse_request_route") or "").strip()
        for row in page_rows
        if str(row.get("_warehouse_request_route") or "").strip()
    }
    route_query = Q()
    for route in route_set:
        route_query |= Q(route__startswith=route)
    warehouse_tasks = list(
        Task.objects.select_related("assigned_to")
        .filter(route_query, assigned_to__role__in=WAREHOUSE_EXECUTOR_ROLES)
        .exclude(status="done")
        .order_by("route", "due_date", "id")
    ) if route_set else []
    attach_warehouse_deadline_context(warehouse_tasks)
    warehouse_deadline_by_route = {}
    for task in warehouse_tasks:
        route = canonical_warehouse_request_route(task.route)
        deadline = getattr(task, "effective_due_date", None) or task.due_date
        current = warehouse_deadline_by_route.get(route)
        if current is None or (deadline and deadline > current["due_date"]):
            change = getattr(task, "warehouse_deadline_change", None)
            warehouse_deadline_by_route[route] = {
                "task_id": task.id,
                "due_date": deadline,
                "change": change,
                "change_count": getattr(task, "warehouse_deadline_change_count", 0),
            }
    can_change_deadline = _team_request_role(request) in DEADLINE_MANAGER_ROLES
    for row in page_rows:
        route = str(row.pop("_warehouse_request_route", "") or "").strip()
        warehouse_deadline = warehouse_deadline_by_route.get(route)
        change = warehouse_deadline.get("change") if warehouse_deadline else None
        row["warehouse_deadline_task_id"] = (
            warehouse_deadline.get("task_id") if warehouse_deadline else None
        )
        row["warehouse_due_date"] = (
            warehouse_deadline.get("due_date") if warehouse_deadline else None
        )
        row["warehouse_deadline_reason"] = str(
            getattr(change, "reason", "") or ""
        )
        row["warehouse_deadline_change_count"] = (
            warehouse_deadline.get("change_count", 0) if warehouse_deadline else 0
        )
        row["can_change_warehouse_deadline"] = bool(
            can_change_deadline and warehouse_deadline
        )
        row.pop("_entry_obj", None)
    client_options = []
    seen_clients = set()
    for row in rows:
        client_id = row.get("client_id")
        if not client_id or client_id in seen_clients:
            continue
        seen_clients.add(client_id)
        client_options.append({"id": client_id, "name": row["client_name"]})
        if len(client_options) >= 80:
            break
    warehouse_options = []
    seen_warehouses = set()
    for row in rows:
        label = str(row.get("warehouse") or "").strip()
        if not label or label == "—" or label in seen_warehouses:
            continue
        seen_warehouses.add(label)
        warehouse_options.append(label)
        if len(warehouse_options) >= 50:
            break
    warehouse_options.sort()
    filters = {
        "type": selected_type,
        "status": selected_status,
        "client": selected_client,
        "warehouse": selected_warehouse,
        "period": selected_period,
        "q": request.GET.get("q") or "",
    }
    range_start = (page_obj.number - 1) * page_size + 1 if filtered else 0
    range_end = range_start + len(page_obj.object_list) - 1 if filtered else 0
    return {
        "order_rows": page_rows,
        "page_obj": page_obj,
        "orders_total": len(rows),
        "orders_filtered_total": len(filtered),
        "orders_range_start": range_start,
        "orders_range_end": range_end,
        "orders_page_size": page_size,
        "orders_page_size_options": ORDERS_PAGE_SIZE_OPTIONS,
        "orders_page_links": [
            {**link, "qs": _orders_pager_query(filters, page=link["number"], page_size=page_size) if not link.get("ellipsis") else ""}
            for link in inventory_pagination_links(page_obj)
        ],
        "orders_prev_qs": _orders_pager_query(filters, page=page_obj.previous_page_number(), page_size=page_size) if page_obj.has_previous() else "",
        "orders_next_qs": _orders_pager_query(filters, page=page_obj.next_page_number(), page_size=page_size) if page_obj.has_next() else "",
        "order_type_tabs": [
            {"key": "all", "label": "Все", "count": len(rows)},
            {"key": "receiving", "label": "Приёмка", "count": sum(1 for row in rows if row["type"] == "receiving")},
            {"key": "processing", "label": "Обработка", "count": sum(1 for row in rows if row["type"] == "processing")},
            {"key": "shipping", "label": "Отгрузки", "count": sum(1 for row in rows if row["type"] == "shipping")},
            {"key": "fbs_movement", "label": "FBS", "count": sum(1 for row in rows if row["type"] == "fbs_movement")},
            {"key": "other", "label": "Другие", "count": sum(1 for row in rows if row["type"] == "other")},
        ],
        "order_status_tabs": [
            {"key": "all", "label": "Все статусы"},
            {"key": "client", "label": "У клиента"},
            {"key": "manager", "label": "У менеджера"},
            {"key": "warehouse", "label": "На складе"},
            {"key": "done", "label": "Завершены"},
        ],
        "order_client_options": client_options,
        "order_warehouse_options": warehouse_options,
        "order_period_options": [{"key": k, "label": v} for k, v in ORDERS_PERIOD_OPTIONS],
        "order_filters": filters,
        "order_kpi": {
            "manager": sum(1 for row in rows if row["bucket"] == "manager"),
            "warehouse": sum(1 for row in rows if row["bucket"] == "warehouse"),
            "client": sum(1 for row in rows if row["bucket"] == "client"),
            "done": sum(1 for row in rows if row["bucket"] == "done"),
        },
        "order_notifications": _order_notifications(rows),
    }


def _manager_reports_context(request) -> dict:
    """Read-only manager report based on manager LK rows and audit snapshots.

    Не пересчитывает складские остатки и не меняет бизнес-статусы: берём уже
    существующие audit-события, ShippingOrder как источник карточки отгрузки и
    текущую очередь Task для SLA/ответственных.
    """

    selected_period = request.GET.get("period") or "week"
    period_keys = {key for key, _label in REPORT_PERIOD_OPTIONS}
    if selected_period not in period_keys:
        selected_period = "week"
    selected_type = request.GET.get("type") or "all"
    if selected_type not in {"all", *REPORT_TYPE_ORDER}:
        selected_type = "all"
    selected_client = request.GET.get("client") or "all"

    entries = sorted(
        _latest_request_entries(limit=2000),
        key=lambda item: item.created_at,
        reverse=True,
    )
    entries = [
        entry
        for entry in entries
        if not str(entry.order_id or "").strip().lower().startswith("draft-")
        and not _is_draft(_display_status_entry(entry))
    ]
    shipping_map = _shipping_orders_by_number(entries)
    rows = [
        _manager_request_row(
            entry,
            live_status=False,
            shipping_order=shipping_map.get((entry.agency_id, str(entry.order_id or "").strip())),
            audit_meta=None,
        )
        for entry in entries
    ]
    today = timezone.localdate()
    period_client_rows = []
    for row in rows:
        if selected_client != "all" and str(row.get("client_id") or "") != selected_client:
            continue
        if not _matches_report_period(row.get("updated_at"), selected_period, today):
            continue
        period_client_rows.append(row)
    filtered_rows = [
        row
        for row in period_client_rows
        if selected_type == "all" or row["type"] == selected_type
    ]

    for row in rows:
        row.pop("_entry_obj", None)
    for row in filtered_rows:
        row.pop("_entry_obj", None)

    total = len(filtered_rows)
    bucket_counts = Counter(row.get("bucket") or "manager" for row in filtered_rows)
    type_counts = Counter(row.get("type") or "other" for row in period_client_rows)
    marketplace_counts = Counter(
        str(row.get("marketplace") or "—").strip() or "—"
        for row in filtered_rows
    )
    warehouse_counts = Counter(
        str(row.get("warehouse") or "—").strip() or "—"
        for row in filtered_rows
        if str(row.get("warehouse") or "—").strip() not in {"", "—"}
    )

    task_context = _build_manager_task_rows(
        _latest_request_entries(limit=MANAGER_DASHBOARD_REQUEST_LIMIT),
        request,
        live_top=False,
    )
    all_task_rows = task_context.pop("_manager_task_rows_all", None) or []
    report_task_rows = []
    for row in all_task_rows:
        if selected_type != "all" and row.get("type") != selected_type:
            continue
        if selected_client != "all" and str(row.get("client_id") or "") != selected_client:
            continue
        if not _matches_report_period(row.get("updated_at") or row.get("created_at"), selected_period, today):
            continue
        report_task_rows.append(row)

    client_map: dict[str, dict] = {}
    for row in filtered_rows:
        client_id = str(row.get("client_id") or "")
        if not client_id:
            continue
        item = client_map.setdefault(
            client_id,
            {
                "id": client_id,
                "name": row.get("client_name") or "—",
                "total": 0,
                "manager": 0,
                "warehouse": 0,
                "client": 0,
                "done": 0,
                "url": build_staff_client_cabinet_url(row.get("client_id")) if row.get("client_id") else "#",
            },
        )
        item["total"] += 1
        bucket = row.get("bucket") or "manager"
        if bucket in REPORT_BUCKET_ORDER:
            item[bucket] += 1
    top_clients = sorted(
        client_map.values(),
        key=lambda item: (item["manager"], item["warehouse"], item["total"]),
        reverse=True,
    )[:8]

    client_options = []
    seen_clients = set()
    for row in rows:
        client_id = row.get("client_id")
        if not client_id or client_id in seen_clients:
            continue
        seen_clients.add(client_id)
        client_options.append({"id": client_id, "name": row.get("client_name") or f"Клиент {client_id}"})
        if len(client_options) >= 80:
            break

    type_summary = []
    for type_key in REPORT_TYPE_ORDER:
        rows_for_type = [row for row in filtered_rows if row.get("type") == type_key]
        type_total = len(rows_for_type)
        type_summary.append(
            {
                "key": type_key,
                "label": _type_label(type_key),
                "count": type_total,
                "percent": _percent(type_total, total),
                "manager": sum(1 for row in rows_for_type if row.get("bucket") == "manager"),
                "warehouse": sum(1 for row in rows_for_type if row.get("bucket") == "warehouse"),
                "done": sum(1 for row in rows_for_type if row.get("bucket") == "done"),
            }
        )

    bucket_summary = [
        {
            "key": key,
            "label": REPORT_BUCKET_LABELS[key],
            "count": int(bucket_counts.get(key) or 0),
            "percent": _percent(bucket_counts.get(key) or 0, total),
            "css": REPORT_BUCKET_CSS[key],
        }
        for key in REPORT_BUCKET_ORDER
    ]

    oldest_date = timezone.now() - timedelta(days=3650)
    recent_rows = sorted(
        filtered_rows,
        key=lambda row: row.get("updated_at") or oldest_date,
        reverse=True,
    )[:10]

    return {
        "report_filters": {
            "period": selected_period,
            "type": selected_type,
            "client": selected_client,
        },
        "report_period_options": [{"key": key, "label": label} for key, label in REPORT_PERIOD_OPTIONS],
        "report_type_options": [
            {"key": "all", "label": "Все типы", "count": len(period_client_rows)},
            *[
                {"key": key, "label": _type_label(key), "count": int(type_counts.get(key) or 0)}
                for key in REPORT_TYPE_ORDER
            ],
        ],
        "report_client_options": client_options,
        "report_generated_at": timezone.localtime(),
        "report_total": total,
        "report_kpi": {
            "total": total,
            "manager": int(bucket_counts.get("manager") or 0),
            "warehouse": int(bucket_counts.get("warehouse") or 0),
            "client": int(bucket_counts.get("client") or 0),
            "done": int(bucket_counts.get("done") or 0),
            "overdue": sum(1 for row in report_task_rows if row.get("is_overdue")),
            "stuck": sum(1 for row in report_task_rows if row.get("focus") == "stuck"),
            "without_executor": sum(1 for row in report_task_rows if row.get("focus") == "no_owner"),
            "docs": sum(1 for row in report_task_rows if row.get("focus") == "docs"),
        },
        "report_bucket_summary": bucket_summary,
        "report_type_summary": type_summary,
        "report_marketplaces": [
            {"label": label, "count": count, "percent": _percent(count, total)}
            for label, count in marketplace_counts.most_common(8)
        ],
        "report_warehouses": [
            {"label": label, "count": count, "percent": _percent(count, total)}
            for label, count in warehouse_counts.most_common(8)
        ],
        "report_top_clients": top_clients,
        "report_recent_rows": recent_rows,
    }


class TeamManagerOrdersView(RoleRequiredMixin, TemplateView):
    """Unified request journal inside manager cabinet."""

    template_name = "teammanager/orders.html"
    allowed_roles = CABINET_ROLES

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        role = _team_request_role(self.request)
        ctx.update(
            {
                "role": role or "manager",
                "title": "Заявки",
                "active_nav": "orders",
            }
        )
        ctx.update(_manager_orders_context(self.request))
        return ctx


class TeamManagerWarehouseDeadlineApi(RoleRequiredMixin, View):
    allowed_roles = CABINET_ROLES

    def post(self, request, pk: int, *args, **kwargs):
        employee = get_request_employee(request)
        role = _team_request_role(request)
        if employee is None or role not in DEADLINE_MANAGER_ROLES:
            return JsonResponse(
                {"ok": False, "error": "Недостаточно прав для изменения срока"},
                status=403,
            )
        try:
            payload = json.loads(request.body.decode("utf-8") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError):
            return JsonResponse({"ok": False, "error": "Некорректные данные"}, status=400)

        due_date = parse_datetime(str(payload.get("due_date") or "").strip())
        if due_date and timezone.is_naive(due_date):
            due_date = timezone.make_aware(due_date, timezone.get_current_timezone())
        task = (
            Task.objects.select_related("assigned_to")
            .filter(pk=pk)
            .first()
        )
        if task is None:
            return JsonResponse({"ok": False, "error": "Задача склада не найдена"}, status=404)
        try:
            change = reschedule_warehouse_request(
                anchor_task=task,
                due_date=due_date,
                reason=str(payload.get("reason") or ""),
                changed_by=employee,
                user=request.user,
            )
        except ValueError as exc:
            return JsonResponse({"ok": False, "error": str(exc)}, status=400)

        return JsonResponse(
            {
                "ok": True,
                "data": {
                    "task_id": task.id,
                    "due_date": timezone.localtime(change.due_date).isoformat(),
                    "reason": change.reason,
                    "change_id": change.id,
                },
            }
        )


class TeamManagerTaskDeadlineApi(RoleRequiredMixin, View):
    """Manual SLA correction for tasks visible in the unified manager cabinet."""

    allowed_roles = CABINET_ROLES

    def post(self, request, pk: int, *args, **kwargs):
        employee = get_request_employee(request)
        role = _team_request_role(request)
        if employee is None or role not in DEADLINE_MANAGER_ROLES:
            return JsonResponse(
                {"ok": False, "error": "Недостаточно прав для изменения срока"},
                status=403,
            )
        try:
            payload = json.loads(request.body.decode("utf-8") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError):
            return JsonResponse({"ok": False, "error": "Некорректные данные"}, status=400)

        reason = " ".join(str(payload.get("reason") or "").split())
        if len(reason) < 5:
            return JsonResponse(
                {"ok": False, "error": "Укажите причину изменения срока"},
                status=400,
            )
        due_date = parse_datetime(str(payload.get("due_date") or "").strip())
        if due_date and timezone.is_naive(due_date):
            due_date = timezone.make_aware(due_date, timezone.get_current_timezone())
        if not due_date:
            return JsonResponse({"ok": False, "error": "Укажите новый срок"}, status=400)
        due_date = timezone.localtime(due_date).replace(second=0, microsecond=0)
        if due_date <= timezone.localtime().replace(second=0, microsecond=0):
            return JsonResponse(
                {"ok": False, "error": "Новый срок должен быть в будущем"},
                status=400,
            )

        with transaction.atomic():
            task = (
                Task.objects.select_for_update(of=("self",))
                .select_related("assigned_to")
                .filter(pk=pk)
                .first()
            )
            if task is None:
                return JsonResponse({"ok": False, "error": "Задача не найдена"}, status=404)
            task_type = _task_type(task, _task_order_ref(task))
            assignee_role = str(getattr(task.assigned_to, "role", "") or "")
            if (
                task.status == "done"
                or task.kind == Task.KIND_WAREHOUSE_INTERNAL
                or task_type not in {"receiving", "processing", "shipping", "other", "logistics"}
                or (task.assigned_to_id and assignee_role not in CABINET_ROLES)
            ):
                return JsonResponse(
                    {"ok": False, "error": "Эта задача не относится к кабинету менеджера"},
                    status=400,
                )
            previous_due_date = timezone.localtime(task.due_date).replace(
                second=0,
                microsecond=0,
            ) if task.due_date else None
            if previous_due_date == due_date:
                return JsonResponse(
                    {"ok": False, "error": "Новый срок совпадает с текущим"},
                    status=400,
                )
            task.due_date = due_date
            task.save(update_fields=["due_date", "updated_at"])
            if request.user.is_authenticated:
                previous_label = (
                    previous_due_date.strftime("%d.%m.%Y %H:%M")
                    if previous_due_date
                    else "не установлен"
                )
                TaskComment.objects.create(
                    task=task,
                    author=request.user,
                    body=(
                        f"Срок менеджера изменён: {previous_label} → "
                        f"{due_date.strftime('%d.%m.%Y %H:%M')}. Причина: {reason}"
                    ),
                )

        sla = _sla_context(task, timezone.localtime())
        return JsonResponse(
            {
                "ok": True,
                "data": {
                    "task_id": task.id,
                    "due_date": due_date.isoformat(),
                    "sla": sla["label"],
                    "reason": reason,
                },
            }
        )


class TeamManagerReportsView(RoleRequiredMixin, TemplateView):
    """Операционные отчёты менеджера без изменения складских процессов."""

    template_name = "teammanager/reports.html"
    allowed_roles = CABINET_ROLES

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        role = _team_request_role(self.request)
        ctx.update(
            {
                "role": role or "manager",
                "title": "Отчёты",
                "active_nav": "reports",
            }
        )
        ctx.update(_manager_reports_context(self.request))
        return ctx


class TeamManagerInventoryView(RoleRequiredMixin, View):
    allowed_roles = CABINET_ROLES

    def get(self, request):
        page = build_inventory_journal_page(request=request)
        if isinstance(page, HttpResponseForbidden):
            return page
        if str(request.GET.get("export") or "").strip().lower() == "xlsx":
            return _export_inventory_excel(page)
        context = page["context"]
        paginator = Paginator(context.get("rows") or [], 100)
        page_obj = paginator.get_page(request.GET.get("page"))
        query_params = request.GET.copy()
        query_params.pop("page", None)
        context["rows"] = list(page_obj.object_list)
        context["page_obj"] = page_obj
        context["page_query"] = query_params.urlencode()
        context["page_links"] = inventory_pagination_links(page_obj)
        context["active_nav"] = "inventory"
        context["inventory_home_url"] = "/team-manager/inventory/"
        context["inventory_page_title"] = "Складские остатки"
        context["inventory_cabinet_url"] = "/team-manager/"
        context["inventory_hide_internal_stock_columns"] = True
        export_params = request.GET.copy()
        export_params.pop("page", None)
        export_params["export"] = "xlsx"
        context["export_excel_url"] = f"{request.path}?{export_params.urlencode()}"
        return render(request, "sklad/inventory_journal.html", context)


class TeamManagerTaskClaimApi(RoleRequiredMixin, View):
    """Взять свободную задачу в работу (без дубликата Task)."""

    allowed_roles = CABINET_ROLES

    def post(self, request, pk: int, *args, **kwargs):
        import json

        employee = get_request_employee(request)
        if employee is None:
            return JsonResponse({"ok": False, "error": "Нет профиля сотрудника"}, status=403)
        task = Task.objects.filter(pk=pk).first()
        if task is None:
            return JsonResponse({"ok": False, "error": "Задача не найдена"}, status=404)
        try:
            payload = json.loads(request.body.decode("utf-8") or "{}")
        except Exception:
            payload = {}
        try:
            handoff = claim_task(
                task=task,
                employee=employee,
                reason=str(payload.get("reason") or ""),
                comment=str(payload.get("comment") or ""),
            )
        except PermissionError as exc:
            return JsonResponse({"ok": False, "error": str(exc)}, status=403)
        except ValueError as exc:
            return JsonResponse({"ok": False, "error": str(exc)}, status=400)
        return JsonResponse(
            {
                "ok": True,
                "data": {
                    "handoff_id": handoff.id,
                    "task_id": task.id,
                    "assigned_to_id": task.assigned_to_id,
                    "status": task.status,
                },
            }
        )


class TeamManagerTaskTransferApi(RoleRequiredMixin, View):
    """Передать задачу или вернуть в общую очередь."""

    allowed_roles = CABINET_ROLES

    def post(self, request, pk: int, *args, **kwargs):
        import json

        from employees.models import Employee

        author = get_request_employee(request)
        if author is None:
            return JsonResponse({"ok": False, "error": "Нет профиля сотрудника"}, status=403)
        task = Task.objects.filter(pk=pk).first()
        if task is None:
            return JsonResponse({"ok": False, "error": "Задача не найдена"}, status=404)
        try:
            payload = json.loads(request.body.decode("utf-8") or "{}")
        except Exception:
            payload = {}
        return_queue = bool(payload.get("return_to_queue"))
        to_employee = None
        to_id = payload.get("to_employee_id")
        if to_id and not return_queue:
            to_employee = Employee.objects.filter(pk=to_id, is_active=True).first()
            if to_employee is None:
                return JsonResponse({"ok": False, "error": "Сотрудник не найден"}, status=400)
        if not return_queue and to_employee is None:
            return JsonResponse({"ok": False, "error": "Укажите получателя или возврат в очередь"}, status=400)
        try:
            handoff = transfer_task(
                task=task,
                author=author,
                to_employee=to_employee,
                reason=str(payload.get("reason") or ""),
                comment=str(payload.get("comment") or ""),
                return_to_queue=return_queue,
            )
        except PermissionError as exc:
            return JsonResponse({"ok": False, "error": str(exc)}, status=403)
        except ValueError as exc:
            return JsonResponse({"ok": False, "error": str(exc)}, status=400)
        return JsonResponse(
            {
                "ok": True,
                "data": {
                    "handoff_id": handoff.id,
                    "task_id": task.id,
                    "assigned_to_id": task.assigned_to_id,
                    "action": handoff.action,
                },
            }
        )


class TeamManagerLogisticsReferenceView(RoleRequiredMixin, TemplateView):
    template_name = "teammanager/references_logistics.html"
    allowed_roles = CABINET_ROLES

    @staticmethod
    def _vehicle_queryset():
        return CarrierVehicle.objects.select_related("carrier", "created_by").order_by(
            "carrier__name", "-is_default", "-is_active", "vehicle_number", "id"
        )

    @staticmethod
    def _ensure_default_vehicle(carrier_id: int, *, preferred: CarrierVehicle | None = None) -> None:
        active = CarrierVehicle.objects.filter(carrier_id=carrier_id, is_active=True)
        if active.filter(is_default=True).exists():
            return
        candidate = preferred if preferred and preferred.is_active else active.order_by("-updated_at", "-id").first()
        if candidate is not None:
            candidate.is_default = True
            candidate.save(update_fields=["is_default", "updated_at"])

    def get_context_data(self, **kwargs):
        form = kwargs.pop("form", None)
        edit_vehicle = kwargs.pop("edit_vehicle", None)
        ctx = super().get_context_data(**kwargs)
        carrier_filter = str(self.request.GET.get("carrier") or "").strip()
        search = str(self.request.GET.get("q") or "").strip()
        vehicles = self._vehicle_queryset()
        if carrier_filter.isdigit():
            vehicles = vehicles.filter(carrier_id=int(carrier_filter))
        if search:
            vehicles = vehicles.filter(
                Q(carrier__name__icontains=search)
                | Q(carrier__short_name__icontains=search)
                | Q(vehicle_name__icontains=search)
                | Q(vehicle_number__icontains=search)
                | Q(driver_name__icontains=search)
                | Q(driver_phone__icontains=search)
            )
        if edit_vehicle is None:
            edit_id = str(self.request.GET.get("edit") or "").strip()
            if edit_id.isdigit():
                edit_vehicle = self._vehicle_queryset().filter(pk=int(edit_id)).first()
        if form is None:
            form = CarrierVehicleForm(instance=edit_vehicle)
        all_vehicles = CarrierVehicle.objects.all()
        ctx.update(
            {
                "role": _team_request_role(self.request) or "manager",
                "title": "Справочники · Логистика",
                "active_nav": "references",
                "reference_section": "logistics",
                "carrier_options": Carrier.objects.order_by("-is_active", "short_name", "name", "id"),
                "carrier_filter": carrier_filter,
                "search_query": search,
                "vehicles": list(vehicles),
                "vehicle_form": form,
                "edit_vehicle": edit_vehicle,
                "vehicle_total": all_vehicles.count(),
                "vehicle_active": all_vehicles.filter(is_active=True).count(),
            }
        )
        return ctx

    def post(self, request, *args, **kwargs):
        action = str(request.POST.get("action") or "save_vehicle").strip()
        vehicle_id = str(request.POST.get("vehicle_id") or "").strip()
        vehicle = self._vehicle_queryset().filter(pk=int(vehicle_id)).first() if vehicle_id.isdigit() else None

        if action == "save_vehicle":
            form = CarrierVehicleForm(request.POST, instance=vehicle)
            if not form.is_valid():
                return self.render_to_response(self.get_context_data(form=form, edit_vehicle=vehicle))
            with transaction.atomic():
                saved = form.save(commit=False)
                Carrier.objects.select_for_update().get(pk=saved.carrier_id)
                if saved.pk is None:
                    saved.created_by = request.user if request.user.is_authenticated else None
                saved.is_default = bool(saved.is_active and saved.is_default)
                if saved.is_default:
                    CarrierVehicle.objects.filter(carrier_id=saved.carrier_id, is_default=True).exclude(
                        pk=saved.pk
                    ).update(is_default=False)
                saved.save()
                self._ensure_default_vehicle(saved.carrier_id, preferred=saved)
            messages.success(request, "Автомобиль перевозчика сохранён.")
            return redirect(f"/team-manager/references/logistics/?carrier={saved.carrier_id}")

        if vehicle is None:
            messages.error(request, "Автомобиль не найден.")
            return redirect("/team-manager/references/logistics/")

        with transaction.atomic():
            Carrier.objects.select_for_update().get(pk=vehicle.carrier_id)
            vehicle.refresh_from_db()
            if action == "set_default":
                if not vehicle.is_active:
                    messages.error(request, "Сначала включите автомобиль.")
                    return redirect(f"/team-manager/references/logistics/?carrier={vehicle.carrier_id}")
                CarrierVehicle.objects.filter(carrier_id=vehicle.carrier_id, is_default=True).exclude(
                    pk=vehicle.pk
                ).update(is_default=False)
                vehicle.is_default = True
                vehicle.save(update_fields=["is_default", "updated_at"])
                messages.success(request, "Автомобиль назначен основным.")
            elif action == "toggle_active":
                vehicle.is_active = not vehicle.is_active
                if not vehicle.is_active:
                    vehicle.is_default = False
                vehicle.save(update_fields=["is_active", "is_default", "updated_at"])
                self._ensure_default_vehicle(vehicle.carrier_id, preferred=vehicle)
                messages.success(request, "Автомобиль включён." if vehicle.is_active else "Автомобиль отключён.")
            else:
                messages.error(request, "Неизвестное действие.")
        return redirect(f"/team-manager/references/logistics/?carrier={vehicle.carrier_id}")


# NOTE: TeamManagerSettingsView follows below (appended) / keep transfer return_queue explicit


class TeamManagerSettingsView(RoleRequiredMixin, TemplateView):
    """Замещение и массовая передача задач (единый кабинет)."""

    template_name = "teammanager/settings.html"
    allowed_roles = CABINET_ROLES

    def get_context_data(self, **kwargs):
        from datetime import date as date_cls

        from .handoff import REASSIGN_POWER_ROLES
        from .roles import ROLE_LABELS

        ctx = super().get_context_data(**kwargs)
        actor = get_request_employee(self.request)
        role = _team_request_role(self.request)
        colleagues = cabinet_colleagues()
        ctx.update(
            {
                "role": role or "manager",
                "title": "Настройки",
                "active_nav": "settings",
                "actor": actor,
                "colleagues": colleagues,
                "coverages": list_coverages_for_settings(actor=actor) if actor else [],
                "transfer_reasons": [{"key": k, "label": v} for k, v in TRANSFER_REASONS],
                "role_labels": ROLE_LABELS,
                "can_manage_others": bool(
                    actor and any(employee_has_role(actor, item) for item in REASSIGN_POWER_ROLES)
                ),
                "today": date_cls.today().isoformat(),
                "flash_ok": self.request.GET.get("ok") or "",
                "flash_err": self.request.GET.get("err") or "",
            }
        )
        return ctx

    def post(self, request, *args, **kwargs):
        from datetime import datetime
        from urllib.parse import urlencode

        from django.shortcuts import redirect
        from employees.models import Employee

        actor = get_request_employee(request)
        if actor is None:
            return redirect("/team-manager/settings/?" + urlencode({"err": "Нет профиля сотрудника"}))

        action = (request.POST.get("action") or "").strip()
        try:
            if action == "create_coverage":
                principal_id = int(request.POST.get("principal_id") or actor.id)
                substitute_id = int(request.POST.get("substitute_id") or 0)
                principal = Employee.objects.filter(pk=principal_id, is_active=True).first()
                substitute = Employee.objects.filter(pk=substitute_id, is_active=True).first()
                if not principal or not substitute:
                    raise ValueError("Выберите основного и замещающего сотрудника")
                valid_from = datetime.strptime(request.POST.get("valid_from") or "", "%Y-%m-%d").date()
                valid_to_raw = (request.POST.get("valid_to") or "").strip()
                valid_to = datetime.strptime(valid_to_raw, "%Y-%m-%d").date() if valid_to_raw else None
                types = request.POST.getlist("task_types")
                upsert_coverage(
                    actor=actor,
                    principal=principal,
                    substitute=substitute,
                    valid_from=valid_from,
                    valid_to=valid_to,
                    task_types=",".join(types),
                    note=str(request.POST.get("note") or ""),
                    user=request.user,
                )
                return redirect("/team-manager/settings/?" + urlencode({"ok": "Замещение сохранено"}))

            if action == "deactivate_coverage":
                coverage_id = int(request.POST.get("coverage_id") or 0)
                coverage = EmployeeCoverage.objects.filter(pk=coverage_id).select_related("principal").first()
                if coverage is None:
                    raise ValueError("Замещение не найдено")
                deactivate_coverage(actor=actor, coverage=coverage)
                return redirect("/team-manager/settings/?" + urlencode({"ok": "Замещение отключено"}))

            if action == "bulk_transfer":
                from_id = int(request.POST.get("from_employee_id") or actor.id)
                from_employee = Employee.objects.filter(pk=from_id, is_active=True).first()
                if from_employee is None:
                    raise ValueError("Сотрудник-источник не найден")
                return_queue = request.POST.get("return_to_queue") == "1"
                to_employee = None
                if not return_queue:
                    to_id = int(request.POST.get("to_employee_id") or 0)
                    to_employee = Employee.objects.filter(pk=to_id, is_active=True).first()
                    if to_employee is None:
                        raise ValueError("Укажите получателя")
                reason_key = request.POST.get("reason") or "other"
                reason_label = dict(TRANSFER_REASONS).get(reason_key, reason_key)
                result = bulk_transfer_tasks(
                    author=actor,
                    from_employee=from_employee,
                    to_employee=to_employee,
                    reason=reason_label,
                    comment=str(request.POST.get("comment") or ""),
                    return_to_queue=return_queue,
                    task_types=",".join(request.POST.getlist("task_types")),
                )
                msg = f"Передано задач: {result['transferred']}"
                if result["skipped"]:
                    msg += f", пропущено: {result['skipped']}"
                return redirect("/team-manager/settings/?" + urlencode({"ok": msg}))

            raise ValueError("Неизвестное действие")
        except (PermissionError, ValueError) as exc:
            return redirect("/team-manager/settings/?" + urlencode({"err": str(exc)}))
        except Exception as exc:
            return redirect("/team-manager/settings/?" + urlencode({"err": f"Ошибка: {exc}"}))


class TeamManagerKnowledgeView(RoleRequiredMixin, TemplateView):
    """Каталог базы знаний кабинета менеджера."""

    template_name = "teammanager/knowledge.html"
    allowed_roles = CABINET_ROLES

    def get_context_data(self, **kwargs):
        from .knowledge_base import list_knowledge_articles, list_knowledge_sections, list_knowledge_tags

        ctx = super().get_context_data(**kwargs)
        role = _team_request_role(self.request)
        ctx.update(
            {
                "role": role or "manager",
                "title": "База знаний",
                "active_nav": "knowledge",
                "kb_articles": list_knowledge_articles(),
                "kb_sections": list_knowledge_sections(),
                "kb_tags": list_knowledge_tags(),
            }
        )
        return ctx


class TeamManagerKnowledgeDocumentView(RoleRequiredMixin, View):
    """Защищенная выдача внутренних документов базы знаний."""

    allowed_roles = CABINET_ROLES

    def get(self, request, *args, **kwargs):
        from .knowledge_base import get_knowledge_document

        document = get_knowledge_document(kwargs.get("document"))
        if document is None:
            raise Http404("Документ не найден")

        path = Path(__file__).resolve().parent / "knowledge_docs" / document["filename"]
        if not path.is_file():
            raise Http404("Файл документа не найден")

        download = str(request.GET.get("download") or "").strip().lower() in {
            "1",
            "true",
            "yes",
        }
        response = FileResponse(
            path.open("rb"),
            as_attachment=download,
            filename=document["download_name"],
        )
        response["Cache-Control"] = "private, no-store"
        response["X-Robots-Tag"] = "noindex, nofollow, noarchive"
        return response


class TeamManagerKnowledgeArticleView(RoleRequiredMixin, TemplateView):
    """Статья базы знаний."""

    template_name = "teammanager/knowledge_article.html"
    allowed_roles = CABINET_ROLES

    def get_context_data(self, **kwargs):
        from .knowledge_base import (
            get_knowledge_article,
            get_knowledge_section,
            list_knowledge_articles,
            list_knowledge_sections,
        )

        ctx = super().get_context_data(**kwargs)
        slug = str(kwargs.get("slug") or "").strip().lower()
        article = get_knowledge_article(slug)
        if article is None:
            raise Http404("Статья не найдена")
        role = _team_request_role(self.request)
        ctx.update(
            {
                "role": role or "manager",
                "title": article.title,
                "active_nav": "knowledge",
                "kb_article": article,
                "kb_articles": list_knowledge_articles(),
                "kb_sections": list_knowledge_sections(),
                "kb_section": get_knowledge_section(article.category),
            }
        )
        return ctx
