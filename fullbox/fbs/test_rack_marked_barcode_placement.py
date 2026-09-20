from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from employees.models import Employee
from fbs.exceptions import FbsMovementError
from fbs.models import FbsBox, FbsPallet, FbsStockBalance, FbsStorageCell
from fbs.services.racks import (
    _resolve_scanned_balance,
    configure_fbs_rack,
    move_scanned_box_item_to_rack_cell,
)
from sklad.models import WarehouseContainer, WarehouseEvent, WarehouseLocation
from sku.models import Agency


@override_settings(FBS_MODULE_ENABLED=True, FBS_WAREHOUSE_WRITES_ENABLED=True)
class MarkedBarcodeRackPlacementTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="marked-rack-picker",
            password="test-password",
        )
        Employee.objects.create(
            user=self.user,
            full_name="Тестовый сборщик FBS",
            role="picker",
            is_active=True,
        )
        self.agency = Agency.objects.create(agn_name="Клиент маркированного размещения")
        self.source_location = WarehouseLocation.objects.create(
            warehouse_code="MSK",
            zone_code="OS",
            zone_kind=WarehouseLocation.ZONE_KIND_STORAGE,
            row_no=19,
            section_no=1,
            tier_no=1,
            cell_no=1,
            location_code="A-19/1-1",
            display_name="A-19/1-1",
            is_active=True,
            is_storage=True,
        )
        self.rack_location = WarehouseLocation.objects.create(
            warehouse_code="MSK",
            zone_code="PR",
            zone_kind=WarehouseLocation.ZONE_KIND_RECEIVING,
            row_no=0,
            section_no=0,
            tier_no=0,
            cell_no=0,
            location_code="PR-MARKED-TEST",
            display_name="Стеллаж маркированного теста",
            capacity_containers=10,
            is_topology_visible=False,
            is_fbs_visible=True,
            is_active=True,
        )
        self.rack = configure_fbs_rack(
            location_id=self.rack_location.id,
            cell_count=1,
            created_by=self.user,
        )
        source_cell = FbsStorageCell.objects.create(
            location=self.source_location,
            cell_code="FBS-MARKED-SOURCE",
            client_cluster=0,
        )
        pallet_container = WarehouseContainer.objects.create(
            agency=self.agency,
            container_type=WarehouseContainer.TYPE_PALLET,
            container_code="FBS-MARKED-PALLET",
            current_location=self.source_location,
            status=WarehouseContainer.STATUS_ACTIVE,
        )
        pallet = FbsPallet.objects.create(
            agency=self.agency,
            pallet_code="FBS-MARKED-PALLET",
            cell=source_cell,
            warehouse_container=pallet_container,
            status=FbsPallet.STATUS_ACTIVE,
        )
        box_container = WarehouseContainer.objects.create(
            agency=self.agency,
            container_type=WarehouseContainer.TYPE_BOX,
            container_code="FBS-MARKED-BOX",
            parent_container=pallet_container,
            current_location=self.source_location,
            status=WarehouseContainer.STATUS_ACTIVE,
        )
        self.box = FbsBox.objects.create(
            agency=self.agency,
            pallet=pallet,
            box_code="FBS-MARKED-BOX",
            source_container=box_container,
            status=FbsBox.STATUS_ACTIVE,
        )
        self.barcode = "2041107696435"
        self.first = self._marked_balance(
            identity="MARKED-ONE",
            marking_code="010460000000008821SERIAL-ONE",
        )
        self.second = self._marked_balance(
            identity="MARKED-TWO",
            marking_code="010460000000008821SERIAL-TWO",
        )

    def _marked_balance(self, *, identity: str, marking_code: str):
        return FbsStockBalance.objects.create(
            agency=self.agency,
            box=self.box,
            identity_key=identity,
            sku_code="SKU-MARKED",
            name="Маркированный товар",
            size="32",
            barcode=self.barcode,
            goods_type="gv",
            marking_code=marking_code,
            qty=1,
            available_qty=1,
            reserved_qty=0,
        )

    def test_barcode_moves_one_marked_unit_and_keeps_its_kiz(self):
        movement = move_scanned_box_item_to_rack_cell(
            source_box_scan=self.box.box_code,
            target_cell_scan="PR-MARKED-TEST-01",
            item_scan=self.barcode,
            performed_by=self.user,
            idempotency_key="marked-barcode-placement",
        )

        self.first.refresh_from_db()
        self.second.refresh_from_db()
        self.assertEqual(self.first.box_id, movement.target_binding.box_id)
        self.assertEqual(self.second.box_id, self.box.id)
        self.assertEqual(self.first.marking_code, "010460000000008821SERIAL-ONE")
        event = WarehouseEvent.objects.get(
            operation=movement.operation,
            event_type="fbs_rack_item_placed",
        )
        self.assertFalse(event.payload["marking_verified"])
        self.assertEqual(event.payload["marking_resolution_mode"], "barcode_auto_selected")

    def test_box_to_cell_screen_requests_product_barcode_without_kiz(self):
        self.client.force_login(self.user)

        response = self.client.get(reverse("fbs:tsd_picker_move_box_cell"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "КИЗ для размещения не требуется")
        self.assertContains(response, 'placeholder="ШК товара"')
        self.assertNotContains(response, "ШК или Data Matrix")

    def test_exact_kiz_scan_remains_supported_and_audited(self):
        movement = move_scanned_box_item_to_rack_cell(
            source_box_scan=self.box.box_code,
            target_cell_scan="PR-MARKED-TEST-01",
            item_scan="]d2" + self.second.marking_code,
            performed_by=self.user,
            idempotency_key="marked-exact-kiz-placement",
        )

        self.second.refresh_from_db()
        self.assertEqual(self.second.box_id, movement.target_binding.box_id)
        event = WarehouseEvent.objects.get(
            operation=movement.operation,
            event_type="fbs_rack_item_placed",
        )
        self.assertTrue(event.payload["marking_verified"])
        self.assertEqual(event.payload["marking_resolution_mode"], "exact_kiz")

    def test_other_internal_moves_still_require_exact_kiz(self):
        with self.assertRaisesMessage(FbsMovementError, "отсканируйте КИЗ"):
            _resolve_scanned_balance(
                balances=[self.first, self.second],
                item_scan=self.barcode,
            )

    def test_barcode_does_not_mix_different_lots(self):
        self.second.lot_code = "OTHER-LOT"
        self.second.save(update_fields=["lot_code", "updated_at"])

        with self.assertRaisesMessage(FbsMovementError, "несколько товаров, партий"):
            move_scanned_box_item_to_rack_cell(
                source_box_scan=self.box.box_code,
                target_cell_scan="PR-MARKED-TEST-01",
                item_scan=self.barcode,
                performed_by=self.user,
                idempotency_key="marked-mixed-lot-placement",
            )
