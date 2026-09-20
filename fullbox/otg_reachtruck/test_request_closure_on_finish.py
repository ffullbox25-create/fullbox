"""A finished shipping order must not leave its OTG delivery requests open.

Closure of a delivery request is decided once, at the instant its MoveRequest
turns done.  If that all-or-nothing predicate is false in that millisecond it is
never retried, so the row stays `dispatched` for good.  On 20.09.2026 there were
63 such rows on production and 52 of them belonged to orders that had already
shipped.
"""
from django.contrib.auth import get_user_model
from django.test import TestCase

from otg_reachtruck.models import OtgDeliveryRequest, OtgPlanningEvent
from reachtruck.models import MoveRequest, MoveTask
from shipping.models import ShippingOrder
from sku.models import Agency

SCAN_FACT_MODE = "scan_facts_v1"
BOX_CODE = "BOX-CLOSE-1"


class OtgRequestClosureOnShippingFinishTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="otg_closure_on_finish", password="pwd"
        )
        self.agency = Agency.objects.create(agn_name="Closure client")
        self.order = ShippingOrder.objects.create(
            number="SO-CLOSE-ON-FINISH-1",
            agency=self.agency,
            created_by=self.user,
            status=ShippingOrder.STATUS_PICKING,
        )

    def _open_request(self, *, status=OtgDeliveryRequest.STATUS_DISPATCHED,
                      shortage_boxes=0, confirmed_scan=False):
        move_request = MoveRequest.objects.create(
            context_type=MoveRequest.CONTEXT_MANUAL,
            context_id=str(self.order.pk),
            agency=self.agency,
            destination_zone="OTG",
            status=MoveRequest.STATUS_DONE if confirmed_scan else MoveRequest.STATUS_PLANNED,
        )
        payload = {
            "shipping_order_id": str(self.order.pk),
            "move_mode": MoveTask.MODE_BOX_FULL,
            "requested_box_selection": "fixed",
            "requested_box_count": 1,
            "requested_boxes": [BOX_CODE],
        }
        if confirmed_scan:
            payload.update(
                {
                    "otg_scan_fact_mode": SCAN_FACT_MODE,
                    "picked_qty": 10,
                    "picked_boxes": [BOX_CODE],
                    "mobile_execution": {
                        "destination_confirmed": True,
                        "boxes_scanned": [BOX_CODE],
                    },
                }
            )
        MoveTask.objects.create(
            request=move_request,
            pallet_code="PAL-CLOSE-1",
            from_zone="OS",
            to_zone="OTG",
            move_mode=MoveTask.MODE_BOX_FULL,
            qty_planned=10,
            qty_done=10 if confirmed_scan else 0,
            status=MoveTask.STATUS_DONE if confirmed_scan else MoveTask.STATUS_CREATED,
            payload=payload,
        )
        return OtgDeliveryRequest.objects.create(
            shipping_order=self.order,
            agency=self.agency,
            move_request=move_request,
            status=status,
            requested_boxes=1,
            planned_boxes=1,
            shortage_boxes=shortage_boxes,
        )

    def _finish_order(self, status):
        with self.captureOnCommitCallbacks(execute=True):
            self.order.status = status
            self.order.save(update_fields=["status", "updated_at"])

    def test_dispatched_request_is_closed_when_the_order_ships(self):
        request = self._open_request()

        self._finish_order(ShippingOrder.STATUS_SHIPPED)

        request.refresh_from_db()
        self.assertEqual(request.status, OtgDeliveryRequest.STATUS_DONE)
        self.assertTrue(request.payload.get("closed_by_shipping_completion"))
        self.assertEqual(request.payload.get("closed_shipping_status"), ShippingOrder.STATUS_SHIPPED)
        self.assertEqual(request.payload.get("closed_from_status"), OtgDeliveryRequest.STATUS_DISPATCHED)
        self.assertTrue(
            OtgPlanningEvent.objects.filter(
                request=request, event_type="closed_by_shipping_completion"
            ).exists()
        )

    def test_request_is_closed_when_the_order_is_canceled(self):
        request = self._open_request()

        self._finish_order(ShippingOrder.STATUS_CANCELED)

        request.refresh_from_db()
        self.assertEqual(request.status, OtgDeliveryRequest.STATUS_DONE)
        self.assertEqual(request.payload.get("closed_shipping_status"), ShippingOrder.STATUS_CANCELED)

    def test_partial_request_with_shortage_is_closed_too(self):
        # `sync_completed_otg_delivery_request` refuses these outright, so before
        # the fix a shortage row could never leave `partial`.
        request = self._open_request(
            status=OtgDeliveryRequest.STATUS_PARTIAL, shortage_boxes=3
        )

        self._finish_order(ShippingOrder.STATUS_PARTIAL)

        request.refresh_from_db()
        self.assertEqual(request.status, OtgDeliveryRequest.STATUS_DONE)
        self.assertEqual(request.payload.get("closed_from_status"), OtgDeliveryRequest.STATUS_PARTIAL)

    def test_blocked_request_is_closed_as_well(self):
        request = self._open_request(status=OtgDeliveryRequest.STATUS_BLOCKED)

        self._finish_order(ShippingOrder.STATUS_SHIPPED)

        request.refresh_from_db()
        self.assertEqual(request.status, OtgDeliveryRequest.STATUS_DONE)

    def test_open_request_is_untouched_while_the_order_is_still_in_work(self):
        request = self._open_request()

        with self.captureOnCommitCallbacks(execute=True):
            self.order.status = ShippingOrder.STATUS_PACKED
            self.order.save(update_fields=["status", "updated_at"])

        request.refresh_from_db()
        self.assertEqual(request.status, OtgDeliveryRequest.STATUS_DISPATCHED)
        self.assertFalse(request.payload.get("closed_by_shipping_completion"))

    def test_genuine_completion_keeps_its_own_reason(self):
        request = self._open_request(confirmed_scan=True)

        self._finish_order(ShippingOrder.STATUS_SHIPPED)

        request.refresh_from_db()
        self.assertEqual(request.status, OtgDeliveryRequest.STATUS_DONE)
        # Closed by the real predicate, not by the shipment fallback.
        self.assertTrue(request.payload.get("completed_by_confirmed_scans"))
        self.assertIsNone(request.payload.get("closed_by_shipping_completion"))

    def test_already_closed_request_is_left_alone(self):
        request = self._open_request()
        request.status = OtgDeliveryRequest.STATUS_DONE
        request.payload = {"completed_by_confirmed_scans": True}
        request.save(update_fields=["status", "payload"])

        self._finish_order(ShippingOrder.STATUS_SHIPPED)

        request.refresh_from_db()
        self.assertEqual(request.payload, {"completed_by_confirmed_scans": True})
        self.assertFalse(
            OtgPlanningEvent.objects.filter(
                request=request, event_type="closed_by_shipping_completion"
            ).exists()
        )
