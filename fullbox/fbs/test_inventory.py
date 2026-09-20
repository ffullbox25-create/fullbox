from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from employees.models import Employee
from fbs.exceptions import FbsInventoryError
from fbs.models import (
    FbsBox,
    FbsIntegrationProfile,
    FbsInventorySession,
    FbsOrder,
    FbsOrderItem,
    FbsPallet,
    FbsStockBalance,
    FbsStorageCell,
)
from fbs.services.inventory import (
    approve_inventory,
    create_inventory_session,
    finish_inventory_count,
    record_inventory_scan,
)
from fbs.services.picking import reserve_order_stock
from fbs.services.inventory_workflow import (
    assign_inventory,
    enqueue_shortage_inventory,
    start_self_kiz_inventory,
    start_inventory_count,
)
from sklad.models import WarehouseLocation
from sku.models import Agency, SKU, SKUBarcode


@override_settings(
    FBS_MODULE_ENABLED=True,
    FBS_WAREHOUSE_WRITES_ENABLED=True,
)
class FbsInventoryScanModeTests(TestCase):
    def setUp(self):
        self.counter = get_user_model().objects.create_user(username="inventory_counter")
        self.agency = Agency.objects.create(agn_name="Inventory test client")
        self.sku = SKU.objects.create(
            agency=self.agency,
            sku_code="INV-SKU-1",
            name="Inventory test product",
        )
        location = WarehouseLocation.objects.create(
            zone_code="FBS",
            location_code="FBS-INV-1",
            display_name="FBS inventory test cell",
            is_storage=True,
        )
        self.cell = FbsStorageCell.objects.create(cell_code="FBS-INV-1", location=location)
        self.pallet = FbsPallet.objects.create(
            agency=self.agency,
            pallet_code="FBS-INV-PALLET-1",
            cell=self.cell,
        )
        self.box = FbsBox.objects.create(
            agency=self.agency,
            pallet=self.pallet,
            box_code="FBS-INV-BOX-1",
        )

    def _balance(
        self,
        *,
        identity_key: str,
        barcode: str,
        marking_code: str = "",
        qty: int = 1,
    ) -> FbsStockBalance:
        return FbsStockBalance.objects.create(
            agency=self.agency,
            box=self.box,
            sku_ref=self.sku,
            identity_key=identity_key,
            sku_code=self.sku.sku_code,
            name=self.sku.name,
            barcode=barcode,
            marking_code=marking_code,
            qty=qty,
            available_qty=qty,
        )

    def _session(self, scan_mode: str) -> FbsInventorySession:
        return create_inventory_session(
            scope_type=FbsInventorySession.SCOPE_BOX,
            mode=FbsInventorySession.MODE_AUDIT,
            scan_mode=scan_mode,
            box=self.box,
            created_by=self.counter,
        )

    @staticmethod
    def _kiz(payload_13: str, serial: str) -> str:
        total = sum(
            int(digit) * (3 if index % 2 == 0 else 1)
            for index, digit in enumerate(reversed(payload_13))
        )
        gtin = f"{payload_13}{(10 - total % 10) % 10}"
        return f"01{gtin}21{serial}"

    def test_kiz_mode_materializes_only_marked_stock_and_requires_exact_kiz(self):
        marked = self._balance(
            identity_key="marked",
            barcode="460000000001",
            marking_code="KIZ-000001",
        )
        self._balance(identity_key="plain", barcode="460000000002", qty=3)
        session = self._session(FbsInventorySession.SCAN_MODE_KIZ)

        self.assertEqual(list(session.lines.values_list("balance_id", flat=True)), [marked.id])
        line = record_inventory_scan(
            session_id=session.id,
            scan_code="KIZ-000001",
            counted_by=self.counter,
        )
        self.assertEqual(line.first_count_qty, 1)
        with self.assertRaisesMessage(FbsInventoryError, "точный КИЗ"):
            record_inventory_scan(
                session_id=session.id,
                scan_code="460000000001",
                counted_by=self.counter,
            )
        with self.assertRaisesMessage(FbsInventoryError, "ДУБЛЬ КИЗа"):
            record_inventory_scan(
                session_id=session.id,
                scan_code="KIZ-000001",
                counted_by=self.counter,
            )
        line.refresh_from_db()
        self.assertEqual(line.first_count_qty, 1)

    def test_barcode_mode_materializes_only_unmarked_stock(self):
        plain = self._balance(identity_key="plain", barcode="460000000010", qty=3)
        self._balance(
            identity_key="marked",
            barcode="460000000011",
            marking_code="KIZ-000011",
        )
        session = self._session(FbsInventorySession.SCAN_MODE_BARCODE)

        self.assertEqual(list(session.lines.values_list("balance_id", flat=True)), [plain.id])
        record_inventory_scan(
            session_id=session.id,
            scan_code="460000000010",
            counted_by=self.counter,
        )
        line = record_inventory_scan(
            session_id=session.id,
            scan_code="460000000010",
            counted_by=self.counter,
        )
        self.assertEqual(line.first_count_qty, 2)
        with self.assertRaisesMessage(FbsInventoryError, "точный штрихкод"):
            record_inventory_scan(
                session_id=session.id,
                scan_code=self.sku.sku_code,
                counted_by=self.counter,
            )

    def test_barcode_mode_rejects_ambiguous_balance_rows(self):
        self._balance(identity_key="plain-1", barcode="460000000020", qty=2)
        self._balance(identity_key="plain-2", barcode="460000000020", qty=4)
        session = self._session(FbsInventorySession.SCAN_MODE_BARCODE)

        with self.assertRaisesMessage(FbsInventoryError, "нескольким строкам остатка"):
            record_inventory_scan(
                session_id=session.id,
                scan_code="460000000020",
                counted_by=self.counter,
            )

    def test_managed_barcode_mode_counts_marked_rows_by_product_barcode(self):
        Employee.objects.create(
            user=self.counter,
            full_name="Счетчик штрихкодов",
            role="picker",
        )
        self.sku.honest_sign = True
        self.sku.save(update_fields=["honest_sign"])
        barcode = "460000000025"
        SKUBarcode.objects.create(sku=self.sku, value=barcode, is_primary=True)
        first = self._balance(
            identity_key="managed-marked-1",
            barcode=barcode,
            marking_code="KIZ-MANAGED-1",
        )
        second = self._balance(
            identity_key="managed-marked-2",
            barcode=barcode,
            marking_code="KIZ-MANAGED-2",
        )
        session = create_inventory_session(
            scope_type=FbsInventorySession.SCOPE_BOX,
            mode=FbsInventorySession.MODE_DRAIN,
            scan_mode=FbsInventorySession.SCAN_MODE_BARCODE,
            box=self.box,
            created_by=self.counter,
            managed_workflow=True,
        )
        session.assigned_to = self.counter
        session.save(update_fields=["assigned_to", "updated_at"])
        session = start_inventory_count(
            session_id=session.id,
            counted_by=self.counter,
            place_scan=self.cell.cell_code,
            box_scan=self.box.box_code,
        )

        self.assertEqual(
            set(session.lines.values_list("balance_id", flat=True)),
            {first.id, second.id},
        )
        first_scan = record_inventory_scan(
            session_id=session.id,
            scan_code=barcode,
            counted_by=self.counter,
        )
        second_scan = record_inventory_scan(
            session_id=session.id,
            scan_code=barcode,
            counted_by=self.counter,
        )

        self.assertNotEqual(first_scan.id, second_scan.id)
        self.assertEqual(
            sum(session.lines.values_list("first_count_qty", flat=True)),
            2,
        )

        surplus = record_inventory_scan(
            session_id=session.id,
            scan_code=barcode,
            counted_by=self.counter,
        )
        self.assertEqual(surplus.balance.marking_code, "")
        self.assertEqual(surplus.first_count_qty, 1)

    def test_shortage_inventory_uses_barcode_for_marked_balance(self):
        marked = self._balance(
            identity_key="shortage-marked",
            barcode="460000000026",
            marking_code="KIZ-SHORTAGE",
        )
        issue = SimpleNamespace(
            id=91,
            allocation=SimpleNamespace(balance=marked),
            allocation_id=92,
            task=SimpleNamespace(order_id=93),
            inventory_session=None,
            save=Mock(),
        )
        fake_session = Mock(id=94)

        with (
            patch(
                "fbs.services.inventory.create_inventory_session",
                return_value=fake_session,
            ) as create_session,
            patch("fbs.services.inventory_workflow.event"),
        ):
            result = enqueue_shortage_inventory(
                issues=[issue],
                reported_by=self.counter,
                protected_quantities={},
            )

        self.assertEqual(result, (fake_session,))
        self.assertEqual(
            create_session.call_args.kwargs["scan_mode"],
            FbsInventorySession.SCAN_MODE_BARCODE,
        )

    def test_session_rejects_mode_without_matching_stock(self):
        self._balance(
            identity_key="marked",
            barcode="460000000030",
            marking_code="KIZ-000030",
        )

        with self.assertRaisesMessage(FbsInventoryError, "нет товара"):
            self._session(FbsInventorySession.SCAN_MODE_BARCODE)

    def _picker(self, username="inventory_picker"):
        user = get_user_model().objects.create_user(username=username)
        Employee.objects.create(
            user=user,
            full_name="Подборщик инвентаризации",
            role="picker",
        )
        return user

    def test_picker_starts_own_kiz_inventory_without_dispatch_task(self):
        picker = self._picker()
        self.sku.honest_sign = True
        self.sku.save(update_fields=["honest_sign"])
        self._balance(
            identity_key="self-kiz-inventory",
            barcode="4601234567893",
            qty=2,
        )

        session = start_self_kiz_inventory(
            counted_by=picker,
            place_scan=self.cell.cell_code,
            box_scan=self.box.box_code,
        )

        session.refresh_from_db()
        self.assertEqual(session.status, FbsInventorySession.STATUS_COUNTING)
        self.assertEqual(session.scan_mode, FbsInventorySession.SCAN_MODE_KIZ)
        self.assertEqual(session.scope_type, FbsInventorySession.SCOPE_BOX)
        self.assertEqual(session.assigned_to, picker)
        self.assertEqual(session.first_counter, picker)
        self.assertEqual(session.created_by, picker)
        self.assertIsNotNone(session.count_started_at)
        self.assertEqual(session.lines.count(), 1)

    def test_picker_self_inventory_rolls_back_for_wrong_cell(self):
        picker = self._picker()
        self.sku.honest_sign = True
        self.sku.save(update_fields=["honest_sign"])
        self._balance(
            identity_key="self-kiz-wrong-cell",
            barcode="4601234567893",
        )

        with self.assertRaisesMessage(FbsInventoryError, "указанную ячейку"):
            start_self_kiz_inventory(
                counted_by=picker,
                place_scan="WRONG-CELL",
                box_scan=self.box.box_code,
            )

        self.assertFalse(FbsInventorySession.objects.exists())

    def test_picker_sees_self_inventory_action_and_can_start_from_tsd(self):
        picker = self._picker()
        self.sku.honest_sign = True
        self.sku.save(update_fields=["honest_sign"])
        self._balance(
            identity_key="self-kiz-view",
            barcode="4601234567893",
        )
        self.client.force_login(picker)

        home = self.client.get(reverse("fbs:tsd_home"))
        self.assertContains(home, "Инвентаризация ЧЗ")
        inventory_page = self.client.get(reverse("fbs:tsd_inventory"))
        self.assertContains(inventory_page, "Заранее созданное задание не требуется")

        response = self.client.post(
            reverse("fbs:tsd_inventory"),
            {
                "action": "self_start",
                "place_scan": self.cell.cell_code,
                "box_scan": self.box.box_code,
            },
        )

        session = FbsInventorySession.objects.get()
        self.assertRedirects(
            response,
            reverse("fbs:tsd_inventory_detail", args=[session.id]),
        )

    def test_storekeeper_can_assign_unstarted_audit_inventory(self):
        storekeeper = get_user_model().objects.create_user(
            username="inventory_dispatcher",
        )
        Employee.objects.create(
            user=storekeeper,
            full_name="Кладовщик инвентаризации",
            role="storekeeper",
        )
        picker = self._picker("inventory_assigned_picker")
        self._balance(
            identity_key="assignable-audit",
            barcode="460000000041",
            qty=5,
        )
        session = create_inventory_session(
            scope_type=FbsInventorySession.SCOPE_BOX,
            mode=FbsInventorySession.MODE_AUDIT,
            scan_mode=FbsInventorySession.SCAN_MODE_BARCODE,
            box=self.box,
            created_by=storekeeper,
        )
        self.assertFalse(session.managed_workflow)
        self.assertIsNone(session.first_counter_id)
        self.client.force_login(storekeeper)
        url = reverse("fbs:tsd_inventory_detail", args=[session.id])

        page = self.client.get(url)
        self.assertContains(page, "Кому проверить")
        self.assertContains(page, picker.employee_profile.full_name)

        response = self.client.post(
            url,
            {"action": "assign", "assigned_to": picker.id},
        )

        self.assertRedirects(response, url)
        session.refresh_from_db()
        self.assertTrue(session.managed_workflow)
        self.assertEqual(session.assigned_to, picker)
        self.assertTrue(
            session.work_events.filter(
                action="assigned",
                actor=storekeeper,
                payload__user_id=picker.id,
            ).exists()
        )

    def test_storekeeper_cannot_assign_aggregate_client_inventory(self):
        storekeeper = get_user_model().objects.create_user(
            username="inventory_aggregate_dispatcher",
        )
        Employee.objects.create(
            user=storekeeper,
            full_name="Кладовщик сводной инвентаризации",
            role="storekeeper",
        )
        picker = self._picker("inventory_aggregate_picker")
        self._balance(
            identity_key="aggregate-client",
            barcode="460000000042",
            qty=5,
        )
        session = create_inventory_session(
            scope_type=FbsInventorySession.SCOPE_AGENCY,
            mode=FbsInventorySession.MODE_AUDIT,
            scan_mode=FbsInventorySession.SCAN_MODE_BARCODE,
            agency=self.agency,
            created_by=storekeeper,
        )

        with self.assertRaisesMessage(
            FbsInventoryError,
            "Назначить на ТСД можно только короб, паллету или ячейку",
        ):
            assign_inventory(
                session_id=session.id,
                assigned_to=picker,
                assigned_by=storekeeper,
            )

        self.client.force_login(storekeeper)
        page = self.client.get(
            reverse("fbs:tsd_inventory_detail", args=[session.id])
        )
        self.assertNotContains(page, "Кому проверить")
        self.assertContains(
            page,
            "Сводную область нельзя назначить исполнителю на ТСД",
        )

    def test_box_kiz_inventory_registers_new_codes_without_changing_total(self):
        self.sku.honest_sign = True
        self.sku.save(update_fields=["honest_sign"])
        barcode = "4601234567893"
        SKUBarcode.objects.create(sku=self.sku, value=barcode, is_primary=True)
        source = self._balance(
            identity_key="uncoded-honest-sign",
            barcode=barcode,
            qty=2,
        )
        first_kiz = self._kiz("0460123456789", "SERIAL-001")
        second_kiz = self._kiz("0460123456789", "SERIAL-002")
        session = create_inventory_session(
            scope_type=FbsInventorySession.SCOPE_BOX,
            mode=FbsInventorySession.MODE_IMMEDIATE,
            scan_mode=FbsInventorySession.SCAN_MODE_KIZ,
            box=self.box,
            created_by=self.counter,
        )

        first_line = record_inventory_scan(
            session_id=session.id,
            scan_code="]d2" + first_kiz,
            counted_by=self.counter,
        )
        second_line = record_inventory_scan(
            session_id=session.id,
            scan_code=second_kiz,
            counted_by=self.counter,
        )
        self.assertEqual(first_line.id, second_line.id)
        finish_inventory_count(session_id=session.id, counted_by=self.counter)
        approve_inventory(session_id=session.id, approved_by=self.counter)

        source.refresh_from_db()
        self.assertEqual((source.qty, source.available_qty, source.reserved_qty), (0, 0, 0))
        coded = FbsStockBalance.objects.filter(
            box=self.box,
            marking_code__gt="",
        ).order_by("marking_code")
        self.assertEqual(coded.count(), 2)
        self.assertEqual(sum(coded.values_list("qty", flat=True)), 2)
        self.assertEqual(
            set(coded.values_list("marking_code", flat=True)),
            {first_kiz, second_kiz},
        )

    def test_box_kiz_inventory_rejects_code_from_another_product(self):
        self.sku.honest_sign = True
        self.sku.save(update_fields=["honest_sign"])
        SKUBarcode.objects.create(
            sku=self.sku,
            value="4601234567893",
            is_primary=True,
        )
        self._balance(
            identity_key="uncoded-wrong-gtin",
            barcode="4601234567893",
        )
        session = self._session(FbsInventorySession.SCAN_MODE_KIZ)

        with self.assertRaisesMessage(FbsInventoryError, "другому товару"):
            record_inventory_scan(
                session_id=session.id,
                scan_code=self._kiz("0460123456790", "SERIAL-WRONG"),
                counted_by=self.counter,
            )

    def test_box_kiz_inventory_rejects_code_registered_in_another_box(self):
        self.sku.honest_sign = True
        self.sku.save(update_fields=["honest_sign"])
        barcode = "4601234567893"
        SKUBarcode.objects.create(sku=self.sku, value=barcode, is_primary=True)
        self._balance(
            identity_key="uncoded-duplicate",
            barcode=barcode,
        )
        other_location = WarehouseLocation.objects.create(
            zone_code="FBS",
            row_no=2,
            section_no=1,
            tier_no=1,
            cell_no=1,
            location_code="FBS-INV-2",
            display_name="FBS inventory other cell",
            is_storage=True,
        )
        other_cell = FbsStorageCell.objects.create(
            cell_code="FBS-INV-2",
            location=other_location,
        )
        other_pallet = FbsPallet.objects.create(
            agency=self.agency,
            pallet_code="FBS-INV-PALLET-2",
            cell=other_cell,
        )
        other_box = FbsBox.objects.create(
            agency=self.agency,
            pallet=other_pallet,
            box_code="FBS-INV-BOX-2",
        )
        kiz = self._kiz("0460123456789", "SERIAL-OTHER-BOX")
        FbsStockBalance.objects.create(
            agency=self.agency,
            box=other_box,
            sku_ref=self.sku,
            identity_key="coded-other-box",
            sku_code=self.sku.sku_code,
            name=self.sku.name,
            barcode=barcode,
            marking_code=kiz,
            qty=1,
            available_qty=1,
        )
        session = self._session(FbsInventorySession.SCAN_MODE_KIZ)

        with self.assertRaisesMessage(FbsInventoryError, "ДУБЛЬ КИЗа"):
            record_inventory_scan(
                session_id=session.id,
                scan_code=kiz,
                counted_by=self.counter,
            )

    def test_kiz_approval_rejects_aggregate_balance_with_active_reserve(self):
        self.sku.honest_sign = True
        self.sku.save(update_fields=["honest_sign"])
        barcode = "4601234567893"
        SKUBarcode.objects.create(sku=self.sku, value=barcode, is_primary=True)
        source = self._balance(
            identity_key="uncoded-reserved",
            barcode=barcode,
            qty=2,
        )
        source.available_qty = 1
        source.reserved_qty = 1
        source.save(update_fields=["available_qty", "reserved_qty", "updated_at"])
        session = create_inventory_session(
            scope_type=FbsInventorySession.SCOPE_BOX,
            mode=FbsInventorySession.MODE_IMMEDIATE,
            scan_mode=FbsInventorySession.SCAN_MODE_KIZ,
            box=self.box,
            created_by=self.counter,
        )
        for serial in ("RESERVED-1", "RESERVED-2"):
            record_inventory_scan(
                session_id=session.id,
                scan_code=self._kiz("0460123456789", serial),
                counted_by=self.counter,
            )
        finish_inventory_count(session_id=session.id, counted_by=self.counter)

        with self.assertRaisesMessage(FbsInventoryError, "активный резерв"):
            approve_inventory(session_id=session.id, approved_by=self.counter)

    def test_reservation_prefers_registered_kiz_for_honest_sign_sku(self):
        self.sku.honest_sign = True
        self.sku.save(update_fields=["honest_sign"])
        barcode = "4601234567893"
        SKUBarcode.objects.create(sku=self.sku, value=barcode, is_primary=True)
        self._balance(identity_key="plain-pick", barcode=barcode, qty=2)
        marked = self._balance(
            identity_key="marked-pick",
            barcode=barcode,
            marking_code=self._kiz("0460123456789", "PICK-FIRST"),
        )
        profile = FbsIntegrationProfile.objects.create(
            agency=self.agency,
            marketplace=FbsIntegrationProfile.MARKETPLACE_WB,
            name="Inventory picking profile",
            external_warehouse_id="inventory-picking-warehouse",
        )
        order = FbsOrder.objects.create(
            profile=profile,
            external_order_id="INVENTORY-PICK-ORDER",
        )
        FbsOrderItem.objects.create(
            order=order,
            external_line_id="INVENTORY-PICK-LINE",
            external_sku=self.sku.sku_code,
            sku=self.sku,
            product_name=self.sku.name,
            barcode=barcode,
            quantity=1,
        )

        result = reserve_order_stock(order_id=order.id, reserved_by=self.counter)

        self.assertTrue(result.reserved)
        self.assertEqual(result.allocations[0].balance_id, marked.id)
