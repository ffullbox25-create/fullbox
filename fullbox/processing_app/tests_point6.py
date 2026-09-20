from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import RequestFactory, TestCase
from django.utils import timezone

from audit.models import OrderAuditEntry
from billing.models import (
    BillingApplication,
    BillingService,
    ClientTariffItem,
    ClientTariffVersion,
    TariffCategory,
    TariffUnit,
    WarehouseServiceFact,
)
from billing.service_catalog import ensure_service_catalog
from billing.warehouse_services import (
    replace_warehouse_facts,
    sync_processing_facts_to_billing,
)
from employees.models import Employee
from sku.models import Agency

from .services import ProcessingWorkflowService
from .stages import PROCESSING_STAGE_DONE, PROCESSING_STAGE_QUALITY_APPROVED
from .views import _processing_work_payload_from_entries


class ProcessingServiceFactsCloseTests(TestCase):
    def setUp(self):
        ensure_service_catalog()
        self.user = get_user_model().objects.create_user(
            username="processing_point6_head",
            password="pwd",
        )
        Employee.objects.create(
            user=self.user,
            full_name="Руководитель обработки",
            role="processing_head",
            is_active=True,
        )
        self.agency = Agency.objects.create(agn_name="Клиент обработки пункт 6")
        self.service = BillingService.objects.get(code="processing_marking")
        category, _created = TariffCategory.objects.get_or_create(
            code="processing",
            defaults={"name": "Обработка", "sort_order": 30, "is_active": True},
        )
        unit, _created = TariffUnit.objects.get_or_create(
            code="pcs",
            defaults={"name": "Штука", "short_name": "шт", "is_active": True},
        )
        version = ClientTariffVersion.objects.create(
            client=self.agency,
            name="Тариф обработки",
            version_number=1,
            status=ClientTariffVersion.STATUS_DRAFT,
            valid_from=timezone.localdate(),
        )
        ClientTariffItem.objects.create(
            tariff_version=version,
            category=category,
            service=self.service,
            service_name=self.service.name,
            unit=unit,
            price=Decimal("8.0000"),
            is_active=True,
        )
        version.status = ClientTariffVersion.STATUS_ACTIVE
        version.save(update_fields=["status"])
        self.request_factory = RequestFactory()

    def _ready_entries(self, order_id: str):
        work_entry = OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="processing",
            action="status",
            agency=self.agency,
            payload={
                "status": "processing_in_work",
                "status_label": "Взята в работу",
                "processing_stage": PROCESSING_STAGE_QUALITY_APPROVED,
                "processing_stage_label": "Проверка качества пройдена",
                "quality_review": {"status": "approved"},
                "cards": [
                    {
                        "id": "card-a",
                        "article": "SKU-A",
                        "rows": [{"size": "42", "qty": "10"}],
                    }
                ],
                "processed_cards": ["card-a"],
                "processing_results": [
                    {
                        "card_id": "card-a",
                        "article": "SKU-A",
                        "size": "42",
                        "destination": "-",
                        "processed": "10",
                    }
                ],
            },
        )
        placement_entry = OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="processing",
            action="update",
            agency=self.agency,
            payload={
                "act": "placement",
                "act_state": "closed",
                "act_boxes": [
                    {"code": "BOX-POINT6-1", "items": [{"sku": "SKU-A", "qty": 10}]}
                ],
                "act_pallets": [
                    {
                        "code": "PAL-POINT6-1",
                        "boxes": ["BOX-POINT6-1"],
                        "items": [],
                        "location": {
                            "zone": "OBR",
                            "row": "",
                            "section": "",
                            "tier": "",
                            "cell": "",
                        },
                    }
                ],
            },
        )
        return [work_entry, placement_entry]

    def _save_fact(self, order_id: str, *, quantity="10", auto_quantity="10"):
        return replace_warehouse_facts(
            client=self.agency,
            order_type="processing",
            order_id=order_id,
            lines=[
                {
                    "service_id": self.service.id,
                    "planned_quantity": "10",
                    "auto_quantity": auto_quantity,
                    "quantity": quantity,
                    "source": "processing_auto"
                    if Decimal(quantity) == Decimal(auto_quantity)
                    else "processing_manual_adjustment",
                    "discrepancy_reason": ""
                    if Decimal(quantity) == Decimal(auto_quantity)
                    else "Подтверждённая ручная корректировка",
                }
            ],
            user=self.user,
        )[0]

    def test_manual_adjustment_requires_reason_and_keeps_auto_quantity(self):
        with self.assertRaisesMessage(ValidationError, "причину ручной корректировки"):
            replace_warehouse_facts(
                client=self.agency,
                order_type="processing",
                order_id="OBR-POINT6-ADJUST",
                lines=[
                    {
                        "service_id": self.service.id,
                        "auto_quantity": "10",
                        "quantity": "12",
                        "source": "processing_manual_adjustment",
                    }
                ],
                user=self.user,
            )

        fact = self._save_fact("OBR-POINT6-ADJUST", quantity="12", auto_quantity="10")

        self.assertEqual(fact.metadata["auto_quantity"], "10")
        self.assertTrue(fact.is_manual)
        self.assertEqual(fact.discrepancy_reason, "Подтверждённая ручная корректировка")

    def test_work_context_builds_service_metrics_from_processing_truth(self):
        order_id = "OBR-POINT6-METRICS"
        entries = self._ready_entries(order_id)
        request = self.request_factory.get(f"/orders/processing/{order_id}/work/")
        request.user = self.user

        context = ProcessingWorkflowService.build_processing_work_page_context(
            order_id=order_id,
            entries=entries,
            payload=_processing_work_payload_from_entries(entries),
            agency=self.agency,
            request=request,
        )

        self.assertEqual(
            context["processing_service_metrics"],
            {
                "declared_qty": 10,
                "processed_qty": 10,
                "boxed_qty": 10,
                "matches": True,
                "boxes_count": 1,
                "pallets_count": 1,
            },
        )

    def test_sync_creates_completed_billing_application_without_charges(self):
        order_id = "OBR-POINT6-BILLING"
        fact = self._save_fact(order_id)

        application, facts = sync_processing_facts_to_billing(
            client=self.agency,
            order_id=order_id,
            completed_at=timezone.now(),
            user=self.user,
            source_payload={
                "quantity_summary": {
                    "declared_qty": 10,
                    "processed_qty": 10,
                    "boxed_qty": 10,
                }
            },
        )

        self.assertIsNotNone(application)
        self.assertEqual(application.application_type, BillingApplication.TYPE_PROCESSING)
        self.assertTrue(application.is_operations_completed)
        self.assertEqual(application.charges.count(), 0)
        self.assertEqual([item.id for item in facts], [fact.id])
        fact.refresh_from_db()
        self.assertEqual(fact.application_id, application.id)
        self.assertEqual(fact.status, WarehouseServiceFact.STATUS_SENT_TO_BILLING)
        self.assertNotIn("price", str(application.source_payload).lower())

    def test_finish_is_idempotent_and_transfers_facts_once(self):
        order_id = "OBR-POINT6-CLOSE"
        entries = self._ready_entries(order_id)
        fact = self._save_fact(order_id)
        request = self.request_factory.post(
            f"/orders/processing/{order_id}/work/",
            data={"action": "finish_processing"},
        )
        request.user = self.user
        finish_checks = {
            "blockers": [],
            "quantity_summary": {
                "declared_qty": 10,
                "processed_qty": 10,
                "boxed_qty": 10,
                "matches": True,
            },
            "placement_payload": {
                "act_boxes": [{"code": "BOX-POINT6-1"}],
                "act_pallets": [{"code": "PAL-POINT6-1"}],
            },
            "placement_closed": True,
            "has_boxes": True,
            "has_pallets": True,
            "warehouse_move_created": True,
            "warehouse_move_completed": True,
            "warehouse_move_progress": {"total_pallets": 1, "done_count": 1},
        }

        with mock.patch(
            "processing_app.views._processing_finish_checks",
            return_value=finish_checks,
        ), mock.patch(
            "processing_app.views._processing_discrepancy_rows",
            return_value=[],
        ):
            first_result = ProcessingWorkflowService.finish_processing(
                order_id=order_id,
                entries=entries,
                request=request,
                role="processing_head",
            )
            second_result = ProcessingWorkflowService.finish_processing(
                order_id=order_id,
                entries=entries,
                request=request,
                role="processing_head",
            )

        self.assertEqual(first_result.status, "done")
        self.assertEqual(second_result.status, "already_done")
        application = BillingApplication.objects.get(
            application_type=BillingApplication.TYPE_PROCESSING,
            application_id=order_id,
            client=self.agency,
        )
        self.assertTrue(application.is_operations_completed)
        self.assertEqual(application.charges.count(), 0)
        fact.refresh_from_db()
        self.assertEqual(fact.application_id, application.id)
        done_entries = OrderAuditEntry.objects.filter(
            order_id=order_id,
            order_type="processing",
            payload__processing_stage=PROCESSING_STAGE_DONE,
        )
        self.assertEqual(done_entries.count(), 1)
        transfer = (done_entries.first().payload or {}).get("billing_transfer") or {}
        self.assertEqual(transfer.get("status"), "sent_to_billing")
        self.assertEqual(transfer.get("application_id"), application.id)
        self.assertEqual(transfer.get("service_fact_ids"), [fact.id])
