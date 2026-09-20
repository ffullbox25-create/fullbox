from datetime import date, datetime

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from employees.models import Employee
from head_manager.models import Carrier
from shipping.models import ShippingOrder
from sku.models import Agency

from .models import LogisticsTrip, LogisticsTripOrder
from .trip_planning import normalize_week_start


class TripWeekPlanningTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.user = user_model.objects.create_user(username="trip_planner", password="pwd")
        self.employee = Employee.objects.create(
            full_name="Логист Планировщик",
            role="logistician",
            user=self.user,
            is_active=True,
        )
        self.storekeeper_user = user_model.objects.create_user(username="trip_planner_storekeeper", password="pwd")
        Employee.objects.create(
            full_name="Кладовщик Планировщик",
            role="storekeeper",
            user=self.storekeeper_user,
            is_active=True,
        )
        self.agency = Agency.objects.create(agn_name="Клиент недельного плана")
        self.carrier = Carrier.objects.create(name="Перевозчик План", short_name="План-Транс", is_active=True)

    def _order(self, number: str, *, slot_date: date | None, destination: str, boxes: int = 4) -> ShippingOrder:
        return ShippingOrder.objects.create(
            number=number,
            agency=self.agency,
            created_by=self.user,
            status=ShippingOrder.STATUS_PACKED,
            slot_date=slot_date,
            planned_ship_date=slot_date,
            destination_warehouse=destination,
            expected_boxes=boxes,
        )

    def _trip(
        self,
        number: str,
        *,
        trip_date: date | None = None,
        status: str = LogisticsTrip.STATUS_DRAFT,
        scheduled_at=None,
        driver_status: str = LogisticsTrip.DRIVER_STATUS_UNASSIGNED,
    ) -> LogisticsTrip:
        return LogisticsTrip.objects.create(
            number=number,
            trip_date=trip_date,
            scheduled_at=scheduled_at,
            status=status,
            driver_status=driver_status,
            carrier=self.carrier,
            vehicle_number="А123ВС777",
            driver_name="Иван Петров",
            assigned_logistician=self.employee,
            created_by=self.user,
        )

    def test_week_value_is_normalized_to_monday(self):
        self.assertEqual(
            normalize_week_start("2026-08-05", today=date(2026, 8, 2)),
            date(2026, 8, 3),
        )
        self.assertEqual(
            normalize_week_start(None, today=date(2026, 8, 2)),
            date(2026, 8, 3),
        )
        self.assertEqual(
            normalize_week_start("not-a-date", today=date(2026, 8, 2)),
            date(2026, 7, 27),
        )

    def test_planning_page_groups_trip_and_renders_card_fields(self):
        trip = self._trip("73_RS", trip_date=date(2026, 8, 4))
        order = self._order("OTG-PLAN-1", slot_date=date(2026, 8, 4), destination="Ozon Софьино", boxes=7)
        LogisticsTripOrder.objects.create(trip=trip, shipping_order=order, delivery_sequence=1, loading_sequence=1)
        self.client.force_login(self.user)

        response = self.client.get(reverse("logistics:trip-planning"), {"week": "2026-08-05"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["week_start"], date(2026, 8, 3))
        self.assertEqual([day["full_name"] for day in response.context["days"]], [
            "Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье",
        ])
        tuesday_rows = response.context["days"][1]["rows"]
        self.assertEqual(len(tuesday_rows), 1)
        self.assertEqual(tuesday_rows[0]["number"], "73_RS")
        self.assertEqual(tuesday_rows[0]["route_label"], "Ozon Софьино")
        self.assertEqual(tuesday_rows[0]["carrier_label"], self.carrier.short_name)
        self.assertEqual(tuesday_rows[0]["driver_label"], "Иван Петров")
        self.assertEqual(tuesday_rows[0]["box_count"], 7)
        self.assertContains(response, "Планирование")
        self.assertContains(response, "Перевозчик:")
        self.assertContains(response, f'href="{reverse("logistics:trip-detail", args=[trip.pk])}"')

    def test_scheduled_time_has_priority_and_preliminary_status_is_visible(self):
        scheduled_at = timezone.make_aware(datetime(2026, 8, 5, 9, 30))
        trip = self._trip(
            "74_RS",
            trip_date=date(2026, 8, 4),
            scheduled_at=scheduled_at,
            driver_status=LogisticsTrip.DRIVER_STATUS_PRELIMINARY,
        )
        order = self._order("OTG-PLAN-2", slot_date=date(2026, 8, 4), destination="Wildberries Коледино")
        LogisticsTripOrder.objects.create(trip=trip, shipping_order=order)
        self.client.force_login(self.user)

        response = self.client.get(reverse("logistics:trip-planning"), {"week": "2026-08-03"})

        wednesday_rows = response.context["days"][2]["rows"]
        self.assertEqual(len(wednesday_rows), 1)
        self.assertEqual(wednesday_rows[0]["time_label"], "09:30")
        self.assertEqual(wednesday_rows[0]["assignment_class"], "preliminary")
        self.assertContains(response, "Предварительный рейс")

    def test_single_linked_order_date_places_undated_draft_in_week(self):
        trip = self._trip("DRAFT-ABCDEF123456")
        order = self._order("OTG-PLAN-3", slot_date=date(2026, 8, 6), destination="Казань РФЦ")
        LogisticsTripOrder.objects.create(trip=trip, shipping_order=order)
        self.client.force_login(self.user)

        response = self.client.get(reverse("logistics:trip-planning"), {"week": "2026-08-03"})

        thursday_rows = response.context["days"][3]["rows"]
        self.assertEqual(len(thursday_rows), 1)
        self.assertEqual(thursday_rows[0]["number"], "Черновик")
        self.assertEqual(thursday_rows[0]["date_source"], "orders")
        self.assertContains(response, "По заявкам")
        self.assertEqual(response.context["undated_rows"], [])

    def test_mixed_linked_dates_stay_in_undated_block_and_storekeeper_can_read(self):
        trip = self._trip("DRAFT-123456ABCDEF")
        first = self._order("OTG-PLAN-4", slot_date=date(2026, 8, 5), destination="Тула")
        second = self._order("OTG-PLAN-5", slot_date=date(2026, 8, 6), destination="Воронеж")
        LogisticsTripOrder.objects.create(trip=trip, shipping_order=first, delivery_sequence=1)
        LogisticsTripOrder.objects.create(trip=trip, shipping_order=second, delivery_sequence=2)
        self.client.force_login(self.storekeeper_user)

        response = self.client.get(reverse("logistics:trip-planning"), {"week": "2026-08-03"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["undated_rows"]), 1)
        self.assertEqual(response.context["undated_rows"][0]["number"], "Черновик")
        self.assertContains(response, "Без даты")
        self.assertContains(response, "Тула · Воронеж")
