from datetime import datetime, timezone
from decimal import Decimal
from io import StringIO
import json
from types import SimpleNamespace
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase

from billing.charge_status import charge_has_agreed_price, charge_manager_status, charge_missing_price
from billing.readiness import preview_application_readiness
from billing.management.commands.preview_billing_readiness import Command


class ChargePriceGuardTests(SimpleTestCase):
    def charge(self, **overrides):
        values = dict(is_manual_override=False, client_tariff_version_id=None,
                      client_logistics_tariff_id=None, is_excluded=False,
                      is_disputed=False, service_changed_at=None, is_confirmed=False,
                      original_quantity=None, quantity=Decimal('2'),
                      is_included_in_invoice=False, is_included_in_act=False,
                      tariff=Decimal('10'))
        values.update(overrides)
        return SimpleNamespace(**values)

    def test_regular_version_is_accepted(self):
        self.assertTrue(charge_has_agreed_price(self.charge(client_tariff_version_id=1)))

    def test_logistics_version_is_accepted(self):
        self.assertTrue(charge_has_agreed_price(self.charge(client_logistics_tariff_id=2)))

    def test_logistics_is_not_missing_price(self):
        self.assertFalse(charge_missing_price(self.charge(client_logistics_tariff_id=2)))

    def test_logistics_ui_shows_review_not_no_price_or_recalc(self):
        self.assertEqual(charge_manager_status(self.charge(client_logistics_tariff_id=2))[0], 'review')

    def test_confirmed_logistics(self):
        self.assertEqual(charge_manager_status(self.charge(client_logistics_tariff_id=2, is_confirmed=True))[0], 'confirmed')

    def test_invoiced_logistics(self):
        self.assertEqual(charge_manager_status(self.charge(client_logistics_tariff_id=2, is_included_in_invoice=True))[0], 'in_invoice')

    def test_no_version_is_still_rejected(self):
        self.assertTrue(charge_missing_price(self.charge()))

    def test_override_is_preserved(self):
        self.assertTrue(charge_has_agreed_price(self.charge(is_manual_override=True)))

    def test_excluded_is_not_reported_missing_price(self):
        self.assertFalse(charge_missing_price(self.charge(is_excluded=True)))

    def test_dispute_precedes_price(self):
        self.assertEqual(charge_manager_status(self.charge(is_disputed=True))[0], 'disputed')


class ReadinessPreviewTests(SimpleTestCase):
    def setUp(self):
        self.app = SimpleNamespace(pk=12, client_id=7, application_id='PR-1', application_type='receiving')
        self.fact = SimpleNamespace(service_code='receiving_goods', quantity=Decimal('80'),
                                    performed_at=datetime(2026, 9, 1, tzinfo=timezone.utc), source_key='fact:1')
        self.resolved = SimpleNamespace(ok=True, tariff=Decimal('10'), unit='шт',
                                        tariff_version=SimpleNamespace(pk=4), logistics_tariff=None,
                                        reason='', note='')
        self.facts = self.enterContext(patch('billing.readiness.candidates_from_warehouse_facts', return_value=[self.fact]))
        self.unresolved = self.enterContext(patch('billing.readiness._unresolved_fact_count', return_value=0))
        self.services = self.enterContext(patch('billing.readiness.BillingService.objects'))
        self.services.filter.return_value.first.return_value = SimpleNamespace(unit='шт')
        self.resolver = self.enterContext(patch('billing.readiness.resolve_client_service_price', return_value=self.resolved))

    def preview(self):
        return preview_application_readiness(self.app)

    def test_preserves_actual_quantity_and_date(self):
        result = self.preview()
        self.assertEqual(result['status'], 'facts_and_tariffs_found')
        self.assertFalse(result['creates_charges'])
        self.assertEqual(result['rows'][0]['quantity'], '80')
        self.assertEqual(self.resolver.call_args.kwargs['performed_at'], self.fact.performed_at)
        self.assertEqual(self.resolver.call_args.kwargs['quantity'], Decimal('80'))
        self.services.get_or_create.assert_not_called()

    def test_missing_facts_never_uses_fallback(self):
        self.facts.return_value = None
        self.assertIn('warehouse_facts_missing', self.preview()['issues'])
        self.resolver.assert_not_called()

    def test_no_eligible_facts(self):
        self.facts.return_value = []
        self.assertIn('no_eligible_warehouse_facts', self.preview()['issues'])

    def test_pending_facts_prevent_ready(self):
        self.unresolved.return_value = 2
        result = self.preview()
        self.assertEqual(result['status'], 'needs_review')
        self.assertEqual(result['unresolved_fact_count'], 2)

    def test_bad_quantities_are_not_promoted_to_one(self):
        for quantity in ['0', '-2', 'NaN', 'Infinity', 'bad']:
            with self.subTest(quantity=quantity):
                self.fact.quantity = quantity
                self.assertIn('invalid_fact_quantity', self.preview()['rows'][0]['issues'])
        self.resolver.assert_not_called()

    def test_missing_execution_date_does_not_use_today(self):
        self.fact.performed_at = None
        self.assertIn('performed_at_missing', self.preview()['rows'][0]['issues'])
        self.resolver.assert_not_called()

    def test_missing_key(self):
        self.fact.source_key = ''
        self.assertIn('source_key_missing', self.preview()['rows'][0]['issues'])

    def test_duplicate_candidate_key(self):
        self.facts.return_value = [self.fact, self.fact]
        result = self.preview()
        self.assertIn('duplicate_source_key', result['rows'][1]['issues'])
        self.assertEqual(result['status'], 'needs_review')

    def test_missing_catalog_entry_is_not_created(self):
        self.services.filter.return_value.first.return_value = None
        self.assertIn('service_missing_or_inactive', self.preview()['rows'][0]['issues'])
        self.services.create.assert_not_called()

    def test_missing_tariff(self):
        self.resolved.ok = False
        self.assertIn('agreed_tariff_missing', self.preview()['rows'][0]['issues'])

    def test_unversioned_resolved_price_is_rejected(self):
        self.resolved.tariff_version = None
        self.assertIn('agreed_tariff_missing', self.preview()['rows'][0]['issues'])

    def test_logistics_version_is_reported(self):
        self.resolved.tariff_version = None
        self.resolved.logistics_tariff = SimpleNamespace(pk=9)
        self.assertEqual(self.preview()['rows'][0]['logistics_tariff_id'], 9)

    def test_zero_tariff_is_contract_review_not_missing_price(self):
        self.resolved.tariff = Decimal('0')
        issues = self.preview()['rows'][0]['issues']
        self.assertEqual(issues, ['zero_tariff_requires_contract_review'])

    def test_invalid_tariff(self):
        for tariff in ['-1', 'NaN', 'Infinity']:
            with self.subTest(tariff=tariff):
                self.resolved.tariff = tariff
                self.assertIn('invalid_tariff', self.preview()['rows'][0]['issues'])

    def test_fbs_and_storage_require_specialized_preview(self):
        for app_type in ['fbs', 'storage']:
            self.app.application_type = app_type
            self.assertIn('specialized_preview_required', self.preview()['issues'])
        self.facts.assert_not_called()


