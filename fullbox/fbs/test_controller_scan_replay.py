from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from django.test import SimpleTestCase

from .controller_views import _controller_agent_replay_event_ids


class FbsControllerAgentReplayTests(SimpleTestCase):
    origin = datetime(2026, 8, 25, tzinfo=timezone.utc)

    @classmethod
    def event(cls, event_id, seconds, value="4600000000001", source="com"):
        return SimpleNamespace(
            id=event_id,
            created_at=cls.origin + timedelta(seconds=seconds),
            payload={"value": value, "source": source},
        )

    def test_retry_does_not_become_baseline_for_next_real_scan(self):
        events = [
            self.event(1, 0),
            self.event(2, 8),
            self.event(3, 16),
            self.event(4, 24),
        ]

        self.assertEqual(_controller_agent_replay_event_ids(events), {2, 4})

    def test_different_value_and_non_com_source_are_not_replays(self):
        events = [
            self.event(1, 0, value="4600000000001"),
            self.event(2, 8, value="4600000000002"),
            self.event(3, 16, value="4600000000001", source="keyboard"),
        ]

        self.assertEqual(_controller_agent_replay_event_ids(events), set())

    def test_same_value_outside_agent_retry_window_is_a_fresh_scan(self):
        events = [
            self.event(1, 0),
            self.event(2, 5),
            self.event(3, 17),
        ]

        self.assertEqual(_controller_agent_replay_event_ids(events), set())
