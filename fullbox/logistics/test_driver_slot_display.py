from datetime import date, time

from django.contrib.auth import get_user_model
from django.test import TestCase

from employees.models import Employee
from logistics.driver_workflow import trip_summary
from logistics.models import LogisticsTrip, LogisticsTripOrder
from shipping.models import ShippingOrder
from sku.models import Agency, Market


class DriverSlotDisplayTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.driver_user = user_model.objects.create_user(username="driver_slot_display", password="pwd")
        self.driver = Employee.objects.create(
            full_name="Водитель Слотов",
            role="driver",
            user=self.driver_user,
            is_active=True,
        )
        self.logistic_user = user_model.objects.create_user(username="driver_slot_logistic", password="pwd")
        self.logistic = Employee.objects.create(
            full_name="Логист Слотов",
            role="logistician",
            user=self.logistic_user,
            is_active=True,
        )
        self.agency = Agency.objects.create(agn_name="Клиент слотов водителя")
        self.market = Market.objects.create(id=9917, name="Ozon слоты")
        self.trip = LogisticsTrip.objects.create(
            number="SLOT-72_RS",
            trip_date=date(2026, 8, 3),
            status=LogisticsTrip.STATUS_PLANNED,
            driver_status=LogisticsTrip.DRIVER_STATUS_PRELIMINARY,
            assigned_driver=self.driver,
            assigned_logistician=self.logistic,
        )

    def _add_order(self, number: str, *, slot_date, slot_time, sequence: int):
        order = ShippingOrder.objects.create(
            number=number,
            agency=self.agency,
            marketplace=self.market,
            status=ShippingOrder.STATUS_PACKED,
            destination_warehouse=f"Склад {sequence}",
            expected_boxes=sequence,
            slot_date=slot_date,
            slot_time=slot_time,
        )
        LogisticsTripOrder.objects.create(
            trip=self.trip,
            shipping_order=order,
            loading_sequence=sequence,
            delivery_sequence=sequence,
        )
        return order

    def test_driver_list_shows_common_delivery_slot(self):
        self._add_order("OTG-SLOT-1", slot_date=date(2026, 8, 3), slot_time=time(18, 0), sequence=1)
        self._add_order("OTG-SLOT-2", slot_date=date(2026, 8, 3), slot_time=time(18, 0), sequence=2)
        self.client.force_login(self.driver_user)

        response = self.client.get("/logistics/driver/trips/?bucket=new")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Слот сдачи")
        self.assertContains(response, "03.08.2026 · 18:00", count=1)

    def test_driver_detail_shows_summary_and_each_order_slot(self):
        self._add_order("OTG-SLOT-3", slot_date=date(2026, 8, 3), slot_time=time(18, 0), sequence=1)
        self._add_order("OTG-SLOT-4", slot_date=date(2026, 8, 4), slot_time=time(10, 30), sequence=2)
        self.client.force_login(self.driver_user)

        response = self.client.get(f"/logistics/driver/trips/{self.trip.pk}/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "03.08.2026 · 18:00; 04.08.2026 · 10:30")
        self.assertContains(response, "03.08.2026 · 18:00")
        self.assertContains(response, "04.08.2026 · 10:30")

    def test_missing_slot_is_explicit(self):
        self._add_order("OTG-SLOT-5", slot_date=None, slot_time=None, sequence=1)

        summary = trip_summary(self.trip)

        self.assertEqual(summary["delivery_slots_label"], "Не указан")
        self.assertEqual(summary["orders"][0]["slot_label"], "Не указан")
