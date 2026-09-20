from django.contrib.auth import get_user_model
from django.test import TestCase

from otg_reachtruck.models import OtgDeliveryRequest, OtgPlanningEvent
from otg_reachtruck.services import sync_completed_otg_delivery_request
from reachtruck.models import MoveRequest, MoveTask
from shipping.models import ShippingOrder
from shipping.packing import _shipping_auto_complete_otg_reachtruck_tasks
from sku.models import Agency


class ShippingPlannerClosureTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="shipping_planner_closure",
            password="pwd",
        )
        self.agency = Agency.objects.create(agn_name="Planner closure client")
        self.order = ShippingOrder.objects.create(
            number="SO-PLANNER-CLOSURE-1",
            agency=self.agency,
            created_by=self.user,
            status=ShippingOrder.STATUS_STOREKEEPER_ACCEPTED,
        )

    def _request_with_task(
        self,
        *,
        code="BOX-PLANNER-1",
        qty=10,
        task_status=MoveTask.STATUS_CREATED,
        task_payload=None,
        shortage_boxes=0,
    ):
        move_request = MoveRequest.objects.create(
            context_type=MoveRequest.CONTEXT_MANUAL,
            context_id=str(self.order.pk),
            agency=self.agency,
            destination_zone="OTG",
            status=(
                MoveRequest.STATUS_DONE
                if task_status == MoveTask.STATUS_DONE
                else MoveRequest.STATUS_PLANNED
            ),
        )
        payload = {
            "shipping_order_id": str(self.order.pk),
            "move_mode": MoveTask.MODE_BOX_FULL,
            "requested_box_selection": "fixed",
            "requested_box_count": 1,
            "requested_boxes": [code],
        }
        if task_payload:
            payload.update(task_payload)
        task = MoveTask.objects.create(
            request=move_request,
            pallet_code="PAL-PLANNER-1",
            from_zone="OS",
            to_zone="OTG",
            move_mode=MoveTask.MODE_BOX_FULL,
            qty_planned=qty,
            qty_done=qty if task_status == MoveTask.STATUS_DONE else 0,
            status=task_status,
            payload=payload,
        )
        planner_request = OtgDeliveryRequest.objects.create(
            shipping_order=self.order,
            agency=self.agency,
            move_request=move_request,
            status=OtgDeliveryRequest.STATUS_DISPATCHED,
            requested_boxes=1,
            planned_boxes=1,
            shortage_boxes=shortage_boxes,
        )
        return move_request, task, planner_request

    @staticmethod
    def _act(code="BOX-PLANNER-1", qty=10):
        return {"act_boxes": [{"code": code, "qty": qty}]}

    def test_packing_completion_recomputes_and_closes_planner(self):
        move_request, task, planner_request = self._request_with_task()

        completed = _shipping_auto_complete_otg_reachtruck_tasks(
            self.order,
            act_data=self._act(),
            user=self.user,
        )

        task.refresh_from_db()
        move_request.refresh_from_db()
        planner_request.refresh_from_db()
        self.assertEqual(completed, [task.id])
        self.assertEqual(task.status, MoveTask.STATUS_DONE)
        self.assertEqual(move_request.status, MoveRequest.STATUS_DONE)
        self.assertEqual(planner_request.status, OtgDeliveryRequest.STATUS_DONE)
        self.assertEqual(
            task.payload.get("otg_completion_fact_mode"),
            "shipping_packing_v1",
        )
        self.assertTrue(planner_request.payload.get("completed_by_confirmed_packing"))

    def test_packing_completion_requires_full_planned_quantity(self):
        move_request, task, planner_request = self._request_with_task(qty=10)

        completed = _shipping_auto_complete_otg_reachtruck_tasks(
            self.order,
            act_data=self._act(qty=9),
            user=self.user,
        )

        task.refresh_from_db()
        move_request.refresh_from_db()
        planner_request.refresh_from_db()
        self.assertEqual(completed, [])
        self.assertEqual(task.status, MoveTask.STATUS_CREATED)
        self.assertEqual(move_request.status, MoveRequest.STATUS_PLANNED)
        self.assertEqual(planner_request.status, OtgDeliveryRequest.STATUS_DISPATCHED)

    def test_partial_move_request_is_not_closed(self):
        move_request, task, planner_request = self._request_with_task()
        MoveTask.objects.create(
            request=move_request,
            pallet_code="PAL-PLANNER-FAILED",
            from_zone="OS",
            to_zone="OTG",
            move_mode=MoveTask.MODE_BOX_FULL,
            qty_planned=10,
            qty_done=0,
            status=MoveTask.STATUS_FAILED,
            payload={"shipping_order_id": str(self.order.pk)},
        )

        _shipping_auto_complete_otg_reachtruck_tasks(
            self.order,
            act_data=self._act(),
            user=self.user,
        )

        task.refresh_from_db()
        move_request.refresh_from_db()
        planner_request.refresh_from_db()
        self.assertEqual(task.status, MoveTask.STATUS_DONE)
        self.assertEqual(move_request.status, MoveRequest.STATUS_PARTIAL)
        self.assertEqual(planner_request.status, OtgDeliveryRequest.STATUS_DISPATCHED)

    def test_shortage_protection_remains_active(self):
        move_request, task, planner_request = self._request_with_task(
            shortage_boxes=1,
        )

        _shipping_auto_complete_otg_reachtruck_tasks(
            self.order,
            act_data=self._act(),
            user=self.user,
        )

        task.refresh_from_db()
        move_request.refresh_from_db()
        planner_request.refresh_from_db()
        self.assertEqual(task.status, MoveTask.STATUS_DONE)
        self.assertEqual(move_request.status, MoveRequest.STATUS_DONE)
        self.assertEqual(planner_request.status, OtgDeliveryRequest.STATUS_DISPATCHED)

    def test_legacy_auto_completion_without_new_fact_is_not_backfilled(self):
        move_request, _task, planner_request = self._request_with_task(
            task_status=MoveTask.STATUS_DONE,
            task_payload={
                "picked_boxes": ["BOX-PLANNER-1"],
                "picked_qty": 10,
                "auto_completed_by_shipping_packing": True,
                "mobile_execution": {"destination_confirmed": True},
            },
        )

        self.assertEqual(sync_completed_otg_delivery_request(move_request), [])
        planner_request.refresh_from_db()
        self.assertEqual(planner_request.status, OtgDeliveryRequest.STATUS_DISPATCHED)

    def test_repeated_completion_is_idempotent(self):
        move_request, task, planner_request = self._request_with_task()
        first = _shipping_auto_complete_otg_reachtruck_tasks(
            self.order,
            act_data=self._act(),
            user=self.user,
        )
        second = _shipping_auto_complete_otg_reachtruck_tasks(
            self.order,
            act_data=self._act(),
            user=self.user,
        )
        move_request.refresh_from_db()

        self.assertEqual(first, [task.id])
        self.assertEqual(second, [])
        self.assertEqual(sync_completed_otg_delivery_request(move_request), [])
        self.assertEqual(
            OtgPlanningEvent.objects.filter(
                request=planner_request,
                event_type="completed_by_confirmed_packing",
            ).count(),
            1,
        )
