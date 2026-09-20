from __future__ import annotations

from dataclasses import asdict, dataclass
from itertools import groupby
from typing import Iterable

from audit.models import OrderAuditEntry

from .stages import processing_is_done, processing_stage_from_payload
from .web_ui import _processing_quantity_summary, _processing_work_payload_from_entries


@dataclass(frozen=True, slots=True)
class ClosedProcessingDiscrepancy:
    order_id: str
    agency_id: int | None
    agency_name: str
    stage: str
    status_label: str
    declared_qty: int
    processed_qty: int
    boxed_qty: int
    differences: tuple[str, ...]
    discrepancy_status: str
    review_status: str
    review_comment: str
    reviewed_by: str
    reviewed_at: str
    completed_at: str
    event_count: int

    def as_dict(self) -> dict:
        payload = asdict(self)
        payload["differences"] = list(self.differences)
        return payload


@dataclass(frozen=True, slots=True)
class ClosedProcessingDiscrepancyReport:
    scanned_count: int
    closed_count: int
    matching_count: int
    discrepancy_count: int
    rows: tuple[ClosedProcessingDiscrepancy, ...]
    sample_truncated: bool

    def as_dict(self) -> dict:
        return {
            "read_only": True,
            "scanned_count": self.scanned_count,
            "closed_count": self.closed_count,
            "matching_count": self.matching_count,
            "discrepancy_count": self.discrepancy_count,
            "sample_truncated": self.sample_truncated,
            "rows": [row.as_dict() for row in self.rows],
        }


def _difference_codes(declared: int, processed: int, boxed: int) -> tuple[str, ...]:
    differences = []
    if declared != processed:
        differences.append("declared_vs_processed")
    if processed != boxed:
        differences.append("processed_vs_boxed")
    if declared != boxed:
        differences.append("declared_vs_boxed")
    return tuple(differences)


def latest_closed_discrepancy_review(entries: Iterable[OrderAuditEntry]) -> dict:
    for entry in reversed(list(entries)):
        payload = entry.payload if isinstance(entry.payload, dict) else {}
        review = payload.get("processing_discrepancy_review")
        if isinstance(review, dict):
            return dict(review)
    return {}


def audit_closed_processing_discrepancies(
    *,
    order_ids: Iterable[str] | None = None,
    sample_limit: int = 100,
) -> ClosedProcessingDiscrepancyReport:
    normalized_order_ids = {
        str(order_id or "").strip()
        for order_id in (order_ids or [])
        if str(order_id or "").strip()
    }
    queryset = (
        OrderAuditEntry.objects.filter(order_type="processing")
        .select_related("agency")
        .order_by("order_id", "created_at", "id")
    )
    if normalized_order_ids:
        queryset = queryset.filter(order_id__in=normalized_order_ids)

    scanned_count = 0
    closed_count = 0
    matching_count = 0
    discrepancy_count = 0
    rows: list[ClosedProcessingDiscrepancy] = []
    normalized_limit = max(int(sample_limit or 0), 0)

    grouped_entries = groupby(
        queryset.iterator(chunk_size=1000),
        key=lambda entry: str(entry.order_id),
    )
    for order_id, grouped in grouped_entries:
        entries = list(grouped)
        if not entries:
            continue
        scanned_count += 1
        payload = _processing_work_payload_from_entries(entries)
        if not processing_is_done(payload):
            continue
        closed_count += 1
        quantity_summary = _processing_quantity_summary(payload)
        declared_qty = int(quantity_summary.get("declared_qty") or 0)
        processed_qty = int(quantity_summary.get("processed_qty") or 0)
        boxed_qty = int(quantity_summary.get("boxed_qty") or 0)
        differences = _difference_codes(declared_qty, processed_qty, boxed_qty)
        if not differences:
            matching_count += 1
            continue

        discrepancy_count += 1
        if len(rows) >= normalized_limit:
            continue
        latest = entries[-1]
        review = latest_closed_discrepancy_review(entries)
        agency_name = ""
        if latest.agency_id and latest.agency:
            agency_name = str(latest.agency.agn_name or "").strip()
        rows.append(
            ClosedProcessingDiscrepancy(
                order_id=order_id,
                agency_id=latest.agency_id,
                agency_name=agency_name,
                stage=processing_stage_from_payload(payload),
                status_label=str(payload.get("status_label") or "").strip(),
                declared_qty=declared_qty,
                processed_qty=processed_qty,
                boxed_qty=boxed_qty,
                differences=differences,
                discrepancy_status=str(payload.get("discrepancy_status") or "").strip(),
                review_status=str(review.get("status") or "").strip(),
                review_comment=str(review.get("comment") or "").strip(),
                reviewed_by=str(review.get("reviewed_by") or "").strip(),
                reviewed_at=str(review.get("reviewed_at") or "").strip(),
                completed_at=str(
                    payload.get("completed_at")
                    or timezone_iso(latest.created_at)
                ),
                event_count=len(entries),
            )
        )

    return ClosedProcessingDiscrepancyReport(
        scanned_count=scanned_count,
        closed_count=closed_count,
        matching_count=matching_count,
        discrepancy_count=discrepancy_count,
        rows=tuple(rows),
        sample_truncated=discrepancy_count > len(rows),
    )


def timezone_iso(value) -> str:
    return value.isoformat() if value else ""
