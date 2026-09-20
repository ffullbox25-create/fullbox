import time

from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.core.cache import cache
from django.http import HttpResponse
from django.contrib.sessions.models import Session
from django.test import RequestFactory, TestCase, override_settings
from django.views import View

from employees.access import RoleRequiredMixin, get_employee_roles, resolve_cabinet_url, role_required
from employees.models import Employee
from employees.views import EmployeeForm


class GlobalSessionSecurityTests(TestCase):
    session_key = "_fullbox_last_activity_at"

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="idle_timeout_user",
            password="pwd",
        )

    def test_login_page_supports_browser_password_manager(self):
        response = self.client.get("/login/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-login-form', html=False)
        self.assertContains(
            response,
            'form method="post" action="/login/" autocomplete="on"',
            html=False,
        )
        self.assertContains(response, 'name="username" autocomplete="username"', html=False)
        self.assertContains(response, 'name="password" type="password" autocomplete="current-password"', html=False)
        self.assertNotContains(response, "readonly", html=False)
        self.assertNotContains(response, "clearFields", html=False)

    @override_settings(AUTHENTICATED_IDLE_TIMEOUT_SECONDS=1800)
    def test_authenticated_session_is_closed_after_thirty_minutes_idle(self):
        self.client.force_login(self.user)
        session = self.client.session
        session[self.session_key] = int(time.time()) - 1801
        session.save()

        response = self.client.get("/__idle-timeout-test__/")

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/login/?session_expired=1")
        self.assertNotIn("_auth_user_id", self.client.session)

    @override_settings(AUTHENTICATED_IDLE_TIMEOUT_SECONDS=1800)
    def test_recently_active_session_remains_authenticated(self):
        self.client.force_login(self.user)
        session = self.client.session
        session.set_expiry(1800)
        session[self.session_key] = int(time.time()) - 120
        session.save()

        response = self.client.get("/__idle-timeout-test__/")

        self.assertEqual(response.status_code, 404)
        self.assertIn("_auth_user_id", self.client.session)
        self.assertNotIn("_session_expiry", self.client.session)
        self.assertGreaterEqual(
            self.client.session[self.session_key],
            int(time.time()) - 2,
        )


class EmployeePasswordValidationTests(TestCase):
    def _form(self, password):
        return EmployeeForm(
            data={
                "full_name": "Тестовый сотрудник",
                "role": "manager",
                "account_username": "security_employee",
                "account_password": password,
                "email": "security-employee@example.com",
                "phone": "",
                "is_active": "on",
            }
        )

    def test_common_password_is_rejected(self):
        form = self._form("password")
        form.is_valid()
        self.assertIn("account_password", form.errors)

    def test_strong_password_passes_password_validators(self):
        form = self._form("N7!vQ2@pL9#xR4$k")
        form.is_valid()
        self.assertNotIn("account_password", form.errors)


@override_settings(
    CACHES={
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "login-rate-limit-tests",
        }
    },
    LOGIN_RATE_LIMIT_WINDOW_SECONDS=60,
    LOGIN_RATE_LIMIT_IP_FAILURES=99,
    LOGIN_RATE_LIMIT_USERNAME_FAILURES=2,
)
class LoginRateLimitTests(TestCase):
    def setUp(self):
        cache.clear()

    def tearDown(self):
        cache.clear()

    def _post_login(self, username, password="wrong", *, ip="203.0.113.10"):
        return self.client.post(
            "/login/",
            {"username": username, "password": password},
            REMOTE_ADDR=ip,
        )

    def test_username_is_blocked_after_repeated_failures(self):
        self.assertEqual(self._post_login("missing-user").status_code, 200)
        self.assertEqual(self._post_login("missing-user").status_code, 200)

        response = self._post_login("missing-user")

        self.assertEqual(response.status_code, 429)
        self.assertEqual(response["Retry-After"], "60")
        self.assertEqual(response["Cache-Control"], "no-store, private")

    @override_settings(
        LOGIN_RATE_LIMIT_IP_FAILURES=3,
        LOGIN_RATE_LIMIT_USERNAME_FAILURES=99,
    )
    def test_ip_is_blocked_across_different_usernames(self):
        for username in ("missing-1", "missing-2", "missing-3"):
            self.assertEqual(self._post_login(username).status_code, 200)

        self.assertEqual(self._post_login("missing-4").status_code, 429)

    def test_successful_login_clears_username_failures(self):
        get_user_model().objects.create_user(
            username="valid-user",
            password="Strong-pass-42!",
        )
        self.assertEqual(self._post_login("valid-user").status_code, 200)
        response = self._post_login("valid-user", "Strong-pass-42!")
        self.assertEqual(response.status_code, 302)
        self.client.logout()

        self.assertEqual(self._post_login("valid-user").status_code, 200)
        self.assertEqual(self._post_login("valid-user").status_code, 200)
        self.assertEqual(self._post_login("valid-user").status_code, 429)

    def test_admin_login_uses_the_same_limit(self):
        for _attempt in range(2):
            response = self.client.post(
                "/admin/login/",
                {"username": "missing-admin", "password": "wrong"},
                REMOTE_ADDR="203.0.113.20",
            )
            self.assertEqual(response.status_code, 200)

        response = self.client.post(
            "/admin/login/",
            {"username": "missing-admin", "password": "wrong"},
            REMOTE_ADDR="203.0.113.20",
        )
        self.assertEqual(response.status_code, 429)

    def test_authenticated_session_cannot_clear_another_username_limit(self):
        helper = get_user_model().objects.create_user(
            username="helper-user",
            password="Strong-pass-43!",
        )
        self.assertEqual(self._post_login("target-user").status_code, 200)
        self.assertEqual(self._post_login("target-user").status_code, 200)
        self.client.force_login(helper)

        response = self._post_login("target-user")

        self.assertEqual(response.status_code, 429)


class SessionLoginRedirectTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()

    def test_role_decorator_redirects_anonymous_user_to_login(self):
        request = self.factory.get("/team-manager/orders/")
        request.user = AnonymousUser()
        protected_view = role_required("manager")(lambda _request: HttpResponse("ok"))

        response = protected_view(request)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/login/?next=/team-manager/orders/")

    def test_role_mixin_redirects_anonymous_user_to_login(self):
        class ProtectedView(RoleRequiredMixin, View):
            allowed_roles = ("manager",)

            def get(self, request):
                return HttpResponse("ok")

        request = self.factory.get("/sklad/")
        request.user = AnonymousUser()

        response = ProtectedView.as_view()(request)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/login/?next=/sklad/")

    def test_authenticated_wrong_role_still_receives_forbidden(self):
        user = get_user_model().objects.create_user(username="wrong_role", password="pwd")
        Employee.objects.create(full_name="Кладовщик", user=user, role="storekeeper", is_active=True)
        request = self.factory.get("/team-manager/orders/")
        request.user = user
        request.session = {}
        protected_view = role_required("manager")(lambda _request: HttpResponse("ok"))

        response = protected_view(request)

        self.assertEqual(response.status_code, 403)


class DirectorCabinetTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="director_cabinet", password="pwd")
        self.employee = Employee.objects.create(
            full_name="Директор Кабинет",
            user=self.user,
            role="director",
            is_active=True,
        )
        self.client.force_login(self.user)
        session = self.client.session
        session["employee_id"] = self.employee.id
        session["employee_role"] = "director"
        session["employee_name"] = self.employee.full_name
        session.save()

    def test_resolve_cabinet_url_director(self):
        self.assertEqual(resolve_cabinet_url("director"), "/cabinet/director/")

    def test_resolve_cabinet_url_accountant(self):
        self.assertEqual(resolve_cabinet_url("accountant"), "/accountant/")

    def test_director_dashboard_matches_template_shell(self):
        page = self.client.get("/cabinet/director/")
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "Панель директора")
        self.assertContains(page, "Требует внимания")
        self.assertContains(page, "9 420 000 ₽")
        self.assertContains(page, "Выручка")
        self.assertContains(page, "План месяца")
        self.assertContains(page, 'href="/shipping/"')
        self.assertContains(page, 'href="/employees/"')
        self.assertContains(page, 'href="/team-manager/billing/"')
        self.assertContains(page, "Состояние клиентов")
        self.assertContains(page, "Лента событий")
        self.assertContains(page, 'href="/static/director/director-dashboard.css"')

    def test_director_dashboard_period_query(self):
        page = self.client.get("/cabinet/director/?period=week")
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, 'href="?period=week"')
        self.assertContains(page, "is-active")


class ManagerCabinetRedirectTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="manager", password="pwd")
        self.employee = Employee.objects.create(
            full_name="Менеджер Новый ЛК",
            user=self.user,
            role="manager",
            is_active=True,
        )

    def test_old_manager_cabinet_redirects_to_team_manager_lk(self):
        self.client.force_login(self.user)
        session = self.client.session
        session["employee_id"] = self.employee.id
        session["employee_role"] = "manager"
        session["employee_name"] = self.employee.full_name
        session.save()

        page = self.client.get("/cabinet/manager/")

        self.assertEqual(page.status_code, 302)
        self.assertEqual(page["Location"], "/team-manager/")

    @override_settings(DEBUG=True)
    def test_dev_login_manager_redirects_to_team_manager_lk(self):
        page = self.client.get("/dev-login/manager/")

        self.assertEqual(page.status_code, 302)
        self.assertEqual(page["Location"], "/team-manager/")


class EmployeeAccountStateSyncTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.hr_user = User.objects.create_user(username="hr_account_sync", password="pwd")
        self.hr_employee = Employee.objects.create(
            full_name="Кадровик Синхронизация",
            user=self.hr_user,
            role="hr",
            is_active=True,
        )
        self.target_user = User.objects.create_user(
            username="manager_account_sync",
            password="target-pwd",
            email="old@example.com",
        )
        self.target_employee = Employee.objects.create(
            full_name="Менеджер Синхронизация",
            user=self.target_user,
            role="manager",
            email="old@example.com",
            is_active=True,
        )
        self.client.force_login(self.hr_user)
        session = self.client.session
        session["employee_id"] = self.hr_employee.id
        session["employee_role"] = "hr"
        session["employee_name"] = self.hr_employee.full_name
        session.save()

    def _post_employee(self, *, is_active: bool, email: str):
        payload = {
            "full_name": self.target_employee.full_name,
            "role": self.target_employee.role,
            "email": email,
            "phone": "",
            "account_username": self.target_user.username,
            "account_password": "",
        }
        if is_active:
            payload["is_active"] = "on"
        return self.client.post(f"/employees/{self.target_employee.id}/edit/", payload)

    def test_deactivation_disables_user_revokes_sessions_and_blocks_login(self):
        target_client = self.client_class()
        target_client.force_login(self.target_user)
        target_session_key = target_client.session.session_key

        response = self._post_employee(is_active=False, email="disabled@example.com")

        self.assertEqual(response.status_code, 302)
        self.target_employee.refresh_from_db()
        self.target_user.refresh_from_db()
        self.assertFalse(self.target_employee.is_active)
        self.assertFalse(self.target_user.is_active)
        self.assertEqual(self.target_user.email, "disabled@example.com")
        self.assertFalse(Session.objects.filter(session_key=target_session_key).exists())

        login_client = self.client_class()
        login_response = login_client.post(
            "/login/",
            {"username": self.target_user.username, "password": "target-pwd"},
        )
        self.assertEqual(login_response.status_code, 200)
        self.assertContains(login_response, "Неверный логин или пароль")

    def test_reactivation_enables_user_and_synchronizes_email(self):
        self.target_employee.is_active = False
        self.target_employee.save(update_fields=["is_active"])
        self.target_user.is_active = False
        self.target_user.save(update_fields=["is_active"])

        response = self._post_employee(is_active=True, email="enabled@example.com")

        self.assertEqual(response.status_code, 302)
        self.target_employee.refresh_from_db()
        self.target_user.refresh_from_db()
        self.assertTrue(self.target_employee.is_active)
        self.assertTrue(self.target_user.is_active)
        self.assertEqual(self.target_user.email, "enabled@example.com")
