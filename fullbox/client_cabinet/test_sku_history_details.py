from django.contrib.auth import get_user_model
from django.test import TestCase

from audit.models import log_sku_change
from sku.audit_history import build_sku_audit_snapshot, sku_audit_snapshot
from sku.models import Agency, SKU


class ClientSKUHistoryDetailsTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="sku_history_manager",
            password="pwd",
            is_staff=True,
        )
        self.agency = Agency.objects.create(agn_name="Клиент истории SKU")
        self.client.force_login(self.user)

    def test_history_modal_shows_exact_before_and_after_values(self):
        sku = SKU.objects.create(
            agency=self.agency,
            sku_code="HISTORY-1",
            name="Товар",
            length_mm="100.0",
        )
        before = sku_audit_snapshot(sku)
        sku.length_mm = "440.0"
        sku.save()
        snapshot = build_sku_audit_snapshot(
            sku,
            before=before,
            source="marketplace",
            marketplace="WB",
        )
        log_sku_change(
            "update",
            sku,
            description="Синхронизация WB: изменена длина.",
            snapshot=snapshot,
        )

        response = self.client.get(
            f"/client/{self.agency.pk}/sku/{sku.pk}/edit/"
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "История изменений")
        self.assertContains(response, "Длина")
        self.assertContains(response, "100.0 мм")
        self.assertContains(response, "440.0 мм")
        self.assertContains(response, "Системное изменение")

    def test_history_does_not_attribute_unlogged_update_to_previous_user(self):
        sku = SKU.objects.create(
            agency=self.agency,
            sku_code="HISTORY-OLD",
            name="Товар",
        )

        response = self.client.get(
            f"/client/{self.agency.pk}/sku/{sku.pk}/edit/"
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            "Системное изменение, детали не зафиксированы",
        )
        self.assertContains(response, "прежняя версия журнала не сохранила")
