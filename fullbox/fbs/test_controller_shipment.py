import base64
import tempfile

from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.test import TestCase, override_settings
from django.utils import timezone

from agent.models import DeviceAgent
from employees.models import Employee
from processing_app.models import ProcessingPrintJob
from sku.models import Agency

from .exceptions import FbsIntegrationError
from .integrations.contracts import (
    WB_ADD_ORDER_TO_HANDOVER,
    WB_CREATE_HANDOVER_BOXES,
    WB_CREATE_HANDOVER_SUPPLY,
    WB_DELIVER_HANDOVER,
    WB_FETCH_HANDOVER_BOX_LABEL,
    WB_FETCH_HANDOVER_SUPPLY_LABEL,
    WB_FETCH_ORDER_STICKER,
    WB_READ_HANDOVER_SUPPLIES,
)
from .integrations.http import MarketplaceHttpResponse
from .models import (
    FbsHandoverBatch,
    FbsHandoverBox,
    FbsHandoverOrderAssignment,
    FbsIntegrationProfile,
    FbsMarketplaceCommand,
    FbsOrder,
    FbsOrderItem,
    FbsOrderLabel,
    FbsPickBatch,
    FbsPickTask,
    FbsWorkstation,
)
from .services import (
    add_order_to_handover_box,
    add_wb_handover_boxes,
    close_handover_box,
    confirm_order_label_scan,
    dispatch_handover_batch,
    ensure_order_label_request,
    ensure_wb_order_handover_assignment,
    process_marketplace_command,
    request_wb_handover_delivery,
    queue_fbs_handover_box_label_print,
    queue_fbs_handover_supply_label_print,
    queue_fbs_order_label_print,
    register_marketplace_label,
    scan_handover_box,
)


class _RaisingTransport:
    def send(self, command):
        raise FbsIntegrationError("Ответ WB потерян после отправки.")


