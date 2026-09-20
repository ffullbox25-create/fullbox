from __future__ import annotations

import json
import shutil
import tempfile
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from billing.models import (
    BillingApplication,
    BillingService,
    ClientTariffItem,
    ClientTariffVersion,
    TariffCategory,
    TariffUnit,
    WarehouseServiceFact,
)
from employees.access import resolve_cabinet_url
from employees.models import Employee
from shipping.models import ShippingOrder
from sku.models import Agency, Market

from .models import (
    LogisticsTrip,
    LogisticsTripOrder,
    ProblemTrip,
    ShippingRoutingState,
    TripAttachment,
    TripHistory,
    TripNotification,
    TripPenalty,
)


User = get_user_model()


@override_settings(ALLOWED_HOSTS=["testserver"])
class DriverTripWorkflowTests(TestCase):
    def setUp(self):
        self.media_root = tempfile.mkdtemp(prefix="fullbox-driver-tests-")
        self.media_override = override_settings(MEDIA_ROOT=self.media_root)
        self.media_override.enable()
        self.addCleanup(self.media_override.disable)
        self.addCleanup(shutil.rmtree, self.media_root, True)

        self.logistic_user = User.objects.create_user(username="driver_flow_logistic", password="pwd")
        self.logistic = Employee.objects.create(
            full_name="Логист Тест",
            role="logistician",
            user=self.logistic_user,
            is_active=True,
        )
        self.driver_user = User.objects.create_user(username="driver_flow_driver", password="pwd")
        self.driver = Employee.objects.create(
            full_name="Водитель Тест",
            phone="+7 900 100-20-30",
            role="driver",
            user=self.driver_user,
            is_active=True,
        )
        self.other_driver_user = User.objects.create_user(username="driver_flow_other", password="pwd")
        self.other_driver = Employee.objects.create(
            full_name="Другой Водитель",
            role="driver",
            user=self.other_driver_user,
            is_active=True,
        )
        self.agency = Agency.objects.create(agn_name="Клиент водительского рейса")
        self.market = Market.objects.create(id=9901, name="Ozon")
        self.order = ShippingOrder.objects.create(
            number="OTG-DRIVER-1",
            agency=self.agency,
            marketplace=self.market,
            status=ShippingOrder.STATUS_PACKED,
            destination_warehouse="Склад Ozon Софьино",
            destination_address="Московская область, Софьино",
            expected_boxes=4,
            supply_number="SUP-100",
        )
        self.trip = LogisticsTrip.objects.create(
            number="9001_RS",
            trip_date=timezone.localdate(),
            status=LogisticsTrip.STATUS_DEPARTED,
            assigned_logistician=self.logistic,
        )
        LogisticsTripOrder.objects.create(trip=self.trip, shipping_order=self.order, loading_sequence=1, delivery_sequence=1)

    def _login(self, user):
        client = Client()
        client.force_login(user)
        return client

    def _assign(self):
        response = self._login(self.logistic_user).post(
            f"/logistics/trips/{self.trip.pk}/driver-assignment/",
            {
                "driver_id": self.driver.pk,
                "scheduled_at": timezone.localtime().strftime("%Y-%m-%dT%H:%M"),
            },
        )
        self.assertEqual(response.status_code, 302)
        self.trip.refresh_from_db()

    def _preassign(self):
        response = self._login(self.logistic_user).post(
            f"/logistics/trips/{self.trip.pk}/driver-assignment/",
            {
                "driver_id": self.driver.pk,
                "scheduled_at": timezone.localtime(timezone.now() + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M"),
                "assignment_mode": "preliminary",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.trip.refresh_from_db()

    def _advance_to_delivery(self):
        self._assign()
        client = self._login(self.driver_user)
        for action in ("accept", "depart", "arrive"):
            response = client.post(
                f"/logistics/driver/trips/{self.trip.pk}/",
                {"action": action},
                HTTP_X_REQUESTED_WITH="XMLHttpRequest",
            )
            self.assertEqual(response.status_code, 200, response.content)
        self.trip.refresh_from_db()
        self.assertEqual(self.trip.driver_status, LogisticsTrip.DRIVER_STATUS_AWAITING_DELIVERY)
        return client

    def _add_logistics_fact(self):
        service, _created = BillingService.objects.get_or_create(
            code="driver_logistics_trip_test",
            defaults={"name": "Тестовая услуга водителя", "unit": "рейс", "is_active": True},
        )
        return WarehouseServiceFact.objects.create(
            client=self.agency,
            order_type="logistics",
            order_id=self.trip.number,
            service=service,
            service_name_snapshot=service.name,
            quantity=1,
            unit="рейс",
            status=WarehouseServiceFact.STATUS_SENT_TO_BILLING,
            source="logistics_manual",
            reported_by=self.driver_user,
        )

    def _agree_logistics_service(self):
        service, _created = BillingService.objects.get_or_create(
            code="driver_logistics_offline_test",
            defaults={"name": "Офлайн-услуга водителя", "unit": "рейс", "is_active": True},
        )
        category, _created = TariffCategory.objects.get_or_create(
            code="logistics",
            defaults={"name": "Логистика", "sort_order": 110, "is_active": True},
        )
        unit, _created = TariffUnit.objects.get_or_create(
            code="trip_driver_test",
            defaults={"name": "Рейс", "short_name": "рейс", "is_active": True},
        )
        version = ClientTariffVersion.objects.create(
            client=self.agency,
            name="Тариф водителя",
            version_number=1,
            status=ClientTariffVersion.STATUS_DRAFT,
            valid_from=timezone.localdate(),
        )
        ClientTariffItem.objects.create(
            tariff_version=version,
            category=category,
            service=service,
            service_name=service.name,
            unit=unit,
            price=100,
            is_active=True,
        )
        version.status = ClientTariffVersion.STATUS_ACTIVE
        version.save(update_fields=["status"])
        return service

    def test_driver_role_has_separate_home(self):
        self.assertEqual(resolve_cabinet_url("driver"), "/logistics/driver/trips/")

    def test_assignment_links_trip_notifies_driver_and_records_history(self):
        self._assign()
        self.assertEqual(self.trip.assigned_driver, self.driver)
        self.assertEqual(self.trip.driver_status, LogisticsTrip.DRIVER_STATUS_ASSIGNED)
        self.assertEqual(self.trip.driver_name, self.driver.full_name)
        self.assertTrue(TripNotification.objects.filter(recipient=self.driver, trip=self.trip).exists())
        self.assertTrue(TripHistory.objects.filter(trip=self.trip, event_type="driver_assigned").exists())
        response = self._login(self.driver_user).get("/logistics/driver/trips/?bucket=new")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.trip.number)

    def test_preliminary_assignment_notifies_driver_but_blocks_trip_actions(self):
        assignment_page = self._login(self.logistic_user).get(f"/logistics/trips/{self.trip.pk}/driver-assignment/")
        self.assertContains(assignment_page, "Предварительно поставить на рейс")
        self.assertContains(assignment_page, "Сразу подтвердить рейс")
        self._preassign()
        self.assertEqual(self.trip.assigned_driver, self.driver)
        self.assertEqual(self.trip.driver_status, LogisticsTrip.DRIVER_STATUS_PRELIMINARY)
        notification = TripNotification.objects.get(
            recipient=self.driver,
            trip=self.trip,
            source_key__contains=":preliminary:",
        )
        self.assertIn("Предварительный рейс", notification.title)
        self.assertIn("пока не подтвержден", notification.text)
        self.assertTrue(TripHistory.objects.filter(trip=self.trip, event_type="driver_preassigned").exists())
        client = self._login(self.driver_user)
        trip_list = client.get("/logistics/driver/trips/?bucket=new")
        self.assertContains(trip_list, "План еще не подтвержден")
        detail = client.get(f"/logistics/driver/trips/{self.trip.pk}/")
        self.assertContains(detail, "рейс еще не подтвержден")
        self.assertNotContains(detail, "Принять рейс")
        blocked = client.post(
            f"/logistics/driver/trips/{self.trip.pk}/",
            {"action": "accept"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(blocked.status_code, 400)

    def test_logistician_confirms_preliminary_trip_and_driver_gets_second_notification(self):
        self._preassign()
        preliminary_notification = TripNotification.objects.get(
            recipient=self.driver,
            trip=self.trip,
            source_key__contains=":preliminary:",
        )
        response = self._login(self.logistic_user).post(
            f"/logistics/trips/{self.trip.pk}/driver-assignment/",
            {
                "driver_id": self.driver.pk,
                "scheduled_at": timezone.localtime(self.trip.scheduled_at).strftime("%Y-%m-%dT%H:%M"),
                "assignment_mode": "confirmed",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.trip.refresh_from_db()
        self.assertEqual(self.trip.driver_status, LogisticsTrip.DRIVER_STATUS_ASSIGNED)
        confirmed_notification = TripNotification.objects.get(
            recipient=self.driver,
            trip=self.trip,
            source_key__contains=":assigned:",
        )
        self.assertGreater(confirmed_notification.pk, preliminary_notification.pk)
        self.assertIn("Рейс подтвержден", confirmed_notification.text)
        self.assertTrue(TripHistory.objects.filter(trip=self.trip, event_type="driver_assignment_confirmed").exists())
        poll = self._login(self.driver_user).get("/logistics/driver/notifications/?after=0").json()
        self.assertEqual([item["kind"] for item in poll["notifications"]], ["preliminary", "assigned"])
        detail = self._login(self.driver_user).get(f"/logistics/driver/trips/{self.trip.pk}/")
        self.assertContains(detail, "Принять рейс")

    def test_driver_notification_poll_returns_new_assignment_after_cursor(self):
        self._assign()
        notification = TripNotification.objects.get(recipient=self.driver, trip=self.trip, source_key__contains=":assigned:")
        client = self._login(self.driver_user)
        response = client.get("/logistics/driver/notifications/?after=0")
        self.assertEqual(response.status_code, 200)
        self.assertIn("no-store", response.headers["Cache-Control"])
        payload = response.json()
        self.assertEqual(payload["last_id"], notification.pk)
        self.assertEqual(len(payload["notifications"]), 1)
        self.assertEqual(payload["notifications"][0]["title"], f"Назначен новый рейс {self.trip.number}")
        self.assertEqual(payload["notifications"][0]["url"], f"/logistics/driver/trips/{self.trip.pk}/")
        self.assertIsNone(TripNotification.objects.get(pk=notification.pk).read_at)
        after_response = client.get(f"/logistics/driver/notifications/?after={notification.pk}")
        self.assertEqual(after_response.json()["notifications"], [])

    def test_driver_pages_include_sound_alert_and_mobile_controls(self):
        self._assign()
        response = self._login(self.driver_user).get("/logistics/driver/trips/?bucket=new")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="driver-sound-toggle"')
        self.assertContains(response, 'id="driver-assignment-alert"')
        self.assertContains(response, "/static/logistics/driver-notifications.js")
        self.assertContains(response, "viewport-fit=cover")
        self.assertContains(response, 'class="card driver-tabs"')

    def test_replacement_notifies_both_drivers_and_hides_trip_from_previous(self):
        self._assign()
        response = self._login(self.logistic_user).post(
            f"/logistics/trips/{self.trip.pk}/driver-assignment/",
            {"driver_id": self.other_driver.pk, "scheduled_at": timezone.localtime().strftime("%Y-%m-%dT%H:%M")},
        )
        self.assertEqual(response.status_code, 302)
        self.trip.refresh_from_db()
        self.assertEqual(self.trip.assigned_driver, self.other_driver)
        self.assertTrue(TripNotification.objects.filter(recipient=self.driver, title__icontains="отменено").exists())
        self.assertTrue(TripNotification.objects.filter(recipient=self.other_driver, title__icontains="Назначен").exists())
        previous_list = self._login(self.driver_user).get("/logistics/driver/trips/?bucket=new")
        self.assertEqual(previous_list.context["rows"], [])

    def test_driver_cannot_open_another_drivers_trip(self):
        self._assign()
        response = self._login(self.other_driver_user).get(f"/logistics/driver/trips/{self.trip.pk}/")
        self.assertEqual(response.status_code, 403)

    def test_driver_and_logistician_screens_render(self):
        self._assign()
        driver_detail = self._login(self.driver_user).get(f"/logistics/driver/trips/{self.trip.pk}/")
        self.assertEqual(driver_detail.status_code, 200)
        self.assertContains(driver_detail, "Принять рейс")
        logistic_client = self._login(self.logistic_user)
        assignment = logistic_client.get(f"/logistics/trips/{self.trip.pk}/driver-assignment/")
        self.assertEqual(assignment.status_code, 200)
        self.assertContains(assignment, self.driver.full_name)
        core_trip = logistic_client.get(f"/logistics/trips/{self.trip.pk}/")
        self.assertEqual(core_trip.status_code, 200)
        self.assertContains(core_trip, "Назначение и результат сдачи")

    def test_schedule_change_and_cancellation_notify_assigned_driver(self):
        self._assign()
        before = TripNotification.objects.filter(recipient=self.driver, trip=self.trip).count()
        self.trip.scheduled_at = timezone.now() + timedelta(days=1)
        self.trip.save(update_fields=["scheduled_at", "updated_at"])
        self.trip.status = LogisticsTrip.STATUS_CANCELED
        self.trip.save(update_fields=["status", "updated_at"])
        self.assertGreaterEqual(TripNotification.objects.filter(recipient=self.driver, trip=self.trip).count(), before + 2)
        self.assertTrue(TripHistory.objects.filter(trip=self.trip, event_type="schedule_changed").exists())
        self.assertTrue(TripHistory.objects.filter(trip=self.trip, event_type="trip_canceled").exists())

    def test_success_requires_gate_photo_and_confirmation(self):
        client = self._advance_to_delivery()
        response = client.post(
            f"/logistics/driver/trips/{self.trip.pk}/",
            {"action": "deliver", "actual_handover_at": timezone.localtime().strftime("%Y-%m-%dT%H:%M")},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(response.status_code, 400)
        self.trip.refresh_from_db()
        self.assertEqual(self.trip.driver_status, LogisticsTrip.DRIVER_STATUS_AWAITING_DELIVERY)

    def test_delivery_screen_opens_logistics_services_before_completion(self):
        client = self._advance_to_delivery()

        response = client.get(f"/logistics/driver/trips/{self.trip.pk}/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="warehouse-services-modal"')
        self.assertContains(response, 'id="driver-trip-service-targets"')
        self.assertContains(response, 'data-order-type="logistics"')

    def test_success_saves_photo_time_and_closes_trip(self):
        client = self._advance_to_delivery()
        self._add_logistics_fact()
        response = client.post(
            f"/logistics/driver/trips/{self.trip.pk}/",
            {
                "action": "deliver",
                "submission_id": "success-1",
                "actual_handover_at": timezone.localtime().strftime("%Y-%m-%dT%H:%M"),
                "result_confirmed": "1",
                "gate_number": "Ворота 12",
                "gate_photo": SimpleUploadedFile("gate.jpg", b"test-image", content_type="image/jpeg"),
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.trip.refresh_from_db()
        self.assertEqual(self.trip.driver_status, LogisticsTrip.DRIVER_STATUS_DELIVERED)
        self.assertEqual(self.trip.status, LogisticsTrip.STATUS_COMPLETED)
        self.assertIsNotNone(self.trip.actual_handover_at)
        self.assertEqual(self.trip.gate_number, "Ворота 12")
        self.assertTrue(TripAttachment.objects.filter(trip=self.trip, attachment_type=TripAttachment.TYPE_GATE).exists())
        self.assertTrue(TripNotification.objects.filter(recipient=self.logistic, trip=self.trip).exists())
        self.assertEqual(ShippingRoutingState.objects.get(shipping_order=self.order).status, ShippingRoutingState.STATUS_DELIVERED)

    def test_success_is_blocked_before_files_and_status_without_logistics_services(self):
        client = self._advance_to_delivery()
        response = client.post(
            f"/logistics/driver/trips/{self.trip.pk}/",
            {
                "action": "deliver",
                "submission_id": "success-without-services",
                "actual_handover_at": timezone.localtime().strftime("%Y-%m-%dT%H:%M"),
                "result_confirmed": "1",
                "gate_photo": SimpleUploadedFile("gate.jpg", b"test-image", content_type="image/jpeg"),
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("услуг", response.json()["message"].lower())
        self.trip.refresh_from_db()
        self.assertEqual(self.trip.status, LogisticsTrip.STATUS_DEPARTED)
        self.assertEqual(self.trip.driver_status, LogisticsTrip.DRIVER_STATUS_AWAITING_DELIVERY)
        self.assertFalse(TripAttachment.objects.filter(trip=self.trip).exists())

    def test_deferred_driver_services_are_saved_with_offline_submission(self):
        client = self._advance_to_delivery()
        service = self._agree_logistics_service()
        response = client.post(
            f"/logistics/driver/trips/{self.trip.pk}/",
            {
                "action": "deliver",
                "submission_id": "offline-services-1",
                "actual_handover_at": timezone.localtime().strftime("%Y-%m-%dT%H:%M"),
                "result_confirmed": "1",
                "logistics_service_facts": json.dumps(
                    [
                        {
                            "client_id": self.agency.pk,
                            "lines": [
                                {
                                    "service_id": service.pk,
                                    "quantity": "1",
                                    "source": "logistics_manual",
                                }
                            ],
                        }
                    ]
                ),
                "gate_photo": SimpleUploadedFile("gate.jpg", b"test-image", content_type="image/jpeg"),
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(response.status_code, 200, response.content)
        fact = WarehouseServiceFact.objects.get(
            client=self.agency,
            order_type="logistics",
            order_id=self.trip.number,
            service=service,
        )
        self.assertEqual(fact.quantity, 1)
        application = BillingApplication.objects.get(
            client=self.agency,
            application_type=BillingApplication.TYPE_LOGISTICS,
            application_id=self.trip.number,
        )
        self.assertEqual(application.charges.count(), 0)

    def test_manual_handover_time_requires_reason(self):
        client = self._advance_to_delivery()
        response = client.post(
            f"/logistics/driver/trips/{self.trip.pk}/",
            {
                "action": "deliver",
                "actual_handover_at": timezone.localtime(timezone.now() - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M"),
                "result_confirmed": "1",
                "gate_photo": SimpleUploadedFile("gate.jpg", b"test-image", content_type="image/jpeg"),
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("причину", response.json()["message"].lower())

    def test_failure_requires_comment_and_photo(self):
        client = self._advance_to_delivery()
        response = client.post(
            f"/logistics/driver/trips/{self.trip.pk}/",
            {
                "action": "fail",
                "reason_code": ProblemTrip.REASON_LATE_SLOT,
                "comment": "",
                "refusal_at": timezone.localtime().strftime("%Y-%m-%dT%H:%M"),
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(ProblemTrip.objects.filter(trip=self.trip).exists())

    def test_failure_creates_problem_and_does_not_close_trip_successfully(self):
        client = self._advance_to_delivery()
        response = client.post(
            f"/logistics/driver/trips/{self.trip.pk}/",
            {
                "action": "fail",
                "submission_id": "failure-1",
                "reason_code": ProblemTrip.REASON_DOCUMENT_ERROR,
                "comment": "На воротах отказали из-за номера документа",
                "refusal_at": timezone.localtime().strftime("%Y-%m-%dT%H:%M"),
                "responsible_party": ProblemTrip.PARTY_CLIENT,
                "evidence_photos": SimpleUploadedFile("failure.jpg", b"test-image", content_type="image/jpeg"),
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.trip.refresh_from_db()
        problem = ProblemTrip.objects.get(trip=self.trip)
        self.assertEqual(self.trip.driver_status, LogisticsTrip.DRIVER_STATUS_PROBLEM)
        self.assertEqual(self.trip.status, LogisticsTrip.STATUS_DEPARTED)
        self.assertEqual(problem.reason_code, ProblemTrip.REASON_DOCUMENT_ERROR)
        self.assertTrue(TripAttachment.objects.filter(trip=self.trip, attachment_type=TripAttachment.TYPE_PROBLEM).exists())
        self.assertEqual(ShippingRoutingState.objects.get(shipping_order=self.order).status, ShippingRoutingState.STATUS_FAILED)
        self.assertTrue(TripNotification.objects.filter(recipient=self.logistic, trip=self.trip, title__icontains="Проблемный").exists())

    def test_problem_trip_accepts_multiple_penalties(self):
        client = self._advance_to_delivery()
        client.post(
            f"/logistics/driver/trips/{self.trip.pk}/",
            {
                "action": "fail",
                "reason_code": ProblemTrip.REASON_OTHER,
                "comment": "Иная подтвержденная причина",
                "refusal_at": timezone.localtime().strftime("%Y-%m-%dT%H:%M"),
                "evidence_photos": SimpleUploadedFile("failure.jpg", b"test-image", content_type="image/jpeg"),
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        problem = ProblemTrip.objects.get(trip=self.trip)
        logistic_client = self._login(self.logistic_user)
        for index in (1, 2):
            response = logistic_client.post(
                f"/logistics/problems/{problem.pk}/",
                {
                    "action": "add_penalty",
                    "marketplace_name": "Ozon",
                    "penalty_type": f"Штраф {index}",
                    "estimated_amount": str(index * 1000),
                },
            )
            self.assertEqual(response.status_code, 302)
        self.assertEqual(TripPenalty.objects.filter(problem=problem).count(), 2)
        self.assertEqual(TripHistory.objects.filter(trip=self.trip, event_type="penalty_added").count(), 2)
        first_penalty = TripPenalty.objects.filter(problem=problem).order_by("id").first()
        logistic_client.post(
            f"/logistics/problems/{problem.pk}/",
            {
                "action": "update_penalty",
                "penalty_id": first_penalty.pk,
                "penalty_status": TripPenalty.STATUS_CONFIRMED,
                "actual_amount": "750",
            },
        )
        first_penalty.refresh_from_db()
        self.assertEqual(first_penalty.status, TripPenalty.STATUS_CONFIRMED)
        self.assertEqual(str(first_penalty.actual_amount), "750.00")
        self.assertTrue(TripHistory.objects.filter(trip=self.trip, event_type="penalty_updated").exists())
        problem_list = logistic_client.get("/logistics/problems/")
        self.assertEqual(problem_list.status_code, 200)
        self.assertContains(problem_list, self.trip.number)
        problem_detail = logistic_client.get(f"/logistics/problems/{problem.pk}/")
        self.assertEqual(problem_detail.status_code, 200)
        self.assertContains(problem_detail, "Штраф 1")

    def test_trip_history_cannot_be_edited_or_deleted(self):
        event = TripHistory.objects.create(trip=self.trip, event_type="test", description="Исходное событие")
        event.description = "Изменено"
        with self.assertRaises(ValueError):
            event.save()
        with self.assertRaises(ValueError):
            event.delete()
