from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import TestCase, SimpleTestCase, override_settings

from sku.models import Agency
from . import test_handover_composition_verification as fixtures
from .controller_views import (
    _check_tote_metadata_poll_orders, _check_tote_metadata_status,
    _decorate_check_tote_metadata,
)
from .models import FbsIntegrationProfile, FbsMarketplaceCommand, FbsOrder, FbsOrderItem, FbsControllerToteOrder
from .services.marketplace import process_marketplace_queue, process_marketplace_command
from .services.sync import _orders_after_cursor, _advance_status_cursor
from .services.totes import check_tote_readiness


@override_settings(FBS_MODULE_ENABLED=True, FBS_WAREHOUSE_WRITES_ENABLED=True, FBS_OUTBOX_ENABLED=True)
class MetadataReadinessTests(TestCase):
    setUp = fixtures.HandoverCompositionVerificationTests.setUp
    _linked_order = fixtures.HandoverCompositionVerificationTests._linked_order
    _controller_tote_order = fixtures.HandoverCompositionVerificationTests._controller_tote_order

    def test_missing_required_transfer_blocks_both_page_and_poll(self):
        FbsOrderItem.objects.create(
            order=self.order, external_line_id='required', external_sku='required',
            quantity=1, requirements={'is_kiz': True},
        )
        tote, row = self._controller_tote_order(status=FbsControllerToteOrder.STATUS_LABELED)
        readiness = check_tote_readiness(tote)
        self.assertIn(self.order.id, readiness.metadata_blocked_order_ids)
        _decorate_check_tote_metadata(row, marketplace='wb', readiness=readiness)
        self.assertFalse(row.metadata_ok)
        self.assertIn('Нет подтверждения', row.metadata_label)
        with self.assertNumQueries(2):
            rows, _ = _check_tote_metadata_poll_orders(tote, readiness=readiness)
        self.assertFalse(rows[0]['ok'])
        self.assertEqual(rows[0]['state'], 'problem')
        self.assertEqual(rows[0]['label'], row.metadata_label)
        # Standalone callers cannot accidentally fall back to an empty-transfer decision.
        self.assertFalse(_check_tote_metadata_poll_orders(tote)[0][0]['ok'])

    def test_order_without_requirements_remains_ready(self):
        FbsOrderItem.objects.create(order=self.order, external_line_id='plain', quantity=1)
        tote, row = self._controller_tote_order(status=FbsControllerToteOrder.STATUS_LABELED)
        readiness = check_tote_readiness(tote)
        self.assertNotIn(self.order.id, readiness.metadata_blocked_order_ids)
        _decorate_check_tote_metadata(row, marketplace='wb', readiness=readiness)
        self.assertTrue(row.metadata_ok)
        self.assertEqual(row.metadata_label, 'Не требуется')

    def test_missing_other_requirement_is_not_hidden_by_confirmed_transfer(self):
        result = _check_tote_metadata_status(
            [SimpleNamespace(status='confirmed')], marketplace='ozon', metadata_blocked=True,
        )
        self.assertFalse(result['ok'])

    def test_local_resolution_does_not_claim_marketplace_confirmation(self):
        result = _check_tote_metadata_status(
            [SimpleNamespace(status='canceled')], marketplace='wb', metadata_blocked=False,
        )
        self.assertTrue(result['ok'])
        self.assertEqual(result['state'], 'resolved')
        self.assertNotIn('Подтверждено WB', result['label'])


