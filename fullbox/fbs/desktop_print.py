"""Authenticated direct-print queue used only by Fullbox Desktop.

Browser workstations keep using the existing Fullbox Agent endpoints.  Desktop
jobs have an explicit ``desktop:<workstation>`` target, so neither transport can
claim the other transport's work.
"""

import hashlib
import json
import re
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

from django.db import connection, transaction
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from processing_app.models import ProcessingPrintJob
from agent.desktop_auth import authenticate_desktop_request, desktop_auth_error_response

from .desktop_presence import record_desktop_presence


_POLL_LOCK_NAMESPACE = 0x464253
_EMPTY_POLL_RETRY_MS = 3000
_REAUTH_POLL_RETRY_MS = 60000
# Acknowledgement deadline for one handed-out Desktop print call.  A single
# order label is confirmed in about a second, so a short base keeps a lost
# label off the floor; the per-label part covers legitimately long batches.
_PRINT_LEASE_BASE_SECONDS = 15
_PRINT_LEASE_PER_LABEL_SECONDS = 2
# A queue that nobody can print must stop reprinting itself eventually.
_MAX_PRINT_ATTEMPTS = 3
# An abandoned queue from an earlier shift must never suddenly print a stack
# of obsolete labels, so only recent work is returned to the queue.
_MAX_RECLAIM_AGE_SECONDS = 1800
# Supply-label confirmation also performs the physical handover transition and
# creates shipping billing facts.  Keep one Desktop acknowledgement comfortably
# below Gunicorn's request timeout even when every supply contains many orders.
_HANDOVER_SUPPLY_MAX_JOBS_PER_CALL = 4
_HANDOVER_SUPPLY_CARD_RE = re.compile(
    r"^fbs:handover-supply:(?P<batch_id>[1-9][0-9]*)(?::reprint:[A-Za-z0-9]+)?$"
)


def _workstation_id(request, data=None) -> str:
    data = data if isinstance(data, dict) else {}
    workstation_id = str(
        data.get("workstation_id")
        or request.GET.get("workstation_id")
        or request.POST.get("workstation_id")
        or ""
    ).strip()
    if not workstation_id or len(workstation_id) > 80:
        return ""
    return workstation_id if re.fullmatch(r"[A-Za-z0-9._-]+", workstation_id) else ""


