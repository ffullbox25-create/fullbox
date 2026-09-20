import uuid
import html
import re
from unittest import mock

from django.contrib.auth import get_user_model
from django.template.loader import render_to_string
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from employees.models import Employee
from sklad.models import WarehouseLocation, WarehouseStockSnapshot
from sku.models import Agency, SKU, SKUBarcode
from processing_app.models import ProcessingPrintJob

from fbs.exceptions import FbsPickingError
from fbs.integrations.contracts import (
    WB_ADD_ORDER_TO_HANDOVER,
    WB_READ_HANDOVER_ORDER_IDS,
)
from fbs.integrations.http import MarketplaceHttpResponse
from fbs.models import (
    FbsBox,
    FbsControllerCheckTote,
    FbsControllerPickTote,
    FbsControllerToteOrder,
    FbsHandoverBatch,
    FbsHandoverOrderAssignment,
    FbsIntegrationProfile,
    FbsMarketplaceCommand,
    FbsOrder,
    FbsOrderItem,
    FbsOrderLabel,
    FbsOrderStockAllocation,
    FbsOrderTraceability,
    FbsPallet,
    FbsPickBatch,
    FbsPickException,
    FbsPickingCart,
    FbsProblemToteItem,
    FbsPickRestockLine,
    FbsPickRestockRequest,
    FbsPickRestockScan,
    FbsPickTask,
    FbsStockBalance,
    FbsStorageCell,
    FbsToteMovement,
    FbsUnknownToteItem,
    FbsWorkstation,
)
from fbs.services.pick_restock import (
    claim_pick_restock_request,
    confirm_marketplace_rejected_order_return,
    pick_restock_state,
    requeue_not_found_order,
    release_order_pick_restock_to_queue,
    scan_pick_restock,
)
from fbs.services.controller_problem_resolution import (
    confirm_problem_order_to_tote,
    confirm_problem_order_to_tote_without_label,
)
from fbs.services.marketplace import process_marketplace_command
from fbs.services.problems import (
    PROBLEM_TOTE_CRITICAL_MESSAGE,
    PROBLEM_TOTE_NOT_FOUND_MESSAGE,
    lookup_problem_tote_item,
    queue_verification_problem_restock,
    restore_restock_unit_to_problem_box,
    return_problem_tote_item_to_check_tote,
)
from fbs.services.totes import (
    close_controller_session,
    record_extra_problem_tote_item,
    start_controller_session,
)
from fbs.tsd_views import (
    _canceled_initial_scan_from_token,
    _verification_service_tote_prompt,
)


