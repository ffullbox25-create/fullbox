from django.contrib.auth import get_user_model
from decimal import Decimal
from django.http import QueryDict
from django.test import RequestFactory, TestCase
from django.utils import timezone
from unittest.mock import Mock, patch

from audit.models import OrderAuditEntry
from employees.models import Employee
from fbs.models import (
    FbsBox,
    FbsIntegrationProfile,
    FbsHandoverBatch,
    FbsHandoverBox,
    FbsHandoverOrder,
    FbsHandoverOrderAssignment,
    FbsInventoryLine,
    FbsInventorySession,
    FbsOrder,
    FbsOrderItem,
    FbsPallet,
    FbsPickBatch,
    FbsPickTask,
    FbsPickRestockRequest,
    FbsPickingCart,
    FbsStockBalance,
    FbsStorageCell,
)
from marking.models import MarkingCode
from logistics.models import LogisticsTrip, LogisticsTripOrder
from shipping.models import ShippingOrder, ShippingOrderItem, ShippingTransportNote
from sklad.models import WarehouseContainer, WarehouseEvent, WarehouseLocation, WarehouseStockSnapshot
from sku.models import Agency, Market, MarketCredential, SKU, SKUBarcode
from todo.models import Task
from warehouse_goods.models import GoodsExtraFieldDefinition, GoodsExtraFieldValue

from .models import (
    WmsNewAcceptance,
    WmsNewAssemblyLine,
    WmsNewAssemblySession,
    WmsNewBox,
    WmsNewBoxItem,
    WmsNewDocument,
    WmsNewDocumentItem,
    WmsNewBundleComponent,
    WmsNewBundleOperation,
    WmsNewBundleStock,
    WmsNewEvent,
    WmsNewExtraFieldDefinition,
    WmsNewExtraFieldValue,
    WmsNewInventoryLine,
    WmsNewInventorySession,
    WmsNewMarkingCode,
    WmsNewMarketplaceStock,
    WmsNewBillingItem,
    WmsNewInvoice,
    WmsNewPartnerProfile,
    WmsNewPrimaryDocument,
    WmsNewLogisticsManifest,
    WmsNewLogisticsOrder,
    WmsNewLogisticsPackage,
    WmsNewLogisticsPackageOrder,
    WmsNewLogisticsRouteRule,
    WmsNewMovement,
    WmsNewOrder,
    WmsNewOrderItem,
    WmsNewProduct,
    WmsNewReturn,
    WmsNewRecord,
    WmsNewShipment,
    WmsNewShipmentBox,
    WmsNewShipmentOrder,
    WmsNewShipmentService,
    WmsNewTask,
    WmsNewWave,
    WmsNewWaveAllocation,
    WmsNewWaveOrder,
    WmsNewWavePickLine,
)
from .services.marking import add_codes, mark_printed, return_codes
from .services.partner_billing import (
    create_invoice,
    create_partner,
    ensure_partner_profiles,
    update_billing_sources,
    update_invoice,
    update_partner,
)
from .services.inventory import (
    approve_inventory,
    create_inventory,
    finish_inventory_count,
    record_inventory_scan,
)
from .services.boxes import create_box, update_box
from .services.extra_fields import (
    create_definition,
    set_definition_active,
    set_product_value,
)
from .services.products import assign_category, create_product, merge_products, update_product
from .services.orders import OrderOperationError, launch_wave
from .services.tasks import apply_board_action, board_stage_for
from .services.shipments import (
    add_box as add_shipment_box,
    add_order as add_shipment_order,
    add_service as add_shipment_service,
    create_shipment,
    finalize_tariff,
    transition_shipment,
    verify_order as verify_shipment_order,
)
from .services.returns import create_return, update_returns
from .services.bundles import assemble_bundle, define_bundle, disassemble_bundle
from .services.movements import move_all_from_location, move_product
from .services.documents import add_item as add_document_item, create_document, post_document
from .services.logistics import (
    accept_order as accept_logistics_order,
    add_orders_to_package,
    create_manifest as create_logistics_manifest,
    create_package as create_logistics_package,
    import_fbs_shipments,
    measure_order as measure_logistics_order,
    save_route_rules,
    update_packages,
)
from .services.marketplace_analytics import refresh_marketplace_stocks
from .services.reports import REPORT_TITLES, build_report
from .services.assembly import (
    AssemblyOperationError,
    move_assembly_problem,
    scan_assembly_order_label,
    scan_assembly_product,
    start_assembly_session,
)
from .services.picking import (
    PickingOperationError,
    cancel_wave,
    scan_product as scan_picking_product,
    scan_source_place,
    start_collection,
)
from .services.sync import (
    sync_acceptances,
    sync_boxes,
    sync_documents,
    sync_extra_fields,
    sync_inventories,
    sync_marking_codes,
    sync_movements,
    sync_logistics,
    sync_orders,
    sync_products,
    sync_returns,
    sync_shipments,
    sync_tasks,
    sync_waves,
)


class WmsNewShipmentOperationTests(TestCase):
    def setUp(self):
        self.agency = Agency.objects.create(agn_name="Партнер отгрузки")
        self.user = get_user_model().objects.create_user(username="shipment-manager")
        self.order = WmsNewOrder.objects.create(
            agency=self.agency,
            external_order_id="SHIP-ORDER-1",
            status=WmsNewOrder.STATUS_READY,
            availability_state="ready",
            source_created_at=timezone.now(),
        )
        WmsNewOrderItem.objects.create(
            order=self.order,
            external_line_id="SHIP-LINE-1",
            product_name="Товар отгрузки",
            quantity=2,
        )

    def test_independent_shipment_lifecycle_never_creates_legacy_handover_rows(self):
        old_counts = (
            FbsHandoverBatch.objects.count(),
            FbsHandoverBox.objects.count(),
            FbsHandoverOrderAssignment.objects.count(),
            FbsHandoverOrder.objects.count(),
        )
        shipment = create_shipment(
            agency=self.agency,
            delivery_type="Wildberries FBS",
            integration_name="Пилот WB",
            actor=self.user,
        )
        box = add_shipment_box(shipment_id=shipment.id, qr_code="FBN-BOX-1", actor=self.user)
        link = add_shipment_order(shipment_id=shipment.id, order_id=self.order.id, actor=self.user)
        verify_shipment_order(
            shipment_id=shipment.id,
            shipment_order_id=link.id,
            box_id=box.id,
            actor=self.user,
        )
        transition_shipment(shipment_id=shipment.id, action="finish_check", actor=self.user)
        transition_shipment(shipment_id=shipment.id, action="send", actor=self.user)
        transition_shipment(shipment_id=shipment.id, action="accept", actor=self.user)
        add_shipment_service(
            shipment_id=shipment.id,
            name="Обработка короба",
            unit_price="120.50",
            quantity="2",
            actor=self.user,
        )
        finalize_tariff(shipment_id=shipment.id, actor=self.user)

        shipment.refresh_from_db()
        self.order.refresh_from_db()
        self.assertEqual(shipment.status, WmsNewShipment.STATUS_ACCEPTED)
        self.assertTrue(shipment.tariff_finalized)
        self.assertEqual(self.order.status, WmsNewOrder.STATUS_DONE)
        self.assertEqual(WmsNewShipmentBox.objects.filter(shipment=shipment).count(), 1)
        self.assertEqual(WmsNewShipmentOrder.objects.filter(shipment=shipment).count(), 1)
        self.assertEqual(WmsNewShipmentService.objects.filter(shipment=shipment).count(), 1)
        self.assertEqual(
            old_counts,
            (
                FbsHandoverBatch.objects.count(),
                FbsHandoverBox.objects.count(),
                FbsHandoverOrderAssignment.objects.count(),
                FbsHandoverOrder.objects.count(),
            ),
        )


class WmsNewReturnOperationTests(TestCase):
    def setUp(self):
        self.agency = Agency.objects.create(agn_name="Партнер возврата")
        self.user = get_user_model().objects.create_user(username="return-manager")
        self.order = WmsNewOrder.objects.create(
            agency=self.agency,
            external_order_id="RETURN-ORDER-1",
            tracking_number="RETURN-TRACK-1",
            status=WmsNewOrder.STATUS_DONE,
        )
        WmsNewOrderItem.objects.create(
            order=self.order,
            external_line_id="RETURN-LINE-1",
            product_name="Возвращаемый товар",
            quantity=2,
        )

    def test_return_scan_and_lifecycle_are_isolated(self):
        legacy_count = FbsPickRestockRequest.objects.count()

        item = create_return(scan=self.order.tracking_number, actor=self.user)
        self.order.refresh_from_db()
        self.assertEqual(item.status, WmsNewReturn.STATUS_QUEUED)
        self.assertEqual(self.order.status, WmsNewOrder.STATUS_RETURN_PENDING)
        update_returns(return_ids=[item.id], action="start", actor=self.user)
        update_returns(return_ids=[item.id], action="complete", actor=self.user)

        item.refresh_from_db()
        self.order.refresh_from_db()
        self.assertEqual(item.status, WmsNewReturn.STATUS_COMPLETED)
        self.assertEqual(item.returned_qty, 2)
        self.assertEqual(self.order.status, WmsNewOrder.STATUS_RETURNED)
        self.assertEqual(FbsPickRestockRequest.objects.count(), legacy_count)
        self.assertEqual(
            WmsNewEvent.objects.filter(entity_type="return", entity_id=item.id).count(),
            3,
        )


