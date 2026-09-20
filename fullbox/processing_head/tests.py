from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from audit.models import OrderAuditEntry
from employees.models import Employee
from marking.models import MarkingCode
from sklad.models import WarehouseStockSnapshot
from sku.models import Agency
from todo.models import Task

from .views import _team_load


User = get_user_model()


def _login_processing_head(client):
    user = User.objects.create_user(username="processing_head_ui", password="pwd")
    employee = Employee.objects.create(
        full_name="Ларионова Марина",
        user=user,
        role="processing_head",
        is_active=True,
    )
    client.force_login(user)
    session = client.session
    session["employee_id"] = employee.id
    session["employee_role"] = "processing_head"
    session["employee_name"] = employee.full_name
    session.save()
    return employee


@override_settings(ALLOWED_HOSTS=["*"])
class ProcessingHeadDashboardTests(TestCase):
    def test_dashboard_renders_processing_head_workspace(self):
        client = Client()
        processing_head = _login_processing_head(client)
        worker = Employee.objects.create(
            full_name="Петров Сергей",
            role="processing_worker",
            is_active=True,
        )
        Task.objects.create(
            title="Заявка на обработку №16",
            route="/orders/processing/16/",
            assigned_to=processing_head,
            observer=worker,
            status="in_progress",
            due_date=timezone.now() + timezone.timedelta(hours=4),
        )
        agency = Agency.objects.create(agn_name="ООО Тестовая компания")
        OrderAuditEntry.objects.create(
            order_id="16",
            order_type="processing",
            action="status",
            agency=agency,
            payload={"status": "processing_in_work"},
        )

        response = client.get("/processing-head/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Кабинет руководителя обработки")
        self.assertContains(response, "Активные заявки обработки")
        self.assertContains(response, "Загрузка персонала")
        self.assertContains(response, "Заявка на обработку №16_OBR")
        self.assertContains(response, "ООО Тестовая компания")
        self.assertContains(response, 'href="/processing-head/requests/?tab=overdue&amp;period=all"')
        self.assertContains(response, 'href="/processing-head/requests/?tab=all&amp;period=all"')
        self.assertContains(response, 'href="/processing-head/?section=planning&amp;tab=planning&amp;period=all"')
        self.assertContains(response, 'href="/processing-head/?section=stock&amp;period=all"')
        self.assertContains(response, 'href="/processing-head/?section=work&amp;tab=in_work&amp;period=all"')
        self.assertContains(response, 'href="/processing-head/?section=personnel&amp;period=all"')
        self.assertContains(response, 'href="/processing-head/reports"')
        self.assertContains(response, 'href="/orders/processing/16/work/"')
        self.assertNotContains(response, 'href="/orders/processing/16/"')
        self.assertNotContains(response, 'href="/employees/"')
        self.assertNotContains(response, "/todo/?role=processing_head")
        self.assertContains(response, 'class="logout-link" href="/logout/"')
        self.assertContains(response, "Выход")
        self.assertNotContains(response, "Динамика обработки")
        self.assertContains(response, "Требует внимания")
        self.assertContains(response, 'id="processing-attention-toggle"')
        self.assertContains(response, "fullbox.processing-head.attention-collapsed")
        self.assertContains(response, "Активные заявки обработки")
        self.assertContains(response, "Товары в обработке")
        self.assertContains(response, "КИЗы / ЧЗ")
        self.assertContains(response, 'href="/processing-head/requests/?executor=__none__&amp;period=all"')
        self.assertContains(response, 'href="/processing-head/requests/?tab=review&amp;period=all"')

    def test_dashboard_scrolls_complete_active_queue_and_exposes_attention_tasks(self):
        client = Client()
        processing_head = _login_processing_head(client)
        for index in range(10):
            Task.objects.create(
                title=f"Заявка на обработку №{500 + index}",
                route=f"/orders/processing/{500 + index}/",
                assigned_to=None if index == 0 else processing_head,
                observer=processing_head if index == 0 else None,
                status="backlog" if index == 1 else "in_progress",
                due_date=timezone.now() - timezone.timedelta(hours=1) if index == 1 else timezone.now() + timezone.timedelta(hours=2),
            )

        response = client.get("/processing-head/?period=all")

        self.assertEqual(len(response.context["priority_tasks"]), 10)
        self.assertContains(response, '<div class="task-list is-scrollable">')
        self.assertLessEqual(len(response.context["attention_tasks"]), 5)
        self.assertEqual(response.context["rightbar_counts"]["unassigned"], 1)
        self.assertEqual(response.context["rightbar_counts"]["critical"], 1)

    def test_dashboard_defaults_to_all_active_processing_requests(self):
        client = Client()
        processing_head = _login_processing_head(client)
        fresh_task = Task.objects.create(
            title="Свежая заявка на обработку",
            route="/orders/processing/17/",
            assigned_to=processing_head,
            status="in_progress",
            due_date=timezone.now() + timezone.timedelta(hours=4),
        )
        old_task = Task.objects.create(
            title="Старая заявка на обработку",
            route="/orders/processing/18/",
            assigned_to=processing_head,
            status="in_progress",
            due_date=timezone.now() + timezone.timedelta(hours=4),
        )
        Task.objects.filter(pk=old_task.pk).update(updated_at=timezone.now() - timezone.timedelta(days=8))
        Task.objects.filter(pk=fresh_task.pk).update(updated_at=timezone.now())

        response = client.get("/processing-head/")

        priority_titles = [row["title"] for row in response.context["priority_tasks"]]
        self.assertEqual(response.context["period_filters"][-1]["selected"], True)
        self.assertIn("Заявка на обработку №17_OBR", priority_titles)
        self.assertIn("Заявка на обработку №18_OBR", priority_titles)
        self.assertContains(response, "Активные заявки обработки")

        response = client.get("/processing-head/?period=today")

        priority_titles = [row["title"] for row in response.context["priority_tasks"]]
        self.assertIn("Заявка на обработку №17_OBR", priority_titles)
        self.assertNotIn("Заявка на обработку №18_OBR", priority_titles)
        self.assertContains(response, "Активные заявки обработки")

    def test_dashboard_queue_shows_new_in_work_and_overdue_processing_requests(self):
        client = Client()
        processing_head = _login_processing_head(client)
        new_task = Task.objects.create(
            title="Новая заявка на обработку",
            route="/orders/processing/31/",
            observer=processing_head,
            status="backlog",
            due_date=timezone.now() + timezone.timedelta(hours=4),
        )
        in_work_task = Task.objects.create(
            title="Заявка в работе",
            route="/orders/processing/32/",
            assigned_to=processing_head,
            status="in_progress",
            due_date=timezone.now() + timezone.timedelta(hours=4),
        )
        overdue_task = Task.objects.create(
            title="Просроченная заявка",
            route="/orders/processing/33/",
            assigned_to=processing_head,
            status="in_progress",
            due_date=timezone.now() - timezone.timedelta(hours=4),
        )
        Task.objects.create(
            title="Закрытая заявка",
            route="/orders/processing/34/",
            assigned_to=processing_head,
            status="done",
        )
        Task.objects.create(
            title="Заявка на проверке",
            route="/orders/processing/35/",
            assigned_to=processing_head,
            status="in_progress",
            due_date=timezone.now() + timezone.timedelta(hours=4),
        )
        Task.objects.create(
            title="Заблокированная заявка",
            route="/orders/processing/36/",
            assigned_to=processing_head,
            status="blocked",
            due_date=timezone.now() + timezone.timedelta(hours=4),
        )

        response = client.get("/processing-head/")

        queue = {row["id"]: row["queue_bucket"] for row in response.context["priority_tasks"]}
        self.assertEqual(queue[new_task.pk], "new")
        self.assertEqual(queue[in_work_task.pk], "in_work")
        self.assertEqual(queue[overdue_task.pk], "overdue")
        self.assertNotContains(response, "Закрытая заявка")
        self.assertNotContains(response, "Заявка на проверке")
        self.assertNotContains(response, "Заблокированная заявка")
        self.assertContains(response, "Завершённые заявки здесь не показываются")

    def test_priority_tasks_show_only_processing_active_work(self):
        client = Client()
        processing_head = _login_processing_head(client)
        Task.objects.create(
            title="Заявка на обработку №20",
            route="/orders/processing/20/",
            assigned_to=processing_head,
            status="backlog",
            due_date=timezone.now() - timezone.timedelta(hours=4),
        )
        Task.objects.create(
            title="Заявка на приемку №133",
            route="/orders/receiving/133/",
            assigned_to=processing_head,
            status="backlog",
            due_date=timezone.now() - timezone.timedelta(hours=4),
        )
        Task.objects.create(
            title="Готовая заявка на обработку №21",
            route="/orders/processing/21/",
            assigned_to=processing_head,
            status="done",
            due_date=timezone.now() - timezone.timedelta(hours=4),
        )
        manager = Employee.objects.create(
            full_name="Дёмина Дарья",
            role="manager",
            is_active=True,
        )
        Task.objects.create(
            title="Подтвердите заявку на обработку №19",
            route="/orders/processing/19/",
            assigned_to=manager,
            status="backlog",
            due_date=timezone.now() - timezone.timedelta(hours=4),
        )

        response = client.get("/processing-head/?tab=overdue&period=all")

        priority_tasks = response.context["priority_tasks"]
        self.assertEqual(len(priority_tasks), 1)
        self.assertEqual(priority_tasks[0]["document_type"], "processing")
        self.assertEqual(priority_tasks[0]["bucket"], "overdue")
        self.assertEqual(priority_tasks[0]["title"], "Заявка на обработку №20_OBR")
        self.assertNotContains(response, "Подтвердите заявку на обработку №19")

    def test_receiving_tasks_do_not_enter_processing_head_dashboard_scope(self):
        client = Client()
        processing_head = _login_processing_head(client)
        Task.objects.create(
            title="Заявка на приемку №140",
            route="/orders/receiving/140/",
            assigned_to=processing_head,
            status="backlog",
            due_date=timezone.now() - timezone.timedelta(hours=4),
        )

        response = client.get("/processing-head/?period=all")

        self.assertEqual(response.context["total_count"], 0)
        self.assertEqual(response.context["priority_tasks"], [])
        self.assertNotContains(response, "Заявка на приемку №140")

    def test_requests_section_shows_all_filtered_processing_tasks(self):
        client = Client()
        processing_head = _login_processing_head(client)
        manager = Employee.objects.create(
            full_name="Дёмина Дарья",
            role="manager",
            is_active=True,
        )
        for index in range(8):
            Task.objects.create(
                title=f"Заявка на обработку №{200 + index}",
                route=f"/orders/processing/{200 + index}/",
                assigned_to=processing_head,
                status="in_progress",
                due_date=timezone.now() + timezone.timedelta(hours=4),
            )
        Task.objects.create(
            title="Готовая заявка на обработку №299",
            route="/orders/processing/299/",
            assigned_to=processing_head,
            status="done",
            due_date=timezone.now() + timezone.timedelta(hours=4),
        )
        Task.objects.create(
            title="Заявка на приемку №301",
            route="/orders/receiving/301/",
            assigned_to=manager,
            status="in_progress",
            due_date=timezone.now() + timezone.timedelta(hours=6),
        )
        Task.objects.create(
            title="Подписать акт приемки по заявке №301",
            route="/orders/receiving/301/",
            assigned_to=processing_head,
            status="done",
            due_date=timezone.now() + timezone.timedelta(hours=5),
        )
        Task.objects.create(
            title="Повторная задача по заявке №207",
            route="/orders/processing/207/",
            assigned_to=manager,
            status="done",
            due_date=timezone.now() + timezone.timedelta(hours=3),
        )
        Task.objects.create(
            title="Другая заявка №OTHER-11",
            route="/team-manager/other-requests/OTHER-11/",
            assigned_to=manager,
            status="backlog",
            due_date=timezone.now() + timezone.timedelta(hours=7),
        )
        receiving_agency = Agency.objects.create(agn_name="ООО Приемка")
        OrderAuditEntry.objects.create(
            order_id="301",
            order_type="receiving",
            action="status",
            agency=receiving_agency,
            payload={"status": "storekeeper_accepted"},
        )

        response = client.get("/processing-head/requests/?tab=all&period=all")

        self.assertEqual(response.context["active_section"], "requests")
        self.assertTrue(response.context["is_requests_page"])
        self.assertContains(response, "Все заявки")
        self.assertContains(response, "Журнал заявок")
        self.assertContains(response, "Приемки, обработка и другие заявки")
        self.assertContains(response, 'class="nav-link active" href="/processing-head/requests/?tab=all&amp;period=all"')
        self.assertNotContains(response, 'name="section" value="requests"')
        self.assertContains(response, '<button class="button primary" type="submit">Применить</button>', html=True)
        self.assertContains(response, 'href="/processing-head/requests/?tab=all&amp;period=all">Сбросить</a>')
        self.assertEqual(len(response.context["priority_tasks"]), 11)
        self.assertEqual(
            len([row for row in response.context["priority_tasks"] if row["document_key"] == "receiving:301"]),
            1,
        )
        self.assertEqual(
            len([row for row in response.context["priority_tasks"] if row["document_key"] == "processing:207"]),
            1,
        )
        self.assertContains(response, "Заявка на обработку №207_OBR")
        self.assertContains(response, "Заявка на обработку №299_OBR")
        self.assertContains(response, "№301_PR")
        self.assertContains(response, "Заявки на приемку")
        self.assertContains(response, "ООО Приемка")
        self.assertContains(response, "Другая заявка №OTHER-11")
        self.assertContains(response, "Другие заявки")

    def test_stock_section_shows_processing_stock_only_inside_cabinet(self):
        client = Client()
        _login_processing_head(client)
        agency = Agency.objects.create(agn_name="ООО Обработка")
        WarehouseStockSnapshot.objects.create(
            agency=agency,
            source_context_type="processing",
            source_context_id="17",
            sku_code="SKU-OBR",
            name="Товар обработки",
            size="42",
            barcode="BAR-OBR",
            qty=12,
            available_qty=7,
            processing_reserved_qty=5,
            container_code="BOX-OBR",
            zone_code="OBR",
            warehouse_state_code="processing",
        )

        response = client.get("/processing-head/?section=stock&period=all")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["active_section"], "stock")
        self.assertContains(response, "Остатки участка обработки")
        self.assertContains(response, "SKU-OBR")
        self.assertContains(response, "Товар обработки")
        self.assertContains(response, "BOX-OBR")

    def test_personnel_section_is_limited_to_processing_staff(self):
        client = Client()
        _login_processing_head(client)
        Employee.objects.create(full_name="Обработчик Ольга", role="processing_worker", is_active=True)
        Employee.objects.create(full_name="Менеджер Мария", role="manager", is_active=True)

        response = client.get("/processing-head/?section=personnel&period=all")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["active_section"], "personnel")
        self.assertContains(response, "Сотрудники обработки")
        self.assertContains(response, "Обработчик Ольга")
        self.assertNotContains(response, "Менеджер Мария")

    def test_reports_section_shows_processing_goods_and_marking_reports(self):
        client = Client()
        processing_head = _login_processing_head(client)
        agency = Agency.objects.create(agn_name="ООО Отчеты")
        Task.objects.create(
            title="Заявка на обработку №301",
            route="/orders/processing/301/",
            assigned_to=processing_head,
            status="in_progress",
            due_date=timezone.now() + timezone.timedelta(hours=2),
        )
        WarehouseStockSnapshot.objects.create(
            agency=agency,
            source_context_type="processing",
            source_context_id="301",
            sku_code="SKU-REPORT",
            name="Товар для отчета",
            qty=8,
            available_qty=5,
            processing_reserved_qty=3,
            zone_code="OBR",
            warehouse_state_code="processing",
        )
        MarkingCode.objects.create(
            agency=agency,
            order_type="processing",
            order_id="301",
            sku_code="SKU-REPORT",
            size="M",
            barcode="BAR-REPORT",
            code="REPORT-CZ-UNIQUE-301",
        )

        response = client.get("/processing-head/?section=reports&period=all")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["active_section"], "reports")
        self.assertContains(response, "Отчет по обработке")
        self.assertContains(response, "Отчет по товарам")
        self.assertContains(response, "Отчет по ЧЗ")
        self.assertContains(response, "SKU-REPORT")
        self.assertContains(response, "Заявка на обработку №301_OBR")
        self.assertNotContains(response, "REPORT-CZ-UNIQUE-301")

    def test_settings_section_opens_label_print_settings(self):
        client = Client()
        _login_processing_head(client)

        response = client.get("/processing-head/?section=settings")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["active_section"], "settings")
        self.assertContains(response, "Настройки печати этикеток")
        self.assertContains(response, "/labels/settings/?return=/processing-head/%3Fsection%3Dsettings")
        self.assertNotContains(response, "Приоритетные задачи")


