from __future__ import annotations

from collections import defaultdict
from datetime import timedelta
from urllib.parse import urlencode

from django.core.cache import cache
from django.db import transaction
from django.db.models import Count, Exists, OuterRef, Q
from django.urls import reverse
from django.utils import timezone

from ..models import (
    FbsIntegrationProfile,
    FbsInventorySession,
    FbsOrder,
    FbsOrderItem,
    FbsOrderStockAllocation,
    FbsPickTask,
    FbsStorekeeperAlertAcknowledgement,
    FbsStorekeeperResponsible,
)
from .picking import ACTIVE_TASK_STATUSES
from .physical_locations import fbs_box_physical_location_label
from .sync import marketplace_terminal_order_q


ALERT_AFTER_HOURS = 10
ALERT_REPEAT_HOURS = 4
ALERT_CACHE_SECONDS = 60
ALERT_CACHE_KEY = "fbs:storekeeper-alerts:v1"
NOT_STARTED_STATUSES = (
    FbsOrder.STATUS_RECEIVED,
    FbsOrder.STATUS_VALIDATION_FAILED,
    FbsOrder.STATUS_AWAITING_STOCK,
    FbsOrder.STATUS_RESERVED,
    FbsOrder.STATUS_EXCEPTION,
)
OPEN_INVENTORY_STATUSES = (
    FbsInventorySession.STATUS_PLANNED,
    FbsInventorySession.STATUS_DRAINING,
    FbsInventorySession.STATUS_COUNTING,
    FbsInventorySession.STATUS_RECOUNT,
    FbsInventorySession.STATUS_APPROVAL,
)
SECURED_ALLOCATION_STATUSES = (
    FbsOrderStockAllocation.STATUS_RESERVED,
    FbsOrderStockAllocation.STATUS_PICKING,
    FbsOrderStockAllocation.STATUS_PICKED,
)
READY_AVAILABILITY = {"secured", "fbs_available"}
STOCK_AVAILABILITY = {
    "in_movement",
    "needs_replenishment",
    "unavailable",
    "no_barcode",
}
KIND_PRESENTATION = {
    "ready": {
        "title": "Товар есть — запустите сборку",
        "action_label": "Открыть очередь клиента",
    },
    "stock": {
        "title": "Нет товара — требуется подсорт",
        "action_label": "Открыть проблемные заказы",
    },
    "problem": {
        "title": "Ошибка заказа — требуется разбор",
        "action_label": "Открыть проблемы",
    },
    "inventory": {
        "title": "Товар не найден — выполните инвентаризацию",
        "action_label": "Открыть инвентаризацию",
    },
}


def is_storekeeper_alert_responsible(user) -> bool:
    return bool(
        getattr(user, "is_authenticated", False)
        and FbsStorekeeperResponsible.objects.filter(
            user_id=user.id,
            is_active=True,
            user__is_active=True,
        ).exists()
    )


def _started_before_q(moment) -> Q:
    return Q(ordered_at__lte=moment) | Q(
        ordered_at__isnull=True,
        imported_at__lte=moment,
    )


def _user_label(user) -> str:
    employee = getattr(user, "employee_profile", None)
    if employee is not None and str(employee.full_name or "").strip():
        return str(employee.full_name).strip()
    return str(user.get_full_name() or user.get_username()).strip()


