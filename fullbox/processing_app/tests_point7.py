from decimal import Decimal
from types import SimpleNamespace
from unittest import mock

from django.contrib.auth import get_user_model
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
from billing.warehouse_services import replace_warehouse_facts
from employees.models import Employee
from sku.models import Agency
from todo.models import Task

from .services import ProcessingWorkflowService
from .stages import (
    PROCESSING_STAGE_AWAITING_APPROVAL,
    PROCESSING_STAGE_DONE,
    PROCESSING_STAGE_MANAGER_APPROVED,
    PROCESSING_STAGE_OBR_ARRIVED,
    PROCESSING_STAGE_OBR_MOVE_CREATED,
    PROCESSING_STAGE_QUALITY_APPROVED,
    PROCESSING_STAGE_QUALITY_CONTROL,
    PROCESSING_STAGE_TAKEN_INTO_PROCESSING,
    PROCESSING_STAGE_UNBOXING_COMPLETED,
    PROCESSING_STAGE_UNBOXING_OPENED,
    log_processing_stage,
)
from .views import _processing_work_payload_from_entries


class ProcessingEndToEndPointSevenTests(TestCase):
    quantity = 10_000

    def setUp(self):
        ensure_service_catalog()
        user_model = get_user_model()
        self.manager_user = user_model.objects.create_user(
            username="processing_point7_manager",
            password="pwd",
        )
        Employee.objects.create(
            user=self.manager_user,
            full_name="Менеджер обработки",
            role="manager",
            is_active=True,
        )
        self.head_user = user_model.objects.create_user(
            username="processing_point7_head",
            password="pwd",
        )
        self.head = Employee.objects.create(
            user=self.head_user,
            full_name="Руководитель обработки",
            role="processing_head",
            is_active=True,
        )
        self.worker_user = user_model.objects.create_user(
            username="processing_point7_worker",
            password="pwd",
        )
        self.worker = Employee.objects.create(
            user=self.worker_user,
            full_name="Исполнитель обработки",
            role="processing_worker",
            is_active=True,
        )
        self.agency = Agency.objects.create(agn_name="Клиент обработки пункт 7")
        self.service = BillingService.objects.get(code="processing_marking")
        category, _created = TariffCategory.objects.get_or_create(
            code="processing",
            defaults={"name": "Обработка", "sort_order": 30, "is_active": True},
        )
        unit, _created = TariffUnit.objects.get_or_create(
            code="pcs",
            defaults={"name": "Штука", "short_name": "шт", "is_active": True},
        )
        tariff = ClientTariffVersion.objects.create(
            client=self.agency,
            name="Тариф обработки E2E",
            version_number=1,
            status=ClientTariffVersion.STATUS_DRAFT,
            valid_from=timezone.localdate(),
        )
        ClientTariffItem.objects.create(
            tariff_version=tariff,
            category=category,
            service=self.service,
            service_name=self.service.name,
            unit=unit,
            price=Decimal("8.0000"),
            is_active=True,
        )
        tariff.status = ClientTariffVersion.STATUS_ACTIVE
        tariff.save(update_fields=["status"])
        self.request_factory = RequestFactory()

    def _entries(self, order_id):
        return list(
            OrderAuditEntry.objects.filter(
                order_id=order_id,
                order_type="processing",
            )
            .select_related("agency")
            .order_by("created_at", "id")
        )

    def _payload(self, order_id):
        return _processing_work_payload_from_entries(self._entries(order_id))

    def _request(self, order_id, user, data):
        request = self.request_factory.post(
            f"/orders/processing/{order_id}/work/",
            data=data,
        )
        request.user = user
        return request

    def _advance(self, order_id, stage, description, *, user=None, extra_payload=None):
        logged, payload = log_processing_stage(
            order_id=order_id,
            payload=self._payload(order_id),
            stage=stage,
            description=description,
            user=user,
            agency=self.agency,
            extra_payload=extra_payload,
        )
        self.assertTrue(logged)
        return payload

    def test_full_processing_chain_is_ordered_idempotent_and_billing_safe(self):
        order_id = "OBR-POINT7-10K"
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="processing",
            action="status",
            agency=self.agency,
            user=self.manager_user,
            description="Клиент отправил заявку на обработку",
            payload={
                "status": "sent_unconfirmed",
                "status_label": "Ждет подтверждения",
                "processing_stage": PROCESSING_STAGE_AWAITING_APPROVAL,
                "processing_stage_label": "Ждет подтверждения",
                "cards": [
                    {
                        "id": "card-10k",
                        "article": "SKU-POINT7",
                        "product_name": "Товар для обработки 10 000 единиц",
                        "rows": [
                            {
                                "size": "42",
                                "barcode": "BAR-POINT7",
                                "qty": str(self.quantity),
                            }
                        ],
                    }
                ],
            },
        )

        manager_request = self._request(
            order_id,
            self.manager_user,
            {"action": "approve_processing"},
        )
        manager_result = ProcessingWorkflowService.handle_processing_detail_action(
            order_id=order_id,
            request=manager_request,
            order_type="processing",
            payload_from_entries=_processing_work_payload_from_entries,
        )
        self.assertEqual(manager_result.status, "ok")
        self.assertTrue(
            Task.objects.filter(
                route=f"/orders/processing/{order_id}/",
                assigned_to=self.head,
            ).exists()
        )

        self._advance(
            order_id,
            PROCESSING_STAGE_OBR_MOVE_CREATED,
            "Создано задание ричтраку на подачу товара в OBR",
            user=self.head_user,
        )
        self._advance(
            order_id,
            PROCESSING_STAGE_OBR_ARRIVED,
            "Товар доставлен в зону обработки",
            user=self.head_user,
        )

        head_take_request = self._request(
            order_id,
            self.head_user,
            {"action": "take_processing"},
        )
        with mock.patch(
            "processing_app.web_ui.WarehouseCommandService.take_processing",
            return_value=SimpleNamespace(status="started"),
        ):
            take_result = ProcessingWorkflowService.handle_processing_detail_action(
                order_id=order_id,
                request=head_take_request,
                order_type="processing",
                payload_from_entries=_processing_work_payload_from_entries,
            )
        self.assertEqual(take_result.status, "ok")

        self._advance(
            order_id,
            PROCESSING_STAGE_UNBOXING_OPENED,
            "Обработка товара начата",
            user=self.worker_user,
        )
        box_codes = [f"BOX-POINT7-{index:03d}" for index in range(1, 101)]
        completed_payload = {
            "processed_cards": ["card-10k"],
            "processing_results": [
                {
                    "card_id": "card-10k",
                    "article": "SKU-POINT7",
                    "size": "42",
                    "barcode": "BAR-POINT7",
                    "destination": "-",
                    "processed": str(self.quantity),
                }
            ],
            "act": "placement",
            "act_state": "closed",
            "flow_closed": True,
            "act_boxes": [
                {
                    "code": code,
                    "items": [{"sku": "SKU-POINT7", "size": "42", "qty": 100}],
                }
                for code in box_codes
            ],
            "act_pallets": [
                {
                    "code": "PAL-POINT7-001",
                    "boxes": box_codes,
                    "items": [],
                    "location": {"zone": "OBR"},
                }
            ],
        }
        self._advance(
            order_id,
            PROCESSING_STAGE_UNBOXING_COMPLETED,
            "Обработка и формирование коробов завершены",
            user=self.worker_user,
            extra_payload=completed_payload,
        )

        quality_request = self._request(
            order_id,
            self.head_user,
            {
                "action": "approve_quality",
                "quality_quantity_checked": "1",
                "quality_processing_checked": "1",
                "quality_marking_checked": "1",
                "quality_packaging_checked": "1",
                "quality_comment": "Количество и результат проверены",
            },
        )
        with mock.patch(
            "processing_app.views._processing_finish_checks",
            return_value={"hard_blockers": []},
        ):
            quality_result = ProcessingWorkflowService.review_processing_quality(
                order_id=order_id,
                entries=self._entries(order_id),
                request=quality_request,
                role="processing_head",
            )
            repeated_quality_result = ProcessingWorkflowService.review_processing_quality(
                order_id=order_id,
                entries=self._entries(order_id),
                request=quality_request,
                role="processing_head",
            )
        self.assertEqual(quality_result.status, "approved")
        self.assertEqual(repeated_quality_result.status, "already_approved")

        fact = replace_warehouse_facts(
            client=self.agency,
            order_type="processing",
            order_id=order_id,
            lines=[
                {
                    "service_id": self.service.id,
                    "planned_quantity": str(self.quantity),
                    "auto_quantity": str(self.quantity),
                    "quantity": str(self.quantity),
                    "source": "processing_auto",
                }
            ],
            user=self.head_user,
        )[0]
        manager_act_request = self._request(
            order_id,
            self.manager_user,
            {"action": "confirm_processing_act_manager"},
        )
        finish_checks = {
            "blockers": [],
            "quantity_summary": {
                "declared_qty": self.quantity,
                "processed_qty": self.quantity,
                "boxed_qty": self.quantity,
                "matches": True,
            },
            "placement_payload": completed_payload,
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
        ), mock.patch(
            "sklad.services.warehouse_write_path.WarehouseWritePathService.consume_processing_input",
        ), mock.patch(
            "sklad.services.warehouse_write_path.WarehouseWritePathService.complete_processing_if_started",
        ), mock.patch(
            "processing_reachtruck.services.complete_obr_move_requests_for_processing",
        ):
            manager_act_result = ProcessingWorkflowService.handle_processing_detail_action(
                order_id=order_id,
                request=manager_act_request,
                order_type="processing",
                payload_from_entries=_processing_work_payload_from_entries,
            )
            repeated_finish_result = ProcessingWorkflowService.finish_processing(
                order_id=order_id,
                entries=self._entries(order_id),
                request=manager_act_request,
                role="manager",
            )

        self.assertEqual(manager_act_result.status, "ok")
        self.assertEqual(repeated_finish_result.status, "already_done")
        stages = list(
            OrderAuditEntry.objects.filter(
                order_id=order_id,
                order_type="processing",
            )
            .exclude(payload__processing_stage="")
            .order_by("created_at", "id")
            .values_list("payload__processing_stage", flat=True)
        )
        self.assertEqual(
            stages,
            [
                PROCESSING_STAGE_AWAITING_APPROVAL,
                PROCESSING_STAGE_MANAGER_APPROVED,
                PROCESSING_STAGE_OBR_MOVE_CREATED,
                PROCESSING_STAGE_OBR_ARRIVED,
                PROCESSING_STAGE_TAKEN_INTO_PROCESSING,
                PROCESSING_STAGE_UNBOXING_OPENED,
                PROCESSING_STAGE_UNBOXING_COMPLETED,
                PROCESSING_STAGE_QUALITY_CONTROL,
                PROCESSING_STAGE_QUALITY_APPROVED,
                PROCESSING_STAGE_DONE,
            ],
        )
        application = BillingApplication.objects.get(
            application_type=BillingApplication.TYPE_PROCESSING,
            application_id=order_id,
            client=self.agency,
        )
        self.assertTrue(application.is_operations_completed)
        self.assertEqual(application.charges.count(), 0)
        fact.refresh_from_db()
        self.assertEqual(fact.application_id, application.id)
        self.assertEqual(fact.status, WarehouseServiceFact.STATUS_SENT_TO_BILLING)
        self.assertEqual(
            OrderAuditEntry.objects.filter(
                order_id=order_id,
                order_type="processing",
                payload__processing_stage=PROCESSING_STAGE_DONE,
            ).count(),
            1,
        )
