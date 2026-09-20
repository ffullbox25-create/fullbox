from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from employees.models import Employee
from fbs.exceptions import FbsInventoryError
from fbs.models import (
    FbsInventorySession,
    FbsStockBalance,
    FbsStorageCell,
)
from fbs.services import (
    activate_drained_inventory,
    confirm_inventory_discrepancies,
    create_fbs_box,
    create_fbs_pallet,
    create_inventory_session,
)
from sku.models import Agency, SKU
from sklad.models import WarehouseLocation


@override_settings(
    FBS_MODULE_ENABLED=True,
    FBS_WAREHOUSE_WRITES_ENABLED=True,
    FBS_ZONE_CODE="FBS",
)
class InventoryDiscrepancyConfirmationTests(TestCase):
    def setUp(self):
        users = get_user_model()
        self.storekeeper = users.objects.create_user(username="inventory_storekeeper")
        Employee.objects.create(
            user=self.storekeeper,
            full_name="Кладовщик подтверждающий",
            role="storekeeper",
        )
        self.picker = users.objects.create_user(username="inventory_counter")
        Employee.objects.create(
            user=self.picker,
            full_name="Первый счетчик",
            role="picker",
        )
        self.outsider = users.objects.create_user(username="inventory_outsider")
        Employee.objects.create(
            user=self.outsider,
            full_name="Посторонний сотрудник",
            role="picker",
        )
        self.agency = Agency.objects.create(agn_name="Inventory confirmation client")
        self.sku = SKU.objects.create(
            agency=self.agency,
            sku_code="INV-CONFIRM-SKU",
            name="Inventory confirmation product",
        )
        location = WarehouseLocation.objects.create(
            warehouse_code="MSK",
            zone_code="FBS",
            zone_kind=WarehouseLocation.ZONE_KIND_STORAGE,
            row_no=91,
            section_no=1,
            tier_no=1,
            cell_no=1,
            location_code="FBS-INV-CONFIRM-1",
            is_storage=True,
            is_pickable=True,
        )
        cell = FbsStorageCell.objects.create(
            cell_code=location.location_code,
            location=location,
            purpose=FbsStorageCell.PURPOSE_PICK,
            client_cluster=1,
        )
        pallet = create_fbs_pallet(
            agency=self.agency,
            cell=cell,
            pallet_code="INV-CONFIRM-PALLET",
        )
        self.box = create_fbs_box(
            agency=self.agency,
            pallet=pallet,
            box_code="INV-CONFIRM-BOX",
        )
        self.balance = FbsStockBalance.objects.create(
            agency=self.agency,
            box=self.box,
            sku_ref=self.sku,
            identity_key="inventory-confirmation-stock",
            sku_code=self.sku.sku_code,
            name=self.sku.name,
            barcode="INV-CONFIRM-BARCODE",
            qty=5,
            available_qty=5,
        )
        self.session = create_inventory_session(
            scope_type=FbsInventorySession.SCOPE_BOX,
            mode=FbsInventorySession.MODE_DRAIN,
            scan_mode=FbsInventorySession.SCAN_MODE_BARCODE,
            box=self.box,
            created_by=self.storekeeper,
            managed_workflow=True,
        )
        self.session = activate_drained_inventory(session_id=self.session.id)
        self.session.status = FbsInventorySession.STATUS_RECOUNT
        self.session.first_counter = self.picker
        self.session.save(update_fields=["status", "first_counter", "updated_at"])
        self.line = self.session.lines.get()
        self.line.first_count_qty = 3
        self.line.save(update_fields=["first_count_qty", "updated_at"])

    def test_storekeeper_can_accept_first_count_and_is_audited(self):
        result = confirm_inventory_discrepancies(
            session_id=self.session.id,
            confirmed_by=self.storekeeper,
            responsibility_acknowledged=True,
        )

        self.balance.refresh_from_db()
        self.assertEqual(result.status, FbsInventorySession.STATUS_DONE)
        self.assertEqual(result.approved_by_id, self.storekeeper.id)
        self.assertEqual(self.balance.qty, 3)
        audit = result.work_events.get(action="first_count_confirmed")
        self.assertEqual(audit.actor_id, self.storekeeper.id)
        self.assertEqual(audit.payload["expected_total"], 5)
        self.assertEqual(audit.payload["confirmed_total"], 3)
        self.assertEqual(audit.payload["delta"], -2)
        self.assertTrue(audit.payload["responsibility_acknowledged"])

    def test_confirmation_requires_explicit_responsibility(self):
        with self.assertRaisesMessage(FbsInventoryError, "Подтвердите ответственность"):
            confirm_inventory_discrepancies(
                session_id=self.session.id,
                confirmed_by=self.storekeeper,
                responsibility_acknowledged=False,
            )

        self.session.refresh_from_db()
        self.balance.refresh_from_db()
        self.assertEqual(self.session.status, FbsInventorySession.STATUS_RECOUNT)
        self.assertEqual(self.balance.qty, 5)

    def test_confirmation_rejects_non_manager(self):
        with self.assertRaises(FbsInventoryError):
            confirm_inventory_discrepancies(
                session_id=self.session.id,
                confirmed_by=self.outsider,
                responsibility_acknowledged=True,
            )

    def test_confirmation_is_blocked_after_second_count_started(self):
        self.session.recount_started_at = self.session.updated_at
        self.session.save(update_fields=["recount_started_at", "updated_at"])

        with self.assertRaisesMessage(FbsInventoryError, "Повторный пересчет уже начат"):
            confirm_inventory_discrepancies(
                session_id=self.session.id,
                confirmed_by=self.storekeeper,
                responsibility_acknowledged=True,
            )

    def test_head_warehouse_sees_conflicting_counts_and_can_close_inventory(self):
        self.line.second_count_qty = 4
        self.line.save(update_fields=["second_count_qty", "updated_at"])
        self.session.status = FbsInventorySession.STATUS_APPROVAL
        self.session.second_counter = self.outsider
        self.session.save(update_fields=["status", "second_counter", "updated_at"])
        self.client.force_login(self.storekeeper)
        url = reverse("fbs:tsd_inventory_detail", args=[self.session.id])

        page = self.client.get(url)

        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "Решение начальника склада")
        self.assertContains(page, "Первый пересчёт: <strong>3</strong>", html=True)
        self.assertContains(page, "Повторный пересчёт: <strong>4</strong>", html=True)
        self.assertContains(
            page,
            f'name="final_{self.line.id}" value="4" required',
        )

        closed = self.client.post(
            url,
            {
                "action": "approve",
                f"final_{self.line.id}": "4",
            },
        )

        self.assertEqual(closed.status_code, 302)
        self.session.refresh_from_db()
        self.balance.refresh_from_db()
        self.assertEqual(self.session.status, FbsInventorySession.STATUS_DONE)
        self.assertEqual(self.session.approved_by_id, self.storekeeper.id)
        self.assertEqual(self.balance.qty, 4)