class WmsNewBundleOperationTests(TestCase):
    def setUp(self):
        self.agency = Agency.objects.create(agn_name="Партнер наборов")
        self.user = get_user_model().objects.create_user(username="bundle-manager")
        self.location = WarehouseLocation.objects.create(
            warehouse_code="MSK",
            zone_code="KIT",
            row_no=1,
            section_no=1,
            tier_no=1,
            cell_no=1,
            location_code="KIT-1",
            is_active=True,
            is_storage=True,
        )
        self.bundle = WmsNewProduct.objects.create(
            agency=self.agency,
            name="Будущий набор",
            article="BUNDLE-1",
        )
        self.component = WmsNewProduct.objects.create(
            agency=self.agency,
            name="Компонент",
            article="COMPONENT-1",
            stock_on_hand=10,
            stock_free=10,
        )

    def test_define_assemble_and_disassemble_use_only_new_stock(self):
        legacy_sku_count = SKU.objects.count()
        legacy_snapshot_count = WarehouseStockSnapshot.objects.count()

        define_bundle(
            bundle_id=self.bundle.id,
            components=[(self.component.id, 3)],
            actor=self.user,
        )
        assemble_bundle(
            bundle_id=self.bundle.id,
            quantity=2,
            location_id=self.location.id,
            actor=self.user,
        )
        self.bundle.refresh_from_db()
        self.component.refresh_from_db()
        self.assertTrue(self.bundle.is_bundle)
        self.assertEqual(self.bundle.stock_on_hand, 2)
        self.assertEqual(self.component.stock_on_hand, 4)
        self.assertEqual(
            WmsNewBundleStock.objects.get(bundle=self.bundle, location=self.location).quantity,
            2,
        )

        disassemble_bundle(
            bundle_id=self.bundle.id,
            quantity=1,
            location_id=self.location.id,
            actor=self.user,
        )

        self.bundle.refresh_from_db()
        self.component.refresh_from_db()
        self.assertEqual(self.bundle.stock_on_hand, 1)
        self.assertEqual(self.component.stock_on_hand, 7)
        self.assertEqual(WmsNewBundleComponent.objects.filter(bundle=self.bundle).count(), 1)
        self.assertEqual(WmsNewBundleOperation.objects.filter(bundle=self.bundle).count(), 3)
        self.assertEqual(SKU.objects.count(), legacy_sku_count)
        self.assertEqual(WarehouseStockSnapshot.objects.count(), legacy_snapshot_count)


class WmsNewMovementOperationTests(TestCase):
    def setUp(self):
        self.agency = Agency.objects.create(agn_name="Партнер перемещений")
        self.user = get_user_model().objects.create_user(username="movement-manager")
        self.source = WarehouseLocation.objects.create(
            warehouse_code="MSK",
            zone_code="MOVE",
            row_no=1,
            section_no=1,
            tier_no=1,
            cell_no=1,
            location_code="MOVE-SOURCE",
            is_active=True,
            is_storage=True,
        )
        self.target = WarehouseLocation.objects.create(
            warehouse_code="MSK",
            zone_code="MOVE",
            row_no=1,
            section_no=1,
            tier_no=1,
            cell_no=2,
            location_code="MOVE-TARGET",
            is_active=True,
            is_storage=True,
        )
        self.product = WmsNewProduct.objects.create(
            agency=self.agency,
            name="Товар перемещения",
            article="MOVE-1",
            stock_on_hand=10,
            stock_free=10,
        )
        self.box = WmsNewBox.objects.create(
            agency=self.agency,
            code="MOVE-BOX-1",
            location=self.source,
            location_code=self.source.location_code,
            zone_code=self.source.zone_code,
            stock_on_hand=10,
            stock_free=10,
            sku_count=1,
        )
        self.item = WmsNewBoxItem.objects.create(
            box=self.box,
            product=self.product,
            agency=self.agency,
            sku_code=self.product.article,
            product_name=self.product.name,
            qty=10,
            available_qty=10,
        )

    def test_move_units_and_all_boxes_change_only_wms_new_domain(self):
        legacy_snapshot_count = WarehouseStockSnapshot.objects.count()
        records = move_product(
            product_id=self.product.id,
            source_location_id=self.source.id,
            target_location_id=self.target.id,
            units=3,
            actor=self.user,
        )
        self.item.refresh_from_db()
        self.product.refresh_from_db()
        self.assertEqual(self.item.qty, 7)
        self.assertEqual(self.product.stock_on_hand, 10)
        self.assertEqual(records[0].quantity, 3)
        self.assertEqual(
            WmsNewBoxItem.objects.get(box__code=f"FBN-MOVE-{self.agency.id}-{self.target.id}").qty,
            3,
        )

        moved = move_all_from_location(
            source_location_id=self.source.id,
            target_location_id=self.target.id,
            actor=self.user,
        )
        self.box.refresh_from_db()
        self.assertEqual(self.box.location_id, self.target.id)
        self.assertEqual(moved[0].quantity, 7)
        self.assertEqual(WarehouseStockSnapshot.objects.count(), legacy_snapshot_count)
        self.assertEqual(WmsNewMovement.objects.filter(is_manual=True).count(), 2)
        self.assertEqual(WmsNewEvent.objects.filter(entity_type="movement").count(), 2)

    def test_sync_copies_source_event_without_changing_it(self):
        source_event = WarehouseEvent.objects.create(
            agency=self.agency,
            event_type="movement_completed",
            from_location=self.source,
            to_location=self.target,
            qty=2,
            payload={"name": "Товар из истории", "sku_code": "HISTORY-1"},
            performed_by=self.user,
            occurred_at=timezone.now(),
        )
        before = WarehouseEvent.objects.get(pk=source_event.id).payload.copy()
        result = sync_movements()
        self.assertEqual(result, {"imported": 1, "updated": 0})
        movement = WmsNewMovement.objects.get(source_event_id=source_event.id)
        self.assertEqual(movement.product_name, "Товар из истории")
        self.assertEqual(movement.action, WmsNewMovement.ACTION_MOVEMENT)
        self.assertEqual(WarehouseEvent.objects.get(pk=source_event.id).payload, before)


class WmsNewDocumentTests(TestCase):
    def setUp(self):
        self.agency = Agency.objects.create(agn_name="Партнер документов")
        self.user = get_user_model().objects.create_user(username="document-manager")
        self.location = WarehouseLocation.objects.create(
            warehouse_code="MSK",
            zone_code="DOC",
            row_no=1,
            section_no=1,
            tier_no=1,
            cell_no=1,
            location_code="DOC-PLACE",
            is_active=True,
            is_storage=True,
        )
        self.product = WmsNewProduct.objects.create(
            agency=self.agency,
            name="Товар документа",
            article="DOC-1",
            barcode="460000000001",
        )

    def test_manual_receipt_and_writeoff_change_only_wms_new_stock(self):
        legacy_counts = (SKU.objects.count(), WarehouseStockSnapshot.objects.count())
        receipt = create_document(
            document_type=WmsNewDocument.TYPE_RECEIPT,
            agency_id=self.agency.id,
            actor=self.user,
        )
        add_document_item(
            document_id=receipt.id,
            product_id=self.product.id,
            quantity=5,
            location_id=self.location.id,
            actor=self.user,
        )
        post_document(document_id=receipt.id, actor=self.user)
        self.product.refresh_from_db()
        receipt.refresh_from_db()
        self.assertEqual((self.product.stock_on_hand, self.product.stock_free), (5, 5))
        self.assertEqual(receipt.status, WmsNewDocument.STATUS_POSTED)
        self.assertEqual(
            WmsNewBoxItem.objects.get(product=self.product, box__location=self.location).qty,
            5,
        )

        writeoff = create_document(
            document_type=WmsNewDocument.TYPE_WRITEOFF,
            agency_id=self.agency.id,
            actor=self.user,
        )
        add_document_item(
            document_id=writeoff.id,
            product_id=self.product.id,
            quantity=2,
            location_id=self.location.id,
            actor=self.user,
        )
        post_document(document_id=writeoff.id, actor=self.user)
        self.product.refresh_from_db()
        self.assertEqual((self.product.stock_on_hand, self.product.stock_free), (3, 3))
        self.assertEqual(WmsNewMovement.objects.filter(source_event_type="wms_new_document").count(), 2)
        self.assertEqual((SKU.objects.count(), WarehouseStockSnapshot.objects.count()), legacy_counts)

    def test_acceptance_is_copied_as_idempotent_receipt_document(self):
        source = WmsNewAcceptance.objects.create(
            source_order_key="PR-DOC-1",
            agency=self.agency,
            task_number="DOC-ACCEPT-1",
            status=WmsNewAcceptance.STATUS_DONE,
            source_created_at=timezone.now(),
            completed_at=timezone.now(),
            source_snapshot={
                "payload": {
                    "flow_state": {
                        "boxes": [
                            {
                                "code": "BOX-DOC-1",
                                "items": [
                                    {
                                        "sku_code": self.product.article,
                                        "name": self.product.name,
                                        "barcode": self.product.barcode,
                                        "qty": 7,
                                    }
                                ],
                            }
                        ]
                    }
                }
            },
        )
        first = sync_documents()
        second = sync_documents()
        document = WmsNewDocument.objects.get(source_acceptance=source)
        line = WmsNewDocumentItem.objects.get(document=document)
        self.assertEqual(first, {"imported": 1, "updated": 0})
        self.assertEqual(second, {"imported": 0, "updated": 1})
        self.assertEqual(document.status, WmsNewDocument.STATUS_POSTED)
        self.assertEqual((document.sku_count, document.unit_count), (1, 7))
        self.assertEqual(line.product_id, self.product.id)
        self.assertEqual(line.location_name, "BOX-DOC-1")