def _age_label(now, started_at) -> str:
    total_minutes = max(int((now - started_at).total_seconds() // 60), 0)
    days, remaining = divmod(total_minutes, 24 * 60)
    hours, minutes = divmod(remaining, 60)
    if days:
        return f"{days} д {hours} ч"
    if hours:
        return f"{hours} ч {minutes} мин"
    return f"{minutes} мин"


def _order_locations(order_ids: list[int]) -> dict[int, list[str]]:
    result: dict[int, list[str]] = defaultdict(list)
    allocations = (
        FbsOrderStockAllocation.objects.filter(
            order_item__order_id__in=order_ids,
            status__in=SECURED_ALLOCATION_STATUSES,
        )
        .select_related("balance__box__pallet__cell__location")
        .order_by("order_item__order_id", "id")
    )
    for allocation in allocations:
        order_id = allocation.order_item.order_id
        label = allocation.balance.box.pallet.cell.warehouse_location_label
        if label and label not in result[order_id]:
            result[order_id].append(label)
    return result


def _order_products(order_ids: list[int]) -> dict[int, list[str]]:
    result: dict[int, list[str]] = defaultdict(list)
    for item in FbsOrderItem.objects.filter(order_id__in=order_ids).order_by(
        "order_id", "id"
    ):
        label = str(item.product_name or item.external_sku or item.barcode or "Товар")
        if label not in result[item.order_id] and len(result[item.order_id]) < 3:
            result[item.order_id].append(label)
    return result


def _order_action_url(kind: str, agency_id: int) -> str:
    if kind == "ready":
        query = urlencode(
            {
                "attention": "stale_unstarted",
                "availability": "ready",
                "agency": agency_id,
            }
        )
        return f"{reverse('fbs:operator_queue')}?{query}"
    if kind == "stock":
        query = urlencode({"problem": "open", "agency": agency_id})
        return f"{reverse('fbs:operator_orders')}?{query}"
    query = urlencode({"problem": "open", "agency": agency_id})
    return f"{reverse('fbs:operator_orders')}?{query}"


def _inventory_location_labels(session) -> list[str]:
    """Show where a shortage box physically stood, not its virtual FBS plan."""
    if session.box_id:
        label = fbs_box_physical_location_label(session.box)
    else:
        label = session.workflow_place_label
    return [label] if label else []


def _build_active_cases() -> list[dict]:
    now = timezone.now()
    threshold = now - timedelta(hours=ALERT_AFTER_HOURS)
    orders = list(
        FbsOrder.objects.filter(internal_status__in=NOT_STARTED_STATUSES)
        .exclude(marketplace_terminal_order_q())
        .select_related("profile__agency")
        .annotate(
            has_active_pick_task=Exists(
                FbsPickTask.objects.filter(
                    order_id=OuterRef("pk"),
                    status__in=ACTIVE_TASK_STATUSES,
                )
            )
        )
        .filter(has_active_pick_task=False)
        .filter(_started_before_q(threshold))
        .order_by("ordered_at", "imported_at", "id")
    )
    # Reuse the same availability and marketplace validation used by the
    # operator queue, so the alert never promises that a blocked order is ready.
    from ..operator_views import _decorate_orders

    _decorate_orders(orders)
    order_ids = [order.id for order in orders]
    locations = _order_locations(order_ids)
    products = _order_products(order_ids)
    cases: list[dict] = []
    for order in orders:
        if order.can_queue and order.availability_state in READY_AVAILABILITY:
            kind = "ready"
        elif order.availability_state in STOCK_AVAILABILITY:
            kind = "stock"
        else:
            kind = "problem"
        started_at = order.ordered_at or order.imported_at
        cases.append(
            {
                "key": f"order:{order.id}:{kind}",
                "kind": kind,
                "agency_id": order.profile.agency_id,
                "client": str(order.profile.agency),
                "marketplace": order.profile.get_marketplace_display(),
                "order_id": order.external_order_id,
                "age_label": _age_label(now, started_at),
                "started_at": started_at.isoformat(),
                "cutoff_at": order.cutoff_at.isoformat() if order.cutoff_at else None,
                "locations": locations.get(order.id, []),
                "products": products.get(order.id, []),
                "action_url": _order_action_url(kind, order.profile.agency_id),
            }
        )

    inventory_sessions = (
        FbsInventorySession.objects.filter(status__in=OPEN_INVENTORY_STATUSES)
        .annotate(issue_count=Count("pick_issues", distinct=True))
        .filter(issue_count__gt=0)
        .select_related(
            "agency",
            "cell__location",
            "pallet__cell__location",
            "box__pallet__cell__location",
            "box__source_container__current_location",
        )
        .prefetch_related("pick_issues__task__order__profile__agency")
        .order_by("created_at", "id")
    )
    for session in inventory_sessions:
        linked_orders = {
            issue.task.order_id: issue.task.order
            for issue in session.pick_issues.all()
            if issue.task_id and issue.task.order_id
        }
        client = str(session.agency or "")
        if not client and linked_orders:
            client = str(next(iter(linked_orders.values())).profile.agency)
        cases.append(
            {
                "key": f"inventory:{session.id}",
                "kind": "inventory",
                "agency_id": session.agency_id,
                "client": client or "Клиент не указан",
                "marketplace": "FBS",
                "order_id": ", ".join(
                    order.external_order_id for order in linked_orders.values()
                ),
                "order_count": len(linked_orders),
                "age_label": _age_label(now, session.created_at),
                "started_at": session.created_at.isoformat(),
                "cutoff_at": None,
                "locations": _inventory_location_labels(session),
                "products": [session.workflow_target_label]
                if session.workflow_target_label
                else [],
                "action_url": reverse(
                    "fbs:tsd_inventory_detail", kwargs={"session_id": session.id}
                ),
            }
        )
    return cases


def active_storekeeper_alert_cases(*, use_cache: bool = True) -> list[dict]:
    if use_cache:
        cached = cache.get(ALERT_CACHE_KEY)
        if cached is not None:
            return cached
    cases = _build_active_cases()
    cache.set(ALERT_CACHE_KEY, cases, ALERT_CACHE_SECONDS)
    return cases


def _group_cases(cases: list[dict]) -> list[dict]:
    grouped: dict[tuple, dict] = {}
    for case in cases:
        group_key = (
            case["kind"],
            case.get("agency_id"),
            case["marketplace"],
            case["action_url"] if case["kind"] == "inventory" else "",
        )
        group = grouped.setdefault(
            group_key,
            {
                "kind": case["kind"],
                "title": KIND_PRESENTATION[case["kind"]]["title"],
                "action_label": KIND_PRESENTATION[case["kind"]]["action_label"],
                "action_url": case["action_url"],
                "client": case["client"],
                "marketplace": case["marketplace"],
                "keys": [],
                "orders": [],
                "locations": [],
                "products": [],
                "oldest_started_at": case["started_at"],
                "oldest_age_label": case["age_label"],
                "nearest_cutoff": case["cutoff_at"],
                "case_count": 0,
            },
        )
        group["keys"].append(case["key"])
        group["case_count"] += int(case.get("order_count") or 1)
        if case["order_id"] and len(group["orders"]) < 20:
            group["orders"].append(case["order_id"])
        for label in case["locations"]:
            if label not in group["locations"] and len(group["locations"]) < 5:
                group["locations"].append(label)
        for label in case["products"]:
            if label not in group["products"] and len(group["products"]) < 5:
                group["products"].append(label)
        if case["started_at"] < group["oldest_started_at"]:
            group["oldest_started_at"] = case["started_at"]
            group["oldest_age_label"] = case["age_label"]
        cutoff = case["cutoff_at"]
        if cutoff and (not group["nearest_cutoff"] or cutoff < group["nearest_cutoff"]):
            group["nearest_cutoff"] = cutoff
    priority = {"inventory": 0, "ready": 1, "stock": 2, "problem": 3}
    return sorted(
        grouped.values(),
        key=lambda row: (priority[row["kind"]], row["oldest_started_at"], row["client"]),
    )


def build_storekeeper_alert_payload() -> dict:
    now = timezone.now()
    repeat_before = now - timedelta(hours=ALERT_REPEAT_HOURS)
    cases = active_storekeeper_alert_cases()
    keys = [case["key"] for case in cases]
    acknowledgements = {
        row.alert_key: row
        for row in FbsStorekeeperAlertAcknowledgement.objects.filter(
            alert_key__in=keys
        ).select_related("responsible__employee_profile")
    }
    groups = _group_cases(cases)
    due_count = 0
    for group in groups:
        group_acks = [acknowledgements.get(key) for key in group["keys"]]
        due_keys = [
            key
            for key, acknowledgement in zip(group["keys"], group_acks)
            if acknowledgement is None
            or acknowledgement.acknowledged_at <= repeat_before
        ]
        fresh_acks = [
            acknowledgement
            for acknowledgement in group_acks
            if acknowledgement is not None
            and acknowledgement.acknowledged_at > repeat_before
        ]
        latest_ack = max(
            fresh_acks,
            key=lambda row: row.acknowledged_at,
            default=None,
        )
        group["due"] = bool(due_keys)
        group["due_keys"] = due_keys
        group["claimed_count"] = len(fresh_acks)
        group["claimed_by"] = (
            _user_label(latest_ack.responsible) if latest_ack is not None else ""
        )
        group["claimed_at"] = (
            timezone.localtime(latest_ack.acknowledged_at).strftime("%d.%m %H:%M")
            if latest_ack is not None
            else ""
        )
        due_count += len(due_keys)
    return {
        "enabled": True,
        "checked_at": timezone.localtime(now).strftime("%d.%m.%Y %H:%M"),
        "threshold_hours": ALERT_AFTER_HOURS,
        "repeat_hours": ALERT_REPEAT_HOURS,
        "total_count": len(cases),
        "due_count": due_count,
        "groups": groups,
    }


def acknowledge_storekeeper_alerts(*, user, alert_keys: list[str]) -> int:
    if not is_storekeeper_alert_responsible(user):
        raise PermissionError("Сотрудник не назначен ответственным за FBS.")
    requested = {
        str(key or "").strip()
        for key in alert_keys[:1000]
        if str(key or "").strip()
    }
    if not requested:
        return 0
    active_cases = active_storekeeper_alert_cases(use_cache=False)
    active_kinds = {
        case["key"]: case["kind"]
        for case in active_cases
        if case["key"] in requested
    }
    if not active_kinds:
        return 0
    now = timezone.now()
    with transaction.atomic():
        existing = set(
            FbsStorekeeperAlertAcknowledgement.objects.filter(
                alert_key__in=active_kinds
            ).values_list("alert_key", flat=True)
        )
        FbsStorekeeperAlertAcknowledgement.objects.bulk_create(
            [
                FbsStorekeeperAlertAcknowledgement(
                    alert_key=key,
                    alert_kind=kind,
                    responsible=user,
                    acknowledged_at=now,
                )
                for key, kind in active_kinds.items()
                if key not in existing
            ],
            ignore_conflicts=True,
        )
        FbsStorekeeperAlertAcknowledgement.objects.filter(
            alert_key__in=active_kinds
        ).update(
            responsible=user,
            acknowledged_at=now,
        )
    return len(active_kinds)
