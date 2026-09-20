import re
from collections import Counter, defaultdict
from urllib.parse import quote, unquote

from django.views.generic import TemplateView

from audit.models import OrderAuditEntry
from employees.access import RoleRequiredMixin, get_request_employee
from fullbox.order_numbers import format_order_number
from processing_app import views as processing_views
from processing_app.stages import processing_stage_from_payload
from processing_app.subzones import (
    PROCESSING_SUBZONE_OUT,
    PROCESSING_SUBZONE_PACK,
    PROCESSING_SUBZONES,
    build_processing_subzone_state,
    processing_subzone_meta,
)
from todo.models import Task


_CARD_TASK_ROUTE_RE = re.compile(r"^/orders/processing/([^/]+)/card/([^/]+)/$")
_FLOW_TASK_ROUTE_RE = re.compile(r"^/orders/processing/([^/]+)/flow/$")


def _positive_int(value) -> int:
    try:
        return max(0, int(str(value or "0").strip()))
    except (TypeError, ValueError):
        return 0


def _client_label(entry) -> str:
    agency = getattr(entry, "agency", None)
    if not agency:
        return "-"
    return str(agency.short_name or agency.agn_name or agency.fio_agn or agency).strip() or "-"



def _clean_processing_text(value) -> str:
    return processing_views._repair_mojibake_text(value).strip()


