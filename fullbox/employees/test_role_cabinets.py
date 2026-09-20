"""Тесты: роли сотрудников → домашние кабинеты и запреты чужих кабинетов."""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings

from employees.access import resolve_cabinet_url
from employees.models import Employee
from sku.models import Agency

User = get_user_model()

# Ожидаемый домашний URL по resolve_cabinet_url + smoke GET.
ROLE_HOME = {
    "manager": "/team-manager/",
    "logistician": "/team-manager/",
    "driver": "/logistics/driver/trips/",
    "accountant": "/accountant/",
    "hr": "/hr/",
    "storekeeper": "/sklad/",
    "head_manager": "/head-manager/",
    "processing_head": "/processing-head/",
    "processing_worker": "/processing-worker/",
    "reachtruck_driver": "/reachtruck/",
    "super_car": "/super-car/",
    "developer": "/dev/",
    "admin": "/admin/",
    "director": "/cabinet/director/",
    "picker": "/cabinet/picker/",  # fallback shell
}

# Кабинеты с жёстким RoleRequiredMixin — чужая роль должна получить 403.
# team-manager dashboard: teammanager.roles.CABINET_ROLES (склад туда не пускают).
STRICT_CABINETS = (
    (
        "/team-manager/",
        {"manager", "logistician", "head_manager", "director", "admin", "developer"},
    ),
    ("/accountant/", {"accountant", "admin", "director", "head_manager"}),
    ("/hr/", {"hr", "admin", "director"}),
    ("/head-manager/", {"head_manager"}),
    ("/processing-head/", {"processing_head"}),
    ("/processing-worker/", {"processing_worker"}),
    ("/cabinet/director/", {"director"}),
    ("/cabinet/director/integrations/telegram/", {"director", "admin", "developer"}),
    ("/team-manager/billing/", {"manager", "head_manager", "accountant", "admin", "director"}),
    ("/sklad/", {"storekeeper"}),
)


def _login_as(http: Client, *, username: str, role: str, is_staff: bool = False) -> Employee:
    user = User.objects.create_user(username=username, password="pwd", is_staff=is_staff)
    emp = Employee.objects.create(
        full_name=f"Тест {role}",
        user=user,
        role=role,
        is_active=True,
    )
    http.force_login(user)
    session = http.session
    session["employee_id"] = emp.id
    session["employee_role"] = role
    session["employee_name"] = emp.full_name
    session.save()
    return emp


@override_settings(ALLOWED_HOSTS=["*"])
class ResolveCabinetUrlTests(TestCase):
    def test_all_known_roles_map(self):
        for role, url in ROLE_HOME.items():
            self.assertEqual(resolve_cabinet_url(role), url, msg=role)

    def test_client_portal_url(self):
        self.assertEqual(resolve_cabinet_url("client"), "/client/dashboard/lk/")
        self.assertEqual(
            resolve_cabinet_url("client", client_id=42),
            "/client/dashboard/lk/?client=42",
        )
        self.assertEqual(
            resolve_cabinet_url(None, client_id=7),
            "/client/dashboard/lk/?client=7",
        )

    def test_unknown_role_fallback(self):
        self.assertEqual(resolve_cabinet_url("custom_role"), "/cabinet/custom_role/")
        self.assertEqual(resolve_cabinet_url(None), "/")


@override_settings(ALLOWED_HOSTS=["*"])
class RoleHomeCabinetSmokeTests(TestCase):
    """Каждая роль открывает свой домашний кабинет (200 или ожидаемый редирект)."""

    def test_each_staff_role_opens_home(self):
        for role, home in ROLE_HOME.items():
            with self.subTest(role=role):
                http = Client()
                _login_as(
                    http,
                    username=f"home_{role}",
                    role=role,
                    is_staff=(role == "admin"),
                )
                resp = http.get(home)
                # admin может редиректить на login-change или index
                if role == "admin":
                    self.assertIn(resp.status_code, {200, 302})
                    if resp.status_code == 302:
                        self.assertTrue(
                            str(resp["Location"]).startswith("/admin"),
                            msg=resp["Location"],
                        )
                elif role == "developer":
                    self.assertIn(resp.status_code, {200, 302})
                else:
                    self.assertEqual(
                        resp.status_code,
                        200,
                        msg=f"{role} → {home} got {resp.status_code}",
                    )

    def test_client_lands_on_lk(self):
        user = User.objects.create_user(username="portal_client_role", password="pwd")
        agency = Agency.objects.create(
            agn_name="Клиент роли",
            short_name="КР",
            portal_user=user,
        )
        http = Client()
        http.force_login(user)
        # старый cabinet/client → ЛК
        resp = http.get("/cabinet/client/")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/client/dashboard/lk/", resp["Location"])
        self.assertIn(f"client={agency.id}", resp["Location"])

        lk = http.get(f"/client/dashboard/lk/?client={agency.id}")
        self.assertEqual(lk.status_code, 200)

    def test_manager_old_cabinet_redirects(self):
        http = Client()
        _login_as(http, username="mgr_redir", role="manager")
        resp = http.get("/cabinet/manager/")
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp["Location"], "/team-manager/")


