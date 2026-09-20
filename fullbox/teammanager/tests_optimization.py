from django.test import SimpleTestCase

from .views import _billing_application_url


class TeamManagerBulkCacheTests(SimpleTestCase):
    def test_billing_url_uses_preloaded_application_id(self):
        result = _billing_application_url(
            task_type="shipping",
            order_id="SO-42",
            client_id=7,
            cache={("shipping", "SO-42", 7): 123},
        )

        self.assertEqual(result, "/team-manager/billing/applications/123/")

    def test_billing_url_uses_preloaded_miss(self):
        result = _billing_application_url(
            task_type="processing",
            order_id="42",
            client_id=7,
            cache={("processing", "42", 7): None},
        )

        self.assertEqual(result, "/team-manager/billing/applications/")
