from django import template
import re
from datetime import datetime
from urllib.parse import quote

from django.db.models import Q
from django.urls import reverse
from django.utils import timezone

from employees.models import Employee
from employees.access import get_employee_for_user
from fullbox.order_numbers import format_order_number, replace_order_number_in_title
from logistics.models import LogisticsTrip
from processing_app.stages import processing_is_done, processing_stage_from_payload, processing_stage_label
from reachtruck.models import MoveTask
from shipping.models import ShippingOrder
from shipping.selectors import shipping_ui_status_label

from audit.models import OrderAuditEntry
from sklad.services.warehouse_state import WarehouseGoodsStateResolver
from ..attention import apply_task_attention_state
from ..models import Task, TaskPanelSnapshot, build_receiving_display_context

register = template.Library()

ROLE_LABELS = dict(Employee.ROLE_CHOICES)
ALL_ROLES_KEY = "__all__"
STATUS_ORDER = ["backlog", "in_progress", "blocked", "done"]
STATUS_LABELS = dict(Task.STATUS_CHOICES)
DOCUMENT_STATUS_FILTERS = [
    ("reachtruck", "Передан ричтракеру"),
    ("accepted", "Принят в работу"),
    ("palletizing", "Ожидает паллетизации"),
]
_RECEIVING_ROUTE_RE = re.compile(r"/orders/receiving/([^/]+)/")
_PROCESSING_ROUTE_RE = re.compile(r"/orders/processing/([^/]+)/")
_OTHER_ROUTE_RE = re.compile(r"/orders/other/([^/]+)/")
_SHIPPING_ROUTE_RE = re.compile(r"/shipping/(\d+)/")
_LOGISTICS_TRIP_ROUTE_RE = re.compile(r"/logistics/trips/(\d+)/")
_DAILY_PROBLEM_CHECK_QUERY = "daily_problem_check="
_IP_PREFIX_RE = re.compile(r"\bиндивидуальный предприниматель\b", re.IGNORECASE)
FILTER_DEFS_BY_ROLE = {
    "manager": [
        ("all", "Все"),
        ("receiving", "Приемка"),
        ("processing", "Обработка"),
        ("shipping", "Отгрузки"),
        ("other", "Прочие"),
    ],
    "storekeeper": [
        ("all", "Все"),
        ("receiving", "Приемка"),
        ("shipping", "Отгрузки"),
        ("other", "Прочие"),
        ("logistics", "Рейсы"),
        ("inspection", "Проверки"),
    ],
    "processing_head": [
        ("all", "Все"),
        ("receiving", "Приемка"),
        ("processing", "Обработка"),
    ],
    "processing_worker": [
        ("all", "Все"),
        ("processing", "Обработка"),
    ],
    "head_manager": [
        ("all", "Все"),
        ("processing", "Обработка"),
        ("shipping", "Отгрузки"),
        ("other", "Прочие"),
        ("logistics", "Рейсы"),
    ],
}
DEFAULT_FILTER_DEFS = [
    ("all", "Все"),
    ("receiving", "Приемка"),
    ("processing", "Обработка"),
    ("shipping", "Отгрузки"),
    ("other", "Прочие"),
    ("logistics", "Рейсы"),
]


def _shorten_ip_name(name: str) -> str:
    if not name:
        return "-"
    normalized = _IP_PREFIX_RE.sub("ИП", name)
    return " ".join(normalized.split()) or "-"


def _agency_panel_label(agency) -> str:
    if not agency:
        return "-"
    for attr in ("short_name", "agn_name", "fio_agn"):
        value = str(getattr(agency, attr, "") or "").strip()
        if value:
            return _shorten_ip_name(value)
    return _shorten_ip_name(str(agency))


def _format_panel_datetime(value) -> str:
    if not value:
        return ""
    dt_value = value
    if timezone.is_naive(dt_value):
        dt_value = timezone.make_aware(dt_value, timezone.get_current_timezone())
    return timezone.localtime(dt_value).strftime("%d.%m.%Y %H:%M")


def _repair_panel_text(value) -> str:
    text = str(value or "")
    if not text:
        return ""

    def _score(raw: str) -> int:
        return (
            raw.count("\ufffd") * 10
            + raw.count("\u00d0") * 5
            + raw.count("\u00d1") * 5
            + raw.count("\u0420") * 2
            + raw.count("\u0421") * 2
            + sum(5 for char in raw if 0x80 <= ord(char) <= 0x9F)
        )

    def _cp1251_bytes(raw: str) -> bytes | None:
        chunks: list[bytes] = []
        for char in raw:
            code = ord(char)
            if 0x80 <= code <= 0x9F:
                chunks.append(bytes([code]))
                continue
            try:
                chunks.append(char.encode("cp1251"))
            except UnicodeEncodeError:
                return None
        return b"".join(chunks)

    current = text
    for _ in range(4):
        raw_bytes = _cp1251_bytes(current)
        if raw_bytes is None:
            break
        try:
            repaired = raw_bytes.decode("utf-8")
        except UnicodeDecodeError:
            break
        if _score(repaired) >= _score(current):
            break
        current = repaired
    return current


@register.filter
def short_name(full_name: str) -> str:
    if not full_name:
        return "-"
    parts = [part for part in full_name.split() if part]
    if not parts:
        return "-"
    surname = parts[0]
    initials = "".join(f"{part[0].upper()}." for part in parts[1:3] if part)
    return f"{surname} {initials}".strip()


def _resolve_role(context, role):
    if role in ("all", "*", "any"):
        return ALL_ROLES_KEY
    if role:
        return role
    if context.get("role"):
        return context["role"]
    request = context.get("request")
    request_user = getattr(request, "user", None) if request else None
    if request_user and request_user.is_authenticated:
        return request.user.username
    return None


def _views():
    from todo import views as todo_views

    return todo_views


def _resolve_attention_employee(context):
    request = context.get("request")
    if not request:
        return None
    request_user = getattr(request, "user", None)
    employee = get_employee_for_user(request_user) if request_user else None
    if employee:
        return employee
    name = request.session.get("employee_name") if hasattr(request, "session") else None
    if name:
        return Employee.objects.filter(full_name=name, is_active=True).first()
    return None


def _normalize_limit(limit, default=6):
    try:
        return int(limit)
    except (TypeError, ValueError):
        return default


def _to_int(value) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return 0


def _status_tone_from_label(label: str | None) -> str:
    normalized = str(label or "").strip().lower()
    if not normalized:
        return "neutral"
    if any(token in normalized for token in ("ошиб", "отмен", "разноглас", "спор", "просроч", "невер", "заблок")):
        return "danger"
    if any(
        token in normalized
        for token in (
            "доставка",
            "ричтрак",
            "создано перемещение",
            "отправлено обратно на склад",
            "подготовка к рейсу",
            "загружено",
            "передано ричтраку",
            "в пути",
        )
    ):
        return "moving"
    if any(
        token in normalized
        for token in (
            "в работе",
            "взята в работу",
            "взято в обработку",
            "товар прибыл в obr",
            "в обработке",
            "начать обработку",
            "размещение открыто",
            "раскороб",
            "паллетизац",
        )
    ):
        return "active"
    if any(
        token in normalized
        for token in (
            "ждет",
            "ожидает",
            "в ожидании",
            "на согласовании",
            "черновик",
            "подтверждени",
            "утверждено менеджером",
        )
    ):
        return "pending"
    if any(
        token in normalized
        for token in (
            "выполн",
            "заверш",
            "готов",
            "размещен",
            "размещение завершено",
            "возвращено на склад",
            "заявка завершена",
            "возвращен на склад",
            "отгруж",
            "принято складом",
            "акт отправлен клиенту",
        )
    ):
        return "done"
    return "neutral"


def _extract_receiving_order_id(route: str | None) -> str | None:
    if not route:
        return None
    match = _RECEIVING_ROUTE_RE.search(route)
    if not match:
        return None
    return match.group(1)


def _extract_processing_order_id(route: str | None) -> str | None:
    if not route:
        return None
    match = _PROCESSING_ROUTE_RE.search(route)
    if not match:
        return None
    return match.group(1)


def _extract_other_order_id(route: str | None) -> str | None:
    if not route:
        return None
    match = _OTHER_ROUTE_RE.search(route)
    if not match:
        return None
    return match.group(1)


def _extract_shipping_order_pk(route: str | None) -> int | None:
    if not route:
        return None
    match = _SHIPPING_ROUTE_RE.search(route)
    if not match:
        return None
    try:
        return int(match.group(1))
    except (TypeError, ValueError):
        return None


def _extract_logistics_trip_pk(route: str | None) -> int | None:
    if not route:
        return None
    match = _LOGISTICS_TRIP_ROUTE_RE.search(route)
    if not match:
        return None
    try:
        return int(match.group(1))
    except (TypeError, ValueError):
        return None


def _is_receiving_sign_task(route: str | None) -> bool:
    return bool(route and "/orders/receiving/" in route and "/act/print" in route)


def _manager_receiving_task_requires_action(task: Task) -> bool:
    route = getattr(task, "route", None)
    if _is_receiving_sign_task(route):
        return True
    if not _extract_receiving_order_id(route):
        return True
    status_label = str(getattr(task, "order_status_label", "") or "").strip().lower()
    if not status_label:
        return True
    return status_label in {"ждет подтверждения", "ждёт подтверждения"}


def _apply_manager_receiving_action_state(task: Task, role_key: str | None) -> None:
    if role_key != "manager":
        return
    if not _extract_receiving_order_id(getattr(task, "route", None)):
        return
    if _manager_receiving_task_requires_action(task):
        return
    task.status = "done"


def _hide_task_for_role(task: Task, role_key: str | None) -> bool:
    if role_key == "storekeeper":
        # Кладовщик не работает с заявками на обработку — не показываем на доске.
        if _extract_processing_order_id(getattr(task, "route", None)):
            return True
        if _is_receiving_sign_task(getattr(task, "route", None)):
            assigned_role = getattr(getattr(task, "assigned_to", None), "role", "") or ""
            return assigned_role != "storekeeper"
    return False


