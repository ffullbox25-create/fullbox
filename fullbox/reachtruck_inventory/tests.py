from datetime import timedelta

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from employees.models import Employee
from inventory.models import Inventory, InventoryLocation
from inventory.services import transfer_to_work
from sklad.models import WarehouseStockSnapshot
from sklad.services.warehouse_write_path import WarehouseWritePathService
from sku.models import Agency, SKU
from todo.models import Task

from .models import InventoryTask
from .services import location_scan_codes


class ReachtruckInventoryExecutionTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.manager = User.objects.create_user(username="inventory_head", password="pass")
        Employee.objects.create(user=self.manager, full_name="Head", role="head_manager", is_active=True)
        self.driver = User.objects.create_user(username="inventory_driver", password="pass")
        Employee.objects.create(user=self.driver, full_name="Reach Driver", role="reachtruck_driver", is_active=True)
        agency = Agency.objects.create(agn_name="Reach inventory client")
        sku = SKU.objects.create(agency=agency, sku_code="RINV-SKU", name="Reach inventory item", size="L")
        WarehouseWritePathService.create_receiving_placement(
            agency=agency,
            order_id="RINV-RCV",
            items=[
                {
                    "order_id": "RINV-RCV",
                    "sku_code": sku.sku_code,
                    "name": sku.name,
                    "size": sku.size,
                    "barcode": "RINV-BC",
                    "goods_type": "штучный",
                    "qty": 5,
                    "pallet_code": "RINV-PAL",
                    "box_code": "RINV-BOX",
                    "location": {"zone": "OS", "row": 3, "section": 2, "tier": 1, "cell": 1},
                }
            ],
            respect_item_location=True,
        )
        snapshot = WarehouseStockSnapshot.objects.get(sku_code=sku.sku_code)
        self.inventory = Inventory.objects.create(
            inventory_type=Inventory.TYPE_PLACES,
            created_by=self.manager,
        )
        InventoryLocation.objects.create(inventory=self.inventory, location=snapshot.location)
        transfer_to_work(self.inventory, requested_by=self.manager)
        self.task = InventoryTask.objects.get(inventory=self.inventory)
        self.client.force_login(self.driver)

    def test_driver_claims_verifies_counts_and_completes_inventory(self):
        response = self.client.post(f"/reachtruck-inventory/{self.task.id}/", {"action": "take"})
        self.assertEqual(response.status_code, 302)

        response = self.client.post(
            f"/reachtruck-inventory/{self.task.id}/",
            {"action": "verify_location", "scan_value": "WRONG-LOCATION"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Ожидается место")

        self.task.refresh_from_db()
        scan_code = sorted(location_scan_codes(self.task))[0]
        response = self.client.post(
            f"/reachtruck-inventory/{self.task.id}/",
            {"action": "verify_location", "scan_value": scan_code},
        )
        self.assertEqual(response.status_code, 302)

        line = self.inventory.lines.get()
        response = self.client.post(
            f"/reachtruck-inventory/{self.task.id}/",
            {"action": "complete", f"line_{line.id}": "3"},
        )
        self.assertEqual(response.status_code, 302)
        self.task.refresh_from_db()
        self.inventory.refresh_from_db()
        line.refresh_from_db()
        self.assertEqual(self.task.status, InventoryTask.STATUS_IN_PROGRESS)
        self.assertIsNotNone(self.task.counted_at)
        self.assertEqual(self.inventory.status, Inventory.STATUS_IN_PROGRESS)

        response = self.client.post(
            f"/reachtruck-inventory/{self.task.id}/",
            {"action": "scan_discrepancy_pallet", "scan_code": "PALLET-FACT-1"},
        )
        self.assertEqual(response.status_code, 302)
        response = self.client.post(
            f"/reachtruck-inventory/{self.task.id}/",
            {"action": "submit_discrepancy"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Отсканируйте хотя бы один короб")
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, InventoryTask.STATUS_IN_PROGRESS)
        response = self.client.post(
            f"/reachtruck-inventory/{self.task.id}/",
            {"action": "scan_discrepancy_box", "scan_code": "BOX-FACT-1"},
        )
        self.assertEqual(response.status_code, 302)
        response = self.client.post(
            f"/reachtruck-inventory/{self.task.id}/",
            {"action": "submit_discrepancy"},
        )
        self.assertEqual(response.status_code, 302)

        self.task.refresh_from_db()
        self.inventory.refresh_from_db()
        self.assertEqual(self.task.status, InventoryTask.STATUS_COMPLETED)
        self.assertEqual(self.inventory.status, Inventory.STATUS_COMPLETED)
        self.assertEqual(self.inventory.performed_by_name, "Reach Driver")
        self.assertEqual(line.actual_qty, 3)
        self.assertEqual(line.difference, -2)
        self.assertEqual(self.task.discrepancy_pallet_codes, ["PALLET-FACT-1"])
        self.assertEqual(self.task.discrepancy_box_codes, ["BOX-FACT-1"])
        self.assertEqual(self.task.discrepancy_reported_by_id, self.driver.id)
        stock = WarehouseStockSnapshot.objects.get(sku_code="RINV-SKU")
        self.assertEqual(stock.qty, 5)
        manager_task = Task.objects.get(route=f"/inventory/{self.inventory.id}/?inventory_task={self.task.id}")
        self.assertEqual(manager_task.assigned_to.user_id, self.manager.id)
        self.assertIn("PALLET-FACT-1", manager_task.description)
        self.assertIn("BOX-FACT-1", manager_task.description)
        self.client.force_login(self.manager)
        response = self.client.get(f"/inventory/{self.inventory.id}/")
        self.assertContains(response, "Отчет получен")
        self.assertContains(response, "PALLET-FACT-1")
        self.assertContains(response, "BOX-FACT-1")

    def test_matching_count_completes_without_container_scans(self):
        self.client.post(f"/reachtruck-inventory/{self.task.id}/", {"action": "take"})
        self.task.refresh_from_db()
        scan_code = sorted(location_scan_codes(self.task))[0]
        self.client.post(
            f"/reachtruck-inventory/{self.task.id}/",
            {"action": "verify_location", "scan_value": scan_code},
        )
        line = self.inventory.lines.get()

        with CaptureQueriesContext(connection) as queries:
            response = self.client.post(
                f"/reachtruck-inventory/{self.task.id}/",
                {"action": "complete", f"line_{line.id}": "5"},
            )

        self.assertEqual(response.status_code, 302)
        locked_line_reads = [
            query["sql"]
            for query in queries.captured_queries
            if query["sql"].lstrip().upper().startswith("SELECT")
            and 'FROM "inventory_line"' in query["sql"]
        ]
        self.assertTrue(locked_line_reads)
        self.assertNotIn("JOIN", locked_line_reads[0].upper())
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, InventoryTask.STATUS_COMPLETED)
        self.assertIsNone(self.task.discrepancy_reported_at)

    def test_expired_lease_returns_task_to_queue_for_another_driver(self):
        self.client.post(f"/reachtruck-inventory/{self.task.id}/", {"action": "take"})
        self.task.refresh_from_db()
        self.task.lease_expires_at = timezone.now() - timedelta(seconds=1)
        self.task.save(update_fields=["lease_expires_at"])
        other_driver = get_user_model().objects.create_user(username="inventory_driver_2", password="pass")
        Employee.objects.create(
            user=other_driver,
            full_name="Second Driver",
            role="reachtruck_driver",
            is_active=True,
        )
        self.client.force_login(other_driver)

        response = self.client.get("/reachtruck-inventory/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, f"Инвентаризация №{self.inventory.id}")
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, InventoryTask.STATUS_CREATED)
        self.assertIsNone(self.task.assigned_to_id)

        response = self.client.post(
            f"/reachtruck-inventory/{self.task.id}/",
            {"action": "take"},
        )
        self.assertEqual(response.status_code, 302)
        self.task.refresh_from_db()
        self.assertEqual(self.task.assigned_to_id, other_driver.id)
        self.assertIsNotNone(self.task.lease_expires_at)

    def test_driver_can_release_task_immediately(self):
        self.client.post(f"/reachtruck-inventory/{self.task.id}/", {"action": "take"})

        response = self.client.post(
            f"/reachtruck-inventory/{self.task.id}/",
            {"action": "release"},
        )

        self.assertRedirects(response, "/reachtruck-inventory/")
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, InventoryTask.STATUS_CREATED)
        self.assertIsNone(self.task.assigned_to_id)
        self.assertIsNone(self.task.lease_expires_at)

    def test_active_lease_cannot_be_taken_by_another_driver(self):
        self.client.post(f"/reachtruck-inventory/{self.task.id}/", {"action": "take"})
        other_driver = get_user_model().objects.create_user(
            username="inventory_driver_busy",
            password="pass",
        )
        Employee.objects.create(
            user=other_driver,
            full_name="Busy Second Driver",
            role="reachtruck_driver",
            is_active=True,
        )
        self.client.force_login(other_driver)

        response = self.client.get(f"/reachtruck-inventory/{self.task.id}/")

        self.assertEqual(response.status_code, 403)
        self.task.refresh_from_db()
        self.assertEqual(self.task.assigned_to_id, self.driver.id)

    def test_activity_heartbeat_extends_lease(self):
        self.client.post(f"/reachtruck-inventory/{self.task.id}/", {"action": "take"})
        self.task.refresh_from_db()
        short_expiry = timezone.now() + timedelta(minutes=1)
        self.task.lease_expires_at = short_expiry
        self.task.save(update_fields=["lease_expires_at"])

        response = self.client.post(
            f"/reachtruck-inventory/{self.task.id}/",
            {"action": "heartbeat"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(response.status_code, 200)
        self.task.refresh_from_db()
        self.assertGreater(self.task.lease_expires_at, short_expiry)

    def test_reachtruck_inventory_category_redirects_to_separate_app(self):
        response = self.client.get("/reachtruck/?mobile_category=inventory")
        self.assertRedirects(response, "/reachtruck-inventory/")

    def test_dashboard_keeps_box_move_entry(self):
        response = self.client.get("/reachtruck-inventory/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Перемещение коробов")
        self.assertContains(response, 'href="/reachtruck-box-move/"')

    def test_dashboard_explains_expired_lease(self):
        response = self.client.get("/reachtruck-inventory/?expired=1")

        self.assertContains(response, "20 минут не было действий")

    def test_dashboard_renders_location_as_readable_map_parts(self):
        self.task.location.location_code = "OS-3-2-1-1"
        self.task.location.display_name = "OS · Ряд 3 · Секция 2 · Ярус 1 · Ячейка 1"
        self.task.location.save(update_fields=["location_code", "display_name"])

        response = self.client.get("/reachtruck-inventory/")

        self.assertContains(response, "Место на карте")
        self.assertContains(response, "A-3/1-1")
        self.assertNotContains(response, "OS-3-2-1-1")
