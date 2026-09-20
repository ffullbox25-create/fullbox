from django.test import TestCase, override_settings
from . import test_handover_composition_verification as fixtures
from .models import FbsHandoverBatch as Batch, FbsOrder
from .services.handover import dispatch_handover_batch, refresh_handover_acceptance
from .exceptions import FbsHandoverError
from processing_app.models import ProcessingPrintJob as Job


@override_settings(FBS_MODULE_ENABLED=True, FBS_WAREHOUSE_WRITES_ENABLED=True, FBS_OUTBOX_ENABLED=True)
class PrintTransitAcceptanceTests(TestCase):
    setUp = fixtures.HandoverCompositionVerificationTests.setUp
    _linked_order = fixtures.HandoverCompositionVerificationTests._linked_order
    _ready_batch_for_supply_label_dispatch = fixtures.HandoverCompositionVerificationTests._ready_batch_for_supply_label_dispatch
    _supply_label_print_job = fixtures.HandoverCompositionVerificationTests._supply_label_print_job

    def test_ozon_ready_next_action_is_print(self):
        from .tsd_views import _handover_next_action
        self.profile.marketplace = 'ozon'; self.profile.save()
        self._ready_batch_for_supply_label_dispatch()
        action = _handover_next_action(self.batch, boxes=[], assignment_count=1, missing_order_count=0, all_assignments_confirmed=True, all_orders_ready=True, composition_ready=True)
        self.assertEqual(action.code, 'print_supply_label')
        self.assertNotIn('перейдет в статус', action.description)

    def test_ozon_cannot_dispatch_without_print(self):
        self.profile.marketplace = 'ozon'; self.profile.save()
        self._ready_batch_for_supply_label_dispatch()
        with self.assertRaisesMessage(FbsHandoverError, 'Сначала распечатайте'):
            dispatch_handover_batch(batch_id=self.batch.pk, dispatched_by=self.controller)
        self.batch.refresh_from_db()
        self.assertEqual(self.batch.status, Batch.STATUS_READY)

    def test_ozon_pending_print_is_not_transit(self):
        self.profile.marketplace = 'ozon'; self.profile.save()
        self._ready_batch_for_supply_label_dispatch()
        for state in (Job.STATUS_PENDING, Job.STATUS_PRINTING, Job.STATUS_FAILED):
            job = self._supply_label_print_job(status=state)
            with self.assertRaisesMessage(FbsHandoverError, 'Принтер еще не подтвердил'):
                dispatch_handover_batch(batch_id=self.batch.pk, dispatched_by=self.controller, supply_label_print_job_id=job.pk)
        self.batch.refresh_from_db(); self.assertIsNone(self.batch.dispatched_at)

    def test_ozon_print_is_transit_until_marketplace_acceptance(self):
        self.profile.marketplace = 'ozon'; self.profile.save()
        self._ready_batch_for_supply_label_dispatch()
        job = self._supply_label_print_job(status=Job.STATUS_PRINTED)
        dispatch_handover_batch(batch_id=self.batch.pk, dispatched_by=self.controller, supply_label_print_job_id=job.pk)
        self.batch.refresh_from_db(); first = self.batch.dispatched_at
        self.assertEqual(self.batch.status, Batch.STATUS_DISPATCHED)
        self.assertIsNone(self.batch.accepted_at)
        dispatch_handover_batch(batch_id=self.batch.pk, dispatched_by=self.controller, supply_label_print_job_id=job.pk)
        self.batch.refresh_from_db(); self.assertEqual(self.batch.dispatched_at, first)
        refresh_handover_acceptance(batch_id=self.batch.pk)
        self.batch.refresh_from_db(); self.assertEqual(self.batch.status, Batch.STATUS_DISPATCHED)
        self.order.marketplace_status = 'delivering'; self.order.save()
        refresh_handover_acceptance(batch_id=self.batch.pk)
        self.batch.refresh_from_db(); self.assertEqual(self.batch.status, Batch.STATUS_ACCEPTED)
        self.assertIsNotNone(self.batch.accepted_at)

    def test_wb_prepared_supply_not_accepted_until_scan_fact(self):
        self._ready_batch_for_supply_label_dispatch()
        job = self._supply_label_print_job(status=Job.STATUS_PRINTED)
        dispatch_handover_batch(batch_id=self.batch.pk, dispatched_by=self.controller, supply_label_print_job_id=job.pk)
        refresh_handover_acceptance(batch_id=self.batch.pk)
        self.batch.refresh_from_db(); self.assertEqual(self.batch.status, Batch.STATUS_DISPATCHED)
        self.batch.marketplace_payload = {'scanDt':'2026-09-10T17:00:00Z'}
        self.batch.save()
        refresh_handover_acceptance(batch_id=self.batch.pk)
        self.batch.refresh_from_db(); self.assertEqual(self.batch.status, Batch.STATUS_ACCEPTED)