class EmployeeFacsimileSecurityTests(TestCase):
    def setUp(self):
        from io import BytesIO
        from tempfile import TemporaryDirectory
        from django.core.files.base import ContentFile
        from PIL import Image
        self._media_dir = TemporaryDirectory()
        self._media_override = override_settings(MEDIA_ROOT=self._media_dir.name)
        self._media_override.enable()
        self.hr_user = get_user_model().objects.create_user(username="hr_facsimile_secure", password="pwd")
        self.hr_employee = Employee.objects.create(full_name="Кадровик Безопасность", user=self.hr_user, role="hr", is_active=True)
        self.manager_user = get_user_model().objects.create_user(username="manager_facsimile_denied", password="pwd")
        self.manager_employee = Employee.objects.create(full_name="Менеджер Без Доступа", user=self.manager_user, role="manager", is_active=True)
        self.target = Employee.objects.create(full_name="Кладовщик Подпись", role="storekeeper", is_active=True)
        signature = BytesIO()
        Image.new("RGBA", (24, 12), (248, 144, 0, 255)).save(signature, format="PNG")
        self.target.facsimile.save("secure-sign.png", ContentFile(signature.getvalue()), save=True)
    def tearDown(self):
        self._media_override.disable(); self._media_dir.cleanup(); super().tearDown()
    def _login(self, user, employee):
        self.client.force_login(user); session = self.client.session
        session["employee_id"] = employee.id; session["employee_role"] = employee.role; session["employee_name"] = employee.full_name; session.save()
    def test_facsimile_endpoint_requires_allowed_role_and_returns_private_png(self):
        url = f"/employees/{self.target.id}/facsimile/"
        self.assertEqual(self.client.get(url).status_code, 403)
        self._login(self.manager_user, self.manager_employee)
        self.assertEqual(self.client.get(url).status_code, 403)
        self.client.logout(); self._login(self.hr_user, self.hr_employee)
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200); self.assertEqual(response["Content-Type"], "image/png")
        self.assertEqual(response["X-Content-Type-Options"], "nosniff"); self.assertIn("private", response["Cache-Control"])
        body = b"".join(response.streaming_content); response.close(); self.assertTrue(body.startswith(b"\x89PNG"))
    def test_employee_pages_hide_raw_media_and_keep_clear_control(self):
        self._login(self.hr_user, self.hr_employee); protected_url = f"/employees/{self.target.id}/facsimile/"
        response = self.client.get("/employees/"); self.assertEqual(response.status_code, 200)
        self.assertContains(response, protected_url); self.assertNotContains(response, "/media/facsimiles/")
        response = self.client.get(f"/employees/{self.target.id}/edit/"); self.assertEqual(response.status_code, 200)
        self.assertContains(response, protected_url); self.assertContains(response, 'name="facsimile-clear"', html=False)
        self.assertNotContains(response, "/media/facsimiles/")


class EmployeeAdditionalRoleTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.hr_user = User.objects.create_user(username="hr_access_roles", password="pwd")
        self.hr_employee = Employee.objects.create(
            full_name="Кадровик Доступы",
            user=self.hr_user,
            role="hr",
            is_active=True,
        )
        self.target_user = User.objects.create_user(username="manager_access_roles", password="pwd")
        self.target = Employee.objects.create(
            full_name="Менеджер Доступы",
            user=self.target_user,
            role="manager",
            is_active=True,
        )

    def _login(self, user, employee):
        self.client.force_login(user)
        session = self.client.session
        session["employee_id"] = employee.id
        session["employee_role"] = employee.role
        session["employee_name"] = employee.full_name
        session.save()

    def _payload(self, *, access_roles):
        return {
            "full_name": self.target.full_name,
            "role": "manager",
            "access_roles": access_roles,
            "email": "",
            "phone": "",
            "account_username": self.target_user.username,
            "account_password": "",
            "is_active": "on",
        }

    def test_hr_can_grant_head_manager_and_accountant_without_changing_primary_role(self):
        self._login(self.hr_user, self.hr_employee)

        response = self.client.post(
            f"/employees/{self.target.id}/edit/",
            self._payload(access_roles=["head_manager", "accountant"]),
        )

        self.assertEqual(response.status_code, 302)
        self.target.refresh_from_db()
        self.assertEqual(self.target.role, "manager")
        self.assertEqual(self.target.normalized_access_roles(), ("head_manager", "accountant"))
        self.assertEqual(
            get_employee_roles(self.target),
            frozenset({"manager", "head_manager", "accountant"}),
        )

    def test_head_manager_cannot_change_additional_roles(self):
        self.target.access_roles = ["accountant"]
        self.target.save(update_fields=["access_roles"])
        head_user = get_user_model().objects.create_user(username="head_no_access_editor", password="pwd")
        head_employee = Employee.objects.create(
            full_name="Главный менеджер без HR",
            user=head_user,
            role="head_manager",
            is_active=True,
        )
        self._login(head_user, head_employee)

        response = self.client.post(
            f"/employees/{self.target.id}/edit/",
            self._payload(access_roles=["head_manager"]),
        )

        self.assertEqual(response.status_code, 302)
        self.target.refresh_from_db()
        self.assertEqual(self.target.normalized_access_roles(), ("accountant",))
