from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from audit.models import OrderAuditEntry, get_order_external_number
from todo.models import Task

from .stages import (
    processing_is_cancelled,
    processing_is_done,
    processing_stage_from_payload,
    processing_stage_label,
)


@dataclass(frozen=True)
class ProcessingOrderAuditRow:
    order_id: str
    external_number: str
    agency_name: str
    stage: str
    stage_label: str
    entries_count: int
    tasks_count: int
    open_tasks_count: int
    tasks_by_status: dict[str, int] = field(default_factory=dict)
    blockers: tuple[str, ...] = ()
    hard_blockers: tuple[str, ...] = ()
    declared_qty: int = 0
    processed_qty: int = 0
    boxed_qty: int = 0
    done: bool = False
    cancelled: bool = False

    @property
    def has_issues(self) -> bool:
        return (
            not self.stage
            or self.open_tasks_count > 0
            or bool(self.blockers)
        )

    def as_dict(self) -> dict:
        return {
            "order_id": self.order_id,
            "external_number": self.external_number,
            "agency_name": self.agency_name,
            "stage": self.stage,
            "stage_label": self.stage_label,
            "entries_count": self.entries_count,
            "tasks_count": self.tasks_count,
            "open_tasks_count": self.open_tasks_count,
            "tasks_by_status": dict(self.tasks_by_status),
            "blockers": list(self.blockers),
            "hard_blockers": list(self.hard_blockers),
            "declared_qty": self.declared_qty,
            "processed_qty": self.processed_qty,
            "boxed_qty": self.boxed_qty,
            "done": self.done,
            "cancelled": self.cancelled,
            "has_issues": self.has_issues,
        }


@dataclass(frozen=True)
class ProcessingOrderAuditReport:
    scanned_count: int
    rows: tuple[ProcessingOrderAuditRow, ...]
    sample_truncated: bool = False
    read_only: bool = True

    @property
    def issue_count(self) -> int:
        return sum(1 for row in self.rows if row.has_issues)

    def as_dict(self) -> dict:
        return {
            "read_only": self.read_only,
            "scanned_count": self.scanned_count,
            "rows_count": len(self.rows),
            "issue_count": self.issue_count,
            "sample_truncated": self.sample_truncated,
            "rows": [row.as_dict() for row in self.rows],
        }


def _agency_name(entry: OrderAuditEntry) -> str:
    agency = getattr(entry, "agency", None)
    if not agency:
        return ""
    return str(agency.short_name or agency.agn_name or agency.fio_agn or agency.id).strip()


def _int_value(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _candidate_order_ids(order_ids: list[str], *, scan_limit: int) -> list[str]:
    explicit_ids = [str(order_id or "").strip() for order_id in order_ids]
    explicit_ids = [order_id for order_id in explicit_ids if order_id]
    if explicit_ids:
        return list(dict.fromkeys(explicit_ids))

    latest_by_order: dict[str, None] = {}
    queryset = (
        OrderAuditEntry.objects.filter(order_type="processing")
        .order_by("-created_at", "-id")
        .values_list("order_id", flat=True)
    )
    for order_id in queryset[: max(scan_limit * 20, scan_limit, 100)]:
        key = str(order_id or "").strip()
        if key:
            latest_by_order.setdefault(key, None)
        if len(latest_by_order) >= scan_limit:
            break
    return list(latest_by_order)


def audit_processing_orders(
    *,
    order_ids: list[str] | None = None,
    limit: int = 100,
    include_done: bool = False,
    only_issues: bool = False,
) -> ProcessingOrderAuditReport:
    from . import views as processing_views

    scan_limit = max(int(limit or 0), 1)
    rows: list[ProcessingOrderAuditRow] = []
    scanned_count = 0
    truncated = False
    for order_id in _candidate_order_ids(order_ids or [], scan_limit=scan_limit * 3):
        entries = list(
            OrderAuditEntry.objects.filter(
                order_type="processing",
                order_id=order_id,
            )
            .select_related("agency")
            .order_by("created_at", "id")
        )
        if not entries:
            continue
        scanned_count += 1
        payload = dict(processing_views._processing_work_payload_from_entries(entries))
        stage = processing_stage_from_payload(payload)
        done = processing_is_done(payload)
        cancelled = processing_is_cancelled(payload)
        if not include_done and (done or cancelled):
            continue

        tasks = list(
            Task.objects.filter(route__startswith=f"/orders/processing/{order_id}/")
            .order_by("-updated_at", "-id")[:200]
        )
        tasks_by_status = dict(Counter(str(task.status or "-") for task in tasks))
        open_tasks_count = sum(1 for task in tasks if task.status != "done")

        blockers: tuple[str, ...] = ()
        hard_blockers: tuple[str, ...] = ()
        declared_qty = processed_qty = boxed_qty = 0
        if not done and not cancelled:
            finish_checks = processing_views._processing_finish_checks(
                str(order_id),
                payload,
                entries,
            )
            blockers = tuple(finish_checks.get("blockers") or ())
            hard_blockers = tuple(finish_checks.get("hard_blockers") or ())
            quantity_summary = finish_checks.get("quantity_summary") or {}
            declared_qty = _int_value(quantity_summary.get("declared_qty"))
            processed_qty = _int_value(quantity_summary.get("processed_qty"))
            boxed_qty = _int_value(quantity_summary.get("boxed_qty"))

        row = ProcessingOrderAuditRow(
            order_id=str(order_id),
            external_number=str(get_order_external_number("processing", order_id) or ""),
            agency_name=_agency_name(entries[-1]),
            stage=stage,
            stage_label=processing_stage_label(payload),
            entries_count=len(entries),
            tasks_count=len(tasks),
            open_tasks_count=open_tasks_count,
            tasks_by_status=tasks_by_status,
            blockers=blockers,
            hard_blockers=hard_blockers,
            declared_qty=declared_qty,
            processed_qty=processed_qty,
            boxed_qty=boxed_qty,
            done=done,
            cancelled=cancelled,
        )
        if only_issues and not row.has_issues:
            continue
        rows.append(row)
        if len(rows) >= scan_limit:
            truncated = True
            break

    return ProcessingOrderAuditReport(
        scanned_count=scanned_count,
        rows=tuple(rows),
        sample_truncated=truncated,
    )