@override_settings(
    FBS_MODULE_ENABLED=True,
    FBS_WAREHOUSE_WRITES_ENABLED=True,
)
class FbsProblemToteRestockTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="problem-tote-picker")
        self.controller = get_user_model().objects.create_user(
            username="problem-tote-controller"
        )
        self.agency = Agency.objects.create(agn_name="Problem tote client")
        self.profile = FbsIntegrationProfile.objects.create(
            agency=self.agency,
            marketplace=FbsIntegrationProfile.MARKETPLACE_WB,
            name="Problem tote profile",
            external_warehouse_id="problem-tote-warehouse",
        )
        self.source_box = self._box(
            cell_code="FBS-PROBLEM-SOURCE-CELL",
            box_code="FBS-PROBLEM-SOURCE-BOX",
            cell_no=1,
        )
        self.quarantine_box = self._box(
            cell_code="FBS-PROBLEM-QUARANTINE-CELL",
            box_code="FBS-PROBLEM-QUARANTINE-BOX",
            cell_no=2,
        )
        self.order = FbsOrder.objects.create(
            profile=self.profile,
            external_order_id="problem-tote-order",
            internal_status=FbsOrder.STATUS_EXCEPTION,
        )
        self.order_item = FbsOrderItem.objects.create(
            order=self.order,
            external_line_id="problem-tote-line",
            external_sku="problem-tote-sku",
            barcode="4600000000001",
            quantity=1,
        )
        self.first_check_tote = FbsPickingCart.objects.create(
            barcode="FBS-CART-PROBLEM-CHECK-FIRST",
            name="Problem flow first check tote",
        )

    def _box(self, *, cell_code: str, box_code: str, cell_no: int) -> FbsBox:
        location = WarehouseLocation.objects.create(
            warehouse_code="MSK",
            zone_code="FBS",
            zone_kind=WarehouseLocation.ZONE_KIND_STORAGE,
            row_no=1,
            section_no=1,
            tier_no=1,
            cell_no=cell_no,
            location_code=cell_code,
            is_active=True,
            is_storage=True,
        )
        cell = FbsStorageCell.objects.create(
            cell_code=cell_code,
            location=location,
            is_active=True,
        )
        pallet = FbsPallet.objects.create(
            agency=self.agency,
            pallet_code=f"PAL-{cell_no}",
            cell=cell,
            status=FbsPallet.STATUS_ACTIVE,
        )
        return FbsBox.objects.create(
            agency=self.agency,
            pallet=pallet,
            box_code=box_code,
            status=FbsBox.STATUS_ACTIVE,
        )

    def _picked_allocation(self, *, marking_code: str = "", pick_task=None):
        balance = FbsStockBalance.objects.create(
            agency=self.agency,
            box=self.source_box,
            identity_key=f"source-{marking_code or 'regular'}",
            sku_code="problem-tote-sku",
            name="Problem tote item",
            barcode=self.order_item.barcode,
            marking_code=marking_code,
            qty=0,
            available_qty=0,
            reserved_qty=0,
        )
        allocation = FbsOrderStockAllocation.objects.create(
            order_item=self.order_item,
            balance=balance,
            pick_task=pick_task,
            qty_reserved=1,
            qty_picked=1,
            status=FbsOrderStockAllocation.STATUS_PICKED,
        )
        return allocation, balance

    def _controller_problem_context(self, *, suffix: str):
        workstation = FbsWorkstation.objects.create(
            barcode=f"FBS-WS-PROBLEM-{suffix}",
            name=f"Problem tote workstation {suffix}",
        )
        unknown_tote = FbsPickingCart.objects.create(
            barcode=f"FBS-CART-UNKNOWN-{suffix}",
            name=f"Unknown tote {suffix}",
        )
        problem_tote = FbsPickingCart.objects.create(
            barcode=f"FBS-CART-PROBLEM-{suffix}",
            name=f"Problem tote {suffix}",
        )
        canceled_tote = FbsPickingCart.objects.create(
            barcode=f"FBS-CART-CANCELED-{suffix}",
            name=f"Canceled tote {suffix}",
        )
        pick_tote = FbsPickingCart.objects.create(
            barcode=f"FBS-CART-PICK-{suffix}",
            name=f"Pick tote {suffix}",
        )
        session = start_controller_session(
            workstation_id=workstation.id,
            controller=self.controller,
            unknown_tote_scan=unknown_tote.barcode,
            problem_tote_scan=problem_tote.barcode,
            canceled_tote_scan=canceled_tote.barcode,
            first_check_tote_scan=self.first_check_tote.barcode,
        )
        check_tote = FbsControllerCheckTote.objects.create(
            session=session,
            tote=None,
            agency=self.agency,
            profile=self.profile,
            opened_by=self.controller,
        )
        batch = FbsPickBatch.objects.create(
            agency=self.agency,
            status=FbsPickBatch.STATUS_VERIFICATION,
            planned_qty=1,
            picked_qty=1,
            cart=pick_tote,
            workstation=workstation,
            verification_assigned_to=self.controller,
            picking_completed_at=timezone.now(),
        )
        task = FbsPickTask.objects.create(
            batch=batch,
            order=self.order,
            status=FbsPickTask.STATUS_PICKED,
            planned_qty=1,
            picked_qty=1,
        )
        allocation, balance = self._picked_allocation(pick_task=task)
        pick_context = FbsControllerPickTote.objects.create(
            session=session,
            check_tote=check_tote,
            pick_batch=batch,
            tote=pick_tote,
            planned_qty=1,
        )
        return {
            "session": session,
            "check_tote": check_tote,
            "pick_context": pick_context,
            "problem_tote": session.problem_tote,
            "canceled_tote": session.canceled_tote,
            "unused_problem_tote": problem_tote,
            "unused_canceled_tote": canceled_tote,
            "allocation": allocation,
            "balance": balance,
        }

    def test_extra_item_is_recorded_in_problem_tote_as_noncritical(self):
        context = self._controller_problem_context(suffix="EXTRA")

        row = record_extra_problem_tote_item(
            pick_batch_id=context["pick_context"].pick_batch_id,
            scanned_value="4600999999999",
            performed_by=self.controller,
        )

        self.assertEqual(row.problem_tote_id, context["problem_tote"].id)
        self.assertEqual(row.source_pick_tote_id, context["pick_context"].id)
        self.assertEqual(row.reason, "Лишний товар")
        self.assertEqual(row.severity, FbsProblemToteItem.SEVERITY_NONCRITICAL)
        self.assertEqual(FbsUnknownToteItem.objects.count(), 0)
        self.assertTrue(
            FbsToteMovement.objects.filter(
                tote=context["problem_tote"],
                target_kind="problem_tote",
                details__problem_item_id=row.id,
            ).exists()
        )

    def test_noncritical_problem_item_returns_to_source_pick_tote(self):
        context = self._controller_problem_context(suffix="RETURN")
        row = FbsProblemToteItem.objects.create(
            session=context["session"],
            problem_tote=context["problem_tote"],
            source_pick_tote=context["pick_context"],
            scanned_value=context["balance"].barcode,
            reason="Лишний товар",
            severity=FbsProblemToteItem.SEVERITY_NONCRITICAL,
            reported_by=self.controller,
        )

        lookup = lookup_problem_tote_item(
            allocation_id=context["allocation"].id,
            actor=self.controller,
        )

        self.assertEqual(lookup.item.id, row.id)
        self.assertTrue(lookup.can_return)
        self.assertIn(context["pick_context"].tote.barcode, lookup.message)
        self.assertIn("исходную тару подбора", lookup.message)
        returned = return_problem_tote_item_to_check_tote(
            allocation_id=context["allocation"].id,
            problem_item_id=row.id,
            problem_tote_scan=context["problem_tote"].barcode,
            returned_by=self.controller,
        )
        self.assertEqual(returned.status, FbsProblemToteItem.STATUS_RETURNED)
        self.assertTrue(
            FbsToteMovement.objects.filter(
                tote=context["problem_tote"],
                target_kind="pick_tote",
                details__problem_item_id=row.id,
                details__returned_to_pick_tote=True,
            ).exists()
        )

    def test_critical_problem_item_cannot_return_to_check_tote(self):
        context = self._controller_problem_context(suffix="CRITICAL")
        row = FbsProblemToteItem.objects.create(
            session=context["session"],
            problem_tote=context["problem_tote"],
            scanned_value=context["balance"].barcode,
            reason="Повреждение",
            severity=FbsProblemToteItem.SEVERITY_CRITICAL,
            reported_by=self.controller,
        )

        lookup = lookup_problem_tote_item(
            allocation_id=context["allocation"].id,
            actor=self.controller,
        )
        self.assertEqual(lookup.message, PROBLEM_TOTE_CRITICAL_MESSAGE)
        self.assertFalse(lookup.can_return)
        with self.assertRaisesMessage(
            FbsPickingError,
            PROBLEM_TOTE_CRITICAL_MESSAGE,
        ):
            return_problem_tote_item_to_check_tote(
                allocation_id=context["allocation"].id,
                problem_item_id=row.id,
                problem_tote_scan=context["problem_tote"].barcode,
                returned_by=self.controller,
            )
        row.refresh_from_db()
        self.assertEqual(row.status, FbsProblemToteItem.STATUS_IN_TOTE)

    def test_missing_problem_tote_item_uses_instruction_message(self):
        context = self._controller_problem_context(suffix="MISSING")

        lookup = lookup_problem_tote_item(
            allocation_id=context["allocation"].id,
            actor=self.controller,
        )

        self.assertIsNone(lookup.item)
        self.assertEqual(lookup.message, PROBLEM_TOTE_NOT_FOUND_MESSAGE)

    def test_controller_problem_queues_picker_return_without_quarantine_place(self):
        context = self._controller_problem_context(suffix="ORDER-RETURN")

        request = queue_verification_problem_restock(
            allocation_id=context["allocation"].id,
            exception_type=FbsPickException.TYPE_NOT_FOUND,
            reason="Товар отсутствует в таре подбора",
            service_tote_scan=context["problem_tote"].barcode,
            reported_by=self.controller,
        )
        repeated = queue_verification_problem_restock(
            allocation_id=context["allocation"].id,
            exception_type=FbsPickException.TYPE_NOT_FOUND,
            reason="Повторная отправка формы",
            service_tote_scan=context["problem_tote"].barcode,
            reported_by=self.controller,
        )

        self.assertEqual(repeated.id, request.id)
        self.assertEqual(FbsPickRestockRequest.objects.count(), 1)
        self.assertEqual(request.source_tote_id, context["problem_tote"].id)
        self.assertIsNone(request.quarantine_box_id)
        line = request.lines.get()
        self.assertEqual(line.source_box_id, self.source_box.id)
        self.assertEqual(line.source_cell_id, self.source_box.pallet.cell_id)
        movement = FbsToteMovement.objects.get(
            tote=context["problem_tote"],
            target_kind="problem_tote",
            details__request_id=request.id,
        )
        self.assertNotIn("quarantine_box_id", movement.details)

        claim_pick_restock_request(request_id=request.id, assigned_to=self.user)
        for stage, value in (
            (FbsPickRestockScan.STAGE_WORKSTATION, context["problem_tote"].barcode),
            (FbsPickRestockScan.STAGE_PICKUP_ITEM, context["balance"].barcode),
            (
                FbsPickRestockScan.STAGE_CELL,
                self.source_box.pallet.cell.location.location_code,
            ),
            (FbsPickRestockScan.STAGE_BOX, self.source_box.box_code),
            (FbsPickRestockScan.STAGE_ITEM, context["balance"].barcode),
        ):
            scan_pick_restock(
                request_id=request.id,
                stage=stage,
                scan_value=value,
                request_token=uuid.uuid4(),
                performed_by=self.user,
                pickup_line_id=(
                    pick_restock_state(request.id)["line"].id
                    if stage == FbsPickRestockScan.STAGE_PICKUP_ITEM
                    else None
                ),
            )

        request.refresh_from_db()
        context["balance"].refresh_from_db()
        self.assertEqual(request.status, FbsPickRestockRequest.STATUS_COMPLETED)
        self.assertEqual(
            (context["balance"].qty, context["balance"].available_qty),
            (1, 1),
        )

    def test_not_found_order_is_reserved_again_in_a_new_wave(self):
        context = self._controller_problem_context(suffix="NOT-FOUND-REWAVE")
        sku = SKU.objects.create(
            agency=self.agency,
            sku_code="problem-tote-sku",
            name="Replacement item",
        )
        SKUBarcode.objects.create(sku=sku, value=self.order_item.barcode)
        self.order_item.sku = sku
        self.order_item.save(update_fields=["sku", "updated_at"])
        replacement_balance = FbsStockBalance.objects.create(
            agency=self.agency,
            box=self.source_box,
            identity_key="replacement-unit",
            sku_ref=sku,
            sku_code=sku.sku_code,
            name=sku.name,
            barcode=self.order_item.barcode,
            qty=1,
            available_qty=1,
            reserved_qty=0,
        )

        request = queue_verification_problem_restock(
            allocation_id=context["allocation"].id,
            exception_type=FbsPickException.TYPE_NOT_FOUND,
            reason="Товар отсутствует в таре подбора",
            service_tote_scan=context["problem_tote"].barcode,
            reported_by=self.controller,
        )

        self.order.refresh_from_db()
        replacement_balance.refresh_from_db()
        replacement_task = self.order.pick_tasks.exclude(batch_id=context["allocation"].pick_task.batch_id).get()
        self.assertEqual(request.status, FbsPickRestockRequest.STATUS_QUEUED)
        self.assertEqual(self.order.internal_status, FbsOrder.STATUS_QUEUED_FOR_PICK)
        self.assertEqual(replacement_task.status, FbsPickTask.STATUS_QUEUED)
        self.assertEqual((replacement_balance.available_qty, replacement_balance.reserved_qty), (0, 1))

    def test_not_found_orders_share_one_pristine_replacement_wave(self):
        first = self._controller_problem_context(suffix="NOT-FOUND-GROUP-FIRST")
        sku = SKU.objects.create(
            agency=self.agency,
            sku_code="problem-tote-sku",
            name="Grouped replacement item",
        )
        SKUBarcode.objects.create(sku=sku, value=self.order_item.barcode)
        self.order_item.sku = sku
        self.order_item.save(update_fields=["sku", "updated_at"])
        first_replacement = FbsStockBalance.objects.create(
            agency=self.agency,
            box=self.source_box,
            identity_key="grouped-replacement-first",
            sku_ref=sku,
            sku_code=sku.sku_code,
            name=sku.name,
            barcode=self.order_item.barcode,
            qty=1,
            available_qty=1,
            reserved_qty=0,
        )
        first_request = queue_verification_problem_restock(
            allocation_id=first["allocation"].id,
            exception_type=FbsPickException.TYPE_NOT_FOUND,
            reason="Первый товар отсутствует в таре",
            service_tote_scan=first["problem_tote"].barcode,
            reported_by=self.controller,
        )
        first_task = self.order.pick_tasks.exclude(
            batch_id=first["pick_context"].pick_batch_id
        ).get()

        second_order = FbsOrder.objects.create(
            profile=self.profile,
            external_order_id="problem-tote-order-second",
            internal_status=FbsOrder.STATUS_EXCEPTION,
        )
        second_item = FbsOrderItem.objects.create(
            order=second_order,
            sku=sku,
            external_line_id="problem-tote-line-second",
            external_sku=sku.sku_code,
            barcode=self.order_item.barcode,
            quantity=1,
        )
        second_source_batch = FbsPickBatch.objects.create(
            agency=self.agency,
            status=FbsPickBatch.STATUS_VERIFICATION,
            planned_qty=1,
            picked_qty=1,
            picking_completed_at=timezone.now(),
        )
        second_source_task = FbsPickTask.objects.create(
            batch=second_source_batch,
            order=second_order,
            status=FbsPickTask.STATUS_PICKED,
            planned_qty=1,
            picked_qty=1,
        )
        second_source_balance = FbsStockBalance.objects.create(
            agency=self.agency,
            box=self.source_box,
            identity_key="grouped-source-second",
            sku_ref=sku,
            sku_code=sku.sku_code,
            name=sku.name,
            barcode=second_item.barcode,
            qty=0,
            available_qty=0,
            reserved_qty=0,
        )
        FbsOrderStockAllocation.objects.create(
            order_item=second_item,
            balance=second_source_balance,
            pick_task=second_source_task,
            qty_reserved=1,
            qty_picked=1,
            status=FbsOrderStockAllocation.STATUS_PICKED,
        )
        second_replacement = FbsStockBalance.objects.create(
            agency=self.agency,
            box=self.source_box,
            identity_key="grouped-replacement-second",
            sku_ref=sku,
            sku_code=sku.sku_code,
            name=sku.name,
            barcode=second_item.barcode,
            qty=1,
            available_qty=1,
            reserved_qty=0,
        )
        second_request = FbsPickRestockRequest.objects.create(
            batch=second_source_batch,
            order=second_order,
            status=FbsPickRestockRequest.STATUS_QUEUED,
            reason_code=FbsPickRestockRequest.REASON_WRONG_PRODUCT,
            reason="Второй товар отсутствует в таре",
            marketplace_action=FbsPickRestockRequest.MARKETPLACE_ACTION_NONE,
            planned_qty=1,
            created_by=self.controller,
        )

        outcome = requeue_not_found_order(
            request_id=second_request.id,
            performed_by=self.controller,
        )

        second_task = second_order.pick_tasks.exclude(batch=second_source_batch).get()
        first_task.batch.refresh_from_db()
        first_replacement.refresh_from_db()
        second_replacement.refresh_from_db()
        self.assertEqual(first_request.status, FbsPickRestockRequest.STATUS_QUEUED)
        self.assertEqual(outcome.pick_batch_id, first_task.batch_id)
        self.assertEqual(second_task.batch_id, first_task.batch_id)
        self.assertEqual(first_task.batch.tasks.count(), 2)
        self.assertEqual(first_task.batch.planned_qty, 2)
        self.assertEqual((first_replacement.available_qty, first_replacement.reserved_qty), (0, 1))
        self.assertEqual((second_replacement.available_qty, second_replacement.reserved_qty), (0, 1))

    def test_controller_problem_cancels_pending_wb_handover_add(self):
        context = self._controller_problem_context(suffix="CANCEL-PENDING-WB")
        handover_batch = FbsHandoverBatch.objects.create(
            profile=self.profile,
            external_supply_id="WB-GI-CANCEL-PENDING",
        )
        assignment = FbsHandoverOrderAssignment.objects.create(
            batch=handover_batch,
            order=self.order,
        )
        command = FbsMarketplaceCommand.objects.create(
            profile=self.profile,
            order=self.order,
            handover_batch=handover_batch,
            command_type=WB_ADD_ORDER_TO_HANDOVER,
            http_method=FbsMarketplaceCommand.METHOD_PATCH,
            endpoint="/api/v3/supplies/WB-GI-CANCEL-PENDING/orders/problem-tote-order",
            endpoint_version="v3",
            idempotency_key="cancel-pending-wb-order",
            payload={},
            payload_hash="cancel-pending-wb-order",
        )

        request = queue_verification_problem_restock(
            allocation_id=context["allocation"].id,
            exception_type=FbsPickException.TYPE_NOT_FOUND,
            reason="Заказ исключен на проверке",
            service_tote_scan=context["problem_tote"].barcode,
            reported_by=self.controller,
        )

        assignment.refresh_from_db()
        command.refresh_from_db()
        self.assertEqual(request.handover_assignment_id, assignment.id)
        self.assertEqual(
            assignment.status,
            FbsHandoverOrderAssignment.STATUS_CANCELED,
        )
        self.assertEqual(command.status, FbsMarketplaceCommand.STATUS_CANCELLED)
        self.assertIn("исключен контролером", command.error)

    @mock.patch("fbs.services.marketplace.schedule_wb_order_exclusion")
    def test_client_canceled_order_already_in_supply_waits_for_wb_absence(
        self,
        schedule_exclusion,
    ):
        context = self._controller_problem_context(suffix="CANCELED-IN-WB")
        self.order.marketplace_status = "cancel"
        self.order.save(update_fields=["marketplace_status", "updated_at"])
        handover_batch = FbsHandoverBatch.objects.create(
            profile=self.profile,
            external_supply_id="WB-GI-CANCELED-IN-WB",
        )
        assignment = FbsHandoverOrderAssignment.objects.create(
            batch=handover_batch,
            order=self.order,
            status=FbsHandoverOrderAssignment.STATUS_CONFIRMED,
            confirmed_at=timezone.now(),
        )

        request = queue_verification_problem_restock(
            allocation_id=context["allocation"].id,
            exception_type=FbsPickException.TYPE_NOT_FOUND,
            reason="Заказ отменен во время проверки",
            service_tote_scan=context["canceled_tote"].barcode,
            product_scans=[context["balance"].barcode],
            reported_by=self.controller,
        )

        self.assertEqual(request.handover_assignment_id, assignment.id)
        self.assertEqual(
            request.status,
            FbsPickRestockRequest.STATUS_WAITING_MARKETPLACE,
        )
        self.assertEqual(
            request.marketplace_action,
            FbsPickRestockRequest.MARKETPLACE_ACTION_VERIFY_CANCEL,
        )
        schedule_exclusion.assert_called_once_with(
            request_id=request.id,
            requested_by=self.controller,
        )

    @override_settings(FBS_OUTBOX_ENABLED=True)
    def test_wb_client_cancellation_releases_return_when_order_id_stays_in_supply(self):
        context = self._controller_problem_context(suffix="CANCELED-STILL-LISTED")
        self.profile.is_active = True
        self.profile.outbox_enabled = True
        self.profile.save(update_fields=["is_active", "outbox_enabled", "updated_at"])
        self.order.external_order_id = "5686106141"
        self.order.marketplace_status = "complete"
        self.order.marketplace_substatus = "canceled_by_client"
        self.order.save(
            update_fields=[
                "external_order_id",
                "marketplace_status",
                "marketplace_substatus",
                "updated_at",
            ]
        )
        handover_batch = FbsHandoverBatch.objects.create(
            profile=self.profile,
            external_supply_id="WB-GI-CANCELED-STILL-LISTED",
        )
        assignment = FbsHandoverOrderAssignment.objects.create(
            batch=handover_batch,
            order=self.order,
            status=FbsHandoverOrderAssignment.STATUS_CONFIRMED,
            confirmed_at=timezone.now(),
        )
        request = queue_verification_problem_restock(
            allocation_id=context["allocation"].id,
            exception_type=FbsPickException.TYPE_NOT_FOUND,
            reason="Заказ отменен во время проверки",
            service_tote_scan=context["canceled_tote"].barcode,
            product_scans=[context["balance"].barcode],
            reported_by=self.controller,
        )
        command = FbsMarketplaceCommand.objects.get(
            order=self.order,
            command_type=WB_READ_HANDOVER_ORDER_IDS,
        )
        transport = mock.Mock()
        transport.send.return_value = MarketplaceHttpResponse(
            status_code=200,
            headers={},
            content=b"",
            json_payload={"orderIds": [5686106141]},
        )

        processed = process_marketplace_command(
            command_id=command.id,
            transport=transport,
        )

        request.refresh_from_db()
        assignment.refresh_from_db()
        self.assertEqual(request.status, FbsPickRestockRequest.STATUS_QUEUED)
        self.assertIsNotNone(request.marketplace_confirmed_at)
        self.assertEqual(
            assignment.status,
            FbsHandoverOrderAssignment.STATUS_CANCELED,
        )
        self.assertEqual(processed.status, FbsMarketplaceCommand.STATUS_CONFIRMED)
        self.assertTrue(processed.response_payload["order_still_assigned"])
        self.assertTrue(processed.response_payload["client_cancellation_confirmed"])

    @override_settings(FBS_OUTBOX_ENABLED=True)
    def test_wb_order_still_waits_when_client_cancellation_is_not_current(self):
        context = self._controller_problem_context(suffix="CANCEL-NOT-CURRENT")
        self.profile.is_active = True
        self.profile.outbox_enabled = True
        self.profile.save(update_fields=["is_active", "outbox_enabled", "updated_at"])
        self.order.external_order_id = "5686106142"
        self.order.marketplace_status = "confirm"
        self.order.marketplace_substatus = "canceled_by_client"
        self.order.save(
            update_fields=[
                "external_order_id",
                "marketplace_status",
                "marketplace_substatus",
                "updated_at",
            ]
        )
        handover_batch = FbsHandoverBatch.objects.create(
            profile=self.profile,
            external_supply_id="WB-GI-CANCEL-NOT-CURRENT",
        )
        assignment = FbsHandoverOrderAssignment.objects.create(
            batch=handover_batch,
            order=self.order,
            status=FbsHandoverOrderAssignment.STATUS_CONFIRMED,
            confirmed_at=timezone.now(),
        )
        request = queue_verification_problem_restock(
            allocation_id=context["allocation"].id,
            exception_type=FbsPickException.TYPE_NOT_FOUND,
            reason="Заказ отменен во время проверки",
            service_tote_scan=context["canceled_tote"].barcode,
            product_scans=[context["balance"].barcode],
            reported_by=self.controller,
        )
        self.order.marketplace_substatus = "waiting"
        self.order.save(update_fields=["marketplace_substatus", "updated_at"])
        command = FbsMarketplaceCommand.objects.get(
            order=self.order,
            command_type=WB_READ_HANDOVER_ORDER_IDS,
        )
        transport = mock.Mock()
        transport.send.return_value = MarketplaceHttpResponse(
            status_code=200,
            headers={},
            content=b"",
            json_payload={"orderIds": [5686106142]},
        )

        processed = process_marketplace_command(
            command_id=command.id,
            transport=transport,
        )

        request.refresh_from_db()
        assignment.refresh_from_db()
        self.assertEqual(
            request.status,
            FbsPickRestockRequest.STATUS_WAITING_MARKETPLACE,
        )
        self.assertEqual(
            assignment.status,
            FbsHandoverOrderAssignment.STATUS_CONFIRMED,
        )
        self.assertEqual(processed.status, FbsMarketplaceCommand.STATUS_RETRY)
        self.assertIn("показывает заказ в поставке", processed.error)

    @override_settings(FBS_OUTBOX_ENABLED=True)
    def test_marketplace_worker_does_not_send_excluded_order(self):
        self.profile.outbox_enabled = True
        self.profile.save(update_fields=["outbox_enabled", "updated_at"])
        self.order.internal_status = FbsOrder.STATUS_PICKED
        self.order.save(update_fields=["internal_status", "updated_at"])
        pick_batch = FbsPickBatch.objects.create(
            agency=self.agency,
            status=FbsPickBatch.STATUS_VERIFICATION,
            planned_qty=1,
            picked_qty=1,
        )
        handover_batch = FbsHandoverBatch.objects.create(
            profile=self.profile,
            external_supply_id="WB-GI-WORKER-GUARD",
        )
        assignment = FbsHandoverOrderAssignment.objects.create(
            batch=handover_batch,
            order=self.order,
        )
        FbsPickRestockRequest.objects.create(
            batch=pick_batch,
            order=self.order,
            handover_assignment=assignment,
            status=FbsPickRestockRequest.STATUS_QUEUED,
            reason_code=FbsPickRestockRequest.REASON_CLIENT_CANCELED,
            reason="Отменен во время проверки",
            planned_qty=1,
        )
        command = FbsMarketplaceCommand.objects.create(
            profile=self.profile,
            order=self.order,
            handover_batch=handover_batch,
            command_type=WB_ADD_ORDER_TO_HANDOVER,
            http_method=FbsMarketplaceCommand.METHOD_PATCH,
            endpoint="/api/v3/supplies/WB-GI-WORKER-GUARD/orders/problem-tote-order",
            endpoint_version="v3",
            idempotency_key="worker-guard-wb-order",
            payload={},
            payload_hash="worker-guard-wb-order",
        )
        transport = mock.Mock()

        processed = process_marketplace_command(
            command_id=command.id,
            transport=transport,
        )

        assignment.refresh_from_db()
        self.assertEqual(processed.status, FbsMarketplaceCommand.STATUS_CANCELLED)
        self.assertEqual(
            assignment.status,
            FbsHandoverOrderAssignment.STATUS_CANCELED,
        )
        transport.send.assert_not_called()

    def test_controller_problem_requires_exact_assigned_problem_tote_scan(self):
        context = self._controller_problem_context(suffix="WRONG-SCAN")
        movements_before = FbsToteMovement.objects.count()

        with self.assertRaisesMessage(FbsPickingError, "Неверная служебная тара"):
            queue_verification_problem_restock(
                allocation_id=context["allocation"].id,
                exception_type=FbsPickException.TYPE_BARCODE,
                reason="Некорректный Честный знак",
                service_tote_scan=context["unused_canceled_tote"].barcode,
                reported_by=self.controller,
            )

        self.assertEqual(FbsPickRestockRequest.objects.count(), 0)
        self.assertEqual(FbsToteMovement.objects.count(), movements_before)
        context["allocation"].pick_task.refresh_from_db()
        self.assertEqual(
            context["allocation"].pick_task.status,
            FbsPickTask.STATUS_PICKED,
        )

    def test_canceled_order_requires_and_uses_assigned_canceled_tote(self):
        context = self._controller_problem_context(suffix="CANCELED-SCAN")
        self.order.marketplace_status = "cancel"
        self.order.save(update_fields=["marketplace_status", "updated_at"])

        with self.assertRaisesMessage(FbsPickingError, "Неверная служебная тара"):
            queue_verification_problem_restock(
                allocation_id=context["allocation"].id,
                exception_type=FbsPickException.TYPE_NOT_FOUND,
                reason="Заказ отменен",
                service_tote_scan=context["unused_problem_tote"].barcode,
                product_scans=[context["balance"].barcode],
                reported_by=self.controller,
            )

        request = queue_verification_problem_restock(
            allocation_id=context["allocation"].id,
            exception_type=FbsPickException.TYPE_NOT_FOUND,
            reason="Заказ отменен",
            service_tote_scan=context["canceled_tote"].barcode,
            product_scans=[context["balance"].barcode],
            reported_by=self.controller,
        )

        self.assertEqual(request.source_tote_id, context["canceled_tote"].id)
        self.assertEqual(
            request.reason_code,
            FbsPickRestockRequest.REASON_CLIENT_CANCELED,
        )
        movement = FbsToteMovement.objects.get(
            details__request_id=request.id,
            target_kind="canceled_tote",
        )
        self.assertEqual(movement.tote_id, context["canceled_tote"].id)
        self.assertEqual(
            movement.details["service_tote_scan"],
            context["canceled_tote"].barcode,
        )
        self.assertEqual(movement.details["product_scan_count"], 1)
        scan = FbsPickRestockScan.objects.get(
            request=request,
            stage=FbsPickRestockScan.STAGE_PICKUP_ITEM,
        )
        self.assertEqual(scan.scan_value, context["balance"].barcode)
        self.assertEqual(scan.expected_value, context["balance"].barcode)

    def test_controller_product_confirmation_does_not_replace_picker_scan(self):
        context = self._controller_problem_context(suffix="PICKER-RESCAN")
        self.order.marketplace_status = "cancel"
        self.order.save(update_fields=["marketplace_status", "updated_at"])
        request = queue_verification_problem_restock(
            allocation_id=context["allocation"].id,
            exception_type=FbsPickException.TYPE_NOT_FOUND,
            reason="Заказ отменен",
            service_tote_scan=context["canceled_tote"].barcode,
            product_scans=[context["balance"].barcode],
            reported_by=self.controller,
        )
        line = request.lines.get()
        claim_pick_restock_request(request_id=request.id, assigned_to=self.user)

        scan_pick_restock(
            request_id=request.id,
            stage=FbsPickRestockScan.STAGE_WORKSTATION,
            scan_value=context["canceled_tote"].barcode,
            request_token=uuid.uuid4(),
            performed_by=self.user,
        )

        state = pick_restock_state(request.id)
        self.assertEqual(state["stage"], FbsPickRestockScan.STAGE_PICKUP_ITEM)
        self.assertEqual(state["pickup_scanned_qty"], 0)
        self.assertEqual(state["pickup_planned_qty"], 1)
        self.assertEqual(
            [option["line"].id for option in state["pickup_options"]],
            [line.id],
        )

        with self.assertRaisesMessage(FbsPickingError, "Сначала выберите товар"):
            scan_pick_restock(
                request_id=request.id,
                stage=FbsPickRestockScan.STAGE_PICKUP_ITEM,
                scan_value=context["balance"].barcode,
                request_token=uuid.uuid4(),
                performed_by=self.user,
            )

        with self.assertRaisesMessage(FbsPickingError, "Неверный товар из тары"):
            scan_pick_restock(
                request_id=request.id,
                stage=FbsPickRestockScan.STAGE_PICKUP_ITEM,
                scan_value="4600000000099",
                pickup_line_id=line.id,
                request_token=uuid.uuid4(),
                performed_by=self.user,
            )

        scan_pick_restock(
            request_id=request.id,
            stage=FbsPickRestockScan.STAGE_PICKUP_ITEM,
            scan_value=context["balance"].barcode,
            pickup_line_id=line.id,
            request_token=uuid.uuid4(),
            performed_by=self.user,
        )
        state = pick_restock_state(request.id)
        self.assertTrue(state["pickup_confirmed"])
        self.assertEqual(state["stage"], FbsPickRestockScan.STAGE_CELL)

    def test_canceled_order_rejects_external_order_number_instead_of_product_scan(self):
        context = self._controller_problem_context(suffix="CANCELED-NO-LABEL")
        self.order.marketplace_status = "cancel"
        self.order.save(update_fields=["marketplace_status", "updated_at"])

        with self.assertRaisesMessage(
            FbsPickingError,
            "Отсканируйте все товары отмененного заказа",
        ):
            queue_verification_problem_restock(
                allocation_id=context["allocation"].id,
                exception_type=FbsPickException.TYPE_NOT_FOUND,
                reason="Заказ отменен",
                service_tote_scan=context["canceled_tote"].barcode,
                order_scan=self.order.external_order_id,
                reported_by=self.controller,
            )

    def test_canceled_order_stops_pending_unapplied_label_print(self):
        context = self._controller_problem_context(suffix="CANCELED-PRINT")
        self.order.marketplace_status = "cancel"
        self.order.save(update_fields=["marketplace_status", "updated_at"])
        label = FbsOrderLabel.objects.create(
            order=self.order,
            marketplace=self.profile.marketplace,
            external_label_id="cancel-label",
            barcode="cancel-label-barcode",
            status=FbsOrderLabel.STATUS_READY,
        )
        print_job = ProcessingPrintJob.objects.create(
            article=self.order.external_order_id,
            card_id=f"fbs:order-label:{label.id}:content",
            barcode=label.barcode,
            status=ProcessingPrintJob.STATUS_PENDING,
        )

        queue_verification_problem_restock(
            allocation_id=context["allocation"].id,
            exception_type=FbsPickException.TYPE_NOT_FOUND,
            reason="Заказ отменен",
            service_tote_scan=context["canceled_tote"].barcode,
            product_scans=[context["balance"].barcode],
            reported_by=self.controller,
        )

        label.refresh_from_db()
        print_job.refresh_from_db()
        self.assertEqual(label.status, FbsOrderLabel.STATUS_CANCELED)
        self.assertEqual(print_job.status, ProcessingPrintJob.STATUS_FAILED)
        self.assertIn("печать остановлена", print_job.error)

    def test_canceled_multi_unit_order_requires_every_product_scan(self):
        context = self._controller_problem_context(suffix="CANCELED-MULTI")
        allocation = context["allocation"]
        allocation.qty_reserved = 2
        allocation.qty_picked = 2
        allocation.save(update_fields=["qty_reserved", "qty_picked", "updated_at"])
        task = allocation.pick_task
        task.planned_qty = 2
        task.picked_qty = 2
        task.save(update_fields=["planned_qty", "picked_qty", "updated_at"])
        batch = task.batch
        batch.planned_qty = 2
        batch.picked_qty = 2
        batch.save(update_fields=["planned_qty", "picked_qty", "updated_at"])
        self.order.marketplace_status = "cancel"
        self.order.save(update_fields=["marketplace_status", "updated_at"])

        with self.assertRaisesMessage(FbsPickingError, "нужно 2 шт., получено 1"):
            queue_verification_problem_restock(
                allocation_id=allocation.id,
                exception_type=FbsPickException.TYPE_NOT_FOUND,
                reason="Заказ отменен",
                service_tote_scan=context["canceled_tote"].barcode,
                product_scans=[context["balance"].barcode],
                reported_by=self.controller,
            )

        request = queue_verification_problem_restock(
            allocation_id=allocation.id,
            exception_type=FbsPickException.TYPE_NOT_FOUND,
            reason="Заказ отменен",
            service_tote_scan=context["canceled_tote"].barcode,
            product_scans=[context["balance"].barcode] * 2,
            reported_by=self.controller,
        )

        self.assertEqual(request.planned_qty, 2)
        self.assertEqual(
            FbsPickRestockScan.objects.filter(
                request=request,
                stage=FbsPickRestockScan.STAGE_PICKUP_ITEM,
            ).count(),
            2,
        )

    @mock.patch("fbs.services.marketplace.schedule_wb_order_exclusion")
    def test_ozon_posting_canceled_exits_local_handover_without_wb_wait(
        self,
        schedule_wb_exclusion,
    ):
        context = self._controller_problem_context(suffix="CANCELED-OZON")
        self.profile.marketplace = FbsIntegrationProfile.MARKETPLACE_OZON
        self.profile.save(update_fields=["marketplace", "updated_at"])
        self.order.marketplace_status = "awaiting_packaging"
        self.order.marketplace_substatus = "posting_canceled"
        self.order.save(
            update_fields=["marketplace_status", "marketplace_substatus", "updated_at"]
        )
        handover_batch = FbsHandoverBatch.objects.create(profile=self.profile)
        assignment = FbsHandoverOrderAssignment.objects.create(
            batch=handover_batch,
            order=self.order,
            status=FbsHandoverOrderAssignment.STATUS_CONFIRMED,
            confirmed_at=timezone.now(),
        )

        request = queue_verification_problem_restock(
            allocation_id=context["allocation"].id,
            exception_type=FbsPickException.TYPE_NOT_FOUND,
            reason="Ozon отменил заказ",
            service_tote_scan=context["canceled_tote"].barcode,
            product_scans=[context["balance"].barcode],
            reported_by=self.controller,
        )

        self.assertEqual(
            request.reason_code,
            FbsPickRestockRequest.REASON_CLIENT_CANCELED,
        )
        self.assertEqual(request.status, FbsPickRestockRequest.STATUS_QUEUED)
        self.assertEqual(
            request.marketplace_action,
            FbsPickRestockRequest.MARKETPLACE_ACTION_NONE,
        )
        self.assertEqual(request.source_tote_id, context["canceled_tote"].id)
        assignment.refresh_from_db()
        self.assertEqual(
            assignment.status,
            FbsHandoverOrderAssignment.STATUS_CANCELED,
        )
        self.assertIsNone(assignment.confirmed_at)
        schedule_wb_exclusion.assert_not_called()
        context["allocation"].pick_task.refresh_from_db()
        context["allocation"].pick_task.batch.refresh_from_db()
        self.assertEqual(
            context["allocation"].pick_task.status,
            FbsPickTask.STATUS_EXCEPTION,
        )
        self.assertEqual(
            context["allocation"].pick_task.batch.status,
            FbsPickBatch.STATUS_DONE,
        )

    def test_canceled_order_label_queue_is_rejected_before_workstation_lookup(self):
        context = self._controller_problem_context(suffix="CANCELED-QUEUE-GUARD")
        self.order.marketplace_status = "cancel"
        self.order.save(update_fields=["marketplace_status", "updated_at"])
        label = FbsOrderLabel.objects.create(
            order=self.order,
            marketplace=self.profile.marketplace,
            external_label_id="canceled-ready-label",
            barcode="canceled-ready-label-barcode",
            status=FbsOrderLabel.STATUS_READY,
        )

        with mock.patch("fbs.services.printing._order_workstation") as workstation:
            from fbs.services.printing import (
                queue_fbs_order_label_print,
                queue_fbs_preloaded_ozon_order_label_print,
            )

            result = queue_fbs_order_label_print(label_id=label.id)
            preloaded_result = queue_fbs_preloaded_ozon_order_label_print(
                label_id=label.id
            )

        self.assertIsNone(result)
        self.assertIsNone(preloaded_result)
        workstation.assert_not_called()

    def test_canceled_verification_prompt_uses_signed_product_scan_not_order_number(self):
        context = self._controller_problem_context(suffix="CANCELED-PROMPT")
        self.order.marketplace_status = "cancel"
        self.order.save(update_fields=["marketplace_status", "updated_at"])
        allocation = context["allocation"]

        prompt = _verification_service_tote_prompt(
            batch=allocation.pick_task.batch,
            allocation=allocation,
            controller=self.controller,
            problem_kind="canceled",
            reason="Маркетплейс отменил этот заказ.",
            initial_item_scan=context["balance"].barcode,
        )

        self.assertFalse(prompt.requires_order_scan)
        self.assertTrue(prompt.requires_product_scans)
        self.assertTrue(prompt.initial_product_confirmed)
        self.assertEqual(prompt.canceled_product_total, 1)
        self.assertEqual(prompt.canceled_product_scan_rows, [])
        self.assertEqual(
            _canceled_initial_scan_from_token(
                token=prompt.initial_product_scan_token,
                batch=allocation.pick_task.batch,
                allocation=allocation,
                controller=self.controller,
            ),
            context["balance"].barcode,
        )

    def test_canceled_verification_screen_asks_for_tote_not_order_label(self):
        context = self._controller_problem_context(suffix="CANCELED-SCREEN")
        Employee.objects.create(
            user=self.controller,
            full_name="Контролер отмененного заказа",
            role="fbs_controller",
        )
        self.order.internal_status = FbsOrder.STATUS_PICKED
        self.order.marketplace_status = "cancel"
        self.order.save(
            update_fields=["internal_status", "marketplace_status", "updated_at"]
        )
        context["balance"].marking_code = "010460000000000121STORED-BUT-NOT-REQUESTED"
        context["balance"].save(update_fields=["marking_code", "updated_at"])
        self.client.force_login(self.controller)
        FbsOrderTraceability.objects.create(
            allocation=context["allocation"],
            qty=1,
            status=FbsOrderTraceability.STATUS_PICKED,
        )

        response = self.client.post(
            reverse(
                "fbs:tsd_pick_verification",
                kwargs={"batch_id": context["allocation"].pick_task.batch_id},
            ),
            {
                "action": "select_item",
                "item_scan": context["balance"].barcode,
            },
        )

        self.assertEqual(response.status_code, 409)
        self.assertContains(
            response,
            "Этикетка заказа для этого действия не требуется",
            status_code=409,
        )
        self.assertContains(
            response,
            'name="initial_product_scan_token"',
            status_code=409,
        )
        self.assertNotContains(response, 'name="order_scan"', status_code=409)
        self.assertNotContains(response, 'name="product_scan"', status_code=409)
        self.assertContains(response, 'name="service_tote_scan"', status_code=409)
        token_match = re.search(
            r'name="initial_product_scan_token" value="([^"]+)"',
            response.content.decode(),
        )
        self.assertIsNotNone(token_match)

        completed = self.client.post(
            reverse(
                "fbs:tsd_pick_verification_problem",
                kwargs={"allocation_id": context["allocation"].id},
            ),
            {
                "action": "return_to_pick",
                "modal_flow": "1",
                "problem_kind": "canceled",
                "exception_type": FbsPickException.TYPE_NOT_FOUND,
                "reason": "Маркетплейс отменил этот заказ.",
                "initial_product_scan_token": html.unescape(token_match.group(1)),
                "service_tote_scan": context["canceled_tote"].barcode,
            },
        )

        self.assertEqual(completed.status_code, 302)
        restock = FbsPickRestockRequest.objects.get(order=self.order)
        self.assertEqual(restock.source_tote_id, context["canceled_tote"].id)

    def test_applied_canceled_label_routes_away_from_verification_form(self):
        context = self._controller_problem_context(suffix="CANCELED-APPLIED")
        self.order.marketplace_status = "cancel"
        self.order.save(update_fields=["marketplace_status", "updated_at"])
        FbsOrderLabel.objects.create(
            order=self.order,
            marketplace=self.profile.marketplace,
            external_label_id="applied-canceled-label",
            barcode="applied-canceled-barcode",
            status=FbsOrderLabel.STATUS_APPLIED,
            applied_by=self.controller,
            applied_at=timezone.now(),
        )
        allocation = context["allocation"]

        prompt = _verification_service_tote_prompt(
            batch=allocation.pick_task.batch,
            allocation=allocation,
            controller=self.controller,
            problem_kind="canceled",
            reason="Маркетплейс отменил этот заказ.",
            initial_item_scan=context["balance"].barcode,
        )

        self.assertTrue(prompt.late_applied_label)
        self.assertFalse(prompt.ready_to_scan)
        self.assertFalse(prompt.requires_product_scans)
        self.assertEqual(prompt.applied_label_barcode, "applied-canceled-barcode")

    def test_canceled_marked_unit_uses_product_barcode_without_kiz_request(self):
        context = self._controller_problem_context(suffix="CANCELED-KIZ")
        marking_code = "010460000000000121SERIAL"
        balance = context["balance"]
        balance.marking_code = marking_code
        balance.save(update_fields=["marking_code", "updated_at"])
        self.order.marketplace_status = "cancel"
        self.order.save(update_fields=["marketplace_status", "updated_at"])
        allocation = context["allocation"]

        prompt = _verification_service_tote_prompt(
            batch=allocation.pick_task.batch,
            allocation=allocation,
            controller=self.controller,
            problem_kind="canceled",
            reason="Маркетплейс отменил этот заказ.",
            initial_item_scan=balance.barcode,
        )
        self.assertTrue(prompt.initial_product_confirmed)
        self.assertEqual(prompt.canceled_product_scan_rows, [])

        request = queue_verification_problem_restock(
            allocation_id=allocation.id,
            exception_type=FbsPickException.TYPE_NOT_FOUND,
            reason="Причину сотрудник не должен определять",
            service_tote_scan=context["canceled_tote"].barcode,
            product_scans=[balance.barcode],
            reported_by=self.controller,
        )
        scan = FbsPickRestockScan.objects.get(
            request=request,
            stage=FbsPickRestockScan.STAGE_PICKUP_ITEM,
        )
        self.assertEqual(scan.scan_value, balance.barcode)
        self.assertEqual(request.reason, "Заказ отменен маркетплейсом")

    def test_session_does_not_release_problem_tote_with_unresolved_item(self):
        context = self._controller_problem_context(suffix="CLOSE")
        context["pick_context"].status = FbsControllerPickTote.STATUS_CLOSED
        context["pick_context"].save(update_fields=["status", "updated_at"])
        context["check_tote"].status = context["check_tote"].STATUS_CLOSED
        context["check_tote"].save(update_fields=["status", "updated_at"])
        FbsProblemToteItem.objects.create(
            session=context["session"],
            problem_tote=context["problem_tote"],
            scanned_value="4600888888888",
            reason="Лишний товар",
            severity=FbsProblemToteItem.SEVERITY_NONCRITICAL,
            reported_by=self.controller,
        )

        with self.assertRaisesMessage(
            FbsPickingError,
            "Сначала завершите обработку товаров в проблемной таре: 1.",
        ):
            close_controller_session(
                session_id=context["session"].id,
                performed_by=self.controller,
            )

    def test_regular_item_is_restored_only_to_unavailable_quarantine_stock(self):
        allocation, source_balance = self._picked_allocation()
        snapshot_count = WarehouseStockSnapshot.objects.count()

        quarantine_balance = restore_restock_unit_to_problem_box(
            allocation=allocation,
            balance=source_balance,
            problem_box=self.quarantine_box,
        )

        quarantine_balance.refresh_from_db()
        source_balance.refresh_from_db()
        self.assertEqual(quarantine_balance.box_id, self.quarantine_box.id)
        self.assertEqual(
            (
                quarantine_balance.qty,
                quarantine_balance.available_qty,
                quarantine_balance.reserved_qty,
            ),
            (1, 0, 0),
        )
        self.assertEqual((source_balance.qty, source_balance.available_qty), (0, 0))
        self.assertEqual(WarehouseStockSnapshot.objects.count(), snapshot_count)

    def test_marked_item_moves_to_quarantine_and_remains_unavailable(self):
        allocation, source_balance = self._picked_allocation(
            marking_code="010460000000000121TEST-MARK"
        )

        quarantine_balance = restore_restock_unit_to_problem_box(
            allocation=allocation,
            balance=source_balance,
            problem_box=self.quarantine_box,
        )

        quarantine_balance.refresh_from_db()
        self.assertEqual(quarantine_balance.id, source_balance.id)
        self.assertEqual(quarantine_balance.box_id, self.quarantine_box.id)
        self.assertEqual(
            (
                quarantine_balance.qty,
                quarantine_balance.available_qty,
                quarantine_balance.reserved_qty,
            ),
            (1, 0, 0),
        )

    def test_order_restock_without_confirmed_source_tote_cannot_be_claimed(self):
        batch = FbsPickBatch.objects.create(
            agency=self.agency,
            status=FbsPickBatch.STATUS_VERIFICATION,
            planned_qty=1,
            picked_qty=1,
        )
        request = FbsPickRestockRequest.objects.create(
            batch=batch,
            order=self.order,
            status=FbsPickRestockRequest.STATUS_QUEUED,
            reason_code=FbsPickRestockRequest.REASON_CLIENT_CANCELED,
            reason="Canceled order awaiting physical tote confirmation",
            planned_qty=1,
        )

        with self.assertRaisesMessage(FbsPickingError, "физическую тару возврата"):
            claim_pick_restock_request(
                request_id=request.id,
                assigned_to=self.user,
            )

        request.refresh_from_db()
        self.assertEqual(request.status, FbsPickRestockRequest.STATUS_QUEUED)
        self.assertIsNone(request.assigned_to_id)

    def test_quarantine_restock_requires_problem_tote_and_finishes_unavailable(self):
        workstation = FbsWorkstation.objects.create(
            barcode="FBS-WS-PROBLEM-TOTE",
            name="Problem tote workstation",
        )
        unknown_tote = FbsPickingCart.objects.create(
            barcode="FBS-CART-PROBLEM-UNKNOWN",
            name="Unknown tote",
        )
        problem_tote = FbsPickingCart.objects.create(
            barcode="FBS-CART-PROBLEM-RETURN",
            name="Problem return tote",
        )
        canceled_tote = FbsPickingCart.objects.create(
            barcode="FBS-CART-CANCELED-RETURN",
            name="Canceled return tote",
        )
        start_controller_session(
            workstation_id=workstation.id,
            controller=self.controller,
            unknown_tote_scan=unknown_tote.barcode,
            problem_tote_scan=problem_tote.barcode,
            canceled_tote_scan=canceled_tote.barcode,
            first_check_tote_scan=self.first_check_tote.barcode,
        )
        batch = FbsPickBatch.objects.create(
            agency=self.agency,
            status=FbsPickBatch.STATUS_VERIFICATION,
            planned_qty=1,
            picked_qty=1,
            workstation=workstation,
        )
        task = FbsPickTask.objects.create(
            batch=batch,
            order=self.order,
            status=FbsPickTask.STATUS_EXCEPTION,
            planned_qty=1,
            picked_qty=1,
        )
        allocation, source_balance = self._picked_allocation(pick_task=task)
        request = FbsPickRestockRequest.objects.create(
            batch=batch,
            order=self.order,
            source_tote=problem_tote,
            quarantine_box=self.quarantine_box,
            status=FbsPickRestockRequest.STATUS_QUEUED,
            reason_code=FbsPickRestockRequest.REASON_METADATA,
            reason="Invalid marking code",
            planned_qty=1,
        )
        FbsPickRestockLine.objects.create(
            request=request,
            allocation=allocation,
            source_balance=source_balance,
            source_box=self.source_box,
            source_cell=self.source_box.pallet.cell,
            planned_qty=1,
        )
        claim_pick_restock_request(request_id=request.id, assigned_to=self.user)

        def scan(stage, value):
            return scan_pick_restock(
                request_id=request.id,
                stage=stage,
                scan_value=value,
                request_token=uuid.uuid4(),
                performed_by=self.user,
                pickup_line_id=(
                    pick_restock_state(request.id)["line"].id
                    if stage == FbsPickRestockScan.STAGE_PICKUP_ITEM
                    else None
                ),
            )

        with self.assertRaisesMessage(FbsPickingError, "Неверная тара"):
            scan(FbsPickRestockScan.STAGE_WORKSTATION, canceled_tote.barcode)
        scan(FbsPickRestockScan.STAGE_WORKSTATION, problem_tote.barcode)
        scan(FbsPickRestockScan.STAGE_PICKUP_ITEM, source_balance.barcode)
        scan(
            FbsPickRestockScan.STAGE_CELL,
            self.quarantine_box.pallet.cell.location.location_code,
        )
        scan(FbsPickRestockScan.STAGE_BOX, self.quarantine_box.box_code)
        scan(FbsPickRestockScan.STAGE_ITEM, source_balance.barcode)

        request.refresh_from_db()
        task.refresh_from_db()
        self.order.refresh_from_db()
        quarantine_balance = FbsStockBalance.objects.get(
            box=self.quarantine_box,
            barcode=source_balance.barcode,
        )
        self.assertEqual(request.status, FbsPickRestockRequest.STATUS_COMPLETED)
        self.assertEqual(task.status, FbsPickTask.STATUS_EXCEPTION)
        self.assertEqual(self.order.internal_status, FbsOrder.STATUS_EXCEPTION)
        self.assertEqual(
            (quarantine_balance.qty, quarantine_balance.available_qty),
            (1, 0),
        )

    def test_confirmed_canceled_order_is_attached_to_canceled_tote(self):
        workstation = FbsWorkstation.objects.create(
            barcode="FBS-WS-CANCELED-TOTE",
            name="Canceled tote workstation",
        )
        unknown_tote = FbsPickingCart.objects.create(
            barcode="FBS-CART-CANCELED-UNKNOWN",
            name="Unknown tote",
        )
        problem_tote = FbsPickingCart.objects.create(
            barcode="FBS-CART-CANCELED-PROBLEM",
            name="Problem tote",
        )
        canceled_tote = FbsPickingCart.objects.create(
            barcode="FBS-CART-CANCELED-SOURCE",
            name="Canceled order tote",
        )
        session = start_controller_session(
            workstation_id=workstation.id,
            controller=self.controller,
            unknown_tote_scan=unknown_tote.barcode,
            problem_tote_scan=problem_tote.barcode,
            canceled_tote_scan=canceled_tote.barcode,
            first_check_tote_scan=self.first_check_tote.barcode,
        )
        batch = FbsPickBatch.objects.create(
            agency=self.agency,
            status=FbsPickBatch.STATUS_VERIFICATION,
            planned_qty=1,
            picked_qty=1,
            workstation=workstation,
        )
        handover_batch = FbsHandoverBatch.objects.create(
            profile=self.profile,
            status=FbsHandoverBatch.STATUS_OPEN,
        )
        assignment = FbsHandoverOrderAssignment.objects.create(
            batch=handover_batch,
            order=self.order,
            status=FbsHandoverOrderAssignment.STATUS_CONFIRMED,
        )
        check_tote = FbsControllerCheckTote.objects.create(
            session=session,
            profile=self.profile,
            agency=self.agency,
            handover_batch=handover_batch,
            status=FbsControllerCheckTote.STATUS_OPEN,
            item_qty=1,
            labeled_qty=1,
            opened_by=self.controller,
        )
        pick_tote = FbsPickingCart.objects.create(
            barcode="FBS-CART-CANCELED-PICK-SOURCE",
            name="Canceled order original pick tote",
        )
        pick_context = FbsControllerPickTote.objects.create(
            session=session,
            check_tote=check_tote,
            pick_batch=batch,
            tote=pick_tote,
            status=FbsControllerPickTote.STATUS_CLOSED,
            planned_qty=1,
            processed_qty=1,
            closed_at=timezone.now(),
        )
        label = FbsOrderLabel.objects.create(
            order=self.order,
            marketplace=FbsIntegrationProfile.MARKETPLACE_WB,
            barcode="FBS-CANCELED-CHECK-TOTE-LABEL",
            status=FbsOrderLabel.STATUS_APPLIED,
            applied_by=self.controller,
            applied_at=timezone.now(),
        )
        tote_order = FbsControllerToteOrder.objects.create(
            check_tote=check_tote,
            pick_tote=pick_context,
            order=self.order,
            label=label,
            status=FbsControllerToteOrder.STATUS_LABELED,
            units=1,
            label_confirmed_by=self.controller,
        )
        request = FbsPickRestockRequest.objects.create(
            batch=batch,
            order=self.order,
            handover_assignment=assignment,
            status=FbsPickRestockRequest.STATUS_QUEUED,
            reason_code=FbsPickRestockRequest.REASON_CLIENT_CANCELED,
            reason="Marketplace cancellation confirmed",
            planned_qty=1,
            created_by=self.controller,
        )

        release_order_pick_restock_to_queue(
            request_id=request.id,
            comment="Physically placed into canceled tote",
            confirm_physical=True,
            canceled_tote_scan=canceled_tote.barcode,
            handover_batch_id=handover_batch.id,
            performed_by=self.controller,
        )

        request.refresh_from_db()
        assignment.refresh_from_db()
        self.order.refresh_from_db()
        tote_order.refresh_from_db()
        movement = FbsToteMovement.objects.get(
            tote=canceled_tote,
            target_kind="canceled_tote",
            details__request_id=request.id,
        )
        self.assertEqual(request.source_tote_id, canceled_tote.id)
        self.assertEqual(assignment.status, FbsHandoverOrderAssignment.STATUS_CANCELED)
        self.assertEqual(self.order.internal_status, FbsOrder.STATUS_EXCEPTION)
        self.assertEqual(movement.controller_session_id, session.id)
        self.assertEqual(tote_order.status, FbsControllerToteOrder.STATUS_REMOVED)

    def _late_canceled_order_return(self, *, suffix: str):
        context = self._controller_problem_context(suffix=suffix)
        Employee.objects.create(
            user=self.controller,
            full_name=f"Контролер возврата {suffix}",
            role="fbs_controller",
        )
        self.order.marketplace_status = "complete"
        self.order.marketplace_substatus = "canceled_by_client"
        self.order.save(update_fields=["marketplace_status", "marketplace_substatus", "updated_at"])
        handover_batch = FbsHandoverBatch.objects.create(
            profile=self.profile,
            status=FbsHandoverBatch.STATUS_OPEN,
        )
        assignment = FbsHandoverOrderAssignment.objects.create(
            batch=handover_batch,
            order=self.order,
            status=FbsHandoverOrderAssignment.STATUS_CONFIRMED,
        )
        request = FbsPickRestockRequest.objects.create(
            batch=context["allocation"].pick_task.batch,
            order=self.order,
            handover_assignment=assignment,
            status=FbsPickRestockRequest.STATUS_QUEUED,
            reason_code=FbsPickRestockRequest.REASON_CLIENT_CANCELED,
            reason="Marketplace cancellation confirmed after label printing",
            planned_qty=1,
            created_by=self.controller,
        )
        FbsPickRestockLine.objects.create(
            request=request,
            allocation=context["allocation"],
            source_balance=context["balance"],
            source_box=self.source_box,
            source_cell=self.source_box.pallet.cell,
            planned_qty=1,
        )
        label = FbsOrderLabel.objects.create(
            order=self.order,
            marketplace=FbsIntegrationProfile.MARKETPLACE_WB,
            barcode=f"*MISSING-{suffix}",
            external_label_id=f"PRINTED-{suffix}",
            status=FbsOrderLabel.STATUS_APPLIED,
            applied_by=self.controller,
            applied_at=timezone.now(),
        )
        print_job = ProcessingPrintJob.objects.create(
            status=ProcessingPrintJob.STATUS_PRINTED,
            order_id=self.order.external_order_id,
            card_id=f"fbs:order-label:{label.id}:missing-paper",
            barcode=label.barcode,
        )
        context.update(
            handover_batch=handover_batch,
            assignment=assignment,
            request=request,
            label=label,
            print_job=print_job,
        )
        return context

    def test_controller_can_identify_canceled_order_by_product_when_label_is_missing(self):
        context = self._late_canceled_order_return(suffix="MISSING-LABEL")

        confirm_problem_order_to_tote_without_label(
            batch_id=context["handover_batch"].id,
            order_id=self.order.id,
            product_scans=[context["balance"].barcode],
            tote_scan=context["canceled_tote"].barcode,
            actor=self.controller,
        )

        context["request"].refresh_from_db()
        context["assignment"].refresh_from_db()
        movement = FbsToteMovement.objects.get(
            tote=context["canceled_tote"],
            target_kind="canceled_tote",
            details__request_id=context["request"].id,
        )
        product_scan = FbsPickRestockScan.objects.get(
            request=context["request"],
            stage=FbsPickRestockScan.STAGE_PICKUP_ITEM,
        )
        self.assertEqual(context["request"].source_tote_id, context["canceled_tote"].id)
        self.assertEqual(
            context["assignment"].status,
            FbsHandoverOrderAssignment.STATUS_CANCELED,
        )
        self.assertEqual(movement.details["identification_mode"], "product_scans_missing_label")
        self.assertEqual(movement.details["product_scan_count"], 1)
        self.assertEqual(movement.details["label_id"], context["label"].id)
        self.assertEqual(movement.details["print_job_id"], context["print_job"].id)
        self.assertEqual(product_scan.scan_value, context["balance"].barcode)

    def test_existing_order_label_confirmation_path_still_works(self):
        context = self._late_canceled_order_return(suffix="LABEL-PATH")

        confirm_problem_order_to_tote(
            batch_id=context["handover_batch"].id,
            order_id=self.order.id,
            order_scan=context["label"].barcode,
            tote_scan=context["canceled_tote"].barcode,
            actor=self.controller,
        )

        movement = FbsToteMovement.objects.get(
            tote=context["canceled_tote"],
            target_kind="canceled_tote",
            details__request_id=context["request"].id,
        )
        self.assertEqual(movement.details["identification_mode"], "order_label")
        self.assertEqual(movement.details["order_scan"], context["label"].barcode)
        self.assertEqual(movement.details["product_scan_count"], 0)

    def test_missing_label_fallback_rejects_order_number_instead_of_product(self):
        context = self._late_canceled_order_return(suffix="ORDER-NUMBER")

        with self.assertRaisesMessage(FbsPickingError, "не относится к товарам"):
            confirm_problem_order_to_tote_without_label(
                batch_id=context["handover_batch"].id,
                order_id=self.order.id,
                product_scans=[self.order.external_order_id],
                tote_scan=context["canceled_tote"].barcode,
                actor=self.controller,
            )

        context["request"].refresh_from_db()
        self.assertIsNone(context["request"].source_tote_id)
        self.assertFalse(
            FbsToteMovement.objects.filter(details__request_id=context["request"].id).exists()
        )

    def test_missing_label_fallback_resolves_marked_unit_from_order_by_product_barcode(self):
        context = self._late_canceled_order_return(suffix="MARKED")
        marking_code = f"01{context['balance'].barcode.zfill(14)}21SERIAL123"
        context["balance"].marking_code = marking_code
        context["balance"].save(update_fields=["marking_code", "updated_at"])
        FbsOrderTraceability.objects.create(
            allocation=context["allocation"],
            marking_code=marking_code,
            qty=1,
            status=FbsOrderTraceability.STATUS_PICKED,
        )

        confirm_problem_order_to_tote_without_label(
            batch_id=context["handover_batch"].id,
            order_id=self.order.id,
            product_scans=[context["balance"].barcode],
            tote_scan=context["canceled_tote"].barcode,
            actor=self.controller,
        )

        scan = FbsPickRestockScan.objects.get(
            request=context["request"],
            stage=FbsPickRestockScan.STAGE_PICKUP_ITEM,
        )
        movement = FbsToteMovement.objects.get(
            tote=context["canceled_tote"],
            target_kind="canceled_tote",
            details__request_id=context["request"].id,
        )
        self.assertEqual(scan.scan_value, context["balance"].barcode)
        self.assertEqual(scan.expected_value, marking_code)
        self.assertIn("КИЗ выбран из привязки", scan.message)
        self.assertEqual(
            movement.details["marking_resolution_mode"],
            "order_traceability",
        )

    def test_missing_label_fallback_rejects_data_matrix_from_another_order(self):
        context = self._late_canceled_order_return(suffix="FOREIGN-MARK")
        marking_code = f"01{context['balance'].barcode.zfill(14)}21ORDER-MARK"
        context["balance"].marking_code = marking_code
        context["balance"].save(update_fields=["marking_code", "updated_at"])
        FbsOrderTraceability.objects.create(
            allocation=context["allocation"],
            marking_code=marking_code,
            qty=1,
            status=FbsOrderTraceability.STATUS_PICKED,
        )
        foreign_marking_code = (
            f"01{context['balance'].barcode.zfill(14)}21FOREIGN-ORDER-MARK"
        )

        with self.assertRaisesMessage(FbsPickingError, "не относится к товарам"):
            confirm_problem_order_to_tote_without_label(
                batch_id=context["handover_batch"].id,
                order_id=self.order.id,
                product_scans=[foreign_marking_code],
                tote_scan=context["canceled_tote"].barcode,
                actor=self.controller,
            )

        context["request"].refresh_from_db()
        self.assertIsNone(context["request"].source_tote_id)

    def test_controller_problem_window_renders_missing_label_product_scan_path(self):
        rendered = render_to_string(
            "fbs/_controller_problems.html",
            {
                "request_role": "fbs_controller",
                "controller_problem_count": 1,
                "controller_problem_force_open": True,
                "controller_problem_action_count": 1,
                "controller_problem_batch_id": 398,
                "controller_problem_url": "/fbs/handover/398/",
                "controller_problem_fingerprint": "test-fingerprint",
                "controller_delivery_sent": False,
                "controller_problems": [
                    {
                        "order_id": self.order.id,
                        "order_number": self.order.external_order_id,
                        "sticker": "not-physically-printed",
                        "kind": "cancel",
                        "reason": "Заказ отменён покупателем",
                        "instruction": "Проверьте физический товар.",
                        "quantity": 1,
                        "actionable": True,
                        "label_actionable": False,
                        "product_scan_actionable": True,
                        "product_scan_total": 1,
                        "product_scan_rows": [
                            {
                                "scan_no": 1,
                                "next_scan_no": None,
                                "name": "Problem tote item",
                                "barcode": self.order_item.barcode,
                                "requires_marking": True,
                            }
                        ],
                        "tote": {"name": "Тара 7", "barcode": "FBS-CART-000007"},
                        "retry_items": [],
                    }
                ],
            },
        )

        self.assertIn("Этикетка отсутствует — проверить товар", rendered)
        self.assertIn('value="resolve_problem_order_without_label"', rendered)
        self.assertIn('name="product_scan"', rendered)
        self.assertIn("Штрихкод товара", rendered)
        self.assertIn("КИЗ будет выбран из заказа", rendered)
        self.assertNotIn('data-agent-scan-kind="marking"', rendered)
        self.assertNotIn('name="order_scan"', rendered)

    def test_controller_can_stage_waiting_wb_order_and_continue_without_stock_change(self):
        context = self._controller_problem_context(suffix="WAITING-WB-STAGE")
        handover_batch = FbsHandoverBatch.objects.create(
            profile=self.profile,
            status=FbsHandoverBatch.STATUS_OPEN,
        )
        assignment = FbsHandoverOrderAssignment.objects.create(
            batch=handover_batch,
            order=self.order,
            status=FbsHandoverOrderAssignment.STATUS_CONFIRMED,
        )
        request = FbsPickRestockRequest.objects.create(
            batch=context["allocation"].pick_task.batch,
            order=self.order,
            handover_assignment=assignment,
            status=FbsPickRestockRequest.STATUS_WAITING_MARKETPLACE,
            reason_code=FbsPickRestockRequest.REASON_MARKETPLACE,
            reason="WB still verifies supply membership",
            marketplace_action=FbsPickRestockRequest.MARKETPLACE_ACTION_VERIFY_CANCEL,
            planned_qty=1,
            created_by=self.controller,
        )
        stock_before = (
            context["balance"].qty,
            context["balance"].available_qty,
            context["balance"].reserved_qty,
        )

        confirm_marketplace_rejected_order_return(
            request_id=request.id,
            canceled_tote_scan=context["canceled_tote"].barcode,
            performed_by=self.controller,
        )
        confirm_marketplace_rejected_order_return(
            request_id=request.id,
            canceled_tote_scan=context["canceled_tote"].barcode,
            performed_by=self.controller,
        )

        request.refresh_from_db()
        assignment.refresh_from_db()
        context["balance"].refresh_from_db()
        self.order.refresh_from_db()
        self.assertEqual(
            request.status,
            FbsPickRestockRequest.STATUS_WAITING_MARKETPLACE,
        )
        self.assertEqual(request.source_tote_id, context["canceled_tote"].id)
        self.assertEqual(
            assignment.status,
            FbsHandoverOrderAssignment.STATUS_CANCELED,
        )
        self.assertEqual(
            self.order.hold_reason,
            "handover_exclusion_waiting_marketplace",
        )
        self.assertEqual(
            (
                context["balance"].qty,
                context["balance"].available_qty,
                context["balance"].reserved_qty,
            ),
            stock_before,
        )
        self.assertEqual(
            FbsToteMovement.objects.filter(
                tote=context["canceled_tote"],
                target_kind="canceled_tote",
                details__request_id=request.id,
            ).count(),
            1,
        )

    def test_controller_stage_rejects_wrong_tote_and_other_handover(self):
        context = self._controller_problem_context(suffix="WAITING-WB-GUARDS")
        handover_batch = FbsHandoverBatch.objects.create(
            profile=self.profile,
            status=FbsHandoverBatch.STATUS_OPEN,
        )
        other_handover = FbsHandoverBatch.objects.create(
            profile=self.profile,
            status=FbsHandoverBatch.STATUS_OPEN,
        )
        assignment = FbsHandoverOrderAssignment.objects.create(
            batch=handover_batch,
            order=self.order,
            status=FbsHandoverOrderAssignment.STATUS_CONFIRMED,
        )
        request = FbsPickRestockRequest.objects.create(
            batch=context["allocation"].pick_task.batch,
            order=self.order,
            handover_assignment=assignment,
            status=FbsPickRestockRequest.STATUS_FAILED,
            reason_code=FbsPickRestockRequest.REASON_MARKETPLACE,
            reason="WB readback failed",
            marketplace_action=FbsPickRestockRequest.MARKETPLACE_ACTION_VERIFY_CANCEL,
            planned_qty=1,
            created_by=self.controller,
        )

        with self.assertRaisesMessage(FbsPickingError, "Неверная тара возврата"):
            release_order_pick_restock_to_queue(
                request_id=request.id,
                comment="",
                confirm_physical=True,
                canceled_tote_scan=context["problem_tote"].barcode,
                handover_batch_id=handover_batch.id,
                performed_by=self.controller,
            )
        with self.assertRaisesMessage(FbsPickingError, "другой отгрузке"):
            release_order_pick_restock_to_queue(
                request_id=request.id,
                comment="",
                confirm_physical=True,
                canceled_tote_scan=context["canceled_tote"].barcode,
                handover_batch_id=other_handover.id,
                performed_by=self.controller,
            )

        request.refresh_from_db()
        assignment.refresh_from_db()
        self.assertIsNone(request.source_tote_id)
        self.assertEqual(
            assignment.status,
            FbsHandoverOrderAssignment.STATUS_CONFIRMED,
        )
        self.assertFalse(
            FbsToteMovement.objects.filter(details__request_id=request.id).exists()
        )

    def test_controller_can_stage_failed_wb_order_without_unlocking_final_status(self):
        context = self._controller_problem_context(suffix="FAILED-WB-STAGE")
        handover_batch = FbsHandoverBatch.objects.create(
            profile=self.profile,
            status=FbsHandoverBatch.STATUS_READY,
        )
        assignment = FbsHandoverOrderAssignment.objects.create(
            batch=handover_batch,
            order=self.order,
            status=FbsHandoverOrderAssignment.STATUS_CONFIRMED,
        )
        request = FbsPickRestockRequest.objects.create(
            batch=context["allocation"].pick_task.batch,
            order=self.order,
            handover_assignment=assignment,
            status=FbsPickRestockRequest.STATUS_FAILED,
            reason_code=FbsPickRestockRequest.REASON_MARKETPLACE,
            reason="WB readback exhausted retries",
            marketplace_action=FbsPickRestockRequest.MARKETPLACE_ACTION_VERIFY_CANCEL,
            planned_qty=1,
            created_by=self.controller,
        )

        confirm_marketplace_rejected_order_return(
            request_id=request.id,
            canceled_tote_scan=context["canceled_tote"].barcode,
            performed_by=self.controller,
        )

        request.refresh_from_db()
        assignment.refresh_from_db()
        self.order.refresh_from_db()
        self.assertEqual(request.status, FbsPickRestockRequest.STATUS_FAILED)
        self.assertEqual(request.source_tote_id, context["canceled_tote"].id)
        self.assertEqual(
            assignment.status,
            FbsHandoverOrderAssignment.STATUS_CANCELED,
        )
        self.assertEqual(self.order.hold_reason, "handover_exclusion_failed")

    def test_canceled_tote_restock_returns_item_to_original_available_stock(self):
        workstation = FbsWorkstation.objects.create(
            barcode="FBS-WS-CANCELED-RESTOCK",
            name="Canceled restock workstation",
        )
        unknown_tote = FbsPickingCart.objects.create(
            barcode="FBS-CART-RESTOCK-UNKNOWN",
            name="Unknown tote",
        )
        problem_tote = FbsPickingCart.objects.create(
            barcode="FBS-CART-RESTOCK-PROBLEM",
            name="Problem tote",
        )
        canceled_tote = FbsPickingCart.objects.create(
            barcode="FBS-CART-RESTOCK-CANCELED",
            name="Canceled order tote",
        )
        start_controller_session(
            workstation_id=workstation.id,
            controller=self.controller,
            unknown_tote_scan=unknown_tote.barcode,
            problem_tote_scan=problem_tote.barcode,
            canceled_tote_scan=canceled_tote.barcode,
            first_check_tote_scan=self.first_check_tote.barcode,
        )
        batch = FbsPickBatch.objects.create(
            agency=self.agency,
            status=FbsPickBatch.STATUS_VERIFICATION,
            planned_qty=1,
            picked_qty=1,
            workstation=workstation,
        )
        task = FbsPickTask.objects.create(
            batch=batch,
            order=self.order,
            status=FbsPickTask.STATUS_PICKED,
            planned_qty=1,
            picked_qty=1,
        )
        allocation, source_balance = self._picked_allocation(pick_task=task)
        request = FbsPickRestockRequest.objects.create(
            batch=batch,
            order=self.order,
            source_tote=canceled_tote,
            status=FbsPickRestockRequest.STATUS_QUEUED,
            reason_code=FbsPickRestockRequest.REASON_CLIENT_CANCELED,
            reason="Canceled order",
            planned_qty=1,
        )
        FbsPickRestockLine.objects.create(
            request=request,
            allocation=allocation,
            source_balance=source_balance,
            source_box=self.source_box,
            source_cell=self.source_box.pallet.cell,
            planned_qty=1,
        )
        claim_pick_restock_request(request_id=request.id, assigned_to=self.user)

        for stage, value in (
            (FbsPickRestockScan.STAGE_WORKSTATION, canceled_tote.barcode),
            (FbsPickRestockScan.STAGE_PICKUP_ITEM, source_balance.barcode),
            (
                FbsPickRestockScan.STAGE_CELL,
                self.source_box.pallet.cell.location.location_code,
            ),
            (FbsPickRestockScan.STAGE_BOX, self.source_box.box_code),
            (FbsPickRestockScan.STAGE_ITEM, source_balance.barcode),
        ):
            scan_pick_restock(
                request_id=request.id,
                stage=stage,
                scan_value=value,
                request_token=uuid.uuid4(),
                performed_by=self.user,
                pickup_line_id=(
                    pick_restock_state(request.id)["line"].id
                    if stage == FbsPickRestockScan.STAGE_PICKUP_ITEM
                    else None
                ),
            )

        request.refresh_from_db()
        task.refresh_from_db()
        self.order.refresh_from_db()
        source_balance.refresh_from_db()
        self.assertEqual(request.status, FbsPickRestockRequest.STATUS_COMPLETED)
        self.assertEqual(task.status, FbsPickTask.STATUS_CANCELED)
        self.assertEqual(self.order.internal_status, FbsOrder.STATUS_CANCELLED)
        self.assertEqual(
            (source_balance.qty, source_balance.available_qty),
            (1, 1),
        )
