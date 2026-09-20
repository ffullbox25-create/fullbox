import json
import os
from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.contrib.sessions.middleware import SessionMiddleware
from django.test import RequestFactory, TestCase, override_settings
from django.utils import timezone

from employees.models import Employee

from .models import AgentCommand, AgentContext, AgentEvent, DeviceAgent
from .auth import secret_digest
from .desktop_auth import authenticate_desktop_request
from .services import (
    agent_commands_response,
    agent_context_claim_response,
    agent_ping_response,
    agent_status_response,
    record_agent_event,
)


class AgentServiceTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.user = get_user_model().objects.create_user(username="agent-user", password="secret")
        Employee.objects.create(user=self.user, full_name="Agent User", role="storekeeper")

    def _with_session(self, request):
        middleware = SessionMiddleware(lambda req: None)
        middleware.process_request(request)
        request.session.save()
        return request

    def test_agent_status_response_requires_agent_id(self):
        request = self.factory.get("/agent/status/")
        request.user = self.user
        request = self._with_session(request)

        response = agent_status_response(request)
        payload = json.loads(response.content)

        self.assertEqual(response.status_code, 400)
        self.assertEqual(payload["error"], "missing_agent_id")

    def test_agent_context_claim_response_creates_context(self):
        request = self.factory.post(
            "/agent/contexts/claim/",
            data=json.dumps({"agent_id": "scanner-1", "order_id": 42, "box_id": "BOX-7"}),
            content_type="application/json",
        )
        request.user = self.user
        request = self._with_session(request)

        response = agent_context_claim_response(request)
        payload = json.loads(response.content)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertTrue(AgentContext.objects.filter(agent_id="scanner-1", order_id=42, box_id="BOX-7").exists())
        self.assertFalse(payload["realtime_enabled"])

    @override_settings(FULLBOX_REALTIME_ENABLED=True, FULLBOX_REALTIME_AGENT_IDS={"scanner-allowed"})
    def test_agent_context_claim_response_marks_realtime_only_for_allowed_agent(self):
        request = self.factory.post(
            "/agent/contexts/claim/",
            data=json.dumps({"agent_id": "scanner-allowed", "order_id": 42}),
            content_type="application/json",
        )
        request.user = self.user
        request = self._with_session(request)

        response = agent_context_claim_response(request)
        payload = json.loads(response.content)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["realtime_enabled"])

        request = self.factory.post(
            "/agent/contexts/claim/",
            data=json.dumps({"agent_id": "scanner-old", "order_id": 42}),
            content_type="application/json",
        )
        request.user = self.user
        request = self._with_session(request)

        response = agent_context_claim_response(request)
        payload = json.loads(response.content)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["realtime_enabled"])

    @override_settings(DEBUG=True, AGENT_SHARED_TOKEN="", PRINT_AGENT_TOKEN="")
    def test_agent_commands_response_delivers_pending_commands(self):
        AgentCommand.objects.create(agent_id="scanner-1", command="scan", payload={"x": 1})
        request = self.factory.get("/agent/commands/?agent_id=scanner-1")
        request.user = self.user
        request = self._with_session(request)

        response = agent_commands_response(request)
        payload = json.loads(response.content)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(len(payload["commands"]), 1)
        self.assertEqual(AgentCommand.objects.get().status, AgentCommand.STATUS_DELIVERED)

    @override_settings(DEBUG=True, AGENT_SHARED_TOKEN="", PRINT_AGENT_TOKEN="")
    def test_agent_ping_response_upserts_device_agent(self):
        request = self.factory.post(
            "/agent/ping/",
            data=json.dumps({"agent_id": "scanner-2", "name": "Scanner 2", "meta": {"com_health": {"ready": True}}}),
            content_type="application/json",
        )
        request.user = self.user
        request = self._with_session(request)

        response = agent_ping_response(request)
        payload = json.loads(response.content)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["ok"])
        agent = DeviceAgent.objects.get(agent_id="scanner-2")
        self.assertEqual(agent.name, "Scanner 2")
        self.assertEqual(agent.meta["com_health"]["ready"], True)

    @override_settings(DEBUG=True, AGENT_SHARED_TOKEN="", PRINT_AGENT_TOKEN="")
    def test_agent_ping_response_keeps_last_known_printers_on_empty_ping(self):
        DeviceAgent.objects.create(
            agent_id="scanner-3",
            name="Scanner 3",
            meta={
                "printers": ["TSC TE200"],
                "last_known_printers": ["TSC TE200"],
                "printer_details": [{"name": "TSC TE200", "is_default": True}],
            },
        )
        request = self.factory.post(
            "/agent/ping/",
            data=json.dumps(
                {
                    "agent_id": "scanner-3",
                    "name": "Scanner 3",
                    "meta": {"printers": [], "com_health": {"ready": False}},
                }
            ),
            content_type="application/json",
        )
        request.user = self.user
        request = self._with_session(request)

        response = agent_ping_response(request)
        payload = json.loads(response.content)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["ok"])
        agent = DeviceAgent.objects.get(agent_id="scanner-3")
        self.assertEqual(agent.meta["printers"], ["TSC TE200"])
        self.assertEqual(agent.meta["last_known_printers"], ["TSC TE200"])
        self.assertEqual(agent.meta["printer_details"], [{"name": "TSC TE200", "is_default": True}])
        self.assertEqual(agent.meta["com_health"]["ready"], False)

    @override_settings(FULLBOX_REALTIME_ENABLED=True)
    def test_pending_agent_command_notifies_realtime_after_commit(self):
        with mock.patch("agent.realtime._group_send") as group_send:
            with self.captureOnCommitCallbacks(execute=True):
                command = AgentCommand.objects.create(agent_id="scanner-1", command="scanner.status")

        group_send.assert_called_once()
        group, message = group_send.call_args.args
        self.assertEqual(group, "fullbox.agent.scanner-1")
        self.assertEqual(message["type"], "agent.command_available")
        self.assertEqual(message["command_id"], command.id)

    @override_settings(FULLBOX_REALTIME_ENABLED=True)
    def test_agent_event_notifies_context_realtime_after_commit(self):
        AgentContext.objects.create(
            agent_id="scanner-1",
            context_id="ctx-1",
            user=self.user,
            active=True,
            last_seen=timezone.now(),
            expires_at=timezone.now() + timedelta(seconds=30),
        )

        with mock.patch("agent.realtime._group_send") as group_send:
            with self.captureOnCommitCallbacks(execute=True):
                event = record_agent_event("scanner-1", AgentEvent.EVENT_SCAN, {"value": "ABC"})

        group_send.assert_called_once()
        group, message = group_send.call_args.args
        self.assertEqual(group, "fullbox.context.ctx-1")
        self.assertEqual(message["type"], "agent.event_available")
        self.assertEqual(message["event"]["id"], event.id)
        self.assertEqual(message["event"]["payload"]["value"], "ABC")


class DesktopAuthenticationTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.agent = DeviceAgent.objects.create(
            agent_id="desktop-auth-0123456789abcdef0123456789abcdef",
            name="desktop-test",
            token_digest=secret_digest("desktop-secret"),
        )

    def _request(self, *, token="desktop-secret", agent_id=None):
        headers = {"HTTP_X_FULLBOX_DESKTOP": "1"}
        if agent_id is not False:
            headers["HTTP_X_FULLBOX_DESKTOP_ID"] = agent_id or self.agent.agent_id
        if token is not False:
            headers["HTTP_X_FULLBOX_DESKTOP_TOKEN"] = token
        headers["HTTP_X_FULLBOX_DESKTOP_VERSION"] = "1.0.24"
        return self.factory.get("/agent/desktop/verify/", **headers)

    def test_individual_token_is_bound_to_workstation(self):
        authentication = authenticate_desktop_request(self._request(), "desktop-test")

        self.assertTrue(authentication.ok)
        self.assertEqual(authentication.mode, "device")
        self.agent.refresh_from_db()
        self.assertEqual(self.agent.version, "1.0.24")
        self.assertIsNotNone(self.agent.last_seen)

    def test_individual_token_rejects_other_workstation(self):
        authentication = authenticate_desktop_request(self._request(), "desktop-other")

        self.assertFalse(authentication.ok)
        self.assertEqual(authentication.error, "desktop_workstation_mismatch")

    def test_invalid_individual_token_never_downgrades_to_legacy(self):
        with mock.patch.dict(os.environ, {"FULLBOX_DESKTOP_LEGACY_AUTH_ENABLED": "true"}):
            authentication = authenticate_desktop_request(
                self._request(token="wrong-secret"),
                "desktop-test",
            )

        self.assertFalse(authentication.ok)
        self.assertEqual(authentication.error, "invalid_desktop_credentials")

    def test_legacy_marker_is_temporarily_accepted_without_individual_headers(self):
        with mock.patch.dict(os.environ, {"FULLBOX_DESKTOP_LEGACY_AUTH_ENABLED": "true"}):
            authentication = authenticate_desktop_request(
                self._request(token=False, agent_id=False),
                "desktop-test",
            )

        self.assertTrue(authentication.ok)
        self.assertEqual(authentication.mode, "legacy")

    def test_legacy_marker_can_be_disabled_after_rollout(self):
        with mock.patch.dict(os.environ, {"FULLBOX_DESKTOP_LEGACY_AUTH_ENABLED": "false"}):
            authentication = authenticate_desktop_request(
                self._request(token=False, agent_id=False),
                "desktop-test",
            )

        self.assertFalse(authentication.ok)
        self.assertEqual(authentication.error, "desktop_activation_required")