@override_settings(FBS_MODULE_ENABLED=True, FBS_OUTBOX_ENABLED=True)
class QueueProfileIsolationTests(TestCase):
    def setUp(self):
        agency = Agency.objects.create(agn_name='queue audit')
        self.disabled = FbsIntegrationProfile.objects.create(agency=agency, marketplace='wb', name='disabled', external_account_id='disabled', is_active=False, outbox_enabled=True)
        self.active = FbsIntegrationProfile.objects.create(agency=agency, marketplace='wb', name='active', external_account_id='active', is_active=True, outbox_enabled=True)
        self.first = self.command(self.disabled, 'first')
        self.second = self.command(self.active, 'second')

    def command(self, profile, key):
        return FbsMarketplaceCommand.objects.create(profile=profile, command_type='test_read', endpoint='/test', idempotency_key=key, payload_hash=key)

    def complete(self, *, command_id, transport=None):
        command = FbsMarketplaceCommand.objects.get(id=command_id)
        command.status = 'confirmed'
        command.save(update_fields=['status'])
        return command

    def test_disabled_profile_does_not_starve_active_profile(self):
        with patch('fbs.services.marketplace.process_marketplace_command', side_effect=self.complete) as process:
            process_marketplace_queue(run_schedulers=False)
        self.assertEqual([c.kwargs['command_id'] for c in process.call_args_list], [self.second.id])
        self.first.refresh_from_db()
        self.assertEqual(self.first.status, 'pending')
        self.assertEqual(self.first.attempt_count, 0)

    def test_outbox_disabled_profile_is_also_skipped(self):
        self.disabled.is_active = True
        self.disabled.outbox_enabled = False
        self.disabled.save()
        with patch('fbs.services.marketplace.process_marketplace_command', side_effect=self.complete) as process:
            process_marketplace_queue(run_schedulers=False)
        self.assertEqual([c.kwargs['command_id'] for c in process.call_args_list], [self.second.id])

    def test_profile_disabled_after_selection_keeps_command_untouched(self):
        transport = Mock()
        result = process_marketplace_command(command_id=self.first.id, transport=transport)
        transport.send.assert_not_called()
        self.assertEqual(result.status, 'pending')
        self.assertEqual(result.attempt_count, 0)

    def test_reenabled_profile_resumes_pending_command(self):
        self.disabled.is_active = True
        self.disabled.save()
        with patch('fbs.services.marketplace.process_marketplace_command', side_effect=self.complete) as process:
            process_marketplace_queue(run_schedulers=False)
        self.assertEqual([c.kwargs['command_id'] for c in process.call_args_list], [self.first.id, self.second.id])

    def test_profile_disabled_between_selection_and_claim_does_not_stop_next(self):
        self.disabled.is_active = True
        self.disabled.save()
        def race(*, command_id, transport=None):
            if command_id == self.first.id:
                FbsIntegrationProfile.objects.filter(pk=self.disabled.pk).update(is_active=False)
                return process_marketplace_command(command_id=command_id, transport=Mock())
            return self.complete(command_id=command_id)
        with patch('fbs.services.marketplace.process_marketplace_command', side_effect=race):
            process_marketplace_queue(run_schedulers=False, limit=2)
        self.second.refresh_from_db()
        self.assertEqual(self.second.status, 'confirmed')


class StatusLaneTests(TestCase):
    def setUp(self):
        agency = Agency.objects.create(agn_name='status audit')
        self.profile = FbsIntegrationProfile.objects.create(agency=agency, marketplace='ozon')
        self.serial = 0

    def orders(self, status, count):
        result=[]
        for _ in range(count):
            self.serial += 1
            result.append(FbsOrder.objects.create(profile=self.profile, external_order_id=str(self.serial), internal_status=status))
        return result

    def advance(self, cursor, rows):
        for row in rows:
            _advance_status_cursor(cursor, row)
        cursor['status_poll_pass'] = int(cursor.get('status_poll_pass', 0)) + 1

    def test_live_orders_have_reserved_capacity_despite_old_history(self):
        self.orders('delivered', 100)
        self.orders('handed_over', 50)
        live=self.orders('reserved', 30)
        rows=_orders_after_cursor(self.profile, {}, 10)
        self.assertEqual(sum(r.internal_status=='reserved' for r in rows),7)
        self.assertEqual(sum(r.internal_status=='handed_over' for r in rows),2)
        self.assertEqual(sum(r.internal_status=='delivered' for r in rows),1)
        self.assertEqual(rows[:7],live[:7])

    def test_each_lane_wraps_without_duplicates_and_eventually_visits_every_order(self):
        all_rows=self.orders('reserved',19)+self.orders('handed_over',5)+self.orders('cancelled',3)
        cursor={};seen=set()
        for _ in range(5):
            rows=_orders_after_cursor(self.profile,cursor,10)
            self.assertEqual(len(rows),len({r.pk for r in rows}))
            seen.update(r.pk for r in rows)
            self.advance(cursor,rows)
        self.assertEqual(seen,{r.pk for r in all_rows})

    def test_small_batches_do_not_starve_history_or_transit(self):
        all_rows=self.orders('picked',1)+self.orders('handed_over',1)+self.orders('returned',1)
        cursor={};seen=set()
        for _ in range(10):
            rows=_orders_after_cursor(self.profile,cursor,1)
            self.assertEqual(len(rows),1)
            seen.add(rows[0].pk);self.advance(cursor,rows)
        self.assertEqual(seen,{r.pk for r in all_rows})

    def test_spare_capacity_is_used_and_another_profile_is_never_selected(self):
        all_rows=self.orders('reserved',15)
        other=FbsIntegrationProfile.objects.create(agency=self.profile.agency,marketplace='wb')
        FbsOrder.objects.create(profile=other,external_order_id='other')
        rows=_orders_after_cursor(self.profile,{},10)
        self.assertEqual(rows,all_rows[:10])