@override_settings(
    FBS_MODULE_ENABLED=True,
    FBS_WAREHOUSE_WRITES_ENABLED=True,
    FBS_OUTBOX_ENABLED=True,
    FBS_MARKING_PUSH_ENABLED=True,
)
class FbsControllerShipmentTests(TestCase):
    def setUp(self):
        self.label_root = tempfile.TemporaryDirectory()
        self.addCleanup(self.label_root.cleanup)
        self.settings_override = self.settings(FBS_LABEL_ROOT=self.label_root.name)
        self.settings_override.enable()
        self.addCleanup(self.settings_override.disable)

        self.operator = get_user_model().objects.create_user(
            username="fbs_shipment_operator",
            password="pwd",
        )
        Employee.objects.create(
            user=self.operator,
            full_name="Контролер отгрузки FBS",
            role="storekeeper",
        )
        self.picker = get_user_model().objects.create_user(
            username="fbs_shipment_picker",
            password="pwd",
        )
        Employee.objects.create(
            user=self.picker,
            full_name="Сборщик FBS",
            role="picker",
        )
        self.device_agent = DeviceAgent.objects.create(
            agent_id="comp-001",
            name="COMP 001",
            host="comp-001",
            last_seen=timezone.now(),
            meta={"printers": ["TSC TE200"]},
        )
        self.workstation = FbsWorkstation.objects.create(
            barcode="FBS-WS-100001",
            name="FBS стол 01",
            device_agent=self.device_agent,
            printer_name="TSC TE200",
        )
        self.agency = Agency.objects.create(agn_name="WB shipment client")
        self.profile = FbsIntegrationProfile.objects.create(
            agency=self.agency,
            marketplace=FbsIntegrationProfile.MARKETPLACE_WB,
            name="WB shipment test",
            external_account_id="wb-shipment-account",
            external_warehouse_id="1931120",
            is_active=True,
            outbox_enabled=True,
            marking_push_enabled=True,
        )

    def _order(self, external_order_id="123456"):
        order = FbsOrder.objects.create(
            profile=self.profile,
            external_order_id=external_order_id,
            internal_status=FbsOrder.STATUS_PICKED,
            marketplace_status="new",
            raw_payload={"warehouseId": 1931120, "officeId": 507, "deliveryType": "fbs"},
        )
        FbsOrderItem.objects.create(
            order=order,
            external_line_id=external_order_id,
            external_sku="WB-SKU-1",
            barcode="460000000001",
            product_name="Тестовый товар WB",
            quantity=1,
            requirements={"cargo_type": 1, "is_b2b": False},
        )
        ensure_order_label_request(order_id=order.id, requested_by=self.operator)
        return order

    @staticmethod
    def _response(payload=None, status=200):
        return MarketplaceHttpResponse(
            status_code=status,
            headers={"Content-Type": "application/json"},
            content=b"",
            json_payload=payload,
        )

    @staticmethod
    def _png_base64():
        return base64.b64encode(b"\x89PNG\r\n\x1a\nFULLBOX-FBS-TEST").decode("ascii")

    def _process(self, command_type, payload=None, *, status=200):
        command = FbsMarketplaceCommand.objects.filter(
            command_type=command_type,
            status__in=(
                FbsMarketplaceCommand.STATUS_PENDING,
                FbsMarketplaceCommand.STATUS_RETRY,
            ),
        ).latest("id")
        return process_marketplace_command(
            command_id=command.id,
            transport=type(
                "Transport",
                (),
                {"send": lambda self, ignored: FbsControllerShipmentTests._response(payload, status)},
            )(),
        )

    def test_wb_shipment_reaches_driver_only_after_box_and_supply_qr(self):
        order = self._order()
        assignment = ensure_wb_order_handover_assignment(
            order_id=order.id,
            assigned_by=self.operator,
        )

        self._process(WB_CREATE_HANDOVER_SUPPLY, {"id": "WB-GI-100001"}, status=201)
        self._process(WB_ADD_ORDER_TO_HANDOVER, status=204)
        self._process(
            WB_FETCH_ORDER_STICKER,
            {
                "stickers": [
                    {
                        "orderId": int(order.external_order_id),
                        "barcode": "WB-ORDER-QR-1",
                        "partA": "A1",
                        "partB": "B1",
                        "file": self._png_base64(),
                    }
                ]
            },
        )

        label = FbsOrderLabel.objects.get(order=order)
        confirm_order_label_scan(
            label_id=label.id,
            label_scan=label.barcode,
            performed_by=self.operator,
        )
        assignment.refresh_from_db()
        self.assertEqual(assignment.status, FbsHandoverOrderAssignment.STATUS_CONFIRMED)

        add_wb_handover_boxes(
            batch_id=assignment.batch_id,
            amount=1,
            requested_by=self.operator,
        )
        self._process(WB_CREATE_HANDOVER_BOXES, {"trbxIds": ["WB-TRBX-1"]}, status=201)
        self._process(
            WB_FETCH_HANDOVER_BOX_LABEL,
            {
                "stickers": [
                    {
                        "barcode": "WB-BOX-QR-1",
                        "file": self._png_base64(),
                    }
                ]
            },
        )

        box = FbsHandoverBox.objects.get(batch_id=assignment.batch_id)
        self.assertTrue(box.label_file)
        add_order_to_handover_box(
            box_id=box.id,
            order_label_scan=label.barcode,
            added_by=self.operator,
        )
        close_handover_box(box_id=box.id)
        scan_handover_box(
            batch_id=assignment.batch_id,
            box_qr_scan="WB-BOX-QR-1",
            scanned_by=self.operator,
        )

        request_wb_handover_delivery(
            batch_id=assignment.batch_id,
            requested_by=self.operator,
        )
        self._process(WB_DELIVER_HANDOVER, status=204)
        self._process(
            WB_FETCH_HANDOVER_SUPPLY_LABEL,
            {"barcode": "WB-SUPPLY-QR-1", "file": self._png_base64()},
        )
        batch = dispatch_handover_batch(
            batch_id=assignment.batch_id,
            dispatched_by=self.operator,
        )

        order.refresh_from_db()
        box.refresh_from_db()
        self.assertEqual(batch.status, FbsHandoverBatch.STATUS_DISPATCHED)
        self.assertEqual(batch.marketplace_state, FbsHandoverBatch.MARKETPLACE_COMPLETE)
        self.assertEqual(batch.supply_qr_code, "WB-SUPPLY-QR-1")
        self.assertTrue(batch.supply_label_file)
        self.assertEqual(box.status, FbsHandoverBox.STATUS_DISPATCHED)
        self.assertEqual(order.internal_status, FbsOrder.STATUS_HANDED_OVER)

    def test_unknown_supply_creation_is_read_before_write_can_repeat(self):
        order = self._order("123457")
        assignment = ensure_wb_order_handover_assignment(
            order_id=order.id,
            assigned_by=self.operator,
        )
        create_command = FbsMarketplaceCommand.objects.get(
            command_type=WB_CREATE_HANDOVER_SUPPLY,
        )

        result = process_marketplace_command(
            command_id=create_command.id,
            transport=_RaisingTransport(),
        )

        self.assertEqual(result.status, FbsMarketplaceCommand.STATUS_CONFLICT)
        self.assertEqual(
            FbsMarketplaceCommand.objects.filter(
                command_type=WB_CREATE_HANDOVER_SUPPLY
            ).count(),
            1,
        )
        readback = FbsMarketplaceCommand.objects.get(
            command_type=WB_READ_HANDOVER_SUPPLIES,
        )
        process_marketplace_command(
            command_id=readback.id,
            transport=type(
                "Transport",
                (),
                {
                    "send": lambda self, ignored: FbsControllerShipmentTests._response(
                        {
                            "supplies": [
                                {
                                    "id": "WB-GI-100002",
                                    "name": assignment.batch.external_name,
                                    "done": False,
                                }
                            ]
                        }
                    )
                },
            )(),
        )

        create_command.refresh_from_db()
        assignment.batch.refresh_from_db()
        self.assertEqual(create_command.status, FbsMarketplaceCommand.STATUS_CONFIRMED)
        self.assertEqual(assignment.batch.external_supply_id, "WB-GI-100002")
        self.assertEqual(
            FbsMarketplaceCommand.objects.filter(
                command_type=WB_CREATE_HANDOVER_SUPPLY
            ).count(),
            1,
        )
        self.assertTrue(
            FbsMarketplaceCommand.objects.filter(
                command_type=WB_ADD_ORDER_TO_HANDOVER,
                order=order,
            ).exists()
        )

    def test_order_label_is_queued_once_for_wave_workstation_and_can_be_reprinted(self):
        order = self._order("123458")
        batch = FbsPickBatch.objects.create(
            agency=self.agency,
            status=FbsPickBatch.STATUS_VERIFICATION,
            planned_qty=1,
            picked_qty=1,
            workstation=self.workstation,
            picking_completed_at=timezone.now(),
        )
        FbsPickTask.objects.create(
            batch=batch,
            order=order,
            status=FbsPickTask.STATUS_PICKED,
            planned_qty=1,
            picked_qty=1,
        )
        content = base64.b64decode(self._png_base64())

        with self.captureOnCommitCallbacks(execute=True):
            label = register_marketplace_label(
                order_id=order.id,
                external_label_id="WB-AUTO-PRINT",
                barcode="WB-AUTO-PRINT-QR",
                label_format=FbsOrderLabel.FORMAT_PNG,
                file_name="wb-auto-print.png",
                file_content=content,
            )
        with self.captureOnCommitCallbacks(execute=True):
            register_marketplace_label(
                order_id=order.id,
                external_label_id="WB-AUTO-PRINT",
                barcode="WB-AUTO-PRINT-QR",
                label_format=FbsOrderLabel.FORMAT_PNG,
                file_name="wb-auto-print.png",
                file_content=content,
            )

        automatic_job = ProcessingPrintJob.objects.get()
        self.assertEqual(automatic_job.printer_name, "TSC TE200")
        self.assertEqual(automatic_job.agent, "comp-001")
        self.assertEqual(automatic_job.order_id, f"FBS-ORDER-{order.id}")
        self.assertEqual(automatic_job.status, ProcessingPrintJob.STATUS_PENDING)
        self.assertEqual(automatic_job.label_width_mm, 58)
        self.assertEqual(automatic_job.label_height_mm, 40)

        first_attempt = queue_fbs_order_label_print(
            label_id=label.id,
            requested_by=self.operator,
            force=True,
        )
        self.assertEqual(first_attempt.id, automatic_job.id)
        self.assertEqual(ProcessingPrintJob.objects.count(), 1)

        automatic_job.status = ProcessingPrintJob.STATUS_PRINTED
        automatic_job.save(update_fields=["status"])
        reprint_job = queue_fbs_order_label_print(
            label_id=label.id,
            requested_by=self.operator,
            force=True,
        )
        self.assertEqual(ProcessingPrintJob.objects.count(), 2)
        repeated_click_job = queue_fbs_order_label_print(
            label_id=label.id,
            requested_by=self.operator,
            force=True,
        )
        self.assertEqual(repeated_click_job.id, reprint_job.id)
        self.assertEqual(ProcessingPrintJob.objects.count(), 2)

        reprint_job.status = ProcessingPrintJob.STATUS_PRINTED
        reprint_job.save(update_fields=["status"])
        completed_reprint_job = queue_fbs_order_label_print(
            label_id=label.id,
            requested_by=self.operator,
            force=True,
        )
        self.assertNotEqual(completed_reprint_job.id, reprint_job.id)
        self.assertEqual(ProcessingPrintJob.objects.count(), 3)
        with self.assertRaisesMessage(Exception, "нет доступа"):
            queue_fbs_order_label_print(
                label_id=label.id,
                requested_by=self.picker,
                force=True,
            )

    def test_box_and_supply_qr_are_sent_to_selected_fbs_workstation(self):
        batch = FbsHandoverBatch.objects.create(
            profile=self.profile,
            external_supply_id="WB-GI-PRINT",
            supply_qr_code="WB-SUPPLY-PRINT",
        )
        batch.supply_label_file.save(
            "supply.png",
            ContentFile(base64.b64decode(self._png_base64())),
        )
        box = FbsHandoverBox.objects.create(
            batch=batch,
            external_box_id="WB-TRBX-PRINT",
            qr_code="WB-BOX-PRINT",
        )
        box.label_file.save(
            "box.png",
            ContentFile(base64.b64decode(self._png_base64())),
        )

        box_job = queue_fbs_handover_box_label_print(
            batch_id=batch.id,
            box_id=box.id,
            workstation_id=self.workstation.id,
            requested_by=self.operator,
        )
        supply_job = queue_fbs_handover_supply_label_print(
            batch_id=batch.id,
            workstation_id=self.workstation.id,
            requested_by=self.operator,
        )

        self.assertEqual(box_job.agent, "comp-001")
        self.assertEqual(supply_job.agent, "comp-001")
        self.assertEqual(box_job.printer_name, "TSC TE200")
        self.assertEqual(supply_job.printer_name, "TSC TE200")
