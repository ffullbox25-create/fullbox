from django.test import TestCase, override_settings
from django.core.exceptions import ValidationError
from sklad.models import WarehouseReserve, WarehouseEvent
from sklad.services.warehouse_write_path import WarehouseWritePathService, WarehouseTransitionError
from sklad.services.fbs_quantity_reserves import pool_reserves, capacity, identity, allocate_pool, protect_sources, convert_unstarted_request
from sklad.services.stock_availability import stock_rows_with_availability
from . import test_client_movement_execution as fixtures
from .client_portal import create_client_movement_request, client_movement_source_stock_payload
from .services.client_movements import approve_client_movement_by_manager, accept_client_movement_request, cancel_client_movement_request


@override_settings(ROOT_URLCONF='fbs.test_urls', FBS_MODULE_ENABLED=True,
                   FBS_WAREHOUSE_WRITES_ENABLED=True,FBS_ZONE_CODE='FBS',
                   FBS_CLIENT_FULL_PALLET_LOGICAL_POST_ON_ACCEPT=False)
class QuantityMovementTests(TestCase):
    setUp = fixtures.FbsClientMovementExecutionTests.setUp
    _source_box = fixtures.FbsClientMovementExecutionTests._source_box
    _snapshot = fixtures.FbsClientMovementExecutionTests._snapshot

    def request(self, qty=5, key='pool', mode='box'):
        return create_client_movement_request(agency=self.agency,mode=mode,
            raw_lines=[{'barcode':self.barcode,'qty':qty,'units_per_box':5,'box_count':qty//5}],
            requested_by=self.manager,idempotency_key=key)

    def test_reserve_is_quantity_without_snapshot_mutation(self):
        s=self._snapshot(code='Q1',qty=5)
        r=self.request()
        s.refresh_from_db()
        self.assertEqual((s.qty,s.available_qty,s.other_reserved_qty),(5,5,0))
        reserve=pool_reserves(self.agency.id,r.id).get()
        self.assertEqual((reserve.qty_reserved,reserve.qty_allocated),(5,0))
        e=reserve.events.get(event_type='fbs_movement_quantity_reserved')
        self.assertIsNone(e.container_id)
        self.assertNotIn('source_snapshot_id',e.payload)
        rows=WarehouseWritePathService.fbs_movement_reserve_allocations(agency=self.agency,request_id=r.id)
        self.assertEqual(rows[0]['snapshot_id'],0)
        self.assertEqual(client_movement_source_stock_payload(agency=self.agency)['total'],0)
        with self.assertRaises(ValidationError):self.request(key='too-many')

    def test_sources_selected_at_acceptance_not_submission(self):
        a=self._snapshot(code='Q2A',qty=5)
        b=self._snapshot(code='Q2B',qty=5)
        r=self.request()
        # Another operation can consume A if B still covers the pooled demand.
        protect_sources([a])
        a.qty=0;a.available_qty=0;a.is_archived=True;a.warehouse_state_code='shipped';a.save()
        approve_client_movement_by_manager(request_id=r.id,reviewed_by=self.manager)
        self.assertTrue(pool_reserves(self.agency.id,r.id).exists())
        result=accept_client_movement_request(request_id=r.id,accepted_by=self.storekeeper,target_pallet_id=self.pallet.id)
        self.assertTrue(result.plans)
        self.assertFalse(pool_reserves(self.agency.id,r.id).exists())
        self.assertEqual(result.plans[0].lines.get().source_container_id,b.container_id)

    def test_box_pool_keeps_requested_box_size(self):
        smaller=self._snapshot(code='Q2-SMALL',qty=2)
        exact=self._snapshot(code='Q2-EXACT',qty=5)
        self._snapshot(code='Q2-LARGE',qty=8)
        r=self.request()

        allocate_pool(r,self.storekeeper)

        rows=WarehouseWritePathService.fbs_movement_reserve_allocations(
            agency=self.agency,request_id=r.id,lock=True)
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['snapshot_id'],exact.id)
        self.assertEqual(rows[0]['container_id'],exact.container_id)
        self.assertEqual(rows[0]['qty'],5)
        smaller.refresh_from_db()
        self.assertEqual((smaller.available_qty,smaller.other_reserved_qty),(2,0))

    def test_cancellation_changes_no_physical_stock(self):
        s=self._snapshot(code='Q3',qty=5)
        r=self.request()
        cancel_client_movement_request(request_id=r.id,agency=self.agency,canceled_by=self.manager)
        s.refresh_from_db()
        self.assertEqual((s.qty,s.available_qty,s.other_reserved_qty),(5,5,0))
        self.assertFalse(pool_reserves(self.agency.id,r.id).exists())

    def test_direct_shipping_and_processing_cannot_overbook_pool(self):
        s=self._snapshot(code='Q4',qty=5)
        self.request()
        item={'sku_code':s.sku_code,'barcode':s.barcode,'size':s.size,'goods_type':s.goods_type,'qty':5,'reserve_pool':True}
        for method in [WarehouseWritePathService.reserve_for_shipping,WarehouseWritePathService.reserve_for_processing]:
            with self.assertRaises(WarehouseTransitionError):
                method(agency=self.agency,order_id='OTHER',items=[item])
        with self.assertRaises(WarehouseTransitionError):protect_sources([s])

    def test_unrelated_reserved_source_is_not_blocked_by_fbs_pool(self):
        from sku.models import SKU

        self._snapshot(code='Q4-POOL',qty=5)
        self.request(key='unrelated-pool')
        other_sku=SKU.objects.create(
            agency=self.agency,
            sku_code='MOV-SKU-OTHER',
            name='Другой товар',
        )
        unrelated=self._snapshot(code='Q4-OTHER',qty=5,barcode='4600000009002')
        unrelated.sku_ref=other_sku
        unrelated.sku_code=other_sku.sku_code
        unrelated.name=other_sku.name
        unrelated.save(update_fields=['sku_ref','sku_code','name','updated_at'])
        WarehouseWritePathService.reserve_for_shipping(
            agency=self.agency,
            order_id='SHIP-UNRELATED',
            items=[{
                'sku_code':unrelated.sku_code,
                'barcode':unrelated.barcode,
                'size':unrelated.size,
                'goods_type':unrelated.goods_type,
                'qty':5,
                'reserve_pool':True,
                'box_codes':[unrelated.container_code],
            }],
        )

        protect_sources([unrelated])

    def test_stock_view_counts_pool_once_and_remains_read_only(self):
        s=self._snapshot(code='Q5',qty=10)
        self.request(mode='item')
        for _ in range(2):
            rows=stock_rows_with_availability(agency=self.agency)
            self.assertEqual(sum(row['available_qty'] for row in rows),5)
        s.refresh_from_db()
        self.assertEqual(s.available_qty,10)

    def test_allocation_shortage_rolls_back_preserving_pool(self):
        s=self._snapshot(code='Q6',qty=5)
        r=self.request()
        s.qty=0;s.available_qty=0;s.is_archived=True;s.save()
        with self.assertRaises(WarehouseTransitionError):allocate_pool(r,self.storekeeper)
        self.assertEqual(pool_reserves(self.agency.id,r.id).get().qty_reserved,5)

    def test_own_pool_is_not_counted_twice_when_allocating(self):
        self._snapshot(code='Q7',qty=5)
        r=self.request()
        allocate_pool(r,self.storekeeper)
        rows=WarehouseWritePathService.fbs_movement_reserve_allocations(agency=self.agency,request_id=r.id,lock=True)
        self.assertEqual(sum(row['qty'] for row in rows),5)
        self.assertTrue(rows[0]['snapshot_id'])

    def test_fbs_respects_existing_shipping_pool(self):
        s=self._snapshot(code='Q8',qty=5)
        WarehouseWritePathService.reserve_for_shipping(agency=self.agency,order_id='SHIP',items=[
            {'sku_code':s.sku_code,'barcode':s.barcode,'size':s.size,'goods_type':s.goods_type,'qty':5,'reserve_pool':True}])
        with self.assertRaises(ValidationError):self.request()

    def test_current_shipping_box_is_protected_from_fbs_quantity_distribution(self):
        current=self._snapshot(code='Q8-CURRENT',qty=5)
        alternate=self._snapshot(code='Q8-ALTERNATE',qty=5)
        WarehouseWritePathService.reserve_for_shipping(
            agency=self.agency,
            order_id='SHIP-CURRENT',
            items=[{
                'sku_code':current.sku_code,
                'barcode':current.barcode,
                'size':current.size,
                'goods_type':current.goods_type,
                'qty':5,
                'reserve_pool':True,
                'box_codes':[current.container_code],
            }],
        )
        self.request(key='fbs-after-shipping')

        rows=stock_rows_with_availability(
            agency=self.agency,
            exclude_shipping_order_id='SHIP-CURRENT',
        )
        available_by_box={row['box_code']:row['available_qty'] for row in rows}
        self.assertEqual(available_by_box[current.container_code],5)
        self.assertEqual(available_by_box[alternate.container_code],0)

    def test_migration_does_not_resurrect_shipped_stock(self):
        s=self._snapshot(code='Q9A',qty=5)
        replacement=self._snapshot(code='Q9B',qty=5)
        r=self.request()
        allocate_pool(r,self.storekeeper)
        s.refresh_from_db()
        self.assertEqual(s.other_reserved_qty,5)
        s.qty=0;s.available_qty=0;s.is_archived=True;s.warehouse_state_code='shipped';s.save()
        converted=convert_unstarted_request(r.id)
        self.assertEqual(converted['restored_available_qty'],0)
        s.refresh_from_db();replacement.refresh_from_db()
        self.assertEqual((s.qty,s.available_qty,s.other_reserved_qty),(0,0,0))
        self.assertEqual(replacement.available_qty,5)
        self.assertEqual(pool_reserves(self.agency.id,r.id).get().qty_reserved,5)

    def test_migration_rejects_started_request(self):
        self._snapshot(code='Q10',qty=5)
        r=self.request();r.status='in_progress';r.save()
        with self.assertRaises(WarehouseTransitionError):convert_unstarted_request(r.id)

    def test_other_client_with_same_barcode_is_not_reserved(self):
        from sku.models import Agency, SKU
        s=self._snapshot(code='Q11A',qty=5)
        other=self._snapshot(code='Q11B',qty=5)
        owner=Agency.objects.create(agn_name='Other owner')
        other.agency=owner;other.save()
        other.container.agency=owner;other.container.save()
        self.request()
        self.assertEqual(sum(r['available_qty'] for r in stock_rows_with_availability(agency=owner)),5)

    def test_direct_exact_fbs_cannot_consume_another_quantity_reserve(self):
        s=self._snapshot(code='Q12',qty=5)
        self.request()
        with self.assertRaises(WarehouseTransitionError):
            WarehouseWritePathService.reserve_for_fbs_movement(agency=self.agency,request_id=999,
                allocations=[dict(snapshot_id=s.id,request_line_id=999,qty=5,container_id=s.container_id)])

    def test_converted_history_no_longer_pins_original_box(self):
        a=self._snapshot(code='Q13A',qty=5)
        self._snapshot(code='Q13B',qty=5)
        r=self.request();allocate_pool(r,self.storekeeper)
        convert_unstarted_request(r.id)
        a.refresh_from_db()
        WarehouseWritePathService._assert_shipping_does_not_take_fbs_stock([a])


from django.test import TransactionTestCase
from django.db import connections
from threading import Barrier, Thread
from queue import Queue


@override_settings(ROOT_URLCONF='fbs.test_urls', FBS_MODULE_ENABLED=True,
                   FBS_WAREHOUSE_WRITES_ENABLED=True,FBS_ZONE_CODE='FBS',
                   FBS_CLIENT_FULL_PALLET_LOGICAL_POST_ON_ACCEPT=False)
class QuantityReserveRaceTests(TransactionTestCase):
    setUp = fixtures.FbsClientMovementExecutionTests.setUp
    _source_box = fixtures.FbsClientMovementExecutionTests._source_box
    _snapshot = fixtures.FbsClientMovementExecutionTests._snapshot

    def test_two_concurrent_requests_cannot_reserve_the_same_quantity(self):
        self._snapshot(code='RACE',qty=5)
        barrier=Barrier(2);results=Queue()
        def worker(key):
            try:
                barrier.wait(timeout=10)
                create_client_movement_request(agency=self.agency,mode='box',
                    raw_lines=[{'barcode':self.barcode,'qty':5,'units_per_box':5,'box_count':1}],
                    requested_by=self.manager,idempotency_key=key)
                results.put('ok')
            except ValidationError:
                results.put('shortage')
            except Exception as e:
                results.put(repr(e))
            finally:connections.close_all()
        threads=[Thread(target=worker,args=(f'race-{i}',)) for i in range(2)]
        for thread in threads:thread.start()
        for thread in threads:thread.join(timeout=20)
        self.assertTrue(all(not t.is_alive() for t in threads))
        self.assertEqual(sorted(results.get_nowait() for _ in range(2)),['ok','shortage'])
        self.assertEqual(sum(r.qty_reserved for r in pool_reserves(self.agency.id)),5)
