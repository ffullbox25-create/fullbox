from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from agent.models import DeviceAgent
from .agent_selection import deduplicate_scanner_device_agents


class ProcessingAgentSelectionTests(TestCase):
    def test_flow_keeps_latest_agent_and_exposes_old_id_as_alias(self):
        old = DeviceAgent.objects.create(
            agent_id="pc-old-comp026",
            name="COMP026",
            host="COMP026",
            last_seen=timezone.now() - timedelta(days=30),
            meta={"com_ports": ["COM4"]},
        )
        current = DeviceAgent.objects.create(
            agent_id="pc-current-comp026",
            name="COMP026",
            host="COMP026",
            last_seen=timezone.now(),
            meta={
                "com_ports": ["COM4"],
                "com_status": {"port": "COM4", "connected": True, "enabled": True},
            },
        )
        agents = deduplicate_scanner_device_agents(
            DeviceAgent.objects.order_by("-last_seen", "-updated_at")
        )

        self.assertEqual(len(agents), 1)
        self.assertEqual(agents[0]["agent"].agent_id, current.agent_id)
        self.assertEqual(agents[0]["aliases"], [old.agent_id])