def _hide_processing_head_shipping_task(task: Task, role_key: str | None) -> bool:
    if role_key != "processing_head":
        return False
    if _extract_shipping_order_pk(getattr(task, "route", None)) is None:
        return False
    assigned_role = getattr(getattr(task, "assigned_to", None), "role", "") or ""
    return assigned_role in {"logistician", "manager"}


def _is_status_entry(entry) -> bool:
    if entry.action == "status":
        return True
    payload = entry.payload or {}
    return bool(payload.get("status") or payload.get("status_label") or payload.get("submit_action") or payload.get("act"))


def _status_label_from_entry(entry) -> str:
    payload = entry.payload or {}
    act = (payload.get("act") or "").lower()
    act_state = (payload.get("act_state") or "").lower()
    if act == "placement":
        if getattr(entry, "order_type", "") == "receiving":
            return WarehouseGoodsStateResolver.resolve_for_receiving_order(
                order_id=str(getattr(entry, "order_id", "") or ""),
                agency=getattr(entry, "agency", None),
                payload=payload,
            ).label_for("default")
        if act_state == "closed":
            return "Товар принят и размещен на складе"
        return "Размещение на складе"
    client_response = (payload.get("act_client_response") or "").lower()
    if client_response == "confirmed":
        return "Акт приемки подтвержден клиентом"
    if client_response == "dispute":
        return "Клиент заявил разногласия по акту приемки"
    if payload.get("act_sent"):
        return "Акт отправлен клиенту" if not payload.get("act_viewed") else "Выполнена"
    if payload.get("act_storekeeper_signed") and not payload.get("act_manager_signed"):
        return "Принято складом, акт приемки отправлен менеджеру"
    if getattr(entry, "order_type", "") == "receiving":
        return WarehouseGoodsStateResolver.resolve_for_receiving_order(
            order_id=str(getattr(entry, "order_id", "") or ""),
            agency=getattr(entry, "agency", None),
            payload=payload,
        ).label_for("default")
    if getattr(entry, "order_type", "") == "processing":
        return processing_stage_label(payload, fallback=payload.get("status_label") or payload.get("status") or "-")
    status_value = (payload.get("status") or payload.get("submit_action") or "").lower()
    status_label = (payload.get("status_label") or "").lower()
    if "взята в работу" in status_label:
        return "Взята в работу"
    if status_value in {"sent_unconfirmed", "send", "submitted"} or "подтверждени" in status_label:
        return "Ждет подтверждения"
    if status_value in {"warehouse", "on_warehouse"} or "ожидании поставки" in status_label or "на складе" in status_label:
        return "В ожидании поставки товара"
    return payload.get("status_label") or payload.get("status") or "-"


def _processing_status_audience_for_role(role_key: str | None) -> str:
    if role_key in {"processing_head", "processing_worker"}:
        return "processing"
    if role_key == "storekeeper":
        return "storekeeper"
    return "default"


def _receiving_status_audience_for_role(role_key: str | None) -> str:
    if role_key == "storekeeper":
        return "storekeeper"
    return "default"


def _processing_status_label_from_entry(entry, *, audience: str = "default") -> str:
    payload = entry.payload or {}
    stage_label = processing_stage_label(payload)
    if stage_label:
        return stage_label
    warehouse_label = WarehouseGoodsStateResolver.resolve_for_processing_order(
        order_id=str(getattr(entry, "order_id", "") or ""),
        agency=getattr(entry, "agency", None),
        payload=payload,
    ).label_for(audience)
    if warehouse_label and warehouse_label != "-":
        return warehouse_label
    return payload.get("status_label") or payload.get("status") or "-"


def _next_step_tone_from_label(label: str | None) -> str:
    normalized = str(label or "").strip().lower()
    if not normalized:
        return ""
    if normalized in {"выдай задание ричтракеру", "ожидаем ричтракер"}:
        return ""
    return ""


def _is_receiving_reachtruck_notice(label: str | None) -> bool:
    return str(label or "").strip().lower() in {
        "выдай задание ричтракеру",
        "ожидаем ричтракер",
    }


def _clear_receiving_reachtruck_notice(task) -> None:
    if _extract_receiving_order_id(getattr(task, "route", "") or "") and _is_receiving_reachtruck_notice(
        getattr(task, "order_notice_label", "")
    ):
        task.order_notice_label = ""
        task.order_notice_tone = ""


def _existing_receiving_order_ids(order_ids) -> set[str]:
    normalized_ids = [
        str(order_id).strip()
        for order_id in (order_ids or [])
        if str(order_id or "").strip()
    ]
    if not normalized_ids:
        return set()
    return set(
        OrderAuditEntry.objects.filter(
            order_type="receiving",
            order_id__in=normalized_ids,
        ).values_list("order_id", flat=True)
    )


def _is_shipping_act_task(route: str | None) -> bool:
    return bool(route and "/shipping/" in route and "/act/" in route)


def _is_daily_problem_check_task(task: Task) -> bool:
    route = str(getattr(task, "route", "") or "")
    return "/sklad/journal/" in route and _DAILY_PROBLEM_CHECK_QUERY in route


def _is_shipping_supplement_task(task: Task) -> bool:
    if _extract_shipping_order_pk(getattr(task, "route", None)) is None:
        return False
    title = _repair_panel_text(getattr(task, "title", "")).strip().casefold()
    return "подтвердить добор" in title or "упаковать добор" in title


def _shipping_task_group_key(task: Task):
    shipping_pk = _extract_shipping_order_pk(getattr(task, "route", None))
    if shipping_pk is None:
        return None
    subtype = "supplement" if _is_shipping_supplement_task(task) else "base"
    return shipping_pk, subtype


def _shipping_supplement_qty_label(task: Task) -> str:
    description = _repair_panel_text(getattr(task, "description", ""))
    match = re.search(r"Количество\s+к\s+добору:\s*(\d+)\s*шт\.?", description, re.IGNORECASE)
    if not match:
        return ""
    return f"{int(match.group(1))} шт."


def _apply_shipping_supplement_panel_fields(task: Task) -> bool:
    is_supplement = _is_shipping_supplement_task(task)
    normalized_title = _repair_panel_text(getattr(task, "title", "")).strip().casefold()
    task.shipping_task_subtype = "supplement" if is_supplement else ""
    task.shipping_supplement_qty_label = (
        _shipping_supplement_qty_label(task) if is_supplement else ""
    )
    task.panel_description = (
        _repair_panel_text(task.description).strip() if is_supplement else ""
    )
    task.panel_action_label = (
        "Упаковать добор"
        if is_supplement and "упаковать добор" in normalized_title
        else "Подтвердить добор"
        if is_supplement
        else ""
    )
    if is_supplement:
        task.panel_title = _repair_panel_text(task.title).strip()
    return is_supplement


def _task_filter_type(task: Task) -> str:
    route = getattr(task, "route", None)
    if _is_daily_problem_check_task(task):
        return "inspection"
    if _extract_logistics_trip_pk(route) is not None:
        return "logistics"
    if _extract_shipping_order_pk(route) is not None:
        return "shipping"
    if _extract_processing_order_id(route):
        return "processing"
    if _extract_other_order_id(route):
        return "other"
    if _extract_receiving_order_id(route):
        return "receiving"
    return "other"


def _panel_title_for_task(task: Task, order_type: str, order_id: str) -> str:
    default_title = _repair_panel_text(task.display_title())
    if order_type == "receiving" and not _is_receiving_sign_task(getattr(task, "route", None)):
        return default_title
    if order_type == "shipping" and "самовывозом" in str(default_title or "").strip().lower():
        return default_title
    raw_title = _repair_panel_text(getattr(task, "title", "")).strip()
    if not raw_title:
        return default_title
    display_id = format_order_number(order_type, order_id)
    normalized = _repair_panel_text(replace_order_number_in_title(
        raw_title,
        order_type,
        order_id,
        default_title=default_title,
    ))
    if display_id and display_id in normalized:
        return normalized
    return default_title


def _filter_defs_for_role(role_key: str | None) -> list[tuple[str, str]]:
    return list(FILTER_DEFS_BY_ROLE.get(role_key, DEFAULT_FILTER_DEFS))


def _normalize_filter_query(value: str | None) -> str:
    return " ".join(str(value or "").strip().split())


def _task_matches_search(task: Task, query: str) -> bool:
    normalized_query = _normalize_filter_query(query).lower()
    if not normalized_query:
        return True
    haystack_parts = [
        getattr(task, "panel_title", ""),
        getattr(task, "title", ""),
        getattr(task, "description", ""),
        getattr(task, "order_client_label", ""),
        getattr(task, "order_status_label", ""),
        getattr(task, "order_notice_label", ""),
        getattr(task, "executor_label", ""),
        getattr(task, "processing_packers_label", ""),
        getattr(task, "logistics_driver_label", ""),
        getattr(task, "logistics_vehicle_number", ""),
        getattr(task, "logistics_direction_label", ""),
        getattr(task, "logistics_pallet_count", ""),
        getattr(task, "panel_updated_at_label", ""),
        getattr(task, "route", ""),
    ]
    haystack = " ".join(_normalize_filter_query(part).lower() for part in haystack_parts if part)
    return all(token in haystack for token in normalized_query.split())


def _normalized_panel_label(value: object | None) -> str:
    return " ".join(str(value or "").strip().lower().split())


def _task_matches_document_status(task: Task, status_filter: str) -> bool:
    if not status_filter:
        return True
    if _extract_shipping_order_pk(getattr(task, "route", None)) is None:
        return False
    status_label = _normalized_panel_label(getattr(task, "order_status_label", ""))
    notice_label = _normalized_panel_label(getattr(task, "order_notice_label", ""))
    if status_filter == "reachtruck":
        return "ричтрак" in notice_label
    if status_filter == "accepted":
        return (
            "принята в работу" in status_label
            or "принят в работу" in status_label
            or "передана в работу кладовщику" in status_label
        )
    if status_filter == "palletizing":
        return "ожидает паллетизации" in status_label or "ожидает паллетизации" in notice_label
    return True


def _snapshot_role_value(role_key: str | None) -> str:
    return str(role_key or "").strip()


