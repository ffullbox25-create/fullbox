"""Durable, idempotent acceptance outside the HTTP worker's 30-second limit.

The existing atomic warehouse service is the only writer of stock/reserves.
Audit rows store job intent/result; PostgreSQL session locks prevent two workers
executing one request. A stopped worker can be resumed by the same POST.
"""
import logging
import subprocess
import sys
from pathlib import Path

from django.contrib.auth import get_user_model
from django.db import connection, transaction
from django.utils import timezone
from audit.models import OrderAuditEntry
from fbs.models import FbsClientMovementRequest
from fbs.exceptions import FbsError

logger = logging.getLogger(__name__)
JOB_TYPE = "fbs_acceptance_job"
LOCK_NAMESPACE = 1909049


def latest_job(request_id):
    return OrderAuditEntry.objects.filter(order_type=JOB_TYPE, order_id=str(request_id)).order_by('-id').first()


def worker_active(request_id):
    if connection.vendor != 'postgresql':
        return False
    with connection.cursor() as cursor:
        cursor.execute("SELECT EXISTS(SELECT 1 FROM pg_locks WHERE locktype='advisory' AND classid=%s AND objid=%s AND objsubid=2 AND granted AND database=(SELECT oid FROM pg_database WHERE datname=current_database()))", [LOCK_NAMESPACE, int(request_id)])
        return bool(cursor.fetchone()[0])


def job_context(request_id):
    job = latest_job(request_id)
    if not job:
        return None
    payload = job.payload or {}
    pending = payload.get('state') in {'queued', 'running'}
    active = pending and worker_active(request_id)
    return {'id': job.pk, 'state': payload.get('state'), 'pending': pending,
            'active': active, 'error': payload.get('error', '')}


def _save_state(job, state, error=''):
    job.payload = {**job.payload, 'state': state, 'error': error,
                   'state_changed_at': timezone.now().isoformat()}
    job.description = {'queued': 'Принятие FBS ожидает запуска', 'running': 'Выполняется принятие FBS',
                       'done': 'Принятие FBS завершено', 'failed': 'Принятие FBS не выполнено'}[state]
    job.save(update_fields=['payload', 'description'])


def launch_job(job_id):
    manage = Path(__file__).resolve().parents[2] / 'manage.py'
    try:
        subprocess.Popen([sys.executable, str(manage), 'accept_fbs_movement_job', str(int(job_id))],
            cwd=str(manage.parent), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)
    except OSError:
        # Intent remains durable. The page offers the same safe launch again.
        logger.exception('Could not launch FBS acceptance job %s', job_id)


@transaction.atomic
def enqueue_acceptance(*, request_row, actor, prepared_box_count=None, allow_item_fallback=False):
    from .client_movements import _require_actor_role, WAREHOUSE_ROLES
    _require_actor_role(actor, WAREHOUSE_ROLES, 'Принять FBS-перемещение может только сотрудник склада.')
    # Separate from the worker lock: never wait on the long warehouse transaction.
    if connection.vendor == 'postgresql':
        with connection.cursor() as cursor:
            cursor.execute('SELECT pg_advisory_xact_lock(%s,%s)', [LOCK_NAMESPACE + 1, request_row.pk])
    job = latest_job(request_row.pk)
    if job and (job.payload or {}).get('state') in {'queued', 'running'}:
        transaction.on_commit(lambda: launch_job(job.pk))
        return job
    job = OrderAuditEntry.objects.create(order_type=JOB_TYPE, order_id=str(request_row.pk),
        agency_id=request_row.agency_id, user=actor, action='status',
        description='Принятие FBS ожидает запуска', payload={
            'state': 'queued', 'request_id': request_row.pk, 'actor_id': actor.pk,
            'prepared_box_count': prepared_box_count, 'allow_item_fallback': bool(allow_item_fallback),
            'state_changed_at': timezone.now().isoformat()})
    transaction.on_commit(lambda: launch_job(job.pk))
    return job


def run_job(job_id):
    """Only the detached command calls this, never the HTTP request."""
    job = OrderAuditEntry.objects.get(pk=job_id, order_type=JOB_TYPE)
    request_id = int(job.order_id)
    if connection.vendor != 'postgresql':
        raise RuntimeError('FBS background acceptance requires PostgreSQL advisory locks')
    with connection.cursor() as cursor:
        cursor.execute('SELECT pg_try_advisory_lock(%s,%s)', [LOCK_NAMESPACE, request_id])
        acquired = cursor.fetchone()[0]
    if not acquired:
        return
    try:
        job.refresh_from_db()
        if job.payload.get('state') not in {'queued', 'running'}:
            return
        _execute_job(job)
    finally:
        with connection.cursor() as cursor:
            cursor.execute('SELECT pg_advisory_unlock(%s,%s)', [LOCK_NAMESPACE, request_id])


def _execute_job(job):
    from .client_movements import accept_client_movement_request
    payload = job.payload
    _save_state(job, 'running')
    try:
        actor = get_user_model().objects.get(pk=payload['actor_id'], is_active=True)
        request_row = FbsClientMovementRequest.objects.get(pk=int(job.order_id), agency_id=job.agency_id)
        if request_row.status not in {FbsClientMovementRequest.STATUS_COMPLETED,
                                     FbsClientMovementRequest.STATUS_AWAITING_MANAGER_CONFIRMATION}:
            # Existing service rechecks roles, state, source, quantity and reserves;
            # its transaction rolls back the entire operation if anything fails.
            accept_client_movement_request(request_id=request_row.pk, accepted_by=actor,
                prepared_box_count=payload.get('prepared_box_count'),
                allow_item_fallback=payload.get('allow_item_fallback', False))
        _save_state(job, 'done')
    except (FbsError, TypeError, ValueError) as exc:
        _save_state(job, 'failed', str(exc)[:1500])
    except Exception:
        logger.exception('FBS acceptance job %s failed', job.pk)
        _save_state(job, 'failed', 'Принятие не завершилось. Обновите страницу и проверьте состояние заявки перед повторным запуском. Сообщите менеджеру номер заявки.')
