from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings

from sklad.models import WarehouseContainer, WarehouseLocation, WarehouseOperation, WarehouseReserve, WarehouseStockSnapshot
from sklad.services.warehouse_transitions import WarehouseTransitionError
from sklad.services.warehouse_write_path import WarehouseWritePathService
from sku.models import SKU
from . import test_client_movement_execution as fixtures
from .client_portal import client_movement_source_stock_payload, create_client_movement_request
from .goods_types import fbs_client_movement_source_stock_q, is_fbs_client_movement_source_stock
from .exceptions import FbsReplenishmentError
from .models import FbsClientMovementRequest, FbsPallet, FbsStockBalance
from .services.client_movements import (
    accept_client_movement_request,
    approve_client_movement_by_manager,
    eligible_source_boxes,
)
from .services.receiving_movements import (
    create_receiving_fbs_movement_request,
    receiving_pallet_movement_options,
)
from .services.free_relocation import inspect_fbs_pallet


@override_settings(ROOT_URLCONF='fbs.test_urls', FBS_MODULE_ENABLED=True,
                   FBS_WAREHOUSE_WRITES_ENABLED=True, FBS_ZONE_CODE='FBS',
                   FBS_CLIENT_FULL_PALLET_LOGICAL_POST_ON_ACCEPT=False)
