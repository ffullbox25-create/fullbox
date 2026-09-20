from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.test import RequestFactory, TestCase

from employees.access import (
    get_request_employee,
    get_request_role,
    get_request_roles,
)
from employees.models import Employee


User = get_user_model()


class RequestEmployeeCacheTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.user = User.objects.create_user(username="request_cache_storekeeper")
        self.employee = Employee.objects.create(
            full_name="Кладовщик кэша запроса",
            user=self.user,
            role="storekeeper",
            access_roles=["head_manager"],
            is_active=True,
        )

    def request_for(self, user=None, **session):
        request = self.factory.get("/")
        request.user = user or self.user
        request.session = session
        return request

    def test_employee_role_and_roles_share_one_query(self):
        request = self.request_for(
            employee_id=self.employee.id,
            employee_role=self.employee.role,
        )

        with self.assertNumQueries(1):
            employee = get_request_employee(request)
            role = get_request_role(request)
            roles = get_request_roles(request)

        self.assertEqual(employee.pk, self.employee.pk)
        self.assertEqual(role, "storekeeper")
        self.assertEqual(roles, frozenset({"storekeeper", "head_manager"}))

    def test_employee_id_change_invalidates_request_cache(self):
        request = self.request_for(employee_id=101, employee_role="storekeeper")
        first = SimpleNamespace(pk=101, role="storekeeper")
        second = SimpleNamespace(pk=202, role="storekeeper")

        def employee_for_user(_user, *, preferred_id=None, preferred_role=None):
            return first if preferred_id == 101 else second

        with patch(
            "employees.access.get_employee_for_user",
            side_effect=employee_for_user,
        ) as lookup:
            self.assertIs(get_request_employee(request), first)
            request.session["employee_id"] = 202
            self.assertIs(get_request_employee(request), second)

        self.assertEqual(lookup.call_count, 2)

    def test_role_and_user_changes_invalidate_request_cache(self):
        request = self.request_for(employee_role="storekeeper")
        other_user = User.objects.create_user(username="request_cache_other")
        results = iter(
            (
                SimpleNamespace(pk=1, role="storekeeper"),
                SimpleNamespace(pk=2, role="head_manager"),
                SimpleNamespace(pk=3, role="director"),
            )
        )

        with patch(
            "employees.access.get_employee_for_user",
            side_effect=lambda *_args, **_kwargs: next(results),
        ) as lookup:
            first = get_request_employee(request)
            request.session["employee_role"] = "head_manager"
            second = get_request_employee(request)
            request.user = other_user
            third = get_request_employee(request)

        self.assertEqual((first.pk, second.pk, third.pk), (1, 2, 3))
        self.assertEqual(lookup.call_count, 3)

    def test_developer_impersonation_is_cached_and_invalidated(self):
        developer = User.objects.create_user(username="dev")
        first = Employee.objects.create(
            full_name="Первый подменённый сотрудник",
            role="head_manager",
            is_active=True,
        )
        second = Employee.objects.create(
            full_name="Второй подменённый сотрудник",
            role="director",
            is_active=True,
        )
        request = self.request_for(
            user=developer,
            developer_impersonated_employee_id=first.id,
        )

        with self.assertNumQueries(1):
            self.assertEqual(get_request_employee(request).pk, first.pk)
            self.assertEqual(get_request_role(request), "head_manager")
            self.assertEqual(get_request_roles(request), frozenset({"head_manager"}))

        request.session["developer_impersonated_employee_id"] = second.id
        with self.assertNumQueries(1):
            self.assertEqual(get_request_employee(request).pk, second.pk)
            self.assertEqual(get_request_role(request), "director")

    def test_anonymous_request_without_session_does_not_query(self):
        request = self.factory.get("/")
        request.user = AnonymousUser()

        with self.assertNumQueries(0):
            self.assertIsNone(get_request_employee(request))

