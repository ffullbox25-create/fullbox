from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable

from django.utils import timezone
from django.utils.dateparse import parse_datetime

from audit.models import OrderAuditEntry, get_order_external_number
from employees.models import Employee
from processing_app.models import ProcessingPrintJob
from processing_app.web_ui import (
    _processing_card_id,
    _processing_params_from_payload,
    _processing_work_payload_from_entries,
    _repair_mojibake_text,
)


def _quantity(value) -> int:
    try:
        return max(int(float(str(value or "0").replace(",", "."))), 0)
    except (TypeError, ValueError):
        return 0


def _aware_datetime(value, fallback=None):
    parsed = value if hasattr(value, "tzinfo") else parse_datetime(str(value or ""))
    if parsed and timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, timezone.get_current_timezone())
    return parsed or fallback


def _actor_label(user) -> str:
    if not user:
        return ""
    return str(user.get_full_name() or user.username or "").strip()


def _employee_maps():
    by_user_id = {}
    by_label = {}
    for employee in Employee.objects.all().select_related("user"):
        if employee.user_id:
            by_user_id[str(employee.user_id)] = employee
        labels = {str(employee.full_name or "").strip()}
        if employee.user:
            labels.add(_actor_label(employee.user))
            labels.add(str(employee.user.username or "").strip())
        for label in labels:
            if label:
                by_label[label.casefold()] = employee
    return by_user_id, by_label


def _employee_identity(
    *,
    raw_label="",
    raw_user_id="",
    employees_by_user_id,
    employees_by_label,
    **_kwargs,
):
    label = str(raw_label or "").strip()
    employee = employees_by_user_id.get(str(raw_user_id or "").strip())
    if not employee and label:
        employee = employees_by_label.get(label.casefold())
    if employee:
        return employee.full_name, employee.get_role_display()
    return label or "Не указан", "Не определена"


def _card_completion_events(entries: list[OrderAuditEntry]):
    events = {}
    for entry in entries:
        payload = entry.payload if isinstance(entry.payload, dict) else {}
        completed_ids = {
            str(value or "").strip().casefold()
            for value in (payload.get("processed_cards") or [])
            if str(value or "").strip()
        }
        for card in payload.get("cards") or []:
            if not isinstance(card, dict):
                continue
            card_id = _processing_card_id(card)
            if not card_id:
                continue
            if card.get("processed_at") or card.get("processed_done") or card.get("processed"):
                completed_ids.add(card_id.casefold())
        for card_id in completed_ids:
            events.setdefault(
                card_id,
                {
                    "date": entry.created_at,
                    "user_id": entry.user_id or "",
                    "label": _actor_label(entry.user),
                },
            )
    return events


def _card_map(payload: dict):
    cards = [card for card in (payload.get("cards") or []) if isinstance(card, dict)]
    by_id = {}
    by_article = {}
    for card in cards:
        card_id = _processing_card_id(card)
        if card_id:
            by_id[card_id.casefold()] = card
        article = str(card.get("article") or card.get("sku") or "").strip()
        if article:
            by_article.setdefault(article.casefold(), []).append(card)
    return cards, by_id, by_article


def _result_card(row: dict, cards, cards_by_id, cards_by_article):
    card_id = str(row.get("card_id") or "").strip()
    if card_id and card_id.casefold() in cards_by_id:
        return cards_by_id[card_id.casefold()]
    source_article = str(
        row.get("source_article") or row.get("article") or row.get("sku") or ""
    ).strip()
    article_matches = cards_by_article.get(source_article.casefold(), [])
    if len(article_matches) == 1:
        return article_matches[0]
    if len(cards) == 1:
        return cards[0]
    return None


def _technical_details(payload: dict) -> str:
    values = []
    for row in _processing_params_from_payload(payload):
        if not isinstance(row, dict):
            continue
        label = _repair_mojibake_text(row.get("label") or "").strip()
        value = _repair_mojibake_text(
            row.get("display_value") or row.get("value") or ""
        ).strip()
        if label and value:
            values.append(f"{label}: {value}")
    return "; ".join(values) or "—"


def _client_name(entries: list[OrderAuditEntry]) -> str:
    agency = next(
        (entry.agency for entry in reversed(entries) if entry.agency_id),
        None,
    )
    if not agency:
        return "—"
    return str(agency.short_name or agency.agn_name or "—").strip() or "—"


