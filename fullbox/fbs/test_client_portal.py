from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone

from sklad.models import (
    WarehouseContainer,
    WarehouseEvent,
    WarehouseLocation,
    WarehouseOperation,
    WarehouseReserve,
    WarehouseStockSnapshot,
)
from sku.models import Agency, SKU, SKUBarcode

from .client_portal import (
    client_overview_payload,
    client_order_detail,
    client_orders_payload,
    client_movement_source_stock_payload,
    client_reports_payload,
    create_client_movement_request,
    validate_client_movement_lines,
)
from .models import (
    FbsClientMovementRequest,
    FbsIntegrationProfile,
    FbsOrder,
    FbsOrderItem,
)


class FbsClientPortalMovementTests(TestCase):
    def setUp(self):
        self.agency = Agency.objects.create(agn_name="Клиент FBS")
        self.other_agency = Agency.objects.create(agn_name="Другой клиент")
        self.sku = SKU.objects.create(
            agency=self.agency,
            sku_code="SKU-1",
            name="Тестовый товар",
            brand="Основной бренд",
        )
        SKUBarcode.objects.create(sku=self.sku, value="4600000000001", is_primary=True)
        self.location = WarehouseLocation.objects.create(
            warehouse_code="MSK",
            zone_code="STORAGE",
            zone_kind=WarehouseLocation.ZONE_KIND_STORAGE,
            location_code="A-01-01",
            row_no=1,
            section_no=1,
            tier_no=1,
            cell_no=1,
        )
        WarehouseStockSnapshot.objects.create(
            agency=self.agency,
            sku_ref=self.sku,
            sku_code=self.sku.sku_code,
            name=self.sku.name,
            barcode="4600000000001",
            goods_type="gv",
            qty=30,
            available_qty=30,
            location=self.location,
            zone_code="STORAGE",
        )

    def _whole_box(self, *, code: str, qty: int):
        container = WarehouseContainer.objects.create(
            agency=self.agency,
            container_type=WarehouseContainer.TYPE_BOX,
            container_code=code,
            current_location=self.location,
        )
        return WarehouseStockSnapshot.objects.create(
            agency=self.agency,
            sku_ref=self.sku,
            sku_code=self.sku.sku_code,
            name=self.sku.name,
            barcode="4600000000001",
            goods_type="gv",
            qty=qty,
            available_qty=qty,
            container=container,
            container_code=container.container_code,
            location=self.location,
            zone_code="STORAGE",
        )

    def test_piece_request_is_saved_without_warehouse_writes(self):
        request_row = create_client_movement_request(
            agency=self.agency,
            mode=FbsClientMovementRequest.MODE_ITEM,
            raw_lines=[{"barcode": "4600000000001", "qty": 7, "box_count": 2}],
            comment="Пополнить FBS",
        )

        self.assertEqual(request_row.status, FbsClientMovementRequest.STATUS_APPROVED)
        self.assertEqual(request_row.requested_qty, 7)
        self.assertEqual(request_row.requested_box_count, 2)
        line = request_row.lines.get()
        self.assertEqual(line.general_available_qty_snapshot, 30)
        self.assertEqual(line.units_per_box, 1)
        self.assertEqual(line.requested_box_count, 2)
        self.assertFalse(WarehouseReserve.objects.exists())
        self.assertFalse(WarehouseOperation.objects.exists())
        self.assertFalse(WarehouseEvent.objects.exists())

    def test_piece_request_validates_new_box_count(self):
        rows, errors = validate_client_movement_lines(
            agency=self.agency,
            mode=FbsClientMovementRequest.MODE_ITEM,
            raw_lines=[{"barcode": "4600000000001", "qty": 2, "box_count": 3}],
        )

        self.assertEqual(rows, [])
        self.assertTrue(any("не может превышать" in message for message in errors))

        rows, errors = validate_client_movement_lines(
            agency=self.agency,
            mode=FbsClientMovementRequest.MODE_ITEM,
            raw_lines=[{"barcode": "4600000000001", "qty": 2, "box_count": 0}],
        )
        self.assertEqual(rows, [])
        self.assertTrue(any("Количество коробов" in message for message in errors))

        rows, errors = validate_client_movement_lines(
            agency=self.agency,
            mode=FbsClientMovementRequest.MODE_ITEM,
            raw_lines=[{"barcode": "4600000000001", "qty": 60, "box_count": 51}],
        )
        self.assertEqual(len(rows), 1)
        self.assertTrue(any("не более 50" in message for message in errors))

    def test_piece_request_uses_one_request_level_mixed_box_for_two_skus(self):
        second_sku = SKU.objects.create(
            agency=self.agency,
            sku_code="SKU-2",
            name="Второй товар",
        )
        SKUBarcode.objects.create(
            sku=second_sku,
            value="4600000000002",
            is_primary=True,
        )
        WarehouseStockSnapshot.objects.create(
            agency=self.agency,
            sku_ref=second_sku,
            sku_code=second_sku.sku_code,
            name=second_sku.name,
            barcode="4600000000002",
            goods_type="gv",
            qty=10,
            available_qty=10,
            location=self.location,
            zone_code="STORAGE",
        )

        request_row = create_client_movement_request(
            agency=self.agency,
            mode=FbsClientMovementRequest.MODE_ITEM,
            raw_lines=[
                {"barcode": "4600000000001", "qty": 1},
                {"barcode": "4600000000002", "qty": 1},
            ],
            requested_box_count=1,
            requested_mixed_box_count=1,
        )

        self.assertEqual(request_row.requested_qty, 2)
        self.assertEqual(request_row.requested_box_count, 1)
        self.assertEqual(request_row.requested_mixed_box_count, 1)
        self.assertEqual(
            list(request_row.lines.values_list("requested_box_count", flat=True)),
            [0, 0],
        )
        self.assertFalse(WarehouseReserve.objects.exists())
        self.assertFalse(WarehouseOperation.objects.exists())
        self.assertFalse(WarehouseEvent.objects.exists())

    def test_request_level_mixed_box_plan_rejects_impossible_values(self):
        with self.assertRaisesMessage(
            ValidationError,
            "Микс-короб возможен только",
        ):
            create_client_movement_request(
                agency=self.agency,
                mode=FbsClientMovementRequest.MODE_ITEM,
                raw_lines=[{"barcode": "4600000000001", "qty": 2}],
                requested_box_count=1,
                requested_mixed_box_count=1,
            )

        with self.assertRaisesMessage(
            ValidationError,
            "не может превышать общее количество коробов",
        ):
            create_client_movement_request(
                agency=self.agency,
                mode=FbsClientMovementRequest.MODE_ITEM,
                raw_lines=[{"barcode": "4600000000001", "qty": 2}],
                requested_box_count=1,
                requested_mixed_box_count=2,
            )

    def test_box_request_enforces_multiplicity(self):
        self._whole_box(code="PORTAL-BOX-12-A", qty=12)
        self._whole_box(code="PORTAL-BOX-12-B", qty=12)

        with self.assertRaises(ValidationError):
            create_client_movement_request(
                agency=self.agency,
                mode=FbsClientMovementRequest.MODE_BOX,
                raw_lines=[
                    {"barcode": "4600000000001", "qty": 25, "units_per_box": 12}
                ],
            )

        request_row = create_client_movement_request(
            agency=self.agency,
            mode=FbsClientMovementRequest.MODE_BOX,
            raw_lines=[{"barcode": "4600000000001", "qty": 24, "units_per_box": 12}],
        )
        self.assertEqual(request_row.requested_qty, 24)
        self.assertEqual(request_row.requested_box_count, 2)
        self.assertEqual(request_row.lines.get().requested_box_count, 2)

    def test_box_request_rejects_arbitrary_multiple_and_payload_lists_real_boxes(self):
        self._whole_box(code="PORTAL-BOX-12-A", qty=12)
        self._whole_box(code="PORTAL-BOX-12-B", qty=12)

        payload = client_movement_source_stock_payload(
            agency=self.agency,
            include_box_options=True,
        )

        self.assertEqual(
            payload["results"][0]["box_options"],
            [
                {
                    "units_per_box": 12,
                    "physical_box_count": 2,
                    "available_box_count": 4,
                    "available_qty": 48,
                }
            ],
        )
        with self.assertRaisesMessage(
            ValidationError,
            "Выберите доступную кратность целого короба",
        ):
            create_client_movement_request(
                agency=self.agency,
                mode=FbsClientMovementRequest.MODE_BOX,
                raw_lines=[
                    {"barcode": "4600000000001", "qty": 6, "units_per_box": 6}
                ],
            )

    def test_box_request_uses_available_stock_when_exact_container_is_mixed(self):
        source = self._whole_box(code="PORTAL-MIXED-BOX", qty=12)
        other_sku = SKU.objects.create(
            agency=self.agency,
            sku_code="SKU-MIXED",
            name="Товар в том же коробе",
        )
        SKUBarcode.objects.create(
            sku=other_sku,
            value="4600000000012",
            is_primary=True,
        )
        WarehouseStockSnapshot.objects.create(
            agency=self.agency,
            sku_ref=other_sku,
            sku_code=other_sku.sku_code,
            name=other_sku.name,
            barcode="4600000000012",
            goods_type="gv",
            qty=1,
            available_qty=1,
            container=source.container,
            container_code=source.container_code,
            location=self.location,
            zone_code="STORAGE",
        )

        row = client_movement_source_stock_payload(
            agency=self.agency,
            include_box_options=True,
        )["results"][0]

        self.assertEqual(
            row["box_options"],
            [
                {
                    "units_per_box": 12,
                    "physical_box_count": 0,
                    "available_box_count": 3,
                    "available_qty": 36,
                }
            ],
        )
        request_row = create_client_movement_request(
            agency=self.agency,
            mode=FbsClientMovementRequest.MODE_BOX,
            raw_lines=[{"barcode": "4600000000001", "qty": 36, "units_per_box": 12}],
        )
        self.assertEqual(request_row.requested_box_count, 3)

    def test_open_request_limits_number_of_selectable_whole_boxes(self):
        self._whole_box(code="PORTAL-BOX-12-A", qty=12)
        self._whole_box(code="PORTAL-BOX-12-B", qty=12)
        create_client_movement_request(
            agency=self.agency,
            mode=FbsClientMovementRequest.MODE_ITEM,
            raw_lines=[{"barcode": "4600000000001", "qty": 31}],
        )

        row = client_movement_source_stock_payload(
            agency=self.agency,
            include_box_options=True,
        )["results"][0]

        self.assertEqual(row["client_available_qty"], 23)
        self.assertEqual(row["box_options"][0]["physical_box_count"], 2)
        self.assertEqual(row["box_options"][0]["available_box_count"], 1)

    def test_matching_is_only_by_client_barcode(self):
        other_sku = SKU.objects.create(
            agency=self.other_agency,
            sku_code="OTHER",
            name="Чужой товар",
        )
        SKUBarcode.objects.create(sku=other_sku, value="9999999999999", is_primary=True)

        rows, errors = validate_client_movement_lines(
            agency=self.agency,
            mode=FbsClientMovementRequest.MODE_ITEM,
            raw_lines=[{"barcode": "9999999999999", "qty": 1}],
        )

        self.assertEqual(rows, [])
        self.assertTrue(any("не найден" in message for message in errors))

    def test_request_rejects_quantity_above_current_general_stock(self):
        rows, errors = validate_client_movement_lines(
            agency=self.agency,
            mode=FbsClientMovementRequest.MODE_ITEM,
            raw_lines=[{"barcode": "4600000000001", "qty": 31}],
        )

        self.assertEqual(len(rows), 1)
        self.assertTrue(any("доступно 30" in message for message in errors))

    def test_open_request_reduces_only_client_available_stock(self):
        request_row = create_client_movement_request(
            agency=self.agency,
            mode=FbsClientMovementRequest.MODE_ITEM,
            raw_lines=[{"barcode": "4600000000001", "qty": 7}],
        )

        payload = client_movement_source_stock_payload(agency=self.agency)
        row = payload["results"][0]
        self.assertEqual(row["warehouse_fact_qty"], 30)
        self.assertEqual(row["warehouse_available_qty"], 30)
        self.assertEqual(row["pending_movement_qty"], 7)
        self.assertEqual(row["client_available_qty"], 23)
        snapshot = WarehouseStockSnapshot.objects.get()
        self.assertEqual(snapshot.qty, 30)
        self.assertEqual(snapshot.available_qty, 30)
        self.assertFalse(WarehouseReserve.objects.exists())

        with self.assertRaises(ValidationError):
            create_client_movement_request(
                agency=self.agency,
                mode=FbsClientMovementRequest.MODE_ITEM,
                raw_lines=[{"barcode": "4600000000001", "qty": 24}],
            )

        request_row.status = FbsClientMovementRequest.STATUS_REJECTED
        request_row.save(update_fields=["status", "updated_at"])
        released = client_movement_source_stock_payload(agency=self.agency)["results"][0]
        self.assertEqual(released["pending_movement_qty"], 0)
        self.assertEqual(released["client_available_qty"], 30)

    def test_warehouse_reserve_is_not_subtracted_twice_and_request_stays_open(self):
        request_row = create_client_movement_request(
            agency=self.agency,
            mode=FbsClientMovementRequest.MODE_ITEM,
            raw_lines=[{"barcode": "4600000000001", "qty": 7}],
        )
        snapshot = WarehouseStockSnapshot.objects.get()
        snapshot.available_qty = 23
        snapshot.other_reserved_qty = 7
        snapshot.save(
            update_fields=["available_qty", "other_reserved_qty", "updated_at"]
        )
        request_row.status = FbsClientMovementRequest.STATUS_WAREHOUSE_ACCEPTED
        request_row.save(update_fields=["status", "updated_at"])

        row = client_movement_source_stock_payload(agency=self.agency)["results"][0]

        self.assertEqual(row["warehouse_available_qty"], 23)
        self.assertEqual(row["pending_movement_qty"], 0)
        self.assertEqual(row["client_available_qty"], 23)
        self.assertEqual(
            client_overview_payload(agency=self.agency)["movement_requests_open"],
            1,
        )

    def test_fbs_zone_stock_is_not_mixed_into_general_movement_source(self):
        fbs_location = WarehouseLocation.objects.create(
            warehouse_code="MSK",
            zone_code="FBS",
            zone_kind=WarehouseLocation.ZONE_KIND_STORAGE,
            location_code="FBS-A-01",
            row_no=2,
            section_no=1,
            tier_no=1,
            cell_no=1,
        )
        WarehouseStockSnapshot.objects.create(
            agency=self.agency,
            sku_ref=self.sku,
            sku_code=self.sku.sku_code,
            name=self.sku.name,
            barcode="4600000000001",
            goods_type="gv",
            qty=11,
            available_qty=11,
            location=fbs_location,
            zone_code="FBS",
        )

        payload = client_movement_source_stock_payload(agency=self.agency)

        self.assertEqual(payload["results"][0]["warehouse_fact_qty"], 30)
        self.assertEqual(payload["results"][0]["client_available_qty"], 30)
        self.assertEqual(payload["summary"]["warehouse_fact_qty"], 30)

    def test_movement_source_filters_catalog_by_article_brand_and_barcode(self):
        other_sku = SKU.objects.create(
            agency=self.agency,
            sku_code="SKU-OTHER",
            name="Другой товар",
            brand="Другой бренд",
        )
        SKUBarcode.objects.create(
            sku=other_sku,
            value="4600000000099",
            is_primary=True,
        )
        WarehouseStockSnapshot.objects.create(
            agency=self.agency,
            sku_ref=other_sku,
            sku_code=other_sku.sku_code,
            name=other_sku.name,
            barcode="4600000000099",
            goods_type="gv",
            qty=5,
            available_qty=5,
            location=self.location,
            zone_code="STORAGE",
        )

        for filters in (
            {"article": "SKU-1"},
            {"brand": "Основной"},
            {"barcode": "000001"},
        ):
            with self.subTest(filters=filters):
                filtered = client_movement_source_stock_payload(
                    agency=self.agency,
                    **filters,
                )
                self.assertEqual(filtered["total"], 1, filtered)

        payload = client_movement_source_stock_payload(
            agency=self.agency,
            article="SKU-1",
            brand="Основной",
            barcode="000001",
        )

        self.assertEqual(payload["total"], 1)
        self.assertEqual(payload["results"][0]["barcode"], "4600000000001")
        self.assertEqual(payload["results"][0]["brand"], "Основной бренд")
        self.assertEqual(
            client_movement_source_stock_payload(
                agency=self.agency,
                article="SKU-1",
                brand="Другой бренд",
            )["total"],
            0,
        )