class PreviewCommandGuardTests(SimpleTestCase):
    def test_non_postgresql_is_rejected(self):
        with patch('billing.management.commands.preview_billing_readiness.connection') as connection:
            connection.vendor = 'sqlite'
            with self.assertRaises(CommandError):
                Command().handle(client=1, limit=1, application=None)
            connection.cursor.assert_not_called()

    def test_nested_transaction_is_rejected(self):
        with patch('billing.management.commands.preview_billing_readiness.connection') as connection:
            connection.vendor = 'postgresql'
            connection.in_atomic_block = True
            with self.assertRaises(CommandError):
                Command().handle(client=1, limit=1, application=None)
            connection.cursor.assert_not_called()

    def test_enforces_read_only_and_client_scoped_application_filter(self):
        target = 'billing.management.commands.preview_billing_readiness.'
        with patch(target + 'connection') as connection, patch(target + 'transaction.atomic'), \
             patch(target + 'BillingApplication.objects') as applications, \
             patch(target + 'preview_application_readiness', return_value={'status': 'needs_review'}) as preview:
            connection.vendor = 'postgresql'
            connection.in_atomic_block = False
            queryset = applications.filter.return_value.select_related.return_value.order_by.return_value
            filtered = queryset.filter.return_value
            app = SimpleNamespace(pk=12, client_id=7)
            filtered.__getitem__.return_value = [app]
            output = StringIO()
            Command(stdout=output).handle(client=7, limit=1, application=12)
            applications.filter.assert_called_once_with(client_id=7)
            queryset.filter.assert_called_once_with(pk=12)
            filtered.__getitem__.assert_called_once_with(slice(None, 2))
            cursor = connection.cursor.return_value.__enter__.return_value
            self.assertEqual(cursor.execute.call_args_list[0].args[0],
                             'SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
            preview.assert_called_once_with(app)
            self.assertTrue(json.loads(output.getvalue())['read_only'])

    def test_invalid_client_rejected_without_query(self):
        with self.assertRaises(CommandError):
            call_command(Command(), client=0)

    def test_unbounded_limit_rejected(self):
        for limit in [0, 101]:
            with self.subTest(limit=limit), self.assertRaises(CommandError):
                call_command(Command(), client=1, limit=limit)

    def test_invalid_application_rejected(self):
        with self.assertRaises(CommandError):
            call_command(Command(), client=1, application=-1)

    def test_no_apply_option(self):
        parser = Command().create_parser('manage.py', 'preview_billing_readiness')
        self.assertNotIn('--apply', parser._option_string_actions)
