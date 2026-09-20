from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from employees.models import Employee

from .models import WarehouseContainer, WarehouseEvent, WarehouseStockSnapshot


class WarehouseBoxCheckTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="warehouse-box-check-storekeeper",
            password="test-password",
        )
        Employee.objects.create(
            full_name="Кладовщик Общего Склада",
            user=self.user,
            role="storekeeper",
            is_active=True,
        )
        self.client.force_login(self.user)

    def test_storekeeper_opens_isolated_check_without_warehouse_writes(self):
        before = (
            WarehouseContainer.objects.count(),
            WarehouseEvent.objects.count(),
            WarehouseStockSnapshot.objects.count(),
        )

        response = self.client.get(reverse("sklad:warehouse_box_check"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Проверка инвентаризации короба")
        self.assertContains(response, "Общий склад")
        self.assertContains(response, "без записи в складской учёт")
        self.assertContains(response, "data-box-check-client-only")
        self.assertContains(response, "warehouse-box-check")
        self.assertEqual(
            before,
            (
                WarehouseContainer.objects.count(),
                WarehouseEvent.objects.count(),
                WarehouseStockSnapshot.objects.count(),
            ),
        )

    def test_box_check_endpoint_rejects_post(self):
        response = self.client.post(
            reverse("sklad:warehouse_box_check"),
            {"box": "CL-TEST", "marking": "01046000000000121SERIAL"},
        )

        self.assertEqual(response.status_code, 405)
        self.assertFalse(WarehouseContainer.objects.exists())
        self.assertFalse(WarehouseEvent.objects.exists())
        self.assertFalse(WarehouseStockSnapshot.objects.exists())

    def test_picker_cannot_open_general_warehouse_check(self):
        picker = get_user_model().objects.create_user(
            username="warehouse-box-check-picker",
            password="test-password",
        )
        Employee.objects.create(
            full_name="Сборщик Без Доступа",
            user=picker,
            role="picker",
            is_active=True,
        )
        self.client.force_login(picker)

        response = self.client.get(reverse("sklad:warehouse_box_check"))

        self.assertEqual(response.status_code, 403)
