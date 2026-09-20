from types import SimpleNamespace
from unittest.mock import patch

from django.test import RequestFactory, SimpleTestCase
from django.urls import resolve, reverse


class InternalDocumentationAccessTests(SimpleTestCase):
    documentation_routes = (
        "project-description",
        "project-structure",
        "project-block-diagram",
        "project-description-file",
        "development-journal",
        "development-journal-file",
    )
    scanner_routes = (
        "scanner-test",
        "scanner-settings",
        "scanner-ims-2290hd",
    )

    def setUp(self):
        self.factory = RequestFactory()

    def _response(self, route_name, *, authenticated, role=None):
        path = reverse(route_name)
        request = self.factory.get(path)
        request.user = SimpleNamespace(
            is_authenticated=authenticated,
            username=f"{role or 'anonymous'}_user",
        )
        request.session = {}
        with patch("employees.access.get_request_role", return_value=role):
            return resolve(path).func(request)

    def _assert_routes_status(self, route_names, expected_status, *, authenticated, role=None):
        for route_name in route_names:
            with self.subTest(route=route_name):
                response = self._response(route_name, authenticated=authenticated, role=role)
                try:
                    self.assertEqual(response.status_code, expected_status)
                finally:
                    response.close()

    def test_anonymous_user_cannot_open_documentation(self):
        self._assert_routes_status(
            self.documentation_routes,
            403,
            authenticated=False,
        )

    def test_client_user_cannot_open_documentation(self):
        self._assert_routes_status(
            self.documentation_routes,
            403,
            authenticated=True,
            role=None,
        )

    def test_manager_cannot_open_documentation(self):
        self._assert_routes_status(
            self.documentation_routes,
            403,
            authenticated=True,
            role="manager",
        )

    def test_developer_can_open_documentation(self):
        self._assert_routes_status(
            self.documentation_routes,
            200,
            authenticated=True,
            role="developer",
        )

    def test_admin_can_open_documentation(self):
        self._assert_routes_status(
            self.documentation_routes,
            200,
            authenticated=True,
            role="admin",
        )

    def test_scanner_pages_remain_available_to_internal_manager(self):
        self._assert_routes_status(
            self.scanner_routes,
            200,
            authenticated=True,
            role="manager",
        )