class ReadyProcessingMovementTests(TestCase):
    _source_box = fixtures.FbsClientMovementExecutionTests._source_box
    _snapshot = fixtures.FbsClientMovementExecutionTests._snapshot
    _approve_and_accept = fixtures.FbsClientMovementExecutionTests._approve_and_accept

    def setUp(self):
        fixtures.FbsClientMovementExecutionTests.setUp(self)
        self.ready_location = WarehouseLocation.objects.create(
            warehouse_code='MSK', zone_code='PR', zone_kind='receiving',
            location_code='PR-READY-1', row_no=51, section_no=1, tier_no=0, cell_no=1,
        )
        self.ready_pallet = WarehouseContainer.objects.create(
            agency=self.agency, container_type='pallet', container_code='READY-PALLET',
            current_location=self.ready_location,
        )

    def ready_box(self, code, **changes):
        row = self._snapshot(code=code, qty=5)
        row.container.current_location = self.ready_location
        row.container.parent_container = self.ready_pallet
        row.container.save()
        row.location = self.ready_location
        row.parent_container = self.ready_pallet
        row.zone_code = 'PR'
        row.zone_kind = 'receiving'
        row.warehouse_state_code = 'placed_after_processing'
        for key, value in changes.items():
            setattr(row, key, value)
        row.save()
        return row

    def payload(self, boxes=True):
        return client_movement_source_stock_payload(
            agency=self.agency, barcode=self.barcode, include_box_options=boxes,
        )

    def request_boxes(self, count):
        return create_client_movement_request(
            agency=self.agency, mode='box',
            raw_lines=[{'barcode': self.barcode, 'qty': count * 5,
                        'units_per_box': 5, 'box_count': count}],
            idempotency_key=f'ready-boxes-{count}',
        )

    def reserve_direct(self, row, request_id=90001):
        return WarehouseWritePathService.reserve_for_fbs_movement(
            agency=self.agency, request_id=request_id, whole_container_ids=[row.container_id],
            allocations=[{'snapshot_id': row.id, 'request_line_id': 1, 'qty': 5,
                          'container_id': row.container_id, 'barcode': row.barcode,
                          'sku_id': row.sku_ref_id}], created_by=self.storekeeper,
        )

    def receiving_box(
        self,
        code,
        *,
        allowed=True,
        sealed=True,
        closed=False,
        agency=None,
        normalize_gv=True,
    ):
        from audit.models import OrderAuditEntry
        row = self._snapshot(code=code, qty=5, normalize_gv=normalize_gv)
        row.container.current_location = self.ready_location
        row.container.parent_container = self.ready_pallet
        row.container.save()
        row.location = self.ready_location
        row.parent_container = self.ready_pallet
        row.zone_code = 'PR'
        row.zone_kind = 'receiving'
        row.warehouse_state_code = 'placed_in_receiving'
        row.source_context_type = 'receiving'
        row.source_context_id = 'PR-FBS-RELEASE'
        row.save()
        OrderAuditEntry.objects.create(
            agency=agency or self.agency, order_type='receiving', order_id=row.source_context_id,
            action='update', payload={'flow_state': {'pallets': [{
                'code': self.ready_pallet.container_code, 'sealed': sealed,
                'boxes': [row.container_code],
            }]}, 'flow_closed': closed},
        )
        self.receiving_permission(allowed, agency=agency)
        return row

    def receiving_permission(self, allowed, *, agency=None):
        from audit.models import OrderAuditEntry
        OrderAuditEntry.objects.create(
            agency=agency or self.agency, order_type='receiving', order_id='PR-FBS-RELEASE',
            action='update', payload={'event': 'receiving_pallet_placement_permission',
                'pallet_code': self.ready_pallet.container_code, 'pallet_placement_allowed': allowed},
        )

    def test_open_receiving_with_pallet_permission_is_available_and_reservable(self):
        source = self.receiving_box('RECEIVING-ALLOWED')
        for boxes in (False, True):
            row = self.payload(boxes)['results'][0]
            self.assertEqual(row['client_available_qty'], 5)
            self.assertIn('Разрешено размещение', row['goods_type'])
        self.reserve_direct(source)
        source.refresh_from_db()
        self.assertEqual((source.qty, source.available_qty, source.other_reserved_qty), (5, 0, 5))
        self.assertEqual(source.location_id, self.ready_location.id)

    def test_receiving_permission_is_rechecked_before_low_level_reservation(self):
        source = self.receiving_box('RECEIVING-REVOKED')
        self.assertEqual(self.payload()['total'], 1)
        self.receiving_permission(False)
        for boxes in (False, True):
            self.assertEqual(self.payload(boxes)['total'], 0)
        with self.assertRaises(WarehouseTransitionError):
            self.reserve_direct(source)
        source.refresh_from_db()
        self.assertEqual((source.qty, source.available_qty, source.other_reserved_qty), (5, 5, 0))
        self.assertFalse(WarehouseReserve.objects.exists())

    def test_receiving_open_pallet_is_blocked_even_with_permission(self):
        source = self.receiving_box('RECEIVING-OPEN-PALLET', sealed=False)
        self.assertEqual(self.payload()['total'], 0)
        with self.assertRaises(WarehouseTransitionError):
            self.reserve_direct(source)

    def test_receiving_without_permission_is_blocked(self):
        source = self.receiving_box('RECEIVING-NO-PERMISSION', allowed=False)
        self.assertEqual(self.payload()['total'], 0)
        with self.assertRaises(WarehouseTransitionError):
            self.reserve_direct(source)

    def test_receiving_permission_is_scoped_to_client(self):
        from sku.models import Agency
        other = Agency.objects.create(agn_name='Other receiving owner')
        source = self.receiving_box('RECEIVING-OTHER-OWNER', agency=other)
        self.assertEqual(self.payload()['total'], 0)
        with self.assertRaises(WarehouseTransitionError):
            self.reserve_direct(source)

    def test_receiving_permission_does_not_release_another_pallet(self):
        from audit.models import OrderAuditEntry
        allowed = self.receiving_box('RECEIVING-ONE-PALLET')
        other_pallet = WarehouseContainer.objects.create(
            agency=self.agency, container_type='pallet', container_code='OTHER-RECEIVING-PALLET',
            current_location=self.ready_location,
        )
        blocked = self.ready_box('RECEIVING-OTHER-PALLET', warehouse_state_code='placed_in_receiving',
                                 source_context_type='receiving', source_context_id=allowed.source_context_id,
                                 parent_container=other_pallet)
        blocked.container.parent_container = other_pallet
        blocked.container.save()
        OrderAuditEntry.objects.create(
            agency=self.agency, order_type='receiving', order_id=allowed.source_context_id,
            action='update', payload={'flow_state': {'pallets': [
                {'code': self.ready_pallet.container_code, 'sealed': True},
                {'code': other_pallet.container_code, 'sealed': True},
            ]}},
        )
        self.assertEqual(self.payload()['results'][0]['client_available_qty'], 5)
        with self.assertRaises(WarehouseTransitionError):
            self.reserve_direct(blocked)
        self.reserve_direct(allowed)

    def test_cancelled_receiving_is_not_released_by_old_permission(self):
        from audit.models import OrderAuditEntry
        source = self.receiving_box('RECEIVING-CANCELLED')
        OrderAuditEntry.objects.create(
            agency=self.agency, order_type='receiving', order_id=source.source_context_id,
            action='status', payload={'status': 'cancelled'},
        )
        self.assertEqual(self.payload()['total'], 0)
        with self.assertRaises(WarehouseTransitionError):
            self.reserve_direct(source)

    def test_closed_receiving_is_available_without_separate_permission(self):
        source = self.receiving_box('RECEIVING-CLOSED', allowed=False, closed=True)
        self.assertEqual(self.payload()['results'][0]['client_available_qty'], 5)
        self.reserve_direct(source)

    def test_receiving_pallet_can_create_exact_fbs_movement(self):
        source = self.receiving_box('RECEIVING-SELECTED')

        request = create_receiving_fbs_movement_request(
            agency_id=self.agency.id,
            order_id=source.source_context_id,
            pallet_codes=[self.ready_pallet.container_code],
            created_by=self.storekeeper,
        )

        source.refresh_from_db()
        self.assertEqual(request.status, FbsClientMovementRequest.STATUS_APPROVED)
        self.assertEqual(request.source_file_name, 'receiving:PR-FBS-RELEASE')
        self.assertEqual(request.requested_qty, 5)
        self.assertEqual(request.requested_box_count, 1)
        self.assertEqual((source.qty, source.available_qty, source.other_reserved_qty), (5, 0, 5))
        reserve = WarehouseReserve.objects.get(
            context_type='fbs_client_movement',
            context_id=str(request.id),
        )
        self.assertTrue(
            reserve.events.filter(
                event_type='fbs_movement_reserved',
                payload__source_snapshot_id=source.id,
            ).exists()
        )

    def test_receiving_movement_lists_and_reserves_only_selected_pallet(self):
        from audit.models import OrderAuditEntry

        selected = self.receiving_box('RECEIVING-FIRST')
        second_pallet = WarehouseContainer.objects.create(
            agency=self.agency,
            container_type='pallet',
            container_code='RECEIVING-PALLET-TWO',
            current_location=self.ready_location,
        )
        other = self.ready_box(
            'RECEIVING-SECOND',
            warehouse_state_code='placed_in_receiving',
            source_context_type='receiving',
            source_context_id=selected.source_context_id,
            parent_container=second_pallet,
        )
        other.container.parent_container = second_pallet
        other.container.save()
        OrderAuditEntry.objects.create(
            agency=self.agency,
            order_type='receiving',
            order_id=selected.source_context_id,
            action='update',
            payload={'flow_state': {'pallets': [
                {'code': self.ready_pallet.container_code, 'sealed': True,
                 'boxes': [selected.container.container_code]},
                {'code': second_pallet.container_code, 'sealed': True,
                 'boxes': [other.container.container_code]},
            ]}},
        )
        OrderAuditEntry.objects.create(
            agency=self.agency,
            order_type='receiving',
            order_id=selected.source_context_id,
            action='update',
            payload={'event': 'receiving_pallet_placement_permission',
                     'pallet_code': second_pallet.container_code,
                     'pallet_placement_allowed': True},
        )

        options = receiving_pallet_movement_options(
            agency_id=self.agency.id,
            order_id=selected.source_context_id,
        )
        self.assertEqual({row.pallet_code for row in options}, {
            self.ready_pallet.container_code,
            second_pallet.container_code,
        })
        request = create_receiving_fbs_movement_request(
            agency_id=self.agency.id,
            order_id=selected.source_context_id,
            pallet_codes=[self.ready_pallet.container_code],
            created_by=self.storekeeper,
        )
        selected.refresh_from_db()
        other.refresh_from_db()
        self.assertEqual((selected.available_qty, selected.other_reserved_qty), (0, 5))
        self.assertEqual((other.available_qty, other.other_reserved_qty), (5, 0))
        self.assertEqual(request.requested_box_count, 1)

    def test_receiving_pallet_option_exposes_articles_and_quantities(self):
        from audit.models import OrderAuditEntry

        first = self.receiving_box('RECEIVING-COMPOSITION-FIRST')
        second_sku = SKU.objects.create(
            agency=self.agency,
            sku_code='MOV-SKU-2',
            name='Второй товар на палете',
        )
        second = self._snapshot(
            code='RECEIVING-COMPOSITION-SECOND',
            qty=7,
            barcode='4600000009002',
        )
        second.container.current_location = self.ready_location
        second.container.parent_container = self.ready_pallet
        second.container.save(update_fields=['current_location', 'parent_container'])
        second.location = self.ready_location
        second.parent_container = self.ready_pallet
        second.zone_code = 'PR'
        second.zone_kind = 'receiving'
        second.warehouse_state_code = 'placed_in_receiving'
        second.source_context_type = 'receiving'
        second.source_context_id = first.source_context_id
        second.sku_ref = second_sku
        second.sku_code = second_sku.sku_code
        second.name = second_sku.name
        second.save()
        OrderAuditEntry.objects.create(
            agency=self.agency,
            order_type='receiving',
            order_id=first.source_context_id,
            action='update',
            payload={'flow_state': {'pallets': [{
                'code': self.ready_pallet.container_code,
                'sealed': True,
                'boxes': [first.container.container_code, second.container.container_code],
            }]}},
        )

        option = receiving_pallet_movement_options(
            agency_id=self.agency.id,
            order_id=first.source_context_id,
        )[0]

        self.assertEqual(option.article_count, 2)
        self.assertEqual(option.position_count, 2)
        self.assertEqual(option.box_count, 2)
        self.assertEqual(option.qty, 12)
        self.assertEqual(
            [(row.sku_code, row.product_name, row.barcode, row.box_count, row.qty)
             for row in option.articles],
            [
                ('MOV-SKU-1', 'Товар для FBS', self.barcode, 1, 5),
                ('MOV-SKU-2', 'Второй товар на палете', '4600000009002', 1, 7),
            ],
        )

    def test_receiving_movement_allows_all_available_pallets_over_fifty(self):
        from audit.models import OrderAuditEntry

        order_id = 'PR-FBS-BULK-51'
        pallets = []
        rows = []
        for index in range(51):
            pallet = WarehouseContainer.objects.create(
                agency=self.agency,
                container_type=WarehouseContainer.TYPE_PALLET,
                container_code=f'RECEIVING-BULK-PALLET-{index:02d}',
                current_location=self.ready_location,
            )
            row = self._snapshot(code=f'RECEIVING-BULK-BOX-{index:02d}', qty=1)
            row.container.current_location = self.ready_location
            row.container.parent_container = pallet
            row.container.save(update_fields=['current_location', 'parent_container'])
            row.location = self.ready_location
            row.parent_container = pallet
            row.zone_code = 'PR'
            row.zone_kind = 'receiving'
            row.warehouse_state_code = 'placed_in_receiving'
            row.source_context_type = 'receiving'
            row.source_context_id = order_id
            row.save()
            pallets.append(pallet)
            rows.append(row)
        OrderAuditEntry.objects.create(
            agency=self.agency,
            order_type='receiving',
            order_id=order_id,
            action='update',
            payload={'flow_state': {'pallets': [
                {
                    'code': pallet.container_code,
                    'sealed': True,
                    'boxes': [row.container.container_code],
                }
                for pallet, row in zip(pallets, rows)
            ]}},
        )
        for pallet in pallets:
            OrderAuditEntry.objects.create(
                agency=self.agency,
                order_type='receiving',
                order_id=order_id,
                action='update',
                payload={
                    'event': 'receiving_pallet_placement_permission',
                    'pallet_code': pallet.container_code,
                    'pallet_placement_allowed': True,
                },
            )

        request = create_receiving_fbs_movement_request(
            agency_id=self.agency.id,
            order_id=order_id,
            pallet_codes=[pallet.container_code for pallet in pallets],
            created_by=self.storekeeper,
        )

        self.assertEqual(request.requested_box_count, 51)
        self.assertEqual(request.requested_qty, 51)
        self.assertEqual(
            WarehouseStockSnapshot.objects.filter(
                id__in=[row.id for row in rows],
                available_qty=0,
                other_reserved_qty=1,
            ).count(),
            51,
        )

    def test_receiving_movement_rejects_non_gv_box(self):
        source = self.receiving_box('RECEIVING-WRONG-TYPE', normalize_gv=False)

        with self.assertRaisesMessage(FbsReplenishmentError, 'недоступны'):
            create_receiving_fbs_movement_request(
                agency_id=self.agency.id,
                order_id=source.source_context_id,
                pallet_codes=[self.ready_pallet.container_code],
                created_by=self.storekeeper,
            )

    @override_settings(FBS_CLIENT_FULL_PALLET_LOGICAL_POST_ON_ACCEPT=True)
    def test_receiving_source_complete_pallet_is_posted_to_fbs_in_place(self):
        source = self.receiving_box('RECEIVING-PHYSICAL')
        source_container_id = source.container_id
        source_location_id = source.container.current_location_id
        source_parent_id = source.container.parent_container_id
        request = create_receiving_fbs_movement_request(
            agency_id=self.agency.id,
            order_id=source.source_context_id,
            pallet_codes=[self.ready_pallet.container_code],
            created_by=self.storekeeper,
        )

        result = accept_client_movement_request(
            request_id=request.id,
            accepted_by=self.storekeeper,
        )

        request.refresh_from_db()
        self.assertEqual(
            request.status,
            FbsClientMovementRequest.STATUS_COMPLETED,
        )
        self.assertTrue(result.plans)
        self.assertFalse(
            fixtures.MoveTask.objects.filter(
                payload__fbs_movement_id=request.id,
            ).exclude(status=fixtures.MoveTask.STATUS_CANCELED).exists()
        )
        source.refresh_from_db()
        self.assertEqual((source.qty, source.available_qty, source.is_archived), (0, 0, True))
        source_container = WarehouseContainer.objects.get(pk=source_container_id)
        self.assertEqual(source_container.current_location_id, source_location_id)
        self.assertEqual(source_container.parent_container_id, source_parent_id)
        self.assertEqual(
            FbsStockBalance.objects.get(
                agency=self.agency,
                box__source_container_id=source_container_id,
                barcode=self.barcode,
            ).qty,
            5,
        )
        pallet = FbsPallet.objects.get(
            agency=self.agency,
            pallet_code=self.ready_pallet.container_code,
        )
        self.assertEqual(pallet.warehouse_container_id, self.ready_pallet.id)
        self.ready_pallet.refresh_from_db()
        self.assertEqual(self.ready_pallet.current_location_id, source_location_id)
        self.assertEqual(self.ready_pallet.source_context_type, 'fbs_storage')
        source_container.refresh_from_db()
        self.assertEqual(source_container.source_context_type, 'fbs_placement')
        inspection = inspect_fbs_pallet(self.ready_pallet.container_code)
        self.assertTrue(inspection['found'])
        self.assertTrue(inspection['can_move'], inspection['blockers'])
        self.assertEqual(inspection['location_id'], source_location_id)

    def test_receiving_item_request_is_accepted_from_actual_pr_location(self):
        self.receiving_box('RECEIVING-ITEM')
        request = create_client_movement_request(
            agency=self.agency, mode='item', raw_lines=[{'barcode': self.barcode, 'qty': 3}],
            idempotency_key='receiving-item',
        )
        approve_client_movement_by_manager(request_id=request.id, reviewed_by=self.manager)
        plan = self._approve_and_accept(request).plans[0]
        task = fixtures.MoveTask.objects.get(payload__fbs_plan_id=plan.id)
        self.assertEqual(task.from_zone, 'PR')

    def test_receiving_whole_box_moves_to_fbs_once_and_conserves_quantity(self):
        source = self.receiving_box('RECEIVING-MOVE')
        self.ready_box('READY-REMAINING')
        request = self.request_boxes(1)
        approve_client_movement_by_manager(request_id=request.id, reviewed_by=self.manager)
        plan = self._approve_and_accept(request).plans[0]
        task = fixtures.MoveTask.objects.get(payload__fbs_plan_id=plan.id)
        self.assertEqual(task.from_zone, 'PR')
        fixtures.take_move_task(legacy_order_id=task.legacy_order_id, user=self.driver,
                               employee_id=self.driver_employee.id,
                               employee_name=self.driver_employee.full_name)
        for scan in (self.ready_pallet.container_code, source.container_code, self.free_os_location.location_code):
            result = fixtures.scan_move_task_step(
                legacy_order_id=task.legacy_order_id, scan_value=scan, user=self.driver,
                employee_id=self.driver_employee.id, employee_name=self.driver_employee.full_name,
            )
            self.assertTrue(result.ok, result.error)
        task.refresh_from_db()
        source.refresh_from_db()
        self.assertEqual(task.status, fixtures.MoveTask.STATUS_DONE)
        self.assertEqual(source.qty, 0)
        self.assertEqual(sum(FbsStockBalance.objects.values_list('qty', flat=True)), 5)
        self.assertEqual(WarehouseStockSnapshot.objects.get(container__container_code__startswith='READY-REMAINING').qty, 5)

    def test_85_units_are_17_whole_boxes_not_35_plus_loose_stock(self):
        for index in range(10):
            self.ready_box(f'READY-{index}')
        for index in range(7):
            self._snapshot(code=f'STORED-{index}', qty=5)
        row = self.payload()['results'][0]
        self.assertEqual(row['client_available_qty'], 85)
        self.assertEqual(row['regular_box_available_count'], 17)
        self.assertEqual(row['whole_box_available_qty'], 85)
        self.assertEqual(row['non_box_available_qty'], 0)
        self.assertEqual(row['box_options'][0]['units_per_box'], 5)
        self.assertIn('Готово после обработки', row['goods_type'])
        self.assertIn('Готово на хранении', row['goods_type'])

    def test_50_units_can_be_reserved_as_10_boxes_without_putaway(self):
        sources = [self.ready_box(f'READY-{index}') for index in range(10)]
        request = self.request_boxes(10)
        self.assertEqual(request.requested_qty, 50)
        self.assertEqual(request.requested_box_count, 10)
        self.assertEqual(WarehouseReserve.objects.filter(context_type='fbs_client_movement', context_id=str(request.id)).count(), 1)
        for source in sources:
            source.refresh_from_db()
            self.assertEqual((source.qty, source.available_qty, source.other_reserved_qty), (5, 5, 0))
            self.assertEqual(source.location_id, self.ready_location.id)
            self.assertEqual(source.warehouse_state_code, 'placed_after_processing')
        self.assertEqual(self.payload()['total'], 0)
        with self.assertRaises(ValidationError):
            create_client_movement_request(agency=self.agency, mode='box', raw_lines=[{
                'barcode': self.barcode, 'qty': 5, 'units_per_box': 5, 'box_count': 1,
            }], idempotency_key='another-request')

    def test_source_zone_in_driver_task_is_pr_and_box_moves_once(self):
        first = self.ready_box('READY-MOVE-FIRST')
        self.ready_box('READY-LEAVE-SECOND')  # Partial pallet requires physical move.
        request = self.request_boxes(1)
        approve_client_movement_by_manager(request_id=request.id, reviewed_by=self.manager)
        plan = self._approve_and_accept(request).plans[0]
        task = fixtures.MoveTask.objects.get(payload__fbs_plan_id=plan.id)
        self.assertEqual(task.from_zone, 'PR')
        fixtures.take_move_task(legacy_order_id=task.legacy_order_id, user=self.driver,
                               employee_id=self.driver_employee.id,
                               employee_name=self.driver_employee.full_name)
        for scan in (self.ready_pallet.container_code, first.container_code, self.free_os_location.location_code):
            result = fixtures.scan_move_task_step(
                legacy_order_id=task.legacy_order_id, scan_value=scan, user=self.driver,
                employee_id=self.driver_employee.id, employee_name=self.driver_employee.full_name,
            )
            self.assertTrue(result.ok, result.error)
        task.refresh_from_db()
        first.refresh_from_db()
        self.assertEqual(task.status, fixtures.MoveTask.STATUS_DONE)
        self.assertEqual(first.qty, 0)
        self.assertEqual(sum(FbsStockBalance.objects.values_list('qty', flat=True)), 5)

    @override_settings(FBS_CLIENT_FULL_PALLET_LOGICAL_POST_ON_ACCEPT=True)
    def test_complete_ready_pallet_can_follow_existing_logical_fbs_route(self):
        sources = [self.ready_box(f'READY-FULL-{index}') for index in range(2)]
        request = self.request_boxes(2)
        approve_client_movement_by_manager(request_id=request.id, reviewed_by=self.manager)
        self._approve_and_accept(request)
        request.refresh_from_db()
        self.assertEqual(request.status, FbsClientMovementRequest.STATUS_COMPLETED)
        self.assertEqual(sum(FbsStockBalance.objects.values_list('qty', flat=True)), 10)
        for source in sources:
            source.container.refresh_from_db()
            self.assertEqual(source.container.current_location_id, self.ready_location.id)

    def test_incomplete_processing_and_receiving_are_not_available_in_any_mode(self):
        self.ready_box('IN-PROCESSING', warehouse_state_code='processing_in_progress')
        self.ready_box('IN-RECEIVING', warehouse_state_code='placed_in_receiving')
        for boxes in (False, True):
            self.assertEqual(self.payload(boxes)['total'], 0)

    def test_write_path_itself_accepts_completed_processing_and_rejects_unfinished(self):
        ready = self.ready_box('DIRECT-READY')
        self.reserve_direct(ready)
        ready.refresh_from_db()
        self.assertEqual((ready.available_qty, ready.other_reserved_qty), (0, 5))
        unfinished = self.ready_box('DIRECT-UNFINISHED', warehouse_state_code='processing_in_progress')
        with self.assertRaises(WarehouseTransitionError):
            self.reserve_direct(unfinished, request_id=90002)
        unfinished.refresh_from_db()
        self.assertEqual((unfinished.available_qty, unfinished.other_reserved_qty), (5, 0))

    def test_box_guards_remain_for_reserves_operations_and_vehicles(self):
        operation = WarehouseOperation.objects.create(agency=self.agency, operation_type='processing')
        cases = [
            {'available_qty': 4, 'shipping_reserved_qty': 1},
            {'available_qty': 4, 'processing_reserved_qty': 1},
            {'available_qty': 4, 'other_reserved_qty': 1},
            {'active_operation': operation}, {'is_in_vehicle': True}, {'goods_type': 'no'},
        ]
        for index, changes in enumerate(cases):
            with self.subTest(changes=changes):
                source = self.ready_box(f'BLOCKED-{index}', **changes)
                with self.assertRaises(WarehouseTransitionError):
                    self.reserve_direct(source, request_id=91000+index)
        self.assertEqual(eligible_source_boxes(agency_id=self.agency.id, barcodes=[self.barcode]), [])
        self.assertFalse(WarehouseReserve.objects.exists())

    def test_released_shipping_zone_stock_is_available_for_fbs(self):
        source = self.ready_box(
            'RELEASED-OTG',
            warehouse_state_code='in_otg',
            zone_kind='shipping',
        )

        self.reserve_direct(source)

        source.refresh_from_db()
        self.assertEqual((source.available_qty, source.other_reserved_qty), (0, 5))

    def test_ready_item_request_also_uses_the_actual_pr_source(self):
        source = self.ready_box('READY-ITEM')
        request = create_client_movement_request(
            agency=self.agency, mode='item', raw_lines=[{'barcode': self.barcode, 'qty': 3}],
            idempotency_key='ready-item',
        )
        source.refresh_from_db()
        self.assertEqual((source.qty, source.available_qty, source.other_reserved_qty), (5, 5, 0))
        approve_client_movement_by_manager(request_id=request.id, reviewed_by=self.manager)
        plan = self._approve_and_accept(request).plans[0]
        task = fixtures.MoveTask.objects.get(payload__fbs_plan_id=plan.id)
        self.assertEqual(task.from_zone, 'PR')

    def test_current_case_reserved_storage_does_not_block_10_ready_boxes(self):
        for index in range(7):
            stored = self._snapshot(code=f'ALREADY-RESERVED-{index}', qty=5)
            self.reserve_direct(stored, request_id=92000+index)
        for index in range(10):
            self.ready_box(f'FREE-AFTER-{index}')
        row = self.payload()['results'][0]
        self.assertEqual(row['client_available_qty'], 50)
        self.assertEqual(row['regular_box_available_count'], 10)
        self.assertEqual(row['non_box_available_qty'], 0)
        self.assertEqual(row['goods_type'], 'Готово после обработки')
        request = self.request_boxes(10)
        self.assertEqual(request.requested_qty, 50)

    def test_ready_box_cannot_be_reserved_by_another_client(self):
        source = self.ready_box('OWNER-READY')
        from sku.models import Agency
        other = Agency.objects.create(agn_name='Another client')
        self.assertEqual(eligible_source_boxes(agency_id=other.id, barcodes=[self.barcode]), [])
        with self.assertRaises(WarehouseTransitionError):
            WarehouseWritePathService.reserve_for_fbs_movement(
                agency=other, request_id=93000, whole_container_ids=[source.container_id],
                allocations=[{'snapshot_id': source.id, 'request_line_id': 1, 'qty': 5,
                              'container_id': source.container_id, 'barcode': self.barcode}],
            )
        source.refresh_from_db()
        self.assertEqual(source.available_qty, 5)

    def test_query_and_object_predicates_agree_on_state_and_zone(self):
        index = 0
        for zone in ('receiving', 'processing', 'storage', 'shipping', 'transit', 'virtual'):
            for state in ('placed_after_processing', 'stored', 'in_otg', 'ready_for_loading',
                          'processing_in_progress', 'placed_in_receiving'):
                source = self.ready_box(f'PREDICATE-{index}', zone_kind=zone, warehouse_state_code=state)
                index += 1
                allowed = is_fbs_client_movement_source_stock(
                    goods_type=source.goods_type, zone_kind=zone, warehouse_state_code=state,
                    container_type='box', container_code=source.container_code, container_status='active',
                )
                self.assertEqual(allowed, WarehouseStockSnapshot.objects.filter(pk=source.id).filter(fbs_client_movement_source_stock_q()).exists())
