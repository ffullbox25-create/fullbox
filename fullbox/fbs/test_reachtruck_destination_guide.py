from django.test import TestCase, override_settings

from reachtruck.models import MoveRequest, MoveTask
from sklad.models import WarehouseContainer, WarehouseLocation
from sklad.location_occupancy import FBS_STORAGE_CONTEXT_TYPE
from sklad.topology import os_location_code
from sku.models import Agency

from .models import FbsBox, FbsPallet, FbsStockBalance, FbsStorageCell
from .services.reachtruck_bridge import (
    BRIDGE_MARKER,
    build_fbs_mobile_execution_snapshot,
)


@override_settings(FBS_MODULE_ENABLED=True, FBS_WAREHOUSE_WRITES_ENABLED=True)
class FbsReachtruckDestinationGuideTests(TestCase):
    def test_destination_guide_is_advisory_and_points_to_nearby_os_places(self):
        agency = Agency.objects.create(agn_name="ООО КЕЙЗИ")
        aggregate_os_location = WarehouseLocation.objects.create(
            warehouse_code="MSK",
            zone_code="OS",
            zone_kind=WarehouseLocation.ZONE_KIND_VIRTUAL,
            location_code="OS",
            is_active=True,
            is_storage=False,
        )
        aggregate_os_cell = FbsStorageCell.objects.create(
            cell_code="FBS@OS",
            location=aggregate_os_location,
            client_cluster=agency.id,
        )
        FbsPallet.objects.create(
            agency=agency,
            pallet_code="FBS-PALLET-LEGACY-AGGREGATE-OS",
            cell=aggregate_os_cell,
            max_boxes=10,
            status=FbsPallet.STATUS_ACTIVE,
        )
        client_location = WarehouseLocation.objects.create(
            warehouse_code="MSK",
            zone_code="OS",
            zone_kind=WarehouseLocation.ZONE_KIND_STORAGE,
            row_no=10,
            section_no=2,
            tier_no=1,
            cell_no=1,
            location_code="OS-10-2-1-1",
            is_active=True,
            is_storage=True,
        )
        nearby_free_location = WarehouseLocation.objects.create(
            warehouse_code="MSK",
            zone_code="OS",
            zone_kind=WarehouseLocation.ZONE_KIND_STORAGE,
            row_no=41,
            section_no=2,
            tier_no=1,
            cell_no=2,
            location_code="OS-41-2-1-2",
            is_active=True,
            is_storage=True,
        )
        WarehouseLocation.objects.create(
            warehouse_code="MSK",
            zone_code="OS",
            zone_kind=WarehouseLocation.ZONE_KIND_STORAGE,
            row_no=41,
            section_no=2,
            tier_no=4,
            cell_no=1,
            location_code="OS-41-2-4-1",
            is_active=True,
            is_storage=True,
        )
        occupied_location = WarehouseLocation.objects.create(
            warehouse_code="MSK",
            zone_code="OS",
            zone_kind=WarehouseLocation.ZONE_KIND_STORAGE,
            row_no=10,
            section_no=2,
            tier_no=1,
            cell_no=2,
            location_code="OS-10-2-1-2",
            is_active=True,
            is_storage=True,
        )
        WarehouseContainer.objects.create(
            agency=agency,
            container_type=WarehouseContainer.TYPE_BOX,
            container_code="FBS-STANDALONE-OCCUPIED-BOX",
            current_location=occupied_location,
        )
        planned_cell = FbsStorageCell.objects.create(
            cell_code="FBS@PLANNED-EMPTY",
            location=nearby_free_location,
            client_cluster=agency.id,
        )
        planned_container = WarehouseContainer.objects.create(
            agency=agency,
            container_type=WarehouseContainer.TYPE_PALLET,
            container_code=f"FBS-PAL-{agency.id}-PLANNED-EMPTY",
            current_location=nearby_free_location,
            status=WarehouseContainer.STATUS_ACTIVE,
            source_context_type=FBS_STORAGE_CONTEXT_TYPE,
            source_context_id=f"FBS-PAL-{agency.id}-PLANNED-EMPTY",
        )
        FbsPallet.objects.create(
            agency=agency,
            pallet_code=f"FBS-PAL-{agency.id}-PLANNED-EMPTY",
            cell=planned_cell,
            warehouse_container=planned_container,
            max_boxes=10,
            status=FbsPallet.STATUS_PLANNED,
        )
        cell = FbsStorageCell.objects.create(
            cell_code="FBS@A-10/1-1",
            location=client_location,
            client_cluster=agency.id,
        )
        client_pallet = FbsPallet.objects.create(
            agency=agency,
            pallet_code="FBS-PALLET-KEIZI",
            cell=cell,
            max_boxes=10,
            status=FbsPallet.STATUS_ACTIVE,
        )
        stocked_box = FbsBox.objects.create(
            agency=agency,
            pallet=client_pallet,
            box_code="FBS-BOX-STOCKED",
            status=FbsBox.STATUS_ACTIVE,
        )
        FbsStockBalance.objects.create(
            agency=agency,
            box=stocked_box,
            identity_key="stocked",
            sku_code="SKU-STOCKED",
            qty=2,
            available_qty=2,
            reserved_qty=0,
        )
        empty_box = FbsBox.objects.create(
            agency=agency,
            pallet=client_pallet,
            box_code="FBS-BOX-EMPTY",
            status=FbsBox.STATUS_ACTIVE,
        )
        FbsStockBalance.objects.create(
            agency=agency,
            box=empty_box,
            identity_key="empty",
            sku_code="SKU-EMPTY",
            qty=0,
            available_qty=0,
            reserved_qty=0,
        )
        request = MoveRequest.objects.create(
            agency=agency,
            destination_zone="OS",
            status=MoveRequest.STATUS_IN_PROGRESS,
        )
        task = MoveTask.objects.create(
            request=request,
            pallet_code="FBS-BOX-TEST",
            move_mode=MoveTask.MODE_BOX_FULL,
            qty_planned=5,
            status=MoveTask.STATUS_IN_PROGRESS,
            legacy_order_id="FBS-GUIDE-TEST",
            payload={
                BRIDGE_MARKER: True,
                "fbs_prepared_box_placement_task": True,
                "source_box_code": "FBS-BOX-TEST",
                "from_location": {
                    "zone": "OS",
                    "row": 10,
                    "section": 2,
                    "tier": 1,
                    "cell": 1,
                },
                "mobile_execution": {
                    "box_confirmed": True,
                    "destination_confirmed": False,
                },
            },
        )

        containers_before = WarehouseContainer.objects.count()
        snapshot = build_fbs_mobile_execution_snapshot(task)
        guide = snapshot["destination_guide"]

        self.assertEqual(snapshot["current_step"], "destination")
        self.assertTrue(guide["advisory"])
        self.assertIn("Линия A · стеллаж 10", guide["summary"])
        self.assertIn(
            os_location_code(row=10, section=2, tier=1, cell=1),
            {row["code"] for row in guide["client_places"]},
        )
        client_place = next(
            row
            for row in guide["client_places"]
            if row["code"] == os_location_code(row=10, section=2, tier=1, cell=1)
        )
        self.assertEqual(client_place["boxes"], 1)
        self.assertTrue(client_place["has_space"])
        self.assertNotIn("OS", {row["code"] for row in guide["client_places"]})
        self.assertIn(
            os_location_code(row=41, section=2, tier=1, cell=2),
            {row["code"] for row in guide["free_places"]},
        )
        self.assertEqual(
            guide["free_places"][0]["code"],
            os_location_code(row=41, section=2, tier=4, cell=1),
        )
        self.assertNotIn(
            os_location_code(row=10, section=2, tier=1, cell=2),
            {row["code"] for row in guide["free_places"]},
        )
        self.assertEqual(WarehouseContainer.objects.count(), containers_before)
        planned_container.refresh_from_db()
        self.assertEqual(planned_container.current_location_id, nearby_free_location.id)
