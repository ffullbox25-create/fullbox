from types import SimpleNamespace

from django.test import TestCase

from reachtruck_free.services import (
    SESSION_MOVE_KEY,
    complete_free_move,
    inspect_location,
)
from sklad.location_occupancy import (
    os_location_occupancy_message,
    shared_os_occupied_location_ids,
)
from sklad.models import WarehouseContainer, WarehouseLocation, WarehouseStockSnapshot
from sklad.services.warehouse_transitions import WarehouseStateCode
from sklad.services.warehouse_write_path import WarehouseWritePathService
from sku.models import Agency


class ReachtruckFreeLocationOccupancyTests(TestCase):
    def setUp(self):
        self.agency = Agency.objects.create(agn_name="Тестовый клиент")
        self.location = WarehouseLocation.objects.create(
            warehouse_code="MSK",
            zone_code="OS",
            zone_kind=WarehouseLocation.ZONE_KIND_STORAGE,
            row_no=1,
            section_no=3,
            tier_no=1,
            cell_no=1,
            location_code="B-1/1-1",
            display_name="B-1/1-1",
            is_active=True,
            is_storage=True,
        )

    def test_active_container_without_stock_is_visible_as_occupied(self):
        pallet = WarehouseContainer.objects.create(
            agency=self.agency,
            container_type=WarehouseContainer.TYPE_PALLET,
            container_code="PALLET-ORPHANED",
            current_location=self.location,
            status=WarehouseContainer.STATUS_ACTIVE,
            source_context_type="receiving",
            source_context_id="old-receiving",
        )

        message = os_location_occupancy_message(self.location)
        self.assertIn("PALLET-ORPHANED", message)
        self.assertIn(self.location.id, shared_os_occupied_location_ids())
        info = inspect_location("B-1/1-1")
        self.assertEqual(info["status_label"], "Занято")
        self.assertEqual(info["status_kind"], "blocked")
        self.assertIn(message, info["blockers"])
        status_metric = next(
            metric for metric in info["summary"] if metric["label"] == "Статус"
        )
        self.assertEqual(status_metric["value"], "Занято")
        self.assertNotEqual(status_metric["value"], message)

        pallet.delete()
        free_info = inspect_location("B-1/1-1")
        self.assertEqual(free_info["status_label"], "Пусто")
        self.assertEqual(free_info["status_kind"], "free")

    def test_pallet_with_active_box_blocks_cell_in_both_checks(self):
        pallet = WarehouseContainer.objects.create(
            agency=self.agency,
            container_type=WarehouseContainer.TYPE_PALLET,
            container_code="PALLET-PHYSICAL",
            current_location=self.location,
            status=WarehouseContainer.STATUS_ACTIVE,
        )
        WarehouseContainer.objects.create(
            agency=self.agency,
            container_type=WarehouseContainer.TYPE_BOX,
            container_code="BOX-PHYSICAL",
            parent_container=pallet,
            current_location=self.location,
            status=WarehouseContainer.STATUS_ACTIVE,
        )

        message = os_location_occupancy_message(self.location)
        self.assertIn("PALLET-PHYSICAL", message)
        self.assertIn(self.location.id, shared_os_occupied_location_ids())
        info = inspect_location("B-1/1-1")
        self.assertEqual(info["status_label"], "Занято")
        self.assertEqual(info["status_kind"], "blocked")
        self.assertIn(message, info["blockers"])


class MutableTestSession(dict):
    modified = False


class ReachtruckFreeExactAisleDestinationTests(TestCase):
    def setUp(self):
        self.agency = Agency.objects.create(agn_name="Клиент междурядья")
        self.source = WarehouseLocation.objects.create(
            warehouse_code="MSK",
            zone_code="PR",
            zone_kind=WarehouseLocation.ZONE_KIND_RECEIVING,
            location_code="PR-TEST-AISLE",
            display_name="Тестовая приемка",
            is_active=True,
        )
        self.destination = WarehouseLocation.objects.create(
            warehouse_code="MSK",
            zone_code="MR",
            zone_kind=WarehouseLocation.ZONE_KIND_STORAGE,
            location_code="C-D",
            display_name="Между рядами C-D",
            is_active=True,
            is_storage=True,
            is_pickable=True,
        )
        self.pallet = WarehouseContainer.objects.create(
            agency=self.agency,
            container_type=WarehouseContainer.TYPE_PALLET,
            container_code="TEST-CD-PALLET",
            current_location=self.source,
            status=WarehouseContainer.STATUS_ACTIVE,
        )
        self.box = WarehouseContainer.objects.create(
            agency=self.agency,
            container_type=WarehouseContainer.TYPE_BOX,
            container_code="TEST-CD-BOX",
            parent_container=self.pallet,
            current_location=self.source,
            status=WarehouseContainer.STATUS_ACTIVE,
        )
        self.snapshot = WarehouseStockSnapshot.objects.create(
            agency=self.agency,
            sku_code="TEST-CD-SKU",
            qty=5,
            available_qty=5,
            container=self.box,
            container_code=self.box.container_code,
            parent_container=self.pallet,
            location=self.source,
            zone_code="PR",
            zone_kind=WarehouseLocation.ZONE_KIND_RECEIVING,
            warehouse_state_code=WarehouseStateCode.PLACED_IN_RECEIVING.value,
        )

    def test_exact_cd_qr_completes_move_without_changing_quantity(self):
        operation = WarehouseWritePathService.start_free_pallet_relocation(
            agency=self.agency,
            pallet_code=self.pallet.container_code,
            expected_location_id=self.source.id,
        )
        session = MutableTestSession(
            {
                SESSION_MOVE_KEY: {
                    "operation_id": operation.id,
                    "pallet_code": self.pallet.container_code,
                    "agency_id": self.agency.id,
                }
            }
        )
        request = SimpleNamespace(session=session, user=None)

        result = complete_free_move(request, scan_value="C-D")

        self.assertEqual(result.operation.status, "done")
        self.assertIn("C-D", result.message)
        self.assertNotIn(SESSION_MOVE_KEY, session)
        self.snapshot.refresh_from_db()
        self.pallet.refresh_from_db()
        self.box.refresh_from_db()
        self.assertEqual(self.snapshot.location_id, self.destination.id)
        self.assertEqual(self.snapshot.zone_code, "MR")
        self.assertEqual(self.snapshot.qty, 5)
        self.assertEqual(self.snapshot.available_qty, 5)
        self.assertEqual(self.pallet.current_location_id, self.destination.id)
        self.assertEqual(self.box.current_location_id, self.destination.id)
