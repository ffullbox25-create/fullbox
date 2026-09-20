from unittest.mock import patch, Mock
from django.test import TestCase, override_settings
from django.db import transaction
from fbs.test_client_movement_execution import FbsClientMovementExecutionTests as Fixture
from fbs.models import FbsClientMovementRequest, FbsReplenishmentPlan, FbsStockMovement
from fbs.services import movement_acceptance_jobs as jobs
from fbs.client_portal import create_client_movement_request
from fbs.services.client_movements import approve_client_movement_by_manager
from fbs.exceptions import FbsReplenishmentError
from audit.models import OrderAuditEntry


@override_settings(ROOT_URLCONF='fullbox.urls', FBS_MODULE_ENABLED=True,
    FBS_WAREHOUSE_WRITES_ENABLED=True, FBS_ZONE_CODE='FBS',
    FBS_CLIENT_FULL_PALLET_LOGICAL_POST_ON_ACCEPT=True)
class AcceptanceJobTests(TestCase):
    setUp = Fixture.setUp
    _source_box = Fixture._source_box
    _snapshot = Fixture._snapshot

    def request_row(self, count=2):
        for n in range(count): self._snapshot(code=f'JOB-{n}', qty=5)
        row = create_client_movement_request(agency=self.agency, mode='box',
            raw_lines=[{'barcode':self.barcode,'qty':count*5,'units_per_box':5,'box_count':count}],
            idempotency_key='acceptance-job-test')
        approve_client_movement_by_manager(request_id=row.pk, reviewed_by=self.manager)
        return row

    def enqueue(self, row):
        return jobs.enqueue_acceptance(request_row=row, actor=self.storekeeper)

    def test_enqueue_does_not_execute_stock_work(self):
        row=self.request_row()
        with patch.object(jobs,'launch_job') as launch:
            with self.captureOnCommitCallbacks(execute=True): job=self.enqueue(row)
            launch.assert_called_once_with(job.pk)
        self.assertEqual(job.payload['state'],'queued')
        self.assertFalse(FbsReplenishmentPlan.objects.exists())
        self.assertFalse(FbsStockMovement.objects.exists())

    def test_repeat_uses_same_pending_job_and_original_actor(self):
        row=self.request_row()
        job=self.enqueue(row)
        again=self.enqueue(row)
        self.assertEqual(again.pk,job.pk)
        self.assertEqual(OrderAuditEntry.objects.filter(order_type=jobs.JOB_TYPE).count(),1)
        self.assertEqual(again.payload['actor_id'],self.storekeeper.pk)

    def test_pending_job_page_shows_safe_resume_and_no_normal_accept_button(self):
        row=self.request_row()
        self.enqueue(row)
        self.client.force_login(self.storekeeper)
        response=self.client.get(f'/fbs/operator/movements/{row.pk}/')
        self.assertContains(response,'Выполняется принятие заявки')
        self.assertContains(response,'Продолжить принятие')
        self.assertNotContains(response,'Учесть в FBS и передать ричтраку')

    def test_rollback_never_launches_worker(self):
        row=self.request_row()
        with patch.object(jobs,'launch_job') as launch:
            with self.captureOnCommitCallbacks(execute=True):
                with transaction.atomic():
                    self.enqueue(row)
                    transaction.set_rollback(True)
            launch.assert_not_called()
        self.assertIsNone(jobs.latest_job(row.pk))

    def test_worker_preserves_existing_atomic_service_and_retry_is_idempotent(self):
        row=self.request_row()
        job=self.enqueue(row)
        jobs._execute_job(job)
        row.refresh_from_db()
        self.assertEqual(row.status,FbsClientMovementRequest.STATUS_COMPLETED)
        self.assertEqual(row.actual_moved_qty,10)
        self.assertEqual(FbsStockMovement.objects.count(),2)
        job.refresh_from_db()
        self.assertEqual(job.payload['state'],'done')
        # Simulates process death after warehouse commit but before final job save.
        jobs._save_state(job,'running')
        jobs._execute_job(job)
        self.assertEqual(FbsStockMovement.objects.count(),2)

    def test_business_failure_is_readable(self):
        row=self.request_row()
        job=self.enqueue(row)
        with patch('fbs.services.client_movements.accept_client_movement_request',side_effect=FbsReplenishmentError('Короб занят')):
            jobs._execute_job(job)
        job.refresh_from_db()
        self.assertEqual(job.payload['state'],'failed')
        self.assertEqual(jobs.job_context(row.pk)['error'],'Короб занят')
        self.assertFalse(FbsStockMovement.objects.exists())

    def test_canceled_request_is_not_accepted(self):
        row=self.request_row()
        job=self.enqueue(row)
        row.status=FbsClientMovementRequest.STATUS_CANCELED
        row.save(update_fields=['status'])
        jobs._execute_job(job)
        job.refresh_from_db()
        self.assertEqual(job.payload['state'],'failed')
        self.assertFalse(FbsStockMovement.objects.exists())

    def test_worker_lock_refuses_parallel_execution(self):
        row=self.request_row()
        job=self.enqueue(row)
        fake=Mock(vendor='postgresql')
        cursor=Mock()
        cursor.fetchone.return_value=(False,)
        fake.cursor.return_value.__enter__=Mock(return_value=cursor)
        fake.cursor.return_value.__exit__=Mock(return_value=False)
        with patch.object(jobs,'connection',fake),patch.object(jobs,'_execute_job') as execute:
            jobs.run_job(job.pk)
            execute.assert_not_called()

    def test_105_box_http_post_queues_without_accepting(self):
        import time
        row=self.request_row(count=105)
        self.client.force_login(self.storekeeper)
        with patch.object(jobs,'launch_job'):
            started=time.monotonic()
            response=self.client.post(f'/fbs/operator/movements/{row.pk}/approve/')
            elapsed=time.monotonic()-started
        print('HTTP_ACCEPT_105_SECONDS',round(elapsed,3))
        self.assertEqual(response.status_code,302)
        self.assertIn('acceptance_job=1',response.url)
        self.assertIsNotNone(jobs.latest_job(row.pk))
        self.assertFalse(FbsReplenishmentPlan.objects.exists())
        self.assertFalse(FbsStockMovement.objects.exists())
        jobs._execute_job(jobs.latest_job(row.pk))
        row.refresh_from_db()
        self.assertEqual(row.status,FbsClientMovementRequest.STATUS_COMPLETED)
        self.assertEqual(row.actual_moved_qty,525)
        self.assertEqual(FbsStockMovement.objects.count(),105)

    def test_launch_failure_keeps_durable_intent(self):
        row=self.request_row()
        job=self.enqueue(row)
        with patch.object(jobs.subprocess,'Popen',side_effect=OSError('no process')):
            jobs.launch_job(job.pk)
        self.assertEqual(jobs.latest_job(row.pk).payload['state'],'queued')

    def test_worker_failure_rolls_back_partial_stock_changes(self):
        row=self.request_row()
        job=self.enqueue(row)
        from fbs.services import replenishment
        original=replenishment._complete_replenishment_allocation
        calls=[]
        def fail_second(**kwargs):
            calls.append(1)
            if len(calls)==2: raise FbsReplenishmentError('Второй короб изменился')
            return original(**kwargs)
        with patch.object(replenishment,'_complete_replenishment_allocation',side_effect=fail_second):
            jobs._execute_job(job)
        self.assertEqual(len(calls),2)
        self.assertFalse(FbsStockMovement.objects.exists())
        self.assertFalse(FbsReplenishmentPlan.objects.exists())
        row.refresh_from_db()
        self.assertEqual(row.status,FbsClientMovementRequest.STATUS_APPROVED)
