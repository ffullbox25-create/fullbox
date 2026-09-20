import json
from io import StringIO
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.http import HttpResponse, JsonResponse
from django.test import RequestFactory, TestCase
from django.urls import reverse

from employees.models import Employee

from .models import AgentAction, Incident, IncidentMessage
from .middleware import EmployeeAiSupportAnnouncementMiddleware
from .diagnostics import redact_diagnostic_text
from .llm import DiagnosisResult, OpenAIResponsesDiagnosticClient
from .policy import POLICY_APPROVAL, POLICY_AUTOMATIC, POLICY_PROGRAMMER
from .worker import process_next_incident


class IncidentCommunicationTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.reporter = user_model.objects.create_user(username="worker", password="secret")
        Employee.objects.create(full_name="Бухгалтер", user=self.reporter, role="accountant")
        self.other = user_model.objects.create_user(username="other", password="secret")
        Employee.objects.create(full_name="Кадровик", user=self.other, role="hr")
        self.reviewer = user_model.objects.create_user(username="manager", password="secret")
        Employee.objects.create(full_name="Руководитель", user=self.reviewer, role="head_manager")
        self.non_employee = user_model.objects.create_user(username="client", password="secret")
        self.excluded_employee = user_model.objects.create_user(username="packer", password="secret")
        Employee.objects.create(full_name="Упаковщик", user=self.excluded_employee, role="packer")
        self.excluded_picker = user_model.objects.create_user(username="picker", password="secret")
        Employee.objects.create(full_name="Подборщик", user=self.excluded_picker, role="picker")

    def _create_incident(self, user=None, **overrides):
        payload = {
            "zone": "Приёмка",
            "object_type": "Заявка",
            "object_id": "OBR-135",
            "description": "При завершении появилась ошибка 500.",
            "expected_result": "Заявка должна завершиться.",
            "reproducible": "unknown",
            "severity": "high",
            "source_url": "https://evil.example/orders/135/?tab=items#secret",
            "page_title": "Приёмка OBR-135",
            "context_json": '{"screen":{"width":1920,"height":1080},"token":"must-not-save"}',
        }
        payload.update(overrides)
        self.client.force_login(user or self.reporter)
        return self.client.post(reverse("ai_support:create"), payload)

    def test_create_incident_adds_safe_context_and_agent_acknowledgement(self):
        response = self._create_incident()

        incident = Incident.objects.get()
        self.assertRedirects(response, reverse("ai_support:detail", args=(incident.pk,)))
        self.assertEqual(incident.reporter, self.reporter)
        self.assertEqual(incident.reporter_role, "accountant")
        self.assertEqual(incident.source_url, "/orders/135/?tab=items")
        self.assertNotIn("token", incident.context)
        self.assertEqual(incident.messages.count(), 2)
        self.assertEqual(incident.messages.first().sender_type, IncidentMessage.SENDER_EMPLOYEE)
        agent_message = incident.messages.last()
        self.assertEqual(agent_message.sender_type, IncidentMessage.SENDER_AGENT)
        self.assertIn("остатки", agent_message.body)
        self.assertIn("движения", agent_message.body)
        action = incident.agent_actions.get()
        self.assertEqual(action.action_type, "inspect_request")
        self.assertEqual(action.policy_level, POLICY_AUTOMATIC)

    def test_employee_only_sees_own_incidents(self):
        self._create_incident()
        other_incident = Incident.objects.create(
            reporter=self.other,
            reporter_role="hr",
            description="Чужая ошибка",
        )
        self.client.force_login(self.reporter)

        response = self.client.get(reverse("ai_support:list"))

        self.assertContains(response, "OBR-135")
        self.assertNotContains(response, other_incident.number)

    def test_authenticated_non_employee_cannot_use_internal_support(self):
        self.client.force_login(self.non_employee)

        response = self.client.get(reverse("ai_support:list"))

        self.assertEqual(response.status_code, 403)

    def test_employee_outside_target_roles_cannot_use_internal_support(self):
        self.client.force_login(self.excluded_employee)

        response = self.client.get(reverse("ai_support:list"))

        self.assertEqual(response.status_code, 403)

    def test_picker_cannot_use_internal_support(self):
        self.client.force_login(self.excluded_picker)

        response = self.client.get(reverse("ai_support:list"))

        self.assertEqual(response.status_code, 403)

    def test_reviewer_sees_all_incidents(self):
        self._create_incident()
        other_incident = Incident.objects.create(reporter=self.other, description="Чужая ошибка")
        self.client.force_login(self.reviewer)

        response = self.client.get(reverse("ai_support:list"))

        self.assertContains(response, "OBR-135")
        self.assertContains(response, other_incident.number)

    def test_navigation_returns_manager_to_real_cabinet(self):
        self.client.force_login(self.reviewer)

        response = self.client.get(reverse("ai_support:list"))

        self.assertContains(response, 'href="/head-manager/"')

    def test_employee_message_cannot_impersonate_agent(self):
        self._create_incident()
        incident = Incident.objects.get()
        self.client.force_login(self.reporter)

        self.client.post(
            reverse("ai_support:message", args=(incident.pk,)),
            {"body": "Дополнительная информация", "sender_type": "agent"},
        )

        message = incident.messages.order_by("-id").first()
        self.assertEqual(message.sender_type, IncidentMessage.SENDER_EMPLOYEE)
        self.assertEqual(message.author, self.reporter)

    def test_employee_cannot_change_incident_status(self):
        self._create_incident()
        incident = Incident.objects.get()
        self.client.force_login(self.reporter)

        response = self.client.post(
            reverse("ai_support:status", args=(incident.pk,)),
            {"status": Incident.STATUS_FIXED},
        )

        self.assertEqual(response.status_code, 403)
        incident.refresh_from_db()
        self.assertEqual(incident.status, Incident.STATUS_NEW)

    def test_reviewer_can_change_status_and_audit_message_is_created(self):
        self._create_incident()
        incident = Incident.objects.get()
        self.client.force_login(self.reviewer)

        response = self.client.post(
            reverse("ai_support:status", args=(incident.pk,)),
            {"status": Incident.STATUS_DIAGNOSING},
        )

        self.assertRedirects(response, reverse("ai_support:detail", args=(incident.pk,)))
        incident.refresh_from_db()
        self.assertEqual(incident.status, Incident.STATUS_DIAGNOSING)
        self.assertEqual(incident.messages.order_by("-id").first().sender_type, IncidentMessage.SENDER_SYSTEM)


class AgentActionPolicyTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.user = user_model.objects.create_user(username="reporter")
        self.approver = user_model.objects.create_user(username="approver")
        self.incident = Incident.objects.create(reporter=self.user, description="Ошибка")

    def test_read_only_action_is_automatic(self):
        action = AgentAction.objects.create(
            incident=self.incident,
            action_type="read_logs",
            title="Прочитать журнал",
            status=AgentAction.STATUS_RUNNING,
        )
        self.assertEqual(action.policy_level, POLICY_AUTOMATIC)
        self.assertTrue(action.executable)

    def test_deploy_requires_human_approval(self):
        action = AgentAction(
            incident=self.incident,
            action_type="deploy_patch",
            title="Выложить исправление",
            status=AgentAction.STATUS_RUNNING,
        )
        with self.assertRaises(ValidationError):
            action.save()

        action.approved_by = self.approver
        action.save()
        self.assertEqual(action.policy_level, POLICY_APPROVAL)
        self.assertTrue(action.executable)

    def test_stock_change_is_always_blocked_for_agent(self):
        proposed = AgentAction.objects.create(
            incident=self.incident,
            action_type="change_stock",
            title="Изменить остаток",
        )
        self.assertEqual(proposed.policy_level, POLICY_PROGRAMMER)
        self.assertFalse(proposed.executable)

        proposed.approved_by = self.approver
        proposed.status = AgentAction.STATUS_RUNNING
        with self.assertRaises(ValidationError):
            proposed.save()


class EmployeeAnnouncementMiddlewareTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        user_model = get_user_model()
        self.employee_user = user_model.objects.create_user(username="notice-worker")
        Employee.objects.create(
            full_name="Получатель уведомления",
            user=self.employee_user,
            role="storekeeper",
        )
        self.client_user = user_model.objects.create_user(username="notice-client")
        self.excluded_user = user_model.objects.create_user(username="notice-packer")
        Employee.objects.create(
            full_name="Упаковщик без уведомления",
            user=self.excluded_user,
            role="packer",
        )

    @staticmethod
    def _html_response(_request):
        return HttpResponse("<html><body><main>Кабинет</main></body></html>")

    def test_notice_is_injected_for_employee(self):
        request = self.factory.get("/cabinet/processing-worker/")
        request.user = self.employee_user

        response = EmployeeAiSupportAnnouncementMiddleware(self._html_response)(request)

        self.assertContains(response, "ai-support-company-notice")
        self.assertContains(response, "Новая внутренняя ИИ‑поддержка")

    def test_notice_is_not_injected_for_non_employee(self):
        request = self.factory.get("/client/dashboard/lk/")
        request.user = self.client_user

        response = EmployeeAiSupportAnnouncementMiddleware(self._html_response)(request)

        self.assertNotContains(response, "ai-support-company-notice")

    def test_notice_is_not_injected_for_employee_outside_target_roles(self):
        request = self.factory.get("/processing-worker/")
        request.user = self.excluded_user

        response = EmployeeAiSupportAnnouncementMiddleware(self._html_response)(request)

        self.assertNotContains(response, "ai-support-company-notice")

    def test_notice_is_not_injected_into_json(self):
        request = self.factory.get("/api/status/")
        request.user = self.employee_user

        response = EmployeeAiSupportAnnouncementMiddleware(
            lambda _request: JsonResponse({"ok": True})
        )(request)

        self.assertNotContains(response, "ai-support-company-notice")


class DiagnosticWorkerTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.user = user_model.objects.create_user(username="diagnostic-reporter")
        self.incident = Incident.objects.create(
            reporter=self.user,
            reporter_role="storekeeper",
            zone="Склад",
            object_type="Заявка",
            object_id="TEST-500",
            description="После нажатия появилась ошибка 500.",
            expected_result="Страница должна открыться.",
        )
        IncidentMessage.objects.create(
            incident=self.incident,
            author=self.user,
            sender_type=IncidentMessage.SENDER_EMPLOYEE,
            body=self.incident.description,
        )
        self.action = AgentAction.objects.create(
            incident=self.incident,
            action_type="inspect_request",
            title="Проверить заявку",
        )

    @staticmethod
    def _diagnosis(**overrides):
        value = {
            "summary": "Найдена ошибка представления.",
            "probable_cause": "Обработчик не проверяет пустое значение.",
            "evidence": ["В журнале есть status=500."],
            "employee_reply": "Ошибка подтверждена и передана на подготовку исправления.",
            "confidence": "medium",
            "needs_more_info": False,
            "question": "",
            "recommended_action": "prepare_patch",
            "risk_area": "none",
        }
        value.update(overrides)
        return value

    def _diagnoser(self, **overrides):
        diagnoser = Mock()
        diagnoser.diagnose.return_value = DiagnosisResult(
            diagnosis=self._diagnosis(**overrides),
            provider_metadata={"provider": "test", "stored": False, "tools_enabled": False},
        )
        return diagnoser

    @staticmethod
    def _diagnostics(_incident):
        return {
            "read_only": True,
            "services": {"fullbox.service": "active"},
            "log_lines": ["secret log text status=500"],
            "log_line_count": 1,
        }

    def test_worker_records_diagnosis_without_storing_logs_or_changing_stock(self):
        action = process_next_incident(
            diagnoser=self._diagnoser(), diagnostics_collector=self._diagnostics
        )

        action.refresh_from_db()
        self.incident.refresh_from_db()
        self.assertEqual(action.status, AgentAction.STATUS_SUCCEEDED)
        self.assertEqual(self.incident.status, Incident.STATUS_CAUSE_FOUND)
        self.assertFalse(self.incident.inventory_changed)
        self.assertFalse(self.incident.movements_changed)
        self.assertNotIn("log_lines", action.result["diagnostics"])
        self.assertEqual(action.result["diagnostics"]["log_line_count"], 1)
        self.assertIn("режиме чтения", self.incident.messages.last().body)

    def test_worker_requests_information_when_diagnosis_needs_it(self):
        process_next_incident(
            diagnoser=self._diagnoser(
                needs_more_info=True,
                question="Какую кнопку вы нажали?",
                recommended_action="investigate_manually",
            ),
            diagnostics_collector=self._diagnostics,
        )

        self.incident.refresh_from_db()
        self.assertEqual(self.incident.status, Incident.STATUS_NEEDS_INFO)
        self.assertIn("Какую кнопку", self.incident.messages.last().body)

    def test_worker_escalates_protected_risk(self):
        process_next_incident(
            diagnoser=self._diagnoser(
                risk_area="stock", recommended_action="escalate_programmer"
            ),
            diagnostics_collector=self._diagnostics,
        )

        self.incident.refresh_from_db()
        self.assertEqual(self.incident.status, Incident.STATUS_ESCALATED)
        self.assertFalse(self.incident.inventory_changed)
        self.assertFalse(self.incident.movements_changed)

    def test_worker_failure_is_audited_and_escalated(self):
        diagnoser = Mock()
        diagnoser.diagnose.side_effect = RuntimeError("provider unavailable")

        action = process_next_incident(
            diagnoser=diagnoser, diagnostics_collector=self._diagnostics
        )

        action.refresh_from_db()
        self.incident.refresh_from_db()
        self.assertEqual(action.status, AgentAction.STATUS_FAILED)
        self.assertEqual(self.incident.status, Incident.STATUS_ESCALATED)
        self.assertEqual(self.incident.messages.last().sender_type, IncidentMessage.SENDER_SYSTEM)


