from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from employees.models import Employee
from fbs.integrations.warehouse_catalog import fetch_client_warehouses
from fbs.models import FbsIntegrationProfile
from fbs.services.client_profiles import (
    SelectedMarketplaceWarehouse,
    configure_client_fbs_profiles,
    is_client_fbs_enabled,
)
from sku.models import Agency, Market, MarketCredential


def _catalog(*, wb=(), ozon=(), wb_error="", ozon_error=""):
    return {
        "wb": {
            "marketplace": "wb",
            "label": "Wildberries",
            "credentials_configured": True,
            "client_id_configured": False,
            "warehouses": list(wb),
            "error": wb_error,
        },
        "ozon": {
            "marketplace": "ozon",
            "label": "Ozon",
            "credentials_configured": True,
            "client_id_configured": True,
            "warehouses": list(ozon),
            "error": ozon_error,
        },
    }


@override_settings(
    ROOT_URLCONF="head_manager.test_urls",
    FBS_MODULE_ENABLED=True,
    FBS_ORDER_PULL_ENABLED=False,
)
class HeadManagerFbsClientSettingsTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.head_manager = user_model.objects.create_user(
            username="hm_fbs_clients",
            password="pwd",
        )
        Employee.objects.create(
            full_name="Начальник склада FBS",
            role="head_manager",
            user=self.head_manager,
            is_active=True,
        )
        self.storekeeper = user_model.objects.create_user(
            username="store_fbs_clients",
            password="pwd",
        )
        Employee.objects.create(
            full_name="Кладовщик FBS",
            role="storekeeper",
            user=self.storekeeper,
            is_active=True,
        )
        self.agency = Agency.objects.create(
            agn_name="ИП Клиент мультисклад",
            inn="123456789012",
        )
        self.wb = Market.objects.create(id=801, name="WB")
        self.ozon = Market.objects.create(id=802, name="OZON")
        MarketCredential.objects.create(
            id=801,
            agency=self.agency,
            market=self.wb,
            market_key="wb-secret",
        )
        MarketCredential.objects.create(
            id=802,
            agency=self.agency,
            market=self.ozon,
            market_key="ozon-secret",
            client_id="ozon-client-1",
        )
        self.client.force_login(self.head_manager)
        self.catalog = _catalog(
            wb=(
                {"marketplace": "wb", "warehouse_id": "101", "name": "WB 1", "status": "Доступен"},
                {"marketplace": "wb", "warehouse_id": "102", "name": "WB 2", "status": "Доступен"},
            ),
            ozon=(
                {"marketplace": "ozon", "warehouse_id": "201", "name": "Ozon 1", "status": "created"},
            ),
        )

    def test_settings_lists_api_client_and_detail_lists_all_warehouses(self):
        settings_response = self.client.get("/head-manager/fbs/settings/")
        with patch(
            "head_manager.web_ui.fetch_client_warehouse_catalog",
            return_value=self.catalog,
        ):
            detail_response = self.client.get(
                f"/head-manager/fbs/settings/clients/{self.agency.id}/"
            )

        self.assertEqual(settings_response.status_code, 200)
        self.assertContains(settings_response, "ИП Клиент мультисклад")
        self.assertContains(
            settings_response,
            f"/head-manager/fbs/settings/clients/{self.agency.id}/",
        )
        self.assertEqual(detail_response.status_code, 200)
        self.assertContains(detail_response, "WB 1")
        self.assertContains(detail_response, "WB 2")
        self.assertContains(detail_response, "Ozon 1")
        self.assertContains(detail_response, "Выгрузка FBS-остатков настраивается отдельно")

    def test_head_manager_enables_multiple_warehouses_without_stock_push(self):
        with patch(
            "head_manager.web_ui.fetch_client_warehouse_catalog",
            return_value=self.catalog,
        ):
            response = self.client.post(
                f"/head-manager/fbs/settings/clients/{self.agency.id}/update/",
                {
                    "fbs_enabled": "1",
                    "warehouses_wb": ["101", "102"],
                    "warehouses_ozon": ["201"],
                    "action": "save",
                },
            )

        self.assertRedirects(
            response,
            f"/head-manager/fbs/settings/clients/{self.agency.id}/",
            fetch_redirect_response=False,
        )
        profiles = FbsIntegrationProfile.objects.filter(agency=self.agency)
        self.assertEqual(profiles.count(), 3)
        self.assertEqual(
            set(profiles.values_list("external_warehouse_id", flat=True)),
            {"101", "102", "201"},
        )
        for profile in profiles:
            self.assertTrue(profile.is_active)
            self.assertTrue(profile.order_pull_enabled)
            self.assertEqual(
                profile.status_pull_enabled,
                profile.marketplace == FbsIntegrationProfile.MARKETPLACE_WB,
            )
            self.assertFalse(profile.outbox_enabled)
            self.assertFalse(profile.marking_push_enabled)
            self.assertFalse(profile.stock_push_enabled)
            self.assertEqual(profile.stock_mode, FbsIntegrationProfile.STOCK_MODE_DISABLED)
        self.assertTrue(is_client_fbs_enabled(self.agency))

    def test_unselected_warehouse_is_disabled_and_client_checkbox_disables_all(self):
        configure_client_fbs_profiles(
            agency=self.agency,
            enabled=True,
            stock_push_keys=frozenset({("wb", "101"), ("wb", "102")}),
            selections=(
                SelectedMarketplaceWarehouse("wb", "101", "WB 1"),
                SelectedMarketplaceWarehouse("wb", "102", "WB 2"),
            ),
        )
        with patch(
            "head_manager.web_ui.fetch_client_warehouse_catalog",
            return_value=self.catalog,
        ):
            self.client.post(
                f"/head-manager/fbs/settings/clients/{self.agency.id}/update/",
                {
                    "fbs_enabled": "1",
                    "warehouses_wb": ["102"],
                    "action": "save",
                },
            )
            self.client.post(
                f"/head-manager/fbs/settings/clients/{self.agency.id}/update/",
                {"action": "save"},
            )

        self.assertFalse(
            FbsIntegrationProfile.objects.filter(
                agency=self.agency,
                is_active=True,
            ).exists()
        )
        self.assertFalse(
            FbsIntegrationProfile.objects.filter(
                agency=self.agency,
                order_pull_enabled=True,
            ).exists()
        )
        self.assertFalse(is_client_fbs_enabled(self.agency))
        for profile in FbsIntegrationProfile.objects.filter(agency=self.agency):
            self.assertFalse(profile.stock_push_enabled)
            self.assertEqual(
                profile.stock_mode,
                FbsIntegrationProfile.STOCK_MODE_DISABLED,
            )

    def test_head_manager_enables_stock_push_only_for_checked_client_warehouse(self):
        with patch(
            "head_manager.web_ui.fetch_client_warehouse_catalog",
            return_value=self.catalog,
        ):
            enabled_response = self.client.post(
                f"/head-manager/fbs/settings/clients/{self.agency.id}/update/",
                {
                    "fbs_enabled": "1",
                    "warehouses_wb": ["101", "102"],
                    "warehouses_ozon": ["201"],
                    "stock_push_warehouses_wb": ["102"],
                    "action": "save",
                },
            )
            enabled_page = self.client.get(
                f"/head-manager/fbs/settings/clients/{self.agency.id}/"
            )

        self.assertEqual(enabled_response.status_code, 302)
        profiles = FbsIntegrationProfile.objects.filter(agency=self.agency)
        self.assertEqual(profiles.count(), 3)
        enabled_profiles = profiles.filter(stock_push_enabled=True)
        self.assertEqual(enabled_profiles.count(), 1)
        enabled_profile = enabled_profiles.get()
        self.assertEqual(enabled_profile.marketplace, FbsIntegrationProfile.MARKETPLACE_WB)
        self.assertEqual(enabled_profile.external_warehouse_id, "102")
        self.assertEqual(enabled_profile.stock_mode, FbsIntegrationProfile.STOCK_MODE_MANAGED)
        for profile in profiles.exclude(pk=enabled_profile.pk):
            self.assertFalse(profile.stock_push_enabled)
            self.assertEqual(profile.stock_mode, FbsIntegrationProfile.STOCK_MODE_DISABLED)
        self.assertContains(
            enabled_page,
            'name="stock_push_warehouses_wb" value="102"',
            html=False,
        )

        with patch(
            "head_manager.web_ui.fetch_client_warehouse_catalog",
            return_value=self.catalog,
        ):
            disabled_response = self.client.post(
                f"/head-manager/fbs/settings/clients/{self.agency.id}/update/",
                {
                    "fbs_enabled": "1",
                    "warehouses_wb": ["101", "102"],
                    "warehouses_ozon": ["201"],
                    "action": "save",
                },
            )

        self.assertEqual(disabled_response.status_code, 302)
        for profile in profiles:
            profile.refresh_from_db()
            self.assertFalse(profile.stock_push_enabled)
            self.assertEqual(
                profile.stock_mode,
                FbsIntegrationProfile.STOCK_MODE_DISABLED,
            )

    def test_unknown_warehouse_id_is_rejected(self):
        with patch(
            "head_manager.web_ui.fetch_client_warehouse_catalog",
            return_value=self.catalog,
        ):
            response = self.client.post(
                f"/head-manager/fbs/settings/clients/{self.agency.id}/update/",
                {
                    "fbs_enabled": "1",
                    "warehouses_wb": ["forged-warehouse"],
                    "action": "save",
                },
                follow=True,
            )

        self.assertContains(response, "Список складов изменился")
        self.assertFalse(FbsIntegrationProfile.objects.filter(agency=self.agency).exists())

    def test_stock_push_for_unselected_warehouse_is_rejected(self):
        with patch(
            "head_manager.web_ui.fetch_client_warehouse_catalog",
            return_value=self.catalog,
        ):
            response = self.client.post(
                f"/head-manager/fbs/settings/clients/{self.agency.id}/update/",
                {
                    "fbs_enabled": "1",
                    "warehouses_wb": ["101"],
                    "stock_push_warehouses_wb": ["102"],
                    "action": "save",
                },
                follow=True,
            )

        self.assertContains(
            response,
            "Выгрузку остатков можно включить только для выбранного FBS-склада.",
        )
        self.assertFalse(FbsIntegrationProfile.objects.filter(agency=self.agency).exists())

    def test_storekeeper_cannot_open_or_update_client_settings(self):
        self.client.force_login(self.storekeeper)
        page = self.client.get(f"/head-manager/fbs/settings/clients/{self.agency.id}/")
        update = self.client.post(
            f"/head-manager/fbs/settings/clients/{self.agency.id}/update/",
            {"fbs_enabled": "1", "warehouses_wb": ["101"]},
        )

        self.assertEqual(page.status_code, 403)
        self.assertEqual(update.status_code, 403)
        self.assertFalse(FbsIntegrationProfile.objects.filter(agency=self.agency).exists())

    def test_head_manager_manually_updates_only_selected_client(self):
        profile = FbsIntegrationProfile.objects.create(
            agency=self.agency,
            marketplace=FbsIntegrationProfile.MARKETPLACE_WB,
            name="WB 1",
            external_warehouse_id="101",
            is_active=True,
            order_pull_enabled=True,
        )
        sync_result = Mock(
            received=3,
            created=2,
            updated=1,
            duplicate=0,
            skipped=0,
        )

        with patch(
            "head_manager.web_ui.pull_profile_orders_manually",
            return_value=sync_result,
        ) as manual_pull:
            response = self.client.post(
                f"/head-manager/fbs/settings/clients/{self.agency.id}/update/",
                {
                    "action": "sync",
                    "return_to": "settings",
                },
            )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/head-manager/fbs/settings/#clients")
        manual_pull.assert_called_once_with(
            profile_id=profile.id,
            actor=self.head_manager,
            limit=1000,
        )

    def test_manual_update_button_is_not_disabled_by_global_timer_flag(self):
        FbsIntegrationProfile.objects.create(
            agency=self.agency,
            marketplace=FbsIntegrationProfile.MARKETPLACE_WB,
            name="WB 1",
            external_warehouse_id="101",
            is_active=True,
            order_pull_enabled=True,
        )
        with patch(
            "head_manager.web_ui.fetch_client_warehouse_catalog",
            return_value=self.catalog,
        ):
            response = self.client.get(
                f"/head-manager/fbs/settings/clients/{self.agency.id}/"
            )

        self.assertContains(response, "Автоматическое обновление заказов выключено")
        self.assertContains(response, "Обновить заказы")
        self.assertNotContains(
            response,
            'value="save_sync" disabled',
            html=False,
        )

    @override_settings(FBS_ORDER_PULL_ENABLED=True)
    def test_enabled_order_pull_explains_automatic_orders_not_client_refresh(self):
        with patch(
            "head_manager.web_ui.fetch_client_warehouse_catalog",
            return_value=self.catalog,
        ):
            detail_response = self.client.get(
                f"/head-manager/fbs/settings/clients/{self.agency.id}/"
            )
        settings_response = self.client.get("/head-manager/fbs/settings/")

        self.assertContains(
            detail_response,
            "Клиент и токены обновляются вручную",
        )
        self.assertContains(
            detail_response,
            "новые заказы этого клиента проверяются автоматически каждые 10 секунд",
        )
        self.assertContains(
            settings_response,
            "Клиенты и токены обновляются вручную",
        )
        self.assertContains(
            settings_response,
            "Новые заказы включенных FBS-клиентов проверяются автоматически каждые 10 секунд",
        )

    def test_wb_catalog_keeps_real_warehouse_ids(self):
        response = Mock(status_code=200)
        response.json.return_value = [
            {"id": 974026, "name": "ОБухово"},
            {"id": 974027, "name": "Подольск", "isDeleting": True},
        ]
        request_func = Mock(return_value=response)

        warehouses = fetch_client_warehouses(
            agency=self.agency,
            marketplace=FbsIntegrationProfile.MARKETPLACE_WB,
            request_func=request_func,
        )

        self.assertEqual(
            [(row.warehouse_id, row.name, row.status) for row in warehouses],
            [
                ("974026", "ОБухово", "Доступен"),
                ("974027", "Подольск", "Удаляется"),
            ],
        )

    def test_ozon_catalog_keeps_real_warehouse_ids(self):
        response = Mock(status_code=200)
        response.json.return_value = {
            "warehouses": [
                {"warehouse_id": 701, "name": "Ozon Москва", "status": "created"},
                {"warehouse_id": 702, "name": "Ozon Казань", "status": "disabled"},
            ],
            "has_next": False,
            "cursor": "",
        }
        request_func = Mock(return_value=response)

        warehouses = fetch_client_warehouses(
            agency=self.agency,
            marketplace=FbsIntegrationProfile.MARKETPLACE_OZON,
            request_func=request_func,
        )

        self.assertEqual(
            [(row.warehouse_id, row.name, row.status) for row in warehouses],
            [
                ("702", "Ozon Казань", "disabled"),
                ("701", "Ozon Москва", "created"),
            ],
        )

    def test_ozon_catalog_reads_all_cursor_pages(self):
        first = Mock(status_code=200)
        first.json.return_value = {
            "warehouses": [{"warehouse_id": 703, "name": "Ozon Тверь"}],
            "has_next": True,
            "cursor": "next-page",
        }
        second = Mock(status_code=200)
        second.json.return_value = {
            "warehouses": [{"warehouse_id": 704, "name": "Ozon Рязань"}],
            "has_next": False,
            "cursor": "",
        }
        request_func = Mock(side_effect=[first, second])

        warehouses = fetch_client_warehouses(
            agency=self.agency,
            marketplace=FbsIntegrationProfile.MARKETPLACE_OZON,
            request_func=request_func,
        )

        self.assertEqual({row.warehouse_id for row in warehouses}, {"703", "704"})
        self.assertEqual(
            request_func.call_args_list[1].kwargs["json"],
            {"limit": 200, "cursor": "next-page"},
        )
        self.assertTrue(
            request_func.call_args_list[0].args[1].endswith("/v2/warehouse/list")
        )