@override_settings(ALLOWED_HOSTS=["*"])
class ProcessingHeadReportsTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.head = _login_processing_head(self.client)
        self.agency = Agency.objects.create(agn_name="ООО Отчёты")
        self.task = Task.objects.create(
            title="Заявка на обработку №404",
            route="/orders/processing/404/",
            assigned_to=self.head,
            status="in_progress",
            due_date=timezone.now() + timezone.timedelta(hours=2),
        )
        MarkingCode.objects.create(
            agency=self.agency,
            order_type="processing",
            order_id="404",
            sku_code="REPORT-KIZ",
            code="REPORT-KIZ-CODE-404",
        )
        WarehouseStockSnapshot.objects.create(
            agency=self.agency,
            source_context_type="processing",
            source_context_id="404",
            sku_code="REPORT-SKU",
            name="Товар отчёта",
            qty=5,
            available_qty=4,
            processing_reserved_qty=1,
            zone_code="OBR",
            warehouse_state_code="processing",
        )

    def test_reports_catalog_and_all_detail_pages_are_available(self):
        response = self.client.get("/processing-head/reports")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Выберите отчёт")
        for slug in ("kiz", "product-movement", "processing", "inventory", "employees", "deviations"):
            self.assertContains(response, f"/processing-head/reports/{slug}")
            detail = self.client.get(f"/processing-head/reports/{slug}?period=month")
            self.assertEqual(detail.status_code, 200)
            self.assertContains(detail, "Экспорт в Excel")
            self.assertContains(detail, "Применить")

    def test_reports_apply_url_filters_and_export_xlsx(self):
        response = self.client.get("/processing-head/reports/kiz?period=month&q=REPORT-KIZ")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "REPORT-KIZ")
        self.assertNotContains(response, "REPORT-KIZ-CODE-404")

        export = self.client.get("/processing-head/reports/kiz?period=month&q=REPORT-KIZ&export=xlsx")

        self.assertEqual(export.status_code, 200)
        self.assertEqual(export["Content-Type"], "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        self.assertTrue(export.get("Content-Disposition"))

    def test_team_load_uses_single_aggregate_query(self):
        Employee.objects.update(is_active=False)
        workers = [
            Employee.objects.create(full_name=f"Обработчик {index}", role="processing_worker", is_active=True)
            for index in range(4)
        ]
        for worker in workers:
            Task.objects.create(
                title=f"Задача {worker.pk}",
                route="/orders/processing/19/",
                assigned_to=worker,
                status="in_progress",
                due_date=timezone.now() + timezone.timedelta(hours=4),
            )

        with self.assertNumQueries(1):
            rows, average = _team_load()

        self.assertEqual(len(rows), 4)
        self.assertGreater(average, 0)