class WmsNewReportTests(TestCase):
    def setUp(self):
        self.user, _ = get_user_model().objects.get_or_create(username="dev")
        self.agency = Agency.objects.create(agn_name="Партнер отчетов")
        self.product = WmsNewProduct.objects.create(
            agency=self.agency,
            name="Отчетный товар",
            article="REPORT-1",
            barcode="460000000099",
            stock_on_hand=12,
            stock_free=9,
            fbs_reserved=3,
        )
        self.order = WmsNewOrder.objects.create(
            agency=self.agency,
            marketplace="Wildberries",
            delivery_type="Wildberries FBS",
            external_order_id="REPORT-ORDER-1",
            status=WmsNewOrder.STATUS_READY,
            source_created_at=timezone.now(),
            source_updated_at=timezone.now(),
        )
        WmsNewOrderItem.objects.create(
            order=self.order,
            external_line_id="REPORT-LINE-1",
            external_sku=self.product.article,
            barcode=self.product.barcode,
            product_name=self.product.name,
            quantity=2,
        )

    def test_all_reports_build_without_legacy_write(self):
        params = QueryDict("run=1&zero_stock=all")
        legacy_counts = (SKU.objects.count(), WarehouseStockSnapshot.objects.count())
        reports = {slug: build_report(slug, params, limit=10) for slug in REPORT_TITLES}
        self.assertEqual(reports["reports-goods"]["rows"][0][3], "REPORT-1")
        self.assertEqual(reports["reports-fbs-status"]["rows"][0][-1], 1)
        self.assertTrue(all(report["generated"] for report in reports.values()))
        self.assertEqual((SKU.objects.count(), WarehouseStockSnapshot.objects.count()), legacy_counts)

    def test_report_is_not_queried_before_generate(self):
        report = build_report("reports-goods", QueryDict(""))
        self.assertFalse(report["generated"])
        self.assertEqual(report["rows"], [])

    def test_report_page_renders_real_rows(self):
        from head_manager.fbs_new import HeadManagerFbsNewView

        request = RequestFactory().get(
            "/head-manager/fbs-new/reports-goods/?run=1&zero_stock=all"
        )
        request.user = self.user
        request.session = {}
        response = HeadManagerFbsNewView.as_view()(request, section="reports-goods")
        content = response.render().content.decode("utf-8")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Отчетный товар", content)
        self.assertNotIn("/head-manager/fbs/", content)


class WmsNewMarketplaceAnalyticsTests(TestCase):
    def setUp(self):
        self.user, _ = get_user_model().objects.get_or_create(username="dev")
        self.agency = Agency.objects.create(agn_name="Партнер аналитики")
        self.market, _ = Market.objects.get_or_create(id=9101, defaults={"name": "WB"})
        self.credential = MarketCredential.objects.create(
            id=9101,
            agency=self.agency,
            market=self.market,
            market_key="test-token",
        )
        self.product = WmsNewProduct.objects.create(
            agency=self.agency,
            name="Товар аналитики",
            article="ANALYTICS-1",
            barcode="460000000111",
            stock_on_hand=15,
            stock_free=15,
            category="Тестовая категория",
        )
        order = WmsNewOrder.objects.create(
            agency=self.agency,
            marketplace="Wildberries",
            external_order_id="ANALYTICS-ORDER-1",
            status=WmsNewOrder.STATUS_DONE,
            source_created_at=timezone.now(),
            source_updated_at=timezone.now(),
        )
        WmsNewOrderItem.objects.create(
            order=order,
            external_line_id="ANALYTICS-LINE-1",
            external_sku=self.product.article,
            barcode=self.product.barcode,
            product_name=self.product.name,
            quantity=2,
        )

    @patch("wms_new.services.marketplace_analytics.requests.get")
    def test_independent_wb_refresh_and_page(self, get_mock):
        response = Mock(status_code=200)
        response.json.return_value = [
            {
                "supplierArticle": self.product.article,
                "barcode": self.product.barcode,
                "warehouseName": "Коледино",
                "quantityFull": 10,
            }
        ]
        get_mock.return_value = response
        legacy_counts = (SKU.objects.count(), WarehouseStockSnapshot.objects.count())
        result = refresh_marketplace_stocks(actor=self.user, agency_id=self.agency.id)
        stock = WmsNewMarketplaceStock.objects.get()
        self.assertEqual(result["imported"], 1)
        self.assertEqual((stock.quantity, stock.fullbox_qty, stock.sales_30d), (10, 15, 2))
        self.assertEqual(stock.days_cover, Decimal("150.00"))
        self.assertEqual((SKU.objects.count(), WarehouseStockSnapshot.objects.count()), legacy_counts)

        from head_manager.fbs_new import HeadManagerFbsNewView

        request = RequestFactory().get(
            f"/head-manager/fbs-new/analytics-marketplace-stocks/?partner={self.agency.id}"
        )
        request.user = self.user
        request.session = {}
        page = HeadManagerFbsNewView.as_view()(
            request, section="analytics-marketplace-stocks"
        )
        content = page.render().content.decode("utf-8")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Коледино", content)
        self.assertIn("Товар аналитики", content)
        self.assertNotIn("/head-manager/fbs/", content)


class WmsNewPartnerBillingTests(TestCase):
    def setUp(self):
        self.user, _ = get_user_model().objects.get_or_create(username="dev")
        self.agency = Agency.objects.create(
            agn_name="Партнер нового биллинга",
            inn="7700000000",
        )
        self.task = WmsNewTask.objects.create(
            agency=self.agency,
            title="Пересчитать коробки",
            workflow_type=WmsNewTask.TYPE_PROCESSING,
            status=WmsNewTask.STATUS_DONE,
            created_by=self.user,
        )
        self.shipment = WmsNewShipment.objects.create(
            agency=self.agency,
            source_batch_id=99001,
            delivery_type="Wildberries FBS",
            integration_name="WB тест",
            item_count=2,
            status=WmsNewShipment.STATUS_ACCEPTED,
            created_by=self.user,
        )
        WmsNewShipmentService.objects.create(
            shipment=self.shipment,
            name="Обработка FBS",
            unit_price=Decimal("12.50"),
            quantity=2,
            created_by=self.user,
        )

    def test_partner_overlay_does_not_change_source_agency(self):
        original_name = self.agency.agn_name
        profiles = ensure_partner_profiles()
        profile = next(item for item in profiles if item.source_agency_id == self.agency.id)
        update_partner(
            profile_id=profile.id,
            actor=self.user,
            name="Пилотное имя",
            balance="125.50",
            requisites="Пилотные реквизиты",
            is_active=True,
            wb_products_enabled=True,
            wb_orders_enabled=True,
        )
        manual = create_partner(name="Новый партнер только FBS-NEW", actor=self.user)
        self.agency.refresh_from_db()
        self.assertEqual(self.agency.agn_name, original_name)
        self.assertEqual(profile.source_agency_id, self.agency.id)
        self.assertTrue(manual.is_manual)
        self.assertIsNone(manual.source_agency_id)

    def test_independent_tariff_invoice_payment_and_primary_document(self):
        source_state = (self.task.status, self.shipment.status)
        self.assertEqual(
            update_billing_sources(
                kind=WmsNewBillingItem.KIND_TASK,
                source_ids=[self.task.id],
                action="price",
                actor=self.user,
            ),
            1,
        )
        self.assertEqual(
            update_billing_sources(
                kind=WmsNewBillingItem.KIND_FBS,
                source_ids=[self.shipment.id],
                action="price",
                actor=self.user,
            ),
            1,
        )
        invoice = create_invoice(
            agency=self.agency,
            invoice_type=WmsNewInvoice.TYPE_SERVICES,
            actor=self.user,
        )
        self.assertEqual(invoice.total_amount, Decimal("25.00"))
        self.assertEqual(invoice.items.count(), 2)
        self.assertEqual(WmsNewPrimaryDocument.objects.filter(invoice=invoice).count(), 1)
        update_invoice(invoice_id=invoice.id, action="ready", actor=self.user)
        update_invoice(invoice_id=invoice.id, action="payment", amount="10", actor=self.user)
        invoice.refresh_from_db()
        self.task.refresh_from_db()
        self.shipment.refresh_from_db()
        self.assertEqual(invoice.paid_amount, Decimal("10.00"))
        self.assertEqual((self.task.status, self.shipment.status), source_state)
        self.assertGreaterEqual(WmsNewEvent.objects.filter(entity_type="wmsnewinvoice").count(), 3)

    def test_partner_pages_render_donor_controls_and_no_legacy_links(self):
        ensure_partner_profiles()
        update_billing_sources(
            kind=WmsNewBillingItem.KIND_FBS,
            source_ids=[self.shipment.id],
            action="price",
            actor=self.user,
        )
        create_invoice(
            agency=self.agency,
            invoice_type=WmsNewInvoice.TYPE_SERVICES,
            actor=self.user,
        )
        from head_manager.fbs_new import HeadManagerFbsNewView

        for section, expected in (
            ("partners-list", "Добавить партнера"),
            ("partners-billing-tasks", "Непротарифицированные задачи"),
            ("partners-billing-storage", "Протарифицированы но не в счете"),
            ("partners-billing-fbs", "Непротарифицированные отгрузки"),
            ("partners-invoices", "Создать новый счет"),
            ("partners-primary-docs", "Скачать"),
        ):
            request = RequestFactory().get(f"/head-manager/fbs-new/{section}/")
            request.user = self.user
            request.session = {}
            response = HeadManagerFbsNewView.as_view()(request, section=section)
            content = response.render().content.decode("utf-8")
            self.assertEqual(response.status_code, 200)
            self.assertIn(expected, content)
            self.assertNotIn("/head-manager/fbs/", content)
            if section == "partners-list":
                self.assertIn(
                    "/head-manager/fbs-new/partners-list/?partner_id=",
                    content,
                )
            if section == "partners-invoices":
                self.assertIn(
                    "/head-manager/fbs-new/partners-invoices/?invoice_id=",
                    content,
                )


