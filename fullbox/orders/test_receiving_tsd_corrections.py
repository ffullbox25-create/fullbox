import json
from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse
from audit.models import OrderAuditEntry
from receiving_cz.models import ReceivingCzUnit
from orders import test_receiving_tsd as fixtures
from orders.services import ReceivingWorkflowService as Workflow
from orders.receiving_tsd_corrections import EVENT


class TsdBoxCorrectionTests(TestCase):
    def setUp(self):
        fixtures.ReceivingTsdTests.setUp(self)
        fixtures.ReceivingTsdTests._assign_receiving_owner(self, self.employee)
        self.item = dict(sku_code=self.sku.sku_code, name=self.sku.name,
                         size="0", barcode="200000000001", qty=211)
        self.box = "TST-1209-000001-gv"
        self.pallet = "TST-1PR-0000001-gv"
        self.state = dict(boxes=[dict(code=self.box, sealed=False, items=[self.item])],
                          pallets=[dict(code=self.pallet, sealed=False, boxes=[self.box], items=[])],
                          activeBox=self.box, activePallet=self.pallet)
        self.draft = self.audit(dict(flow_state=self.state, flow_client_version=1))
        self.url = reverse("orders-receiving-tsd-correct-box", args=[self.order_id])

    def audit(self, payload):
        return OrderAuditEntry.objects.create(order_type="receiving", order_id=self.order_id,
                                             action="update", agency=self.agency, user=self.user,
                                             payload=payload)

    def correct(self, **changes):
        payload = dict(event_id="correction-1", box_code=self.box, row_index=0,
                       item=self.item, expected_qty=211, quantity=210)
        payload.update(changes)
        return self.client.post(self.url, data=json.dumps(payload), content_type="application/json")

    def save_fixture(self):
        self.draft.payload["flow_state"] = self.state
        self.draft.save(update_fields=["payload"])

    def test_reduce_updates_box_pallet_and_order_and_audits_actor(self):
        response = self.correct()
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()
        for key in ("active_box_qty", "active_pallet_qty", "accepted_qty"):
            self.assertEqual(data["summary"][key], 210)
        entry = OrderAuditEntry.objects.get(order_id=self.order_id, payload__event=EVENT)
        self.assertEqual(entry.user_id, self.user.id)
        self.assertEqual(entry.payload["quantity_before"], 211)
        self.assertEqual(entry.payload["quantity_after"], 210)

    def test_zero_removes_only_selected_row_and_keeps_open_box(self):
        self.state["boxes"][0]["items"].append(dict(self.item, barcode="another", qty=3))
        self.save_fixture()
        response = self.correct(quantity=0)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["summary"]["accepted_qty"], 3)
        self.assertEqual(response.json()["state"]["activeBox"], self.box)
        self.assertEqual(response.json()["state"]["boxes"][0]["items"][0]["barcode"], "another")

    def test_zero_last_row_keeps_container_available_for_scanning(self):
        response = self.correct(quantity=0)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["summary"]["accepted_qty"], 0)
        self.assertEqual(response.json()["state"]["activeBox"], self.box)

    def test_duplicate_is_idempotent(self):
        self.assertEqual(self.correct().status_code, 200)
        response = self.correct()
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(response.json()["duplicate_ignored"])
        self.assertEqual(OrderAuditEntry.objects.filter(payload__event=EVENT).count(), 1)

    def test_stale_quantity_returns_current_state_without_write(self):
        before = OrderAuditEntry.objects.count()
        response = self.correct(expected_qty=210, quantity=209)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["status"], "stale_box")
        self.assertEqual(response.json()["summary"]["active_box_qty"], 211)
        self.assertEqual(OrderAuditEntry.objects.count(), before)

    def test_identity_mismatch_is_not_applied(self):
        self.assertEqual(self.correct(item=dict(self.item, barcode="wrong")).status_code, 409)

    def test_invalid_quantities_and_increases_rejected(self):
        for value in (-1, 0.5, True, "1.0", "", None, 211, 212):
            with self.subTest(value=value):
                self.assertEqual(self.correct(quantity=value).status_code, 409)
        self.assertFalse(OrderAuditEntry.objects.filter(payload__event=EVENT).exists())

    def test_different_box_is_rejected(self):
        self.assertEqual(self.correct(box_code="other").status_code, 409)

    def test_sealed_box_is_rejected(self):
        self.state["boxes"][0]["sealed"] = True
        self.save_fixture()
        self.assertEqual(self.correct().status_code, 409)

    def test_sealed_pallet_is_rejected(self):
        self.state["pallets"][0]["sealed"] = True
        self.save_fixture()
        self.assertEqual(self.correct().status_code, 409)

    def test_other_storekeeper_is_rejected(self):
        self.client.force_login(self.other_user)
        self.assertEqual(self.correct().status_code, 409)

    def test_anonymous_is_rejected(self):
        self.client.logout()
        self.assertEqual(self.correct().status_code, 302)

    def test_permission_blocks_correction_even_if_pallet_looks_open(self):
        self.audit(dict(event=Workflow.PALLET_PLACEMENT_PERMISSION_EVENT,
                        pallet_code=self.pallet, pallet_placement_allowed=True))
        self.assertEqual(self.correct().status_code, 409)

    def test_materialized_pallet_remains_locked_after_permission_revoked(self):
        self.audit(dict(event=Workflow.PALLET_STOCK_MATERIALIZED_EVENT, pallet_code=self.pallet))
        self.assertEqual(self.correct().status_code, 409)

    def test_existing_stock_is_guarded(self):
        with patch("orders.receiving_tsd_corrections.WarehouseStockSnapshot.objects.filter") as query:
            query.return_value.filter.return_value.exists.return_value = True
            self.assertEqual(self.correct().status_code, 409)

    def test_post_required(self):
        self.assertEqual(self.client.get(self.url).status_code, 405)

    def test_template_exposes_editor(self):
        from django.template.loader import render_to_string
        html = render_to_string("orders/receiving_tsd_detail.html", {
            "order_id": self.order_id, "client": self.agency})
        self.assertIn('id="edit-box"', html)
        self.assertIn('data-correct-box-url="' + self.url + '"', html)
        self.assertIn('data-is-marked="0"', html)

    def marked_units(self):
        status = OrderAuditEntry.objects.filter(order_id=self.order_id, action="status").latest("id")
        status.payload["receiving_mode"] = "cz"
        status.save(update_fields=["payload"])
        return [ReceivingCzUnit.objects.create(
            order_id=self.order_id, agency=self.agency, sku=self.sku,
            sku_code=self.sku.sku_code, name=self.sku.name, size="0", barcode="200000000001",
            marking_code="010460000000000021" + serial, box_code=self.box,
            pallet_code=self.pallet, accepted_by=self.user,
        ) for serial in ("serial-one", "serial-two")]

    def test_marked_correction_removes_exact_unit_and_recounts(self):
        units = self.marked_units()
        response = self.correct(marking_code=units[0].marking_code)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertFalse(ReceivingCzUnit.objects.filter(pk=units[0].pk).exists())
        self.assertTrue(ReceivingCzUnit.objects.filter(pk=units[1].pk).exists())
        self.assertEqual(response.json()["summary"]["active_pallet_qty"], 1)
        self.assertEqual(response.json()["summary"]["accepted_qty"], 1)

    def test_marked_quantity_without_code_and_foreign_code_rejected(self):
        self.marked_units()
        self.assertEqual(self.correct().status_code, 409)
        self.assertEqual(self.correct(marking_code="not-in-box").status_code, 409)
        self.assertEqual(ReceivingCzUnit.objects.count(), 2)

    def test_failed_persist_rolls_back_mark_deletion(self):
        units = self.marked_units()
        with patch("orders.receiving_tsd_corrections._persist_state", side_effect=ValueError("save failed")):
            self.assertEqual(self.correct(marking_code=units[0].marking_code).status_code, 409)
        self.assertEqual(ReceivingCzUnit.objects.count(), 2)
        self.assertFalse(OrderAuditEntry.objects.filter(payload__event=EVENT).exists())

    def test_materialized_marked_pallet_cannot_lose_mark(self):
        units = self.marked_units()
        self.audit(dict(event=Workflow.PALLET_STOCK_MATERIALIZED_EVENT, pallet_code=self.pallet))
        self.assertEqual(self.correct(marking_code=units[0].marking_code).status_code, 409)
        self.assertEqual(ReceivingCzUnit.objects.count(), 2)