def task_panel_snapshot_role_keys() -> list[str]:
    keys = []
    for role in FILTER_DEFS_BY_ROLE:
        if role not in keys:
            keys.append(role)
    for role, _label in Employee.ROLE_CHOICES:
        if role not in keys:
            keys.append(role)
    if ALL_ROLES_KEY not in keys:
        keys.append(ALL_ROLES_KEY)
    return keys


def _snapshot_defaults_for_task(task, *, hidden=False) -> dict:
    panel_url = getattr(task, "panel_url", "") or getattr(task, "route", "") or reverse("todo:detail", args=[task.id])
    panel_title = _repair_panel_text(getattr(task, "panel_title", "") or task.display_title())
    executor_label = getattr(task, "executor_label", None)
    if executor_label is None and getattr(task, "assigned_to", None):
        executor_label = task.assigned_to.full_name
    route = getattr(task, "route", "") or ""
    if _extract_logistics_trip_pk(route) is not None:
        executor_label = getattr(task, "logistics_driver_label", None) or executor_label
    order_notice_label = getattr(task, "order_notice_label", None) or ""
    if _extract_logistics_trip_pk(route) is not None:
        pallet_count = getattr(task, "logistics_pallet_count", None)
        if pallet_count is not None:
            order_notice_label = str(pallet_count)
    order_notice_tone = getattr(task, "order_notice_tone", None) or ""
    if _extract_receiving_order_id(route) and _is_receiving_reachtruck_notice(
        order_notice_label
    ):
        order_notice_label = ""
        order_notice_tone = ""
    return {
        "task_route": route,
        "task_status": getattr(task, "status", "") or "",
        "filter_type": getattr(task, "filter_type", "") or _task_filter_type(task),
        "panel_title": panel_title,
        "panel_url": panel_url,
        "order_client_id": getattr(task, "order_client_id", None),
        "order_client_label": getattr(task, "order_client_label", None) or "",
        "order_status_label": getattr(task, "order_status_label", None) or "",
        "order_status_tone": getattr(task, "order_status_tone", None) or "",
        "order_notice_label": order_notice_label,
        "order_notice_tone": order_notice_tone,
        "executor_label": executor_label or "",
        "processing_packers_label": (
            getattr(task, "logistics_vehicle_number", None)
            if _extract_logistics_trip_pk(route) is not None
            else getattr(task, "processing_packers_label", None)
        ) or "",
        "worker_title": (
            getattr(task, "logistics_direction_label", None)
            if _extract_logistics_trip_pk(route) is not None
            else getattr(task, "worker_title", None)
        ) or "",
        "panel_updated_at_label": getattr(task, "panel_updated_at_label", None)
        or _format_panel_datetime(getattr(task, "updated_at", None)),
        "is_hidden": bool(hidden),
    }


def _apply_snapshot_to_task(task, snapshot: TaskPanelSnapshot) -> None:
    task.route = snapshot.task_route or task.route
    if task.status != "done":
        task.status = snapshot.task_status or task.status
    task.filter_type = snapshot.filter_type
    task.order_client_id = snapshot.order_client_id
    task.order_client_label = snapshot.order_client_label or None
    task.order_status_label = snapshot.order_status_label or None
    task.order_status_tone = snapshot.order_status_tone or "neutral"
    task.order_notice_label = snapshot.order_notice_label or ""
    task.order_notice_tone = snapshot.order_notice_tone or ""
    task.executor_label = snapshot.executor_label or None
    task.processing_packers_label = snapshot.processing_packers_label or None
    task.worker_title = snapshot.worker_title or None
    task.panel_url = snapshot.panel_url or task.route or reverse("todo:detail", args=[task.id])
    task.panel_title = _repair_panel_text(snapshot.panel_title or task.display_title())
    task.panel_updated_at_label = snapshot.panel_updated_at_label or _format_panel_datetime(getattr(task, "updated_at", None))
    if _extract_logistics_trip_pk(task.route) is not None:
        task.logistics_driver_label = snapshot.executor_label or ""
        task.logistics_vehicle_number = snapshot.processing_packers_label or ""
        task.logistics_direction_label = snapshot.worker_title or ""
        task.logistics_pallet_count = _to_int(snapshot.order_notice_label)
    _clear_receiving_reachtruck_notice(task)


def _store_task_panel_snapshots(all_tasks, visible_tasks, role_key: str | None) -> None:
    snapshot_role = _snapshot_role_value(role_key)
    visible_map = {task.id: task for task in visible_tasks}
    visible_ids = set(visible_map)
    all_task_ids = [task.id for task in all_tasks]
    stale_qs = TaskPanelSnapshot.objects.filter(role_key=snapshot_role)
    if all_task_ids:
        stale_qs.exclude(task_id__in=all_task_ids).delete()
    else:
        stale_qs.delete()
    for task in all_tasks:
        source_task = visible_map.get(task.id, task)
        defaults = _snapshot_defaults_for_task(source_task, hidden=task.id not in visible_ids)
        TaskPanelSnapshot.objects.update_or_create(
            task=task,
            role_key=snapshot_role,
            defaults=defaults,
        )


def _store_task_panel_snapshot_rows(tasks, role_key: str | None, *, hidden_task_ids=None) -> None:
    snapshot_role = _snapshot_role_value(role_key)
    if not snapshot_role:
        return
    hidden_task_ids = set(hidden_task_ids or [])
    snapshot_updated_at = timezone.now()
    snapshots = []
    for task in tasks:
        snapshots.append(
            TaskPanelSnapshot(
                task=task,
                role_key=snapshot_role,
                snapshot_updated_at=snapshot_updated_at,
                **_snapshot_defaults_for_task(task, hidden=task.id in hidden_task_ids),
            )
        )
    if not snapshots:
        return
    TaskPanelSnapshot.objects.bulk_create(
        snapshots,
        update_conflicts=True,
        unique_fields=["task", "role_key"],
        update_fields=[
            "task_route",
            "task_status",
            "filter_type",
            "panel_title",
            "panel_url",
            "order_client_id",
            "order_client_label",
            "order_status_label",
            "order_status_tone",
            "order_notice_label",
            "order_notice_tone",
            "executor_label",
            "processing_packers_label",
            "worker_title",
            "panel_updated_at_label",
            "is_hidden",
            "snapshot_updated_at",
        ],
    )


def sync_task_panel_snapshots_for_role(role_key: str | None, *, include_created_by=True):
    task_panel(
        {
            "role": role_key,
            "_task_panel_force_rebuild": True,
            "_task_panel_store_snapshots": True,
        },
        role=role_key,
        include_created_by=include_created_by,
    )


def sync_task_panel_snapshots_for_roles(role_keys, *, include_created_by=True):
    for role_key in role_keys:
        sync_task_panel_snapshots_for_role(role_key, include_created_by=include_created_by)


def _build_task_panel_payload(
    tasks,
    *,
    context,
    request,
    role_key,
    allowed_filter_values,
    filter_defs,
    selected_type,
    selected_client,
    selected_query,
    selected_document_status,
    limit_value,
    today,
    show_meta,
):
    visible_tasks = [
        task
        for task in tasks
        if task.filter_type == "other" or task.filter_type in allowed_filter_values
    ]
    attention_employee = _resolve_attention_employee(context)
    visible_tasks = apply_task_attention_state(visible_tasks, attention_employee)

    client_options_map: dict[int, str] = {}
    for task in visible_tasks:
        client_id = getattr(task, "order_client_id", None)
        client_label = getattr(task, "order_client_label", None)
        if client_id and client_label:
            client_options_map[int(client_id)] = client_label
    client_options = [
        {
            "id": client_id,
            "label": label,
            "selected": str(client_id) == selected_client,
        }
        for client_id, label in sorted(client_options_map.items(), key=lambda item: item[1].lower())
    ]

    filter_pool = list(visible_tasks)
    if selected_client:
        filter_pool = [
            task
            for task in filter_pool
            if str(getattr(task, "order_client_id", "") or "") == selected_client
        ]
    if selected_query:
        filter_pool = [
            task
            for task in filter_pool
            if _task_matches_search(task, selected_query)
        ]
    if selected_document_status:
        filter_pool = [
            task
            for task in filter_pool
            if _task_matches_document_status(task, selected_document_status)
        ]

    open_filter_pool = [task for task in filter_pool if task.status != "done"]
    filter_counts = {value: 0 for value, _label in filter_defs if value != "all"}
    for task in open_filter_pool:
        if task.filter_type in filter_counts:
            filter_counts[task.filter_type] += 1

    filtered_tasks = list(filter_pool)
    if selected_type == "all" and role_key == "storekeeper":
        filtered_tasks = [
            task
            for task in filtered_tasks
            if task.filter_type != "inspection"
        ]
    elif selected_type != "all":
        filtered_tasks = [
            task
            for task in filtered_tasks
            if task.filter_type == selected_type
        ]

    open_tasks = [task for task in filtered_tasks if task.status != "done"]
    done_tasks = [task for task in filtered_tasks if task.status == "done"]
    open_routes = {task.route for task in open_tasks if task.route}
    done_tasks = [task for task in done_tasks if not task.route or task.route not in open_routes]

    def due_date_only(task):
        return timezone.localtime(task.due_date).date() if task.due_date else None

    status_map = {
        "backlog": [task for task in open_tasks if due_date_only(task) and due_date_only(task) < today],
        "in_progress": [task for task in open_tasks if due_date_only(task) == today],
        "blocked": [task for task in open_tasks if due_date_only(task) and due_date_only(task) > today],
        "done": done_tasks,
    }
    totals = {status: len(status_map[status]) for status in status_map}
    columns = []
    for status in STATUS_ORDER:
        status_tasks = sorted(
            status_map[status],
            key=lambda task: (task.updated_at, task.created_at),
            reverse=True,
        )[:limit_value]
        columns.append(
            {
                "status": status,
                "label": STATUS_LABELS.get(status, status),
                "count": totals.get(status, 0),
                "tasks": status_tasks,
            }
        )
    role_label = None
    if role_key == ALL_ROLES_KEY:
        role_label = "Все роли"
    elif role_key:
        role_label = ROLE_LABELS.get(role_key)
    create_url = reverse("todo:create")
    if request:
        current_path = request.get_full_path()
        if current_path:
            create_url = f"{create_url}?next={quote(current_path, safe='/')}"
    return {
        "task_panel_columns": columns,
        "task_panel_stats": [
            {
                "status": status,
                "label": STATUS_LABELS.get(status, status),
                "count": totals.get(status, 0),
            }
            for status in STATUS_ORDER
        ],
        "task_panel_total": sum(totals.values()),
        "task_panel_role": role_key,
        "task_panel_role_label": role_label,
        "task_panel_show_meta": show_meta,
        "task_panel_attention_employee_id": attention_employee.id if attention_employee else None,
        "task_panel_create_url": create_url,
        "task_panel_list_url": reverse("todo:list"),
        "task_panel_filter_tabs": [
            {
                "value": value,
                "label": label,
                "count": (
                    len([
                        task
                        for task in open_filter_pool
                        if role_key != "storekeeper" or task.filter_type != "inspection"
                    ])
                    if value == "all"
                    else int(filter_counts.get(value, 0))
                ),
                "active": selected_type == value,
            }
            for value, label in filter_defs
        ],
        "task_panel_active_filter_type": selected_type,
        "task_panel_client_options": client_options,
        "task_panel_selected_client": selected_client,
        "task_panel_search_query": selected_query,
        "task_panel_document_status_options": [
            {
                "value": value,
                "label": label,
                "selected": selected_document_status == value,
            }
            for value, label in DOCUMENT_STATUS_FILTERS
        ],
        "task_panel_selected_document_status": selected_document_status,
    }