class WmsNewLogisticsTests(TestCase):
    def setUp(self):
        self.agency = Agency.objects.create(agn_name="Партнер логистики")
        self.user = get_user_model().objects.create_user(username="logistics-manager")

    def test_sync_copies_shipping_and_trip_and_preserves_pilot_measurement(self):
        source_order = ShippingOrder.objects.create(
            number="OTG-LOG-1",
            agency=self.agency,
            status=ShippingOrder.STATUS_PACKED,
            shipping_barcode="TRACK-LOG-1",
        )
        ShippingOrderItem.objects.create(
            order=source_order,
            sku_code="LOG-SKU-1",
            name="Товар логистики",
            qty_requested=3,
        )
        ShippingTransportNote.objects.create(order=source_order, cargo_weight_kg="1.250")
        source_trip = LogisticsTrip.objects.create(number="LOG-TRIP-1")
        LogisticsTripOrder.objects.create(trip=source_trip, shipping_order=source_order)
        old_order_status = source_order.status

        result = sync_logistics()
        self.assertGreaterEqual(result["imported"], 8)
        target = WmsNewLogisticsOrder.objects.get(source_shipping_order_id=source_order.id)
        manifest = WmsNewLogisticsManifest.objects.get(source_trip_id=source_trip.id)
        self.assertEqual(target.weight_g, 1250)
        self.assertEqual(target.item_count, 3)
        self.assertEqual(manifest.total_items, 3)
        self.assertEqual(WmsNewLogisticsRouteRule.objects.count(), 6)

        measure_logistics_order(
            order_id=target.id,
            weight_g=1400,
            width_mm=100,
            height_mm=200,
            depth_mm=300,
            actor=self.user,
        )
        sync_logistics()
        target.refresh_from_db()
        source_order.refresh_from_db()
        self.assertEqual(target.weight_g, 1400)
        self.assertEqual(target.status, WmsNewLogisticsOrder.STATUS_OK)
        self.assertEqual(source_order.status, old_order_status)

    def test_independent_fbs_import_package_manifest_and_routes(self):
        order = WmsNewOrder.objects.create(
            agency=self.agency,
            external_order_id="LOG-FBS-ORDER",
            tracking_number="LOG-FBS-TRACK",
            delivery_type="Wildberries FBS",
            status=WmsNewOrder.STATUS_READY,
        )
        WmsNewOrderItem.objects.create(
            order=order,
            external_line_id="LOG-FBS-LINE",
            product_name="Товар FBS",
            quantity=2,
        )
        shipment = WmsNewShipment.objects.create(
            agency=self.agency,
            delivery_type="Wildberries FBS",
        )
        WmsNewShipmentOrder.objects.create(shipment=shipment, order=order)
        legacy_shipping_count = ShippingOrder.objects.count()
        legacy_trip_count = LogisticsTrip.objects.count()

        imported = import_fbs_shipments(shipment_ids=[shipment.id], actor=self.user)
        logistics_order = imported[0]
        measure_logistics_order(
            order_id=logistics_order.id,
            weight_g=500,
            width_mm=100,
            height_mm=100,
            depth_mm=100,
            actor=self.user,
        )
        package = create_logistics_package(
            carrier_code=WmsNewLogisticsPackage.CARRIER_GBS,
            delivery_service="Wildberries FBS",
            warehouse_code="Основной склад",
            actor=self.user,
        )
        add_orders_to_package(
            package_id=package.id, order_ids=[logistics_order.id], actor=self.user
        )
        manifest = create_logistics_manifest(
            name="Рейс FBS-NEW", total_weight_g=0, actor=self.user
        )
        update_packages(
            package_ids=[package.id], action="manifest", manifest_id=manifest.id, actor=self.user
        )
        update_packages(package_ids=[package.id], action="send", actor=self.user)
        save_route_rules(
            rows=[
                {
                    "delivery_service": "Wildberries FBS",
                    "international_carrier": WmsNewLogisticsPackage.CARRIER_GBS,
                    "local_carrier": "",
                    "is_active": True,
                }
            ],
            actor=self.user,
        )

        package.refresh_from_db()
        manifest.refresh_from_db()
        self.assertEqual(package.status, WmsNewLogisticsPackage.STATUS_SENT)
        self.assertEqual(package.manifest_id, manifest.id)
        self.assertEqual(package.weight_g, 500)
        self.assertEqual(manifest.total_items, 2)
        self.assertEqual(WmsNewLogisticsPackageOrder.objects.count(), 1)
        self.assertEqual(ShippingOrder.objects.count(), legacy_shipping_count)
        self.assertEqual(LogisticsTrip.objects.count(), legacy_trip_count)


class WmsNewShipmentSyncTests(TestCase):
    def setUp(self):
        self.agency = Agency.objects.create(agn_name="Партнер синхронизации отгрузки")
        self.profile = FbsIntegrationProfile.objects.create(
            agency=self.agency,
            marketplace=FbsIntegrationProfile.MARKETPLACE_WB,
            name="Тестовая интеграция WB",
            external_account_id="shipment-account",
            external_warehouse_id="shipment-warehouse",
        )
        self.source_order = FbsOrder.objects.create(
            profile=self.profile,
            external_order_id="SOURCE-SHIPMENT-ORDER",
            internal_status=FbsOrder.STATUS_READY_FOR_HANDOVER,
            ordered_at=timezone.now(),
        )
        FbsOrderItem.objects.create(
            order=self.source_order,
            external_line_id="SOURCE-SHIPMENT-LINE",
            product_name="Товар",
            quantity=1,
        )
        sync_orders()

    def test_sync_copies_boxes_and_orders_and_preserves_pilot_status(self):
        source = FbsHandoverBatch.objects.create(
            profile=self.profile,
            external_supply_id="WB-GI-TEST",
            status=FbsHandoverBatch.STATUS_OPEN,
        )
        FbsHandoverOrderAssignment.objects.create(
            batch=source,
            order=self.source_order,
            status=FbsHandoverOrderAssignment.STATUS_CONFIRMED,
        )
        source_box = FbsHandoverBox.objects.create(batch=source, qr_code="SOURCE-BOX")
        FbsHandoverOrder.objects.create(box=source_box, order=self.source_order)

        result = sync_shipments()

        self.assertEqual(result, {"imported": 1, "updated": 0, "linked": 1})
        target = WmsNewShipment.objects.get(source_batch_id=source.id)
        self.assertEqual(target.status, WmsNewShipment.STATUS_NEW)
        self.assertEqual(target.boxes.count(), 1)
        self.assertEqual(target.shipment_orders.count(), 1)
        target.status = WmsNewShipment.STATUS_CHECKING
        target.pilot_revision = 1
        target.save(update_fields=("status", "pilot_revision"))
        source.status = FbsHandoverBatch.STATUS_ACCEPTED
        source.save(update_fields=("status", "updated_at"))

        sync_shipments()

        target.refresh_from_db()
        self.assertEqual(target.status, WmsNewShipment.STATUS_CHECKING)

    def test_return_sync_copies_order_request_and_preserves_pilot_status(self):
        source_wave = FbsPickBatch.objects.create(
            agency=self.agency,
            status=FbsPickBatch.STATUS_DONE,
            planned_qty=1,
            picked_qty=1,
        )
        source = FbsPickRestockRequest.objects.create(
            batch=source_wave,
            order=self.source_order,
            status=FbsPickRestockRequest.STATUS_QUEUED,
            reason="Тест возврата",
            planned_qty=1,
        )

        result = sync_returns()

        self.assertEqual(result, {"imported": 1, "updated": 0, "linked": 1})
        target = WmsNewReturn.objects.get(source_request_id=source.id)
        self.assertEqual(target.order.source_order_id, self.source_order.id)
        self.assertEqual(target.status, WmsNewReturn.STATUS_QUEUED)
        target.status = WmsNewReturn.STATUS_IN_PROGRESS
        target.pilot_revision = 1
        target.save(update_fields=("status", "pilot_revision"))
        source.status = FbsPickRestockRequest.STATUS_COMPLETED
        source.returned_qty = 1
        source.completed_at = timezone.now()
        source.save(update_fields=("status", "returned_qty", "completed_at", "updated_at"))

        sync_returns()

        target.refresh_from_db()
        self.assertEqual(target.status, WmsNewReturn.STATUS_IN_PROGRESS)
        self.assertEqual(target.returned_qty, 1)


