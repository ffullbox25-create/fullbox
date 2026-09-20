from __future__ import annotations

from fbs.models import FbsPickScanEvent, FbsPickTask


def record_pick_scan_event(
    *,
    batch_id: int,
    stage: str,
    result: str,
    scan_value: str = "",
    expected_value: str = "",
    task_id: int | None = None,
    allocation_id: int | None = None,
    quantity_after: int | None = None,
    message: str = "",
    created_by=None,
) -> FbsPickScanEvent:
    actor = created_by if getattr(created_by, "is_authenticated", False) else None
    event = FbsPickScanEvent(
        batch_id=batch_id,
        task_id=task_id,
        allocation_id=allocation_id,
        stage=stage,
        result=result,
        scan_value=str(scan_value or "").strip(),
        expected_value=str(expected_value or "").strip(),
        quantity_after=quantity_after,
        message=str(message or "").strip(),
        created_by=actor,
    )
    event.full_clean()
    event.save()
    return event


def record_order_label_scan_events(
    *,
    order_id: int,
    scan_value: str,
    expected_value: str,
    result: str,
    message: str,
    created_by=None,
) -> tuple[FbsPickScanEvent, ...]:
    task_rows = FbsPickTask.objects.filter(order_id=order_id).values_list("batch_id", "id")
    return tuple(
        record_pick_scan_event(
            batch_id=batch_id,
            task_id=task_id,
            stage=FbsPickScanEvent.STAGE_ORDER_LABEL,
            result=result,
            scan_value=scan_value,
            expected_value=expected_value,
            message=message,
            created_by=created_by,
        )
        for batch_id, task_id in task_rows
    )
