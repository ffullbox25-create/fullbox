from datetime import timedelta
from unittest import mock

from django.test import TestCase
from django.utils import timezone

from audit.models import OrderAuditEntry
from marking.models import MarkingCode
from receiving_cz.models import ReceivingCzUnit
from receiving_cz.services import ReceivingCzItem, ReceivingCzOrderContext, scan_unit, delete_unit, _build_boxes_and_pallets
from shipping.models import ShippingOrder
from sklad.models import WarehouseEvent, WarehouseStockSnapshot
from sku.models import Agency, SKU, SKUBarcode


class ShippedMarkIntakeTests(TestCase):
    code = "010460000000000821RETURN1\x1d91ABCD\x1d92CRYPTO"

    def setUp(self):
        self.agency = Agency.objects.create(agn_name="Abdulaev", pref="FAV", inn="340346345482")
        self.sku = SKU.objects.create(agency=self.agency, sku_code="RETURN-SKU", name="Return", size="42", honest_sign=True)
        SKUBarcode.objects.create(sku=self.sku, value="4600000000008", size="42", is_primary=True)
        self.item = ReceivingCzItem(sku_code=self.sku.sku_code, size="42", barcode="4600000000008", sku=self.sku)
        self.context = ReceivingCzOrderContext(
            order_id="PR-000349", entries=[], latest=None, agency=self.agency,
            status_payload={"goods_type": "gv", "receiving_mode": "cz"}, items=[self.item],
        )
        self.shipped_at = timezone.now() - timedelta(days=2)
        self.old = ReceivingCzUnit.objects.create(
            order_id="PR-000229", agency=self.agency, sku=self.sku, sku_code=self.sku.sku_code,
            size="42", barcode=self.item.barcode, marking_code=self.code, box_code="OLD-BOX",
        )
        ReceivingCzUnit.objects.filter(pk=self.old.pk).update(accepted_at=self.shipped_at - timedelta(days=1))
        self.mark = MarkingCode.objects.create(
            agency=self.agency, sku=self.sku, sku_code=self.sku.sku_code, size="42",
            code=self.code, order_type="receiving", order_id=self.old.order_id,
            used_at=self.shipped_at - timedelta(days=1), box_barcode="OLD-BOX",
        )
        self.shipment = ShippingOrder.objects.create(number="OTG-000529", agency=self.agency, status="shipped", shipped_at=self.shipped_at)
        self.event = WarehouseEvent.objects.create(
            agency=self.agency, event_type="shipped", stock_context_type="shipping",
            stock_context_id=self.shipment.number, qty=1, occurred_at=self.shipped_at,
        )
        self.snapshot = WarehouseStockSnapshot.objects.create(
            agency=self.agency, sku_code=self.sku.sku_code, size="42", marking_code=self.code,
            qty=0, is_archived=True, warehouse_state_code="shipped", last_event=self.event,
            source_context_id=self.old.order_id, container_code="OLD-BOX",
        )

    def scan(self, code=None, box="NEW-BOX"):
        return scan_unit(context=self.context, barcode=self.item.barcode, marking_code=code or self.code,
                         box_code=box, pallet_code="NEW-PALLET", user=None)

    def assert_blocked(self):
        self.assertNotEqual(self.scan().status, "ok")
        self.assertEqual(ReceivingCzUnit.objects.filter(order_id=self.context.order_id).count(), 0)
        self.assertFalse(OrderAuditEntry.objects.filter(payload__event_code="receiving_shipped_mark_readmitted").exists())

    def test_shipped_zero_snapshot_allowed_preserving_history(self):
        result = self.scan()
        self.assertEqual(result.status, "ok", result.error)
        self.assertEqual(ReceivingCzUnit.objects.count(), 2)
        self.mark.refresh_from_db()
        self.assertEqual(self.mark.order_id, "PR-000229")
        self.assertEqual(self.mark.box_barcode, "OLD-BOX")
        self.snapshot.refresh_from_db()
        self.assertEqual(self.snapshot.qty, 0)
        self.assertTrue(self.snapshot.is_archived)
        audit = OrderAuditEntry.objects.get(payload__event_code="receiving_shipped_mark_readmitted")
        self.assertEqual(audit.payload["shipping_event_id"], self.event.pk)
        self.assertEqual(audit.payload["receiving_unit_id"], result.unit.pk)

    def test_second_client_order_allowed(self):
        self.agency.inn = "341601596136"
        self.agency.save(update_fields=["inn"])
        self.context.order_id = "PR-000350"
        self.assertEqual(self.scan().status, "ok")

    def test_all_boxes_not_just_example_box(self):
        self.snapshot.container_code = "ANOTHER-OLD-BOX"
        self.snapshot.save(update_fields=["container_code"])
        result = self.scan(box="ANOTHER-NEW-BOX")
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.unit.box_code, "ANOTHER-NEW-BOX")

    def test_duplicate_full_short_and_scanner_prefix_blocked(self):
        self.assertEqual(self.scan().status, "ok")
        for code in [self.code, self.code.split("\x1d")[0], "]d2" + self.code]:
            result = self.scan(code, box="DIFFERENT-BOX")
            self.assertEqual(result.status, "duplicate")
        self.assertEqual(ReceivingCzUnit.objects.filter(order_id=self.context.order_id).count(), 1)

    def test_unapproved_order_blocked(self):
        self.context.order_id = "PR-000351"
        self.assert_blocked()

    def test_wrong_client_binding_blocked(self):
        self.context.order_id = "PR-000350"
        self.assert_blocked()

    def test_no_shipment_evidence_blocked(self):
        self.snapshot.last_event = None
        self.snapshot.save(update_fields=["last_event"])
        self.assert_blocked()

    def test_unfinished_shipment_blocked(self):
        self.shipment.status = "draft"
        self.shipment.save(update_fields=["status"])
        self.assert_blocked()

    def test_wrong_shipment_client_blocked(self):
        other = Agency.objects.create(agn_name="Other", pref="OTH")
        self.shipment.agency = other
        self.shipment.save(update_fields=["agency"])
        self.assert_blocked()

    def test_wrong_source_sku_and_size_blocked(self):
        for field, value in [("sku_code", "OTHER-SKU"), ("size", "44")]:
            original = getattr(self.snapshot, field)
            setattr(self.snapshot, field, value)
            self.snapshot.save(update_fields=[field])
            self.assert_blocked()
            setattr(self.snapshot, field, original)
            self.snapshot.save(update_fields=[field])

    def test_live_warehouse_blocks_even_short_code(self):
        WarehouseStockSnapshot.objects.create(agency=self.agency, sku_code=self.sku.sku_code,
            marking_code=self.code.split("\x1d")[0], qty=1, is_archived=False)
        self.assert_blocked()

    def test_active_reservation_without_quantity_blocks(self):
        WarehouseStockSnapshot.objects.create(agency=self.agency, sku_code=self.sku.sku_code,
            marking_code=self.code, qty=0, shipping_reserved_qty=1, is_archived=False)
        self.assert_blocked()

    def test_fbs_live_stock_blocks(self):
        # The shared matcher is tested against real snapshots above; this
        # boundary assertion isolates FBS without creating an unrelated wave.
        from receiving_cz import repeat_receiving
        original = repeat_receiving._matching_mark_snapshot
        def match(queryset, code):
            if queryset.model is repeat_receiving.FbsStockBalance:
                return object()
            return original(queryset, code)
        with mock.patch.object(repeat_receiving, "_matching_mark_snapshot", side_effect=match):
            self.assert_blocked()

    def test_newer_unfinished_receiving_blocks(self):
        ReceivingCzUnit.objects.create(order_id="PR-OTHER", agency=self.agency, sku_code=self.sku.sku_code,
            marking_code=self.code, barcode=self.item.barcode, box_code="OTHER")
        self.assert_blocked()

    def test_newer_registry_usage_blocks(self):
        self.mark.used_at = timezone.now()
        self.mark.save(update_fields=["used_at"])
        self.assert_blocked()

    def test_brand_new_code_remains_accepted_normally(self):
        result = self.scan("010460000000000821NEW-UNIT")
        self.assertEqual(result.status, "ok", result.error)
        self.assertFalse(OrderAuditEntry.objects.filter(payload__event_code="receiving_shipped_mark_readmitted").exists())

    def test_zero_quantity_shipment_event_not_proof(self):
        self.event.qty = 0
        self.event.save(update_fields=["qty"])
        self.assert_blocked()

    def test_partial_shipment_with_unit_event_allowed(self):
        self.shipment.status = "partial_shipped"
        self.shipment.save(update_fields=["status"])
        self.assertEqual(self.scan().status, "ok")

    def test_delete_new_scan_does_not_delete_old_registry_or_history(self):
        result = self.scan()
        self.assertEqual(result.status, "ok")
        delete_unit(context=self.context, unit_id=result.unit.pk)
        self.assertTrue(MarkingCode.objects.filter(pk=self.mark.pk, order_id="PR-000229").exists())
        self.assertTrue(ReceivingCzUnit.objects.filter(pk=self.old.pk).exists())
        self.assertEqual(self.scan().status, "ok")

    def test_placement_contains_only_current_intake_once(self):
        self.assertEqual(self.scan().status, "ok")
        units = list(ReceivingCzUnit.objects.filter(order_id=self.context.order_id))
        boxes, pallets = _build_boxes_and_pallets(units)
        self.assertEqual(len(boxes), 1)
        self.assertEqual(boxes[0]["code"], "NEW-BOX")
        self.assertEqual(len(boxes[0]["items"]), 1)
        self.assertEqual(boxes[0]["items"][0]["qty"], 1)
        self.assertEqual(boxes[0]["items"][0]["marking_code"], self.code)
        self.assertEqual(pallets[0]["boxes"], ["NEW-BOX"])

    def test_closed_intake_remains_closed(self):
        with mock.patch("receiving_cz.services._flow_closed", return_value=True):
            self.assertEqual(self.scan().status, "closed")
        self.assertEqual(ReceivingCzUnit.objects.count(), 1)