class WmsNewShadowSyncTests(TestCase):
    def setUp(self):
        self.agency = Agency.objects.create(agn_name="Партнер синхронизации")
        self.profile = FbsIntegrationProfile.objects.create(
            agency=self.agency,
            marketplace=FbsIntegrationProfile.MARKETPLACE_WB,
            name="Тест WB",
            external_account_id="test-account",
            external_warehouse_id="test-wh",
        )

    def test_order_sync_copies_source_but_never_overwrites_pilot_state(self):
        source = FbsOrder.objects.create(
            profile=self.profile,
            external_order_id="SYNC-1",
            internal_status=FbsOrder.STATUS_RECEIVED,
            raw_payload={"tracking_number": "TRACK-1"},
            ordered_at=timezone.now(),
        )
        FbsOrderItem.objects.create(
            order=source,
            external_line_id="LINE-1",
            external_sku="SKU-1",
            barcode="BAR-1",
            product_name="Тестовый товар",
            quantity=2,
        )

        result = sync_orders()

        self.assertEqual(result["imported"], 1)
        target = WmsNewOrder.objects.get(source_order_id=source.id)
        self.assertEqual(target.status, WmsNewOrder.STATUS_NEW)
        self.assertEqual(target.tracking_number, "TRACK-1")
        self.assertEqual(target.items.get().quantity, 2)

        target.status = WmsNewOrder.STATUS_DONE
        target.pilot_revision = 1
        target.save(update_fields=["status", "pilot_revision"])
        source.internal_status = FbsOrder.STATUS_CANCELLED
        source.save(update_fields=["internal_status", "updated_at"])

        sync_orders()

        target.refresh_from_db()
        self.assertEqual(target.source_status, FbsOrder.STATUS_CANCELLED)
        self.assertEqual(target.status, WmsNewOrder.STATUS_DONE)

    def test_wave_sync_copies_source_links_and_preserves_pilot_state(self):
        source_order = FbsOrder.objects.create(
            profile=self.profile,
            external_order_id="SYNC-WAVE-ORDER-1",
            internal_status=FbsOrder.STATUS_RECEIVED,
            ordered_at=timezone.now(),
        )
        FbsOrderItem.objects.create(
            order=source_order,
            external_line_id="SYNC-WAVE-LINE-1",
            product_name="Товар волны",
            quantity=2,
        )
        source_wave = FbsPickBatch.objects.create(
            agency=self.agency,
            status=FbsPickBatch.STATUS_QUEUED,
            planned_qty=2,
        )
        FbsPickTask.objects.create(
            batch=source_wave,
            order=source_order,
            planned_qty=2,
        )
        sync_orders()

        result = sync_waves()

        self.assertEqual(result, {"imported": 1, "updated": 0, "linked": 1})
        target = WmsNewWave.objects.get(source_batch_id=source_wave.id)
        link = WmsNewWaveOrder.objects.get(wave=target)
        self.assertEqual(link.order.source_order_id, source_order.id)
        self.assertEqual(target.planned_orders, 1)
        self.assertEqual(target.planned_units, 2)
        self.assertEqual(target.status, WmsNewWave.STATUS_QUEUED)

        target.status = WmsNewWave.STATUS_CANCELLED
        target.pilot_revision = 1
        target.save(update_fields=["status", "pilot_revision"])
        source_wave.status = FbsPickBatch.STATUS_IN_PROGRESS
        source_wave.save(update_fields=["status", "updated_at"])

        sync_waves()

        target.refresh_from_db()
        source_wave.refresh_from_db()
        self.assertEqual(target.source_status, FbsPickBatch.STATUS_IN_PROGRESS)
        self.assertEqual(target.status, WmsNewWave.STATUS_CANCELLED)
        self.assertEqual(source_wave.status, FbsPickBatch.STATUS_IN_PROGRESS)
        self.assertEqual(target.source_snapshot["tasks"][0]["status"], "queued")

    def test_task_sync_copies_source_but_preserves_pilot_changes(self):
        source = Task.objects.create(
            title="Системная приемка",
            description="Исходная задача",
            route="/orders/receiving/ABC/",
            status="backlog",
            priority="high",
            due_date=timezone.now(),
        )
        OrderAuditEntry.objects.create(
            order_id="ABC",
            order_type="receiving",
            action="create",
            agency=self.agency,
        )

        sync_tasks()

        target = WmsNewTask.objects.get(source_task_id=source.id)
        self.assertEqual(target.workflow_type, WmsNewTask.TYPE_ACCEPTANCE)
        self.assertEqual(target.status, WmsNewTask.STATUS_NEW)
        self.assertEqual(target.agency, self.agency)

        target.status = WmsNewTask.STATUS_DONE
        target.pilot_revision = 1
        target.save(update_fields=["status", "pilot_revision"])
        source.status = "blocked"
        source.save(update_fields=["status", "updated_at"])

        sync_tasks()

        target.refresh_from_db()
        self.assertEqual(target.status, WmsNewTask.STATUS_DONE)
        self.assertEqual(target.source_snapshot["source_status"], "blocked")

    def test_product_sync_copies_stock_and_preserves_pilot_changes(self):
        source = SKU.objects.create(
            agency=self.agency,
            sku_code="SKU-WMS-1",
            name="Исходный товар",
            weight_kg="0.250",
            width_mm=40,
            length_mm=50,
            height_mm=60,
        )
        SKUBarcode.objects.create(sku=source, value="BAR-WMS-1", is_primary=True)
        WarehouseStockSnapshot.objects.create(
            agency=self.agency,
            sku_ref=source,
            sku_code=source.sku_code,
            name=source.name,
            barcode="BAR-WMS-1",
            qty=9,
            available_qty=7,
            processing_reserved_qty=2,
        )

        result = sync_products()

        self.assertEqual(result["imported"], 1)
        target = WmsNewProduct.objects.get(source_sku_id=source.id)
        self.assertEqual(target.stock_on_hand, 9)
        self.assertEqual(target.stock_free, 7)
        self.assertEqual(target.internal_reserved, 2)
        self.assertEqual(str(target.weight_grams), "250.000")
        self.assertEqual(target.dimensions, "4*5*6")

        target.name = "Пилотное название"
        target.pilot_revision = 1
        target.save(update_fields=("name", "pilot_revision"))
        source.name = "Источник изменился"
        source.save(update_fields=("name", "updated_at"))
        sync_products()

        target.refresh_from_db()
        self.assertEqual(target.name, "Пилотное название")

    def test_acceptance_sync_is_one_way_and_preserves_pilot_state(self):
        OrderAuditEntry.objects.create(
            order_id="RCV-1",
            order_type="receiving",
            action="create",
            agency=self.agency,
            description="Создана заявка",
            payload={"title": "Тестовая приемка", "expected_qty": 12},
        )
        OrderAuditEntry.objects.create(
            order_id="RCV-1",
            order_type="receiving",
            action="status",
            agency=self.agency,
            description="Приемка завершена",
            payload={
                "status": "completed",
                "received_qty": 11,
                "expected_qty": 12,
                "receiving_mode": "scan",
            },
        )
        legacy_count = OrderAuditEntry.objects.count()

        result = sync_acceptances()

        self.assertEqual(result["imported"], 1)
        target = WmsNewAcceptance.objects.get(source_order_key="RCV-1")
        self.assertEqual(target.status, WmsNewAcceptance.STATUS_DONE)
        self.assertEqual(target.received_qty, 11)
        self.assertEqual(target.expected_qty, 12)
        self.assertEqual(target.acceptance_type, WmsNewAcceptance.TYPE_SCAN)
        self.assertEqual(OrderAuditEntry.objects.count(), legacy_count)

        target.status = WmsNewAcceptance.STATUS_CANCELLED
        target.pilot_revision = 1
        target.save(update_fields=("status", "pilot_revision"))
        sync_acceptances()
        target.refresh_from_db()
        self.assertEqual(target.status, WmsNewAcceptance.STATUS_CANCELLED)

    def test_marking_sync_is_one_way_and_preserves_pilot_state(self):
        sku = SKU.objects.create(
            agency=self.agency,
            sku_code="MARK-SKU-1",
            name="Маркируемый товар",
        )
        sync_products()
        source = MarkingCode.objects.create(
            agency=self.agency,
            sku=sku,
            sku_code=sku.sku_code,
            barcode="4600000000002",
            code="010460000000000221ABC123",
            order_type="receiving",
            order_id="RCV-MARK-1",
            source="scan",
            used_at=timezone.now(),
        )
        legacy_count = MarkingCode.objects.count()

        result = sync_marking_codes()

        self.assertEqual(result["imported"], 1)
        target = WmsNewMarkingCode.objects.get(source_code_id=source.id)
        self.assertEqual(target.product.source_sku_id, sku.id)
        self.assertEqual(target.received_reference, "RCV-MARK-1")
        self.assertIsNotNone(target.retired_at)
        self.assertEqual(MarkingCode.objects.count(), legacy_count)

        target.retired_at = None
        target.is_returned = True
        target.pilot_revision = 1
        target.save(update_fields=("retired_at", "is_returned", "pilot_revision"))
        sync_marking_codes()
        target.refresh_from_db()
        self.assertIsNone(target.retired_at)
        self.assertTrue(target.is_returned)

    def test_extra_field_sync_is_one_way_and_preserves_pilot_state(self):
        sku = SKU.objects.create(
            agency=self.agency,
            sku_code="EXTRA-SKU-1",
            name="Товар с полями",
        )
        sync_products()
        definition = GoodsExtraFieldDefinition.objects.create(
            name="Серия",
            slug="series",
            field_type=GoodsExtraFieldDefinition.TYPE_TEXT,
            sort_order=10,
        )
        source_value = GoodsExtraFieldValue.objects.create(
            sku=sku,
            definition=definition,
            value="A-1",
        )
        legacy_counts = (
            GoodsExtraFieldDefinition.objects.count(),
            GoodsExtraFieldValue.objects.count(),
        )

        result = sync_extra_fields()

        self.assertEqual(result["imported"], 2)
        target_definition = WmsNewExtraFieldDefinition.objects.get(
            source_definition_id=definition.id
        )
        target_value = WmsNewExtraFieldValue.objects.get(source_value_id=source_value.id)
        self.assertEqual(target_definition.code, "series")
        self.assertEqual(target_value.value, "A-1")
        self.assertEqual(
            legacy_counts,
            (
                GoodsExtraFieldDefinition.objects.count(),
                GoodsExtraFieldValue.objects.count(),
            ),
        )

        target_value.value = "PILOT"
        target_value.pilot_revision = 1
        target_value.save(update_fields=("value", "pilot_revision"))
        source_value.value = "SOURCE"
        source_value.save(update_fields=("value", "updated_at"))
        sync_extra_fields()
        target_value.refresh_from_db()
        self.assertEqual(target_value.value, "PILOT")


class WmsNewPickingOperationTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="wms-new-picker")
        self.agency = Agency.objects.create(agn_name="Партнер подбора FBS-NEW")
        self.sku = SKU.objects.create(
            agency=self.agency,
            sku_code="PICK-SKU-1",
            name="Товар подбора",
        )
        self.location = WarehouseLocation.objects.create(
            warehouse_code="MSK",
            zone_code="A",
            row_no=1,
            section_no=2,
            tier_no=3,
            cell_no=4,
            location_code="A-01-02-03-04",
            display_name="A-01-02-03-04",
            is_active=True,
            is_pickable=True,
            is_storage=True,
        )
        self.product = WmsNewProduct.objects.create(
            source_sku_id=self.sku.id,
            agency=self.agency,
            name=self.sku.name,
            article=self.sku.sku_code,
            barcode="4600000000777",
            stock_on_hand=5,
            stock_free=5,
        )
        self.box = WmsNewBox.objects.create(
            agency=self.agency,
            code="NEW-PICK-BOX-1",
            location=self.location,
            location_code=self.location.location_code,
            stock_on_hand=5,
            stock_free=5,
            sku_count=1,
        )
        self.box_item = WmsNewBoxItem.objects.create(
            box=self.box,
            product=self.product,
            agency=self.agency,
            sku_code=self.sku.sku_code,
            product_name=self.sku.name,
            barcode=self.product.barcode,
            qty=5,
            available_qty=5,
        )
        self.cart = FbsPickingCart.objects.create(
            barcode="FBS-CART-900010",
            name="Тележка подбора 10",
        )
        self.order = WmsNewOrder.objects.create(
            agency=self.agency,
            marketplace="wb",
            external_order_id="PICK-ORDER-1",
            tracking_number="PICK-TRACK-1",
            status=WmsNewOrder.STATUS_NEW,
            availability_state="ready",
        )
        self.item = WmsNewOrderItem.objects.create(
            order=self.order,
            sku=self.sku,
            external_line_id="PICK-LINE-1",
            external_sku=self.sku.sku_code,
            barcode=self.product.barcode,
            product_name=self.sku.name,
            quantity=2,
        )

    def test_wave_reserves_routes_picks_and_hands_actual_quantity_to_assembly(self):
        legacy_snapshots = WarehouseStockSnapshot.objects.count()
        wave = launch_wave(order_ids=[self.order.id], actor=self.user)

        line = WmsNewWavePickLine.objects.get(wave=wave, order_item=self.item)
        allocation = WmsNewWaveAllocation.objects.get(pick_line=line)
        self.box_item.refresh_from_db()
        self.product.refresh_from_db()
        self.assertEqual(allocation.source_place_code, self.location.location_code)
        self.assertEqual(allocation.reserved_quantity, 2)
        self.assertEqual(self.box_item.available_qty, 3)
        self.assertEqual(self.box_item.reserved_qty, 2)
        self.assertEqual(self.product.stock_free, 3)
        self.assertEqual(self.product.internal_reserved, 2)
        self.assertEqual(WarehouseStockSnapshot.objects.count(), legacy_snapshots)

        start_collection(wave_id=wave.id, place_scan=self.cart.barcode, actor=self.user)
        with self.assertRaises(PickingOperationError):
            scan_source_place(wave_id=wave.id, place_scan="WRONG-PLACE", actor=self.user)
        scan_source_place(
            wave_id=wave.id,
            place_scan=self.location.location_code,
            actor=self.user,
        )
        scan_picking_product(wave_id=wave.id, barcode=self.product.barcode, actor=self.user)
        scan_picking_product(wave_id=wave.id, barcode=self.product.barcode, actor=self.user)

        wave.refresh_from_db()
        line.refresh_from_db()
        self.box_item.refresh_from_db()
        self.product.refresh_from_db()
        self.order.refresh_from_db()
        self.assertEqual(wave.status, WmsNewWave.STATUS_VERIFICATION)
        self.assertEqual(wave.picked_units, 2)
        self.assertEqual(line.status, WmsNewWavePickLine.STATUS_PICKED)
        self.assertEqual(self.order.status, WmsNewOrder.STATUS_PICKED)
        self.assertEqual(self.box_item.qty, 3)
        self.assertEqual(self.box_item.reserved_qty, 0)
        self.assertEqual(self.product.stock_on_hand, 5)
        self.assertEqual(self.product.internal_reserved, 2)

        session = start_assembly_session(place_scan=self.cart.barcode, actor=self.user)
        self.assertEqual(session.planned_items, 2)
        for _ in range(2):
            assembly_line = scan_assembly_product(
                session_id=session.id,
                barcode=self.product.barcode,
                actor=self.user,
            )
            self.assertEqual(assembly_line.status, WmsNewAssemblyLine.STATUS_AWAITING_ORDER)
            scan_assembly_order_label(
                session_id=session.id,
                barcode=self.order.tracking_number,
                actor=self.user,
            )
        session.refresh_from_db()
        wave.refresh_from_db()
        self.product.refresh_from_db()
        self.assertEqual(session.status, WmsNewAssemblySession.STATUS_COMPLETED)
        self.assertEqual(wave.status, WmsNewWave.STATUS_DONE)
        self.assertEqual(self.product.stock_on_hand, 3)
        self.assertEqual(self.product.internal_reserved, 0)

    def test_cancel_wave_releases_only_fbs_new_reservation(self):
        wave = launch_wave(order_ids=[self.order.id], actor=self.user)
        legacy_snapshots = WarehouseStockSnapshot.objects.count()

        cancel_wave(wave_id=wave.id, actor=self.user)

        wave.refresh_from_db()
        self.order.refresh_from_db()
        self.box_item.refresh_from_db()
        self.product.refresh_from_db()
        self.assertEqual(wave.status, WmsNewWave.STATUS_CANCELLED)
        self.assertEqual(self.order.status, WmsNewOrder.STATUS_NEW)
        self.assertEqual(self.box_item.available_qty, 5)
        self.assertEqual(self.box_item.reserved_qty, 0)
        self.assertEqual(self.product.stock_free, 5)
        self.assertEqual(self.product.internal_reserved, 0)
        self.assertEqual(WarehouseStockSnapshot.objects.count(), legacy_snapshots)

    def test_shortage_deny_does_not_leave_wave_or_reservation(self):
        self.item.quantity = 8
        self.item.save(update_fields=("quantity",))
        WmsNewRecord.objects.create(
            module="settings",
            entity_type="system",
            source_key="fbs",
            title="fbs",
            status="active",
            payload={"insufficient_stock_wave": "deny"},
        )

        with self.assertRaises(OrderOperationError):
            launch_wave(order_ids=[self.order.id], actor=self.user)

        self.order.refresh_from_db()
        self.box_item.refresh_from_db()
        self.product.refresh_from_db()
        self.assertEqual(self.order.status, WmsNewOrder.STATUS_NEW)
        self.assertEqual(self.box_item.available_qty, 5)
        self.assertEqual(self.box_item.reserved_qty, 0)
        self.assertEqual(self.product.stock_free, 5)
        self.assertFalse(WmsNewWave.objects.exists())


class WmsNewAssemblyOperationTests(TestCase):
    def setUp(self):
        self.agency = Agency.objects.create(agn_name="Партнер сборки FBS-NEW")
        self.cart = FbsPickingCart.objects.create(
            barcode="FBS-CART-900001",
            name="Тестовое место сборки",
        )
        self.order = WmsNewOrder.objects.create(
            agency=self.agency,
            marketplace="wb",
            external_order_id="ASSEMBLY-ORDER-1",
            tracking_number="TRACK-ASSEMBLY-1",
            status=WmsNewOrder.STATUS_PICKED,
        )
        self.item = WmsNewOrderItem.objects.create(
            order=self.order,
            external_line_id="ASSEMBLY-LINE-1",
            external_sku="ASSEMBLY-SKU-1",
            barcode="ASSEMBLY-BARCODE-1",
            product_name="Товар сборки",
            quantity=1,
        )
        self.wave = WmsNewWave.objects.create(
            number="FBN-ASSEMBLY-1",
            agency=self.agency,
            status=WmsNewWave.STATUS_VERIFICATION,
            planned_orders=1,
            planned_units=1,
            picked_units=1,
            cart_name=self.cart.name,
        )
        WmsNewWaveOrder.objects.create(wave=self.wave, order=self.order, sequence=1)

    def test_scanner_builds_order_only_in_wms_new_tables(self):
        legacy_wave_count = FbsPickBatch.objects.count()
        session = start_assembly_session(place_scan=self.cart.barcode)

        self.assertEqual(session.place_name, self.cart.name)
        self.assertEqual(session.planned_items, 1)
        line = scan_assembly_product(
            session_id=session.id,
            barcode=self.item.barcode,
        )
        self.assertEqual(line.status, WmsNewAssemblyLine.STATUS_AWAITING_ORDER)
        with self.assertRaises(AssemblyOperationError):
            scan_assembly_order_label(session_id=session.id, barcode="WRONG")

        line = scan_assembly_order_label(
            session_id=session.id,
            barcode=self.order.tracking_number,
        )

        line.refresh_from_db()
        session.refresh_from_db()
        self.order.refresh_from_db()
        self.wave.refresh_from_db()
        self.assertEqual(line.status, WmsNewAssemblyLine.STATUS_ASSEMBLED)
        self.assertEqual(session.status, WmsNewAssemblySession.STATUS_COMPLETED)
        self.assertEqual(session.assembled_items, 1)
        self.assertEqual(self.order.status, WmsNewOrder.STATUS_READY)
        self.assertEqual(self.wave.status, WmsNewWave.STATUS_DONE)
        self.assertGreater(self.order.pilot_revision, 0)
        self.assertGreater(self.wave.pilot_revision, 0)
        self.assertEqual(FbsPickBatch.objects.count(), legacy_wave_count)
        self.assertTrue(
            WmsNewEvent.objects.filter(
                entity_type="assembly_line", entity_id=line.id, action="assemble_good"
            ).exists()
        )

    def test_problem_good_marks_only_new_order_and_records_place(self):
        session = start_assembly_session(place_scan=self.cart.name)
        line = scan_assembly_product(session_id=session.id, barcode=self.item.barcode)

        moved = move_assembly_problem(
            session_id=session.id,
            line_id=line.id,
            problem_place="PROBLEM-PLACE-1",
            reason="Повреждение",
        )

        moved.refresh_from_db()
        session.refresh_from_db()
        self.order.refresh_from_db()
        self.assertEqual(moved.status, WmsNewAssemblyLine.STATUS_PROBLEM)
        self.assertEqual(moved.problem_place, "PROBLEM-PLACE-1")
        self.assertEqual(self.order.status, WmsNewOrder.STATUS_EXCEPTION)
        self.assertEqual(session.status, WmsNewAssemblySession.STATUS_COMPLETED)
        self.assertEqual(session.problem_items, 1)

    def test_required_marking_blocks_product_until_value_is_supplied(self):
        self.item.requirements = {"need_marking": True}
        self.item.save(update_fields=["requirements"])
        session = start_assembly_session(place_scan=self.cart.name)

        with self.assertRaises(AssemblyOperationError):
            scan_assembly_product(session_id=session.id, barcode=self.item.barcode)

        line = scan_assembly_product(
            session_id=session.id,
            barcode=self.item.barcode,
            extra_data={"marking_code": "KIZ-001"},
        )
        self.assertEqual(line.extra_data["marking_code"], "KIZ-001")


