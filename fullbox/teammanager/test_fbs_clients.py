from copy import deepcopy
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from accountant.models import ClientLifecycle
from accountant.selectors import ensure_lifecycle
from employees.models import Employee
from fbs.models import FbsIntegrationProfile
from sku.models import Agency, Market, MarketCredential


def _catalog():
    return {
        "wb": {
            "marketplace": "wb",
            "label": "Wildberries",
            "credentials_configured": True,
            "client_id_configured": False,
            "warehouses": [
                {
                    "marketplace": "wb",
                    "warehouse_id": "WB-101",
                    "name": "WB Основной",
                    "status": "Доступен",
                }
            ],
            "error": "",
        },
        "ozon": {
            "marketplace": "ozon",
            "label": "Ozon",
            "credentials_configured": False,
            "client_id_configured": False,
            "warehouses": [],
            "error": "",
        },
    }


class TeamManagerFbsClientsTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="manager_fbs_clients",
            password="pwd",
        )
        Employee.objects.create(
            full_name="Менеджер FBS",
            user=self.user,
            role="manager",
            is_active=True,
        )
        self.agency = Agency.objects.create(
            agn_name="Клиент FBS менеджера",
            inn="7700123456",
            pref="MFB",
            mened_user_id=self.user.id,
        )
        ensure_lifecycle(
            self.agency,
            status=ClientLifecycle.STATUS_ACTIVE,
            user=self.user,
        )
        self.wb = Market.objects.create(id=982801, name="WB")
        MarketCredential.objects.create(
            id=982801,
            agency=self.agency,
            market=self.wb,
            market_key="wb-token",
        )
        self.client.force_login(self.user)

    def test_manager_menu_and_fbs_client_list_have_no_warehouse_sections(self):
        response = self.client.get("/team-manager/fbs/clients/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Клиенты FBS")
        self.assertContains(response, "Клиент FBS менеджера")
        self.assertContains(
            response,
            f'/team-manager/fbs/clients/{self.agency.id}/settings/',
        )
        self.assertContains(response, "Подключение и настройки")
        self.assertNotContains(response, "Оборудование FBS")
        self.assertNotContains(response, "Политика хранения")
        self.assertNotContains(response, "Подсорт FBS")

    @patch("teammanager.fbs_clients.fetch_client_warehouse_catalog")
    def test_manager_opens_client_fbs_settings_without_internal_warehouse_ui(self, fetch_catalog):
        fetch_catalog.side_effect = lambda _agency: deepcopy(_catalog())

        response = self.client.get(
            f"/team-manager/fbs/clients/{self.agency.id}/settings/"
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Настройки FBS")
        self.assertContains(response, "WB Основной")
        self.assertContains(response, "Склад маркетплейса")
        self.assertNotContains(response, "Рабочие места")
        self.assertNotContains(response, "Тележки")
        self.assertNotContains(response, "Политика хранения")
        self.assertNotContains(response, "Подсорт FBS")

    @patch("teammanager.fbs_clients.fetch_client_warehouse_catalog")
    def test_manager_can_enable_fbs_for_visible_client(self, fetch_catalog):
        fetch_catalog.side_effect = lambda _agency: deepcopy(_catalog())

        response = self.client.post(
            f"/team-manager/fbs/clients/{self.agency.id}/settings/update/",
            {
                "action": "save",
                "fbs_enabled": "1",
                "warehouses_wb": ["WB-101"],
                "stock_push_warehouses_wb": ["WB-101"],
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response["Location"],
            f"/team-manager/fbs/clients/{self.agency.id}/settings/",
        )
        profile = FbsIntegrationProfile.objects.get(
            agency=self.agency,
            marketplace=FbsIntegrationProfile.MARKETPLACE_WB,
            external_warehouse_id="WB-101",
        )
        self.assertTrue(profile.is_active)
        self.assertTrue(profile.order_pull_enabled)
        self.assertTrue(profile.stock_push_enabled)
        self.assertEqual(profile.stock_mode, FbsIntegrationProfile.STOCK_MODE_MANAGED)

    def test_manager_cannot_open_another_managers_fbs_client(self):
        other_user = get_user_model().objects.create_user(
            username="other_manager_fbs",
            password="pwd",
        )
        Employee.objects.create(
            full_name="Другой менеджер FBS",
            user=other_user,
            role="manager",
            is_active=True,
        )
        other_agency = Agency.objects.create(
            agn_name="Чужой клиент FBS",
            mened_user_id=other_user.id,
        )
        ensure_lifecycle(
            other_agency,
            status=ClientLifecycle.STATUS_ACTIVE,
            user=other_user,
        )
        MarketCredential.objects.create(
            id=982802,
            agency=other_agency,
            market=self.wb,
            market_key="other-token",
        )

        response = self.client.get(
            f"/team-manager/fbs/clients/{other_agency.id}/settings/"
        )

        self.assertEqual(response.status_code, 404)