class ProcessingWorkerDashboard(RoleRequiredMixin, TemplateView):
    template_name = "processing_worker/dashboard.html"
    allowed_roles = ("processing_worker", "packer")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        employee = get_request_employee(self.request)
        role = str(getattr(employee, "role", "") or "").strip()
        is_packer = role == "packer"
        is_processor = role == "processing_worker"
        context["role"] = role
        context["title"] = "Упаковщица" if is_packer else "Обработчик"
        context["is_packer"] = is_packer
        context["is_processor"] = is_processor
        context["employee"] = employee
        if not employee:
            context["card_queue"] = []
            context["order_assignments"] = []
            context["packaging_tasks"] = []
            context["selected_assignment"] = None
            context["queue_counts"] = {"active": 0, "done": 0, "all": 0}
            return context

        tasks = list(
            Task.objects.select_related("assigned_to")
            .filter(
                assigned_to=employee,
                route__contains="/orders/processing/",
            )
            .order_by("-updated_at", "-id")[:300]
        )
        card_tasks = []
        packaging_tasks_raw = []
        order_ids = set()
        for task in tasks:
            card_match = _CARD_TASK_ROUTE_RE.match(str(task.route or "").strip())
            if card_match and is_packer:
                order_id = card_match.group(1)
                card_tasks.append((task, order_id, unquote(card_match.group(2))))
                order_ids.add(order_id)
                continue
            flow_match = _FLOW_TASK_ROUTE_RE.match(str(task.route or "").strip())
            if flow_match and is_processor:
                order_id = flow_match.group(1)
                order_ids.add(order_id)
                packaging_tasks_raw.append((task, order_id))

        entries_by_order = defaultdict(list)
        if order_ids:
            for entry in (
                OrderAuditEntry.objects.filter(
                    order_type="processing",
                    order_id__in=order_ids,
                )
                .select_related("agency")
                .order_by("order_id", "created_at", "id")
            ):
                entries_by_order[str(entry.order_id)].append(entry)
        active_packaging_order_ids = set()
        if order_ids:
            packaging_routes = {
                f"/orders/processing/{order_id}/flow/": order_id
                for order_id in order_ids
            }
            for route in (
                Task.objects.filter(route__in=packaging_routes)
                .exclude(status__in=("done", "blocked"))
                .values_list("route", flat=True)
            ):
                order_id = packaging_routes.get(str(route or "").strip())
                if order_id:
                    active_packaging_order_ids.add(order_id)

        coassignees_by_route = defaultdict(list)
        card_routes = {str(task.route or "").strip() for task, _order_id, _card_id in card_tasks}
        if card_routes:
            for route, assignee_name in (
                Task.objects.filter(route__in=card_routes, assigned_to__isnull=False)
                .select_related("assigned_to")
                .order_by("route", "assigned_to__full_name", "id")
                .values_list("route", "assigned_to__full_name")
            ):
                clean_name = str(assignee_name or "").strip()
                if clean_name and clean_name not in coassignees_by_route[route]:
                    coassignees_by_route[route].append(clean_name)

        queue = []
        seen_routes = set()
        packaging_order_ids = {order_id for _task, order_id in packaging_tasks_raw}
        for task, order_id, card_id in card_tasks:
            if order_id in packaging_order_ids:
                continue
            if task.route in seen_routes:
                continue
            seen_routes.add(task.route)
            entries = entries_by_order.get(order_id) or []
            if not entries:
                continue
            payload = processing_views._processing_work_payload_from_entries(entries)
            card = next(
                (
                    item
                    for item in (payload.get("cards") or [])
                    if isinstance(item, dict)
                    and processing_views._processing_card_id(item) == card_id
                ),
                None,
            )
            if not card:
                continue
            rows = [row for row in (card.get("rows") or []) if isinstance(row, dict)]
            declared_qty = sum(
                _positive_int(row.get("qty") or row.get("recount_qty") or row.get("processing_qty"))
                for row in rows
            )
            barcodes = []
            for row in rows:
                barcode = str(row.get("barcode") or row.get("barcode_value") or "").strip()
                if barcode and barcode not in barcodes:
                    barcodes.append(barcode)
            processed_cards, _ = processing_views._processing_card_sets(payload)
            card_done = card_id in processed_cards or task.status == "done"
            placement_completed = processing_views._flow_closed_from_entries(entries)
            subzone_state = build_processing_subzone_state(
                card_ids=[card_id],
                processed_card_ids=processed_cards,
                assigned_card_ids=[card_id] if task.status != "done" else [],
                stage=processing_stage_from_payload(payload),
                packaging_active=order_id in active_packaging_order_ids,
                placement_completed=placement_completed,
            )
            operational_subzone = (
                subzone_state["cards"].get(card_id)
                or subzone_state["current"]
                or {}
            )
            if card_done and placement_completed:
                operational_subzone = processing_subzone_meta(PROCESSING_SUBZONE_OUT)
            if card_done:
                status_label = "Готово"
                status_tone = "success"
            elif task.status == "blocked":
                status_label = "Приостановлено"
                status_tone = "warning"
            elif task.status == "in_progress":
                status_label = "В работе"
                status_tone = "info"
            else:
                status_label = "Назначено"
                status_tone = "neutral"
            params = []
            for item in processing_views._processing_params_from_payload(payload):
                label = processing_views._repair_mojibake_text(item.get("label") or "").strip()
                value = processing_views._repair_mojibake_text(item.get("value") or "").strip()
                if not label or not value or value == "-":
                    continue
                params.append(f"{label}: {value}")
                if len(params) >= 4:
                    break
            queue.append(
                {
                    "task_id": task.id,
                    "order_id": order_id,
                    "order_number": format_order_number("processing", order_id),
                    "card_id": card_id,
                    "article": _clean_processing_text(card.get("article") or card_id) or card_id,
                    "result_article": _clean_processing_text(card.get("result_article") or ""),
                    "product_name": _clean_processing_text(card.get("product_name") or card.get("name") or "") or "-",
                    "photo_url": str(card.get("photo_url") or card.get("product_photo_url") or "").strip(),
                    "barcodes": barcodes[:3],
                    "declared_qty": declared_qty,
                    "client": _client_label(entries[-1]),
                    "status_label": status_label,
                    "status_tone": status_tone,
                    "is_done": card_done,
                    "priority": task.get_priority_display(),
                    "due_date": task.due_date,
                    "updated_at": task.updated_at,
                    "params": params,
                    "url": processing_views._processing_card_task_route(order_id, card_id),
                    "assignees": coassignees_by_route.get(task.route, []),
                    "operational_subzone": operational_subzone,
                }
            )

        pending_packaging_order_ids = set()
        if is_packer and order_ids:
            packaging_routes = {
                f"/orders/processing/{order_id}/flow/": order_id
                for order_id in order_ids
            }
            for route in Task.objects.filter(
                route__in=packaging_routes,
                assigned_to__role="processing_worker",
                status="blocked",
            ).values_list("route", flat=True):
                pending_order_id = packaging_routes.get(str(route or "").strip())
                if pending_order_id:
                    pending_packaging_order_ids.add(pending_order_id)
        assignments_by_order = {}
        for item in queue:
            order_id = item["order_id"]
            assignment = assignments_by_order.setdefault(
                order_id,
                {
                    "order_id": order_id,
                    "order_number": item["order_number"],
                    "client": item["client"],
                    "cards": [],
                    "assigned_workers": [],
                    "declared_qty": 0,
                    "pending_packaging": order_id in pending_packaging_order_ids,
                },
            )
            assignment["cards"].append(item)
            assignment["declared_qty"] += item["declared_qty"]
            for worker_name in item["assignees"]:
                if worker_name not in assignment["assigned_workers"]:
                    assignment["assigned_workers"].append(worker_name)

        order_assignments = []
        for assignment in assignments_by_order.values():
            cards = assignment["cards"]
            active_cards = [item for item in cards if not item["is_done"]]
            assignment["cards_total"] = len(cards)
            assignment["cards_done"] = len(cards) - len(active_cards)
            assignment["is_done"] = not active_cards
            assignment["status_label"] = (
                "Завершено"
                if assignment["is_done"]
                else (
                    "В работе"
                    if any(item["status_label"] == "В работе" for item in active_cards)
                    else "Назначено руководителем"
                )
            )
            assignment["status_tone"] = "success" if assignment["is_done"] else "info"
            representative = (active_cards or cards)[0]
            assignment["operational_subzone"] = representative["operational_subzone"]
            due_dates = [item["due_date"] for item in cards if item["due_date"]]
            assignment["due_date"] = min(due_dates) if due_dates else None
            assignment["url"] = (
                "/processing-worker/?order="
                + quote(str(assignment["order_id"]), safe="")
            )
            assignment["start_url"] = (
                assignment["url"]
                if assignment["is_done"]
                else representative["url"]
                + "?return="
                + quote("/processing-worker/", safe="")
            )
            order_assignments.append(assignment)
        order_assignments.sort(
            key=lambda item: (
                item["is_done"],
                item["due_date"] is None,
                item["due_date"],
                item["order_number"],
            )
        )

        packaging_tasks = []
        seen_packaging_routes = set()
        for task, order_id in packaging_tasks_raw:
            if task.route in seen_packaging_routes:
                continue
            seen_packaging_routes.add(task.route)
            entries = entries_by_order.get(order_id) or []
            payload = processing_views._processing_work_payload_from_entries(entries)
            quantity_summary = processing_views._processing_quantity_summary(payload)
            cards_total = processing_views._processing_cards_total(payload)
            processed_cards, _placed_cards = processing_views._processing_card_sets(payload)
            placement_completed = processing_views._flow_closed_from_entries(entries)
            task_done = task.status == "done" or placement_completed
            can_start = bool(
                not task_done
                and entries
                and processing_views.ProcessingFlowView()._can_start(entries)
            )
            packaging_tasks.append({
                "task_id": task.id,
                "title": _clean_processing_text(task.title) or f"Формирование коробов по заявке №{order_id}",
                "order_id": order_id,
                "order_number": format_order_number("processing", order_id),
                "client": _client_label((entries or [None])[-1]),
                "description": _clean_processing_text(task.description),
                "priority": task.get_priority_display(),
                "due_date": task.due_date,
                "url": task.route,
                "operational_subzone": processing_subzone_meta(
                    PROCESSING_SUBZONE_OUT if task_done else PROCESSING_SUBZONE_PACK
                ),
                "can_start": can_start,
                "status_label": (
                    "Завершено"
                    if task_done
                    else (
                        "Можно формировать короба"
                        if can_start
                        else "Ожидает готовности товара"
                    )
                ),
                "status_tone": "success" if task_done or can_start else "warning",
                "is_done": task_done,
                "cards_total": cards_total,
                "cards_done": len(processed_cards),
                "declared_qty": quantity_summary.get("declared_qty", 0),
                "processed_qty": quantity_summary.get("processed_qty", 0),
            })

        all_items = order_assignments + packaging_tasks
        counts = {
            "active": sum(1 for item in all_items if not item["is_done"]),
            "done": sum(1 for item in all_items if item["is_done"]),
            "all": len(all_items),
        }
        status_filter = str(self.request.GET.get("status") or "active").strip().lower()
        if status_filter not in {"active", "done", "all"}:
            status_filter = "active"
        search = str(self.request.GET.get("q") or "").strip()

        def matches_status(item):
            if status_filter == "active":
                return not item["is_done"]
            if status_filter == "done":
                return item["is_done"]
            return True

        filtered_order_assignments = [
            item for item in order_assignments if matches_status(item)
        ]
        filtered_packaging_tasks = [item for item in packaging_tasks if matches_status(item)]
        if search:
            search_key = search.casefold()
            filtered_order_assignments = [
                assignment
                for assignment in filtered_order_assignments
                if search_key
                in " ".join(
                    [
                        assignment["order_number"],
                        assignment["client"],
                        " ".join(assignment["assigned_workers"]),
                    ]
                    + [
                        " ".join(
                            (
                                card["article"],
                                card["result_article"],
                                card["product_name"],
                                " ".join(card["barcodes"]),
                            )
                        )
                        for card in assignment["cards"]
                    ]
                ).casefold()
            ]
            filtered_packaging_tasks = [
                item
                for item in filtered_packaging_tasks
                if search_key
                in " ".join(
                    (
                        item["order_number"],
                        item["title"],
                        item["client"],
                        item["description"],
                    )
                ).casefold()
            ]

        visible_items = filtered_packaging_tasks + filtered_order_assignments
        visible_subzone_counts = Counter(
            str((item.get("operational_subzone") or {}).get("key") or "")
            for item in visible_items
        )
        context["status_filter"] = status_filter
        context["search_query"] = search
        context["queue_counts"] = counts
        context["card_queue"] = []
        context["order_assignments"] = filtered_order_assignments
        context["packaging_tasks"] = filtered_packaging_tasks
        selected_order_id = str(self.request.GET.get("order") or "").strip()
        context["selected_assignment"] = next(
            (
                assignment
                for assignment in order_assignments
                if assignment["order_id"] == selected_order_id
            ),
            None,
        )
        context["processing_subzone_steps"] = [
            {
                **processing_subzone_meta(item["key"]),
                "count": visible_subzone_counts.get(item["key"], 0),
            }
            for item in PROCESSING_SUBZONES
        ]
        return context
