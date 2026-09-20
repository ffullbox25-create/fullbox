from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from billing.models import BillingService
from employees.models import Employee
from sklad.models import WarehouseContainer, WarehouseLocation, WarehouseStockSnapshot
from sklad.topology import os_location_code
from sku.models import Agency, SKU, SKUBarcode

from .client_portal import create_client_movement_request
from .models import (
    FbsBox,
    FbsPallet,
    FbsReplenishmentAllocation,
    FbsReplenishmentPlan,
    FbsStockBalance,
    FbsStorageCell,
)
from .services.client_movements import accept_client_movement_request
from .services import replenishment as replenishment_service


@override_settings(
    FBS_MODULE_ENABLED=True,
    FBS_WAREHOUSE_WRITES_ENABLED=True,
    FBS_ZONE_CODE="FBS",
)
class FbsWholeBoxBatchCompletionTests(TestCase):
    def setUp(self):
        users = get_user_model()
        self.storekeeper = users.objects.create_user(username="batch-storekeeper")
        self.driver = users.objects.create_user(username="batch-driver")
        Employee.objects.create(
            user=self.storekeeper,
            full_name="Batch storekeeper",
            role="storekeeper",
        )
        Employee.objects.create(
            user=self.driver,
            full_name="Batch driver",
            role="reachtruck_driver",
        )
        self.agency = Agency.objects.create(agn_name="Batch FBS client")
        self.sku = SKU.objects.create(
            agency=self.agency,
            sku_code="BATCH-SKU",
            name="Batch product",
        )
        self.barcode = "4600000099001"
        SKUBarcode.objects.create(sku=self.sku, value=self.barcode, is_primary=True)
        self.source_location = WarehouseLocation.objects.create(
            warehouse_code="MSK",
            zone_code="STORAGE",
            zone_kind=WarehouseLocation.ZONE_KIND_STORAGE,
            row_no=51,
            section_no=1,
            tier_no=2,
            cell_no=1,
            location_code="BATCH-SOURCE",
            is_storage=True,
        )
        self.source_pallet = WarehouseContainer.objects.create(
            agency=self.agency,
            container_type=WarehouseContainer.TYPE_PALLET,
            container_code="BATCH-SOURCE-PALLET",
            current_location=self.source_location,
        )
        self.source_box = WarehouseContainer.objects.create(
            agency=self.agency,
            container_type=WarehouseContainer.TYPE_BOX,
            container_code="BATCH-SOURCE-BOX",
            parent_container=self.source_pallet,
            current_location=self.source_location,
        )
        for index, qty in enumerate((2, 3), start=1):
            WarehouseStockSnapshot.objects.create(
                agency=self.agency,
                stock_unit_type="box",
                source_context_type="receiving",
                source_context_id=f"BATCH-SOURCE-{index}",
                sku_ref=self.sku,
                sku_code=self.sku.sku_code,
                name=self.sku.name,
                barcode=self.barcode,
                goods_type="gv",
                marking_code=f"BATCH-MARK-{index}",
                qty=qty,
                available_qty=qty,
                container=self.source_box,
                container_code=self.source_box.container_code,
                location=self.source_location,
                zone_code="STORAGE",
                zone_kind=WarehouseLocation.ZONE_KIND_STORAGE,
            )
        fbs_location = WarehouseLocation.objects.create(
            warehouse_code="MSK",
            zone_code="FBS",
            zone_kind=WarehouseLocation.ZONE_KIND_STORAGE,
            row_no=51,
            section_no=1,
            tier_no=1,
            cell_no=1,
            location_code="BATCH-FBS",
            is_storage=True,
        )
        cell = FbsStorageCell.objects.create(
            cell_code="BATCH-FBS",
            location=fbs_location,
        )
        FbsPallet.objects.create(
            agency=self.agency,
            cell=cell,
            pallet_code="BATCH-FBS-PALLET",
            max_boxes=10,
        )
        self.destination = WarehouseLocation.objects.create(
            warehouse_code="MSK",
            zone_code="OS",
            zone_kind=WarehouseLocation.ZONE_KIND_STORAGE,
            row_no=6,
            section_no=2,
            tier_no=1,
            cell_no=1,
            location_code="OS-6-2-1-1",
            display_name="Batch destination",
            is_storage=True,
        )
        BillingService.objects.get_or_create(
            code="fbs_receiving_goods",
            defaults={"name": "FBS movement", "unit": "шт"},
        )
        BillingService.objects.get_or_create(
            code="fbs_movement_box",
            defaults={"name": "FBS box movement", "unit": "кор."},
        )

    def test_whole_box_selects_destination_and_refreshes_once(self):
        request_row = create_client_movement_request(
            agency=self.agency,
            mode="box",
            raw_lines=[
                {
                    "barcode": self.barcode,
                    "qty": 5,
                    "units_per_box": 5,
                }
            ],
        )
        plan = accept_client_movement_request(
            request_id=request_row.id,
            accepted_by=self.storekeeper,
        ).plans[0]
        replenishment_service.claim_replenishment_plan(
            plan_id=plan.id,
            assigned_to=self.driver,
        )
        allocations = list(
            FbsReplenishmentAllocation.objects.filter(line__plan=plan).order_by("id")
        )
        self.assertEqual(len(allocations), 2)

        with (
            patch.object(
                replenishment_service,
                "_select_replenishment_destination",
                wraps=replenishment_service._select_replenishment_destination,
            ) as select_destination,
            patch.object(
                replenishment_service,
                "_refresh_completion_status",
                wraps=replenishment_service._refresh_completion_status,
            ) as refresh_completion,
        ):
            movements = replenishment_service.complete_replenishment_box_allocations(
                allocation_ids=[row.id for row in allocations],
                source_scan=self.source_box.container_code,
                target_box_scan=os_location_code(row=6, section=2, tier=1, cell=1),
                performed_by=self.driver,
            )

        self.assertEqual(len(movements), 2)
        self.assertEqual(select_destination.call_count, 1)
        self.assertEqual(refresh_completion.call_count, 1)
        plan.refresh_from_db()
        self.source_box.refresh_from_db()
        self.assertEqual(plan.status, FbsReplenishmentPlan.STATUS_DONE)
        self.assertEqual(plan.moved_qty, 5)
        self.assertEqual(self.source_box.current_location_id, self.destination.id)
        self.assertEqual(
            sum(FbsStockBalance.objects.filter(box_id=plan.target_box_id).values_list("qty", flat=True)),
            5,
        )

        with patch.object(
            replenishment_service,
            "_select_replenishment_destination",
            wraps=replenishment_service._select_replenishment_destination,
        ) as repeated_select:
            repeated = replenishment_service.complete_replenishment_box_allocations(
                allocation_ids=[row.id for row in allocations],
                source_scan=self.source_box.container_code,
                target_box_scan=os_location_code(row=6, section=2, tier=1, cell=1),
                performed_by=self.driver,
            )
        self.assertEqual([row.id for row in repeated], [row.id for row in movements])
        self.assertEqual(repeated_select.call_count, 0)

    def test_whole_box_with_140_stock_rows_completes_as_one_batch(self):
        WarehouseStockSnapshot.objects.filter(container=self.source_box).delete()
        WarehouseStockSnapshot.objects.bulk_create(
            [
                WarehouseStockSnapshot(
                    agency=self.agency,
                    stock_unit_type="box",
                    source_context_type="receiving",
                    source_context_id=f"BATCH-LARGE-{index}",
                    sku_ref=self.sku,
                    sku_code=self.sku.sku_code,
                    name=self.sku.name,
                    barcode=self.barcode,
                    goods_type="gv",
                    marking_code=f"BATCH-LARGE-MARK-{index}",
                    qty=1,
                    available_qty=1,
                    container=self.source_box,
                    container_code=self.source_box.container_code,
                    location=self.source_location,
                    zone_code="STORAGE",
                    zone_kind=WarehouseLocation.ZONE_KIND_STORAGE,
                )
                for index in range(140)
            ]
        )
        request_row = create_client_movement_request(
            agency=self.agency,
            mode="box",
            raw_lines=[
                {
                    "barcode": self.barcode,
                    "qty": 140,
                    "units_per_box": 140,
                }
            ],
        )
        plan = accept_client_movement_request(
            request_id=request_row.id,
            accepted_by=self.storekeeper,
        ).plans[0]
        replenishment_service.claim_replenishment_plan(
            plan_id=plan.id,
            assigned_to=self.driver,
        )
        allocation_ids = list(
            FbsReplenishmentAllocation.objects.filter(line__plan=plan)
            .order_by("id")
            .values_list("id", flat=True)
        )
        self.assertEqual(len(allocation_ids), 140)

        with (
            patch.object(
                replenishment_service,
                "_select_replenishment_destination",
                wraps=replenishment_service._select_replenishment_destination,
            ) as select_destination,
            patch.object(
                replenishment_service,
                "_refresh_completion_status",
                wraps=replenishment_service._refresh_completion_status,
            ) as refresh_completion,
        ):
            movements = replenishment_service.complete_replenishment_box_allocations(
                allocation_ids=allocation_ids,
                source_scan=self.source_box.container_code,
                target_box_scan=os_location_code(row=6, section=2, tier=1, cell=1),
                performed_by=self.driver,
            )

        self.assertEqual(len(movements), 140)
        self.assertEqual(select_destination.call_count, 1)
        self.assertEqual(refresh_completion.call_count, 1)
        plan.refresh_from_db()
        self.assertEqual(plan.status, FbsReplenishmentPlan.STATUS_DONE)
        self.assertEqual(plan.moved_qty, 140)

    def test_whole_box_moves_to_new_pallet_when_planned_pallet_is_shared(self):
        request_row = create_client_movement_request(
            agency=self.agency,
            mode="box",
            raw_lines=[
                {
                    "barcode": self.barcode,
                    "qty": 5,
                    "units_per_box": 5,
                }
            ],
        )
        plan = accept_client_movement_request(
            request_id=request_row.id,
            accepted_by=self.storekeeper,
        ).plans[0]
        initial_pallet_id = plan.target_pallet_id
        FbsBox.objects.create(
            agency=self.agency,
            pallet=plan.target_pallet,
            box_code="BATCH-SHARED-TARGET",
            status=FbsBox.STATUS_PLANNED,
        )
        replenishment_service.claim_replenishment_plan(
            plan_id=plan.id,
            assigned_to=self.driver,
        )
        allocation_ids = list(
            FbsReplenishmentAllocation.objects.filter(line__plan=plan)
            .order_by("id")
            .values_list("id", flat=True)
        )

        movements = replenishment_service.complete_replenishment_box_allocations(
            allocation_ids=allocation_ids,
            source_scan=self.source_box.container_code,
            target_box_scan=os_location_code(row=6, section=2, tier=1, cell=1),
            performed_by=self.driver,
        )

        self.assertEqual(len(movements), 2)
        plan.refresh_from_db()
        self.source_box.refresh_from_db()
        self.assertEqual(plan.status, FbsReplenishmentPlan.STATUS_DONE)
        self.assertNotEqual(plan.target_pallet_id, initial_pallet_id)
        self.assertEqual(plan.target_pallet.cell.location_id, self.destination.id)
        self.assertEqual(self.source_box.current_location_id, self.destination.id)
