from __future__ import annotations

import shutil
import tempfile
from datetime import timedelta
from io import BytesIO
from pathlib import Path

from PIL import Image
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.utils import timezone

from billing.models import WarehouseServiceFact
from employees.models import Employee
from sklad.models import (
    WarehouseEvent,
    WarehouseLocation,
    WarehouseOperation,
    WarehouseReserve,
    WarehouseStockSnapshot,
)
from sku.models import Agency, SKU

from .exceptions import FbsFeatureDisabled, FbsReturnError
from .models import (
    FbsBox,
    FbsIntegrationProfile,
    FbsOrder,
    FbsOrderItem,
    FbsOrderStockAllocation,
    FbsOrderTraceability,
    FbsPallet,
    FbsReturn,
    FbsReturnUnit,
    FbsStockBalance,
    FbsStorageCell,
)
from .services.returns import (
    add_return_photo,
    complete_fbs_return,
    confirm_return_order_scan,
    inspect_return_unit,
    register_fbs_return,
    scan_return_unit,
)


@override_settings(
    ROOT_URLCONF="fbs.test_urls_returns",
    FBS_MODULE_ENABLED=True,
    FBS_WAREHOUSE_WRITES_ENABLED=True,
    FBS_BILLING_ENABLED=True,
    FBS_ZONE_CODE="FBS",
)
class FbsReturnWorkflowTests(TestCase):
    @classmethod
    def setUpClass(cls):
        cls.return_root = Path(tempfile.mkdtemp(prefix="fullbox-fbs-returns-"))
        cls.return_root_override = override_settings(FBS_RETURN_ROOT=cls.return_root)
        cls.return_root_override.enable()
        super().setUpClass()

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        cls.return_root_override.disable()
        shutil.rmtree(cls.return_root, ignore_errors=True)

    def setUp(self):
        user_model = get_user_model()
        self.storekeeper = user_model.objects.create_user(
            username="fbs_return_storekeeper",
            password="pwd",
        )
        self.picker = user_model.objects.create_user(
            username="fbs_return_picker",
            password="pwd",
        )
        Employee.objects.create(
            user=self.storekeeper,
            full_name="Кладовщик возвратов",
            role="storekeeper",
        )
        Employee.objects.create(
            user=self.picker,
            full_name="Сборщик возвратов",
            role="picker",
        )
        self.agency = Agency.objects.create(agn_name="Клиент возвратов")
        self.sku = SKU.objects.create(
            agency=self.agency,
            sku_code="RET-SKU-1",
            name="Товар возврата",
        )
        self.box = self._box(self.agency, "RET-A", 1)
        self.other_agency = Agency.objects.create(agn_name="Чужой клиент")
        self.other_box = self._box(self.other_agency, "RET-B", 2)
        self.profile = FbsIntegrationProfile.objects.create(
            agency=self.agency,
            marketplace=FbsIntegrationProfile.MARKETPLACE_WB,
            name="WB Возвраты",
            external_account_id="return-account",
            external_warehouse_id="return-warehouse",
        )

    def _box(self, agency, prefix, row):
        location = WarehouseLocation.objects.create(
            warehouse_code="MSK",
            zone_code="FBS",
            zone_kind=WarehouseLocation.ZONE_KIND_STORAGE,
            row_no=row,
            section_no=1,
            tier_no=1,
            cell_no=1,
            location_code=f"{prefix}-CELL",
            is_storage=True,
            is_pickable=True,
        )
        cell = FbsStorageCell.objects.create(
            cell_code=f"{prefix}-CELL",
            location=location,
        )
        pallet = FbsPallet.objects.create(
            agency=agency,
            cell=cell,
            pallet_code=f"{prefix}-PALLET",
            status=FbsPallet.STATUS_ACTIVE,
        )
        return FbsBox.objects.create(
            agency=agency,
            pallet=pallet,
            box_code=f"{prefix}-BOX",
            status=FbsBox.STATUS_ACTIVE,
        )

    def _picked_order(self, *, qty=2, marking_code=""):
        order = FbsOrder.objects.create(
            profile=self.profile,
            external_order_id=f"RETURN-ORDER-{FbsOrder.objects.count() + 1}",
            internal_status=FbsOrder.STATUS_DELIVERED,
        )
        item = FbsOrderItem.objects.create(
            order=order,
            external_line_id=f"RETURN-LINE-{order.id}",
            external_sku=self.sku.sku_code,
            barcode="4600000000001",
            sku=self.sku,
            product_name=self.sku.name,
            quantity=qty,
        )
        balance = FbsStockBalance.objects.create(
            agency=self.agency,
            box=self.box,
            sku_ref=self.sku,
            identity_key=f"{order.id:064x}",
            sku_code=self.sku.sku_code,
            name=self.sku.name,
            barcode=item.barcode,
            goods_type="Готовый товар",
            marking_code=marking_code,
            lot_code="LOT-RETURN",
            qty=0,
            available_qty=0,
            reserved_qty=0,
        )
        allocation = FbsOrderStockAllocation.objects.create(
            order_item=item,
            balance=balance,
            reserved_by=self.picker,
            picked_by=self.picker,
            qty_reserved=qty,
            qty_picked=qty,
            status=FbsOrderStockAllocation.STATUS_PICKED,
        )
        FbsOrderTraceability.objects.create(
            allocation=allocation,
            marking_code=marking_code,
            lot_code="LOT-RETURN",
            qty=qty,
            status=FbsOrderTraceability.STATUS_PICKED,
        )
        return order, item, balance, allocation

    def _return(self, order, qty):
        return_record = register_fbs_return(
            order_id=order.id,
            expected_qty=qty,
            created_by=self.storekeeper,
        )
        confirm_return_order_scan(
            return_id=return_record.id,
            order_scan=order.external_order_id,
            performed_by=self.storekeeper,
        )
        return return_record

    def _photo(self, name="damage.png"):
        payload = BytesIO()
        Image.new("RGB", (8, 8), color=(248, 104, 0)).save(payload, format="PNG")
        return SimpleUploadedFile(name, payload.getvalue(), content_type="image/png")

    def test_only_good_units_enter_fbs_balance_after_completed_inspection(self):
        order, _, balance, _ = self._picked_order(qty=2)
        return_record = self._return(order, 2)
        good = scan_return_unit(
            return_id=return_record.id,
            item_scan="4600000000001",
            performed_by=self.storekeeper,
        )
        damaged = scan_return_unit(
            return_id=return_record.id,
            item_scan="4600000000001",
            performed_by=self.storekeeper,
        )
        inspect_return_unit(
            unit_id=good.id,
            condition=FbsReturnUnit.CONDITION_GOOD,
            target_box_scan=self.box.box_code,
            performed_by=self.storekeeper,
        )
        add_return_photo(
            unit_id=damaged.id,
            uploaded_file=self._photo(),
            kind="product",
            performed_by=self.storekeeper,
        )
        inspect_return_unit(
            unit_id=damaged.id,
            condition=FbsReturnUnit.CONDITION_DAMAGED,
            condition_reason="Повреждена упаковка",
            performed_by=self.storekeeper,
        )

        balance.refresh_from_db()
        self.assertEqual((balance.qty, balance.available_qty), (0, 0))
        self.assertFalse(WarehouseEvent.objects.filter(event_type="fbs_return_restocked").exists())

        completed = complete_fbs_return(
            return_id=return_record.id,
            performed_by=self.storekeeper,
        )

        balance.refresh_from_db()
        order.refresh_from_db()
        self.assertEqual(completed.status, FbsReturn.STATUS_COMPLETED)
        self.assertEqual((balance.qty, balance.available_qty, balance.reserved_qty), (1, 1, 0))
        self.assertEqual(order.internal_status, FbsOrder.STATUS_RETURNED)
        self.assertEqual(
            WarehouseEvent.objects.filter(event_type="fbs_return_restocked").count(),
            1,
        )
        self.assertEqual(
            WarehouseEvent.objects.filter(event_type="fbs_return_quarantined").count(),
            1,
        )
        self.assertFalse(WarehouseStockSnapshot.objects.exists())
        self.assertFalse(WarehouseReserve.objects.exists())
        self.assertFalse(WarehouseOperation.objects.exists())
        fact = WarehouseServiceFact.objects.get(
            order_type=WarehouseServiceFact.ORDER_FBS,
            order_id=return_record.number,
        )
        self.assertEqual(fact.service.code, "fbs_return_inspection")
        self.assertEqual(int(fact.quantity), 2)

    def test_completion_is_idempotent_for_stock_events_and_billing(self):
        order, _, balance, _ = self._picked_order(qty=1)
        return_record = self._return(order, 1)
        unit = scan_return_unit(
            return_id=return_record.id,
            item_scan="4600000000001",
            performed_by=self.storekeeper,
        )
        inspect_return_unit(
            unit_id=unit.id,
            condition=FbsReturnUnit.CONDITION_GOOD,
            target_box_scan=self.box.box_code,
            performed_by=self.storekeeper,
        )

        complete_fbs_return(return_id=return_record.id, performed_by=self.storekeeper)
        complete_fbs_return(return_id=return_record.id, performed_by=self.storekeeper)

        balance.refresh_from_db()
        self.assertEqual((balance.qty, balance.available_qty), (1, 1))
        self.assertEqual(
            WarehouseEvent.objects.filter(event_type="fbs_return_restocked").count(),
            1,
        )
        self.assertEqual(
            WarehouseServiceFact.objects.filter(order_id=return_record.number).count(),
            1,
        )

    def test_damaged_and_quarantine_require_reason_and_photo(self):
        order, _, balance, _ = self._picked_order(qty=1)
        return_record = self._return(order, 1)
        unit = scan_return_unit(
            return_id=return_record.id,
            item_scan="4600000000001",
            performed_by=self.storekeeper,
        )

        with self.assertRaisesMessage(FbsReturnError, "причину"):
            inspect_return_unit(
                unit_id=unit.id,
                condition=FbsReturnUnit.CONDITION_QUARANTINE,
                performed_by=self.storekeeper,
            )
        with self.assertRaisesMessage(FbsReturnError, "фотографию"):
            inspect_return_unit(
                unit_id=unit.id,
                condition=FbsReturnUnit.CONDITION_QUARANTINE,
                condition_reason="Нужна проверка",
                performed_by=self.storekeeper,
            )
        add_return_photo(
            unit_id=unit.id,
            uploaded_file=self._photo("quarantine.png"),
            kind="package",
            performed_by=self.storekeeper,
        )
        inspect_return_unit(
            unit_id=unit.id,
            condition=FbsReturnUnit.CONDITION_QUARANTINE,
            condition_reason="Нужна проверка",
            performed_by=self.storekeeper,
        )
        complete_fbs_return(return_id=return_record.id, performed_by=self.storekeeper)

        balance.refresh_from_db()
        self.assertEqual((balance.qty, balance.available_qty), (0, 0))

    def test_kiz_must_be_scanned_exactly_and_cannot_be_returned_twice(self):
        order, _, balance, _ = self._picked_order(qty=1, marking_code="KIZ-RETURN-001")
        return_record = self._return(order, 1)

        with self.assertRaisesMessage(FbsReturnError, "не относится"):
            scan_return_unit(
                return_id=return_record.id,
                item_scan=balance.barcode,
                performed_by=self.storekeeper,
            )
        unit = scan_return_unit(
            return_id=return_record.id,
            item_scan="KIZ-RETURN-001",
            performed_by=self.storekeeper,
        )
        inspect_return_unit(
            unit_id=unit.id,
            condition=FbsReturnUnit.CONDITION_GOOD,
            target_box_scan=self.box.box_code,
            performed_by=self.storekeeper,
        )
        complete_fbs_return(return_id=return_record.id, performed_by=self.storekeeper)

        balance.refresh_from_db()
        self.assertEqual((balance.qty, balance.available_qty), (1, 1))
        with self.assertRaisesMessage(FbsReturnError, "не более 0"):
            register_fbs_return(
                order_id=order.id,
                expected_qty=1,
                created_by=self.storekeeper,
            )

    def test_order_qr_and_client_box_are_enforced(self):
        order, _, balance, _ = self._picked_order(qty=1)
        return_record = register_fbs_return(
            order_id=order.id,
            expected_qty=1,
            created_by=self.storekeeper,
        )
        with self.assertRaisesMessage(FbsReturnError, "QR заказа"):
            scan_return_unit(
                return_id=return_record.id,
                item_scan=balance.barcode,
                performed_by=self.storekeeper,
            )
        with self.assertRaisesMessage(FbsReturnError, "не совпадает"):
            confirm_return_order_scan(
                return_id=return_record.id,
                order_scan="OTHER-ORDER",
                performed_by=self.storekeeper,
            )
        confirm_return_order_scan(
            return_id=return_record.id,
            order_scan=order.external_order_id,
            performed_by=self.storekeeper,
        )
        unit = scan_return_unit(
            return_id=return_record.id,
            item_scan=balance.barcode,
            performed_by=self.storekeeper,
        )
        with self.assertRaisesMessage(FbsReturnError, "короб клиента"):
            inspect_return_unit(
                unit_id=unit.id,
                condition=FbsReturnUnit.CONDITION_GOOD,
                target_box_scan=self.other_box.box_code,
                performed_by=self.storekeeper,
            )

    def test_expired_unit_cannot_be_released_as_good(self):
        order, _, _, allocation = self._picked_order(qty=1)
        allocation.traceability.expiry_date = timezone.localdate() - timedelta(days=1)
        allocation.traceability.save(update_fields=["expiry_date", "updated_at"])
        return_record = self._return(order, 1)
        unit = scan_return_unit(
            return_id=return_record.id,
            item_scan="4600000000001",
            performed_by=self.storekeeper,
        )
        with self.assertRaisesMessage(FbsReturnError, "Срок годности истек"):
            inspect_return_unit(
                unit_id=unit.id,
                condition=FbsReturnUnit.CONDITION_GOOD,
                target_box_scan=self.box.box_code,
                performed_by=self.storekeeper,
            )

    def test_partial_returns_keep_order_pending_until_all_picked_units_returned(self):
        order, _, _, _ = self._picked_order(qty=2)
        first_return = self._return(order, 1)
        first_unit = scan_return_unit(
            return_id=first_return.id,
            item_scan="4600000000001",
            performed_by=self.storekeeper,
        )
        inspect_return_unit(
            unit_id=first_unit.id,
            condition=FbsReturnUnit.CONDITION_GOOD,
            target_box_scan=self.box.box_code,
            performed_by=self.storekeeper,
        )
        complete_fbs_return(return_id=first_return.id, performed_by=self.storekeeper)
        order.refresh_from_db()
        self.assertEqual(order.internal_status, FbsOrder.STATUS_RETURN_PENDING)

        second_return = self._return(order, 1)
        second_unit = scan_return_unit(
            return_id=second_return.id,
            item_scan="4600000000001",
            performed_by=self.storekeeper,
        )
        inspect_return_unit(
            unit_id=second_unit.id,
            condition=FbsReturnUnit.CONDITION_GOOD,
            target_box_scan=self.box.box_code,
            performed_by=self.storekeeper,
        )
        complete_fbs_return(return_id=second_return.id, performed_by=self.storekeeper)
        order.refresh_from_db()
        self.assertEqual(order.internal_status, FbsOrder.STATUS_RETURNED)

    @override_settings(FBS_WAREHOUSE_WRITES_ENABLED=False)
    def test_write_flag_blocks_return_registration(self):
        order, _, _, _ = self._picked_order(qty=1)
        with self.assertRaises(FbsFeatureDisabled):
            register_fbs_return(
                order_id=order.id,
                expected_qty=1,
                created_by=self.storekeeper,
            )
        self.assertFalse(FbsReturn.objects.exists())

    def test_storekeeper_screen_is_desktop_and_picker_is_forbidden(self):
        order, _, _, _ = self._picked_order(qty=1)
        return_record = register_fbs_return(
            order_id=order.id,
            expected_qty=1,
            created_by=self.storekeeper,
        )
        self.client.force_login(self.storekeeper)
        listing = self.client.get("/fbs/operator/returns/")
        detail = self.client.get(f"/fbs/operator/returns/{return_record.id}/")
        dashboard = self.client.get("/fbs/tsd/storekeeper/")
        self.assertEqual(listing.status_code, 200)
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(dashboard.status_code, 200)
        self.assertContains(listing, "tsd-page-desktop")
        self.assertContains(listing, "Возвраты")
        self.assertContains(detail, return_record.number)
        self.assertContains(dashboard, "Возвраты")

        self.client.force_login(self.picker)
        forbidden = self.client.get("/fbs/operator/returns/")
        self.assertEqual(forbidden.status_code, 403)