def _processing_rows(
    *,
    entries,
    payload,
    start,
    end,
    client,
    document,
    order_id,
    employees_by_user_id,
    employees_by_label,
    **_kwargs,
):
    rows = []
    cards, cards_by_id, cards_by_article = _card_map(payload)
    completion_events = _card_completion_events(entries)
    technical_details = _technical_details(payload)
    assigned_card_ids = {
        str(row.get("card_id") or "").strip().casefold()
        for row in (payload.get("processing_work_assignments") or [])
        if isinstance(row, dict)
        and str(row.get("status") or "").strip().lower() == "completed"
        and str(row.get("card_id") or "").strip()
    }
    for result in payload.get("processing_results") or []:
        if not isinstance(result, dict):
            continue
        card = _result_card(result, cards, cards_by_id, cards_by_article)
        if not card:
            continue
        card_id = _processing_card_id(card)
        if card_id.casefold() in assigned_card_ids:
            continue
        completion = completion_events.get(card_id.casefold(), {})
        completed = bool(
            card.get("processed_at")
            or card.get("processed_done")
            or card.get("processed")
            or completion
        )
        if not completed:
            continue
        completed_at = _aware_datetime(
            card.get("processed_at"),
            completion.get("date"),
        )
        if not completed_at or not start <= completed_at <= end:
            continue
        executor, role = _employee_identity(
            raw_label=card.get("processed_by") or completion.get("label"),
            raw_user_id=completion.get("user_id"),
            employees_by_user_id=employees_by_user_id,
            employees_by_label=employees_by_label,
        )
        source_article = str(
            result.get("source_article")
            or card.get("article")
            or card.get("sku")
            or ""
        ).strip()
        article = str(
            result.get("article")
            or result.get("result_article")
            or source_article
            or "—"
        ).strip()
        product = _repair_mojibake_text(
            result.get("product_name")
            or card.get("product_name")
            or card.get("name")
            or "—"
        ).strip()
        processed = _quantity(result.get("processed"))
        defect = _quantity(result.get("defect"))
        shortage = _quantity(result.get("shortage"))
        labels = _quantity(result.get("labels_printed"))
        tags = _quantity(result.get("tags_replaced"))
        rows.append(
            {
                "date": completed_at,
                "client": client,
                "operation": "Обработка товара",
                "product": product or "—",
                "article": article or "—",
                "source_article": source_article or "—",
                "size": str(result.get("size") or "—").strip() or "—",
                "barcode": str(result.get("barcode") or "—").strip() or "—",
                "processed": processed,
                "boxed": 0,
                "boxes": 0,
                "defect": defect,
                "shortage": shortage,
                "labels": labels,
                "tags": tags,
                "details": technical_details,
                "status": "Обработка завершена",
                "document": document,
                "executor": executor,
                "employee_role": role,
                "url": f"/orders/processing/{order_id}/work/",
                "search": [
                    executor,
                    role,
                    client,
                    document,
                    order_id,
                    product,
                    article,
                    source_article,
                    result.get("barcode"),
                    result.get("size"),
                    technical_details,
                ],
            }
        )
    return rows