def _desktop_access_required(request, workstation_id: str):
    authentication = authenticate_desktop_request(request, workstation_id)
    if not authentication.ok:
        return desktop_auth_error_response(authentication)
    if getattr(request.user, "is_authenticated", False):
        return None
    response = JsonResponse(
        {
            "ok": False,
            "error": "Сессия Fullbox Desktop завершена. Войдите в систему повторно.",
            "reauth_required": True,
            "retry_after_ms": _REAUTH_POLL_RETRY_MS,
        },
        status=401,
    )
    response["Retry-After"] = str(_REAUTH_POLL_RETRY_MS // 1000)
    return response


def _empty_queue_response(request):
    response = JsonResponse(
        {
            "ok": True,
            "has_job": False,
            "hasJob": False,
            "retry_after_ms": _EMPTY_POLL_RETRY_MS,
            "desktop_auth": str(getattr(request, "fullbox_desktop_auth_mode", "legacy") or "legacy"),
        }
    )
    response["Retry-After"] = str(_EMPTY_POLL_RETRY_MS // 1000)
    return response


def _handover_supply_batch_id(card_id: str) -> int | None:
    match = _HANDOVER_SUPPLY_CARD_RE.fullmatch(str(card_id or "").strip())
    return int(match.group("batch_id")) if match else None


def _is_hidden_label_renderer_poll(request) -> bool:
    """Keep the image-only label renderer away from the physical print queue.

    Fullbox Desktop currently exposes the direct-print bridge to every browser
    frame.  The hidden ``/labels/settings/?renderer=1`` iframe renders label
    images for its parent page and must never claim physical print jobs itself.
    """

    referer = str(request.headers.get("Referer") or "").strip()
    if not referer:
        return False
    try:
        parsed = urlsplit(referer)
    except ValueError:
        return False
    return (
        parsed.path.rstrip("/") == "/labels/settings"
        and parse_qs(parsed.query).get("renderer") == ["1"]
    )


def _try_workstation_poll_lock(target_agent: str) -> bool:
    """Allow only one concurrent queue poll for a Desktop workstation.

    PostgreSQL transaction advisory locks are non-blocking and need no schema
    change.  Other database backends are used only by local tests and keep the
    previous single-process behaviour.
    """

    if connection.vendor != "postgresql":
        return True
    lock_key = int.from_bytes(
        hashlib.blake2s(target_agent.encode("utf-8"), digest_size=4).digest(),
        byteorder="big",
        signed=True,
    )
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_try_advisory_xact_lock(%s, %s)",
            [_POLL_LOCK_NAMESPACE, lock_key],
        )
        row = cursor.fetchone()
    return bool(row and row[0])


def _print_lease_seconds(label_count: int) -> int:
    """Return the acknowledgement deadline for one Desktop print call.

    Desktop prints a whole batch with a single native call and confirms it as a
    whole, so the deadline has to grow with the number of labels: one label is
    acknowledged in about a second, eighty legitimately take minutes.
    """

    return _PRINT_LEASE_BASE_SECONDS + _PRINT_LEASE_PER_LABEL_SECONDS * max(
        1, int(label_count or 1)
    )


def _reclaim_expired_jobs(target_agent: str) -> None:
    """Return labels that Desktop took but never printed to the queue.

    A job is marked ``printing`` when it is handed out, but the poll that
    carries it lives in the page, not in the Desktop process.  The controller
    screen reloads right after the last scan of an order, and that reload kills
    the in-flight poll carrying the freshly queued label: the row stays
    ``printing`` for good and the label never reaches the printer.

    Reclaiming is safe because Desktop keeps its own on-disk completion store
    and flushes it at the start of every poll, so a label that really was
    printed is acknowledged within one poll interval.  A lease that runs out
    with no acknowledgement therefore means the label is genuinely missing.

    Jobs claimed before leases existed keep a ``NULL`` lease and are left
    alone, together with anything older than ``_MAX_RECLAIM_AGE_SECONDS``:
    reprinting a stale queue would put obsolete labels on live orders.
    """

    now = timezone.now()
    ProcessingPrintJob.objects.filter(
        agent=target_agent,
        status=ProcessingPrintJob.STATUS_PRINTING,
        lease_until__isnull=False,
        lease_until__lte=now,
        attempt_count__lt=_MAX_PRINT_ATTEMPTS,
        updated_at__gte=now - timedelta(seconds=_MAX_RECLAIM_AGE_SECONDS),
    ).update(
        status=ProcessingPrintJob.STATUS_PENDING,
        claimed_at=None,
        lease_until=None,
        updated_at=now,
    )


@require_GET
def jobs_next(request):
    workstation_id = _workstation_id(request)
    if not workstation_id:
        return JsonResponse({"ok": False, "error": "Не определён компьютер Fullbox Desktop."}, status=400)
    denied = _desktop_access_required(request, workstation_id)
    if denied is not None:
        return denied
    if _is_hidden_label_renderer_poll(request):
        return _empty_queue_response(request)
    record_desktop_presence(request, workstation_id)
    target_agent = f"desktop:{workstation_id}"

    _reclaim_expired_jobs(target_agent)

    if not ProcessingPrintJob.objects.filter(
        agent=target_agent,
        status=ProcessingPrintJob.STATUS_PENDING,
    ).exists():
        return _empty_queue_response(request)

    with transaction.atomic():
        # A second window on the same computer returns immediately instead of
        # waiting for the first window and occupying a sync Gunicorn worker.
        if not _try_workstation_poll_lock(target_agent):
            return _empty_queue_response(request)

        pending = ProcessingPrintJob.objects.select_for_update().filter(
            status=ProcessingPrintJob.STATUS_PENDING,
            agent=target_agent,
        )
        job = pending.order_by("created_at", "id").first()
        if job is None:
            return _empty_queue_response(request)
        from processing_app.services import ProcessingWorkflowService
        from processing_app.web_ui import _serialize_print_job

        handover_supply_job = _handover_supply_batch_id(job.card_id) is not None
        compatible_pending = pending.exclude(pk=job.pk).filter(
            printer_name=job.printer_name,
            label_width_mm=job.label_width_mm,
            label_height_mm=job.label_height_mm,
        )
        if handover_supply_job:
            compatible_pending = compatible_pending.filter(
                card_id__startswith="fbs:handover-supply:"
            )
            additional_job_limit = _HANDOVER_SUPPLY_MAX_JOBS_PER_CALL - 1
        else:
            compatible_pending = compatible_pending.exclude(
                card_id__startswith="fbs:handover-supply:"
            )
            additional_job_limit = 79
        batch_jobs = [job]
        batch_jobs.extend(
            list(
                compatible_pending.order_by("created_at", "id")[:additional_job_limit]
            )
        )
        label_payloads = []
        job_ids = []
        claimed_jobs = []
        for batch_job in batch_jobs:
            labels = ProcessingWorkflowService._print_job_label_payloads(batch_job)
            if not labels:
                continue
            label_payloads.extend(labels)
            job_ids.append(int(batch_job.id))
            claimed_jobs.append(batch_job)
        if not label_payloads or not job_ids:
            return _empty_queue_response(request)
        # The batch is printed by one native call and acknowledged as a whole,
        # so every job in it shares that call's deadline.
        claim_time = timezone.now()
        lease_until = claim_time + timedelta(
            seconds=_print_lease_seconds(len(label_payloads))
        )
        for batch_job in claimed_jobs:
            batch_job.status = ProcessingPrintJob.STATUS_PRINTING
            batch_job.claimed_at = claim_time
            batch_job.lease_until = lease_until
            batch_job.attempt_count = int(batch_job.attempt_count or 0) + 1
            # Keep all label payloads until Desktop confirms the print.  Without
            # them an expired multi-label job cannot be retried safely.
            batch_job.save(
                update_fields=[
                    "status",
                    "claimed_at",
                    "lease_until",
                    "attempt_count",
                    "updated_at",
                ]
            )
        payload = _serialize_print_job(job, label_list=label_payloads, job_ids=job_ids)
    return JsonResponse(
        {
            "ok": True,
            "has_job": True,
            "hasJob": True,
            "job": payload,
            "desktop_auth": str(getattr(request, "fullbox_desktop_auth_mode", "legacy") or "legacy"),
        }
    )


@require_POST
def jobs_presence(request):
    try:
        data = json.loads(request.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return JsonResponse({"ok": False, "error": "Некорректный JSON."}, status=400)
    if not isinstance(data, dict):
        return JsonResponse({"ok": False, "error": "Некорректный JSON."}, status=400)
    workstation_id = _workstation_id(request, data)
    if not workstation_id:
        return JsonResponse({"ok": False, "error": "Не определён компьютер Fullbox Desktop."}, status=400)
    denied = _desktop_access_required(request, workstation_id)
    if denied is not None:
        return denied
    if not isinstance(data.get("printers"), list):
        return JsonResponse({"ok": False, "error": "Не получен список принтеров."}, status=400)
    presence = record_desktop_presence(request, workstation_id, data)
    return JsonResponse(
        {
            "ok": True,
            "workstation_id": workstation_id,
            "printers": list((presence or {}).get("printers") or []),
            "preferred_printer": str((presence or {}).get("preferred_printer") or ""),
            "desktop_auth": str(getattr(request, "fullbox_desktop_auth_mode", "legacy") or "legacy"),
        }
    )


@require_POST
def jobs_complete(request):
    try:
        data = json.loads(request.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return JsonResponse({"ok": False, "error": "Некорректный JSON."}, status=400)
    if not isinstance(data, dict):
        return JsonResponse({"ok": False, "error": "Некорректный JSON."}, status=400)
    workstation_id = _workstation_id(request, data)
    if not workstation_id:
        return JsonResponse({"ok": False, "error": "Не определён компьютер Fullbox Desktop."}, status=400)
    denied = _desktop_access_required(request, workstation_id)
    if denied is not None:
        return denied
    raw_job_ids = data.get("job_ids")
    if not isinstance(raw_job_ids, list) or not raw_job_ids or len(raw_job_ids) > 500:
        return JsonResponse({"ok": False, "error": "Не указаны задания прямой печати."}, status=400)
    job_ids = []
    for raw_value in raw_job_ids:
        try:
            value = int(raw_value)
        except (TypeError, ValueError):
            continue
        if value > 0 and value not in job_ids:
            job_ids.append(value)
    if not job_ids:
        return JsonResponse({"ok": False, "error": "Не указаны задания прямой печати."}, status=400)
    status_value = str(data.get("status") or "").strip().lower()
    if status_value not in {ProcessingPrintJob.STATUS_PRINTED, ProcessingPrintJob.STATUS_FAILED}:
        return JsonResponse({"ok": False, "error": "Некорректный статус прямой печати."}, status=400)
    target_agent = f"desktop:{workstation_id}"
    from .exceptions import FbsHandoverError
    from .models import FbsHandoverBatch
    from .services.handover import dispatch_handover_batch
    from processing_app.services import ProcessingWorkflowService

    jobs = list(
        ProcessingPrintJob.objects.filter(id__in=job_ids, agent=target_agent)
        .order_by("id")
    )
    if len(jobs) != len(job_ids):
        return JsonResponse({"ok": False, "error": "Задание относится к другому компьютеру."}, status=403)
    allowed_current = {
        ProcessingPrintJob.STATUS_PENDING,
        ProcessingPrintJob.STATUS_PRINTING,
        status_value,
    }
    if any(job.status not in allowed_current for job in jobs):
        return JsonResponse({"ok": False, "error": "Статус задания уже изменён."}, status=409)
    error_text = str(data.get("error") or "").strip()[:1000]
    updated = 0
    handover_transition_errors = []
    for job_id in [job.id for job in jobs]:
        # Commit each printed supply independently.  If a later supply hits a
        # request timeout, Desktop retries the same acknowledgement and resumes
        # from the first still-uncommitted job instead of rolling back the
        # entire printed stack.
        with transaction.atomic():
            job = ProcessingPrintJob.objects.select_for_update().get(
                pk=job_id,
                agent=target_agent,
            )
            if job.status not in allowed_current:
                return JsonResponse(
                    {"ok": False, "error": "Статус задания уже изменён."},
                    status=409,
                )
            if job.status != status_value:
                result = ProcessingWorkflowService.processing_print_jobs_complete(
                    data={"job_id": job.id, "status": status_value, "error": error_text},
                )
                if result.http_status != 200 or not result.payload.get("ok"):
                    return JsonResponse(result.payload, status=result.http_status)
                updated += int(result.payload.get("updated") or 0)
            if status_value != ProcessingPrintJob.STATUS_PRINTED:
                continue
            handover_batch_id = _handover_supply_batch_id(job.card_id)
            if handover_batch_id is None:
                continue
            try:
                dispatch_handover_batch(
                    batch_id=handover_batch_id,
                    dispatched_by=request.user,
                    supply_label_print_job_id=job.id,
                )
            except (FbsHandoverError, FbsHandoverBatch.DoesNotExist) as exc:
                handover_transition_errors.append(
                    {"batch_id": handover_batch_id, "error": str(exc)}
                )
    payload = {
        "ok": True,
        "updated": updated,
        "job_ids": job_ids,
        "status": status_value,
        "desktop_auth": str(getattr(request, "fullbox_desktop_auth_mode", "legacy") or "legacy"),
    }
    if handover_transition_errors:
        payload["handover_transition_errors"] = handover_transition_errors
    return JsonResponse(payload)
