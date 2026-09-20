from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from employees.models import Employee
from sklad.models import WarehouseLocation, WarehouseStockSnapshot
from sku.models import Agency, Market, MarketCredential, SKU, SKUBarcode
from sku.models import SKUPhoto

from audit.models import OrderAuditEntry

from .exceptions import FbsPickingError
from .integrations.http import marketplace_credentials_status
from .models import (
    FbsBox,
    FbsClientMovementRequest,
    FbsClientMovementRequestLine,
    FbsHandoverBatch,
    FbsHandoverBox,
    FbsHandoverOrder,
    FbsIntegrationProfile,
    FbsMarketplaceMetadataTransfer,
    FbsOrder,
    FbsOrderItem,
    FbsOrderLabel,
    FbsOrderStockAllocation,
    FbsPallet,
    FbsPickBatch,
    FbsPickScanEvent,
    FbsPickTask,
    FbsReplenishmentPlan,
    FbsStockBalance,
    FbsStorageCell,
    FbsSyncCursor,
    FbsPickingCart,
    FbsWorkstation,
)
from .services import MarketplaceSyncResult, prepare_pick_queue, reserve_order_stock


@override_settings(
    ROOT_URLCONF="fbs.test_urls_returns",
    FBS_MODULE_ENABLED=True,
    FBS_WAREHOUSE_WRITES_ENABLED=True,
    FBS_ZONE_CODE="FBS",
)
class FbsOperatorUiTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.storekeeper = user_model.objects.create_user(
            username="fbs_operator_storekeeper",
            password="pwd",
        )
        self.picker = user_model.objects.create_user(
            username="fbs_operator_picker",
            password="pwd",
        )
        self.manager = user_model.objects.create_user(
            username="fbs_operator_manager",
            password="pwd",
        )
        Employee.objects.create(
            user=self.storekeeper,
            full_name="Кладовщик оператор FBS",
            role="storekeeper",
        )
        Employee.objects.create(
            user=self.picker,
            full_name="Сборщик FBS",
            role="picker",
        )
        Employee.objects.create(
            user=self.manager,
            full_name="Менеджер FBS",
            role="manager",
        )
        self.first = self._create_client_context(1, "Первый клиент")

    def _create_client_context(self, index, name):
        agency = Agency.objects.create(agn_name=name)
        sku = SKU.objects.create(
            agency=agency,
            sku_code=f"OP-SKU-{index}",
            name=f"Товар {index}",
        )
        barcode = f"OP-BARCODE-{index}"
        SKUBarcode.objects.create(sku=sku, value=barcode, is_primary=True)
        location = WarehouseLocation.objects.create(
            warehouse_code="MSK",
            zone_code="FBS",
            zone_kind=WarehouseLocation.ZONE_KIND_STORAGE,
            row_no=20 + index,
            section_no=1,
            tier_no=1,
            cell_no=1,
            location_code=f"FBS-OP-{index}",
            is_storage=True,
            is_pickable=True,
        )
        cell = FbsStorageCell.objects.create(
            cell_code=f"FBS-OP-{index}",
            location=location,
        )
        pallet = FbsPallet.objects.create(
            agency=agency,
            cell=cell,
            pallet_code=f"FBS-OP-PALLET-{index}",
        )
        box = FbsBox.objects.create(
            agency=agency,
            pallet=pallet,
            box_code=f"FBS-OP-BOX-{index}",
        )
        profile = FbsIntegrationProfile.objects.create(
            agency=agency,
            marketplace=FbsIntegrationProfile.MARKETPLACE_WB,
            name=f"WB {name}",
            external_account_id=f"operator-account-{index}",
            external_warehouse_id=f"operator-warehouse-{index}",
        )
        return {
            "agency": agency,
            "sku": sku,
            "barcode": barcode,
            "box": box,
            "profile": profile,
            "index": index,
        }

    def _create_order(self, context=None, *, suffix="1", qty=1, stock=True):
        context = context or self.first
        order = FbsOrder.objects.create(
            profile=context["profile"],
            external_order_id=f"OP-{context['index']}-{suffix}",
        )
        FbsOrderItem.objects.create(
            order=order,
            external_line_id=f"LINE-{suffix}",
            external_sku=context["sku"].sku_code,
            barcode=context["barcode"],
            sku=context["sku"],
            product_name=context["sku"].name,
            quantity=qty,
        )
        if stock:
            FbsStockBalance.objects.create(
                agency=context["agency"],
                box=context["box"],
                sku_ref=context["sku"],
                identity_key=f"{FbsStockBalance.objects.count() + 1:064x}",
                sku_code=context["sku"].sku_code,
                name=context["sku"].name,
                barcode=context["barcode"],
                qty=qty,
                available_qty=qty,
            )
        return order

    def test_operator_tabs_and_all_drilldowns_open(self):
        order = self._create_order()
        reserve_order_stock(order_id=order.id, reserved_by=self.storekeeper)
        batch = prepare_pick_queue(
            limit=25,
            order_ids=[order.id],
            max_orders_per_batch=100,
            performed_by=self.storekeeper,
        ).batches[0]
        self.client.force_login(self.storekeeper)

        urls = (
            "/fbs/tsd/storekeeper/",
            "/fbs/operator/orders/",
            "/fbs/operator/clients/",
            "/fbs/operator/movements/",
            f"/fbs/operator/orders/{order.id}/",
            f"/fbs/operator/clients/{self.first['agency'].id}/",
            "/fbs/operator/waves/",
            f"/fbs/operator/waves/{batch.id}/",
            "/fbs/operator/problems/",
            "/fbs/tsd/storekeeper/demand/",
            "/fbs/tsd/labels/",
            "/fbs/tsd/storekeeper/handover/",
            "/fbs/tsd/storekeeper/inventory/",
            "/fbs/metadata/",
        )
        for url in urls:
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)

        orders = self.client.get("/fbs/operator/orders/")
        self.assertContains(orders, "operator-nav")
        self.assertContains(orders, "tsd-page-desktop")
        self.assertContains(orders, f"/fbs/operator/orders/{order.id}/")
        self.assertContains(
            orders,
            f"/fbs/operator/clients/{self.first['agency'].id}/",
        )

    def test_picker_keeps_compact_tsd_and_cannot_open_operator_screens(self):
        self.client.force_login(self.picker)

        picking = self.client.get("/fbs/tsd/picking/")
        operator = self.client.get("/fbs/operator/orders/")

        self.assertEqual(picking.status_code, 200)
        self.assertNotContains(picking, "tsd-page-desktop")
        self.assertNotContains(picking, "operator-nav")
        self.assertEqual(operator.status_code, 403)

    def test_handover_detail_unifies_orders_labels_metadata_and_acceptance(self):
        order = self._create_order(suffix="handover", qty=2, stock=False)
        order.internal_status = FbsOrder.STATUS_READY_FOR_HANDOVER
        order.marketplace_status = "confirm"
        order.save(update_fields=["internal_status", "marketplace_status", "updated_at"])
        item = order.items.get()
        item.requirements = {"required_meta": ["marking", "expiry"]}
        item.save(update_fields=["requirements", "updated_at"])
        label = FbsOrderLabel.objects.create(
            order=order,
            marketplace=FbsIntegrationProfile.MARKETPLACE_WB,
            external_label_id="WB-LABEL-UNIFIED",
            barcode="WB-ORDER-UNIFIED",
            file_url="https://example.test/wb-label.png",
            status=FbsOrderLabel.STATUS_APPLIED,
        )
        FbsMarketplaceMetadataTransfer.objects.create(
            order_item=item,
            metadata_type=FbsMarketplaceMetadataTransfer.TYPE_MARKING_CODE,
            value="TEST-KIZ-UNIFIED",
            is_required=True,
            status=FbsMarketplaceMetadataTransfer.STATUS_CONFIRMED,
            idempotency_key="a" * 64,
        )
        FbsMarketplaceMetadataTransfer.objects.create(
            order_item=item,
            metadata_type=FbsMarketplaceMetadataTransfer.TYPE_EXPIRATION,
            value="2027-12-31",
            is_required=True,
            status=FbsMarketplaceMetadataTransfer.STATUS_CONFIRMED,
            idempotency_key="b" * 64,
        )
        batch = FbsHandoverBatch.objects.create(
            profile=self.first["profile"],
            external_supply_id="WB-SUPPLY-UNIFIED",
            created_by=self.storekeeper,
        )
        box = FbsHandoverBox.objects.create(
            batch=batch,
            qr_code="WB-BOX-UNIFIED",
            external_box_id="BOX-17",
        )
        FbsHandoverOrder.objects.create(
            box=box,
            order=order,
            added_by=self.storekeeper,
        )
        self.client.force_login(self.storekeeper)

        response = self.client.get(f"/fbs/tsd/storekeeper/handover/{batch.id}/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Карточка отгрузки FBS")
        self.assertContains(response, "WB-SUPPLY-UNIFIED")
        self.assertContains(response, "WB-BOX-UNIFIED")
        self.assertContains(response, order.external_order_id)
        self.assertContains(response, label.external_label_id)
        self.assertContains(response, "Подтверждено 2 из 2")
        self.assertContains(response, "КИЗ")
        self.assertContains(response, "Срок годности")
        self.assertContains(response, "Файлы этикеток заказов")
        self.assertContains(response, "Идентификатор поставки")
        self.assertContains(response, "Открыть реестр водителя")
        self.assertEqual(response.context["handover_order_count"], 1)
        self.assertEqual(response.context["handover_unit_count"], 2)

        manifest = self.client.get(
            f"/fbs/tsd/storekeeper/handover/{batch.id}/driver-manifest/"
        )
        self.assertEqual(manifest.status_code, 200)
        self.assertContains(manifest, "Реестр передачи FBS водителю")
        self.assertContains(manifest, "Печать / PDF")
        self.assertContains(manifest, "/static/vendor/qrcode.min.js")
        self.assertContains(manifest, 'data-qr="WB-BOX-UNIFIED"')
        self.assertContains(manifest, order.external_order_id)
        self.assertContains(manifest, "Передал")
        self.assertContains(manifest, "Водитель")
        self.assertContains(manifest, "Принял")

    def test_handover_acceptance_shows_partial_and_rejected_order_counts(self):
        accepted_order = self._create_order(suffix="accepted", stock=False)
        pending_order = self._create_order(suffix="pending", stock=False)
        FbsOrder.objects.filter(pk=accepted_order.pk).update(
            internal_status=FbsOrder.STATUS_HANDED_OVER,
            marketplace_status="sorted",
        )
        FbsOrder.objects.filter(pk=pending_order.pk).update(
            internal_status=FbsOrder.STATUS_HANDED_OVER,
            marketplace_status="confirm",
        )
        batch = FbsHandoverBatch.objects.create(
            profile=self.first["profile"],
            external_supply_id="WB-SUPPLY-ACCEPTANCE",
            status=FbsHandoverBatch.STATUS_DISPATCHED,
            created_by=self.storekeeper,
        )
        box = FbsHandoverBox.objects.create(
            batch=batch,
            qr_code="WB-BOX-ACCEPTANCE",
            status=FbsHandoverBox.STATUS_DISPATCHED,
        )
        FbsHandoverOrder.objects.create(
            box=box,
            order=accepted_order,
            added_by=self.storekeeper,
        )
        FbsHandoverOrder.objects.create(
            box=box,
            order=pending_order,
            added_by=self.storekeeper,
        )
        self.client.force_login(self.storekeeper)

        partial = self.client.get(f"/fbs/tsd/storekeeper/handover/{batch.id}/")

        self.assertEqual(partial.status_code, 200)
        self.assertContains(partial, "Принята частично")
        self.assertEqual(partial.context["handover_accepted_orders"], 1)
        self.assertEqual(partial.context["handover_rejected_orders"], 0)

        FbsOrder.objects.filter(pk__in=[accepted_order.pk, pending_order.pk]).update(
            marketplace_status="cancelled"
        )
        FbsHandoverBatch.objects.filter(pk=batch.pk).update(
            status=FbsHandoverBatch.STATUS_PROBLEM
        )
        rejected = self.client.get(f"/fbs/tsd/storekeeper/handover/{batch.id}/")

        self.assertEqual(rejected.status_code, 200)
        self.assertContains(rejected, "Отклонена")
        self.assertEqual(rejected.context["handover_accepted_orders"], 0)
        self.assertEqual(rejected.context["handover_rejected_orders"], 2)

    def test_storekeeper_dashboard_shows_sla_control_without_writes(self):
        now = timezone.now()
        overdue = self._create_order(suffix="overdue", stock=False)
        overdue.cutoff_at = now - timedelta(minutes=15)
        overdue.save(update_fields=["cutoff_at", "updated_at"])
        urgent = self._create_order(suffix="urgent", stock=False)
        urgent.cutoff_at = now + timedelta(minutes=45)
        urgent.save(update_fields=["cutoff_at", "updated_at"])
        later = self._create_order(suffix="later", stock=False)
        later.cutoff_at = now + timedelta(hours=5)
        later.save(update_fields=["cutoff_at", "updated_at"])
        self._create_order(suffix="without-cutoff", stock=False)
        canceled = self._create_order(suffix="canceled", stock=False)
        canceled.internal_status = FbsOrder.STATUS_CANCELLED
        canceled.save(update_fields=["internal_status", "updated_at"])
        self.client.force_login(self.storekeeper)

        response = self.client.get("/fbs/tsd/storekeeper/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["sla_counts"]["tracked"], 3)
        self.assertEqual(response.context["sla_counts"]["on_time"], 2)
        self.assertEqual(response.context["sla_counts"]["urgent"], 1)
        self.assertEqual(response.context["sla_counts"]["overdue"], 1)
        self.assertEqual(response.context["sla_counts"]["without_cutoff"], 1)
        self.assertEqual(response.context["sla_counts"]["risk_percent"], 33)
        self.assertEqual(response.context["daily_flow"]["received"], 5)
        self.assertEqual(response.context["daily_flow"]["canceled"], 1)
        self.assertContains(response, "Контроль SLA")
        self.assertContains(response, "До 2 часов")
        self.assertContains(response, "Просрочено")
        self.assertContains(response, "Без срока 1")

    def test_demand_dispatch_links_orders_and_filters_deadlines_without_writes(self):
        now = timezone.now()
        overdue = self._create_order(suffix="demand-overdue", qty=2, stock=False)
        overdue.cutoff_at = now - timedelta(minutes=15)
        overdue.save(update_fields=["cutoff_at", "updated_at"])
        urgent = self._create_order(suffix="demand-urgent", qty=2, stock=False)
        urgent.cutoff_at = now + timedelta(minutes=45)
        urgent.save(update_fields=["cutoff_at", "updated_at"])
        WarehouseStockSnapshot.objects.create(
            agency=self.first["agency"],
            sku_ref=self.first["sku"],
            sku_code=self.first["sku"].sku_code,
            name=self.first["sku"].name,
            barcode=self.first["barcode"],
            goods_type="готовый",
            qty=10,
            available_qty=10,
            zone_code="OS",
        )
        snapshots_before = list(
            WarehouseStockSnapshot.objects.values_list("id", "qty", "available_qty")
        )
        self.client.force_login(self.storekeeper)

        response = self.client.get(
            "/fbs/tsd/storekeeper/demand/?deadline=overdue"
        )
        search_response = self.client.get(
            f"/fbs/tsd/storekeeper/demand/?q={urgent.external_order_id}"
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["demand_rows"]), 1)
        row = response.context["demand_rows"][0]
        self.assertEqual(row.state, "ready")
        self.assertEqual(row.order_count, 1)
        self.assertEqual(row.orders[0].id, overdue.id)
        self.assertEqual(
            response.context["dispatch_summary"],
            {"rows": 1, "orders": 1, "overdue": 1, "urgent": 0},
        )
        self.assertContains(response, overdue.external_order_id)
        self.assertNotContains(response, urgent.external_order_id)
        self.assertContains(response, f"/fbs/operator/orders/{overdue.id}/")
        self.assertContains(response, "Просрочен")
        self.assertContains(response, "Затронуто заказов")
        self.assertContains(response, "Создать все предложенные планы")
        self.assertEqual(search_response.status_code, 200)
        self.assertEqual(len(search_response.context["demand_rows"]), 1)
        legacy_row = SimpleNamespace(
            agency_id=self.first["agency"].id,
            agency_name=str(self.first["agency"]),
            sku_id=self.first["sku"].id,
            sku_code=self.first["sku"].sku_code,
            sku_name=self.first["sku"].name,
            ordered_qty=4,
            fbs_qty=0,
            open_plan_qty=0,
            uncovered_qty=4,
            general_available_qty=10,
            proposed_qty=4,
            general_shortage_qty=0,
            destination_blocked_qty=0,
            target_box_id=self.first["box"].id,
            target_box_code=self.first["box"].box_code,
            minimum_qty=0,
            target_qty=0,
            auto_suggest=True,
        )
        legacy_analysis = SimpleNamespace(
            rows=(legacy_row,),
            order_count=2,
            ordered_qty=4,
            proposed_qty=4,
            blocked_qty=0,
            unmapped_line_count=0,
            unmapped_qty=0,
        )
        with patch(
            "fbs.tsd_views.analyze_replenishment_demand",
            return_value=legacy_analysis,
        ):
            legacy_response = self.client.get("/fbs/tsd/storekeeper/demand/")
        self.assertEqual(legacy_response.status_code, 200)
        self.assertEqual(
            legacy_response.context["demand_rows"][0].barcode_label,
            self.first["barcode"],
        )
        self.assertFalse(FbsReplenishmentPlan.objects.exists())
        self.assertFalse(FbsOrderStockAllocation.objects.exists())
        self.assertEqual(
            list(
                WarehouseStockSnapshot.objects.values_list(
                    "id", "qty", "available_qty"
                )
            ),
            snapshots_before,
        )

    def test_demand_mapping_filter_explains_unmapped_order_without_writes(self):
        order = self._create_order(suffix="mapping-error", stock=False)
        order.internal_status = FbsOrder.STATUS_VALIDATION_FAILED
        order.save(update_fields=["internal_status", "updated_at"])
        item = order.items.get()
        item.sku = None
        item.barcode = "OP-UNKNOWN-BARCODE"
        item.requirements = {
            "sku_mapping": "barcode_not_found",
            "marketplace_barcodes": ["OP-UNKNOWN-BARCODE"],
        }
        item.save(update_fields=["sku", "barcode", "requirements", "updated_at"])
        snapshots_before = list(
            WarehouseStockSnapshot.objects.values_list("id", "qty", "available_qty")
        )
        self.client.force_login(self.storekeeper)

        response = self.client.get(
            f"/fbs/tsd/storekeeper/demand/?state=mapping&agency={self.first['agency'].id}"
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["demand_rows"], [])
        self.assertEqual(
            response.context["mapping_summary"],
            {
                "total_lines": 1,
                "total_orders": 1,
                "total_qty": 1,
                "filtered_lines": 1,
                "filtered_orders": 1,
                "filtered_qty": 1,
                "hidden_lines": 0,
            },
        )
        self.assertEqual(response.context["mapping_rows"][0].order_id, order.id)
        self.assertContains(response, "ШК не найден в номенклатуре клиента.")
        self.assertContains(response, "OP-UNKNOWN-BARCODE")
        self.assertContains(response, f"/fbs/operator/orders/{order.id}/")
        self.assertContains(response, "Ошибка сопоставления")
        self.assertFalse(FbsReplenishmentPlan.objects.exists())
        self.assertFalse(FbsOrderStockAllocation.objects.exists())
        self.assertEqual(
            list(
                WarehouseStockSnapshot.objects.values_list(
                    "id", "qty", "available_qty"
                )
            ),
            snapshots_before,
        )

    def test_manager_cannot_open_storekeeper_operator_contour(self):
        self.client.force_login(self.manager)

        for url in (
            "/fbs/operator/orders/",
            "/fbs/operator/clients/",
            "/fbs/operator/movements/",
            "/fbs/operator/waves/",
            "/fbs/operator/problems/",
        ):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 403)

    def test_orders_support_page_sizes_and_client_filter_without_writes(self):
        own_order = self._create_order(suffix="own")
        other = self._create_client_context(2, "Второй клиент")
        self._create_order(other, suffix="other")
        self.client.force_login(self.storekeeper)

        response = self.client.get(
            f"/fbs/operator/orders/?agency={self.first['agency'].id}&page_size=200"
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["page_size"], 200)
        self.assertEqual(response.context["page"].paginator.count, 1)
        self.assertContains(response, own_order.external_order_id)
        self.assertContains(response, '<option value="25"')
        self.assertContains(response, '<option value="50"')
        self.assertContains(response, '<option value="100"')
        self.assertContains(response, '<option value="200"')
        self.assertContains(response, f'name="order_ids" value="{own_order.id}"')
        self.assertFalse(FbsPickTask.objects.exists())

    def test_order_control_filters_marketplace_status_units_and_shows_timeline_without_writes(self):
        now = timezone.now()
        overdue = self._create_order(suffix="sla-overdue", qty=2, stock=False)
        overdue.marketplace_status = "new"
        overdue.cutoff_at = now - timedelta(minutes=15)
        overdue.save(update_fields=["marketplace_status", "cutoff_at", "updated_at"])
        urgent = self._create_order(suffix="sla-urgent", qty=5, stock=False)
        urgent.marketplace_status = "confirm"
        urgent.marketplace_substatus = "awaiting_deliver"
        urgent.ordered_at = now - timedelta(hours=1)
        urgent.cutoff_at = now + timedelta(minutes=45)
        urgent.save(
            update_fields=[
                "marketplace_status",
                "marketplace_substatus",
                "ordered_at",
                "cutoff_at",
                "updated_at",
            ]
        )
        self._create_order(suffix="sla-no-cutoff", qty=1, stock=False)
        canceled = self._create_order(suffix="sla-canceled", qty=5, stock=False)
        canceled.internal_status = FbsOrder.STATUS_CANCELLED
        canceled.save(update_fields=["internal_status", "updated_at"])
        snapshots_before = list(
            WarehouseStockSnapshot.objects.values_list("id", "qty", "available_qty")
        )
        balances_before = list(
            FbsStockBalance.objects.values_list(
                "id", "qty", "available_qty", "reserved_qty"
            )
        )
        self.client.force_login(self.storekeeper)

        response = self.client.get(
            "/fbs/operator/orders/?marketplace_status=confirm"
            "&unit_min=4&unit_max=5&deadline=urgent&page_size=25"
        )
        detail = self.client.get(f"/fbs/operator/orders/{urgent.id}/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["page"].paginator.count, 1)
        self.assertEqual(response.context["page"].object_list[0].id, urgent.id)
        self.assertEqual(response.context["filters"]["marketplace_status"], "confirm")
        self.assertEqual(response.context["filters"]["unit_min"], "4")
        self.assertEqual(response.context["filters"]["unit_max"], "5")
        self.assertEqual(
            response.context["sla_summary"],
            {"active": 3, "overdue": 1, "urgent": 1, "without_cutoff": 1},
        )
        self.assertContains(response, "Внутренний статус")
        self.assertContains(response, "Статус площадки")
        self.assertContains(response, "Срок отгрузки")
        self.assertContains(response, "awaiting_deliver")
        self.assertNotContains(response, overdue.external_order_id)
        self.assertEqual(detail.status_code, 200)
        self.assertContains(detail, "История заказа")
        self.assertContains(detail, "Заказ создан на площадке")
        self.assertContains(detail, "Заказ импортирован")
        self.assertContains(detail, "Текущее состояние заказа")
        self.assertIn(
            "Текущее состояние заказа",
            {row["title"] for row in detail.context["timeline"]},
        )
        self.assertFalse(FbsPickTask.objects.exists())
        self.assertFalse(FbsOrderStockAllocation.objects.exists())
        self.assertEqual(
            list(WarehouseStockSnapshot.objects.values_list("id", "qty", "available_qty")),
            snapshots_before,
        )
        self.assertEqual(
            list(
                FbsStockBalance.objects.values_list(
                    "id", "qty", "available_qty", "reserved_qty"
                )
            ),
            balances_before,
        )

    def test_order_detail_uses_photo_fallbacks_and_shows_full_handover_summary(self):
        gallery_order = self._create_order(suffix="gallery-photo", qty=2, stock=False)
        SKUPhoto.objects.create(
            sku=self.first["sku"],
            url="https://example.test/gallery-photo.jpg",
        )
        batch = FbsHandoverBatch.objects.create(
            profile=self.first["profile"],
            external_supply_id="WB-SUPPLY-ORDER-CARD",
            status=FbsHandoverBatch.STATUS_DISPATCHED,
            dispatched_at=timezone.now(),
            created_by=self.storekeeper,
            dispatched_by=self.storekeeper,
        )
        box = FbsHandoverBox.objects.create(
            batch=batch,
            qr_code="WB-BOX-ORDER-CARD",
            status=FbsHandoverBox.STATUS_DISPATCHED,
        )
        FbsHandoverOrder.objects.create(
            box=box,
            order=gallery_order,
            added_by=self.storekeeper,
        )
        payload_context = self._create_client_context(2, "Фото из маркетплейса")
        payload_order = self._create_order(
            payload_context,
            suffix="payload-photo",
            stock=False,
        )
        payload_item = payload_order.items.get()
        payload_item.sku = None
        payload_item.raw_payload = {
            "media": {"primary_image": "https://example.test/marketplace-photo.jpg"}
        }
        payload_item.save(update_fields=["sku", "raw_payload", "updated_at"])
        self.client.force_login(self.storekeeper)

        gallery_detail = self.client.get(f"/fbs/operator/orders/{gallery_order.id}/")
        payload_detail = self.client.get(f"/fbs/operator/orders/{payload_order.id}/")

        self.assertEqual(gallery_detail.status_code, 200)
        self.assertContains(gallery_detail, "https://example.test/gallery-photo.jpg")
        self.assertContains(gallery_detail, "Отгрузка")
        self.assertContains(gallery_detail, "WB-SUPPLY-ORDER-CARD")
        self.assertContains(gallery_detail, "WB-BOX-ORDER-CARD")
        self.assertContains(gallery_detail, "1 зак. / 2 шт.")
        self.assertContains(
            gallery_detail,
            f"/fbs/tsd/storekeeper/handover/{batch.id}/",
        )
        self.assertEqual(payload_detail.status_code, 200)
        self.assertContains(payload_detail, "https://example.test/marketplace-photo.jpg")

    def test_order_status_audit_keeps_each_transition_and_renders_before_after(self):
        order = self._create_order(suffix="audit", stock=False)
        order.internal_status = FbsOrder.STATUS_AWAITING_STOCK
        order.save(update_fields=["internal_status", "updated_at"])
        order.internal_status = FbsOrder.STATUS_RESERVED
        order.save(update_fields=["internal_status", "updated_at"])
        order.marketplace_status = "confirm"
        order.marketplace_substatus = "awaiting_deliver"
        order.save(
            update_fields=[
                "marketplace_status",
                "marketplace_substatus",
                "updated_at",
            ]
        )
        self.client.force_login(self.storekeeper)

        response = self.client.get(f"/fbs/operator/orders/{order.id}/")
        audit_rows = OrderAuditEntry.objects.filter(
            order_type="fbs_order",
            order_id=str(order.id),
        ).order_by("created_at", "id")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(audit_rows.count(), 3)
        self.assertEqual(
            audit_rows[0].payload["changes"]["internal_status"],
            {"from": FbsOrder.STATUS_RECEIVED, "to": FbsOrder.STATUS_AWAITING_STOCK},
        )
        self.assertEqual(
            audit_rows[1].payload["changes"]["internal_status"],
            {"from": FbsOrder.STATUS_AWAITING_STOCK, "to": FbsOrder.STATUS_RESERVED},
        )
        self.assertContains(response, "Fullbox: Ожидает товар -&gt; Зарезервирован")
        self.assertContains(response, "Площадка: - -&gt; confirm")
        self.assertContains(response, "Подстатус: - -&gt; awaiting_deliver")

    def test_orders_show_read_only_stock_availability_by_exact_barcode(self):
        available_order = self._create_order(suffix="available", qty=2)

        movement = self._create_client_context(2, "Клиент с перемещением")
        movement_order = self._create_order(movement, suffix="movement", qty=2, stock=False)
        movement_request = FbsClientMovementRequest.objects.create(
            agency=movement["agency"],
            mode=FbsClientMovementRequest.MODE_ITEM,
            status=FbsClientMovementRequest.STATUS_SUBMITTED,
            requested_qty=3,
            requested_box_count=1,
        )
        FbsClientMovementRequestLine.objects.create(
            request=movement_request,
            sku=movement["sku"],
            barcode=movement["barcode"],
            sku_code=movement["sku"].sku_code,
            product_name=movement["sku"].name,
            requested_qty=3,
        )

        general = self._create_client_context(3, "Клиент с общим остатком")
        general_order = self._create_order(general, suffix="general", qty=2, stock=False)
        WarehouseStockSnapshot.objects.create(
            agency=general["agency"],
            sku_ref=general["sku"],
            sku_code=general["sku"].sku_code,
            name=general["sku"].name,
            barcode=general["barcode"],
            goods_type="готовый",
            qty=5,
            available_qty=5,
            zone_code="OS",
        )

        missing = self._create_client_context(4, "Клиент без остатка")
        missing_order = self._create_order(missing, suffix="missing", qty=2, stock=False)
        WarehouseStockSnapshot.objects.create(
            agency=missing["agency"],
            sku_ref=missing["sku"],
            sku_code=missing["sku"].sku_code,
            barcode=f"{missing['barcode']}-OTHER",
            goods_type="готовый",
            qty=9,
            available_qty=9,
            zone_code="OS",
        )
        balances_before = list(
            FbsStockBalance.objects.values_list("id", "qty", "available_qty", "reserved_qty")
        )
        snapshots_before = list(
            WarehouseStockSnapshot.objects.values_list("id", "qty", "available_qty")
        )
        self.client.force_login(self.storekeeper)

        response = self.client.get("/fbs/operator/orders/?page_size=25")

        self.assertEqual(response.status_code, 200)
        orders = {order.id: order for order in response.context["page"].object_list}
        self.assertEqual(orders[available_order.id].availability_state, "fbs_available")
        self.assertEqual(orders[available_order.id].availability_group, "available")
        self.assertEqual(orders[movement_order.id].availability_state, "in_movement")
        self.assertEqual(orders[movement_order.id].availability_group, "risk")
        self.assertEqual(orders[general_order.id].availability_state, "needs_replenishment")
        self.assertEqual(orders[general_order.id].availability_group, "risk")
        self.assertEqual(orders[missing_order.id].availability_state, "unavailable")
        self.assertEqual(orders[missing_order.id].availability_group, "no_fbs")
        general_row = orders[general_order.id].availability_rows[0]
        self.assertEqual(general_row["general_available_qty"], 5)
        self.assertContains(response, "FBS факт")
        self.assertContains(response, "Ожидает перемещение")
        self.assertContains(response, "Нужен подсорт")
        self.assertContains(response, "Товара недостаточно")
        self.assertContains(response, "Доступно")
        self.assertContains(response, "Может не хватить")
        self.assertContains(response, "Нет на FBS")
        self.assertContains(response, "Общий склад для подсорта")
        self.assertContains(
            response,
            "Общий склад в FBS-сборку не входит: это только источник будущего подсорта.",
        )
        self.assertEqual(
            list(FbsStockBalance.objects.values_list("id", "qty", "available_qty", "reserved_qty")),
            balances_before,
        )
        self.assertEqual(
            list(WarehouseStockSnapshot.objects.values_list("id", "qty", "available_qty")),
            snapshots_before,
        )

    def test_orders_filter_by_stock_availability(self):
        ready = self._create_order(suffix="ready-filter", qty=2)
        missing_context = self._create_client_context(2, "Недостаточный остаток")
        missing = self._create_order(
            missing_context,
            suffix="missing-filter",
            qty=2,
            stock=False,
        )
        self.client.force_login(self.storekeeper)

        ready_response = self.client.get(
            f"/fbs/operator/orders/?agency={self.first['agency'].id}"
            "&availability=ready&page_size=25"
        )
        missing_response = self.client.get(
            f"/fbs/operator/orders/?agency={missing_context['agency'].id}"
            "&availability=unavailable&page_size=25"
        )
        risk_context = self._create_client_context(3, "Ожидает подсорт")
        risk = self._create_order(risk_context, suffix="risk-filter", qty=2, stock=False)
        WarehouseStockSnapshot.objects.create(
            agency=risk_context["agency"],
            sku_ref=risk_context["sku"],
            sku_code=risk_context["sku"].sku_code,
            barcode=risk_context["barcode"],
            goods_type="готовый",
            qty=5,
            available_qty=5,
            zone_code="OS",
        )
        risk_response = self.client.get(
            f"/fbs/operator/orders/?agency={risk_context['agency'].id}"
            "&availability=risk&page_size=25"
        )

        self.assertEqual(ready_response.status_code, 200)
        self.assertContains(ready_response, ready.external_order_id)
        self.assertNotContains(ready_response, missing.external_order_id)
        self.assertEqual(ready_response.context["filters"]["availability"], "ready")
        self.assertEqual(missing_response.status_code, 200)
        self.assertContains(missing_response, missing.external_order_id)
        self.assertNotContains(missing_response, ready.external_order_id)
        self.assertEqual(risk_response.status_code, 200)
        self.assertContains(risk_response, risk.external_order_id)
        self.assertEqual(risk_response.context["filters"]["availability"], "risk")

    def test_empty_selection_prioritizes_fbs_ready_orders_before_shortage(self):
        shortage_sku = SKU.objects.create(
            agency=self.first["agency"],
            sku_code="OP-SKU-SHORTAGE",
            name="Товар без остатка",
        )
        shortage_barcode = "OP-BARCODE-SHORTAGE"
        SKUBarcode.objects.create(sku=shortage_sku, value=shortage_barcode, is_primary=True)
        shortage = FbsOrder.objects.create(
            profile=self.first["profile"],
            external_order_id="OP-SHORTAGE-FIRST",
            cutoff_at=timezone.now() - timedelta(hours=1),
        )
        FbsOrderItem.objects.create(
            order=shortage,
            external_line_id="LINE-SHORTAGE-FIRST",
            external_sku=shortage_sku.sku_code,
            barcode=shortage_barcode,
            sku=shortage_sku,
            quantity=1,
        )
        ready = self._create_order(suffix="ready-later", qty=1)
        ready.cutoff_at = timezone.now() + timedelta(hours=1)
        ready.save(update_fields=["cutoff_at", "updated_at"])
        self.client.force_login(self.storekeeper)

        with patch("fbs.operator_views.prepare_pick_queue") as prepare:
            prepare.return_value = SimpleNamespace(
                batches=(),
                tasks_created=0,
                awaiting_stock_orders=0,
                validation_failed_orders=0,
            )
            response = self.client.post(
                "/fbs/operator/waves/prepare/",
                {
                    "agency": str(self.first["agency"].id),
                    "queue_limit": "25",
                },
            )

        self.assertEqual(response.status_code, 302)
        submitted_ids = prepare.call_args.kwargs["order_ids"]
        self.assertEqual(submitted_ids[:2], [ready.id, shortage.id])
        self.assertFalse(FbsOrderStockAllocation.objects.exists())

    def test_selected_order_does_not_sweep_unrelated_reserved_order(self):
        selected = self._create_order(suffix="selected")
        unrelated = self._create_order(suffix="reserved")
        reserve_order_stock(order_id=unrelated.id, reserved_by=self.storekeeper)
        self.client.force_login(self.storekeeper)

        response = self.client.post(
            "/fbs/operator/waves/prepare/",
            {
                "agency": str(self.first["agency"].id),
                "queue_limit": "25",
                "order_ids": [str(selected.id)],
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(FbsPickTask.objects.count(), 1)
        self.assertTrue(FbsPickTask.objects.filter(order=selected).exists())
        self.assertFalse(FbsPickTask.objects.filter(order=unrelated).exists())
        queued_audit = OrderAuditEntry.objects.filter(
            order_type="fbs_order",
            order_id=str(selected.id),
            payload__source="prepare_pick_queue",
        ).get()
        self.assertEqual(
            queued_audit.payload["changes"]["internal_status"],
            {"from": FbsOrder.STATUS_RESERVED, "to": FbsOrder.STATUS_QUEUED_FOR_PICK},
        )
        unrelated.refresh_from_db()
        self.assertEqual(unrelated.internal_status, FbsOrder.STATUS_RESERVED)

    def test_selected_orders_from_different_clients_are_rejected_before_reserve(self):
        second = self._create_client_context(2, "Второй клиент")
        first_order = self._create_order(suffix="first")
        second_order = self._create_order(second, suffix="second")
        self.client.force_login(self.storekeeper)

        response = self.client.post(
            "/fbs/operator/waves/prepare/",
            {
                "agency": str(self.first["agency"].id),
                "queue_limit": "25",
                "order_ids": [str(first_order.id), str(second_order.id)],
            },
        )

        self.assertEqual(response.status_code, 409)
        self.assertContains(
            response,
            "В одну подачу разрешены заказы только выбранного клиента.",
            status_code=409,
        )
        self.assertFalse(FbsPickBatch.objects.exists())
        self.assertFalse(FbsPickTask.objects.exists())
        first_order.refresh_from_db()
        second_order.refresh_from_db()
        self.assertEqual(first_order.internal_status, FbsOrder.STATUS_RECEIVED)
        self.assertEqual(second_order.internal_status, FbsOrder.STATUS_RECEIVED)

    def test_single_client_service_guard_rejects_mixed_order_ids(self):
        second = self._create_client_context(2, "Второй клиент")
        first_order = self._create_order(suffix="first")
        second_order = self._create_order(second, suffix="second")

        with self.assertRaisesMessage(
            FbsPickingError,
            "В одну подачу разрешены заказы только одного клиента.",
        ):
            prepare_pick_queue(
                limit=25,
                order_ids=[first_order.id, second_order.id],
                single_agency_only=True,
                max_orders_per_batch=100,
                performed_by=self.storekeeper,
            )

        self.assertFalse(FbsPickTask.objects.exists())
        self.assertFalse(FbsPickBatch.objects.exists())

    def test_filtered_wave_requires_one_client_and_supports_empty_checkbox_selection(self):
        first_order = self._create_order(suffix="first")
        second = self._create_client_context(2, "Второй клиент")
        second_order = self._create_order(second, suffix="second")
        self.client.force_login(self.storekeeper)

        without_client = self.client.post(
            "/fbs/operator/waves/prepare/",
            {"queue_limit": "25"},
        )
        selected_client = self.client.post(
            "/fbs/operator/waves/prepare/",
            {
                "agency": str(self.first["agency"].id),
                "queue_limit": "25",
            },
        )

        self.assertEqual(without_client.status_code, 409)
        self.assertContains(
            without_client,
            "Сначала выберите одного клиента для подачи волны.",
            status_code=409,
        )
        self.assertEqual(selected_client.status_code, 302)
        self.assertTrue(FbsPickTask.objects.filter(order=first_order).exists())
        self.assertFalse(FbsPickTask.objects.filter(order=second_order).exists())

    def test_queue_of_200_is_split_at_100_units_per_wave(self):
        orders = []
        for index in range(101):
            order = FbsOrder.objects.create(
                profile=self.first["profile"],
                external_order_id=f"OP-BULK-{index:03d}",
            )
            FbsOrderItem.objects.create(
                order=order,
                external_line_id=f"LINE-{index:03d}",
                external_sku=self.first["sku"].sku_code,
                barcode=self.first["barcode"],
                sku=self.first["sku"],
                quantity=1,
            )
            orders.append(order)
        FbsStockBalance.objects.create(
            agency=self.first["agency"],
            box=self.first["box"],
            sku_ref=self.first["sku"],
            identity_key="f" * 64,
            sku_code=self.first["sku"].sku_code,
            name=self.first["sku"].name,
            barcode=self.first["barcode"],
            qty=101,
            available_qty=101,
        )

        result = prepare_pick_queue(
            limit=200,
            order_ids=[order.id for order in orders],
            single_agency_only=True,
            max_orders_per_batch=100,
            max_units_per_batch=100,
            performed_by=self.storekeeper,
        )

        self.assertEqual(len(result.batches), 2)
        self.assertEqual(sorted(batch.planned_qty for batch in result.batches), [1, 100])
        self.assertEqual(result.tasks_created, 101)

    def test_prepare_wave_requires_csrf(self):
        order = self._create_order()
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.storekeeper)
        csrf_client.get("/fbs/operator/orders/")
        rejected = csrf_client.post(
            "/fbs/operator/waves/prepare/",
            {
                "agency": str(self.first["agency"].id),
                "queue_limit": "25",
                "order_ids": [str(order.id)],
            },
        )
        token = csrf_client.cookies["csrftoken"].value
        accepted = csrf_client.post(
            "/fbs/operator/waves/prepare/",
            {
                "csrfmiddlewaretoken": token,
                "agency": str(self.first["agency"].id),
                "queue_limit": "25",
                "order_ids": [str(order.id)],
            },
        )

        self.assertEqual(rejected.status_code, 403)
        self.assertEqual(accepted.status_code, 302)

    def test_waves_show_full_timing_duration_and_equipment_without_writes(self):
        workstation = FbsWorkstation.objects.create(
            barcode="FBS-WS-000001",
            name="Стол 01",
            printer_name="TSC TE200",
        )
        cart = FbsPickingCart.objects.create(
            barcode="FBS-CART-000001",
            name="Тележка 01",
        )
        batch = FbsPickBatch.objects.create(
            agency=self.first["agency"],
            status=FbsPickBatch.STATUS_DONE,
            planned_qty=10,
            picked_qty=10,
            assigned_to=self.picker,
            workstation=workstation,
            cart=cart,
            created_by=self.storekeeper,
        )
        now = timezone.now().replace(second=0, microsecond=0)
        FbsPickBatch.objects.filter(pk=batch.pk).update(
            created_at=now - timedelta(hours=2),
            claimed_at=now - timedelta(hours=1, minutes=35),
            started_at=now - timedelta(hours=1, minutes=35),
            completed_at=now - timedelta(minutes=5),
        )
        FbsPickScanEvent.objects.create(
            batch=batch,
            stage=FbsPickScanEvent.STAGE_CELL,
            result=FbsPickScanEvent.RESULT_SUCCESS,
            scan_value="FBS-OP-1",
            expected_value="FBS-OP-1",
            message="Ячейка подтверждена.",
            created_by=self.picker,
        )
        before = FbsPickBatch.objects.filter(pk=batch.pk).values().get()
        self.client.force_login(self.storekeeper)

        listing = self.client.get("/fbs/operator/waves/?status=all")
        detail = self.client.get(f"/fbs/operator/waves/{batch.id}/")

        self.assertEqual(listing.status_code, 200)
        self.assertEqual(detail.status_code, 200)
        for response in (listing, detail):
            self.assertContains(response, "Стол 01")
            self.assertContains(response, "Тележка 01")
            self.assertContains(response, "1 ч 30 мин.")
            self.assertContains(response, "Завершена")
        self.assertContains(detail, "Создана")
        self.assertContains(detail, "Начата")
        self.assertContains(detail, "Длительность")
        self.assertContains(detail, "Журнал волны")
        self.assertContains(detail, "Ячейка подтверждена")
        self.assertEqual(FbsPickBatch.objects.filter(pk=batch.pk).values().get(), before)

    def _add_wb_client_token(self, context=None):
        context = context or self.first
        market, _ = Market.objects.get_or_create(id=1, defaults={"name": "WB"})
        return MarketCredential.objects.create(
            id=100 + context["index"],
            agency=context["agency"],
            market=market,
            market_key=f"client-token-{context['index']}",
        )

    @override_settings(FBS_MARKETPLACE_CREDENTIALS={})
    def test_fbs_uses_token_saved_by_client_cabinet_without_exposing_value(self):
        self._add_wb_client_token()

        state = marketplace_credentials_status(self.first["profile"])

        self.assertTrue(state["configured"])
        self.assertEqual(state["source"], "client_cabinet")
        self.assertNotIn("client-token", str(state))

    @override_settings(FBS_ORDER_PULL_ENABLED=True, FBS_MARKETPLACE_CREDENTIALS={})
    def test_clients_screen_shows_credentials_api_result_and_order_switch_separately(self):
        self._add_wb_client_token()
        FbsSyncCursor.objects.create(
            profile=self.first["profile"],
            stream=FbsSyncCursor.STREAM_ORDERS,
            cursor_key="default",
            last_polled_at=timezone.now(),
            last_success_at=timezone.now() - timedelta(minutes=1),
        )
        self.client.force_login(self.storekeeper)

        response = self.client.get("/fbs/operator/clients/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Клиенты и кабинеты")
        self.assertContains(response, str(self.first["agency"]))
        self.assertContains(response, "Подключен")
        self.assertContains(response, "Токенов: 1/1")
        self.assertContains(response, "Выключено")
        self.assertNotContains(response, "client-token-1")

    @override_settings(FBS_ORDER_PULL_ENABLED=True, FBS_MARKETPLACE_CREDENTIALS={})
    @patch("fbs.operator_views.pull_profile_orders")
    def test_client_order_refresh_uses_existing_sync_service(self, pull_orders):
        profile = self.first["profile"]
        profile.is_active = True
        profile.order_pull_enabled = True
        profile.save(update_fields=["is_active", "order_pull_enabled", "updated_at"])
        self._add_wb_client_token()
        pull_orders.return_value = MarketplaceSyncResult(received=4, created=3, updated=1)
        self.client.force_login(self.storekeeper)

        response = self.client.post(
            f"/fbs/operator/clients/{self.first['agency'].id}/sync/",
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        pull_orders.assert_called_once_with(profile_id=profile.id, limit=10000)
        self.assertContains(response, "получено: 4")
        self.assertContains(response, "новых: 3")

    @override_settings(FBS_ORDER_PULL_ENABLED=True, FBS_MARKETPLACE_CREDENTIALS={})
    @patch("fbs.operator_views.pull_profile_orders")
    def test_main_refresh_only_runs_profiles_enabled_for_order_pull(self, pull_orders):
        enabled = self.first["profile"]
        enabled.is_active = True
        enabled.order_pull_enabled = True
        enabled.save(update_fields=["is_active", "order_pull_enabled", "updated_at"])
        second = self._create_client_context(2, "Второй клиент")
        self._add_wb_client_token()
        self._add_wb_client_token(second)
        pull_orders.return_value = MarketplaceSyncResult()
        self.client.force_login(self.storekeeper)

        response = self.client.post("/fbs/operator/orders/sync/", follow=True)

        self.assertEqual(response.status_code, 200)
        pull_orders.assert_called_once_with(profile_id=enabled.id, limit=10000)
        self.assertContains(response, "Проверено профилей: 1")

    def test_picker_cannot_refresh_fbs_orders(self):
        self.client.force_login(self.picker)

        response = self.client.post("/fbs/operator/orders/sync/")

        self.assertEqual(response.status_code, 403)
