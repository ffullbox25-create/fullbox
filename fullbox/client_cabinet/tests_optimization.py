from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase

from . import api_views


class ClientDashboardCacheOptimizationTests(SimpleTestCase):
    @mock.patch("client_cabinet.api_views._cache_set")
    @mock.patch("client_cabinet.api_views._build_request_payloads")
    @mock.patch("client_cabinet.api_views._cache_get")
    def test_request_rows_use_shared_django_cache(self, cache_get, build_rows, cache_set):
        rows = [{"order_id": "42"}]
        cache_get.side_effect = [None, rows]
        build_rows.return_value = rows
        agency = SimpleNamespace(id=7)

        self.assertEqual(api_views._request_payloads(agency), rows)
        self.assertEqual(api_views._request_payloads(agency), rows)

        build_rows.assert_called_once_with(agency)
        cache_set.assert_called_once_with(
            "client-cabinet:request-payloads:v1:7",
            rows,
            api_views._REQUEST_PAYLOADS_TTL_SEC,
        )

    @mock.patch("client_cabinet.api_views.build_live_dashboard")
    @mock.patch("client_cabinet.api_views._cache_add", return_value=False)
    @mock.patch("client_cabinet.api_views._cache_get")
    def test_live_dashboard_uses_stale_value_during_refresh(
        self,
        cache_get,
        cache_add,
        build_live_dashboard,
    ):
        stale = {"status": "online", "stock": {"units": 12}}
        cache_get.side_effect = [None, stale]

        result = api_views._live_dashboard(SimpleNamespace(id=7))

        self.assertEqual(result, stale)
        cache_add.assert_called_once()
        build_live_dashboard.assert_not_called()
