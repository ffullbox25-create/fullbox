import time

from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import make_password
from django.test import TestCase, override_settings

from .badges import issue_badge, revoke_badge, set_qr_login_enabled, token_lookup
from .models import Employee, EmployeeBadgeEvent


@override_settings(
    ALLOWED_HOSTS=["testserver"],
    FBS_QR_LOGIN_ALLOWED_IPS=("192.168.1.1",),
)
class EmployeeQrLoginTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="qr_storekeeper",
            password="Strong-pass-42!",
        )
        self.employee = Employee.objects.create(
            full_name="Кладовщик QR",
            user=self.user,
            role="storekeeper",
            is_active=True,
        )
        self.badge, self.raw_token = issue_badge(employee=self.employee, actor=None)
        set_qr_login_enabled(employee=self.employee, enabled=True, actor=None)

    def _post(self, token=None, **extra):
        headers = {"HTTP_X_REAL_IP": "192.168.1.1"}
        headers.update(extra)
        return self.client.post(
            "/login/qr/",
            {"badge_token": token if token is not None else self.raw_token},
            **headers,
        )

    def test_new_badge_uses_scanner_safe_uppercase_hex_token(self):
        self.assertRegex(self.raw_token, r"^FBX-EMP-[A-F0-9]{48}$")

    def test_legacy_mixed_case_badge_still_works_with_exact_value(self):
        legacy_token = "FBX-EMP-AbCdEfGhIjKlMnOpQrStUvWxYz_12345"
        self.badge.token_lookup = token_lookup(legacy_token)
        self.badge.token_digest = make_password(legacy_token)
        self.badge.save(update_fields=("token_lookup", "token_digest"))

        response = self._post(
            legacy_token,
            HTTP_USER_AGENT="FullboxDesktop/1.0.17",
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/sklad/")

    def test_fullbox_desktop_login_loads_direct_scanner_bridge(self):
        response = self.client.get(
            "/login/",
            HTTP_USER_AGENT="Mozilla/5.0 FullboxDesktop/1.0.17",
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'src="/static/fbs/tsd.js?v=20260829-1"')
        self.assertContains(response, "data-scan-autosubmit")

    def test_regular_browser_login_does_not_load_direct_scanner_bridge(self):
        response = self.client.get(
            "/login/",
            HTTP_USER_AGENT="Mozilla/5.0 Chrome/128.0",
        )

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "/static/fbs/tsd.js")
        self.assertNotContains(response, 'id="badge-token"')

    def test_warehouse_badge_login_works_in_fullbox_desktop(self):
        response = self._post(HTTP_USER_AGENT="FullboxDesktop/1.0.17")

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/sklad/")
        self.assertEqual(int(self.client.session["_auth_user_id"]), self.user.id)
        self.assertEqual(self.client.session["employee_id"], self.employee.id)
        self.badge.refresh_from_db()
        self.assertEqual(self.badge.last_used_ip, "192.168.1.1")

    def test_spoofed_forwarded_for_does_not_bypass_real_ip(self):
        response = self._post(
            HTTP_X_REAL_IP="203.0.113.44",
            HTTP_X_FORWARDED_FOR="192.168.1.1",
        )

        self.assertEqual(response.status_code, 401)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_remote_addr_is_used_when_x_real_ip_is_missing(self):
        response = self.client.post(
            "/login/qr/",
            {"badge_token": self.raw_token},
            REMOTE_ADDR="192.168.1.1",
            HTTP_USER_AGENT="FullboxDesktop/1.0.17",
        )

        self.assertEqual(response.status_code, 302)

    def test_disabled_access_rejects_valid_badge(self):
        set_qr_login_enabled(employee=self.employee, enabled=False, actor=None)

        response = self._post()

        self.assertEqual(response.status_code, 401)

    def test_revoked_badge_is_rejected_and_access_is_disabled(self):
        revoke_badge(employee=self.employee, actor=None)

        response = self._post()

        self.assertEqual(response.status_code, 401)
        self.employee.refresh_from_db()
        self.assertFalse(self.employee.qr_login_enabled)

    def test_inactive_employee_or_user_is_rejected(self):
        self.employee.is_active = False
        self.employee.save(update_fields=("is_active",))
        response = self._post()
        self.assertEqual(response.status_code, 401)

        self.employee.is_active = True
        self.employee.save(update_fields=("is_active",))
        self.user.is_active = False
        self.user.save(update_fields=("is_active",))
        response = self._post()
        self.assertEqual(response.status_code, 401)

    def test_processing_head_role_can_login_with_qr(self):
        self.employee.role = "processing_head"
        self.employee.save(update_fields=("role",))

        response = self._post(
            HTTP_USER_AGENT="Mozilla/5.0 FullboxDesktop/1.0.17"
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/processing-head/")
        self.assertEqual(int(self.client.session["_auth_user_id"]), self.user.id)

    def test_non_warehouse_role_is_rejected_even_with_enabled_flag(self):
        self.employee.role = "accountant"
        self.employee.qr_login_enabled = True
        self.employee.save(update_fields=("role", "qr_login_enabled"))

        response = self._post()

        self.assertEqual(response.status_code, 401)

    def test_eleventh_attempt_in_minute_is_rate_limited(self):
        for _attempt in range(10):
            response = self._post("FBX-EMP-wrong-wrong-wrong-wrong")
            self.assertEqual(response.status_code, 401)

        response = self._post()

        self.assertEqual(response.status_code, 401)
        self.assertTrue(
            EmployeeBadgeEvent.objects.filter(
                event_type=EmployeeBadgeEvent.EVENT_RATE_LIMITED,
                ip_address="192.168.1.1",
            ).exists()
        )

    def test_raw_token_never_appears_in_event_data(self):
        self._post()
        event_text = "\n".join(
            str(value)
            for value in EmployeeBadgeEvent.objects.values_list(
                "event_type", "details", "ip_address"
            )
        )
        self.assertNotIn(self.raw_token, event_text)

    def test_normal_password_login_remains_available(self):
        response = self.client.post(
            "/login/",
            {"username": self.user.username, "password": "Strong-pass-42!"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/sklad/")


@override_settings(ALLOWED_HOSTS=["testserver"])
class PfbsTsdQrLoginTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="pfbs_tsd_picker",
            password="Strong-pass-42!",
        )
        self.employee = Employee.objects.create(
            full_name="Подборщик PFBS",
            user=self.user,
            role="picker",
            is_active=True,
        )
        self.badge, self.raw_token = issue_badge(employee=self.employee, actor=None)
        set_qr_login_enabled(employee=self.employee, enabled=True, actor=None)

    def test_short_tsd_entry_is_scanner_only(self):
        response = self.client.get(
            "/tsd/",
            HTTP_USER_AGENT="Mozilla/5.0 Chrome/128.0",
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Вход в ТСД")
        self.assertContains(response, "Подборщик PFBS")
        self.assertContains(response, 'action="/tsd/"')
        self.assertContains(response, 'id="badge-token"')
        self.assertContains(response, 'src="/static/fbs/tsd.js?v=20260829-1"')
        self.assertNotContains(response, 'id="username"')
        self.assertNotContains(response, 'id="password"')

    def test_picker_badge_logs_in_from_regular_tsd_browser(self):
        response = self.client.post(
            "/tsd/",
            {"badge_token": self.raw_token},
            HTTP_X_REAL_IP="192.168.1.25",
            HTTP_USER_AGENT="Mozilla/5.0 Chrome/128.0",
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/fbs/tsd/picking/")
        self.assertEqual(int(self.client.session["_auth_user_id"]), self.user.id)
        self.assertEqual(self.client.session["employee_id"], self.employee.id)

    def test_picker_badge_accepts_lowercased_keyboard_wedge_output(self):
        response = self.client.post(
            "/tsd/",
            {"badge_token": self.raw_token.lower()},
            HTTP_X_REAL_IP="192.168.1.25",
            HTTP_USER_AGENT="Mozilla/5.0 Chrome/128.0",
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/fbs/tsd/picking/")
        self.assertEqual(int(self.client.session["_auth_user_id"]), self.user.id)

    def test_non_picker_badge_is_rejected_on_tsd_entry(self):
        storekeeper_user = get_user_model().objects.create_user(
            username="pfbs_tsd_storekeeper",
            password="Strong-pass-43!",
        )
        storekeeper = Employee.objects.create(
            full_name="Кладовщик не подборщик",
            user=storekeeper_user,
            role="storekeeper",
            is_active=True,
        )
        _badge, raw_token = issue_badge(employee=storekeeper, actor=None)
        set_qr_login_enabled(employee=storekeeper, enabled=True, actor=None)

        response = self.client.post(
            "/tsd/",
            {"badge_token": raw_token},
            HTTP_X_REAL_IP="192.168.1.26",
            HTTP_USER_AGENT="Mozilla/5.0 Chrome/128.0",
        )

        self.assertEqual(response.status_code, 401)
        self.assertNotIn("_auth_user_id", self.client.session)
        self.assertTrue(
            EmployeeBadgeEvent.objects.filter(
                employee=storekeeper,
                event_type=EmployeeBadgeEvent.EVENT_LOGIN_FAILURE,
                details__reason="tsd_picker_required",
            ).exists()
        )

    def test_regular_client_login_does_not_show_tsd_qr(self):
        response = self.client.get(
            "/login/",
            HTTP_USER_AGENT="Mozilla/5.0 Chrome/128.0",
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="username"')
        self.assertNotContains(response, 'id="badge-token"')
        self.assertNotContains(response, "Подборщик PFBS")

    def test_anonymous_long_pfbs_tsd_url_returns_to_short_entry(self):
        response = self.client.get("/fbs/tsd/picking/")

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/tsd/")

    def test_idle_picker_returns_to_short_tsd_entry(self):
        self.client.force_login(self.user)
        session = self.client.session
        session["_fullbox_last_activity_at"] = int(time.time()) - 3600
        session.save()

        response = self.client.get("/fbs/tsd/picking/")

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/tsd/?session_expired=1")
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_authenticated_picker_short_entry_opens_picking(self):
        self.client.force_login(self.user)

        response = self.client.get("/tsd/")

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/fbs/tsd/picking/")


@override_settings(ALLOWED_HOSTS=["testserver"])
class ReceivingTsdQrLoginTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="receiving_tsd_storekeeper",
            password="Strong-pass-44!",
        )
        self.employee = Employee.objects.create(
            full_name="Кладовщик приёмки",
            user=self.user,
            role="storekeeper",
            is_active=True,
        )
        self.badge, self.raw_token = issue_badge(employee=self.employee, actor=None)
        set_qr_login_enabled(employee=self.employee, enabled=True, actor=None)

    def test_short_receiving_entry_is_receiving_only(self):
        response = self.client.get(
            "/tsd/receiving/",
            HTTP_USER_AGENT="Mozilla/5.0 Chrome/128.0",
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Кладовщик приёмки")
        self.assertContains(response, 'action="/tsd/receiving/"')
        self.assertContains(response, 'id="badge-token"')
        self.assertNotContains(response, "Подборщик PFBS")
        self.assertNotContains(response, 'id="username"')
        self.assertNotContains(response, 'id="password"')

    def test_storekeeper_badge_opens_receiving_queue(self):
        response = self.client.post(
            "/tsd/receiving/",
            {"badge_token": self.raw_token},
            HTTP_X_REAL_IP="192.168.1.27",
            HTTP_USER_AGENT="Mozilla/5.0 Chrome/128.0",
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/orders/receiving/tsd/")
        self.assertEqual(int(self.client.session["_auth_user_id"]), self.user.id)
        self.assertEqual(self.client.session["employee_id"], self.employee.id)

    def test_picker_badge_is_rejected_on_receiving_entry(self):
        picker_user = get_user_model().objects.create_user(
            username="receiving_tsd_picker",
            password="Strong-pass-45!",
        )
        picker = Employee.objects.create(
            full_name="Подборщик не приёмка",
            user=picker_user,
            role="picker",
            is_active=True,
        )
        _badge, raw_token = issue_badge(employee=picker, actor=None)
        set_qr_login_enabled(employee=picker, enabled=True, actor=None)

        response = self.client.post(
            "/tsd/receiving/",
            {"badge_token": raw_token},
            HTTP_X_REAL_IP="192.168.1.28",
            HTTP_USER_AGENT="Mozilla/5.0 Chrome/128.0",
        )

        self.assertEqual(response.status_code, 401)
        self.assertNotIn("_auth_user_id", self.client.session)
        self.assertTrue(
            EmployeeBadgeEvent.objects.filter(
                employee=picker,
                event_type=EmployeeBadgeEvent.EVENT_LOGIN_FAILURE,
                details__reason="tsd_storekeeper_required",
            ).exists()
        )

    def test_authenticated_storekeeper_opens_receiving_queue(self):
        self.client.force_login(self.user)

        response = self.client.get("/tsd/receiving/")

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/orders/receiving/tsd/")

    def test_switch_employee_returns_to_receiving_badge_screen(self):
        self.client.force_login(self.user)

        response = self.client.get("/tsd/receiving/?switch=1")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Кладовщик приёмки")
        self.assertNotIn("_auth_user_id", self.client.session)


@override_settings(ALLOWED_HOSTS=["testserver"])
class EmployeeBadgeManagementTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.hr_user = User.objects.create_user(username="qr_hr", password="pass")
        self.hr = Employee.objects.create(
            full_name="ОК QR",
            user=self.hr_user,
            role="hr",
            is_active=True,
        )
        self.target_user = User.objects.create_user(username="qr_picker", password="pass")
        self.target = Employee.objects.create(
            full_name="Сборщик QR",
            user=self.target_user,
            role="picker",
            is_active=True,
        )
        self.client.force_login(self.hr_user)

    def test_issue_shows_qr_once_and_enables_access(self):
        response = self.client.post(f"/employees/{self.target.id}/badge/issue/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "data:image/png;base64,")
        self.assertContains(response, "QR показан только сейчас")
        self.target.refresh_from_db()
        self.assertTrue(self.target.qr_login_enabled)

        response = self.client.get(f"/employees/{self.target.id}/edit/")
        self.assertNotContains(response, "data:image/png;base64,")
        self.assertContains(response, "QR уже был показан")

    def test_issue_provisions_qr_only_account_when_employee_has_no_user(self):
        target = Employee.objects.create(
            full_name="Кладовщик без аккаунта",
            role="storekeeper",
            is_active=True,
        )

        response = self.client.post(f"/employees/{target.id}/badge/issue/")

        self.assertEqual(response.status_code, 200)
        target.refresh_from_db()
        self.assertIsNotNone(target.user_id)
        self.assertTrue(target.user.is_active)
        self.assertFalse(target.user.has_usable_password())
        self.assertTrue(target.qr_login_enabled)

    def test_access_cannot_be_enabled_without_badge(self):
        response = self.client.post(
            f"/employees/{self.target.id}/badge/toggle/",
            {"enabled": "1"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, "Сначала выдайте сотруднику QR-бейдж", status_code=400)

    def test_processing_head_can_receive_badge_and_enable_access(self):
        self.target.role = "processing_head"
        self.target.save(update_fields=("role",))

        issue_response = self.client.post(
            f"/employees/{self.target.id}/badge/issue/"
        )
        self.assertEqual(issue_response.status_code, 200)
        self.assertContains(issue_response, "data:image/png;base64,")

        toggle_response = self.client.post(
            f"/employees/{self.target.id}/badge/toggle/",
            {"enabled": "1"},
        )
        self.assertEqual(toggle_response.status_code, 302)
        self.target.refresh_from_db()
        self.assertTrue(self.target.qr_login_enabled)

    def test_non_hr_manager_cannot_issue_badge(self):
        manager_user = get_user_model().objects.create_user(username="qr_manager", password="pass")
        Employee.objects.create(
            full_name="Менеджер QR",
            user=manager_user,
            role="manager",
            is_active=True,
        )
        self.client.force_login(manager_user)

        response = self.client.post(f"/employees/{self.target.id}/badge/issue/")

        self.assertEqual(response.status_code, 403)
        self.assertFalse(self.target.badges.exists())
