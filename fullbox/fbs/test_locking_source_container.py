from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase, override_settings

from sklad.models import WarehouseContainer, WarehouseLocation
from sku.models import Agency, SKU, SKUBarcode

from .models import (
    FbsBox,
    FbsIntegrationProfile,
    FbsOrder,
    FbsOrderItem,
    FbsOrderStockAllocation,
    FbsOrderTraceability,
    FbsPallet,
    FbsPickBatch,
    FbsPickTask,
    FbsStockBalance,
    FbsStorageCell,
)
from .services.picking import (
    _archive_empty_fbs_box_after_pick,
    complete_pick_allocation,
    create_pick_batches,
)


@override_settings(
    FBS_MODULE_ENABLED=True,
    FBS_WAREHOUSE_WRITES_ENABLED=True,
    FBS_STATUS_PULL_ENABLED=False,
)
class FbsNullableSourceContainerLockingTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="fbs-null-source-picker"
        )
        self.agency = Agency.objects.create(agn_name="FBS nullable source client")
        self.profile = FbsIntegrationProfile.objects.create(
            agency=self.agency,
            marketplace=FbsIntegrationProfile.MARKETPLACE_OZON,
            name="FBS nullable source profile",
            external_warehouse_id="fbs-nullable-source-warehouse",
            is_active=True,
        )
        location = WarehouseLocation.objects.create(
            warehouse_code="MSK",
            zone_code="OS",
            zone_kind=WarehouseLocation.ZONE_KIND_STORAGE,
            row_no=72,
            section_no=1,
            tier_no=1,
            cell_no=1,
            location_code="FBS-NULL-SOURCE-LOCATION",
            is_active=True,
            is_storage=True,
            is_pickable=True,
        )
        self.cell = FbsStorageCell.objects.create(
            cell_code="FBS-NULL-SOURCE-CELL",
            location=location,
            purpose=FbsStorageCell.PURPOSE_PICK,
            is_active=True,
        )
        pallet = FbsPallet.objects.create(
            agency=self.agency,
            pallet_code="FBS-NULL-SOURCE-PALLET",
            cell=self.cell,
            max_boxes=10,
            status=FbsPallet.STATUS_ACTIVE,
        )
        self.box = FbsBox.objects.create(
            agency=self.agency,
            pallet=pallet,
            box_code="FBS-NULL-SOURCE-BOX",
            source_container=None,
            status=FbsBox.STATUS_ACTIVE,
        )
        self.sku = SKU.objects.create(
            agency=self.agency,
            sku_code="FBS-NULL-SOURCE-SKU",
            name="Товар без исходного контейнера",
        )
        self.barcode = "4600000009200"
        SKUBarcode.objects.create(
            sku=self.sku,
            value=self.barcode,
            is_primary=True,
        )
        self.balance = FbsStockBalance.objects.create(
            agency=self.agency,
            box=self.box,
            sku_ref=self.sku,
            identity_key="nullable-source",
            sku_code=self.sku.sku_code,
            name=self.sku.name,
            barcode=self.barcode,
            qty=2,
            available_qty=0,
            reserved_qty=2,
        )
        self.order = FbsOrder.objects.create(
            profile=self.profile,
            external_order_id="NULL-SOURCE-ORDER",
            internal_status=FbsOrder.STATUS_RESERVED,
        )
        item = FbsOrderItem.objects.create(
            order=self.order,
            external_line_id="NULL-SOURCE-LINE",
            external_sku=self.sku.sku_code,
            sku=self.sku,
            barcode=self.barcode,
            product_name=self.sku.name,
            quantity=2,
            requirements={},
        )
        self.allocation = FbsOrderStockAllocation.objects.create(
            order_item=item,
            balance=self.balance,
            qty_reserved=2,
            status=FbsOrderStockAllocation.STATUS_RESERVED,
            reserved_by=self.user,
        )
        FbsOrderTraceability.objects.create(
            allocation=self.allocation,
            qty=2,
            status=FbsOrderTraceability.STATUS_RESERVED,
        )

    def _run_without_nullable_lock_join(self, operation):
        lock_queries = []

        def capture_lock_query(execute, sql, params, many, context):
            if "FOR UPDATE" in sql.upper():
                lock_queries.append(sql)
            return execute(sql, params, many, context)

        with connection.execute_wrapper(capture_lock_query):
            result = operation()

        if connection.features.has_select_for_update:
            self.assertTrue(lock_queries)
        nullable_join_queries = [
            sql for sql in lock_queries if '"warehouse_container"' in sql
        ]
        self.assertEqual(nullable_join_queries, [])
        return result

    def test_create_pick_batches_locks_allocation_without_nullable_join(self):
        self.assertIsNone(self.box.source_container_id)

        batches = self._run_without_nullable_lock_join(
            lambda: create_pick_batches(order_ids=[self.order.id])
        )

        self.assertEqual(len(batches), 1)
        self.assertEqual(batches[0].planned_qty, 2)
        self.allocation.refresh_from_db()
        self.assertIsNotNone(self.allocation.pick_task_id)

    def test_complete_pick_allocation_locks_rows_without_nullable_joins(self):
        batch = create_pick_batches(order_ids=[self.order.id])[0]
        self.allocation.refresh_from_db()
        task = self.allocation.pick_task
        FbsPickBatch.objects.filter(pk=batch.id).update(
            status=FbsPickBatch.STATUS_IN_PROGRESS,
            assigned_to=self.user,
        )
        FbsPickTask.objects.filter(pk=task.id).update(
            status=FbsPickTask.STATUS_IN_PROGRESS,
            assigned_to=self.user,
        )
        FbsOrderStockAllocation.objects.filter(pk=self.allocation.id).update(
            status=FbsOrderStockAllocation.STATUS_PICKING,
        )

        completed = self._run_without_nullable_lock_join(
            lambda: complete_pick_allocation(
                allocation_id=self.allocation.id,
                cell_scan=self.cell.warehouse_location_code,
                box_scan=self.box.box_code,
                item_scan=self.barcode,
                performed_by=self.user,
            )
        )

        self.assertEqual(completed.qty_picked, 1)
        self.assertEqual(completed.status, FbsOrderStockAllocation.STATUS_PICKING)
        self.balance.refresh_from_db()
        self.assertEqual(self.balance.qty, 1)
        self.assertEqual(self.balance.reserved_qty, 1)

    def test_empty_box_is_archived_only_after_stock_and_reservations_are_gone(self):
        pallet_container = WarehouseContainer.objects.create(
            agency=self.agency,
            container_type=WarehouseContainer.TYPE_PALLET,
            container_code="FBS-EMPTY-PICK-PALLET",
            current_location=self.cell.location,
            status=WarehouseContainer.STATUS_ACTIVE,
        )
        source_container = WarehouseContainer.objects.create(
            agency=self.agency,
            container_type=WarehouseContainer.TYPE_BOX,
            container_code="FBS-EMPTY-PICK-CONTAINER",
            parent_container=pallet_container,
            current_location=self.cell.location,
            status=WarehouseContainer.STATUS_ACTIVE,
        )
        self.box.pallet.warehouse_container = pallet_container
        self.box.pallet.save(update_fields=["warehouse_container", "updated_at"])
        self.box.source_container = source_container
        self.box.save(update_fields=["source_container", "updated_at"])

        self.assertFalse(_archive_empty_fbs_box_after_pick(box_id=self.box.id))
        self.box.refresh_from_db()
        source_container.refresh_from_db()
        self.assertEqual(self.box.status, FbsBox.STATUS_ACTIVE)
        self.assertEqual(source_container.status, WarehouseContainer.STATUS_ACTIVE)

        FbsStockBalance.objects.filter(pk=self.balance.id).update(
            qty=0,
            available_qty=0,
            reserved_qty=0,
        )
        FbsOrderStockAllocation.objects.filter(pk=self.allocation.id).update(
            status=FbsOrderStockAllocation.STATUS_PICKED,
            qty_picked=2,
        )

        self.assertTrue(_archive_empty_fbs_box_after_pick(box_id=self.box.id))
        self.box.refresh_from_db()
        source_container.refresh_from_db()
        pallet_container.refresh_from_db()
        self.box.pallet.refresh_from_db()
        self.assertEqual(self.box.status, FbsBox.STATUS_ARCHIVED)
        self.assertEqual(source_container.status, WarehouseContainer.STATUS_ARCHIVED)
        self.assertIsNone(source_container.current_location_id)
        self.assertIsNone(source_container.parent_container_id)
        self.assertEqual(self.box.pallet.status, FbsPallet.STATUS_ARCHIVED)
        self.assertEqual(pallet_container.status, WarehouseContainer.STATUS_ARCHIVED)
        self.assertIsNone(pallet_container.current_location_id)

    def test_empty_box_keeps_parent_place_when_sibling_still_has_stock(self):
        pallet_container = WarehouseContainer.objects.create(
            agency=self.agency,
            container_type=WarehouseContainer.TYPE_PALLET,
            container_code="FBS-PARTIAL-PICK-PALLET",
            current_location=self.cell.location,
            status=WarehouseContainer.STATUS_ACTIVE,
        )
        source_container = WarehouseContainer.objects.create(
            agency=self.agency,
            container_type=WarehouseContainer.TYPE_BOX,
            container_code="FBS-PARTIAL-PICK-EMPTY",
            parent_container=pallet_container,
            current_location=self.cell.location,
            status=WarehouseContainer.STATUS_ACTIVE,
        )
        sibling_container = WarehouseContainer.objects.create(
            agency=self.agency,
            container_type=WarehouseContainer.TYPE_BOX,
            container_code="FBS-PARTIAL-PICK-LIVE",
            parent_container=pallet_container,
            current_location=self.cell.location,
            status=WarehouseContainer.STATUS_ACTIVE,
        )
        self.box.pallet.warehouse_container = pallet_container
        self.box.pallet.save(update_fields=["warehouse_container", "updated_at"])
        self.box.source_container = source_container
        self.box.save(update_fields=["source_container", "updated_at"])
        sibling_box = FbsBox.objects.create(
            agency=self.agency,
            pallet=self.box.pallet,
            box_code="FBS-PARTIAL-PICK-LIVE-BOX",
            source_container=sibling_container,
            status=FbsBox.STATUS_ACTIVE,
        )
        FbsStockBalance.objects.create(
            agency=self.agency,
            box=sibling_box,
            sku_ref=self.sku,
            identity_key="partial-pick-live",
            sku_code=self.sku.sku_code,
            name=self.sku.name,
            barcode=self.barcode,
            qty=1,
            available_qty=1,
            reserved_qty=0,
        )
        FbsStockBalance.objects.filter(pk=self.balance.id).update(
            qty=0,
            available_qty=0,
            reserved_qty=0,
        )
        FbsOrderStockAllocation.objects.filter(pk=self.allocation.id).update(
            status=FbsOrderStockAllocation.STATUS_PICKED,
            qty_picked=2,
        )

        self.assertTrue(_archive_empty_fbs_box_after_pick(box_id=self.box.id))

        self.box.refresh_from_db()
        source_container.refresh_from_db()
        pallet_container.refresh_from_db()
        self.box.pallet.refresh_from_db()
        self.assertEqual(self.box.status, FbsBox.STATUS_ARCHIVED)
        self.assertEqual(source_container.status, WarehouseContainer.STATUS_ARCHIVED)
        self.assertEqual(self.box.pallet.status, FbsPallet.STATUS_ACTIVE)
        self.assertEqual(pallet_container.status, WarehouseContainer.STATUS_ACTIVE)
        self.assertEqual(pallet_container.current_location_id, self.cell.location_id)