class BulkShippedMarkIntakeTests(TestCase):
    """Regression: unused imported ЧЗ + ordinary receipt shipped by boxes."""
    code = ShippedMarkIntakeTests.code
    scan = ShippedMarkIntakeTests.scan
    assert_blocked = ShippedMarkIntakeTests.assert_blocked

    def setUp(self):
        ShippedMarkIntakeTests.setUp(self)
        self.agency.inn = "341601596136"
        self.agency.save(update_fields=["inn"])
        self.context.order_id = "PR-000350"
        self.old.delete()
        self.mark.order_id = "PR-000282"
        self.mark.source = "import"
        self.mark.used_at = None
        self.mark.barcode = self.item.barcode
        self.mark.box_barcode = ""
        self.mark.save()
        MarkingCode.objects.filter(pk=self.mark.pk).update(created_at=self.shipped_at - timedelta(days=2))
        self.snapshot.source_context_type = "receiving"
        self.snapshot.source_context_id = "PR-000282"
        self.snapshot.marking_code = ""
        self.snapshot.barcode = self.item.barcode
        self.snapshot.save()
        self.receipt = OrderAuditEntry.objects.create(
            order_type="receiving", order_id="PR-000282", action="status", agency=self.agency,
            payload={"status": "done", "flow_closed": True, "act_state": "closed",
                     "receiving_mode": "standard", "act_items": [{"sku_code": self.item.sku_code,
                     "size": self.item.size, "actual_qty": 1}]},
        )
        OrderAuditEntry.objects.filter(pk=self.receipt.pk).update(created_at=self.shipped_at - timedelta(days=1))

    def test_imported_mark_from_fully_shipped_bulk_receipt_allowed(self):
        result = self.scan()
        self.assertEqual(result.status, "ok", result.error)
        audit = OrderAuditEntry.objects.get(payload__event_code="receiving_shipped_mark_readmitted")
        self.assertEqual(audit.payload["evidence_type"], "completed_receiving_bulk_shipment")
        self.assertEqual(audit.payload["source_receipt_qty"], 1)
        self.assertEqual(audit.payload["source_shipped_qty"], 1)
        self.mark.refresh_from_db()
        self.assertEqual(self.mark.order_id, "PR-000282")
        self.assertIsNone(self.mark.used_at)

    def test_second_scan_in_another_box_is_blocked(self):
        self.assertEqual(self.scan().status, "ok")
        self.assertEqual(self.scan(self.code.split("\x1d")[0], box="NEXT-BOX").status, "duplicate")
        self.assertEqual(ReceivingCzUnit.objects.filter(order_id="PR-000350").count(), 1)

    def test_bulk_size_alias_requires_exact_barcode(self):
        self.snapshot.size = "42 (RUS)"
        self.snapshot.save(update_fields=["size"])
        self.receipt.payload["act_items"][0]["size"] = self.snapshot.size
        self.receipt.save(update_fields=["payload"])
        self.assertEqual(self.scan().status, "ok")

    def test_unknown_size_and_barcode_not_matched(self):
        self.snapshot.size = "42 (RUS)"
        self.snapshot.barcode = "OTHER"
        self.snapshot.save(update_fields=["size", "barcode"])
        self.receipt.payload["act_items"][0]["size"] = self.snapshot.size
        self.receipt.save(update_fields=["payload"])
        self.assert_blocked()

    def test_remaining_stock_in_any_source_item_blocks(self):
        WarehouseStockSnapshot.objects.create(agency=self.agency, source_context_type="receiving",
            source_context_id="PR-000282", sku_code="OTHER-SKU", qty=1, is_archived=False)
        self.assert_blocked()

    def test_incomplete_shipment_quantity_blocks(self):
        self.receipt.payload["act_items"][0]["actual_qty"] = 2
        self.receipt.save(update_fields=["payload"])
        self.assert_blocked()

    def test_open_source_receipt_blocks(self):
        self.receipt.payload["flow_closed"] = False
        self.receipt.save(update_fields=["payload"])
        self.assert_blocked()

    def test_missing_actual_quantity_blocks(self):
        self.receipt.payload["act_items"][0].pop("actual_qty")
        self.receipt.save(update_fields=["payload"])
        self.assert_blocked()

    def test_unshipped_source_document_blocks(self):
        self.shipment.status = "draft"
        self.shipment.save(update_fields=["status"])
        self.assert_blocked()

    def test_shared_event_cannot_be_counted_twice(self):
        WarehouseStockSnapshot.objects.create(agency=self.agency, source_context_type="receiving",
            source_context_id="PR-000282", sku_code=self.item.sku_code, size=self.item.size,
            qty=0, is_archived=True, warehouse_state_code="shipped", last_event=self.event)
        self.receipt.payload["act_items"][0]["actual_qty"] = 2
        self.receipt.save(update_fields=["payload"])
        self.assert_blocked()

    def test_non_imported_code_cannot_use_bulk_fallback(self):
        self.mark.source = "scan"
        self.mark.save(update_fields=["source"])
        self.assert_blocked()

    def test_later_intake_without_snapshot_blocks(self):
        ReceivingCzUnit.objects.create(agency=self.agency, order_id="PR-OTHER", sku_code=self.item.sku_code,
            marking_code=self.code, barcode=self.item.barcode, box_code="OTHER")
        self.assert_blocked()

    def test_later_registry_import_cannot_use_old_receipt(self):
        MarkingCode.objects.filter(pk=self.mark.pk).update(created_at=timezone.now())
        self.assert_blocked()

    def test_unapproved_intake_cannot_use_bulk_fallback(self):
        self.context.order_id = "PR-000351"
        self.assert_blocked()

    def test_whole_receipt_must_match_not_just_scanned_item(self):
        self.receipt.payload["act_items"].append({"sku_code": "OTHER", "size": "", "actual_qty": 1})
        self.receipt.save(update_fields=["payload"])
        self.assert_blocked()
