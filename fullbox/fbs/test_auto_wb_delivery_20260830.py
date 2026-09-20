from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from employees.models import Employee
from sku.models import Agency

from .integrations.wb import build_wb_deliver_handover_spec
from .controller_views import _wb_delivery_status_payload
from .models import (
    FbsControllerCheckTote,
    FbsControllerSession,
    FbsHandoverBatch,
    FbsHandoverBox,
    FbsHandoverOrder,
    FbsIntegrationProfile,
    FbsMarketplaceCommand,
    FbsOrder,
    FbsPickingCart,
    FbsToteZone,
    FbsWorkstation,
)
from .services.marketplace import (
    _enqueue_handover_spec,
    schedule_wb_handover_delivery,
)
from .services.totes import _close_controller_check_tote_locked


@override_settings(
    FBS_MODULE_ENABLED=True,
    FBS_WAREHOUSE_WRITES_ENABLED=True,
    FBS_OUTBOX_ENABLED=True,
)
class FbsAutomaticWbDeliveryTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="wb_auto_delivery")
        Employee.objects.create(
            user=self.user,
            full_name="WB automatic delivery controller",
            role="fbs_controller",
        )
        self.agency = Agency.objects.create(agn_name="WB automatic delivery client")
        self.profile = FbsIntegrationProfile.objects.create(
            agency=self.agency,
            marketplace=FbsIntegrationProfile.MARKETPLACE_WB,
            name="WB automatic delivery",
            external_account_id="wb-auto-delivery",
            external_warehouse_id="1876669",
            is_active=True,
            outbox_enabled=True,
        )
        self.batch = FbsHandoverBatch.objects.create(
            profile=self.profile,
            external_supply_id="WB-GI-AUTO-DELIVERY",
            status=FbsHandoverBatch.STATUS_READY,
            marketplace_state=FbsHandoverBatch.MARKETPLACE_OPEN,
        )
        self.box = FbsHandoverBox.objects.create(
            batch=self.batch,
            qr_code="FBS-WB-AUTO-DELIVERY-BOX",
            status=FbsHandoverBox.STATUS_SCANNED,
        )
        self.order = FbsOrder.objects.create(
            profile=self.profile,
            external_order_id="WB-AUTO-DELIVERY-ORDER",
            internal_status=FbsOrder.STATUS_READY_FOR_HANDOVER,
        )
        FbsHandoverOrder.objects.create(
            box=self.box,
            order=self.order,
            status=FbsHandoverOrder.STATUS_ACTIVE,
            verified_by=self.user,
            verified_at=timezone.now(),
        )

    @patch("fbs.services.marketplace.wb_handover_uses_marketplace_boxes", return_value=False)
    @patch("fbs.services.marketplace.assert_handover_composition_ready")
    def test_confirmed_delivery_command_keeps_batch_complete(
        self,
        _composition_ready,
        _uses_marketplace_boxes,
    ):
        command = _enqueue_handover_spec(
            self.batch,
            build_wb_deliver_handover_spec(self.batch.external_supply_id),
            requested_by=self.user,
        )
        command.status = FbsMarketplaceCommand.STATUS_CONFIRMED
        command.save(update_fields=["status", "updated_at"])

        returned = schedule_wb_handover_delivery(
            batch_id=self.batch.id,
            requested_by=self.user,
        )

        self.batch.refresh_from_db()
        self.assertEqual(returned.id, command.id)
        self.assertEqual(
            self.batch.marketplace_state,
            FbsHandoverBatch.MARKETPLACE_COMPLETE,
        )
        self.assertEqual(
            FbsMarketplaceCommand.objects.filter(
                handover_batch=self.batch,
                command_type=command.command_type,
            ).count(),
            1,
        )

    @patch("fbs.services.handover.request_wb_handover_delivery")
    @patch("fbs.services.handover.close_handover_box")
    @patch("fbs.services.totes.check_tote_readiness")
    @patch("fbs.services.totes.active_check_tote_orders")
    def test_closing_checked_wb_tote_schedules_delivery_once(
        self,
        active_orders,
        readiness,
        close_box,
        request_delivery,
    ):
        active_queryset = MagicMock()
        active_queryset.exclude.return_value = active_queryset
        active_queryset.count.return_value = 0
        active_orders.return_value = active_queryset
        readiness.return_value = SimpleNamespace(ready=True, reasons=())
        workstation = FbsWorkstation.objects.create(
            barcode="FBS-WS-990030",
            name="WB automatic delivery workstation",
        )
        free_zone = FbsToteZone.objects.create(
            barcode="FBS-ZONE-AUTO-DELIVERY",
            name="WB automatic delivery free zone",
            kind=FbsToteZone.KIND_FREE,
        )
        unknown_tote = FbsPickingCart.objects.create(
            barcode="FBS-CART-990030",
            name="WB automatic delivery unknown tote",
        )
        session = FbsControllerSession.objects.create(
            workstation=workstation,
            controller=self.user,
            unknown_tote=unknown_tote,
            free_zone=free_zone,
        )
        check_tote = FbsControllerCheckTote.objects.create(
            session=session,
            agency=self.agency,
            profile=self.profile,
            handover_batch=self.batch,
            status=FbsControllerCheckTote.STATUS_READY,
            composition_qty=1,
            opened_by=self.user,
        )

        result = _close_controller_check_tote_locked(
            check_tote=check_tote,
            actor=self.user,
        )

        result.refresh_from_db()
        self.assertEqual(result.status, FbsControllerCheckTote.STATUS_CLOSED)
        close_box.assert_called_once_with(box_id=self.box.id)
        request_delivery.assert_called_once_with(
            batch_id=self.batch.id,
            requested_by=self.user,
            controller_auto_dispatch_check_tote_id=result.id,
        )

    def test_manual_wb_delivery_button_is_not_rendered_by_template(self):
        handover_template = (
            Path(settings.BASE_DIR) / "templates/fbs/tsd_handover_detail.html"
        ).read_text(encoding="utf-8")
        controller_template = (
            Path(settings.BASE_DIR) / "templates/fbs/controller_check_tote.html"
        ).read_text(encoding="utf-8")

        self.assertNotIn("Передать поставку в WB", handover_template)
        self.assertIn("Отправка в WB выполняется автоматически", handover_template)
        self.assertIn("data-auto-close-check-form", controller_template)
        self.assertIn("ВСЯ ПОСТАВКА ПРОВЕРЕНА", controller_template)
        self.assertIn("автоматически передаю поставку в доставку WB", controller_template)
        self.assertIn("window.setTimeout(finishShipment, 200)", controller_template)

    def test_delivery_status_payload_changes_without_manual_button(self):
        self.batch.marketplace_state = (
            FbsHandoverBatch.MARKETPLACE_DELIVERY_PENDING
        )

        pending = _wb_delivery_status_payload(self.batch)

        self.assertEqual(pending["delivery_status"], "uploading")
        self.assertIn("загружается", pending["message"])
        self.assertGreater(pending["retry_after_ms"], 0)

        self.batch.marketplace_state = FbsHandoverBatch.MARKETPLACE_COMPLETE

        complete = _wb_delivery_status_payload(self.batch)

        self.assertEqual(complete["delivery_status"], "complete")
        self.assertIn("передано в доставку WB", complete["message"])
        self.assertEqual(complete["retry_after_ms"], 0)
