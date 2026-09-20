from types import SimpleNamespace

from django.db.models import Q
from django.test import TestCase

from audit.models import OrderAuditEntry
from sku.models import Agency
from fbs.goods_types import _permission_audit_rows, receiving_placement_allowed_snapshot_ids


class ReceivingPermissionProjectionTests(TestCase):
    keys = ('status', 'act', 'act_state', 'flow_closed', 'flow_reopened',
            'event', 'pallet_code', 'pallet_placement_allowed', 'flow_pallets')

    def setUp(self):
        self.agency = Agency.objects.create(agn_name='Projection test')

    def entry(self, payload, order='permission-test'):
        return OrderAuditEntry.objects.bulk_create([OrderAuditEntry(
            agency=self.agency, order_type='receiving', order_id=order, action='update', payload=payload,
        )])[0]

    def test_projection_matches_orm_with_empty_legacy_and_large_documents(self):
        payloads = [None, {}, {'flow_state': None}, {'flow_boxes': []},
                    {'status': 'cancelled', 'flow_closed': False, 'flow_reopened': True},
                    {'flow_pallets': [{'code': 'P', 'sealed': False}]},
                    {'flow_state': {'pallets': [{'code': 'P', 'sealed': True}],
                                    'boxes': [{'marking': 'x' * 500000}]},
                     'pallet_placement_allowed': False, 'act': {'status': 'closed'}}]
        selected = [self.entry(payload).pk for payload in payloads]
        self.entry({'status': 'must-not-leak'}, order='another-order')
        qs = OrderAuditEntry.objects.filter(pk__in=selected).order_by('created_at', 'id')
        expected = list(qs.annotate(
            has_flow_state=Q(payload__has_key='flow_state'),
            has_flow_boxes=Q(payload__has_key='flow_boxes'),
        ).values('agency_id', 'order_id', 'has_flow_state', 'has_flow_boxes',
                 'payload__flow_state__pallets', *('payload__' + key for key in self.keys)))
        self.assertEqual(list(_permission_audit_rows(qs, self.keys)), expected)

    def snapshot(self, agency_id=None):
        return SimpleNamespace(pk=123, agency_id=agency_id or self.agency.pk,
                               source_context_type='receiving', source_context_id='permission-test',
                               zone_code='PR', warehouse_state_code='placed_in_receiving',
                               parent_container=SimpleNamespace(container_code='P'))

    def test_closed_reopened_and_cancelled_states_are_read_fresh(self):
        entry = self.entry({'flow_closed': True})
        snapshot = self.snapshot()
        self.assertEqual(receiving_placement_allowed_snapshot_ids([snapshot]), {123})
        entry.payload = {'flow_reopened': True}
        entry.save(update_fields=['payload'])
        self.assertEqual(receiving_placement_allowed_snapshot_ids([snapshot]), set())
        self.entry({'flow_closed': True, 'status': 'cancelled'})
        self.assertEqual(receiving_placement_allowed_snapshot_ids([snapshot]), set())

    def test_permission_cannot_leak_to_another_agency(self):
        self.entry({'flow_closed': True})
        other = Agency.objects.create(agn_name='Other projection owner')
        self.assertEqual(receiving_placement_allowed_snapshot_ids([self.snapshot(other.pk)]), set())
