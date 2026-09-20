from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from employees.access import resolve_cabinet_url
from employees.models import Employee


@override_settings(ROOT_URLCONF="fullbox.test_picker_fbs_redirect_urls")
class PickerFbsRedirectTests(TestCase):
    def setUp(self):
        self.password = "test-picker-password"
        self.user = get_user_model().objects.create_user(
            username="picker_mobile_redirect",
            password=self.password,
        )
        Employee.objects.create(
            user=self.user,
            full_name="Опря Сергей",
            role="picker",
            is_active=True,
        )

    def test_picker_cabinet_url_is_fbs_mobile_picking(self):
        self.assertEqual(resolve_cabinet_url("picker"), "/fbs/tsd/picking/")
        self.assertEqual(resolve_cabinet_url("manager"), "/team-manager/")

    def test_picker_login_redirects_to_fbs_mobile_picking(self):
        response = self.client.post(
            "/login/",
            {"username": self.user.username, "password": self.password},
        )

        self.assertRedirects(
            response,
            "/fbs/tsd/picking/",
            fetch_redirect_response=False,
        )

    def test_legacy_picker_cabinet_redirects_to_fbs_mobile_picking(self):
        self.client.force_login(self.user)

        response = self.client.get("/cabinet/picker/")

        self.assertRedirects(
            response,
            "/fbs/tsd/picking/",
            fetch_redirect_response=False,
        )