def _work_assignment_rows(
    *,
    payload,
    start,
    end,
    client,
    document,
    order_id,
    employees_by_user_id,
    employees_by_label,
    **_kwargs,
):
    rows = []
    cards, cards_by_id, _cards_by_article = _card_map(payload)
    cards_by_id = cards_by_id or {}
    for assignment in payload.get("processing_work_assignments") or []:
        if not isinstance(assignment, dict):
            continue
        if str(assignment.get("status") or "").strip().lower() != "completed":
            continue
        completed_at = _aware_datetime(assignment.get("completed_at"))
        if not completed_at or not start <= completed_at <= end:
            continue
        card_id = str(assignment.get("card_id") or "").strip()
        card = cards_by_id.get(card_id.casefold()) or (cards[0] if len(cards) == 1 else {})
        operation = _repair_mojibake_text(
            assignment.get("operation_label") or "Операция по техкарте"
        ).strip()
        operation_key = str(assignment.get("operation_key") or "").strip().casefold()
        operation_folded = operation.casefold()
        actual_qty = _quantity(assignment.get("actual_qty"))
        is_label_operation = "маркир" in operation_folded or "этикет" in operation_folded
        is_tag_operation = "бирк" in operation_folded or "tag" in operation_key
        executor, role = _employee_identity(
            raw_label=assignment.get("assignee_name"),
            employees_by_user_id=employees_by_user_id,
            employees_by_label=employees_by_label,
        )
        article = str(
            assignment.get("article") or card.get("article") or card.get("sku") or "—"
        ).strip() or "—"
        product = _repair_mojibake_text(
            card.get("product_name") or card.get("name") or "—"
        ).strip() or "—"
        rows.append(
            {
                "date": completed_at,
                "client": client,
                "operation": operation or "Операция по техкарте",
                "product": product,
                "article": article,
                "source_article": article,
                "size": "—",
                "barcode": "—",
                "processed": 0 if is_label_operation or is_tag_operation else actual_qty,
                "boxed": 0,
                "boxes": 0,
                "defect": 0,
                "shortage": 0,
                "labels": actual_qty if is_label_operation else 0,
                "tags": actual_qty if is_tag_operation else 0,
                "details": (
                    f"План: {_quantity(assignment.get('planned_qty'))}; "
                    f"факт: {actual_qty}"
                    + (f"; комментарий: {assignment.get('comment')}" if assignment.get("comment") else "")
                ),
                "status": "Факт подтвержден руководителем",
                "document": document,
                "executor": executor,
                "employee_role": role,
                "url": f"/orders/processing/{order_id}/work/",
                "search": [
                    executor,
                    role,
                    client,
                    document,
                    order_id,
                    product,
                    article,
                    operation,
                    assignment.get("comment"),
                ],
            }
        )
    return rows


def _print_rows(
    *,
    print_jobs,
    payload,
    client,
    document,
    order_id,
    employees_by_user_id,
    employees_by_label,
    **_kwargs,
):
    cards, cards_by_id, _cards_by_article = _card_map(payload)
    grouped = {}
    for job in print_jobs or []:
        requested_by = str(job.requested_by or "").strip()
        parts = requested_by.split(":", 2)
        recipient = parts[2].strip() if len(parts) == 3 and parts[0] == "packer" else requested_by
        key = (
            str(job.card_id or "").strip(),
            recipient,
            str(job.article or "").strip(),
            str(job.barcode or "").strip(),
            str(job.size or "").strip(),
            str(job.processing_param_key or job.template_key or "").strip(),
        )
        item = grouped.setdefault(key, {"qty": 0, "date": job.updated_at})
        item["qty"] += _quantity(job.copies_count)
        if job.updated_at and (not item["date"] or job.updated_at > item["date"]):
            item["date"] = job.updated_at
    rows = []
    for key, item in grouped.items():
        card_id, recipient, article, barcode, size, template = key
        card = cards_by_id.get(card_id.casefold()) or (cards[0] if len(cards) == 1 else {})
        executor, role = _employee_identity(
            raw_label=recipient,
            employees_by_user_id=employees_by_user_id,
            employees_by_label=employees_by_label,
        )
        product = _repair_mojibake_text(
            card.get("product_name") or card.get("name") or "—"
        ).strip() or "—"
        rows.append(
            {
                "date": item["date"],
                "client": client,
                "operation": "Печать этикеток",
                "product": product,
                "article": article or str(card.get("article") or "—"),
                "source_article": article or str(card.get("article") or "—"),
                "size": size or "—",
                "barcode": barcode or "—",
                "processed": 0,
                "boxed": 0,
                "boxes": 0,
                "defect": 0,
                "shortage": 0,
                "labels": item["qty"],
                "tags": 0,
                "details": f"Шаблон: {template or 'стандартный'}; получатель: {executor}",
                "status": "Напечатано",
                "document": document,
                "executor": executor,
                "employee_role": role,
                "url": f"/orders/processing/{order_id}/work/",
                "search": [executor, role, client, document, order_id, product, article, barcode, template],
            }
        )
    return rows


def _closed_flow_date(entries: list[OrderAuditEntry], payload: dict):
    flow_closed_at = _aware_datetime(payload.get("flow_closed_at"))
    if flow_closed_at:
        return flow_closed_at
    for entry in reversed(entries):
        entry_payload = entry.payload if isinstance(entry.payload, dict) else {}
        if entry_payload.get("flow_closed") or entry_payload.get("act_state") == "closed":
            return entry.created_at
    return None


