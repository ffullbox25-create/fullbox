from __future__ import annotations

from collections import defaultdict

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from audit.models import OrderAuditEntry
from processing_app.models import ProcessingWorkEvent
from processing_app.work_journal import (
    processing_work_event_key,
    record_processing_assignment_completion,
    record_processing_box_completions,
)


def _aware_datetime(value, fallback):
    parsed = parse_datetime(str(value or "").strip())
    if parsed is None:
        return fallback
    if timezone.is_naive(parsed):
        return timezone.make_aware(parsed, timezone.get_current_timezone())
    return parsed


def _latest_payload(entries, key):
    for entry in reversed(entries):
        payload = entry.payload if isinstance(entry.payload, dict) else {}
        if key in payload and payload.get(key) not in (None, "", [], {}):
            return payload.get(key), entry
    return None, entries[-1]


class Command(BaseCommand):
    help = (
        "Переносит завершённые операции обработки и сформированные короба "
        "из аудита заявок в ProcessingWorkEvent. Без --apply работает read-only."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Записать события. Без флага команда только показывает план переноса.",
        )

    def handle(self, *args, **options):
        apply_changes = bool(options.get("apply"))
        entries_by_order = defaultdict(list)
        queryset = (
            OrderAuditEntry.objects.filter(order_type="processing")
            .select_related("agency", "user")
            .order_by("order_id", "created_at", "id")
        )
        for entry in queryset.iterator(chunk_size=1000):
            entries_by_order[str(entry.order_id)].append(entry)

        assignment_candidates = []
        box_candidates = []
        ownerless_boxes = 0
        for order_id, entries in entries_by_order.items():
            assignments, assignment_entry = _latest_payload(
                entries,
                "processing_work_assignments",
            )
            if isinstance(assignments, list):
                for assignment in assignments:
                    if not isinstance(assignment, dict):
                        continue
                    if str(assignment.get("status") or "").strip().lower() != "completed":
                        continue
                    occurred_at = _aware_datetime(
                        assignment.get("completed_at"),
                        assignment_entry.created_at,
                    )
                    assignment_candidates.append(
                        (order_id, assignment_entry, assignment, occurred_at)
                    )

            boxes, box_entry = _latest_payload(entries, "act_boxes")
            if not isinstance(boxes, list):
                continue
            flow_closed_at, flow_entry = _latest_payload(entries, "flow_closed_at")
            occurred_at = _aware_datetime(
                flow_closed_at,
                (flow_entry or box_entry).created_at,
            )
            owned_boxes = []
            for box in boxes:
                if not isinstance(box, dict) or not str(box.get("code") or "").strip():
                    continue
                if not box.get("owner_user_id"):
                    ownerless_boxes += 1
                    continue
                owned_boxes.append(box)
            if owned_boxes:
                box_candidates.append((order_id, box_entry, owned_boxes, occurred_at))

        candidate_keys = []
        for order_id, _entry, assignment, _occurred_at in assignment_candidates:
            assignment_id = str(assignment.get("id") or "").strip()
            if assignment_id:
                candidate_keys.append(
                    processing_work_event_key(
                        ProcessingWorkEvent.TYPE_OPERATION_COMPLETED,
                        order_id,
                        assignment_id,
                    )
                )
        for order_id, _entry, boxes, _occurred_at in box_candidates:
            candidate_keys.extend(
                processing_work_event_key(
                    ProcessingWorkEvent.TYPE_BOX_FORMED,
                    order_id,
                    box.get("code"),
                )
                for box in boxes
            )
        existing_keys = set(
            ProcessingWorkEvent.objects.filter(event_key__in=candidate_keys).values_list(
                "event_key", flat=True
            )
        )

        created_assignments = 0
        created_boxes = 0
        if apply_changes:
            with transaction.atomic():
                for order_id, entry, assignment, occurred_at in assignment_candidates:
                    _event, created = record_processing_assignment_completion(
                        order_id=order_id,
                        agency=entry.agency,
                        assignment=assignment,
                        occurred_at=occurred_at,
                        recorded_by=entry.user,
                    )
                    created_assignments += int(created)
                for order_id, entry, boxes, occurred_at in box_candidates:
                    created_boxes += record_processing_box_completions(
                        order_id=order_id,
                        agency=entry.agency,
                        boxes=boxes,
                        occurred_at=occurred_at,
                        recorded_by=entry.user,
                    )

        mode = "APPLY" if apply_changes else "DRY-RUN"
        self.stdout.write(f"PROCESSING WORK EVENT BACKFILL: {mode}")
        self.stdout.write(f"orders_scanned={len(entries_by_order)}")
        self.stdout.write(f"assignment_candidates={len(assignment_candidates)}")
        self.stdout.write(
            "box_candidates="
            + str(sum(len(boxes) for _order, _entry, boxes, _occurred in box_candidates))
        )
        self.stdout.write(f"ownerless_boxes_skipped={ownerless_boxes}")
        self.stdout.write(f"already_present={len(existing_keys)}")
        if apply_changes:
            self.stdout.write(f"assignments_created={created_assignments}")
            self.stdout.write(f"boxes_created={created_boxes}")
            self.stdout.write(
                self.style.SUCCESS(
                    f"Создано событий: {created_assignments + created_boxes}."
                )
            )
        else:
            self.stdout.write("Данные не изменялись. Для записи используйте --apply.")
