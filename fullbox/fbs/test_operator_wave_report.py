from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from employees.models import Employee
from fbs.models import (
    FbsBox,
    FbsHandoverBatch,
    FbsHandoverBox,
    FbsHandoverOrder,
    FbsIntegrationProfile,
    FbsOrder,
    FbsOrderItem,
    FbsOrderStockAllocation,
    FbsPallet,
    FbsPickBatch,
    FbsPickingCart,
    FbsPickTask,
    FbsStockBalance,
    FbsStorageCell,
)
from sklad.models import WarehouseLocation
from sku.models import Agency, SKU


@override_settings(FBS_MODULE_ENABLED=True)
class OperatorWaveReportTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.storekeeper = user_model.objects.create_user(
            username="storekeeper_wave_report",
            password="pwd",
        )
        Employee.objects.create(
            full_name="Кладовщик отчёта по волнам",
            user=self.storekeeper,
            role="storekeeper",
            is_active=True,
        )
        self.picker = user_model.objects.create_user(
            username="wave_picker",
            first_name="Сборщик",
            last_name="Волнов",
        )
        self.dispatcher = user_model.objects.create_user(
            username="wave_dispatcher",
            first_name="Кладовщик",
            last_name="Передающий",
        )
        self.agency = Agency.objects.create(
            agn_name="Клиент отчёта по волнам",
            short_name="Клиент волн",
        )
        self.sku = SKU.objects.create(
            agency=self.agency,
            sku_code="SKU-WAVE-1",
            name="Товар отчёта по волнам",
        )
        location = WarehouseLocation.objects.create(
            warehouse_code="MSK",
            zone_code="OS",
            zone_kind=WarehouseLocation.ZONE_KIND_STORAGE,
            row_no=92,
            section_no=1,
            tier_no=1,
            cell_no=1,
            location_code="OS-92-1-1-1",
            display_name="Адрес волнового отчёта",
        )
        cell = FbsStorageCell.objects.create(
            cell_code="FBS@WAVE-REPORT",
            location=location,
            is_active=True,
        )
        pallet = FbsPallet.objects.create(
            agency=self.agency,
            pallet_code="FBS-PAL-WAVE-REPORT",
            cell=cell,
            status=FbsPallet.STATUS_ACTIVE,
        )
        box = FbsBox.objects.create(
            agency=self.agency,
            pallet=pallet,
            box_code="FBS-BOX-WAVE-REPORT",
            status=FbsBox.STATUS_ACTIVE,
        )
        balance = FbsStockBalance.objects.create(
            agency=self.agency,
            box=box,
            sku_ref=self.sku,
            identity_key="b" * 64,
            sku_code=self.sku.sku_code,
            name=self.sku.name,
            barcode="4600000000152",
            goods_type="gv",
            qty=0,
            available_qty=0,
            reserved_qty=0,
        )
        profile = FbsIntegrationProfile.objects.create(
            agency=self.agency,
            marketplace=FbsIntegrationProfile.MARKETPLACE_WB,
            name="WB отчёт по волнам",
            external_warehouse_id="WB-WAVE-REPORT",
            is_active=True,
        )
        order = FbsOrder.objects.create(
            profile=profile,
            external_order_id="WAVE-ORDER-1001",
            internal_status=FbsOrder.STATUS_HANDED_OVER,
            marketplace_status="complete",
        )
        order_item = FbsOrderItem.objects.create(
            order=order,
            external_line_id="wave-line-1",
            external_sku="ART-WAVE-1",
            barcode="4600000000152",
            sku=self.sku,
            product_name=self.sku.name,
            quantity=2,
        )
        cart = FbsPickingCart.objects.create(
            barcode="FBS-CART-990001",
            name="Тележка отчёта 1",
            is_active=True,
        )
        picked_at = timezone.now() - timedelta(hours=2)
        self.batch = FbsPickBatch.objects.create(
            agency=self.agency,
            status=FbsPickBatch.STATUS_DONE,
            planned_qty=2,
            picked_qty=2,
            assigned_to=self.picker,
            cart=cart,
            created_by=self.storekeeper,
            started_at=picked_at - timedelta(minutes=15),
            picking_completed_at=picked_at + timedelta(minutes=5),
            completed_at=picked_at + timedelta(minutes=10),
        )
        task = FbsPickTask.objects.create(
            batch=self.batch,
            order=order,
            status=FbsPickTask.STATUS_PICKED,
            assigned_to=self.picker,
            sort_order=1,
            planned_qty=2,
            picked_qty=2,
            claimed_at=picked_at - timedelta(minutes=10),
            completed_at=picked_at + timedelta(minutes=5),
        )
        FbsOrderStockAllocation.objects.create(
            order_item=order_item,
            balance=balance,
            pick_task=task,
            picked_by=self.picker,
            qty_reserved=2,
            qty_picked=2,
            status=FbsOrderStockAllocation.STATUS_PICKED,
            picked_at=picked_at,
        )
        handover_batch = FbsHandoverBatch.objects.create(
            profile=profile,
            external_supply_id="WB-SUPPLY-WAVE-77",
            status=FbsHandoverBatch.STATUS_DISPATCHED,
            created_by=self.storekeeper,
            dispatched_by=self.dispatcher,
            dispatched_at=picked_at + timedelta(hours=1),
        )
        handover_box = FbsHandoverBox.objects.create(
            batch=handover_batch,
            qr_code="WB-TRANSPORT-BOX-WAVE-77",
            status=FbsHandoverBox.STATUS_DISPATCHED,
            scanned_by=self.dispatcher,
            scanned_at=picked_at + timedelta(minutes=45),
        )
        FbsHandoverOrder.objects.create(
            box=handover_box,
            order=order,
            added_by=self.dispatcher,
            verified_by=self.dispatcher,
            verified_at=picked_at + timedelta(minutes=50),
        )
        self.client.force_login(self.storekeeper)

    def test_storekeeper_sees_who_picked_what_and_where_it_went(self):
        before = (
            FbsPickBatch.objects.count(),
            FbsPickTask.objects.count(),
            FbsOrderStockAllocation.objects.count(),
            FbsHandoverOrder.objects.count(),
        )

        response = self.client.get(
            "/fbs/operator/waves/report/",
            {
                "agency": self.agency.id,
                "picker": self.picker.id,
                "q": "ART-WAVE-1",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Кто, когда, что собрал и куда передал")
        self.assertContains(response, f"#{self.batch.id}")
        self.assertContains(response, "Сборщик Волнов")
        self.assertContains(response, "ART-WAVE-1")
        self.assertContains(response, "4600000000152")
        self.assertContains(response, "2 шт.")
        self.assertContains(response, "Тележка отчёта 1")
        self.assertContains(response, "WAVE-ORDER-1001")
        self.assertContains(response, "WB-SUPPLY-WAVE-77")
        self.assertContains(response, "WB-TRANSPORT-BOX-WAVE-77")
        self.assertContains(response, "Передано водителю")
        self.assertContains(response, "Кладовщик Передающий")
        self.assertEqual(
            before,
            (
                FbsPickBatch.objects.count(),
                FbsPickTask.objects.count(),
                FbsOrderStockAllocation.objects.count(),
                FbsHandoverOrder.objects.count(),
            ),
        )

    def test_other_picker_filter_hides_wave_rows(self):
        response = self.client.get(
            "/fbs/operator/waves/report/",
            {"picker": self.dispatcher.id},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Собранных товаров по выбранным фильтрам нет")
        self.assertNotContains(response, "WAVE-ORDER-1001")

    def test_wave_list_has_report_link(self):
        response = self.client.get("/fbs/operator/waves/", {"status": "all"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Отчёт по волнам")
        self.assertContains(response, "/fbs/operator/waves/report/")
