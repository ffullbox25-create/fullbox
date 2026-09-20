from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone

from accountant.models import ClientLifecycle
from billing.external_trip import sync_completed_external_trip
from billing.models import (
    ApplicationCharge,
    BillingApplication,
    BillingService,
    ClientLogisticsTariff,
    ClientLogisticsTariffItem,
    ClientTariffItem,
    ClientTariffVersion,
    TariffCategory,
    TariffUnit,
)
from billing.service_catalog import ensure_service_catalog
from billing.statuses import BillingStatus
from employees.models import Employee
from shipping.models import ShippingOrder
from sku.models import Agency
from todo.models import Task

from .driver_workflow import change_driver_status, confirm_delivery
from .external_trip import ExternalTripForm, create_external_trip, update_external_trip
from .models import LogisticsExternalTrip, LogisticsTrip, LogisticsTripOrder, ShippingRoutingState, TripNotification
from .trip_planning import build_trip_planning_context


class ExternalTripFeatureTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.logistician_user = user_model.objects.create_user(username="external_logistician", password="pwd")
        self.logistician = Employee.objects.create(
            user=self.logistician_user,
            full_name="Логист Внешних Рейсов",
            role="logistician",
            is_active=True,
        )
        self.driver_user = user_model.objects.create_user(username="external_driver", password="pwd")
        self.driver = Employee.objects.create(
            user=self.driver_user,
            full_name="Водитель Внешнего Рейса",
            role="driver",
            phone="+79990001122",
            is_active=True,
        )
        self.agency = Agency.objects.create(agn_name="Клиент внешней перевозки")
        self.schedule = timezone.now() + timedelta(days=1)

    def _data(self, **overrides):
        data = {
            "client": self.agency,
            "pickup_address": "Москва, Варшавское шоссе, 1",
            "delivery_address": "Тула, Советская улица, 10",
            "scheduled_at": self.schedule,
            "driver": self.driver,
            "assignment_mode": "confirmed",
            "cargo_description": "10 коробов с одеждой",
            "pickup_contact": "Анна",
            "pickup_phone": "+79990000001",
            "delivery_contact": "Иван",
            "delivery_phone": "+79990000002",
            "vehicle_type": LogisticsTrip.VEHICLE_HIRED,
            "vehicle_name": "Газель",
            "vehicle_number": "А123АА77",
            "route_comment": "Позвонить за час",
        }
        data.update(overrides)
        return data

    def _create_trip(self, **overrides):
        return create_external_trip(
            cleaned_data=self._data(**overrides),
            actor=self.logistician,
            user=self.logistician_user,
        )

    def test_create_external_trip_has_no_internal_or_warehouse_side_effects(self):
        task_count = Task.objects.count()
        trip = self._create_trip()

        self.assertEqual(trip.trip_kind, LogisticsTrip.KIND_EXTERNAL)
        self.assertEqual(trip.status, LogisticsTrip.STATUS_PLANNED)
        self.assertEqual(trip.assigned_driver, self.driver)
        self.assertEqual(trip.orders.count(), 0)
        self.assertEqual(ShippingRoutingState.objects.count(), 0)
        self.assertEqual(Task.objects.count(), task_count)
        self.assertEqual(trip.external_details.client, self.agency)
        self.assertEqual(trip.external_details.pickup_address, "Москва, Варшавское шоссе, 1")
        notification = TripNotification.objects.get(recipient=self.driver, trip=trip)
        self.assertIn("Маршрут:", notification.text)
        self.assertNotIn("Маркетплейс:", notification.text)

    def test_external_trip_rejects_internal_shipping_order_link(self):
        trip = self._create_trip()
        order = ShippingOrder.objects.create(
            number="EXT-LINK-1",
            agency=self.agency,
            created_by=self.logistician_user,
        )
        with self.assertRaises(ValidationError):
            LogisticsTripOrder.objects.create(trip=trip, shipping_order=order)
        self.assertFalse(LogisticsTripOrder.objects.filter(trip=trip).exists())

    def test_preliminary_external_trip_is_visible_but_not_startable(self):
        trip = self._create_trip(assignment_mode="preliminary")
        self.assertEqual(trip.status, LogisticsTrip.STATUS_DRAFT)
        self.assertEqual(trip.driver_status, LogisticsTrip.DRIVER_STATUS_PRELIMINARY)
        with self.assertRaises(ValidationError):
            change_driver_status(
                trip=trip,
                employee=self.driver,
                role="driver",
                action="accept",
                user=self.driver_user,
            )

    def test_planner_and_driver_mobile_card_show_external_route(self):
        trip = self._create_trip()
        week_start = timezone.localtime(self.schedule).date() - timedelta(days=timezone.localtime(self.schedule).weekday())
        context = build_trip_planning_context(role="logistician", week_value=week_start.isoformat())
        rows = [row for day in context["days"] for row in day["rows"]]
        card = next(row for row in rows if row["trip"].pk == trip.pk)
        self.assertTrue(card["is_external"])
        self.assertIn("Москва", card["route_label"])
        self.assertEqual(card["detail_url"], reverse("logistics:external-trip-detail", args=[trip.pk]))

        change_driver_status(trip=trip, employee=self.driver, role="driver", action="accept", user=self.driver_user)
        change_driver_status(trip=trip, employee=self.driver, role="driver", action="depart", user=self.driver_user)
        self.client.force_login(self.driver_user)
        response = self.client.get(reverse("logistics:driver-trip-detail", args=[trip.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Внешний рейс")
        self.assertContains(response, "Точка А · забрать")
        self.assertContains(response, "Прибыл в точку Б")

    def test_logistician_can_open_creation_form(self):
        self.client.force_login(self.logistician_user)
        response = self.client.get(reverse("logistics:external-trip-create"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Создать внешний рейс")
        self.assertContains(response, "Точка А")
        self.assertEqual(response.context["form"].fields["assignment_mode"].initial, "confirmed")

    def test_external_trip_appears_in_all_trips_and_legacy_detail_redirects(self):
        trip = self._create_trip()
        self.client.force_login(self.logistician_user)
        response = self.client.get(reverse("logistics:trip-list"))
        self.assertEqual(response.status_code, 200)
        external_row = next(row for row in response.context["trips"] if row["trip"].pk == trip.pk)
        self.assertTrue(external_row["is_external"])
        self.assertEqual(external_row["order_count"], "Внешний")
        self.assertIn("Москва", external_row["destination_preview"])

        redirect_response = self.client.get(reverse("logistics:trip-detail", args=[trip.pk]))
        self.assertRedirects(
            redirect_response,
            reverse("logistics:external-trip-detail", args=[trip.pk]),
            fetch_redirect_response=False,
        )

    def test_route_update_notifies_same_driver(self):
        trip = self._create_trip()
        before = TripNotification.objects.filter(trip=trip, recipient=self.driver).count()
        updated_data = self._data(delivery_address="Калуга, улица Ленина, 20")
        updated = update_external_trip(
            trip=trip,
            cleaned_data=updated_data,
            actor=self.logistician,
            user=self.logistician_user,
        )
        self.assertEqual(updated.external_details.delivery_address, "Калуга, улица Ленина, 20")
        self.assertEqual(TripNotification.objects.filter(trip=trip, recipient=self.driver).count(), before + 1)
        self.assertIn(
            "Калуга",
            TripNotification.objects.filter(trip=trip, recipient=self.driver).order_by("-id").first().text,
        )


class ExternalTripBillingTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.logistician_user = user_model.objects.create_user(username="billing_logistician", password="pwd")
        self.logistician = Employee.objects.create(
            user=self.logistician_user,
            full_name="Логист Billing",
            role="logistician",
            is_active=True,
        )
        self.driver_user = user_model.objects.create_user(username="billing_driver", password="pwd")
        self.driver = Employee.objects.create(
            user=self.driver_user,
            full_name="Водитель Billing",
            role="driver",
            is_active=True,
        )
        self.agency = Agency.objects.create(agn_name="Клиент Billing внешнего рейса")
        self.schedule = timezone.now() + timedelta(hours=2)

    def _trip(self):
        return create_external_trip(
            cleaned_data={
                "client": self.agency,
                "pickup_address": "Точка А",
                "delivery_address": "Точка Б",
                "scheduled_at": self.schedule,
                "driver": self.driver,
                "assignment_mode": "confirmed",
                "cargo_description": "Палета",
                "vehicle_type": "",
                "vehicle_name": "",
                "vehicle_number": "",
                "route_comment": "",
            },
            actor=self.logistician,
            user=self.logistician_user,
        )

    def _publish_trip_tariff(self, price=Decimal("12500")):
        ClientLifecycle.objects.create(agency=self.agency, status=ClientLifecycle.STATUS_ACTIVE)
        services = ensure_service_catalog()
        service = services["logistics_external_trip"]
        category, _ = TariffCategory.objects.get_or_create(
            code="logistics",
            defaults={"name": "Логистика", "sort_order": 70, "is_active": True},
        )
        if service.category_id != category.id:
            service.category = category
            service.save(update_fields=["category", "updated_at"])
        unit, _ = TariffUnit.objects.get_or_create(
            code="trip",
            defaults={"name": "Рейс", "short_name": "рейс", "is_active": True},
        )
        version = ClientTariffVersion.objects.create(
            client=self.agency,
            name="Тариф на внешний рейс",
            version_number=1,
            status=ClientTariffVersion.STATUS_ACTIVE,
            valid_from=timezone.localdate() - timedelta(days=1),
        )
        ClientTariffItem.objects.create(
            tariff_version=version,
            category=category,
            service=service,
            service_name=service.name,
            unit=unit,
            price=price,
        )
        return version

    def _publish_logistics_tariff(self, price=Decimal("7800.00")):
        ClientLifecycle.objects.get_or_create(agency=self.agency, defaults={"status": ClientLifecycle.STATUS_ACTIVE})
        tariff = ClientLogisticsTariff.objects.create(
            client=self.agency,
            name="Логистический прайс",
            version_number=1,
            status=ClientLogisticsTariff.STATUS_ACTIVE,
            valid_from=timezone.localdate() - timedelta(days=1),
        )
        ClientLogisticsTariffItem.objects.create(
            tariff=tariff,
            marketplace="other",
            warehouse_name="Точка Б",
            price_per_pallet=price,
        )
        return tariff

    def _complete_trip(self, trip):
        LogisticsTrip.objects.filter(pk=trip.pk).update(
            status=LogisticsTrip.STATUS_COMPLETED,
            driver_status=LogisticsTrip.DRIVER_STATUS_DELIVERED,
            closed_at=timezone.now(),
        )
        trip.refresh_from_db()
        return trip

    def test_completed_external_trip_creates_one_priced_charge_idempotently(self):
        self._publish_trip_tariff()
        trip = self._complete_trip(self._trip())

        first = sync_completed_external_trip(trip, user=self.logistician_user)
        second = sync_completed_external_trip(trip.pk, user=self.logistician_user)

        self.assertEqual(first.pk, second.pk)
        self.assertEqual(BillingApplication.objects.filter(application_type=BillingApplication.TYPE_LOGISTICS).count(), 1)
        self.assertEqual(ApplicationCharge.objects.filter(application=first).count(), 1)
        charge = ApplicationCharge.objects.get(application=first)
        self.assertEqual(charge.service.code, "logistics_external_trip")
        self.assertEqual(charge.quantity, Decimal("1"))
        self.assertEqual(charge.tariff, Decimal("12500"))
        self.assertEqual(charge.source_type, ApplicationCharge.SOURCE_LOGISTICS_TRIP)

    def test_completed_external_trip_uses_client_logistics_tariff_when_direction_matches(self):
        self._publish_trip_tariff(price=Decimal("12500"))
        logistics_tariff = self._publish_logistics_tariff(price=Decimal("7800.00"))
        trip = self._complete_trip(self._trip())

        application = sync_completed_external_trip(trip, user=self.logistician_user)

        charge = application.charges.get()
        self.assertEqual(charge.service.code, "logistics_external_trip")
        self.assertEqual(charge.tariff, Decimal("7800.00"))
        self.assertEqual(charge.client_logistics_tariff_id, logistics_tariff.id)
        self.assertIsNotNone(charge.client_logistics_tariff_item_id)
        self.assertIsNone(charge.client_tariff_item_id)

    def test_missing_tariff_does_not_block_completion_and_is_visible_in_billing(self):
        trip = self._complete_trip(self._trip())
        application = sync_completed_external_trip(trip, user=self.logistician_user)

        self.assertIsNotNone(application)
        self.assertEqual(application.charges.count(), 0)
        application.refresh_from_db()
        self.assertEqual(application.billing_status, BillingStatus.CALCULATION_DRAFT)
        self.assertIn("configuration_error", application.source_payload.get("billing", {}))

    def test_problem_or_canceled_external_trip_is_not_billed(self):
        trip = self._trip()
        LogisticsTrip.objects.filter(pk=trip.pk).update(
            status=LogisticsTrip.STATUS_CANCELED,
            driver_status=LogisticsTrip.DRIVER_STATUS_PROBLEM,
        )
        self.assertIsNone(sync_completed_external_trip(trip.pk, user=self.logistician_user))
        self.assertFalse(BillingApplication.objects.filter(application_type=BillingApplication.TYPE_LOGISTICS).exists())

    def test_driver_completion_schedules_billing_after_commit(self):
        self._publish_trip_tariff(price=Decimal("9000"))
        trip = self._trip()
        change_driver_status(trip=trip, employee=self.driver, role="driver", action="accept", user=self.driver_user)
        change_driver_status(trip=trip, employee=self.driver, role="driver", action="depart", user=self.driver_user)
        change_driver_status(trip=trip, employee=self.driver, role="driver", action="arrive", user=self.driver_user)
        request = RequestFactory().post(
            reverse("logistics:driver-trip-detail", args=[trip.pk]),
            data={
                "submission_id": "external-delivery-1",
                "result_confirmed": "1",
                "actual_handover_at": timezone.localtime().strftime("%Y-%m-%dT%H:%M"),
                "gate_photo": SimpleUploadedFile("delivery.jpg", b"test-image", content_type="image/jpeg"),
                "driver_comment": "Груз передан",
            },
        )
        request.user = self.driver_user

        with self.captureOnCommitCallbacks(execute=True):
            completed = confirm_delivery(
                request=request,
                trip=trip,
                employee=self.driver,
                role="driver",
            )

        self.assertEqual(completed.status, LogisticsTrip.STATUS_COMPLETED)
        self.assertEqual(completed.driver_status, LogisticsTrip.DRIVER_STATUS_DELIVERED)
        application = BillingApplication.objects.get(application_type=BillingApplication.TYPE_LOGISTICS)
        self.assertEqual(application.charges.get().tariff, Decimal("9000"))