class FbsClientPortalOrderTests(TestCase):
    def setUp(self):
        self.agency = Agency.objects.create(agn_name="Клиент заказов")
        self.other_agency = Agency.objects.create(agn_name="Другой клиент заказов")
        self.profile = FbsIntegrationProfile.objects.create(
            agency=self.agency,
            marketplace=FbsIntegrationProfile.MARKETPLACE_WB,
            name="WB основной",
            external_warehouse_id="1931120",
            is_active=True,
        )
        self.other_profile = FbsIntegrationProfile.objects.create(
            agency=self.other_agency,
            marketplace=FbsIntegrationProfile.MARKETPLACE_OZON,
            name="Ozon чужой",
            external_warehouse_id="other",
            is_active=True,
        )

    def _order(self, profile, external_id, status, **extra):
        order = FbsOrder.objects.create(
            profile=profile,
            external_order_id=external_id,
            internal_status=status,
            ordered_at=timezone.now(),
            **extra,
        )
        FbsOrderItem.objects.create(
            order=order,
            external_line_id=f"line-{external_id}",
            external_sku="SKU-1",
            barcode="4600000000001",
            product_name="Товар",
            quantity=2,
        )
        return order

    def test_orders_and_details_are_isolated_by_agency(self):
        own = self._order(self.profile, "WB-1", FbsOrder.STATUS_PICKING)
        foreign = self._order(self.other_profile, "OZ-1", FbsOrder.STATUS_RECEIVED)

        payload = client_orders_payload(agency=self.agency)

        self.assertEqual(payload["total"], 1)
        self.assertEqual(payload["results"][0]["external_order_id"], "WB-1")
        self.assertIsNotNone(client_order_detail(agency=self.agency, order_id=own.id))
        self.assertIsNone(client_order_detail(agency=self.agency, order_id=foreign.id))

    def test_reports_split_work_and_held_orders(self):
        self._order(self.profile, "WB-WORK", FbsOrder.STATUS_PICKING)
        self._order(
            self.profile,
            "WB-HOLD",
            FbsOrder.STATUS_EXCEPTION,
            problem_reason="Нужна проверка",
        )
        self._order(self.profile, "WB-DONE", FbsOrder.STATUS_DELIVERED)

        payload = client_reports_payload(agency=self.agency)

        self.assertEqual(payload["summary"]["total"], 3)
        self.assertEqual(payload["summary"]["in_work"], 1)
        self.assertEqual(payload["summary"]["held_today"], 1)
        self.assertEqual(payload["summary"]["completed"], 1)
