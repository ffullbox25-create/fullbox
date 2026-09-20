from django.contrib.auth import get_user_model
from django.test import TestCase

from employees.models import Employee


class StorekeeperFbsKnowledgeTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="storekeeper_fbs_kb",
            password="pwd",
        )
        Employee.objects.create(
            full_name="Оператор FBS",
            user=self.user,
            role="storekeeper",
            is_active=True,
        )
        self.client.force_login(self.user)

    def test_catalog_features_fbs_operator_guide(self):
        response = self.client.get("/sklad/knowledge/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "FBS: работа оператора-кладовщика")
        self.assertContains(response, 'href="/sklad/knowledge/fbs-storekeeper-operator/"')
        self.assertContains(response, "полный порядок смены")

    def test_fbs_operator_guide_contains_workflow_and_print_controls(self):
        response = self.client.get("/sklad/knowledge/fbs-storekeeper-operator/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Печать инструкции")
        self.assertContains(response, "Подача волны")
        self.assertContains(response, "Проверка товара, КИЗ и срока годности")
        self.assertContains(response, "Короба и подготовка передачи")
        self.assertContains(response, "Контрольный лист смены")
        self.assertContains(response, "Подпись сотрудника")
        self.assertContains(response, "Общий склад для подсорта")
        self.assertContains(response, 'href="/fbs/tsd/storekeeper/"')

    def test_picker_cannot_open_storekeeper_knowledge_base(self):
        picker = get_user_model().objects.create_user(username="picker_fbs_kb", password="pwd")
        Employee.objects.create(
            full_name="Сборщик FBS",
            user=picker,
            role="picker",
            is_active=True,
        )
        self.client.force_login(picker)

        response = self.client.get("/sklad/knowledge/fbs-storekeeper-operator/")

        self.assertEqual(response.status_code, 403)
