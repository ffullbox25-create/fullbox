from django.contrib.auth import get_user_model
from django.test import TestCase

from audit.models import OrderAuditEntry
from employees.models import Employee
from shipping.models import ShippingOrder
from sku.models import Agency
from todo.models import Task

from .lk_requests import _create_cancel_approval_task


class CancelAppealAssignmentTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.fallback_user = user_model.objects.create_user(username="appeal_fallback")
        self.fallback_manager = Employee.objects.create(
            user=self.fallback_user,
            full_name="А Администратор",
            role="manager",
            is_active=True,
        )
        self.owner_user = user_model.objects.create_user(username="appeal_owner")
        self.owner_manager = Employee.objects.create(
            user=self.owner_user,
            full_name="Я Ответственный",
            role="manager",
            is_active=True,
        )
        self.agency = Agency.objects.create(agn_name="Клиент без привязки менеджера")
        self.order = ShippingOrder.objects.create(
            number="OTG-009901",
            agency=self.agency,
            created_by=self.owner_user,
            status=ShippingOrder.STATUS_PICKING,
        )

    def test_shipping_cancel_appeal_is_assigned_to_order_owner_without_client_mapping(self):
        task = _create_cancel_approval_task(
            agency=self.agency,
            order_type="shipping",
            order_id=self.order.number,
            user=self.owner_user,
            reason="Необработанный товар",
        )

        self.assertEqual(task.assigned_to, self.owner_manager)

    def test_explicit_client_manager_has_priority_over_order_owner(self):
        self.agency.mened_user_id = self.fallback_user.id
        self.agency.save(update_fields=["mened_user_id"])

        task = _create_cancel_approval_task(
            agency=self.agency,
            order_type="shipping",
            order_id=self.order.number,
            user=self.owner_user,
            reason="Необработанный товар",
        )

        self.assertEqual(task.assigned_to, self.fallback_manager)

    def test_manager_queue_prioritizes_cancel_request_over_older_shipping_status(self):
        OrderAuditEntry.objects.create(
            order_id=self.order.number,
            order_type="shipping",
            action="status",
            agency=self.agency,
            user=self.owner_user,
            description="Созданы задания ричтраку на отбор в OTG",
            payload={
                "shipping_state": ShippingOrder.STATUS_PICKING,
                "status_label": "В отборе",
            },
        )
        OrderAuditEntry.objects.create(
            order_id=self.order.number,
            order_type="shipping",
            action="cancel_request",
            agency=self.agency,
            user=self.owner_user,
            description="Необработанный товар",
            payload={
                "status": "cancel_requested",
                "status_label": "Отмена на согласовании менеджера",
                "cancel_reason": "Необработанный товар",
                "cancel_requested_by_client": True,
            },
        )
        Task.objects.create(
            title="Согласуйте отмену заявки №9901_OTG",
            route=f"/shipping/{self.order.pk}/",
            status="backlog",
            assigned_to=self.owner_manager,
        )

        self.client.force_login(self.owner_user)
        response = self.client.get("/team-manager/", {"type": "shipping", "q": "9901"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Отмена на согласовании менеджера")
        self.assertContains(response, "Запросить подтверждение склада")
        self.assertNotContains(response, "Подтвердить отгрузку")

    def test_shipping_detail_shows_client_cancel_reason_to_manager(self):
        OrderAuditEntry.objects.create(
            order_id=self.order.number,
            order_type="shipping",
            action="status",
            agency=self.agency,
            user=self.owner_user,
            description="Созданы задания ричтраку на отбор в OTG",
            payload={
                "shipping_state": ShippingOrder.STATUS_PICKING,
                "status_label": "В отборе",
            },
        )
        OrderAuditEntry.objects.create(
            order_id=self.order.number,
            order_type="shipping",
            action="cancel_request",
            agency=self.agency,
            user=self.owner_user,
            description="Необработанный товар",
            payload={
                "status": "cancel_requested",
                "status_label": "Отмена на согласовании менеджера",
                "cancel_reason": "Необработанный товар",
                "cancel_requested_by_client": True,
            },
        )

        self.client.force_login(self.owner_user)
        response = self.client.get(f"/shipping/{self.order.pk}/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Клиент запросил отмену")
        self.assertContains(response, "Причина: Необработанный товар")
        self.assertContains(response, ">Необработанный товар</textarea>")
        self.assertContains(response, "Запросить подтверждение склада")