@override_settings(ALLOWED_HOSTS=["*"])
class RoleCabinetDenialTests(TestCase):
    """Чужая роль не заходит в защищённые кабинеты."""

    def test_wrong_role_forbidden_on_strict_cabinets(self):
        probe_roles = [
            "manager",
            "accountant",
            "hr",
            "storekeeper",
            "logistician",
            "driver",
            "head_manager",
            "processing_worker",
            "reachtruck_driver",
            "director",
        ]
        for role in probe_roles:
            http = Client()
            _login_as(http, username=f"deny_{role}", role=role)
            for path, allowed in STRICT_CABINETS:
                with self.subTest(role=role, path=path):
                    resp = http.get(path)
                    if role in allowed:
                        self.assertIn(
                            resp.status_code,
                            {200, 302},
                            msg=f"{role} should access {path}",
                        )
                    else:
                        self.assertEqual(
                            resp.status_code,
                            403,
                            msg=f"{role} must be denied on {path}, got {resp.status_code}",
                        )

    def test_cabinet_role_mismatch_forbidden(self):
        """/cabinet/<role>/ только для своей роли (кроме manager→redirect)."""
        http = Client()
        _login_as(http, username="cab_mismatch", role="storekeeper")
        forbidden = http.get("/cabinet/director/")
        self.assertEqual(forbidden.status_code, 403)
        own_shell = http.get("/cabinet/storekeeper/")
        # storekeeper home в mapping — /sklad/, но shell /cabinet/storekeeper/ допустим если role совпал
        self.assertIn(own_shell.status_code, {200, 302})

    def test_anonymous_forbidden(self):
        http = Client()
        for path, _ in STRICT_CABINETS:
            with self.subTest(path=path):
                resp = http.get(path)
                self.assertIn(resp.status_code, {302, 403})


@override_settings(ALLOWED_HOSTS=["*"])
class RoleCabinetCrossChecksTests(TestCase):
    def test_storekeeper_sklad_ok_accountant_denied(self):
        sk = Client()
        _login_as(sk, username="sk_ok", role="storekeeper")
        self.assertEqual(sk.get("/sklad/").status_code, 200)

        acc = Client()
        _login_as(acc, username="acc_no_sklad", role="accountant")
        self.assertEqual(acc.get("/sklad/").status_code, 403)

    def test_manager_billing_ok_storekeeper_denied(self):
        mgr = Client()
        _login_as(mgr, username="mgr_bill", role="manager")
        self.assertIn(mgr.get("/team-manager/billing/").status_code, {200, 302})

        sk = Client()
        _login_as(sk, username="sk_no_bill", role="storekeeper")
        self.assertEqual(sk.get("/team-manager/billing/").status_code, 403)

    def test_director_telegram_ok_manager_denied(self):
        director = Client()
        _login_as(director, username="dir_tg_role", role="director")
        self.assertEqual(director.get("/cabinet/director/integrations/telegram/").status_code, 200)

        mgr = Client()
        _login_as(mgr, username="mgr_no_tg_dir", role="manager")
        self.assertEqual(mgr.get("/cabinet/director/integrations/telegram/").status_code, 403)

    def test_head_manager_only(self):
        hm = Client()
        _login_as(hm, username="hm_ok", role="head_manager")
        self.assertEqual(hm.get("/head-manager/").status_code, 200)

        mgr = Client()
        _login_as(mgr, username="mgr_no_hm", role="manager")
        self.assertEqual(mgr.get("/head-manager/").status_code, 403)

    def test_inactive_employee_not_used_for_role(self):
        user = User.objects.create_user(username="inactive_emp", password="pwd")
        Employee.objects.create(
            full_name="Неактивный",
            user=user,
            role="manager",
            is_active=False,
        )
        http = Client()
        http.force_login(user)
        # без активной роли team-manager должен запретить
        resp = http.get("/team-manager/")
        self.assertEqual(resp.status_code, 403)
