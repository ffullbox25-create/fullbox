from django.contrib.auth import get_user_model
from django.test import TestCase

from employees.models import Employee
from sklad.models import WarehouseStockSnapshot
from sklad.services.warehouse_write_path import WarehouseWritePathService
from sku.models import Agency
from todo.models import Task

from .models import BoxMoveOperation


class ReachtruckBoxMoveDashboardTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.driver = User.objects.create_user(username="box_driver", password="pass")
        Employee.objects.create(user=self.driver, full_name="Driver", role="reachtruck_driver", is_active=True)
        self.head_user = User.objects.create_user(username="head", password="pass")
        Employee.objects.create(user=self.head_user, full_name="Head Manager", role="head_manager", is_active=True)
        self.agency = Agency.objects.create(agn_name="Client")
        self.client.force_login(self.driver)

    def _place_box(self, *, pallet_code: str, box_code: str, barcode: str, row: int, section: int, qty: int = 5) -> None:
        WarehouseWritePathService.create_receiving_placement(
            agency=self.agency,
            order_id=f"RCV-{box_code}",
            items=[
                {
                    "order_id": f"RCV-{box_code}",
                    "sku_code": f"SKU-{box_code}",
                    "name": f"Item {box_code}",
                    "size": "0",
                    "barcode": barcode,
                    "goods_type": "gv",
                    "qty": qty,
                    "pallet_code": pallet_code,
                    "box_code": box_code,
                    "location": {"zone": "OS", "row": row, "section": section, "tier": 1, "cell": 1},
                }
            ],
            respect_item_location=True,
        )

    def test_box_move_rebinds_selected_box_to_destination_pallet(self):
        self._place_box(pallet_code="PAL-SRC", box_code="BOX-SRC-1", barcode="BC-SRC-1", row=1, section=2)
        self._place_box(pallet_code="PAL-SRC", box_code="BOX-SRC-2", barcode="BC-SRC-2", row=1, section=2)
        self._place_box(pallet_code="PAL-DEST", box_code="BOX-DEST-1", barcode="BC-DEST-1", row=2, section=3)

        self.client.post("/reachtruck-box-move/", {"action": "scan_box", "scan_value": "BOX-SRC-1"})
        self.client.post("/reachtruck-box-move/", {"action": "confirm_box", "box_code": "BOX-SRC-1"})
        self.client.post("/reachtruck-box-move/", {"action": "finish_selection"})
        self.client.post("/reachtruck-box-move/", {"action": "scan_destination_pallet", "scan_value": "PAL-DEST"})
        self.client.post("/reachtruck-box-move/", {"action": "scan_destination_box", "scan_value": "BOX-DEST-1"})
        response = self.client.post("/reachtruck-box-move/", {"action": "complete"})

        operation = BoxMoveOperation.objects.get()
        self.assertRedirects(response, f"/reachtruck-box-move/?operation={operation.id}&done=1")
        operation.refresh_from_db()
        self.assertEqual(operation.status, BoxMoveOperation.STATUS_DONE)
        moved_snapshot = WarehouseStockSnapshot.objects.get(container__container_code="BOX-SRC-1")
        self.assertEqual(moved_snapshot.parent_container.container_code, "PAL-DEST")
        remaining_snapshot = WarehouseStockSnapshot.objects.get(container__container_code="BOX-SRC-2")
        self.assertEqual(remaining_snapshot.parent_container.container_code, "PAL-SRC")

    def test_box_move_with_missing_destination_box_creates_head_manager_task(self):
        self._place_box(pallet_code="PAL-SRC", box_code="BOX-SRC-1", barcode="BC-SRC-1", row=1, section=2)
        self._place_box(pallet_code="PAL-DEST", box_code="BOX-DEST-1", barcode="BC-DEST-1", row=2, section=3)

        self.client.post("/reachtruck-box-move/", {"action": "scan_box", "scan_value": "BOX-SRC-1"})
        self.client.post("/reachtruck-box-move/", {"action": "confirm_box", "box_code": "BOX-SRC-1"})
        self.client.post("/reachtruck-box-move/", {"action": "finish_selection"})
        self.client.post("/reachtruck-box-move/", {"action": "scan_destination_pallet", "scan_value": "PAL-DEST"})
        self.client.post("/reachtruck-box-move/", {"action": "complete"})

        operation = BoxMoveOperation.objects.get()
        self.assertEqual(operation.status, BoxMoveOperation.STATUS_DONE_WITH_DISCREPANCY)
        task = Task.objects.get()
        self.assertEqual(task.priority, "urgent")
        self.assertEqual(task.assigned_to.role, "head_manager")
        self.assertIn("BOX-DEST-1", task.description)
