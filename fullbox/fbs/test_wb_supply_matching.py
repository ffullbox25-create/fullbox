from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone

from sku.models import Agency

from .exceptions import FbsHandoverError
from .integrations.contracts import (
    WB_ADD_ORDER_TO_HANDOVER,
    WB_READ_HANDOVER_ORDER_IDS,
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
)
from .services.handover import wb_handover_compatibility_key
from .services.marketplace import (
    WB_SUPPLY_LOOKUP_CANDIDATE_OPERATION,
    WB_SUPPLY_LOOKUP_OPERATION,
    _apply_success,
    _schedule_controller_wb_box_if_ready,
    _sync_handover_failure,
    process_marketplace_command,
    process_marketplace_queue,
)


class _NoSendTransport:
    def __init__(self):
        self.commands = []

    def send(self, command):
        self.commands.append(command)
        raise AssertionError("Terminal WB order must not be sent to the marketplace")


@override_settings(
    FBS_MODULE_ENABLED=True,
    FBS_OUTBOX_ENABLED=True,
    FBS_MARKETPLACE_MAX_ATTEMPTS=8,
)
class WbSupplyMatchingTests(TestCase):
    def setUp(self):
        agency = Agency.objects.create(agn_name="WB supply matching client")
        self.profile = FbsIntegrationProfile.objects.create(
            agency=agency,
            marketplace=FbsIntegrationProfile.MARKETPLACE_WB,
            name="WB supply matching",
            external_warehouse_id="1931120",
            outbox_enabled=True,
        )
        self.batch = FbsHandoverBatch.objects.create(
            profile=self.profile,
            external_supply_id="WB-GI-TARGET-1",
            external_name="FULLBOX target",
            marketplace_state=FbsHandoverBatch.MARKETPLACE_OPEN,
        )
        self.order = FbsOrder.objects.create(
            profile=self.profile,
            external_order_id="5499000001",
            internal_status=FbsOrder.STATUS_READY_FOR_HANDOVER,
            marketplace_status="confirm",
        )
        self.assignment = FbsHandoverOrderAssignment.objects.create(
            batch=self.batch,
            order=self.order,
            status=FbsHandoverOrderAssignment.STATUS_PENDING,
        )
        self.write_command = self._command(
            command_type=WB_ADD_ORDER_TO_HANDOVER,
            endpoint="/api/marketplace/v3/supplies/WB-GI-TARGET-1/orders/5499000001",
            key="write",
            status=FbsMarketplaceCommand.STATUS_CONFLICT,
            payload={"query": {}, "body": {}, "context": {}},
        )

    def _command(self, *, command_type, endpoint, key, status, payload):
        return FbsMarketplaceCommand.objects.create(
            profile=self.profile,
            order=self.order,
            handover_batch=self.batch,
            command_type=command_type,
            http_method=FbsMarketplaceCommand.METHOD_GET,
            endpoint=endpoint,
            endpoint_version="v3",
            idempotency_key=f"test-wb-supply-matching-{key}",
            payload=payload,
            payload_hash=f"hash-{key}",
            status=status,
            attempt_count=1,
        )

    @staticmethod
    def response(payload):
        return MarketplaceHttpResponse(
            status_code=200,
            headers={},
            content=b"",
            json_payload=payload,
        )

    def intended_membership_command(self):
        return self._command(
            command_type=WB_READ_HANDOVER_ORDER_IDS,
            endpoint="/api/marketplace/v3/supplies/WB-GI-TARGET-1/order-ids",
            key="target-membership",
            status=FbsMarketplaceCommand.STATUS_SENT,
            payload={
                "query": {},
                "body": {},
                "context": {
                    "write_command_id": self.write_command.id,
                    "readback_operation": "add_order_target_membership",
                    "intended_supply_id": self.batch.external_supply_id,
                },
            },
        )

    def test_ready_batch_without_workstation_logs_error_and_does_not_create_box(self):
        self.batch.compatibility_key = (
            "warehouse:1931120:cargo:1:b2b:1:office:242:delivery:fbs"
        )
        self.batch.save(update_fields=["compatibility_key", "updated_at"])
        self.assignment.status = FbsHandoverOrderAssignment.STATUS_CONFIRMED
        self.assignment.confirmed_at = timezone.now()
        self.assignment.save(
            update_fields=["status", "confirmed_at", "updated_at"]
        )

        with self.assertLogs("fbs.services.marketplace", level="ERROR") as captured:
            _schedule_controller_wb_box_if_ready(batch_id=self.batch.id)

        self.assertFalse(
            FbsHandoverBox.objects.filter(batch=self.batch).exists()
        )
        message = "\n".join(captured.output)
        self.assertIn(f"batch {self.batch.id}", message)
        self.assertIn(f"order_ids=[{self.order.id}]", message)
        self.assertIn("lacks ':workstation:'", message)
        self.assertIn("controller routing is incomplete", message)

    def test_wb_compatibility_key_keeps_destination_for_normal_order(self):
        self.order.raw_payload = {"deliveryType": "fbs", "officeId": 242}
        self.order.save(update_fields=["raw_payload", "updated_at"])

        key = wb_handover_compatibility_key(
            self.order,
            workstation_id=4,
        )

        self.assertIn(":destination:warehouse_sc:workstation:4", key)

    def test_wb_compatibility_key_rejects_empty_destination(self):
        with patch(
            "fbs.services.handover.wb_order_destination_kind",
            return_value="",
        ):
            with self.assertRaisesMessage(
                FbsHandoverError,
                "обязательный destination",
            ):
                wb_handover_compatibility_key(self.order)

    def test_order_found_in_another_supply_is_persisted_as_not_ok(self):
        intended = self.intended_membership_command()
        _apply_success(intended.id, self.response({"orderIds": []}))

        lookup = FbsMarketplaceCommand.objects.get(
            command_type=WB_READ_HANDOVER_SUPPLIES,
            payload__context__readback_operation=WB_SUPPLY_LOOKUP_OPERATION,
        )
        lookup.status = FbsMarketplaceCommand.STATUS_SENT
        lookup.save(update_fields=["status", "updated_at"])
        _apply_success(
            lookup.id,
            self.response(
                {
                    "supplies": [
                        {"id": "WB-GI-TARGET-1", "name": "FULLBOX target", "done": False},
                        {"id": "WB-GI-OTHER-1", "name": "Поставка из кабинета WB", "done": False},
                    ]
                }
            ),
        )

        candidate = FbsMarketplaceCommand.objects.get(
            command_type=WB_READ_HANDOVER_ORDER_IDS,
            payload__context__readback_operation=WB_SUPPLY_LOOKUP_CANDIDATE_OPERATION,
        )
        candidate.status = FbsMarketplaceCommand.STATUS_SENT
        candidate.save(update_fields=["status", "updated_at"])
        _apply_success(
            candidate.id,
            self.response({"orderIds": [int(self.order.external_order_id)]}),
        )

        self.assignment.refresh_from_db()
        self.write_command.refresh_from_db()
        candidate.refresh_from_db()
        self.assertEqual(self.assignment.status, FbsHandoverOrderAssignment.STATUS_ERROR)
        self.assertIn("Не ОК", self.assignment.error)
        self.assertIn("WB-GI-OTHER-1", self.assignment.error)
        self.assertEqual(self.write_command.status, FbsMarketplaceCommand.STATUS_FAILED)
        self.assertTrue(candidate.response_payload["matched"])

    def test_no_other_open_supply_keeps_safe_retry(self):
        intended = self.intended_membership_command()
        _apply_success(intended.id, self.response({"orderIds": []}))

        lookup = FbsMarketplaceCommand.objects.get(
            command_type=WB_READ_HANDOVER_SUPPLIES,
            payload__context__readback_operation=WB_SUPPLY_LOOKUP_OPERATION,
        )
        lookup.status = FbsMarketplaceCommand.STATUS_SENT
        lookup.save(update_fields=["status", "updated_at"])
        _apply_success(
            lookup.id,
            self.response(
                {
                    "supplies": [
                        {"id": "WB-GI-TARGET-1", "name": "FULLBOX target", "done": False},
                        {"id": "WB-GI-CLOSED-1", "name": "Closed", "done": True},
                    ]
                }
            ),
        )

        self.assignment.refresh_from_db()
        self.write_command.refresh_from_db()
        self.assertEqual(self.assignment.status, FbsHandoverOrderAssignment.STATUS_PENDING)
        self.assertEqual(self.write_command.status, FbsMarketplaceCommand.STATUS_RETRY)
        self.assertIn("Других открытых поставок", self.write_command.error)

    def test_client_canceled_substatus_stops_retry(self):
        self.order.marketplace_status = "new"
        self.order.marketplace_substatus = "canceled_by_client"
        self.order.save(
            update_fields=["marketplace_status", "marketplace_substatus", "updated_at"]
        )
        intended = self.intended_membership_command()
        _apply_success(intended.id, self.response({"orderIds": []}))

        lookup = FbsMarketplaceCommand.objects.get(
            command_type=WB_READ_HANDOVER_SUPPLIES,
            payload__context__readback_operation=WB_SUPPLY_LOOKUP_OPERATION,
        )
        lookup.status = FbsMarketplaceCommand.STATUS_SENT
        lookup.save(update_fields=["status", "updated_at"])
        _apply_success(
            lookup.id,
            self.response(
                {
                    "supplies": [
                        {
                            "id": "WB-GI-TARGET-1",
                            "name": "FULLBOX target",
                            "done": False,
                        }
                    ]
                }
            ),
        )

        self.assignment.refresh_from_db()
        self.write_command.refresh_from_db()
        self.assertEqual(self.assignment.status, FbsHandoverOrderAssignment.STATUS_ERROR)
        self.assertEqual(self.write_command.status, FbsMarketplaceCommand.STATUS_FAILED)
        self.assertIn("сборочное задание уже", self.write_command.error)

    def test_client_canceled_retry_stops_before_transport(self):
        self.order.marketplace_substatus = "canceled_by_client"
        self.order.save(update_fields=["marketplace_substatus", "updated_at"])
        self.write_command.status = FbsMarketplaceCommand.STATUS_RETRY
        self.write_command.next_attempt_at = timezone.now() + timedelta(hours=1)
        self.write_command.save(
            update_fields=["status", "next_attempt_at", "updated_at"]
        )
        transport = _NoSendTransport()

        result = process_marketplace_command(
            command_id=self.write_command.id,
            transport=transport,
        )

        self.assignment.refresh_from_db()
        self.assertEqual(result.status, FbsMarketplaceCommand.STATUS_FAILED)
        self.assertIsNone(result.next_attempt_at)
        self.assertEqual(self.assignment.status, FbsHandoverOrderAssignment.STATUS_ERROR)
        self.assertEqual(transport.commands, [])

    def test_queue_sweeps_future_client_canceled_retry(self):
        self.order.marketplace_substatus = "canceled_by_client"
        self.order.save(update_fields=["marketplace_substatus", "updated_at"])
        self.write_command.status = FbsMarketplaceCommand.STATUS_RETRY
        self.write_command.next_attempt_at = timezone.now() + timedelta(hours=1)
        self.write_command.save(
            update_fields=["status", "next_attempt_at", "updated_at"]
        )
        transport = _NoSendTransport()

        result = process_marketplace_queue(
            limit=10,
            transport=transport,
            run_schedulers=False,
        )

        self.write_command.refresh_from_db()
        self.assertEqual(result.inspected, 1)
        self.assertEqual(result.failed, 1)
        self.assertEqual(self.write_command.status, FbsMarketplaceCommand.STATUS_FAILED)
        self.assertIsNone(self.write_command.next_attempt_at)
        self.assertEqual(transport.commands, [])

    def test_supply_list_failure_keeps_safe_retry(self):
        lookup = self._command(
            command_type=WB_READ_HANDOVER_SUPPLIES,
            endpoint="/api/marketplace/v3/supplies",
            key="supply-list-failure",
            status=FbsMarketplaceCommand.STATUS_FAILED,
            payload={
                "query": {},
                "body": {},
                "context": {
                    "readback_operation": WB_SUPPLY_LOOKUP_OPERATION,
                    "write_command_id": self.write_command.id,
                    "intended_supply_id": self.batch.external_supply_id,
                },
            },
        )
        lookup.error = "Маркетплейс временно недоступен."
        lookup.save(update_fields=["error", "updated_at"])

        _sync_handover_failure(lookup)

        self.assignment.refresh_from_db()
        self.write_command.refresh_from_db()
        self.assertEqual(self.assignment.status, FbsHandoverOrderAssignment.STATUS_PENDING)
        self.assertEqual(self.write_command.status, FbsMarketplaceCommand.STATUS_RETRY)
        self.assertIn("безопасная повторная сверка", self.write_command.error)
