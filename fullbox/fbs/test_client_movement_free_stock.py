from django.test import TestCase

from sklad.models import (
    WarehouseContainer,
    WarehouseLocation,
    WarehouseStockSnapshot,
)
from sku.models import Agency, SKU, SKUBarcode

from .client_portal import (
    client_movement_source_stock_payload,
    validate_client_movement_lines,
)
from .models import FbsClientMovementRequest, FbsClientMovementRequestLine


class ClientMovementFreeStockTests(TestCase):
    def setUp(self):
        self.agency = Agency.objects.create(agn_name="Клиент со свободным остатком")
        self.location = WarehouseLocation.objects.create(
            warehouse_code="MSK",
            zone_code="PR",
            zone_kind=WarehouseLocation.ZONE_KIND_RECEIVING,
            location_code="FREE-STOCK-PR",
            row_no=1,
            section_no=1,
            tier_no=1,
            cell_no=1,
        )
        self.busy_location = WarehouseLocation.objects.create(
            warehouse_code="MSK",
            zone_code="OBR",
            zone_kind=WarehouseLocation.ZONE_KIND_PROCESSING,
            location_code="FREE-STOCK-OBR",
            row_no=2,
            section_no=1,
            tier_no=1,
            cell_no=1,
        )
        self.partial_sku = self._sku("FREE-PARTIAL", "4600000001001")
        self.busy_sku = self._sku("BUSY-ONLY", "4600000001002")
        self.loose_sku = self._sku("FREE-LOOSE", "4600000001003")

        free_box = WarehouseContainer.objects.create(
            agency=self.agency,
            container_type=WarehouseContainer.TYPE_BOX,
            container_code="FREE-BOX-30-gv",
            current_location=self.location,
        )
        self._snapshot(
            sku=self.partial_sku,
            barcode="4600000001001",
            qty=30,
            available_qty=30,
            location=self.location,
            container=free_box,
        )
        self._snapshot(
            sku=self.partial_sku,
            barcode="4600000001001",
            qty=10,
            available_qty=0,
            location=self.busy_location,
        )
        self._snapshot(
            sku=self.busy_sku,
            barcode="4600000001002",
            qty=12,
            available_qty=0,
            location=self.busy_location,
        )
        self._snapshot(
            sku=self.loose_sku,
            barcode="4600000001003",
            qty=7,
            available_qty=7,
            location=self.location,
        )

    def _sku(self, sku_code, barcode):
        sku = SKU.objects.create(
            agency=self.agency,
            sku_code=sku_code,
            name=f"Товар {sku_code}",
        )
        SKUBarcode.objects.create(sku=sku, value=barcode, is_primary=True)
        return sku

    def _snapshot(
        self,
        *,
        sku,
        barcode,
        qty,
        available_qty,
        location,
        container=None,
    ):
        return WarehouseStockSnapshot.objects.create(
            agency=self.agency,
            sku_ref=sku,
            sku_code=sku.sku_code,
            name=sku.name,
            barcode=barcode,
            goods_type="gv",
            qty=qty,
            available_qty=available_qty,
            container=container,
            container_code=container.container_code if container else "",
            location=location,
            zone_code=location.zone_code,
            zone_kind=location.zone_kind,
            warehouse_state_code=("placed_after_processing" if container else "processing_in_progress"),
        )

    def test_item_picker_returns_only_positive_ready_free_stock(self):
        payload = client_movement_source_stock_payload(agency=self.agency)
        rows = {row["barcode"]: row for row in payload["results"]}

        self.assertEqual(set(rows), {"4600000001001"})
        self.assertEqual(rows["4600000001001"]["client_available_qty"], 30)
        self.assertNotIn("4600000001003", rows)
        self.assertTrue(all(row["client_available_qty"] > 0 for row in rows.values()))
        self.assertEqual(payload["summary"]["client_available_qty"], 30)

    def test_box_picker_hides_stock_without_a_free_whole_box(self):
        payload = client_movement_source_stock_payload(
            agency=self.agency,
            include_box_options=True,
        )

        self.assertEqual(
            [row["barcode"] for row in payload["results"]],
            ["4600000001001"],
        )
        row = payload["results"][0]
        self.assertEqual(row["client_available_qty"], 30)
        self.assertEqual(row["whole_box_available_qty"], 30)
        self.assertEqual(row["regular_box_available_count"], 1)

    def test_picker_and_validation_match_catalog_barcode_case_insensitively(self):
        catalog_sku = self._sku("CASE-CATALOG", "OZN2446435402")
        source_sku = SKU.objects.create(
            agency=self.agency,
            sku_code="CASE-SOURCE",
            name="Исходная карточка",
        )
        source_box = WarehouseContainer.objects.create(
            agency=self.agency,
            container_type=WarehouseContainer.TYPE_BOX,
            container_code="CASE-SOURCE-BOX-gv",
            current_location=self.location,
        )
        self._snapshot(
            sku=source_sku,
            barcode="ozn2446435402",
            qty=9,
            available_qty=9,
            location=self.location,
            container=source_box,
        )

        payload = client_movement_source_stock_payload(agency=self.agency)
        row = next(
            row
            for row in payload["results"]
            if row["barcode"] == "ozn2446435402"
        )
        lines, errors = validate_client_movement_lines(
            agency=self.agency,
            mode=FbsClientMovementRequest.MODE_ITEM,
            raw_lines=[
                {
                    "barcode": "ozn2446435402",
                    "qty": 5,
                    "box_count": 1,
                }
            ],
        )

        self.assertEqual(row["sku_id"], catalog_sku.id)
        self.assertEqual(row["client_available_qty"], 9)
        self.assertEqual(errors, [])
        self.assertEqual(lines[0]["sku_id"], catalog_sku.id)

    def test_item_and_box_picker_include_legacy_vp_box(self):
        legacy_sku = self._sku("FREE-LEGACY-VP", "4600000001004")
        legacy_box = WarehouseContainer.objects.create(
            agency=self.agency,
            container_type=WarehouseContainer.TYPE_BOX,
            container_code="FREE-BOX-13_VP",
            current_location=self.location,
        )
        self._snapshot(
            sku=legacy_sku,
            barcode="4600000001004",
            qty=13,
            available_qty=13,
            location=self.location,
            container=legacy_box,
        )

        item_payload = client_movement_source_stock_payload(agency=self.agency)
        box_payload = client_movement_source_stock_payload(
            agency=self.agency,
            include_box_options=True,
        )

        item_row = next(
            row for row in item_payload["results"] if row["barcode"] == "4600000001004"
        )
        box_row = next(
            row for row in box_payload["results"] if row["barcode"] == "4600000001004"
        )
        self.assertEqual(item_row["client_available_qty"], 13)
        self.assertEqual(box_row["whole_box_available_qty"], 13)
        self.assertEqual(box_row["regular_box_available_count"], 1)

    def test_open_request_that_uses_all_free_stock_hides_the_position(self):
        request_row = FbsClientMovementRequest.objects.create(
            agency=self.agency,
            mode=FbsClientMovementRequest.MODE_ITEM,
            status=FbsClientMovementRequest.STATUS_SUBMITTED,
            requested_qty=30,
        )
        FbsClientMovementRequestLine.objects.create(
            request=request_row,
            sku=self.partial_sku,
            barcode="4600000001001",
            sku_code=self.partial_sku.sku_code,
            product_name=self.partial_sku.name,
            requested_qty=30,
            units_per_box=1,
            requested_box_count=1,
            general_available_qty_snapshot=30,
        )

        payload = client_movement_source_stock_payload(agency=self.agency)

        self.assertNotIn(
            "4600000001001",
            {row["barcode"] for row in payload["results"]},
        )

    def test_picker_never_includes_another_clients_free_stock(self):
        other_agency = Agency.objects.create(agn_name="Другой клиент")
        other_sku = SKU.objects.create(
            agency=other_agency,
            sku_code="OTHER-FREE",
            name="Чужой свободный товар",
        )
        SKUBarcode.objects.create(
            sku=other_sku,
            value="4600000001999",
            is_primary=True,
        )
        WarehouseStockSnapshot.objects.create(
            agency=other_agency,
            sku_ref=other_sku,
            sku_code=other_sku.sku_code,
            name=other_sku.name,
            barcode="4600000001999",
            goods_type="gv",
            qty=100,
            available_qty=100,
            location=self.location,
            zone_code=self.location.zone_code,
            zone_kind=self.location.zone_kind,
        )

        payload = client_movement_source_stock_payload(agency=self.agency)

        self.assertNotIn(
            "4600000001999",
            {row["barcode"] for row in payload["results"]},
        )