def _shipping_reachtruck_notice_label(move_tasks) -> str:
    tasks = [
        task
        for task in list(move_tasks or [])
        if task.status != MoveTask.STATUS_CANCELED
    ]
    if not tasks:
        return ""
    in_progress = [task for task in tasks if task.status == MoveTask.STATUS_IN_PROGRESS]
    created = [task for task in tasks if task.status == MoveTask.STATUS_CREATED]
    done = [task for task in tasks if task.status == MoveTask.STATUS_DONE]
    if in_progress:
        driver_names = []
        for task in in_progress:
            name = str(task.assigned_to_name or "").strip()
            if name and name not in driver_names:
                driver_names.append(name)
        if driver_names:
            suffix = ", ".join(driver_names[:2])
            if len(driver_names) > 2:
                suffix += ", ..."
            return f"Ричтрак в работе: {suffix}"
        return "Ричтрак в работе"
    if created:
        return "Передано ричтраку"
    return ""


def _shipping_panel_date_label(order: ShippingOrder | None) -> str:
    if not order:
        return "-"
    date_value = getattr(order, "planned_ship_date", None) or getattr(order, "slot_date", None)
    time_value = getattr(order, "slot_time", None)
    if not date_value and getattr(order, "delivery_type", "") in {
        ShippingOrder.DELIVERY_PICKUP,
        ShippingOrder.DELIVERY_COURIER,
    }:
        created_at = getattr(order, "created_at", None)
        if created_at:
            date_value = timezone.localtime(created_at).date()
    if not date_value:
        return "-"
    label = date_value.strftime("%d.%m.%Y") if hasattr(date_value, "strftime") else str(date_value)
    if time_value:
        label = f"{label} {time_value.strftime('%H:%M') if hasattr(time_value, 'strftime') else time_value}"
    return label


def _shipping_panel_title(order: ShippingOrder | None, fallback_id: object | None) -> str:
    raw_number = str(getattr(order, "number", "") or fallback_id or "").strip()
    delivery_type = str(getattr(order, "delivery_type", "") or "").strip()
    prefix = (
        "Заявка на отгрузку самовывозом"
        if delivery_type == ShippingOrder.DELIVERY_PICKUP
        else "Заявка на отгрузку"
    )
    return f"{prefix} №{format_order_number('shipping', raw_number)}"


def _append_unique_label(
    values: list[str],
    value: object | None,
    *,
    case_sensitive: bool = True,
) -> None:
    label = " ".join(str(value or "").strip().split())
    if not label or label == "-":
        return
    comparison_label = label if case_sensitive else label.casefold()
    existing_labels = values if case_sensitive else [item.casefold() for item in values]
    if comparison_label not in existing_labels:
        values.append(label)


def _preview_label(values: list[str], *, limit: int = 2) -> str:
    cleaned: list[str] = []
    for value in values:
        _append_unique_label(cleaned, value)
    if not cleaned:
        return "-"
    visible = cleaned[:limit]
    suffix = f" +{len(cleaned) - limit}" if len(cleaned) > limit else ""
    return ", ".join(visible) + suffix


def _shipping_packing_payloads_by_number(order_numbers) -> dict[str, dict]:
    normalized_numbers = [
        str(order_number or "").strip()
        for order_number in (order_numbers or [])
        if str(order_number or "").strip()
    ]
    if not normalized_numbers:
        return {}
    entries = (
        OrderAuditEntry.objects.filter(
            order_type="shipping",
            order_id__in=normalized_numbers,
            payload__act="shipping_packing",
        )
        .order_by("order_id", "-created_at")
    )
    payloads: dict[str, dict] = {}
    for entry in entries:
        if entry.order_id in payloads:
            continue
        payloads[entry.order_id] = dict(entry.payload or {})
    return payloads


def _pallet_count_from_shipping_packing_payload(payload: dict | None) -> int:
    if not isinstance(payload, dict):
        return 0
    count = _to_int(payload.get("pallet_count"))
    if count > 0:
        return count
    pallets = payload.get("act_pallets")
    return len(pallets) if isinstance(pallets, list) else 0


def _panel_filter_state(request, role_key: str | None) -> tuple[str, str, str, str]:
    selected_type = "all"
    selected_client = ""
    selected_query = ""
    selected_document_status = ""
    if not request:
        return selected_type, selected_client, selected_query, selected_document_status
    session = getattr(request, "session", None)
    session_key = f"todo_panel_filters:{request.path}"
    if request.GET.get("todo_filters_reset"):
        if session is not None:
            session.pop(session_key, None)
        return selected_type, selected_client, selected_query, selected_document_status
    if request.GET.get("todo_filters_applied"):
        selected_type = str(request.GET.get("todo_filter_type") or "all").strip() or "all"
        selected_client = str(request.GET.get("todo_filter_client") or "").strip()
        selected_query = _normalize_filter_query(request.GET.get("todo_filter_query") or "")
        selected_document_status = str(request.GET.get("todo_filter_document_status") or "").strip()
        if session is not None:
            session[session_key] = {
                "selected_type": selected_type,
                "selected_client": selected_client,
                "selected_query": selected_query,
                "selected_document_status": selected_document_status,
            }
        return selected_type, selected_client, selected_query, selected_document_status
    if session is not None:
        saved = session.get(session_key) or {}
        selected_type = str(saved.get("selected_type") or "all").strip() or "all"
        selected_client = str(saved.get("selected_client") or "").strip()
        selected_query = _normalize_filter_query(saved.get("selected_query") or "")
        selected_document_status = str(saved.get("selected_document_status") or "").strip()
    return selected_type, selected_client, selected_query, selected_document_status


