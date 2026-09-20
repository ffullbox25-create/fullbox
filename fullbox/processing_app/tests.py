import json
from datetime import timedelta
from io import BytesIO
from unittest import mock
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import quote

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import RequestFactory
from django.test import SimpleTestCase, TestCase
from django.utils import timezone
from openpyxl import Workbook

from audit.models import AuditEntry, OrderAuditEntry
from billing.models import BillingService, WarehouseServiceFact
from employees.models import Employee
from marking.models import MarkingCode
from reachtruck.models import BoxClaim, MoveRequest, MoveRequestItem, MoveTask
from reachtruck.services.task_commands import scan_move_task_step, take_move_task
from sklad.models import (
    WarehouseContainer,
    WarehouseEvent,
    WarehouseOperation,
    WarehouseReserve,
    WarehouseStockSnapshot,
)
from sklad.services import WarehouseStateCode
from sklad.services.stock_availability import StockAvailabilityService
from sklad.services.stock_operations import OperationalStockService
from sklad.services.warehouse_write_path import WarehouseWritePathService
from sklad.test_utils import create_warehouse_snapshot_row
from sku.models import Agency, SKU, SKUBarcode
from shipping.models import ShippingOrder, ShippingOrderItem
from shipping.services import create_pick_tasks, reserve_order, ship_order
from todo.models import Task
from labels.utils import set_print_agent_pause
from orders.services import ReceivingWorkflowService
from orders.web_ui import _latest_payload_from_entries
from processing_reachtruck.services import (
    build_obr_requested_rows,
    create_obr_move_request_response,
)

from .models import ProcessingFlowSession, ProcessingOrderAttachment, ProcessingPrintJob
from .services import ProcessingFinishResult, ProcessingWorkflowService
from .web_ui import processing_flow_close_box
from .stages import (
    PROCESSING_STAGE_AWAITING_APPROVAL,
    PROCESSING_STAGE_CANCELLED,
    PROCESSING_STAGE_DONE,
    PROCESSING_STAGE_DRAFT,
    PROCESSING_STAGE_MANAGER_APPROVED,
    PROCESSING_STAGE_OBR_ARRIVED,
    PROCESSING_STAGE_OBR_MOVE_CREATED,
    PROCESSING_STAGE_QUALITY_APPROVED,
    PROCESSING_STAGE_QUALITY_CONTROL,
    PROCESSING_STAGE_REWORK,
    PROCESSING_STAGE_RETURNED_TO_STOCK,
    PROCESSING_STAGE_RETURN_TO_STOCK_SENT,
    PROCESSING_STAGE_TAKEN_INTO_PROCESSING,
    PROCESSING_STAGE_UNBOXING_COMPLETED,
    PROCESSING_STAGE_UNBOXING_OPENED,
    log_processing_stage,
    processing_stage_transition_allowed,
    processing_stage_transition_error,
)
from .subzones import (
    PROCESSING_SUBZONE_IN,
    PROCESSING_SUBZONE_OUT,
    PROCESSING_SUBZONE_PACK,
    PROCESSING_SUBZONE_QC,
    PROCESSING_SUBZONE_WORK,
    build_processing_subzone_state,
)
from .views import (
    _create_processing_warehouse_moves,
    _processing_discrepancy_rows,
    _processing_finish_checks,
    _sync_processing_obr_remainder_task,
    _processing_quantity_summary,
    _processing_card_sets,
    _processing_active_correction_mode,
    _processing_flow_ready_cards,
    _processing_placement_entry,
    _processing_warehouse_move_progress,
    _processing_reserve_rows_for_order,
    _expected_processing_results,
    _inventory_items_for_agency,
    _processing_box_picker_rows,
    _processing_work_payload_from_entries,
    _processing_results_are_ready,
    _repair_mojibake_text,
    _replace_processing_reserves,
    ProcessingDetailView,
    ProcessingFlowView,
    ProcessingHomeView,
    ProcessingWorkView,
    download_processing_print_agent_cmd,
    download_processing_print_agent_install_cmd,
    processing_print_jobs_recover,
)


class ProcessingStageTransitionTests(TestCase):
    def setUp(self):
        self.agency = Agency.objects.create(agn_name="Processing Stage Agency")

    def test_all_canonical_transitions_are_allowed(self):
        transitions = (
            (None, PROCESSING_STAGE_DRAFT),
            (PROCESSING_STAGE_DRAFT, PROCESSING_STAGE_AWAITING_APPROVAL),
            (PROCESSING_STAGE_AWAITING_APPROVAL, PROCESSING_STAGE_MANAGER_APPROVED),
            (PROCESSING_STAGE_MANAGER_APPROVED, PROCESSING_STAGE_OBR_MOVE_CREATED),
            (PROCESSING_STAGE_MANAGER_APPROVED, PROCESSING_STAGE_OBR_ARRIVED),
            (PROCESSING_STAGE_MANAGER_APPROVED, PROCESSING_STAGE_UNBOXING_OPENED),
            (PROCESSING_STAGE_OBR_MOVE_CREATED, PROCESSING_STAGE_OBR_ARRIVED),
            (PROCESSING_STAGE_OBR_ARRIVED, PROCESSING_STAGE_TAKEN_INTO_PROCESSING),
            (PROCESSING_STAGE_TAKEN_INTO_PROCESSING, PROCESSING_STAGE_UNBOXING_OPENED),
            (PROCESSING_STAGE_UNBOXING_OPENED, PROCESSING_STAGE_UNBOXING_COMPLETED),
            (PROCESSING_STAGE_UNBOXING_COMPLETED, PROCESSING_STAGE_QUALITY_CONTROL),
            (PROCESSING_STAGE_QUALITY_CONTROL, PROCESSING_STAGE_REWORK),
            (PROCESSING_STAGE_REWORK, PROCESSING_STAGE_QUALITY_CONTROL),
            (PROCESSING_STAGE_QUALITY_CONTROL, PROCESSING_STAGE_QUALITY_APPROVED),
            (PROCESSING_STAGE_QUALITY_APPROVED, PROCESSING_STAGE_DONE),
            (PROCESSING_STAGE_QUALITY_APPROVED, PROCESSING_STAGE_RETURN_TO_STOCK_SENT),
            (PROCESSING_STAGE_QUALITY_APPROVED, PROCESSING_STAGE_RETURNED_TO_STOCK),
            (PROCESSING_STAGE_RETURN_TO_STOCK_SENT, PROCESSING_STAGE_RETURNED_TO_STOCK),
            (PROCESSING_STAGE_RETURNED_TO_STOCK, PROCESSING_STAGE_DONE),
        )

        for current_stage, target_stage in transitions:
            with self.subTest(current_stage=current_stage, target_stage=target_stage):
                payload = {"processing_stage": current_stage} if current_stage else {}
                self.assertTrue(processing_stage_transition_allowed(payload, target_stage))
                self.assertEqual(processing_stage_transition_error(payload, target_stage), "")

    def test_transition_matrix_blocks_skips_rollbacks_and_terminal_changes(self):
        blocked = (
            (PROCESSING_STAGE_DRAFT, PROCESSING_STAGE_MANAGER_APPROVED),
            (PROCESSING_STAGE_AWAITING_APPROVAL, PROCESSING_STAGE_DONE),
            (PROCESSING_STAGE_MANAGER_APPROVED, PROCESSING_STAGE_UNBOXING_COMPLETED),
            (PROCESSING_STAGE_UNBOXING_COMPLETED, PROCESSING_STAGE_OBR_ARRIVED),
            (PROCESSING_STAGE_UNBOXING_COMPLETED, PROCESSING_STAGE_DONE),
            (PROCESSING_STAGE_QUALITY_CONTROL, PROCESSING_STAGE_DONE),
            (PROCESSING_STAGE_DONE, PROCESSING_STAGE_AWAITING_APPROVAL),
            (PROCESSING_STAGE_CANCELLED, PROCESSING_STAGE_DRAFT),
        )

        for current_stage, target_stage in blocked:
            with self.subTest(current_stage=current_stage, target_stage=target_stage):
                payload = {"processing_stage": current_stage}
                self.assertFalse(processing_stage_transition_allowed(payload, target_stage))
                self.assertTrue(processing_stage_transition_error(payload, target_stage))

    def test_cancellation_is_allowed_only_before_manager_approval(self):
        self.assertTrue(
            processing_stage_transition_allowed(
                {"processing_stage": PROCESSING_STAGE_DRAFT},
                PROCESSING_STAGE_CANCELLED,
            )
        )
        self.assertTrue(
            processing_stage_transition_allowed(
                {"processing_stage": PROCESSING_STAGE_AWAITING_APPROVAL},
                PROCESSING_STAGE_CANCELLED,
            )
        )
        self.assertFalse(
            processing_stage_transition_allowed(
                {"processing_stage": PROCESSING_STAGE_MANAGER_APPROVED},
                PROCESSING_STAGE_CANCELLED,
            )
        )

    def test_legacy_status_uses_the_same_transition_matrix(self):
        payload = {
            "status": "sent_unconfirmed",
            "status_label": "Ждет подтверждения",
        }

        self.assertTrue(
            processing_stage_transition_allowed(payload, PROCESSING_STAGE_MANAGER_APPROVED)
        )
        self.assertFalse(processing_stage_transition_allowed(payload, PROCESSING_STAGE_DONE))

    def test_invalid_transition_does_not_write_audit_entry_or_change_payload(self):
        payload = {
            "processing_stage": PROCESSING_STAGE_AWAITING_APPROVAL,
            "processing_stage_label": "Ждет подтверждения",
            "status": "sent_unconfirmed",
        }

        logged, next_payload = log_processing_stage(
            order_id="STAGE-INVALID",
            payload=payload,
            stage=PROCESSING_STAGE_DONE,
            description="Недопустимое завершение",
            agency=self.agency,
        )

        self.assertFalse(logged)
        self.assertEqual(next_payload, payload)
        self.assertFalse(
            OrderAuditEntry.objects.filter(
                order_id="STAGE-INVALID",
                order_type="processing",
            ).exists()
        )

    def test_duplicate_transition_is_idempotent(self):
        payload = {
            "processing_stage": PROCESSING_STAGE_MANAGER_APPROVED,
            "status": "processing_head",
        }

        logged, next_payload = log_processing_stage(
            order_id="STAGE-DUPLICATE",
            payload=payload,
            stage=PROCESSING_STAGE_MANAGER_APPROVED,
            description="Повторное подтверждение",
            agency=self.agency,
        )

        self.assertFalse(logged)
        self.assertEqual(next_payload, payload)
        self.assertFalse(
            OrderAuditEntry.objects.filter(
                order_id="STAGE-DUPLICATE",
                order_type="processing",
            ).exists()
        )

    def test_valid_transition_writes_one_audit_entry(self):
        logged, next_payload = log_processing_stage(
            order_id="STAGE-VALID",
            payload={
                "processing_stage": PROCESSING_STAGE_AWAITING_APPROVAL,
                "status": "sent_unconfirmed",
            },
            stage=PROCESSING_STAGE_MANAGER_APPROVED,
            description="Заявка утверждена менеджером",
            agency=self.agency,
        )

        self.assertTrue(logged)
        self.assertEqual(next_payload["processing_stage"], PROCESSING_STAGE_MANAGER_APPROVED)
        self.assertEqual(
            OrderAuditEntry.objects.filter(
                order_id="STAGE-VALID",
                order_type="processing",
                payload__processing_stage=PROCESSING_STAGE_MANAGER_APPROVED,
            ).count(),
            1,
        )


class ProcessingResultsReadinessTests(SimpleTestCase):
    def test_expected_results_fallback_from_cards(self):
        payload = {
            "cards": [
                {
                    "id": "card-a",
                    "article": "SKU-ONE",
                    "rows": [
                        {"size": "42", "qty": "10"},
                        {"size": "43", "qty": "5"},
                    ],
                }
            ]
        }
        expected = _expected_processing_results(payload)
        self.assertEqual(
            expected,
            [
                ("card-a", "sku-one", "42", "-"),
                ("card-a", "sku-one", "43", "-"),
            ],
        )

    def test_expected_results_keeps_rows_of_same_article_for_different_cards(self):
        payload = {
            "cards": [
                {"id": "card-a", "article": "SKU-ONE", "rows": [{"size": "42", "qty": "10"}]},
                {"id": "card-b", "article": "SKU-ONE", "rows": [{"size": "42", "qty": "10"}]},
            ]
        }
        expected = _expected_processing_results(payload)
        self.assertCountEqual(
            expected,
            [
                ("card-a", "sku-one", "42", "-"),
                ("card-b", "sku-one", "42", "-"),
            ],
        )

    def test_results_ready_requires_all_fields_from_requirements(self):
        payload = {
            "cards": [
                {"article": "SKU-ONE", "rows": [{"size": "42", "qty": "10"}]},
            ],
            "defect_percent": "10%",
            "marking_5840_qty": "1",
            "tag_owner": "Фуллбокс",
            "processing_results": [
                {
                    "article": "SKU-ONE",
                    "size": "42",
                    "destination": "-",
                    "processed": "10",
                    "defect": "0",
                    "shortage": "0",
                    "labels_printed": "10",
                    "labels_unboxed": "10",
                    "tags_replaced": "0",
                }
            ],
        }
        self.assertTrue(_processing_results_are_ready(payload, include_shipping=False))

        payload["processing_results"][0].pop("tags_replaced")
        self.assertTrue(_processing_results_are_ready(payload, include_shipping=False))

    def test_expected_results_aggregates_direction_plan_rows(self):
        payload = {
            "direction_addresses_json": ["Москва"],
            "direction_plan_json": {
                "directions": ["Москва"],
                "rows": [
                    {"article": "SKU-ONE", "size": "42", "quantities": [10]},
                ],
            },
        }
        self.assertEqual(_expected_processing_results(payload), [("", "sku-one", "42", "-")])

    def test_include_shipping_flag_does_not_require_shipping_on_processing_card(self):
        payload = {
            "direction_addresses_json": ["Москва"],
            "direction_plan_json": {
                "directions": ["Москва"],
                "rows": [
                    {"article": "SKU-ONE", "size": "42", "quantities": [10]},
                ],
            },
            "processing_results": [
                {
                    "article": "SKU-ONE",
                    "size": "42",
                    "destination": "-",
                    "processed": "10",
                }
            ],
        }
        self.assertTrue(_processing_results_are_ready(payload, include_shipping=False))
        self.assertTrue(_processing_results_are_ready(payload, include_shipping=True))

    def test_discrepancy_rows_compare_expected_payload_to_factual_placement_payload(self):
        expected_payload = {
            "cards": [
                {"article": "SKU-ONE", "rows": [{"size": "42", "qty": "10"}]},
            ],
        }
        factual_payload = {
            "act_boxes": [
                {
                    "code": "BOX-1",
                    "items": [
                        {"sku": "SKU-ONE", "sku_code": "SKU-ONE", "name": "Item", "size": "42", "qty": 10},
                    ],
                }
            ],
            "act_pallets": [],
        }

        self.assertEqual(_processing_discrepancy_rows(expected_payload, factual_payload), [])

    def test_quantity_summary_requires_declared_processed_and_boxed_quantities_to_match(self):
        payload = {
            "cards": [{"article": "SKU-ONE", "rows": [{"size": "42", "qty": "10"}]}],
            "processing_results": [{"article": "SKU-ONE", "size": "42", "processed": "9"}],
        }
        placement_payload = {
            "act_boxes": [{"code": "BOX-1", "items": [{"sku": "SKU-ONE", "size": "42", "qty": 9}]}],
        }

        summary = _processing_quantity_summary(payload, placement_payload)

        self.assertEqual(summary["declared_qty"], 10)
        self.assertEqual(summary["processed_qty"], 9)
        self.assertEqual(summary["boxed_qty"], 9)
        self.assertFalse(summary["matches"])

    def test_explicit_empty_placement_ignores_stale_inbound_act_boxes(self):
        payload = {
            "cards": [{"article": "SKU-ONE", "rows": [{"size": "42", "qty": "1"}]}],
            "processing_results": [{"article": "SKU-ONE", "size": "42", "processed": "1"}],
            "act_boxes": [
                {"code": f"BOX-INBOUND-{index}", "items": [{"sku": "SKU-ONE", "size": "42", "qty": 1}]}
                for index in range(7)
            ],
            "act_pallets": [{"code": "PAL-INBOUND-1"}],
        }

        legacy_summary = _processing_quantity_summary(payload)
        active_summary = _processing_quantity_summary(payload, {})
        discrepancy_rows = _processing_discrepancy_rows(payload, {})

        self.assertEqual(legacy_summary["boxed_qty"], 7)
        self.assertEqual(active_summary["declared_qty"], 1)
        self.assertEqual(active_summary["processed_qty"], 1)
        self.assertEqual(active_summary["boxed_qty"], 0)
        self.assertFalse(active_summary["matches"])
        self.assertEqual(len(discrepancy_rows), 1)
        self.assertEqual(discrepancy_rows[0]["expected_qty"], 1)
        self.assertEqual(discrepancy_rows[0]["factual_qty"], 0)

    def test_processing_work_payload_backfills_cards_and_results_from_history(self):
        agency = Agency(agn_name="Backfill Agency")
        entries = [
            OrderAuditEntry(
                order_id="1",
                order_type="processing",
                action="status",
                agency=agency,
                payload={
                    "status": "processing_in_work",
                    "status_label": "Взята в работу",
                    "cards": [{"article": "SKU-ONE", "rows": [{"size": "42", "qty": "10"}]}],
                    "marking_5860_qty": "2",
                    "marking_75120_qty": "1",
                    "processing_results": [
                        {"article": "SKU-ONE", "size": "42", "destination": "-", "processed": "10"},
                    ],
                },
            ),
            OrderAuditEntry(
                order_id="1",
                order_type="processing",
                action="status",
                agency=agency,
                payload={
                    "status": "processing_in_work",
                    "status_label": "Разногласия — на утверждении руководителя обработки",
                    "act": "placement",
                    "act_state": "closed",
                    "discrepancy_status": "reported",
                },
            ),
        ]

        payload = _processing_work_payload_from_entries(entries)

        self.assertEqual(payload.get("status"), "processing_in_work")
        self.assertEqual(len(payload.get("cards") or []), 1)
        self.assertEqual(len(payload.get("processing_results") or []), 1)
        self.assertEqual(payload.get("marking_5860_qty"), "2")
        self.assertEqual(payload.get("marking_75120_qty"), "1")
        self.assertEqual(payload.get("act_state"), "closed")
        self.assertEqual(payload.get("discrepancy_status"), "reported")

    def test_processing_work_payload_ignores_reachtruck_source_depletion_status(self):
        agency = Agency(agn_name="Reachtruck Stage Recovery Agency")
        entries = [
            OrderAuditEntry(
                order_id="23",
                order_type="processing",
                action="status",
                agency=agency,
                payload={
                    "status": "processing_head",
                    "status_label": "товар прибыл в OBR",
                    "processing_stage": PROCESSING_STAGE_OBR_ARRIVED,
                    "processing_stage_label": "товар прибыл в OBR",
                    "cards": [{"id": "card-a", "article": "SKU-A"}],
                },
            ),
            OrderAuditEntry(
                order_id="23",
                order_type="processing",
                action="update",
                agency=agency,
                payload={
                    "processed_cards": ["card-a"],
                    "processing_results": [
                        {"card_id": "card-a", "article": "SKU-A", "processed": "1"},
                    ],
                },
            ),
            OrderAuditEntry(
                order_id="23",
                order_type="processing",
                action="status",
                agency=agency,
                payload={
                    "act": "placement",
                    "act_state": "closed",
                    "act_items_removed": True,
                    "act_boxes": [],
                    "act_pallets": [],
                },
            ),
        ]

        payload = _processing_work_payload_from_entries(entries)

        self.assertEqual(payload.get("processing_stage"), PROCESSING_STAGE_OBR_ARRIVED)
        self.assertEqual(payload.get("status"), "processing_head")
        self.assertEqual(payload.get("processed_cards"), ["card-a"])
        self.assertEqual(len(payload.get("processing_results") or []), 1)
        self.assertNotIn("act_items_removed", payload)

    def test_processing_placement_entry_prefers_closed_flow_snapshot_over_partial_updates(self):
        agency = Agency(agn_name="Placement Agency")
        entries = [
            OrderAuditEntry(
                order_id="1",
                order_type="processing",
                action="update",
                agency=agency,
                payload={
                    "act": "placement",
                    "flow_closed": True,
                    "act_pallets": [{"code": "PAL-1"}, {"code": "PAL-2"}],
                },
            ),
            OrderAuditEntry(
                order_id="1",
                order_type="processing",
                action="status",
                agency=agency,
                payload={
                    "act": "placement",
                    "act_pallets": [{"code": "PAL-1"}],
                },
            ),
        ]

        placement_entry = _processing_placement_entry(entries)

        self.assertIsNotNone(placement_entry)
        self.assertEqual(
            [item.get("code") for item in ((placement_entry.payload or {}).get("act_pallets") or [])],
            ["PAL-1", "PAL-2"],
        )

    def test_processing_placement_entry_ignores_reachtruck_source_depletion_snapshot(self):
        agency = Agency(agn_name="Reachtruck Placement Agency")
        entries = [
            OrderAuditEntry(
                order_id="23",
                order_type="processing",
                action="status",
                agency=agency,
                payload={
                    "act": "placement",
                    "act_state": "closed",
                    "act_items_removed": True,
                    "act_boxes": [{"code": "BOX-INBOUND-1"}],
                    "act_pallets": [{"code": "PAL-INBOUND-1"}],
                },
            ),
        ]

        self.assertIsNone(_processing_placement_entry(entries))


    def test_card_sets_ignore_completion_flag_when_processed_quantity_is_blank(self):
        payload = {
            "cards": [
                {
                    "id": "card-a",
                    "article": "SKU-A",
                    "processed_done": True,
                    "processed_at": "2026-07-27T17:24:06+03:00",
                }
            ],
            "processed_cards": ["card-a"],
            "processing_results": [
                {
                    "card_id": "card-a",
                    "article": "SKU-A",
                    "size": "42",
                    "destination": "-",
                    "processed": "",
                }
            ],
        }

        processed_cards, placed_cards = _processing_card_sets(payload)

        self.assertEqual(processed_cards, set())
        self.assertEqual(placed_cards, set())



class ProcessingWorkTemplateRegressionTests(SimpleTestCase):
    def test_processing_card_exposes_sized_packaging_parameter_fields(self):
        template_path = (
            Path(__file__).resolve().parent
            / "templates"
            / "processing"
            / "processing_card.html"
        )
        template_source = template_path.read_text(encoding="utf-8")

        self.assertIn('name="manual_param_size"', template_source)
        self.assertIn('name="manual_param_qty"', template_source)
        self.assertIn("Упаковка в курьерский пакет", template_source)
        self.assertIn("Упаковка в пакет зип лок", template_source)
        self.assertIn("const sizedPackageEnabled", template_source)

    def test_processing_technical_card_uses_clear_checkmarks(self):
        template_path = (
            Path(__file__).resolve().parent
            / "templates"
            / "processing"
            / "processing_technical_card.html"
        )
        template_source = template_path.read_text(encoding="utf-8")

        self.assertIn('.cb.checked::after{', template_source)
        self.assertIn('content:"✓";', template_source)
        self.assertNotIn('.cb.checked{ background:#777; }', template_source)

    def test_both_work_views_expose_confirmed_processing_head_cancellation(self):
        templates_dir = Path(__file__).resolve().parent / "templates" / "processing"
        for template_name in ("processing_work_table.html", "processing_work.html"):
            with self.subTest(template_name=template_name):
                template_source = (templates_dir / template_name).read_text(encoding="utf-8")
                self.assertIn("{% if can_processing_head_cancel %}", template_source)
                self.assertIn("data-open-processing-cancel", template_source)
                self.assertIn('value="cancel_processing_by_head"', template_source)
                self.assertIn("Подтвердить отмену", template_source)

    def test_processing_act_is_read_only_for_client_and_manager_finishes_processing(self):
        template_path = (
            Path(__file__).resolve().parent.parent
            / "orders"
            / "templates"
            / "orders"
            / "detail.html"
        )
        template_source = template_path.read_text(encoding="utf-8")

        self.assertIn('value="confirm_processing_act_manager"', template_source)
        self.assertIn("Подтвердить акт и завершить обработку", template_source)
        self.assertNotIn('value="confirm_processing_act_client"', template_source)
        self.assertNotIn('value="dispute_processing_act_client"', template_source)

    def test_client_processing_files_upload_separately_from_autosave(self):
        template_path = (
            Path(__file__).resolve().parent
            / "templates"
            / "processing"
            / "form_client_lk.html"
        )
        template_source = template_path.read_text(encoding="utf-8")
        self.assertIn('id="processing-documents-upload"', template_source)
        self.assertIn('id="processing-documents-selection"', template_source)
        self.assertIn("const uploadDocuments = async () => {", template_source)
        self.assertIn("data.delete('documents');", template_source)
        self.assertIn("event.target !== documentsInput", template_source)
        self.assertIn("if (documentsInput) documentsInput.value = '';", template_source)

    def test_client_processing_autosave_remembers_json_draft_order(self):
        template_path = (
            Path(__file__).resolve().parent
            / "templates"
            / "processing"
            / "form_client_lk.html"
        )
        template_source = template_path.read_text(encoding="utf-8")

        self.assertGreaterEqual(
            template_source.count("const orderId = rememberDraftOrder(result);"),
            2,
        )
        self.assertIn("const result = await response.json().catch(() => ({}));", template_source)
        self.assertIn("setStatus(clean(result.error) || 'Не удалось автосохранить');", template_source)

    def test_client_processing_form_blocks_same_article_with_visible_guidance(self):
        template_path = (
            Path(__file__).resolve().parent
            / "templates"
            / "processing"
            / "form_client_lk.html"
        )
        template_source = template_path.read_text(encoding="utf-8")
        self.assertIn('id="article-change-guidance"', template_source)
        self.assertIn('id="article-change-error"', template_source)
        self.assertIn("function articleChangeValidationState()", template_source)
        self.assertIn("Замена А → А невозможна.", template_source)
        self.assertIn("articleChangeState.valid", template_source)

    def test_work_card_treats_pallet_full_move_as_delivery_to_obr(self):
        template_path = (
            Path(__file__).resolve().parent
            / "templates"
            / "processing"
            / "processing_work.html"
        )
        template_source = template_path.read_text(encoding="utf-8")
        self.assertIn("let hasPalletFullDone = false;", template_source)
        self.assertIn("if (mode === MOVE_MODE_PALLET_FULL)", template_source)
        self.assertIn(
            "const completedByFullTransfer = requestedQty <= 0 && !hasActive && (hasBoxFullDone || hasPalletFullDone);",
            template_source,
        )

    def test_work_template_uses_compact_move_summary_without_reachtruck_planning_details(self):
        template_path = (
            Path(__file__).resolve().parent
            / "templates"
            / "processing"
            / "processing_work.html"
        )
        template_source = template_path.read_text(encoding="utf-8")
        self.assertIn('id="reachtruck-summary"', template_source)
        self.assertIn("Сводка доставки", template_source)
        self.assertIn("Паллеты, короба и маршрут сервер спланирует сам после отправки", template_source)
        self.assertIn("reachtruck-summary-line-qty", template_source)
        self.assertNotIn('id="reachtruck-details"', template_source)
        self.assertNotIn("Как это работает", template_source)
        self.assertNotIn('id="reachtruck-card-label"', template_source)
        self.assertNotIn("requested_rows_json", template_source)

    def test_work_table_template_uses_processing_head_work_layout(self):
        template_path = (
            Path(__file__).resolve().parent
            / "templates"
            / "processing"
            / "processing_work_table.html"
        )
        template_source = template_path.read_text(encoding="utf-8")
        self.assertIn("work-topbar", template_source)
        self.assertIn("Детали заявки", template_source)
        self.assertIn("Контроль выполнения", template_source)
        self.assertIn("Состав товара", template_source)
        self.assertIn("work-services-details", template_source)
        self.assertLess(template_source.index("Состав товара"), template_source.index("Фактически оказанные услуги"))
        self.assertIn("and placement_completed", template_source)
        self.assertIn("work-params-details", template_source)
        self.assertIn(
            ".processing-work .work-params-details[open] .block-body {\n"
            "      max-height: none;\n"
            "      overflow: visible;",
            template_source,
        )
        self.assertIn(".processing-work .processing-content", template_source)
        self.assertIn("html {\n      height: 100%;\n      overflow: hidden;", template_source)
        self.assertIn(".processing-work .wrap {\n      height: 100vh;", template_source)
        self.assertIn("height: calc(100vh - 16px);", template_source)
        self.assertIn("overflow-y: auto", template_source)
        self.assertIn("overflow-x: hidden", template_source)
        self.assertIn('class="panel processing-block work-history-details"', template_source)
        self.assertNotIn('class="panel processing-block work-history-details" open', template_source)
        self.assertIn("max-height: min(42vh, 420px)", template_source)
        self.assertIn('id="take-work-reachtruck-btn"', template_source)
        self.assertIn("createReachtruckObrRequest", template_source)
        self.assertIn("fetch('/reachtruck/requests/create/'", template_source)
        self.assertIn("formData.append('to_zone', 'OBR')", template_source)
        self.assertIn('id="packaging-modal"', template_source)
        self.assertIn('data-open-packaging-modal', template_source)
        self.assertIn("Поручить формирование коробов", template_source)
        self.assertIn("Назначить упаковщиц на товар", template_source)
        self.assertIn('name="assignee_ids"', template_source)
        self.assertIn("Формирование коробов", template_source)
        self.assertIn("Формирование коробов назначается отдельно", template_source)
        self.assertNotIn("Что осталось выполнить:", template_source)
        self.assertIn('id="processing-services-modal"', template_source)
        self.assertIn("Сохранить услуги и закрыть заявку", template_source)
        self.assertIn("processing-quantity-summary", template_source)
        self.assertIn("Операционные подзоны OBR", template_source)
        self.assertIn("processing_subzone_steps", template_source)
        self.assertIn("{{ zone.code }}", template_source)
        self.assertIn("{{ zone.label }}", template_source)
        self.assertIn("operationalSubzoneCode", template_source)
        self.assertIn("Контроль качества обработки", template_source)
        self.assertIn('name="action" value="approve_quality"', template_source)
        self.assertIn('name="action" value="return_to_rework"', template_source)
        self.assertIn("data-open-quality-approve", template_source)
        self.assertIn("data-open-quality-rework", template_source)

    def test_processing_history_repairs_legacy_mojibake(self):
        self.assertEqual(
            _repair_mojibake_text("Р—Р°СЏРІРєР° СѓС‚РІРµСЂР¶РґРµРЅР° РјРµРЅРµРґР¶РµСЂРѕРј"),
            "Заявка утверждена менеджером",
        )


    def test_processing_card_separates_save_navigation_from_explicit_completion(self):
        template_path = (
            Path(__file__).resolve().parent
            / "templates"
            / "processing"
            / "processing_card.html"
        )
        template_source = template_path.read_text(encoding="utf-8")
        return_handler_start = template_source.index(
            "returnLink.addEventListener('click'"
        )
        return_handler_end = template_source.index(
            "form.submit();",
            return_handler_start,
        )
        return_handler = template_source[
            return_handler_start:return_handler_end
        ]
        self.assertIn("actionInput.value = 'save_results';", return_handler)

        completion_handler_start = template_source.index("if (saveButton) {")
        completion_handler_end = template_source.index(
            "const printLinks",
            completion_handler_start,
        )
        completion_handler = template_source[
            completion_handler_start:completion_handler_end
        ]
        self.assertIn("actionInput.value = 'finish_card';", completion_handler)

    def test_processing_card_prints_large_cz_request_in_sequential_chunks(self):
        template_path = (
            Path(__file__).resolve().parent
            / "templates"
            / "processing"
            / "processing_card.html"
        )
        template_source = template_path.read_text(encoding="utf-8")

        self.assertIn("const processingCzReservationChunkSize = 10;", template_source)
        self.assertIn("while (jobIds.length < copies && !queueError)", template_source)
        self.assertIn(
            "const chunkQty = Math.min(processingCzReservationChunkSize, copies - chunkStart);",
            template_source,
        )
        self.assertIn(
            "const reserved = await postProcessingCz('', { qty: chunkQty, barcode, size });",
            template_source,
        )
        self.assertNotIn(
            "const reserved = await postProcessingCz('', { qty: copies, barcode, size });",
            template_source,
        )
        self.assertIn("Отправка партии ЧЗ: ${jobIds.length + batch.length} / ${copies}", template_source)

    def test_work_table_does_not_infer_processed_quantity_from_declared_quantity(self):
        template_path = (
            Path(__file__).resolve().parent
            / "templates"
            / "processing"
            / "processing_work_table.html"
        )
        template_source = template_path.read_text(encoding="utf-8")
        self.assertIn("const processedDone = resultsReady && completionFlag;", template_source)
        self.assertIn("const processedQty = rowProcessedTotal || draftProcessedTotal;", template_source)
        self.assertNotIn("(processedDone ? totalQty : 0)", template_source)

    def test_work_table_allows_services_modal_when_services_are_only_finish_blocker(self):
        template_path = (
            Path(__file__).resolve().parent
            / "templates"
            / "processing"
            / "processing_work_table.html"
        )
        template_source = template_path.read_text(encoding="utf-8")
        self.assertIn("{% if finish_action_ready %}", template_source)
        self.assertIn("Сохранить услуги и закрыть заявку", template_source)
        self.assertIn('<div class="modal-title">Закрытие заявки</div>', template_source)
        self.assertIn('<button class="btn finish-ready" type="submit">Закрыть заявку</button>', template_source)
        self.assertNotIn("{% if finish_services_confirmed %}", template_source)
        self.assertNotIn("hasSavedServices", template_source)
        self.assertIn("and quality_is_approved", template_source)
        self.assertIn(
            "Окно услуг недоступно. Обновите страницу и повторите завершение.",
            template_source,
        )
        self.assertIn(
            "Услуги ещё не загрузились. Обновите страницу и повторите завершение.",
            template_source,
        )

        panel_path = (
            Path(__file__).resolve().parent
            / "templates"
            / "processing"
            / "_processing_service_facts_panel.html"
        )
        panel_source = panel_path.read_text(encoding="utf-8")
        self.assertIn("const differsFromPlan", panel_source)
        self.assertIn("план, расчёт и факт отличаются", panel_source)



class ProcessingCardSaveMergeTests(TestCase):
    def setUp(self):
        self.order_id = "500"
        self.user = get_user_model().objects.create_user(username="ph_user", password="x")
        Employee.objects.create(
            full_name="Processing Head",
            user=self.user,
            role="processing_head",
            is_active=True,
        )
        self.client.force_login(self.user)
        self.agency = Agency.objects.create(agn_name="Test Agency")
        self.base_payload = {
            "status": "processing_in_work",
            "status_label": "Взята в работу",
            "cards": [
                {
                    "id": "card-a",
                    "article": "IP/AA5532черный",
                    "rows": [{"size": "27", "barcode": "2000508267118", "qty": "100"}],
                },
                {
                    "id": "card-b",
                    "article": "IP/AA5",
                    "rows": [{"size": "27", "barcode": "2000508269068", "qty": "100"}],
                },
            ],
        }
        OrderAuditEntry.objects.create(
            order_id=self.order_id,
            order_type="processing",
            action="status",
            agency=self.agency,
            payload=self.base_payload,
        )

    def _post_card_result(self, card_id: str, article: str, size: str, barcode: str, processed: str):
        url = f"/orders/processing/{self.order_id}/card/{card_id}/?article={quote(article)}"
        data = {
            "action": "save_results",
            "return": f"/orders/processing/{self.order_id}/work/",
            "return_to": f"/orders/processing/{self.order_id}/work/",
            "card_id": card_id,
            "result_article[]": [article],
            "result_size[]": [size],
            "result_destination[]": ["-"],
            "result_received[]": ["100"],
            "result_processed[]": [processed],
            "result_defect[]": ["0"],
            "result_shortage[]": ["0"],
            "result_labels_printed[]": ["100"],
            "result_labels_unboxed[]": ["100"],
            "result_shipped_qty[]": ["0"],
            "result_tags_replaced[]": ["0"],
        }
        return self.client.post(url, data)

    @staticmethod
    def _results_map(payload: dict) -> dict[tuple[str, str, str, str], dict]:
        result = {}
        for item in payload.get("processing_results") or []:
            if not isinstance(item, dict):
                continue
            key = (
                str(item.get("card_id") or "").strip().lower(),
                str(item.get("article") or "").strip().lower(),
                str(item.get("size") or "").strip().lower(),
                str(item.get("destination") or "").strip().lower() or "-",
            )
            result[key] = item
        return result

    def test_save_results_merges_rows_between_cards(self):
        first = self._post_card_result(
            card_id="card-a",
            article="IP/AA5532черный",
            size="27",
            barcode="2000508267118",
            processed="100",
        )
        self.assertEqual(first.status_code, 302)

        second = self._post_card_result(
            card_id="card-b",
            article="IP/AA5",
            size="27",
            barcode="2000508269068",
            processed="90",
        )
        self.assertEqual(second.status_code, 302)

        latest = (
            OrderAuditEntry.objects.filter(order_id=self.order_id, order_type="processing")
            .order_by("-id")
            .first()
        )
        self.assertIsNotNone(latest)
        results_map = self._results_map(latest.payload or {})
        self.assertEqual(len(results_map), 2)
        self.assertEqual(
            results_map[("card-a", "ip/aa5532черный", "27", "-")].get("processed"),
            "100",
        )
        self.assertEqual(
            results_map[("card-b", "ip/aa5", "27", "-")].get("processed"),
            "90",
        )

    def test_save_results_updates_same_row_without_duplication(self):
        self._post_card_result(
            card_id="card-a",
            article="IP/AA5532черный",
            size="27",
            barcode="2000508267118",
            processed="100",
        )
        self._post_card_result(
            card_id="card-b",
            article="IP/AA5",
            size="27",
            barcode="2000508269068",
            processed="90",
        )
        self._post_card_result(
            card_id="card-a",
            article="IP/AA5532черный",
            size="27",
            barcode="2000508267118",
            processed="95",
        )

        latest = (
            OrderAuditEntry.objects.filter(order_id=self.order_id, order_type="processing")
            .order_by("-id")
            .first()
        )
        self.assertIsNotNone(latest)
        results_map = self._results_map(latest.payload or {})
        self.assertEqual(len(results_map), 2)
        self.assertEqual(
            results_map[("card-a", "ip/aa5532черный", "27", "-")].get("processed"),
            "95",
        )

    def test_save_results_replaces_legacy_direction_rows_for_same_card(self):
        seeded_payload = dict(self.base_payload)
        seeded_payload["processing_results"] = [
            {
                "card_id": "card-a",
                "article": "IP/AA5532черный",
                "size": "27",
                "destination": "Москва",
                "processed": "40",
            },
            {
                "card_id": "card-a",
                "article": "IP/AA5532черный",
                "size": "27",
                "destination": "Казань",
                "processed": "60",
            },
        ]
        OrderAuditEntry.objects.create(
            order_id=self.order_id,
            order_type="processing",
            action="result",
            agency=self.agency,
            payload=seeded_payload,
        )

        response = self._post_card_result(
            card_id="card-a",
            article="IP/AA5532черный",
            size="27",
            barcode="2000508267118",
            processed="100",
        )
        self.assertEqual(response.status_code, 302)

        latest = (
            OrderAuditEntry.objects.filter(order_id=self.order_id, order_type="processing")
            .order_by("-id")
            .first()
        )
        self.assertIsNotNone(latest)
        results_map = self._results_map(latest.payload or {})
        self.assertIn(("card-a", "ip/aa5532черный", "27", "-"), results_map)
        self.assertNotIn(("card-a", "ip/aa5532черный", "27", "москва"), results_map)
        self.assertNotIn(("card-a", "ip/aa5532черный", "27", "казань"), results_map)

    def test_return_to_processing_saves_results_and_marks_card(self):
        url = f"/orders/processing/{self.order_id}/card/card-a/?article={quote('IP/AA5532черный')}"
        response = self.client.post(
            url,
            {
                "action": "return_to_processing",
                "return": f"/orders/processing/{self.order_id}/work/",
                "return_to": f"/orders/processing/{self.order_id}/work/",
                "card_id": "card-a",
                "result_article[]": ["IP/AA5532черный"],
                "result_size[]": ["27"],
                "result_destination[]": ["-"],
                "result_received[]": ["100"],
                "result_processed[]": ["88"],
                "result_defect[]": ["0"],
                "result_shortage[]": ["0"],
                "result_labels_printed[]": ["100"],
                "result_labels_unboxed[]": ["100"],
                "result_shipped_qty[]": ["0"],
                "result_tags_replaced[]": ["0"],
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, f"/orders/processing/{self.order_id}/work/")

        latest = (
            OrderAuditEntry.objects.filter(order_id=self.order_id, order_type="processing")
            .order_by("-id")
            .first()
        )
        self.assertIsNotNone(latest)
        payload = latest.payload or {}
        results_map = self._results_map(payload)
        self.assertEqual(
            results_map[("card-a", "ip/aa5532черный", "27", "-")].get("processed"),
            "88",
        )
        self.assertIn("card-a", payload.get("processed_cards") or [])
        cards = payload.get("cards") or []
        card_a = next((card for card in cards if isinstance(card, dict) and card.get("id") == "card-a"), None)
        self.assertIsNotNone(card_a)
        self.assertTrue(bool(card_a.get("processed_done")))

    def test_save_results_keeps_card_active_when_results_are_ready(self):
        url = f"/orders/processing/{self.order_id}/card/card-a/?article={quote('IP/AA5532черный')}"
        response = self.client.post(
            url,
            {
                "action": "save_results",
                "return": f"/orders/processing/{self.order_id}/work/",
                "return_to": f"/orders/processing/{self.order_id}/work/",
                "card_id": "card-a",
                "result_article[]": ["IP/AA5532черный"],
                "result_size[]": ["27"],
                "result_destination[]": ["-"],
                "result_received[]": ["100"],
                "result_processed[]": ["88"],
                "result_defect[]": ["0"],
                "result_shortage[]": ["0"],
                "result_labels_printed[]": ["100"],
                "result_labels_unboxed[]": ["100"],
                "result_shipped_qty[]": ["0"],
                "result_tags_replaced[]": ["0"],
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, f"/orders/processing/{self.order_id}/work/")

        latest = (
            OrderAuditEntry.objects.filter(order_id=self.order_id, order_type="processing")
            .order_by("-id")
            .first()
        )
        self.assertIsNotNone(latest)
        payload = latest.payload or {}
        self.assertNotIn("card-a", payload.get("processed_cards") or [])
        cards = payload.get("cards") or []
        card_a = next((card for card in cards if isinstance(card, dict) and card.get("id") == "card-a"), None)
        self.assertIsNotNone(card_a)
        self.assertFalse(bool(card_a.get("processed_done")))

    def test_save_results_restores_additional_barcode_received_and_defect(self):
        base_line_key = "card-a|0|ip/aa5532черный|27|2000508267118|-|100"
        additional_line_key = f"{base_line_key}|barcode|2000508267999|test"
        url = f"/orders/processing/{self.order_id}/card/card-a/?article={quote('IP/AA5532черный')}"
        response = self.client.post(
            url,
            {
                "action": "save_results",
                "return": f"/orders/processing/{self.order_id}/work/",
                "return_to": f"/orders/processing/{self.order_id}/work/",
                "card_id": "card-a",
                "result_article[]": ["IP/AA5532черный", "IP/AA5532черный"],
                "result_source_article[]": ["IP/AA5532черный", "IP/AA5532черный"],
                "result_product_name[]": ["Товар", "Товар"],
                "result_barcode[]": ["2000508267118", "2000508267999"],
                "result_size[]": ["27", "28"],
                "result_destination[]": ["-", "-"],
                "result_line_key[]": [base_line_key, additional_line_key],
                "result_received[]": ["96", "4"],
                "result_is_additional[]": ["0", "1"],
                "result_parent_line_key[]": [base_line_key, base_line_key],
                "result_processed[]": ["94", "3"],
                "result_defect[]": ["2", "1"],
                "result_shortage[]": ["0", "0"],
                "result_labels_printed[]": ["0", "0"],
                "result_labels_unboxed[]": ["0", "0"],
                "result_shipped_qty[]": ["0", "0"],
                "result_tags_replaced[]": ["0", "0"],
            },
        )
        self.assertEqual(response.status_code, 302)

        latest = (
            OrderAuditEntry.objects.filter(order_id=self.order_id, order_type="processing")
            .order_by("-id")
            .first()
        )
        self.assertIsNotNone(latest)
        saved_rows = latest.payload.get("processing_results") or []
        additional = next(
            row for row in saved_rows if row.get("line_key") == additional_line_key
        )
        self.assertTrue(additional.get("is_additional"))
        self.assertEqual(additional.get("parent_line_key"), base_line_key)
        self.assertEqual(additional.get("received"), "4")
        self.assertEqual(additional.get("defect"), "1")

        page = self.client.get(url)
        self.assertEqual(page.status_code, 200)
        rows = page.context["results_rows"]
        base = next(row for row in rows if row.get("line_key") == base_line_key)
        restored = next(row for row in rows if row.get("line_key") == additional_line_key)
        self.assertEqual(base.get("received"), 96)
        self.assertEqual(restored.get("barcode"), "2000508267999")
        self.assertEqual(restored.get("size"), "28")
        self.assertEqual(restored.get("defect"), "1")
        self.assertTrue(restored.get("is_additional"))
        self.assertTrue(page.context["results_quality_visible"])
        self.assertContains(page, 'name="result_received[]"')
        self.assertContains(page, 'name="result_defect[]"')


class ProcessingCardDataConfirmationTests(TestCase):
    def setUp(self):
        self.order_id = "502-confirmation"
        self.worker_user = get_user_model().objects.create_user(
            username="processing_confirmation_worker",
            password="x",
        )
        self.head_user = get_user_model().objects.create_user(
            username="processing_confirmation_head",
            password="x",
        )
        self.worker = Employee.objects.create(
            full_name="Confirmation Worker",
            user=self.worker_user,
            role="processing_worker",
            is_active=True,
        )
        self.head = Employee.objects.create(
            full_name="Confirmation Head",
            user=self.head_user,
            role="processing_head",
            is_active=True,
        )
        self.agency = Agency.objects.create(agn_name="Confirmation Agency")
        self.card_route = f"/orders/processing/{self.order_id}/card/card-a/"
        self.base_payload = {
            "status": "processing_in_work",
            "status_label": "Взята в работу",
            "cards": [
                {
                    "id": "card-a",
                    "article": "SKU-A",
                    "product_name": "Товар А",
                    "rows": [
                        {
                            "article": "SKU-A",
                            "barcode": "BAR-A",
                            "size": "42",
                            "qty": "10",
                        }
                    ],
                }
            ],
        }
        OrderAuditEntry.objects.create(
            order_id=self.order_id,
            order_type="processing",
            action="status",
            agency=self.agency,
            payload=self.base_payload,
        )
        Task.objects.create(
            title=f"Обработать SKU-A по заявке №{self.order_id}",
            route=self.card_route,
            assigned_to=self.worker,
            observer=self.head,
            created_by=self.head_user,
            status="backlog",
        )
        self.client.force_login(self.worker_user)

    def _finish_data(self, **extra):
        data = {
            "action": "finish_card",
            "card_id": "card-a",
            "result_article[]": ["SKU-A"],
            "result_source_article[]": ["SKU-A"],
            "result_product_name[]": ["Товар А"],
            "result_barcode[]": ["BAR-A"],
            "result_size[]": ["42"],
            "result_destination[]": ["-"],
            "result_line_key[]": ["card-a|0|sku-a|42|bar-a|-|10"],
            "result_received[]": ["10"],
            "result_processed[]": ["10"],
            "result_defect[]": ["0"],
            "result_shortage[]": ["0"],
            "result_labels_printed[]": ["0"],
            "result_labels_unboxed[]": ["0"],
            "result_shipped_qty[]": ["0"],
            "result_tags_replaced[]": ["0"],
        }
        data.update(extra)
        return data

    def _latest_payload(self):
        return (
            OrderAuditEntry.objects.filter(
                order_id=self.order_id,
                order_type="processing",
            )
            .order_by("-id")
            .values_list("payload", flat=True)
            .first()
            or {}
        )

    def test_worker_card_requires_explicit_data_confirmation(self):
        page = self.client.get(self.card_route)
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "Проверка данных товара")
        self.assertContains(page, 'name="data_match_status"')
        self.assertContains(page, 'name="worker_data_confirmed"')

        response = self.client.post(self.card_route, self._finish_data())

        self.assertEqual(response.status_code, 302)
        self.assertIn("data_confirmation=worker_required", response.url)
        self.assertEqual(
            OrderAuditEntry.objects.filter(
                order_id=self.order_id,
                order_type="processing",
            ).count(),
            1,
        )
        self.assertNotIn("card-a", self._latest_payload().get("processed_cards") or [])

    def test_worker_can_confirm_matching_data(self):
        response = self.client.post(
            self.card_route,
            self._finish_data(
                data_match_status="match",
                worker_data_confirmed="1",
            ),
        )

        self.assertEqual(response.status_code, 302)
        self.assertIn("data_confirmation=confirmed", response.url)
        payload = self._latest_payload()
        confirmation = payload["processing_card_data_confirmations"]["card-a"]
        self.assertEqual(confirmation["status"], "confirmed")
        self.assertEqual(confirmation["worker_name"], "Confirmation Worker")
        self.assertIn("card-a", payload.get("processed_cards") or [])
        self.assertFalse(
            Task.objects.filter(route=self.card_route).exclude(status="done").exists()
        )

    def test_mismatch_waits_for_processing_head_confirmation(self):
        stock_count_before = WarehouseStockSnapshot.objects.count()
        reported = self.client.post(
            self.card_route,
            self._finish_data(
                data_match_status="mismatch",
                worker_data_confirmed="1",
                data_mismatch_comment="ШК на товаре отличается от карточки",
            ),
        )

        self.assertEqual(reported.status_code, 302)
        self.assertIn("data_confirmation=mismatch_reported", reported.url)
        payload = self._latest_payload()
        confirmation = payload["processing_card_data_confirmations"]["card-a"]
        self.assertEqual(confirmation["status"], "pending_head")
        self.assertNotIn("card-a", payload.get("processed_cards") or [])
        review_task = Task.objects.get(
            route=self.card_route,
            title=ProcessingWorkflowService._processing_data_review_task_title(
                self.order_id,
                "card-a",
            ),
        )
        self.assertEqual(review_task.assigned_to, self.head)
        self.assertEqual(review_task.status, "backlog")
        self.assertEqual(WarehouseStockSnapshot.objects.count(), stock_count_before)

        self.client.force_login(self.head_user)
        review_page = self.client.get(self.card_route)
        self.assertContains(review_page, "Ожидает подтверждения руководителя")
        self.assertContains(review_page, 'name="head_data_confirmed"')
        approved = self.client.post(
            self.card_route,
            {
                "action": "approve_processing_data_mismatch",
                "card_id": "card-a",
                "head_data_confirmed": "1",
            },
        )

        self.assertEqual(approved.status_code, 302)
        self.assertIn("data_confirmation=approved", approved.url)
        payload = self._latest_payload()
        confirmation = payload["processing_card_data_confirmations"]["card-a"]
        self.assertEqual(confirmation["status"], "approved")
        self.assertEqual(confirmation["approved_by"], "Confirmation Head")
        self.assertIn("card-a", payload.get("processed_cards") or [])
        review_task.refresh_from_db()
        self.assertEqual(review_task.status, "done")
        self.assertEqual(WarehouseStockSnapshot.objects.count(), stock_count_before)

    def test_pending_mismatch_survives_later_stale_result_entry(self):
        pending_payload = json.loads(json.dumps(self.base_payload))
        pending_payload["processing_results"] = [
            {
                "card_id": "card-a",
                "article": "SKU-A",
                "source_article": "SKU-A",
                "product_name": "Товар А",
                "barcode": "BAR-A",
                "size": "42",
                "destination": "-",
                "line_key": "card-a|0|sku-a|42|bar-a|-|10",
                "received": "10",
                "processed": "8",
                "defect": "0",
                "shortage": "2",
            }
        ]
        pending_payload["processed_cards"] = []
        pending_payload["processing_card_data_confirmations"] = {
            "card-a": {
                "status": "pending_head",
                "worker_id": self.worker.id,
                "worker_name": self.worker.full_name,
                "reported_at": timezone.now().isoformat(),
                "comment": "В коробе меньше товара, чем в заявке",
            }
        }
        OrderAuditEntry.objects.create(
            order_id=self.order_id,
            order_type="processing",
            action="processing_data_mismatch",
            agency=self.agency,
            payload=pending_payload,
        )
        Task.objects.create(
            title=ProcessingWorkflowService._processing_data_review_task_title(
                self.order_id,
                "card-a",
            ),
            route=self.card_route,
            assigned_to=self.head,
            created_by=self.worker_user,
            status="backlog",
        )

        stale_payload = json.loads(json.dumps(self.base_payload))
        stale_payload["processing_results"] = [
            {
                "card_id": "card-a",
                "article": "SKU-A",
                "source_article": "SKU-A",
                "product_name": "Товар А",
                "barcode": "BAR-A",
                "size": "42",
                "destination": "-",
                "line_key": "card-a|0|sku-a|42|bar-a|-|10",
                "received": "10",
                "processed": "10",
                "defect": "0",
                "shortage": "0",
            }
        ]
        stale_payload["processed_cards"] = []
        OrderAuditEntry.objects.create(
            order_id=self.order_id,
            order_type="processing",
            action="result",
            agency=self.agency,
            payload=stale_payload,
        )

        self.client.force_login(self.head_user)
        review_page = self.client.get(self.card_route)
        self.assertEqual(review_page.status_code, 200)
        self.assertContains(review_page, "Ожидает подтверждения руководителя")
        self.assertContains(review_page, 'name="head_data_confirmed"')

        entry_count_before_save = OrderAuditEntry.objects.filter(
            order_id=self.order_id,
            order_type="processing",
        ).count()
        saved = self.client.post(
            self.card_route,
            self._finish_data(action="save_results"),
        )
        self.assertEqual(saved.status_code, 302)
        self.assertEqual(
            OrderAuditEntry.objects.filter(
                order_id=self.order_id,
                order_type="processing",
            ).count(),
            entry_count_before_save,
        )

        approved = self.client.post(
            self.card_route,
            {
                "action": "approve_processing_data_mismatch",
                "card_id": "card-a",
                "head_data_confirmed": "1",
            },
        )
        self.assertEqual(approved.status_code, 302)
        self.assertIn("data_confirmation=approved", approved.url)
        self.assertEqual(
            self._latest_payload()["processing_card_data_confirmations"]["card-a"]["status"],
            "approved",
        )


class ProcessingCardCorrectionTests(TestCase):
    def setUp(self):
        self.order_id = "503-correction"
        self.head_user = get_user_model().objects.create_user(
            username="processing_correction_head",
            password="x",
        )
        Employee.objects.create(
            full_name="Correction Head",
            user=self.head_user,
            role="processing_head",
            is_active=True,
        )
        self.agency = Agency.objects.create(agn_name="Correction Agency")
        self.card_route = f"/orders/processing/{self.order_id}/card/card-a/"
        self.payload = {
            "status": "processing_in_work",
            "status_label": "Взята в работу",
            "cards": [
                {
                    "id": "card-a",
                    "article": "SKU-A",
                    "product_name": "Товар А",
                    "processed_done": True,
                    "processed_at": "2026-08-18T10:00:00+03:00",
                    "processed_by": "Упаковщица",
                    "rows": [
                        {
                            "article": "SKU-A",
                            "barcode": "BAR-A",
                            "size": "42",
                            "qty": "10",
                        }
                    ],
                }
            ],
            "processed_cards": ["card-a"],
            "processing_card_data_confirmations": {
                "card-a": {
                    "status": "confirmed",
                    "worker_id": 100,
                    "worker_name": "Упаковщица",
                    "confirmed_at": "2026-08-18T10:00:00+03:00",
                }
            },
            "processing_results": [
                {
                    "card_id": "card-a",
                    "article": "SKU-A",
                    "source_article": "SKU-A",
                    "product_name": "Товар А",
                    "barcode": "BAR-A",
                    "size": "42",
                    "destination": "-",
                    "line_key": "card-a|sku-a|42|bar-a|-|0",
                    "received": "10",
                    "processed": "10",
                    "defect": "0",
                    "shortage": "0",
                }
            ],
        }
        OrderAuditEntry.objects.create(
            order_id=self.order_id,
            order_type="processing",
            action="result",
            agency=self.agency,
            payload=self.payload,
        )
        self.client.force_login(self.head_user)

    def _latest_payload(self):
        return (
            OrderAuditEntry.objects.filter(
                order_id=self.order_id,
                order_type="processing",
            )
            .order_by("-id")
            .values_list("payload", flat=True)
            .first()
            or {}
        )

    def test_head_reopens_confirmed_card_and_adds_size_without_stock_changes(self):
        stock_count_before = WarehouseStockSnapshot.objects.count()
        page = self.client.get(self.card_route)
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "Внести изменения")
        self.assertContains(page, 'value="reopen_processing_data"')

        reopened = self.client.post(
            self.card_route,
            {
                "action": "reopen_processing_data",
                "card_id": "card-a",
                "reopen_reason": "Обнаружен дополнительный размер",
            },
        )

        self.assertEqual(reopened.status_code, 302)
        self.assertIn("data_confirmation=reopened", reopened.url)
        payload = self._latest_payload()
        confirmation = payload["processing_card_data_confirmations"]["card-a"]
        self.assertEqual(confirmation["status"], "reopened")
        self.assertEqual(confirmation["reopen_reason"], "Обнаружен дополнительный размер")
        self.assertNotIn("card-a", payload.get("processed_cards") or [])
        self.assertFalse(payload["cards"][0]["processed_done"])
        self.assertEqual(
            payload["processing_card_corrections"][-1]["reason"],
            "Обнаружен дополнительный размер",
        )

        saved = self.client.post(
            self.card_route,
            {
                "action": "save_results",
                "card_id": "card-a",
                "result_article[]": ["SKU-A", "SKU-A"],
                "result_source_article[]": ["SKU-A", "SKU-A"],
                "result_product_name[]": ["Товар А", "Товар А"],
                "result_barcode[]": ["BAR-A", "BAR-B"],
                "result_size[]": ["42", "44"],
                "result_destination[]": ["-", "-"],
                "result_line_key[]": [
                    "card-a|sku-a|42|bar-a|-|0",
                    "card-a|sku-a|44|bar-b|-|additional",
                ],
                "result_is_additional[]": ["0", "1"],
                "result_parent_line_key[]": [
                    "card-a|sku-a|42|bar-a|-|0",
                    "card-a|sku-a|42|bar-a|-|0",
                ],
                "result_received[]": ["10", "1"],
                "result_processed[]": ["10", "1"],
                "result_defect[]": ["0", "0"],
                "result_shortage[]": ["0", "0"],
                "result_labels_printed[]": ["0", "0"],
                "result_labels_unboxed[]": ["0", "0"],
                "result_shipped_qty[]": ["0", "0"],
                "result_tags_replaced[]": ["0", "0"],
            },
        )

        self.assertEqual(saved.status_code, 302)
        saved_rows = self._latest_payload().get("processing_results") or []
        self.assertTrue(
            any(
                row.get("card_id") == "card-a"
                and row.get("size") == "44"
                and row.get("barcode") == "BAR-B"
                for row in saved_rows
            )
        )
        self.assertEqual(WarehouseStockSnapshot.objects.count(), stock_count_before)

    def test_reopen_is_blocked_after_box_formation(self):
        ProcessingFlowSession.objects.create(
            order_id=self.order_id,
            order_type="processing",
            agent_id="test-agent",
            user=self.head_user,
            status=ProcessingFlowSession.STATUS_OPEN,
            flow_state={"boxes": [{"code": "OBR-BOX-1"}], "pallets": []},
        )

        page = self.client.get(self.card_route)
        self.assertEqual(page.status_code, 200)
        self.assertNotContains(page, 'value="reopen_processing_data"')
        blocked = self.client.post(
            self.card_route,
            {
                "action": "reopen_processing_data",
                "card_id": "card-a",
                "reopen_reason": "Попытка после упаковки",
            },
        )

        self.assertEqual(blocked.status_code, 302)
        self.assertIn("data_confirmation=reopen_blocked", blocked.url)
        payload = self._latest_payload()
        self.assertEqual(
            payload["processing_card_data_confirmations"]["card-a"]["status"],
            "confirmed",
        )


class ProcessingCardSaveSameArticleTests(TestCase):
    def setUp(self):
        self.order_id = "501"
        self.user = get_user_model().objects.create_user(username="ph_user_same", password="x")
        Employee.objects.create(
            full_name="Processing Head Same",
            user=self.user,
            role="processing_head",
            is_active=True,
        )
        self.client.force_login(self.user)
        self.agency = Agency.objects.create(agn_name="Same Article Agency")
        OrderAuditEntry.objects.create(
            order_id=self.order_id,
            order_type="processing",
            action="status",
            agency=self.agency,
            payload={
                "status": "processing_in_work",
                "status_label": "Взята в работу",
                "cards": [
                    {
                        "id": "card-a",
                        "article": "IP/AA5",
                        "rows": [{"size": "27", "barcode": "2000508267118", "qty": "100"}],
                    },
                    {
                        "id": "card-b",
                        "article": "IP/AA5",
                        "rows": [{"size": "27", "barcode": "2000508269068", "qty": "100"}],
                    },
                ],
            },
        )

    def _post_result(self, card_id: str, processed: str):
        return self.client.post(
            f"/orders/processing/{self.order_id}/card/{card_id}/?article={quote('IP/AA5')}",
            {
                "action": "save_results",
                "return": f"/orders/processing/{self.order_id}/work/",
                "return_to": f"/orders/processing/{self.order_id}/work/",
                "card_id": card_id,
                "result_article[]": ["IP/AA5"],
                "result_size[]": ["27"],
                "result_destination[]": ["-"],
                "result_received[]": ["100"],
                "result_processed[]": [processed],
                "result_defect[]": ["0"],
                "result_shortage[]": ["0"],
                "result_labels_printed[]": ["100"],
                "result_labels_unboxed[]": ["100"],
                "result_shipped_qty[]": ["0"],
                "result_tags_replaced[]": ["0"],
            },
        )

    def test_same_article_size_from_different_cards_do_not_overwrite(self):
        self.assertEqual(self._post_result("card-a", "100").status_code, 302)
        self.assertEqual(self._post_result("card-b", "90").status_code, 302)

        latest = (
            OrderAuditEntry.objects.filter(order_id=self.order_id, order_type="processing")
            .order_by("-id")
            .first()
        )
        self.assertIsNotNone(latest)
        rows = [row for row in (latest.payload or {}).get("processing_results") or [] if isinstance(row, dict)]
        self.assertEqual(len(rows), 2)
        by_card = {str(row.get("card_id") or "").strip().lower(): row for row in rows}
        self.assertEqual(by_card["card-a"].get("processed"), "100")
        self.assertEqual(by_card["card-b"].get("processed"), "90")


class ProcessingWorkFlowConditionsTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="ph_user_work", password="x")
        Employee.objects.create(
            full_name="Processing Head Work",
            user=self.user,
            role="processing_head",
            is_active=True,
        )
        self.client.force_login(self.user)
        self.agency = Agency.objects.create(agn_name="Work Conditions Agency")

    def _create_order(self, order_id: str, payload: dict):
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="processing",
            action="status",
            agency=self.agency,
            payload=payload,
        )

    def test_work_page_allows_parallel_boxes_when_one_card_is_ready(self):
        order_id = "610"
        payload = {
            "status": "processing_in_work",
            "status_label": "Взята в работу",
            "cards": [
                {"id": "card-a", "article": "SKU-A", "rows": [{"size": "42", "qty": "10"}]},
                {"id": "card-b", "article": "SKU-B", "rows": [{"size": "43", "qty": "10"}]},
            ],
            "processed_cards": ["card-a"],
            "processing_results": [
                {"card_id": "card-a", "article": "SKU-A", "size": "42", "destination": "-", "processed": "10"},
                {"card_id": "card-b", "article": "SKU-B", "size": "43", "destination": "-", "processed": "10"},
            ],
        }
        self._create_order(order_id, payload)

        response = self.client.get(f"/orders/processing/{order_id}/work/")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(bool(response.context["processing_all_cards_processed"]))
        self.assertTrue(bool(response.context["processing_results_ready_for_flow"]))
        self.assertEqual(response.context["ready_cards_count"], 1)
        self.assertTrue(bool(response.context["can_open_processing_flow"]))
        self.assertFalse(bool(response.context["can_complete_processing_flow"]))

    def test_work_page_blocks_when_results_are_not_filled(self):
        order_id = "611"
        payload = {
            "status": "processing_in_work",
            "status_label": "Взята в работу",
            "cards": [
                {"id": "card-a", "article": "SKU-A", "rows": [{"size": "42", "qty": "10"}]},
                {"id": "card-b", "article": "SKU-B", "rows": [{"size": "43", "qty": "10"}]},
            ],
            "processed_cards": ["card-a", "card-b"],
            "processing_results": [],
        }
        self._create_order(order_id, payload)

        response = self.client.get(f"/orders/processing/{order_id}/work/")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(bool(response.context["processing_all_cards_processed"]))
        self.assertFalse(bool(response.context["processing_results_ready_for_flow"]))
        self.assertFalse(bool(response.context["can_open_processing_flow"]))
        self.assertFalse(bool(response.context["can_complete_processing_flow"]))

    def test_work_page_allows_flow_when_cards_and_results_are_ready(self):
        order_id = "612"
        payload = {
            "status": "processing_in_work",
            "status_label": "Взята в работу",
            "cards": [
                {"id": "card-a", "article": "SKU-A", "rows": [{"size": "42", "qty": "10"}]},
                {"id": "card-b", "article": "SKU-B", "rows": [{"size": "43", "qty": "10"}]},
            ],
            "processed_cards": ["card-a", "card-b"],
            "processing_results": [
                {"card_id": "card-a", "article": "SKU-A", "size": "42", "destination": "-", "processed": "10"},
                {"card_id": "card-b", "article": "SKU-B", "size": "43", "destination": "-", "processed": "10"},
            ],
        }
        self._create_order(order_id, payload)

        response = self.client.get(f"/orders/processing/{order_id}/work/")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(bool(response.context["processing_all_cards_processed"]))
        self.assertTrue(bool(response.context["processing_results_ready_for_flow"]))
        self.assertTrue(bool(response.context["can_open_processing_flow"]))
        self.assertTrue(bool(response.context["can_complete_processing_flow"]))

    def test_work_page_keeps_packaging_available_after_reachtruck_source_depletion(self):
        order_id = "612-INBOUND"
        self._create_order(
            order_id,
            {
                "status": "processing_in_work",
                "status_label": "Взята в работу",
                "cards": [
                    {"id": "card-a", "article": "SKU-A", "rows": [{"size": "42", "qty": "10"}]},
                ],
                "processed_cards": ["card-a"],
                "processing_results": [
                    {
                        "card_id": "card-a",
                        "article": "SKU-A",
                        "size": "42",
                        "destination": "-",
                        "processed": "10",
                    },
                ],
            },
        )
        self._create_order(
            order_id,
            {
                "act": "placement",
                "act_state": "closed",
                "act_items_removed": True,
                "act_boxes": [{"code": "BOX-INBOUND-1"}],
                "act_pallets": [{"code": "PAL-INBOUND-1"}],
            },
        )

        response = self.client.get(f"/orders/processing/{order_id}/work/")

        self.assertEqual(response.status_code, 200)
        self.assertFalse(bool(response.context["placement_completed"]))
        self.assertTrue(bool(response.context["can_open_processing_flow"]))
        self.assertContains(response, "Поручить формирование коробов")

    def test_work_page_allows_access_after_placement_is_closed(self):
        order_id = "613"
        self._create_order(
            order_id,
            {
                "status": "processing_in_work",
                "status_label": "Взята в работу",
                "cards": [
                    {"id": "card-a", "article": "SKU-A", "rows": [{"size": "42", "qty": "10"}]},
                ],
                "processed_cards": ["card-a"],
                "processing_results": [
                    {"card_id": "card-a", "article": "SKU-A", "size": "42", "destination": "-", "processed": "10"},
                ],
            },
        )
        self._create_order(
            order_id,
            {
                "act": "placement",
                "act_state": "closed",
                "flow_closed": True,
            },
        )

        response = self.client.get(f"/orders/processing/{order_id}/work/")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(bool(response.context["placement_completed"]))


class ProcessingWarehouseMoveCreationTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="ph_user_wh_moves", password="x")
        Employee.objects.create(
            full_name="Processing Head Warehouse",
            user=self.user,
            role="processing_head",
            is_active=True,
        )
        self.client.force_login(self.user)
        self.agency = Agency.objects.create(agn_name="Warehouse Move Agency")
        self.request_factory = RequestFactory()

    def test_send_to_warehouse_uses_selected_destinations(self):
        order_id = "615"
        first_entry = OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="processing",
            action="status",
            agency=self.agency,
            payload={
                "status": "processing_in_work",
                "status_label": "Взята в работу",
                "cards": [
                    {"id": "card-a", "article": "SKU-A", "rows": [{"size": "42", "qty": "10"}]},
                ],
                "processed_cards": ["card-a"],
                "processing_results": [
                    {"card_id": "card-a", "article": "SKU-A", "size": "42", "destination": "-", "processed": "10"},
                ],
            },
        )
        second_entry = OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="processing",
            action="status",
            agency=self.agency,
            payload={
                "act": "placement",
                "act_state": "closed",
                "act_boxes": [
                    {"code": "BOX-WH-1", "items": [{"sku": "SKU-A", "qty": 10}]},
                ],
                "act_pallets": [
                    {
                        "code": "PAL-WH-1",
                        "boxes": ["BOX-WH-1"],
                        "items": [],
                        "location": {"zone": "OBR", "row": "", "section": "", "tier": "", "cell": ""},
                    }
                ],
            },
        )
        request = self.request_factory.post(
            f"/orders/processing/{order_id}/work/",
            data={"action": "send_to_warehouse"},
        )
        request.user = self.user
        created, skipped_existing, skipped_missing = _create_processing_warehouse_moves(
            order_id,
            [first_entry, second_entry],
            request,
            destinations_by_pallet={
                "PAL-WH-1": {"zone": "OS", "row": 2, "section": 3, "tier": 1, "cell": 4},
            },
        )

        self.assertEqual((created, skipped_existing, skipped_missing), (1, 0, 0))
        task = MoveTask.objects.get()
        self.assertEqual(task.pallet_code, "PAL-WH-1")
        self.assertEqual(task.to_zone, "OS")
        self.assertEqual(task.to_row, 2)
        self.assertEqual(task.to_section, 3)
        self.assertEqual(task.to_tier, 1)
        self.assertEqual(task.to_cell, 4)

    def test_send_to_warehouse_skips_pallets_without_confirmed_destination(self):
        order_id = "616"
        first_entry = OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="processing",
            action="status",
            agency=self.agency,
            payload={
                "status": "processing_in_work",
                "status_label": "Взята в работу",
            },
        )
        second_entry = OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="processing",
            action="status",
            agency=self.agency,
            payload={
                "act": "placement",
                "act_state": "closed",
                "act_boxes": [],
                "act_pallets": [
                    {
                        "code": "PAL-WH-2",
                        "boxes": [],
                        "items": [{"sku": "SKU-A", "qty": 5}],
                        "location": {"zone": "OBR", "row": "", "section": "", "tier": "", "cell": ""},
                    },
                    {
                        "code": "PAL-WH-3",
                        "boxes": [],
                        "items": [{"sku": "SKU-A", "qty": 5}],
                        "location": {"zone": "OBR", "row": "", "section": "", "tier": "", "cell": ""},
                    },
                ],
            },
        )
        request = self.request_factory.post(
            f"/orders/processing/{order_id}/work/",
            data={"action": "send_to_warehouse"},
        )
        request.user = self.user

        created, skipped_existing, skipped_missing = _create_processing_warehouse_moves(
            order_id,
            [first_entry, second_entry],
            request,
            destinations_by_pallet={
                "PAL-WH-2": {"zone": "OS", "row": 1, "section": 1, "tier": 1, "cell": 2},
                "PAL-WH-3": {"zone": "PR", "row": "", "section": "", "tier": "", "cell": ""},
            },
        )

        self.assertEqual((created, skipped_existing, skipped_missing), (1, 0, 1))
        self.assertEqual(MoveTask.objects.count(), 1)

    def test_work_page_prefills_suggested_destination_for_warehouse_move_modal(self):
        order_id = "617"
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="processing",
            action="status",
            agency=self.agency,
            payload={
                "status": "processing_in_work",
                "status_label": "Взята в работу",
                "cards": [
                    {"id": "card-a", "article": "SKU-A", "rows": [{"size": "42", "qty": "10"}]},
                ],
                "processed_cards": ["card-a"],
                "processing_results": [
                    {"card_id": "card-a", "article": "SKU-A", "size": "42", "destination": "-", "processed": "10"},
                ],
            },
        )
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="processing",
            action="status",
            agency=self.agency,
            payload={
                "status": "processing_in_work",
                "status_label": "Взята в работу",
                "act": "placement",
                "act_state": "closed",
                "act_boxes": [
                    {"code": "BOX-WH-SUGGEST-1", "items": [{"sku": "SKU-A", "qty": 10}]},
                ],
                "act_pallets": [
                    {
                        "code": "PAL-WH-SUGGEST-1",
                        "boxes": ["BOX-WH-SUGGEST-1"],
                        "items": [],
                        "location": {"zone": "OBR", "row": "", "section": "", "tier": "", "cell": ""},
                    }
                ],
            },
        )

        response = self.client.get(f"/orders/processing/{order_id}/work/")

        self.assertEqual(response.status_code, 200)
        rows = response.context["warehouse_move_rows"]
        self.assertEqual(len(rows), 1)
        destination = rows[0]["destination"]
        self.assertEqual(destination["zone"], "OS")
        self.assertEqual(destination["row"], 1)
        self.assertEqual(destination["section"], 1)
        self.assertEqual(destination["tier"], 1)
        self.assertEqual(destination["cell"], 1)
        self.assertIn("Сервис предложил место хранения", rows[0]["destination_note"])

    def test_processing_warehouse_move_progress_uses_warehouse_location_when_audit_missing(self):
        order_id = "617-WH-PROGRESS"
        storage_location = WarehouseWritePathService.ensure_location(
            warehouse_code="MSK",
            zone_code="OS",
            row_no=2,
            section_no=3,
            tier_no=1,
            cell_no=4,
        )
        WarehouseStockSnapshot.objects.create(
            agency=self.agency,
            source_context_type="legacy_stock",
            source_context_id=f"legacy-{order_id}",
            sku_code="SKU-A",
            name="Processing Item",
            size="42",
            barcode=f"BC-{order_id}",
            goods_type="gv",
            qty=10,
            available_qty=10,
            container_code="PAL-WH-PROG-1",
            location=storage_location,
            zone_code=storage_location.zone_code,
            zone_kind=storage_location.zone_kind,
            warehouse_state_code="stored",
        )

        progress = _processing_warehouse_move_progress(
            order_id,
            placement_pallets=[
                {
                    "code": "PAL-WH-PROG-1",
                    "location": {"zone": "OBR", "row": "", "section": "", "tier": "", "cell": ""},
                }
            ],
            agency=self.agency,
        )

        self.assertEqual(progress["done_count"], 1)
        self.assertEqual(progress["not_created_count"], 0)
        self.assertIn("PAL-WH-PROG-1", progress["implicit_done_codes"])
        self.assertEqual(progress["moves_by_pallet"]["PAL-WH-PROG-1"]["to_zone"], "OS")

    def test_send_to_warehouse_skips_pallet_already_moved_in_warehouse_without_audit(self):
        order_id = "617-WH-SKIP"
        first_entry = OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="processing",
            action="status",
            agency=self.agency,
            payload={
                "status": "processing_in_work",
                "status_label": "Взята в работу",
            },
        )
        second_entry = OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="processing",
            action="status",
            agency=self.agency,
            payload={
                "act": "placement",
                "act_state": "closed",
                "act_boxes": [],
                "act_pallets": [
                    {
                        "code": "PAL-WH-SKIP-1",
                        "boxes": [],
                        "items": [{"sku": "SKU-A", "qty": 5}],
                        "location": {"zone": "OBR", "row": "", "section": "", "tier": "", "cell": ""},
                    }
                ],
            },
        )
        storage_location = WarehouseWritePathService.ensure_location(
            warehouse_code="MSK",
            zone_code="OS",
            row_no=1,
            section_no=2,
            tier_no=1,
            cell_no=3,
        )
        WarehouseStockSnapshot.objects.create(
            agency=self.agency,
            source_context_type="legacy_stock",
            source_context_id=f"legacy-{order_id}",
            sku_code="SKU-A",
            name="Processing Item",
            size="42",
            barcode=f"BC-{order_id}",
            goods_type="gv",
            qty=5,
            available_qty=5,
            container_code="PAL-WH-SKIP-1",
            location=storage_location,
            zone_code=storage_location.zone_code,
            zone_kind=storage_location.zone_kind,
            warehouse_state_code="stored",
        )
        request = self.request_factory.post(
            f"/orders/processing/{order_id}/work/",
            data={"action": "send_to_warehouse"},
        )
        request.user = self.user

        created, skipped_existing, skipped_missing = _create_processing_warehouse_moves(
            order_id,
            [first_entry, second_entry],
            request,
            destinations_by_pallet={
                "PAL-WH-SKIP-1": {"zone": "OS", "row": 1, "section": 2, "tier": 1, "cell": 3},
            },
        )

        self.assertEqual((created, skipped_existing, skipped_missing), (0, 1, 0))
        self.assertEqual(MoveTask.objects.count(), 0)


class ProcessingWorkflowServiceTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="proc_service_user", password="x")
        Employee.objects.create(
            full_name="Processing Service User",
            user=self.user,
            role="processing_head",
            is_active=True,
        )
        self.client.force_login(self.user)
        self.agency = Agency.objects.create(agn_name="Processing Service Agency")
        self.request_factory = RequestFactory()

    def _create_processing_head_cancel_order(self, order_id: str, *, stage: str):
        status = "processing_in_work" if stage == PROCESSING_STAGE_TAKEN_INTO_PROCESSING else "processing_head"
        entry = OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="processing",
            action="status",
            agency=self.agency,
            payload={
                "status": status,
                "status_label": (
                    "взято в обработку"
                    if stage == PROCESSING_STAGE_TAKEN_INTO_PROCESSING
                    else "утверждено менеджером"
                ),
                "processing_stage": stage,
                "cards": [
                    {
                        "id": "card-a",
                        "article": "SKU-A",
                        "rows": [{"barcode": "BAR-A", "qty": "10"}],
                    }
                ],
            },
        )
        task = Task.objects.create(
            title=f"Заявка на обработку №{order_id}",
            route=f"/orders/processing/{order_id}/",
            assigned_to=Employee.objects.get(user=self.user),
            status="backlog",
        )
        return entry, task

    def test_processing_head_cancels_manager_approved_order_before_work(self):
        order_id = "35-CANCEL-BEFORE-WORK"
        _entry, task = self._create_processing_head_cancel_order(
            order_id,
            stage=PROCESSING_STAGE_MANAGER_APPROVED,
        )
        request = self.request_factory.post(
            f"/orders/processing/{order_id}/work/",
            data={
                "action": "cancel_processing_by_head",
                "processing_cancel_reason": "created_by_mistake",
                "processing_cancel_comment": "Заявка больше не требуется",
            },
        )
        request.user = self.user

        result = ProcessingWorkflowService.cancel_processing_by_head(
            order_id=order_id,
            request=request,
        )

        self.assertEqual(result.status, "cancelled")
        latest = (
            OrderAuditEntry.objects.filter(order_id=order_id, order_type="processing")
            .order_by("created_at", "id")
            .last()
        )
        self.assertEqual((latest.payload or {}).get("processing_stage"), PROCESSING_STAGE_CANCELLED)
        self.assertEqual((latest.payload or {}).get("cancelled_by"), "processing_head")
        task.refresh_from_db()
        self.assertEqual(task.status, "done")
        self.assertFalse(
            WarehouseReserve.objects.filter(
                context_type="processing",
                context_id=order_id,
            ).exists()
        )

    def test_processing_head_cancel_button_is_available_only_before_work(self):
        before_order_id = "35-CANCEL-BUTTON-BEFORE"
        before_entry, _task = self._create_processing_head_cancel_order(
            before_order_id,
            stage=PROCESSING_STAGE_MANAGER_APPROVED,
        )
        before_request = self.request_factory.get(
            f"/orders/processing/{before_order_id}/work/"
        )
        before_request.user = self.user
        before_context = ProcessingWorkflowService.build_processing_work_page_context(
            order_id=before_order_id,
            entries=[before_entry],
            payload=dict(before_entry.payload or {}),
            agency=self.agency,
            request=before_request,
        )

        after_order_id = "35-CANCEL-BUTTON-AFTER"
        after_entry, _task = self._create_processing_head_cancel_order(
            after_order_id,
            stage=PROCESSING_STAGE_TAKEN_INTO_PROCESSING,
        )
        after_request = self.request_factory.get(
            f"/orders/processing/{after_order_id}/work/"
        )
        after_request.user = self.user
        after_context = ProcessingWorkflowService.build_processing_work_page_context(
            order_id=after_order_id,
            entries=[after_entry],
            payload=dict(after_entry.payload or {}),
            agency=self.agency,
            request=after_request,
        )

        self.assertTrue(before_context["can_processing_head_cancel"])
        self.assertFalse(after_context["can_processing_head_cancel"])

    def test_processing_head_cancellation_is_blocked_after_work_started(self):
        order_id = "35-CANCEL-AFTER-WORK"
        self._create_processing_head_cancel_order(
            order_id,
            stage=PROCESSING_STAGE_TAKEN_INTO_PROCESSING,
        )
        request = self.request_factory.post(
            f"/orders/processing/{order_id}/work/",
            data={"action": "cancel_processing_by_head"},
        )
        request.user = self.user

        result = ProcessingWorkflowService.cancel_processing_by_head(
            order_id=order_id,
            request=request,
        )

        self.assertEqual(result.status, "cancel_blocked")
        self.assertIn("заявка уже взята в работу", result.error_message)
        self.assertFalse(
            OrderAuditEntry.objects.filter(
                order_id=order_id,
                order_type="processing",
                payload__processing_stage=PROCESSING_STAGE_CANCELLED,
            ).exists()
        )

    def test_processing_work_payload_restores_card_parameters_after_later_event(self):
        order_id = "card-param-persistence"
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="processing",
            action="status",
            agency=self.agency,
            payload={
                "status": "processing_in_work",
                "cards": [
                    {
                        "id": "card-a",
                        "article": "SKU-A",
                        "rows": [{"barcode": "BAR-A", "qty": "1"}],
                    }
                ],
            },
        )
        expected_params = [
            {
                "card_id": "card-a",
                "article": "SKU-A",
                "label": "Маркировка 58/40",
                "value": "1",
            }
        ]
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="processing",
            action="update",
            agency=self.agency,
            payload={"processing_card_extra_params": expected_params},
        )
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="processing",
            action="comment",
            agency=self.agency,
            payload={"comment": "Следующее событие заявки"},
        )

        entries = list(
            OrderAuditEntry.objects.filter(
                order_id=order_id,
                order_type="processing",
            ).order_by("created_at", "id")
        )

        self.assertEqual(
            _processing_work_payload_from_entries(entries).get(
                "processing_card_extra_params"
            ),
            expected_params,
        )

    def _create_processing_snapshot(self, *, order_id: str, state_code: str = "processing_in_progress"):
        location = WarehouseWritePathService.ensure_location(
            warehouse_code="MSK",
            zone_code="OBR",
        )
        snapshot = WarehouseStockSnapshot.objects.create(
            agency=self.agency,
            source_context_type="legacy_stock",
            source_context_id=f"legacy-{order_id}",
            sku_code="SKU-PROC",
            name="Processing Item",
            size="42",
            barcode=f"BC-{order_id}",
            goods_type="gv",
            qty=10,
            available_qty=0,
            processing_reserved_qty=10,
            container_code=f"PAL-{order_id}",
            location=location,
            zone_code=location.zone_code,
            zone_kind=location.zone_kind,
            warehouse_state_code=state_code,
        )
        WarehouseReserve.objects.create(
            agency=self.agency,
            reserve_type=WarehouseReserve.TYPE_PROCESSING,
            context_type="processing",
            context_id=order_id,
            sku_code=snapshot.sku_code,
            size=snapshot.size,
            barcode=snapshot.barcode,
            goods_type=snapshot.goods_type,
            qty_reserved=10,
            status=WarehouseReserve.STATUS_ACTIVE,
            source_document_type="processing_order",
            source_document_id=order_id,
            created_by=self.user,
        )
        return snapshot

    def _create_ready_processing_entries(self, order_id: str):
        first_entry = OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="processing",
            action="status",
            agency=self.agency,
            payload={
                "status": "processing_in_work",
                "status_label": "Взята в работу",
                "processing_stage": PROCESSING_STAGE_QUALITY_APPROVED,
                "processing_stage_label": "проверка качества пройдена",
                "quality_review": {"status": "approved"},
                "cards": [
                    {"id": "card-a", "article": "SKU-A", "rows": [{"size": "42", "qty": "10"}]},
                ],
                "processed_cards": ["card-a"],
                "processing_results": [
                    {"card_id": "card-a", "article": "SKU-A", "size": "42", "destination": "-", "processed": "10"},
                ],
            },
        )
        second_entry = OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="processing",
            action="update",
            agency=self.agency,
            payload={
                "act": "placement",
                "act_state": "closed",
                "act_boxes": [
                    {"code": "BOX-SERVICE-1", "items": [{"sku": "SKU-A", "qty": 10}]},
                ],
                "act_pallets": [
                    {
                        "code": "PAL-SERVICE-1",
                        "boxes": ["BOX-SERVICE-1"],
                        "items": [],
                        "location": {"zone": "OBR", "row": "", "section": "", "tier": "", "cell": ""},
                    }
                ],
            },
        )
        return [first_entry, second_entry]

    def _add_processing_act_entry(
        self,
        order_id: str,
        *,
        manager_response: str = "pending",
        client_response: str = "pending",
    ) -> OrderAuditEntry:
        return OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="processing",
            action="status",
            agency=self.agency,
            payload={
                "status": "processing_in_work",
                "status_label": "Акт обработки ожидает подтверждения",
                "processing_stage": PROCESSING_STAGE_QUALITY_APPROVED,
                "processing_stage_label": "Проверка качества пройдена",
                "quality_review": {"status": "approved"},
                "processing_act": {
                    "version": 1,
                    "status": "awaiting_confirmations",
                    "manager_response": manager_response,
                    "client_response": client_response,
                },
            },
        )

    def test_finish_processing_does_not_wait_for_pending_client_confirmation(self):
        order_id = "617-MANAGER-ONLY-ACT"
        entries = self._create_ready_processing_entries(order_id)
        entries.append(
            self._add_processing_act_entry(
                order_id,
                manager_response="confirmed",
                client_response="pending",
            )
        )
        request = self.request_factory.post(f"/orders/processing/{order_id}/work/")
        request.user = self.user

        with mock.patch(
            "processing_app.views._processing_finish_checks",
            return_value={"blockers": ["Контрольный следующий этап"]},
        ):
            result = ProcessingWorkflowService.finish_processing(
                order_id=order_id,
                entries=entries,
                request=request,
                role="processing_head",
            )

        self.assertEqual(result.status, "blocked")
        self.assertIn("Контрольный следующий этап", result.error_message)
        self.assertNotIn("клиент", result.error_message.lower())

    def test_client_cannot_confirm_processing_act(self):
        order_id = "617-CLIENT-ACT-READ-ONLY"
        entries = self._create_ready_processing_entries(order_id)
        entries.append(self._add_processing_act_entry(order_id))
        request = self.request_factory.post(
            f"/orders/processing/{order_id}/?client={self.agency.id}",
            data={"action": "confirm_processing_act_client"},
        )
        request.user = self.user
        request._client_agency = self.agency

        context = ProcessingWorkflowService.build_processing_detail_page_context(
            ctx={"agency": self.agency, "client_view": True},
            order_id=order_id,
            entries_list=entries,
            request=request,
            payload_from_entries=_processing_work_payload_from_entries,
        )
        result = ProcessingWorkflowService.handle_processing_detail_action(
            order_id=order_id,
            request=request,
            order_type="processing",
            payload_from_entries=_processing_work_payload_from_entries,
        )

        self.assertFalse(context["can_confirm_processing_act_client"])
        self.assertEqual(result.status, "forbidden")

    def test_manager_confirmation_uses_existing_finish_command(self):
        order_id = "617-MANAGER-ACT-FINISH"
        entries = self._create_ready_processing_entries(order_id)
        entries.append(self._add_processing_act_entry(order_id))
        manager_user = get_user_model().objects.create_user(
            username="proc_manager_act_finish",
            password="x",
        )
        Employee.objects.create(
            full_name="Processing Act Manager",
            user=manager_user,
            role="manager",
            is_active=True,
        )
        request = self.request_factory.post(
            f"/orders/processing/{order_id}/",
            data={"action": "confirm_processing_act_manager"},
        )
        request.user = manager_user

        with mock.patch.object(
            ProcessingWorkflowService,
            "finish_processing",
            return_value=ProcessingFinishResult(status="done"),
        ) as finish_processing:
            result = ProcessingWorkflowService.handle_processing_detail_action(
                order_id=order_id,
                request=request,
                order_type="processing",
                payload_from_entries=_processing_work_payload_from_entries,
            )
            repeated_result = ProcessingWorkflowService.handle_processing_detail_action(
                order_id=order_id,
                request=request,
                order_type="processing",
                payload_from_entries=_processing_work_payload_from_entries,
            )

        self.assertEqual(result.status, "ok")
        self.assertEqual(repeated_result.status, "ok")
        self.assertEqual(finish_processing.call_count, 2)
        self.assertEqual(finish_processing.call_args.kwargs["role"], "manager")
        latest_payload = _processing_work_payload_from_entries(
            list(
                OrderAuditEntry.objects.filter(
                    order_id=order_id,
                    order_type="processing",
                ).order_by("created_at", "id")
            )
        )
        self.assertEqual(
            latest_payload["processing_act"]["manager_response"],
            "confirmed",
        )
        self.assertEqual(latest_payload["processing_act"]["status"], "confirmed")
        self.assertEqual(
            OrderAuditEntry.objects.filter(
                order_id=order_id,
                order_type="processing",
                description="Менеджер подтвердил акт обработки",
            ).count(),
            1,
        )

    def _create_processing_service_fact(
        self,
        order_id: str,
        *,
        status: str = WarehouseServiceFact.STATUS_SENT_TO_BILLING,
    ) -> WarehouseServiceFact:
        service, _created = BillingService.objects.get_or_create(
            code="processing_close_test",
            defaults={"name": "Тестовая услуга обработки", "unit": "шт"},
        )
        return WarehouseServiceFact.objects.create(
            client=self.agency,
            order_type=WarehouseServiceFact.ORDER_PROCESSING,
            order_id=order_id,
            service=service,
            service_name_snapshot=service.name,
            quantity=10,
            unit="шт",
            status=status,
            reported_by=self.user,
        )

    def test_finish_processing_blocks_missing_confirmed_services_before_side_effects(self):
        order_id = "617-CLOSE-SERVICES"
        entries = self._create_ready_processing_entries(order_id)
        request = self.request_factory.post(
            f"/orders/processing/{order_id}/work/",
            data={"action": "finish_processing"},
        )
        request.user = self.user

        with mock.patch.object(
            WarehouseWritePathService,
            "consume_processing_input",
        ) as consume_processing_input:
            result = ProcessingWorkflowService.finish_processing(
                order_id=order_id,
                entries=entries,
                request=request,
                role="processing_head",
            )

        self.assertEqual(result.status, "blocked")
        self.assertIn("фактически оказанные услуги", result.error_message)
        consume_processing_input.assert_not_called()
        self.assertFalse(
            OrderAuditEntry.objects.filter(
                order_id=order_id,
                order_type="processing",
                payload__processing_stage=PROCESSING_STAGE_DONE,
            ).exists()
        )

    def test_work_context_allows_opening_services_modal_when_only_services_are_missing(self):
        order_id = "617-CLOSE-SERVICES-UI"
        entries = self._create_ready_processing_entries(order_id)
        request = self.request_factory.get(f"/orders/processing/{order_id}/work/")
        request.user = self.user

        context = ProcessingWorkflowService.build_processing_work_page_context(
            order_id=order_id,
            entries=entries,
            payload=_processing_work_payload_from_entries(entries),
            agency=self.agency,
            request=request,
        )

        self.assertFalse(context["finish_ready"])
        self.assertTrue(context["finish_action_ready"])
        self.assertFalse(context["finish_services_confirmed"])
        self.assertEqual(
            context["finish_blockers"],
            ["Подтвердите фактически оказанные услуги по заявке."],
        )

    def test_finish_checks_block_open_internal_processing_tasks(self):
        order_id = "617-CLOSE-TASKS"
        entries = self._create_ready_processing_entries(order_id)
        self._create_processing_service_fact(order_id)
        Task.objects.create(
            title="Формирование коробов",
            route=f"/orders/processing/{order_id}/flow/",
            assigned_to=Employee.objects.get(user=self.user),
            status="in_progress",
        )

        checks = _processing_finish_checks(
            order_id,
            _processing_work_payload_from_entries(entries),
            entries,
        )

        self.assertIn("Завершите внутренние задачи обработки: открыто 1.", checks["blockers"])
        self.assertEqual(checks["open_internal_tasks_count"], 1)

    def test_finish_checks_block_quantity_mismatch(self):
        order_id = "617-CLOSE-QUANTITY"
        entries = self._create_ready_processing_entries(order_id)
        first_payload = dict(entries[0].payload or {})
        first_payload["processing_results"] = [
            {"card_id": "card-a", "article": "SKU-A", "size": "42", "destination": "-", "processed": "9"},
        ]
        entries[0].payload = first_payload
        entries[0].save(update_fields=["payload"])
        placement_payload = dict(entries[1].payload or {})
        placement_payload["act_boxes"] = [
            {"code": "BOX-SERVICE-1", "items": [{"sku": "SKU-A", "qty": 9}]},
        ]
        entries[1].payload = placement_payload
        entries[1].save(update_fields=["payload"])
        self._create_processing_service_fact(order_id)

        checks = _processing_finish_checks(
            order_id,
            _processing_work_payload_from_entries(entries),
            entries,
        )

        self.assertIn(
            "Количество обработанного товара не совпадает с заявленным.",
            checks["blockers"],
        )
        self.assertEqual(
            checks["quantity_summary"],
            {"declared_qty": 10, "processed_qty": 9, "boxed_qty": 9, "matches": False},
        )

    def test_finish_checks_accept_completed_tasks_and_confirmed_services(self):
        order_id = "617-CLOSE-READY"
        entries = self._create_ready_processing_entries(order_id)
        service_fact = self._create_processing_service_fact(
            order_id,
            status=WarehouseServiceFact.STATUS_NEEDS_CLARIFICATION,
        )
        task = Task.objects.create(
            title="Формирование коробов",
            route=f"/orders/processing/{order_id}/flow/",
            assigned_to=Employee.objects.get(user=self.user),
            status="done",
        )

        blocked_checks = _processing_finish_checks(
            order_id,
            _processing_work_payload_from_entries(entries),
            entries,
        )
        self.assertFalse(blocked_checks["service_facts_confirmed"])
        self.assertIn("Подтвердите фактически оказанные услуги по заявке.", blocked_checks["blockers"])

        service_fact.status = WarehouseServiceFact.STATUS_SENT_TO_BILLING
        service_fact.save(update_fields=["status"])
        ready_checks = _processing_finish_checks(
            order_id,
            _processing_work_payload_from_entries(entries),
            entries,
        )

        task.refresh_from_db()
        self.assertEqual(task.status, "done")
        self.assertTrue(ready_checks["service_facts_confirmed"])
        self.assertEqual(ready_checks["open_internal_tasks_count"], 0)
        self.assertEqual(ready_checks["blockers"], [])

    def test_send_processing_to_warehouse_rejects_invalid_destination_json(self):
        order_id = "618"
        entries = self._create_ready_processing_entries(order_id)
        request = self.request_factory.post(
            f"/orders/processing/{order_id}/work/",
            data={"warehouse_destinations_json": "{bad json"},
        )
        request.user = self.user

        result = ProcessingWorkflowService.send_processing_to_warehouse(
            order_id=order_id,
            entries=entries,
            request=request,
        )

        self.assertEqual(result.status, "invalid_destination")
        self.assertIn("Некорректный JSON", result.error_message)

    def test_send_processing_to_warehouse_creates_linked_reachtruck_task(self):
        order_id = "618-LINKED"
        entries = self._create_ready_processing_entries(order_id)
        request = self.request_factory.post(
            f"/orders/processing/{order_id}/work/",
            data={
                "warehouse_destinations_json": json.dumps(
                    [
                        {
                            "pallet_code": "PAL-SERVICE-1",
                            "destination": {
                                "zone": "OS",
                                "row": 1,
                                "section": 2,
                                "tier": 1,
                                "cell": 3,
                            },
                        }
                    ]
                )
            },
        )
        request.user = self.user

        result = ProcessingWorkflowService.send_processing_to_warehouse(
            order_id=order_id,
            entries=entries,
            request=request,
        )

        self.assertEqual(result.status, "ok")
        self.assertEqual(result.created_count, 1)
        task = MoveTask.objects.get()
        self.assertEqual(task.from_zone, "OBR")
        self.assertEqual(task.to_zone, "OS")
        self.assertEqual(task.payload.get("processing_order_id"), order_id)

    def test_cancel_processing_warehouse_moves_returns_canceled_status(self):
        order_id = "619"
        entries = self._create_ready_processing_entries(order_id)
        create_request = self.request_factory.post(
            f"/orders/processing/{order_id}/work/",
            data={"action": "send_to_warehouse"},
        )
        create_request.user = self.user
        _create_processing_warehouse_moves(
            order_id,
            entries,
            create_request,
            destinations_by_pallet={
                "PAL-SERVICE-1": {"zone": "OS", "row": 1, "section": 2, "tier": 1, "cell": 3},
            },
        )
        cancel_request = self.request_factory.post(
            f"/orders/processing/{order_id}/work/",
            data={"action": "cancel_warehouse_moves"},
        )
        cancel_request.user = self.user

        result = ProcessingWorkflowService.cancel_processing_warehouse_moves(
            order_id=order_id,
            entries=entries,
            request=cancel_request,
        )

        self.assertEqual(result.status, "canceled")
        self.assertEqual(result.canceled_count, 1)
        task = MoveTask.objects.get()
        self.assertEqual(task.status, "canceled")

    def test_build_processing_work_page_context_marks_obr_delivery_task_as_active(self):
        order_id = "6191"
        entries = self._create_ready_processing_entries(order_id)
        move_request = MoveRequest.objects.create(
            context_type=MoveRequest.CONTEXT_PROCESSING,
            context_id=order_id,
            agency=self.agency,
            requested_by=self.user,
            requested_by_role="processing_head",
            requested_by_name="Processing Service User",
            destination_zone="OBR",
            status=MoveRequest.STATUS_CREATED,
        )
        MoveTask.objects.create(
            request=move_request,
            pallet_code="PAL-OBR-1",
            from_zone="OS",
            to_zone="OBR",
            move_mode=MoveTask.MODE_BOX_FULL,
            qty_planned=10,
            status=MoveTask.STATUS_IN_PROGRESS,
            payload={
                "status": "in_progress",
                "status_label": "Взята в работу",
                "requested_sku": "SKU-A",
                "requested_qty": 10,
                "processing_order_id": order_id,
            },
        )
        request = self.request_factory.get(f"/orders/processing/{order_id}/work/")
        request.user = self.user

        context = ProcessingWorkflowService.build_processing_work_page_context(
            order_id=order_id,
            entries=entries,
            payload=_processing_work_payload_from_entries(entries),
            agency=self.agency,
            request=request,
        )

        cards = context["draft_payload"]["cards"]
        self.assertEqual(len(cards), 1)
        self.assertTrue(cards[0]["delivery_has_active"])
        self.assertFalse(cards[0]["delivery_ready"])
        self.assertEqual(cards[0]["delivery_state_code"], "active")
        self.assertEqual(cards[0]["delivery_status_lines"], ["Взята в работу · 1 палл."])
        self.assertFalse(context["processing_delivery_completed"])

    def test_processing_delivery_legacy_acceptance_is_explicit_and_card_scoped(self):
        order_id = "6191-LEGACY-DELIVERY"
        entries = [
            OrderAuditEntry.objects.create(
                order_id=order_id,
                order_type="processing",
                action="status",
                agency=self.agency,
                payload={
                    "status": "processing_in_work",
                    "status_label": "Взята в работу",
                    "cards": [
                        {
                            "id": "legacy-card",
                            "article": "SKU-LEGACY",
                            "goods_type": "vz",
                            "rows": [{"barcode": "BC-LEGACY", "qty": "1"}],
                            "delivery_legacy_acceptance": {
                                "accepted": True,
                                "scope": "processing_delivery",
                                "reason": "Разовая приемка ранее доставленного товара.",
                            },
                        },
                        {
                            "id": "strict-card",
                            "article": "SKU-STRICT",
                            "goods_type": "vz",
                            "rows": [{"barcode": "BC-STRICT", "qty": "1"}],
                        },
                    ],
                },
            )
        ]
        request = self.request_factory.get(f"/orders/processing/{order_id}/work/")
        request.user = self.user

        context = ProcessingWorkflowService.build_processing_work_page_context(
            order_id=order_id,
            entries=entries,
            payload=_processing_work_payload_from_entries(entries),
            agency=self.agency,
            request=request,
        )

        cards = {card["id"]: card for card in context["draft_payload"]["cards"]}
        self.assertTrue(cards["legacy-card"]["delivery_ready"])
        self.assertEqual(cards["legacy-card"]["delivery_state_code"], "done")
        self.assertTrue(cards["legacy-card"]["delivery_legacy_accepted"])
        self.assertFalse(cards["strict-card"]["delivery_ready"])
        self.assertEqual(cards["strict-card"]["delivery_state_code"], "pending")

    def test_work_history_includes_reachtruck_task_number_and_status(self):
        order_id = "6191-HISTORY"
        entries = self._create_ready_processing_entries(order_id)
        move_request = MoveRequest.objects.create(
            context_type=MoveRequest.CONTEXT_PROCESSING,
            context_id=order_id,
            agency=self.agency,
            requested_by=self.user,
            requested_by_name="Руководитель обработки",
            destination_zone="OBR",
        )
        task = MoveTask.objects.create(
            request=move_request,
            pallet_code="PAL-OBR-HISTORY",
            to_zone="OBR",
            status=MoveTask.STATUS_CREATED,
            payload={"status_label": "Передано водителю"},
        )
        request = self.request_factory.get(f"/orders/processing/{order_id}/work/")
        request.user = self.user

        context = ProcessingWorkflowService.build_processing_work_page_context(
            order_id=order_id,
            entries=entries,
            payload=_processing_work_payload_from_entries(entries),
            agency=self.agency,
            request=request,
        )

        history_item = next(
            item
            for item in context["work_timeline"]
            if item["status"] == f"Задание ричтрака №{task.pk}"
        )
        self.assertEqual(history_item["description"], "Подача товара в зону обработки · Передано водителю.")

    def test_build_processing_work_page_context_matches_obr_delivery_by_request_items(self):
        order_id = "6191-REQUEST-ITEMS"
        entries = self._create_ready_processing_entries(order_id)
        move_request = MoveRequest.objects.create(
            context_type=MoveRequest.CONTEXT_PROCESSING,
            context_id=order_id,
            agency=self.agency,
            requested_by=self.user,
            requested_by_role="processing_head",
            requested_by_name="Processing Service User",
            destination_zone="OBR",
            status=MoveRequest.STATUS_CREATED,
        )
        MoveRequestItem.objects.create(
            request=move_request,
            sku_code="SKU-A",
            barcode="200000000100",
            goods_type="gv",
            qty_requested=10,
            qty_planned=10,
        )
        MoveTask.objects.create(
            request=move_request,
            pallet_code="PAL-OBR-REQUEST",
            from_zone="OS",
            to_zone="OBR",
            move_mode=MoveTask.MODE_BOX_FULL,
            qty_planned=10,
            status=MoveTask.STATUS_CREATED,
            payload={
                "status": "created",
                "status_label": "Передано ричтракеру",
                "processing_order_id": order_id,
            },
        )
        request = self.request_factory.get(f"/orders/processing/{order_id}/work/")
        request.user = self.user

        context = ProcessingWorkflowService.build_processing_work_page_context(
            order_id=order_id,
            entries=entries,
            payload=_processing_work_payload_from_entries(entries),
            agency=self.agency,
            request=request,
        )

        cards = context["draft_payload"]["cards"]
        self.assertEqual(len(cards), 1)
        self.assertTrue(cards[0]["delivery_has_active"])
        self.assertFalse(cards[0]["delivery_ready"])
        self.assertEqual(cards[0]["delivery_state_code"], "active")
        self.assertEqual(cards[0]["delivery_status_lines"], ["Передана водителю · 1 палл."])

    def test_build_processing_work_page_context_uses_warehouse_truth_for_obr_delivery(self):
        order_id = "6191-TRUTH-DONE"
        entries = [
            OrderAuditEntry.objects.create(
                order_id=order_id,
                order_type="processing",
                action="status",
                agency=self.agency,
                payload={
                    "status": "processing_in_work",
                    "status_label": "Взята в работу",
                    "cards": [
                        {
                            "id": "card-a",
                            "article": "SKU-PROC",
                            "goods_type": "gv",
                            "rows": [{"size": "42", "barcode": f"BC-{order_id}", "qty": "10"}],
                        },
                    ],
                    "processed_cards": ["card-a"],
                    "processing_results": [
                        {
                            "card_id": "card-a",
                            "article": "SKU-PROC",
                            "size": "42",
                            "destination": "-",
                            "processed": "10",
                        },
                    ],
                },
            ),
            OrderAuditEntry.objects.create(
                order_id=order_id,
                order_type="processing",
                action="status",
                agency=self.agency,
                payload={
                    "act": "placement",
                    "act_state": "closed",
                    "act_boxes": [{"code": "BOX-TRUTH-1", "items": [{"sku": "SKU-PROC", "qty": 10}]}],
                    "act_pallets": [
                        {
                            "code": "PAL-TRUTH-1",
                            "boxes": ["BOX-TRUTH-1"],
                            "items": [],
                            "location": {"zone": "OBR", "row": "", "section": "", "tier": "", "cell": ""},
                        }
                    ],
                },
            ),
        ]
        self._create_processing_snapshot(order_id=order_id, state_code="processing_in_progress")
        request = self.request_factory.get(f"/orders/processing/{order_id}/work/")
        request.user = self.user

        context = ProcessingWorkflowService.build_processing_work_page_context(
            order_id=order_id,
            entries=entries,
            payload=_processing_work_payload_from_entries(entries),
            agency=self.agency,
            request=request,
        )

        cards = context["draft_payload"]["cards"]
        self.assertEqual(len(cards), 1)
        self.assertFalse(cards[0]["delivery_has_active"])
        self.assertTrue(cards[0]["delivery_ready"])
        self.assertEqual(cards[0]["delivery_state_code"], "done")
        self.assertEqual(cards[0]["delivery_status_lines"], [])
        self.assertTrue(context["processing_delivery_completed"])

    def test_build_processing_work_page_context_exposes_box_discrepancy_by_sku(self):
        order_id = "6191-DISCREPANCY"
        entries = self._create_ready_processing_entries(order_id)
        placement_entry = entries[-1]
        placement_payload = dict(placement_entry.payload)
        placement_payload["act_boxes"] = [
            {"code": "BOX-SERVICE-1", "items": [{"sku": "SKU-A", "size": "42", "qty": 7}]},
        ]
        placement_entry.payload = placement_payload
        placement_entry.save(update_fields=["payload"])
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
            context["processing_quantity_discrepancy_rows"],
            [
                {
                    "sku_code": "sku-a",
                    "size": "42",
                    "name": "",
                    "expected_qty": 10,
                    "factual_qty": 7,
                    "delta_qty": -3,
                    "missing_qty": 3,
                    "extra_qty": 0,
                }
            ],
        )

    def test_build_processing_work_page_context_treats_reserved_stock_in_obr_as_delivered(self):
        order_id = "6191-TRUTH-RESERVED-OBR"
        entries = [
            OrderAuditEntry.objects.create(
                order_id=order_id,
                order_type="processing",
                action="status",
                agency=self.agency,
                payload={
                    "status": "processing_in_work",
                    "status_label": "Р’Р·СЏС‚Р° РІ СЂР°Р±РѕС‚Сѓ",
                    "cards": [
                        {
                            "id": "card-a",
                            "article": "SKU-PROC",
                            "goods_type": "gv",
                            "rows": [{"size": "42", "barcode": f"BC-{order_id}", "qty": "10"}],
                        },
                    ],
                    "processed_cards": ["card-a"],
                    "processing_results": [
                        {
                            "card_id": "card-a",
                            "article": "SKU-PROC",
                            "size": "42",
                            "destination": "-",
                            "processed": "10",
                        },
                    ],
                },
            ),
        ]
        self._create_processing_snapshot(order_id=order_id, state_code=WarehouseStateCode.RESERVED_FOR_PROCESSING.value)
        request = self.request_factory.get(f"/orders/processing/{order_id}/work/")
        request.user = self.user

        context = ProcessingWorkflowService.build_processing_work_page_context(
            order_id=order_id,
            entries=entries,
            payload=_processing_work_payload_from_entries(entries),
            agency=self.agency,
            request=request,
        )

        cards = context["draft_payload"]["cards"]
        self.assertEqual(len(cards), 1)
        self.assertFalse(cards[0]["delivery_has_active"])
        self.assertTrue(cards[0]["delivery_ready"])
        self.assertEqual(cards[0]["delivery_state_code"], "done")
        self.assertEqual(cards[0]["delivery_status_lines"], [])

    def test_build_processing_work_page_context_reopens_card_move_after_obr_cancel(self):
        order_id = "6192"
        entries = self._create_ready_processing_entries(order_id)
        move_request = MoveRequest.objects.create(
            context_type=MoveRequest.CONTEXT_PROCESSING,
            context_id=order_id,
            agency=self.agency,
            requested_by=self.user,
            requested_by_role="processing_head",
            requested_by_name="Processing Service User",
            destination_zone="OBR",
            status=MoveRequest.STATUS_CANCELED,
        )
        MoveTask.objects.create(
            request=move_request,
            pallet_code="PAL-OBR-2",
            from_zone="OS",
            to_zone="OBR",
            move_mode=MoveTask.MODE_BOX_FULL,
            qty_planned=10,
            status=MoveTask.STATUS_CANCELED,
            payload={
                "status": "canceled",
                "status_label": "Отменено руководителем обработки",
                "requested_sku": "SKU-A",
                "requested_qty": 10,
                "processing_order_id": order_id,
            },
        )
        request = self.request_factory.get(f"/orders/processing/{order_id}/work/")
        request.user = self.user

        context = ProcessingWorkflowService.build_processing_work_page_context(
            order_id=order_id,
            entries=entries,
            payload=_processing_work_payload_from_entries(entries),
            agency=self.agency,
            request=request,
        )

        cards = context["draft_payload"]["cards"]
        self.assertEqual(len(cards), 1)
        self.assertFalse(cards[0]["delivery_has_active"])
        self.assertFalse(cards[0]["delivery_ready"])
        self.assertEqual(cards[0]["delivery_state_code"], "canceled")
        self.assertEqual(cards[0]["delivery_status_lines"], [])

    def test_build_processing_work_page_context_prefers_existing_move_destination(self):
        order_id = "620"
        entries = self._create_ready_processing_entries(order_id)
        create_request = self.request_factory.post(
            f"/orders/processing/{order_id}/work/",
            data={"action": "send_to_warehouse"},
        )
        create_request.user = self.user
        _create_processing_warehouse_moves(
            order_id,
            entries,
            create_request,
            destinations_by_pallet={
                "PAL-SERVICE-1": {"zone": "MR", "row": 4, "section": "", "tier": "", "cell": ""},
            },
        )
        request = self.request_factory.get(f"/orders/processing/{order_id}/work/")
        request.user = self.user

        context = ProcessingWorkflowService.build_processing_work_page_context(
            order_id=order_id,
            entries=entries,
            payload=_processing_work_payload_from_entries(entries),
            agency=self.agency,
            request=request,
        )

        self.assertEqual(context["warehouse_destination_summary"], "MR · Между рядами · Ряд 4")
        self.assertEqual(len(context["warehouse_move_rows"]), 1)
        self.assertEqual(context["warehouse_move_rows"][0]["destination"]["zone"], "MR")
        self.assertEqual(context["warehouse_move_rows"][0]["destination"]["row"], 4)

    def test_finish_processing_reports_discrepancy_for_processing_head(self):
        order_id = "621"
        entries = self._create_ready_processing_entries(order_id)
        request = self.request_factory.post(
            f"/orders/processing/{order_id}/work/",
            data={"action": "finish_processing"},
        )
        request.user = self.user

        with mock.patch(
            "processing_app.views._processing_finish_checks",
            return_value={
                "blockers": [],
                "placement_payload": {"act_boxes": [{"code": "BOX-SERVICE-1"}], "act_pallets": [{"code": "PAL-SERVICE-1"}]},
                "placement_closed": True,
                "has_boxes": True,
                "has_pallets": True,
                "warehouse_move_created": True,
                "warehouse_move_completed": True,
                "warehouse_move_progress": {"total_pallets": 1, "done_count": 1},
            },
        ), mock.patch(
            "processing_app.views._processing_discrepancy_rows",
            return_value=[{"sku": "SKU-A", "expected_qty": 10, "actual_qty": 9}],
        ):
            result = ProcessingWorkflowService.finish_processing(
                order_id=order_id,
                entries=entries,
                request=request,
                role="processing_head",
            )

        self.assertEqual(result.status, "discrepancy_reported_head")
        status_entry = None
        for entry in OrderAuditEntry.objects.filter(order_id=order_id, order_type="processing").order_by("-id"):
            payload = entry.payload or {}
            if isinstance(payload, dict) and payload.get("discrepancy_status"):
                status_entry = entry
                break
        self.assertIsNotNone(status_entry)
        self.assertEqual((status_entry.payload or {}).get("discrepancy_status"), "reported")
        self.assertFalse(
            Task.objects.filter(
                route=f"/orders/processing/{order_id}/",
                assigned_to__role="processing_head",
                title=f"Разногласие по обработке №{order_id}",
            ).exclude(status="done").exists()
        )

    def test_finish_checks_do_not_block_declared_processed_mismatch(self):
        placement_entry = mock.Mock(
            payload={
                "act_state": "closed",
                "act_boxes": [{"code": "BOX-FACT"}],
                "act_pallets": [{"code": "PAL-FACT"}],
            }
        )
        open_tasks = mock.Mock()
        open_tasks.count.return_value = 0
        with (
            mock.patch(
                "processing_app.web_ui._processing_placement_entry",
                return_value=placement_entry,
            ),
            mock.patch(
                "processing_app.web_ui._processing_warehouse_move_progress",
                return_value={"has_any_task": True, "all_done": True, "not_created_count": 0},
            ),
            mock.patch(
                "processing_app.web_ui._processing_quantity_summary",
                return_value={"declared_qty": 70, "processed_qty": 60, "boxed_qty": 60},
            ),
            mock.patch(
                "processing_app.web_ui._processing_open_internal_tasks",
                return_value=open_tasks,
            ),
            mock.patch(
                "processing_app.web_ui._processing_confirmed_service_facts",
                return_value=True,
            ),
            mock.patch("processing_app.web_ui._order_has_open_boxes", return_value=False),
            mock.patch("processing_app.web_ui._processing_results_are_ready", return_value=True),
        ):
            result = _processing_finish_checks("29", {}, [])

        self.assertEqual(result["hard_blockers"], [])
        self.assertTrue(result["declared_processed_mismatch"])

    def test_finish_processing_rejects_invalid_stage_before_side_effects(self):
        order_id = "621-STAGE"
        entries = [
            OrderAuditEntry.objects.create(
                order_id=order_id,
                order_type="processing",
                action="status",
                agency=self.agency,
                payload={
                    "processing_stage": PROCESSING_STAGE_AWAITING_APPROVAL,
                    "status": "sent_unconfirmed",
                    "status_label": "Ждет подтверждения",
                },
            )
        ]
        request = self.request_factory.post(
            f"/orders/processing/{order_id}/work/",
            data={"action": "finish_processing"},
        )
        request.user = self.user

        with mock.patch(
            "processing_app.views._processing_finish_checks"
        ) as finish_checks, mock.patch.object(
            WarehouseWritePathService,
            "consume_processing_input",
        ) as consume_processing_input:
            result = ProcessingWorkflowService.finish_processing(
                order_id=order_id,
                entries=entries,
                request=request,
                role="processing_head",
            )

        self.assertEqual(result.status, "blocked")
        self.assertIn("проверку качества", result.error_message)
        finish_checks.assert_not_called()
        consume_processing_input.assert_not_called()
        self.assertFalse(
            OrderAuditEntry.objects.filter(
                order_id=order_id,
                order_type="processing",
                payload__processing_stage=PROCESSING_STAGE_DONE,
            ).exists()
        )

    def test_finish_processing_returns_done_and_closes_processing_tasks(self):
        order_id = "622"
        entries = self._create_ready_processing_entries(order_id)
        Task.objects.create(
            title=f"Processing order #{order_id}",
            route=f"/orders/processing/{order_id}/",
            assigned_to=Employee.objects.get(user=self.user),
        )
        request = self.request_factory.post(
            f"/orders/processing/{order_id}/work/",
            data={"action": "finish_processing"},
        )
        request.user = self.user

        with mock.patch(
            "processing_app.views._processing_finish_checks",
            return_value={
                "blockers": [],
                "placement_payload": {"act_boxes": [{"code": "BOX-SERVICE-1"}], "act_pallets": [{"code": "PAL-SERVICE-1"}]},
                "placement_closed": True,
                "has_boxes": True,
                "has_pallets": True,
                "warehouse_move_created": True,
                "warehouse_move_completed": True,
                "warehouse_move_progress": {"total_pallets": 1, "done_count": 1},
            },
        ), mock.patch("processing_app.views._processing_discrepancy_rows", return_value=[]):
            result = ProcessingWorkflowService.finish_processing(
                order_id=order_id,
                entries=entries,
                request=request,
                role="processing_head",
            )

        self.assertEqual(result.status, "done")
        latest = (
            OrderAuditEntry.objects.filter(order_id=order_id, order_type="processing")
            .order_by("-id")
            .first()
        )
        self.assertIsNotNone(latest)
        self.assertEqual((latest.payload or {}).get("status"), "done")
        self.assertFalse(
            Task.objects.filter(route=f"/orders/processing/{order_id}/").exclude(status="done").exists()
        )

    def test_finish_processing_releases_warehouse_reserve_when_processing_closes(self):
        order_id = "622-WH-REFRESH"
        entries = self._create_ready_processing_entries(order_id)
        WarehouseReserve.objects.create(
            agency=self.agency,
            reserve_type=WarehouseReserve.TYPE_PROCESSING,
            context_type="processing",
            context_id=order_id,
            sku_code="SKU-A",
            size="42",
            goods_type="gv",
            qty_reserved=10,
            status=WarehouseReserve.STATUS_ACTIVE,
        )
        request = self.request_factory.post(
            f"/orders/processing/{order_id}/work/",
            data={"action": "finish_processing"},
        )
        request.user = self.user

        with mock.patch(
            "processing_app.views._processing_finish_checks",
            return_value={
                "blockers": [],
                "placement_payload": {"act_boxes": [{"code": "BOX-SERVICE-1"}], "act_pallets": [{"code": "PAL-SERVICE-1"}]},
                "placement_closed": True,
                "has_boxes": True,
                "has_pallets": True,
                "warehouse_move_created": True,
                "warehouse_move_completed": True,
                "warehouse_move_progress": {"total_pallets": 1, "done_count": 1},
            },
        ), mock.patch("processing_app.views._processing_discrepancy_rows", return_value=[]):
            result = ProcessingWorkflowService.finish_processing(
                order_id=order_id,
                entries=entries,
                request=request,
                role="processing_head",
            )

        self.assertEqual(result.status, "done")
        reserve = WarehouseReserve.objects.get(
            agency=self.agency,
            reserve_type=WarehouseReserve.TYPE_PROCESSING,
            context_type="processing",
            context_id=order_id,
        )
        self.assertEqual(reserve.status, WarehouseReserve.STATUS_RELEASED)

    def test_build_processing_card_page_context_returns_selected_card_context(self):
        order_id = "623"
        entries = self._create_ready_processing_entries(order_id)
        request = self.request_factory.get(
            f"/orders/processing/{order_id}/card/card-a/",
            data={"return": f"/orders/processing/{order_id}/work/"},
        )
        request.user = self.user

        context = ProcessingWorkflowService.build_processing_card_page_context(
            order_id=order_id,
            card_id="card-a",
            request=request,
            payload=entries[0].payload or {},
            agency=self.agency,
        )

        self.assertEqual(context["card"]["article"], "SKU-A")
        self.assertEqual(context["return_url"], f"/orders/processing/{order_id}/work/")
        self.assertTrue(bool(context["card_processed"]))
        self.assertIn(f"/orders/processing/{order_id}/card/card-a/technical/", context["technical_card_url"])
        self.assertIn("/labels/settings/?return=", context["label_settings_url"])
        self.assertIn(f"%2Forders%2Fprocessing%2F{order_id}%2Fcard%2Fcard-a%2F", context["label_settings_url"])

    def test_build_processing_card_page_context_keeps_expanded_label_keys_separate(self):
        order_id = "623-labels"
        payload = {
            "status": "processing_in_work",
            "status_label": "Взята в работу",
            "marking_5860_qty": "1",
            "marking_75120_qty": "3",
            "cards": [
                {
                    "id": "card-a",
                    "article": "SKU-A",
                    "rows": [{"size": "42", "barcode": "BAR-42", "qty": "10"}],
                }
            ],
        }
        request = self.request_factory.get(f"/orders/processing/{order_id}/card/card-a/")
        request.user = self.user

        context = ProcessingWorkflowService.build_processing_card_page_context(
            order_id=order_id,
            card_id="card-a",
            request=request,
            payload=payload,
            agency=self.agency,
        )

        print_items = context["results_rows"][0]["print_items"]
        self.assertEqual(
            [(item["label_key"], item["label_type"], item["required_qty"]) for item in print_items],
            [("item_5860", "58/60", 10), ("item_75120", "75/120", 30)],
        )

    def test_manual_processing_parameter_is_scoped_to_selected_card(self):
        order_id = "623-card-param"
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="processing",
            action="status",
            agency=self.agency,
            payload={
                "status": "processing_in_work",
                "status_label": "Взята в работу",
                "cards": [
                    {
                        "id": "card-a",
                        "article": "SKU-A",
                        "product_name": "Товар A",
                        "rows": [{"size": "42", "barcode": "BAR-A", "qty": "10"}],
                    },
                    {
                        "id": "card-b",
                        "article": "SKU-B",
                        "product_name": "Товар B",
                        "rows": [{"size": "44", "barcode": "BAR-B", "qty": "5"}],
                    },
                ],
                "processing_results": [
                    {
                        "card_id": "card-a",
                        "article": "SKU-A",
                        "size": "42",
                        "barcode": "BAR-A",
                        "destination": "-",
                        "processed": "10",
                    },
                    {
                        "card_id": "card-b",
                        "article": "SKU-B",
                        "size": "44",
                        "barcode": "BAR-B",
                        "destination": "-",
                        "processed": "5",
                    },
                ],
            },
        )
        add_request = self.request_factory.post(
            f"/orders/processing/{order_id}/card/card-a/",
            data={
                "action": "add_processing_param",
                "card_id": "card-a",
                "manual_param_label": "Маркировка 58/40",
                "manual_param_value": "2",
            },
        )
        add_request.user = self.user

        result = ProcessingWorkflowService.handle_processing_card_action(
            order_id=order_id,
            request=add_request,
            action="add_processing_param",
        )

        self.assertEqual(result.status, "ok")
        latest_payload = (
            OrderAuditEntry.objects.filter(order_id=order_id, order_type="processing")
            .latest("id")
            .payload
            or {}
        )
        self.assertFalse(bool(latest_payload.get("marking_5840_qty")))
        self.assertFalse(bool(latest_payload.get("processing_head_extra_params")))
        self.assertEqual(
            latest_payload.get("processing_card_extra_params"),
            [
                {
                    "card_id": "card-a",
                    "article": "SKU-A",
                    "label": "Маркировка 58/40",
                    "value": "2",
                    "added_by": "proc_service_user",
                    "added_at": mock.ANY,
                }
            ],
        )

        card_a_request = self.request_factory.get(f"/orders/processing/{order_id}/card/card-a/")
        card_a_request.user = self.user
        card_a_context = ProcessingWorkflowService.build_processing_card_page_context(
            order_id=order_id,
            card_id="card-a",
            request=card_a_request,
            payload=latest_payload,
            agency=self.agency,
        )
        card_b_request = self.request_factory.get(f"/orders/processing/{order_id}/card/card-b/")
        card_b_request.user = self.user
        card_b_context = ProcessingWorkflowService.build_processing_card_page_context(
            order_id=order_id,
            card_id="card-b",
            request=card_b_request,
            payload=latest_payload,
            agency=self.agency,
        )

        scoped_rows = [
            row
            for row in card_a_context["processing_params"]
            if row.get("scope") == "card"
        ]
        self.assertEqual(
            [(row["label"], row["value"], row["article"]) for row in scoped_rows],
            [("Маркировка 58/40", "2", "SKU-A")],
        )
        self.assertFalse(
            any(row.get("scope") == "card" for row in card_b_context["processing_params"])
        )
        self.assertEqual(
            [button["label_key"] for button in card_a_context["label_print_buttons"]],
            ["item"],
        )
        self.assertEqual(card_b_context["label_print_buttons"], [])
        self.assertEqual(card_a_context["results_rows"][0]["print_items"][0]["required_qty"], 20)
        self.assertEqual(card_b_context["results_rows"][0]["print_items"], [])

    def test_sized_packaging_parameters_are_saved_for_selected_card(self):
        order_id = "623-card-packaging-params"
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="processing",
            action="status",
            agency=self.agency,
            payload={
                "status": "processing_in_work",
                "status_label": "Взята в работу",
                "cards": [
                    {
                        "id": "card-a",
                        "article": "SKU-A",
                        "product_name": "Товар A",
                        "rows": [{"size": "42", "barcode": "BAR-A", "qty": "10"}],
                    }
                ],
            },
        )

        for label, size, qty in (
            ("Упаковка в курьерский пакет", "30x40", "12"),
            ("Упаковка в пакет зип лок", "20x30", "7"),
        ):
            request = self.request_factory.post(
                f"/orders/processing/{order_id}/card/card-a/",
                data={
                    "action": "add_processing_param",
                    "card_id": "card-a",
                    "manual_param_label": label,
                    "manual_param_size": size,
                    "manual_param_qty": qty,
                },
            )
            request.user = self.user
            result = ProcessingWorkflowService.handle_processing_card_action(
                order_id=order_id,
                request=request,
                action="add_processing_param",
            )
            self.assertEqual(result.status, "ok")

        latest_payload = (
            OrderAuditEntry.objects.filter(order_id=order_id, order_type="processing")
            .latest("id")
            .payload
            or {}
        )
        self.assertEqual(
            [
                (item.get("label"), item.get("value"))
                for item in latest_payload.get("processing_card_extra_params") or []
            ],
            [
                ("Упаковка в курьерский пакет", "Размер: 30x40; Кол-во: 12"),
                ("Упаковка в пакет зип лок", "Размер: 20x30; Кол-во: 7"),
            ],
        )

    def test_sized_packaging_parameter_requires_size_and_positive_quantity(self):
        order_id = "623-card-packaging-invalid"
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="processing",
            action="status",
            agency=self.agency,
            payload={
                "status": "processing_in_work",
                "cards": [{"id": "card-a", "article": "SKU-A", "rows": []}],
            },
        )
        request = self.request_factory.post(
            f"/orders/processing/{order_id}/card/card-a/",
            data={
                "action": "add_processing_param",
                "card_id": "card-a",
                "manual_param_label": "Упаковка в курьерский пакет",
                "manual_param_size": "",
                "manual_param_qty": "0",
            },
        )
        request.user = self.user

        result = ProcessingWorkflowService.handle_processing_card_action(
            order_id=order_id,
            request=request,
            action="add_processing_param",
        )

        self.assertEqual(result.status, "ok")
        latest_payload = (
            OrderAuditEntry.objects.filter(order_id=order_id, order_type="processing")
            .latest("id")
            .payload
            or {}
        )
        self.assertFalse(latest_payload.get("processing_card_extra_params"))

    def test_cancel_manual_processing_parameter_removes_only_selected_card_value(self):
        order_id = "623-card-param-cancel"
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="processing",
            action="status",
            agency=self.agency,
            payload={
                "status": "processing_in_work",
                "status_label": "Взята в работу",
                "cards": [
                    {"id": "card-a", "article": "SKU-A", "rows": [{"size": "42", "qty": "10"}]},
                    {"id": "card-b", "article": "SKU-B", "rows": [{"size": "44", "qty": "5"}]},
                ],
                "processing_card_extra_params": [
                    {
                        "card_id": "card-a",
                        "article": "SKU-A",
                        "label": "Маркировка 58/40",
                        "value": "2",
                    },
                    {
                        "card_id": "card-b",
                        "article": "SKU-B",
                        "label": "Маркировка 58/40",
                        "value": "3",
                    },
                ],
            },
        )
        cancel_request = self.request_factory.post(
            f"/orders/processing/{order_id}/card/card-a/",
            data={
                "action": "cancel_processing_param",
                "card_id": "card-a",
                "manual_param_label": "Маркировка 58/40",
            },
        )
        cancel_request.user = self.user

        result = ProcessingWorkflowService.handle_processing_card_action(
            order_id=order_id,
            request=cancel_request,
            action="cancel_processing_param",
        )

        self.assertEqual(result.status, "ok")
        latest_payload = (
            OrderAuditEntry.objects.filter(order_id=order_id, order_type="processing")
            .latest("id")
            .payload
            or {}
        )
        self.assertEqual(
            latest_payload.get("processing_card_extra_params"),
            [
                {
                    "card_id": "card-b",
                    "article": "SKU-B",
                    "label": "Маркировка 58/40",
                    "value": "3",
                    "added_by": "",
                    "added_at": "",
                }
            ],
        )

    def test_handle_processing_card_action_marks_card_done(self):
        order_id = "624"
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="processing",
            action="status",
            agency=self.agency,
            payload={
                "status": "processing_in_work",
                "status_label": "Взята в работу",
                "cards": [{"id": "card-a", "article": "SKU-A", "rows": [{"size": "42", "qty": "10"}]}],
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
        request = self.request_factory.post(
            f"/orders/processing/{order_id}/card/card-a/",
            data={
                "action": "finish_card",
                "card_id": "card-a",
                "data_match_status": "match",
                "worker_data_confirmed": "1",
            },
        )
        request.user = self.user

        result = ProcessingWorkflowService.handle_processing_card_action(
            order_id=order_id,
            request=request,
            action="finish_card",
        )

        self.assertEqual(result.status, "ok")
        latest = (
            OrderAuditEntry.objects.filter(order_id=order_id, order_type="processing")
            .order_by("-id")
            .first()
        )
        self.assertIsNotNone(latest)
        payload = latest.payload or {}
        self.assertIn("card-a", payload.get("processed_cards") or [])
        card = next(
            (
                item
                for item in (payload.get("cards") or [])
                if isinstance(item, dict) and item.get("id") == "card-a"
            ),
            None,
        )
        self.assertIsNotNone(card)
        self.assertTrue(bool(card.get("processed_done")))


    def test_handle_processing_card_action_rejects_finish_without_processed_quantity(self):
        order_id = "624-empty"
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="processing",
            action="status",
            agency=self.agency,
            payload={
                "status": "processing_in_work",
                "status_label": "Взята в работу",
                "cards": [{"id": "card-a", "article": "SKU-A", "rows": [{"size": "42", "qty": "10"}]}],
                "processing_results": [
                    {
                        "card_id": "card-a",
                        "article": "SKU-A",
                        "size": "42",
                        "destination": "-",
                        "processed": "",
                    }
                ],
            },
        )
        request = self.request_factory.post(
            f"/orders/processing/{order_id}/card/card-a/",
            data={
                "action": "finish_card",
                "card_id": "card-a",
                "data_match_status": "match",
                "worker_data_confirmed": "1",
            },
        )
        request.user = self.user

        result = ProcessingWorkflowService.handle_processing_card_action(
            order_id=order_id,
            request=request,
            action="finish_card",
        )

        self.assertEqual(result.status, "not_ready")
        latest = (
            OrderAuditEntry.objects.filter(order_id=order_id, order_type="processing")
            .order_by("-id")
            .first()
        )
        payload = latest.payload or {}
        self.assertNotIn("card-a", payload.get("processed_cards") or [])
        card = next(item for item in payload["cards"] if item.get("id") == "card-a")
        self.assertFalse(bool(card.get("processed_done")))

    def test_scan_processing_flow_marking_returns_not_required_when_cz_disabled(self):
        order_id = "625"
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="processing",
            action="status",
            agency=self.agency,
            payload={
                "status": "processing_in_work",
                "status_label": "Взята в работу",
                "cards": [{"id": "card-a", "article": "SKU-A", "rows": [{"size": "42", "qty": "10"}]}],
            },
        )
        request = self.request_factory.post(
            f"/orders/processing/{order_id}/flow/marking-scan/",
            data=json.dumps({"code": "CZ-1", "box_barcode": "BOX-1", "agent_id": "agent-1"}),
            content_type="application/json",
        )
        request.user = self.user

        result = ProcessingWorkflowService.scan_processing_flow_marking(
            order_id=order_id,
            request=request,
        )

        self.assertEqual(result.status, "not_required")
        self.assertEqual(result.http_status, 400)
        self.assertEqual(result.payload.get("error"), "ЧЗ не требуется.")

    def test_normalize_marking_code_strips_gs1_datamatrix_symbology_identifier(self):
        from .views import _normalize_marking_code

        canonical_code = "010466640680002221SERIAL62501"

        self.assertEqual(_normalize_marking_code(f"]d2{canonical_code}"), canonical_code)
        self.assertEqual(_normalize_marking_code(f"]D2{canonical_code}"), canonical_code)
        self.assertEqual(_normalize_marking_code(canonical_code), canonical_code)

    def test_processing_with_remarking_binds_honest_sign_code_to_box_and_closes_flow(self):
        order_id = "625-CZ"
        total_qty = 10
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="processing",
            action="status",
            agency=self.agency,
            payload={
                "status": "processing_in_work",
                "status_label": "Взята в работу",
                "processing_stage": PROCESSING_STAGE_UNBOXING_OPENED,
                "processing_stage_label": "открыта раскоробовка",
                "marking_5840_each_qty": "1",
                "cards": [{"id": "card-cz", "article": "SKU-CZ", "rows": [{"size": "42", "qty": str(total_qty)}]}],
                "processed_cards": ["card-cz"],
                "processing_results": [
                    {
                        "card_id": "card-cz",
                        "article": "SKU-CZ",
                        "size": "42",
                        "destination": "-",
                        "processed": str(total_qty),
                        "result_barcode": "BAR-CZ-42",
                    }
                ],
            },
        )
        marking_code = MarkingCode.objects.create(
            agency=self.agency,
            code="CZ-REMARK-625-01",
            sku_code="SKU-CZ",
            size="42",
            barcode="BAR-CZ-42",
        )
        scan_request = self.request_factory.post(
            f"/orders/processing/{order_id}/flow/marking-scan/",
            data=json.dumps({"code": marking_code.code, "box_barcode": "BOX-CZ-01", "agent_id": "agent-cz"}),
            content_type="application/json",
        )
        scan_request.user = self.user

        scan_result = ProcessingWorkflowService.scan_processing_flow_marking(
            order_id=order_id,
            request=scan_request,
        )

        self.assertEqual(scan_result.status, "ok")
        marking_code.refresh_from_db()
        self.assertEqual(marking_code.order_id, order_id)
        self.assertEqual(marking_code.box_barcode, "BOX-CZ-01")
        self.assertEqual(marking_code.used_by, self.user)
        self.assertIsNotNone(marking_code.used_at)

        entries = list(OrderAuditEntry.objects.filter(order_id=order_id, order_type="processing").order_by("created_at"))
        completion_request = self.request_factory.post(
            f"/orders/processing/{order_id}/flow/",
            data={
                "agent_id": "agent-cz",
                "boxes_json": json.dumps(
                    [
                        {
                            "code": "BOX-CZ-01",
                            "items": [
                                {
                                    "sku": "SKU-CZ",
                                    "name": "Товар с ЧЗ",
                                    "size": "42",
                                    "barcode": "BAR-CZ-42",
                                    "qty": total_qty,
                                }
                            ],
                        }
                    ]
                ),
                "pallets_json": json.dumps(
                    [{"code": "PAL-CZ-01", "boxes": ["BOX-CZ-01"], "items": []}]
                ),
            },
        )
        completion_request.user = self.user

        with mock.patch("processing_app.views._state_has_open_box_with_items", return_value=False):
            completion = ProcessingWorkflowService.complete_processing_flow(
                order_id=order_id,
                entries=entries,
                request=completion_request,
                normalize_flow_state=lambda boxes, pallets, active_box, active_pallet: {
                    "boxes": boxes,
                    "pallets": pallets,
                    "activeBox": active_box,
                    "activePallet": active_pallet,
                },
                items_from_placement_act=lambda _entries: [],
                can_start=True,
                can_finish_with_mismatch=False,
                can_reassign_boxes=True,
            )

        self.assertEqual(completion.status, "ok")
        completed = OrderAuditEntry.objects.filter(order_id=order_id, order_type="processing").latest("id")
        marking_snapshot = (completed.payload.get("act_marking_boxes") or [])[0]
        self.assertEqual(marking_snapshot["code"], marking_code.code)
        self.assertEqual(marking_snapshot["sku_code"], "SKU-CZ")
        self.assertEqual(marking_snapshot["size"], "42")
        self.assertEqual(marking_snapshot["barcode"], "BAR-CZ-42")
        self.assertEqual(marking_snapshot["box_barcode"], "BOX-CZ-01")
        self.assertTrue(marking_snapshot["box_label"])

    def test_build_processing_technical_card_page_context_builds_checks(self):
        order_id = "626"
        entries = self._create_ready_processing_entries(order_id)
        payload = dict(entries[0].payload or {})
        payload.update(
            {
                "measure_needed": "Да",
                "measure_weight": "100",
                "marking_5840_qty": "2",
                "bubble_wrap_supply": "Клиент",
            }
        )
        request = self.request_factory.get(f"/orders/processing/{order_id}/card/card-a/technical/")
        request.user = self.user
        base_ctx = ProcessingWorkflowService.build_processing_card_page_context(
            order_id=order_id,
            card_id="card-a",
            request=request,
            payload=payload,
            agency=self.agency,
        )

        context = ProcessingWorkflowService.build_processing_technical_card_page_context(
            ctx=base_ctx,
            order_id=order_id,
            card_id="card-a",
            request=request,
            payload=payload,
        )

        self.assertTrue(bool(context["tech_checks"]["measure_yes"]))
        self.assertTrue(bool(context["tech_checks"]["marking_sticker_2"]))
        self.assertTrue(bool(context["tech_checks"]["marking_58x40_2"]))
        self.assertTrue(bool(context["tech_checks"]["bubble_wrap_supply_client"]))
        self.assertEqual(context["tech"]["measure_weight"], "100")
        self.assertIn(f"/orders/processing/{order_id}/card/card-a/", context["card_page_url"])

    def test_build_processing_technical_card_page_context_fills_empty_product_details_from_sku(self):
        order_id = "626-sku"
        entries = self._create_ready_processing_entries(order_id)
        payload = dict(entries[0].payload or {})
        SKU.objects.create(
            agency=self.agency,
            sku_code="SKU-A",
            name="Product from SKU",
            brand="Brand from SKU",
            color="White",
            composition="Cotton",
            gender="Unisex",
            season="All season",
            tovar_category="Decor",
            img="https://example.test/product.webp",
            source="manual",
        )
        request = self.request_factory.get(f"/orders/processing/{order_id}/card/card-a/technical/")
        request.user = self.user
        request._processing_card_agency = self.agency
        base_ctx = ProcessingWorkflowService.build_processing_card_page_context(
            order_id=order_id,
            card_id="card-a",
            request=request,
            payload=payload,
            agency=self.agency,
        )

        context = ProcessingWorkflowService.build_processing_technical_card_page_context(
            ctx=base_ctx,
            order_id=order_id,
            card_id="card-a",
            request=request,
            payload=payload,
        )

        self.assertEqual(context["card"]["product_name"], "Product from SKU")
        self.assertEqual(context["card"]["photo_url"], "https://example.test/product.webp")
        self.assertEqual(context["tech"]["supplier"], "Processing Service Agency")
        self.assertEqual(context["tech"]["brand"], "Brand from SKU")
        self.assertEqual(context["tech"]["subject"], "Decor")
        self.assertEqual(context["tech"]["color"], "White")
        self.assertEqual(context["tech"]["composition"], "Cotton")
        self.assertEqual(context["tech"]["gender"], "Unisex")
        self.assertEqual(context["tech"]["season"], "All season")

    def test_build_processing_technical_card_page_context_marks_expanded_sizes(self):
        order_id = "626-b"
        entries = self._create_ready_processing_entries(order_id)
        payload = dict(entries[0].payload or {})
        payload.update(
            {
                "marking_5860_qty": "1",
                "marking_75120_qty": "3",
                "marking_info": "Да",
            }
        )
        request = self.request_factory.get(f"/orders/processing/{order_id}/card/card-a/technical/")
        request.user = self.user
        base_ctx = ProcessingWorkflowService.build_processing_card_page_context(
            order_id=order_id,
            card_id="card-a",
            request=request,
            payload=payload,
            agency=self.agency,
        )

        context = ProcessingWorkflowService.build_processing_technical_card_page_context(
            ctx=base_ctx,
            order_id=order_id,
            card_id="card-a",
            request=request,
            payload=payload,
        )

        self.assertTrue(bool(context["tech_checks"]["marking_58_60"]))
        self.assertTrue(bool(context["tech_checks"]["marking_58x60_1"]))
        self.assertTrue(bool(context["tech_checks"]["marking_75_120"]))
        self.assertTrue(bool(context["tech_checks"]["marking_75x120_3"]))
        self.assertEqual(context["tech"]["marking_5860_qty"], "1")
        self.assertEqual(context["tech"]["marking_75120_qty"], "3")

    def test_build_processing_label_print_page_context_enriches_rows_and_status(self):
        order_id = "627"
        payload = {
            "status": "processing_in_work",
            "status_label": "Взята в работу",
            "marking_5840_qty": "2",
            "marking_5840_each_qty": "1",
            "cards": [
                {
                    "id": "card-a",
                    "article": "SKU-A",
                    "rows": [{"size": "42", "barcode": "BAR-42", "qty": "10"}],
                }
            ],
        }
        request = self.request_factory.get(f"/orders/processing/{order_id}/card/card-a/labels/")
        request.user = self.user
        base_ctx = ProcessingWorkflowService.build_processing_card_page_context(
            order_id=order_id,
            card_id="card-a",
            request=request,
            payload=payload,
            agency=self.agency,
        )
        MarkingCode.objects.create(
            agency=self.agency,
            code="CZ-CODE-1",
            barcode="BAR-42",
            size="42",
            order_type="processing",
            order_id=order_id,
            sku_code="SKU-A",
        )
        ProcessingPrintJob.objects.create(
            order_id=order_id,
            card_id="card-a",
            article="SKU-A",
            barcode="BAR-42",
            size="42",
            printer_name="Printer",
            label_png_base64="xxx",
            label_width_mm=58,
            label_height_mm=40,
            status=ProcessingPrintJob.STATUS_PENDING,
        )

        context = ProcessingWorkflowService.build_processing_label_print_page_context(
            ctx=base_ctx,
            order_id=order_id,
            card_id="card-a",
            request=request,
            payload=payload,
            agency=self.agency,
        )

        self.assertEqual(context["label_rows"][0]["print_qty_no_cz"], 20)
        self.assertEqual(context["label_rows"][0]["print_qty_cz"], 10)
        self.assertIn("CZ-CODE-1", context["label_rows"][0]["cz_codes"])
        self.assertEqual(context["print_queue_pending"], 1)
        self.assertIn(f"/orders/processing/{order_id}/card/card-a/", context["processing_card_url"])

    def test_build_processing_label_print_page_context_supports_expanded_no_cz_sizes(self):
        order_id = "627-b"
        payload = {
            "status": "processing_in_work",
            "status_label": "Взята в работу",
            "marking_5860_qty": "1",
            "marking_75120_qty": "3",
            "cards": [
                {
                    "id": "card-a",
                    "article": "SKU-A",
                    "rows": [{"size": "42", "barcode": "BAR-42", "qty": "10"}],
                }
            ],
        }
        request = self.request_factory.get(f"/orders/processing/{order_id}/card/card-a/labels/")
        request.user = self.user
        base_ctx = ProcessingWorkflowService.build_processing_card_page_context(
            order_id=order_id,
            card_id="card-a",
            request=request,
            payload=payload,
            agency=self.agency,
        )

        context = ProcessingWorkflowService.build_processing_label_print_page_context(
            ctx=base_ctx,
            order_id=order_id,
            card_id="card-a",
            request=request,
            payload=payload,
            agency=self.agency,
        )

        self.assertEqual(
            [item["key"] for item in context["label_sizes"]],
            ["item_5860", "item_75120"],
        )
        self.assertEqual(context["label_rows"][0]["print_qtys"]["item_5860"], 10)
        self.assertEqual(context["label_rows"][0]["print_qtys"]["item_75120"], 30)
        self.assertEqual(context["label_rows"][0]["print_qty_no_cz"], 40)

    def test_article_change_label_values_are_used_only_when_client_flag_is_enabled(self):
        order_id = "627-article-change-flag"
        base_card = {
            "id": "card-a",
            "article": "SKU-A",
            "product_name": "Source item",
            "result_article": "SKU-B",
            "result_product_name": "Target item",
            "result_barcode": "BAR-B",
            "rows": [
                {
                    "article": "SKU-A",
                    "size": "42",
                    "barcode": "BAR-A",
                    "qty": "10",
                    "result_article": "SKU-B",
                    "result_product_name": "Target item",
                    "result_barcode": "BAR-B",
                }
            ],
        }
        request = self.request_factory.get(
            f"/orders/processing/{order_id}/card/card-a/labels/"
        )
        request.user = self.user

        ordinary_payload = {"cards": [base_card]}
        ordinary_card_ctx = ProcessingWorkflowService.build_processing_card_page_context(
            order_id=order_id,
            card_id="card-a",
            request=request,
            payload=ordinary_payload,
            agency=self.agency,
        )
        ordinary_label_ctx = ProcessingWorkflowService.build_processing_label_print_page_context(
            ctx=ordinary_card_ctx,
            order_id=order_id,
            card_id="card-a",
            request=request,
            payload=ordinary_payload,
            agency=self.agency,
        )

        self.assertFalse(ordinary_label_ctx["article_change_active"])
        self.assertEqual(ordinary_label_ctx["label_sample"]["article"], "SKU-A")
        self.assertEqual(ordinary_label_ctx["label_sample_barcode"], "BAR-A")

        relabel_payload = {
            "article_change_needed": "Да",
            "cards": [base_card],
        }
        relabel_card_ctx = ProcessingWorkflowService.build_processing_card_page_context(
            order_id=order_id,
            card_id="card-a",
            request=request,
            payload=relabel_payload,
            agency=self.agency,
        )
        relabel_label_ctx = ProcessingWorkflowService.build_processing_label_print_page_context(
            ctx=relabel_card_ctx,
            order_id=order_id,
            card_id="card-a",
            request=request,
            payload=relabel_payload,
            agency=self.agency,
        )

        self.assertTrue(relabel_label_ctx["article_change_active"])
        self.assertEqual(relabel_label_ctx["label_sample"]["article"], "SKU-B")
        self.assertEqual(relabel_label_ctx["label_sample_barcode"], "BAR-B")
        self.assertEqual(relabel_label_ctx["article_change_source_article"], "SKU-A")
        self.assertEqual(relabel_label_ctx["article_change_target_article"], "SKU-B")

    def test_build_processing_detail_page_context_prefers_warehouse_status_label(self):
        order_id = "628"
        entry = OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="processing",
            action="status",
            agency=self.agency,
            payload={
                "status": "processing_head",
                "status_label": "Передано в обработку",
                "article": "SKU-PROC",
                "product_name": "Товар обработки",
            },
        )
        processing_location = WarehouseWritePathService.ensure_location(
            warehouse_code="MSK",
            zone_code="OBR",
        )
        WarehouseStockSnapshot.objects.create(
            agency=self.agency,
            source_context_type="legacy_stock",
            source_context_id=f"legacy-{order_id}",
            sku_code="SKU-PROC",
            name="Processing Item",
            size="42",
            barcode="BC-PROC-42",
            goods_type="gv",
            qty=10,
            available_qty=0,
            processing_reserved_qty=10,
            container_code=f"PAL-{order_id}",
            location=processing_location,
            zone_code=processing_location.zone_code,
            zone_kind=processing_location.zone_kind,
            warehouse_state_code="processing_in_progress",
        )
        WarehouseReserve.objects.create(
            agency=self.agency,
            reserve_type=WarehouseReserve.TYPE_PROCESSING,
            context_type="processing",
            context_id=order_id,
            sku_code="SKU-PROC",
            size="42",
            barcode="BC-PROC-42",
            goods_type="gv",
            qty_reserved=10,
            status=WarehouseReserve.STATUS_ACTIVE,
            source_document_type="processing_order",
            source_document_id=order_id,
            created_by=self.user,
        )
        request = self.request_factory.get(f"/orders/processing/{order_id}/")
        request.user = self.user
        base_ctx = {"agency": self.agency, "client_view": False}

        context = ProcessingWorkflowService.build_processing_detail_page_context(
            ctx=base_ctx,
            order_id=order_id,
            entries_list=[entry],
            request=request,
            payload_from_entries=lambda entries: entries[-1].payload or {},
        )

        self.assertEqual(context["status_label"], "Товар в обработке")
        self.assertIn("/orders/processing/628/work/", context["processing_work_url"])

    def test_handle_processing_detail_action_take_processing_redirects_to_work(self):
        order_id = "629"
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="processing",
            action="status",
            agency=self.agency,
            payload={
                "status": "processing_head",
                "status_label": "Передано в обработку",
                "article": "SKU-PROC",
            },
        )
        request = self.request_factory.post(
            f"/orders/processing/{order_id}/",
            data={"action": "take_processing"},
        )
        request.user = self.user

        result = ProcessingWorkflowService.handle_processing_detail_action(
            order_id=order_id,
            request=request,
            order_type="processing",
            payload_from_entries=lambda entries: entries[-1].payload or {},
        )

        self.assertEqual(result.status, "ok")
        self.assertEqual(result.redirect_to, f"/orders/processing/{order_id}/work/")

    def test_take_processing_rejects_invalid_stage_before_warehouse_command(self):
        order_id = "629-STAGE"
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="processing",
            action="status",
            agency=self.agency,
            payload={
                "processing_stage": PROCESSING_STAGE_AWAITING_APPROVAL,
                "status": "sent_unconfirmed",
                "status_label": "Ждет подтверждения",
                "article": "SKU-PROC",
            },
        )
        request = self.request_factory.post(
            f"/orders/processing/{order_id}/",
            data={"action": "take_processing"},
        )
        request.user = self.user

        with mock.patch(
            "processing_app.web_ui.WarehouseCommandService.take_processing"
        ) as take_processing:
            result = ProcessingWorkflowService.handle_processing_detail_action(
                order_id=order_id,
                request=request,
                order_type="processing",
                payload_from_entries=lambda entries: entries[-1].payload or {},
            )

        self.assertEqual(result.status, "denied")
        take_processing.assert_not_called()
        self.assertFalse(
            OrderAuditEntry.objects.filter(
                order_id=order_id,
                order_type="processing",
                payload__processing_stage=PROCESSING_STAGE_TAKEN_INTO_PROCESSING,
            ).exists()
        )

    def test_handle_processing_detail_action_take_processing_uses_merged_payload_and_skips_duplicate_status(self):
        order_id = "629-MERGED"
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="processing",
            action="status",
            agency=self.agency,
            description="Заявка на обработку принята в работу",
            payload={
                "status": "processing_in_work",
                "status_label": "Взята в работу",
                "article": "SKU-PROC",
                "work_started_at": "2026-05-05T13:20:00+03:00",
            },
        )
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="processing",
            action="comment",
            agency=self.agency,
            description="Промежуточный комментарий",
            payload={"comment": "Промежуточный комментарий"},
        )
        request = self.request_factory.post(
            f"/orders/processing/{order_id}/",
            data={"action": "take_processing"},
        )
        request.user = self.user

        result = ProcessingWorkflowService.handle_processing_detail_action(
            order_id=order_id,
            request=request,
            order_type="processing",
            payload_from_entries=_latest_payload_from_entries,
        )

        self.assertEqual(result.status, "already_in_progress")
        self.assertEqual(result.redirect_to, f"/orders/processing/{order_id}/work/")
        self.assertEqual(
            OrderAuditEntry.objects.filter(
                order_id=order_id,
                order_type="processing",
                action="status",
                payload__status="processing_in_work",
            ).count(),
            1,
        )

    def test_build_processing_detail_page_context_hides_manager_actions_when_warehouse_already_started(self):
        order_id = "629-WH"
        entry = OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="processing",
            action="status",
            agency=self.agency,
            payload={
                "status": "submitted",
                "status_label": "На подтверждении",
                "article": "SKU-PROC",
            },
        )
        self._create_processing_snapshot(order_id=order_id, state_code="processing_in_progress")
        manager_user = get_user_model().objects.create_user(username="proc_manager_ctx", password="x")
        Employee.objects.create(
            full_name="Processing Manager",
            user=manager_user,
            role="manager",
            is_active=True,
        )
        request = self.request_factory.get(f"/orders/processing/{order_id}/")
        request.user = manager_user
        base_ctx = {"agency": self.agency, "client_view": False}

        context = ProcessingWorkflowService.build_processing_detail_page_context(
            ctx=base_ctx,
            order_id=order_id,
            entries_list=[entry],
            request=request,
            payload_from_entries=lambda entries: entries[-1].payload or {},
        )

        self.assertEqual(context["status_label"], "Товар в обработке")
        self.assertFalse(context["can_approve_processing"])
        self.assertFalse(context["can_edit_processing"])

    def test_build_processing_detail_page_context_keeps_manager_actions_for_reserved_only_order(self):
        order_id = "629-RESERVE"
        entry = OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="processing",
            action="status",
            agency=self.agency,
            payload={
                "status": "submitted",
                "status_label": "На подтверждении",
                "article": "SKU-PROC",
            },
        )
        self._create_processing_snapshot(order_id=order_id, state_code="reserved_for_processing")
        manager_user = get_user_model().objects.create_user(username="proc_manager_reserved", password="x")
        Employee.objects.create(
            full_name="Processing Manager Reserved",
            user=manager_user,
            role="manager",
            is_active=True,
        )
        request = self.request_factory.get(f"/orders/processing/{order_id}/")
        request.user = manager_user
        base_ctx = {"agency": self.agency, "client_view": False}

        context = ProcessingWorkflowService.build_processing_detail_page_context(
            ctx=base_ctx,
            order_id=order_id,
            entries_list=[entry],
            request=request,
            payload_from_entries=lambda entries: entries[-1].payload or {},
        )

        self.assertEqual(context["status_label"], "Ждет подтверждения")
        self.assertTrue(context["can_approve_processing"])
        self.assertTrue(context["can_edit_processing"])
        self.assertTrue(context["can_cancel_order"])

    def test_build_processing_detail_page_context_requires_client_request_after_submission(self):
        order_id = "629-CLIENT-CANCEL-CTX"
        entry = OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="processing",
            action="status",
            agency=self.agency,
            payload={
                "status": "sent_unconfirmed",
                "submit_action": "submitted",
                "status_label": "Ждет подтверждения",
                "processing_stage": "awaiting_approval",
                "processing_stage_label": "Ждет подтверждения",
                "article": "SKU-PROC",
            },
        )
        request = self.request_factory.get(f"/orders/processing/{order_id}/?client={self.agency.id}")
        request.user = self.user
        request._client_agency = self.agency
        base_ctx = {"agency": self.agency, "client_view": True}

        context = ProcessingWorkflowService.build_processing_detail_page_context(
            ctx=base_ctx,
            order_id=order_id,
            entries_list=[entry],
            request=request,
            payload_from_entries=lambda entries: entries[-1].payload or {},
        )

        self.assertFalse(context["can_edit_processing"])
        self.assertFalse(context["can_cancel_order"])
        self.assertTrue(context["can_request_processing_change"])
        self.assertTrue(context["can_request_processing_cancel"])
        self.assertEqual(context["processing_client_card"]["status_title"], "Ожидает проверки менеджером")
        self.assertEqual(context["processing_client_card"]["steps"][1]["state"], "current")

    def test_build_processing_detail_page_context_allows_manager_cancel_in_client_view(self):
        order_id = "629-MANAGER-CLIENT-VIEW-CANCEL-CTX"
        entry = OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="processing",
            action="status",
            agency=self.agency,
            payload={
                "status": "sent_unconfirmed",
                "submit_action": "submitted",
                "status_label": "Ждет подтверждения",
                "processing_stage": "awaiting_approval",
                "processing_stage_label": "Ждет подтверждения",
                "article": "SKU-PROC",
            },
        )
        manager_user = get_user_model().objects.create_user(
            username="proc_manager_client_view_ctx",
            password="x",
        )
        Employee.objects.create(
            full_name="Processing Manager Client View",
            user=manager_user,
            role="manager",
            is_active=True,
        )
        request = self.request_factory.get(
            f"/orders/processing/{order_id}/?client={self.agency.id}"
        )
        request.user = manager_user
        request._client_agency = self.agency
        base_ctx = {"agency": self.agency, "client_view": True}

        context = ProcessingWorkflowService.build_processing_detail_page_context(
            ctx=base_ctx,
            order_id=order_id,
            entries_list=[entry],
            request=request,
            payload_from_entries=lambda entries: entries[-1].payload or {},
        )

        self.assertTrue(context["can_cancel_order"])
        self.assertFalse(context["can_request_processing_cancel"])

    def test_build_processing_detail_page_context_shows_request_actions_after_manager_approval(self):
        order_id = "629-CLIENT-REQUEST-CTX"
        entry = OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="processing",
            action="status",
            agency=self.agency,
            payload={
                "status": "processing_head",
                "status_label": "утверждено менеджером",
                "processing_stage": "manager_approved",
                "processing_stage_label": "утверждено менеджером",
                "product_name": "Товар для обработки",
                "marketplace": "WB",
                "stock_rows": [
                    {
                        "article": "SKU-PROC",
                        "name": "Товар для обработки",
                        "barcode": "460000000001",
                        "size": "0",
                        "qty": "15",
                    }
                ],
            },
        )
        request = self.request_factory.get(f"/orders/processing/{order_id}/?client={self.agency.id}")
        request.user = self.user
        request._client_agency = self.agency
        base_ctx = {"agency": self.agency, "client_view": True}

        context = ProcessingWorkflowService.build_processing_detail_page_context(
            ctx=base_ctx,
            order_id=order_id,
            entries_list=[entry],
            request=request,
            payload_from_entries=lambda entries: entries[-1].payload or {},
        )

        self.assertFalse(context["can_edit_processing"])
        self.assertFalse(context["can_cancel_order"])
        self.assertTrue(context["can_request_processing_change"])
        self.assertTrue(context["can_request_processing_cancel"])
        self.assertEqual(context["processing_client_card"]["status_title"], "Подтверждена")
        self.assertEqual(context["processing_composition_rows"][0]["size_display"], "Не указан")
        self.assertEqual(context["processing_composition_rows"][0]["qty_display"], "15 шт.")

    def test_build_processing_detail_page_context_exposes_completed_result_for_manager(self):
        order_id = "629-MANAGER-RESULT"
        manager_user = get_user_model().objects.create_user(username="processing_result_manager", password="x")
        Employee.objects.create(
            full_name="Processing Result Manager",
            user=manager_user,
            role="manager",
            is_active=True,
        )
        payload = {
            "status": "done",
            "processing_stage": "done",
            "cards": [
                {
                    "id": "card-a",
                    "article": "SKU-A",
                    "result_article": "SKU-B",
                    "product_name": "Товар",
                    "rows": [{"article": "SKU-A", "result_article": "SKU-B", "qty": 10}],
                }
            ],
            "processing_results": [
                {
                    "card_id": "card-a",
                    "source_article": "SKU-A",
                    "article": "SKU-B",
                    "barcode": "BAR-B",
                    "product_name": "Товар после обработки",
                    "processed": "10",
                    "tags_replaced": "10",
                }
            ],
            "act_state": "closed",
            "act_items": [
                {"sku_code": "SKU-B", "name": "Товар после обработки", "actual_qty": 10, "box_qty": 1, "pallet_qty": 1}
            ],
            "act_boxes": [
                {"code": "BOX-RESULT-1", "sealed": True, "items": [{"sku_code": "SKU-B", "qty": 10}]}
            ],
            "act_pallets": [
                {"code": "PAL-RESULT-1", "boxes": ["BOX-RESULT-1"], "items": []}
            ],
        }
        entry = OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="processing",
            action="status",
            agency=self.agency,
            payload=payload,
        )
        request = self.request_factory.get(f"/orders/processing/{order_id}/")
        request.user = manager_user

        context = ProcessingWorkflowService.build_processing_detail_page_context(
            ctx={"agency": self.agency, "client_view": False},
            order_id=order_id,
            entries_list=[entry],
            request=request,
            payload_from_entries=lambda entries: entries[-1].payload or {},
        )

        result = context["processing_manager_result"]
        self.assertTrue(context["show_processing_manager_result"])
        self.assertEqual(result["processed_qty"], 10)
        self.assertEqual(result["boxed_qty"], 10)
        self.assertEqual(result["box_count"], 1)
        self.assertEqual(result["pallet_count"], 1)
        self.assertEqual(result["result_rows"][0]["source_article"], "SKU-A")
        self.assertIn("Замена артикула: SKU-A → SKU-B", result["result_rows"][0]["operations_text"])
        self.assertEqual(result["box_rows"][0]["pallet_code"], "PAL-RESULT-1")

    def test_build_processing_detail_page_context_hides_manager_result_in_client_view(self):
        order_id = "629-CLIENT-RESULT-HIDDEN"
        payload = {
            "status": "done",
            "processing_stage": "done",
            "processing_results": [{"article": "SKU-B", "processed": "1"}],
        }
        entry = OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="processing",
            action="status",
            agency=self.agency,
            payload=payload,
        )
        request = self.request_factory.get(f"/orders/processing/{order_id}/?client={self.agency.id}")
        request.user = self.user

        context = ProcessingWorkflowService.build_processing_detail_page_context(
            ctx={"agency": self.agency, "client_view": True},
            order_id=order_id,
            entries_list=[entry],
            request=request,
            payload_from_entries=lambda entries: entries[-1].payload or {},
        )

        self.assertFalse(context["show_processing_manager_result"])

    def test_processing_detail_context_never_exposes_receiving_act(self):
        order_id = "27"
        entries = self._create_ready_processing_entries(order_id)
        request = self.request_factory.get(f"/orders/processing/{order_id}/?client={self.agency.id}")
        request.user = self.user
        request._client_agency = self.agency

        context = ProcessingWorkflowService.build_processing_detail_page_context(
            ctx={
                "agency": self.agency,
                "client_view": True,
                "can_print_receiving_act": True,
                "has_receiving_act": True,
            },
            order_id=order_id,
            entries_list=entries,
            request=request,
            payload_from_entries=lambda rows: rows[-1].payload or {},
        )

        self.assertFalse(context["can_print_receiving_act"])
        self.assertFalse(context["has_receiving_act"])

    def test_take_processing_redirects_to_work_when_payload_is_stale_but_warehouse_in_progress(self):
        order_id = "629-WH-TAKE"
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="processing",
            action="status",
            agency=self.agency,
            payload={
                "status": "submitted",
                "status_label": "На подтверждении",
                "article": "SKU-PROC",
            },
        )
        self._create_processing_snapshot(order_id=order_id, state_code="processing_in_progress")

        request = self.request_factory.post(
            f"/orders/processing/{order_id}/",
            data={"action": "take_processing"},
        )
        request.user = self.user

        result = ProcessingWorkflowService.handle_processing_detail_action(
            order_id=order_id,
            request=request,
            order_type="processing",
            payload_from_entries=lambda entries: entries[-1].payload or {},
        )

        self.assertEqual(result.status, "already_in_progress")
        self.assertEqual(result.redirect_to, f"/orders/processing/{order_id}/work/")

    def test_approve_processing_is_noop_when_warehouse_already_started(self):
        order_id = "629-WH-APPROVE"
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="processing",
            action="status",
            agency=self.agency,
            payload={
                "status": "submitted",
                "status_label": "На подтверждении",
                "article": "SKU-PROC",
            },
        )
        self._create_processing_snapshot(order_id=order_id, state_code="processing_in_progress")
        manager_user = get_user_model().objects.create_user(username="proc_manager_approve", password="x")
        Employee.objects.create(
            full_name="Processing Approver",
            user=manager_user,
            role="manager",
            is_active=True,
        )
        request = self.request_factory.post(
            f"/orders/processing/{order_id}/",
            data={"action": "approve_processing"},
        )
        request.user = manager_user

        result = ProcessingWorkflowService.handle_processing_detail_action(
            order_id=order_id,
            request=request,
            order_type="processing",
            payload_from_entries=lambda entries: entries[-1].payload or {},
        )

        self.assertEqual(result.status, "done")
        self.assertEqual(result.redirect_to, f"/orders/processing/{order_id}/")
        self.assertEqual(
            OrderAuditEntry.objects.filter(order_id=order_id, order_type="processing", payload__status="processing_head").count(),
            0,
        )

    def test_approve_processing_updates_status_when_only_reserved_for_processing(self):
        order_id = "629-WH-RESERVED-APPROVE"
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="processing",
            action="status",
            agency=self.agency,
            payload={
                "status": "submitted",
                "status_label": "На подтверждении",
                "article": "SKU-PROC",
            },
        )
        self._create_processing_snapshot(order_id=order_id, state_code="reserved_for_processing")
        manager_user = get_user_model().objects.create_user(username="proc_manager_reserved_approve", password="x")
        Employee.objects.create(
            full_name="Processing Reserved Approver",
            user=manager_user,
            role="manager",
            is_active=True,
        )
        request = self.request_factory.post(
            f"/orders/processing/{order_id}/",
            data={"action": "approve_processing"},
        )
        request.user = manager_user

        result = ProcessingWorkflowService.handle_processing_detail_action(
            order_id=order_id,
            request=request,
            order_type="processing",
            payload_from_entries=lambda entries: entries[-1].payload or {},
        )

        self.assertEqual(result.status, "ok")
        self.assertEqual(result.redirect_to, f"/orders/processing/{order_id}/")
        self.assertEqual(
            OrderAuditEntry.objects.filter(order_id=order_id, order_type="processing", payload__status="processing_head").count(),
            1,
        )

    def test_approve_processing_rejects_same_article_only_for_client_article_change(self):
        order_id = "629-ARTICLE-CHANGE-SAME"
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="processing",
            action="status",
            agency=self.agency,
            payload={
                "status": "sent_unconfirmed",
                "status_label": "Ждет подтверждения",
                "processing_stage": PROCESSING_STAGE_AWAITING_APPROVAL,
                "processing_stage_label": "Ждет подтверждения",
                "article_change_needed": "Да",
                "cards": [
                    {
                        "id": "card-a",
                        "article": "SKU-A",
                        "result_article": "SKU-A",
                        "result_barcode": "BAR-A",
                        "rows": [
                            {
                                "article": "SKU-A",
                                "size": "42",
                                "barcode": "BAR-A",
                                "result_article": "SKU-A",
                                "result_barcode": "BAR-A",
                            }
                        ],
                    }
                ],
            },
        )
        manager_user = get_user_model().objects.create_user(
            username="proc_manager_article_change_same",
            password="x",
        )
        Employee.objects.create(
            full_name="Processing Article Change Manager",
            user=manager_user,
            role="manager",
            is_active=True,
        )
        request = self.request_factory.post(
            f"/orders/processing/{order_id}/",
            data={"action": "approve_processing"},
        )
        request.user = manager_user

        result = ProcessingWorkflowService.handle_processing_detail_action(
            order_id=order_id,
            request=request,
            order_type="processing",
            payload_from_entries=lambda entries: entries[-1].payload or {},
        )

        self.assertEqual(result.status, "invalid_article_change")
        self.assertIn("должен отличаться", result.error_message)
        self.assertFalse(
            OrderAuditEntry.objects.filter(
                order_id=order_id,
                order_type="processing",
                payload__processing_stage=PROCESSING_STAGE_MANAGER_APPROVED,
            ).exists()
        )

    def test_approve_processing_ignores_article_pair_without_client_article_change_flag(self):
        order_id = "629-ORDINARY-SAME-ARTICLE"
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="processing",
            action="status",
            agency=self.agency,
            payload={
                "status": "sent_unconfirmed",
                "status_label": "Ждет подтверждения",
                "processing_stage": PROCESSING_STAGE_AWAITING_APPROVAL,
                "processing_stage_label": "Ждет подтверждения",
                "cards": [
                    {
                        "id": "card-a",
                        "article": "SKU-A",
                        "result_article": "SKU-A",
                        "result_barcode": "BAR-A",
                        "rows": [
                            {
                                "article": "SKU-A",
                                "size": "42",
                                "barcode": "BAR-A",
                                "result_article": "SKU-A",
                                "result_barcode": "BAR-A",
                            }
                        ],
                    }
                ],
            },
        )
        manager_user = get_user_model().objects.create_user(
            username="proc_manager_ordinary_same",
            password="x",
        )
        Employee.objects.create(
            full_name="Processing Ordinary Manager",
            user=manager_user,
            role="manager",
            is_active=True,
        )
        request = self.request_factory.post(
            f"/orders/processing/{order_id}/",
            data={"action": "approve_processing"},
        )
        request.user = manager_user

        result = ProcessingWorkflowService.handle_processing_detail_action(
            order_id=order_id,
            request=request,
            order_type="processing",
            payload_from_entries=lambda entries: entries[-1].payload or {},
        )

        self.assertNotEqual(result.status, "invalid_article_change")

    def test_cancel_order_marks_processing_cancelled_and_releases_reserve(self):
        order_id = "629-CANCEL-WAITING"
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="processing",
            action="status",
            agency=self.agency,
            payload={
                "status": "sent_unconfirmed",
                "submit_action": "submitted",
                "status_label": "Ждет подтверждения",
                "processing_stage": "awaiting_approval",
                "processing_stage_label": "Ждет подтверждения",
                "article": "SKU-CANCEL",
            },
        )
        WarehouseReserve.objects.create(
            agency=self.agency,
            reserve_type=WarehouseReserve.TYPE_PROCESSING,
            context_type="processing",
            context_id=order_id,
            sku_code="SKU-CANCEL",
            size="42",
            goods_type="gv",
            qty_reserved=120,
            status=WarehouseReserve.STATUS_ACTIVE,
        )
        Task.objects.create(
            title=f"Processing order #{order_id}",
            route=f"/orders/processing/{order_id}/",
            assigned_to=Employee.objects.get(user=self.user),
        )
        manager_user = get_user_model().objects.create_user(username="proc_manager_cancel", password="x")
        Employee.objects.create(
            full_name="Processing Cancel Manager",
            user=manager_user,
            role="manager",
            is_active=True,
        )
        request = self.request_factory.post(
            f"/orders/processing/{order_id}/",
            data={"action": "cancel_order", "cancel_reason": "Клиент отказался"},
        )
        request.user = manager_user

        result = ProcessingWorkflowService.handle_processing_detail_action(
            order_id=order_id,
            request=request,
            order_type="processing",
            payload_from_entries=lambda entries: entries[-1].payload or {},
        )

        self.assertEqual(result.status, "cancelled")
        self.assertEqual(result.redirect_to, f"/orders/processing/{order_id}/")
        latest = (
            OrderAuditEntry.objects.filter(order_id=order_id, order_type="processing")
            .order_by("-created_at")
            .first()
        )
        self.assertIsNotNone(latest)
        payload = latest.payload or {}
        self.assertEqual(payload.get("status"), "cancelled")
        self.assertEqual(payload.get("status_label"), "Отменена")
        self.assertEqual(payload.get("cancel_reason"), "Клиент отказался")
        self.assertEqual(payload.get("processing_stage"), "cancelled")
        reserve = WarehouseReserve.objects.get(
            agency=self.agency,
            reserve_type=WarehouseReserve.TYPE_PROCESSING,
            context_type="processing",
            context_id=order_id,
        )
        self.assertEqual(reserve.status, WarehouseReserve.STATUS_RELEASED)
        self.assertFalse(
            Task.objects.filter(route=f"/orders/processing/{order_id}/").exclude(status="done").exists()
        )

    def test_manager_cancel_from_client_view_is_classified_as_manager(self):
        order_id = "629-MANAGER-CLIENT-VIEW-CANCEL"
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="processing",
            action="status",
            agency=self.agency,
            payload={
                "status": "sent_unconfirmed",
                "submit_action": "submitted",
                "status_label": "Ждет подтверждения",
                "processing_stage": "awaiting_approval",
                "processing_stage_label": "Ждет подтверждения",
                "article": "SKU-CANCEL",
            },
        )
        manager_user = get_user_model().objects.create_user(
            username="proc_manager_client_view_cancel",
            password="x",
        )
        Employee.objects.create(
            full_name="Processing Manager Client View Cancel",
            user=manager_user,
            role="manager",
            is_active=True,
        )
        request = self.request_factory.post(
            f"/orders/processing/{order_id}/?client={self.agency.id}",
            data={"action": "cancel_order", "cancel_reason": "Клиент попросил отменить"},
        )
        request.user = manager_user
        request._client_agency = self.agency

        result = ProcessingWorkflowService.handle_processing_detail_action(
            order_id=order_id,
            request=request,
            order_type="processing",
            payload_from_entries=lambda entries: entries[-1].payload or {},
        )

        self.assertEqual(result.status, "cancelled")
        latest = (
            OrderAuditEntry.objects.filter(order_id=order_id, order_type="processing")
            .order_by("-created_at", "-id")
            .first()
        )
        self.assertEqual((latest.payload or {}).get("status"), "cancelled")
        self.assertEqual((latest.payload or {}).get("cancelled_by"), "manager")
        self.assertIn("Заявка отменена менеджером", latest.description)

    def test_client_cannot_directly_cancel_submitted_processing_order(self):
        order_id = "629-CLIENT-CANCEL-WAITING"
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="processing",
            action="status",
            agency=self.agency,
            payload={
                "status": "sent_unconfirmed",
                "submit_action": "submitted",
                "status_label": "Ждет подтверждения",
                "processing_stage": "awaiting_approval",
                "processing_stage_label": "Ждет подтверждения",
                "article": "SKU-CANCEL",
            },
        )
        request = self.request_factory.post(
            f"/orders/processing/{order_id}/?client={self.agency.id}",
            data={"action": "cancel_order", "cancel_reason": "Неактуальная заявка"},
        )
        request.user = self.user
        request._client_agency = self.agency

        result = ProcessingWorkflowService.handle_processing_detail_action(
            order_id=order_id,
            request=request,
            order_type="processing",
            payload_from_entries=lambda entries: entries[-1].payload or {},
        )

        self.assertEqual(result.status, "forbidden")
        self.assertEqual(result.redirect_to, f"/orders/processing/{order_id}/?client={self.agency.id}")
        latest = (
            OrderAuditEntry.objects.filter(order_id=order_id, order_type="processing")
            .order_by("-created_at")
            .first()
        )
        payload = latest.payload or {}
        self.assertEqual(payload.get("status"), "sent_unconfirmed")
        self.assertIsNone(payload.get("cancelled_by"))
        self.assertIsNone(payload.get("cancel_reason"))

    def test_processing_marking_availability_counts_reserved_and_free_codes(self):
        manager_user = get_user_model().objects.create_user(username="proc_marking_manager", password="x")
        Employee.objects.create(
            full_name="Processing Marking Manager",
            user=manager_user,
            role="manager",
            is_active=True,
        )
        request = self.request_factory.post("/orders/processing/marking-availability/")
        request.user = manager_user
        MarkingCode.objects.create(
            agency=self.agency,
            code="MC-1",
            barcode="BAR-1",
            order_type="processing",
            order_id="ORD-1",
        )
        MarkingCode.objects.create(
            agency=self.agency,
            code="MC-2",
            barcode="BAR-1",
            order_type="processing",
            order_id="",
        )

        result = ProcessingWorkflowService.processing_marking_availability(
            request=request,
            data={
                "order_id": "ORD-1",
                "agency_id": self.agency.id,
                "items": [{"barcode": "BAR-1", "qty": 3}, {"qty": 2}],
            },
        )

        self.assertEqual(result.status, "ok")
        self.assertEqual(result.payload["required"], 3)
        self.assertEqual(result.payload["available"], 2)
        self.assertEqual(result.payload["free"], 1)
        self.assertEqual(result.payload["missing"], 1)
        self.assertEqual(result.payload["missing_barcodes"], 2)

    def test_processing_marking_import_returns_success_payload(self):
        manager_user = get_user_model().objects.create_user(username="proc_import_manager", password="x")
        Employee.objects.create(
            full_name="Processing Import Manager",
            user=manager_user,
            role="manager",
            is_active=True,
        )
        request = self.request_factory.post(
            "/orders/processing/marking-import/",
            data={"agency_id": str(self.agency.id)},
        )
        request.user = manager_user
        upload = SimpleUploadedFile("cz.xlsx", b"fake-xlsx", content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

        with mock.patch("processing_app.views._import_marking_codes", return_value=(True, {"created": 2, "updated": 1})):
            result = ProcessingWorkflowService.processing_marking_import(
                request=request,
                cz_file=upload,
                cards_payload=[{"id": "card-a"}],
                order_id="ORD-2",
            )

        self.assertEqual(result.status, "ok")
        self.assertTrue(result.payload["ok"])
        self.assertEqual(result.payload["created"], 2)

    def test_enqueue_processing_print_job_creates_job(self):
        request = self.request_factory.post("/orders/processing/print-jobs/enqueue/")
        request.user = self.user

        result = ProcessingWorkflowService.enqueue_processing_print_job(
            request=request,
            data={
                "order_id": "ORD-3",
                "card_id": "card-a",
                "article": "SKU-A",
                "barcode": "BAR-42",
                "size": "42",
                "printer_name": "Printer",
                "label_png_base64": "abc123",
            },
        )

        self.assertEqual(result.status, "ok")
        self.assertTrue(ProcessingPrintJob.objects.filter(pk=result.payload["job_id"]).exists())

    def test_enqueue_processing_print_job_rejects_article_a_for_client_article_change(self):
        order_id = "ORD-ARTICLE-CHANGE"
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="processing",
            action="status",
            agency=self.agency,
            payload={
                "article_change_needed": "Да",
                "cards": [
                    {
                        "id": "card-a",
                        "article": "SKU-A",
                        "result_article": "SKU-B",
                        "result_barcode": "BAR-B",
                        "rows": [
                            {
                                "article": "SKU-A",
                                "size": "42",
                                "barcode": "BAR-A",
                                "result_article": "SKU-B",
                                "result_barcode": "BAR-B",
                            }
                        ],
                    }
                ],
            },
        )
        request = self.request_factory.post("/orders/processing/print-jobs/enqueue/")
        request.user = self.user

        result = ProcessingWorkflowService.enqueue_processing_print_job(
            request=request,
            data={
                "order_id": order_id,
                "card_id": "card-a",
                "article": "SKU-A",
                "barcode": "BAR-A",
                "size": "42",
                "template_key": "item",
                "printer_name": "Printer",
                "label_png_base64": "abc123",
            },
        )

        self.assertEqual(result.status, "article_change_target_mismatch")
        self.assertEqual(result.http_status, 409)
        self.assertFalse(ProcessingPrintJob.objects.filter(order_id=order_id).exists())

    def test_enqueue_processing_print_job_accepts_only_article_b_for_client_article_change(self):
        order_id = "ORD-ARTICLE-CHANGE-B"
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="processing",
            action="status",
            agency=self.agency,
            payload={
                "article_change_needed": "Да",
                "cards": [
                    {
                        "id": "card-a",
                        "article": "SKU-A",
                        "result_article": "SKU-B",
                        "result_barcode": "BAR-B",
                        "rows": [
                            {
                                "article": "SKU-A",
                                "size": "42",
                                "barcode": "BAR-A",
                                "result_article": "SKU-B",
                                "result_barcode": "BAR-B",
                            }
                        ],
                    }
                ],
            },
        )
        request = self.request_factory.post("/orders/processing/print-jobs/enqueue/")
        request.user = self.user

        result = ProcessingWorkflowService.enqueue_processing_print_job(
            request=request,
            data={
                "order_id": order_id,
                "card_id": "card-a",
                "article": "SKU-B",
                "barcode": "BAR-B",
                "size": "42",
                "template_key": "item",
                "printer_name": "Printer",
                "label_png_base64": "abc123",
            },
        )

        self.assertEqual(result.status, "ok")
        job = ProcessingPrintJob.objects.get(pk=result.payload["job_id"])
        self.assertEqual(job.article, "SKU-B")
        self.assertEqual(job.barcode, "BAR-B")

    def test_enqueue_processing_print_job_does_not_apply_article_change_without_client_flag(self):
        order_id = "ORD-ORDINARY-PROCESSING"
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="processing",
            action="status",
            agency=self.agency,
            payload={
                "cards": [
                    {
                        "id": "card-a",
                        "article": "SKU-A",
                        "result_article": "SKU-B",
                        "result_barcode": "BAR-B",
                        "rows": [
                            {
                                "article": "SKU-A",
                                "size": "42",
                                "barcode": "BAR-A",
                                "result_article": "SKU-B",
                                "result_barcode": "BAR-B",
                            }
                        ],
                    }
                ],
            },
        )
        request = self.request_factory.post("/orders/processing/print-jobs/enqueue/")
        request.user = self.user

        result = ProcessingWorkflowService.enqueue_processing_print_job(
            request=request,
            data={
                "order_id": order_id,
                "card_id": "card-a",
                "article": "SKU-A",
                "barcode": "BAR-A",
                "size": "42",
                "template_key": "item",
                "printer_name": "Printer",
                "label_png_base64": "abc123",
            },
        )

        self.assertEqual(result.status, "ok")
        job = ProcessingPrintJob.objects.get(pk=result.payload["job_id"])
        self.assertEqual(job.article, "SKU-A")
        self.assertEqual(job.barcode, "BAR-A")

    def test_enqueue_processing_print_job_creates_single_batch_job(self):
        request = self.request_factory.post("/orders/processing/print-jobs/enqueue/")
        request.user = self.user

        result = ProcessingWorkflowService.enqueue_processing_print_job(
            request=request,
            data={
                "order_id": "ORD-BATCH",
                "barcode": "BOX-1",
                "printer_name": "Printer",
                "label_png_base64": "img1",
                "label_png_base64_list": ["img1", "img2"],
                "agent_id": "agent-1",
            },
        )

        self.assertEqual(result.status, "ok")
        self.assertEqual(result.payload["job_ids"], [result.payload["job_id"]])
        self.assertEqual(result.payload["label_count"], 2)
        self.assertEqual(result.payload["queued_count"], 2)
        job = ProcessingPrintJob.objects.get(pk=result.payload["job_id"])
        self.assertEqual(job.label_png_base64, "img1")
        self.assertEqual(job.label_png_base64_list, ["img1", "img2"])
        self.assertEqual(job.copies_count, 2)

    def test_enqueue_processing_print_job_notifies_realtime_after_commit(self):
        request = self.request_factory.post("/orders/processing/print-jobs/enqueue/")
        request.user = self.user

        with self.settings(FULLBOX_REALTIME_ENABLED=True):
            with mock.patch("agent.realtime._group_send") as group_send:
                with self.captureOnCommitCallbacks(execute=True):
                    result = ProcessingWorkflowService.enqueue_processing_print_job(
                        request=request,
                        data={
                            "order_id": "ORD-3",
                            "card_id": "card-a",
                            "article": "SKU-A",
                            "barcode": "BAR-42",
                            "size": "42",
                            "printer_name": "Printer",
                            "label_png_base64": "abc123",
                            "agent_id": "agent-1",
                        },
                    )

        self.assertEqual(result.status, "ok")
        group_send.assert_called_once()
        group, message = group_send.call_args.args
        self.assertEqual(group, "fullbox.agent.agent-1")
        self.assertEqual(message["type"], "print.job_available")
        self.assertEqual(message["job_id"], result.payload["job_id"])

    def test_processing_print_jobs_next_moves_pending_job_to_printing(self):
        ProcessingPrintJob.objects.create(
            order_id="ORD-4",
            card_id="card-a",
            article="SKU-A",
            barcode="BAR-42",
            size="42",
            printer_name="Printer",
            label_png_base64="abc123",
            label_width_mm=58,
            label_height_mm=40,
            status=ProcessingPrintJob.STATUS_PENDING,
        )

        result = ProcessingWorkflowService.processing_print_jobs_next(agent_name="agent-1")

        self.assertEqual(result.status, "ok")
        self.assertTrue(result.payload["has_job"])
        job = ProcessingPrintJob.objects.get()
        self.assertEqual(job.status, ProcessingPrintJob.STATUS_PRINTING)
        self.assertEqual(job.agent, "agent-1")

    def test_processing_print_jobs_next_claims_legacy_id_for_same_desktop_host(self):
        from agent.models import DeviceAgent

        DeviceAgent.objects.create(
            agent_id="desktop:desktop-comp027-2",
            name="COMP027",
            host="COMP027",
        )
        legacy_job = ProcessingPrintJob.objects.create(
            order_id="ORD-LEGACY",
            barcode="BAR-LEGACY",
            printer_name="Printer",
            label_png_base64="abc123",
            status=ProcessingPrintJob.STATUS_PENDING,
            agent="desktop-comp027",
        )

        result = ProcessingWorkflowService.processing_print_jobs_next(
            agent_name="desktop:desktop-comp027-2"
        )

        self.assertEqual(result.status, "ok")
        legacy_job.refresh_from_db()
        self.assertEqual(legacy_job.status, ProcessingPrintJob.STATUS_PRINTING)
        self.assertEqual(legacy_job.agent, "desktop-comp027")

    def test_processing_print_jobs_next_empty_queue_returns_backoff_hint(self):
        result = ProcessingWorkflowService.processing_print_jobs_next(agent_name="agent-1")

        self.assertEqual(result.status, "empty")
        self.assertEqual(result.payload["retry_after_ms"], 1200)

    def test_processing_print_jobs_next_serializes_batch_labels(self):
        ProcessingPrintJob.objects.create(
            order_id="ORD-BATCH",
            barcode="BOX-1",
            printer_name="Printer",
            label_png_base64="img1",
            label_png_base64_list=["img1", "img2"],
            label_width_mm=60,
            label_height_mm=58,
            status=ProcessingPrintJob.STATUS_PENDING,
            agent="agent-1",
        )

        result = ProcessingWorkflowService.processing_print_jobs_next(agent_name="agent-1")

        self.assertEqual(result.status, "ok")
        job_payload = result.payload["job"]
        self.assertEqual(job_payload["labelPngBase64"], "img1")
        self.assertEqual(job_payload["labelPngBase64List"], ["img1", "img2"])
        self.assertEqual(job_payload["labelCount"], 2)

    def test_processing_print_jobs_next_respects_paused_queue_without_claiming_job(self):
        ProcessingPrintJob.objects.create(
            order_id="ORD-4",
            card_id="card-a",
            article="SKU-A",
            barcode="BAR-42",
            size="42",
            printer_name="Printer",
            label_png_base64="abc123",
            label_width_mm=58,
            label_height_mm=40,
            status=ProcessingPrintJob.STATUS_PENDING,
        )

        with TemporaryDirectory() as tmp_dir:
            status_path = Path(tmp_dir) / "print_agent_status.json"
            with mock.patch("labels.utils.print_agent_status_path", return_value=status_path):
                set_print_agent_pause(True, by="tester")
                result = ProcessingWorkflowService.processing_print_jobs_next(agent_name="agent-1")

        self.assertEqual(result.status, "paused")
        job = ProcessingPrintJob.objects.get()
        self.assertEqual(job.status, ProcessingPrintJob.STATUS_PENDING)
        self.assertEqual(job.agent, "")

    def test_processing_print_jobs_complete_updates_job_status(self):
        job = ProcessingPrintJob.objects.create(
            order_id="ORD-5",
            card_id="card-a",
            article="SKU-A",
            barcode="BAR-42",
            size="42",
            printer_name="Printer",
            label_png_base64="abc123",
            label_width_mm=58,
            label_height_mm=40,
            status=ProcessingPrintJob.STATUS_PRINTING,
        )

        result = ProcessingWorkflowService.processing_print_jobs_complete(
            data={"job_id": job.id, "status": "failed", "error": "boom"},
        )

        self.assertEqual(result.status, "ok")
        job.refresh_from_db()
        self.assertEqual(job.status, ProcessingPrintJob.STATUS_FAILED)
        self.assertEqual(job.error, "boom")

    def test_processing_print_jobs_reset_rejects_invalid_mode(self):
        request = self.request_factory.post("/orders/processing/print-jobs/reset/")
        request.user = self.user

        result = ProcessingWorkflowService.processing_print_jobs_reset(
            request=request,
            data={"mode": "weird"},
        )

        self.assertEqual(result.status, "invalid_mode")
        self.assertEqual(result.http_status, 400)

    def test_print_recover_endpoint_allows_any_employee_role(self):
        user = get_user_model().objects.create_user(username="print_recover_logistician", password="x")
        Employee.objects.create(
            full_name="Print Recover Logistician",
            user=user,
            role="logistician",
            is_active=True,
        )
        ProcessingPrintJob.objects.create(
            barcode="RECOVER-1",
            printer_name="Printer",
            label_png_base64="abc123",
            agent="agent-1",
            status=ProcessingPrintJob.STATUS_PENDING,
        )
        request = self.request_factory.post(
            "/orders/processing/print-jobs/recover/",
            data=json.dumps({"agent_id": "agent-1"}),
            content_type="application/json",
        )
        request.user = user

        response = processing_print_jobs_recover(request)

        self.assertEqual(response.status_code, 200)
        self.assertFalse(ProcessingPrintJob.objects.filter(agent="agent-1").exists())

    def test_build_processing_home_page_context_exposes_draft_payload(self):
        order_id = "630"
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="processing",
            action="create",
            agency=self.agency,
            payload={
                "status": "draft",
                "status_label": "Черновик",
                "submit_action": "draft",
                "product_name": "Draft product",
            },
        )
        request = self.request_factory.get(
            "/orders/processing/",
            data={"client": self.agency.id, "order": order_id},
        )
        request.user = self.user

        context = ProcessingWorkflowService.build_processing_home_page_context(
            request=request,
            submitted=False,
            draft_saved=False,
            error="",
        )

        self.assertEqual(context["draft_order_id"], order_id)
        self.assertEqual(context["status_label"], "Черновик")
        self.assertEqual(context["draft_payload"]["product_name"], "Draft product")

    def test_build_processing_home_page_context_prefers_warehouse_status_for_edit_order(self):
        order_id = "630-WH"
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="processing",
            action="status",
            agency=self.agency,
            payload={
                "status": "submitted",
                "status_label": "На подтверждении",
                "product_name": "Warehouse product",
            },
        )
        self._create_processing_snapshot(order_id=order_id, state_code="processing_in_progress")
        manager_user = get_user_model().objects.create_user(username="proc_home_manager", password="x")
        Employee.objects.create(
            full_name="Processing Home Manager",
            user=manager_user,
            role="manager",
            is_active=True,
        )
        request = self.request_factory.get(
            "/orders/processing/",
            data={"agency": self.agency.id, "order": order_id, "edit": "1"},
        )
        request.user = manager_user

        context = ProcessingWorkflowService.build_processing_home_page_context(
            request=request,
            submitted=False,
            draft_saved=False,
            error="",
        )

        self.assertEqual(context["edit_order_id"], order_id)
        self.assertEqual(context["status_label"], "Товар в обработке")

    def test_build_processing_directions_page_context_keeps_return_url(self):
        request = self.request_factory.get(
            "/orders/processing/directions/",
            data={"return": "/orders/processing/123/work/"},
        )
        request.user = self.user

        context = ProcessingWorkflowService.build_processing_directions_page_context(
            request=request,
        )

        self.assertEqual(context["return_url"], "/orders/processing/123/work/")
        self.assertEqual(context["return_url_json"], "\"/orders/processing/123/work/\"")

    def test_build_processing_stock_picker_page_context_uses_referer_order(self):
        order_id = "631"
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="processing",
            action="status",
            agency=self.agency,
            payload={"status": "processing_in_work", "status_label": "Взята в работу"},
        )
        create_warehouse_snapshot_row(
            agency=self.agency,
            order_type="receiving",
            order_id="500",
            sku="SKU-RES",
            name="Reserved item",
            size="42",
            goods_type="Не обработанный",
            qty=50,
            available_qty=0,
            processing_reserved_qty=50,
            pallet_code="PAL-RES-631",
        )
        WarehouseReserve.objects.create(
            agency=self.agency,
            reserve_type=WarehouseReserve.TYPE_PROCESSING,
            context_type="processing",
            context_id=order_id,
            sku_code="SKU-RES",
            size="42",
            goods_type="Не обработанный",
            qty_reserved=50,
            status=WarehouseReserve.STATUS_ACTIVE,
        )
        request = self.request_factory.get(
            "/orders/processing/stock/",
            data={"client": self.agency.id},
            HTTP_REFERER=f"http://testserver/orders/processing/{order_id}/work/",
        )
        request.user = self.user

        context = ProcessingWorkflowService.build_processing_stock_picker_page_context(
            request=request,
        )

        items = json.loads(context["inventory_items_json"] or "[]")
        qty = sum(int(item.get("qty") or 0) for item in items if (item.get("sku") or "") == "SKU-RES")
        self.assertEqual(qty, 50)
        self.assertEqual(context["return_url"], f"/orders/processing/?client={self.agency.id}")

    def test_delete_processing_draft_removes_entries_and_releases_marking_codes(self):
        order_id = "draft-delete-1"
        portal_user = get_user_model().objects.create_user(username="processing_portal_user", password="x")
        self.agency.portal_user = portal_user
        self.agency.save(update_fields=["portal_user"])
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="processing",
            action="create",
            agency=self.agency,
            payload={"status": "draft", "status_label": "Черновик", "submit_action": "draft"},
        )
        marking_code = MarkingCode.objects.create(
            agency=self.agency,
            code="CZ-DRAFT-1",
            barcode="BAR-DRAFT",
            order_type="processing",
            order_id=order_id,
        )
        request = self.request_factory.post(f"/orders/processing/drafts/{order_id}/delete/")
        request.user = portal_user

        response = ProcessingWorkflowService.delete_processing_draft(
            request=request,
            order_id=order_id,
        )

        self.assertEqual(response.status_code, 302)
        self.assertFalse(OrderAuditEntry.objects.filter(order_id=order_id, order_type="processing").exists())
        marking_code.refresh_from_db()
        self.assertEqual(marking_code.order_id, "")

    def test_submit_processing_autosaves_draft(self):
        request = self.request_factory.post(
            "/orders/processing/",
            data={
                "agency_id": str(self.agency.id),
                "submit_action": "draft",
                "draft_autosave": "1",
                "product_name": "Draft processing product",
            },
        )
        request.user = self.user

        response = ProcessingWorkflowService.submit_processing(request=request)

        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content.decode("utf-8"))
        self.assertTrue(data["ok"])
        self.assertTrue(str(data["draft_order_id"]).startswith("draft-"))
        latest = OrderAuditEntry.objects.filter(order_id=data["order_id"], order_type="processing").order_by("-id").first()
        self.assertIsNotNone(latest)
        self.assertEqual((latest.payload or {}).get("status"), "draft")

    def test_empty_legacy_draft_uses_current_client_unit_picker(self):
        enabled = ProcessingWorkflowService._client_processing_unit_picker_enabled(
            client_agency=self.agency,
            edit_order_id="",
            draft_order_id="draft-legacy-empty",
            draft_payload={
                "status": "draft",
                "stock_rows": [],
                "size_rows": [],
            },
        )

        self.assertTrue(enabled)

    def test_populated_legacy_draft_keeps_legacy_picker(self):
        for payload in (
            {"status": "draft", "stock_rows": [{"article": "SKU-1", "qty": 12}]},
            {
                "status": "draft",
                "cards": [{"article": "SKU-1", "rows": [{"qty": 12}]}],
            },
        ):
            with self.subTest(payload=payload):
                enabled = ProcessingWorkflowService._client_processing_unit_picker_enabled(
                    client_agency=self.agency,
                    edit_order_id="",
                    draft_order_id="draft-legacy-populated",
                    draft_payload=payload,
                )

                self.assertFalse(enabled)

    def test_processing_attachment_is_not_duplicated_by_followup_autosave(self):
        with TemporaryDirectory() as media_root, self.settings(MEDIA_ROOT=media_root):
            first_request = self.request_factory.post(
                "/orders/processing/",
                data={
                    "agency_id": str(self.agency.id),
                    "submit_action": "draft",
                    "draft_autosave": "1",
                    "product_name": "Draft with one attachment",
                    "documents": SimpleUploadedFile(
                        "processing-note.txt",
                        b"processing attachment",
                        content_type="text/plain",
                    ),
                },
            )
            first_request.user = self.user
            first_request._client_agency = self.agency

            first_response = ProcessingWorkflowService.submit_processing(request=first_request)
            first_data = json.loads(first_response.content.decode("utf-8"))
            order_id = first_data["order_id"]

            self.assertEqual(first_response.status_code, 200)
            self.assertEqual(
                ProcessingOrderAttachment.objects.filter(
                    order_id=order_id,
                    agency=self.agency,
                ).count(),
                1,
            )

            autosave_request = self.request_factory.post(
                "/orders/processing/",
                data={
                    "agency_id": str(self.agency.id),
                    "submit_action": "draft",
                    "draft_autosave": "1",
                    "draft_order_id": order_id,
                    "product_name": "Draft with one attachment",
                },
            )
            autosave_request.user = self.user
            autosave_request._client_agency = self.agency

            autosave_response = ProcessingWorkflowService.submit_processing(request=autosave_request)

            self.assertEqual(autosave_response.status_code, 200)
            self.assertEqual(
                ProcessingOrderAttachment.objects.filter(
                    order_id=order_id,
                    agency=self.agency,
                ).count(),
                1,
            )

    def test_submit_processing_client_submission_creates_manager_task(self):
        manager_user = get_user_model().objects.create_user(username="proc_home_manager", password="x")
        manager_employee = Employee.objects.create(
            full_name="Processing Home Manager",
            user=manager_user,
            role="manager",
            is_active=True,
        )
        request = self.request_factory.post(
            "/orders/processing/",
            data={
                "submit_action": "send",
                "product_name": "Processing order product",
                "article": "SKU-HOME-1",
                "stock_article[]": ["SKU-HOME-1"],
                "stock_size[]": ["42"],
                "stock_barcode[]": ["BAR-HOME-1"],
                "stock_qty[]": ["0"],
            },
        )
        request.user = self.user
        request._client_agency = self.agency

        response = ProcessingWorkflowService.submit_processing(request=request)

        self.assertEqual(response.status_code, 302)
        latest = OrderAuditEntry.objects.filter(order_type="processing", agency=self.agency).order_by("-id").first()
        self.assertIsNotNone(latest)
        order_id = latest.order_id
        self.assertTrue(
            Task.objects.filter(
                route=f"/orders/processing/{order_id}/",
                assigned_to=manager_employee,
            ).exclude(status="done").exists()
        )
        task = Task.objects.filter(route=f"/orders/processing/{order_id}/", assigned_to=manager_employee).exclude(status="done").first()
        self.assertIn("Подтвердите заявку на обработку", task.title)
        self.assertIn("Клиент:", task.description)
        sla_hours = (task.due_date - task.created_at).total_seconds() / 3600
        self.assertAlmostEqual(sla_hours, 48, delta=1 / 60)

    def test_submit_processing_closes_leftover_client_drafts(self):
        portal_user = get_user_model().objects.create_user(username="proc_ghost_client", password="x")
        self.agency.portal_user = portal_user
        self.agency.save(update_fields=["portal_user"])
        OrderAuditEntry.objects.create(
            order_id="draft-leftover-1",
            order_type="processing",
            action="create",
            agency=self.agency,
            user=portal_user,
            description="Черновик заявки на обработку",
            payload={"status": "draft", "status_label": "Черновик", "submit_action": "draft"},
        )
        request = self.request_factory.post(
            "/orders/processing/",
            data={
                "submit_action": "send",
                "product_name": "После отправки без черновика",
                "article": "SKU-GHOST-1",
                "stock_article[]": ["SKU-GHOST-1"],
                "stock_size[]": ["42"],
                "stock_barcode[]": ["BAR-GHOST-1"],
                "stock_qty[]": ["0"],
            },
        )
        request.user = portal_user
        request._client_agency = self.agency

        response = ProcessingWorkflowService.submit_processing(request=request)

        self.assertEqual(response.status_code, 302)
        from client_cabinet.client_drafts import is_draft_payload, list_client_draft_order_ids

        self.assertEqual(list_client_draft_order_ids(agency=self.agency, order_type="processing"), [])
        leftover = (
            OrderAuditEntry.objects.filter(order_id="draft-leftover-1", order_type="processing")
            .order_by("-created_at", "-id")
            .first()
        )
        self.assertFalse(is_draft_payload(leftover.payload))
        submitted = (
            OrderAuditEntry.objects.filter(agency=self.agency, order_type="processing")
            .exclude(order_id__startswith="draft-")
            .order_by("-created_at", "-id")
            .first()
        )
        self.assertEqual((submitted.payload or {}).get("status"), "sent_unconfirmed")
        self.assertEqual((submitted.payload or {}).get("status_label"), "Ждет подтверждения")

    def test_submit_processing_autosave_skipped_after_recent_client_submit(self):
        portal_user = get_user_model().objects.create_user(username="proc_race_client", password="x")
        self.agency.portal_user = portal_user
        self.agency.save(update_fields=["portal_user"])
        OrderAuditEntry.objects.create(
            order_id="91",
            order_type="processing",
            action="create",
            agency=self.agency,
            user=portal_user,
            description="Заявка на обработку №91",
            payload={
                "status": "sent_unconfirmed",
                "status_label": "Ждет подтверждения",
                "submit_action": "submitted",
            },
        )
        request = self.request_factory.post(
            "/orders/processing/",
            data={
                "submit_action": "draft",
                "draft_autosave": "1",
                "product_name": "Гонка после отправки",
            },
        )
        request.user = portal_user
        request._client_agency = self.agency

        response = ProcessingWorkflowService.submit_processing(request=request)

        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content.decode("utf-8"))
        self.assertTrue(data["ok"])
        self.assertEqual(data.get("skipped"), "already_submitted")
        self.assertEqual(data.get("draft_order_id"), "")
        self.assertEqual(data.get("order_id"), "91")
        from client_cabinet.client_drafts import list_client_draft_order_ids

        self.assertEqual(list_client_draft_order_ids(agency=self.agency, order_type="processing"), [])


    def test_submit_processing_from_draft_with_placeholder_barcode_creates_manager_task(self):
        manager_user = get_user_model().objects.create_user(username="proc_placeholder_manager", password="x")
        manager_employee = Employee.objects.create(
            full_name="Processing Placeholder Manager",
            user=manager_user,
            role="manager",
            is_active=True,
        )
        create_warehouse_snapshot_row(
            agency=self.agency,
            order_type="receiving",
            order_id="REC-PLACEHOLDER-1",
            sku="028",
            name="Placeholder Barcode Item",
            size="25-32",
            barcode="",
            goods_type="op",
            qty=240,
            available_qty=240,
            pallet_code="PAL-028-1",
            zone="OS",
        )
        draft_order_id = "draft-placeholder-proc"
        OrderAuditEntry.objects.create(
            order_id=draft_order_id,
            order_type="processing",
            action="update",
            agency=self.agency,
            payload={
                "status": "draft",
                "status_label": "Черновик",
            },
        )
        request = self.request_factory.post(
            "/orders/processing/",
            data={
                "submit_action": "send",
                "draft_order_id": draft_order_id,
                "cards_json": json.dumps(
                    [
                        {
                            "id": "card-1",
                            "article": "028",
                            "product_name": "Placeholder Barcode Item",
                            "goods_type": "op",
                            "rows": [
                                {
                                    "article": "028",
                                    "size": "25-32",
                                    "barcode": "-",
                                    "qty": "240",
                                }
                            ],
                        }
                    ],
                    ensure_ascii=False,
                ),
            },
        )
        request.user = self.user
        request._client_agency = self.agency

        response = ProcessingWorkflowService.submit_processing(request=request)

        self.assertEqual(response.status_code, 302)
        latest = OrderAuditEntry.objects.filter(order_type="processing", agency=self.agency).order_by("-id").first()
        self.assertIsNotNone(latest)
        self.assertNotEqual(latest.order_id, draft_order_id)
        self.assertFalse(OrderAuditEntry.objects.filter(order_id=draft_order_id, order_type="processing").exists())
        reserve = WarehouseReserve.objects.get(
            agency=self.agency,
            reserve_type=WarehouseReserve.TYPE_PROCESSING,
            context_type="processing",
            context_id=latest.order_id,
            sku_code="028",
        )
        self.assertEqual(reserve.barcode, "")
        self.assertEqual(reserve.goods_type, "op")
        self.assertTrue(
            Task.objects.filter(
                route=f"/orders/processing/{latest.order_id}/",
                assigned_to=manager_employee,
            ).exclude(status="done").exists()
        )

    def test_submit_processing_preserves_box_codes_from_card_rows(self):
        create_warehouse_snapshot_row(
            agency=self.agency,
            order_type="receiving",
            order_id="REC-CARD-BOX-1",
            sku="SKU-CARD-BOX",
            name="Card box item",
            size="M",
            barcode="BAR-CARD-BOX",
            goods_type="op",
            qty=12,
            available_qty=12,
            pallet_code="PAL-CARD-BOX",
            box_code="BOX-CARD-1",
        )
        create_warehouse_snapshot_row(
            agency=self.agency,
            order_type="receiving",
            order_id="REC-CARD-BOX-2",
            sku="SKU-CARD-BOX",
            name="Card box item",
            size="M",
            barcode="BAR-CARD-BOX",
            goods_type="op",
            qty=12,
            available_qty=12,
            pallet_code="PAL-CARD-BOX",
            box_code="BOX-CARD-2",
        )
        request = self.request_factory.post(
            "/orders/processing/",
            data={
                "submit_action": "send",
                "cards_json": json.dumps(
                    [
                        {
                            "id": "card-box",
                            "article": "SKU-CARD-BOX",
                            "product_name": "Card box item",
                            "goods_type": "op",
                            "rows": [
                                {
                                    "article": "SKU-CARD-BOX",
                                    "size": "M",
                                    "barcode": "BAR-CARD-BOX",
                                    "qty": "12",
                                    "box_qty": 12,
                                    "boxes": 1,
                                    "box_codes": ["BOX-CARD-2"],
                                }
                            ],
                        }
                    ],
                    ensure_ascii=False,
                ),
            },
        )
        request.user = self.user
        request._client_agency = self.agency

        response = ProcessingWorkflowService.submit_processing(request=request)

        self.assertEqual(response.status_code, 302)
        latest = OrderAuditEntry.objects.filter(order_type="processing", agency=self.agency).order_by("-id").first()
        self.assertIsNotNone(latest)
        payload_row = (latest.payload.get("cards") or [])[0]["rows"][0]
        self.assertEqual(payload_row["box_codes"], ["BOX-CARD-2"])
        untouched = WarehouseStockSnapshot.objects.get(agency=self.agency, container_code="BOX-CARD-1")
        selected = WarehouseStockSnapshot.objects.get(agency=self.agency, container_code="BOX-CARD-2")
        self.assertEqual(untouched.processing_reserved_qty, 0)
        self.assertEqual(selected.processing_reserved_qty, 12)

    def test_new_client_unit_request_saves_the_exact_pr_box_without_substitution(self):
        selected = create_warehouse_snapshot_row(
            agency=self.agency,
            order_type="receiving",
            order_id="REC-PR-UNIT-1",
            sku="SKU-PR-UNIT",
            name="PR unit item",
            size="M",
            barcode="BAR-PR-UNIT",
            goods_type="op",
            qty=99,
            available_qty=99,
            pallet_code="PAL-PR-UNIT-1",
            box_code="BOX-PR-UNIT-1",
            zone="PR",
            warehouse_state_code=WarehouseStateCode.PLACED_IN_RECEIVING.value,
        )
        untouched = create_warehouse_snapshot_row(
            agency=self.agency,
            order_type="receiving",
            order_id="REC-PR-UNIT-2",
            sku="SKU-PR-UNIT",
            name="PR unit item",
            size="M",
            barcode="BAR-PR-UNIT",
            goods_type="op",
            qty=99,
            available_qty=99,
            pallet_code="PAL-PR-UNIT-2",
            box_code="BOX-PR-UNIT-2",
            zone="PR",
            warehouse_state_code=WarehouseStateCode.PLACED_IN_RECEIVING.value,
        )
        default_codes = {
            code
            for row in _processing_box_picker_rows(self.agency)
            for code in row.get("box_codes") or []
        }
        unit_rows = _processing_box_picker_rows(
            self.agency,
            include_receiving_stock=True,
            separate_boxes=True,
        )
        selected_picker_row = next(
            row for row in unit_rows if row.get("box_codes") == ["BOX-PR-UNIT-1"]
        )
        self.assertNotIn("BOX-PR-UNIT-1", default_codes)
        self.assertEqual(selected_picker_row.get("source_zone"), "PR")
        self.assertEqual(selected_picker_row.get("available_qty"), 99)
        request = self.request_factory.post(
            "/orders/processing/",
            data={
                "submit_action": "send",
                "client_unit_picker_v1": "1",
                "cards_json": json.dumps(
                    [
                        {
                            "id": "card-pr-unit",
                            "article": "SKU-PR-UNIT",
                            "product_name": "PR unit item",
                            "goods_type": "op",
                            "rows": [
                                {
                                    "article": "SKU-PR-UNIT",
                                    "size": "M",
                                    "barcode": "BAR-PR-UNIT",
                                    "qty": 99,
                                    "box_qty": 99,
                                    "boxes": 1,
                                    "box_codes": ["BOX-PR-UNIT-1"],
                                    "source_state": WarehouseStateCode.PLACED_IN_RECEIVING.value,
                                    "source_zone": "PR",
                                }
                            ],
                        }
                    ],
                    ensure_ascii=False,
                ),
            },
        )
        request.user = self.user
        request._client_agency = self.agency

        response = ProcessingWorkflowService.submit_processing(request=request)

        self.assertEqual(response.status_code, 302)
        latest = OrderAuditEntry.objects.filter(order_type="processing", agency=self.agency).order_by("-id").first()
        self.assertIsNotNone(latest)
        self.assertTrue((latest.payload or {}).get("client_unit_picker_v1"))
        stock_row = (latest.payload.get("stock_rows") or [])[0]
        self.assertEqual(stock_row.get("qty"), 99)
        self.assertEqual(stock_row.get("box_codes"), ["BOX-PR-UNIT-1"])
        self.assertTrue(stock_row.get("strict_selected_box"))
        selected.refresh_from_db()
        untouched.refresh_from_db()
        self.assertEqual(selected.processing_reserved_qty, 0)
        self.assertEqual(selected.available_qty, 99)
        self.assertEqual(selected.warehouse_state_code, WarehouseStateCode.PLACED_IN_RECEIVING.value)
        self.assertEqual(untouched.processing_reserved_qty, 0)
        self.assertEqual(untouched.available_qty, 99)
        self.assertEqual(untouched.warehouse_state_code, WarehouseStateCode.PLACED_IN_RECEIVING.value)

    def test_client_unit_picker_keeps_fully_available_mixed_box_lines_together(self):
        create_warehouse_snapshot_row(
            agency=self.agency,
            order_type="receiving",
            order_id="REC-MIXED-UNIT-1",
            sku="SKU-MIXED-A",
            name="Mixed item A",
            size="M",
            barcode="BAR-MIXED-A",
            goods_type="op",
            qty=4,
            available_qty=4,
            pallet_code="PAL-MIXED-UNIT-1",
            box_code="BOX-MIXED-UNIT-1",
        )
        create_warehouse_snapshot_row(
            agency=self.agency,
            order_type="receiving",
            order_id="REC-MIXED-UNIT-1",
            sku="SKU-MIXED-B",
            name="Mixed item B",
            size="L",
            barcode="BAR-MIXED-B",
            goods_type="op",
            qty=6,
            available_qty=6,
            pallet_code="PAL-MIXED-UNIT-1",
            box_code="BOX-MIXED-UNIT-1",
        )

        mixed_rows = [
            row
            for row in _processing_box_picker_rows(self.agency, separate_boxes=True)
            if row.get("box_codes") == ["BOX-MIXED-UNIT-1"]
        ]

        self.assertEqual(len(mixed_rows), 2)
        self.assertEqual({row.get("sku_code") for row in mixed_rows}, {"SKU-MIXED-A", "SKU-MIXED-B"})
        self.assertTrue(all(row.get("is_mixed_box") for row in mixed_rows))
        self.assertEqual(len({row.get("mixed_group") for row in mixed_rows}), 1)
        self.assertTrue(all(row.get("available_boxes") == 1 for row in mixed_rows))
        self.assertEqual(sum(int(row.get("available_qty") or 0) for row in mixed_rows), 10)

    def test_client_unit_picker_hides_partially_reserved_mixed_box(self):
        create_warehouse_snapshot_row(
            agency=self.agency,
            order_type="receiving",
            order_id="REC-MIXED-UNIT-RESERVED",
            sku="SKU-MIXED-AVAILABLE",
            name="Mixed available item",
            size="M",
            barcode="BAR-MIXED-AVAILABLE",
            goods_type="op",
            qty=4,
            available_qty=4,
            pallet_code="PAL-MIXED-UNIT-RESERVED",
            box_code="BOX-MIXED-UNIT-RESERVED",
        )
        create_warehouse_snapshot_row(
            agency=self.agency,
            order_type="receiving",
            order_id="REC-MIXED-UNIT-RESERVED",
            sku="SKU-MIXED-RESERVED",
            name="Mixed reserved item",
            size="L",
            barcode="BAR-MIXED-RESERVED",
            goods_type="op",
            qty=6,
            available_qty=3,
            processing_reserved_qty=3,
            pallet_code="PAL-MIXED-UNIT-RESERVED",
            box_code="BOX-MIXED-UNIT-RESERVED",
        )
        WarehouseReserve.objects.create(
            agency=self.agency,
            reserve_type=WarehouseReserve.TYPE_PROCESSING,
            context_type="processing",
            context_id="OTHER-PROCESSING-ORDER",
            sku_code="SKU-MIXED-RESERVED",
            size="L",
            barcode="BAR-MIXED-RESERVED",
            goods_type="op",
            qty_reserved=3,
            status=WarehouseReserve.STATUS_ACTIVE,
        )

        mixed_rows = [
            row
            for row in _processing_box_picker_rows(self.agency, separate_boxes=True)
            if row.get("box_codes") == ["BOX-MIXED-UNIT-RESERVED"]
        ]

        self.assertEqual(mixed_rows, [])

    def test_client_submit_passes_services_and_article_change_to_manager_format(self):
        from .web_ui import _is_yes_value, _processing_params_from_payload

        create_warehouse_snapshot_row(
            agency=self.agency,
            order_type="receiving",
            order_id="REC-CLIENT-MAP-1",
            sku="SKU-A-MAP",
            name="Source article item",
            size="42",
            barcode="BAR-A-MAP",
            goods_type="op",
            qty=24,
            available_qty=24,
            pallet_code="PAL-A-MAP",
            box_code="BOX-A-MAP",
        )
        request = self.request_factory.post(
            "/orders/processing/",
            data={
                "submit_action": "send",
                "marketplace": "WB",
                "set_build": "Да",
                "set_qty": "3",
                "article_change_needed": "Да",
                "bag_replace_needed": "Да",
                "bag_replace_type": "Zip",
                "bag_replace_supply": "Фуллбокс",
                "individual_box_needed": "Да",
                "comments": "Клиентский комментарий",
                "cards_json": json.dumps(
                    [
                        {
                            "id": "c1",
                            "article": "SKU-A-MAP",
                            "product_name": "Source article item",
                            "goods_type": "op",
                            "result_article": "SKU-B-MAP",
                            "result_product_name": "Target article item",
                            "result_barcode": "BAR-B-MAP",
                            "rows": [
                                {
                                    "article": "SKU-A-MAP",
                                    "size": "42",
                                    "barcode": "BAR-A-MAP",
                                    "qty": "24",
                                    "boxes": 1,
                                    "box_qty": 24,
                                    "box_codes": ["BOX-A-MAP"],
                                    "result_article": "SKU-B-MAP",
                                    "result_product_name": "Target article item",
                                    "result_barcode": "BAR-B-MAP",
                                }
                            ],
                        }
                    ],
                    ensure_ascii=False,
                ),
            },
        )
        request.user = self.user
        request._client_agency = self.agency

        response = ProcessingWorkflowService.submit_processing(request=request)
        self.assertEqual(response.status_code, 302)

        latest = OrderAuditEntry.objects.filter(order_type="processing", agency=self.agency).order_by("-id").first()
        self.assertIsNotNone(latest)
        payload = latest.payload or {}
        self.assertEqual(payload.get("marketplace"), "WB")
        self.assertEqual(payload.get("set_build"), "Да")
        self.assertEqual(str(payload.get("set_qty")), "3")
        self.assertEqual(payload.get("article_change_needed"), "Да")
        self.assertEqual(payload.get("bag_replace_type"), "Zip")
        self.assertEqual(payload.get("individual_box_needed"), "Да")
        self.assertEqual(payload.get("comments"), "Клиентский комментарий")
        card = (payload.get("cards") or [])[0]
        self.assertEqual(card.get("result_article"), "SKU-B-MAP")
        self.assertEqual(card["rows"][0].get("result_article"), "SKU-B-MAP")
        stock_row = (payload.get("stock_rows") or [])[0]
        self.assertEqual(stock_row.get("result_article"), "SKU-B-MAP")

        self.assertTrue(_is_yes_value("Да"))
        self.assertTrue(_is_yes_value("да"))
        params = {row["label"]: row["value"] for row in _processing_params_from_payload(payload)}
        self.assertEqual(params.get("Маркетплейс"), "WB")
        self.assertIn("3", str(params.get("Сборка набора") or ""))
        self.assertIn("SKU-A-MAP", str(params.get("Изменение артикула товара") or ""))
        self.assertIn("SKU-B-MAP", str(params.get("Изменение артикула товара") or ""))
        self.assertEqual(params.get("Прочие") or params.get("Комментарий") or payload.get("comments"), "Клиентский комментарий")

        tech_ctx = ProcessingWorkflowService.build_processing_technical_card_page_context(
            ctx={"card": card, "card_rows": card.get("rows") or []},
            order_id=latest.order_id,
            card_id="c1",
            request=request,
            payload=payload,
        )
        self.assertTrue(tech_ctx["tech_checks"]["set_yes"])
        self.assertFalse(tech_ctx["tech_checks"]["set_no"])
        self.assertTrue(tech_ctx["tech_checks"]["article_change_yes"])
        self.assertIn("SKU-B-MAP", tech_ctx["tech"]["article_change_text"])

    def test_client_submit_rejects_same_article_when_article_change_is_enabled(self):
        source_sku = SKU.objects.create(
            agency=self.agency,
            sku_code="SKU-SAME",
            name="Same article item",
            source="marketplace",
        )
        SKUBarcode.objects.create(
            sku=source_sku,
            value="BAR-SAME",
            size="42",
            is_primary=True,
        )
        request = self.request_factory.post(
            "/orders/processing/",
            data={
                "submit_action": "send",
                "marketplace": "WB",
                "article_change_needed": "Да",
                "cards_json": json.dumps(
                    [
                        {
                            "id": "card-same",
                            "article": "SKU-SAME",
                            "product_name": "Same article item",
                            "goods_type": "op",
                            "result_sku_id": str(source_sku.id),
                            "result_article": "SKU-SAME",
                            "result_product_name": "Same article item",
                            "result_barcode": "BAR-SAME",
                            "rows": [
                                {
                                    "article": "SKU-SAME",
                                    "size": "42",
                                    "barcode": "BAR-SAME",
                                    "qty": "1",
                                    "boxes": 1,
                                    "box_qty": 1,
                                    "result_sku_id": str(source_sku.id),
                                    "result_article": "SKU-SAME",
                                    "result_product_name": "Same article item",
                                    "result_barcode": "BAR-SAME",
                                }
                            ],
                        }
                    ],
                    ensure_ascii=False,
                ),
            },
        )
        request.user = self.user
        request._client_agency = self.agency

        response = ProcessingWorkflowService.submit_processing(request=request)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Замена А → А невозможна.")
        self.assertFalse(
            OrderAuditEntry.objects.filter(
                order_type="processing",
                agency=self.agency,
            ).exists()
        )

    def test_submit_processing_keeps_stock_unreserved_until_reachtruck_fact(self):
        create_warehouse_snapshot_row(
            agency=self.agency,
            order_type="receiving",
            order_id="REC-ROLLBACK-1",
            sku="028",
            name="Rollback Item",
            size="25-32",
            barcode="",
            goods_type="op",
            qty=240,
            available_qty=240,
            pallet_code="PAL-ROLLBACK-1",
            zone="OS",
        )
        request = self.request_factory.post(
            "/orders/processing/",
            data={
                "submit_action": "send",
                "cards_json": json.dumps(
                    [
                        {
                            "id": "card-1",
                            "article": "028",
                            "product_name": "Rollback Item",
                            "goods_type": "op",
                            "rows": [
                                {
                                    "article": "028",
                                    "size": "25-32",
                                    "barcode": "-",
                                    "qty": "240",
                                }
                            ],
                        }
                    ],
                    ensure_ascii=False,
                ),
            },
        )
        request.user = self.user
        request._client_agency = self.agency

        with mock.patch("processing_app.views._replace_processing_reserves") as replace_processing_reserves:
            response = ProcessingWorkflowService.submit_processing(request=request)

        self.assertEqual(response.status_code, 302)
        latest = OrderAuditEntry.objects.filter(order_type="processing", agency=self.agency).order_by("-id").first()
        self.assertIsNotNone(latest)
        replace_processing_reserves.assert_called_once_with(latest.order_id, self.agency, [])
        snapshot = WarehouseStockSnapshot.objects.get(agency=self.agency, sku_code="028")
        self.assertEqual(snapshot.processing_reserved_qty, 0)
        self.assertEqual(snapshot.available_qty, 240)

    def test_submit_processing_rejects_edit_when_warehouse_already_started(self):
        order_id = "630-EDIT-WH"
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="processing",
            action="status",
            agency=self.agency,
            payload={
                "status": "submitted",
                "status_label": "На подтверждении",
                "product_name": "Warehouse processing product",
            },
        )
        self._create_processing_snapshot(order_id=order_id, state_code="processing_in_progress")
        manager_user = get_user_model().objects.create_user(username="proc_edit_manager", password="x")
        Employee.objects.create(
            full_name="Processing Edit Manager",
            user=manager_user,
            role="manager",
            is_active=True,
        )
        request = self.request_factory.post(
            "/orders/processing/",
            data={
                "agency_id": str(self.agency.id),
                "edit_order_id": order_id,
                "draft_autosave": "1",
                "product_name": "Updated processing product",
            },
        )
        request.user = manager_user

        response = ProcessingWorkflowService.submit_processing(request=request)

        self.assertEqual(response.status_code, 400)
        data = json.loads(response.content.decode("utf-8"))
        self.assertFalse(data["ok"])
        self.assertIn("недоступна для редактирования", data["error"])
        self.assertEqual(
            OrderAuditEntry.objects.filter(order_id=order_id, order_type="processing").count(),
            1,
        )


class ProcessingDraftDetailRedirectTests(TestCase):
    def setUp(self):
        self.request_factory = RequestFactory()
        self.agency = Agency.objects.create(agn_name="Draft Detail Agency")
        self.manager = get_user_model().objects.create_user(username="draft_detail_manager", password="x")
        Employee.objects.create(
            full_name="Draft Detail Manager",
            user=self.manager,
            role="manager",
            is_active=True,
        )
        self.portal_user = get_user_model().objects.create_user(username="draft_detail_portal", password="x")
        self.agency.portal_user = self.portal_user
        self.agency.save(update_fields=["portal_user"])

    def test_manager_with_client_opens_existing_draft_on_form(self):
        order_id = "draft-321ca1708b3f"
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="processing",
            action="create",
            agency=self.agency,
            payload={"status": "draft", "status_label": "Черновик", "submit_action": "draft"},
        )
        request = self.request_factory.get(
            f"/orders/processing/{order_id}/",
            data={"client": self.agency.id},
        )
        request.user = self.manager
        response = ProcessingDetailView.as_view()(request, order_id=order_id)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response.url,
            f"/orders/processing/?client={self.agency.id}&order={order_id}&status=draft",
        )

    def test_deleted_draft_redirects_with_notice(self):
        order_id = "draft-already-gone"
        request = self.request_factory.get(
            f"/orders/processing/{order_id}/",
            data={"client": self.agency.id},
        )
        request.user = self.portal_user
        response = ProcessingDetailView.as_view()(request, order_id=order_id)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response.url,
            f"/orders/processing/?client={self.agency.id}&notice=draft_gone",
        )

    def test_home_shows_draft_gone_notice(self):
        request = self.request_factory.get(
            "/orders/processing/",
            data={"client": self.agency.id, "notice": "draft_gone"},
        )
        request.user = self.portal_user
        response = ProcessingHomeView.as_view()(request)
        self.assertEqual(response.status_code, 200)
        response.render()
        body = response.content.decode("utf-8")
        self.assertIn("Черновик уже удалён", body)

    def test_staff_without_client_forbidden_is_utf8_html(self):
        order_id = "draft-no-client"
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="processing",
            action="create",
            agency=self.agency,
            payload={"status": "draft", "status_label": "Черновик", "submit_action": "draft"},
        )
        request = self.request_factory.get(f"/orders/processing/{order_id}/")
        request.user = self.manager
        response = ProcessingDetailView.as_view()(request, order_id=order_id)
        self.assertEqual(response.status_code, 403)
        body = response.content.decode("utf-8")
        self.assertIn("Доступ запрещен", body)
        self.assertNotIn("Р”РѕСЃС‚СѓРї", body)
        self.assertIn("charset=utf-8", response["Content-Type"].lower())


class ProcessingFlowServiceTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="proc_flow_service_user", password="x")
        Employee.objects.create(
            full_name="Processing Flow Service User",
            user=self.user,
            role="processing_head",
            is_active=True,
        )
        self.request_factory = RequestFactory()
        self.agency = Agency.objects.create(agn_name="Processing Flow Service Agency")
        self.barcode = "BAR-SKU-A"

    def _create_entries(self, order_id: str, *, flow_closed: bool = False):
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="processing",
            action="status",
            agency=self.agency,
            payload={
                "status": "processing_in_work",
                "status_label": "Взята в работу",
                "processing_stage": PROCESSING_STAGE_UNBOXING_OPENED,
                "processing_stage_label": "открыта раскоробовка",
                "cards": [
                    {
                        "id": "card-a",
                        "article": "SKU-A",
                        "rows": [{"size": "42", "qty": "10", "barcode": self.barcode}],
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
                        "result_barcode": self.barcode,
                    },
                ],
            },
        )
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="processing",
            action="update",
            agency=self.agency,
            payload={
                "act": "placement",
                "flow_closed": flow_closed,
                "act_boxes": [
                    {
                        "code": "BOX-FLOW-1",
                        "items": [{"sku": "SKU-A", "barcode": self.barcode, "qty": 10}],
                    }
                ],
                "act_pallets": [{"code": "PAL-FLOW-1", "boxes": ["BOX-FLOW-1"], "items": []}],
                "flow_closed_at": timezone.localtime().isoformat() if flow_closed else "",
            },
        )
        return list(OrderAuditEntry.objects.filter(order_id=order_id, order_type="processing").order_by("created_at"))

    @staticmethod
    def _normalize_flow_state(boxes, pallets, active_box, active_pallet):
        return {
            "boxes": boxes,
            "pallets": pallets,
            "activeBox": active_box,
            "activePallet": active_pallet,
        }

    def _create_processing_shipping_attachment(
        self,
        *,
        order_id: str,
        rows: list[dict],
        filename: str = "marketplace-shipping.xlsx",
    ) -> ProcessingOrderAttachment:
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "Техническое задание"
        sheet.append(
            [
                "Наименование товара",
                "Количество",
                "Баркод товара",
                "Артикул",
                "Модель",
            ]
        )
        for row in rows:
            sheet.append(
                [
                    row.get("name", ""),
                    row.get("qty", 0),
                    row.get("barcode", ""),
                    row.get("article", ""),
                    row.get("model", ""),
                ]
            )
        content = BytesIO()
        workbook.save(content)
        workbook.close()
        return ProcessingOrderAttachment.objects.create(
            order_id=order_id,
            agency=self.agency,
            uploaded_by=self.user,
            file=SimpleUploadedFile(
                filename,
                content.getvalue(),
                content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            ),
        )

    def _create_catalog_barcode(self, *, sku_code: str, name: str, barcode: str) -> None:
        sku = SKU.objects.create(
            agency=self.agency,
            sku_code=sku_code,
            name=name,
        )
        SKUBarcode.objects.create(
            sku=sku,
            value=barcode,
            is_primary=True,
        )

    def test_attachment_shipping_barcode_replaces_internal_virgin_barcode(self):
        order_id = "63"
        internal_barcode = "2975678466803"
        outgoing_barcode = "OZN2446435402"
        self._create_catalog_barcode(
            sku_code="Z0608500200107713",
            name="VIRGIN SILVER 5S",
            barcode=outgoing_barcode,
        )
        payload = {
            "processing_results": [
                {
                    "article": "VIRGIN Silver 5S (НК85)",
                    "name": "Набор из 5 ремешков",
                    "processed": "74",
                    "result_barcode": internal_barcode,
                }
            ]
        }
        with TemporaryDirectory() as media_root, self.settings(MEDIA_ROOT=media_root):
            self._create_processing_shipping_attachment(
                order_id=order_id,
                rows=[
                    {
                        "name": "Набор из 5 ремешков",
                        "qty": 74,
                        "barcode": outgoing_barcode,
                        "article": "Z0608500200107713",
                        "model": "VIRGIN SILVER 5S",
                    }
                ],
            )
            items = ProcessingWorkflowService._processing_fact_items_by_barcode(
                payload=payload,
                order_id=order_id,
                agency_id=self.agency.id,
            )

        self.assertEqual(items[0]["barcode"], outgoing_barcode.lower())
        self.assertIn(internal_barcode, items[0]["barcode_aliases"])
        self.assertEqual(items[0]["outgoing_barcode_source"], "processing_attachment")

    def test_attachment_shipping_barcode_replaces_internal_titan_barcode(self):
        order_id = "71"
        internal_barcode = "2975737305900"
        outgoing_barcode = "OZN4389317620"
        self._create_catalog_barcode(
            sku_code="Z01010002001077056",
            name="TITAN SILVER NEW",
            barcode=outgoing_barcode,
        )
        payload = {
            "processing_results": [
                {
                    "article": "TITAN Silver (NX1 PRO)",
                    "name": "Смарт-часы наручные",
                    "processed": "65",
                    "result_barcode": internal_barcode,
                }
            ]
        }
        with TemporaryDirectory() as media_root, self.settings(MEDIA_ROOT=media_root):
            self._create_processing_shipping_attachment(
                order_id=order_id,
                rows=[
                    {
                        "name": "Смарт-часы наручные",
                        "qty": 65,
                        "barcode": outgoing_barcode,
                        "article": "Z01010002001077056",
                        "model": "TITAN SILVER NEW",
                    }
                ],
            )
            items = ProcessingWorkflowService._processing_fact_items_by_barcode(
                payload=payload,
                order_id=order_id,
                agency_id=self.agency.id,
            )

        self.assertEqual(items[0]["barcode"], outgoing_barcode.lower())
        self.assertIn(internal_barcode, items[0]["barcode_aliases"])

    def test_attachment_shipping_barcode_does_not_guess_ambiguous_mapping(self):
        order_id = "AMBIGUOUS-BARCODE"
        internal_barcode = "2975000000001"
        for index, outgoing_barcode in enumerate(("OZN9000000001", "OZN9000000002"), start=1):
            self._create_catalog_barcode(
                sku_code=f"OUT-{index}",
                name="VIRGIN SILVER 5S",
                barcode=outgoing_barcode,
            )
        payload = {
            "processing_results": [
                {
                    "article": "VIRGIN Silver 5S (НК85)",
                    "name": "Набор из 5 ремешков",
                    "processed": "74",
                    "result_barcode": internal_barcode,
                }
            ]
        }
        with TemporaryDirectory() as media_root, self.settings(MEDIA_ROOT=media_root):
            self._create_processing_shipping_attachment(
                order_id=order_id,
                rows=[
                    {
                        "qty": 74,
                        "barcode": "OZN9000000001",
                        "model": "VIRGIN SILVER 5S",
                    },
                    {
                        "qty": 74,
                        "barcode": "OZN9000000002",
                        "model": "VIRGIN SILVER 5S",
                    },
                ],
            )
            items = ProcessingWorkflowService._processing_fact_items_by_barcode(
                payload=payload,
                order_id=order_id,
                agency_id=self.agency.id,
            )

        self.assertEqual(items[0]["barcode"], internal_barcode)
        self.assertNotIn("outgoing_barcode_source", items[0])

    def test_comment_relabel_barcode_replaces_internal_barcode(self):
        internal_barcode = "2973453683108"
        outgoing_barcode = "OZN5601751874"
        self._create_catalog_barcode(
            sku_code="Z01098003020077014",
            name="LEAF BLACK J98",
            barcode=outgoing_barcode,
        )
        payload = {
            "comments": (
                "модель/артикул - количество - баркод\n"
                f"LEAF Black (J98)-150 шт - {outgoing_barcode}"
            ),
            "processing_results": [
                {
                    "article": "LEAF Black (J98)",
                    "processed": "150",
                    "tags_replaced": "150",
                    "result_barcode": internal_barcode,
                }
            ],
        }

        items = ProcessingWorkflowService._processing_fact_items_by_barcode(
            payload=payload,
            order_id="58",
            agency_id=self.agency.id,
        )

        self.assertEqual(items[0]["barcode"], outgoing_barcode.lower())
        self.assertIn(internal_barcode, items[0]["barcode_aliases"])
        self.assertEqual(
            items[0]["outgoing_barcode_source"],
            "processing_comment_relabel",
        )

    def test_comment_barcode_stays_alias_when_relabelling_is_incomplete(self):
        internal_barcode = "2973453683108"
        outgoing_barcode = "OZN5601751874"
        self._create_catalog_barcode(
            sku_code="Z01098003020077014",
            name="LEAF BLACK J98",
            barcode=outgoing_barcode,
        )
        payload = {
            "comments": f"LEAF Black (J98) - 150 шт - {outgoing_barcode}",
            "processing_results": [
                {
                    "article": "LEAF Black (J98)",
                    "processed": "150",
                    "tags_replaced": "149",
                    "result_barcode": internal_barcode,
                }
            ],
        }

        items = ProcessingWorkflowService._processing_fact_items_by_barcode(
            payload=payload,
            order_id="58",
            agency_id=self.agency.id,
        )

        self.assertEqual(items[0]["barcode"], internal_barcode)
        self.assertIn(outgoing_barcode.lower(), items[0]["barcode_aliases"])
        self.assertNotIn("outgoing_barcode_source", items[0])

    def test_complete_processing_flow_rejects_invalid_stage_before_side_effects(self):
        order_id = "622-FLOW-STAGE"
        entries = [
            OrderAuditEntry.objects.create(
                order_id=order_id,
                order_type="processing",
                action="status",
                agency=self.agency,
                payload={
                    "processing_stage": PROCESSING_STAGE_AWAITING_APPROVAL,
                    "status": "sent_unconfirmed",
                    "status_label": "Ждет подтверждения",
                },
            )
        ]
        request = self.request_factory.post(
            f"/orders/processing/{order_id}/flow/",
            data={"boxes_json": "[]", "pallets_json": "[]"},
        )
        request.user = self.user

        with mock.patch.object(
            OperationalStockService,
            "replace_order_placement",
        ) as replace_order_placement:
            result = ProcessingWorkflowService.complete_processing_flow(
                order_id=order_id,
                entries=entries,
                request=request,
                normalize_flow_state=self._normalize_flow_state,
                items_from_placement_act=lambda _entries: [],
                can_start=True,
                can_finish_with_mismatch=False,
                can_reassign_boxes=True,
            )

        self.assertEqual(result.status, "error")
        self.assertEqual(result.error_code, "invalid_stage_transition")
        replace_order_placement.assert_not_called()

    def test_save_processing_flow_draft_creates_session(self):
        order_id = "623"
        entries = self._create_entries(order_id)
        request = self.request_factory.post(
            f"/orders/processing/{order_id}/flow/",
            data={
                "boxes_json": '[{"code":"BOX-1","items":[{"sku":"SKU-A","qty":10}]}]',
                "pallets_json": '[{"code":"PAL-1","boxes":["BOX-1"],"items":[]}]',
                "active_box": "BOX-1",
                "active_pallet": "PAL-1",
                "agent_id": "agent-1",
            },
        )
        request.user = self.user

        with mock.patch("processing_app.views.ProcessingFlowView._can_start", return_value=True):
            result = ProcessingWorkflowService.save_processing_flow_draft(
                order_id=order_id,
                entries=entries,
                request=request,
                normalize_flow_state=self._normalize_flow_state,
                can_reassign_boxes=True,
            )

        self.assertEqual(result.status, "ok")
        session = ProcessingFlowSession.objects.get(order_id=order_id, agent_id="agent-1")
        self.assertEqual(session.flow_state.get("activeBox"), "BOX-1")
        self.assertEqual(session.flow_state.get("activePallet"), "PAL-1")

    def test_save_processing_flow_draft_persists_box_characteristics_to_container_truth(self):
        order_id = "623-DIMS"
        entries = self._create_entries(order_id)
        request = self.request_factory.post(
            f"/orders/processing/{order_id}/flow/",
            data={
                "boxes_json": (
                    '[{"code":"BOX-DIMS-1","gross_weight_g":1750,"width_mm":300,"height_mm":220,"depth_mm":410,'
                    '"items":[{"sku":"SKU-A","qty":10}]}]'
                ),
                "pallets_json": '[{"code":"PAL-DIMS-1","boxes":["BOX-DIMS-1"],"items":[]}]',
                "active_box": "",
                "active_pallet": "PAL-DIMS-1",
                "agent_id": "agent-dims",
            },
        )
        request.user = self.user

        with mock.patch("processing_app.views.ProcessingFlowView._can_start", return_value=True):
            result = ProcessingWorkflowService.save_processing_flow_draft(
                order_id=order_id,
                entries=entries,
                request=request,
                normalize_flow_state=self._normalize_flow_state,
                can_reassign_boxes=True,
            )

        self.assertEqual(result.status, "ok")
        container = WarehouseContainer.objects.get(agency=self.agency, container_code="BOX-DIMS-1")
        self.assertEqual(container.gross_weight_g, 1750)
        self.assertEqual(container.width_mm, 300)
        self.assertEqual(container.height_mm, 220)
        self.assertEqual(container.depth_mm, 410)

    def test_reopen_processing_flow_creates_reopen_snapshot_session(self):
        order_id = "624"
        entries = self._create_entries(order_id, flow_closed=True)
        request = self.request_factory.post(
            f"/orders/processing/{order_id}/flow/",
            data={"flow_action": "reopen"},
        )
        request.user = self.user

        result = ProcessingWorkflowService.reopen_processing_flow(
            order_id=order_id,
            entries=entries,
            request=request,
            normalize_flow_state=self._normalize_flow_state,
        )

        self.assertEqual(result.status, "ok")
        seed_session = ProcessingFlowSession.objects.get(order_id=order_id, agent_id="__reopen_snapshot__")
        self.assertEqual(seed_session.status, ProcessingFlowSession.STATUS_OPEN)
        self.assertEqual(len(seed_session.flow_state.get("boxes") or []), 1)

    def test_reopen_processing_flow_uses_full_closed_act_instead_of_later_partial_placement(self):
        order_id = "624-full-act"
        entries = self._create_entries(order_id, flow_closed=True)
        closed_entry = entries[-1]
        closed_payload = dict(closed_entry.payload or {})
        closed_payload["act_boxes"] = [
            *(closed_payload.get("act_boxes") or []),
            {
                "code": "BOX-FLOW-2",
                "items": [{"sku": "SKU-A", "barcode": self.barcode, "qty": 2}],
            },
        ]
        closed_payload["act_pallets"] = [
            *(closed_payload.get("act_pallets") or []),
            {"code": "PAL-FLOW-2", "boxes": ["BOX-FLOW-2"], "items": []},
        ]
        closed_entry.payload = closed_payload
        closed_entry.save(update_fields=["payload"])
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="processing",
            action="update",
            agency=self.agency,
            payload={
                "act": "placement",
                "act_boxes": [{"code": "BOX-FLOW-2", "items": []}],
                "act_pallets": [{"code": "PAL-FLOW-2", "boxes": ["BOX-FLOW-2"], "items": []}],
            },
        )
        entries = list(
            OrderAuditEntry.objects.filter(order_id=order_id, order_type="processing")
            .order_by("created_at", "id")
        )
        request = self.request_factory.post(
            f"/orders/processing/{order_id}/flow/",
            data={"flow_action": "reopen"},
        )
        request.user = self.user

        result = ProcessingWorkflowService.reopen_processing_flow(
            order_id=order_id,
            entries=entries,
            request=request,
            normalize_flow_state=self._normalize_flow_state,
        )

        self.assertEqual(result.status, "ok")
        seed_session = ProcessingFlowSession.objects.get(order_id=order_id, agent_id="__reopen_snapshot__")
        self.assertEqual(
            [box.get("code") for box in seed_session.flow_state.get("boxes") or []],
            ["BOX-FLOW-1", "BOX-FLOW-2"],
        )

    def test_reopen_processing_flow_requires_correction_mode_after_pallet_release(self):
        order_id = "624-released"
        self._create_entries(order_id, flow_closed=True)
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="processing",
            action="update",
            agency=self.agency,
            payload={
                "event": ProcessingWorkflowService.PROCESSING_PALLET_RELEASE_EVENT,
                "pallet_code": "PAL-FLOW-1",
            },
        )
        entries = list(
            OrderAuditEntry.objects.filter(order_id=order_id, order_type="processing")
            .order_by("created_at", "id")
        )
        request = self.request_factory.post(
            f"/orders/processing/{order_id}/flow/",
            data={"flow_action": "reopen"},
        )
        request.user = self.user

        result = ProcessingWorkflowService.reopen_processing_flow(
            order_id=order_id,
            entries=entries,
            request=request,
            normalize_flow_state=self._normalize_flow_state,
        )

        self.assertEqual(result.status, "released_pallets_require_correction")
        self.assertEqual(result.released_pallet_codes, ("PAL-FLOW-1",))
        self.assertFalse(ProcessingFlowSession.objects.filter(order_id=order_id).exists())
        self.assertFalse(
            OrderAuditEntry.objects.filter(
                order_id=order_id,
                order_type="processing",
                payload__flow_reopened=True,
            ).exists()
        )

    def test_reopen_processing_flow_additional_box_mode_reopens_worker_task(self):
        order_id = "624-correction"
        entries = self._create_entries(order_id, flow_closed=True)
        status_entry = entries[0]
        status_payload = dict(status_entry.payload or {})
        status_payload["placed_cards"] = ["card-a"]
        status_entry.payload = status_payload
        status_entry.save(update_fields=["payload"])
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="processing",
            action="update",
            agency=self.agency,
            payload={
                "event": ProcessingWorkflowService.PROCESSING_PALLET_RELEASE_EVENT,
                "pallet_code": "PAL-FLOW-1",
            },
        )
        worker_user = get_user_model().objects.create_user(username="proc_flow_reopen_worker", password="x")
        worker = Employee.objects.create(
            full_name="Processing Flow Reopen Worker",
            user=worker_user,
            role="processing_worker",
            is_active=True,
        )
        packaging_task = Task.objects.create(
            title=f"Формирование коробов по заявке №{order_id}",
            route=f"/orders/processing/{order_id}/flow/",
            assigned_to=worker,
            created_by=self.user,
            status="done",
            due_date=timezone.localtime(),
        )
        entries = list(
            OrderAuditEntry.objects.filter(order_id=order_id, order_type="processing")
            .order_by("created_at", "id")
        )
        request = self.request_factory.post(
            f"/orders/processing/{order_id}/flow/",
            data={"flow_action": "reopen", "correction_mode": "additional_box"},
        )
        request.user = self.user

        result = ProcessingWorkflowService.reopen_processing_flow(
            order_id=order_id,
            entries=entries,
            request=request,
            normalize_flow_state=self._normalize_flow_state,
            correction_mode="additional_box",
        )

        self.assertEqual(result.status, "ok")
        self.assertEqual(result.correction_mode, "additional_box")
        self.assertEqual(result.reopened_task_id, packaging_task.id)
        packaging_task.refresh_from_db()
        self.assertEqual(packaging_task.status, "in_progress")
        seed_session = ProcessingFlowSession.objects.get(order_id=order_id, agent_id="__reopen_snapshot__")
        self.assertEqual(
            [box.get("code") for box in seed_session.flow_state.get("boxes") or []],
            ["BOX-FLOW-1"],
        )
        reopen_payload = (
            OrderAuditEntry.objects.filter(
                order_id=order_id,
                order_type="processing",
                payload__flow_reopened=True,
            )
            .order_by("-created_at", "-id")
            .values_list("payload", flat=True)
            .first()
        )
        self.assertEqual(reopen_payload.get("correction_mode"), "additional_box")
        self.assertEqual(reopen_payload.get("released_pallet_codes"), ["PAL-FLOW-1"])
        self.assertEqual(reopen_payload.get("reopened_task_id"), packaging_task.id)

        reopened_entries = list(
            OrderAuditEntry.objects.filter(order_id=order_id, order_type="processing")
            .select_related("agency")
            .order_by("created_at", "id")
        )
        work_payload = _processing_work_payload_from_entries(reopened_entries)
        self.assertEqual(_processing_flow_ready_cards(work_payload), set())
        self.assertEqual(
            _processing_active_correction_mode(reopened_entries),
            "additional_box",
        )
        self.assertTrue(ProcessingFlowView()._can_start(reopened_entries))

        context_request = self.request_factory.get(
            f"/orders/processing/{order_id}/flow/"
        )
        context_request.user = self.user
        context = ProcessingWorkflowService.build_processing_flow_page_context(
            order_id=order_id,
            entries=reopened_entries,
            request=context_request,
            can_finish_flow=True,
            can_finish_flow_mismatch=True,
            can_reassign_boxes=True,
            is_directional_unboxing_payload=lambda payload: False,
            items_from_placement_act=lambda _entries: [
                {
                    "sku_code": "SKU-A",
                    "name": "Item",
                    "size": "42",
                    "actual_qty": 10,
                }
            ],
            normalize_flow_state=self._normalize_flow_state,
            find_flow_state=lambda _entries: {},
            placement_act_entry=lambda _entries: _entries[1],
            ok=False,
            error="",
        )
        self.assertTrue(context["processing_correction_mode"])

    def test_reopened_flow_without_additional_box_mode_stays_blocked_when_all_cards_are_placed(self):
        order_id = "624-ordinary-reopen"
        entries = self._create_entries(order_id, flow_closed=True)
        status_entry = entries[0]
        status_payload = dict(status_entry.payload or {})
        status_payload["placed_cards"] = ["card-a"]
        status_entry.payload = status_payload
        status_entry.save(update_fields=["payload"])
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="processing",
            action="update",
            agency=self.agency,
            payload={"flow_reopened": True},
        )
        reopened_entries = list(
            OrderAuditEntry.objects.filter(order_id=order_id, order_type="processing")
            .select_related("agency")
            .order_by("created_at", "id")
        )

        self.assertEqual(_processing_active_correction_mode(reopened_entries), "")
        self.assertFalse(ProcessingFlowView()._can_start(reopened_entries))

    def test_get_processing_flow_shared_state_uses_reopened_placement_without_open_sessions(self):
        order_id = "625"
        self._create_entries(order_id, flow_closed=True)
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="processing",
            action="update",
            agency=self.agency,
            payload={"flow_reopened": True},
        )

        result = ProcessingWorkflowService.get_processing_flow_shared_state(order_id=order_id)

        self.assertEqual(result.status, "ok")
        self.assertEqual(len(result.flow_state["boxes"]), 1)
        self.assertEqual(result.flow_state["boxes"][0]["code"], "BOX-FLOW-1")

    def test_log_processing_flow_box_action_writes_staff_overaction(self):
        order_id = "626"
        self._create_entries(order_id)
        request = self.request_factory.post(
            f"/orders/processing/{order_id}/flow/box-action/",
            data=json.dumps(
                {
                    "action": "edit",
                    "box_code": "BOX-FLOW-1",
                    "pallet_index": 1,
                    "box_index": 1,
                    "total_qty": 10,
                }
            ),
            content_type="application/json",
        )
        request.user = self.user

        result = ProcessingWorkflowService.log_processing_flow_box_action(
            order_id=order_id,
            request=request,
        )

        self.assertEqual(result.status, "ok")
        overaction = AuditEntry.objects.filter(journal__code="staff_overactions").latest("id")
        self.assertEqual(overaction.snapshot.get("box_code"), "BOX-FLOW-1")

    def test_confirm_legacy_barcode_uses_scanned_barcode_not_article(self):
        order_id = "626-LEGACY-BARCODE"
        self._create_entries(order_id)
        session = ProcessingFlowSession.objects.create(
            order_id=order_id,
            order_type="processing",
            agent_id="legacy-agent",
            user=self.user,
            flow_state={
                "boxes": [
                    {
                        "code": "BOX-LEGACY-1",
                        "sealed": True,
                        "items": [
                            {
                                "sku": "DIFFERENT-ARTICLE",
                                "name": "Legacy item",
                                "size": "42",
                                "qty": 10,
                            }
                        ],
                    }
                ],
                "pallets": [
                    {"code": "PAL-LEGACY-1", "boxes": ["BOX-LEGACY-1"], "items": []}
                ],
                "activeBox": "",
                "activePallet": "PAL-LEGACY-1",
            },
        )
        stock_count = WarehouseStockSnapshot.objects.count()
        request = self.request_factory.post(
            f"/orders/processing/{order_id}/flow/box-action/",
            data=json.dumps({}),
            content_type="application/json",
        )
        request.user = self.user

        result = ProcessingWorkflowService.confirm_processing_flow_legacy_barcode(
            order_id=order_id,
            request=request,
            payload={
                "barcode": self.barcode,
                "targets": [{"box_code": "BOX-LEGACY-1", "item_index": 0, "qty": 10}],
            },
        )

        self.assertEqual(result.status, "ok")
        session.refresh_from_db()
        saved_item = session.flow_state["boxes"][0]["items"][0]
        self.assertEqual(saved_item["sku"], "DIFFERENT-ARTICLE")
        self.assertEqual(saved_item["barcode"], self.barcode.lower())
        self.assertEqual(saved_item["qty"], 10)
        self.assertEqual(session.flow_state["pallets"][0]["code"], "PAL-LEGACY-1")
        self.assertEqual(WarehouseStockSnapshot.objects.count(), stock_count)

    def test_confirm_legacy_barcode_rejects_barcode_outside_processing_fact(self):
        order_id = "626-LEGACY-BARCODE-REJECT"
        self._create_entries(order_id)
        session = ProcessingFlowSession.objects.create(
            order_id=order_id,
            order_type="processing",
            agent_id="legacy-agent-reject",
            user=self.user,
            flow_state={
                "boxes": [
                    {
                        "code": "BOX-LEGACY-REJECT",
                        "sealed": True,
                        "items": [{"sku": "SKU-A", "qty": 10}],
                    }
                ],
                "pallets": [
                    {
                        "code": "PAL-LEGACY-REJECT",
                        "boxes": ["BOX-LEGACY-REJECT"],
                        "items": [],
                    }
                ],
            },
        )
        request = self.request_factory.post(
            f"/orders/processing/{order_id}/flow/box-action/",
            data=json.dumps({}),
            content_type="application/json",
        )
        request.user = self.user

        result = ProcessingWorkflowService.confirm_processing_flow_legacy_barcode(
            order_id=order_id,
            request=request,
            payload={
                "barcode": "BAR-WRONG",
                "targets": [
                    {"box_code": "BOX-LEGACY-REJECT", "item_index": 0, "qty": 10}
                ],
            },
        )

        self.assertEqual(result.status, "barcode_mismatch")
        session.refresh_from_db()
        self.assertFalse(session.flow_state["boxes"][0]["items"][0].get("barcode"))

    def test_complete_processing_flow_links_filled_boxes_to_matching_empty_pallet(self):
        order_id = "626-PALLET-FALLBACK"
        entries = self._create_entries(order_id)
        packaging_task = Task.objects.create(
            title="Формирование коробов",
            route=f"/orders/processing/{order_id}/flow/",
            assigned_to=Employee.objects.get(user=self.user),
            status="in_progress",
        )
        owner_id = str(self.user.id)
        ProcessingFlowSession.objects.create(
            order_id=order_id,
            order_type="processing",
            agent_id="agent-owner",
            user=self.user,
            flow_state={
                "boxes": [
                    {
                        "code": "BOX-FILLED-1",
                        "items": [
                            {
                                "sku": "SKU-A",
                                "name": "Item",
                                "size": "42",
                                "barcode": self.barcode,
                                "qty": 10,
                            }
                        ],
                        "sealed": True,
                        "owner_user_id": owner_id,
                        "owner_agent_id": "agent-owner",
                        "owner_user_label": "Owner",
                    },
                    {
                        "code": "BOX-EMPTY-1",
                        "items": [],
                        "sealed": False,
                        "owner_user_id": owner_id,
                        "owner_agent_id": "agent-owner",
                        "owner_user_label": "Owner",
                    },
                ],
                "pallets": [
                    {
                        "code": "PAL-EMPTY-1",
                        "boxes": ["BOX-EMPTY-1"],
                        "items": [],
                        "sealed": False,
                        "owner_user_id": owner_id,
                        "owner_agent_id": "agent-owner",
                        "owner_user_label": "Owner",
                    },
                    {
                        "code": "PAL-OTHER-1",
                        "boxes": [],
                        "items": [],
                        "sealed": False,
                        "owner_user_id": "other-user",
                        "owner_agent_id": "agent-other",
                        "owner_user_label": "Other",
                    },
                ],
                "activeBox": "BOX-EMPTY-1",
                "activePallet": "PAL-EMPTY-1",
            },
        )
        request = self.request_factory.post(
            f"/orders/processing/{order_id}/flow/",
            data={"boxes_json": "[]", "pallets_json": "[]", "agent_id": "agent-owner"},
        )
        request.user = self.user

        with mock.patch(
            "processing_app.views._state_has_open_box_with_items",
            return_value=False,
        ):
            result = ProcessingWorkflowService.complete_processing_flow(
                order_id=order_id,
                entries=entries,
                request=request,
                normalize_flow_state=self._normalize_flow_state,
                items_from_placement_act=lambda _entries: [{"sku_code": "SKU-A", "name": "Item", "size": "42", "actual_qty": 10}],
                can_start=True,
                can_finish_with_mismatch=True,
                can_reassign_boxes=True,
            )

        self.assertEqual(result.status, "ok")
        latest_entry = OrderAuditEntry.objects.filter(order_id=order_id, order_type="processing").latest("id")
        self.assertEqual(
            latest_entry.payload.get("processing_stage"),
            PROCESSING_STAGE_QUALITY_CONTROL,
        )
        self.assertEqual(
            (latest_entry.payload.get("quality_review") or {}).get("status"),
            "pending",
        )
        saved_pallets = latest_entry.payload.get("act_pallets") or []
        self.assertEqual(len(saved_pallets), 1)
        self.assertEqual(saved_pallets[0]["code"], "PAL-EMPTY-1")
        self.assertEqual(saved_pallets[0]["boxes"], ["BOX-FILLED-1"])
        packaging_task.refresh_from_db()
        self.assertEqual(packaging_task.status, "done")

    def test_complete_processing_flow_rejects_unassigned_boxes(self):
        order_id = "627"
        entries = self._create_entries(order_id)
        request = self.request_factory.post(
            f"/orders/processing/{order_id}/flow/",
            data={
                "boxes_json": '[{"code":"BOX-1","items":[{"sku":"SKU-A","qty":10}]}]',
                "pallets_json": '[{"code":"PAL-1","boxes":[],"items":[{"sku":"SKU-X","qty":1}]}]',
            },
        )
        request.user = self.user

        with mock.patch("processing_app.views._state_has_open_box_with_items", return_value=False):
            result = ProcessingWorkflowService.complete_processing_flow(
                order_id=order_id,
                entries=entries,
                request=request,
                normalize_flow_state=self._normalize_flow_state,
                items_from_placement_act=lambda _entries: [{"sku_code": "SKU-A", "name": "Item", "size": "42", "actual_qty": 10}],
                can_start=True,
                can_finish_with_mismatch=True,
                can_reassign_boxes=True,
            )

        self.assertEqual(result.status, "error")
        self.assertEqual(result.error_code, "unassigned_boxes")

    def test_complete_processing_flow_records_shortage_fact_for_head_approval(self):
        order_id = "627-QTY"
        entries = self._create_entries(order_id)
        request = self.request_factory.post(
            f"/orders/processing/{order_id}/flow/",
            data={
                "boxes_json": json.dumps(
                    [
                        {
                            "code": "BOX-1",
                            "items": [
                                {
                                    "sku": "SKU-A",
                                    "name": "Item",
                                    "size": "42",
                                    "barcode": self.barcode,
                                    "qty": 7,
                                }
                            ],
                        }
                    ]
                ),
                "pallets_json": '[{"code":"PAL-1","boxes":["BOX-1"],"items":[]}]',
            },
        )
        request.user = self.user

        with mock.patch("processing_app.views._state_has_open_box_with_items", return_value=False):
            result = ProcessingWorkflowService.complete_processing_flow(
                order_id=order_id,
                entries=entries,
                request=request,
                normalize_flow_state=self._normalize_flow_state,
                items_from_placement_act=lambda _entries: [
                    {"sku_code": "SKU-A", "name": "Item", "size": "42", "actual_qty": 10}
                ],
                can_start=True,
                can_finish_with_mismatch=True,
                can_reassign_boxes=True,
            )

        self.assertEqual(result.status, "ok")
        completed = OrderAuditEntry.objects.filter(
            order_id=order_id,
            order_type="processing",
        ).latest("id")
        placement_item = (completed.payload.get("act_items") or [])[0]
        self.assertEqual(placement_item["actual_qty"], 7)
        self.assertEqual(placement_item["expected_qty"], 10)
        self.assertEqual(placement_item["factual_qty"], 7)
        discrepancy = next(
            row
            for row in completed.payload.get("discrepancy_items") or []
            if row.get("discrepancy_type") == "processing_output_quantity"
        )
        self.assertEqual(discrepancy["expected_qty"], 10)
        self.assertEqual(discrepancy["factual_qty"], 7)
        self.assertEqual(discrepancy["delta_qty"], -3)
        self.assertEqual(completed.payload.get("discrepancy_status"), "reported")
        self.assertEqual(
            (completed.payload.get(ProcessingWorkflowService.PROCESSING_WAREHOUSE_COMMIT_KEY) or {}).get("status"),
            "pending_head_approval",
        )

    def test_complete_processing_flow_records_surplus_fact_for_head_approval(self):
        order_id = "627-QTY-SURPLUS"
        entries = self._create_entries(order_id)
        source_payload = dict(entries[0].payload or {})
        source_payload["cards"][0]["rows"][0]["qty"] = "1"
        source_payload["processing_results"][0]["processed"] = "1"
        entries[0].payload = source_payload
        entries[0].save(update_fields=["payload"])
        request = self.request_factory.post(
            f"/orders/processing/{order_id}/flow/",
            data={
                "boxes_json": json.dumps(
                    [
                        {
                            "code": "BOX-SURPLUS-1",
                            "items": [
                                {
                                    "sku": "SKU-A",
                                    "name": "Item",
                                    "size": "42",
                                    "barcode": self.barcode,
                                    "qty": 30,
                                }
                            ],
                        }
                    ]
                ),
                "pallets_json": json.dumps(
                    [{"code": "PAL-SURPLUS-1", "boxes": ["BOX-SURPLUS-1"], "items": []}]
                ),
            },
        )
        request.user = self.user

        with mock.patch("processing_app.views._state_has_open_box_with_items", return_value=False):
            result = ProcessingWorkflowService.complete_processing_flow(
                order_id=order_id,
                entries=entries,
                request=request,
                normalize_flow_state=self._normalize_flow_state,
                items_from_placement_act=lambda _entries: [
                    {"sku_code": "SKU-A", "name": "Item", "size": "42", "actual_qty": 1}
                ],
                can_start=True,
                can_finish_with_mismatch=True,
                can_reassign_boxes=True,
            )

        self.assertEqual(result.status, "ok")
        completed = OrderAuditEntry.objects.filter(
            order_id=order_id,
            order_type="processing",
        ).latest("id")
        placement_item = (completed.payload.get("act_items") or [])[0]
        self.assertEqual(placement_item["actual_qty"], 30)
        self.assertEqual(placement_item["expected_qty"], 1)
        self.assertEqual(placement_item["factual_qty"], 30)
        discrepancy = next(
            row
            for row in completed.payload.get("discrepancy_items") or []
            if row.get("discrepancy_type") == "processing_output_quantity"
        )
        self.assertEqual(discrepancy["expected_qty"], 1)
        self.assertEqual(discrepancy["factual_qty"], 30)
        self.assertEqual(discrepancy["delta_qty"], 29)
        self.assertEqual(completed.payload.get("discrepancy_status"), "reported")
        self.assertEqual(
            (completed.payload.get(ProcessingWorkflowService.PROCESSING_WAREHOUSE_COMMIT_KEY) or {}).get("status"),
            "pending_head_approval",
        )

    def test_complete_processing_flow_matches_only_barcode_when_articles_differ(self):
        order_id = "627-BARCODE-IDENTITY"
        barcode = "2046279701017"
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="processing",
            action="status",
            agency=self.agency,
            payload={
                "status": "processing_in_work",
                "status_label": "Взята в работу",
                "processing_stage": PROCESSING_STAGE_UNBOXING_OPENED,
                "processing_stage_label": "открыта раскоробовка",
                "cards": [
                    {
                        "id": "card-n021",
                        "article": "N021",
                        "rows": [{"size": "0", "qty": "480", "barcode": barcode}],
                    }
                ],
                "processed_cards": ["card-n021"],
                "processing_results": [
                    {
                        "card_id": "card-n021",
                        "article": "N021",
                        "size": "0",
                        "processed": "480",
                        "result_barcode": barcode,
                    }
                ],
            },
        )
        entries = list(
            OrderAuditEntry.objects.filter(
                order_id=order_id,
                order_type="processing",
            ).order_by("created_at")
        )
        request = self.request_factory.post(
            f"/orders/processing/{order_id}/flow/",
            data={
                "boxes_json": json.dumps(
                    [
                        {
                            "code": "BOX-N021-1",
                            "items": [
                                {
                                    "sku": "\u043d\u043e\u0436021",
                                    "name": "\u041d\u043e\u0436",
                                    "size": "0",
                                    "barcode": barcode,
                                    "qty": 480,
                                }
                            ],
                        }
                    ]
                ),
                "pallets_json": json.dumps(
                    [{"code": "PAL-N021-1", "boxes": ["BOX-N021-1"], "items": []}]
                ),
            },
        )
        request.user = self.user

        with (
            mock.patch("processing_app.views._state_has_open_box_with_items", return_value=False),
            mock.patch("processing_app.services.WarehouseWritePathService.consume_processing_input"),
            mock.patch("processing_app.services.OperationalStockService.replace_order_placement"),
        ):
            result = ProcessingWorkflowService.complete_processing_flow(
                order_id=order_id,
                entries=entries,
                request=request,
                normalize_flow_state=self._normalize_flow_state,
                items_from_placement_act=lambda _entries: [],
                can_start=True,
                can_finish_with_mismatch=False,
                can_reassign_boxes=True,
            )

        self.assertEqual(result.status, "ok")
        completed = OrderAuditEntry.objects.filter(
            order_id=order_id,
            order_type="processing",
        ).latest("id")
        placement_item = (completed.payload.get("act_items") or [])[0]
        self.assertEqual(placement_item["barcode"], barcode)
        self.assertEqual(placement_item["actual_qty"], 480)

    def test_complete_processing_flow_rejects_different_product_barcode(self):
        order_id = "627-BARCODE-MISMATCH"
        entries = self._create_entries(order_id)
        request = self.request_factory.post(
            f"/orders/processing/{order_id}/flow/",
            data={
                "boxes_json": json.dumps(
                    [
                        {
                            "code": "BOX-WRONG-BARCODE",
                            "items": [
                                {
                                    "sku": "SKU-A",
                                    "name": "Item",
                                    "size": "42",
                                    "barcode": "BAR-OTHER",
                                    "qty": 10,
                                }
                            ],
                        }
                    ]
                ),
                "pallets_json": json.dumps(
                    [
                        {
                            "code": "PAL-WRONG-BARCODE",
                            "boxes": ["BOX-WRONG-BARCODE"],
                            "items": [],
                        }
                    ]
                ),
            },
        )
        request.user = self.user

        with mock.patch("processing_app.views._state_has_open_box_with_items", return_value=False):
            result = ProcessingWorkflowService.complete_processing_flow(
                order_id=order_id,
                entries=entries,
                request=request,
                normalize_flow_state=self._normalize_flow_state,
                items_from_placement_act=lambda _entries: [],
                can_start=True,
                can_finish_with_mismatch=False,
                can_reassign_boxes=True,
            )

        self.assertEqual(result.status, "error")
        self.assertEqual(result.error_code, "barcode_mismatch")

    def test_complete_processing_flow_does_not_recognize_box_by_article(self):
        order_id = "627-ARTICLE-NOT-IDENTITY"
        entries = self._create_entries(order_id)
        request = self.request_factory.post(
            f"/orders/processing/{order_id}/flow/",
            data={
                "boxes_json": json.dumps(
                    [
                        {
                            "code": "BOX-WITHOUT-BARCODE",
                            "items": [
                                {
                                    "sku": "SKU-A",
                                    "name": "Item",
                                    "size": "42",
                                    "qty": 10,
                                }
                            ],
                        }
                    ]
                ),
                "pallets_json": json.dumps(
                    [
                        {
                            "code": "PAL-WITHOUT-BARCODE",
                            "boxes": ["BOX-WITHOUT-BARCODE"],
                            "items": [],
                        }
                    ]
                ),
            },
        )
        request.user = self.user

        with mock.patch("processing_app.views._state_has_open_box_with_items", return_value=False):
            result = ProcessingWorkflowService.complete_processing_flow(
                order_id=order_id,
                entries=entries,
                request=request,
                normalize_flow_state=self._normalize_flow_state,
                items_from_placement_act=lambda _entries: [],
                can_start=True,
                can_finish_with_mismatch=False,
                can_reassign_boxes=True,
            )

        self.assertEqual(result.status, "error")
        self.assertEqual(result.error_code, "missing_item_barcode")

    def test_complete_processing_flow_rejects_fact_without_product_barcode(self):
        order_id = "627-MISSING-BARCODE"
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="processing",
            action="status",
            agency=self.agency,
            payload={
                "status": "processing_in_work",
                "status_label": "Взята в работу",
                "processing_stage": PROCESSING_STAGE_UNBOXING_OPENED,
                "processing_stage_label": "открыта раскоробовка",
                "cards": [
                    {
                        "id": "card-missing-barcode",
                        "article": "SKU-WITHOUT-BARCODE",
                        "rows": [{"size": "0", "qty": "10"}],
                    }
                ],
                "processed_cards": ["card-missing-barcode"],
                "processing_results": [
                    {
                        "card_id": "card-missing-barcode",
                        "article": "SKU-WITHOUT-BARCODE",
                        "size": "0",
                        "processed": "10",
                    }
                ],
            },
        )
        entries = list(
            OrderAuditEntry.objects.filter(
                order_id=order_id,
                order_type="processing",
            ).order_by("created_at")
        )
        request = self.request_factory.post(
            f"/orders/processing/{order_id}/flow/",
            data={"boxes_json": "[]", "pallets_json": "[]"},
        )
        request.user = self.user

        result = ProcessingWorkflowService.complete_processing_flow(
            order_id=order_id,
            entries=entries,
            request=request,
            normalize_flow_state=self._normalize_flow_state,
            items_from_placement_act=lambda _entries: [],
            can_start=True,
            can_finish_with_mismatch=False,
            can_reassign_boxes=True,
        )

        self.assertEqual(result.status, "error")
        self.assertEqual(result.error_code, "missing_item_barcode")

    def test_complete_processing_flow_handles_ten_thousand_units_without_quantity_mismatch(self):
        order_id = "627-10K"
        total_qty = 10_000
        boxes = [
            {
                "code": f"BOX-10K-{index:03d}",
                "items": [
                    {
                        "sku": "SKU-10K",
                        "name": "Товар 10K",
                        "size": "42",
                        "barcode": "BAR-10K",
                        "qty": 100,
                    }
                ],
            }
            for index in range(1, 101)
        ]
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="processing",
            action="status",
            agency=self.agency,
            payload={
                "status": "processing_in_work",
                "status_label": "Взята в работу",
                "processing_stage": PROCESSING_STAGE_UNBOXING_OPENED,
                "processing_stage_label": "открыта раскоробовка",
                "cards": [{"id": "card-10k", "article": "SKU-10K", "rows": [{"size": "42", "qty": str(total_qty)}]}],
                "processed_cards": ["card-10k"],
                "processing_results": [
                    {
                        "card_id": "card-10k",
                        "article": "SKU-10K",
                        "size": "42",
                        "destination": "-",
                        "processed": str(total_qty),
                        "result_barcode": "BAR-10K",
                    }
                ],
            },
        )
        entries = list(OrderAuditEntry.objects.filter(order_id=order_id, order_type="processing").order_by("created_at"))
        request = self.request_factory.post(
            f"/orders/processing/{order_id}/flow/",
            data={
                "boxes_json": json.dumps(boxes),
                "pallets_json": json.dumps(
                    [{"code": "PAL-10K-001", "boxes": [box["code"] for box in boxes], "items": []}]
                ),
            },
        )
        request.user = self.user

        with (
            mock.patch(
                "processing_app.views._state_has_open_box_with_items",
                return_value=False,
            ),
            mock.patch(
                "processing_app.services.WarehouseWritePathService.consume_processing_input",
            ),
            mock.patch(
                "processing_app.services.OperationalStockService.replace_order_placement",
            ),
        ):
            result = ProcessingWorkflowService.complete_processing_flow(
                order_id=order_id,
                entries=entries,
                request=request,
                normalize_flow_state=self._normalize_flow_state,
                items_from_placement_act=lambda _entries: [],
                can_start=True,
                can_finish_with_mismatch=False,
                can_reassign_boxes=True,
            )

        self.assertEqual(result.status, "ok")
        completed = OrderAuditEntry.objects.filter(order_id=order_id, order_type="processing").latest("id")
        self.assertEqual(
            completed.payload.get("processing_stage"),
            PROCESSING_STAGE_QUALITY_CONTROL,
        )
        placement_item = (completed.payload.get("act_items") or [])[0]
        self.assertEqual(placement_item["actual_qty"], total_qty)
        self.assertEqual(placement_item["box_qty"], 100)
        self.assertEqual(placement_item["pallet_qty"], 1)
        self.assertFalse(bool(completed.payload.get("discrepancy_detected")))

    def test_complete_processing_flow_persists_box_characteristics_to_act_and_container_truth(self):
        order_id = "627-DIMS"
        entries = self._create_entries(order_id)
        request = self.request_factory.post(
            f"/orders/processing/{order_id}/flow/",
            data={
                "boxes_json": json.dumps(
                    [
                        {
                            "code": "BOX-DIMS-COMPLETE-1",
                            "gross_weight_g": 2220,
                            "width_mm": 330,
                            "height_mm": 240,
                            "depth_mm": 450,
                            "items": [
                                {
                                    "sku": "SKU-A",
                                    "name": "Item",
                                    "size": "42",
                                    "barcode": self.barcode,
                                    "qty": 10,
                                }
                            ],
                        }
                    ]
                ),
                "pallets_json": '[{"code":"PAL-DIMS-COMPLETE-1","boxes":["BOX-DIMS-COMPLETE-1"],"items":[]}]',
            },
        )
        request.user = self.user

        with mock.patch("processing_app.views._state_has_open_box_with_items", return_value=False):
            result = ProcessingWorkflowService.complete_processing_flow(
                order_id=order_id,
                entries=entries,
                request=request,
                normalize_flow_state=self._normalize_flow_state,
                items_from_placement_act=lambda _entries: [{"sku_code": "SKU-A", "name": "Item", "size": "42", "actual_qty": 10}],
                can_start=True,
                can_finish_with_mismatch=True,
                can_reassign_boxes=True,
            )

        self.assertEqual(result.status, "ok")
        latest_entry = OrderAuditEntry.objects.filter(order_id=order_id, order_type="processing").latest("id")
        saved_box = (latest_entry.payload.get("act_boxes") or [])[0]
        self.assertEqual(saved_box["gross_weight_g"], 2220)
        self.assertEqual(saved_box["width_mm"], 330)
        self.assertEqual(saved_box["height_mm"], 240)
        self.assertEqual(saved_box["depth_mm"], 450)
        container = WarehouseContainer.objects.get(agency=self.agency, container_code="BOX-DIMS-COMPLETE-1")
        self.assertEqual(container.gross_weight_g, 2220)
        self.assertEqual(container.width_mm, 330)
        self.assertEqual(container.height_mm, 240)
        self.assertEqual(container.depth_mm, 450)

    def test_build_processing_flow_page_context_uses_locked_placement_state(self):
        order_id = "628"
        entries = self._create_entries(order_id, flow_closed=True)
        request = self.request_factory.get(f"/orders/processing/{order_id}/flow/")
        request.user = self.user

        context = ProcessingWorkflowService.build_processing_flow_page_context(
            order_id=order_id,
            entries=entries,
            request=request,
            can_finish_flow=True,
            can_finish_flow_mismatch=True,
            can_reassign_boxes=True,
            is_directional_unboxing_payload=lambda payload: False,
            items_from_placement_act=lambda _entries: [{"sku_code": "SKU-A", "name": "Item", "size": "42", "actual_qty": 10}],
            normalize_flow_state=self._normalize_flow_state,
            find_flow_state=lambda _entries: {},
            placement_act_entry=lambda _entries: _entries[-1],
            ok=False,
            error="",
        )

        self.assertTrue(context["flow_locked"])
        self.assertEqual(len(context["flow_state"]["boxes"]), 1)
        self.assertEqual(context["items"][0]["barcode"], self.barcode.lower())
        self.assertEqual(context["act_print_url"], f"/orders/processing/{order_id}/placement/?return=/orders/processing/{order_id}/flow/")


    def test_build_processing_flow_page_context_includes_known_box_metrics(self):
        history_item = {
            "sku": "SKU-A",
            "name": "Item",
            "size": "42",
            "barcode": "BC-PROC-HISTORY-1",
            "goods_type": "gv",
            "qty": 10,
        }
        OrderAuditEntry.objects.create(
            order_id="628-HISTORY-METRICS",
            order_type="processing",
            action="update",
            agency=self.agency,
            payload={
                "act": "placement",
                "act_state": "closed",
                "act_boxes": [
                    {
                        "code": "BOX-PROC-HISTORY-1",
                        "gross_weight_g": 2330,
                        "width_mm": 340,
                        "height_mm": 250,
                        "depth_mm": 460,
                        "items": [history_item],
                    }
                ],
            },
        )
        order_id = "628-METRICS-CONTEXT"
        entries = self._create_entries(order_id, flow_closed=True)
        request = self.request_factory.get(f"/orders/processing/{order_id}/flow/")
        request.user = self.user

        context = ProcessingWorkflowService.build_processing_flow_page_context(
            order_id=order_id,
            entries=entries,
            request=request,
            can_finish_flow=True,
            can_finish_flow_mismatch=True,
            can_reassign_boxes=True,
            is_directional_unboxing_payload=lambda payload: False,
            items_from_placement_act=lambda _entries: [{"sku_code": "SKU-A", "name": "Item", "size": "42", "actual_qty": 10}],
            normalize_flow_state=self._normalize_flow_state,
            find_flow_state=lambda _entries: {},
            placement_act_entry=lambda _entries: _entries[-1],
            ok=False,
            error="",
        )
        expected_key = ReceivingWorkflowService._build_box_metric_lookup_key(
            {"items": [history_item]},
            mode="processing",
        )

        self.assertEqual(context["known_box_metrics"][expected_key]["gross_weight_g"], 2330)
        self.assertEqual(context["known_box_metrics"][expected_key]["width_mm"], 340)
        self.assertEqual(context["known_box_metrics"][expected_key]["height_mm"], 250)
        self.assertEqual(context["known_box_metrics"][expected_key]["depth_mm"], 460)

class ProcessingBoxMarkingModeTests(TestCase):
    marking_code = "010460123456789321SERIAL001"

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="processing_box_marking_worker",
            password="x",
        )
        Employee.objects.create(
            full_name="Processing Box Marking Worker",
            user=self.user,
            role="processing_worker",
            is_active=True,
        )
        self.agency = Agency.objects.create(agn_name="Processing Box Marking Agency")
        self.sku = SKU.objects.create(
            agency=self.agency,
            sku_code="SKU-CZ",
            name="Товар с ЧЗ",
            size="42",
            honest_sign=True,
        )
        self.barcode = SKUBarcode.objects.create(
            sku=self.sku,
            value="4601234567893",
            size="42",
            is_primary=True,
        )
        self.request_factory = RequestFactory()

    @staticmethod
    def _normalize_flow_state(boxes, pallets, active_box, active_pallet):
        return {
            "boxes": boxes,
            "pallets": pallets,
            "activeBox": active_box,
            "activePallet": active_pallet,
        }

    def _create_entries(self, order_id: str):
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="processing",
            action="status",
            agency=self.agency,
            payload={
                "status": "processing_in_work",
                "status_label": "Взята в работу",
                "processing_stage": PROCESSING_STAGE_UNBOXING_OPENED,
                "processing_stage_label": "открыта раскоробовка",
                "marking_5840_each_qty": "1",
                "cards": [
                    {
                        "id": "card-cz",
                        "article": self.sku.sku_code,
                        "rows": [{"size": self.sku.size, "qty": "10"}],
                    }
                ],
                "processed_cards": ["card-cz"],
                "processing_results": [
                    {
                        "card_id": "card-cz",
                        "article": self.sku.sku_code,
                        "size": self.sku.size,
                        "destination": "-",
                        "processed": "10",
                    }
                ],
            },
        )
        return list(
            OrderAuditEntry.objects.filter(
                order_id=order_id,
                order_type="processing",
            ).order_by("created_at")
        )

    def _scan_request(
        self,
        order_id: str,
        box_barcode: str,
        *,
        marking_code: str | None = None,
        product_barcode: str | None = None,
    ):
        request = self.request_factory.post(
            f"/orders/processing/{order_id}/flow/marking-scan/",
            data=json.dumps(
                {
                    "code": marking_code or self.marking_code,
                    "product_barcode": product_barcode or self.barcode.value,
                    "worker_marking_mode": "with_cz",
                    "box_barcode": box_barcode,
                    "agent_id": "agent-cz",
                }
            ),
            content_type="application/json",
        )
        request.user = self.user
        return request

    def test_box_validation_requires_one_unique_marking_per_cz_unit(self):
        valid_cz = ProcessingWorkflowService.inspect_processing_box_marking_mode(
            {
                "code": "BOX-CZ",
                "marking_mode": "with_cz",
                "items": [
                    {
                        "sku": self.sku.sku_code,
                        "size": self.sku.size,
                        "qty": 1,
                        "marking_codes": [self.marking_code],
                    }
                ],
            }
        )
        valid_without_cz = ProcessingWorkflowService.inspect_processing_box_marking_mode(
            {
                "code": "BOX-NO-CZ",
                "marking_mode": "without_cz",
                "items": [{"sku": self.sku.sku_code, "size": self.sku.size, "qty": 2}],
            }
        )
        mixed = ProcessingWorkflowService.inspect_processing_box_marking_mode(
            {
                "code": "BOX-MIXED",
                "marking_mode": "with_cz",
                "items": [
                    {
                        "sku": self.sku.sku_code,
                        "size": self.sku.size,
                        "qty": 2,
                        "marking_codes": [self.marking_code],
                    }
                ],
            }
        )

        self.assertTrue(valid_cz["ok"])
        self.assertTrue(valid_without_cz["ok"])
        self.assertFalse(mixed["ok"])
        self.assertEqual(mixed["qty"], 2)
        self.assertEqual(mixed["marking_count"], 1)

    def test_save_draft_rejects_mixed_cz_box(self):
        order_id = "FLOW-CZ-DRAFT"
        entries = self._create_entries(order_id)
        request = self.request_factory.post(
            f"/orders/processing/{order_id}/flow/",
            data={
                "boxes_json": json.dumps(
                    [
                        {
                            "code": "BOX-MIXED",
                            "marking_mode": "with_cz",
                            "items": [
                                {
                                    "sku": self.sku.sku_code,
                                    "size": self.sku.size,
                                    "qty": 2,
                                    "marking_codes": [self.marking_code],
                                }
                            ],
                        }
                    ]
                ),
                "pallets_json": "[]",
                "active_box": "BOX-MIXED",
                "active_pallet": "",
                "agent_id": "agent-cz",
            },
        )
        request.user = self.user

        with mock.patch("processing_app.views.ProcessingFlowView._can_start", return_value=True):
            result = ProcessingWorkflowService.save_processing_flow_draft(
                order_id=order_id,
                entries=entries,
                request=request,
                normalize_flow_state=self._normalize_flow_state,
                can_reassign_boxes=True,
            )

        self.assertEqual(result.status, "marking_mode_conflict")
        session = ProcessingFlowSession.objects.get(order_id=order_id, agent_id="agent-cz")
        self.assertEqual(session.flow_state.get("boxes"), [])

    def test_scan_rejects_cz_in_box_with_unmarked_goods(self):
        order_id = "FLOW-CZ-MIX"
        self._create_entries(order_id)
        ProcessingFlowSession.objects.create(
            order_id=order_id,
            order_type="processing",
            agent_id="agent-cz",
            user=self.user,
            flow_state={
                "boxes": [
                    {
                        "code": "BOX-NO-CZ",
                        "marking_mode": "without_cz",
                        "items": [
                            {
                                "sku": self.sku.sku_code,
                                "size": self.sku.size,
                                "qty": 1,
                            }
                        ],
                        "sealed": False,
                    }
                ],
                "pallets": [],
                "activeBox": "BOX-NO-CZ",
                "activePallet": "",
            },
        )

        with mock.patch(
            "processing_app.views._processing_receiving_items",
            return_value=[
                {
                    "sku_code": self.sku.sku_code,
                    "name": self.sku.name,
                    "size": self.sku.size,
                    "actual_qty": 10,
                }
            ],
        ):
            result = ProcessingWorkflowService.scan_processing_flow_marking(
                order_id=order_id,
                request=self._scan_request(order_id, "BOX-NO-CZ"),
            )

        self.assertEqual(result.status, "box_marking_mode_conflict")
        self.assertEqual(result.http_status, 409)
        self.assertIn("уже есть товар без ЧЗ", result.payload["error"])
        self.assertFalse(MarkingCode.objects.filter(code=self.marking_code).exists())

    def test_duplicate_cz_returns_original_box_and_order(self):
        order_id = "FLOW-CZ-DUPLICATE"
        self._create_entries(order_id)
        allowed_items = [
            {
                "sku_code": self.sku.sku_code,
                "name": self.sku.name,
                "size": self.sku.size,
                "actual_qty": 10,
            }
        ]

        with mock.patch(
            "processing_app.views._processing_receiving_items",
            return_value=allowed_items,
        ):
            first = ProcessingWorkflowService.scan_processing_flow_marking(
                order_id=order_id,
                request=self._scan_request(order_id, "BOX-CZ-ORIGINAL"),
            )
            duplicate = ProcessingWorkflowService.scan_processing_flow_marking(
                order_id=order_id,
                request=self._scan_request(order_id, "BOX-CZ-SECOND"),
            )

        self.assertEqual(first.status, "ok")
        self.assertEqual(duplicate.status, "already_used")
        self.assertEqual(duplicate.http_status, 409)
        self.assertEqual(duplicate.payload["used_box_barcode"], "BOX-CZ-ORIGINAL")
        self.assertEqual(duplicate.payload["used_order_id"], order_id)
        self.assertIn("BOX-CZ-ORIGINAL", duplicate.payload["error"])
        session = ProcessingFlowSession.objects.get(order_id=order_id, agent_id="agent-cz")
        original_box = next(
            box
            for box in session.flow_state["boxes"]
            if box["code"] == "BOX-CZ-ORIGINAL"
        )
        self.assertEqual(original_box["marking_mode"], "with_cz")
        self.assertEqual(original_box["items"][0]["qty"], 1)
        self.assertEqual(original_box["items"][0]["marking_codes"], [self.marking_code])

    def test_scan_accepts_valid_gtin_without_matching_product_barcode(self):
        order_id = "FLOW-CZ-GTIN-MISMATCH"
        self._create_entries(order_id)
        mismatched_code = "010467051345448321WRONGGTIN001"

        with mock.patch(
            "processing_app.views._processing_receiving_items",
            return_value=[
                {
                    "sku_code": self.sku.sku_code,
                    "name": self.sku.name,
                    "size": self.sku.size,
                    "actual_qty": 10,
                }
            ],
        ):
            result = ProcessingWorkflowService.scan_processing_flow_marking(
                order_id=order_id,
                request=self._scan_request(
                    order_id,
                    "BOX-CZ-GTIN-MISMATCH",
                    marking_code=mismatched_code,
                ),
            )

        self.assertEqual(result.status, "ok")
        self.assertEqual(result.http_status, 200)
        marking_row = MarkingCode.objects.get(code=mismatched_code)
        self.assertEqual(marking_row.sku_id, self.sku.id)
        self.assertEqual(marking_row.sku_code, self.sku.sku_code)
        self.assertEqual(marking_row.barcode, self.barcode.value)
        self.assertEqual(marking_row.box_barcode, "BOX-CZ-GTIN-MISMATCH")
        session = ProcessingFlowSession.objects.get(
            order_id=order_id,
            agent_id="agent-cz",
        )
        self.assertEqual(session.flow_state["boxes"][0]["items"][0]["qty"], 1)
        self.assertEqual(
            session.flow_state["boxes"][0]["items"][0]["marking_codes"],
            [mismatched_code],
        )

    def test_scan_still_rejects_invalid_gtin_checksum(self):
        order_id = "FLOW-CZ-GTIN-INVALID"
        self._create_entries(order_id)
        invalid_code = "010467051345448421INVALIDGTIN001"

        with mock.patch(
            "processing_app.views._processing_receiving_items",
            return_value=[
                {
                    "sku_code": self.sku.sku_code,
                    "name": self.sku.name,
                    "size": self.sku.size,
                    "actual_qty": 10,
                }
            ],
        ):
            result = ProcessingWorkflowService.scan_processing_flow_marking(
                order_id=order_id,
                request=self._scan_request(
                    order_id,
                    "BOX-CZ-GTIN-INVALID",
                    marking_code=invalid_code,
                ),
            )

        self.assertEqual(result.status, "invalid_marking_gtin")
        self.assertEqual(result.http_status, 409)
        self.assertFalse(MarkingCode.objects.filter(code=invalid_code).exists())

    def test_scan_still_rejects_code_registered_for_another_sku(self):
        order_id = "FLOW-CZ-OTHER-SKU"
        self._create_entries(order_id)
        other_sku = SKU.objects.create(
            agency=self.agency,
            sku_code="SKU-CZ-OTHER",
            name="Другой товар с ЧЗ",
            size="42",
            honest_sign=True,
        )
        other_barcode = SKUBarcode.objects.create(
            sku=other_sku,
            value="4670513454483",
            size="42",
            is_primary=True,
        )
        registered_code = "010467051345448321REGISTEREDOTHER001"
        MarkingCode.objects.create(
            order_type="processing",
            order_id=order_id,
            agency=self.agency,
            sku=other_sku,
            sku_code=other_sku.sku_code,
            size=other_sku.size,
            barcode=other_barcode.value,
            code=registered_code,
            source="import",
        )

        with mock.patch(
            "processing_app.views._processing_receiving_items",
            return_value=[
                {
                    "sku_code": self.sku.sku_code,
                    "name": self.sku.name,
                    "size": self.sku.size,
                    "actual_qty": 10,
                }
            ],
        ):
            result = ProcessingWorkflowService.scan_processing_flow_marking(
                order_id=order_id,
                request=self._scan_request(
                    order_id,
                    "BOX-CZ-OTHER-SKU",
                    marking_code=registered_code,
                ),
            )

        self.assertEqual(result.status, "marking_barcode_mismatch")
        self.assertEqual(result.http_status, 409)
        self.assertEqual(MarkingCode.objects.get(code=registered_code).sku_id, other_sku.id)

    def test_scan_accepts_alternate_registered_gtin_for_same_sku(self):
        order_id = "FLOW-CZ-GTIN-ALTERNATE"
        self._create_entries(order_id)
        alternate_barcode = SKUBarcode.objects.create(
            sku=self.sku,
            value="4670513454483",
            size="42",
            is_primary=False,
        )
        alternate_code = "010467051345448321ALTERNATE001"

        with mock.patch(
            "processing_app.views._processing_receiving_items",
            return_value=[
                {
                    "sku_code": self.sku.sku_code,
                    "name": self.sku.name,
                    "size": self.sku.size,
                    "actual_qty": 10,
                }
            ],
        ):
            result = ProcessingWorkflowService.scan_processing_flow_marking(
                order_id=order_id,
                request=self._scan_request(
                    order_id,
                    "BOX-CZ-GTIN-ALTERNATE",
                    marking_code=alternate_code,
                    product_barcode=alternate_barcode.value,
                ),
            )

        self.assertEqual(result.status, "ok")
        self.assertEqual(result.payload["marking_code"], alternate_code)
        marking_row = MarkingCode.objects.get(code=alternate_code)
        self.assertEqual(marking_row.sku_id, self.sku.id)
        self.assertEqual(marking_row.box_barcode, "BOX-CZ-GTIN-ALTERNATE")


class ProcessingSafeBoxCancellationTests(TestCase):
    marking_code = "010460123456700821CANCEL001"

    def setUp(self):
        user_model = get_user_model()
        self.head_user = user_model.objects.create_user(
            username="processing_safe_cancel_head",
            password="x",
        )
        Employee.objects.create(
            full_name="Руководитель обработки",
            user=self.head_user,
            role="processing_head",
            is_active=True,
        )
        self.warehouse_head_user = user_model.objects.create_user(
            username="processing_safe_cancel_warehouse_head",
            password="x",
        )
        Employee.objects.create(
            full_name="Начальник склада",
            user=self.warehouse_head_user,
            role="head_manager",
            is_active=True,
        )
        self.worker_user = user_model.objects.create_user(
            username="processing_safe_cancel_worker",
            password="x",
        )
        Employee.objects.create(
            full_name="Обработчик",
            user=self.worker_user,
            role="processing_worker",
            is_active=True,
        )
        self.agency = Agency.objects.create(
            agn_name="Processing Safe Cancel Agency"
        )
        self.sku = SKU.objects.create(
            agency=self.agency,
            sku_code="SKU-CANCEL-CZ",
            name="Товар для отмены короба",
            size="42",
            honest_sign=True,
        )
        self.barcode = SKUBarcode.objects.create(
            sku=self.sku,
            value="4601234567008",
            size="42",
            is_primary=True,
        )
        self.request_factory = RequestFactory()

    @staticmethod
    def _normalize_flow_state(boxes, pallets, active_box, active_pallet):
        return {
            "boxes": boxes,
            "pallets": pallets,
            "activeBox": active_box,
            "activePallet": active_pallet,
        }

    def _create_box_state(
        self,
        order_id: str,
        *,
        box_code: str,
        marking_code: str | None = None,
        marking_mode: str = "with_cz",
    ):
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="processing",
            action="status",
            agency=self.agency,
            payload={
                "status": "processing_in_work",
                "status_label": "Взята в работу",
                "processing_stage": PROCESSING_STAGE_UNBOXING_OPENED,
                "processing_stage_label": "открыта раскоробовка",
                "cards": [
                    {
                        "id": "card-cancel",
                        "article": self.sku.sku_code,
                        "rows": [{"size": self.sku.size, "qty": "10"}],
                    }
                ],
                "processed_cards": ["card-cancel"],
                "processing_results": [
                    {
                        "card_id": "card-cancel",
                        "article": self.sku.sku_code,
                        "size": self.sku.size,
                        "destination": "-",
                        "processed": "10",
                    }
                ],
            },
        )
        item = {
            "sku_code": self.sku.sku_code,
            "sku": self.sku.sku_code,
            "name": self.sku.name,
            "size": self.sku.size,
            "barcode": self.barcode.value,
            "qty": 1,
        }
        if marking_code:
            item["marking_codes"] = [marking_code]
        session = ProcessingFlowSession.objects.create(
            order_id=order_id,
            order_type="processing",
            agent_id=f"agent-{order_id}",
            user=self.worker_user,
            status=ProcessingFlowSession.STATUS_OPEN,
            flow_state={
                "boxes": [
                    {
                        "code": box_code,
                        "items": [item],
                        "sealed": True,
                        "marking_mode": marking_mode,
                        "owner_agent_id": f"agent-{order_id}",
                        "owner_user_id": self.worker_user.id,
                        "owner_user_label": "Обработчик",
                    }
                ],
                "pallets": [
                    {
                        "code": f"PAL-{order_id}",
                        "boxes": [box_code],
                        "items": [],
                        "sealed": False,
                        "owner_agent_id": f"agent-{order_id}",
                        "owner_user_id": self.worker_user.id,
                        "owner_user_label": "Обработчик",
                    }
                ],
                "activeBox": "",
                "activePallet": f"PAL-{order_id}",
            },
        )
        pallet = WarehouseContainer.objects.create(
            agency=self.agency,
            container_type=WarehouseContainer.TYPE_PALLET,
            container_code=f"PAL-{order_id}",
            source_context_type="processing",
            source_context_id=order_id,
        )
        container = WarehouseContainer.objects.create(
            agency=self.agency,
            container_type=WarehouseContainer.TYPE_BOX,
            container_code=box_code,
            parent_container=pallet,
            source_context_type="processing",
            source_context_id=order_id,
        )
        marking_row = None
        if marking_code:
            marking_row = MarkingCode.objects.create(
                order_type="processing",
                order_id=order_id,
                agency=self.agency,
                sku=self.sku,
                sku_code=self.sku.sku_code,
                size=self.sku.size,
                barcode=self.barcode.value,
                box_barcode=box_code,
                code=marking_code,
                source="scan",
                created_by=self.worker_user,
                used_at=timezone.localtime(),
                used_by=self.worker_user,
            )
        return session, container, marking_row

    def _cancel_request(self, order_id: str, box_code: str, *, user=None):
        request = self.request_factory.post(
            f"/orders/processing/{order_id}/flow/cancel-box/",
            data=json.dumps(
                {
                    "box_code": box_code,
                    "reason": "Ошибочно сформирован короб",
                }
            ),
            content_type="application/json",
        )
        request.user = user or self.head_user
        return request

    def _scan_request(self, order_id: str, box_code: str):
        request = self.request_factory.post(
            f"/orders/processing/{order_id}/flow/scan/",
            data=json.dumps(
                {
                    "code": self.marking_code,
                    "product_barcode": self.barcode.value,
                    "worker_marking_mode": "with_cz",
                    "box_barcode": box_code,
                    "agent_id": f"agent-{order_id}",
                }
            ),
            content_type="application/json",
        )
        request.user = self.worker_user
        return request

    def test_head_cancels_box_releases_marking_and_preserves_history(self):
        order_id = "SAFE-CANCEL-1"
        box_code = "BOX-SAFE-CANCEL-1"
        session, container, marking_row = self._create_box_state(
            order_id,
            box_code=box_code,
            marking_code=self.marking_code,
        )

        result = ProcessingWorkflowService.cancel_processing_flow_box(
            order_id=order_id,
            request=self._cancel_request(order_id, box_code),
        )

        self.assertEqual(result.status, "ok")
        self.assertEqual(result.payload["released_marking_count"], 1)
        marking_row.refresh_from_db()
        self.assertIsNone(marking_row.used_at)
        self.assertIsNone(marking_row.used_by)
        self.assertEqual(marking_row.box_barcode, "")
        session.refresh_from_db()
        self.assertEqual(session.flow_state["boxes"], [])
        self.assertEqual(session.flow_state["pallets"][0]["boxes"], [])
        container.refresh_from_db()
        self.assertEqual(container.status, WarehouseContainer.STATUS_ARCHIVED)
        self.assertIsNone(container.parent_container)
        cancellation_entry = OrderAuditEntry.objects.filter(
            order_id=order_id,
            payload__has_key="processing_box_cancellation",
        ).latest("id")
        cancellation = cancellation_entry.payload["processing_box_cancellation"]
        self.assertEqual(cancellation["box_code"], box_code)
        self.assertEqual(cancellation["flow_marking_codes"], [self.marking_code])
        self.assertEqual(
            cancellation["released_marking_rows"][0]["id"],
            marking_row.id,
        )
        self.assertEqual(cancellation["reason"], "Ошибочно сформирован короб")
        self.assertTrue(
            AuditEntry.objects.filter(
                journal__code="staff_overactions",
                snapshot__box_code=box_code,
            ).exists()
        )

        repeated = ProcessingWorkflowService.cancel_processing_flow_box(
            order_id=order_id,
            request=self._cancel_request(order_id, box_code),
        )
        self.assertEqual(repeated.status, "already_canceled")
        self.assertTrue(repeated.payload["already_canceled"])

    def test_worker_cannot_cancel_box(self):
        order_id = "SAFE-CANCEL-FORBIDDEN"
        box_code = "BOX-SAFE-CANCEL-FORBIDDEN"
        session, _container, marking_row = self._create_box_state(
            order_id,
            box_code=box_code,
            marking_code=self.marking_code,
        )

        result = ProcessingWorkflowService.cancel_processing_flow_box(
            order_id=order_id,
            request=self._cancel_request(
                order_id,
                box_code,
                user=self.worker_user,
            ),
        )

        self.assertEqual(result.status, "forbidden")
        self.assertEqual(result.http_status, 403)
        marking_row.refresh_from_db()
        self.assertIsNotNone(marking_row.used_at)
        session.refresh_from_db()
        self.assertEqual(session.flow_state["boxes"][0]["code"], box_code)

    def test_warehouse_head_can_cancel_box(self):
        order_id = "SAFE-CANCEL-WAREHOUSE-HEAD"
        box_code = "BOX-SAFE-CANCEL-WAREHOUSE-HEAD"
        _session, container, _marking_row = self._create_box_state(
            order_id,
            box_code=box_code,
            marking_mode="without_cz",
        )

        result = ProcessingWorkflowService.cancel_processing_flow_box(
            order_id=order_id,
            request=self._cancel_request(
                order_id,
                box_code,
                user=self.warehouse_head_user,
            ),
        )

        self.assertEqual(result.status, "ok")
        container.refresh_from_db()
        self.assertEqual(container.status, WarehouseContainer.STATUS_ARCHIVED)

    def test_cancellation_is_blocked_after_box_reaches_stock(self):
        order_id = "SAFE-CANCEL-STOCK"
        box_code = "BOX-SAFE-CANCEL-STOCK"
        session, _container, marking_row = self._create_box_state(
            order_id,
            box_code=box_code,
            marking_code=self.marking_code,
        )
        create_warehouse_snapshot_row(
            agency=self.agency,
            order_type="processing",
            order_id=order_id,
            sku=self.sku.sku_code,
            name=self.sku.name,
            size=self.sku.size,
            barcode=self.barcode.value,
            marking_code=self.marking_code,
            box_code=box_code,
            pallet_code=f"PAL-{order_id}",
        )

        result = ProcessingWorkflowService.cancel_processing_flow_box(
            order_id=order_id,
            request=self._cancel_request(order_id, box_code),
        )

        self.assertEqual(result.status, "warehouse_started")
        self.assertEqual(result.http_status, 409)
        marking_row.refresh_from_db()
        self.assertIsNotNone(marking_row.used_at)
        session.refresh_from_db()
        self.assertEqual(session.flow_state["boxes"][0]["code"], box_code)
        self.assertFalse(
            OrderAuditEntry.objects.filter(
                order_id=order_id,
                payload__has_key="processing_box_cancellation",
            ).exists()
        )

    def test_canceled_box_is_blocked_but_released_cz_scans_into_new_box(self):
        order_id = "SAFE-CANCEL-RESCAN"
        old_box_code = "BOX-SAFE-CANCEL-OLD"
        self._create_box_state(
            order_id,
            box_code=old_box_code,
            marking_code=self.marking_code,
        )
        cancellation = ProcessingWorkflowService.cancel_processing_flow_box(
            order_id=order_id,
            request=self._cancel_request(order_id, old_box_code),
        )
        self.assertEqual(cancellation.status, "ok")

        allowed_items = [
            {
                "sku_code": self.sku.sku_code,
                "name": self.sku.name,
                "size": self.sku.size,
                "actual_qty": 10,
            }
        ]
        with mock.patch(
            "processing_app.views._processing_receiving_items",
            return_value=allowed_items,
        ):
            stale_scan = ProcessingWorkflowService.scan_processing_flow_marking(
                order_id=order_id,
                request=self._scan_request(order_id, old_box_code),
            )
            new_scan = ProcessingWorkflowService.scan_processing_flow_marking(
                order_id=order_id,
                request=self._scan_request(order_id, "BOX-SAFE-CANCEL-NEW"),
            )

        self.assertEqual(stale_scan.status, "box_canceled")
        self.assertEqual(stale_scan.http_status, 409)
        self.assertIn(old_box_code, stale_scan.payload["error"])
        self.assertEqual(new_scan.status, "ok")
        marking_row = MarkingCode.objects.get(code=self.marking_code)
        self.assertEqual(marking_row.box_barcode, "BOX-SAFE-CANCEL-NEW")
        self.assertIsNotNone(marking_row.used_at)

    def test_stale_draft_cannot_restore_canceled_box(self):
        order_id = "SAFE-CANCEL-DRAFT"
        box_code = "BOX-SAFE-CANCEL-DRAFT"
        self._create_box_state(
            order_id,
            box_code=box_code,
            marking_code=self.marking_code,
        )
        entries = list(
            OrderAuditEntry.objects.filter(
                order_id=order_id,
                order_type="processing",
            ).order_by("created_at")
        )
        stale_box = {
            "code": box_code,
            "items": [
                {
                    "sku_code": self.sku.sku_code,
                    "size": self.sku.size,
                    "qty": 1,
                    "marking_codes": [self.marking_code],
                }
            ],
            "sealed": True,
            "marking_mode": "with_cz",
        }
        cancellation = ProcessingWorkflowService.cancel_processing_flow_box(
            order_id=order_id,
            request=self._cancel_request(order_id, box_code),
        )
        self.assertEqual(cancellation.status, "ok")
        request = self.request_factory.post(
            f"/orders/processing/{order_id}/flow/",
            data={
                "boxes_json": json.dumps([stale_box]),
                "pallets_json": "[]",
                "active_box": box_code,
                "active_pallet": "",
                "agent_id": f"agent-{order_id}",
            },
        )
        request.user = self.worker_user

        with mock.patch(
            "processing_app.views.ProcessingFlowView._can_start",
            return_value=True,
        ):
            result = ProcessingWorkflowService.save_processing_flow_draft(
                order_id=order_id,
                entries=entries,
                request=request,
                normalize_flow_state=self._normalize_flow_state,
                can_reassign_boxes=False,
            )

        self.assertEqual(result.status, "ok")
        session = ProcessingFlowSession.objects.get(
            order_id=order_id,
            agent_id=f"agent-{order_id}",
        )
        self.assertEqual(session.flow_state["boxes"], [])

    def test_shared_flow_purges_canceled_box_from_every_open_session(self):
        order_id = "SAFE-CANCEL-SHARED-PURGE"
        box_code = "BOX-SAFE-CANCEL-SHARED-PURGE"
        self._create_box_state(
            order_id,
            box_code=box_code,
            marking_code=self.marking_code,
        )
        cancellation = ProcessingWorkflowService.cancel_processing_flow_box(
            order_id=order_id,
            request=self._cancel_request(order_id, box_code),
        )
        self.assertEqual(cancellation.status, "ok")
        stale_session = ProcessingFlowSession.objects.create(
            order_id=order_id,
            order_type="processing",
            agent_id="stale-agent",
            user=self.worker_user,
            status=ProcessingFlowSession.STATUS_OPEN,
            flow_state={
                "boxes": [
                    {
                        "code": box_code,
                        "items": [
                            {
                                "sku_code": self.sku.sku_code,
                                "size": self.sku.size,
                                "qty": 1,
                                "marking_codes": [self.marking_code],
                            }
                        ],
                        "sealed": True,
                        "marking_mode": "with_cz",
                    }
                ],
                "pallets": [
                    {
                        "code": "PAL-STALE",
                        "boxes": [box_code],
                        "items": [],
                        "sealed": False,
                    }
                ],
                "activeBox": box_code,
                "activePallet": "PAL-STALE",
            },
        )

        result = ProcessingWorkflowService.get_processing_flow_shared_state(
            order_id=order_id,
        )

        self.assertEqual(result.flow_state["boxes"], [])
        self.assertEqual(result.flow_state["pallets"][0]["boxes"], [])
        stale_session.refresh_from_db()
        self.assertEqual(stale_session.flow_state["boxes"], [])
        self.assertEqual(stale_session.flow_state["pallets"][0]["boxes"], [])
        self.assertEqual(stale_session.flow_state["activeBox"], "")

    def test_close_box_cannot_restore_unrelated_canceled_box(self):
        order_id = "SAFE-CANCEL-CLOSE-BOX"
        canceled_box_code = "BOX-SAFE-CANCEL-CLOSE-OLD"
        new_box_code = "BOX-SAFE-CANCEL-CLOSE-NEW"
        self._create_box_state(
            order_id,
            box_code=canceled_box_code,
            marking_code=self.marking_code,
        )
        cancellation = ProcessingWorkflowService.cancel_processing_flow_box(
            order_id=order_id,
            request=self._cancel_request(order_id, canceled_box_code),
        )
        self.assertEqual(cancellation.status, "ok")
        request = self.request_factory.post(
            f"/orders/processing/{order_id}/flow/close-box/",
            data=json.dumps(
                {
                    "box_code": new_box_code,
                    "boxes": [
                        {
                            "code": canceled_box_code,
                            "items": [
                                {
                                    "sku_code": self.sku.sku_code,
                                    "size": self.sku.size,
                                    "qty": 1,
                                    "marking_codes": [self.marking_code],
                                }
                            ],
                            "sealed": True,
                            "marking_mode": "with_cz",
                        },
                        {
                            "code": new_box_code,
                            "items": [
                                {
                                    "sku_code": self.sku.sku_code,
                                    "size": self.sku.size,
                                    "qty": 1,
                                }
                            ],
                            "sealed": False,
                            "marking_mode": "without_cz",
                        },
                    ],
                    "pallets": [
                        {
                            "code": f"PAL-{order_id}",
                            "boxes": [canceled_box_code, new_box_code],
                            "items": [],
                            "sealed": False,
                        }
                    ],
                    "active_box": new_box_code,
                    "active_pallet": f"PAL-{order_id}",
                    "agent_id": f"agent-{order_id}",
                }
            ),
            content_type="application/json",
        )
        request.user = self.worker_user

        response = processing_flow_close_box(request, order_id)

        self.assertEqual(response.status_code, 200)
        payload = json.loads(response.content)
        self.assertEqual(payload["box_code"], new_box_code)
        self.assertEqual(
            [box["code"] for box in payload["flow_state"]["boxes"]],
            [new_box_code],
        )
        self.assertEqual(
            payload["flow_state"]["pallets"][0]["boxes"],
            [new_box_code],
        )
        session = ProcessingFlowSession.objects.get(
            order_id=order_id,
            agent_id=f"agent-{order_id}",
        )
        self.assertEqual(
            [box["code"] for box in session.flow_state["boxes"]],
            [new_box_code],
        )

    def test_integrity_uses_flow_as_fact_and_reports_db_orphan(self):
        order_id = "SAFE-CANCEL-INTEGRITY"
        box_code = "BOX-SAFE-CANCEL-INTEGRITY"
        session, _container, _marking_row = self._create_box_state(
            order_id,
            box_code=box_code,
            marking_code=self.marking_code,
        )

        synchronized = ProcessingWorkflowService.build_processing_flow_integrity(
            order_id=order_id,
            flow_state=session.flow_state,
        )

        self.assertTrue(synchronized["in_sync"])
        self.assertEqual(synchronized["flow_total_qty"], 1)
        self.assertEqual(synchronized["flow_marking_count"], 1)
        self.assertEqual(synchronized["db_used_marking_count"], 1)
        MarkingCode.objects.create(
            order_type="processing",
            order_id=order_id,
            agency=self.agency,
            sku=self.sku,
            sku_code=self.sku.sku_code,
            size=self.sku.size,
            barcode=self.barcode.value,
            box_barcode="BOX-ORPHAN",
            code="010460123456700821ORPHAN001",
            source="scan",
            created_by=self.worker_user,
            used_at=timezone.localtime(),
            used_by=self.worker_user,
        )

        mismatch = ProcessingWorkflowService.build_processing_flow_integrity(
            order_id=order_id,
            flow_state=session.flow_state,
        )

        self.assertFalse(mismatch["in_sync"])
        self.assertEqual(mismatch["flow_total_qty"], 1)
        self.assertEqual(mismatch["db_only_count"], 1)
        self.assertEqual(mismatch["flow_only_count"], 0)
        self.assertEqual(mismatch["db_used_marking_count"], 2)

    def test_nonempty_box_cannot_be_deleted_through_legacy_draft_or_action(self):
        order_id = "SAFE-CANCEL-LEGACY"
        box_code = "BOX-SAFE-CANCEL-LEGACY"
        session, _container, _marking_row = self._create_box_state(
            order_id,
            box_code=box_code,
            marking_mode="without_cz",
        )
        entries = list(
            OrderAuditEntry.objects.filter(
                order_id=order_id,
                order_type="processing",
            ).order_by("created_at")
        )
        request = self.request_factory.post(
            f"/orders/processing/{order_id}/flow/",
            data={
                "boxes_json": "[]",
                "pallets_json": "[]",
                "active_box": "",
                "active_pallet": "",
                "agent_id": f"agent-{order_id}",
            },
        )
        request.user = self.worker_user
        with mock.patch(
            "processing_app.views.ProcessingFlowView._can_start",
            return_value=True,
        ):
            result = ProcessingWorkflowService.save_processing_flow_draft(
                order_id=order_id,
                entries=entries,
                request=request,
                normalize_flow_state=self._normalize_flow_state,
                can_reassign_boxes=False,
            )
        self.assertEqual(result.status, "ok")
        session.refresh_from_db()
        self.assertEqual(session.flow_state["boxes"][0]["code"], box_code)

        action_request = self.request_factory.post(
            f"/orders/processing/{order_id}/flow/box-action/",
            data=json.dumps({"action": "delete", "box_code": box_code}),
            content_type="application/json",
        )
        action_request.user = self.worker_user
        action_result = ProcessingWorkflowService.log_processing_flow_box_action(
            order_id=order_id,
            request=action_request,
        )
        self.assertEqual(action_result.status, "invalid_action")


class ProcessingFlowMarkingTemplateTests(SimpleTestCase):
    def test_both_flow_templates_use_explicit_post_release_correction_mode(self):
        templates_dir = Path(__file__).resolve().parent / "templates" / "processing"
        for template_name in (
            "processing_flow.html",
            "processing_flow_directions.html",
        ):
            with self.subTest(template_name=template_name):
                source = (templates_dir / template_name).read_text(encoding="utf-8")
                self.assertIn('name="correction_mode" value="additional_box"', source)
                self.assertIn("Корректировка после размещения", source)
                self.assertIn("Уже переданные палеты останутся заблокированы", source)
                self.assertIn("старую палету сначала нужно вернуть сканированием в OBR", source)

    def test_both_flow_templates_enforce_strict_cz_pair_ui(self):
        templates_dir = Path(__file__).resolve().parent / "templates" / "processing"
        for template_name in (
            "processing_flow.html",
            "processing_flow_directions.html",
        ):
            with self.subTest(template_name=template_name):
                source = (templates_dir / template_name).read_text(encoding="utf-8")
                self.assertIn('id="cz-scan-modal"', source)
                self.assertIn("Сначала сканируйте ШК товара, затем DataMatrix ЧЗ.", source)
                self.assertIn("worker_marking_mode: 'with_cz'", source)
                self.assertIn("box.marking_mode = requestedMode;", source)
                self.assertIn("playCzScanSuccessSound();", source)
                self.assertIn("let czDuplicateBlocked = false;", source)
                self.assertIn("const showCzDuplicateWarning = (data, currentBoxCode) => {", source)
                self.assertIn("ДУБЛЬ ЧЗ В ТЕКУЩЕМ КОРОБЕ", source)
                self.assertIn("ЧЗ УЖЕ В ДРУГОМ КОРОБЕ", source)
                self.assertIn("Товар удалён из текущего сканирования", source)
                self.assertIn("barcodeInput.disabled = true;", source)
                self.assertIn("Понятно, сканировать следующий товар", source)
                self.assertIn("if (czScanBusy || czDuplicateBlocked)", source)
                self.assertIn('id="flow-integrity"', source)
                self.assertIn("Факт в коробах", source)
                self.assertIn("Расхождение ЧЗ", source)
                self.assertIn("applyFlowIntegrity(data.flow_integrity || {});", source)
                self.assertIn("params.set('integrity', '1');", source)
                self.assertIn("window.__canCompleteProcessingFlow", source)
                self.assertIn("!canCompleteProcessingFlow", source)
                self.assertIn("Параллельное формирование", source)

    def test_both_flow_templates_confirm_legacy_items_only_by_scanned_barcode(self):
        templates_dir = Path(__file__).resolve().parent / "templates" / "processing"
        for template_name in (
            "processing_flow.html",
            "processing_flow_directions.html",
        ):
            with self.subTest(template_name=template_name):
                source = (templates_dir / template_name).read_text(encoding="utf-8")
                self.assertIn('id="legacy-barcode-panel"', source)
                self.assertIn("action: 'confirm_legacy_barcode'", source)
                self.assertIn("targets: legacyBarcodeTarget.targets", source)
                self.assertIn("if (legacyBarcodeTarget)", source)
                self.assertNotIn("plannedBySku", source)

    def test_both_flow_templates_expose_only_safe_head_box_cancellation(self):
        templates_dir = Path(__file__).resolve().parent / "templates" / "processing"
        for template_name in (
            "processing_flow.html",
            "processing_flow_directions.html",
        ):
            with self.subTest(template_name=template_name):
                source = (templates_dir / template_name).read_text(
                    encoding="utf-8"
                )
                self.assertIn("{% if can_cancel_processing_boxes %}", source)
                self.assertIn('data-action="cancel"', source)
                self.assertNotIn('data-action="delete"', source)
                self.assertIn('id="box-cancel-modal"', source)
                self.assertIn("Причина отмены", source)
                self.assertIn("История отменённых коробов", source)
                self.assertIn("/flow/cancel-box/", source)
                self.assertIn("submitBoxCancellation", source)
                self.assertIn("payload.error === 'canceled_box'", source)


class ProcessingFlowTemplateSelectionTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="ph_user_flow_tpl", password="x")
        Employee.objects.create(
            full_name="Processing Head Flow Template",
            user=self.user,
            role="processing_head",
            is_active=True,
        )
        self.client.force_login(self.user)
        self.agency = Agency.objects.create(agn_name="Flow Template Agency")

    def _create_order(self, order_id: str, payload: dict):
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="processing",
            action="status",
            agency=self.agency,
            payload=payload,
        )

    @staticmethod
    def _base_ready_payload() -> dict:
        return {
            "status": "processing_in_work",
            "status_label": "Взята в работу",
            "cards": [
                {"id": "card-a", "article": "SKU-A", "rows": [{"size": "42", "qty": "10"}]},
            ],
            "processed_cards": ["card-a"],
            "processing_results": [
                {"card_id": "card-a", "article": "SKU-A", "size": "42", "destination": "-", "processed": "10"},
            ],
        }

    @staticmethod
    def _partial_ready_payload() -> dict:
        return {
            "status": "processing_in_work",
            "status_label": "Взята в работу",
            "cards": [
                {"id": "card-a", "article": "SKU-A", "rows": [{"size": "42", "qty": "10"}]},
                {"id": "card-b", "article": "SKU-B", "rows": [{"size": "43", "qty": "10"}]},
            ],
            "processed_cards": ["card-a"],
            "processing_results": [
                {"card_id": "card-a", "article": "SKU-A", "size": "42", "destination": "-", "processed": "10"},
            ],
        }

    def test_partial_flow_opens_without_advancing_whole_order_stage(self):
        order_id = "810-PARTIAL"
        self._create_order(order_id, self._partial_ready_payload())

        response = self.client.get(f"/orders/processing/{order_id}/flow/")

        self.assertEqual(response.status_code, 200)
        self.assertFalse(bool(response.context["can_complete_processing_flow"]))
        self.assertEqual(response.context["processing_cards_done_count"], 1)
        self.assertEqual(response.context["processing_cards_total_count"], 2)
        self.assertContains(response, "Параллельное формирование")
        self.assertContains(response, "SKU-A")
        self.assertNotContains(response, "SKU-B")
        self.assertEqual(
            OrderAuditEntry.objects.filter(order_id=order_id, order_type="processing").count(),
            1,
        )

    def test_partial_flow_saves_draft_but_cannot_complete_order(self):
        order_id = "810-PARTIAL-DRAFT"
        self._create_order(order_id, self._partial_ready_payload())
        boxes = [
            {
                "code": "BOX-PARTIAL-1",
                "sealed": True,
                "items": [
                    {"sku_code": "SKU-A", "name": "SKU-A", "size": "42", "qty": 10},
                ],
            },
        ]
        pallets = [
            {"code": "PAL-PARTIAL-1", "sealed": True, "boxes": ["BOX-PARTIAL-1"]},
        ]

        draft_response = self.client.post(
            f"/orders/processing/{order_id}/flow/",
            {
                "flow_action": "draft",
                "agent_id": "partial-test-agent",
                "boxes_json": json.dumps(boxes),
                "pallets_json": json.dumps(pallets),
                "active_box": "",
                "active_pallet": "",
            },
        )

        self.assertEqual(draft_response.status_code, 200)
        self.assertEqual(draft_response.json().get("ok"), True)
        session = ProcessingFlowSession.objects.get(
            order_id=order_id,
            order_type="processing",
            agent_id="partial-test-agent",
        )
        self.assertEqual(session.flow_state["boxes"][0]["code"], "BOX-PARTIAL-1")

        finish_response = self.client.post(
            f"/orders/processing/{order_id}/flow/",
            {
                "boxes_json": json.dumps(boxes),
                "pallets_json": json.dumps(pallets),
            },
        )

        self.assertEqual(finish_response.status_code, 302)
        self.assertIn("error=cannot_start", finish_response.url)
        self.assertFalse(
            OrderAuditEntry.objects.filter(
                order_id=order_id,
                order_type="processing",
                payload__flow_closed=True,
            ).exists()
        )

    def test_flow_uses_directional_template_when_direction_distribution_exists(self):
        order_id = "811"
        payload = self._base_ready_payload()
        payload["direction_addresses_json"] = ["Москва", "Томск"]
        payload["direction_plan_json"] = {
            "directions": ["Москва", "Томск"],
            "rows": [{"article": "SKU-A", "size": "42", "quantities": [4, 6]}],
        }
        self._create_order(order_id, payload)

        response = self.client.get(f"/orders/processing/{order_id}/flow/")
        self.assertEqual(response.status_code, 200)
        template_names = {t.name for t in response.templates if getattr(t, "name", "")}
        self.assertIn("processing/processing_flow_directions.html", template_names)
        self.assertContains(response, 'id="pallet-close-count-modal"', html=False)
        self.assertContains(response, "openPalletCloseConfirm", html=False)

    def test_flow_uses_standard_template_without_direction_distribution(self):
        order_id = "812"
        payload = self._base_ready_payload()
        self._create_order(order_id, payload)

        response = self.client.get(f"/orders/processing/{order_id}/flow/")
        self.assertEqual(response.status_code, 200)
        template_names = {t.name for t in response.templates if getattr(t, "name", "")}
        self.assertIn("processing/processing_flow.html", template_names)
        self.assertNotIn("processing/processing_flow_directions.html", template_names)
        self.assertContains(response, 'id="pallet-close-count-modal"', html=False)
        self.assertContains(response, "openPalletCloseConfirm", html=False)


class ProcessingReachtruckBarcodeSelectionTests(TestCase):
    def setUp(self):
        self.agency = Agency.objects.create(agn_name="Processing Barcode Selection Agency")
        self.user = get_user_model().objects.create_user(
            username="processing_barcode_selection_head",
            password="x",
        )
        Employee.objects.create(
            full_name="Руководитель выбора коробов",
            user=self.user,
            role="processing_head",
            is_active=True,
        )

    def _create_box(
        self,
        *,
        sku: str,
        barcode: str,
        goods_type: str,
        box_code: str,
        qty: int = 10,
    ) -> WarehouseStockSnapshot:
        return create_warehouse_snapshot_row(
            agency=self.agency,
            order_type="receiving",
            order_id=f"REC-{box_code}",
            sku=sku,
            name="Товар для подачи в OBR",
            barcode=barcode,
            goods_type=goods_type,
            qty=qty,
            available_qty=qty,
            box_code=box_code,
            pallet_code=f"PAL-{box_code}",
            zone="OS",
            warehouse_state_code=WarehouseStateCode.STORED.value,
        )

    def test_obr_pick_uses_barcode_even_when_article_and_goods_type_differ(self):
        snapshot = self._create_box(
            sku="SKU-STOCK",
            barcode="BAR-STRICT",
            goods_type="op",
            box_code="BOX-STRICT",
        )

        rows = build_obr_requested_rows(
            agency=self.agency,
            processing_order_id="PROC-BARCODE",
            request_items=[
                {
                    "requested_article": "SKU-REQUEST-DIFFERENT",
                    "requested_barcodes": ["BAR-STRICT"],
                    "requested_goods_type": "готовый",
                    "requested_qty": 10,
                }
            ],
        )

        self.assertEqual(sum(int(row.get("qty") or 0) for row in rows), 10)
        self.assertEqual({row.get("box_code") for row in rows}, {snapshot.container_code})

    def test_obr_pick_rejects_wrong_barcode_even_when_article_matches(self):
        self._create_box(
            sku="SKU-SAME",
            barcode="BAR-STOCK",
            goods_type="op",
            box_code="BOX-WRONG-BARCODE",
        )

        rows = build_obr_requested_rows(
            agency=self.agency,
            processing_order_id="PROC-WRONG-BARCODE",
            request_items=[
                {
                    "requested_article": "SKU-SAME",
                    "requested_barcodes": ["BAR-OTHER"],
                    "requested_goods_type": "op",
                    "requested_qty": 10,
                }
            ],
        )

        self.assertEqual(rows, [])

    def test_obr_pick_keeps_article_fallback_for_legacy_item_without_barcode(self):
        self._create_box(
            sku="SKU-LEGACY",
            barcode="",
            goods_type="op",
            box_code="BOX-LEGACY",
        )

        rows = build_obr_requested_rows(
            agency=self.agency,
            processing_order_id="PROC-LEGACY",
            request_items=[
                {
                    "requested_article": "SKU-LEGACY",
                    "requested_barcodes": [],
                    "requested_goods_type": "оптовый",
                    "requested_qty": 10,
                }
            ],
        )

        self.assertEqual(sum(int(row.get("qty") or 0) for row in rows), 10)
        self.assertEqual({row.get("box_code") for row in rows}, {"BOX-LEGACY"})

    def test_obr_request_reserves_selected_os_box(self):
        snapshot = self._create_box(
            sku="SKU-RESERVE",
            barcode="BAR-RESERVE",
            goods_type="op",
            box_code="BOX-RESERVE",
        )
        request = RequestFactory().post("/orders/processing/PROC-RESERVE/obr/", data={})
        request.user = self.user

        response = create_obr_move_request_response(
            request=request,
            agency=self.agency,
            processing_order_id="PROC-RESERVE",
            request_items=[
                {
                    "requested_article": "SKU-RESERVE",
                    "requested_barcodes": ["BAR-RESERVE"],
                    "requested_goods_type": "op",
                    "requested_qty": 10,
                }
            ],
        )

        self.assertEqual(response.status_code, 200)
        snapshot.refresh_from_db()
        self.assertEqual(snapshot.available_qty, 0)
        self.assertEqual(snapshot.processing_reserved_qty, 10)
        self.assertEqual(snapshot.warehouse_state_code, WarehouseStateCode.RESERVED_FOR_PROCESSING.value)
        reserve = WarehouseReserve.objects.get(
            agency=self.agency,
            reserve_type=WarehouseReserve.TYPE_PROCESSING,
            context_type="processing",
            context_id="PROC-RESERVE",
        )
        self.assertEqual(reserve.qty_reserved, 10)
        self.assertEqual(BoxClaim.objects.filter(status=BoxClaim.STATUS_CLAIMED).count(), 1)

    def test_unreconciled_delivered_box_is_hidden_and_not_selected(self):
        self._create_box(
            sku="SKU-DELIVERED",
            barcode="BAR-DELIVERED",
            goods_type="op",
            box_code="BOX-DELIVERED",
        )
        move_request = MoveRequest.objects.create(
            context_type="processing",
            context_id="PROC-OLD",
            agency=self.agency,
            destination_zone="OBR",
            status="done",
        )
        move_task = MoveTask.objects.create(
            request=move_request,
            pallet_code="PAL-BOX-DELIVERED",
            status=MoveTask.STATUS_DONE,
        )
        BoxClaim.objects.create(
            agency=self.agency,
            move_task=move_task,
            box_code="BOX-DELIVERED",
            pallet_code="PAL-BOX-DELIVERED",
            claim_kind=BoxClaim.KIND_BOX,
            status=BoxClaim.STATUS_DELIVERED,
            delivered_at=timezone.now(),
        )

        picker_codes = {
            code
            for row in _processing_box_picker_rows(self.agency, separate_boxes=True)
            for code in row.get("box_codes") or []
        }
        selected_rows = build_obr_requested_rows(
            agency=self.agency,
            processing_order_id="PROC-NEW",
            request_items=[
                {
                    "requested_article": "SKU-DELIVERED",
                    "requested_barcodes": ["BAR-DELIVERED"],
                    "requested_goods_type": "op",
                    "requested_qty": 10,
                }
            ],
        )

        self.assertNotIn("BOX-DELIVERED", picker_codes)
        self.assertEqual(selected_rows, [])

    def test_submit_processing_rejects_stale_selected_box(self):
        self._create_box(
            sku="SKU-REPLAY",
            barcode="BAR-REPLAY",
            goods_type="op",
            box_code="BOX-REPLAY",
        )
        move_request = MoveRequest.objects.create(
            context_type="processing",
            context_id="PROC-DONE",
            agency=self.agency,
            destination_zone="OBR",
            status="done",
        )
        move_task = MoveTask.objects.create(
            request=move_request,
            pallet_code="PAL-BOX-REPLAY",
            status=MoveTask.STATUS_DONE,
        )
        BoxClaim.objects.create(
            agency=self.agency,
            move_task=move_task,
            box_code="BOX-REPLAY",
            pallet_code="PAL-BOX-REPLAY",
            claim_kind=BoxClaim.KIND_BOX,
            status=BoxClaim.STATUS_DELIVERED,
            delivered_at=timezone.now(),
        )
        request = RequestFactory().post(
            "/orders/processing/",
            data={
                "submit_action": "send",
                "client_unit_picker_v1": "1",
                "cards_json": json.dumps(
                    [
                        {
                            "id": "card-replay",
                            "article": "SKU-REPLAY",
                            "product_name": "Replay item",
                            "goods_type": "op",
                            "rows": [
                                {
                                    "article": "SKU-REPLAY",
                                    "barcode": "BAR-REPLAY",
                                    "qty": 10,
                                    "box_codes": ["BOX-REPLAY"],
                                }
                            ],
                        }
                    ],
                    ensure_ascii=False,
                ),
            },
        )
        request.user = self.user
        request._client_agency = self.agency

        response = ProcessingWorkflowService.submit_processing(request=request)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Короба уже заняты другой заявкой или доставлены в обработку")
        self.assertFalse(
            OrderAuditEntry.objects.filter(
                agency=self.agency,
                order_type="processing",
            ).exists()
        )


class ProcessingWarehouseFactGuardTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="processing_fact_guard",
            password="x",
        )
        Employee.objects.create(
            full_name="Processing Fact Guard",
            user=self.user,
            role="processing_head",
            is_active=True,
        )
        self.agency = Agency.objects.create(agn_name="Processing Fact Guard Agency")

    def _create_source_box(
        self,
        *,
        order_id: str,
        box_code: str,
        pallet_code: str,
        qty: int = 10,
        barcode: str = "FACT-BC",
    ) -> WarehouseStockSnapshot:
        return create_warehouse_snapshot_row(
            agency=self.agency,
            order_type="receiving",
            order_id=order_id,
            sku="SKU-FACT",
            name="Товар для обработки",
            barcode=barcode,
            qty=qty,
            available_qty=qty,
            box_code=box_code,
            pallet_code=pallet_code,
            zone="OS",
            warehouse_state_code=WarehouseStateCode.STORED.value,
        )

    def test_full_box_arrival_is_idempotent_for_same_reachtruck_task(self):
        snapshot = self._create_source_box(
            order_id="REC-FACT-1",
            box_code="BOX-FACT-1",
            pallet_code="PAL-FACT-1",
        )

        first_qty = WarehouseWritePathService.mark_processing_boxes_arrived_to_obr(
            agency=self.agency,
            order_id="PROC-FACT-1",
            box_codes=["BOX-FACT-1"],
            performed_by=self.user,
            source_document_id="TASK-FACT-1",
        )
        repeated_qty = WarehouseWritePathService.mark_processing_boxes_arrived_to_obr(
            agency=self.agency,
            order_id="PROC-FACT-1",
            box_codes=["BOX-FACT-1"],
            performed_by=self.user,
            source_document_id="TASK-FACT-1",
        )

        snapshot.refresh_from_db()
        self.assertEqual(first_qty, 10)
        self.assertEqual(repeated_qty, 10)
        self.assertEqual(snapshot.source_context_type, "processing")
        self.assertEqual(snapshot.source_context_id, "PROC-FACT-1")
        self.assertEqual(snapshot.zone_code, "OBR")
        self.assertEqual(
            WarehouseEvent.objects.filter(
                agency=self.agency,
                event_type="processing_zone_arrived",
                source_document_type="reachtruck_processing_task",
                source_document_id="TASK-FACT-1",
            ).count(),
            1,
        )

    def test_same_full_box_cannot_be_delivered_by_second_task(self):
        self._create_source_box(
            order_id="REC-FACT-2",
            box_code="BOX-FACT-2",
            pallet_code="PAL-FACT-2",
        )
        WarehouseWritePathService.mark_processing_boxes_arrived_to_obr(
            agency=self.agency,
            order_id="PROC-FACT-2",
            box_codes=["BOX-FACT-2"],
            performed_by=self.user,
            source_document_id="TASK-FACT-2-A",
        )

        with self.assertRaisesMessage(ValueError, "уже закреплён"):
            WarehouseWritePathService.mark_processing_boxes_arrived_to_obr(
                agency=self.agency,
                order_id="PROC-FACT-OTHER",
                box_codes=["BOX-FACT-2"],
                performed_by=self.user,
                source_document_id="TASK-FACT-2-B",
            )

    def test_partial_pick_retry_does_not_take_quantity_twice(self):
        source_snapshot = self._create_source_box(
            order_id="REC-FACT-3",
            box_code="BOX-FACT-3",
            pallet_code="PAL-FACT-3",
            qty=10,
            barcode="FACT-BC-3",
        )
        picked_rows = [
            {
                "box_code": "BOX-FACT-3",
                "picked_qty": 4,
                "barcode_qty": {"FACT-BC-3": 4},
            }
        ]

        first_ids = WarehouseWritePathService.complete_partial_processing_pick_to_obr(
            agency=self.agency,
            order_id="PROC-FACT-3",
            source_pallet_code="PAL-FACT-3",
            picked_rows=picked_rows,
            performed_by=self.user,
            source_document_id="TASK-FACT-3",
        )
        repeated_ids = WarehouseWritePathService.complete_partial_processing_pick_to_obr(
            agency=self.agency,
            order_id="PROC-FACT-3",
            source_pallet_code="PAL-FACT-3",
            picked_rows=picked_rows,
            performed_by=self.user,
            source_document_id="TASK-FACT-3",
        )

        source_snapshot.refresh_from_db()
        self.assertEqual(repeated_ids, first_ids)
        self.assertEqual(source_snapshot.qty, 6)
        self.assertEqual(
            WarehouseStockSnapshot.objects.get(id=first_ids[0]).qty,
            4,
        )
        self.assertEqual(
            WarehouseEvent.objects.filter(
                agency=self.agency,
                event_type="processing_zone_arrived",
                source_document_type="reachtruck_processing_task",
                source_document_id="TASK-FACT-3",
            ).count(),
            1,
        )

    def test_processing_input_consumption_requires_exact_output_quantity(self):
        input_snapshot = create_warehouse_snapshot_row(
            agency=self.agency,
            order_type="processing",
            order_id="PROC-FACT-4",
            sku="SKU-A",
            name="Исходный товар A",
            barcode="FACT-A",
            qty=10,
            available_qty=0,
            box_code="BOX-FACT-4",
            pallet_code="PAL-FACT-4",
            zone="OBR",
            warehouse_state_code=WarehouseStateCode.IN_PROCESSING_ZONE.value,
        )

        operation = WarehouseWritePathService.consume_processing_input(
            agency=self.agency,
            order_id="PROC-FACT-4",
            performed_by=self.user,
            expected_output_qty=10,
        )

        input_snapshot.refresh_from_db()
        self.assertEqual(operation.status, WarehouseOperation.STATUS_DONE)
        self.assertEqual(operation.done_qty, 10)
        self.assertEqual(
            input_snapshot.warehouse_state_code,
            WarehouseStateCode.PROCESSING_CONSUMED.value,
        )

    def test_processing_input_consumption_blocks_quantity_mismatch_without_writes(self):
        input_snapshot = create_warehouse_snapshot_row(
            agency=self.agency,
            order_type="processing",
            order_id="PROC-FACT-5",
            sku="SKU-A",
            name="Исходный товар A",
            barcode="FACT-A-5",
            qty=12,
            available_qty=0,
            box_code="BOX-FACT-5",
            pallet_code="PAL-FACT-5",
            zone="OBR",
            warehouse_state_code=WarehouseStateCode.IN_PROCESSING_ZONE.value,
        )

        with self.assertRaisesMessage(ValueError, "в OBR подано 12"):
            WarehouseWritePathService.consume_processing_input(
                agency=self.agency,
                order_id="PROC-FACT-5",
                performed_by=self.user,
                expected_output_qty=10,
            )

        input_snapshot.refresh_from_db()
        self.assertEqual(
            input_snapshot.warehouse_state_code,
            WarehouseStateCode.IN_PROCESSING_ZONE.value,
        )
        self.assertFalse(
            WarehouseOperation.objects.filter(
                agency=self.agency,
                operation_type=WarehouseOperation.TYPE_PROCESSING,
                context_type="processing",
                context_id="PROC-FACT-5",
            ).exists()
        )

    def test_processing_input_consumption_keeps_approved_remainder_in_obr(self):
        input_snapshot = create_warehouse_snapshot_row(
            agency=self.agency,
            order_type="processing",
            order_id="PROC-FACT-REMAINDER",
            sku="SKU-A",
            name="Исходный товар A",
            barcode="FACT-A-REMAINDER",
            qty=12,
            available_qty=0,
            box_code="BOX-FACT-REMAINDER",
            pallet_code="PAL-FACT-REMAINDER",
            zone="OBR",
            warehouse_state_code=WarehouseStateCode.IN_PROCESSING_ZONE.value,
        )

        operation = WarehouseWritePathService.consume_processing_input(
            agency=self.agency,
            order_id="PROC-FACT-REMAINDER",
            performed_by=self.user,
            expected_output_qty=10,
            allow_obr_remainder=True,
        )

        input_snapshot.refresh_from_db()
        input_snapshot.container.refresh_from_db()
        event = WarehouseEvent.objects.get(
            agency=self.agency,
            event_type="processing_consumed",
            stock_context_type="processing",
            stock_context_id="PROC-FACT-REMAINDER",
        )
        self.assertEqual(operation.status, WarehouseOperation.STATUS_DONE)
        self.assertEqual(operation.done_qty, 10)
        self.assertEqual(input_snapshot.qty, 2)
        self.assertEqual(input_snapshot.available_qty, 0)
        self.assertEqual(input_snapshot.zone_code, "OBR")
        self.assertEqual(
            input_snapshot.warehouse_state_code,
            WarehouseStateCode.IN_PROCESSING_ZONE.value,
        )
        self.assertNotEqual(input_snapshot.container.status, WarehouseContainer.STATUS_ARCHIVED)
        self.assertEqual(event.qty, 10)
        self.assertTrue((event.payload or {}).get("partial"))
        self.assertEqual((event.payload or {}).get("remainder_qty"), 2)

        request = RequestFactory().post("/orders/processing/PROC-FACT-REMAINDER/flow/")
        request.user = self.user
        _sync_processing_obr_remainder_task(
            "PROC-FACT-REMAINDER",
            self.agency,
            request,
            planned_qty=12,
            processed_qty=10,
        )
        task = Task.objects.get(title="Остаток в OBR по заявке №PROC-FACT-REMAINDER")
        self.assertEqual(task.assigned_to.user, self.user)
        self.assertIn("Осталось в OBR: 2 шт.", task.description)
        self.assertIn("BOX-FACT-REMAINDER: 2 шт.", task.description)

    def test_processing_input_consumption_blocks_output_above_obr_even_with_remainder_mode(self):
        create_warehouse_snapshot_row(
            agency=self.agency,
            order_type="processing",
            order_id="PROC-FACT-SHORTAGE",
            sku="SKU-A",
            name="Исходный товар A",
            barcode="FACT-A-SHORTAGE",
            qty=8,
            available_qty=0,
            box_code="BOX-FACT-SHORTAGE",
            pallet_code="PAL-FACT-SHORTAGE",
            zone="OBR",
            warehouse_state_code=WarehouseStateCode.IN_PROCESSING_ZONE.value,
        )

        with self.assertRaisesMessage(ValueError, "в OBR подано 8"):
            WarehouseWritePathService.consume_processing_input(
                agency=self.agency,
                order_id="PROC-FACT-SHORTAGE",
                performed_by=self.user,
                expected_output_qty=10,
                allow_obr_remainder=True,
            )

        self.assertFalse(
            WarehouseEvent.objects.filter(
                agency=self.agency,
                event_type="processing_consumed",
                stock_context_type="processing",
                stock_context_id="PROC-FACT-SHORTAGE",
            ).exists()
        )

    def test_processing_output_cannot_use_free_relocation(self):
        create_warehouse_snapshot_row(
            agency=self.agency,
            order_type="processing",
            order_id="PROC-FACT-6",
            sku="SKU-B",
            name="Результат обработки B",
            barcode="FACT-B-6",
            qty=10,
            available_qty=10,
            box_code="BOX-FACT-6",
            pallet_code="PAL-FACT-6",
            zone="OBR",
            warehouse_state_code=WarehouseStateCode.PLACED_AFTER_PROCESSING.value,
        )

        with self.assertRaisesMessage(ValueError, "связанное задание"):
            WarehouseWritePathService.start_free_pallet_relocation(
                agency=self.agency,
                pallet_code="PAL-FACT-6",
                performed_by=self.user,
            )


class ProcessingFlowOperationalStockTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="ph_user_flow_stock", password="x")
        Employee.objects.create(
            full_name="Processing Head Flow Stock",
            user=self.user,
            role="processing_head",
            is_active=True,
        )
        self.reachtruck_user = get_user_model().objects.create_user(username="reachtruck_flow_stock", password="x")
        self.reachtruck = Employee.objects.create(
            full_name="Ричтракер сквозного теста",
            user=self.reachtruck_user,
            role="reachtruck",
            is_active=True,
        )
        self.client.force_login(self.user)
        self.agency = Agency.objects.create(agn_name="Flow Stock Agency")

    def test_flow_close_writes_finished_goods_to_operational_stock(self):
        order_id = "813"
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="processing",
            action="status",
            agency=self.agency,
            payload={
                "status": "processing_in_work",
                "status_label": "Взята в работу",
                "processing_stage": PROCESSING_STAGE_UNBOXING_OPENED,
                "goods_type": "gv",
                "goods_type_label": "Готовый",
                "cards": [
                    {
                        "id": "card-a",
                        "article": "SKU-FLOW",
                        "rows": [{"size": "42", "qty": "10", "barcode": "FLOW-BAR-1"}],
                    },
                ],
                "processed_cards": ["card-a"],
                "processing_results": [
                    {
                        "card_id": "card-a",
                        "article": "SKU-FLOW",
                        "name": "Flow Item",
                        "size": "42",
                        "barcode": "FLOW-BAR-1",
                        "destination": "-",
                        "processed": "10",
                    },
                ],
            },
        )
        create_warehouse_snapshot_row(
            agency=self.agency,
            order_type="processing",
            order_id=order_id,
            sku="SKU-SOURCE-FLOW",
            name="Исходный товар",
            barcode="FLOW-SOURCE-1",
            qty=10,
            available_qty=0,
            box_code="BOX-SOURCE-FLOW-1",
            pallet_code="PAL-SOURCE-FLOW-1",
            zone="OBR",
            warehouse_state_code=WarehouseStateCode.PROCESSING_IN_PROGRESS.value,
        )

        response = self.client.post(
            f"/orders/processing/{order_id}/flow/",
            {
                "boxes_json": (
                    '[{"code":"BOX-FLOW-1","items":[{"sku":"SKU-FLOW","sku_code":"SKU-FLOW",'
                    '"name":"Flow Item","size":"42","barcode":"FLOW-BAR-1","qty":10}],'
                    '"sealed":true}]'
                ),
                "pallets_json": (
                    '[{"code":"PAL-FLOW-1","boxes":["BOX-FLOW-1"],"items":[],"sealed":true,'
                    '"location":{"zone":"OS","row":3,"section":2,"tier":1,"cell":4}}]'
                ),
            },
        )

        self.assertEqual(response.status_code, 302)
        snapshot = WarehouseStockSnapshot.objects.get(
            agency=self.agency,
            source_context_type="processing",
            source_context_id=order_id,
        )
        self.assertEqual(snapshot.sku_code, "SKU-FLOW")
        self.assertEqual(snapshot.container.container_code, "BOX-FLOW-1")
        self.assertEqual(snapshot.parent_container.container_code, "PAL-FLOW-1")
        self.assertEqual(snapshot.zone_code, "OBR")
        self.assertEqual(snapshot.goods_type, "gv")
        self.assertEqual(snapshot.warehouse_state_code, WarehouseStateCode.PLACED_AFTER_PROCESSING.value)
        self.assertEqual(snapshot.available_qty, 10)

    def test_processed_goods_can_return_to_storage_then_be_reserved_and_shipped(self):
        order_id = "814-E2E"
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="processing",
            action="status",
            agency=self.agency,
            payload={
                "status": "processing_in_work",
                "goods_type": "gv",
                "cards": [
                    {
                        "id": "card-e2e",
                        "article": "SKU-E2E",
                        "rows": [{"size": "42", "qty": "10", "barcode": "E2E-BAR-1"}],
                    }
                ],
                "processed_cards": ["card-e2e"],
                "processing_results": [
                    {
                        "card_id": "card-e2e",
                        "article": "SKU-E2E",
                        "name": "Сквозной товар",
                        "size": "42",
                        "barcode": "E2E-BAR-1",
                        "destination": "-",
                        "processed": "10",
                    }
                ],
            },
        )

        response = self.client.post(
            f"/orders/processing/{order_id}/flow/",
            {
                "boxes_json": (
                    '[{"code":"BOX-E2E-1","items":[{"sku":"SKU-E2E","sku_code":"SKU-E2E",'
                    '"name":"Сквозной товар","size":"42","barcode":"E2E-BAR-1","qty":10}],"sealed":true}]'
                ),
                "pallets_json": '[{"code":"PAL-E2E-1","boxes":["BOX-E2E-1"],"items":[],"sealed":true}]',
            },
        )
        self.assertEqual(response.status_code, 302)

        return_operation = WarehouseWritePathService.complete_processing_output_return_to_storage(
            agency=self.agency,
            order_id=order_id,
            pallet_code="PAL-E2E-1",
            destination_zone_code="OS",
            destination_row_no=1,
            destination_section_no=1,
            destination_tier_no=1,
            destination_cell_no=1,
            performed_by=self.user,
        )
        self.assertIsNotNone(return_operation)
        snapshot = WarehouseStockSnapshot.objects.get(
            agency=self.agency,
            source_context_type="processing",
            source_context_id=order_id,
        )
        self.assertEqual(snapshot.warehouse_state_code, WarehouseStateCode.STORED.value)
        self.assertEqual(snapshot.available_qty, 10)

        shipping_order = ShippingOrder.objects.create(
            number="SO-E2E-814",
            agency=self.agency,
            created_by=self.user,
            status=ShippingOrder.STATUS_SUBMITTED,
        )
        shipping_item = ShippingOrderItem.objects.create(
            order=shipping_order,
            sku_code="SKU-E2E",
            name="Сквозной товар",
            size="42",
            barcode="E2E-BAR-1",
            goods_type="Готовый",
            qty_requested=10,
        )

        reserve_order(shipping_order, self.user)
        shipping_order.refresh_from_db()
        shipping_item.refresh_from_db()
        self.assertEqual(shipping_order.status, ShippingOrder.STATUS_RESERVED)
        self.assertEqual(shipping_item.qty_reserved, 10)
        self.assertTrue(
            WarehouseReserve.objects.filter(
                agency=self.agency,
                reserve_type=WarehouseReserve.TYPE_SHIPPING,
                context_id=shipping_order.number,
                status=WarehouseReserve.STATUS_ACTIVE,
            ).exists()
        )

        move_ids = create_pick_tasks(
            shipping_order,
            self.user,
            requested_by_name="Руководитель обработки",
            requested_by_role="processing_head",
        )
        self.assertEqual(len(move_ids), 1)
        taken = take_move_task(
            legacy_order_id=move_ids[0],
            user=self.reachtruck_user,
            employee_id=self.reachtruck.id,
            employee_name=self.reachtruck.full_name,
        )
        self.assertTrue(taken.ok, taken.error)
        for scan_value in ("PAL-E2E-1", "BOX-E2E-1", "OTG"):
            completed_pick = scan_move_task_step(
                legacy_order_id=move_ids[0],
                scan_value=scan_value,
                user=self.reachtruck_user,
                employee_id=self.reachtruck.id,
                employee_name=self.reachtruck.full_name,
            )
            self.assertTrue(completed_pick.ok, completed_pick.error)
        self.assertTrue(completed_pick.completed)
        pick_task = MoveTask.objects.get(legacy_order_id=move_ids[0])
        self.assertEqual(pick_task.status, MoveTask.STATUS_DONE)

        ship_order(shipping_order, self.user, shipped_qty_by_item={shipping_item.id: 10})
        shipping_order.refresh_from_db()
        shipping_item.refresh_from_db()
        snapshot.refresh_from_db()
        self.assertEqual(shipping_order.status, ShippingOrder.STATUS_SHIPPED)
        self.assertEqual(shipping_item.qty_shipped, 10)
        self.assertEqual(snapshot.warehouse_state_code, WarehouseStateCode.SHIPPED.value)
        self.assertTrue(snapshot.is_archived)


class ProcessingPackagingAssignmentFlowTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="ph_user_packaging", password="x")
        Employee.objects.create(
            full_name="Processing Head Packaging",
            user=self.user,
            role="processing_head",
            is_active=True,
        )
        self.worker_user = get_user_model().objects.create_user(
            username="processing_packaging_worker",
            password="x",
        )
        self.worker = Employee.objects.create(
            full_name="Worker One",
            user=self.worker_user,
            role="processing_worker",
            is_active=True,
        )
        self.client.force_login(self.user)
        self.agency = Agency.objects.create(agn_name="Packaging Flow Agency")

    def _create_order(self, order_id: str, payload: dict):
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="processing",
            action="status",
            agency=self.agency,
            payload=payload,
        )

    @staticmethod
    def _base_payload() -> dict:
        return {
            "status": "processing_in_work",
            "status_label": "Взята в работу",
            "cards": [
                {"id": "card-a", "article": "SKU-A", "rows": [{"size": "42", "qty": "10"}]},
                {"id": "card-b", "article": "SKU-B", "rows": [{"size": "43", "qty": "10"}]},
            ],
        }

    def test_assign_packaging_deferred_until_flow_conditions_are_ready(self):
        order_id = "701"
        payload = self._base_payload()
        payload["processed_cards"] = []
        payload["processing_results"] = []
        self._create_order(order_id, payload)

        response = self.client.post(
            f"/orders/processing/{order_id}/assign-packaging/",
            {"assignee_id": str(self.worker.id)},
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn("assign=deferred", response.url)
        self.assertFalse(
            Task.objects.filter(
                route=f"/orders/processing/{order_id}/flow/",
                assigned_to=self.worker,
            ).exclude(status="done").exists()
        )
        latest = (
            OrderAuditEntry.objects.filter(order_id=order_id, order_type="processing")
            .order_by("-id")
            .first()
        )
        self.assertIsNotNone(latest)
        self.assertEqual(
            str((latest.payload or {}).get("packing_assignment_state") or "").strip().lower(),
            "pending",
        )

    def test_pending_packaging_task_auto_dispatches_when_work_page_is_ready(self):
        order_id = "702"
        payload = self._base_payload()
        payload["processed_cards"] = []
        payload["processing_results"] = []
        self._create_order(order_id, payload)
        self.client.post(
            f"/orders/processing/{order_id}/assign-packaging/",
            {"assignee_id": str(self.worker.id)},
        )
        self.assertFalse(
            Task.objects.filter(
                route=f"/orders/processing/{order_id}/flow/",
                assigned_to=self.worker,
            ).exclude(status="done").exists()
        )
        ready_payload = self._base_payload()
        ready_payload["processed_cards"] = ["card-a"]
        ready_payload["processing_results"] = [
            {"card_id": "card-a", "article": "SKU-A", "size": "42", "destination": "-", "processed": "10"},
        ]
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="processing",
            action="status",
            agency=self.agency,
            payload=ready_payload,
        )

        response = self.client.get(f"/orders/processing/{order_id}/work/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context.get("assign_status"), "auto_dispatched")
        self.assertFalse(bool(response.context.get("can_complete_processing_flow")))
        self.assertTrue(
            Task.objects.filter(route=f"/orders/processing/{order_id}/flow/", assigned_to=self.worker)
            .exclude(status="done")
            .exists()
        )
        task = Task.objects.get(
            route=f"/orders/processing/{order_id}/flow/",
            assigned_to=self.worker,
        )
        self.assertEqual(task.status, "backlog")
        latest_assignment_state_entry = None
        for entry in OrderAuditEntry.objects.filter(order_id=order_id, order_type="processing").order_by("-id"):
            payload = entry.payload or {}
            if isinstance(payload, dict) and "packing_assignment_state" in payload:
                latest_assignment_state_entry = entry
                break
        self.assertIsNotNone(latest_assignment_state_entry)
        self.assertEqual(
            str((latest_assignment_state_entry.payload or {}).get("packing_assignment_state") or "").strip().lower(),
            "dispatched",
        )

    def test_packaging_assignment_rejects_worker_without_cabinet_before_task_changes(self):
        order_id = "703-NO-CABINET"
        payload = self._base_payload()
        payload["processed_cards"] = []
        payload["processing_results"] = []
        self._create_order(order_id, payload)
        worker_without_cabinet = Employee.objects.create(
            full_name="Worker Without Cabinet",
            role="processing_worker",
            is_active=True,
        )
        route = f"/orders/processing/{order_id}/flow/"
        current_task = Task.objects.create(
            title="Current packaging task",
            route=route,
            assigned_to=self.worker,
            status="backlog",
        )
        audit_count = OrderAuditEntry.objects.filter(
            order_id=order_id,
            order_type="processing",
        ).count()

        response = self.client.post(
            f"/orders/processing/{order_id}/assign-packaging/",
            {"assignee_id": str(worker_without_cabinet.id)},
        )

        self.assertEqual(response.status_code, 302)
        self.assertIn("assign_error=worker_cabinet_missing", response.url)
        current_task.refresh_from_db()
        self.assertEqual(current_task.status, "backlog")
        self.assertEqual(
            OrderAuditEntry.objects.filter(
                order_id=order_id,
                order_type="processing",
            ).count(),
            audit_count,
        )
        notification = self.client.get(response.url)
        self.assertContains(
            notification,
            "Личный кабинет обработчика не создан или отключён. Выберите другого сотрудника.",
        )

    def test_pending_packaging_does_not_auto_dispatch_to_worker_without_cabinet(self):
        order_id = "703-PENDING-NO-CABINET"
        worker_without_cabinet = Employee.objects.create(
            full_name="Pending Worker Without Cabinet",
            role="processing_worker",
            is_active=True,
        )
        ready_payload = self._base_payload()
        ready_payload["processed_cards"] = ["card-a"]
        ready_payload["processing_results"] = [
            {
                "card_id": "card-a",
                "article": "SKU-A",
                "size": "42",
                "destination": "-",
                "processed": "10",
            },
        ]
        ready_payload.update(
            {
                "packing_assignment_state": "pending",
                "packing_assignee_id": worker_without_cabinet.id,
                "packing_assignee": worker_without_cabinet.full_name,
                "packing_assignee_role": worker_without_cabinet.role,
            }
        )
        self._create_order(order_id, ready_payload)
        audit_count = OrderAuditEntry.objects.filter(
            order_id=order_id,
            order_type="processing",
        ).count()

        response = self.client.get(f"/orders/processing/{order_id}/work/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            "Личный кабинет обработчика не создан или отключён. Выберите другого сотрудника.",
        )
        self.assertFalse(
            Task.objects.filter(
                route=f"/orders/processing/{order_id}/flow/",
                assigned_to=worker_without_cabinet,
            ).exists()
        )
        self.assertEqual(
            OrderAuditEntry.objects.filter(
                order_id=order_id,
                order_type="processing",
            ).count(),
            audit_count,
        )

    def test_assign_packaging_creates_task_immediately_when_ready(self):
        order_id = "703"
        ready_payload = self._base_payload()
        ready_payload["processed_cards"] = ["card-a"]
        ready_payload["processing_results"] = [
            {"card_id": "card-a", "article": "SKU-A", "size": "42", "destination": "-", "processed": "10"},
        ]
        self._create_order(order_id, ready_payload)

        response = self.client.post(
            f"/orders/processing/{order_id}/assign-packaging/",
            {"assignee_id": str(self.worker.id)},
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn("assign=ok", response.url)
        self.assertTrue(
            Task.objects.filter(route=f"/orders/processing/{order_id}/flow/", assigned_to=self.worker)
            .exclude(status="done")
            .exists()
        )

    def test_reassign_packaging_closes_previous_former_task(self):
        order_id = "704"
        payload = self._base_payload()
        payload["processed_cards"] = []
        payload["processing_results"] = []
        self._create_order(order_id, payload)
        second_worker_user = get_user_model().objects.create_user(
            username="processing_packaging_worker_two",
            password="x",
        )
        second_worker = Employee.objects.create(
            full_name="Worker Two",
            user=second_worker_user,
            role="processing_worker",
            is_active=True,
        )

        self.client.post(
            f"/orders/processing/{order_id}/assign-packaging/",
            {"assignee_id": str(self.worker.id)},
        )
        response = self.client.post(
            f"/orders/processing/{order_id}/assign-packaging/",
            {"assignee_id": str(second_worker.id)},
        )

        self.assertEqual(response.status_code, 302)
        self.assertIn("assign=deferred", response.url)
        route = f"/orders/processing/{order_id}/flow/"
        self.assertFalse(Task.objects.filter(route=route).exclude(status="done").exists())
        latest_assignment_state_entry = None
        for entry in OrderAuditEntry.objects.filter(order_id=order_id, order_type="processing").order_by("-id"):
            payload = entry.payload or {}
            if isinstance(payload, dict) and "packing_assignment_state" in payload:
                latest_assignment_state_entry = entry
                break
        self.assertIsNotNone(latest_assignment_state_entry)
        latest_payload = latest_assignment_state_entry.payload or {}
        self.assertEqual(str(latest_payload.get("packing_assignment_state") or ""), "pending")
        self.assertEqual(str(latest_payload.get("packing_assignee_id") or ""), str(second_worker.id))

    def test_pending_packaging_dispatches_immediately_after_card_completion(self):
        order_id = "705"
        payload = self._base_payload()
        payload["processed_cards"] = []
        payload["processing_results"] = []
        self._create_order(order_id, payload)
        self.client.post(
            f"/orders/processing/{order_id}/assign-packaging/",
            {"assignee_id": str(self.worker.id)},
        )
        ready_payload = self._base_payload()
        ready_payload["processed_cards"] = ["card-a"]
        ready_payload["processing_results"] = [
            {"card_id": "card-a", "article": "SKU-A", "size": "42", "destination": "-", "processed": "10"},
        ]
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="processing",
            action="update",
            agency=self.agency,
            payload=ready_payload,
        )

        ProcessingWorkflowService._dispatch_pending_processing_packaging_if_ready(
            order_id=order_id,
        )

        task = Task.objects.get(
            route=f"/orders/processing/{order_id}/flow/",
            assigned_to=self.worker,
        )
        self.assertEqual(task.status, "backlog")


class ProcessingStockPickerReserveExclusionTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="manager_stock_picker", password="x")
        Employee.objects.create(
            full_name="Manager Stock Picker",
            user=self.user,
            role="manager",
            is_active=True,
        )
        self.client.force_login(self.user)
        self.agency = Agency.objects.create(agn_name="Stock Picker Agency")
        self.order_id = "9901"
        OrderAuditEntry.objects.create(
            order_id=self.order_id,
            order_type="processing",
            action="status",
            agency=self.agency,
            payload={"status": "processing_in_work", "status_label": "Взята в работу"},
        )
        create_warehouse_snapshot_row(
            agency=self.agency,
            order_type="receiving",
            order_id="500",
            sku="SKU-RES",
            name="Reserved item",
            size="42",
            goods_type="Не обработанный",
            qty=50,
            available_qty=0,
            processing_reserved_qty=50,
            pallet_code="PAL-RES-9901",
        )
        WarehouseReserve.objects.create(
            agency=self.agency,
            reserve_type=WarehouseReserve.TYPE_PROCESSING,
            context_type="processing",
            context_id=self.order_id,
            sku_code="SKU-RES",
            size="42",
            goods_type="Не обработанный",
            qty_reserved=50,
            status=WarehouseReserve.STATUS_ACTIVE,
        )

    def _stock_picker_qty(self, response) -> int:
        self.assertEqual(response.status_code, 200)
        items = json.loads(response.context["inventory_items_json"] or "[]")
        return sum(int(item.get("qty") or 0) for item in items if (item.get("sku") or "") == "SKU-RES")

    def test_stock_picker_without_order_context_keeps_item_unavailable(self):
        response = self.client.get(f"/orders/processing/stock/?client={self.agency.id}")
        self.assertEqual(self._stock_picker_qty(response), 0)

    def test_stock_picker_uses_referer_processing_order_for_reserve_exclusion(self):
        response = self.client.get(
            f"/orders/processing/stock/?client={self.agency.id}",
            HTTP_REFERER=f"http://testserver/orders/processing/{self.order_id}/work/",
        )
        self.assertEqual(self._stock_picker_qty(response), 50)

    def test_stock_picker_uses_card_referer_processing_order_for_reserve_exclusion(self):
        response = self.client.get(
            f"/orders/processing/stock/?client={self.agency.id}",
            HTTP_REFERER=(
                "http://testserver/orders/processing/"
                f"{self.order_id}/card/card_1772333219271_0/?article=SKU-RES"
            ),
        )
        self.assertEqual(self._stock_picker_qty(response), 50)

    def test_stock_picker_hides_stock_reserved_for_shipping(self):
        WarehouseReserve.objects.filter(agency=self.agency).delete()
        WarehouseStockSnapshot.objects.filter(agency=self.agency, sku_code="SKU-RES").update(
            available_qty=0,
            processing_reserved_qty=0,
            shipping_reserved_qty=50,
        )
        WarehouseReserve.objects.create(
            agency=self.agency,
            reserve_type=WarehouseReserve.TYPE_SHIPPING,
            context_type="shipping",
            context_id="SO-RES-1",
            sku_code="SKU-RES",
            size="42",
            goods_type="Не обработанный",
            qty_reserved=50,
            status=WarehouseReserve.STATUS_ACTIVE,
        )

        response = self.client.get(f"/orders/processing/stock/?client={self.agency.id}")

        self.assertEqual(self._stock_picker_qty(response), 0)

    def test_stock_picker_returns_box_rows_for_processing_choice(self):
        create_warehouse_snapshot_row(
            agency=self.agency,
            order_type="receiving",
            order_id="REC-BOX-PROC-1",
            sku="SKU-BOX-PROC",
            name="Boxed processing item",
            size="L",
            barcode="BAR-BOX-PROC",
            goods_type="op",
            qty=20,
            available_qty=20,
            pallet_code="PAL-BOX-PROC",
            box_code="BOX-PROC-1",
        )
        create_warehouse_snapshot_row(
            agency=self.agency,
            order_type="receiving",
            order_id="REC-BOX-PROC-2",
            sku="SKU-BOX-PROC",
            name="Boxed processing item",
            size="L",
            barcode="BAR-BOX-PROC",
            goods_type="op",
            qty=20,
            available_qty=20,
            pallet_code="PAL-BOX-PROC",
            box_code="BOX-PROC-2",
        )

        response = self.client.get(f"/orders/processing/stock/?client={self.agency.id}")

        self.assertEqual(response.status_code, 200)
        rows = json.loads(response.context["stock_rows_json"] or "[]")
        row = next(item for item in rows if item.get("sku_code") == "SKU-BOX-PROC")
        self.assertEqual(row["box_qty"], 20)
        self.assertEqual(row["available_boxes"], 2)
        self.assertEqual(row["available_qty"], 40)
        self.assertEqual(row["box_codes"], ["BOX-PROC-1", "BOX-PROC-2"])
        direct_row = next(item for item in _processing_box_picker_rows(self.agency) if item.get("sku_code") == "SKU-BOX-PROC")
        self.assertEqual(direct_row["box_codes"], row["box_codes"])

    def test_processing_home_context_includes_box_rows_for_inline_picker(self):
        create_warehouse_snapshot_row(
            agency=self.agency,
            order_type="receiving",
            order_id="REC-BOX-PROC-HOME-1",
            sku="SKU-BOX-HOME",
            name="Home boxed item",
            size="L",
            barcode="BAR-BOX-HOME",
            goods_type="op",
            qty=20,
            available_qty=20,
            pallet_code="PAL-BOX-HOME",
            box_code="BOX-HOME-1",
        )

        response = self.client.get(f"/orders/processing/?client={self.agency.id}")

        self.assertEqual(response.status_code, 200)
        rows = json.loads(response.context["stock_rows_json"] or "[]")
        row = next(item for item in rows if item.get("sku_code") == "SKU-BOX-HOME")
        self.assertEqual(row["available_boxes"], 1)
        self.assertEqual(row["box_codes"], ["BOX-HOME-1"])

    def test_processing_home_renders_inline_stock_picker_without_popup_launcher(self):
        response = self.client.get(f"/orders/processing/?client={self.agency.id}")
        picker_path = Path(__file__).resolve().parent / "templates" / "processing" / "_client_stock_picker.html"
        picker_source = picker_path.read_text(encoding="utf-8")

        self.assertEqual(response.status_code, 200)
        self.assertIn('id="client-processing-stock-table"', picker_source)
        self.assertIn('id="client-processing-stock-filter-name"', picker_source)
        self.assertIn('id="client-processing-stock-filter-article"', picker_source)
        self.assertIn('id="client-processing-stock-filter-barcode"', picker_source)
        self.assertNotContains(response, 'id="add-product-btn"', html=False)
        self.assertNotContains(response, "window.open(", html=False)
        self.assertNotContains(response, "cta-round", html=False)

    def test_processing_home_renders_live_selected_quantity_summary(self):
        template_path = Path(__file__).resolve().parent / "templates" / "processing" / "form_client_lk.html"
        source = template_path.read_text(encoding="utf-8")

        self.assertIn('id="processing-selection-summary"', source)
        self.assertIn('id="selected-line-count"', source)
        self.assertIn('id="selected-box-count"', source)
        self.assertIn('id="selected-item-count"', source)
        self.assertIn("Выбрано в заявку", source)
        self.assertIn("setText('selected-item-count', items)", source)

    def test_processing_home_renders_new_packaging_params(self):
        response = self.client.get(f"/orders/processing/?client={self.agency.id}")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'name="individual_box_needed"', html=False)
        self.assertContains(response, 'name="vacuum_pack_needed"', html=False)

    def test_processing_params_include_individual_box_and_vacuum_pack(self):
        from .web_ui import _available_processing_param_labels, _processing_params_from_payload

        labels = _available_processing_param_labels({})
        self.assertIn("Упаковка в индивидуальную коробку", labels)
        self.assertIn("Вакуумная упаковка", labels)
        self.assertIn("Упаковка в курьерский пакет", labels)
        self.assertIn("Упаковка в пакет зип лок", labels)
        self.assertIn("Скрепление скотчем", labels)
        self.assertIn("Замена короба", labels)
        self.assertIn("Упаковка в стрейч пленку", labels)

        params = _processing_params_from_payload(
            {
                "individual_box_needed": "Да",
                "vacuum_pack_needed": "Да",
            }
        )
        params_by_label = {row["label"]: row["value"] for row in params}
        self.assertEqual(params_by_label["Упаковка в индивидуальную коробку"], "Да")
        self.assertEqual(params_by_label["Вакуумная упаковка"], "Да")


class ProcessingStockAvailabilityContractTests(TestCase):
    def setUp(self):
        self.agency = Agency.objects.create(agn_name="Contract Agency")
        create_warehouse_snapshot_row(
            agency=self.agency,
            order_type="receiving",
            order_id="901",
            sku="SKU-CONTRACT",
            name="Contract Item",
            size="44",
            goods_type="Не обработанный",
            qty=70,
            available_qty=10,
            processing_reserved_qty=60,
            pallet_code="PAL-CONTRACT",
        )
        WarehouseReserve.objects.create(
            agency=self.agency,
            reserve_type=WarehouseReserve.TYPE_PROCESSING,
            context_type="processing",
            context_id="5001",
            sku_code="SKU-CONTRACT",
            size="44",
            goods_type="Не обработанный",
            qty_reserved=50,
            status=WarehouseReserve.STATUS_ACTIVE,
        )
        WarehouseReserve.objects.create(
            agency=self.agency,
            reserve_type=WarehouseReserve.TYPE_PROCESSING,
            context_type="processing",
            context_id="5002",
            sku_code="SKU-CONTRACT",
            size="44",
            goods_type="Не обработанный",
            qty_reserved=10,
            status=WarehouseReserve.STATUS_ACTIVE,
        )

    @staticmethod
    def _qty(items: list[dict]) -> int:
        return sum(
            int(item.get("qty") or 0)
            for item in items
            if (item.get("sku") or "").strip() == "SKU-CONTRACT"
        )

    def test_processing_app_wrapper_matches_stock_service_default(self):
        service_items = StockAvailabilityService.inventory_items_for_agency(self.agency)
        processing_items = _inventory_items_for_agency(self.agency)
        self.assertEqual(processing_items, service_items)
        self.assertEqual(self._qty(service_items), 10)

    def test_processing_app_wrapper_matches_stock_service_with_exclusion(self):
        service_items = StockAvailabilityService.inventory_items_for_agency(
            self.agency,
            exclude_processing_order_id="5001",
        )
        processing_items = _inventory_items_for_agency(self.agency, exclude_order_id="5001")
        self.assertEqual(processing_items, service_items)
        self.assertEqual(self._qty(service_items), 60)


class ProcessingReserveMaterializedAvailabilityTests(TestCase):
    def setUp(self):
        self.agency = Agency.objects.create(agn_name="Reserve Refresh Agency")
        create_warehouse_snapshot_row(
            agency=self.agency,
            order_type="receiving",
            order_id="910",
            sku="SKU-RESERVE",
            name="Reserve Item",
            size="44",
            goods_type="Не обработанный",
            qty=70,
            available_qty=70,
            pallet_code="PAL-RESERVE-1",
            row=1,
            section=1,
            tier=1,
            cell=2,
        )

    def test_replace_processing_reserves_updates_available_qty_for_affected_key(self):
        _replace_processing_reserves(
            "P-100",
            self.agency,
            [
                {
                    "sku": "SKU-RESERVE",
                    "size": "44",
                    "goods_type": "Не обработанный",
                    "qty": 50,
                }
            ],
        )

        snapshot = WarehouseStockSnapshot.objects.get(agency=self.agency, sku_code="SKU-RESERVE", size="44")
        self.assertEqual(snapshot.processing_reserved_qty, 50)
        self.assertEqual(snapshot.shipping_reserved_qty, 0)
        self.assertEqual(snapshot.available_qty, 20)

    def test_replace_processing_reserves_syncs_warehouse_reserve_when_pallet_known(self):
        _replace_processing_reserves(
            "P-101",
            self.agency,
            [
                {
                    "sku": "SKU-RESERVE",
                    "size": "44",
                    "goods_type": "Не обработанный",
                    "qty": 30,
                }
            ],
        )

        warehouse_reserve = WarehouseReserve.objects.get(
            agency=self.agency,
            reserve_type=WarehouseReserve.TYPE_PROCESSING,
            context_type="processing",
            context_id="P-101",
        )
        snapshot = WarehouseStockSnapshot.objects.get(
            agency=self.agency,
            container_code="PAL-RESERVE-1",
            sku_code="SKU-RESERVE",
            size="44",
        )
        self.assertEqual(warehouse_reserve.qty_reserved, 30)
        self.assertEqual(snapshot.processing_reserved_qty, 30)
        self.assertEqual(snapshot.available_qty, 40)
        self.assertEqual(snapshot.warehouse_state_code, "reserved_for_processing")

    def test_replace_processing_reserves_treats_placeholder_barcode_as_empty(self):
        _replace_processing_reserves(
            "P-103",
            self.agency,
            [
                {
                    "sku": "SKU-RESERVE",
                    "size": "44",
                    "barcode": "-",
                    "goods_type": "Не обработанный",
                    "qty": 25,
                }
            ],
        )

        warehouse_reserve = WarehouseReserve.objects.get(
            agency=self.agency,
            reserve_type=WarehouseReserve.TYPE_PROCESSING,
            context_type="processing",
            context_id="P-103",
        )
        snapshot = WarehouseStockSnapshot.objects.get(
            agency=self.agency,
            container_code="PAL-RESERVE-1",
            sku_code="SKU-RESERVE",
            size="44",
        )
        self.assertEqual(warehouse_reserve.barcode, "")
        self.assertEqual(warehouse_reserve.qty_reserved, 25)
        self.assertEqual(snapshot.processing_reserved_qty, 25)
        self.assertEqual(snapshot.available_qty, 45)

    def test_replace_processing_reserves_uses_selected_box_codes(self):
        create_warehouse_snapshot_row(
            agency=self.agency,
            order_type="receiving",
            order_id="REC-BOX-1",
            sku="SKU-BOX-RESERVE",
            name="Box reserve item",
            size="44",
            barcode="BAR-BOX-RESERVE",
            goods_type="op",
            qty=10,
            available_qty=10,
            pallet_code="PAL-BOX-RESERVE",
            box_code="BOX-RESERVE-1",
        )
        create_warehouse_snapshot_row(
            agency=self.agency,
            order_type="receiving",
            order_id="REC-BOX-2",
            sku="SKU-BOX-RESERVE",
            name="Box reserve item",
            size="44",
            barcode="BAR-BOX-RESERVE",
            goods_type="op",
            qty=10,
            available_qty=10,
            pallet_code="PAL-BOX-RESERVE",
            box_code="BOX-RESERVE-2",
        )

        _replace_processing_reserves(
            "P-BOX",
            self.agency,
            [
                {
                    "sku": "SKU-BOX-RESERVE",
                    "size": "44",
                    "barcode": "BAR-BOX-RESERVE",
                    "goods_type": "op",
                    "qty": 10,
                    "box_codes": ["BOX-RESERVE-2"],
                }
            ],
        )

        untouched = WarehouseStockSnapshot.objects.get(agency=self.agency, container_code="BOX-RESERVE-1")
        selected = WarehouseStockSnapshot.objects.get(agency=self.agency, container_code="BOX-RESERVE-2")
        self.assertEqual(untouched.processing_reserved_qty, 0)
        self.assertEqual(untouched.available_qty, 10)
        self.assertEqual(selected.processing_reserved_qty, 10)
        self.assertEqual(selected.available_qty, 0)

    def test_replace_processing_reserves_allows_partial_selected_box(self):
        create_warehouse_snapshot_row(
            agency=self.agency,
            order_type="receiving",
            order_id="REC-SPLIT-1",
            sku="SKU-SPLIT-RESERVE",
            name="Split reserve item",
            size="44",
            barcode="BAR-SPLIT-RESERVE",
            goods_type="op",
            qty=10,
            available_qty=10,
            pallet_code="PAL-SPLIT-RESERVE",
            box_code="BOX-SPLIT-RESERVE",
        )

        _replace_processing_reserves(
            "P-SPLIT",
            self.agency,
            [
                {
                    "sku": "SKU-SPLIT-RESERVE",
                    "size": "44",
                    "barcode": "BAR-SPLIT-RESERVE",
                    "goods_type": "op",
                    "qty": 4,
                    "box_codes": ["BOX-SPLIT-RESERVE"],
                    "allow_partial_box_reserve": True,
                }
            ],
        )

        snapshot = WarehouseStockSnapshot.objects.get(agency=self.agency, container_code="BOX-SPLIT-RESERVE")
        self.assertEqual(snapshot.processing_reserved_qty, 4)
        self.assertEqual(snapshot.available_qty, 6)

    def test_processing_reserve_rows_for_order_return_outstanding_warehouse_qty(self):
        WarehouseReserve.objects.create(
            agency=self.agency,
            reserve_type=WarehouseReserve.TYPE_PROCESSING,
            context_type="processing",
            context_id="P-102",
            sku_code="SKU-RESERVE",
            size="44",
            goods_type="Не обработанный",
            qty_reserved=30,
            qty_satisfied=8,
            status=WarehouseReserve.STATUS_PARTIALLY_SATISFIED,
        )

        rows = _processing_reserve_rows_for_order("P-102", self.agency)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["sku"], "SKU-RESERVE")
        self.assertEqual(rows[0]["qty"], 22)


class ProcessingWarehouseOperationBridgeTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="proc_bridge_user", password="x")
        Employee.objects.create(
            full_name="Processing Bridge User",
            user=self.user,
            role="processing_head",
            is_active=True,
        )
        self.client.force_login(self.user)
        self.agency = Agency.objects.create(agn_name="Processing Bridge Agency")
        self.request_factory = RequestFactory()
        self.processing_location = WarehouseWritePathService.ensure_location(
            warehouse_code="MSK",
            zone_code="OBR",
        )

    def _create_processing_snapshot(
        self,
        *,
        order_id: str,
        state_code: str,
        active_operation: WarehouseOperation | None = None,
    ) -> WarehouseStockSnapshot:
        snapshot = WarehouseStockSnapshot.objects.create(
            agency=self.agency,
            source_context_type="legacy_stock",
            source_context_id=f"legacy-{order_id}",
            sku_code="SKU-PROC",
            name="Processing Item",
            size="42",
            barcode="BC-PROC-42",
            goods_type="gv",
            qty=10,
            available_qty=0,
            processing_reserved_qty=10,
            container_code=f"PAL-{order_id}",
            location=self.processing_location,
            zone_code=self.processing_location.zone_code,
            zone_kind=self.processing_location.zone_kind,
            warehouse_state_code=state_code,
            active_operation=active_operation,
            active_operation_type=active_operation.operation_type if active_operation else "",
        )
        WarehouseReserve.objects.create(
            agency=self.agency,
            reserve_type=WarehouseReserve.TYPE_PROCESSING,
            context_type="processing",
            context_id=order_id,
            sku_code=snapshot.sku_code,
            size=snapshot.size,
            barcode=snapshot.barcode,
            goods_type=snapshot.goods_type,
            qty_reserved=10,
            status=WarehouseReserve.STATUS_ACTIVE,
            source_document_type="processing_order",
            source_document_id=order_id,
            created_by=self.user,
        )
        return snapshot

    def test_take_processing_starts_warehouse_processing_when_goods_are_in_obr(self):
        order_id = "P-200"
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="processing",
            action="status",
            agency=self.agency,
            payload={
                "status": "processing_head",
                "status_label": "Передано в обработку",
                "cards": [{"id": "card-a", "article": "SKU-PROC", "rows": [{"size": "42", "qty": "10"}]}],
            },
        )
        snapshot = self._create_processing_snapshot(
            order_id=order_id,
            state_code="in_processing_zone",
        )

        response = self.client.post(
            f"/orders/processing/{order_id}/",
            {"action": "take_processing"},
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, f"/orders/processing/{order_id}/work/")
        operation = WarehouseOperation.objects.get(
            agency=self.agency,
            operation_type=WarehouseOperation.TYPE_PROCESSING,
            context_type="processing",
            context_id=order_id,
        )
        snapshot.refresh_from_db()
        self.assertEqual(operation.status, WarehouseOperation.STATUS_IN_PROGRESS)
        self.assertEqual(snapshot.warehouse_state_code, "processing_in_progress")
        self.assertEqual(snapshot.active_operation_id, operation.id)

    def test_take_processing_redirects_to_work_when_warehouse_already_in_progress(self):
        order_id = "P-200-INPROGRESS"
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="processing",
            action="status",
            agency=self.agency,
            payload={
                "status": "processing_head",
                "status_label": "Передано в обработку",
                "cards": [{"id": "card-a", "article": "SKU-PROC", "rows": [{"size": "42", "qty": "10"}]}],
            },
        )
        self._create_processing_snapshot(
            order_id=order_id,
            state_code="processing_in_progress",
        )

        response = self.client.post(
            f"/orders/processing/{order_id}/",
            {"action": "take_processing"},
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, f"/orders/processing/{order_id}/work/")
        self.assertEqual(
            WarehouseOperation.objects.filter(
                agency=self.agency,
                operation_type=WarehouseOperation.TYPE_PROCESSING,
                context_type="processing",
                context_id=order_id,
            ).count(),
            0,
        )

    def test_processing_home_redirects_processing_head_to_dashboard(self):
        response = self.client.get("/orders/processing/")

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, "/processing-head/?tab=all&period=all")

    def test_processing_work_redirects_processing_head_before_manager_approval(self):
        order_id = "P-200-WAITING"
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="processing",
            action="status",
            agency=self.agency,
            payload={
                "status": "sent_unconfirmed",
                "submit_action": "submitted",
                "status_label": "Ждет подтверждения",
                "processing_stage": "awaiting_approval",
                "processing_stage_label": "Ждет подтверждения",
                "article": "SKU-PROC",
            },
        )

        response = self.client.get(f"/orders/processing/{order_id}/work/")

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, "/processing-head/?tab=all&period=all")

    def test_processing_work_page_prefers_warehouse_status_label(self):
        order_id = "P-200A"
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="processing",
            action="status",
            agency=self.agency,
            payload={
                "status": "processing_head",
                "status_label": "Передано в обработку",
                "cards": [{"id": "card-a", "article": "SKU-PROC", "rows": [{"size": "42", "qty": "10"}]}],
            },
        )
        self._create_processing_snapshot(
            order_id=order_id,
            state_code="processing_in_progress",
        )

        response = self.client.get(f"/orders/processing/{order_id}/work/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["status_label"], "Товар в обработке")

    def test_processing_work_page_shows_quality_subzone_and_hides_repeated_delivery(self):
        order_id = "P-200A-QC"
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="processing",
            action="status",
            agency=self.agency,
            payload={
                "status": "processing_in_work",
                "status_label": "В работе",
                "processing_stage": PROCESSING_STAGE_QUALITY_CONTROL,
                "processing_stage_label": "Проверка качества",
                "cards": [
                    {"id": "card-a", "article": "SKU-PROC", "rows": [{"size": "42", "qty": "10"}]},
                ],
                "processed_cards": ["card-a"],
                "processing_results": [
                    {"card_id": "card-a", "article": "SKU-PROC", "size": "42", "processed": "10"},
                ],
            },
        )
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="processing",
            action="update",
            agency=self.agency,
            payload={
                "act": "placement",
                "act_state": "closed",
                "act_boxes": [
                    {"code": "BOX-QC-1", "items": [{"sku": "SKU-PROC", "size": "42", "qty": 10}]},
                ],
                "act_pallets": [
                    {"code": "PAL-QC-1", "boxes": ["BOX-QC-1"], "items": []},
                ],
            },
        )
        self._create_processing_snapshot(
            order_id=order_id,
            state_code="processing_in_progress",
        )

        response = self.client.get(f"/orders/processing/{order_id}/work/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "OBR-QC · Проверка качества")
        self.assertContains(response, "Подтверждение результата")
        self.assertNotContains(response, 'id="take-work-reachtruck-btn"')

    def test_processing_work_page_allows_stale_payload_when_warehouse_in_progress(self):
        order_id = "P-200A-WH-ONLY"
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="processing",
            action="status",
            agency=self.agency,
            payload={
                "status": "draft",
                "status_label": "Черновик",
                "cards": [{"id": "card-a", "article": "SKU-PROC", "rows": [{"size": "42", "qty": "10"}]}],
            },
        )
        self._create_processing_snapshot(
            order_id=order_id,
            state_code="processing_in_progress",
        )

        response = self.client.get(f"/orders/processing/{order_id}/work/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["status_label"], "Товар в обработке")

    def test_processing_detail_page_prefers_warehouse_status_label(self):
        order_id = "P-200B"
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="processing",
            action="status",
            agency=self.agency,
            payload={
                "status": "processing_head",
                "status_label": "Передано в обработку",
                "article": "SKU-PROC",
                "product_name": "Товар обработки",
            },
        )
        self._create_processing_snapshot(
            order_id=order_id,
            state_code="processing_in_progress",
        )

        response = self.client.get(f"/orders/processing/{order_id}/")

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, f"/orders/processing/{order_id}/work/")

    def test_processing_detail_page_prefers_warehouse_status_label_for_manager_view(self):
        order_id = "P-200B-MANAGER"
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="processing",
            action="status",
            agency=self.agency,
            payload={
                "status": "processing_head",
                "status_label": "Передано в обработку",
                "article": "SKU-PROC",
                "product_name": "Товар обработки",
            },
        )
        self._create_processing_snapshot(
            order_id=order_id,
            state_code="processing_in_progress",
        )
        manager_user = get_user_model().objects.create_user(username="proc_detail_manager", password="x")
        Employee.objects.create(
            full_name="Processing Detail Manager",
            user=manager_user,
            role="manager",
            is_active=True,
        )
        self.client.force_login(manager_user)

        response = self.client.get(f"/orders/processing/{order_id}/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["status_label"], "Товар в обработке")

    def test_processing_work_page_allows_manager_view(self):
        order_id = "P-200B-MANAGER-WORK"
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="processing",
            action="status",
            agency=self.agency,
            payload={
                "status": "processing_in_work",
                "status_label": "Взята в работу",
                "cards": [{"id": "card-a", "article": "SKU-PROC", "rows": [{"size": "42", "qty": "10"}]}],
                "processed_cards": ["card-a"],
                "processing_results": [
                    {
                        "card_id": "card-a",
                        "article": "SKU-PROC",
                        "size": "42",
                        "destination": "-",
                        "processed": "10",
                    }
                ],
            },
        )
        manager_user = get_user_model().objects.create_user(username="proc_work_manager", password="x")
        Employee.objects.create(
            full_name="Processing Work Manager",
            user=manager_user,
            role="manager",
            is_active=True,
        )
        self.client.force_login(manager_user)

        response = self.client.get(f"/orders/processing/{order_id}/work/")
        self.assertEqual(response.status_code, 200)
        # Write actions stay restricted.
        denied = self.client.post(
            f"/orders/processing/{order_id}/work/",
            {"action": "finish_processing"},
        )
        self.assertEqual(denied.status_code, 403)

    def test_processing_card_page_allows_stale_payload_when_warehouse_in_progress(self):
        order_id = "P-200B-WH-ONLY"
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="processing",
            action="status",
            agency=self.agency,
            payload={
                "status": "draft",
                "status_label": "Черновик",
                "cards": [{"id": "card-a", "article": "SKU-PROC", "rows": [{"size": "42", "qty": "10"}]}],
            },
        )
        self._create_processing_snapshot(
            order_id=order_id,
            state_code="processing_in_progress",
        )

        response = self.client.get(f"/orders/processing/{order_id}/card/card-a/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["status_label"], "Товар в обработке")

    def test_processing_work_completion_finishes_warehouse_processing_operation(self):
        order_id = "P-201"
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="processing",
            action="status",
            agency=self.agency,
            payload={
                "status": "processing_in_work",
                "status_label": "Взята в работу",
                "processing_stage": PROCESSING_STAGE_QUALITY_APPROVED,
                "processing_stage_label": "проверка качества пройдена",
                "quality_review": {"status": "approved"},
                "cards": [{"id": "card-a", "article": "SKU-PROC", "rows": [{"size": "42", "qty": "10"}]}],
                "processed_cards": ["card-a"],
                "processing_results": [
                    {
                        "card_id": "card-a",
                        "article": "SKU-PROC",
                        "size": "42",
                        "destination": "-",
                        "processed": "10",
                    }
                ],
            },
        )
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="processing",
            action="status",
            agency=self.agency,
            payload={
                "status": "processing_in_work",
                "status_label": "Взята в работу",
                "cards": [{"id": "card-a", "article": "SKU-PROC", "rows": [{"size": "42", "qty": "10"}]}],
                "processed_cards": ["card-a"],
                "processing_results": [
                    {
                        "card_id": "card-a",
                        "article": "SKU-PROC",
                        "size": "42",
                        "destination": "-",
                        "processed": "10",
                    }
                ],
                "act": "placement",
                "act_state": "closed",
                "processing_stage": PROCESSING_STAGE_QUALITY_APPROVED,
                "processing_stage_label": "проверка качества пройдена",
                "quality_review": {"status": "approved"},
                "act_boxes": [{"code": "BOX-PROC-1", "items": [{"sku": "SKU-PROC", "qty": 10}]}],
                "act_pallets": [
                    {
                        "code": "PAL-PROC-1",
                        "boxes": ["BOX-PROC-1"],
                        "items": [],
                        "location": {"zone": "MR", "row": 1, "section": "", "tier": "", "cell": ""},
                    }
                ],
            },
        )
        operation = WarehouseOperation.objects.create(
            agency=self.agency,
            operation_type=WarehouseOperation.TYPE_PROCESSING,
            context_type="processing",
            context_id=order_id,
            source_location=self.processing_location,
            destination_location=self.processing_location,
            source_zone_code=self.processing_location.zone_code,
            destination_zone_code=self.processing_location.zone_code,
            status=WarehouseOperation.STATUS_IN_PROGRESS,
            planned_qty=10,
            started_at=timezone.now(),
        )
        snapshot = self._create_processing_snapshot(
            order_id=order_id,
            state_code="processing_in_progress",
            active_operation=operation,
        )

        request = self.request_factory.post(
            f"/orders/processing/{order_id}/work/",
            {"action": "finish_processing"},
        )
        request.user = self.user
        view = ProcessingWorkView()
        view.setup(request, order_id=order_id)
        with mock.patch(
            "processing_app.views._processing_finish_checks",
            return_value={
                "blockers": [],
                "placement_payload": {
                    "act_boxes": [{"code": "BOX-PROC-1"}],
                    "act_pallets": [{"code": "PAL-PROC-1"}],
                },
                "placement_closed": True,
                "has_boxes": True,
                "has_pallets": True,
                "warehouse_move_created": True,
                "warehouse_move_completed": True,
                "warehouse_move_progress": {
                    "total_pallets": 1,
                    "done_count": 1,
                    "created_count": 0,
                    "in_progress_count": 0,
                    "active_count": 0,
                    "canceled_count": 0,
                    "not_created_count": 0,
                    "pending_count": 0,
                    "moves_by_pallet": {},
                    "implicit_done_codes": {"PAL-PROC-1"},
                },
            },
        ), mock.patch("processing_app.views._processing_discrepancy_rows", return_value=[]):
            response = view.post(request, order_id=order_id)
        self.assertEqual(response.status_code, 302)
        operation.refresh_from_db()
        snapshot.refresh_from_db()
        reserve = WarehouseReserve.objects.get(
            agency=self.agency,
            reserve_type=WarehouseReserve.TYPE_PROCESSING,
            context_type="processing",
            context_id=order_id,
        )
        self.assertEqual(operation.status, WarehouseOperation.STATUS_DONE)
        self.assertEqual(snapshot.warehouse_state_code, "placed_after_processing")
        self.assertEqual(snapshot.processing_reserved_qty, 0)
        self.assertEqual(snapshot.available_qty, 10)
        self.assertIsNone(snapshot.active_operation_id)
        self.assertEqual(reserve.status, WarehouseReserve.STATUS_RELEASED)


class ProcessingQualityControlTests(TestCase):
    def setUp(self):
        self.order_id = "QC-100"
        self.head_user = get_user_model().objects.create_user(
            username="processing_quality_head",
            password="x",
        )
        self.head = Employee.objects.create(
            full_name="Руководитель проверки",
            user=self.head_user,
            role="processing_head",
            is_active=True,
        )
        self.worker_user = get_user_model().objects.create_user(
            username="processing_quality_worker",
            password="x",
        )
        self.worker = Employee.objects.create(
            full_name="Исполнитель доработки",
            user=self.worker_user,
            role="processing_worker",
            is_active=True,
        )
        self.agency = Agency.objects.create(agn_name="Клиент проверки качества")
        self.request_factory = RequestFactory()
        OrderAuditEntry.objects.create(
            order_id=self.order_id,
            order_type="processing",
            action="status",
            agency=self.agency,
            payload={
                "status": "processing_in_work",
                "status_label": "На проверке качества",
                "processing_stage": PROCESSING_STAGE_QUALITY_CONTROL,
                "processing_stage_label": "на проверке качества",
                "quality_review": {"status": "pending"},
                "cards": [
                    {
                        "id": "card-a",
                        "article": "SKU-QC",
                        "product_name": "Товар для проверки",
                        "processed_done": True,
                        "processed_at": timezone.localtime().isoformat(),
                        "rows": [{"size": "42", "barcode": "BAR-QC", "qty": "10"}],
                    }
                ],
                "processed_cards": ["card-a"],
                "processing_results": [
                    {
                        "card_id": "card-a",
                        "article": "SKU-QC",
                        "size": "42",
                        "barcode": "BAR-QC",
                        "destination": "-",
                        "processed": "10",
                    }
                ],
                "act": "placement",
                "act_state": "closed",
                "act_boxes": [
                    {"code": "BOX-QC-1", "items": [{"sku": "SKU-QC", "qty": 10}]}
                ],
                "act_pallets": [
                    {"code": "PAL-QC-1", "boxes": ["BOX-QC-1"], "items": []}
                ],
            },
        )

    def _entries(self):
        return list(
            OrderAuditEntry.objects.filter(
                order_id=self.order_id,
                order_type="processing",
            ).order_by("created_at", "id")
        )

    def _request(self, data, *, user=None):
        request = self.request_factory.post(
            f"/orders/processing/{self.order_id}/work/",
            data=data,
        )
        request.user = user or self.head_user
        return request

    @staticmethod
    def _approval_data():
        return {
            "action": "approve_quality",
            "quality_quantity_checked": "1",
            "quality_processing_checked": "1",
            "quality_marking_checked": "1",
            "quality_packaging_checked": "1",
            "quality_comment": "Проверено без замечаний",
        }

    def _return_data(self):
        return {
            "action": "return_to_rework",
            "rework_reason": "Ошибка маркировки",
            "rework_comment": "Перепроверить и заменить этикетку",
            "rework_assignee_id": str(self.worker.id),
            "rework_due_at": (
                timezone.localtime() + timedelta(hours=2)
            ).isoformat(),
            "rework_card_ids": ["card-a"],
        }

    def test_quality_approval_requires_complete_checklist(self):
        result = ProcessingWorkflowService.review_processing_quality(
            order_id=self.order_id,
            entries=self._entries(),
            request=self._request({"action": "approve_quality"}),
            role="processing_head",
        )

        self.assertEqual(result.status, "checklist_incomplete")
        self.assertFalse(
            OrderAuditEntry.objects.filter(
                order_id=self.order_id,
                payload__processing_stage=PROCESSING_STAGE_QUALITY_APPROVED,
            ).exists()
        )

    def test_quality_approval_is_idempotent(self):
        with mock.patch(
            "processing_app.views._processing_finish_checks",
            return_value={"hard_blockers": []},
        ):
            first = ProcessingWorkflowService.review_processing_quality(
                order_id=self.order_id,
                entries=self._entries(),
                request=self._request(self._approval_data()),
                role="processing_head",
            )
            second = ProcessingWorkflowService.review_processing_quality(
                order_id=self.order_id,
                entries=self._entries(),
                request=self._request(self._approval_data()),
                role="processing_head",
            )

        self.assertEqual(first.status, "approved")
        self.assertEqual(second.status, "already_approved")
        self.assertEqual(
            OrderAuditEntry.objects.filter(
                order_id=self.order_id,
                payload__processing_stage=PROCESSING_STAGE_QUALITY_APPROVED,
            ).count(),
            1,
        )
        latest_payload = self._entries()[-1].payload
        self.assertEqual(latest_payload["quality_review"]["status"], "approved")
        self.assertEqual(
            latest_payload["processing_card_subzones"]["card-a"],
            PROCESSING_SUBZONE_OUT,
        )

    def test_quality_return_requires_reason(self):
        data = self._return_data()
        data["rework_reason"] = ""

        result = ProcessingWorkflowService.review_processing_quality(
            order_id=self.order_id,
            entries=self._entries(),
            request=self._request(data),
            role="processing_head",
        )

        self.assertEqual(result.status, "missing_reason")
        self.assertFalse(Task.objects.exists())

    def test_quality_return_reopens_card_task_without_duplicates(self):
        route = f"/orders/processing/{self.order_id}/card/card-a/"
        Task.objects.create(
            title="Старая задача",
            route=route,
            assigned_to=self.worker,
            status="done",
        )
        Task.objects.create(
            title="Дубликат",
            route=route,
            assigned_to=self.worker,
            status="backlog",
        )

        result = ProcessingWorkflowService.review_processing_quality(
            order_id=self.order_id,
            entries=self._entries(),
            request=self._request(self._return_data()),
            role="processing_head",
        )

        self.assertEqual(result.status, "rework")
        self.assertEqual(Task.objects.filter(route=route).exclude(status="done").count(), 1)
        task = Task.objects.filter(route=route).exclude(status="done").get()
        self.assertEqual(task.assigned_to_id, self.worker.id)
        self.assertEqual(task.priority, "high")
        latest_payload = self._entries()[-1].payload
        self.assertEqual(
            latest_payload["processing_stage"],
            PROCESSING_STAGE_REWORK,
        )
        self.assertNotIn("card-a", latest_payload["processed_cards"])
        self.assertTrue(latest_payload["cards"][0]["rework_required"])
        self.assertEqual(
            latest_payload["processing_card_subzones"]["card-a"],
            PROCESSING_SUBZONE_WORK,
        )

    def test_completed_rework_returns_to_quality_control(self):
        returned = ProcessingWorkflowService.review_processing_quality(
            order_id=self.order_id,
            entries=self._entries(),
            request=self._request(self._return_data()),
            role="processing_head",
        )
        self.assertEqual(returned.status, "rework")
        route = f"/orders/processing/{self.order_id}/card/card-a/"
        Task.objects.filter(route=route).update(status="done")
        payload = dict(self._entries()[-1].payload)
        payload["processed_cards"] = ["card-a"]
        payload["cards"][0]["processed_done"] = True
        payload["cards"][0]["processed_at"] = timezone.localtime().isoformat()
        OrderAuditEntry.objects.create(
            order_id=self.order_id,
            order_type="processing",
            action="update",
            agency=self.agency,
            user=self.worker_user,
            payload=payload,
        )
        worker_request = self._request(
            {"action": "save_results"},
            user=self.worker_user,
        )

        logged = ProcessingWorkflowService._resubmit_quality_after_rework(
            order_id=self.order_id,
            request=worker_request,
        )

        self.assertTrue(logged)
        latest_payload = self._entries()[-1].payload
        self.assertEqual(
            latest_payload["processing_stage"],
            PROCESSING_STAGE_QUALITY_CONTROL,
        )
        self.assertEqual(
            latest_payload["quality_review"]["status"],
            "pending_recheck",
        )
        self.assertEqual(
            latest_payload["processing_card_subzones"]["card-a"],
            PROCESSING_SUBZONE_QC,
        )

    def test_finish_is_blocked_until_quality_is_approved(self):
        result = ProcessingWorkflowService.finish_processing(
            order_id=self.order_id,
            entries=self._entries(),
            request=self._request({"action": "finish_processing"}),
            role="processing_head",
        )

        self.assertEqual(result.status, "blocked")
        self.assertIn("проверку качества", result.error_message)

    def test_processing_worker_cannot_review_quality(self):
        result = ProcessingWorkflowService.review_processing_quality(
            order_id=self.order_id,
            entries=self._entries(),
            request=self._request(self._approval_data(), user=self.worker_user),
            role="processing_worker",
        )

        self.assertEqual(result.status, "forbidden")

    def test_quality_stage_redirects_legacy_cards_view_to_work_table(self):
        self.client.force_login(self.head_user)

        response = self.client.get(
            f"/orders/processing/{self.order_id}/work/?view=cards&source=quality"
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response.url,
            f"/orders/processing/{self.order_id}/work/?source=quality",
        )

    def test_quality_work_table_renders_review_actions(self):
        self.client.force_login(self.head_user)

        response = self.client.get(
            f"/orders/processing/{self.order_id}/work/"
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Контроль качества обработки")
        self.assertContains(response, "Подтвердить выполнение")
        self.assertContains(response, "Вернуть на доработку")


class ProcessingSubzoneTests(SimpleTestCase):
    def test_mixed_cards_are_split_between_in_work_and_quality_control(self):
        state = build_processing_subzone_state(
            card_ids=["card-a", "card-b", "card-c"],
            processed_card_ids=["card-c"],
            assigned_card_ids=["card-b"],
            stage=PROCESSING_STAGE_TAKEN_INTO_PROCESSING,
        )

        self.assertEqual(state["current"]["key"], PROCESSING_SUBZONE_WORK)
        self.assertEqual(state["cards"]["card-a"]["key"], PROCESSING_SUBZONE_IN)
        self.assertEqual(state["cards"]["card-b"]["key"], PROCESSING_SUBZONE_WORK)
        self.assertEqual(state["cards"]["card-c"]["key"], PROCESSING_SUBZONE_QC)
        self.assertEqual(state["counts"][PROCESSING_SUBZONE_IN], 1)
        self.assertEqual(state["counts"][PROCESSING_SUBZONE_WORK], 1)
        self.assertEqual(state["counts"][PROCESSING_SUBZONE_QC], 1)

    def test_packaging_and_closed_flow_override_card_level_subzones(self):
        packing = build_processing_subzone_state(
            card_ids=["card-a", "card-b"],
            processed_card_ids=["card-a", "card-b"],
            stage=PROCESSING_STAGE_UNBOXING_OPENED,
            packaging_active=True,
        )
        quality_control = build_processing_subzone_state(
            card_ids=["card-a", "card-b"],
            processed_card_ids=["card-a", "card-b"],
            stage=PROCESSING_STAGE_UNBOXING_COMPLETED,
            placement_completed=True,
        )
        outgoing = build_processing_subzone_state(
            card_ids=["card-a", "card-b"],
            processed_card_ids=["card-a", "card-b"],
            stage=PROCESSING_STAGE_QUALITY_APPROVED,
            placement_completed=True,
        )

        self.assertEqual(packing["current"]["key"], PROCESSING_SUBZONE_PACK)
        self.assertEqual(packing["counts"][PROCESSING_SUBZONE_PACK], 2)
        self.assertEqual(quality_control["current"]["key"], PROCESSING_SUBZONE_QC)
        self.assertEqual(quality_control["counts"][PROCESSING_SUBZONE_QC], 2)
        self.assertEqual(outgoing["current"]["key"], PROCESSING_SUBZONE_OUT)
        self.assertEqual(outgoing["counts"][PROCESSING_SUBZONE_OUT], 2)

    def test_manager_approved_order_is_not_inside_an_obr_subzone(self):
        state = build_processing_subzone_state(
            card_ids=["card-a"],
            stage=PROCESSING_STAGE_MANAGER_APPROVED,
        )

        self.assertEqual(state["current"], {})
        self.assertFalse(state["is_in_obr"])
        self.assertEqual(sum(state["counts"].values()), 0)


class ProcessingWorkerCardQueueTests(TestCase):
    def setUp(self):
        self.head_user = get_user_model().objects.create_user(
            username="processing_card_head",
            password="x",
        )
        self.head = Employee.objects.create(
            full_name="Руководитель карточек",
            user=self.head_user,
            role="processing_head",
            is_active=True,
        )
        self.worker_user = get_user_model().objects.create_user(
            username="processing_card_worker",
            password="x",
        )
        self.worker = Employee.objects.create(
            full_name="Обработчик карточки",
            user=self.worker_user,
            role="processing_worker",
            is_active=True,
        )
        self.other_worker_user = get_user_model().objects.create_user(
            username="processing_card_other_worker",
            password="x",
        )
        self.other_worker = Employee.objects.create(
            full_name="Другой обработчик",
            user=self.other_worker_user,
            role="processing_worker",
            is_active=True,
        )
        self.agency = Agency.objects.create(agn_name="Клиент очереди обработки")
        self.order_id = "CARD-QUEUE-1"
        OrderAuditEntry.objects.create(
            order_id=self.order_id,
            order_type="processing",
            action="status",
            agency=self.agency,
            payload={
                "status": "processing_in_work",
                "status_label": "Взята в работу",
                "processing_stage": PROCESSING_STAGE_TAKEN_INTO_PROCESSING,
                "processing_stage_label": "взято в обработку",
                "cards": [
                    {
                        "id": "card-a",
                        "article": "SKU-A",
                        "product_name": "Товар А",
                        "rows": [{"size": "42", "barcode": "BAR-A", "qty": "10"}],
                    },
                    {
                        "id": "card-b",
                        "article": "SKU-B",
                        "product_name": "Товар Б",
                        "rows": [{"size": "44", "barcode": "BAR-B", "qty": "5"}],
                    },
                ],
            },
        )

    def test_assignment_above_declared_quantity_is_saved_with_warning(self):
        packer_user = get_user_model().objects.create_user(
            username="processing_card_packer",
            password="x",
        )
        packer = Employee.objects.create(
            full_name="Упаковщица карточки",
            user=packer_user,
            role="packer",
            is_active=True,
        )
        request = RequestFactory().post(
            f"/orders/processing/{self.order_id}/assign-card/",
            {
                "card_id": "card-a",
                "assignee_ids": [str(packer.id)],
                f"assignee_qty_{packer.id}": "12",
                "operation_key": "technical_card",
            },
        )
        request.user = self.head_user

        result = ProcessingWorkflowService.assign_processing_card(
            order_id=self.order_id,
            request=request,
        )

        self.assertEqual(result.status, "ok")
        self.assertIn("card_assign=ok", result.redirect_to)
        self.assertIn("card_assign_warning=quantity_exceeded", result.redirect_to)
        self.assertIn("card_declared_qty=10", result.redirect_to)
        self.assertIn("card_planned_qty=12", result.redirect_to)
        route = f"/orders/processing/{self.order_id}/card/card-a/"
        self.assertTrue(
            Task.objects.filter(route=route, assigned_to=packer).exclude(status="done").exists()
        )
        latest_payload = (
            OrderAuditEntry.objects.filter(
                order_id=self.order_id,
                order_type="processing",
            )
            .order_by("-id")
            .values_list("payload", flat=True)
            .first()
        )
        performer_rows = (latest_payload or {}).get("processing_card_performers", {}).get(
            "card-a",
            [],
        )
        self.assertEqual(
            [row.get("id") for row in performer_rows],
            [packer.id],
        )
        assignment = next(
            row
            for row in (latest_payload or {}).get("processing_work_assignments", [])
            if row.get("card_id") == "card-a"
        )
        self.assertEqual(assignment.get("planned_qty"), 12)
        self.assertEqual(assignment.get("status"), "assigned")

    def _assign_card(
        self,
        card_id: str = "card-a",
        worker: Employee | None = None,
        workers: list[Employee] | None = None,
    ):
        self.client.force_login(self.head_user)
        data = {"card_id": card_id}
        if workers is None:
            data["assignee_id"] = str((worker or self.worker).id)
        else:
            data["assignee_ids"] = [str(item.id) for item in workers]
        return self.client.post(
            f"/orders/processing/{self.order_id}/assign-card/",
            data,
        )

    def _enqueue_container_label(self, *, worker_user, card_id: str):
        self.client.force_login(worker_user)
        return self.client.post(
            "/orders/processing/print-jobs/",
            data=json.dumps(
                {
                    "order_id": self.order_id,
                    "card_id": card_id,
                    "barcode": f"{card_id.upper()}-1",
                    "printer_name": "Test printer",
                    "label_png_base64": "test-image",
                }
            ),
            content_type="application/json",
        )

    def test_packaging_worker_can_enqueue_box_and_pallet_labels(self):
        Task.objects.create(
            title="Packaging",
            route=f"/orders/processing/{self.order_id}/flow/",
            assigned_to=self.worker,
        )

        for card_id in ("box", "pallet"):
            with self.subTest(card_id=card_id):
                response = self._enqueue_container_label(
                    worker_user=self.worker_user,
                    card_id=card_id,
                )
                self.assertEqual(response.status_code, 200)
                self.assertTrue(
                    ProcessingPrintJob.objects.filter(
                        order_id=self.order_id,
                        card_id=card_id,
                    ).exists()
                )

    def test_other_worker_cannot_enqueue_container_label(self):
        Task.objects.create(
            title="Packaging",
            route=f"/orders/processing/{self.order_id}/flow/",
            assigned_to=self.worker,
        )

        response = self._enqueue_container_label(
            worker_user=self.other_worker_user,
            card_id="box",
        )

        self.assertEqual(response.status_code, 403)
        self.assertFalse(
            ProcessingPrintJob.objects.filter(
                order_id=self.order_id,
                card_id="box",
            ).exists()
        )

    def test_completed_packaging_task_does_not_grant_container_print_access(self):
        Task.objects.create(
            title="Packaging",
            route=f"/orders/processing/{self.order_id}/flow/",
            assigned_to=self.worker,
            status="done",
        )

        response = self._enqueue_container_label(
            worker_user=self.worker_user,
            card_id="pallet",
        )

        self.assertEqual(response.status_code, 403)

    def test_legacy_agent_commands_use_canonical_https_url(self):
        request = RequestFactory().get("/orders/processing/print-agent/start/")
        request.user = self.head_user

        with mock.patch(
            "processing_app.web_ui._get_print_agent_token",
            return_value="test-token",
        ):
            start_response = download_processing_print_agent_cmd(request)
            install_response = download_processing_print_agent_install_cmd(request)

        for response in (start_response, install_response):
            content = response.content.decode("utf-8")
            self.assertIn("https://lk.fullbox.ru", content)
            self.assertNotIn("http://93.123.255.241", content)

    def test_processing_head_assigns_multiple_workers_per_product_card(self):
        workers = [self.worker, self.other_worker]
        first = self._assign_card(workers=workers)
        second = self._assign_card(workers=workers)

        self.assertEqual(first.status_code, 302)
        self.assertIn("card_assign=ok", first.url)
        self.assertEqual(second.status_code, 302)
        self.assertIn("card_assign=already_assigned", second.url)
        route = f"/orders/processing/{self.order_id}/card/card-a/"
        tasks = Task.objects.filter(route=route).exclude(status="done")
        self.assertEqual(tasks.count(), 2)
        self.assertEqual(
            set(tasks.values_list("assigned_to_id", flat=True)),
            {self.worker.id, self.other_worker.id},
        )
        latest_payload = (
            OrderAuditEntry.objects.filter(
                order_id=self.order_id,
                order_type="processing",
            )
            .order_by("-id")
            .values_list("payload", flat=True)
            .first()
        )
        self.assertEqual(
            (latest_payload or {}).get("processing_card_subzones", {}).get("card-a"),
            PROCESSING_SUBZONE_WORK,
        )
        self.assertEqual(
            set(
                (latest_payload or {})
                .get("last_processing_card_assignment", {})
                .get("assignee_ids", [])
            ),
            {self.worker.id, self.other_worker.id},
        )

    def test_card_assignment_rejects_worker_without_cabinet_before_task_changes(self):
        worker_without_cabinet = Employee.objects.create(
            full_name="Обработчик без кабинета",
            role="processing_worker",
            is_active=True,
        )
        route = f"/orders/processing/{self.order_id}/card/card-a/"
        current_task = Task.objects.create(
            title="Current card task",
            route=route,
            assigned_to=self.worker,
            status="backlog",
        )
        audit_count = OrderAuditEntry.objects.filter(
            order_id=self.order_id,
            order_type="processing",
        ).count()

        response = self._assign_card(workers=[worker_without_cabinet])

        self.assertEqual(response.status_code, 302)
        self.assertIn("card_assign_error=worker_cabinet_missing", response.url)
        current_task.refresh_from_db()
        self.assertEqual(current_task.status, "backlog")
        self.assertEqual(
            OrderAuditEntry.objects.filter(
                order_id=self.order_id,
                order_type="processing",
            ).count(),
            audit_count,
        )
        notification = self.client.get(response.url)
        self.assertContains(
            notification,
            "У одной из выбранных упаковщиц личный кабинет не создан или отключён. Выберите другого сотрудника.",
        )

    def test_parameter_changes_and_result_saves_keep_product_card_workers(self):
        self._assign_card(workers=[self.worker, self.other_worker])
        route = f"/orders/processing/{self.order_id}/card/card-a/"
        self.client.force_login(self.head_user)

        parameter_response = self.client.post(
            route,
            {
                "action": "add_processing_param",
                "card_id": "card-a",
                "manual_param_label": "Маркировка 58/40",
                "manual_param_value": "2",
            },
        )
        self.assertEqual(parameter_response.status_code, 302)

        self.client.force_login(self.worker_user)
        save_response = self.client.post(
            route,
            {
                "action": "save_results",
                "card_id": "card-a",
                "result_article[]": ["SKU-A"],
                "result_source_article[]": ["SKU-A"],
                "result_product_name[]": ["Товар А"],
                "result_barcode[]": ["BAR-A"],
                "result_size[]": ["42"],
                "result_destination[]": ["-"],
                "result_line_key[]": ["card-a|sku-a|42|bar-a|-|0"],
                "result_received[]": ["10"],
                "result_processed[]": ["10"],
                "result_defect[]": ["0"],
                "result_shortage[]": ["0"],
                "result_labels_printed[]": ["0"],
                "result_labels_unboxed[]": ["0"],
                "result_shipped_qty[]": ["0"],
                "result_tags_replaced[]": ["0"],
                "data_match_status": "match",
                "worker_data_confirmed": "1",
            },
        )

        self.assertEqual(save_response.status_code, 302)
        active_tasks = Task.objects.filter(route=route).exclude(status="done")
        self.assertEqual(active_tasks.count(), 2)
        self.assertEqual(
            set(active_tasks.values_list("assigned_to_id", flat=True)),
            {self.worker.id, self.other_worker.id},
        )
        latest_payload = (
            OrderAuditEntry.objects.filter(
                order_id=self.order_id,
                order_type="processing",
            )
            .order_by("-id")
            .values_list("payload", flat=True)
            .first()
            or {}
        )
        self.assertEqual(
            [
                (
                    item.get("card_id"),
                    item.get("label"),
                    item.get("value"),
                )
                for item in latest_payload.get("processing_card_extra_params") or []
            ],
            [("card-a", "Маркировка 58/40", "2")],
        )

    def test_processing_head_updates_product_card_worker_list(self):
        route = f"/orders/processing/{self.order_id}/card/card-a/"
        first = self._assign_card(workers=[self.worker, self.other_worker])
        updated = self._assign_card(workers=[self.other_worker])

        self.assertIn("card_assign=ok", first.url)
        self.assertIn("card_assign=ok", updated.url)
        active_tasks = Task.objects.filter(route=route).exclude(status="done")
        self.assertEqual(active_tasks.count(), 1)
        self.assertEqual(active_tasks.get().assigned_to_id, self.other_worker.id)
        self.assertTrue(
            Task.objects.filter(
                route=route,
                assigned_to=self.worker,
                status="done",
            ).exists()
        )

    def test_multiple_assigned_workers_can_open_same_product_card(self):
        self._assign_card(workers=[self.worker, self.other_worker])

        for worker_user in (self.worker_user, self.other_worker_user):
            with self.subTest(worker=worker_user.username):
                self.client.force_login(worker_user)
                response = self.client.get(
                    f"/orders/processing/{self.order_id}/card/card-a/"
                )
                self.assertEqual(response.status_code, 200)

    def test_worker_dashboard_shows_only_assigned_product_cards(self):
        self._assign_card("card-a")
        self.client.force_login(self.worker_user)

        response = self.client.get("/processing-worker/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "SKU-A")
        self.assertNotContains(response, "SKU-B")
        self.assertContains(response, "OBR-WORK")
        self.assertEqual(response.context["queue_counts"]["active"], 1)
        self.assertEqual(len(response.context["card_queue"]), 1)

    def test_worker_cannot_open_another_workers_card(self):
        self._assign_card("card-a")
        self.client.force_login(self.other_worker_user)

        denied = self.client.get(
            f"/orders/processing/{self.order_id}/card/card-a/"
        )

        self.assertEqual(denied.status_code, 403)

    def test_worker_saves_result_and_card_task_stays_active(self):
        self._assign_card(
            "card-a",
            workers=[self.worker, self.other_worker],
        )
        route = f"/orders/processing/{self.order_id}/card/card-a/"
        self.client.force_login(self.worker_user)

        opened = self.client.get(route)
        self.assertEqual(opened.status_code, 200)
        saved = self.client.post(
            route,
            {
                "action": "save_results",
                "card_id": "card-a",
                "result_article[]": ["SKU-A"],
                "result_source_article[]": ["SKU-A"],
                "result_product_name[]": ["Товар А"],
                "result_barcode[]": ["BAR-A"],
                "result_size[]": ["42"],
                "result_destination[]": ["-"],
                "result_line_key[]": ["card-a|sku-a|42|bar-a|-|0"],
                "result_received[]": ["10"],
                "result_processed[]": ["10"],
                "result_defect[]": ["0"],
                "result_shortage[]": ["0"],
                "result_labels_printed[]": ["0"],
                "result_labels_unboxed[]": ["0"],
                "result_shipped_qty[]": ["0"],
                "result_tags_replaced[]": ["0"],
                "data_match_status": "match",
                "worker_data_confirmed": "1",
            },
        )

        self.assertEqual(saved.status_code, 302)
        tasks = Task.objects.filter(route=route)
        self.assertEqual(tasks.count(), 2)
        self.assertEqual(tasks.exclude(status="done").count(), 2)
        latest_payload = (
            OrderAuditEntry.objects.filter(
                order_id=self.order_id,
                order_type="processing",
            )
            .order_by("-id")
            .values_list("payload", flat=True)
            .first()
        )
        processed_cards = set((latest_payload or {}).get("processed_cards") or [])
        self.assertNotIn("card-a", processed_cards)
        self.assertEqual(
            (latest_payload or {}).get("processing_card_subzones", {}).get("card-a"),
            PROCESSING_SUBZONE_WORK,
        )
        dashboard = self.client.get("/processing-worker/?status=active")
        self.assertContains(dashboard, "SKU-A")
        self.assertContains(dashboard, "OBR-WORK")

    def test_worker_explicitly_completes_product_card_tasks(self):
        self._assign_card(
            "card-a",
            workers=[self.worker, self.other_worker],
        )
        route = f"/orders/processing/{self.order_id}/card/card-a/"
        self.client.force_login(self.worker_user)

        completed = self.client.post(
            route,
            {
                "action": "finish_card",
                "card_id": "card-a",
                "result_article[]": ["SKU-A"],
                "result_source_article[]": ["SKU-A"],
                "result_product_name[]": ["Товар А"],
                "result_barcode[]": ["BAR-A"],
                "result_size[]": ["42"],
                "result_destination[]": ["-"],
                "result_line_key[]": ["card-a|sku-a|42|bar-a|-|0"],
                "result_received[]": ["10"],
                "result_processed[]": ["10"],
                "result_defect[]": ["0"],
                "result_shortage[]": ["0"],
                "result_labels_printed[]": ["0"],
                "result_labels_unboxed[]": ["0"],
                "result_shipped_qty[]": ["0"],
                "result_tags_replaced[]": ["0"],
                "data_match_status": "match",
                "worker_data_confirmed": "1",
            },
        )

        self.assertEqual(completed.status_code, 302)
        tasks = Task.objects.filter(route=route)
        self.assertEqual(tasks.count(), 2)
        self.assertFalse(tasks.exclude(status="done").exists())


class ProcessingDesktopRolloutGuideTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="desktop-rollout-guide",
            password="test-password",
        )
        Employee.objects.create(
            user=self.user,
            full_name="Desktop Rollout Guide",
            role="storekeeper",
        )
        self.client.force_login(self.user)

    def test_guide_lists_bound_desktop_without_exposing_token(self):
        from agent.auth import secret_digest
        from agent.models import DeviceAgent

        DeviceAgent.objects.create(
            agent_id="desktop-auth-0123456789abcdef0123456789abcdef",
            name="desktop-comp-test",
            host="COMP-TEST",
            version="1.0.24",
            last_seen=timezone.now(),
            token_digest=secret_digest("must-not-appear"),
        )

        with mock.patch.dict(
            "os.environ",
            {"FULLBOX_DESKTOP_LEGACY_AUTH_ENABLED": "true"},
        ):
            response = self.client.get("/orders/processing/print-agent/guide/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Fullbox Desktop 1.0.24")
        self.assertContains(response, "desktop-comp-test")
        self.assertContains(response, "COMP-TEST")
        self.assertContains(response, "активен")
        self.assertContains(response, "Включён")
        self.assertNotContains(response, "must-not-appear")

    def test_guide_explains_safe_empty_rollout_state(self):
        response = self.client.get("/orders/processing/print-agent/guide/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Пока не привязан ни один Desktop")
        self.assertContains(response, "Старый режим оставлен включённым")