def _box_rows(
    *,
    entries,
    payload,
    start,
    end,
    client,
    document,
    order_id,
    employees_by_user_id,
    employees_by_label,
    **_kwargs,
):
    completed_at = _closed_flow_date(entries, payload)
    if not completed_at or not start <= completed_at <= end:
        return []
    if not (payload.get("flow_closed") or payload.get("act_state") == "closed"):
        return []
    rows = []
    for box in payload.get("act_boxes") or []:
        if not isinstance(box, dict):
            continue
        executor, role = _employee_identity(
            raw_label=box.get("owner_user_label"),
            raw_user_id=box.get("owner_user_id"),
            employees_by_user_id=employees_by_user_id,
            employees_by_label=employees_by_label,
        )
        box_code = str(box.get("code") or "—").strip() or "—"
        direction = str(
            box.get("direction") or box.get("direction_name") or ""
        ).strip()
        items = [item for item in (box.get("items") or []) if isinstance(item, dict)]
        for item_index, item in enumerate(items):
            article = str(item.get("sku_code") or item.get("sku") or "—").strip()
            product = _repair_mojibake_text(item.get("name") or "—").strip()
            boxed = _quantity(item.get("qty"))
            rows.append(
                {
                    "date": completed_at,
                    "client": client,
                    "operation": "Формирование коробов",
                    "product": product or "—",
                    "article": article or "—",
                    "source_article": article or "—",
                    "size": str(item.get("size") or "—").strip() or "—",
                    "barcode": str(item.get("barcode") or "—").strip() or "—",
                    "processed": 0,
                    "boxed": boxed,
                    "boxes": 1 if item_index == 0 else 0,
                    "defect": 0,
                    "shortage": 0,
                    "labels": 0,
                    "tags": 0,
                    "details": (
                        f"Короб: {box_code}"
                        + (f"; направление: {direction}" if direction else "")
                    ),
                    "status": "Короб сформирован",
                    "document": document,
                    "executor": executor,
                    "employee_role": role,
                    "url": f"/orders/processing/{order_id}/work/",
                    "search": [
                        executor,
                        role,
                        client,
                        document,
                        order_id,
                        product,
                        article,
                        item.get("barcode"),
                        item.get("size"),
                        box_code,
                        direction,
                    ],
                }
            )
    return rows


def _entry_groups(entries: Iterable[OrderAuditEntry]):
    current_order_id = None
    current_entries = []
    for entry in entries:
        if current_order_id is not None and entry.order_id != current_order_id:
            yield current_order_id, current_entries
            current_entries = []
        current_order_id = entry.order_id
        current_entries.append(entry)
    if current_order_id is not None:
        yield current_order_id, current_entries


def processing_employee_fact_rows(start, end):
    employees_by_user_id, employees_by_label = _employee_maps()
    queryset = (
        OrderAuditEntry.objects.filter(
            order_type="processing",
            created_at__lte=end,
        )
        .select_related("agency", "user")
        .order_by("order_id", "created_at", "id")
    )
    print_jobs_by_order = defaultdict(list)
    for job in ProcessingPrintJob.objects.filter(
        status=ProcessingPrintJob.STATUS_PRINTED,
        updated_at__gte=start,
        updated_at__lte=end,
    ).order_by("order_id", "updated_at", "id"):
        print_jobs_by_order[str(job.order_id or "")].append(job)
    rows = []
    for order_id, entries in _entry_groups(queryset.iterator(chunk_size=1000)):
        payload = _processing_work_payload_from_entries(entries)
        client = _client_name(entries)
        document = get_order_external_number("processing", order_id)
        common = {
            "entries": entries,
            "payload": payload,
            "start": start,
            "end": end,
            "client": client,
            "document": document,
            "order_id": order_id,
            "employees_by_user_id": employees_by_user_id,
            "employees_by_label": employees_by_label,
            "print_jobs": print_jobs_by_order.get(str(order_id), []),
        }
        rows.extend(_work_assignment_rows(**common))
        rows.extend(_processing_rows(**common))
        rows.extend(_print_rows(**common))
        rows.extend(_box_rows(**common))
    return rows