@register.inclusion_tag("todo/_task_panel.html", takes_context=True)
def task_panel(context, role=None, limit=6, show_meta=True, include_created_by=True):
    role_key = _resolve_role(context, role)
    request = context.get("request")
    force_rebuild = bool(context.get("_task_panel_force_rebuild"))
    store_snapshots = bool(context.get("_task_panel_store_snapshots", False))
    filter_defs = _filter_defs_for_role(role_key)
    allowed_filter_values = {value for value, _label in filter_defs if value != "all"}
    selected_type, selected_client, selected_query, selected_document_status = _panel_filter_state(request, role_key)
    if selected_type != "all" and selected_type not in allowed_filter_values:
        selected_type = "all"
    document_status_values = {value for value, _label in DOCUMENT_STATUS_FILTERS}
    if selected_document_status not in document_status_values:
        selected_document_status = ""
    selected_schedule_status = str(
        request.GET.get("todo_filter_schedule_status") if request else ""
    ).strip()
    open_only = bool(context.get("_task_panel_open_only")) and (
        selected_type == "all"
        and not selected_client
        and not selected_query
        and not selected_document_status
        and selected_schedule_status != "done"
    )
    snapshot_role = _snapshot_role_value(role_key)
    snapshot_tasks = []
    snapshot_task_ids = set()
    snapshot_done_rows = []
    if not force_rebuild:
        snapshot_qs = TaskPanelSnapshot.objects.filter(role_key=snapshot_role)
        snapshot_done_filter = Q(task__status="done", task_status="done")
        if open_only:
            snapshot_done_rows = list(
                snapshot_qs.filter(snapshot_done_filter).values_list(
                    "task_id",
                    "task_route",
                    "filter_type",
                    "is_hidden",
                    "task__title",
                )
            )
            snapshot_task_ids.update(row[0] for row in snapshot_done_rows)
            snapshot_qs = snapshot_qs.exclude(snapshot_done_filter)
        snapshot_rows = list(
            snapshot_qs.select_related(
                "task",
                "task__assigned_to",
                "task__created_by",
                "task__observer",
            )
        )
        if snapshot_rows:
            active_supplement_shipping_pks = {
                shipping_pk
                for snapshot in snapshot_rows
                for shipping_pk in [_extract_shipping_order_pk(snapshot.task_route or snapshot.task.route)]
                if (
                    shipping_pk is not None
                    and snapshot.task.status != "done"
                    and _is_shipping_supplement_task(snapshot.task)
                )
            }
            for snapshot in snapshot_rows:
                snapshot_route = snapshot.task_route or getattr(snapshot.task, "route", "") or ""
                if snapshot.is_hidden:
                    hidden_shipping_pk = _extract_shipping_order_pk(snapshot_route)
                    if (
                        hidden_shipping_pk in active_supplement_shipping_pks
                        and snapshot.task.status != "done"
                    ):
                        task = snapshot.task
                        _apply_snapshot_to_task(task, snapshot)
                        _apply_manager_receiving_action_state(task, role_key)
                        if _hide_task_for_role(task, role_key):
                            snapshot_task_ids.add(snapshot.task_id)
                            continue
                        snapshot_task_ids.add(snapshot.task_id)
                        snapshot_tasks.append(task)
                        continue
                    if role_key == "processing_head" and (
                        _extract_receiving_order_id(snapshot_route)
                        or _extract_shipping_order_pk(snapshot_route) is not None
                    ):
                        snapshot.delete()
                        continue
                    snapshot_task_ids.add(snapshot.task_id)
                    continue
                task = snapshot.task
                _apply_snapshot_to_task(task, snapshot)
                _apply_manager_receiving_action_state(task, role_key)
                if _hide_task_for_role(task, role_key):
                    snapshot.delete()
                    continue
                is_flow_snapshot = (
                    _extract_receiving_order_id(task.route)
                    or _extract_shipping_order_pk(task.route) is not None
                    or _extract_logistics_trip_pk(task.route) is not None
                )
                if is_flow_snapshot:
                    snapshot_task_ids.add(snapshot.task_id)
                    snapshot_tasks.append(task)
                    continue
                snapshot_task_ids.add(snapshot.task_id)
                snapshot_tasks.append(task)
    current_employee = None
    request_user = getattr(request, "user", None) if request else None
    if request_user and request_user.is_authenticated:
        current_employee = get_employee_for_user(request_user)
    tasks_qs = Task.objects.select_related(
        "assigned_to",
        "created_by",
        "observer",
    ).exclude(kind=Task.KIND_WAREHOUSE_INTERNAL)
    if role_key == "processing_worker":
        if current_employee:
            tasks_qs = tasks_qs.filter(assigned_to=current_employee)
        else:
            tasks_qs = tasks_qs.none()
    role_filter = None
    if role_key and role_key != ALL_ROLES_KEY:
        role_filter = Q(assigned_to__role=role_key) | Q(observer__role=role_key)
        if include_created_by:
            role_filter |= Q(created_by__username=role_key)
        if role_key == "storekeeper":
            role_filter |= Q(route__contains="/orders/processing/")
        if role_key == "processing_head":
            role_filter |= Q(route__contains="/orders/receiving/")
    if role_filter is not None:
        tasks_qs = tasks_qs.filter(role_filter)
    if open_only:
        tasks_qs = tasks_qs.exclude(status="done")
    if snapshot_task_ids:
        tasks_qs = tasks_qs.exclude(pk__in=snapshot_task_ids)
    limit_value = _normalize_limit(limit)
    today = timezone.localdate()
    processing_status_audience = _processing_status_audience_for_role(role_key)

    task_scan_limit = max(limit_value * 8, 300)
    tasks = list(tasks_qs.order_by("-updated_at", "-id")[:task_scan_limit])
    unsnapshotted_tasks = list(tasks)
    receiving_by_order = {}
    processing_by_order = {}
    shipping_by_order = {}
    logistics_by_trip = {}
    other_tasks = []

    def _is_processing_flow_task(task):
        return bool(task.route and "/orders/processing/" in task.route and "/flow/" in task.route)

    def _prefer_open(existing_task, candidate_task):
        if not existing_task:
            return candidate_task
        if existing_task.status == "done" and candidate_task.status != "done":
            return candidate_task
        if existing_task.status != "done" and candidate_task.status == "done":
            return existing_task
        if candidate_task.updated_at > existing_task.updated_at:
            return candidate_task
        return existing_task

    def _prefer_processing_task(existing_task, candidate_task):
        if not existing_task:
            return candidate_task
        existing_flow = _is_processing_flow_task(existing_task)
        candidate_flow = _is_processing_flow_task(candidate_task)
        if existing_flow != candidate_flow:
            return existing_task if not existing_flow else candidate_task
        return _prefer_open(existing_task, candidate_task)

    def _dedupe_processing_tasks(task_list):
        if role_key == "processing_worker":
            return list(task_list)
        preferred_by_order = {}
        for task in task_list:
            processing_id = _extract_processing_order_id(task.route)
            if not processing_id:
                continue
            preferred_by_order[processing_id] = _prefer_processing_task(
                preferred_by_order.get(processing_id),
                task,
            )
        if not preferred_by_order:
            return list(task_list)
        deduped_tasks = []
        added_processing_ids = set()
        for task in task_list:
            processing_id = _extract_processing_order_id(task.route)
            if not processing_id:
                deduped_tasks.append(task)
                continue
            if processing_id in added_processing_ids:
                continue
            deduped_tasks.append(preferred_by_order[processing_id])
            added_processing_ids.add(processing_id)
        return deduped_tasks

    def _prefer_receiving_task(existing_task, candidate_task):
        if not existing_task:
            return candidate_task
        existing_sign = _is_receiving_sign_task(existing_task.route)
        candidate_sign = _is_receiving_sign_task(candidate_task.route)
        if existing_sign != candidate_sign:
            if role_key == "manager":
                return candidate_task if candidate_sign else existing_task
            return existing_task if not existing_sign else candidate_task
        return _prefer_open(existing_task, candidate_task)

    def _dedupe_receiving_tasks(task_list):
        preferred_by_order = {}
        for task in task_list:
            receiving_id = _extract_receiving_order_id(task.route)
            if not receiving_id:
                continue
            preferred_by_order[receiving_id] = _prefer_receiving_task(
                preferred_by_order.get(receiving_id),
                task,
            )
        if not preferred_by_order:
            return list(task_list)
        deduped_tasks = []
        added_receiving_ids = set()
        for task in task_list:
            receiving_id = _extract_receiving_order_id(task.route)
            if not receiving_id:
                deduped_tasks.append(task)
                continue
            if receiving_id in added_receiving_ids:
                continue
            deduped_tasks.append(preferred_by_order[receiving_id])
            added_receiving_ids.add(receiving_id)
        return deduped_tasks

    def _prefer_shipping_task(existing_task, candidate_task):
        if not existing_task:
            return candidate_task
        existing_act = _is_shipping_act_task(existing_task.route)
        candidate_act = _is_shipping_act_task(candidate_task.route)
        if existing_act != candidate_act and role_key in {"manager", "head_manager"}:
            if candidate_act and candidate_task.status != "done":
                return candidate_task
            if existing_act and existing_task.status != "done":
                return existing_task
        return _prefer_open(existing_task, candidate_task)

    def _dedupe_shipping_tasks(task_list):
        preferred_by_order = {}
        for task in task_list:
            shipping_key = _shipping_task_group_key(task)
            if shipping_key is None:
                continue
            preferred_by_order[shipping_key] = _prefer_shipping_task(
                preferred_by_order.get(shipping_key),
                task,
            )
        if not preferred_by_order:
            return list(task_list)
        deduped_tasks = []
        added_shipping_keys = set()
        for task in task_list:
            shipping_key = _shipping_task_group_key(task)
            if shipping_key is None:
                deduped_tasks.append(task)
                continue
            if shipping_key in added_shipping_keys:
                continue
            deduped_tasks.append(preferred_by_order[shipping_key])
            added_shipping_keys.add(shipping_key)
        return deduped_tasks

    def _prefer_storekeeper_shipping_supplement(task_list):
        """Show the concrete supplement action instead of the base shipping card."""
        if role_key != "storekeeper":
            return list(task_list)
        active_supplement_shipping_pks = {
            shipping_pk
            for task in task_list
            for shipping_pk in [_extract_shipping_order_pk(getattr(task, "route", None))]
            if (
                shipping_pk is not None
                and task.status != "done"
                and _is_shipping_supplement_task(task)
            )
        }
        if not active_supplement_shipping_pks:
            return list(task_list)
        return [
            task
            for task in task_list
            if not (
                _extract_shipping_order_pk(getattr(task, "route", None))
                in active_supplement_shipping_pks
                and not _is_shipping_supplement_task(task)
            )
        ]

    if role_key == "processing_worker":
        combined_tasks = list(tasks)
    else:
        for task in tasks:
            if _hide_task_for_role(task, role_key):
                continue
            order_id = _extract_receiving_order_id(task.route)
            if order_id:
                receiving_by_order[order_id] = _prefer_receiving_task(
                    receiving_by_order.get(order_id),
                    task,
                )
                continue
            processing_id = _extract_processing_order_id(task.route)
            if processing_id:
                processing_by_order[processing_id] = _prefer_processing_task(
                    processing_by_order.get(processing_id),
                    task,
                )
                continue
            shipping_key = _shipping_task_group_key(task)
            if shipping_key is not None:
                shipping_by_order[shipping_key] = _prefer_shipping_task(
                    shipping_by_order.get(shipping_key),
                    task,
                )
                continue
            trip_pk = _extract_logistics_trip_pk(task.route)
            if trip_pk is not None:
                logistics_by_trip[trip_pk] = _prefer_open(
                    logistics_by_trip.get(trip_pk),
                    task,
                )
                continue
            other_tasks.append(task)
        combined_tasks = (
            other_tasks
            + list(receiving_by_order.values())
            + list(processing_by_order.values())
            + list(shipping_by_order.values())
            + list(logistics_by_trip.values())
        )

    receiving_order_ids = {
        order_id
        for order_id in (
            _extract_receiving_order_id(task.route)
            for task in combined_tasks
        )
        if order_id
    }
    if receiving_order_ids:
        existing_receiving_ids = _existing_receiving_order_ids(receiving_order_ids)
        combined_tasks = [
            task
            for task in combined_tasks
            if (
                not _extract_receiving_order_id(task.route)
                or _extract_receiving_order_id(task.route) in existing_receiving_ids
            )
        ]

    processing_order_ids = {}
    for task in combined_tasks:
        order_id = _extract_processing_order_id(task.route)
        if order_id:
            processing_order_ids[order_id] = True
    if processing_order_ids:
        entries = (
            OrderAuditEntry.objects.filter(
                order_type="processing",
                order_id__in=list(processing_order_ids),
            )
            .order_by("order_id", "-created_at")
        )
        status_by_order = {}
        stage_by_order = {}
        for entry in entries:
            if not _is_status_entry(entry):
                continue
            if entry.order_id not in status_by_order:
                status_by_order[entry.order_id] = _processing_status_label_from_entry(
                    entry,
                    audience=processing_status_audience,
                )
            if entry.order_id not in stage_by_order:
                stage_value = processing_stage_from_payload(entry.payload or {})
                if stage_value:
                    stage_by_order[entry.order_id] = stage_value
        for task in combined_tasks:
            order_id = _extract_processing_order_id(task.route)
            if not order_id:
                continue
            status_label = (status_by_order.get(order_id) or "").lower()
            stage_value = str(stage_by_order.get(order_id) or "").strip().lower()
            is_processing_done = processing_is_done(
                {
                    "status_label": status_label,
                    "processing_stage": stage_value,
                }
            )
            if is_processing_done:
                task.status = "done"
            if (
                status_label in {"взято в обработку", "открыта раскоробовка", "раскоробовка завершена"}
                or stage_value in {"taken_into_processing", "unboxing_opened", "unboxing_completed"}
            ) and task.status == "done" and not is_processing_done:
                task.status = "in_progress"
            if "раскороб" in status_label or stage_value in {"unboxing_opened", "unboxing_completed"}:
                if role_key == "processing_worker":
                    if "/cz-flow/" not in (task.route or ""):
                        task.route = f"/orders/processing/{order_id}/flow/"
                else:
                    task.route = f"/orders/processing/{order_id}/work/"
            elif stage_value == "done":
                task.route = f"/orders/processing/{order_id}/"
            elif (
                status_label in {"взято в обработку", "отправлено обратно на склад", "возвращено на склад"}
                or stage_value in {"taken_into_processing", "return_to_stock_sent", "returned_to_stock"}
            ):
                task.route = f"/orders/processing/{order_id}/work/"

    done_tasks = [task for task in combined_tasks if task.status == "done"]
    open_tasks = [task for task in combined_tasks if task.status != "done"]
    open_routes = {task.route for task in open_tasks if task.route}
    done_tasks = [task for task in done_tasks if not task.route or task.route not in open_routes]
    combined_tasks = open_tasks + done_tasks

    tasks = _prefer_storekeeper_shipping_supplement(combined_tasks)
    snapshot_source_tasks = list(tasks)
    receiving_order_ids = {}
    processing_order_ids = {}
    other_order_ids = {}
    refresh_snapshot_metadata = role_key != "storekeeper"
    live_snapshot_tasks = [
        task
        for task in snapshot_tasks
        if (
            refresh_snapshot_metadata
            or (
                role_key == "storekeeper"
                and _extract_receiving_order_id(getattr(task, "route", None))
            )
        )
        and (
            _extract_receiving_order_id(getattr(task, "route", None))
            or _extract_other_order_id(getattr(task, "route", None))
            or _extract_logistics_trip_pk(getattr(task, "route", None)) is not None
            or _extract_shipping_order_pk(getattr(task, "route", None)) is not None
        )
    ]
    metadata_source_tasks = list(tasks) + live_snapshot_tasks
    for task in metadata_source_tasks:
        order_id = _extract_receiving_order_id(task.route)
        if order_id:
            receiving_order_ids[order_id] = True
        order_id = _extract_processing_order_id(task.route)
        if order_id:
            processing_order_ids[order_id] = True
        order_id = _extract_other_order_id(task.route)
        if order_id:
            other_order_ids[order_id] = True
    receiving_status_by_order = {}
    receiving_next_step_by_order = {}
    receiving_client_by_order = {}
    receiving_client_id_by_order = {}
    receiving_entries_by_order = {}
    receiving_latest_entry_by_order = {}
    receiving_status_entry_by_order = {}
    receiving_updated_at_by_order = {}
    receiving_display_context_by_order = {}
    if receiving_order_ids:
        receiving_status_audience = _receiving_status_audience_for_role(role_key)
        entries = (
            OrderAuditEntry.objects.filter(
                order_type="receiving",
                order_id__in=list(receiving_order_ids),
            )
            .select_related("agency")
            .order_by("order_id", "-created_at")
        )
        for entry in entries:
            receiving_entries_by_order.setdefault(entry.order_id, []).append(entry)
            if entry.order_id not in receiving_latest_entry_by_order:
                receiving_latest_entry_by_order[entry.order_id] = entry
            if entry.order_id not in receiving_status_entry_by_order and _is_status_entry(entry):
                receiving_status_entry_by_order[entry.order_id] = entry
            if entry.order_id not in receiving_client_by_order:
                if entry.agency:
                    receiving_client_by_order[entry.order_id] = _agency_panel_label(entry.agency)
                else:
                    receiving_client_by_order[entry.order_id] = "-"
            if entry.order_id not in receiving_client_id_by_order:
                receiving_client_id_by_order[entry.order_id] = int(entry.agency_id or 0) if entry.agency_id else None
            if entry.order_id not in receiving_updated_at_by_order:
                receiving_updated_at_by_order[entry.order_id] = entry.created_at
        resolver_payload = {}
        for order_id in receiving_order_ids:
            latest_entry = receiving_status_entry_by_order.get(order_id) or receiving_latest_entry_by_order.get(order_id)
            order_entries = receiving_entries_by_order.get(order_id) or []
            receiving_display_context_by_order[order_id] = build_receiving_display_context(
                order_id,
                entries=order_entries,
            )
            resolver_payload[order_id] = {
                "agency": getattr(latest_entry, "agency", None),
                "payload": (latest_entry.payload or {}) if latest_entry else {},
                "entries": order_entries,
            }
        receiving_results = WarehouseGoodsStateResolver.resolve_many_for_receiving_orders(
            resolver_payload
        )
        for order_id, receiving_result in receiving_results.items():
            receiving_status_by_order[order_id] = receiving_result.label_for(receiving_status_audience)
            receiving_next_step_by_order[order_id] = receiving_result.next_step_for(receiving_status_audience)
    processing_status_by_order = {}
    processing_client_by_order = {}
    processing_client_id_by_order = {}
    processing_packers_by_order = {}
    processing_updated_at_by_order = {}
    processing_head_employee = None
    if role_key == "processing_head":
        processing_head_employee = (
            Employee.objects.filter(role="processing_head", is_active=True)
            .order_by("full_name")
            .first()
        )
    if processing_order_ids:
        entries = (
            OrderAuditEntry.objects.filter(
                order_type="processing",
                order_id__in=list(processing_order_ids),
            )
            .select_related("agency")
            .order_by("order_id", "-created_at")
        )
        for entry in entries:
            if entry.order_id not in processing_client_by_order:
                if entry.agency:
                    processing_client_by_order[entry.order_id] = _agency_panel_label(entry.agency)
                else:
                    processing_client_by_order[entry.order_id] = "-"
            if entry.order_id not in processing_client_id_by_order:
                processing_client_id_by_order[entry.order_id] = int(entry.agency_id or 0) if entry.agency_id else None
            if entry.order_id not in processing_updated_at_by_order:
                processing_updated_at_by_order[entry.order_id] = entry.created_at
            if entry.order_id in processing_status_by_order:
                continue
            if not _is_status_entry(entry):
                continue
            processing_status_by_order[entry.order_id] = _processing_status_label_from_entry(
                entry,
                audience=processing_status_audience,
            )
        processing_routes = [
            route
            for order_id in processing_order_ids
            for route in (
                f"/orders/processing/{order_id}/flow/",
                f"/orders/processing/{order_id}/cz-flow/",
            )
        ]
        packer_tasks = (
            Task.objects.filter(
                route__in=processing_routes,
                assigned_to__role="processing_worker",
            )
            .exclude(status="done")
            .select_related("assigned_to")
        )
        for pack_task in packer_tasks:
            pack_order_id = _extract_processing_order_id(pack_task.route)
            if not pack_order_id or not pack_task.assigned_to:
                continue
            label = pack_task.assigned_to.full_name or str(pack_task.assigned_to)
            labels = processing_packers_by_order.setdefault(pack_order_id, [])
            if label not in labels:
                labels.append(label)
    shipping_live_tasks = list(tasks)
    shipping_source_tasks = shipping_live_tasks + (
        list(snapshot_tasks) if refresh_snapshot_metadata else []
    )
    shipping_live_order_pks = {
        shipping_pk
        for shipping_pk in (
            _extract_shipping_order_pk(task.route)
            for task in shipping_live_tasks
        )
        if shipping_pk is not None
    }
    shipping_order_pks = {
        shipping_pk
        for shipping_pk in (
            _extract_shipping_order_pk(task.route)
            for task in shipping_source_tasks
        )
        if shipping_pk is not None
    }
    shipping_orders_by_pk = {
        order.pk: order
        for order in ShippingOrder.objects.select_related("agency", "marketplace").filter(pk__in=shipping_order_pks)
    } if shipping_order_pks else {}
    hidden_shipping_order_pks = {
        order_pk
        for order_pk, order in shipping_orders_by_pk.items()
        if order.status == ShippingOrder.STATUS_DRAFT
    }
    if hidden_shipping_order_pks:
        tasks = [
            task
            for task in tasks
            if _extract_shipping_order_pk(task.route) not in hidden_shipping_order_pks
        ]
        snapshot_tasks = [
            task
            for task in snapshot_tasks
            if _extract_shipping_order_pk(task.route) not in hidden_shipping_order_pks
        ]
        shipping_orders_by_pk = {
            order_pk: order
            for order_pk, order in shipping_orders_by_pk.items()
            if order_pk not in hidden_shipping_order_pks
        }
        shipping_live_order_pks.difference_update(hidden_shipping_order_pks)
    shipping_status_by_pk = {}
    shipping_reachtruck_notice_by_pk = {}
    for task in snapshot_tasks:
        shipping_pk = _extract_shipping_order_pk(task.route)
        if shipping_pk is None:
            continue
        status_label = str(getattr(task, "order_status_label", "") or "").strip()
        notice_label = str(getattr(task, "order_notice_label", "") or "").strip()
        if status_label:
            shipping_status_by_pk.setdefault(shipping_pk, status_label)
        if notice_label:
            shipping_reachtruck_notice_by_pk.setdefault(shipping_pk, notice_label)
    terminal_shipping_labels = {
        ShippingOrder.STATUS_CANCELED: "ОТМЕНЕНА",
        ShippingOrder.STATUS_SHIPPED: "Отгружена",
        ShippingOrder.STATUS_PARTIAL: "Отгружена частично",
    }
    for order_pk, order in shipping_orders_by_pk.items():
        terminal_label = terminal_shipping_labels.get(order.status)
        if terminal_label:
            shipping_status_by_pk[order_pk] = terminal_label
        else:
            shipping_status_by_pk[order_pk] = shipping_ui_status_label(order)
    shipping_updated_at_by_pk = {
        order_pk: _shipping_panel_date_label(order)
        for order_pk, order in shipping_orders_by_pk.items()
    }
    shipping_destination_by_pk = {
        order_pk: str(getattr(order, "destination_warehouse", "") or "").strip() or "-"
        for order_pk, order in shipping_orders_by_pk.items()
    }
    shipping_marketplace_by_pk = {
        order_pk: str(getattr(getattr(order, "marketplace", None), "name", "") or "").strip() or "-"
        for order_pk, order in shipping_orders_by_pk.items()
    }
    shipping_reachtruck_order_pks = {
        order_pk
        for order_pk in shipping_live_order_pks
        if order_pk in shipping_orders_by_pk
        and shipping_orders_by_pk[order_pk].status not in terminal_shipping_labels
    }
    shipping_reachtruck_tasks_by_pk = {order_pk: [] for order_pk in shipping_reachtruck_order_pks}
    if shipping_reachtruck_order_pks:
        shipping_pk_by_number = {
            str(order.number or "").strip(): order_pk
            for order_pk, order in shipping_orders_by_pk.items()
            if order_pk in shipping_reachtruck_order_pks and str(order.number or "").strip()
        }
        agency_ids = {
            int(order.agency_id)
            for order_pk, order in shipping_orders_by_pk.items()
            if order_pk in shipping_reachtruck_order_pks
            if order.agency_id
        }
        move_tasks = (
            MoveTask.objects.filter(
                request__agency_id__in=agency_ids,
                to_zone="OTG",
            )
            .select_related("request")
            .order_by("id")
            if agency_ids
            else MoveTask.objects.none()
        )
        for move_task in move_tasks:
            payload = move_task.payload if isinstance(move_task.payload, dict) else {}
            shipping_pk = _to_int(payload.get("shipping_order_pk") or payload.get("order_pk"))
            if shipping_pk not in shipping_reachtruck_order_pks:
                shipping_number = str(payload.get("shipping_order_id") or payload.get("order_id") or "").strip()
                shipping_pk = shipping_pk_by_number.get(shipping_number, 0)
            if shipping_pk in shipping_reachtruck_tasks_by_pk:
                shipping_reachtruck_tasks_by_pk[shipping_pk].append(move_task)
    shipping_reachtruck_notice_by_pk.update({
        order_pk: _shipping_reachtruck_notice_label(move_tasks)
        for order_pk, move_tasks in shipping_reachtruck_tasks_by_pk.items()
    })
    shipping_client_by_pk = {}
    for order_pk, order in shipping_orders_by_pk.items():
        if order.agency:
            shipping_client_by_pk[order_pk] = _agency_panel_label(order.agency)
        else:
            shipping_client_by_pk[order_pk] = "-"
    logistics_source_tasks = list(tasks) + live_snapshot_tasks
    logistics_trip_pks = {
        trip_pk
        for trip_pk in (
            _extract_logistics_trip_pk(task.route)
            for task in logistics_source_tasks
        )
        if trip_pk is not None
    }
    logistics_status_by_pk = {}
    logistics_updated_at_by_pk = {}
    logistics_driver_by_pk = {}
    logistics_vehicle_by_pk = {}
    logistics_direction_by_pk = {}
    logistics_client_by_pk = {}
    logistics_pallet_count_by_pk = {}
    if logistics_trip_pks:
        trips = (
            LogisticsTrip.objects.filter(pk__in=logistics_trip_pks)
            .prefetch_related("orders__shipping_order__agency")
        )
        shipping_numbers_by_trip: dict[int, list[str]] = {}
        trip_order_details: dict[int, dict[str, list[str]]] = {}
        for trip in trips:
            logistics_status_by_pk[trip.pk] = _views()._trip_status_label(trip)
            logistics_updated_at_by_pk[trip.pk] = getattr(trip, "updated_at", None)
            logistics_driver_by_pk[trip.pk] = str(getattr(trip, "driver_name", "") or "").strip() or "-"
            logistics_vehicle_by_pk[trip.pk] = str(getattr(trip, "vehicle_number", "") or "").strip() or "-"
            details = {"clients": [], "directions": [], "numbers": []}
            for link in trip.orders.all():
                order = getattr(link, "shipping_order", None)
                if not order:
                    continue
                order_number = str(getattr(order, "number", "") or "").strip()
                if order_number:
                    details["numbers"].append(order_number)
                _append_unique_label(details["clients"], _agency_panel_label(getattr(order, "agency", None)))
                _append_unique_label(
                    details["directions"],
                    getattr(order, "destination_warehouse", None)
                    or getattr(order, "destination_address", None),
                    case_sensitive=False,
                )
            shipping_numbers_by_trip[trip.pk] = details["numbers"]
            trip_order_details[trip.pk] = details
        all_trip_order_numbers = [
            order_number
            for order_numbers in shipping_numbers_by_trip.values()
            for order_number in order_numbers
        ]
        packing_payloads = _shipping_packing_payloads_by_number(all_trip_order_numbers)
        for trip_pk, details in trip_order_details.items():
            logistics_client_by_pk[trip_pk] = _preview_label(details.get("clients") or [])
            logistics_direction_by_pk[trip_pk] = _preview_label(details.get("directions") or [])
            logistics_pallet_count_by_pk[trip_pk] = sum(
                _pallet_count_from_shipping_packing_payload(packing_payloads.get(order_number))
                for order_number in shipping_numbers_by_trip.get(trip_pk, [])
            )
    other_status_by_order = {}
    other_client_by_order = {}
    other_client_id_by_order = {}
    other_updated_at_by_order = {}
    if other_order_ids:
        entries = (
            OrderAuditEntry.objects.filter(
                order_type="other",
                order_id__in=list(other_order_ids),
            )
            .select_related("agency")
            .order_by("order_id", "-created_at")
        )
        for entry in entries:
            payload = entry.payload if isinstance(entry.payload, dict) else {}
            if entry.order_id not in other_status_by_order and _is_status_entry(entry):
                other_status_by_order[entry.order_id] = (
                    payload.get("status_label")
                    or payload.get("workflow_status_label")
                    or payload.get("status")
                    or payload.get("workflow_status")
                    or "-"
                )
            if entry.order_id not in other_client_by_order:
                other_client_by_order[entry.order_id] = _agency_panel_label(entry.agency) if entry.agency else "-"
            if entry.order_id not in other_client_id_by_order:
                other_client_id_by_order[entry.order_id] = int(entry.agency_id or 0) if entry.agency_id else None
            if entry.order_id not in other_updated_at_by_order:
                other_updated_at_by_order[entry.order_id] = entry.created_at

    def enrich_task_for_panel(task, full=False):
        task.filter_type = _task_filter_type(task)
        task.order_client_id = None
        task.order_client_label = None
        task.order_status_label = None
        task.order_status_tone = "neutral"
        task.order_notice_label = ""
        task.order_notice_tone = ""
        task.processing_packers_label = None
        task.logistics_driver_label = ""
        task.logistics_vehicle_number = ""
        task.logistics_direction_label = ""
        task.logistics_pallet_count = None
        task.panel_updated_at_label = _format_panel_datetime(getattr(task, "updated_at", None))
        if full:
            task.executor_label = task.assigned_to.full_name if task.assigned_to else None
            task.panel_url = reverse("todo:detail", args=[task.id])
            if role_key == "processing_worker":
                task.worker_title = _repair_panel_text(task.title)

        order_id = _extract_receiving_order_id(task.route)
        if order_id:
            task.order_status_label = receiving_status_by_order.get(order_id)
            task.order_status_tone = _status_tone_from_label(task.order_status_label)
            next_step_label = receiving_next_step_by_order.get(order_id) or ""
            next_step_tone = _next_step_tone_from_label(next_step_label)
            if next_step_tone:
                task.order_notice_label = next_step_label
                task.order_notice_tone = next_step_tone
            status_label_normalized = str(task.order_status_label or "").strip().lower()
            storekeeper_storage_done = (
                role_key == "storekeeper"
                and status_label_normalized == "товар принят и размещен на складе"
            )
            if (
                status_label_normalized == "выполнена"
                or status_label_normalized.startswith("отмен")
                or status_label_normalized.startswith("удален")
                or storekeeper_storage_done
            ):
                task.status = "done"
            elif role_key == "manager" and not _manager_receiving_task_requires_action(task):
                task.status = "done"
            elif task.status == "done":
                task.status = "in_progress"
            task.order_client_label = receiving_client_by_order.get(order_id)
            task.order_client_id = receiving_client_id_by_order.get(order_id)
            task.panel_updated_at_label = _format_panel_datetime(
                receiving_updated_at_by_order.get(order_id) or task.updated_at
            )
            if not full:
                return
            display_context = receiving_display_context_by_order.get(order_id) or {}
            task.executor_label = task.assigned_to.full_name if task.assigned_to else None
            task.panel_url = task.route
            task.panel_title = (
                _panel_title_for_task(task, "receiving", order_id)
                if _is_receiving_sign_task(task.route)
                else (display_context.get("title") or f"Заявка на приемку №{format_order_number('receiving', order_id)}")
            )
            task.panel_updated_at_label = _format_panel_datetime(
                display_context.get("updated_at") or receiving_updated_at_by_order.get(order_id) or task.updated_at
            )
            if role_key == "processing_worker":
                task.worker_title = _repair_panel_text(task.title)
            return

        order_id = _extract_processing_order_id(task.route)
        if order_id:
            task.order_status_label = processing_status_by_order.get(order_id)
            task.order_status_tone = _status_tone_from_label(task.order_status_label)
            task.order_client_label = processing_client_by_order.get(order_id)
            task.order_client_id = processing_client_id_by_order.get(order_id)
            task.panel_updated_at_label = _format_panel_datetime(
                processing_updated_at_by_order.get(order_id) or task.updated_at
            )
            if not full:
                return
            packers = processing_packers_by_order.get(order_id) or []
            task.processing_packers_label = ", ".join(packers) if packers else None
            if role_key == "processing_head" and processing_head_employee:
                if task.assigned_to and task.assigned_to.role == "processing_head":
                    task.executor_label = task.assigned_to.full_name
                else:
                    task.executor_label = processing_head_employee.full_name
            else:
                task.executor_label = task.assigned_to.full_name if task.assigned_to else None
            if role_key == "processing_worker":
                task.worker_title = _repair_panel_text(task.title).strip() or f"Задача на раскоробовку товара по заявке №{order_id}"
            task.panel_url = task.route
            if role_key == "processing_worker" and task.worker_title:
                task.panel_title = task.worker_title
            else:
                task.panel_title = _panel_title_for_task(task, "processing", order_id)
            return

        shipping_pk = _extract_shipping_order_pk(task.route)
        if shipping_pk is not None:
            is_supplement = _apply_shipping_supplement_panel_fields(task)
            task.order_status_label = shipping_status_by_pk.get(shipping_pk)
            task.order_status_tone = _status_tone_from_label(task.order_status_label)
            task.order_notice_label = shipping_reachtruck_notice_by_pk.get(shipping_pk) or ""
            task.order_notice_tone = _status_tone_from_label(task.order_notice_label)
            task.order_client_label = shipping_client_by_pk.get(shipping_pk)
            shipping_order = shipping_orders_by_pk.get(shipping_pk)
            task.order_client_id = int(shipping_order.agency_id or 0) if shipping_order and shipping_order.agency_id else None
            task.panel_updated_at_label = shipping_updated_at_by_pk.get(shipping_pk) or "-"
            task.executor_label = shipping_destination_by_pk.get(shipping_pk) or "-"
            task.processing_packers_label = shipping_marketplace_by_pk.get(shipping_pk) or "-"
            if shipping_order and shipping_order.status in terminal_shipping_labels:
                task.status = "done"
            if not full:
                return
            task.panel_url = task.route
            task.panel_title = (
                task.panel_title
                if is_supplement
                else _shipping_panel_title(shipping_order, shipping_pk)
            )
            return

        trip_pk = _extract_logistics_trip_pk(task.route)
        if trip_pk is not None:
            task.order_status_label = logistics_status_by_pk.get(trip_pk)
            task.order_status_tone = _status_tone_from_label(task.order_status_label)
            if task.order_status_label and task.order_status_label.strip().lower() == "завершен":
                task.status = "done"
            task.panel_updated_at_label = _format_panel_datetime(
                logistics_updated_at_by_pk.get(trip_pk) or task.updated_at
            )
            if not full:
                return
            task.order_client_label = logistics_client_by_pk.get(trip_pk) or "-"
            task.logistics_driver_label = logistics_driver_by_pk.get(trip_pk) or "-"
            task.logistics_vehicle_number = logistics_vehicle_by_pk.get(trip_pk) or "-"
            task.logistics_direction_label = logistics_direction_by_pk.get(trip_pk) or "-"
            task.logistics_pallet_count = int(logistics_pallet_count_by_pk.get(trip_pk) or 0)
            task.executor_label = task.assigned_to.full_name if task.assigned_to else None
            task.panel_url = task.route
            task.panel_title = _repair_panel_text(task.title)
            return

        order_id = _extract_other_order_id(task.route)
        if order_id:
            task.order_status_label = other_status_by_order.get(order_id)
            task.order_status_tone = _status_tone_from_label(task.order_status_label)
            task.order_client_label = other_client_by_order.get(order_id)
            task.order_client_id = other_client_id_by_order.get(order_id)
            task.panel_updated_at_label = _format_panel_datetime(
                other_updated_at_by_order.get(order_id) or task.updated_at
            )
            if str(task.order_status_label or "").strip().lower() in {"completed", "выполнена", "завершена", "closed"}:
                task.status = "done"
            if not full:
                return
            task.executor_label = task.assigned_to.full_name if task.assigned_to else None
            task.panel_url = task.route
            task.panel_title = _panel_title_for_task(task, "other", order_id)
            return

        if full:
            task.order_client_label = None
            task.panel_title = _repair_panel_text(task.display_title())

    for task in tasks:
        enrich_task_for_panel(task, full=True)
    for task in live_snapshot_tasks:
        enrich_task_for_panel(task, full=True)
    for task in snapshot_tasks:
        task.filter_type = _task_filter_type(task)
        _apply_shipping_supplement_panel_fields(task)

    computed_tasks = list(tasks)
    if snapshot_tasks:
        open_routes = {task.route for task in tasks if task.status != "done" and task.route}
        open_shipping_keys = {
            shipping_key
            for task in tasks
            for shipping_key in [_shipping_task_group_key(task)]
            if task.status != "done" and shipping_key is not None
        }

        def _snapshot_has_live_equivalent(task):
            shipping_key = _shipping_task_group_key(task)
            if shipping_key is not None:
                return shipping_key in open_shipping_keys
            return bool(task.route and task.route in open_routes)

        snapshot_tasks = [
            task
            for task in snapshot_tasks
            if not _snapshot_has_live_equivalent(task)
        ]
        tasks = tasks + snapshot_tasks
        tasks = _dedupe_receiving_tasks(tasks)
        tasks = _dedupe_processing_tasks(tasks)
        tasks = _dedupe_shipping_tasks(tasks)
    tasks = _prefer_storekeeper_shipping_supplement(tasks)
    tasks = [
        task
        for task in tasks
        if not _hide_processing_head_shipping_task(task, role_key)
    ]
    if unsnapshotted_tasks and not force_rebuild and not store_snapshots:
        visible_computed_ids = {task.id for task in computed_tasks}
        _store_task_panel_snapshot_rows(
            unsnapshotted_tasks,
            role_key,
            hidden_task_ids={
                task.id
                for task in unsnapshotted_tasks
                if task.id not in visible_computed_ids
            },
        )

    if store_snapshots:
        _store_task_panel_snapshots(snapshot_source_tasks, tasks, role_key)

    panel = _build_task_panel_payload(
        tasks,
        context=context,
        request=request,
        role_key=role_key,
        allowed_filter_values=allowed_filter_values,
        filter_defs=filter_defs,
        selected_type=selected_type,
        selected_client=selected_client,
        selected_query=selected_query,
        selected_document_status=selected_document_status,
        limit_value=limit_value,
        today=today,
        show_meta=show_meta,
    )
    if open_only:
        open_routes = {
            str(getattr(task, "route", "") or "").strip()
            for task in tasks
            if getattr(task, "status", "") != "done"
            and str(getattr(task, "route", "") or "").strip()
        }
        open_receiving_ids = {
            order_id
            for task in tasks
            if getattr(task, "status", "") != "done"
            for order_id in [_extract_receiving_order_id(getattr(task, "route", None))]
            if order_id
        }
        loaded_done_receiving_ids = {
            order_id
            for task in tasks
            if getattr(task, "status", "") == "done"
            for order_id in [_extract_receiving_order_id(getattr(task, "route", None))]
            if order_id
        }
        open_processing_ids = {
            order_id
            for task in tasks
            if getattr(task, "status", "") != "done"
            for order_id in [_extract_processing_order_id(getattr(task, "route", None))]
            if order_id
        }
        loaded_done_processing_ids = {
            order_id
            for task in tasks
            if getattr(task, "status", "") == "done"
            for order_id in [_extract_processing_order_id(getattr(task, "route", None))]
            if order_id
        }
        open_shipping_keys = {
            shipping_key
            for task in tasks
            if getattr(task, "status", "") != "done"
            for shipping_key in [_shipping_task_group_key(task)]
            if shipping_key is not None
        }
        loaded_done_shipping_keys = {
            shipping_key
            for task in tasks
            if getattr(task, "status", "") == "done"
            for shipping_key in [_shipping_task_group_key(task)]
            if shipping_key is not None
        }
        done_keys = set()
        for task_id, raw_route, filter_type, is_hidden, raw_title in snapshot_done_rows:
            if is_hidden or filter_type == "inspection":
                continue
            route = str(raw_route or "").strip()
            if route and route in open_routes:
                continue
            receiving_id = _extract_receiving_order_id(route)
            if receiving_id:
                if (
                    receiving_id not in open_receiving_ids
                    and receiving_id not in loaded_done_receiving_ids
                ):
                    done_keys.add(("receiving", receiving_id))
                continue
            processing_id = _extract_processing_order_id(route)
            if processing_id:
                if (
                    processing_id not in open_processing_ids
                    and processing_id not in loaded_done_processing_ids
                ):
                    done_keys.add(("processing", processing_id))
                continue
            shipping_pk = _extract_shipping_order_pk(route)
            if shipping_pk is not None:
                normalized_title = _repair_panel_text(raw_title).strip().casefold()
                subtype = (
                    "supplement"
                    if "подтвердить добор" in normalized_title
                    or "упаковать добор" in normalized_title
                    else "base"
                )
                shipping_key = (shipping_pk, subtype)
                if (
                    shipping_key not in open_shipping_keys
                    and shipping_key not in loaded_done_shipping_keys
                ):
                    done_keys.add(("shipping", *shipping_key))
                continue
            done_keys.add(("task", task_id))
        done_total = len(done_keys)
        for column in panel.get("task_panel_columns") or []:
            if column.get("status") == "done":
                column["count"] = int(column.get("count") or 0) + done_total
        for stat in panel.get("task_panel_stats") or []:
            if stat.get("status") == "done":
                stat["count"] = int(stat.get("count") or 0) + done_total
        panel["task_panel_total"] = int(panel.get("task_panel_total") or 0) + done_total
    return panel
