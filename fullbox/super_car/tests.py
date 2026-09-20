from types import SimpleNamespace
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase

from employees.models import Employee


class SuperCarAccessTests(TestCase):
    def _login_as_role(self, *, username: str, role: str, full_name: str) -> None:
        user = get_user_model().objects.create_user(username=username, password="testpass123")
        employee = Employee.objects.create(
            full_name=full_name,
            role=role,
            user=user,
            is_active=True,
        )
        self.client.force_login(user)
        session = self.client.session
        session["employee_id"] = employee.id
        session["employee_name"] = employee.full_name
        session["employee_role"] = employee.role
        session.save()

    def test_super_car_dashboard_allows_super_car_role(self):
        self._login_as_role(
            username="super-car-test",
            role="super_car",
            full_name="Super Car Test",
        )

        with mock.patch("super_car.views.build_dashboard_context", return_value={}):
            response = self.client.get("/super-car/")

        self.assertEqual(response.status_code, 200)

    def test_super_car_dashboard_forbids_other_roles(self):
        self._login_as_role(
            username="manager-test",
            role="manager",
            full_name="Manager Test",
        )

        response = self.client.get("/super-car/")

        self.assertEqual(response.status_code, 403)

    def test_super_car_dashboard_shows_root_sections(self):
        self._login_as_role(
            username="super-car-root",
            role="super_car",
            full_name="Super Car Root",
        )

        response = self.client.get("/super-car/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "\u0420\u0430\u0437\u043c\u0435\u0449\u0435\u043d\u0438\u0435")
        self.assertContains(response, "\u0421\u0431\u043e\u0440\u043a\u0430")
        self.assertContains(response, "\u0414\u043e\u043f\u044b")
        self.assertContains(response, "\u041f\u0430\u043b\u043b\u0435\u0442\u044b \u043d\u0430 \u0445\u0440\u0430\u043d\u0435\u043d\u0438\u0435")
        self.assertContains(response, "\u041f\u043e\u0434\u0431\u043e\u0440 \u0438 \u043f\u043e\u0434\u0430\u0447\u0430 \u0433\u0440\u0443\u0437\u0430")
        self.assertContains(response, "\u0411\u044b\u0441\u0442\u0440\u044b\u0435 \u0434\u0435\u0439\u0441\u0442\u0432\u0438\u044f")

    def test_super_car_dashboard_shows_placement_submenu(self):
        self._login_as_role(
            username="super-car-placement",
            role="super_car",
            full_name="Super Car Placement",
        )

        response = self.client.get("/super-car/?mobile_section=placement")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "\u0421 \u043f\u0440\u0438\u0435\u043c\u043a\u0438")
        self.assertContains(response, "\u0421 \u043e\u0431\u0440\u0430\u0431\u043e\u0442\u043a\u0438")

    def test_super_car_dashboard_shows_extras_locate_action(self):
        self._login_as_role(
            username="super-car-extras",
            role="super_car",
            full_name="Super Car Extras",
        )

        response = self.client.get("/super-car/?mobile_section=extras")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "\u0423\u043a\u0430\u0437\u0430\u0442\u044c \u043c\u0435\u0441\u0442\u043e")

    def test_super_car_shipping_category_redirects_to_shared_otg_queue(self):
        self._login_as_role(
            username="super-car-shipping",
            role="super_car",
            full_name="Super Car Shipping",
        )

        response = self.client.get(
            "/super-car/",
            {
                "mobile_section": "assembly",
                "mobile_category": "assembly_shipping",
            },
        )

        self.assertRedirects(
            response,
            "/otg-reachtruck/",
            fetch_redirect_response=False,
        )

    def test_super_car_can_take_task_from_shared_otg_queue(self):
        self._login_as_role(
            username="super-car-otg",
            role="super_car",
            full_name="Super Car OTG",
        )
        group = {
            "legacy_order_ids": ["MOVE-191"],
            "tasks": [],
        }

        with (
            mock.patch(
                "otg_reachtruck.views._active_otg_groups",
                return_value={"OTG-000191": group},
            ),
            mock.patch(
                "otg_reachtruck.views._request_context",
                return_value={},
            ),
            mock.patch(
                "otg_reachtruck.views.take_otg_move_request",
                return_value=SimpleNamespace(
                    ok=True,
                    completed=False,
                    message="Заявка взята в работу.",
                ),
            ) as take_request,
        ):
            response = self.client.post(
                "/otg-reachtruck/",
                {
                    "action": "take",
                    "request": "OTG-000191",
                },
            )

        self.assertEqual(response.status_code, 200)
        take_request.assert_called_once()
