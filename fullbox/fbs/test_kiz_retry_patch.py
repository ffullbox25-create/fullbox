from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db.models import Sum
from django.test import RequestFactory, TestCase, override_settings
from django.template.loader import render_to_string
from django.utils import timezone

from audit.models import OrderAuditEntry
from employees.models import Employee
from sku.models import Agency, SKU, SKUBarcode
from sklad.models import WarehouseLocation

from .exceptions import FbsIntegrationError, FbsPickingError
from .integrations.contracts import (
    WB_ADD_ORDER_TO_HANDOVER,
    WB_CANCEL_ORDER,
    WB_CREATE_HANDOVER_SUPPLY,
    WB_DELIVER_HANDOVER,
    WB_READ_HANDOVER_ORDER_IDS,
    WB_READ_HANDOVER_SUPPLY,
    WB_READ_ORDER_METADATA,
    WB_SET_ORDER_SGTINS,
)
from .integrations.http import MarketplaceHttpResponse
from .models import (
    FbsBox,
    FbsControllerCheckTote,
    FbsControllerPickTote,
    FbsControllerSession,
    FbsControllerToteOrder,
    FbsHandoverBatch,
    FbsHandoverBox,
    FbsHandoverOrder,
    FbsHandoverOrderAssignment,
    FbsIntegrationProfile,
    FbsMarketplaceCommand,
    FbsMarketplaceMetadataTransfer,
    FbsOrder,
    FbsOrderItem,
    FbsOrderLabel,
    FbsOrderStockAllocation,
    FbsOrderTraceability,
    FbsPallet,
    FbsPickBatch,
    FbsPickException,
    FbsPickScanEvent,
    FbsPickVerificationProgress,
    FbsPickingCart,
    FbsProblemToteItem,
    FbsPickTask,
    FbsStockBalance,
    FbsStorageCell,
    FbsToteBinding,
    FbsToteMovement,
    FbsToteZone,
    FbsWorkstation,
)
from .services.marketplace import (
    _apply_wb_metadata_readback,
    _set_transfer_state,
    _wb_metadata_rejection_error,
    is_final_wb_marking_rejection,
    process_marketplace_command,
    retry_wb_marking_code,
    schedule_wb_handover_order,
)
from .controller_views import (
    _check_tote_metadata_poll_orders,
    _check_tote_metadata_status,
)
from .services.handover import handover_composition_readiness
from .services.labels import confirm_order_label_scan
from .services.pick_restock import (
    finalize_invalid_kiz_reroute,
    request_invalid_kiz_reroute,
)
from .services.picking import verify_pick_allocation_unit
from .services.traceability import create_allocation_trace
from .services.totes import attach_pick_tote_to_available_check_tote
from .tsd_views import _handover_detail_summary, tsd_handover_detail


urlpatterns = []


class _StaticTransport:
    def __init__(self, payload=None, status=200):
        self.payload = payload
        self.status = status

    def send(self, command):
        return MarketplaceHttpResponse(
            status_code=self.status,
            headers={"Content-Type": "application/json"},
            content=b"",
            json_payload=self.payload,
        )


