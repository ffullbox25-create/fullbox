from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings

from audit.models import OrderAuditEntry
from employees.models import Employee
from sku.models import Agency
from todo.models import Task


User = get_user_model()


@override_settings(ALLOWED_HOSTS=["*"])
class ProcessingWorkerDashboardTests(TestCase):
    def _login_worker(self, username: str):
        user = User.objects.create_user(username=username, password="pwd")
        worker = Employee.objects.create(
            full_name="Красивская Ольга",
            user=user,
            role="processing_worker",
            is_active=True,
        )
        client = Client()
        client.force_login(user)
        session = client.session
        session["employee_id"] = worker.id
        session["employee_role"] = "processing_worker"
        session["employee_name"] = worker.full_name
        session.save()
        return client, worker

    def test_dashboard_repairs_mojibake_packaging_task_title(self):
        user = User.objects.create_user(username="processing_worker_ui", password="pwd")
        worker = Employee.objects.create(
            full_name="Красивская Ольга",
            user=user,
            role="processing_worker",
            is_active=True,
        )
        agency = Agency.objects.create(agn_name="ООО Тест обработки")
        OrderAuditEntry.objects.create(
            order_id="POINT11-WORKER",
            order_type="processing",
            action="status",
            agency=agency,
            payload={
                "status": "processing_in_work",
                "processing_stage": "unboxing_opened",
                "processing_stage_label": "открыта раскоробовка",
                "cards": [],
            },
        )
        Task.objects.create(
            title="Р—Р°РґР°С‡Р° РЅР° СЂР°СЃРєРѕСЂРѕР±РѕРІРєСѓ С‚РѕРІР°СЂР° РїРѕ Р·Р°СЏРІРєРµ в„–POINT11-WORKER",
            route="/orders/processing/POINT11-WORKER/flow/",
            assigned_to=worker,
            status="backlog",
        )
        client = Client()
        client.force_login(user)
        session = client.session
        session["employee_id"] = worker.id
        session["employee_role"] = "processing_worker"
        session["employee_name"] = worker.full_name
        session.save()

        response = client.get("/processing-worker/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            "Формирование коробов по заявке POINT11-WORKER",
        )
        self.assertNotContains(response, "Р—Р°Рґ")

    def test_blocked_packaging_assignment_is_hidden_while_processing_is_pending(self):
        client, worker = self._login_worker("processing_worker_pending_boxes")
        agency = Agency.objects.create(agn_name="ООО Клиент коробов")
        order_id = "BOXES-PENDING"
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="processing",
            action="status",
            agency=agency,
            payload={
                "status": "processing_in_work",
                "cards": [
                    {"id": "c1", "article": "SKU-BOX", "rows": [{"size": "42", "qty": "30"}]},
                ],
                "processed_cards": [],
                "processing_results": [],
            },
        )
        route = f"/orders/processing/{order_id}/flow/"
        Task.objects.create(
            title=f"Формирование коробов по заявке №{order_id}",
            route=route,
            assigned_to=worker,
            status="blocked",
        )

        response = client.get("/processing-worker/")

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "ООО Клиент коробов")
        self.assertNotContains(response, "Ожидает готовой товарной карты")
        self.assertNotContains(response, f'href="{route}"')

    def test_packaging_assignment_opens_when_processing_is_ready(self):
        client, worker = self._login_worker("processing_worker_ready_boxes")
        agency = Agency.objects.create(agn_name="ООО Готовые короба")
        order_id = "BOXES-READY"
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="processing",
            action="status",
            agency=agency,
            payload={
                "status": "processing_in_work",
                "cards": [
                    {"id": "c1", "article": "SKU-BOX", "rows": [{"size": "42", "qty": "30"}]},
                    {"id": "c2", "article": "SKU-WAIT", "rows": [{"size": "43", "qty": "20"}]},
                ],
                "processed_cards": ["c1", "c2"],
                "processing_results": [
                    {
                        "card_id": "c1",
                        "article": "SKU-BOX",
                        "size": "42",
                        "destination": "-",
                        "processed": "30",
                    },
                    {
                        "card_id": "c2",
                        "article": "SKU-WAIT",
                        "size": "43",
                        "destination": "-",
                        "processed": "20",
                    },
                ],
            },
        )
        route = f"/orders/processing/{order_id}/flow/"
        Task.objects.create(
            title=f"Формирование коробов по заявке №{order_id}",
            route=route,
            assigned_to=worker,
            status="backlog",
        )

        response = client.get("/processing-worker/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Товарные позиции: 2 из 2")
        self.assertContains(response, "Обработано: 50 шт.")
        self.assertContains(response, "Можно формировать короба")
        self.assertContains(response, f'href="{route}"')

    def test_completed_order_assignment_replaces_technical_card_in_done_queue(self):
        client, worker = self._login_worker("processing_worker_done_assignment")
        agency = Agency.objects.create(agn_name="ООО Завершённое формирование")
        order_id = "BOXES-DONE"
        card_payload = {
            "status": "processing_in_work",
            "processing_stage": "quality_control",
            "cards": [
                {
                    "id": "c1",
                    "article": "TECH-CARD-MUST-NOT-BE-SHOWN",
                    "rows": [{"barcode": "460000000001", "qty": "30"}],
                },
            ],
            "processed_cards": ["c1"],
            "processing_results": [
                {
                    "card_id": "c1",
                    "article": "SKU-READY",
                    "destination": "-",
                    "processed": "30",
                },
            ],
        }
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="processing",
            action="status",
            agency=agency,
            payload=card_payload,
        )
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="processing",
            action="update",
            agency=agency,
            payload={
                **card_payload,
                "act": "placement",
                "act_boxes": [{"code": "BOX-1"}],
                "flow_closed": True,
            },
        )
        Task.objects.create(
            title="Техническая карта",
            route=f"/orders/processing/{order_id}/card/c1/",
            assigned_to=worker,
            status="done",
        )
        flow_route = f"/orders/processing/{order_id}/flow/"
        Task.objects.create(
            title=f"Формирование коробов по заявке №{order_id}",
            route=flow_route,
            assigned_to=worker,
            status="done",
        )

        response = client.get("/processing-worker/?status=done")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["queue_counts"], {"active": 0, "done": 1, "all": 1})
        self.assertEqual(response.context["card_queue"], [])
        self.assertEqual(len(response.context["packaging_tasks"]), 1)
        self.assertTrue(response.context["packaging_tasks"][0]["is_done"])
        self.assertEqual(
            response.context["packaging_tasks"][0]["operational_subzone"]["code"],
            "OBR-OUT",
        )
        self.assertContains(response, f"Формирование коробов по заявке {order_id}")
        self.assertContains(response, "Открыть результат")
        self.assertNotContains(response, "TECH-CARD-MUST-NOT-BE-SHOWN")

    def test_card_tasks_are_grouped_into_one_leader_assignment(self):
        client, worker = self._login_worker("processing_worker_grouped_assignment")
        helper_user = User.objects.create_user(username="processing_worker_helper", password="pwd")
        helper = Employee.objects.create(
            full_name="Григорян Шушик",
            user=helper_user,
            role="processing_worker",
            is_active=True,
        )
        agency = Agency.objects.create(agn_name="ИП Татьянин")
        order_id = "28"
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="processing",
            action="status",
            agency=agency,
            payload={
                "status": "processing_in_work",
                "processing_stage": "processing_in_work",
                "cards": [
                    {
                        "id": "c1",
                        "article": "нож020",
                        "product_name": "Нож складной",
                        "rows": [{"barcode": "2054099382668", "qty": "120"}],
                    },
                    {
                        "id": "c2",
                        "article": "нож023",
                        "product_name": "Нож туристический",
                        "rows": [{"barcode": "20540994973106", "qty": "360"}],
                    },
                ],
                "processed_cards": [],
                "processing_results": [],
            },
        )
        for card_id in ("c1", "c2"):
            route = f"/orders/processing/{order_id}/card/{card_id}/"
            Task.objects.create(
                title=f"Техническая карта {card_id}",
                route=route,
                assigned_to=worker,
                status="backlog",
            )
            Task.objects.create(
                title=f"Техническая карта {card_id}",
                route=route,
                assigned_to=helper,
                status="backlog",
            )
        Task.objects.create(
            title=f"Формирование коробов по заявке №{order_id}",
            route=f"/orders/processing/{order_id}/flow/",
            assigned_to=worker,
            status="blocked",
        )

        response = client.get("/processing-worker/?status=active")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["queue_counts"], {"active": 1, "done": 0, "all": 1})
        self.assertEqual(response.context["card_queue"], [])
        self.assertEqual(response.context["packaging_tasks"], [])
        self.assertEqual(len(response.context["order_assignments"]), 1)
        assignment = response.context["order_assignments"][0]
        self.assertEqual(assignment["cards_total"], 2)
        self.assertEqual(assignment["declared_qty"], 480)
        self.assertTrue(assignment["pending_packaging"])
        self.assertEqual(
            assignment["assigned_workers"],
            ["Григорян Шушик", "Красивская Ольга"],
        )
        self.assertTrue(assignment["start_url"].startswith("/orders/processing/28/card/"))
        self.assertTrue(assignment["start_url"].endswith("?return=%2Fprocessing-worker%2F"))
        self.assertContains(response, "Формирование товара по заявке 28_OBR")
        self.assertContains(response, "Начать обработку", count=1)
        self.assertNotContains(response, "Открыть техкарту")
        self.assertNotContains(response, "нож020")

        detail_response = client.get("/processing-worker/?order=28")

        self.assertEqual(detail_response.status_code, 200)
        self.assertIsNotNone(detail_response.context["selected_assignment"])
        self.assertContains(detail_response, "Задание руководителя · 28_OBR")
        self.assertContains(detail_response, "нож020")
        self.assertContains(detail_response, "нож023")
        self.assertContains(detail_response, "Открыть техкарту", count=2)
        self.assertContains(
            detail_response,
            "Следующий этап «Формирование коробов» уже назначен",
        )

        first_start_url = assignment["start_url"]
        first_card_route = first_start_url.split("?", 1)[0]
        Task.objects.filter(route=first_card_route, assigned_to=worker).update(status="done")
        next_response = client.get("/processing-worker/?status=active")
        next_start_url = next_response.context["order_assignments"][0]["start_url"]
        self.assertNotEqual(next_start_url, first_start_url)
        self.assertTrue(next_start_url.startswith("/orders/processing/28/card/"))
        self.assertTrue(next_start_url.endswith("?return=%2Fprocessing-worker%2F"))
