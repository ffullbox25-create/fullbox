from unittest import mock
from types import SimpleNamespace

from django.test import SimpleTestCase, TestCase, override_settings

from . import test_handover_composition_verification as fixtures
from .models import FbsControllerToteOrder, FbsIntegrationProfile
from .exceptions import FbsHandoverError
from .services.totes import (
    _pack_check_tote_order,
    confirm_order_label_to_check_tote,
    auto_finalize_verified_ozon_check_tote,
    auto_finalize_trusted_wb_check_tote,
)
from .services.handover import dispatch_handover_batch
from django.utils import timezone


class PrimaryReuseGuardTests(SimpleTestCase):
    def test_background_cannot_manufacture_composition_scan(self):
        with self.assertRaisesMessage(FbsHandoverError, 'повторная проверка'):
            _pack_check_tote_order(check_tote=None, tote_order=None,
                label_scan='saved-barcode', actor=None, primary_scan_reused=True)


@override_settings(FBS_MODULE_ENABLED=True, FBS_WAREHOUSE_WRITES_ENABLED=True, FBS_OUTBOX_ENABLED=True)
class RepeatControlRegressionTests(TestCase):
    setUp = fixtures.HandoverCompositionVerificationTests.setUp
    _linked_order = fixtures.HandoverCompositionVerificationTests._linked_order
    _controller_tote_order = fixtures.HandoverCompositionVerificationTests._controller_tote_order
    _processing_pick_tote_order = fixtures.HandoverCompositionVerificationTests._processing_pick_tote_order

    def test_ozon_primary_scan_does_not_pack_existing_order(self):
        self.profile.marketplace = FbsIntegrationProfile.MARKETPLACE_OZON
        self.profile.save(update_fields=['marketplace'])
        check, row, pick = self._processing_pick_tote_order()
        with mock.patch('fbs.services.handover.ensure_order_handover_assignment'), mock.patch(
            'fbs.services.totes.refresh_check_tote_status'
        ):
            result = confirm_order_label_to_check_tote(
                label_id=self.label.pk, label_scan=self.label.barcode,
                pick_batch_id=pick.pick_batch_id, performed_by=self.controller)
        self.assertEqual(result.status, FbsControllerToteOrder.STATUS_LABELED)
        self.assertIsNone(result.composition_checked_at)
        check.refresh_from_db()
        self.assertEqual(check.composition_qty, 0)

    def test_legacy_reused_wb_scan_is_not_finalized(self):
        check, row = self._controller_tote_order(status=FbsControllerToteOrder.STATUS_PACKED)
        row.primary_order_label_scan_reused = True
        row.save(update_fields=['primary_order_label_scan_reused'])
        self.assertIsNone(auto_finalize_trusted_wb_check_tote(check_tote_id=check.id))
        check.refresh_from_db()
        self.assertIsNone(check.closed_at)

    def test_ozon_background_does_not_dispatch(self):
        self.profile.marketplace = FbsIntegrationProfile.MARKETPLACE_OZON
        self.profile.save(update_fields=['marketplace'])
        check, row = self._controller_tote_order(status=FbsControllerToteOrder.STATUS_LABELED)
        with mock.patch('fbs.services.totes._materialize_primary_control', return_value=False), mock.patch('fbs.services.totes.check_tote_readiness', return_value=SimpleNamespace(ready=True)), mock.patch(
            'fbs.services.handover.dispatch_handover_batch'
        ) as dispatch:
            self.assertIsNone(auto_finalize_verified_ozon_check_tote(check_tote_id=check.id))
        dispatch.assert_not_called()
        self.batch.refresh_from_db()
        self.assertIsNone(self.batch.dispatched_at)

    def test_fully_rechecked_ozon_can_be_prepared_without_dispatch(self):
        self.profile.marketplace = FbsIntegrationProfile.MARKETPLACE_OZON
        self.profile.save(update_fields=['marketplace'])
        check, row = self._controller_tote_order(status=FbsControllerToteOrder.STATUS_PACKED)
        row.composition_checked_at = timezone.now()
        row.save(update_fields=['composition_checked_at'])
        self.link.verified_label = self.label
        self.link.verified_by = self.controller
        self.link.verified_at = timezone.now()
        self.link.save()
        with mock.patch('fbs.services.totes._materialize_primary_control', return_value=True), mock.patch('fbs.services.totes.check_tote_readiness', return_value=SimpleNamespace(ready=True)), mock.patch(
            'fbs.services.totes._close_controller_check_tote_locked'
        ) as close, mock.patch('fbs.services.handover.dispatch_handover_batch') as dispatch:
            auto_finalize_verified_ozon_check_tote(check_tote_id=check.id)
        close.assert_called_once()
        dispatch.assert_not_called()

    def test_explicit_repeat_scan_clears_legacy_flag_without_double_count(self):
        check, row = self._controller_tote_order(status=FbsControllerToteOrder.STATUS_PACKED, composition_qty=1)
        row.primary_order_label_scan_reused = True
        row.save(update_fields=['primary_order_label_scan_reused'])
        with mock.patch('fbs.services.handover.verify_handover_order_label', return_value=self.link):
            result = _pack_check_tote_order(check_tote=check, tote_order=row,
                label_scan=self.label.barcode, actor=self.controller)
        self.assertFalse(result.primary_order_label_scan_reused)
        self.assertIsNotNone(result.composition_checked_at)
        check.refresh_from_db()
        self.assertEqual(check.composition_qty, 1)

    def test_direct_auto_dispatch_cannot_bypass_controller(self):
        with self.assertRaisesMessage(FbsHandoverError, 'Автоматическая передача'):
            dispatch_handover_batch(batch_id=self.batch.id,
                dispatched_by=self.controller, verified_ozon_auto=True)

    def test_primary_composition_does_not_bypass_ready_status(self):
        check, row = self._controller_tote_order(status=FbsControllerToteOrder.STATUS_PACKED)
        row.primary_order_label_scan_reused = True
        row.save(update_fields=['primary_order_label_scan_reused'])
        with self.assertRaisesMessage(FbsHandoverError, 'Не все короба'):
            dispatch_handover_batch(batch_id=self.batch.id, dispatched_by=self.controller)
