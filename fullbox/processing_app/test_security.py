from types import SimpleNamespace
from unittest.mock import patch

from django.test import RequestFactory, SimpleTestCase

from .web_ui import processing_flow_container_code


class ProcessingContainerCodeSecurityTests(SimpleTestCase):
    def setUp(self):
        self.request_factory = RequestFactory()

    @patch("processing_app.web_ui.issue_pallet_code")
    @patch("processing_app.web_ui.issue_container_code")
    def test_get_is_rejected_before_container_code_write_path(self, issue_container_code, issue_pallet_code):
        request = self.request_factory.get("/orders/processing/P-1/flow/container-code/")
        request.user = SimpleNamespace(is_authenticated=True, is_staff=True)

        response = processing_flow_container_code(request, "P-1")

        self.assertEqual(response.status_code, 405)
        issue_container_code.assert_not_called()
        issue_pallet_code.assert_not_called()

    def test_anonymous_post_is_redirected_to_login(self):
        request = self.request_factory.post(
            "/orders/processing/P-1/flow/container-code/",
            data="{}",
            content_type="application/json",
        )
        request.user = SimpleNamespace(is_authenticated=False)

        response = processing_flow_container_code(request, "P-1")

        self.assertEqual(response.status_code, 302)
        self.assertIn("/login/", response.url)

    def test_endpoint_is_not_csrf_exempt(self):
        self.assertFalse(getattr(processing_flow_container_code, "csrf_exempt", False))
