"""Проверка отмены менеджером по всем типам заявок."""

from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import Client, RequestFactory, TestCase
from django.utils import timezone

from audit.models import OrderAuditEntry
from client_cabinet.lk_requests import apply_client_request_action
from client_cabinet.models import OtherRequest
from client_cabinet.other_request_workflow import ensure_other_request_from_audit
from employees.models import Employee
from orders.services import ReceivingWorkflowService
from processing_app.services import ProcessingWorkflowService
from shipping.models import ShippingOrder
from shipping.workflow import cancel_order as cancel_shipping_order
from sklad.models import WarehouseReserve
from sklad.services.warehouse_state import WarehouseGoodsStateResolver, WarehouseStateCode
from sku.models import Agency
from todo.models import Task


class ManagerCancelAcrossOrderTypesTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="cancel_mgr", password="pwd")
        self.employee = Employee.objects.create(
            user=self.user,
            role="manager",
            full_name="Менеджер Отмены",
            is_active=True,
        )
        self.processing_head_user = get_user_model().objects.create_user(
            username="cancel_processing_head",
            password="pwd",
        )
        self.processing_head = Employee.objects.create(
            user=self.processing_head_user,
            role="processing_head",
            full_name="Начальник обработки",
            is_active=True,
        )
        self.agency = Agency.objects.create(agn_name="Клиент отмены")
        self.client = Client()
        self.client.force_login(self.user)

    def test_receiving_cancel_updates_status_and_display(self):
        order_id = "RCV-CANCEL-1"
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="receiving",
            action="status",
            agency=self.agency,
            payload={
                "status": "sent_unconfirmed",
                "submit_action": "submitted",
                "status_label": "Ждет подтверждения",
            },
        )
        Task.objects.create(
            title=f"Receiving {order_id}",
            route=f"/orders/receiving/{order_id}/",
            assigned_to=self.employee,
        )

        response = self.client.post(
            f"/orders/receiving/{order_id}/",
            data={"action": "cancel_order", "cancel_reason": "Клиент отказался от поставки"},
        )
        self.assertEqual(response.status_code, 302)

        latest = (
            OrderAuditEntry.objects.filter(order_id=order_id, order_type="receiving")
            .order_by("-created_at")
            .first()
        )
        payload = latest.payload or {}
        self.assertEqual(payload.get("status"), "cancelled")
        self.assertEqual(payload.get("cancel_reason"), "Клиент отказался от поставки")
        resolved = WarehouseGoodsStateResolver.resolve_for_receiving_order(
            order_id=order_id,
            agency=self.agency,
            payload=payload,
        )
        self.assertEqual(resolved.code, WarehouseStateCode.CANCELED)
        self.assertEqual(resolved.label_for("default"), "Отменена")
        self.assertFalse(
            Task.objects.filter(route=f"/orders/receiving/{order_id}/").exclude(status="done").exists()
        )

    def test_client_submitted_receiving_cancel_requires_manager_approval(self):
        order_id = "RCV-CLIENT-APPROVAL-1"
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="receiving",
            action="status",
            agency=self.agency,
            payload={
                "status": "sent_unconfirmed",
                "submit_action": "submitted",
                "status_label": "Ждет подтверждения",
            },
        )

        ok, message = apply_client_request_action(
            agency=self.agency,
            order_type="receiving",
            order_id=order_id,
            action="cancel",
            user=self.user,
            text="Изменились планы",
        )

        self.assertTrue(ok)
        self.assertIn("менеджеру", message)
        latest = (
            OrderAuditEntry.objects.filter(
                order_id=order_id,
                order_type="receiving",
            )
            .order_by("-created_at", "-id")
            .first()
        )
        self.assertEqual((latest.payload or {}).get("status"), "cancel_requested")
        self.assertNotEqual((latest.payload or {}).get("status"), "cancelled")

    def test_client_submitted_receiving_change_creates_manager_request(self):
        order_id = "RCV-CLIENT-CHANGE-1"
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="receiving",
            action="status",
            agency=self.agency,
            payload={
                "status": "sent_unconfirmed",
                "submit_action": "submitted",
                "status_label": "Ждет подтверждения",
            },
        )

        ok, message = apply_client_request_action(
            agency=self.agency,
            order_type="receiving",
            order_id=order_id,
            action="change_request",
            user=self.user,
            text="Изменить дату поставки",
        )

        self.assertTrue(ok)
        self.assertIn("менеджеру", message)
        latest = (
            OrderAuditEntry.objects.filter(
                order_id=order_id,
                order_type="receiving",
            )
            .order_by("-created_at", "-id")
            .first()
        )
        self.assertEqual(latest.action, "change_request")
        self.assertTrue((latest.payload or {}).get("change_requested_by_client"))
        self.assertEqual((latest.payload or {}).get("status"), "sent_unconfirmed")

    def test_receiving_after_manager_handoff_requires_warehouse_approval(self):
        order_id = "RCV-WAREHOUSE-APPROVAL-1"
        initial = OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="receiving",
            action="status",
            agency=self.agency,
            payload={
                "status": "sent_unconfirmed",
                "submit_action": "submitted",
                "status_label": "Ждет подтверждения",
            },
        )
        ReceivingWorkflowService.confirm_receiving_to_warehouse(
            order_id=order_id,
            agency=self.agency,
            status_payload=initial.payload,
            user=self.user,
            submitted_at=timezone.localtime(),
        )

        response = self.client.post(
            f"/orders/receiving/{order_id}/",
            data={
                "action": "cancel_order",
                "cancel_reason": "Клиент попросил отменить",
            },
        )
        self.assertEqual(response.status_code, 302)
        latest = (
            OrderAuditEntry.objects.filter(
                order_id=order_id,
                order_type="receiving",
            )
            .order_by("-created_at", "-id")
            .first()
        )
        self.assertEqual((latest.payload or {}).get("status"), "warehouse")

        response = self.client.post(
            f"/orders/receiving/{order_id}/",
            data={
                "action": "request_warehouse_cancel",
                "cancel_reason": "Клиент попросил отменить",
            },
        )
        self.assertEqual(response.status_code, 302)
        latest = (
            OrderAuditEntry.objects.filter(
                order_id=order_id,
                order_type="receiving",
            )
            .order_by("-created_at", "-id")
            .first()
        )
        self.assertEqual(
            (latest.payload or {}).get("status"),
            "warehouse_cancel_requested",
        )

        warehouse_client = Client()
        warehouse_client.force_login(self.processing_head_user)
        response = warehouse_client.post(
            f"/orders/receiving/{order_id}/",
            data={"action": "approve_warehouse_cancel"},
        )
        self.assertEqual(response.status_code, 302)
        latest = (
            OrderAuditEntry.objects.filter(
                order_id=order_id,
                order_type="receiving",
            )
            .order_by("-created_at", "-id")
            .first()
        )
        self.assertEqual((latest.payload or {}).get("status"), "cancelled")
        self.assertEqual((latest.payload or {}).get("cancelled_by"), "warehouse")

    def test_packing_cancel_updates_status(self):
        order_id = "PKG-CANCEL-1"
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="packing",
            action="status",
            agency=self.agency,
            payload={
                "status": "sent_unconfirmed",
                "submit_action": "submitted",
                "status_label": "Ждет подтверждения",
            },
        )

        response = self.client.post(
            f"/orders/packing/{order_id}/",
            data={"action": "cancel_order", "cancel_reason": "Не нужна упаковка"},
        )
        self.assertEqual(response.status_code, 302)
        latest = (
            OrderAuditEntry.objects.filter(order_id=order_id, order_type="packing")
            .order_by("-created_at")
            .first()
        )
        self.assertEqual((latest.payload or {}).get("status"), "cancelled")
        self.assertEqual((latest.payload or {}).get("cancel_reason"), "Не нужна упаковка")

    def test_processing_cancel_updates_status_and_releases_reserve(self):
        order_id = "OBR-CANCEL-1"
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="processing",
            action="status",
            agency=self.agency,
            payload={
                "status": "sent_unconfirmed",
                "submit_action": "submitted",
                "status_label": "Ждет подтверждения",
                "processing_stage": "awaiting_approval",
            },
        )
        WarehouseReserve.objects.create(
            agency=self.agency,
            reserve_type=WarehouseReserve.TYPE_PROCESSING,
            context_type="processing",
            context_id=order_id,
            sku_code="SKU-C",
            size="42",
            goods_type="gv",
            qty_reserved=10,
            status=WarehouseReserve.STATUS_ACTIVE,
        )
        from django.test import RequestFactory

        request = RequestFactory().post(
            f"/orders/processing/{order_id}/",
            data={"action": "cancel_order", "cancel_reason": "Ошибка в заявке"},
        )
        request.user = self.user
        result = ProcessingWorkflowService.handle_processing_detail_action(
            order_id=order_id,
            request=request,
            order_type="processing",
            payload_from_entries=lambda entries: entries[-1].payload or {},
        )
        self.assertEqual(result.status, "cancelled")
        reserve = WarehouseReserve.objects.get(
            agency=self.agency,
            context_type="processing",
            context_id=order_id,
        )
        self.assertEqual(reserve.status, WarehouseReserve.STATUS_RELEASED)

    def test_processing_after_manager_approval_requires_warehouse_approval(self):
        order_id = "OBR-WAREHOUSE-APPROVAL-1"
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="processing",
            action="status",
            agency=self.agency,
            payload={
                "status": "processing_head",
                "submit_action": "submitted",
                "status_label": "утверждено менеджером",
                "processing_stage": "manager_approved",
                "processing_stage_label": "утверждено менеджером",
            },
        )
        request_factory = RequestFactory()
        direct_request = request_factory.post(
            f"/orders/processing/{order_id}/",
            data={"action": "cancel_order", "cancel_reason": "Отмена"},
        )
        direct_request.user = self.user
        result = ProcessingWorkflowService.handle_processing_detail_action(
            order_id=order_id,
            request=direct_request,
            order_type="processing",
            payload_from_entries=lambda entries: entries[-1].payload or {},
        )
        self.assertEqual(result.status, "forbidden")

        request_cancel = request_factory.post(
            f"/orders/processing/{order_id}/",
            data={
                "action": "request_warehouse_cancel",
                "cancel_reason": "Клиент попросил отменить",
            },
        )
        request_cancel.user = self.user
        result = ProcessingWorkflowService.handle_processing_detail_action(
            order_id=order_id,
            request=request_cancel,
            order_type="processing",
            payload_from_entries=lambda entries: entries[-1].payload or {},
        )
        self.assertEqual(result.status, "warehouse_cancel_requested")

        approve_request = request_factory.post(
            f"/orders/processing/{order_id}/",
            data={"action": "approve_warehouse_cancel"},
        )
        approve_request.user = self.processing_head_user
        result = ProcessingWorkflowService.handle_processing_detail_action(
            order_id=order_id,
            request=approve_request,
            order_type="processing",
            payload_from_entries=lambda entries: entries[-1].payload or {},
        )
        self.assertEqual(result.status, "cancelled")
        latest = (
            OrderAuditEntry.objects.filter(
                order_id=order_id,
                order_type="processing",
            )
            .order_by("-created_at", "-id")
            .first()
        )
        self.assertEqual((latest.payload or {}).get("status"), "cancelled")
        self.assertEqual((latest.payload or {}).get("cancelled_by"), "warehouse")

    def test_client_submitted_processing_cancel_requires_manager_approval(self):
        order_id = "OBR-CLIENT-APPROVAL-1"
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="processing",
            action="status",
            agency=self.agency,
            payload={
                "status": "sent_unconfirmed",
                "submit_action": "submitted",
                "status_label": "Ждет подтверждения",
                "processing_stage": "awaiting_approval",
                "processing_stage_label": "Ждет подтверждения",
            },
        )

        ok, message = apply_client_request_action(
            agency=self.agency,
            order_type="processing",
            order_id=order_id,
            action="cancel",
            user=self.user,
            text="Нужно исправить состав",
        )

        self.assertTrue(ok)
        self.assertIn("менеджеру", message)
        latest = (
            OrderAuditEntry.objects.filter(
                order_id=order_id,
                order_type="processing",
            )
            .order_by("-created_at", "-id")
            .first()
        )
        self.assertEqual((latest.payload or {}).get("status"), "cancel_requested")
        self.assertNotEqual((latest.payload or {}).get("status"), "cancelled")

    def test_processing_warehouse_facts_block_automatic_cancel(self):
        order_id = "OBR-WAREHOUSE-STARTED-1"
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="processing",
            action="status",
            agency=self.agency,
            payload={
                "status": "processing_head",
                "submit_action": "submitted",
                "status_label": "утверждено менеджером",
                "processing_stage": "manager_approved",
                "processing_stage_label": "утверждено менеджером",
            },
        )
        request_factory = RequestFactory()
        request_cancel = request_factory.post(
            f"/orders/processing/{order_id}/",
            data={
                "action": "request_warehouse_cancel",
                "cancel_reason": "Клиент попросил отменить",
            },
        )
        request_cancel.user = self.user
        result = ProcessingWorkflowService.handle_processing_detail_action(
            order_id=order_id,
            request=request_cancel,
            order_type="processing",
            payload_from_entries=lambda entries: entries[-1].payload or {},
        )
        self.assertEqual(result.status, "warehouse_cancel_requested")

        approve_request = request_factory.post(
            f"/orders/processing/{order_id}/",
            data={"action": "approve_warehouse_cancel"},
        )
        approve_request.user = self.processing_head_user
        with patch.object(
            WarehouseGoodsStateResolver,
            "resolve_for_processing_order",
            return_value=SimpleNamespace(
                code=WarehouseStateCode.PROCESSING_IN_PROGRESS,
            ),
        ):
            result = ProcessingWorkflowService.handle_processing_detail_action(
                order_id=order_id,
                request=approve_request,
                order_type="processing",
                payload_from_entries=lambda entries: entries[-1].payload or {},
            )

        self.assertEqual(result.status, "warehouse_cancel_rejected")
        latest = (
            OrderAuditEntry.objects.filter(
                order_id=order_id,
                order_type="processing",
            )
            .order_by("-created_at", "-id")
            .first()
        )
        self.assertEqual(latest.action, "warehouse_cancel_rejected")
        self.assertEqual((latest.payload or {}).get("status"), "processing_head")

    def test_shipping_cancel_sets_canceled_status(self):
        order = ShippingOrder.objects.create(
            agency=self.agency,
            number="OTG-CANCEL-1",
            status=ShippingOrder.STATUS_SUBMITTED,
            created_by=self.user,
        )
        cancel_shipping_order(order, self.user, reason="Отмена менеджером")
        order.refresh_from_db()
        self.assertEqual(order.status, ShippingOrder.STATUS_CANCELED)

    def test_other_cancel_updates_domain_and_audit(self):
        order_id = "OTH-CANCEL-1"
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="other",
            action="status",
            agency=self.agency,
            payload={
                "status": "submitted",
                "status_label": "На подтверждении менеджера",
                "order_title": "Замер",
                "category_code": "measure",
                "category_label": "Замер",
            },
        )
        req = ensure_other_request_from_audit(order_id)
        self.assertIsNotNone(req)

        response = self.client.post(
            f"/orders/other/{order_id}/",
            data={"action": "cancel", "cancel_reason": "Клиент передумал"},
        )
        self.assertEqual(response.status_code, 302)
        req.refresh_from_db()
        self.assertEqual(req.status, OtherRequest.STATUS_CANCELLED)
        self.assertEqual(req.cancel_reason, "Клиент передумал")
        latest = (
            OrderAuditEntry.objects.filter(order_id=order_id, order_type="other")
            .order_by("-created_at")
            .first()
        )
        self.assertEqual((latest.payload or {}).get("status"), "cancelled")
