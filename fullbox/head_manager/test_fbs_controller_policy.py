from django.contrib.auth import get_user_model
from django.test import TestCase

from employees.models import Employee
from fbs.models import FbsControllerPolicy


class HeadManagerFbsControllerPolicyTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.manager_user = user_model.objects.create_user(
            username="policy_head_manager",
            password="pwd",
        )
        Employee.objects.create(
            full_name="Главный менеджер настройки FBS",
            role="head_manager",
            user=self.manager_user,
            is_active=True,
        )
        self.controller_user = user_model.objects.create_user(
            username="policy_fbs_controller",
            password="pwd",
        )
        self.controller = Employee.objects.create(
            full_name="Контролёр настройки FBS",
            role="fbs_controller",
            user=self.controller_user,
            is_active=True,
        )
        self.client.force_login(self.manager_user)

    def test_policy_can_be_saved_for_controller_with_user(self):
        response = self.client.post(
            f"/head-manager/fbs/controllers/{self.controller.pk}/policy/",
            {"skip_repeat_wb_label_scan": "on"},
        )

        self.assertRedirects(
            response,
            "/head-manager/fbs/controllers/",
            fetch_redirect_response=False,
        )
        policy = FbsControllerPolicy.objects.get(
            controller=self.controller_user,
        )
        self.assertTrue(policy.skip_repeat_wb_label_scan)
        self.assertEqual(policy.updated_by, self.manager_user)