@override_settings(
    ROOT_URLCONF="fbs.test_kiz_retry_patch",
    FBS_MODULE_ENABLED=True,
    FBS_WAREHOUSE_WRITES_ENABLED=True,
    FBS_OUTBOX_ENABLED=True,
    FBS_MARKING_PUSH_ENABLED=True,
)
class FbsKizRetryPatchTests(TestCase):
    OLD_KIZ = "010466040680002221OLD123"
    VALID_KIZ = "010466040680002221ABC123"
    OTHER_PRODUCT_KIZ = "010461046911083521ABC123"

    def test_invalid_kiz_rewave_route_survives_submit_once_button_lock(self):
        template = (
            Path(settings.BASE_DIR) / "templates/fbs/tsd_handover_detail.html"
        ).read_text(encoding="utf-8")
        rewave_route = '<input type="hidden" name="route" value="rewave">'
        rewave_button = (
            '<button class="button button-primary button-compact" '
            'type="submit">На добор / новая волна</button>'
        )
        quarantine_button = (
            '<button class="button button-danger button-compact" type="submit" '
            'name="route" value="quarantine">В карантин кладовщику</button>'
        )

        self.assertIn(rewave_route, template)
        self.assertIn(rewave_button, template)
        self.assertIn(quarantine_button, template)
        self.assertLess(template.index(rewave_route), template.index(rewave_button))
        self.assertLess(template.index(rewave_button), template.index(quarantine_button))

    def test_handover_template_has_retry_all_queue_controls(self):
        template = (
            Path(settings.BASE_DIR) / "templates/fbs/tsd_handover_detail.html"
        ).read_text(encoding="utf-8")

        self.assertIn("Пересканировать все проблемные КИЗы", template)
        self.assertIn('data-open-kiz-retry-all', template)
        self.assertIn('name="kiz_rescan_all"', template)
        self.assertIn("Проверить и перейти к следующему", template)

    def test_handover_template_shows_label_number_and_product_barcode_on_retry(self):
        template = (
            Path(settings.BASE_DIR) / "templates/fbs/tsd_handover_detail.html"
        ).read_text(encoding="utf-8")

        self.assertIn("Номер этикетки WB", template)
        self.assertIn("Штрихкод товара", template)
        self.assertIn('data-kiz-retry-label-number', template)
        self.assertIn('data-kiz-retry-product-barcode', template)
        self.assertIn(
            'data-label-number="{{ item.kiz_retry_label_number|escape }}"',
            template,
        )
        self.assertIn(
            'data-product-barcode="{{ item.kiz_retry_product_barcode|escape }}"',
            template,
        )
        self.assertIn(
            "Скан передаётся в WB без внутреннего сопоставления",
            template,
        )
        self.assertNotIn(
            "Штрихкод товара, русские буквы и неполный код не будут приняты",
            template,
        )

    def setUp(self):
        self.user = get_user_model().objects.create_user(username="kiz_retry")
        Employee.objects.create(
            user=self.user,
            full_name="KIZ retry controller",
            role="fbs_controller",
        )
        self.session_controller = get_user_model().objects.create_user(
            username="controller_on_previous_shift"
        )
        self.agency = Agency.objects.create(agn_name="KIZ retry client")
        self.sku = SKU.objects.create(
            agency=self.agency,
            sku_code="KIZ-SKU",
            name="Marked product",
        )
        SKUBarcode.objects.create(sku=self.sku, value="4660406800022")
        self.profile = FbsIntegrationProfile.objects.create(
            agency=self.agency,
            marketplace=FbsIntegrationProfile.MARKETPLACE_WB,
            name="WB KIZ retry",
            external_account_id="wb-kiz-retry",
            external_warehouse_id="1876669",
            is_active=True,
            outbox_enabled=True,
            marking_push_enabled=True,
        )
        location = WarehouseLocation.objects.create(
            warehouse_code="MSK",
            zone_code="FBS",
            zone_kind=WarehouseLocation.ZONE_KIND_STORAGE,
            row_no=1,
            section_no=1,
            tier_no=1,
            cell_no=1,
            location_code="FBS-KIZ-1",
            is_storage=True,
            is_pickable=True,
        )
        cell = FbsStorageCell.objects.create(cell_code="FBS-KIZ-1", location=location)
        pallet = FbsPallet.objects.create(
            agency=self.agency,
            pallet_code="FBS-KIZ-PALLET",
            cell=cell,
            status=FbsPallet.STATUS_ACTIVE,
        )
        box = FbsBox.objects.create(
            agency=self.agency,
            pallet=pallet,
            box_code="FBS-KIZ-BOX",
            status=FbsBox.STATUS_ACTIVE,
        )
        self.balance = FbsStockBalance.objects.create(
            agency=self.agency,
            box=box,
            sku_ref=self.sku,
            identity_key="1" * 64,
            sku_code=self.sku.sku_code,
            name=self.sku.name,
            barcode="4660406800022",
            marking_code=self.OLD_KIZ,
            qty=0,
            available_qty=0,
        )
        self.order = FbsOrder.objects.create(
            profile=self.profile,
            external_order_id="5497933554",
            internal_status=FbsOrder.STATUS_READY_FOR_HANDOVER,
            marketplace_status="confirm",
        )
        self.item = FbsOrderItem.objects.create(
            order=self.order,
            external_line_id="KIZ-LINE-1",
            external_sku=self.sku.sku_code,
            sku=self.sku,
            barcode=self.balance.barcode,
            product_name=self.sku.name,
            quantity=1,
            requirements={"required_meta": ["sgtin"]},
        )
        allocation = FbsOrderStockAllocation.objects.create(
            order_item=self.item,
            balance=self.balance,
            qty_reserved=1,
            qty_picked=1,
            status=FbsOrderStockAllocation.STATUS_PICKED,
        )
        self.trace = FbsOrderTraceability.objects.create(
            allocation=allocation,
            marking_code=self.OLD_KIZ,
            qty=1,
            status=FbsOrderTraceability.STATUS_PICKED,
        )
        self.transfer = FbsMarketplaceMetadataTransfer.objects.create(
            order_item=self.item,
            traceability=self.trace,
            metadata_type=FbsMarketplaceMetadataTransfer.TYPE_MARKING_CODE,
            value=self.OLD_KIZ,
            is_required=True,
            status=FbsMarketplaceMetadataTransfer.STATUS_CONFLICT,
            idempotency_key="bad-kiz-transfer",
            external_status="wb_rejected:sgtinintroduced",
            last_error=(
                "WB отклонил КИЗ: код уже введен в оборот и отклонен площадкой."
            ),
        )
        self.batch = FbsHandoverBatch.objects.create(
            profile=self.profile,
            external_supply_id="WB-GI-TEST",
            status=FbsHandoverBatch.STATUS_OPEN,
            marketplace_state=FbsHandoverBatch.MARKETPLACE_OPEN,
        )
        self.assignment = FbsHandoverOrderAssignment.objects.create(
            batch=self.batch,
            order=self.order,
            status=FbsHandoverOrderAssignment.STATUS_CONFIRMED,
        )
        self.workstation = FbsWorkstation.objects.create(
            barcode="FBS-WS-900001",
            name="KIZ controller desk",
        )
        self.problem_tote = FbsPickingCart.objects.create(
            barcode="FBS-CART-900001",
            name="KIZ problem tote",
        )
        self.unknown_tote = FbsPickingCart.objects.create(
            barcode="FBS-CART-900002",
            name="KIZ unknown tote",
        )
        self.free_zone = FbsToteZone.objects.create(
            barcode="FBS-TOTE-ZONE-KIZ-TEST",
            name="KIZ free tote zone",
            kind=FbsToteZone.KIND_FREE,
        )
        self.controller_session = FbsControllerSession.objects.create(
            workstation=self.workstation,
            controller=self.session_controller,
            unknown_tote=self.unknown_tote,
            problem_tote=self.problem_tote,
            free_zone=self.free_zone,
        )
        FbsToteBinding.objects.create(
            tote=self.problem_tote,
            state=FbsToteBinding.STATE_AT_CONTROL,
            workstation=self.workstation,
            controller_session=self.controller_session,
            updated_by=self.session_controller,
        )

    def _prepare_invalid_kiz_reroute(
        self,
        *,
        create_handover_link=True,
        order_status=FbsOrder.STATUS_PICKED,
    ):
        self.order.internal_status = order_status
        self.order.save(update_fields=["internal_status", "updated_at"])
        pick_batch = FbsPickBatch.objects.create(
            agency=self.agency,
            workstation=self.workstation,
            status=FbsPickBatch.STATUS_VERIFICATION,
            planned_qty=1,
            picked_qty=1,
            picking_completed_at=timezone.now(),
            verification_assigned_to=self.user,
        )
        task = FbsPickTask.objects.create(
            batch=pick_batch,
            order=self.order,
            status=FbsPickTask.STATUS_PICKED,
            planned_qty=1,
            picked_qty=1,
        )
        allocation = self.trace.allocation
        allocation.pick_task = task
        allocation.save(update_fields=["pick_task", "updated_at"])
        handover_link = None
        if create_handover_link:
            handover_box = FbsHandoverBox.objects.create(
                batch=self.batch,
                qr_code="FBS-KIZ-HANDOVER-BOX",
            )
            handover_link = FbsHandoverOrder.objects.create(
                box=handover_box,
                order=self.order,
                added_by=self.user,
            )
        return pick_batch, task, allocation, handover_link

    def _complete_reroute_supply_move(self, *, target_batch, supply_id):
        create_supply = FbsMarketplaceCommand.objects.get(
            handover_batch=target_batch,
            command_type=WB_CREATE_HANDOVER_SUPPLY,
        )
        process_marketplace_command(
            command_id=create_supply.id,
            transport=_StaticTransport({"id": supply_id}),
        )
        add_order = FbsMarketplaceCommand.objects.get(
            handover_batch=target_batch,
            command_type=WB_ADD_ORDER_TO_HANDOVER,
        )
        process_marketplace_command(
            command_id=add_order.id,
            transport=_StaticTransport(status=204),
        )
        read_supply = FbsMarketplaceCommand.objects.get(
            handover_batch=target_batch,
            command_type=WB_READ_HANDOVER_SUPPLY,
        )
        process_marketplace_command(
            command_id=read_supply.id,
            transport=_StaticTransport({"id": supply_id, "done": False}),
        )
        read_orders = FbsMarketplaceCommand.objects.get(
            handover_batch=target_batch,
            command_type=WB_READ_HANDOVER_ORDER_IDS,
        )
        process_marketplace_command(
            command_id=read_orders.id,
            transport=_StaticTransport(
                {"orderIds": [int(self.order.external_order_id)]}
            ),
        )

    def _prepare_deferred_kiz_verification(self):
        self.transfer.delete()
        self.trace.marking_code = ""
        self.trace.status = FbsOrderTraceability.STATUS_PICKED
        self.trace.save(update_fields=["marking_code", "status", "updated_at"])
        self.item.requirements = {
            "required_meta": ["sgtin"],
            "wb_marking_codes": [self.OLD_KIZ],
        }
        self.item.save(update_fields=["requirements", "updated_at"])
        self.order.internal_status = FbsOrder.STATUS_PICKED
        self.order.marketplace_status = "confirm"
        self.order.save(
            update_fields=["internal_status", "marketplace_status", "updated_at"]
        )
        pick_batch = FbsPickBatch.objects.create(
            agency=self.agency,
            workstation=self.workstation,
            status=FbsPickBatch.STATUS_VERIFICATION,
            planned_qty=1,
            picked_qty=1,
            picking_completed_at=timezone.now(),
            verification_started_at=timezone.now(),
            verification_assigned_to=self.user,
        )
        task = FbsPickTask.objects.create(
            batch=pick_batch,
            order=self.order,
            status=FbsPickTask.STATUS_PICKED,
            planned_qty=1,
            picked_qty=1,
        )
        allocation = self.trace.allocation
        allocation.pick_task = task
        allocation.status = FbsOrderStockAllocation.STATUS_PICKED
        allocation.qty_reserved = 1
        allocation.qty_picked = 1
        allocation.save(
            update_fields=[
                "pick_task",
                "status",
                "qty_reserved",
                "qty_picked",
                "updated_at",
            ]
        )
        label = FbsOrderLabel.objects.create(
            order=self.order,
            marketplace=FbsIntegrationProfile.MARKETPLACE_WB,
            external_label_id="WB-DEFERRED-KIZ-LABEL",
            barcode="WB-DEFERRED-KIZ-BARCODE",
            status=FbsOrderLabel.STATUS_READY,
            ready_at=timezone.now(),
        )
        return pick_batch, task, allocation, label

    def _prepare_ozon_kiz_verification(self):
        self.transfer.delete()
        self.assignment.delete()
        ozon_profile = FbsIntegrationProfile.objects.create(
            agency=self.agency,
            marketplace=FbsIntegrationProfile.MARKETPLACE_OZON,
            name="Ozon KIZ verification",
            external_account_id="ozon-kiz-verification",
            external_warehouse_id="1020005028894230",
            is_active=True,
        )
        self.order.profile = ozon_profile
        self.order.internal_status = FbsOrder.STATUS_PICKED
        self.order.marketplace_status = "awaiting_packaging"
        self.order.save(
            update_fields=["profile", "internal_status", "marketplace_status", "updated_at"]
        )
        self.item.requirements = {"required_meta": ["marking"]}
        self.item.save(update_fields=["requirements", "updated_at"])
        pick_batch = FbsPickBatch.objects.create(
            agency=self.agency,
            workstation=self.workstation,
            status=FbsPickBatch.STATUS_VERIFICATION,
            planned_qty=1,
            picked_qty=1,
            picking_completed_at=timezone.now(),
            verification_started_at=timezone.now(),
            verification_assigned_to=self.user,
        )
        task = FbsPickTask.objects.create(
            batch=pick_batch,
            order=self.order,
            status=FbsPickTask.STATUS_PICKED,
            planned_qty=1,
            picked_qty=1,
        )
        allocation = self.trace.allocation
        allocation.pick_task = task
        allocation.status = FbsOrderStockAllocation.STATUS_PICKED
        allocation.qty_reserved = 1
        allocation.qty_picked = 1
        allocation.save(
            update_fields=[
                "pick_task",
                "status",
                "qty_reserved",
                "qty_picked",
                "updated_at",
            ]
        )
        return pick_batch, task, allocation

    def _create_ozon_technical_kiz_owner(
        self, *, pick_batch, marking_code, verified=False
    ):
        other_order = FbsOrder.objects.create(
            profile=self.order.profile,
            external_order_id=f"OZON-TECHNICAL-{FbsOrder.objects.count()}",
            internal_status=FbsOrder.STATUS_PICKED,
            marketplace_status="awaiting_packaging",
        )
        other_item = FbsOrderItem.objects.create(
            order=other_order,
            external_line_id=f"OZON-TECHNICAL-LINE-{other_order.id}",
            external_sku=self.sku.sku_code,
            sku=self.sku,
            barcode=self.balance.barcode,
            product_name=self.sku.name,
            quantity=1,
            requirements={"required_meta": ["marking"]},
        )
        other_task = FbsPickTask.objects.create(
            batch=pick_batch,
            order=other_order,
            status=FbsPickTask.STATUS_PICKED,
            planned_qty=1,
            picked_qty=1,
        )
        other_allocation = FbsOrderStockAllocation.objects.create(
            order_item=other_item,
            balance=self.balance,
            pick_task=other_task,
            qty_reserved=1,
            qty_picked=1,
            status=FbsOrderStockAllocation.STATUS_PICKED,
        )
        other_trace = FbsOrderTraceability.objects.create(
            allocation=other_allocation,
            marking_code=marking_code,
            qty=1,
            status=FbsOrderTraceability.STATUS_PICKED,
        )
        if verified:
            FbsPickVerificationProgress.objects.create(
                allocation=other_allocation,
                qty_verified=1,
                verified_by=self.user,
                started_at=timezone.now(),
                completed_at=timezone.now(),
            )
        return other_order, other_allocation, other_trace

    def _prepare_ozon_grouped_verification(self, qty=2, *, internal_only=False):
        pick_batch, task, allocation = self._prepare_ozon_kiz_verification()
        self.balance.marking_code = ""
        self.balance.save(update_fields=["marking_code", "updated_at"])
        self.item.quantity = qty
        if internal_only:
            self.sku.honest_sign = True
            self.sku.save(update_fields=["honest_sign"])
            self.item.requirements = {"is_kiz": False, "mandatory_mark": []}
            self.item.raw_payload = {"is_kiz": False}
        self.item.save(update_fields=["quantity", "requirements", "raw_payload", "updated_at"])
        for row in (pick_batch, task):
            row.planned_qty = qty
            row.picked_qty = qty
            row.save(update_fields=["planned_qty", "picked_qty", "updated_at"])
        allocation.qty_reserved = qty
        allocation.qty_picked = qty
        allocation.save(update_fields=["qty_reserved", "qty_picked", "updated_at"])
        self.trace.qty = qty
        self.trace.marking_code = ""
        self.trace.save(update_fields=["qty", "marking_code", "updated_at"])
        return pick_batch, task, allocation

    def test_ozon_grouped_internal_marking_accepts_two_distinct_units(self):
        pick_batch, task, allocation = self._prepare_ozon_grouped_verification(
            internal_only=True,
        )
        balance_before = (self.balance.qty, self.balance.available_qty, self.balance.reserved_qty)
        for code in (self.VALID_KIZ, self.OTHER_PRODUCT_KIZ):
            current = FbsOrderStockAllocation.objects.filter(
                pick_task=task, traceability__marking_code="",
            ).get()
            progress = verify_pick_allocation_unit(
                allocation_id=current.id, item_scan=self.balance.barcode,
                marking_scan=code, performed_by=self.user,
            )
            self.assertEqual(progress.qty_verified, 1)
        rows = FbsOrderStockAllocation.objects.filter(pick_task=task)
        self.assertEqual(rows.count(), 2)
        self.assertEqual(rows.aggregate(reserved=Sum("qty_reserved"), picked=Sum("qty_picked")),
                         {"reserved": 2, "picked": 2})
        self.assertEqual(set(rows.values_list("traceability__marking_code", flat=True)),
                         {self.VALID_KIZ, self.OTHER_PRODUCT_KIZ})
        self.assertFalse(rows.exclude(traceability__qty=1).exists())
        self.assertEqual(FbsPickVerificationProgress.objects.filter(
            allocation__pick_task=task,
        ).aggregate(total=Sum("qty_verified"))["total"], 2)
        self.balance.refresh_from_db()
        self.assertEqual((self.balance.qty, self.balance.available_qty, self.balance.reserved_qty),
                         balance_before)
        pick_batch.refresh_from_db()
        task.refresh_from_db()
        self.assertEqual((pick_batch.planned_qty, pick_batch.picked_qty, task.planned_qty, task.picked_qty),
                         (2, 2, 2, 2))
        self.assertFalse(FbsMarketplaceMetadataTransfer.objects.filter(
            order_item=self.item, metadata_type=FbsMarketplaceMetadataTransfer.TYPE_MARKING_CODE,
        ).exists())

    def test_ozon_grouped_marking_waits_for_every_unit_before_metadata(self):
        _batch, task, _allocation = self._prepare_ozon_grouped_verification(qty=3)
        codes = (self.VALID_KIZ, self.OTHER_PRODUCT_KIZ, "010466040680002221THIRD1")
        for index, code in enumerate(codes):
            current = FbsOrderStockAllocation.objects.get(
                pick_task=task, traceability__marking_code="",
            )
            verify_pick_allocation_unit(
                allocation_id=current.id, item_scan=self.balance.barcode,
                marking_scan=code, performed_by=self.user,
            )
            if index < 2:
                self.assertFalse(FbsMarketplaceMetadataTransfer.objects.filter(order_item=self.item).exists())
                self.assertFalse(FbsOrderLabel.objects.filter(order=self.order).exists())
        transfers = FbsMarketplaceMetadataTransfer.objects.filter(
            order_item=self.item, metadata_type=FbsMarketplaceMetadataTransfer.TYPE_MARKING_CODE,
        )
        self.assertEqual(set(transfers.values_list("value", flat=True)), set(codes))
        self.assertEqual(transfers.count(), 3)

    def test_ozon_grouped_duplicate_kiz_rolls_back_split(self):
        batch, task, allocation = self._prepare_ozon_grouped_verification()
        other_order, _other_allocation, _other_trace = self._create_ozon_technical_kiz_owner(
            pick_batch=batch, marking_code=self.OTHER_PRODUCT_KIZ, verified=True,
        )
        with self.assertRaisesMessage(FbsPickingError, other_order.external_order_id):
            verify_pick_allocation_unit(
                allocation_id=allocation.id, item_scan=self.balance.barcode,
                marking_scan=self.OTHER_PRODUCT_KIZ, performed_by=self.user,
            )
        allocation.refresh_from_db()
        self.trace.refresh_from_db()
        self.assertEqual((allocation.qty_reserved, allocation.qty_picked, self.trace.qty), (2, 2, 2))
        self.assertEqual(self.trace.marking_code, "")
        self.assertEqual(FbsOrderStockAllocation.objects.filter(pick_task=task).count(), 1)
        self.assertFalse(FbsPickVerificationProgress.objects.filter(allocation=allocation).exists())

    def test_ozon_grouped_second_unit_cannot_reuse_first_kiz(self):
        _batch, task, allocation = self._prepare_ozon_grouped_verification()
        verify_pick_allocation_unit(
            allocation_id=allocation.id, item_scan=self.balance.barcode,
            marking_scan=self.VALID_KIZ, performed_by=self.user,
        )
        remainder = FbsOrderStockAllocation.objects.get(pick_task=task, traceability__marking_code="")
        with self.assertRaises(FbsPickingError):
            verify_pick_allocation_unit(
                allocation_id=remainder.id, item_scan=self.balance.barcode,
                marking_scan=self.VALID_KIZ, performed_by=self.user,
            )
        self.assertFalse(FbsPickVerificationProgress.objects.filter(allocation=remainder).exists())
        self.assertEqual(FbsPickVerificationProgress.objects.filter(
            allocation__pick_task=task,
        ).aggregate(total=Sum("qty_verified"))["total"], 1)

    def test_marking_retry_screen_displays_actual_error(self):
        reason = "КИЗ уже использован в заказе <123>."
        html = render_to_string("fbs/tsd_pick_verification.html", {
            "verification_base_template": "fbs/tsd_verification_fragment_base.html",
            "item_selected": True,
            "marking_required": True,
            "retry_marking": True,
            "error": reason,
        })
        self.assertIn("КИЗ не принят.", html)
        self.assertIn("КИЗ уже использован в заказе &lt;123&gt;.", html)
        self.assertIn("Повторно отсканируйте только КИЗ", html)

    def test_ozon_kiz_replaces_technical_trace_without_changing_stock_balance(self):
        pick_batch, _task, allocation = self._prepare_ozon_kiz_verification()
        balance_before = (
            self.balance.marking_code,
            self.balance.qty,
            self.balance.available_qty,
            self.balance.reserved_qty,
        )

        progress = verify_pick_allocation_unit(
            allocation_id=allocation.id,
            item_scan=self.balance.barcode,
            marking_scan=self.VALID_KIZ,
            performed_by=self.user,
        )

        allocation.refresh_from_db()
        self.trace.refresh_from_db()
        self.balance.refresh_from_db()
        self.assertEqual(progress.qty_verified, 1)
        self.assertEqual(allocation.balance_id, self.balance.id)
        self.assertEqual(self.trace.marking_code, self.VALID_KIZ)
        self.assertEqual(
            (
                self.balance.marking_code,
                self.balance.qty,
                self.balance.available_qty,
                self.balance.reserved_qty,
            ),
            balance_before,
        )
        transfer = FbsMarketplaceMetadataTransfer.objects.get(
            traceability=self.trace,
            metadata_type=FbsMarketplaceMetadataTransfer.TYPE_MARKING_CODE,
        )
        self.assertEqual(transfer.value, self.VALID_KIZ)
        self.assertTrue(
            FbsPickScanEvent.objects.filter(
                batch=pick_batch,
                allocation=allocation,
                stage=FbsPickScanEvent.STAGE_VERIFY_MARKING,
                result=FbsPickScanEvent.RESULT_SUCCESS,
            ).exists()
        )

    def test_ozon_new_trace_binds_only_controller_fact_before_metadata(self):
        _pick_batch, _task, allocation = self._prepare_ozon_kiz_verification()
        self.trace.delete()

        self.trace = create_allocation_trace(allocation)

        self.balance.refresh_from_db()
        self.assertEqual(self.balance.marking_code, self.OLD_KIZ)
        self.assertEqual(self.trace.marking_code, "")
        balance_before = (
            self.balance.marking_code,
            self.balance.qty,
            self.balance.available_qty,
            self.balance.reserved_qty,
        )

        progress = verify_pick_allocation_unit(
            allocation_id=allocation.id,
            item_scan=self.balance.barcode,
            marking_scan=self.OTHER_PRODUCT_KIZ,
            performed_by=self.user,
        )

        self.trace.refresh_from_db()
        self.balance.refresh_from_db()
        self.assertEqual(progress.qty_verified, 1)
        self.assertEqual(self.trace.marking_code, self.OTHER_PRODUCT_KIZ)
        self.assertEqual(
            (
                self.balance.marking_code,
                self.balance.qty,
                self.balance.available_qty,
                self.balance.reserved_qty,
            ),
            balance_before,
        )
        transfer = FbsMarketplaceMetadataTransfer.objects.get(
            traceability=self.trace,
            metadata_type=FbsMarketplaceMetadataTransfer.TYPE_MARKING_CODE,
        )
        self.assertEqual(transfer.value, self.OTHER_PRODUCT_KIZ)

    def test_ozon_accepts_valid_kiz_when_gtin_differs_from_product_barcode(self):
        pick_batch, _task, allocation = self._prepare_ozon_kiz_verification()
        balance_before = (
            self.balance.marking_code,
            self.balance.qty,
            self.balance.available_qty,
            self.balance.reserved_qty,
        )

        progress = verify_pick_allocation_unit(
            allocation_id=allocation.id,
            item_scan=self.balance.barcode,
            marking_scan=self.OTHER_PRODUCT_KIZ,
            performed_by=self.user,
        )

        self.trace.refresh_from_db()
        self.balance.refresh_from_db()
        self.assertEqual(progress.qty_verified, 1)
        self.assertEqual(self.trace.marking_code, self.OTHER_PRODUCT_KIZ)
        self.assertEqual(
            (
                self.balance.marking_code,
                self.balance.qty,
                self.balance.available_qty,
                self.balance.reserved_qty,
            ),
            balance_before,
        )
        transfer = FbsMarketplaceMetadataTransfer.objects.get(
            traceability=self.trace,
            metadata_type=FbsMarketplaceMetadataTransfer.TYPE_MARKING_CODE,
        )
        self.assertEqual(transfer.value, self.OTHER_PRODUCT_KIZ)
        self.assertTrue(
            FbsPickScanEvent.objects.filter(
                batch=pick_batch,
                allocation=allocation,
                stage=FbsPickScanEvent.STAGE_VERIFY_MARKING,
                result=FbsPickScanEvent.RESULT_SUCCESS,
            ).exists()
        )

    def test_ozon_reassigns_technical_kiz_from_unverified_order_same_wave(self):
        pick_batch, _task, allocation = self._prepare_ozon_kiz_verification()
        other_order, other_allocation, other_trace = (
            self._create_ozon_technical_kiz_owner(
                pick_batch=pick_batch,
                marking_code=self.OTHER_PRODUCT_KIZ,
            )
        )
        balance_before = (
            self.balance.marking_code,
            self.balance.qty,
            self.balance.available_qty,
            self.balance.reserved_qty,
        )

        progress = verify_pick_allocation_unit(
            allocation_id=allocation.id,
            item_scan=self.balance.barcode,
            marking_scan=self.OTHER_PRODUCT_KIZ,
            performed_by=self.user,
        )

        self.trace.refresh_from_db()
        other_trace.refresh_from_db()
        self.balance.refresh_from_db()
        self.assertEqual(progress.qty_verified, 1)
        self.assertEqual(self.trace.marking_code, self.OTHER_PRODUCT_KIZ)
        self.assertEqual(other_trace.marking_code, "")
        self.assertFalse(
            FbsPickVerificationProgress.objects.filter(
                allocation=other_allocation
            ).exists()
        )
        self.assertEqual(
            FbsOrderTraceability.objects.filter(
                marking_code=self.OTHER_PRODUCT_KIZ,
                status=FbsOrderTraceability.STATUS_PICKED,
            ).count(),
            1,
        )
        self.assertEqual(
            (
                self.balance.marking_code,
                self.balance.qty,
                self.balance.available_qty,
                self.balance.reserved_qty,
            ),
            balance_before,
        )
        event = FbsPickScanEvent.objects.get(
            batch=pick_batch,
            allocation=allocation,
            stage=FbsPickScanEvent.STAGE_VERIFY_MARKING,
            result=FbsPickScanEvent.RESULT_SUCCESS,
        )
        self.assertIn(other_order.external_order_id, event.message)

    def test_ozon_rejects_kiz_already_verified_in_other_order(self):
        pick_batch, _task, allocation = self._prepare_ozon_kiz_verification()
        other_order, _other_allocation, other_trace = (
            self._create_ozon_technical_kiz_owner(
                pick_batch=pick_batch,
                marking_code=self.OTHER_PRODUCT_KIZ,
                verified=True,
            )
        )

        with self.assertRaisesMessage(
            FbsPickingError,
            other_order.external_order_id,
        ):
            verify_pick_allocation_unit(
                allocation_id=allocation.id,
                item_scan=self.balance.barcode,
                marking_scan=self.OTHER_PRODUCT_KIZ,
                performed_by=self.user,
            )

        self.trace.refresh_from_db()
        other_trace.refresh_from_db()
        self.assertEqual(self.trace.marking_code, self.OLD_KIZ)
        self.assertEqual(other_trace.marking_code, self.OTHER_PRODUCT_KIZ)
        self.assertFalse(
            FbsPickVerificationProgress.objects.filter(allocation=allocation).exists()
        )

    def test_wb_kiz_is_bound_only_after_product_kiz_and_order_label_scans(self):
        _pick_batch, _task, allocation, label = (
            self._prepare_deferred_kiz_verification()
        )

        self.assertFalse(
            FbsStockBalance.objects.filter(marking_code=self.VALID_KIZ).exists()
        )

        verify_pick_allocation_unit(
            allocation_id=allocation.id,
            item_scan=self.balance.barcode,
            marking_scan=self.VALID_KIZ,
            performed_by=self.user,
        )

        allocation.refresh_from_db()
        self.trace.refresh_from_db()
        self.balance.refresh_from_db()
        self.assertEqual(allocation.balance_id, self.balance.id)
        self.assertEqual(self.trace.marking_code, "")
        self.assertEqual(self.balance.qty, 0)
        self.assertEqual(self.balance.reserved_qty, 0)

        confirm_order_label_scan(
            label_id=label.id,
            label_scan=label.barcode,
            performed_by=self.user,
        )

        allocation.refresh_from_db()
        self.trace.refresh_from_db()
        self.balance.refresh_from_db()
        label.refresh_from_db()
        self.assertEqual(allocation.balance_id, self.balance.id)
        self.assertEqual(self.trace.marking_code, self.VALID_KIZ)
        self.assertEqual(self.balance.qty, 0)
        self.assertEqual(self.balance.available_qty, 0)
        self.assertEqual(self.balance.reserved_qty, 0)
        transfer = FbsMarketplaceMetadataTransfer.objects.get(
            traceability=self.trace,
            metadata_type=FbsMarketplaceMetadataTransfer.TYPE_MARKING_CODE,
        )
        self.assertEqual(transfer.value, self.VALID_KIZ)
        self.assertEqual(label.status, FbsOrderLabel.STATUS_APPLIED)

    def test_rewave_verification_uses_existing_handover_flow(self):
        pick_batch, _task, allocation, _label = (
            self._prepare_deferred_kiz_verification()
        )
        other_batch = FbsHandoverBatch.objects.create(
            profile=self.profile,
            external_supply_id="WB-GI-OTHER-CONTROLLER-FLOW",
            status=FbsHandoverBatch.STATUS_OPEN,
            marketplace_state=FbsHandoverBatch.MARKETPLACE_OPEN,
        )
        other_check_tote = FbsControllerCheckTote.objects.create(
            session=self.controller_session,
            agency=self.agency,
            profile=self.profile,
            handover_batch=other_batch,
            status=FbsControllerCheckTote.STATUS_COMPOSITION,
            item_qty=2,
            labeled_qty=1,
            composition_qty=1,
            opened_by=self.session_controller,
        )
        pick_cart = FbsPickingCart.objects.create(
            barcode="FBS-CART-REWAVE-VERIFY",
            name="Re-wave verification tote",
        )
        pick_batch.cart = pick_cart
        pick_batch.save(update_fields=["cart", "updated_at"])
        pick_context = FbsControllerPickTote.objects.create(
            session=self.controller_session,
            check_tote=other_check_tote,
            pick_batch=pick_batch,
            tote=pick_cart,
            planned_qty=1,
        )

        progress = verify_pick_allocation_unit(
            allocation_id=allocation.id,
            item_scan=self.balance.barcode,
            marking_scan=self.VALID_KIZ,
            performed_by=self.user,
        )

        pick_context.refresh_from_db()
        other_check_tote.refresh_from_db()
        target_check_tote = pick_context.check_tote
        self.assignment.refresh_from_db()
        self.assertEqual(progress.qty_verified, 1)
        self.assertEqual(self.assignment.batch_id, self.batch.id)
        self.assertEqual(target_check_tote.handover_batch_id, self.batch.id)
        self.assertEqual(target_check_tote.item_qty, 1)
        self.assertEqual(other_check_tote.item_qty, 1)
        self.assertEqual(
            FbsHandoverOrderAssignment.objects.filter(order=self.order).count(),
            1,
        )
        self.assertTrue(
            FbsToteMovement.objects.filter(
                pick_batch=pick_batch,
                handover_batch=self.batch,
                details__reason="existing_handover_assignment",
            ).exists()
        )

    def test_pick_tote_uses_existing_handover_before_first_item_scan(self):
        pick_batch, _task, _allocation, _label = (
            self._prepare_deferred_kiz_verification()
        )
        other_batch = FbsHandoverBatch.objects.create(
            profile=self.profile,
            external_supply_id="WB-GI-OTHER-EARLY-FLOW",
            status=FbsHandoverBatch.STATUS_OPEN,
            marketplace_state=FbsHandoverBatch.MARKETPLACE_OPEN,
        )
        other_check_tote = FbsControllerCheckTote.objects.create(
            session=self.controller_session,
            agency=self.agency,
            profile=self.profile,
            handover_batch=other_batch,
            status=FbsControllerCheckTote.STATUS_OPEN,
            item_qty=2,
            opened_by=self.session_controller,
        )
        pick_cart = FbsPickingCart.objects.create(
            barcode="FBS-CART-REWAVE-EARLY",
            name="Re-wave early routing tote",
        )
        pick_batch.cart = pick_cart
        pick_batch.verification_assigned_to = self.session_controller
        pick_batch.verification_started_at = timezone.now()
        pick_batch.save(
            update_fields=[
                "cart",
                "verification_assigned_to",
                "verification_started_at",
                "updated_at",
            ]
        )

        pick_context = attach_pick_tote_to_available_check_tote(
            session_id=self.controller_session.id,
            pick_tote_scan=pick_cart.barcode,
            performed_by=self.session_controller,
        )

        pick_context.refresh_from_db()
        target = pick_context.check_tote
        other_check_tote.refresh_from_db()
        self.assertEqual(target.handover_batch_id, self.batch.id)
        self.assertEqual(target.item_qty, 1)
        self.assertEqual(other_check_tote.item_qty, 2)

    def test_wb_deferred_kiz_defers_problem_tote_history_to_wb(self):
        _pick_batch, _task, allocation, label = (
            self._prepare_deferred_kiz_verification()
        )
        problem_item = FbsProblemToteItem.objects.create(
            session=self.controller_session,
            problem_tote=self.problem_tote,
            order=self.order,
            order_item=self.item,
            scanned_value=self.VALID_KIZ,
            reason="Невалидный КИЗ",
            severity=FbsProblemToteItem.SEVERITY_CRITICAL,
            status=FbsProblemToteItem.STATUS_IN_TOTE,
            reported_by=self.user,
        )

        progress = verify_pick_allocation_unit(
            allocation_id=allocation.id,
            item_scan=self.balance.barcode,
            marking_scan=self.VALID_KIZ,
            performed_by=self.user,
        )

        allocation.refresh_from_db()
        self.trace.refresh_from_db()
        self.assertEqual(progress.qty_verified, 1)
        self.assertEqual(allocation.balance_id, self.balance.id)
        self.assertEqual(self.trace.marking_code, "")

        confirm_order_label_scan(
            label_id=label.id,
            label_scan=label.barcode,
            performed_by=self.user,
        )

        self.trace.refresh_from_db()
        problem_item.refresh_from_db()
        self.assertEqual(self.trace.marking_code, self.VALID_KIZ)
        self.assertEqual(problem_item.status, FbsProblemToteItem.STATUS_IN_TOTE)

    def test_wb_deferred_kiz_ignores_previous_order_and_scan_bindings(self):
        _pick_batch, _task, allocation, label = (
            self._prepare_deferred_kiz_verification()
        )
        other_order = FbsOrder.objects.create(
            profile=self.profile,
            external_order_id="5497933559",
            internal_status=FbsOrder.STATUS_PICKED,
            marketplace_status="confirm",
        )
        other_item = FbsOrderItem.objects.create(
            order=other_order,
            external_line_id="KIZ-PREVIOUS-LINE",
            external_sku=self.sku.sku_code,
            sku=self.sku,
            barcode=self.balance.barcode,
            product_name=self.sku.name,
            quantity=1,
            requirements={"required_meta": ["sgtin"]},
        )
        other_batch = FbsPickBatch.objects.create(
            agency=self.agency,
            workstation=self.workstation,
            status=FbsPickBatch.STATUS_VERIFICATION,
            planned_qty=1,
            picked_qty=1,
            picking_completed_at=timezone.now(),
        )
        other_task = FbsPickTask.objects.create(
            batch=other_batch,
            order=other_order,
            status=FbsPickTask.STATUS_PICKED,
            planned_qty=1,
            picked_qty=1,
        )
        other_allocation = FbsOrderStockAllocation.objects.create(
            order_item=other_item,
            balance=self.balance,
            pick_task=other_task,
            qty_reserved=1,
            qty_picked=1,
            status=FbsOrderStockAllocation.STATUS_PICKED,
        )
        FbsOrderTraceability.objects.create(
            allocation=other_allocation,
            marking_code=self.VALID_KIZ,
            qty=1,
            status=FbsOrderTraceability.STATUS_PICKED,
        )
        FbsPickScanEvent.objects.create(
            batch=other_batch,
            task=other_task,
            allocation=other_allocation,
            stage=FbsPickScanEvent.STAGE_VERIFY_MARKING,
            result=FbsPickScanEvent.RESULT_SUCCESS,
            scan_value=self.VALID_KIZ,
            created_by=self.user,
        )

        verify_pick_allocation_unit(
            allocation_id=allocation.id,
            item_scan=self.balance.barcode,
            marking_scan=self.VALID_KIZ,
            performed_by=self.user,
        )
        confirm_order_label_scan(
            label_id=label.id,
            label_scan=label.barcode,
            performed_by=self.user,
        )

        self.trace.refresh_from_db()
        self.assertEqual(self.trace.marking_code, self.VALID_KIZ)
        self.assertEqual(
            FbsOrderTraceability.objects.filter(
                marking_code=self.VALID_KIZ,
                status=FbsOrderTraceability.STATUS_PICKED,
            ).count(),
            2,
        )

    def test_wb_kiz_does_not_require_stock_or_gtin_mapping(self):
        _pick_batch, _task, allocation, label = (
            self._prepare_deferred_kiz_verification()
        )
        unmapped_kiz = self.OTHER_PRODUCT_KIZ
        self.assertFalse(
            FbsStockBalance.objects.filter(marking_code=unmapped_kiz).exists()
        )

        verify_pick_allocation_unit(
            allocation_id=allocation.id,
            item_scan=self.balance.barcode,
            marking_scan=unmapped_kiz,
            performed_by=self.user,
        )

        self.trace.refresh_from_db()
        self.assertEqual(self.trace.marking_code, "")
        confirm_order_label_scan(
            label_id=label.id,
            label_scan=label.barcode,
            performed_by=self.user,
        )

        allocation.refresh_from_db()
        self.trace.refresh_from_db()
        self.assertEqual(allocation.balance_id, self.balance.id)
        self.assertEqual(self.trace.marking_code, unmapped_kiz)
        self.assertFalse(
            FbsStockBalance.objects.filter(marking_code=unmapped_kiz).exists()
        )

    def test_released_previous_wave_does_not_block_repicked_order_verification(self):
        pick_batch, _task, allocation, _label = (
            self._prepare_deferred_kiz_verification()
        )
        old_batch = FbsPickBatch.objects.create(
            agency=self.agency,
            status=FbsPickBatch.STATUS_CANCELED,
            planned_qty=1,
            picked_qty=0,
            canceled_at=timezone.now(),
        )
        old_task = FbsPickTask.objects.create(
            batch=old_batch,
            order=self.order,
            status=FbsPickTask.STATUS_CANCELED,
            planned_qty=1,
            picked_qty=0,
            canceled_at=timezone.now(),
        )
        FbsOrderStockAllocation.objects.create(
            order_item=self.item,
            balance=self.balance,
            pick_task=old_task,
            qty_reserved=1,
            qty_picked=0,
            status=FbsOrderStockAllocation.STATUS_RELEASED,
            released_at=timezone.now(),
        )

        progress = verify_pick_allocation_unit(
            allocation_id=allocation.id,
            item_scan=self.balance.barcode,
            marking_scan=self.OTHER_PRODUCT_KIZ,
            performed_by=self.user,
        )

        self.assertEqual(progress.qty_verified, 1)
        self.assertTrue(
            FbsPickScanEvent.objects.filter(
                batch=pick_batch,
                allocation=allocation,
                stage=FbsPickScanEvent.STAGE_VERIFY_MARKING,
                result=FbsPickScanEvent.RESULT_SUCCESS,
            ).exists()
        )

    def test_handover_summary_exposes_only_retryable_problem_kiz(self):
        FbsOrderLabel.objects.create(
            order=self.order,
            marketplace=FbsIntegrationProfile.MARKETPLACE_WB,
            external_label_id="WB-RETRY-LABEL-5497933554",
            barcode="WB-RETRY-LABEL-BARCODE",
            status=FbsOrderLabel.STATUS_READY,
        )
        context = _handover_detail_summary(self.batch)

        self.assertEqual(context["handover_kiz_retry_count"], 1)
        self.assertEqual(
            context["handover_kiz_retry_items"][0].order_item_id,
            self.item.id,
        )
        self.assertEqual(
            context["handover_kiz_retry_items"][0].label_number,
            "WB-RETRY-LABEL-5497933554",
        )
        self.assertEqual(
            context["handover_kiz_retry_items"][0].product_barcode,
            self.balance.barcode,
        )
        self.transfer.status = FbsMarketplaceMetadataTransfer.STATUS_CONFIRMED
        self.transfer.last_error = ""
        self.transfer.save(update_fields=["status", "last_error", "updated_at"])

        refreshed_context = _handover_detail_summary(self.batch)

        self.assertEqual(refreshed_context["handover_kiz_retry_count"], 0)

    def test_optional_scanned_kiz_still_waits_for_wb_confirmation(self):
        self.transfer.is_required = False
        self.transfer.status = FbsMarketplaceMetadataTransfer.STATUS_SENT
        self.transfer.external_status = "request_sent"
        self.transfer.last_error = ""
        self.transfer.save(
            update_fields=[
                "is_required",
                "status",
                "external_status",
                "last_error",
                "updated_at",
            ]
        )

        readiness = handover_composition_readiness(self.batch)

        self.assertTrue(
            any("КИЗ не подтвержден" in reason for reason in readiness.reasons)
        )
        context = _handover_detail_summary(self.batch)
        kiz_check = (
            context["handover_assignments"][0]
            .order.ui_items[0]
            .metadata_checks[0]
        )
        self.assertTrue(kiz_check.required)
        self.assertEqual(kiz_check.state, "in_progress")

    def test_quarantine_is_blocked_for_nonfinal_internal_kiz_error(self):
        self.transfer.external_status = "command_failed"
        self.transfer.last_error = "Временная внутренняя ошибка обмена."
        self.transfer.save(
            update_fields=["external_status", "last_error", "updated_at"]
        )
        self._prepare_invalid_kiz_reroute()

        with self.assertRaisesMessage(
            FbsPickingError,
            "только после окончательного отказа WB",
        ):
            request_invalid_kiz_reroute(
                handover_batch_id=self.batch.id,
                order_id=self.order.id,
                route="quarantine",
                problem_tote_scan=self.problem_tote.barcode,
                requested_by=self.user,
            )

        self.assertFalse(FbsProblemToteItem.objects.exists())

    def test_retry_all_redirect_keeps_queue_mode(self):
        request = RequestFactory().post(
            f"/fbs/handover/{self.batch.id}/",
            {
                "action": "retry_kiz",
                "order_item_id": str(self.item.id),
                "marking_scan": self.VALID_KIZ,
                "kiz_rescan_all": "1",
            },
        )
        request.user = self.user
        command = SimpleNamespace(order=self.order)
        detail_url = f"/fbs/handover/{self.batch.id}/"

        with patch(
            "fbs.tsd_views.retry_wb_marking_code",
            return_value=command,
        ), patch("fbs.tsd_views.reverse", return_value=detail_url):
            response = tsd_handover_detail(request, batch_id=self.batch.id)

        self.assertEqual(response.status_code, 302)
        query = parse_qs(urlparse(response["Location"]).query)
        self.assertEqual(query["kiz_requeued"], [self.order.external_order_id])
        self.assertEqual(query["kiz_rescan_all"], ["1"])

    def test_invalid_kiz_rejects_wrong_tote_without_partial_changes(self):
        self._prepare_invalid_kiz_reroute()
        wrong_tote = FbsPickingCart.objects.create(
            barcode="FBS-CART-900003",
            name="Wrong problem tote",
        )

        with self.assertRaisesMessage(FbsPickingError, "другая тара"):
            request_invalid_kiz_reroute(
                handover_batch_id=self.batch.id,
                order_id=self.order.id,
                route="rewave",
                problem_tote_scan=wrong_tote.barcode,
                requested_by=self.user,
            )

        self.assignment.refresh_from_db()
        self.assertEqual(self.assignment.batch_id, self.batch.id)
        self.assertEqual(
            self.assignment.status,
            FbsHandoverOrderAssignment.STATUS_CONFIRMED,
        )
        self.assertEqual(FbsHandoverBatch.objects.count(), 1)
        self.assertFalse(FbsProblemToteItem.objects.exists())
        self.assertFalse(
            FbsMarketplaceCommand.objects.filter(
                command_type=WB_ADD_ORDER_TO_HANDOVER
            ).exists()
        )

    def test_invalid_kiz_finalization_requires_physical_tote_record(self):
        _pick_batch, old_task, _allocation, _link = (
            self._prepare_invalid_kiz_reroute()
        )
        reroute = request_invalid_kiz_reroute(
            handover_batch_id=self.batch.id,
            order_id=self.order.id,
            route="quarantine",
            problem_tote_scan=self.problem_tote.barcode,
            requested_by=self.user,
        )
        FbsProblemToteItem.objects.filter(order=self.order).delete()
        self.assignment.refresh_from_db()
        self.assignment.status = FbsHandoverOrderAssignment.STATUS_CONFIRMED
        self.assignment.save(update_fields=["status", "updated_at"])

        with self.assertRaisesMessage(
            FbsPickingError,
            "не зарегистрирована в таре",
        ):
            finalize_invalid_kiz_reroute(assignment_id=self.assignment.id)

        old_task.refresh_from_db()
        self.assertEqual(old_task.status, FbsPickTask.STATUS_PICKED)
        self.assertEqual(self.assignment.batch_id, reroute.target_batch_id)

    def test_invalid_kiz_moves_without_wb_cancel_and_creates_new_wave(self):
        old_pick_batch, old_task, old_allocation, old_link = (
            self._prepare_invalid_kiz_reroute()
        )
        alternate_balance = FbsStockBalance.objects.create(
            agency=self.agency,
            box=self.balance.box,
            sku_ref=self.sku,
            identity_key="3" * 64,
            sku_code=self.sku.sku_code,
            name=self.sku.name,
            barcode=self.balance.barcode,
            marking_code=self.VALID_KIZ,
            qty=1,
            available_qty=1,
        )

        reroute = request_invalid_kiz_reroute(
            handover_batch_id=self.batch.id,
            order_id=self.order.id,
            route="rewave",
            problem_tote_scan=self.problem_tote.barcode,
            requested_by=self.user,
        )
        duplicate = request_invalid_kiz_reroute(
            handover_batch_id=self.batch.id,
            order_id=self.order.id,
            route="rewave",
            problem_tote_scan=self.problem_tote.barcode,
            requested_by=self.user,
        )

        self.assertEqual(duplicate.target_batch_id, reroute.target_batch_id)
        self.assertEqual(FbsHandoverBatch.objects.count(), 2)
        self.assignment.refresh_from_db()
        old_link.refresh_from_db()
        self.assertEqual(self.assignment.batch_id, reroute.target_batch_id)
        self.assertEqual(
            self.assignment.status,
            FbsHandoverOrderAssignment.STATUS_PENDING,
        )
        self.assertEqual(old_link.status, FbsHandoverOrder.STATUS_RETURN_PENDING)
        readiness = handover_composition_readiness(self.batch)
        self.assertFalse(readiness.ready)
        self.assertTrue(
            any("перенос" in reason.casefold() for reason in readiness.reasons)
        )
        self.assertFalse(
            FbsMarketplaceCommand.objects.filter(
                command_type=WB_CANCEL_ORDER
            ).exists()
        )

        target_batch = FbsHandoverBatch.objects.get(pk=reroute.target_batch_id)
        self._complete_reroute_supply_move(
            target_batch=target_batch,
            supply_id="WB-GI-KIZ-REWAVE",
        )

        problem_commands = list(
            FbsMarketplaceCommand.objects.filter(
                status__in=(
                    FbsMarketplaceCommand.STATUS_FAILED,
                    FbsMarketplaceCommand.STATUS_CONFLICT,
                    FbsMarketplaceCommand.STATUS_RETRY,
                )
            ).values("command_type", "status", "error")
        )
        self.assertEqual(problem_commands, [])
        self.assignment.refresh_from_db()
        self.order.refresh_from_db()
        self.transfer.refresh_from_db()
        old_link.refresh_from_db()
        old_task.refresh_from_db()
        old_pick_batch.refresh_from_db()
        alternate_balance.refresh_from_db()
        old_allocation.refresh_from_db()
        self.assertEqual(
            self.assignment.status,
            FbsHandoverOrderAssignment.STATUS_CONFIRMED,
        )
        self.assertEqual(old_link.status, FbsHandoverOrder.STATUS_EXCLUDED)
        self.assertEqual(old_task.status, FbsPickTask.STATUS_EXCEPTION)
        self.assertEqual(old_pick_batch.status, FbsPickBatch.STATUS_DONE)
        self.assertEqual(
            self.transfer.status,
            FbsMarketplaceMetadataTransfer.STATUS_CANCELED,
        )
        self.assertEqual(self.order.internal_status, FbsOrder.STATUS_QUEUED_FOR_PICK)
        new_task = self.order.pick_tasks.get(status=FbsPickTask.STATUS_QUEUED)
        self.assertNotEqual(new_task.batch_id, old_pick_batch.id)
        self.assertEqual(new_task.planned_qty, 1)
        self.assertEqual(alternate_balance.available_qty, 0)
        self.assertEqual(alternate_balance.reserved_qty, 1)
        self.assertEqual(old_allocation.status, FbsOrderStockAllocation.STATUS_PICKED)
        problem_item = FbsProblemToteItem.objects.get(
            order=self.order,
            order_item=self.item,
            status=FbsProblemToteItem.STATUS_IN_TOTE,
        )
        self.assertEqual(problem_item.problem_tote, self.problem_tote)
        self.assertEqual(problem_item.scanned_value, self.OLD_KIZ)
        self.assertEqual(
            problem_item.severity,
            FbsProblemToteItem.SEVERITY_CRITICAL,
        )
        movement = FbsToteMovement.objects.get(
            handover_batch=self.batch,
            details__problem_item_id=problem_item.id,
        )
        self.assertEqual(movement.target_code, self.problem_tote.barcode)
        self.assertEqual(movement.details["allocation_id"], old_allocation.id)
        self.assertEqual(movement.details["route"], "rewave")
        self.assertTrue(movement.details["physically_confirmed"])
        self.assertTrue(
            FbsPickException.objects.filter(
                task=old_task,
                allocation=old_allocation,
                status=FbsPickException.STATUS_OPEN,
            ).exists()
        )
        self.assertFalse(
            FbsMarketplaceCommand.objects.filter(
                command_type=WB_CANCEL_ORDER
            ).exists()
        )

    def test_confirmed_reroute_releases_only_source_shipment(self):
        old_pick_batch, _old_task, _old_allocation, old_link = (
            self._prepare_invalid_kiz_reroute()
        )
        alternate_balance = FbsStockBalance.objects.create(
            agency=self.agency,
            box=self.balance.box,
            sku_ref=self.sku,
            identity_key="4" * 64,
            sku_code=self.sku.sku_code,
            name=self.sku.name,
            barcode=self.balance.barcode,
            marking_code=self.VALID_KIZ,
            qty=1,
            available_qty=1,
        )
        good_order = FbsOrder.objects.create(
            profile=self.profile,
            external_order_id="5497933555",
            internal_status=FbsOrder.STATUS_READY_FOR_HANDOVER,
            marketplace_status="confirm",
        )
        FbsOrderItem.objects.create(
            order=good_order,
            external_line_id="GOOD-LINE-1",
            external_sku=self.sku.sku_code,
            sku=self.sku,
            barcode=self.balance.barcode,
            product_name=self.sku.name,
            quantity=1,
        )
        FbsHandoverOrderAssignment.objects.create(
            batch=self.batch,
            order=good_order,
            status=FbsHandoverOrderAssignment.STATUS_CONFIRMED,
        )
        problem_label = FbsOrderLabel.objects.create(
            order=self.order,
            marketplace=FbsIntegrationProfile.MARKETPLACE_WB,
            barcode="WB-PROBLEM-LABEL",
            status=FbsOrderLabel.STATUS_APPLIED,
            applied_by=self.user,
            applied_at=timezone.now(),
        )
        good_label = FbsOrderLabel.objects.create(
            order=good_order,
            marketplace=FbsIntegrationProfile.MARKETPLACE_WB,
            barcode="WB-GOOD-LABEL",
            status=FbsOrderLabel.STATUS_APPLIED,
            applied_by=self.user,
            applied_at=timezone.now(),
        )
        good_link = FbsHandoverOrder.objects.create(
            box=old_link.box,
            order=good_order,
            added_by=self.user,
            verified_label=good_label,
            verified_by=self.user,
            verified_at=timezone.now(),
        )
        check_tote = FbsControllerCheckTote.objects.create(
            session=self.controller_session,
            agency=self.agency,
            profile=self.profile,
            handover_batch=self.batch,
            status=FbsControllerCheckTote.STATUS_COMPOSITION,
            item_qty=2,
            labeled_qty=2,
            composition_qty=2,
            opened_by=self.session_controller,
        )
        pick_cart = FbsPickingCart.objects.create(
            barcode="FBS-CART-900004",
            name="Closed source pick tote",
        )
        pick_tote = FbsControllerPickTote.objects.create(
            session=self.controller_session,
            check_tote=check_tote,
            pick_batch=old_pick_batch,
            tote=pick_cart,
            status=FbsControllerPickTote.STATUS_CLOSED,
            planned_qty=2,
            processed_qty=2,
        )
        problem_tote_order = FbsControllerToteOrder.objects.create(
            check_tote=check_tote,
            pick_tote=pick_tote,
            order=self.order,
            label=problem_label,
            transport_box=old_link.box,
            status=FbsControllerToteOrder.STATUS_PACKED,
            units=1,
            label_confirmed_by=self.user,
        )
        good_tote_order = FbsControllerToteOrder.objects.create(
            check_tote=check_tote,
            pick_tote=pick_tote,
            order=good_order,
            label=good_label,
            transport_box=old_link.box,
            status=FbsControllerToteOrder.STATUS_PACKED,
            units=1,
            label_confirmed_by=self.user,
        )

        reroute = request_invalid_kiz_reroute(
            handover_batch_id=self.batch.id,
            order_id=self.order.id,
            route="rewave",
            problem_tote_scan=self.problem_tote.barcode,
            requested_by=self.user,
        )
        target_batch = FbsHandoverBatch.objects.get(pk=reroute.target_batch_id)
        self._complete_reroute_supply_move(
            target_batch=target_batch,
            supply_id="WB-GI-KIZ-SEPARATE",
        )

        self.assignment.refresh_from_db()
        self.batch.refresh_from_db()
        check_tote.refresh_from_db()
        problem_tote_order.refresh_from_db()
        good_tote_order.refresh_from_db()
        good_link.box.refresh_from_db()
        self.assertEqual(self.assignment.batch_id, target_batch.id)
        self.assertEqual(
            problem_tote_order.status,
            FbsControllerToteOrder.STATUS_REMOVED,
        )
        self.assertEqual(
            good_tote_order.status,
            FbsControllerToteOrder.STATUS_PACKED,
        )
        self.assertEqual(check_tote.status, FbsControllerCheckTote.STATUS_CLOSED)
        self.assertEqual(self.batch.status, FbsHandoverBatch.STATUS_READY)
        self.assertEqual(good_link.box.status, FbsHandoverBox.STATUS_CLOSED)
        self.assertTrue(handover_composition_readiness(self.batch).ready)
        self.assertTrue(
            self.order.pick_tasks.filter(status=FbsPickTask.STATUS_QUEUED).exists()
        )
        alternate_balance.refresh_from_db()
        self.assertEqual(alternate_balance.reserved_qty, 1)

    def test_invalid_kiz_rewave_uses_another_unit_from_same_unmarked_balance(self):
        _old_pick_batch, _old_task, old_allocation, _old_link = (
            self._prepare_invalid_kiz_reroute()
        )
        self.balance.marking_code = ""
        self.balance.qty = 3
        self.balance.available_qty = 2
        self.balance.reserved_qty = 1
        self.balance.save(
            update_fields=[
                "marking_code",
                "qty",
                "available_qty",
                "reserved_qty",
                "updated_at",
            ]
        )

        reroute = request_invalid_kiz_reroute(
            handover_batch_id=self.batch.id,
            order_id=self.order.id,
            route="rewave",
            problem_tote_scan=self.problem_tote.barcode,
            requested_by=self.user,
        )
        target_batch = FbsHandoverBatch.objects.get(pk=reroute.target_batch_id)
        self._complete_reroute_supply_move(
            target_batch=target_batch,
            supply_id="WB-GI-KIZ-SAME-AGGREGATE",
        )

        self.assignment.refresh_from_db()
        self.order.refresh_from_db()
        self.balance.refresh_from_db()
        replacement = (
            FbsOrderStockAllocation.objects.filter(
                order_item=self.item,
                status=FbsOrderStockAllocation.STATUS_RESERVED,
            )
            .exclude(pk=old_allocation.pk)
            .get()
        )
        self.assertEqual(
            self.assignment.status,
            FbsHandoverOrderAssignment.STATUS_CONFIRMED,
        )
        self.assertEqual(self.order.internal_status, FbsOrder.STATUS_QUEUED_FOR_PICK)
        self.assertEqual(replacement.balance_id, self.balance.id)
        self.assertEqual(self.balance.available_qty, 1)
        self.assertEqual(self.balance.reserved_qty, 2)

    def test_invalid_kiz_quarantine_notifies_storekeeper_without_new_wave(self):
        _old_pick_batch, old_task, old_allocation, old_link = (
            self._prepare_invalid_kiz_reroute(
                create_handover_link=False,
                order_status=FbsOrder.STATUS_READY_FOR_HANDOVER,
            )
        )
        reroute = request_invalid_kiz_reroute(
            handover_batch_id=self.batch.id,
            order_id=self.order.id,
            route="quarantine",
            problem_tote_scan=self.problem_tote.barcode,
            requested_by=self.user,
        )
        self.order.refresh_from_db()
        self.assertEqual(self.order.internal_status, FbsOrder.STATUS_PICKED)
        self.assertEqual(self.order.hold_reason, "handover_kiz_move_pending")
        self.assertIsNone(old_link)
        readiness = handover_composition_readiness(self.batch)
        self.assertFalse(readiness.ready)
        self.assertTrue(
            any("перенос" in reason.casefold() for reason in readiness.reasons)
        )
        target_batch = FbsHandoverBatch.objects.get(pk=reroute.target_batch_id)
        self._complete_reroute_supply_move(
            target_batch=target_batch,
            supply_id="WB-GI-KIZ-QUARANTINE",
        )

        self.order.refresh_from_db()
        self.assertEqual(self.order.internal_status, FbsOrder.STATUS_EXCEPTION)
        self.assertEqual(self.order.hold_reason, "handover_kiz_quarantine")
        self.assertIn("уведомление клиенту", self.order.problem_reason)
        self.assertFalse(FbsHandoverOrder.objects.filter(order=self.order).exists())
        self.assertFalse(
            self.order.pick_tasks.filter(
                status__in=(FbsPickTask.STATUS_QUEUED, FbsPickTask.STATUS_IN_PROGRESS)
            ).exists()
        )
        self.assertTrue(
            FbsProblemToteItem.objects.filter(
                order=self.order,
                problem_tote=self.problem_tote,
                scanned_value=self.OLD_KIZ,
                status=FbsProblemToteItem.STATUS_IN_TOTE,
            ).exists()
        )
        self.assertTrue(
            FbsPickException.objects.filter(
                task=old_task,
                allocation=old_allocation,
                status=FbsPickException.STATUS_OPEN,
            ).exists()
        )
        self.assertFalse(
            FbsMarketplaceCommand.objects.filter(
                command_type=WB_CANCEL_ORDER
            ).exists()
        )

    def test_problem_kiz_is_replaced_and_one_new_outbox_command_is_created(self):
        command = retry_wb_marking_code(
            batch_id=self.batch.id,
            order_item_id=self.item.id,
            marking_scan=self.VALID_KIZ,
            requested_by=self.user,
        )

        self.trace.refresh_from_db()
        self.transfer.refresh_from_db()
        self.assertEqual(self.trace.marking_code, self.VALID_KIZ)
        self.assertEqual(self.transfer.value, self.VALID_KIZ)
        self.balance.refresh_from_db()
        self.assertEqual(self.balance.marking_code, self.OLD_KIZ)
        self.assertEqual(
            self.transfer.status,
            FbsMarketplaceMetadataTransfer.STATUS_QUEUED,
        )
        self.assertEqual(command.command_type, WB_SET_ORDER_SGTINS)
        self.assertEqual(command.payload["body"], {"sgtins": [self.VALID_KIZ]})
        self.assertEqual(command.request_source, FbsMarketplaceCommand.SOURCE_METADATA_CONTROL)
        audit = OrderAuditEntry.objects.get()
        self.assertTrue(audit.payload["accepted_physical_fact"])
        self.assertTrue(audit.payload["balance_marking_unchanged"])

    def test_deliver_metadata_error_exposes_order_for_rescan_without_supply_retry(self):
        self.batch.status = FbsHandoverBatch.STATUS_READY
        self.batch.marketplace_state = FbsHandoverBatch.MARKETPLACE_DELIVERY_PENDING
        self.batch.save(update_fields=["status", "marketplace_state", "updated_at"])
        self.transfer.status = FbsMarketplaceMetadataTransfer.STATUS_CONFIRMED
        self.transfer.external_status = "sgtin"
        self.transfer.last_error = ""
        self.transfer.save(
            update_fields=["status", "external_status", "last_error", "updated_at"]
        )
        delivery = FbsMarketplaceCommand.objects.create(
            profile=self.profile,
            handover_batch=self.batch,
            command_type=WB_DELIVER_HANDOVER,
            http_method=FbsMarketplaceCommand.METHOD_PATCH,
            endpoint="/api/v3/supplies/WB-GI-TEST/deliver",
            idempotency_key="test-deliver-meta-validation",
            payload={},
            payload_hash="deliver-meta-validation",
        )

        result = process_marketplace_command(
            command_id=delivery.id,
            transport=_StaticTransport(
                status=409,
                payload={
                    "code": "MetaValidationFail",
                    "data": {
                        "orders": [
                            {
                                "id": int(self.order.external_order_id),
                                "metaDetails": [
                                    {
                                        "key": "sgtin",
                                        "value": self.OLD_KIZ,
                                        "decision": "sgtinNoGS",
                                    }
                                ],
                            }
                        ]
                    },
                    "message": "Fix them to dispatch items",
                },
            ),
        )

        self.transfer.refresh_from_db()
        self.batch.refresh_from_db()
        self.assertEqual(result.status, FbsMarketplaceCommand.STATUS_CONFLICT)
        self.assertIsNone(result.next_attempt_at)
        self.assertEqual(
            self.transfer.status,
            FbsMarketplaceMetadataTransfer.STATUS_CONFLICT,
        )
        self.assertEqual(self.transfer.external_status, "wb_rejected:sgtinNoGS")
        self.assertIn("разделители GS", self.transfer.last_error)
        self.assertEqual(
            self.batch.marketplace_state,
            FbsHandoverBatch.MARKETPLACE_ERROR,
        )
        summary = _handover_detail_summary(self.batch, compact_controller=True)
        self.assertEqual(summary["handover_kiz_retry_count"], 1)
        self.assertEqual(
            summary["handover_kiz_retry_items"][0].order_number,
            self.order.external_order_id,
        )

    def test_ready_handover_rescan_resumes_delivery_after_wb_confirmation(self):
        self.batch.status = FbsHandoverBatch.STATUS_READY
        self.batch.marketplace_state = FbsHandoverBatch.MARKETPLACE_ERROR
        self.batch.save(update_fields=["status", "marketplace_state", "updated_at"])
        delivery = FbsMarketplaceCommand.objects.create(
            profile=self.profile,
            handover_batch=self.batch,
            command_type=WB_DELIVER_HANDOVER,
            http_method=FbsMarketplaceCommand.METHOD_PATCH,
            endpoint="/api/v3/supplies/WB-GI-TEST/deliver",
            idempotency_key="test-resume-deliver-after-kiz",
            payload={},
            payload_hash="resume-deliver-after-kiz",
            status=FbsMarketplaceCommand.STATUS_CONFLICT,
            http_status=409,
            response_payload={
                "code": "MetaValidationFail",
                "data": {
                    "orders": [
                        {
                            "id": int(self.order.external_order_id),
                            "metaDetails": [
                                {
                                    "key": "sgtin",
                                    "value": self.OLD_KIZ,
                                    "decision": "sgtinNoGS",
                                }
                            ],
                        }
                    ]
                },
            },
        )

        retry_wb_marking_code(
            batch_id=self.batch.id,
            order_item_id=self.item.id,
            marking_scan=self.VALID_KIZ,
            requested_by=self.user,
        )
        self.transfer.refresh_from_db()
        _set_transfer_state(
            [self.transfer],
            status=FbsMarketplaceMetadataTransfer.STATUS_CONFIRMED,
            external_status="sgtin",
        )

        delivery.refresh_from_db()
        self.batch.refresh_from_db()
        self.assertEqual(delivery.status, FbsMarketplaceCommand.STATUS_RETRY)
        self.assertIsNotNone(delivery.next_attempt_at)
        self.assertEqual(
            self.batch.marketplace_state,
            FbsHandoverBatch.MARKETPLACE_DELIVERY_PENDING,
        )

    def test_retry_restores_lost_gs_separators_before_wb_outbox(self):
        no_gs = "010466040680002221SER12391ABCD92" + "X" * 44
        expected = "010466040680002221SER123\x1d91ABCD\x1d92" + "X" * 44

        command = retry_wb_marking_code(
            batch_id=self.batch.id,
            order_item_id=self.item.id,
            marking_scan=no_gs,
            requested_by=self.user,
        )

        self.trace.refresh_from_db()
        self.transfer.refresh_from_db()
        self.assertEqual(self.trace.marking_code, expected)
        self.assertEqual(self.transfer.value, expected)
        self.assertEqual(command.payload["body"], {"sgtins": [expected]})
        self.assertEqual(command.payload["body"]["sgtins"][0].count("\x1d"), 2)

    def test_separator_only_retry_keeps_wb_kiz_when_internal_barcode_differs(self):
        no_gs = "010461046911078121SER12391ABCD92" + "X" * 44
        expected = "010461046911078121SER123\x1d91ABCD\x1d92" + "X" * 44
        self.trace.marking_code = no_gs
        self.trace.save(update_fields=["marking_code", "updated_at"])
        self.transfer.value = no_gs
        self.transfer.save(update_fields=["value", "updated_at"])

        command = retry_wb_marking_code(
            batch_id=self.batch.id,
            order_item_id=self.item.id,
            marking_scan=no_gs,
            requested_by=self.user,
        )

        self.trace.refresh_from_db()
        self.transfer.refresh_from_db()
        self.assertEqual(self.trace.marking_code, expected)
        self.assertEqual(self.transfer.value, expected)
        self.assertEqual(command.payload["body"], {"sgtins": [expected]})
        audit = OrderAuditEntry.objects.get()
        self.assertTrue(audit.payload["gs_separator_only_repair"])

    def test_same_kiz_after_terminal_conflict_creates_a_fresh_command(self):
        first = retry_wb_marking_code(
            batch_id=self.batch.id,
            order_item_id=self.item.id,
            marking_scan=self.VALID_KIZ,
            requested_by=self.user,
        )
        first.status = FbsMarketplaceCommand.STATUS_CONFLICT
        first.error = "WB rejected marking"
        first.save(update_fields=["status", "error", "updated_at"])
        self.transfer.status = FbsMarketplaceMetadataTransfer.STATUS_CONFLICT
        self.transfer.last_error = first.error
        self.transfer.save(update_fields=["status", "last_error", "updated_at"])

        second = retry_wb_marking_code(
            batch_id=self.batch.id,
            order_item_id=self.item.id,
            marking_scan=self.VALID_KIZ,
            requested_by=self.user,
        )

        self.assertNotEqual(first.id, second.id)
        self.assertEqual(FbsMarketplaceCommand.objects.count(), 2)
        self.assertEqual(second.status, FbsMarketplaceCommand.STATUS_PENDING)

    def test_wb_metadata_rejection_is_explained_in_russian(self):
        self.assertEqual(
            _wb_metadata_rejection_error(
                key="sgtin",
                raw_decision="sgtinIntroduced",
                errors=[],
            ),
            "WB отклонил КИЗ: код уже введен в оборот и отклонен площадкой "
            "(sgtinIntroduced).",
        )

    def test_matching_sgtin_introduced_without_errors_is_confirmed(self):
        self.transfer.value = self.VALID_KIZ
        self.transfer.status = FbsMarketplaceMetadataTransfer.STATUS_SENT
        self.transfer.save(update_fields=["value", "status", "updated_at"])
        check_tote = FbsControllerCheckTote.objects.create(
            session=self.controller_session,
            agency=self.agency,
            profile=self.profile,
            handover_batch=self.batch,
            status=FbsControllerCheckTote.STATUS_WAITING_KIZ,
            item_qty=1,
            labeled_qty=1,
            opened_by=self.session_controller,
        )
        pick_batch = FbsPickBatch.objects.create(
            agency=self.agency,
            workstation=self.workstation,
            status=FbsPickBatch.STATUS_VERIFICATION,
            planned_qty=1,
            picked_qty=1,
            picking_completed_at=timezone.now(),
        )
        pick_cart = FbsPickingCart.objects.create(
            barcode="FBS-CART-900003",
            name="Confirmed metadata pick tote",
        )
        pick_tote = FbsControllerPickTote.objects.create(
            session=self.controller_session,
            check_tote=check_tote,
            pick_batch=pick_batch,
            tote=pick_cart,
            status=FbsControllerPickTote.STATUS_CLOSED,
            planned_qty=1,
            processed_qty=1,
        )
        label = FbsOrderLabel.objects.create(
            order=self.order,
            marketplace=FbsIntegrationProfile.MARKETPLACE_WB,
            external_label_id="WB-METADATA-AUTOREFRESH-LABEL",
            barcode="WB-METADATA-AUTOREFRESH-BARCODE",
            status=FbsOrderLabel.STATUS_APPLIED,
            applied_by=self.user,
            applied_at=timezone.now(),
        )
        FbsControllerToteOrder.objects.create(
            check_tote=check_tote,
            pick_tote=pick_tote,
            order=self.order,
            label=label,
            status=FbsControllerToteOrder.STATUS_LABELED,
            units=1,
            label_confirmed_by=self.user,
        )
        command = FbsMarketplaceCommand.objects.create(
            profile=self.profile,
            order=self.order,
            command_type=WB_READ_ORDER_METADATA,
            http_method=FbsMarketplaceCommand.METHOD_POST,
            endpoint="/api/marketplace/v3/orders/meta",
            idempotency_key="test-sgtin-introduced-matching",
            payload={},
            payload_hash="matching",
            status=FbsMarketplaceCommand.STATUS_SENT,
        )

        _apply_wb_metadata_readback(
            command,
            {
                "meta_details": {
                    "sgtin": {
                        "value": self.VALID_KIZ,
                        "errors": [],
                        "decision": "sgtinIntroduced",
                    }
                }
            },
            MarketplaceHttpResponse(
                status_code=200,
                headers={"Content-Type": "application/json"},
                content=b"",
                json_payload={},
            ),
        )

        self.transfer.refresh_from_db()
        command.refresh_from_db()
        self.assertEqual(
            self.transfer.status,
            FbsMarketplaceMetadataTransfer.STATUS_CONFIRMED,
        )
        self.assertEqual(self.transfer.external_status, "sgtinintroduced")
        self.assertEqual(self.transfer.last_error, "")
        self.assertEqual(command.status, FbsMarketplaceCommand.STATUS_CONFIRMED)
        check_tote.refresh_from_db()
        self.assertEqual(check_tote.status, FbsControllerCheckTote.STATUS_READY)

    def test_wb_optional_without_echoed_kiz_or_errors_is_confirmed(self):
        self.transfer.value = self.VALID_KIZ
        self.transfer.status = FbsMarketplaceMetadataTransfer.STATUS_SENT
        self.transfer.external_status = "request_sent"
        self.transfer.last_error = ""
        self.transfer.save(
            update_fields=[
                "value",
                "status",
                "external_status",
                "last_error",
                "updated_at",
            ]
        )
        command = FbsMarketplaceCommand.objects.create(
            profile=self.profile,
            order=self.order,
            command_type=WB_READ_ORDER_METADATA,
            http_method=FbsMarketplaceCommand.METHOD_POST,
            endpoint="/api/marketplace/v3/orders/meta",
            idempotency_key="test-optional-without-echoed-kiz",
            payload={},
            payload_hash="optional-without-echo",
            status=FbsMarketplaceCommand.STATUS_SENT,
        )

        _apply_wb_metadata_readback(
            command,
            {
                "meta_details": {
                    "sgtin": {
                        "value": None,
                        "errors": [],
                        "decision": "optional",
                    }
                }
            },
            MarketplaceHttpResponse(
                status_code=200,
                headers={"Content-Type": "application/json"},
                content=b"",
                json_payload={},
            ),
        )

        self.transfer.refresh_from_db()
        command.refresh_from_db()
        self.assertEqual(
            self.transfer.status,
            FbsMarketplaceMetadataTransfer.STATUS_CONFIRMED,
        )
        self.assertEqual(self.transfer.external_status, "optional")
        self.assertEqual(self.transfer.last_error, "")
        self.assertFalse(is_final_wb_marking_rejection(self.transfer))
        self.assertEqual(command.status, FbsMarketplaceCommand.STATUS_CONFIRMED)

    def test_wb_optional_with_errors_remains_conflict(self):
        self.transfer.value = self.VALID_KIZ
        self.transfer.status = FbsMarketplaceMetadataTransfer.STATUS_SENT
        self.transfer.save(update_fields=["value", "status", "updated_at"])
        command = FbsMarketplaceCommand.objects.create(
            profile=self.profile,
            order=self.order,
            command_type=WB_READ_ORDER_METADATA,
            http_method=FbsMarketplaceCommand.METHOD_POST,
            endpoint="/api/marketplace/v3/orders/meta",
            idempotency_key="test-optional-with-errors",
            payload={},
            payload_hash="optional-with-errors",
            status=FbsMarketplaceCommand.STATUS_SENT,
        )

        _apply_wb_metadata_readback(
            command,
            {
                "meta_details": {
                    "sgtin": {
                        "value": None,
                        "errors": ["validationError"],
                        "decision": "optional",
                    }
                }
            },
            MarketplaceHttpResponse(
                status_code=200,
                headers={"Content-Type": "application/json"},
                content=b"",
                json_payload={},
            ),
        )

        self.transfer.refresh_from_db()
        command.refresh_from_db()
        self.assertEqual(
            self.transfer.status,
            FbsMarketplaceMetadataTransfer.STATUS_CONFLICT,
        )
        self.assertEqual(self.transfer.external_status, "wb_rejected:optional")
        self.assertTrue(is_final_wb_marking_rejection(self.transfer))
        self.assertEqual(command.status, FbsMarketplaceCommand.STATUS_CONFLICT)

    @override_settings(FBS_MARKETPLACE_MAX_ATTEMPTS=1)
    def test_wb_pending_kiz_decision_retries_beyond_attempt_limit(self):
        self.transfer.value = self.VALID_KIZ
        self.transfer.status = FbsMarketplaceMetadataTransfer.STATUS_SENT
        self.transfer.external_status = "request_sent"
        self.transfer.save(
            update_fields=["value", "status", "external_status", "updated_at"]
        )
        command = FbsMarketplaceCommand.objects.create(
            profile=self.profile,
            order=self.order,
            command_type=WB_READ_ORDER_METADATA,
            http_method=FbsMarketplaceCommand.METHOD_POST,
            endpoint="/api/marketplace/v3/orders/meta",
            idempotency_key="test-pending-after-max-attempts",
            payload={},
            payload_hash="pending-after-max",
            status=FbsMarketplaceCommand.STATUS_SENT,
            attempt_count=10,
        )

        _apply_wb_metadata_readback(
            command,
            {
                "meta_details": {
                    "sgtin": {
                        "value": self.VALID_KIZ,
                        "errors": [],
                        "decision": "pending",
                    }
                }
            },
            MarketplaceHttpResponse(
                status_code=200,
                headers={"Content-Type": "application/json"},
                content=b"",
                json_payload={},
            ),
        )

        self.transfer.refresh_from_db()
        command.refresh_from_db()
        self.assertEqual(command.status, FbsMarketplaceCommand.STATUS_RETRY)
        self.assertEqual(
            self.transfer.status,
            FbsMarketplaceMetadataTransfer.STATUS_RETRY,
        )
        self.assertFalse(is_final_wb_marking_rejection(self.transfer))

    def test_wb_pending_kiz_decision_uses_fast_readback_delay(self):
        self.transfer.value = self.VALID_KIZ
        self.transfer.status = FbsMarketplaceMetadataTransfer.STATUS_SENT
        self.transfer.external_status = "request_sent"
        self.transfer.save(
            update_fields=["value", "status", "external_status", "updated_at"]
        )
        command = FbsMarketplaceCommand.objects.create(
            profile=self.profile,
            order=self.order,
            command_type=WB_READ_ORDER_METADATA,
            http_method=FbsMarketplaceCommand.METHOD_POST,
            endpoint="/api/marketplace/v3/orders/meta",
            idempotency_key="test-fast-pending-readback",
            payload={},
            payload_hash="fast-pending-readback",
            status=FbsMarketplaceCommand.STATUS_SENT,
            attempt_count=1,
        )
        now = timezone.now()

        with patch("fbs.services.marketplace.timezone.now", return_value=now):
            _apply_wb_metadata_readback(
                command,
                {
                    "meta_details": {
                        "sgtin": {
                            "value": self.VALID_KIZ,
                            "errors": [],
                            "decision": "pending",
                        }
                    }
                },
                MarketplaceHttpResponse(
                    status_code=200,
                    headers={"Content-Type": "application/json"},
                    content=b"",
                    json_payload={},
                ),
            )

        command.refresh_from_db()
        self.assertEqual(command.status, FbsMarketplaceCommand.STATUS_RETRY)
        self.assertEqual(command.next_attempt_at, now + timedelta(seconds=2))

    def test_controller_metadata_problem_shows_exact_wb_reason(self):
        reason = "WB отклонил КИЗ: площадка вернула решение «sgtinNoGs»."
        self.transfer.status = FbsMarketplaceMetadataTransfer.STATUS_CONFLICT
        self.transfer.last_error = reason
        self.transfer.save(update_fields=["status", "last_error", "updated_at"])

        metadata = _check_tote_metadata_status(
            [self.transfer],
            marketplace=FbsIntegrationProfile.MARKETPLACE_WB,
        )

        self.assertEqual(metadata["state"], "problem")
        self.assertEqual(metadata["label"], reason)

    def test_controller_metadata_poll_uses_two_narrow_queries_for_fifty_orders(self):
        check_tote = FbsControllerCheckTote.objects.create(
            session=self.controller_session,
            agency=self.agency,
            profile=self.profile,
            handover_batch=self.batch,
            status=FbsControllerCheckTote.STATUS_WAITING_KIZ,
            item_qty=50,
            labeled_qty=50,
            opened_by=self.session_controller,
        )
        pick_batch = FbsPickBatch.objects.create(
            agency=self.agency,
            workstation=self.workstation,
            status=FbsPickBatch.STATUS_VERIFICATION,
            planned_qty=50,
            picked_qty=50,
            picking_completed_at=timezone.now(),
        )
        pick_cart = FbsPickingCart.objects.create(
            barcode="FBS-CART-METADATA-POLL",
            name="Metadata poll pick tote",
        )
        pick_tote = FbsControllerPickTote.objects.create(
            session=self.controller_session,
            check_tote=check_tote,
            pick_batch=pick_batch,
            tote=pick_cart,
            status=FbsControllerPickTote.STATUS_CLOSED,
            planned_qty=50,
            processed_qty=50,
        )
        first_transfer = None
        for index in range(50):
            order = FbsOrder.objects.create(
                profile=self.profile,
                external_order_id=f"METADATA-POLL-{index}",
                internal_status=FbsOrder.STATUS_PICKED,
            )
            item = FbsOrderItem.objects.create(
                order=order,
                external_line_id=f"METADATA-POLL-LINE-{index}",
                external_sku=self.sku.sku_code,
                sku=self.sku,
                barcode=self.balance.barcode,
                product_name=self.sku.name,
                quantity=1,
            )
            transfer = FbsMarketplaceMetadataTransfer.objects.create(
                order_item=item,
                metadata_type=FbsMarketplaceMetadataTransfer.TYPE_MARKING_CODE,
                value=f"METADATA-POLL-KIZ-{index}",
                is_required=True,
                status=FbsMarketplaceMetadataTransfer.STATUS_QUEUED,
                idempotency_key=f"metadata-poll-transfer-{index}",
            )
            if first_transfer is None:
                first_transfer = transfer
            label = FbsOrderLabel.objects.create(
                order=order,
                marketplace=FbsIntegrationProfile.MARKETPLACE_WB,
                external_label_id=f"METADATA-POLL-LABEL-{index}",
                barcode=f"METADATA-POLL-BARCODE-{index}",
                status=FbsOrderLabel.STATUS_APPLIED,
                applied_by=self.user,
                applied_at=timezone.now(),
            )
            FbsControllerToteOrder.objects.create(
                check_tote=check_tote,
                pick_tote=pick_tote,
                order=order,
                label=label,
                status=FbsControllerToteOrder.STATUS_LABELED,
                units=1,
                label_confirmed_by=self.user,
            )

        oldest_waiting_at = timezone.now() - timedelta(minutes=7)
        FbsMarketplaceMetadataTransfer.objects.filter(pk=first_transfer.pk).update(
            prepared_at=oldest_waiting_at
        )

        readiness = SimpleNamespace(metadata_blocked_order_ids=frozenset(
            check_tote.orders.values_list("order_id", flat=True)
        ))
        with self.assertNumQueries(2):
            rows, waiting_since = _check_tote_metadata_poll_orders(
                check_tote, readiness=readiness,
            )

        self.assertEqual(len(rows), 50)
        self.assertTrue(all(row["state"] == "queued" for row in rows))
        self.assertEqual(waiting_since, oldest_waiting_at)

    def test_mismatched_sgtin_introduced_remains_conflict(self):
        self.transfer.value = self.VALID_KIZ
        self.transfer.status = FbsMarketplaceMetadataTransfer.STATUS_SENT
        self.transfer.save(update_fields=["value", "status", "updated_at"])
        command = FbsMarketplaceCommand.objects.create(
            profile=self.profile,
            order=self.order,
            command_type=WB_READ_ORDER_METADATA,
            http_method=FbsMarketplaceCommand.METHOD_POST,
            endpoint="/api/marketplace/v3/orders/meta",
            idempotency_key="test-sgtin-introduced-mismatch",
            payload={},
            payload_hash="mismatch",
            status=FbsMarketplaceCommand.STATUS_SENT,
        )

        _apply_wb_metadata_readback(
            command,
            {
                "meta_details": {
                    "sgtin": {
                        "value": "010461046911083521DIFFERENT",
                        "errors": [],
                        "decision": "sgtinIntroduced",
                    }
                }
            },
            MarketplaceHttpResponse(
                status_code=200,
                headers={"Content-Type": "application/json"},
                content=b"",
                json_payload={},
            ),
        )

        self.transfer.refresh_from_db()
        command.refresh_from_db()
        self.assertEqual(
            self.transfer.status,
            FbsMarketplaceMetadataTransfer.STATUS_CONFLICT,
        )
        self.assertTrue(
            self.transfer.external_status.startswith("wb_rejected:")
        )
        self.assertTrue(is_final_wb_marking_rejection(self.transfer))
        self.assertEqual(command.status, FbsMarketplaceCommand.STATUS_CONFLICT)

    def test_matching_sgtin_introduced_with_errors_remains_conflict(self):
        self.transfer.value = self.VALID_KIZ
        self.transfer.status = FbsMarketplaceMetadataTransfer.STATUS_SENT
        self.transfer.save(update_fields=["value", "status", "updated_at"])
        command = FbsMarketplaceCommand.objects.create(
            profile=self.profile,
            order=self.order,
            command_type=WB_READ_ORDER_METADATA,
            http_method=FbsMarketplaceCommand.METHOD_POST,
            endpoint="/api/marketplace/v3/orders/meta",
            idempotency_key="test-sgtin-introduced-errors",
            payload={},
            payload_hash="errors",
            status=FbsMarketplaceCommand.STATUS_SENT,
        )

        _apply_wb_metadata_readback(
            command,
            {
                "meta_details": {
                    "sgtin": {
                        "value": self.VALID_KIZ,
                        "errors": ["validationError"],
                        "decision": "sgtinIntroduced",
                    }
                }
            },
            MarketplaceHttpResponse(
                status_code=200,
                headers={"Content-Type": "application/json"},
                content=b"",
                json_payload={},
            ),
        )

        self.transfer.refresh_from_db()
        command.refresh_from_db()
        self.assertEqual(
            self.transfer.status,
            FbsMarketplaceMetadataTransfer.STATUS_CONFLICT,
        )
        self.assertEqual(command.status, FbsMarketplaceCommand.STATUS_CONFLICT)

    def test_complete_order_cannot_be_retried(self):
        self.order.marketplace_status = "complete"
        self.order.save(update_fields=["marketplace_status", "updated_at"])
        with self.assertRaises(FbsIntegrationError):
            retry_wb_marking_code(
                batch_id=self.batch.id,
                order_item_id=self.item.id,
                marking_scan=self.VALID_KIZ,
                requested_by=self.user,
            )
        self.assertFalse(FbsMarketplaceCommand.objects.exists())

    def test_rescan_sends_kiz_of_another_product_to_wb(self):
        command = retry_wb_marking_code(
            batch_id=self.batch.id,
            order_item_id=self.item.id,
            marking_scan=self.OTHER_PRODUCT_KIZ,
            requested_by=self.user,
        )

        self.trace.refresh_from_db()
        self.transfer.refresh_from_db()
        self.assertEqual(self.trace.marking_code, self.OTHER_PRODUCT_KIZ)
        self.assertEqual(self.transfer.value, self.OTHER_PRODUCT_KIZ)
        self.assertEqual(
            command.payload["body"],
            {"sgtins": [self.OTHER_PRODUCT_KIZ]},
        )

    def test_rescan_sends_unrecognized_nonempty_code_to_wb(self):
        unrecognized_scan = "raw-unrecognized-scan-12345"

        command = retry_wb_marking_code(
            batch_id=self.batch.id,
            order_item_id=self.item.id,
            marking_scan=unrecognized_scan,
            requested_by=self.user,
        )

        self.assertEqual(
            command.payload["body"],
            {"sgtins": [unrecognized_scan]},
        )

    def test_rescan_still_requires_a_nonempty_scan(self):
        with self.assertRaisesMessage(
            FbsIntegrationError,
            "Отсканируйте КИЗ Честного знака повторно",
        ):
            retry_wb_marking_code(
                batch_id=self.batch.id,
                order_item_id=self.item.id,
                marking_scan="",
                requested_by=self.user,
            )

        self.assertFalse(FbsMarketplaceCommand.objects.exists())

    def test_rescan_allows_kiz_still_counted_in_active_stock(self):
        active_box = FbsBox.objects.create(
            agency=self.agency,
            pallet=self.balance.box.pallet,
            box_code="FBS-KIZ-ACTIVE-BOX",
            status=FbsBox.STATUS_ACTIVE,
        )
        FbsStockBalance.objects.create(
            agency=self.agency,
            box=active_box,
            sku_ref=self.sku,
            identity_key="2" * 64,
            sku_code=self.sku.sku_code,
            name=self.sku.name,
            barcode=self.balance.barcode,
            marking_code=self.VALID_KIZ,
            qty=1,
            available_qty=1,
        )

        command = retry_wb_marking_code(
            batch_id=self.batch.id,
            order_item_id=self.item.id,
            marking_scan=self.VALID_KIZ,
            requested_by=self.user,
        )

        self.assertEqual(command.command_type, WB_SET_ORDER_SGTINS)
        self.assertEqual(command.payload["body"], {"sgtins": [self.VALID_KIZ]})

    def test_rescan_allows_kiz_used_by_another_active_order(self):
        other_order = FbsOrder.objects.create(
            profile=self.profile,
            external_order_id="5497933555",
            internal_status=FbsOrder.STATUS_READY_FOR_HANDOVER,
            marketplace_status="confirm",
        )
        other_item = FbsOrderItem.objects.create(
            order=other_order,
            external_line_id="KIZ-LINE-2",
            external_sku=self.sku.sku_code,
            sku=self.sku,
            barcode=self.balance.barcode,
            product_name=self.sku.name,
            quantity=1,
            requirements={"required_meta": ["sgtin"]},
        )
        other_allocation = FbsOrderStockAllocation.objects.create(
            order_item=other_item,
            balance=self.balance,
            qty_reserved=1,
            qty_picked=1,
            status=FbsOrderStockAllocation.STATUS_PICKED,
        )
        FbsOrderTraceability.objects.create(
            allocation=other_allocation,
            marking_code=self.VALID_KIZ,
            qty=1,
            status=FbsOrderTraceability.STATUS_PICKED,
        )
        other_batch = FbsHandoverBatch.objects.create(
            profile=self.profile,
            external_supply_id="WB-GI-OTHER",
            status=FbsHandoverBatch.STATUS_OPEN,
            marketplace_state=FbsHandoverBatch.MARKETPLACE_OPEN,
        )
        FbsHandoverOrderAssignment.objects.create(
            batch=other_batch,
            order=other_order,
            status=FbsHandoverOrderAssignment.STATUS_CONFIRMED,
        )

        command = retry_wb_marking_code(
            batch_id=self.batch.id,
            order_item_id=self.item.id,
            marking_scan=self.VALID_KIZ,
            requested_by=self.user,
        )

        self.assertEqual(command.command_type, WB_SET_ORDER_SGTINS)

    def test_rescan_allows_kiz_from_previous_metadata_transfer_order(self):
        other_order = FbsOrder.objects.create(
            profile=self.profile,
            external_order_id="5497933556",
            internal_status=FbsOrder.STATUS_READY_FOR_HANDOVER,
            marketplace_status="confirm",
        )
        other_item = FbsOrderItem.objects.create(
            order=other_order,
            external_line_id="KIZ-LINE-3",
            external_sku=self.sku.sku_code,
            sku=self.sku,
            barcode=self.balance.barcode,
            product_name=self.sku.name,
            quantity=1,
            requirements={"required_meta": ["sgtin"]},
        )
        other_batch = FbsHandoverBatch.objects.create(
            profile=self.profile,
            external_supply_id="WB-GI-TRANSFER",
            status=FbsHandoverBatch.STATUS_OPEN,
            marketplace_state=FbsHandoverBatch.MARKETPLACE_OPEN,
        )
        FbsHandoverOrderAssignment.objects.create(
            batch=other_batch,
            order=other_order,
            status=FbsHandoverOrderAssignment.STATUS_CONFIRMED,
        )
        FbsMarketplaceMetadataTransfer.objects.create(
            order_item=other_item,
            metadata_type=FbsMarketplaceMetadataTransfer.TYPE_MARKING_CODE,
            value=self.VALID_KIZ,
            is_required=True,
            status=FbsMarketplaceMetadataTransfer.STATUS_CONFLICT,
            idempotency_key="other-order-kiz-transfer",
            external_status="sgtinnotfound",
            last_error="WB отклонил КИЗ.",
        )

        command = retry_wb_marking_code(
            batch_id=self.batch.id,
            order_item_id=self.item.id,
            marking_scan=self.VALID_KIZ,
            requested_by=self.user,
        )

        self.assertEqual(command.command_type, WB_SET_ORDER_SGTINS)

    def test_wb_204_requires_supply_membership_readback_before_confirmation(self):
        self.assignment.status = FbsHandoverOrderAssignment.STATUS_PENDING
        self.assignment.confirmed_at = None
        self.assignment.save(update_fields=["status", "confirmed_at", "updated_at"])
        command = schedule_wb_handover_order(assignment_id=self.assignment.id)

        process_marketplace_command(
            command_id=command.id,
            transport=_StaticTransport(status=204),
        )
        command.refresh_from_db()
        self.assignment.refresh_from_db()
        self.assertEqual(command.status, FbsMarketplaceCommand.STATUS_CONFLICT)
        self.assertEqual(
            self.assignment.status,
            FbsHandoverOrderAssignment.STATUS_PENDING,
        )

        read_supply = FbsMarketplaceCommand.objects.get(
            command_type=WB_READ_HANDOVER_SUPPLY,
        )
        process_marketplace_command(
            command_id=read_supply.id,
            transport=_StaticTransport({"id": self.batch.external_supply_id, "done": False}),
        )
        read_orders = FbsMarketplaceCommand.objects.get(
            command_type=WB_READ_HANDOVER_ORDER_IDS,
        )
        process_marketplace_command(
            command_id=read_orders.id,
            transport=_StaticTransport({"orderIds": [int(self.order.external_order_id)]}),
        )

        command.refresh_from_db()
        self.assignment.refresh_from_db()
        self.assertEqual(command.status, FbsMarketplaceCommand.STATUS_CONFIRMED)
        self.assertEqual(
            self.assignment.status,
            FbsHandoverOrderAssignment.STATUS_CONFIRMED,
        )

    def test_closed_supply_readback_marks_batch_complete_and_stops_order(self):
        self.assignment.status = FbsHandoverOrderAssignment.STATUS_PENDING
        self.assignment.confirmed_at = None
        self.assignment.save(update_fields=["status", "confirmed_at", "updated_at"])
        command = schedule_wb_handover_order(assignment_id=self.assignment.id)

        process_marketplace_command(
            command_id=command.id,
            transport=_StaticTransport(status=204),
        )
        read_supply = FbsMarketplaceCommand.objects.get(
            command_type=WB_READ_HANDOVER_SUPPLY,
        )
        process_marketplace_command(
            command_id=read_supply.id,
            transport=_StaticTransport(
                {"id": self.batch.external_supply_id, "done": True}
            ),
        )

        self.batch.refresh_from_db()
        self.assignment.refresh_from_db()
        command.refresh_from_db()
        self.assertEqual(
            self.batch.marketplace_state,
            FbsHandoverBatch.MARKETPLACE_COMPLETE,
        )
        self.assertEqual(self.batch.marketplace_payload["done"], True)
        self.assertEqual(
            self.assignment.status,
            FbsHandoverOrderAssignment.STATUS_ERROR,
        )
        self.assertIn("поставка уже закрыта", self.assignment.error)
        self.assertNotIn("будет перенесен", self.assignment.error)
        self.assertEqual(command.status, FbsMarketplaceCommand.STATUS_FAILED)
