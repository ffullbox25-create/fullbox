from unittest import mock
from types import SimpleNamespace
from django.test import TestCase, override_settings
from . import test_handover_composition_verification as fixtures
from .models import FbsControllerToteOrder as Row, FbsHandoverBatch as Batch, FbsOrder, FbsHandoverOrderAssignment
from .services.totes import _materialize_primary_control, auto_finalize_trusted_wb_check_tote, auto_finalize_verified_ozon_check_tote, auto_finalize_verified_ozon_handovers, controller_skips_repeat_wb_label_scan
from .services.handover import dispatch_handover_batch
from .services.marketplace import _apply_success
from .integrations.contracts import WB_DELIVER_HANDOVER
from .integrations.http import MarketplaceHttpResponse
from .exceptions import FbsHandoverError


@override_settings(FBS_MODULE_ENABLED=True, FBS_WAREHOUSE_WRITES_ENABLED=True, FBS_OUTBOX_ENABLED=True)
class PrimaryControlReadyTests(TestCase):
    setUp = fixtures.HandoverCompositionVerificationTests.setUp
    _linked_order = fixtures.HandoverCompositionVerificationTests._linked_order
    _controller_tote_order = fixtures.HandoverCompositionVerificationTests._controller_tote_order
    _ready_batch_for_supply_label_dispatch = fixtures.HandoverCompositionVerificationTests._ready_batch_for_supply_label_dispatch

    def prepare(self):
        self.box.external_box_id = 'external-box'
        self.box.label_file.name = 'box.pdf'
        self.box.save()
        return self._controller_tote_order(status=Row.STATUS_LABELED)

    def test_primary_policy_for_controller_without_individual_flag(self):
        self.assertTrue(controller_skips_repeat_wb_label_scan(self.controller))
        self.assertFalse(controller_skips_repeat_wb_label_scan(None))

    def test_wb_primary_becomes_ready_once_without_dispatch(self):
        check, row = self.prepare()
        with mock.patch('fbs.services.handover.request_wb_handover_delivery'), mock.patch('fbs.services.handover.dispatch_handover_batch') as dispatch:
            auto_finalize_trusted_wb_check_tote(check_tote_id=check.pk)
            auto_finalize_trusted_wb_check_tote(check_tote_id=check.pk)
        row.refresh_from_db(); check.refresh_from_db(); self.batch.refresh_from_db()
        self.assertEqual(self.batch.status, Batch.STATUS_READY)
        self.assertIsNone(self.batch.dispatched_at)
        self.assertEqual(check.composition_qty, 1)
        self.assertTrue(row.primary_order_label_scan_reused)
        self.assertEqual(row.composition_checked_at, row.label_confirmed_at)
        dispatch.assert_not_called()

    def test_ozon_primary_is_dispatched_after_verified_control(self):
        self.profile.marketplace = 'ozon'; self.profile.save()
        check, row = self.prepare()
        result = auto_finalize_verified_ozon_check_tote(check_tote_id=check.pk)
        self.batch.refresh_from_db(); row.refresh_from_db(); check.refresh_from_db()
        self.assertEqual(result.id, self.batch.id)
        self.assertEqual(self.batch.status, Batch.STATUS_DISPATCHED)
        self.assertTrue(row.primary_order_label_scan_reused)
        self.assertIsNotNone(self.batch.dispatched_at)
        self.assertEqual(check.status, check.STATUS_CLOSED)

    def test_closed_legacy_ozon_check_is_not_backfilled(self):
        self.profile.marketplace = 'ozon'; self.profile.save()
        check, _row = self.prepare()
        check.status = check.STATUS_CLOSED
        check.closed_by = self.controller
        from django.utils import timezone
        check.closed_at = timezone.now()
        check.save(update_fields=['status', 'closed_by', 'closed_at', 'updated_at'])
        self.batch.status = Batch.STATUS_READY
        self.batch.save(update_fields=['status', 'updated_at'])

        self.assertEqual(auto_finalize_verified_ozon_handovers(), ())
        self.batch.refresh_from_db()
        self.assertEqual(self.batch.status, Batch.STATUS_READY)
        self.assertIsNone(self.batch.dispatched_at)

    def test_ozon_cancelled_order_blocks_auto_dispatch(self):
        self.profile.marketplace = 'ozon'; self.profile.save()
        check, _row = self.prepare()
        self.order.marketplace_status = 'cancelled'
        self.order.save(update_fields=['marketplace_status', 'updated_at'])

        with self.assertRaisesMessage(FbsHandoverError, 'отменен маркетплейсом'):
            auto_finalize_verified_ozon_check_tote(check_tote_id=check.pk)

        self.batch.refresh_from_db(); check.refresh_from_db()
        self.assertEqual(self.batch.status, Batch.STATUS_OPEN)
        self.assertNotEqual(check.status, check.STATUS_CLOSED)

    def test_wb_marketplace_confirmation_dispatches_marked_controller_flow(self):
        check, _row = self.prepare()
        auto_finalize_trusted_wb_check_tote(check_tote_id=check.pk)
        command = self.batch.marketplace_commands.get(
            command_type=WB_DELIVER_HANDOVER,
        )
        self.assertEqual(
            command.payload['context']['controller_auto_dispatch_check_tote_id'],
            check.id,
        )

        _apply_success(
            command.id,
            MarketplaceHttpResponse(
                status_code=200,
                headers={},
                content=b'',
                json_payload={'done': True},
            ),
        )

        self.batch.refresh_from_db()
        self.assertEqual(self.batch.status, Batch.STATUS_DISPATCHED)
        self.assertIsNotNone(self.batch.dispatched_at)

    def test_metadata_block_prevents_materialization(self):
        check, row = self.prepare()
        with mock.patch('fbs.services.totes.check_tote_readiness', return_value=SimpleNamespace(ready=False)):
            self.assertFalse(_materialize_primary_control(check))
        row.refresh_from_db(); self.assertEqual(row.status, Row.STATUS_LABELED)

    def test_unchecked_order_not_materialized(self):
        check, row = self.prepare()
        self.order.internal_status = FbsOrder.STATUS_PICKED; self.order.save()
        self.assertFalse(_materialize_primary_control(check))
        row.refresh_from_db(); self.assertEqual(row.status, Row.STATUS_LABELED)

    def test_unconfirmed_assignment_not_materialized(self):
        check, row = self.prepare()
        FbsHandoverOrderAssignment.objects.filter(batch=self.batch).update(status='pending')
        self.assertFalse(_materialize_primary_control(check))

    def test_auto_dispatch_requires_closed_controller_evidence(self):
        self.profile.marketplace = 'ozon'; self.profile.save()
        self._ready_batch_for_supply_label_dispatch()
        with self.assertRaisesMessage(FbsHandoverError, 'тару контроля'):
            dispatch_handover_batch(
                batch_id=self.batch.pk,
                dispatched_by=self.controller,
                verified_controller_auto=True,
            )
