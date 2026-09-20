from datetime import datetime, timedelta

from django.contrib.auth import get_user_model
from django.template import Context, RequestContext, Template
from django.test import RequestFactory
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from audit.models import OrderAuditEntry
from employees.models import Employee
from logistics.models import LogisticsTrip, LogisticsTripOrder
from shipping.models import ShippingOrder
from reachtruck.models import MoveRequest, MoveTask
from sku.models import Agency
from sklad.models import WarehouseLocation, WarehouseReserve, WarehouseStockSnapshot
from sklad.services.warehouse_write_path import WarehouseWritePathService

from .models import Task, TaskAttention, TaskChecklistItem, TaskPanelSnapshot
from .services import (
    build_internal_task_list_queryset,
    build_task_list_queryset,
    build_trip_context,
    can_access_task,
    send_receiving_to_warehouse,
)


class InternalWarehouseTaskTests(TestCase):
    def setUp(self):
        self.manager_user = get_user_model().objects.create_user(
            username="warehouse-task-manager",
            password="test-pass",
        )
        self.manager = Employee.objects.create(
            full_name="Менеджер Задач",
            role="manager",
            user=self.manager_user,
        )
        self.storekeeper_user = get_user_model().objects.create_user(
            username="warehouse-task-storekeeper",
            password="test-pass",
        )
        self.storekeeper = Employee.objects.create(
            full_name="Кладовщик Ответственный",
            role="storekeeper",
            user=self.storekeeper_user,
        )
        self.other_storekeeper_user = get_user_model().objects.create_user(
            username="warehouse-task-other-storekeeper",
            password="test-pass",
        )
        self.other_storekeeper = Employee.objects.create(
            full_name="Кладовщик Другой",
            role="storekeeper",
            user=self.other_storekeeper_user,
        )

    def _task(self, **kwargs):
        values = {
            "title": "Проверить внутреннее размещение",
            "description": "Проверить результат и приложить комментарий.",
            "kind": Task.KIND_WAREHOUSE_INTERNAL,
            "assigned_to": self.storekeeper,
            "observer": self.manager,
            "created_by": self.manager_user,
            "due_date": timezone.now() + timedelta(days=1),
        }
        values.update(kwargs)
        return Task.objects.create(**values)

    def test_manager_creates_internal_task_with_participant_and_checklist(self):
        self.client.force_login(self.manager_user)
        due_date = timezone.localtime(timezone.now() + timedelta(days=2)).strftime(
            "%Y-%m-%dT%H:%M"
        )

        response = self.client.post(
            reverse("todo:create") + "?kind=warehouse_internal",
            {
                "kind": Task.KIND_WAREHOUSE_INTERNAL,
                "title": "Подготовить внутреннюю зону",
                "description": "Освободить и проверить место.",
                "assigned_to": str(self.storekeeper.pk),
                "participants": [str(self.other_storekeeper.pk)],
                "observer": str(self.manager.pk),
                "priority": "high",
                "due_date": due_date,
                "checklist-TOTAL_FORMS": "2",
                "checklist-INITIAL_FORMS": "0",
                "checklist-MIN_NUM_FORMS": "0",
                "checklist-MAX_NUM_FORMS": "1000",
                "checklist-0-title": "Проверить ячейку",
                "checklist-1-title": "Сообщить результат",
            },
        )

        self.assertEqual(response.status_code, 302)
        task = Task.objects.get(title="Подготовить внутреннюю зону")
        self.assertEqual(task.kind, Task.KIND_WAREHOUSE_INTERNAL)
        self.assertEqual(task.assigned_to, self.storekeeper)
        self.assertEqual(list(task.participants.all()), [self.other_storekeeper])
        self.assertEqual(
            list(task.checklist_items.values_list("title", flat=True)),
            ["Проверить ячейку", "Сообщить результат"],
        )

    def test_internal_task_screens_render_for_manager_and_responsible(self):
        self.client.force_login(self.manager_user)

        list_response = self.client.get(
            reverse("todo:list"),
            {"kind": Task.KIND_WAREHOUSE_INTERNAL},
        )
        create_response = self.client.get(
            reverse("todo:create"),
            {"kind": Task.KIND_WAREHOUSE_INTERNAL},
        )

        self.assertEqual(list_response.status_code, 200)
        self.assertContains(list_response, "Внутренние задачи склада")
        self.assertEqual(create_response.status_code, 200)
        self.assertContains(create_response, "Новая задача складу")
        self.assertContains(create_response, "Чек-лист")

        task = self._task()
        TaskChecklistItem.objects.create(
            task=task,
            title="Проверить результат",
            position=0,
        )
        self.client.force_login(self.storekeeper_user)

        detail_response = self.client.get(reverse("todo:detail", args=[task.pk]))

        self.assertEqual(detail_response.status_code, 200)
        self.assertContains(detail_response, "Проверить внутреннее размещение")
        self.assertContains(detail_response, "Взять в работу")
        self.assertContains(detail_response, "Проверить результат")

    def test_same_role_employee_cannot_see_another_person_task(self):
        task = self._task()
        request = RequestFactory().get(
            reverse("todo:list"),
            {"kind": Task.KIND_WAREHOUSE_INTERNAL, "status": "all"},
        )
        request.user = self.other_storekeeper_user

        tasks = build_internal_task_list_queryset(request=request)

        self.assertNotIn(task, list(tasks))

    def test_participant_updates_checklist_but_only_responsible_completes(self):
        task = self._task()
        task.participants.add(self.other_storekeeper)
        item = TaskChecklistItem.objects.create(
            task=task,
            title="Проверить результат",
            position=0,
        )
        self.client.force_login(self.other_storekeeper_user)

        response = self.client.post(
            reverse("todo:detail", args=[task.pk]),
            {"action": "toggle_checklist", "item_id": str(item.pk)},
        )
        self.assertEqual(response.status_code, 302)
        item.refresh_from_db()
        task.refresh_from_db()
        self.assertTrue(item.is_completed)
        self.assertEqual(task.status, "in_progress")

        self.client.post(reverse("todo:detail", args=[task.pk]), {"action": "complete"})
        task.refresh_from_db()
        self.assertEqual(task.status, "in_progress")

        self.client.force_login(self.storekeeper_user)
        self.client.post(reverse("todo:detail", args=[task.pk]), {"action": "complete"})
        task.refresh_from_db()
        self.assertEqual(task.status, "done")

    def test_responsible_cannot_complete_with_open_checklist_item(self):
        task = self._task(status="in_progress")
        TaskChecklistItem.objects.create(task=task, title="Открытый пункт", position=0)
        self.client.force_login(self.storekeeper_user)

        self.client.post(reverse("todo:detail", args=[task.pk]), {"action": "complete"})

        task.refresh_from_db()
        self.assertEqual(task.status, "in_progress")

    def test_internal_task_is_not_rendered_in_operational_panel(self):
        self._task(title="Скрытая внутренняя задача")

        html = Template(
            "{% load todo_panel %}{% task_panel role='storekeeper' %}"
        ).render(Context({}))

        self.assertNotIn("Скрытая внутренняя задача", html)


class TodoDisplayTitleTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()

    def test_display_title_formats_shipping_order_number(self):
        agency = Agency.objects.create(agn_name="Клиент todo")
        order = ShippingOrder.objects.create(
            number="SO-000001",
            agency=agency,
        )
        task = Task.objects.create(
            title="Проверьте заявку на отгрузку №SO-000001",
            route=f"/shipping/{order.pk}/",
        )

        self.assertEqual(task.display_title(), "Заявка на отгрузку №1_OTG")

    def test_display_title_formats_storekeeper_shipping_title(self):
        agency = Agency.objects.create(agn_name="Клиент shipping sklad")
        order = ShippingOrder.objects.create(
            number="SO-000001",
            agency=agency,
        )
        task = Task.objects.create(
            title="Подготовьте заявку на отгрузку №SO-000001",
            route=f"/shipping/{order.pk}/",
        )

        self.assertEqual(task.display_title(), "Заявка на отгрузку №1_OTG")

    def test_storekeeper_request_journal_status_cards_filter_rows(self):
        user = get_user_model().objects.create_user(
            username="storekeeper_status_cards",
            password="pwd",
        )
        storekeeper = Employee.objects.create(
            full_name="Кладовщиков Статус",
            role="storekeeper",
            user=user,
        )
        now = timezone.localtime().replace(hour=12, minute=0, second=0, microsecond=0)
        overdue = Task.objects.create(
            title="Просроченная заявка №OVERDUE",
            route="/todo/overdue/",
            status="in_progress",
            due_date=now - timedelta(days=1),
            assigned_to=storekeeper,
        )
        today = Task.objects.create(
            title="Сегодняшняя заявка №TODAY",
            route="/todo/today/",
            status="in_progress",
            due_date=now,
            assigned_to=storekeeper,
        )
        Task.objects.create(
            title="Будущая заявка №SOON",
            route="/todo/soon/",
            status="in_progress",
            due_date=now + timedelta(days=1),
            assigned_to=storekeeper,
        )
        Task.objects.create(
            title="Готовая заявка №DONE",
            route="/todo/done/",
            status="done",
            assigned_to=storekeeper,
        )
        request = self.factory.get(
            "/sklad/",
            {
                "todo_filters_applied": "1",
                "todo_filter_type": "all",
                "todo_filter_schedule_status": "backlog",
            },
        )
        request.user = user
        request.session = {}

        html = Template(
            "{% load sklad_request_journal %}{% storekeeper_request_journal %}"
        ).render(RequestContext(request, {}))

        self.assertIn(f'data-request-row="{overdue.pk}"', html)
        self.assertNotIn(f'data-request-row="{today.pk}"', html)
        self.assertIn('class="request-stat backlog active"', html)
        self.assertIn("todo_filter_schedule_status=in_progress", html)
        self.assertIn("todo_filter_schedule_status=blocked", html)
        self.assertIn("todo_filter_schedule_status=done", html)

    def test_display_title_formats_receiving_order_number(self):
        agency = Agency.objects.create(agn_name="Клиент приемки")
        entry = OrderAuditEntry.objects.create(
            order_id="3",
            order_type="receiving",
            action="status",
            agency=agency,
            payload={"status": "warehouse", "items": [{"sku_code": "SKU-1", "qty": "10"}]},
        )
        created_at = timezone.make_aware(datetime(2026, 5, 12, 9, 30))
        OrderAuditEntry.objects.filter(pk=entry.pk).update(created_at=created_at)
        task = Task.objects.create(
            title="Принять заявку на приемку товара №3",
            route="/orders/receiving/3/",
        )

        self.assertEqual(task.display_title(), "Заявка на приемку №3_PR от 12.05.2026")

    def test_display_title_formats_receiving_order_without_items(self):
        agency = Agency.objects.create(agn_name="Клиент без товаров")
        entry = OrderAuditEntry.objects.create(
            order_id="4",
            order_type="receiving",
            action="status",
            agency=agency,
            payload={"status": "warehouse", "items": []},
        )
        created_at = timezone.make_aware(datetime(2026, 5, 10, 14, 5))
        OrderAuditEntry.objects.filter(pk=entry.pk).update(created_at=created_at)
        task = Task.objects.create(
            title="Принять заявку на приемку товара №4",
            route="/orders/receiving/4/",
        )

        self.assertEqual(task.display_title(), "Заявка на приемку без указания товара №4_PR от 10.05.2026")

    def test_display_title_formats_receiving_order_with_mismatch(self):
        agency = Agency.objects.create(agn_name="Клиент с расхождениями")
        created_entry = OrderAuditEntry.objects.create(
            order_id="5",
            order_type="receiving",
            action="status",
            agency=agency,
            payload={"status": "warehouse", "items": [{"sku_code": "SKU-5", "qty": "5"}]},
        )
        mismatch_entry = OrderAuditEntry.objects.create(
            order_id="5",
            order_type="receiving",
            action="status",
            agency=agency,
            payload={
                "act": "receiving",
                "act_label": "Акт приемки с расхождениями",
                "act_mismatch": True,
                "act_items": [
                    {"sku_code": "SKU-5", "planned_qty": 5, "actual_qty": 3},
                ],
            },
        )
        OrderAuditEntry.objects.filter(pk=created_entry.pk).update(
            created_at=timezone.make_aware(datetime(2026, 5, 8, 11, 0))
        )
        OrderAuditEntry.objects.filter(pk=mismatch_entry.pk).update(
            created_at=timezone.make_aware(datetime(2026, 5, 12, 16, 45))
        )
        task = Task.objects.create(
            title="Принять заявку на приемку товара №5",
            route="/orders/receiving/5/",
        )

        self.assertEqual(task.display_title(), "Заявка на приемку с расхождениями №5_PR от 08.05.2026")

    def test_display_title_prefers_latest_receiving_fact_without_mismatch(self):
        agency = Agency.objects.create(agn_name="Клиент исправленной приемки")
        created_entry = OrderAuditEntry.objects.create(
            order_id="5A",
            order_type="receiving",
            action="status",
            agency=agency,
            payload={"status": "warehouse", "items": [{"sku_code": "SKU-5A", "qty": "5"}]},
        )
        mismatch_entry = OrderAuditEntry.objects.create(
            order_id="5A",
            order_type="receiving",
            action="status",
            agency=agency,
            payload={
                "act": "receiving",
                "act_label": "Акт приемки с расхождениями",
                "act_mismatch": True,
                "act_items": [
                    {"sku_code": "SKU-5A", "planned_qty": 5, "actual_qty": 3},
                ],
            },
        )
        clean_entry = OrderAuditEntry.objects.create(
            order_id="5A",
            order_type="receiving",
            action="status",
            agency=agency,
            payload={
                "act": "receiving",
                "act_label": "Акт приемки",
                "act_mismatch": False,
                "act_items": [
                    {"sku_code": "SKU-5A", "planned_qty": 5, "actual_qty": 5},
                ],
            },
        )
        OrderAuditEntry.objects.filter(pk=created_entry.pk).update(
            created_at=timezone.make_aware(datetime(2026, 5, 8, 11, 0))
        )
        OrderAuditEntry.objects.filter(pk=mismatch_entry.pk).update(
            created_at=timezone.make_aware(datetime(2026, 5, 12, 16, 45))
        )
        OrderAuditEntry.objects.filter(pk=clean_entry.pk).update(
            created_at=timezone.make_aware(datetime(2026, 5, 13, 9, 15))
        )
        task = Task.objects.create(
            title="Принять заявку на приемку товара №5A",
            route="/orders/receiving/5A/",
        )

        self.assertEqual(task.display_title(), "Заявка на приемку №5A от 08.05.2026")

    def test_display_title_keeps_without_items_when_fact_arrives_later(self):
        agency = Agency.objects.create(agn_name="Клиент без товара в заявке")
        created_entry = OrderAuditEntry.objects.create(
            order_id="6",
            order_type="receiving",
            action="create",
            agency=agency,
            payload={"status": "sent_unconfirmed", "items": []},
        )
        mismatch_entry = OrderAuditEntry.objects.create(
            order_id="6",
            order_type="receiving",
            action="status",
            agency=agency,
            payload={
                "act": "receiving",
                "act_label": "Акт приемки с расхождениями",
                "act_mismatch": True,
                "act_items": [
                    {"sku_code": "SKU-6", "planned_qty": 0, "actual_qty": 4},
                ],
            },
        )
        OrderAuditEntry.objects.filter(pk=created_entry.pk).update(
            created_at=timezone.make_aware(datetime(2026, 5, 6, 8, 15))
        )
        OrderAuditEntry.objects.filter(pk=mismatch_entry.pk).update(
            created_at=timezone.make_aware(datetime(2026, 5, 12, 10, 20))
        )
        task = Task.objects.create(
            title="Принять заявку на приемку товара №6",
            route="/orders/receiving/6/",
        )

        self.assertEqual(task.display_title(), "Заявка на приемку без указания товара №6_PR от 06.05.2026")

    def test_display_title_formats_processing_order_number(self):
        task = Task.objects.create(
            title="Заявка на обработку №7",
            route="/orders/processing/7/",
        )

        self.assertEqual(task.display_title(), "Заявка на обработку №7_OBR")

    def test_task_panel_shows_client_badge_for_shipping_review(self):
        agency = Agency.objects.create(agn_name="Индивидуальный предприниматель Опра Сергей Николаевич")
        manager = Employee.objects.create(full_name="Менеджеров Сергей", role="manager")
        order = ShippingOrder.objects.create(
            number="SO-000001",
            agency=agency,
        )
        Task.objects.create(
            title="Проверьте заявку на отгрузку №SO-000001",
            route=f"/shipping/{order.pk}/",
            status="blocked",
            assigned_to=manager,
        )

        html = Template(
            "{% load todo_panel %}{% task_panel role='manager' %}"
        ).render(Context({}))

        self.assertIn('<span class="task-meta-label">Клиент:</span>', html)
        self.assertIn("ИП Опра Сергей Николаевич", html)
        self.assertIn(f"onclick=\"window.location.href='/shipping/{order.pk}/'\"", html)
        self.assertIn(f'<a href="/shipping/{order.pk}/">Проверьте заявку на отгрузку №1_OTG</a>', html)
        self.assertNotIn("Постановщик:", html)
        self.assertNotIn('<span class="task-tag">Клиент: ИП Опра Сергей Николаевич</span>', html)

    def test_task_panel_shows_shipping_status_label(self):
        agency = Agency.objects.create(agn_name="Клиент со статусом")
        manager = Employee.objects.create(full_name="Менеджеров Сергей", role="manager")
        order = ShippingOrder.objects.create(
            number="SO-000002",
            agency=agency,
            status=ShippingOrder.STATUS_RESERVED,
        )
        Task.objects.create(
            title="Проверьте заявку на отгрузку №SO-000002",
            route=f"/shipping/{order.pk}/",
            status="done",
            assigned_to=manager,
        )

        html = Template(
            "{% load todo_panel %}{% task_panel role='manager' %}"
        ).render(Context({}))

        self.assertNotIn("Статус заявки:", html)
        self.assertIn("Согласована и передана в работу кладовщику", html)

    def test_task_panel_shows_storekeeper_accepted_shipping_status_label(self):
        agency = Agency.objects.create(agn_name="Клиент склада")
        storekeeper = Employee.objects.create(full_name="Кладовщиков Алексей", role="storekeeper")
        order = ShippingOrder.objects.create(
            number="SO-000003",
            agency=agency,
            status=ShippingOrder.STATUS_STOREKEEPER_ACCEPTED,
        )
        Task.objects.create(
            title="Подготовьте заявку на отгрузку №SO-000003",
            route=f"/shipping/{order.pk}/",
            status="in_progress",
            assigned_to=storekeeper,
        )

        html = Template(
            "{% load todo_panel %}{% task_panel role='storekeeper' %}"
        ).render(Context({}))

        self.assertNotIn("Статус заявки:", html)
        self.assertIn("Принята в работу складом", html)

    def test_task_panel_shows_preparing_for_trip_status_for_packed_shipping_order(self):
        agency = Agency.objects.create(agn_name="Клиент рейса")
        storekeeper = Employee.objects.create(full_name="Кладовщиков Алексей", role="storekeeper")
        logistician = Employee.objects.create(full_name="Логистов Сергей", role="logistician")
        order = ShippingOrder.objects.create(
            number="SO-000004",
            agency=agency,
            status=ShippingOrder.STATUS_PACKED,
        )
        trip = LogisticsTrip.objects.create(
            number="7_RS",
            status=LogisticsTrip.STATUS_PLANNED,
            assigned_logistician=logistician,
        )
        LogisticsTripOrder.objects.create(
            trip=trip,
            shipping_order=order,
            loading_sequence=1,
            delivery_sequence=1,
        )
        Task.objects.create(
            title="Подготовьте заявку на отгрузку №SO-000004",
            route=f"/shipping/{order.pk}/",
            status="in_progress",
            assigned_to=storekeeper,
        )

        html = Template(
            "{% load todo_panel %}{% task_panel role='storekeeper' %}"
        ).render(Context({}))

        self.assertIn("Подготовка к рейсу", html)
        self.assertNotIn("Короба на новых паллетах", html)

    def test_task_panel_shows_loaded_for_trip_status_for_departed_shipping_order(self):
        agency = Agency.objects.create(agn_name="Клиент в пути")
        storekeeper = Employee.objects.create(full_name="Кладовщиков Алексей", role="storekeeper")
        logistician = Employee.objects.create(full_name="Логистов Сергей", role="logistician")
        order = ShippingOrder.objects.create(
            number="SO-000004A",
            agency=agency,
            status=ShippingOrder.STATUS_PACKED,
        )
        trip = LogisticsTrip.objects.create(
            number="8_RS",
            status=LogisticsTrip.STATUS_DEPARTED,
            assigned_logistician=logistician,
        )
        LogisticsTripOrder.objects.create(
            trip=trip,
            shipping_order=order,
            loading_sequence=1,
            delivery_sequence=1,
        )
        Task.objects.create(
            title="Подготовьте заявку на отгрузку №SO-000004A",
            route=f"/shipping/{order.pk}/",
            status="in_progress",
            assigned_to=storekeeper,
        )

        html = Template(
            "{% load todo_panel %}{% task_panel role='storekeeper' %}"
        ).render(Context({}))

        self.assertIn("Загружено в машину", html)
        self.assertNotIn("Короба на новых паллетах", html)

    def test_task_panel_shows_otg_palletizing_status_when_boxes_delivered(self):
        agency = Agency.objects.create(agn_name="Клиент OTG")
        storekeeper = Employee.objects.create(full_name="Кладовщиков Алексей", role="storekeeper")
        order = ShippingOrder.objects.create(
            number="SO-000004B",
            agency=agency,
            status=ShippingOrder.STATUS_PICKING,
        )
        OrderAuditEntry.objects.create(
            agency=agency,
            order_type="receiving",
            order_id="R-TODO-1",
            action="placement",
            payload={
                "act": "placement",
                "act_state": "closed",
                "act_boxes": [
                    {
                        "code": "BX-TODO-1",
                        "qty": 12,
                        "items": [
                            {
                                "sku_code": "SKU-TODO-1",
                                "name": "Товар OTG",
                                "size": "42",
                                "barcode": "200000000401",
                                "goods_type": "Готовый",
                                "qty": 12,
                            }
                        ],
                    }
                ],
            },
        )
        move_request = MoveRequest.objects.create(
            context_type=MoveRequest.CONTEXT_MANUAL,
            context_id="",
            agency=agency,
            destination_zone="OTG",
            status=MoveRequest.STATUS_DONE,
        )
        MoveTask.objects.create(
            request=move_request,
            pallet_code="PL-TODO-OTG",
            to_zone="OTG",
            status=MoveTask.STATUS_DONE,
            payload={
                "shipping_order_id": order.number,
                "shipping_order_pk": order.pk,
                "receiving_order_id": "R-TODO-1",
                "picked_boxes": ["BX-TODO-1"],
                "picked_rows": [{"box_code": "BX-TODO-1", "qty": 12}],
            },
        )
        Task.objects.create(
            title="Подготовьте заявку на отгрузку №SO-000004B",
            route=f"/shipping/{order.pk}/",
            status="in_progress",
            assigned_to=storekeeper,
        )

        html = Template(
            "{% load todo_panel %}{% task_panel role='storekeeper' %}"
        ).render(Context({}))

        self.assertIn("Товар доставлен в OTG, ожидает паллетизации", html)
        self.assertNotIn("Доставка в зону отгрузки (ричтрак)", html)

    def test_task_panel_deduplicates_shipping_order_tasks_for_manager(self):
        agency = Agency.objects.create(agn_name="Клиент отгрузки")
        manager = Employee.objects.create(full_name="Менеджеров Сергей", role="manager")
        storekeeper = Employee.objects.create(full_name="Кладовщиков Алексей", role="storekeeper")
        order = ShippingOrder.objects.create(
            number="SO-000005",
            agency=agency,
            status=ShippingOrder.STATUS_PACKED,
        )
        Task.objects.create(
            title="Заявка на отгрузку №SO-000005",
            route=f"/shipping/{order.pk}/",
            status="done",
            assigned_to=manager,
        )
        Task.objects.create(
            title="Заявка на отгрузку №SO-000005",
            route=f"/shipping/{order.pk}/",
            status="done",
            assigned_to=storekeeper,
            observer=manager,
        )

        html = Template(
            "{% load todo_panel %}{% task_panel role='manager' %}"
        ).render(Context({}))

        self.assertEqual(html.count("Заявка на отгрузку №5_OTG"), 1)
        self.assertEqual(html.count("Подготовлена складом, ожидает логиста"), 1)

    def test_storekeeper_task_panel_keeps_active_shipping_supplement_as_separate_card(self):
        agency = Agency.objects.create(agn_name="Клиент добора")
        storekeeper = Employee.objects.create(full_name="Кладовщиков Добор", role="storekeeper")
        order = ShippingOrder.objects.create(
            number="OTG-000216",
            agency=agency,
            status=ShippingOrder.STATUS_STOREKEEPER_ACCEPTED,
        )
        Task.objects.create(
            title="Заявка на отгрузку №OTG-000216",
            route=f"/shipping/{order.pk}/",
            status="in_progress",
            priority="normal",
            assigned_to=storekeeper,
            due_date=timezone.localtime(),
        )
        Task.objects.create(
            title="СРОЧНО: подтвердить добор OTG-000216",
            description="Количество к добору: 10 шт.\nСтарое задание добора.",
            route=f"/shipping/{order.pk}/",
            status="in_progress",
            priority="urgent",
            assigned_to=storekeeper,
            due_date=timezone.localtime(),
        )
        Task.objects.create(
            title="СРОЧНО: подтвердить добор OTG-000216",
            description=(
                "Режим добора: поштучно\n"
                "Количество к добору: 32 шт.\n"
                "Откройте заявку и подтвердите задание."
            ),
            route=f"/shipping/{order.pk}/",
            status="in_progress",
            priority="urgent",
            assigned_to=storekeeper,
            due_date=timezone.localtime(),
        )

        html = Template(
            "{% load todo_panel %}{% task_panel role='storekeeper' %}"
        ).render(Context({}))

        self.assertEqual(html.count('class="task-card clickable'), 2)
        self.assertEqual(html.count("shipping-supplement"), 1)
        self.assertIn("Заявка на отгрузку №216_OTG", html)
        self.assertIn("СРОЧНО: подтвердить добор OTG-000216", html)
        self.assertIn("Количество к добору:</span>", html)
        self.assertIn("32 шт.", html)
        self.assertNotIn("10 шт.", html)
        self.assertIn("Срочно", html)
        self.assertIn("Подтвердить добор", html)

    def test_storekeeper_task_panel_shows_supplement_packing_as_direct_urgent_action(self):
        agency = Agency.objects.create(agn_name="Клиент упаковки добора")
        storekeeper = Employee.objects.create(
            full_name="Кладовщиков Упаковка",
            role="storekeeper",
            is_active=True,
        )
        order = ShippingOrder.objects.create(
            number="OTG-000222",
            agency=agency,
            status=ShippingOrder.STATUS_STOREKEEPER_ACCEPTED,
        )
        Task.objects.create(
            title="Заявка на отгрузку №OTG-000222",
            route=f"/shipping/{order.pk}/",
            status="in_progress",
            priority="normal",
            assigned_to=storekeeper,
            due_date=timezone.localtime(),
        )
        Task.objects.create(
            title="СРОЧНО: упаковать добор OTG-000222",
            description=(
                "Количество к добору: 54 шт.\n"
                "Добор доставлен в OTG. Упакуйте товар в новые короба."
            ),
            route=f"/shipping/{order.pk}/packing/loose/",
            status="in_progress",
            priority="urgent",
            assigned_to=storekeeper,
            due_date=timezone.localtime(),
        )

        html = Template(
            "{% load todo_panel %}{% task_panel role='storekeeper' %}"
        ).render(Context({}))

        self.assertEqual(html.count('class="task-card clickable'), 2)
        self.assertEqual(html.count("shipping-supplement"), 1)
        self.assertIn("СРОЧНО: упаковать добор OTG-000222", html)
        self.assertIn("54 шт.", html)
        self.assertIn("Срочно", html)
        self.assertIn("Упаковать добор", html)
        self.assertIn(f'/shipping/{order.pk}/packing/loose/', html)

    def test_storekeeper_task_panel_chunk_reveals_stale_hidden_base_beside_supplement(self):
        user = get_user_model().objects.create_user(username="storekeeper_supplement_panel", password="pwd")
        agency = Agency.objects.create(agn_name="Клиент snapshot добора")
        storekeeper = Employee.objects.create(
            full_name="Кладовщиков Snapshot",
            role="storekeeper",
            user=user,
            is_active=True,
        )
        order = ShippingOrder.objects.create(
            number="OTG-000217",
            agency=agency,
            status=ShippingOrder.STATUS_STOREKEEPER_ACCEPTED,
        )
        route = f"/shipping/{order.pk}/"
        base_task = Task.objects.create(
            title="Заявка на отгрузку №OTG-000217",
            route=route,
            status="in_progress",
            priority="normal",
            assigned_to=storekeeper,
            due_date=timezone.localtime(),
        )
        supplement_task = Task.objects.create(
            title="СРОЧНО: подтвердить добор OTG-000217",
            description="Количество к добору: 32 шт.\nПодтвердите согласованный добор.",
            route=route,
            status="in_progress",
            priority="urgent",
            assigned_to=storekeeper,
            due_date=timezone.localtime(),
        )
        TaskPanelSnapshot.objects.create(
            task=base_task,
            role_key="storekeeper",
            task_route=route,
            task_status="in_progress",
            filter_type="shipping",
            panel_title="Заявка на отгрузку №217_OTG",
            panel_url=route,
            is_hidden=True,
        )
        TaskPanelSnapshot.objects.create(
            task=supplement_task,
            role_key="storekeeper",
            task_route=route,
            task_status="in_progress",
            filter_type="shipping",
            panel_title="Заявка на отгрузку №217_OTG",
            panel_url=route,
            is_hidden=False,
        )

        self.client.force_login(user)
        response = self.client.get(
            reverse("todo:panel_chunk"),
            {
                "status": "in_progress",
                "role": "storekeeper",
                "offset": 0,
                "limit": 20,
            },
        )

        self.assertEqual(response.status_code, 200)
        html = response.json()["html"]
        self.assertEqual(html.count('class="task-card clickable'), 2)
        self.assertEqual(html.count("shipping-supplement"), 1)
        self.assertIn("Заявка на отгрузку №217_OTG", html)
        self.assertIn("СРОЧНО: подтвердить добор OTG-000217", html)
        self.assertIn("Количество к добору:</span>", html)
        self.assertIn("32 шт.", html)
        self.assertIn("Подтвердить добор", html)

    def test_task_panel_prefers_open_shipping_act_task_for_manager(self):
        agency = Agency.objects.create(agn_name="Клиент акта отгрузки")
        manager = Employee.objects.create(full_name="Менеджеров Сергей", role="manager")
        order = ShippingOrder.objects.create(
            number="SO-000006",
            agency=agency,
            status=ShippingOrder.STATUS_PACKED,
        )
        Task.objects.create(
            title="Заявка на отгрузку №SO-000006",
            route=f"/shipping/{order.pk}/",
            status="done",
            assigned_to=manager,
        )
        Task.objects.create(
            title="Подписать акт отгрузки №SO-000006",
            route=f"/shipping/{order.pk}/act/",
            status="in_progress",
            assigned_to=manager,
        )

        html = Template(
            "{% load todo_panel %}{% task_panel role='manager' %}"
        ).render(Context({}))

        self.assertEqual(html.count("Подписать акт отгрузки №6_OTG"), 1)
        self.assertIn(f'href="/shipping/{order.pk}/act/"', html)
        self.assertNotIn(f'href="/shipping/{order.pk}/">Подписать акт отгрузки №6_OTG</a>', html)

    def test_task_panel_deduplicates_receiving_order_and_sign_task_for_manager(self):
        agency = Agency.objects.create(agn_name="Клиент приемки")
        manager = Employee.objects.create(full_name="Менеджеров Сергей", role="manager")
        OrderAuditEntry.objects.create(
            order_id="1",
            order_type="receiving",
            action="status",
            agency=agency,
            payload={"status": "warehouse", "items": [{"sku_code": "SKU-1", "qty": "10"}]},
        )
        Task.objects.create(
            title="Проверьте размещение по заявке на приемку товара №1",
            route="/orders/receiving/1/",
            status="backlog",
            assigned_to=manager,
        )
        Task.objects.create(
            title="Подписать акт приемки по заявке №1",
            route="/orders/receiving/1/act/print/",
            status="in_progress",
            assigned_to=manager,
        )

        html = Template(
            "{% load todo_panel %}{% task_panel role='manager' %}"
        ).render(Context({}))

        self.assertEqual(html.count("Подписать акт приемки по заявке №1_PR"), 1)
        self.assertIn('href="/orders/receiving/1/act/print/"', html)
        self.assertNotIn('href="/orders/receiving/1/"', html)
        self.assertNotIn("Заявка на приемку №1_PR", html)

    def test_task_panel_hides_receiving_sign_duplicate_for_storekeeper(self):
        user_model = get_user_model()
        agency = Agency.objects.create(agn_name="Клиент приемки склада")
        storekeeper_user = user_model.objects.create_user(username="todo_storekeeper_receiving", password="pwd")
        storekeeper = Employee.objects.create(full_name="Кладовщиков Алексей", role="storekeeper", user=storekeeper_user)
        manager = Employee.objects.create(full_name="Менеджеров Сергей", role="manager")
        OrderAuditEntry.objects.create(
            order_id="2",
            order_type="receiving",
            action="status",
            agency=agency,
            payload={
                "act_storekeeper_signed": True,
                "act_manager_signed": False,
                "status": "warehouse",
            },
        )
        Task.objects.create(
            title="Заявка на приемку без указания товара",
            route="/orders/receiving/2/",
            status="in_progress",
            assigned_to=storekeeper,
        )
        Task.objects.create(
            title="Подписать акт приемки по заявке №2",
            route="/orders/receiving/2/act/print/",
            status="in_progress",
            assigned_to=manager,
            created_by=storekeeper_user,
        )

        html = Template(
            "{% load todo_panel %}{% task_panel role='storekeeper' %}"
        ).render(Context({}))

        self.assertEqual(html.count("Заявка на приемку без указания товара №2_PR от"), 1)
        self.assertNotIn("Подписать акт приемки по заявке №2", html)
        self.assertIn("Завершена приемка", html)
        self.assertIn("ВЫДАЙ ЗАДАНИЕ РИЧТРАКЕРУ", html)
        self.assertIn("Последнее изменение:", html)

    def test_storekeeper_panel_keeps_erroneously_deleted_receiving_done(self):
        agency = Agency.objects.create(agn_name="Клиент ошибочной приемки")
        storekeeper = Employee.objects.create(
            full_name="Кладовщиков Ошибочная",
            role="storekeeper",
        )
        order_id = "188"
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="receiving",
            action="warehouse_erroneous_deleted",
            agency=agency,
            payload={
                "status": "cancelled",
                "submit_action": "cancelled",
                "status_label": "Удалена как ошибочная",
                "deleted_as_erroneous": True,
                "cancel_reason": "Создана ошибочно",
            },
        )
        Task.objects.create(
            title="Принять заявку на приемку товара №188",
            route=f"/orders/receiving/{order_id}/",
            status="done",
            assigned_to=storekeeper,
        )

        html = Template(
            "{% load todo_panel %}{% task_panel role='storekeeper' %}"
        ).render(Context({}))

        self.assertIn("Удалена как ошибочная", html)
        self.assertRegex(
            html,
            r'(?s)<div class="task-col done">.*?№188_PR',
        )

    def test_task_panel_keeps_without_items_title_when_receiving_fact_has_mismatch(self):
        agency = Agency.objects.create(agn_name="Клиент пустой приемки")
        storekeeper = Employee.objects.create(full_name="Кладовщиков Алексей", role="storekeeper")
        created_entry = OrderAuditEntry.objects.create(
            order_id="7",
            order_type="receiving",
            action="create",
            agency=agency,
            payload={"status": "sent_unconfirmed", "items": []},
        )
        mismatch_entry = OrderAuditEntry.objects.create(
            order_id="7",
            order_type="receiving",
            action="status",
            agency=agency,
            payload={
                "act": "receiving",
                "act_label": "Акт приемки с расхождениями",
                "act_mismatch": True,
                "status": "warehouse",
                "status_label": "Товар принят и размещен на складе",
                "act_items": [
                    {"sku_code": "SKU-7", "planned_qty": 0, "actual_qty": 2},
                ],
            },
        )
        OrderAuditEntry.objects.filter(pk=created_entry.pk).update(
            created_at=timezone.make_aware(datetime(2026, 5, 6, 9, 0))
        )
        OrderAuditEntry.objects.filter(pk=mismatch_entry.pk).update(
            created_at=timezone.make_aware(datetime(2026, 5, 7, 12, 17))
        )
        Task.objects.create(
            title="Принять заявку на приемку товара №7",
            route="/orders/receiving/7/",
            status="done",
            assigned_to=storekeeper,
        )

        html = Template(
            "{% load todo_panel %}{% task_panel role='storekeeper' %}"
        ).render(Context({}))

        self.assertIn("Заявка на приемку без указания товара №7_PR от 06.05.2026", html)
        self.assertNotIn("Заявка на приемку с расхождениями №7_PR", html)
        self.assertIn("Последнее изменение:</span>", html)
        self.assertIn("07.05.2026 12:17</span>", html)

    def test_task_panel_keeps_storekeeper_receiving_open_until_storage(self):
        agency = Agency.objects.create(agn_name="Клиент открытой приемки")
        storekeeper = Employee.objects.create(full_name="Кладовщиков Алексей", role="storekeeper")
        order_id = "92"
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="receiving",
            action="status",
            agency=agency,
            payload={
                "status": "warehouse",
                "status_label": "В ожидании поставки товара",
                "items": [{"sku_code": "SKU-92", "qty": "4"}],
            },
        )
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="receiving",
            action="status",
            agency=agency,
            payload={
                "act": "receiving",
                "act_label": "Акт приемки",
                "act_storekeeper_signed": True,
            },
        )
        location = WarehouseWritePathService.ensure_location(warehouse_code="MSK", zone_code="PR")
        WarehouseStockSnapshot.objects.create(
            agency=agency,
            source_context_type="receiving",
            source_context_id=order_id,
            sku_code="SKU-92",
            name="Товар приемки",
            size="42",
            barcode="200000009902",
            goods_type="gv",
            qty=4,
            available_qty=4,
            container_code="PAL-STATUS-92",
            location=location,
            zone_code=location.zone_code,
            zone_kind=location.zone_kind,
            warehouse_state_code="placed_in_receiving",
        )
        Task.objects.create(
            title="Приемка открыта для кладовщика",
            route=f"/orders/receiving/{order_id}/",
            status="done",
            assigned_to=storekeeper,
        )

        html = Template(
            "{% load todo_panel %}{% task_panel role='storekeeper' %}"
        ).render(Context({}))

        self.assertIn("Завершена приемка", html)
        self.assertIn("ВЫДАЙ ЗАДАНИЕ РИЧТРАКЕРУ", html)
        self.assertRegex(html, r'(?s)<div class="task-col done">.*?<div class="pill done">0</div>')
        self.assertRegex(html, r'(?s)<div class="task-col blocked">.*?<div class="pill blocked">1</div>')

    def test_task_panel_marks_manager_receiving_done_after_manager_sign(self):
        agency = Agency.objects.create(agn_name="Клиент приемки менеджера")
        manager = Employee.objects.create(full_name="Менеджеров Сергей", role="manager")
        order_id = "93"
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="receiving",
            action="status",
            agency=agency,
            payload={
                "status": "warehouse",
                "status_label": "В ожидании поставки товара",
                "items": [{"sku_code": "SKU-93", "qty": "4"}],
            },
        )
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="receiving",
            action="status",
            agency=agency,
            payload={
                "act": "receiving",
                "act_label": "Акт приемки",
                "act_storekeeper_signed": True,
                "act_manager_signed": True,
            },
        )
        location = WarehouseWritePathService.ensure_location(warehouse_code="MSK", zone_code="PR")
        WarehouseStockSnapshot.objects.create(
            agency=agency,
            source_context_type="receiving",
            source_context_id=order_id,
            sku_code="SKU-93",
            name="Товар приемки менеджера",
            size="42",
            barcode="200000009903",
            goods_type="gv",
            qty=4,
            available_qty=4,
            container_code="PAL-STATUS-93",
            location=location,
            zone_code=location.zone_code,
            zone_kind=location.zone_kind,
            warehouse_state_code="placed_in_receiving",
        )
        Task.objects.create(
            title="Приемка завершена для менеджера",
            route=f"/orders/receiving/{order_id}/",
            status="backlog",
            assigned_to=manager,
        )

        html = Template(
            "{% load todo_panel %}{% task_panel role='manager' %}"
        ).render(Context({}))

        self.assertIn("Выполнена", html)
        self.assertRegex(html, r'(?s)<div class="task-col done">.*?<div class="pill done">1</div>')

    def test_task_panel_storekeeper_waits_for_reachtruck_when_it_was_issued_before_manager_sign(self):
        agency = Agency.objects.create(agn_name="Клиент ожидания ричтрака")
        storekeeper = Employee.objects.create(full_name="Кладовщиков Алексей", role="storekeeper")
        order_id = "94"
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="receiving",
            action="status",
            agency=agency,
            payload={
                "status": "warehouse",
                "status_label": "В ожидании поставки товара",
                "items": [{"sku_code": "SKU-94", "qty": "4"}],
            },
        )
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="receiving",
            action="status",
            agency=agency,
            payload={
                "act": "receiving",
                "act_label": "Акт приемки",
                "act_storekeeper_signed": True,
            },
        )
        location = WarehouseWritePathService.ensure_location(warehouse_code="MSK", zone_code="PR")
        WarehouseStockSnapshot.objects.create(
            agency=agency,
            source_context_type="receiving",
            source_context_id=order_id,
            sku_code="SKU-94",
            name="Товар ожидания ричтрака",
            size="42",
            barcode="200000009904",
            goods_type="gv",
            qty=4,
            available_qty=4,
            container_code="PAL-STATUS-94",
            location=location,
            zone_code=location.zone_code,
            zone_kind=location.zone_kind,
            warehouse_state_code="placed_in_receiving",
        )
        move_request = MoveRequest.objects.create(
            context_type=MoveRequest.CONTEXT_RECEIVING,
            context_id=order_id,
            agency=agency,
            destination_zone="OS",
            status=MoveRequest.STATUS_IN_PROGRESS,
        )
        MoveTask.objects.create(
            request=move_request,
            pallet_code="PAL-STATUS-94",
            to_zone="OS",
            status=MoveTask.STATUS_CREATED,
        )
        Task.objects.create(
            title="Приемка ожидает ричтрака",
            route=f"/orders/receiving/{order_id}/",
            status="done",
            assigned_to=storekeeper,
        )

        html = Template(
            "{% load todo_panel %}{% task_panel role='storekeeper' %}"
        ).render(Context({}))

        self.assertIn("Размещение на складе", html)
        self.assertIn("ОЖИДАЕМ РИЧТРАКЕР", html)
        self.assertRegex(html, r'(?s)<div class="task-col done">.*?<div class="pill done">0</div>')

    def test_task_panel_filters_by_order_type(self):
        agency = Agency.objects.create(agn_name="Клиент фильтра типов")
        storekeeper = Employee.objects.create(full_name="Кладовщиков Алексей", role="storekeeper")
        shipping_order = ShippingOrder.objects.create(number="SO-000077", agency=agency)
        OrderAuditEntry.objects.create(
            order_id="11",
            order_type="receiving",
            action="status",
            agency=agency,
            payload={"status": "warehouse", "items": [{"sku_code": "SKU-11", "qty": "10"}]},
        )
        Task.objects.create(
            title="Приемка",
            route="/orders/receiving/11/",
            status="in_progress",
            assigned_to=storekeeper,
        )
        Task.objects.create(
            title="Отгрузка",
            route=f"/shipping/{shipping_order.pk}/",
            status="in_progress",
            assigned_to=storekeeper,
        )
        OrderAuditEntry.objects.create(
            order_id="22",
            order_type="processing",
            action="status",
            agency=agency,
            payload={"status": "in_progress", "status_label": "Взята в работу"},
        )
        Task.objects.create(
            title="Обработка",
            route="/orders/processing/22/",
            status="in_progress",
            assigned_to=storekeeper,
        )

        request = self.factory.get(
            "/team-storekeeper/",
            {"todo_filters_applied": "1", "todo_filter_type": "shipping"},
        )
        html = Template(
            "{% load todo_panel %}{% task_panel role='storekeeper' %}"
        ).render(RequestContext(request, {}))

        self.assertIn("Заявка на отгрузку", html)
        self.assertNotIn("Заявка на приемку №11_PR от", html)
        self.assertNotIn("Заявка на обработку №22_OBR", html)
        self.assertRegex(html, r'(?s)value="processing".*?task-filter-tab-count">\((1)\)</span>')

    def test_manager_task_panel_includes_processing_tasks_and_filter(self):
        agency = Agency.objects.create(agn_name="Клиент обработки менеджера")
        manager = Employee.objects.create(full_name="Менеджеров Сергей", role="manager")
        shipping_order = ShippingOrder.objects.create(number="SO-000078", agency=agency)
        OrderAuditEntry.objects.create(
            order_id="12",
            order_type="receiving",
            action="status",
            agency=agency,
            payload={"status": "warehouse", "items": [{"sku_code": "SKU-12", "qty": "10"}]},
        )
        Task.objects.create(
            title="Приемка",
            route="/orders/receiving/12/",
            status="in_progress",
            assigned_to=manager,
        )
        OrderAuditEntry.objects.create(
            order_id="23",
            order_type="processing",
            action="status",
            agency=agency,
            payload={"status": "sent_unconfirmed", "status_label": "Ждет подтверждения"},
        )
        Task.objects.create(
            title="Обработка",
            route="/orders/processing/23/",
            status="in_progress",
            assigned_to=manager,
        )
        Task.objects.create(
            title="Отгрузка",
            route=f"/shipping/{shipping_order.pk}/",
            status="in_progress",
            assigned_to=manager,
        )

        html = Template(
            "{% load todo_panel %}{% task_panel role='manager' %}"
        ).render(Context({}))

        self.assertIn("Заявка на обработку №23_OBR", html)
        self.assertRegex(html, r'(?s)value="processing".*?task-filter-tab-count">\((1)\)</span>')

        request = self.factory.get(
            "/team-manager/",
            {"todo_filters_applied": "1", "todo_filter_type": "processing"},
        )
        filtered_html = Template(
            "{% load todo_panel %}{% task_panel role='manager' %}"
        ).render(RequestContext(request, {}))

        self.assertIn("Заявка на обработку №23_OBR", filtered_html)
        self.assertNotIn("Заявка на приемку №12_PR от", filtered_html)
        self.assertNotIn("Заявка на отгрузку №78_OTG", filtered_html)

    def test_task_panel_filters_by_client(self):
        client_a = Agency.objects.create(agn_name="Клиент А")
        client_b = Agency.objects.create(agn_name="Клиент Б")
        manager = Employee.objects.create(full_name="Менеджеров Сергей", role="manager")
        order_a = ShippingOrder.objects.create(number="SO-000010", agency=client_a)
        order_b = ShippingOrder.objects.create(number="SO-000011", agency=client_b)
        Task.objects.create(
            title="Отгрузка А",
            route=f"/shipping/{order_a.pk}/",
            status="in_progress",
            assigned_to=manager,
        )
        Task.objects.create(
            title="Отгрузка Б",
            route=f"/shipping/{order_b.pk}/",
            status="in_progress",
            assigned_to=manager,
        )

        request = self.factory.get(
            "/team-manager/",
            {
                "todo_filters_applied": "1",
                "todo_filter_type": "all",
                "todo_filter_client": str(client_b.id),
            },
        )
        html = Template(
            "{% load todo_panel %}{% task_panel role='manager' %}"
        ).render(RequestContext(request, {}))

        self.assertIn("Клиент Б", html)
        self.assertIn("Заявка на отгрузку №11_OTG", html)
        self.assertNotIn("Заявка на отгрузку №10_OTG", html)

    def test_task_panel_filters_by_search_query(self):
        client_a = Agency.objects.create(agn_name="Клиент Альфа")
        client_b = Agency.objects.create(agn_name="Клиент Бета")
        manager = Employee.objects.create(full_name="Менеджеров Сергей", role="manager")
        order_a = ShippingOrder.objects.create(number="SO-000012", agency=client_a)
        order_b = ShippingOrder.objects.create(number="SO-000013", agency=client_b)
        Task.objects.create(
            title="Отгрузка Альфа",
            route=f"/shipping/{order_a.pk}/",
            status="in_progress",
            assigned_to=manager,
        )
        Task.objects.create(
            title="Отгрузка Бета",
            route=f"/shipping/{order_b.pk}/",
            status="in_progress",
            assigned_to=manager,
        )

        request = self.factory.get(
            "/team-manager/",
            {
                "todo_filters_applied": "1",
                "todo_filter_type": "all",
                "todo_filter_query": "13_OTG",
            },
        )
        html = Template(
            "{% load todo_panel %}{% task_panel role='manager' %}"
        ).render(RequestContext(request, {}))

        self.assertIn('name="todo_filter_query"', html)
        self.assertIn('value="13_OTG"', html)
        self.assertIn("Заявка на отгрузку №13_OTG", html)
        self.assertNotIn("Заявка на отгрузку №12_OTG", html)

    def test_processing_head_task_panel_routes_placement_completed_order_to_work_page(self):
        agency = Agency.objects.create(agn_name="Клиент обработки")
        processing_head = Employee.objects.create(full_name="Руководитель обработки", role="processing_head")
        OrderAuditEntry.objects.create(
            order_id="44",
            order_type="processing",
            action="update",
            agency=agency,
            payload={
                "act": "placement",
                "act_state": "closed",
                "flow_closed": True,
                "status": "processing_in_work",
            },
        )
        Task.objects.create(
            title="Заявка на обработку №44",
            route="/orders/processing/44/",
            status="in_progress",
            assigned_to=processing_head,
        )

        html = Template(
            "{% load todo_panel %}{% task_panel role='processing_head' %}"
        ).render(Context({}))

        self.assertIn('onclick="window.location.href=\'/orders/processing/44/work/\'"', html)
        self.assertIn('<a href="/orders/processing/44/work/">Заявка на обработку №44_OBR</a>', html)
        self.assertIn("взято в обработку", html)

    def test_processing_task_panel_prefers_warehouse_status_over_stale_payload(self):
        agency = Agency.objects.create(agn_name="Клиент обработки склад")
        processing_head = Employee.objects.create(full_name="Руководитель обработки", role="processing_head")
        processing_location = WarehouseWritePathService.ensure_location(
            warehouse_code="MSK",
            zone_code="OBR",
        )
        OrderAuditEntry.objects.create(
            order_id="45",
            order_type="processing",
            action="status",
            agency=agency,
            payload={
                "status": "processing_head",
                "status_label": "Передано в обработку",
            },
        )
        snapshot = WarehouseStockSnapshot.objects.create(
            agency=agency,
            source_context_type="legacy_stock",
            source_context_id="legacy-45",
            sku_code="SKU-PROCESS-TODO",
            name="Товар обработки",
            size="42",
            barcode="200000001045",
            goods_type="gv",
            qty=10,
            available_qty=0,
            processing_reserved_qty=10,
            container_code="PAL-PROC-TODO-45",
            location=processing_location,
            zone_code=processing_location.zone_code,
            zone_kind=processing_location.zone_kind,
            warehouse_state_code="processing_in_progress",
        )
        WarehouseReserve.objects.create(
            agency=agency,
            reserve_type=WarehouseReserve.TYPE_PROCESSING,
            context_type="processing",
            context_id="45",
            sku_code=snapshot.sku_code,
            size=snapshot.size,
            barcode=snapshot.barcode,
            goods_type=snapshot.goods_type,
            qty_reserved=10,
            status=WarehouseReserve.STATUS_ACTIVE,
        )
        Task.objects.create(
            title="Заявка на обработку №45",
            route="/orders/processing/45/",
            status="in_progress",
            assigned_to=processing_head,
        )

        html = Template(
            "{% load todo_panel %}{% task_panel role='processing_head' %}"
        ).render(Context({}))

        self.assertIn("утверждено менеджером", html)

    def test_processing_task_panel_keeps_processing_status_when_latest_entry_is_otg_noise(self):
        agency = Agency.objects.create(agn_name="Клиент OTG шума")
        processing_head = Employee.objects.create(full_name="Руководитель обработки", role="processing_head")
        storage_location = WarehouseWritePathService.ensure_location(
            warehouse_code="MSK",
            zone_code="OS",
            row_no=3,
            section_no=2,
            tier_no=1,
            cell_no=4,
        )
        OrderAuditEntry.objects.create(
            order_id="149",
            order_type="processing",
            action="status",
            agency=agency,
            payload={
                "status": "done",
                "status_label": "Заявка завершена",
                "processing_stage": "done",
            },
        )
        OrderAuditEntry.objects.create(
            order_id="149",
            order_type="processing",
            action="status",
            agency=agency,
            payload={
                "act": "placement",
                "act_state": "closed",
                "act_items_removed": True,
            },
        )
        WarehouseStockSnapshot.objects.create(
            agency=agency,
            source_context_type="legacy_stock",
            source_context_id="legacy-149",
            sku_code="SKU-PROCESS-TODO-149",
            name="Товар после размещения",
            size="42",
            barcode="200000001149",
            goods_type="gv",
            qty=5,
            available_qty=5,
            processing_reserved_qty=0,
            container_code="PAL-PROC-TODO-149",
            location=storage_location,
            zone_code=storage_location.zone_code,
            zone_kind=storage_location.zone_kind,
            warehouse_state_code="placed_after_processing",
        )
        WarehouseReserve.objects.create(
            agency=agency,
            reserve_type=WarehouseReserve.TYPE_PROCESSING,
            context_type="processing",
            context_id="149",
            sku_code="SKU-PROCESS-TODO-149",
            size="42",
            barcode="200000001149",
            goods_type="gv",
            qty_reserved=5,
            status=WarehouseReserve.STATUS_SATISFIED,
        )
        Task.objects.create(
            title="Заявка на обработку №149",
            route="/orders/processing/149/",
            status="in_progress",
            assigned_to=processing_head,
        )

        html = Template(
            "{% load todo_panel %}{% task_panel role='processing_head' %}"
        ).render(Context({}))

        self.assertNotIn('task-status-pill status-tone-neutral">-</span>', html)
        self.assertIn("Размещение завершено", html)
        self.assertIn('onclick="window.location.href=\'/orders/processing/149/work/\'"', html)

    def test_processing_head_task_panel_shows_move_to_processing_when_reachtruck_task_active(self):
        agency = Agency.objects.create(agn_name="Клиент доставки в OBR")
        processing_head = Employee.objects.create(full_name="Руководитель обработки", role="processing_head")
        OrderAuditEntry.objects.create(
            order_id="145",
            order_type="processing",
            action="status",
            agency=agency,
            payload={
                "status": "processing_in_work",
                "status_label": "Взята в работу",
            },
        )
        move_request = MoveRequest.objects.create(
            context_type=MoveRequest.CONTEXT_PROCESSING,
            context_id="145",
            agency=agency,
            destination_zone="OBR",
            status=MoveRequest.STATUS_IN_PROGRESS,
        )
        MoveTask.objects.create(
            request=move_request,
            pallet_code="PAL-PROC-TODO-145",
            from_zone="OS",
            to_zone="OBR",
            move_mode=MoveTask.MODE_BOX_PARTIAL,
            status=MoveTask.STATUS_IN_PROGRESS,
            payload={
                "processing_order_id": "145",
                "status": "in_progress",
                "status_label": "В работе",
            },
        )
        Task.objects.create(
            title="Заявка на обработку №145",
            route="/orders/processing/145/",
            status="in_progress",
            assigned_to=processing_head,
        )

        html = Template(
            "{% load todo_panel %}{% task_panel role='processing_head' %}"
        ).render(Context({}))

        self.assertIn("взято в обработку", html)
        self.assertIn("status-tone-active", html)
        self.assertNotIn("Передано в обработку", html)

    def test_processing_head_task_panel_uses_processing_label_for_reserved_order(self):
        agency = Agency.objects.create(agn_name="Клиент ожидания OBR")
        processing_head = Employee.objects.create(full_name="Руководитель обработки", role="processing_head")
        storage_location = WarehouseWritePathService.ensure_location(
            warehouse_code="MSK",
            zone_code="OS",
            row_no=1,
            section_no=1,
            tier_no=3,
            cell_no=2,
        )
        OrderAuditEntry.objects.create(
            order_id="146",
            order_type="processing",
            action="status",
            agency=agency,
            payload={
                "status": "processing_head",
                "status_label": "Передано в обработку",
            },
        )
        WarehouseStockSnapshot.objects.create(
            agency=agency,
            source_context_type="legacy_stock",
            source_context_id="legacy-146",
            sku_code="SKU-PROCESS-TODO-146",
            name="Товар обработки",
            size="42",
            barcode="200000001146",
            goods_type="gv",
            qty=10,
            available_qty=0,
            processing_reserved_qty=10,
            container_code="PAL-PROC-TODO-146",
            location=storage_location,
            zone_code=storage_location.zone_code,
            zone_kind=storage_location.zone_kind,
            warehouse_state_code="reserved_for_processing",
        )
        WarehouseReserve.objects.create(
            agency=agency,
            reserve_type=WarehouseReserve.TYPE_PROCESSING,
            context_type="processing",
            context_id="146",
            sku_code="SKU-PROCESS-TODO-146",
            size="42",
            barcode="200000001146",
            goods_type="gv",
            qty_reserved=10,
            status=WarehouseReserve.STATUS_ACTIVE,
        )
        Task.objects.create(
            title="Заявка на обработку №146",
            route="/orders/processing/146/",
            status="in_progress",
            assigned_to=processing_head,
        )

        html = Template(
            "{% load todo_panel %}{% task_panel role='processing_head' %}"
        ).render(Context({}))

        self.assertIn("утверждено менеджером", html)
        self.assertIn("status-tone-pending", html)
        self.assertNotIn("Передано в обработку", html)

    def test_processing_head_task_panel_keeps_active_processing_label_for_stored_order(self):
        agency = Agency.objects.create(agn_name="Клиент активной обработки")
        processing_head = Employee.objects.create(full_name="Руководитель обработки", role="processing_head")
        storage_location = WarehouseWritePathService.ensure_location(
            warehouse_code="MSK",
            zone_code="OS",
            row_no=2,
            section_no=3,
            tier_no=1,
            cell_no=4,
        )
        OrderAuditEntry.objects.create(
            order_id="147",
            order_type="processing",
            action="status",
            agency=agency,
            payload={
                "status": "processing_in_work",
                "status_label": "Взята в работу",
            },
        )
        WarehouseStockSnapshot.objects.create(
            agency=agency,
            source_context_type="legacy_stock",
            source_context_id="legacy-147",
            sku_code="SKU-PROCESS-TODO-147",
            name="Товар после обработки",
            size="44",
            barcode="200000001147",
            goods_type="gv",
            qty=6,
            available_qty=6,
            processing_reserved_qty=0,
            container_code="PAL-PROC-TODO-147",
            location=storage_location,
            zone_code=storage_location.zone_code,
            zone_kind=storage_location.zone_kind,
            warehouse_state_code="stored",
        )
        WarehouseReserve.objects.create(
            agency=agency,
            reserve_type=WarehouseReserve.TYPE_PROCESSING,
            context_type="processing",
            context_id="147",
            sku_code="SKU-PROCESS-TODO-147",
            size="44",
            barcode="200000001147",
            goods_type="gv",
            qty_reserved=6,
            status=WarehouseReserve.STATUS_SATISFIED,
        )
        Task.objects.create(
            title="Заявка на обработку №147",
            route="/orders/processing/147/",
            status="in_progress",
            assigned_to=processing_head,
        )

        html = Template(
            "{% load todo_panel %}{% task_panel role='processing_head' %}"
        ).render(Context({}))

        self.assertIn("взято в обработку", html)
        self.assertNotIn("Размещение завершено", html)
        self.assertNotIn("Товар возвращен на склад", html)
        self.assertIn('onclick="window.location.href=\'/orders/processing/147/work/\'"', html)

    def test_processing_head_task_panel_shows_placement_completed_for_stored_closed_order(self):
        agency = Agency.objects.create(agn_name="Клиент завершенного размещения")
        processing_head = Employee.objects.create(full_name="Руководитель обработки", role="processing_head")
        storage_location = WarehouseWritePathService.ensure_location(
            warehouse_code="MSK",
            zone_code="OS",
            row_no=4,
            section_no=1,
            tier_no=2,
            cell_no=5,
        )
        OrderAuditEntry.objects.create(
            order_id="148",
            order_type="processing",
            action="update",
            agency=agency,
            payload={
                "status": "processing_in_work",
                "status_label": "Взята в работу",
                "act": "placement",
                "act_state": "closed",
                "flow_closed": True,
            },
        )
        WarehouseStockSnapshot.objects.create(
            agency=agency,
            source_context_type="legacy_stock",
            source_context_id="legacy-148",
            sku_code="SKU-PROCESS-TODO-148",
            name="Товар после завершения размещения",
            size="46",
            barcode="200000001148",
            goods_type="gv",
            qty=4,
            available_qty=4,
            processing_reserved_qty=0,
            container_code="PAL-PROC-TODO-148",
            location=storage_location,
            zone_code=storage_location.zone_code,
            zone_kind=storage_location.zone_kind,
            warehouse_state_code="stored",
        )
        WarehouseReserve.objects.create(
            agency=agency,
            reserve_type=WarehouseReserve.TYPE_PROCESSING,
            context_type="processing",
            context_id="148",
            sku_code="SKU-PROCESS-TODO-148",
            size="46",
            barcode="200000001148",
            goods_type="gv",
            qty_reserved=4,
            status=WarehouseReserve.STATUS_SATISFIED,
        )
        Task.objects.create(
            title="Заявка на обработку №148",
            route="/orders/processing/148/",
            status="in_progress",
            assigned_to=processing_head,
        )

        html = Template(
            "{% load todo_panel %}{% task_panel role='processing_head' %}"
        ).render(Context({}))

        self.assertIn("взято в обработку", html)
        self.assertNotIn("Товар возвращен на склад", html)
        self.assertIn('onclick="window.location.href=\'/orders/processing/148/work/\'"', html)

    def test_manager_processing_task_panel_keeps_waiting_status_before_approval_even_with_reserve(self):
        agency = Agency.objects.create(agn_name="Клиент ожидания обработки")
        manager = Employee.objects.create(full_name="Менеджеров Сергей", role="manager")
        location = WarehouseWritePathService.ensure_location(
            warehouse_code="MSK",
            zone_code="OBR",
        )
        OrderAuditEntry.objects.create(
            order_id="46",
            order_type="processing",
            action="status",
            agency=agency,
            payload={
                "status": "sent_unconfirmed",
                "status_label": "Ждет подтверждения",
            },
        )
        snapshot = WarehouseStockSnapshot.objects.create(
            agency=agency,
            source_context_type="legacy_stock",
            source_context_id="legacy-46",
            sku_code="SKU-PROCESS-TODO-46",
            name="Товар обработки",
            size="42",
            barcode="200000001046",
            goods_type="gv",
            qty=10,
            available_qty=0,
            processing_reserved_qty=10,
            container_code="PAL-PROC-TODO-46",
            location=location,
            zone_code=location.zone_code,
            zone_kind=location.zone_kind,
            warehouse_state_code="reserved_for_processing",
        )
        WarehouseReserve.objects.create(
            agency=agency,
            reserve_type=WarehouseReserve.TYPE_PROCESSING,
            context_type="processing",
            context_id="46",
            sku_code=snapshot.sku_code,
            size=snapshot.size,
            barcode=snapshot.barcode,
            goods_type=snapshot.goods_type,
            qty_reserved=10,
            status=WarehouseReserve.STATUS_ACTIVE,
        )
        Task.objects.create(
            title="Заявка на обработку №46",
            route="/orders/processing/46/",
            status="in_progress",
            assigned_to=manager,
        )

        html = Template(
            "{% load todo_panel %}{% task_panel role='manager' %}"
        ).render(Context({}))

        self.assertIn("Ждет подтверждения", html)
        self.assertNotIn("Передано в обработку", html)

    def test_storekeeper_tabs_include_logistics_and_count_only_open_tasks(self):
        agency = Agency.objects.create(agn_name="Клиент рейсов")
        storekeeper = Employee.objects.create(full_name="Кладовщиков Алексей", role="storekeeper")
        logistician = Employee.objects.create(full_name="Логистов Сергей", role="logistician")
        shipping_order = ShippingOrder.objects.create(number="SO-000078", agency=agency)
        receiving_order_id = "31"
        OrderAuditEntry.objects.create(
            order_id=receiving_order_id,
            order_type="receiving",
            action="status",
            agency=agency,
            payload={"status": "warehouse", "items": [{"sku_code": "SKU-31", "qty": "10"}]},
        )
        Task.objects.create(
            title="Приемка",
            route=f"/orders/receiving/{receiving_order_id}/",
            status="in_progress",
            assigned_to=storekeeper,
        )
        Task.objects.create(
            title="Отгрузка",
            route=f"/shipping/{shipping_order.pk}/",
            status="done",
            assigned_to=storekeeper,
        )
        trip = LogisticsTrip.objects.create(
            number="3_RS",
            status=LogisticsTrip.STATUS_LOADING,
            assigned_logistician=logistician,
        )
        Task.objects.create(
            title="Рейс №3_RS",
            route=f"/logistics/trips/{trip.pk}/",
            status="backlog",
            assigned_to=storekeeper,
        )

        html = Template(
            "{% load todo_panel %}{% task_panel role='storekeeper' %}"
        ).render(Context({}))

        self.assertRegex(html, r'(?s)name="todo_filter_type".*?value="logistics"')
        self.assertIn("Рейсы", html)
        self.assertRegex(html, r'(?s)value="all".*?task-filter-tab-count">\((2)\)</span>')
        self.assertRegex(html, r'(?s)value="receiving".*?task-filter-tab-count">\((1)\)</span>')
        self.assertRegex(html, r'(?s)value="shipping".*?task-filter-tab-count">\((0)\)</span>')
        self.assertRegex(html, r'(?s)value="logistics".*?task-filter-tab-count">\((1)\)</span>')

    def test_task_panel_uses_warehouse_status_for_receiving_before_storage(self):
        agency = Agency.objects.create(agn_name="Клиент статуса склада")
        storekeeper = Employee.objects.create(full_name="Кладовщиков Алексей", role="storekeeper")
        order_id = "91"
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="receiving",
            action="status",
            agency=agency,
            payload={
                "act": "placement",
                "act_state": "closed",
                "status": "warehouse",
                "status_label": "Товар принят и размещен на складе",
            },
        )
        location = WarehouseWritePathService.ensure_location(warehouse_code="MSK", zone_code="PR")
        WarehouseStockSnapshot.objects.create(
            agency=agency,
            source_context_type="receiving",
            source_context_id=order_id,
            sku_code="SKU-STATUS-91",
            name="Товар статуса",
            size="42",
            barcode="200000009901",
            goods_type="gv",
            qty=2,
            available_qty=2,
            container_code="PAL-STATUS-91",
            location=location,
            zone_code=location.zone_code,
            zone_kind=location.zone_kind,
            warehouse_state_code="placed_in_receiving",
        )
        Task.objects.create(
            title="Приемка со статусом склада",
            route=f"/orders/receiving/{order_id}/",
            status="in_progress",
            assigned_to=storekeeper,
        )

        html = Template(
            "{% load todo_panel %}{% task_panel role='storekeeper' %}"
        ).render(Context({}))

        self.assertIn("Завершена приемка", html)
        self.assertNotIn("Товар принят и размещен на складе", html)

    def test_head_manager_tabs_include_logistics_and_filter_trip_tasks(self):
        agency = Agency.objects.create(agn_name="Клиент логистики ГМ")
        head_manager = Employee.objects.create(full_name="Главменеджеров Павел", role="head_manager")
        logistician = Employee.objects.create(full_name="Логистов Сергей", role="logistician")
        shipping_order = ShippingOrder.objects.create(number="SO-000079", agency=agency)
        OrderAuditEntry.objects.create(
            order_id="41",
            order_type="processing",
            action="status",
            agency=agency,
            payload={"status": "in_progress", "status_label": "Взята в работу"},
        )
        Task.objects.create(
            title="Обработка",
            route="/orders/processing/41/",
            status="in_progress",
            assigned_to=head_manager,
        )
        Task.objects.create(
            title="Отгрузка",
            route=f"/shipping/{shipping_order.pk}/",
            status="in_progress",
            assigned_to=head_manager,
        )
        trip = LogisticsTrip.objects.create(
            number="5_RS",
            status=LogisticsTrip.STATUS_LOADING,
            assigned_logistician=logistician,
        )
        Task.objects.create(
            title="Рейс №5_RS",
            route=f"/logistics/trips/{trip.pk}/",
            status="backlog",
            assigned_to=head_manager,
        )

        html = Template(
            "{% load todo_panel %}{% task_panel role='head_manager' %}"
        ).render(Context({}))

        self.assertRegex(html, r'(?s)name="todo_filter_type".*?value="logistics"')
        self.assertIn("Рейсы", html)
        self.assertRegex(html, r'(?s)value="processing".*?task-filter-tab-count">\((1)\)</span>')
        self.assertRegex(html, r'(?s)value="shipping".*?task-filter-tab-count">\((1)\)</span>')
        self.assertRegex(html, r'(?s)value="logistics".*?task-filter-tab-count">\((1)\)</span>')

        request = self.factory.get(
            "/head-manager/",
            {"todo_filters_applied": "1", "todo_filter_type": "logistics"},
        )
        filtered_html = Template(
            "{% load todo_panel %}{% task_panel role='head_manager' %}"
        ).render(RequestContext(request, {}))

        self.assertIn("Рейс №5_RS", filtered_html)
        self.assertNotIn("Заявка на отгрузку №79_OTG", filtered_html)
        self.assertNotIn("Заявка на обработку №41_OBR", filtered_html)

    def test_task_panel_shows_logistics_status_label(self):
        storekeeper = Employee.objects.create(full_name="Кладовщиков Алексей", role="storekeeper")
        logistician = Employee.objects.create(full_name="Логистов Сергей", role="logistician")
        trip = LogisticsTrip.objects.create(
            number="3_RS",
            status=LogisticsTrip.STATUS_LOADING,
            assigned_logistician=logistician,
        )
        Task.objects.create(
            title="Рейс №3_RS",
            route=f"/logistics/trips/{trip.pk}/",
            status="backlog",
            assigned_to=storekeeper,
        )

        html = Template(
            "{% load todo_panel %}{% task_panel role='storekeeper' %}"
        ).render(Context({}))

        self.assertNotIn("Статус рейса:", html)
        self.assertIn("Погрузка", html)

    def test_build_trip_context_uses_shipping_ui_status_label(self):
        agency = Agency.objects.create(agn_name="Клиент статуса логистики")
        logistician = Employee.objects.create(full_name="Логистов Сергей", role="logistician")
        storekeeper = Employee.objects.create(full_name="Кладовщиков Алексей", role="storekeeper")
        order = ShippingOrder.objects.create(
            number="SO-000401",
            agency=agency,
            status=ShippingOrder.STATUS_PACKED,
        )
        trip = LogisticsTrip.objects.create(
            number="11_RS",
            status=LogisticsTrip.STATUS_PLANNED,
            assigned_logistician=logistician,
        )
        LogisticsTripOrder.objects.create(
            trip=trip,
            shipping_order=order,
            loading_sequence=1,
            delivery_sequence=1,
        )
        task = Task.objects.create(
            title="Рейс №11_RS",
            route=f"/logistics/trips/{trip.pk}/",
            status="backlog",
            assigned_to=storekeeper,
        )

        context = build_trip_context(task)

        self.assertIsNotNone(context)
        self.assertEqual(context["orders"][0]["status_label"], "Подготовка к рейсу")


class TodoReturnUrlTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.user = user_model.objects.create_user(
            username="director_todo_return",
            password="pwd",
        )
        Employee.objects.create(
            user=self.user,
            full_name="Директор Иван",
            role="director",
        )
        self.client.force_login(self.user)

    def test_create_redirects_back_to_next_url(self):
        response = self.client.post(
            reverse("todo:create"),
            {
                "title": "Проверить возврат в кабинет",
                "description": "Тестовая задача",
                "assigned_to": "",
                "observer": "",
                "priority": "normal",
                "due_date": timezone.localtime(timezone.now() + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M"),
                "next": "/cabinet/director/",
            },
        )

        self.assertRedirects(response, "/cabinet/director/")
        task = Task.objects.get(title="Проверить возврат в кабинет")
        self.assertEqual(task.created_by, self.user)

    def test_create_form_uses_next_url_in_actions(self):
        response = self.client.get(reverse("todo:create"), {"next": "/cabinet/director/"})

        self.assertContains(response, 'href="/cabinet/director/"')
        self.assertContains(response, 'name="next" value="/cabinet/director/"', html=False)

    def test_cabinet_task_panel_create_link_keeps_origin(self):
        request = RequestFactory().get("/cabinet/director/?tab=tasks")
        request.user = self.user

        html = Template(
            "{% load todo_panel %}{% task_panel role='director' %}"
        ).render(RequestContext(request, {}))

        self.assertIn('href="/todo/new/?next=/cabinet/director/%3Ftab%3Dtasks"', html)


class TodoServiceLayerTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        user_model = get_user_model()
        self.manager_user = user_model.objects.create_user(
            username="todo_service_manager",
            password="pwd",
        )
        self.creator_user = user_model.objects.create_user(
            username="todo_service_creator",
            password="pwd",
        )
        self.manager = Employee.objects.create(
            user=self.manager_user,
            full_name="Менеджеров Сергей",
            role="manager",
            is_active=True,
        )
        self.observer = Employee.objects.create(
            full_name="Наблюдатель Мария",
            role="manager",
            is_active=True,
        )
        self.storekeeper = Employee.objects.create(
            full_name="Кладовщиков Алексей",
            role="storekeeper",
            is_active=True,
        )

    def test_build_task_list_queryset_returns_only_visible_tasks(self):
        assigned_task = Task.objects.create(
            title="Назначенная задача",
            assigned_to=self.manager,
        )
        observer_task = Task.objects.create(
            title="Наблюдаемая задача",
            observer=self.manager,
        )
        creator_task = Task.objects.create(
            title="Созданная мной задача",
            created_by=self.manager_user,
        )
        Task.objects.create(
            title="Чужая задача",
            assigned_to=self.storekeeper,
        )
        request = self.factory.get("/todo/")
        request.user = self.manager_user

        tasks = build_task_list_queryset(request=request, role="manager")

        self.assertCountEqual(
            list(tasks.values_list("pk", flat=True)),
            [assigned_task.pk, observer_task.pk, creator_task.pk],
        )

    def test_can_access_task_allows_creator_and_rejects_unrelated_user(self):
        task = Task.objects.create(
            title="Проверка доступа",
            assigned_to=self.storekeeper,
            created_by=self.creator_user,
        )
        creator_request = self.factory.get(f"/todo/{task.pk}/")
        creator_request.user = self.creator_user
        stranger_request = self.factory.get(f"/todo/{task.pk}/")
        stranger_request.user = self.manager_user

        self.assertTrue(can_access_task(request=creator_request, task=task, role="manager"))
        self.assertFalse(can_access_task(request=stranger_request, task=task, role="manager"))

    def test_storekeeper_receiving_start_actions_are_csrf_post_forms(self):
        storekeeper_user = get_user_model().objects.create_user(
            username="todo_receiving_start_storekeeper",
            password="pwd",
        )
        self.storekeeper.user = storekeeper_user
        self.storekeeper.save(update_fields=["user"])
        agency = Agency.objects.create(agn_name="Клиент приемки todo start")
        order_id = "R-TODO-POST-START-1"
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="receiving",
            action="status",
            agency=agency,
            payload={
                "status": "warehouse",
                "status_label": "В ожидании поставки товара",
                "goods_type": "op",
                "items": [{"sku_code": "SKU-TODO-START", "name": "Товар", "qty": 1}],
            },
        )
        task = Task.objects.create(
            title=f"Принять заявку №{order_id}",
            route=f"/orders/receiving/{order_id}/",
            assigned_to=self.storekeeper,
            status="backlog",
        )
        self.client.force_login(storekeeper_user)

        response = self.client.get(reverse("todo:detail", args=[task.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            f'method="post" action="/orders/receiving/{order_id}/flow/"',
            count=2,
            html=False,
        )
        self.assertContains(
            response,
            'name="flow_action" value="start"',
            count=2,
            html=False,
        )
        self.assertContains(response, "csrfmiddlewaretoken")
        self.assertNotContains(
            response,
            f'href="/orders/receiving/{order_id}/flow/">Взять в работу</a>',
            html=False,
        )
        self.assertNotContains(
            response,
            f'href="/orders/receiving/{order_id}/act/">Взять в работу</a>',
            html=False,
        )

    def test_send_receiving_to_warehouse_closes_manager_task_and_creates_storekeeper_task(self):
        agency = Agency.objects.create(agn_name="Клиент приемки todo service")
        OrderAuditEntry.objects.create(
            order_id="R-TODO-SVC-1",
            order_type="receiving",
            action="create",
            agency=agency,
            payload={"status": "draft", "status_label": "Черновик"},
        )
        manager_task = Task.objects.create(
            title="Проверьте заявку на приемку товара №R-TODO-SVC-1",
            route="/orders/receiving/R-TODO-SVC-1/",
            assigned_to=self.manager,
            created_by=self.manager_user,
        )
        request = self.factory.post("/todo/detail/")
        request.user = self.manager_user

        result = send_receiving_to_warehouse(manager_task, request)

        self.assertTrue(result)
        manager_task.refresh_from_db()
        self.assertEqual(manager_task.status, "done")
        self.assertTrue(
            OrderAuditEntry.objects.filter(
                order_id="R-TODO-SVC-1",
                order_type="receiving",
                action="status",
                payload__status="warehouse",
            ).exists()
        )
        follow_up = Task.objects.exclude(pk=manager_task.pk).get(route="/orders/receiving/R-TODO-SVC-1/")
        self.assertEqual(follow_up.assigned_to, self.storekeeper)
        self.assertEqual(follow_up.created_by, self.manager_user)

    def test_build_trip_context_aggregates_trip_participants_and_totals(self):
        logistician = Employee.objects.create(
            full_name="Логистов Сергей",
            role="logistician",
            is_active=True,
        )
        agency = Agency.objects.create(agn_name="Клиент рейса todo service")
        order = ShippingOrder.objects.create(
            number="SO-000101",
            agency=agency,
            expected_boxes=8,
            destination_warehouse="Казань РФЦ",
        )
        trip = LogisticsTrip.objects.create(
            number="12_RS",
            status=LogisticsTrip.STATUS_LOADING,
            assigned_logistician=logistician,
        )
        LogisticsTripOrder.objects.create(
            trip=trip,
            shipping_order=order,
            loading_sequence=1,
            delivery_sequence=1,
        )
        OrderAuditEntry.objects.create(
            order_type="shipping",
            order_id=order.number,
            action="packing",
            payload={
                "act": "shipping_packing",
                "pallet_count": 2,
                "delivered_box_count": 5,
            },
        )
        task = Task.objects.create(
            title="Рейс на погрузку",
            route=f"/logistics/trips/{trip.pk}/",
            assigned_to=self.storekeeper,
            created_by=self.manager_user,
        )

        context = build_trip_context(task)

        self.assertIsNotNone(context)
        self.assertEqual(context["total_pallets"], 2)
        self.assertEqual(context["total_boxes"], 5)
        self.assertIn("Логист: Логистов Сергей", context["participants"])
        self.assertIn("Кладовщик: Кладовщиков Алексей", context["participants"])


class TodoTaskDetailTripLayoutTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.user = user_model.objects.create_user(username="todo_trip_storekeeper", password="pwd")
        self.storekeeper = Employee.objects.create(
            full_name="Кладовщиков Алексей",
            role="storekeeper",
            user=self.user,
            is_active=True,
        )
        self.logistician = Employee.objects.create(
            full_name="Логистов Сергей",
            role="logistician",
            is_active=True,
        )
        agency = Agency.objects.create(agn_name='Общество с ограниченной ответственностью "Кейзи"')
        self.order1 = ShippingOrder.objects.create(
            number="SO-000001",
            agency=agency,
            destination_warehouse="Санкт_Петербург_РФЦ",
            expected_boxes=9,
        )
        self.order2 = ShippingOrder.objects.create(
            number="SO-000002",
            agency=agency,
            destination_warehouse="Екатеринбург — ул. Испытателей, 14Г",
            expected_boxes=50,
        )
        self.trip = LogisticsTrip.objects.create(
            number="3_RS",
            status=LogisticsTrip.STATUS_LOADING,
            vehicle_name="Транспорт ФуллБокс",
            vehicle_number="Е777ЕК999",
            driver_name="Иванов Иван",
            driver_phone="+7 900 000-00-00",
            assigned_logistician=self.logistician,
        )
        LogisticsTripOrder.objects.create(
            trip=self.trip,
            shipping_order=self.order1,
            loading_sequence=1,
            delivery_sequence=1,
            comment="Первая точка",
        )
        LogisticsTripOrder.objects.create(
            trip=self.trip,
            shipping_order=self.order2,
            loading_sequence=2,
            delivery_sequence=2,
            comment="Вторая точка",
        )
        self.task = Task.objects.create(
            title="Рейс на погрузку",
            route=f"/logistics/trips/{self.trip.pk}/",
            assigned_to=self.storekeeper,
        )
        self.client.force_login(self.user)

    def test_trip_task_detail_uses_wide_table_layout_without_right_sidebar(self):
        response = self.client.get(reverse("todo:detail", args=[self.task.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Заявки, включенные в рейс")
        self.assertContains(response, "Маршрут")
        self.assertContains(response, "Склад назначения")
        self.assertContains(response, "Комментарий логиста")
        self.assertContains(response, "1_OTG")
        self.assertContains(response, "2_OTG")
        self.assertContains(response, "Первая точка")
        self.assertContains(response, "Вторая точка")
        self.assertNotContains(response, 'class="detail-chat"', html=False)

    def test_storekeeper_task_panel_uses_direct_trip_link_for_logistics_task(self):
        request = RequestFactory().get("/sklad/")
        request.user = self.user

        html = Template(
            "{% load todo_panel %}{% task_panel role='storekeeper' %}"
        ).render(RequestContext(request, {}))

        detail_url = reverse("todo:detail", args=[self.task.pk])
        self.assertIn(f"onclick=\"window.location.href='/logistics/trips/{self.trip.pk}/'\"", html)
        self.assertIn(f'<a href="/logistics/trips/{self.trip.pk}/">{self.task.display_title()}</a>', html)
        self.assertNotIn(f'href="{detail_url}"', html)


class TodoPanelFilterPersistenceTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        user_model = get_user_model()
        self.user = user_model.objects.create_user(
            username="manager_todo_filters",
            password="pwd",
        )
        self.manager = Employee.objects.create(
            user=self.user,
            full_name="Менеджеров Сергей",
            role="manager",
        )
        self.client_a = Agency.objects.create(agn_name="Клиент А")
        self.client_b = Agency.objects.create(agn_name="Клиент Б")
        self.order_a = ShippingOrder.objects.create(number="SO-000020", agency=self.client_a)
        self.order_b = ShippingOrder.objects.create(number="SO-000021", agency=self.client_b)
        Task.objects.create(
            title="Отгрузка А",
            route=f"/shipping/{self.order_a.pk}/",
            status="in_progress",
            assigned_to=self.manager,
        )
        Task.objects.create(
            title="Отгрузка Б",
            route=f"/shipping/{self.order_b.pk}/",
            status="in_progress",
            assigned_to=self.manager,
        )
        self.session_key = "todo_panel_filters:/team-manager/"

    def test_task_panel_restores_saved_filters_from_session(self):
        request = self.factory.get(
            "/team-manager/",
            {
                "todo_filters_applied": "1",
                "todo_filter_type": "shipping",
                "todo_filter_client": str(self.client_b.id),
                "todo_filter_query": "21_OTG",
            },
        )
        request.user = self.user
        request.session = {}

        initial_html = Template(
            "{% load todo_panel %}{% task_panel role='manager' %}"
        ).render(RequestContext(request, {}))

        self.assertIn("Клиент Б", initial_html)
        self.assertNotIn("Заявка на отгрузку №20_OTG", initial_html)
        self.assertEqual(
            request.session[self.session_key],
            {
                "selected_type": "shipping",
                "selected_client": str(self.client_b.id),
                "selected_query": "21_OTG",
            },
        )

        repeat_request = self.factory.get("/team-manager/")
        repeat_request.user = self.user
        repeat_request.session = request.session

        repeat_html = Template(
            "{% load todo_panel %}{% task_panel role='manager' %}"
        ).render(RequestContext(repeat_request, {}))

        self.assertIn("Клиент Б", repeat_html)
        self.assertNotIn("Заявка на отгрузку №20_OTG", repeat_html)
        self.assertIn('option value="%s" selected' % self.client_b.id, repeat_html)
        self.assertIn('name="todo_filter_type" value="shipping"', repeat_html)
        self.assertIn('name="todo_filter_query"', repeat_html)
        self.assertIn('value="21_OTG"', repeat_html)
        self.assertIn('class="task-filter-tab active"', repeat_html)

    def test_task_panel_reset_clears_saved_filters(self):
        session = {
            self.session_key: {
                "selected_type": "shipping",
                "selected_client": str(self.client_b.id),
                "selected_query": "21_OTG",
            }
        }
        request = self.factory.get("/team-manager/", {"todo_filters_reset": "1"})
        request.user = self.user
        request.session = session

        html = Template(
            "{% load todo_panel %}{% task_panel role='manager' %}"
        ).render(RequestContext(request, {}))

        self.assertNotIn(self.session_key, request.session)
        self.assertIn("Клиент А", html)
        self.assertIn("Клиент Б", html)

    def test_team_manager_hides_status_label_caption(self):
        request = self.factory.get("/team-manager/")
        request.user = self.user
        request.session = {}

        html = Template(
            "{% load todo_panel %}{% task_panel role='manager' %}"
        ).render(RequestContext(request, {}))

        self.assertNotIn("Статус заявки:", html)


class TaskAttentionTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.manager_user = user_model.objects.create_user(
            username="attention-manager",
            password="test-pass",
        )
        self.manager = Employee.objects.create(
            user=self.manager_user,
            full_name="Менеджер Уведомлений",
            role="manager",
        )
        self.observer_user = user_model.objects.create_user(
            username="attention-observer",
            password="test-pass",
        )
        self.observer = Employee.objects.create(
            user=self.observer_user,
            full_name="Наблюдатель Уведомлений",
            role="manager",
        )

    def test_new_task_notifies_each_recipient_independently(self):
        task = Task.objects.create(
            title="Новая заявка",
            route="/orders/",
            assigned_to=self.manager,
            observer=self.observer,
        )

        self.assertTrue(
            TaskAttention.objects.filter(
                task=task,
                employee=self.manager,
                viewed_at__isnull=True,
            ).exists()
        )
        self.assertTrue(
            TaskAttention.objects.filter(
                task=task,
                employee=self.observer,
                viewed_at__isnull=True,
            ).exists()
        )

        self.client.force_login(self.manager_user)
        response = self.client.get(reverse("todo:open", args=[task.pk]))

        self.assertRedirects(response, "/orders/", fetch_redirect_response=False)
        self.assertFalse(
            TaskAttention.objects.filter(
                task=task,
                employee=self.manager,
                viewed_at__isnull=True,
            ).exists()
        )
        self.assertTrue(
            TaskAttention.objects.filter(
                task=task,
                employee=self.observer,
                viewed_at__isnull=True,
            ).exists()
        )

    def test_reassignment_removes_old_alert_and_notifies_new_executor(self):
        task = Task.objects.create(
            title="Переназначаемая заявка",
            assigned_to=self.manager,
        )
        TaskAttention.objects.filter(task=task, employee=self.manager).update(
            viewed_at=timezone.now()
        )

        task.assigned_to = self.observer
        task.save(update_fields=["assigned_to", "updated_at"])

        self.assertFalse(
            TaskAttention.objects.filter(task=task, employee=self.manager).exists()
        )
        self.assertTrue(
            TaskAttention.objects.filter(
                task=task,
                employee=self.observer,
                viewed_at__isnull=True,
            ).exists()
        )

    def test_task_panel_marks_only_unread_task_as_attention(self):
        task = Task.objects.create(
            title="Заявка требует просмотра",
            route="/orders/",
            assigned_to=self.manager,
            due_date=timezone.now(),
        )
        request = RequestFactory().get("/cabinet/manager/")
        request.user = self.manager_user
        request.session = {}

        html = Template(
            "{% load todo_panel %}{% task_panel role='manager' %}"
        ).render(RequestContext(request, {}))

        self.assertIn("task-card clickable", html)
        self.assertIn(" attention\"", html)
        self.assertIn("Новая", html)
        self.assertIn(f'data-task-view-url="/todo/{task.pk}/open/?mark=1"', html)
        self.assertIn(
            f'onclick="window.location.href=\'/todo/{task.pk}/\'"',
            html,
        )

    def test_mark_only_endpoint_clears_alert_without_redirect(self):
        task = Task.objects.create(
            title="Просмотр без перехода",
            route="/orders/",
            assigned_to=self.manager,
        )
        self.client.force_login(self.manager_user)

        response = self.client.get(
            reverse("todo:open", args=[task.pk]),
            {"mark": "1"},
        )

        self.assertEqual(response.status_code, 204)
        self.assertFalse(
            TaskAttention.objects.filter(
                task=task,
                employee=self.manager,
                viewed_at__isnull=True,
            ).exists()
        )
