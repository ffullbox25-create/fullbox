from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from employees.models import Employee

from .models import (
    FbsStorekeeperAlertAcknowledgement,
    FbsStorekeeperResponsible,
)
from .services.storekeeper_alerts import (
    acknowledge_storekeeper_alerts,
    build_storekeeper_alert_payload,
    is_storekeeper_alert_responsible,
    _inventory_location_labels,
)


def alert_case(key="order:101:ready"):
    return {
        "key": key,
        "kind": "ready",
        "agency_id": 7,
        "client": "Тестовый клиент",
        "marketplace": "Ozon",
        "order_id": "ORDER-101",
        "age_label": "11 ч 0 мин",
        "started_at": (timezone.now() - timedelta(hours=11)).isoformat(),
        "cutoff_at": None,
        "locations": ["OS · Линия A · Стеллаж 1 · Этаж 1 · Ячейка 1"],
        "products": ["Товар"],
        "action_url": "/fbs/operator/queue/?agency=7",
    }


@override_settings(FBS_MODULE_ENABLED=True)
class StorekeeperAlertControlTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = get_user_model().objects.create_user(
            username="responsible", password="test-password"
        )
        Employee.objects.create(
            full_name="Ответственный Кладовщик",
            user=self.user,
            role="storekeeper",
            is_active=True,
        )
        FbsStorekeeperResponsible.objects.create(user=self.user)

    def test_only_explicitly_assigned_storekeeper_is_responsible(self):
        other = get_user_model().objects.create_user(
            username="other-storekeeper", password="test-password"
        )
        Employee.objects.create(
            full_name="Другой Кладовщик",
            user=other,
            role="storekeeper",
            is_active=True,
        )

        self.assertTrue(is_storekeeper_alert_responsible(self.user))
        self.assertFalse(is_storekeeper_alert_responsible(other))

    @patch(
        "fbs.services.storekeeper_alerts.fbs_box_physical_location_label",
        return_value="PR · Зона приемки",
    )
    def test_inventory_alert_prefers_physical_box_location(self, location_label):
        session = SimpleNamespace(
            box_id=17,
            box=object(),
            workflow_place_label="План FBS · место определяется сканом ричтрака",
        )

        self.assertEqual(
            _inventory_location_labels(session),
            ["PR · Зона приемки"],
        )
        location_label.assert_called_once_with(session.box)

    @patch("fbs.services.storekeeper_alerts.active_storekeeper_alert_cases")
    def test_acknowledgement_is_shared_and_repeats_after_four_hours(self, cases):
        cases.return_value = [alert_case()]
        acknowledged = acknowledge_storekeeper_alerts(
            user=self.user,
            alert_keys=["order:101:ready"],
        )

        self.assertEqual(acknowledged, 1)
        payload = build_storekeeper_alert_payload()
        self.assertEqual(payload["due_count"], 0)
        self.assertEqual(payload["groups"][0]["claimed_by"], "Ответственный Кладовщик")

        FbsStorekeeperAlertAcknowledgement.objects.update(
            acknowledged_at=timezone.now() - timedelta(hours=4, minutes=1)
        )
        payload = build_storekeeper_alert_payload()
        self.assertEqual(payload["due_count"], 1)
        self.assertTrue(payload["groups"][0]["due"])

    @patch("fbs.services.storekeeper_alerts.active_storekeeper_alert_cases")
    def test_acknowledgement_ignores_alert_that_is_no_longer_active(self, cases):
        cases.return_value = []

        acknowledged = acknowledge_storekeeper_alerts(
            user=self.user,
            alert_keys=["order:101:ready"],
        )

        self.assertEqual(acknowledged, 0)
        self.assertFalse(FbsStorekeeperAlertAcknowledgement.objects.exists())

    def test_non_responsible_cannot_acknowledge(self):
        other = get_user_model().objects.create_user(
            username="not-responsible", password="test-password"
        )

        with self.assertRaises(PermissionError):
            acknowledge_storekeeper_alerts(
                user=other,
                alert_keys=["order:101:ready"],
            )

    def test_non_responsible_storekeeper_receives_no_alert_control(self):
        other = get_user_model().objects.create_user(
            username="ordinary-storekeeper", password="test-password"
        )
        Employee.objects.create(
            full_name="Обычный Кладовщик",
            user=other,
            role="storekeeper",
            is_active=True,
        )
        self.client.force_login(other)

        response = self.client.get(reverse("fbs:tsd_storekeeper_alerts"))

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["enabled"])

    @patch("fbs.services.storekeeper_alerts.build_storekeeper_alert_payload")
    def test_responsible_storekeeper_can_poll_alerts(self, build_payload):
        build_payload.return_value = {
            "enabled": True,
            "total_count": 1,
            "due_count": 1,
            "groups": [],
        }
        self.client.force_login(self.user)

        response = self.client.get(reverse("fbs:tsd_storekeeper_alerts"))

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["enabled"])
        self.assertEqual(response.json()["due_count"], 1)
        self.assertEqual(response.json()["modal_due_count"], 1)

    def test_acknowledgement_snoozes_remaining_modal_tasks_for_one_hour(self):
        payload = {
            "enabled": True,
            "checked_at": "15.09.2026 09:30",
            "total_count": 4,
            "due_count": 3,
            "groups": [],
        }
        self.client.force_login(self.user)

        with patch(
            "fbs.services.storekeeper_alerts.acknowledge_storekeeper_alerts",
            return_value=1,
        ), patch(
            "fbs.services.storekeeper_alerts.build_storekeeper_alert_payload",
            return_value=payload,
        ):
            response = self.client.post(
                reverse("fbs:tsd_storekeeper_alerts"),
                {"alert_keys": ["order:101:ready"]},
            )

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["pending_due_count"], 3)
            self.assertEqual(response.json()["due_count"], 0)
            self.assertEqual(response.json()["modal_due_count"], 0)
            self.assertTrue(response.json()["control_snoozed"])
            self.assertEqual(response.json()["snoozed_due_count"], 3)
            self.assertGreaterEqual(response.json()["snooze_minutes_left"], 59)

            poll_response = self.client.get(reverse("fbs:tsd_storekeeper_alerts"))
            self.assertEqual(poll_response.json()["modal_due_count"], 0)

            session = self.client.session
            session["fbs_storekeeper_alert_snooze_until"] = int(
                (timezone.now() - timedelta(minutes=1)).timestamp()
            )
            session.save()
            repeat_response = self.client.get(reverse("fbs:tsd_storekeeper_alerts"))

        self.assertEqual(repeat_response.json()["pending_due_count"], 3)
        self.assertEqual(repeat_response.json()["due_count"], 3)
        self.assertEqual(repeat_response.json()["modal_due_count"], 3)
        self.assertFalse(repeat_response.json()["control_snoozed"])

    @patch("fbs.services.storekeeper_alerts.build_storekeeper_alert_payload")
    def test_recent_acknowledgement_snoozes_a_new_session(self, build_payload):
        build_payload.return_value = {
            "enabled": True,
            "checked_at": "15.09.2026 09:30",
            "total_count": 4,
            "due_count": 3,
            "groups": [],
        }
        FbsStorekeeperAlertAcknowledgement.objects.create(
            alert_key="order:101:ready",
            alert_kind="ready",
            responsible=self.user,
            acknowledged_at=timezone.now() - timedelta(minutes=30),
        )
        self.client.force_login(self.user)

        response = self.client.get(reverse("fbs:tsd_storekeeper_alerts"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["pending_due_count"], 3)
        self.assertEqual(response.json()["due_count"], 0)
        self.assertTrue(response.json()["control_snoozed"])
        self.assertLessEqual(response.json()["snooze_minutes_left"], 30)
