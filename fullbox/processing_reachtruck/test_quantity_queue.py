import json
from unittest.mock import patch
from django.test import TestCase, RequestFactory
from django.contrib.auth import get_user_model
from employees.models import Employee
from sku.models import Agency
from audit.models import OrderAuditEntry
from sklad.test_utils import create_warehouse_snapshot_row
from sklad.models import WarehouseStockSnapshot, WarehouseReserve
from reachtruck.models import MoveRequest, MoveTask, BoxClaim
from processing_reachtruck.quantity_queue import create_quantity_queue, resume_agency, waiting_quantity
from reachtruck.services.move_requests import _recompute_request_status


class QuantityQueueTests(TestCase):
    def setUp(self):
        self.agency = Agency.objects.create(agn_name="Queue test")
        user = get_user_model().objects.create_user(username="head")
        Employee.objects.create(user=user, full_name="Head", role="processing_head", is_active=True)
        self.request = RequestFactory().post("/reachtruck/requests/create/", {})
        self.request.user = user
        self.demand = [{"requested_article": "SKU", "requested_barcodes": ["BAR"], "requested_goods_type": "no", "requested_qty": 20}]
        OrderAuditEntry.objects.create(agency=self.agency, order_type="processing", order_id="Q", action="create",
            payload={"client_unit_picker_v1": True, "stock_rows": [{"article":"SKU","barcode":"BAR","goods_type":"no","qty":20,"source_zone":"OS","box_codes":["OLD"],"strict_selected_box":True}]})

    def stock(self, code, qty):
        return create_warehouse_snapshot_row(agency=self.agency, order_type="receiving", order_id="PR",
            sku="SKU", barcode="BAR", goods_type="no", qty=qty, available_qty=qty,
            box_code=code, pallet_code="PAL-"+code, zone="OS")

    def submit(self):
        response = create_quantity_queue(request=self.request, agency=self.agency, order_key="Q", request_items=self.demand)
        self.assertEqual(response.status_code,200,response.content)
        return json.loads(response.content)

    def test_partial_is_accepted_and_repeated_submit_is_idempotent(self):
        self.stock("FREE", 12)
        result=self.submit()
        self.assertEqual((result["planned_qty"],result["queued_qty"]),(12,8))
        again=self.submit()
        self.assertEqual(again["request_id"],result["request_id"])
        self.assertEqual(MoveTask.objects.count(),1)

    def test_all_quantity_can_wait_without_physical_task(self):
        result=self.submit()
        self.assertEqual(result["queued_qty"],20)
        self.assertEqual(result["tasks_created"],0)
        self.assertFalse(WarehouseReserve.objects.exists())

    def test_concrete_location_requirement_falls_back_to_generic_obr(self):
        OrderAuditEntry.objects.create(
            agency=self.agency,
            order_type="processing",
            order_id="Q",
            action="status",
            payload={"processing_concrete_location_required": True},
        )
        self.stock("FREE", 20)

        result = self.submit()

        self.assertEqual((result["planned_qty"], result["queued_qty"]), (20, 0))
        task = MoveTask.objects.get()
        self.assertEqual(task.payload.get("destination_code"), "OBR")
        self.assertNotIn("concrete_location_required", task.payload)

    def test_new_stock_resumes_exact_remainder_once(self):
        self.stock("FREE",12)
        result=self.submit()
        self.stock("NEW",50)
        resume_agency(self.agency.pk)
        resume_agency(self.agency.pk)
        q=MoveRequest.objects.get(pk=result["request_id"])
        self.assertEqual(waiting_quantity(q),0)
        self.assertEqual(sum(q.tasks.values_list("qty_planned",flat=True)),20)
        self.assertEqual(q.tasks.count(),2)

    def test_remainder_uses_free_box_with_different_quantity(self):
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
                        "box_qty": 12,
                        "source_zone": "OS",
                        "box_codes": ["FIRST"],
                        "strict_selected_box": True,
                    },
                    {
                        "article": "SKU",
                        "barcode": "BAR",
                        "goods_type": "no",
                        "qty": 20,
                        "source_zone": "PR",
                        "box_codes": ["ALREADY-MOVED"],
                        "strict_selected_box": True,
                    },
                ],
            },
        )
        self.stock("FIRST", 12)
        result = self.submit()
        self.assertEqual((result["planned_qty"], result["queued_qty"]), (12, 8))

        self.stock("SECOND", 8)
        resume_agency(self.agency.pk)

        queue = MoveRequest.objects.get(pk=result["request_id"])
        self.assertEqual(waiting_quantity(queue), 0)
        self.assertEqual(sum(queue.tasks.values_list("qty_planned", flat=True)), 20)
        self.assertEqual(queue.tasks.count(), 2)

    def test_done_first_task_does_not_close_waiting_request(self):
        from reachtruck.services.move_requests import _recompute_request_status
        self.stock("FREE",12)
        result=self.submit()
        q=MoveRequest.objects.get(pk=result["request_id"])
        task=q.tasks.get(); task.status="done"; task.qty_done=12; task.save()
        _recompute_request_status(q)
        q.refresh_from_db()
        self.assertEqual(q.status,MoveRequest.STATUS_PARTIAL)
        self.assertEqual(waiting_quantity(q),8)

    def test_canceled_queue_is_not_resumed(self):
        result=self.submit()
        MoveRequest.objects.filter(pk=result["request_id"]).update(status="canceled")
        self.stock("NEW",50)
        resume_agency(self.agency.pk)
        self.assertFalse(MoveTask.objects.exists())

    def test_foreign_reserve_is_untouched(self):
        busy=self.stock("OLD",50)
        busy.available_qty=35;busy.processing_reserved_qty=15;busy.save()
        self.submit()
        busy.refresh_from_db()
        self.assertEqual((busy.available_qty,busy.processing_reserved_qty),(35,15))
        self.assertFalse(MoveTask.objects.exists())

    def test_racing_claim_rolls_back_new_reserve_but_keeps_demand(self):
        stock=self.stock("FREE",50)
        with patch("processing_reachtruck.quantity_queue.claim_boxes_for_task",side_effect=ValueError("busy")):
            result=self.submit()
        stock.refresh_from_db()
        self.assertEqual(result["queued_qty"],20)
        self.assertEqual(stock.available_qty,50)
        self.assertFalse(WarehouseReserve.objects.exists())
        self.assertFalse(MoveTask.objects.exists())

    def test_stock_signal_resumes_after_commit(self):
        self.submit()
        with self.captureOnCommitCallbacks(execute=True):
            self.stock("NEW",50)
        self.assertEqual(MoveTask.objects.count(),1)

    def test_failed_transaction_never_resumes_queue(self):
        from django.db import transaction
        self.submit()
        with self.captureOnCommitCallbacks(execute=True):
            try:
                with transaction.atomic():
                    self.stock("ROLLBACK",50)
                    raise ValueError("rollback")
            except ValueError:
                pass
        self.assertFalse(MoveTask.objects.exists())

    def test_fifo_cannot_assign_same_stock_twice(self):
        self.submit()
        create_quantity_queue(request=self.request,agency=self.agency,order_key="Q2",request_items=self.demand)
        self.stock("NEW",20)
        resume_agency(self.agency.pk)
        self.assertEqual(MoveTask.objects.count(),1)
        self.assertEqual(MoveTask.objects.get().request.context_id,"Q")

    def test_previous_task_completion_releases_queue_after_commit(self):
        with self.captureOnCommitCallbacks(execute=True):
            self.stock("BUSY",50)
            previous = MoveRequest.objects.create(agency=self.agency,context_type="processing",context_id="OLD")
            task = MoveTask.objects.create(request=previous,pallet_code="PAL-BUSY",qty_planned=1,status="in_progress")
            claim = BoxClaim.objects.create(agency=self.agency,move_task=task,box_code="BUSY",claim_kind="partial")
            result = self.submit()
            self.assertEqual(result["queued_qty"],20)
            task.status="done";task.qty_done=1;task.save()
            claim.status="delivered";claim.save()
        q=MoveRequest.objects.get(pk=result["request_id"])
        self.assertEqual(waiting_quantity(q),0)
        self.assertEqual(q.tasks.count(),1)

    def test_cancelled_processing_order_cannot_resume_taskless_queue(self):
        result=self.submit()
        OrderAuditEntry.objects.create(agency=self.agency,order_type="processing",order_id="Q",action="status",payload={"status":"cancelled"})
        self.stock("NEW",50)
        resume_agency(self.agency.pk)
        self.assertFalse(MoveTask.objects.exists())
        self.assertEqual(MoveRequest.objects.get(pk=result["request_id"]).status,"canceled")

    def test_cancelled_processing_order_releases_all_queue_claims_and_reserves(self):
        stock = self.stock("FREE", 20)
        result = self.submit()
        move_request = MoveRequest.objects.get(pk=result["request_id"])
        task = move_request.tasks.get()
        delivered_task = MoveTask.objects.create(
            request=move_request,
            legacy_order_id="PROC-QUEUE-ALREADY-DONE",
            status=MoveTask.STATUS_DONE,
            qty_planned=3,
            qty_done=3,
        )
        self.assertEqual(task.status, MoveTask.STATUS_CREATED)
        self.assertTrue(
            BoxClaim.objects.filter(
                move_task=task,
                status=BoxClaim.STATUS_CLAIMED,
            ).exists()
        )
        self.assertTrue(
            WarehouseReserve.objects.filter(
                context_type="processing",
                context_id="Q",
                status=WarehouseReserve.STATUS_ACTIVE,
            ).exists()
        )

        OrderAuditEntry.objects.create(
            agency=self.agency,
            order_type="processing",
            order_id="Q",
            action="status",
            payload={"status": "cancelled"},
        )
        resume_agency(self.agency.pk)

        move_request.refresh_from_db()
        task.refresh_from_db()
        delivered_task.refresh_from_db()
        stock.refresh_from_db()
        self.assertEqual(move_request.status, MoveRequest.STATUS_CANCELED)
        self.assertEqual(task.status, MoveTask.STATUS_CANCELED)
        self.assertEqual(delivered_task.status, MoveTask.STATUS_DONE)
        self.assertEqual(delivered_task.qty_done, 3)
        self.assertFalse(
            BoxClaim.objects.filter(
                move_task=task,
                status=BoxClaim.STATUS_CLAIMED,
            ).exists()
        )
        self.assertFalse(
            WarehouseReserve.objects.filter(
                context_type="processing",
                context_id="Q",
                status=WarehouseReserve.STATUS_ACTIVE,
            ).exists()
        )
        self.assertEqual(stock.available_qty, stock.qty)

    def test_cancel_does_not_release_box_already_confirmed_at_source(self):
        self.stock("IN-TRANSIT", 20)
        result = self.submit()
        move_request = MoveRequest.objects.get(pk=result["request_id"])
        task = move_request.tasks.get()
        payload = dict(task.payload or {})
        payload["mobile_execution"] = {
            "source_confirmed": True,
            "destination_confirmed": False,
        }
        task.payload = payload
        task.status = MoveTask.STATUS_IN_PROGRESS
        task.save(update_fields=["payload", "status", "updated_at"])
        OrderAuditEntry.objects.create(
            agency=self.agency,
            order_type="processing",
            order_id="Q",
            action="status",
            payload={"status": "cancelled"},
        )

        resume_agency(self.agency.pk)

        move_request.refresh_from_db()
        task.refresh_from_db()
        self.assertEqual(move_request.status, MoveRequest.STATUS_BLOCKED)
        self.assertEqual(task.status, MoveTask.STATUS_IN_PROGRESS)
        self.assertTrue(
            BoxClaim.objects.filter(
                move_task=task,
                status=BoxClaim.STATUS_CLAIMED,
            ).exists()
        )
