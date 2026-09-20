from django.contrib.auth import get_user_model
from django.test import TestCase

from employees.models import Employee
from reachtruck_inventory.models import InventoryTask
from sklad.models import WarehouseStockSnapshot
from sklad.services.warehouse_write_path import WarehouseWritePathService
from sku.models import Agency, SKU, SKUBarcode

from .models import Inventory, InventoryLocation
from .services import transfer_to_work


class InventoryWorkflowTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.manager = User.objects.create_user(username="inventory_manager", password="pass")
        Employee.objects.create(user=self.manager, full_name="Manager", role="head_manager", is_active=True)
        self.agency = Agency.objects.create(agn_name="Inventory client")
        self.sku = SKU.objects.create(
            agency=self.agency,
            sku_code="INV-SKU-1",
            name="Inventory item",
            size="M",
        )
        WarehouseWritePathService.create_receiving_placement(
            agency=self.agency,
            order_id="INV-RCV-1",
            items=[
                {
                    "order_id": "INV-RCV-1",
                    "sku_code": self.sku.sku_code,
                    "name": self.sku.name,
                    "size": self.sku.size,
                    "barcode": "INV-BC-1",
                    "goods_type": "штучный",
                    "qty": 7,
                    "pallet_code": "INV-PAL-1",
                    "box_code": "INV-BOX-1",
                    "location": {"zone": "OS", "row": 1, "section": 2, "tier": 1, "cell": 1},
                }
            ],
            respect_item_location=True,
        )
        self.snapshot = WarehouseStockSnapshot.objects.get(sku_code=self.sku.sku_code)

    def test_transfer_by_places_creates_frozen_lines_and_special_task(self):
        inventory = Inventory.objects.create(
            inventory_type=Inventory.TYPE_PLACES,
            created_by=self.manager,
        )
        InventoryLocation.objects.create(inventory=inventory, location=self.snapshot.location)

        transfer_to_work(inventory, requested_by=self.manager)

        inventory.refresh_from_db()
        self.assertEqual(inventory.status, Inventory.STATUS_PENDING)
        line = inventory.lines.get()
        self.assertEqual(line.planned_qty, 7)
        self.assertIsNone(line.actual_qty)
        self.assertEqual(line.source_snapshot_ids[0]["id"], self.snapshot.id)
        task = InventoryTask.objects.get(inventory=inventory)
        self.assertEqual(task.location_id, self.snapshot.location_id)
        self.assertEqual(task.status, InventoryTask.STATUS_CREATED)

    def test_head_manager_can_open_inventory_journal(self):
        self.client.force_login(self.manager)
        response = self.client.get("/inventory/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Инвентаризации")

    def test_head_manager_can_open_transferred_inventory_detail(self):
        inventory = Inventory.objects.create(
            inventory_type=Inventory.TYPE_PLACES,
            created_by=self.manager,
        )
        InventoryLocation.objects.create(inventory=inventory, location=self.snapshot.location)
        transfer_to_work(inventory, requested_by=self.manager)
        self.client.force_login(self.manager)

        response = self.client.get(f"/inventory/{inventory.id}/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, f"Инвентаризация №{inventory.id}")
        self.assertContains(response, "Ожидает исполнителя")
        self.assertContains(response, self.sku.sku_code)

    def test_inventory_detail_uses_stock_map_location_label(self):
        self.snapshot.location.location_code = "OS-1-2-1-1"
        self.snapshot.location.display_name = "OS · Ряд 1 · Секция 2 · Ярус 1 · Ячейка 1"
        self.snapshot.location.save(update_fields=["location_code", "display_name"])
        inventory = Inventory.objects.create(
            inventory_type=Inventory.TYPE_PLACES,
            created_by=self.manager,
        )
        InventoryLocation.objects.create(inventory=inventory, location=self.snapshot.location)
        transfer_to_work(inventory, requested_by=self.manager)
        self.client.force_login(self.manager)

        response = self.client.get(f"/inventory/{inventory.id}/")

        self.assertContains(
            response,
            "A-1/1-1",
            count=2,
        )
        self.assertNotContains(response, "OS-1-2-1-1")
        task = InventoryTask.objects.get(inventory=inventory)
        self.assertEqual(
            task.location_code,
            "A-1/1-1",
        )

    def test_sku_search_returns_exact_inventory_choice(self):
        SKUBarcode.objects.create(sku=self.sku, value="INV-SEARCH-BC", is_primary=True)
        self.client.force_login(self.manager)

        response = self.client.get("/inventory/api/sku-search/", {"q": "SEARCH-BC"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["items"][0]["id"], self.sku.id)
        self.assertIn(self.sku.sku_code, response.json()["items"][0]["label"])

    def test_create_form_uses_sku_search_and_stock_map_picker(self):
        self.client.force_login(self.manager)

        response = self.client.get("/inventory/new/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="sku-search"', html=False)
        self.assertContains(response, 'id="id_sku"', html=False)
        self.assertContains(response, "Открыть карту склада")
        self.assertContains(response, 'purpose: "inventory"', html=False)
        self.assertContains(response, "stockmap-inventory-locations-picked", html=False)
        self.assertContains(response, 'mapParams.append("selected"', html=False)
        self.assertContains(response, 'mapParams.set("return_to"', html=False)
        self.assertContains(response, 'window.location.assign("/stockmap/visual/?"', html=False)
        self.assertContains(response, "inventory_create_stockmap_draft", html=False)
        self.assertNotContains(response, "window.open(", html=False)
        self.assertNotContains(response, '<option value="full">Полная</option>', html=False)

    def test_create_by_goods_accepts_sku_selected_from_search(self):
        self.client.force_login(self.manager)

        response = self.client.post(
            "/inventory/new/",
            {
                "inventory_type": Inventory.TYPE_GOODS,
                "sku": str(self.sku.id),
                "comment": "Проверка товара",
            },
        )

        inventory = Inventory.objects.latest("id")
        self.assertRedirects(response, f"/inventory/{inventory.id}/")
        self.assertEqual(inventory.sku_id, self.sku.id)

    def test_create_by_places_accepts_location_selected_on_map(self):
        self.client.force_login(self.manager)

        response = self.client.post(
            "/inventory/new/",
            {
                "inventory_type": Inventory.TYPE_PLACES,
                "locations": [str(self.snapshot.location_id)],
                "comment": "Проверка места",
            },
        )

        inventory = Inventory.objects.latest("id")
        self.assertRedirects(response, f"/inventory/{inventory.id}/")
        self.assertEqual(
            list(inventory.scope_locations.values_list("location_id", flat=True)),
            [self.snapshot.location_id],
        )
