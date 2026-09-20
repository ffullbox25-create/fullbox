"""Tests for the manager queue read model.

The production test command is intentionally run only after separate approval.
"""

from datetime import timedelta
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

from django.test import SimpleTestCase
from django.utils import timezone

from .queue import _query_url
from .queue_states import QUEUE_STATES, is_trip_confirmation, queue_state


class QueueStateTests(SimpleTestCase):
    def task(self, **overrides):
        now = timezone.now()
        values = {
            "title": "Обычная задача",
            "route": "/orders/receiving/42/",
            "status": "in_progress",
            "due_date": now + timedelta(hours=3),
            "updated_at": now,
        }
        values.update(overrides)
        return SimpleNamespace(**values)

    def status(self, bucket):
        return SimpleNamespace(bucket=bucket)

    def test_trip_confirmation_has_priority_over_overdue(self):
        now = timezone.now()
        task = self.task(
            title="Подтвердить доставку: рейс №112_RS",
            route="/logistics/trips/112/",
            due_date=now - timedelta(days=3),
        )
        self.assertTrue(is_trip_confirmation(task))
        self.assertEqual(queue_state(task, request_status=self.status("manager"), now=now), "to_confirm")

    def test_arbitrary_title_is_not_trip_confirmation(self):
        task = self.task(
            title="Подтвердить доставку клиенту",
            route="/logistics/trips/112/",
        )
        self.assertFalse(is_trip_confirmation(task))

    def test_receiving_act_has_priority_over_overdue(self):
        now = timezone.now()
        task = self.task(due_date=now - timedelta(days=1))
        payload = {
            "act_storekeeper_signed": True,
            "act_manager_signed": False,
            "act_sent": False,
        }
        self.assertEqual(
            queue_state(task, request_status=self.status("manager"), payload=payload, now=now),
            "to_confirm",
        )

    def test_manager_backlog_has_priority_over_overdue(self):
        now = timezone.now()
        task = self.task(status="backlog", due_date=now - timedelta(days=1))
        self.assertEqual(queue_state(task, request_status=self.status("manager"), now=now), "to_accept")

    def test_overdue_precedes_stuck(self):
        now = timezone.now()
        task = self.task(
            due_date=now - timedelta(hours=1),
            updated_at=now - timedelta(days=5),
        )
        self.assertEqual(queue_state(task, request_status=self.status("warehouse"), now=now), "overdue")

    def test_waiting_states_use_structured_sources(self):
        now = timezone.now()
        self.assertEqual(queue_state(self.task(), request_status=self.status("client"), now=now), "waiting_client")
        self.assertEqual(queue_state(self.task(), request_status=self.status("warehouse"), now=now), "waiting_warehouse")
        self.assertEqual(
            queue_state(self.task(), request_status=self.status("manager"), routing_status="routed", now=now),
            "waiting_logistics",
        )

    def test_state_catalog_is_closed_and_unique(self):
        keys = [key for key, _label in QUEUE_STATES]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertEqual(
            set(keys),
            {"to_accept", "to_confirm", "waiting_client", "waiting_warehouse", "waiting_logistics", "overdue", "stuck", "done"},
        )


class QueueLinkTests(SimpleTestCase):
    def test_state_link_preserves_current_filters_and_resets_page(self):
        url = _query_url(
            {
                "state": "overdue",
                "client": "17",
                "warehouse": "Купавна",
                "due": "week",
                "scope": "mine",
                "q": "PR-42",
            },
            state="to_confirm",
            page=1,
        )
        query = parse_qs(urlparse(url).query)
        self.assertEqual(query["state"], ["to_confirm"])
        self.assertEqual(query["client"], ["17"])
        self.assertEqual(query["warehouse"], ["Купавна"])
        self.assertEqual(query["due"], ["week"])
        self.assertEqual(query["scope"], ["mine"])
        self.assertEqual(query["q"], ["PR-42"])
        self.assertNotIn("page", query)
