from itertools import product
from django.test import TestCase, RequestFactory
from django.template.loader import render_to_string
from django.utils import timezone
from django.core.paginator import Paginator
from sku.models import Agency
from .models import FbsHandoverBatch as Batch, FbsIntegrationProfile as Profile
from .controller_shipment_ui import controller_shipment_stage, shipment_stage_filters, shipment_stage_counts
from .tsd_views import _handover_queryset, _prepare_handover_list_rows


class ShipmentPresentationTests(TestCase):
    def setUp(self):
        agency = Agency.objects.create(agn_name='Shipment presentation test')
        self.wb = Profile.objects.create(agency=agency, marketplace='wb')
        self.ozon = Profile.objects.create(agency=agency, marketplace='ozon')

    def test_filters_and_counts_match_physical_stage_for_all_state_combinations(self):
        for profile, status, sent, label in product(
            (self.wb, self.ozon), ('open','ready','dispatched','accepted','problem','archived'),
            (False, True), (False, True),
        ):
            Batch.objects.create(profile=profile,status=status,
                dispatched_at=timezone.now() if sent else None,
                marketplace_state=Batch.MARKETPLACE_COMPLETE,
                supply_label_file='label.png' if label else '')
        filters=shipment_stage_filters()
        expected={key: set() for key in filters}
        for batch in Batch.objects.select_related('profile'):
            key=controller_shipment_stage(batch).key
            expected[key].add(batch.pk)
            if key not in ('archived','accepted','transit'): expected['active'].add(batch.pk)
        counts=shipment_stage_counts()
        for key, condition in filters.items():
            self.assertSetEqual(set(Batch.objects.filter(condition).values_list('pk',flat=True)),expected[key],key)
            self.assertEqual(counts[key],len(expected[key]),key)

    def test_both_roles_show_transit_for_problem_record_already_dispatched(self):
        batch=Batch.objects.create(profile=self.ozon,status='problem',dispatched_at=timezone.now())
        batches=_prepare_handover_list_rows(list(_handover_queryset().filter(pk=batch.pk)))
        self.assertEqual(batches[0].movement_stage.key,'transit')
        for role in ('storekeeper','fbs_controller'):
            request=RequestFactory().get('/fbs/tsd/storekeeper/handover/')
            html=render_to_string('fbs/tsd_handover_list.html',dict(
                request=request,request_role=role,batches=batches,
                page_obj=Paginator(batches,50).page(1),handover_page_size=50,
            ))
            self.assertIn('controller-stage-pill-transit">В пути</span>',html)
            self.assertNotIn('Отклонена / проблема',html)

    def test_marketplace_preparation_does_not_count_as_dispatch(self):
        Batch.objects.create(profile=self.wb,status='ready',marketplace_state=Batch.MARKETPLACE_COMPLETE,supply_label_file='label.png')
        counts=shipment_stage_counts()
        self.assertEqual(counts['ready'],1)
        self.assertEqual(counts['active'],1)
        self.assertEqual(counts['transit'],0)
        self.assertEqual(counts['accepted'],0)
