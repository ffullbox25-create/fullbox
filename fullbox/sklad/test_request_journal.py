from __future__ import annotations

import re
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from audit.models import OrderAuditEntry
from employees.models import Employee
from fbs.models import FbsClientMovementRequest
from fullbox.order_numbers import format_order_number
from shipping.models import ShippingOrder
from sku.models import Agency
from todo.models import Task


class StorekeeperRequestJournalTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="storekeeper_request_journal",
            password="pwd",
        )
        self.employee = Employee.objects.create(
            user=self.user,
            full_name="Агафонов Алексей Николаевич",
            role="storekeeper",
            is_active=True,
        )
        self.agency = Agency.objects.create(agn_name="ООО «Кейзи»")
        self.client.force_login(self.user)

    def _create_receiving_task(self, number: int) -> Task:
        order_id = str(number)
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="receiving",
            action="status",
            agency=self.agency,
            payload={
                "status": "warehouse",
                "status_label": "В ожидании поставки товара",
                "items": [{"sku_code": f"SKU-{number}", "qty": 1}],
            },
        )
        return Task.objects.create(
            title=f"Заявка на приемку №{number}_PR",
            route=f"/orders/receiving/{order_id}/",
            assigned_to=self.employee,
            status="in_progress",
            due_date=timezone.now() + timedelta(days=1),
        )

    def test_dashboard_renders_request_table_without_create_or_message_panel(self):
        self._create_receiving_task(701)

        response = self.client.get("/sklad/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Журнал заявок")
        self.assertContains(response, 'class="request-table"', html=False)
        self.assertContains(response, f"№{format_order_number('receiving', '701')}")
        self.assertContains(response, self.agency.agn_name)
        self.assertContains(response, 'href="/orders/receiving/701/"', html=False)
        self.assertContains(response, 'href="/todo/"', html=False)
        self.assertContains(response, "client-lk/assets/fullbox-logo.png")
        self.assertNotContains(response, "Создать заявку")
        self.assertNotContains(response, "Оперативные обновления")
        self.assertNotContains(response, 'class="task-board"', html=False)

    def test_dashboard_keeps_existing_type_filter(self):
        self._create_receiving_task(702)
        Task.objects.create(
            title="Прочая складская задача",
            route="/todo/999/",
            assigned_to=self.employee,
            status="in_progress",
            due_date=timezone.now() + timedelta(days=1),
        )

        response = self.client.get(
            "/sklad/",
            {
                "todo_filters_applied": "1",
                "todo_filter_type": "receiving",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, f"№{format_order_number('receiving', '702')}")
        self.assertNotContains(response, "Прочая складская задача")
        self.assertContains(response, 'name="todo_filter_type" value="receiving" class="request-tab active"', html=False)

    def test_daily_problem_checks_have_separate_tab_and_stable_numbers(self):
        self._create_receiving_task(703)
        check = Task.objects.create(
            title="Ежедневная проверка проблемной тары · 09.08.2026",
            route="/sklad/journal/?daily_problem_check=2026-08-09#daily-problem-control",
            assigned_to=self.employee,
            status="in_progress",
            due_date=timezone.now() + timedelta(days=1),
        )

        response = self.client.get("/sklad/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, f"№{format_order_number('receiving', '703')}")
        self.assertNotContains(response, f"№ПРВ-{check.id:06d}")
        self.assertContains(
            response,
            'name="todo_filter_type" value="inspection" class="request-tab"',
            html=False,
        )

        response = self.client.get(
            "/sklad/",
            {
                "todo_filters_applied": "1",
                "todo_filter_type": "inspection",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, f"№ПРВ-{check.id:06d}")
        self.assertContains(response, "Проверка")
        self.assertNotContains(response, f"№{format_order_number('receiving', '703')}")
        self.assertContains(
            response,
            'name="todo_filter_type" value="inspection" class="request-tab active"',
            html=False,
        )

    def test_daily_problem_checks_are_not_in_other_requests(self):
        check = Task.objects.create(
            title="Ежедневная проверка проблемной тары · 08.08.2026",
            route="/sklad/journal/?daily_problem_check=2026-08-08#daily-problem-control",
            assigned_to=self.employee,
            status="in_progress",
            due_date=timezone.now() + timedelta(days=1),
        )

        response = self.client.get(
            "/sklad/",
            {
                "todo_filters_applied": "1",
                "todo_filter_type": "other",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, f"№ПРВ-{check.id:06d}")

    def test_dashboard_shows_transfer_type_and_destination_address(self):
        order = ShippingOrder.objects.create(
            number="OTG-000801",
            agency=self.agency,
            status=ShippingOrder.STATUS_RESERVED,
            delivery_type=ShippingOrder.DELIVERY_TRANSFER,
            destination_address="FBS",
            planned_ship_date=timezone.localdate(),
            expected_boxes=4,
        )
        Task.objects.create(
            title="Заявка на отгрузку №OTG-000801",
            route=f"/shipping/{order.pk}/",
            assigned_to=self.employee,
            status="in_progress",
            due_date=timezone.now() + timedelta(hours=1),
        )

        response = self.client.get(
            "/sklad/",
            {
                "todo_filters_applied": "1",
                "todo_filter_type": "shipping",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Перемещение")
        self.assertContains(response, "FBS")

    def test_dashboard_includes_manager_approved_fbs_movement_in_general_journal(self):
        approved = FbsClientMovementRequest.objects.create(
            agency=self.agency,
            mode=FbsClientMovementRequest.MODE_ITEM,
            status=FbsClientMovementRequest.STATUS_APPROVED,
            requested_qty=6,
            requested_by=self.user,
        )
        submitted = FbsClientMovementRequest.objects.create(
            agency=self.agency,
            mode=FbsClientMovementRequest.MODE_BOX,
            status=FbsClientMovementRequest.STATUS_SUBMITTED,
            requested_qty=12,
            requested_box_count=2,
            requested_by=self.user,
        )

        response = self.client.get("/sklad/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "FBS перемещения")
        self.assertContains(response, 'href="/fbs/operator/movements/"', html=False)
        self.assertContains(response, approved.number)
        self.assertContains(response, "FBS перемещение")
        self.assertContains(response, "Передана на склад")
        self.assertContains(
            response,
            f'href="/fbs/operator/movements/{approved.id}/"',
            html=False,
        )
        self.assertNotContains(response, submitted.number)

    def test_fbs_movement_tab_and_search_are_separate_from_receiving(self):
        movement = FbsClientMovementRequest.objects.create(
            agency=self.agency,
            mode=FbsClientMovementRequest.MODE_ITEM,
            status=FbsClientMovementRequest.STATUS_APPROVED,
            requested_qty=8,
            requested_by=self.user,
        )
        self._create_receiving_task(804)

        response = self.client.get(
            "/sklad/",
            {
                "todo_filters_applied": "1",
                "todo_filter_type": "fbs_movement",
                "todo_filter_query": movement.number,
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, movement.number)
        self.assertNotContains(response, format_order_number("receiving", "804"))
        self.assertContains(
            response,
            'name="todo_filter_type" value="fbs_movement" class="request-tab active"',
            html=False,
        )

        response = self.client.get(
            "/sklad/",
            {
                "todo_filters_applied": "1",
                "todo_filter_type": "receiving",
            },
        )
        self.assertContains(response, format_order_number("receiving", "804"))
        self.assertNotContains(response, movement.number)

    def test_dashboard_paginates_rows_and_preserves_page_size(self):
        for number in range(710, 722):
            self._create_receiving_task(number)

        response = self.client.get(
            "/sklad/",
            {
                "todo_page": "2",
                "todo_page_size": "10",
            },
        )

        self.assertEqual(response.status_code, 200)
        html = response.content.decode("utf-8")
        self.assertEqual(len(re.findall(r'data-request-row="\d+"', html)), 2)
        self.assertContains(response, "Показано 11–12 из 12")
        self.assertContains(response, '<option value="10" selected>10</option>', html=False)
        self.assertContains(response, "todo_page=1", html=False)