class ReadTransportConnectionTests(SimpleTestCase):
    def test_requests_reuse_one_session_and_close_it(self):
        from .integrations.http import RequestsMarketplaceReadTransport
        response=SimpleNamespace(status_code=200,headers={},content=b'',json=lambda:{})
        with patch('fbs.integrations.http.requests.Session') as session, patch('fbs.integrations.http._credentials_for',return_value={}), patch('fbs.integrations.http._headers_for',return_value={}):
            session.return_value.request.return_value=response
            transport=RequestsMarketplaceReadTransport(reuse_connections=True)
            profile=SimpleNamespace(marketplace='ozon')
            spec=SimpleNamespace(http_method='POST',endpoint='/test',query={},body={})
            transport.send(profile,spec);transport.send(profile,spec);transport.close()
            session.assert_called_once_with()
            self.assertEqual(session.return_value.request.call_count,2)
            session.return_value.close.assert_called_once_with()


@override_settings(FBS_MODULE_ENABLED=True, FBS_STATUS_PULL_ENABLED=True)
class StatusCursorFailureTests(TestCase):
    def setUp(self):
        agency=Agency.objects.create(agn_name='cursor failure audit')
        self.profile=FbsIntegrationProfile.objects.create(agency=agency,marketplace='ozon',is_active=True,status_pull_enabled=True)
        self.orders=[FbsOrder.objects.create(profile=self.profile,external_order_id=str(i),internal_status='reserved') for i in (1,2,3)]

    def test_failed_request_advances_only_successfully_processed_order(self):
        from .exceptions import FbsIntegrationError
        from .models import FbsSyncCursor
        from .services.sync import pull_profile_statuses
        response=SimpleNamespace(status_code=200,json_payload={})
        transport=Mock()
        transport.send.side_effect=[response,FbsIntegrationError('temporary failure')]
        with patch('fbs.services.sync.parse_ozon_posting_status',return_value={}), patch('fbs.services.sync._ingest_status',return_value='updated'):
            with self.assertRaises(FbsIntegrationError):
                pull_profile_statuses(profile_id=self.profile.id,transport=transport,limit=3)
        cursor=FbsSyncCursor.objects.get(profile=self.profile,stream='statuses')
        self.assertEqual(cursor.cursor['active_after_order_pk'],self.orders[0].pk)
        self.assertEqual(cursor.lease_token,'')
        self.assertIn('temporary failure',cursor.last_error)
        transport.close.assert_not_called()
        self.assertEqual(_orders_after_cursor(self.profile,cursor.cursor,3)[0].pk,self.orders[1].pk)

    def test_owned_transport_is_closed_even_on_failure(self):
        from .exceptions import FbsIntegrationError
        from .services.sync import pull_profile_statuses
        with patch('fbs.services.sync.RequestsMarketplaceReadTransport') as cls:
            cls.return_value.send.side_effect=FbsIntegrationError('temporary failure')
            with self.assertRaises(FbsIntegrationError):
                pull_profile_statuses(profile_id=self.profile.id,limit=3)
            cls.assert_called_once_with(reuse_connections=True)
            cls.return_value.close.assert_called_once_with()


class DesktopUpdateNoticeTests(SimpleTestCase):
    def test_only_old_desktop_versions_need_update(self):
        from django.test import RequestFactory
        from .templatetags.fbs_workspace import fbs_desktop_needs_update
        for agent, expected in (
            ("FullboxDesktop/1.0.17", True),
            ("FullboxDesktop/1.0.20", True),
            ("FullboxDesktop/1.0.21", False),
            ("FullboxDesktop/1.0.210", False),
            ("FullboxDesktop/1.1.0", False),
            ("Mozilla/5.0 Chrome/152.0.0", False),
        ):
            with self.subTest(agent=agent):
                request = RequestFactory().get("/fbs/", HTTP_USER_AGENT=agent)
                self.assertEqual(fbs_desktop_needs_update(request), expected)

    def test_notice_is_rendered_by_server_and_survives_partial_page_refresh(self):
        from django.template.loader import render_to_string
        from django.test import RequestFactory
        for version, expected in (("1.0.20", True), ("1.0.21", False)):
            request = RequestFactory().get("/fbs/", HTTP_USER_AGENT=f"FullboxDesktop/{version}")
            html = render_to_string("fbs/tsd_base.html", {"request": request})
            self.assertEqual('id="fbs-desktop-update"' in html, expected)
            if expected:
                self.assertIn('downloads/desktop/Fullbox-Desktop.exe', html)
