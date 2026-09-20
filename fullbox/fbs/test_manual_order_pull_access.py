from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from accountant.models import ClientLifecycle
from accountant.selectors import ensure_lifecycle
from employees.models import Employee
from fbs.exceptions import FbsIntegrationError
from fbs.services.sync import MarketplaceSyncResult, pull_profile_orders_manually
from sku.models import Agency


class ManualOrderPullAccessTests(TestCase):
    def setUp(self):
        self.manager_user = get_user_model().objects.create_user(
            username="manual_fbs_manager",
            password="pwd",
        )
        Employee.objects.create(
            full_name="Менеджер ручного обновления FBS",
            user=self.manager_user,
            role="manager",
            is_active=True,
        )
        self.agency = Agency.objects.create(
            agn_name="Клиент ручного обновления FBS",
            inn="7700999101",
            pref="MFS",
            mened_user_id=self.manager_user.id,
        )
        ensure_lifecycle(
            self.agency,
            status=ClientLifecycle.STATUS_ACTIVE,
            user=self.manager_user,
        )

    @patch("fbs.services.sync._pull_profile_orders")
    @patch("fbs.services.sync._profile_for_sync")
    def test_assigned_manager_can_pull_wb_and_ozon_orders(
        self,
        profile_for_sync,
        pull_orders,
    ):
        expected = MarketplaceSyncResult(received=2, created=2)
        pull_orders.return_value = expected

        for profile_id, marketplace in ((41, "wb"), (42, "ozon")):
            profile = SimpleNamespace(
                pk=profile_id,
                agency_id=self.agency.id,
                marketplace=marketplace,
            )
            profile_for_sync.return_value = profile

            result = pull_profile_orders_manually(
                profile_id=profile_id,
                actor=self.manager_user,
            )

            self.assertEqual(result, expected)
            self.assertIs(pull_orders.call_args.kwargs["loaded_profile"], profile)

        self.assertEqual(pull_orders.call_count, 2)

    @patch("fbs.services.sync._pull_profile_orders")
    @patch("fbs.services.sync._profile_for_sync")
    def test_manager_cannot_pull_another_managers_orders(
        self,
        profile_for_sync,
        pull_orders,
    ):
        other_user = get_user_model().objects.create_user(
            username="other_manual_fbs_manager",
            password="pwd",
        )
        Employee.objects.create(
            full_name="Другой менеджер ручного обновления FBS",
            user=other_user,
            role="manager",
            is_active=True,
        )
        profile_for_sync.return_value = SimpleNamespace(
            pk=43,
            agency_id=self.agency.id,
            marketplace="wb",
        )

        with self.assertRaisesRegex(FbsIntegrationError, "Нет доступа"):
            pull_profile_orders_manually(profile_id=43, actor=other_user)

        pull_orders.assert_not_called()

    @patch("fbs.services.sync._pull_profile_orders")
    @patch("fbs.services.sync._profile_for_sync")
    def test_unrelated_employee_role_cannot_pull_orders(
        self,
        profile_for_sync,
        pull_orders,
    ):
        storekeeper_user = get_user_model().objects.create_user(
            username="manual_fbs_storekeeper",
            password="pwd",
        )
        Employee.objects.create(
            full_name="Кладовщик без ручного обновления FBS",
            user=storekeeper_user,
            role="storekeeper",
            is_active=True,
        )
        profile_for_sync.return_value = SimpleNamespace(
            pk=44,
            agency_id=self.agency.id,
            marketplace="ozon",
        )

        with self.assertRaisesRegex(FbsIntegrationError, "только менеджерам"):
            pull_profile_orders_manually(profile_id=44, actor=storekeeper_user)

        pull_orders.assert_not_called()
