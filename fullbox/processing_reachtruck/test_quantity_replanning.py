import json
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase

from audit.models import OrderAuditEntry
from employees.models import Employee
from reachtruck.models import BoxClaim, MoveRequest, MoveTask
from sklad.models import WarehouseReserve
from sklad.test_utils import create_warehouse_snapshot_row
from sku.models import Agency
from processing_reachtruck.services import build_obr_requested_rows, create_obr_move_request_response


class QuantityReplanningTests(TestCase):
    def setUp(self):
        self.agency = Agency.objects.create(agn_name="Quantity replan")
        self.item = {"requested_article": "SKU", "requested_barcodes": ["BAR"],
                     "requested_goods_type": "no", "requested_qty": 20}

    def box(self, code, qty=50, **kwargs):
        return create_warehouse_snapshot_row(
            agency=self.agency, order_type="receiving", order_id="PR-Q",
            sku=kwargs.pop("sku", "SKU"), barcode=kwargs.pop("barcode", "BAR"),
            goods_type=kwargs.pop("goods_type", "no"), qty=qty, available_qty=qty,
            pallet_code="PAL-" + code, box_code=code, zone=kwargs.pop("zone", "OS"), **kwargs)

    def save_selection(self, quantity=True, **row_fields):
        row = {"article": "SKU", "barcode": "BAR", "goods_type": "no", "qty": 20,
               "source_zone": "OS", "box_codes": ["BUSY"], "strict_selected_box": True}
        row.update(row_fields)
        OrderAuditEntry.objects.create(agency=self.agency, order_type="processing", order_id="Q",
            action="create", payload={"client_unit_picker_v1": quantity, "stock_rows": [row]})

    def plan(self):
        return build_obr_requested_rows(agency=self.agency, processing_order_id="Q", request_items=[self.item])

    def test_partial_reserve_is_skipped_and_other_box_used(self):
        busy = self.box("BUSY")
        busy.available_qty = 35
        busy.processing_reserved_qty = 15
        busy.save()
        self.box("FREE")
        self.save_selection()
        rows = self.plan()
        self.assertEqual([(r["box_code"], r["qty"]) for r in rows], [("FREE", 20)])
        self.assertTrue(rows[0]["is_partial_pick"])
        busy.refresh_from_db()
        self.assertEqual((busy.available_qty, busy.processing_reserved_qty), (35, 15))

    def test_active_claim_is_skipped(self):
        self.box("BUSY")
        self.box("FREE")
        self.save_selection()
        with patch("processing_reachtruck.services.unavailable_box_claim_codes", return_value={"BUSY"}):
            self.assertEqual([r["box_code"] for r in self.plan()], ["FREE"])

    def test_shipping_box_is_skipped(self):
        self.box("BUSY")
        self.box("FREE")
        self.save_selection()
        with patch("processing_reachtruck.services.StockAvailabilityService.shipping_reserved_box_codes", return_value={"BUSY"}):
            self.assertEqual([r["box_code"] for r in self.plan()], ["FREE"])

    def test_latest_quantity_payload_does_not_restore_old_exact_selection(self):
        self.box("FREE")
        self.save_selection(quantity=False)
        self.save_selection()
        self.assertEqual([r["box_code"] for r in self.plan()], ["FREE"])

    def test_explicit_legacy_box_selection_stays_exact(self):
        self.box("FREE")
        self.save_selection(quantity=False)
        self.assertEqual(self.plan(), [])

    def test_receiving_selection_is_not_silently_replaced(self):
        self.box("FREE")
        self.save_selection(source_zone="PR")
        self.assertEqual(self.plan(), [])

    def test_mixed_selection_is_not_silently_replaced(self):
        self.box("FREE")
        self.save_selection(is_mixed_box=True)
        self.assertEqual(self.plan(), [])

    def test_wrong_barcode_and_goods_type_are_not_used(self):
        self.box("WRONG-BAR", barcode="OTHER")
        self.box("WRONG-TYPE", goods_type="gv")
        self.save_selection()
        self.assertEqual(self.plan(), [])

    def test_insufficient_free_stock_does_not_use_reserved_units(self):
        busy = self.box("BUSY")
        busy.available_qty = 35
        busy.processing_reserved_qty = 15
        busy.save()
        self.box("FREE", qty=10)
        self.save_selection()
        self.assertEqual(self.plan(), [])

    def test_exact_quantity_can_span_multiple_new_boxes(self):
        self.box("FREE-1", qty=12)
        self.box("FREE-2", qty=15)
        self.save_selection()
        rows = self.plan()
        self.assertEqual(sum(r["qty"] for r in rows), 20)
        self.assertEqual({r["box_code"] for r in rows}, {"FREE-1", "FREE-2"})

    def test_quantity_queue_does_not_wait_for_saved_source_box_size(self):
        self.box("FREE-1", qty=12)
        self.box("FREE-2", qty=8)
        OrderAuditEntry.objects.create(
            agency=self.agency,
            order_type="processing",
            order_id="Q",
            action="create",
            payload={
                "client_unit_picker_v1": True,
                "stock_rows": [
                    {
                        "article": "SKU",
                        "barcode": "BAR",
                        "goods_type": "no",
                        "qty": 20,
                        "box_qty": 50,
                        "source_zone": "OS",
                        "box_codes": ["OLD-SIZE"],
                        "strict_selected_box": True,
                    },
                    {
                        "article": "SKU",
                        "barcode": "BAR",
                        "goods_type": "no",
                        "qty": 20,
                        "source_zone": "PR",
                        "box_codes": ["OLD-EXACT"],
                        "strict_selected_box": True,
                    },
                ],
            },
        )

        direct_rows = self.plan()
        queued_rows = build_obr_requested_rows(
            agency=self.agency,
            processing_order_id="Q",
            request_items=[self.item],
            allow_partial=True,
        )

        self.assertEqual(direct_rows, [])
        self.assertEqual(sum(row["qty"] for row in queued_rows), 20)
        self.assertEqual(
            {row["box_code"] for row in queued_rows},
            {"FREE-1", "FREE-2"},
        )

    def test_released_otg_box_can_be_sent_to_processing(self):
        self.box(
            "OTG-FREE",
            zone="OTG",
            warehouse_state_code="in_otg",
        )
        self.save_selection()

        rows = self.plan()

        self.assertEqual([(row["box_code"], row["qty"]) for row in rows], [("OTG-FREE", 20)])

    def test_shortage_reports_required_and_dispatchable_quantity(self):
        self.box("FREE", qty=10)
        self.save_selection()
        errors = []
        rows = build_obr_requested_rows(agency=self.agency, processing_order_id="Q",
            request_items=[self.item], planning_errors=errors)
        self.assertEqual(rows, [])
        self.assertEqual(errors, ["SKU: нужно 20 шт., в подходящих свободных коробах 10 шт."])

    def test_dispatch_reserves_only_new_box_and_prevents_second_assignment(self):
        busy = self.box("BUSY")
        busy.available_qty = 35
        busy.processing_reserved_qty = 15
        busy.save()
        self.box("FREE")
        self.save_selection()
        user = get_user_model().objects.create_user(username="quantity-head")
        Employee.objects.create(user=user, full_name="Head", role="processing_head", is_active=True)
        request = RequestFactory().post("/reachtruck/requests/create/", {})
        request.user = user
        response = create_obr_move_request_response(request=request, agency=self.agency,
            processing_order_id="Q", request_items=[self.item])
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(json.loads(response.content)["ok"])
        self.assertEqual(list(BoxClaim.objects.filter(move_task__request__context_id="Q").values_list("box_code", flat=True)), ["FREE"])
        self.assertEqual(sum(WarehouseReserve.objects.filter(agency=self.agency, context_id="Q").values_list("qty_reserved", flat=True)), 20)
        busy.refresh_from_db()
        self.assertEqual((busy.available_qty, busy.processing_reserved_qty), (35, 15))
        again = create_obr_move_request_response(request=request, agency=self.agency,
            processing_order_id="Q", request_items=[self.item])
        self.assertEqual(again.status_code, 200)
        self.assertEqual(MoveRequest.objects.filter(agency=self.agency, context_id="Q").count(), 1)
        self.assertEqual(MoveTask.objects.filter(request__context_id="Q").count(), 1)
