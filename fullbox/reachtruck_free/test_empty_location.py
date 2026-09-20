from django.contrib.auth import get_user_model
from django.test import TestCase

from employees.models import Employee
from fbs.models import FbsBox, FbsPallet, FbsStockBalance, FbsStorageCell
from reachtruck_free.empty_location import confirm_physical_empty_location
from sklad.models import WarehouseContainer, WarehouseEvent, WarehouseLocation, WarehouseStockSnapshot
from sku.models import Agency
from todo.models import Task


class PhysicalEmptyLocationTests(TestCase):
    def setUp(self):
        self.agency = Agency.objects.create(agn_name="Тестовый клиент")
        self.user = get_user_model().objects.create_user(username="empty-location-driver")
        Employee.objects.create(
            full_name="Водитель тестовый",
            user=self.user,
            role="reachtruck_driver",
            is_active=True,
        )
        Employee.objects.create(
            full_name="Начальник тестовый",
            role="head_manager",
            is_active=True,
        )
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

    def create_pallet(self, code="EMPTY-PALLET"):
        return WarehouseContainer.objects.create(
            agency=self.agency,
            container_type=WarehouseContainer.TYPE_PALLET,
            container_code=code,
            current_location=self.location,
            status=WarehouseContainer.STATUS_ACTIVE,
            source_context_type="receiving",
            source_context_id="test",
        )

    def test_confirm_archives_empty_pallet_and_writes_audit_event(self):
        pallet = self.create_pallet()

        result = confirm_physical_empty_location(
            scan_value="B-1/1-1",
            user=self.user,
            role="reachtruck_driver",
        )

        pallet.refresh_from_db()
        self.assertTrue(result.released)
        self.assertEqual(pallet.status, WarehouseContainer.STATUS_ARCHIVED)
        self.assertIsNone(pallet.current_location_id)
        event = WarehouseEvent.objects.get(event_type="physical_empty_location_released")
        self.assertEqual(event.from_location_id, self.location.id)
        self.assertTrue(event.payload["physical_empty_confirmed"])
        self.assertFalse(event.payload["stock_quantity_changed"])

    def test_live_stock_blocks_release_and_creates_verification_task(self):
        pallet = self.create_pallet("PALLET-WITH-STOCK")
        box = WarehouseContainer.objects.create(
            agency=self.agency,
            container_type=WarehouseContainer.TYPE_BOX,
            container_code="BOX-WITH-STOCK",
            parent_container=pallet,
            current_location=self.location,
            status=WarehouseContainer.STATUS_ACTIVE,
        )
        WarehouseStockSnapshot.objects.create(
            agency=self.agency,
            sku_code="SKU-1",
            qty=2,
            available_qty=2,
            container=box,
            container_code=box.container_code,
            parent_container=pallet,
            location=self.location,
            zone_code="OS",
            warehouse_state_code="stored",
        )

        result = confirm_physical_empty_location(
            scan_value="B-1/1-1",
            user=self.user,
            role="reachtruck_driver",
        )

        pallet.refresh_from_db()
        self.assertFalse(result.released)
        self.assertEqual(pallet.status, WarehouseContainer.STATUS_ACTIVE)
        self.assertEqual(pallet.current_location_id, self.location.id)
        self.assertTrue(Task.objects.filter(
            title="СРОЧНО: физически пустое место занято в системе",
        ).exists())
        self.assertTrue(WarehouseEvent.objects.filter(
            event_type="physical_empty_location_blocked",
        ).exists())

    def test_duplicate_report_reuses_open_verification_task(self):
        pallet = self.create_pallet("PALLET-WITH-RESERVE")
        WarehouseStockSnapshot.objects.create(
            agency=self.agency,
            sku_code="SKU-2",
            qty=0,
            other_reserved_qty=1,
            container=pallet,
            parent_container=pallet,
            location=self.location,
            zone_code="OS",
            warehouse_state_code="stored",
        )

        first = confirm_physical_empty_location(
            scan_value="B-1/1-1", user=self.user, role="reachtruck_driver",
        )
        second = confirm_physical_empty_location(
            scan_value="B-1/1-1", user=self.user, role="reachtruck_driver",
        )

        self.assertEqual(first.task_id, second.task_id)
        self.assertEqual(Task.objects.filter(
            title="СРОЧНО: физически пустое место занято в системе",
        ).count(), 1)

    def test_second_confirmation_reports_already_free(self):
        self.create_pallet()
        confirm_physical_empty_location(
            scan_value="B-1/1-1", user=self.user, role="reachtruck_driver",
        )

        result = confirm_physical_empty_location(
            scan_value="B-1/1-1", user=self.user, role="reachtruck_driver",
        )

        self.assertTrue(result.already_free)
        self.assertFalse(result.released)

    def test_other_role_cannot_release_location(self):
        self.create_pallet()

        with self.assertRaisesMessage(ValueError, "только водитель ричтрака"):
            confirm_physical_empty_location(
                scan_value="B-1/1-1",
                user=self.user,
                role="storekeeper",
            )

    def test_empty_fbs_box_and_pallet_are_archived_without_changing_stock(self):
        pallet = self.create_pallet("FBS-EMPTY-PALLET")
        pallet.source_context_type = "fbs_storage"
        pallet.save(update_fields=["source_context_type", "updated_at"])
        box_container = WarehouseContainer.objects.create(
            agency=self.agency,
            container_type=WarehouseContainer.TYPE_BOX,
            container_code="FBS-EMPTY-BOX",
            parent_container=pallet,
            current_location=self.location,
            status=WarehouseContainer.STATUS_ACTIVE,
            source_context_type="fbs_storage",
        )
        cell = FbsStorageCell.objects.create(
            cell_code="FBS-EMPTY-CELL",
            location=self.location,
            purpose=FbsStorageCell.PURPOSE_PICK,
            is_active=True,
        )
        fbs_pallet = FbsPallet.objects.create(
            agency=self.agency,
            pallet_code=pallet.container_code,
            cell=cell,
            warehouse_container=pallet,
            status=FbsPallet.STATUS_ACTIVE,
        )
        fbs_box = FbsBox.objects.create(
            agency=self.agency,
            pallet=fbs_pallet,
            box_code=box_container.container_code,
            source_container=box_container,
            status=FbsBox.STATUS_ACTIVE,
        )
        FbsStockBalance.objects.create(
            agency=self.agency,
            box=fbs_box,
            identity_key="empty-fbs",
            sku_code="SKU-FBS",
            qty=0,
            available_qty=0,
            reserved_qty=0,
        )

        result = confirm_physical_empty_location(
            scan_value="B-1/1-1",
            user=self.user,
            role="reachtruck_driver",
        )

        self.assertTrue(result.released)
        pallet.refresh_from_db()
        box_container.refresh_from_db()
        fbs_pallet.refresh_from_db()
        fbs_box.refresh_from_db()
        self.assertEqual(pallet.status, WarehouseContainer.STATUS_ARCHIVED)
        self.assertEqual(box_container.status, WarehouseContainer.STATUS_ARCHIVED)
        self.assertEqual(fbs_pallet.status, FbsPallet.STATUS_ARCHIVED)
        self.assertEqual(fbs_box.status, FbsBox.STATUS_ARCHIVED)