class WmsNewProductOperationTests(TestCase):
    def setUp(self):
        self.agency = Agency.objects.create(agn_name="Партнер товаров")

    def test_product_operations_write_only_wms_new_tables_and_audit(self):
        legacy_before = SKU.objects.count()
        first = create_product(
            agency=self.agency,
            name="Первый товар",
            article="NEW-1",
            barcode="111",
            weight_grams="100",
        )
        second = create_product(
            agency=self.agency,
            name="Второй товар",
            article="NEW-2",
            barcode="222",
            weight_grams="200",
        )
        update_product(product_id=first.id, name="Первый новый", category="Тест")
        assign_category(product_ids=[first.id, second.id], category="Общая")
        target = merge_products(product_ids=[first.id, second.id], target_id=first.id)

        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(SKU.objects.count(), legacy_before)
        self.assertEqual(target.id, first.id)
        self.assertEqual(first.category, "Общая")
        self.assertTrue(second.is_archived)
        self.assertEqual(second.merged_into_id, first.id)
        self.assertGreaterEqual(WmsNewEvent.objects.filter(entity_type="product").count(), 6)

    def test_marking_operations_write_only_wms_new_registry_and_audit(self):
        product = create_product(
            agency=self.agency,
            name="Товар с КИЗ",
            article="MARK-NEW-1",
            barcode="4600000000003",
        )
        legacy_before = MarkingCode.objects.count()
        items = add_codes(
            agency=self.agency,
            product=product,
            codes="CODE-NEW-1\nCODE-NEW-2",
        )
        mark_printed(code_ids=[items[0].id])
        return_codes(code_ids=[items[0].id])

        items[0].refresh_from_db()
        self.assertEqual(MarkingCode.objects.count(), legacy_before)
        self.assertEqual(items[0].print_count, 1)
        self.assertTrue(items[0].is_returned)
        self.assertIsNone(items[0].retired_at)
        self.assertEqual(
            WmsNewEvent.objects.filter(entity_type="marking_code", entity_id=items[0].id).count(),
            3,
        )

    def test_extra_field_operations_write_only_wms_new_tables_and_audit(self):
        product = create_product(
            agency=self.agency,
            name="Товар для поля",
            article="EXTRA-NEW-1",
        )
        legacy_before = (
            GoodsExtraFieldDefinition.objects.count(),
            GoodsExtraFieldValue.objects.count(),
        )
        definition = create_definition(
            name="Партия",
            code="batch",
            field_type=WmsNewExtraFieldDefinition.TYPE_TEXT,
            sort_order=5,
        )
        value = set_product_value(
            definition_id=definition.id,
            product_id=product.id,
            raw_value="B-7",
        )
        set_definition_active(definition_id=definition.id, active=False)

        definition.refresh_from_db()
        self.assertEqual(value.value, "B-7")
        self.assertFalse(definition.is_active)
        self.assertEqual(
            legacy_before,
            (
                GoodsExtraFieldDefinition.objects.count(),
                GoodsExtraFieldValue.objects.count(),
            ),
        )
        self.assertEqual(
            WmsNewEvent.objects.filter(entity_type__startswith="extra_field").count(),
            3,
        )


class WmsNewInventoryOperationTests(TestCase):
    def setUp(self):
        self.agency = Agency.objects.create(agn_name="Партнер инвентаризации")
        self.sku = SKU.objects.create(
            agency=self.agency,
            sku_code="INV-NEW-SKU-1",
            name="Товар инвентаризации",
        )
        self.product = WmsNewProduct.objects.create(
            source_sku_id=self.sku.id,
            agency=self.agency,
            article=self.sku.sku_code,
            name=self.sku.name,
            barcode="4600000000999",
            stock_on_hand=2,
            stock_free=2,
        )
        location = WarehouseLocation.objects.create(
            zone_code="FBS",
            location_code="FBS-NEW-INV-1",
            display_name="Ячейка новой инвентаризации",
            is_storage=True,
        )
        self.cell = FbsStorageCell.objects.create(
            cell_code="FBS-NEW-INV-1",
            location=location,
        )
        self.pallet = FbsPallet.objects.create(
            agency=self.agency,
            pallet_code="FBS-NEW-INV-PALLET-1",
            cell=self.cell,
        )
        self.box = FbsBox.objects.create(
            agency=self.agency,
            pallet=self.pallet,
            box_code="FBS-NEW-INV-BOX-1",
        )
        self.balance = FbsStockBalance.objects.create(
            agency=self.agency,
            box=self.box,
            sku_ref=self.sku,
            identity_key="new-inventory-balance",
            sku_code=self.sku.sku_code,
            name=self.sku.name,
            barcode="4600000000999",
            qty=2,
            available_qty=2,
        )
        self.first_user = get_user_model().objects.create_user(username="new_inv_first")
        self.second_user = get_user_model().objects.create_user(username="new_inv_second")
        self.manager_user = get_user_model().objects.create_user(username="new_inv_manager")
        Employee.objects.create(
            user=self.first_user,
            full_name="Первый счетчик",
            role="storekeeper",
        )
        Employee.objects.create(
            user=self.second_user,
            full_name="Второй счетчик",
            role="storekeeper",
        )
        Employee.objects.create(
            user=self.manager_user,
            full_name="Начальник склада",
            role="head_manager",
        )

    def test_inventory_adjusts_only_wms_new_stock_and_keeps_legacy_balance(self):
        legacy_qty = self.balance.qty
        session = create_inventory(
            scope_type=WmsNewInventorySession.SCOPE_BOX,
            mode=WmsNewInventorySession.MODE_IMMEDIATE,
            scan_mode=WmsNewInventorySession.SCAN_MODE_BARCODE,
            box=self.box,
            agency=self.agency,
            actor=self.manager_user,
        )
        self.assertEqual(session.lines.count(), 1)
        self.assertTrue(session.storage_lock.is_active)

        record_inventory_scan(
            session_id=session.id,
            scan_code=self.balance.barcode,
            actor=self.first_user,
        )
        session = finish_inventory_count(session_id=session.id, actor=self.first_user)
        self.assertEqual(session.status, WmsNewInventorySession.STATUS_RECOUNT)

        record_inventory_scan(
            session_id=session.id,
            scan_code=self.balance.barcode,
            actor=self.second_user,
        )
        session = finish_inventory_count(session_id=session.id, actor=self.second_user)
        self.assertEqual(session.status, WmsNewInventorySession.STATUS_APPROVAL)
        approve_inventory(session_id=session.id, actor=self.manager_user)

        self.balance.refresh_from_db()
        self.product.refresh_from_db()
        session.refresh_from_db()
        self.assertEqual(self.balance.qty, legacy_qty)
        self.assertEqual(self.product.stock_on_hand, 1)
        self.assertEqual(self.product.stock_free, 1)
        self.assertGreater(self.product.pilot_revision, 0)
        self.assertEqual(session.status, WmsNewInventorySession.STATUS_DONE)
        self.assertFalse(session.storage_lock.is_active)
        self.assertGreaterEqual(
            WmsNewEvent.objects.filter(entity_type="inventory", entity_id=session.id).count(),
            6,
        )

    def test_inventory_sync_copies_legacy_history_one_way(self):
        source = FbsInventorySession.objects.create(
            scope_type=FbsInventorySession.SCOPE_BOX,
            mode=FbsInventorySession.MODE_AUDIT,
            scan_mode=FbsInventorySession.SCAN_MODE_BARCODE,
            status=FbsInventorySession.STATUS_COUNTING,
            agency=self.agency,
            box=self.box,
            created_by=self.manager_user,
        )
        source_line = FbsInventoryLine.objects.create(
            session=source,
            balance=self.balance,
            expected_qty=2,
            first_count_qty=1,
        )
        legacy_counts = (FbsInventorySession.objects.count(), FbsInventoryLine.objects.count())

        result = sync_inventories()

        target = WmsNewInventorySession.objects.get(source_session_id=source.id)
        target_line = WmsNewInventoryLine.objects.get(source_line_id=source_line.id)
        self.assertEqual(result["imported"], 2)
        self.assertEqual(target.scope_type, WmsNewInventorySession.SCOPE_BOX)
        self.assertEqual(target.status, WmsNewInventorySession.STATUS_COUNTING)
        self.assertEqual(target_line.product, self.product)
        self.assertEqual(target_line.expected_qty, 2)
        self.assertEqual(
            legacy_counts,
            (FbsInventorySession.objects.count(), FbsInventoryLine.objects.count()),
        )

    def test_inventory_lock_blocks_only_new_wave_operations(self):
        session = create_inventory(
            scope_type=WmsNewInventorySession.SCOPE_BOX,
            mode=WmsNewInventorySession.MODE_IMMEDIATE,
            scan_mode=WmsNewInventorySession.SCAN_MODE_BARCODE,
            box=self.box,
            agency=self.agency,
            actor=self.manager_user,
        )
        order = WmsNewOrder.objects.create(
            agency=self.agency,
            external_order_id="INV-LOCK-ORDER-1",
            status=WmsNewOrder.STATUS_NEW,
            availability_state="ready",
        )
        WmsNewOrderItem.objects.create(
            order=order,
            sku=self.sku,
            external_line_id="INV-LOCK-LINE-1",
            external_sku=self.sku.sku_code,
            barcode=self.balance.barcode,
            product_name=self.sku.name,
            quantity=1,
        )

        with self.assertRaisesMessage(OrderOperationError, session.number):
            launch_wave(order_ids=[order.id], actor=self.manager_user)

        order.refresh_from_db()
        self.balance.refresh_from_db()
        self.assertEqual(order.status, WmsNewOrder.STATUS_NEW)
        self.assertEqual(self.balance.qty, 2)

    def test_box_sync_and_operations_never_change_legacy_container(self):
        source_box = WarehouseContainer.objects.create(
            agency=self.agency,
            container_type=WarehouseContainer.TYPE_BOX,
            container_code="GENERAL-BOX-1",
            current_location=self.cell.location,
        )
        source_snapshot = WarehouseStockSnapshot.objects.create(
            agency=self.agency,
            sku_ref=self.sku,
            sku_code=self.sku.sku_code,
            name=self.sku.name,
            barcode=self.balance.barcode,
            qty=5,
            available_qty=4,
            processing_reserved_qty=1,
            container=source_box,
            container_code=source_box.container_code,
            location=self.cell.location,
            zone_code=self.cell.location.zone_code,
        )

        result = sync_boxes()

        target = WmsNewBox.objects.get(source_container_id=source_box.id)
        target_item = WmsNewBoxItem.objects.get(source_snapshot_id=source_snapshot.id)
        self.assertEqual(result["imported"], 2)
        self.assertEqual(target.stock_on_hand, 5)
        self.assertEqual(target.stock_free, 4)
        self.assertEqual(target.reserved_qty, 1)
        self.assertEqual(target_item.product, self.product)

        second_location = WarehouseLocation.objects.create(
            zone_code="FBS",
            row_no=2,
            section_no=1,
            tier_no=1,
            cell_no=1,
            location_code="FBS-NEW-INV-2",
            display_name="Вторая ячейка нового короба",
            is_storage=True,
        )
        update_box(
            box_id=target.id,
            code=target.code,
            location=second_location,
            actor=self.first_user,
        )
        source_box.refresh_from_db()
        target.refresh_from_db()
        self.assertEqual(source_box.current_location_id, self.cell.location_id)
        self.assertEqual(target.location_id, second_location.id)
        self.assertGreater(target.pilot_revision, 0)
        repeated = sync_boxes()
        target.refresh_from_db()
        self.assertEqual(repeated, {"imported": 0, "updated": 0})
        self.assertEqual(target.location_id, second_location.id)

        legacy_count = WarehouseContainer.objects.count()
        manual = create_box(
            agency=self.agency,
            code="WMS-NEW-ONLY-BOX",
            location=second_location,
            actor=self.manager_user,
        )
        self.assertEqual(WarehouseContainer.objects.count(), legacy_count)
        self.assertTrue(manual.is_manual)
        self.assertTrue(
            WmsNewEvent.objects.filter(entity_type="box", entity_id=manual.id).exists()
        )


