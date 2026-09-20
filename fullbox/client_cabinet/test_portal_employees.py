from django.contrib.auth import get_user_model
from django.test import Client, RequestFactory, TestCase

from client_cabinet.models import AgencyPortalMember
from client_cabinet.portal_access import (
    ROLE_ACCOUNTANT,
    ROLE_ADMIN,
    ROLE_MANAGER,
    ROLE_OPERATOR,
    SECTION_KEYS,
    sections_for_role,
)
from client_cabinet.portal_members import create_portal_employee, resolve_portal_agency_for_user
from sku.models import Agency

User = get_user_model()


class PortalEmployeesTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="portal_owner", password="pwd", email="owner@example.com")
        self.agency = Agency.objects.create(
            agn_name="ИП Тест Сотрудники",
            fio_agn="Иванов Иван",
            email="owner@example.com",
            portal_user=self.owner,
        )
        self.client = Client()
        self.client.force_login(self.owner)

    def _member(self, *, role: str, email: str):
        member, _password = create_portal_employee(
            agency=self.agency,
            last_name="Сотрудник",
            first_name=role,
            email=email,
            position="Тестовая роль",
            role=role,
            custom_sections=None,
            created_by=self.owner,
        )
        return member

    def _legacy_operator(self, *, email: str):
        user = User.objects.create_user(
            username=f"legacy_{AgencyPortalMember.objects.count()}",
            password="pwd",
            email=email,
        )
        return AgencyPortalMember.objects.create(
            agency=self.agency,
            user=user,
            last_name="Сотрудник",
            first_name="operator",
            email=email,
            position="Старая роль",
            role=ROLE_OPERATOR,
            sections=sections_for_role(ROLE_OPERATOR),
            created_by=self.owner,
        )

    def test_employees_tab_renders(self):
        response = self.client.get(f"/client/{self.agency.id}/edit/?tab=employees")
        self.assertEqual(response.status_code, 200)
        content = response.content.decode("utf-8")
        self.assertIn("Сотрудники и доступы", content)
        self.assertIn("Добавить сотрудника", content)
        self.assertIn("Роли и доступы", content)

    def test_add_employee_creates_user_and_member(self):
        response = self.client.post(
            f"/client/{self.agency.id}/edit/?tab=employees",
            {
                "portal_action": "add_employee",
                "portal_last_name": "Петров",
                "portal_first_name": "Пётр",
                "portal_email": "petrov@example.com",
                "portal_position": "Менеджер по логистике",
                "portal_role": ROLE_MANAGER,
            },
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        member = AgencyPortalMember.objects.get(agency=self.agency, email="petrov@example.com")
        self.assertEqual(member.role, ROLE_MANAGER)
        self.assertEqual(member.sections, sections_for_role(ROLE_MANAGER))
        self.assertTrue(member.user_id)
        self.assertTrue(member.is_active)

    def test_member_can_open_cabinet(self):
        member = self._legacy_operator(email="sidorov@example.com")
        c = Client()
        c.force_login(member.user)
        response = c.get(f"/client/dashboard/lk/?client={self.agency.id}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["selected_client"].id, self.agency.id)
        self.assertEqual(response.context["portal_allowed_sections"], ["requests"])

    def test_owner_has_all_sections(self):
        response = self.client.get(f"/client/dashboard/lk/?client={self.agency.id}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["portal_allowed_sections"], list(SECTION_KEYS))

    def test_same_person_can_work_in_two_agencies_with_distinct_logins(self):
        second_owner = User.objects.create_user(
            username="second_portal_owner",
            password="pwd",
            email="second-owner@example.com",
        )
        second_agency = Agency.objects.create(
            agn_name="ИП Второй кабинет",
            fio_agn="Петров Пётр",
            email="second-owner@example.com",
            portal_user=second_owner,
        )

        first_member, first_password = create_portal_employee(
            agency=self.agency,
            last_name="Общий",
            first_name="Менеджер",
            email="shared-manager@example.com",
            position="Менеджер",
            role=ROLE_MANAGER,
            custom_sections=None,
            created_by=self.owner,
        )
        second_member, second_password = create_portal_employee(
            agency=second_agency,
            last_name="Общий",
            first_name="Менеджер",
            email="shared-manager@example.com",
            position="Менеджер",
            role=ROLE_MANAGER,
            custom_sections=None,
            created_by=second_owner,
        )

        self.assertNotEqual(first_member.user_id, second_member.user_id)
        self.assertNotEqual(first_member.user.username, second_member.user.username)
        self.assertTrue(first_member.user.username.startswith(f"a{self.agency.id}_"))
        self.assertTrue(second_member.user.username.startswith(f"a{second_agency.id}_"))
        self.assertEqual(first_member.full_name, second_member.full_name)
        self.assertEqual(first_member.email, second_member.email)

        first_client = Client()
        second_client = Client()
        self.assertTrue(first_client.login(username=first_member.user.username, password=first_password))
        self.assertTrue(second_client.login(username=second_member.user.username, password=second_password))
        self.assertEqual(resolve_portal_agency_for_user(first_member.user), self.agency)
        self.assertEqual(resolve_portal_agency_for_user(second_member.user), second_agency)

    def test_same_email_remains_unique_inside_one_agency(self):
        create_portal_employee(
            agency=self.agency,
            last_name="Первый",
            first_name="Менеджер",
            email="duplicate@example.com",
            position="Менеджер",
            role=ROLE_MANAGER,
            custom_sections=None,
            created_by=self.owner,
        )

        with self.assertRaisesRegex(ValueError, "Сотрудник с таким email уже добавлен"):
            create_portal_employee(
                agency=self.agency,
                last_name="Второй",
                first_name="Менеджер",
                email="duplicate@example.com",
                position="Менеджер",
                role=ROLE_MANAGER,
                custom_sections=None,
                created_by=self.owner,
            )

    def test_operator_can_open_requests_but_not_finance_or_stock(self):
        member = self._legacy_operator(email="operator@example.com")
        c = Client()
        c.force_login(member.user)

        requests_response = c.get(f"/client/api/v1/requests/?client={self.agency.id}")
        finance_response = c.get(f"/client/api/v1/finance/?client={self.agency.id}")
        stock_response = c.get(f"/client/api/v1/stock-journal/?client={self.agency.id}")
        live_response = c.get(f"/client/api/v1/dashboard/live/?client={self.agency.id}")
        acts_response = c.get(f"/client/api/v1/billing/acts/?client={self.agency.id}")
        dashboard_response = c.get(f"/client/api/v1/dashboard/?client={self.agency.id}&lite=1")

        self.assertEqual(requests_response.status_code, 200)
        self.assertEqual(finance_response.status_code, 403)
        self.assertEqual(stock_response.status_code, 403)
        self.assertEqual(live_response.status_code, 403)
        self.assertEqual(acts_response.status_code, 403)
        self.assertEqual(dashboard_response.status_code, 200)
        dashboard = dashboard_response.json()["data"]
        self.assertEqual(dashboard["stock"]["summary"]["total_units"], 0)
        self.assertEqual(dashboard["action_urls"]["sku_list"], "#")

    def test_accountant_can_open_finance_but_not_requests(self):
        member = self._member(role=ROLE_ACCOUNTANT, email="accountant@example.com")
        c = Client()
        c.force_login(member.user)

        finance_response = c.get(f"/client/api/v1/finance/?client={self.agency.id}")
        requests_response = c.get(f"/client/api/v1/requests/?client={self.agency.id}")

        self.assertEqual(finance_response.status_code, 200)
        self.assertEqual(requests_response.status_code, 403)

    def test_operator_cannot_open_company_profile_or_manage_employees(self):
        member = self._legacy_operator(email="no-profile@example.com")
        c = Client()
        c.force_login(member.user)

        response = c.get(f"/client/{self.agency.id}/edit/?tab=employees")

        self.assertEqual(response.status_code, 403)

    def test_admin_member_cannot_create_removed_operator_role(self):
        member = self._member(role=ROLE_ADMIN, email="portal-admin@example.com")
        c = Client()
        c.force_login(member.user)

        page = c.get(f"/client/{self.agency.id}/edit/?tab=employees")
        create_response = c.post(
            f"/client/{self.agency.id}/edit/?tab=employees",
            {
                "portal_action": "add_employee",
                "portal_last_name": "Новый",
                "portal_first_name": "Оператор",
                "portal_email": "created-by-admin@example.com",
                "portal_position": "Оператор",
                "portal_role": ROLE_OPERATOR,
            },
        )

        self.assertEqual(page.status_code, 200)
        self.assertNotContains(page, '<option value="operator">', html=False)
        self.assertNotContains(page, '<div class="role-legend-item operator">', html=False)
        self.assertEqual(create_response.status_code, 302)
        self.assertFalse(
            AgencyPortalMember.objects.filter(
                agency=self.agency,
                email="created-by-admin@example.com",
                role=ROLE_OPERATOR,
            ).exists()
        )

    def test_operator_is_recognized_by_client_request_workflows(self):
        member = self._legacy_operator(email="workflow-operator@example.com")
        request = RequestFactory().get(f"/shipping/new/?client={self.agency.id}")
        request.user = member.user
        request.session = {}

        from orders.web_ui import _client_agency_from_request as receiving_agency
        from processing_app.web_ui import _client_agency_from_request as processing_agency
        from shipping.workflow import request_scope

        shipping_scope, _role, shipping_agency = request_scope(request)
        self.assertEqual(shipping_scope, "client")
        self.assertEqual(shipping_agency.id, self.agency.id)
        self.assertEqual(receiving_agency(request).id, self.agency.id)
        self.assertEqual(processing_agency(request).id, self.agency.id)

    def test_accountant_is_not_recognized_by_client_request_workflows(self):
        member = self._member(role=ROLE_ACCOUNTANT, email="workflow-accountant@example.com")
        request = RequestFactory().get(f"/shipping/new/?client={self.agency.id}")
        request.user = member.user
        request.session = {}

        from orders.web_ui import _client_agency_from_request as receiving_agency
        from processing_app.web_ui import _client_agency_from_request as processing_agency
        from shipping.workflow import request_scope

        shipping_scope, _role, shipping_agency = request_scope(request)
        self.assertIsNone(shipping_scope)
        self.assertIsNone(shipping_agency)
        self.assertIsNone(receiving_agency(request))
        self.assertIsNone(processing_agency(request))