class DiagnosticClientTests(TestCase):
    def test_redaction_removes_common_secrets_and_personal_markers(self):
        source = (
            "user=ivan ip=192.168.1.10 email=a@example.com token=abc "
            "password=qwerty Authorization: Bearer bearer-value"
        )

        redacted = redact_diagnostic_text(source)

        self.assertNotIn("ivan", redacted)
        self.assertNotIn("192.168.1.10", redacted)
        self.assertNotIn("a@example.com", redacted)
        self.assertNotIn("abc", redacted)
        self.assertNotIn("qwerty", redacted)
        self.assertNotIn("bearer-value", redacted)

    @patch("ai_support.llm.requests.post")
    def test_responses_request_has_strict_schema_no_tools_and_no_storage(self, post):
        diagnosis = DiagnosticWorkerTests._diagnosis()
        response = Mock(status_code=200, headers={"x-request-id": "req-test"})
        response.json.return_value = {
            "id": "resp-test",
            "model": "test-model",
            "output": [
                {
                    "type": "message",
                    "content": [
                        {"type": "output_text", "text": json.dumps(diagnosis, ensure_ascii=False)}
                    ],
                }
            ],
            "usage": {"input_tokens": 10, "output_tokens": 20, "total_tokens": 30},
        }
        post.return_value = response
        client = OpenAIResponsesDiagnosticClient(
            api_key="test-key",
            model="test-model",
            base_url="https://api.openai.com/v1",
        )

        result = client.diagnose({"incident": {"number": "INC-1"}})

        payload = post.call_args.kwargs["json"]
        self.assertFalse(payload["store"])
        self.assertEqual(payload["tools"], [])
        self.assertFalse(payload["parallel_tool_calls"])
        self.assertTrue(payload["text"]["format"]["strict"])
        self.assertEqual(payload["text"]["format"]["type"], "json_schema")
        self.assertFalse(result.provider_metadata["stored"])
        self.assertFalse(result.provider_metadata["tools_enabled"])

    def test_disabled_management_command_leaves_queue_untouched(self):
        user = get_user_model().objects.create_user(username="disabled-worker-test")
        incident = Incident.objects.create(reporter=user, description="Ошибка 500")
        action = AgentAction.objects.create(
            incident=incident,
            action_type="inspect_request",
            title="Проверить заявку",
        )
        output = StringIO()

        call_command("process_ai_support", stdout=output)

        action.refresh_from_db()
        incident.refresh_from_db()
        self.assertEqual(action.status, AgentAction.STATUS_PROPOSED)
        self.assertEqual(incident.status, Incident.STATUS_NEW)
        self.assertIn("enabled=false", output.getvalue())