class WmsNewDonorParityContractTests(TestCase):
    def setUp(self):
        self.user, _ = get_user_model().objects.get_or_create(username="dev")
        self.agency = Agency.objects.create(agn_name="Партнер сквозного контракта")

    def _render(self, section="home"):
        from head_manager.fbs_new import HeadManagerFbsNewView

        path = "/head-manager/fbs-new/" if section == "home" else f"/head-manager/fbs-new/{section}/"
        request = RequestFactory().get(path)
        request.user = self.user
        request.session = {}
        kwargs = {} if section == "home" else {"section": section}
        response = HeadManagerFbsNewView.as_view()(request, **kwargs)
        response.render()
        return response, response.content.decode("utf-8")

    def test_all_menu_routes_render_inside_new_contour(self):
        from head_manager.fbs_new import WMS_NEW_MODULES

        sections = ["home"]
        for module in WMS_NEW_MODULES:
            if module["slug"] != "home":
                sections.append(module["slug"])
            sections.extend(child["slug"] for child in module.get("children", ()))
        self.assertEqual(len(sections), 74)
        for section in sections:
            with self.subTest(section=section):
                response, content = self._render(section)
                self.assertEqual(response.status_code, 200)
                self.assertNotIn("/head-manager/fbs/", content)

    def test_confirmed_donor_gaps_have_exact_structural_contracts(self):
        expectations = {
            "home": (
                "SLA приемки поставок",
                "SLA по заказам",
                "Заказы на контроле",
                "Таймер дедлайна заказов",
                "Настроить дашборд",
            ),
            "problems": (
                "Тип проблемы",
                "Статус",
                "Создана",
                "Решена",
                "Пользователь",
                "Описание",
            ),
            "tasks-acceptance": (
                "Новая",
                "Обработка разрешена",
                "Приемка товара",
                "Приемка завершена",
                "Размещение на хранение",
            ),
            "tasks-processing": (
                "Подбор товара",
                "Обработка завершена",
                "Размещение на хранение",
            ),
            "tasks-shipment": (
                "Подбор товара",
                "Ожидает отгрузки",
                "Доставляется",
            ),
            "tasks-other": ("Новая", "Обработка разрешена", "Обработка"),
            "tasks-multiacceptance": (
                "Добавить мультиприемку",
                "ID задачи или Название, артикул, ID или ШК товара",
                "Кол-во партнеров",
                "Кол-во SKU",
                "Кол-во товара",
            ),
            "warehouse-goods": (
                "FBS-NEW: синхронизировано",
                "Добавить товар",
                "Экспорт в EXCEL",
                "Импорт из EXCEL",
                "Объединить товары",
                "Действия с выбранными",
            ),
            "instructions": (
                "ПЕРВИЧНАЯ НАСТРОЙКА",
                "СБОРКА НЕСКОЛЬКИХ ЗАКАЗОВ",
                "ОБРАБОТКА ВОЗВРАТОВ",
                "ПЛАНИРОВАНИЕ ЗАКУПОК",
            ),
        }
        for section, needles in expectations.items():
            with self.subTest(section=section):
                _, content = self._render(section)
                for needle in needles:
                    self.assertIn(needle, content)

    def test_board_transition_is_audited_and_source_task_is_unchanged(self):
        source = Task.objects.create(title="Исходная задача", status="backlog")
        task = WmsNewTask.objects.create(
            source_task_id=source.id,
            agency=self.agency,
            workflow_type=WmsNewTask.TYPE_ACCEPTANCE,
            title=source.title,
            status=WmsNewTask.STATUS_NEW,
            source_snapshot={"sku_count": 2, "unit_count": 7},
        )
        apply_board_action(task_id=task.id, action="advance", actor=self.user)
        task.refresh_from_db()
        source.refresh_from_db()
        self.assertEqual(board_stage_for(task), "allowed")
        self.assertEqual(task.status, WmsNewTask.STATUS_IN_PROGRESS)
        self.assertEqual(source.status, "backlog")
        self.assertTrue(
            WmsNewEvent.objects.filter(
                entity_type="task", entity_id=task.id, action="board_advance"
            ).exists()
        )

    def test_multiacceptance_and_problem_actions_write_only_shadow_records(self):
        self.client.force_login(self.user)
        old_counts = (Task.objects.count(), FbsOrder.objects.count())
        response = self.client.post(
            "/head-manager/fbs-new/tasks/multiacceptance/action/",
            {
                "action": "create",
                "title": "Общая приемка",
                "partner_ids": [str(self.agency.id)],
                "sku_count": "3",
                "unit_count": "12",
            },
        )
        self.assertEqual(response.status_code, 302)
        multi = WmsNewRecord.objects.get(module="tasks", entity_type="multiacceptance")
        self.assertEqual(multi.payload["unit_count"], 12)
        problem_response = self.client.post(
            "/head-manager/fbs-new/problems/action/",
            {"source_key": "task:991", "status": "resolved"},
        )
        self.assertEqual(problem_response.status_code, 302)
        self.assertEqual(
            WmsNewRecord.objects.get(
                module="problems", entity_type="problem_state", source_key="task:991"
            ).status,
            "resolved",
        )
        self.assertEqual(old_counts, (Task.objects.count(), FbsOrder.objects.count()))
        self.assertGreaterEqual(WmsNewEvent.objects.count(), 2)

    def test_acceptance_row_opens_donor_style_terminal_tabs(self):
        acceptance = WmsNewAcceptance.objects.create(
            source_order_key="ACC-DEEP-1",
            agency=self.agency,
            task_number="619",
            title="Приемка для FBS",
            received_qty=4,
            expected_qty=4,
            status=WmsNewAcceptance.STATUS_DONE,
            source_snapshot={
                "payload": {
                    "items": [
                        {
                            "id": 1,
                            "name": "Товар приемки",
                            "article": "ACC-1",
                            "barcode": "4600000000001",
                            "planned_qty": 4,
                            "actual_qty": 4,
                        }
                    ]
                }
            },
        )
        from head_manager.fbs_new import HeadManagerFbsNewView

        request = RequestFactory().get(
            f"/head-manager/fbs-new/warehouse-acceptances/?acceptance_id={acceptance.id}&tab=goods"
        )
        request.user = self.user
        request.session = {}
        response = HeadManagerFbsNewView.as_view()(request, section="warehouse-acceptances")
        content = response.render().content.decode("utf-8")
        self.assertIn("Принятые товары", content)
        self.assertIn("Товар приемки", content)
        self.assertIn("Маркировка", content)
        self.assertIn("Приемка", content)
