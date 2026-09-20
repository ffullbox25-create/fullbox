from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from accountant.models import ClientLifecycle
from billing.models import BillingApplication
from employees.models import Employee
from sklad.models import WarehouseLocation, WarehouseStockSnapshot
from sku.models import Agency, SKU, SKUBarcode

from fbs.client_portal import create_client_movement_request
from fbs.models import FbsClientMovementRequest


@override_settings(ROOT_URLCONF="fbs.test_urls_billing", FBS_MODULE_ENABLED=True)
class FbsManagerBillingUiTests(TestCase):
    def setUp(self):
        self.manager_user = get_user_model().objects.create_user(username="fbs-ui-manager")
        self.manager = Employee.objects.create(
            user=self.manager_user,
            full_name="Менеджер FBS UI",
            role="manager",
        )
        self.agency = Agency.objects.create(
            agn_name="Клиент FBS UI",
            mened_user_id=self.manager_user.id,
        )
        ClientLifecycle.objects.create(
            agency=self.agency,
            status=ClientLifecycle.STATUS_ACTIVE,
        )
        self.sku = SKU.objects.create(
            agency=self.agency,
            sku_code="FBS-UI-SKU",
            name="Товар FBS UI",
        )
        barcode = "4600000007784"
        SKUBarcode.objects.create(sku=self.sku, value=barcode, is_primary=True)
        location = WarehouseLocation.objects.create(
            warehouse_code="MSK",
            zone_code="STORAGE",
            zone_kind=WarehouseLocation.ZONE_KIND_STORAGE,
            location_code="FBS-UI-STOCK",
            row_no=91,
            section_no=1,
            tier_no=1,
            cell_no=1,
        )
        WarehouseStockSnapshot.objects.create(
            agency=self.agency,
            sku_ref=self.sku,
            sku_code=self.sku.sku_code,
            name=self.sku.name,
            barcode=barcode,
            goods_type="gv",
            qty=5,
            available_qty=5,
            location=location,
            zone_code="STORAGE",
        )
        self.movement = create_client_movement_request(
            agency=self.agency,
            mode=FbsClientMovementRequest.MODE_ITEM,
            raw_lines=[{"barcode": barcode, "qty": 3}],
        )
        BillingApplication.objects.create(
            application_type=BillingApplication.TYPE_FBS,
            application_id="FBS-UI-BILLING",
            client=self.agency,
            legal_entity=self.agency,
            manager=self.manager,
            operational_status="completed",
            operational_status_label="Выполнена",
        )
        self.client.force_login(self.manager_user)

    def test_manager_can_open_fbs_movement_read_only(self):
        listing = self.client.get("/team-manager/fbs/movements/")
        detail = self.client.get(
            f"/team-manager/fbs/movements/{self.movement.id}/"
        )
        post_response = self.client.post(
            f"/team-manager/fbs/movements/{self.movement.id}/",
            {"action": "approve"},
        )

        self.assertEqual(listing.status_code, 200)
        self.assertContains(listing, self.movement.number)
        self.assertEqual(detail.status_code, 200)
        self.assertContains(detail, "Просмотр цепочки и статуса")
        self.assertNotContains(detail, "Подтвердить и передать на склад")
        self.assertNotContains(detail, "Подтвердить выполнение")
        self.assertEqual(post_response.status_code, 405)
        self.movement.refresh_from_db()
        self.assertEqual(self.movement.status, FbsClientMovementRequest.STATUS_APPROVED)

    def test_new_fbs_movement_is_visible_without_manager_action_task(self):
        listing = self.client.get("/team-manager/orders/")
        dashboard = self.client.get("/team-manager/?section=desk")

        self.assertEqual(listing.status_code, 200)
        self.assertContains(listing, self.movement.number)
        self.assertContains(listing, "FBS перемещение")
        self.assertContains(
            listing,
            f'/team-manager/fbs/movements/{self.movement.id}/',
        )
        self.assertEqual(dashboard.status_code, 200)
        self.assertNotContains(dashboard, self.movement.number)
        self.assertNotContains(
            dashboard,
            f'/team-manager/fbs/movements/{self.movement.id}/',
        )

    def test_manager_can_open_separate_fbs_billing_page(self):
        response = self.client.get("/team-manager/billing/fbs/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Биллинг FBS")
        self.assertContains(response, "FBS-UI-BILLING")
        self.assertNotContains(response, "FBO / основные услуги")
