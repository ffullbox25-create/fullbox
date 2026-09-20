from pathlib import Path
from types import SimpleNamespace

from django.conf import settings
from django.test import SimpleTestCase, TestCase

from sku.models import Agency

from .controller_views import _check_tote_metadata_status
from .integrations.contracts import (
    WB_ADD_ORDER_TO_HANDOVER,
    WB_FETCH_ORDER_STICKER,
    WB_READ_ORDER_METADATA,
    WB_SET_ORDER_EXPIRATION,
    WB_SET_ORDER_SGTINS,
)
from .models import (
    FbsIntegrationProfile,
    FbsMarketplaceCommand,
    FbsMarketplaceMetadataTransfer,
    FbsOrder,
)
from .services.marketplace import (
    _marketplace_queue_priority_value,
    _prioritize_interactive_label_commands,
)


class WbCheckToteMetadataStatusTests(SimpleTestCase):
    def status(self, transfer_status):
        transfers = (
            []
            if transfer_status is None
            else [SimpleNamespace(status=transfer_status)]
        )
        return _check_tote_metadata_status(
            transfers,
            marketplace=FbsIntegrationProfile.MARKETPLACE_WB,
        )

    def test_wb_status_labels_follow_automatic_transfer(self):
        self.assertEqual(self.status(None)["state"], "not_required")

        queued = self.status(FbsMarketplaceMetadataTransfer.STATUS_QUEUED)
        self.assertEqual(queued["state"], "queued")
        self.assertEqual(queued["label"], "Отправляется в WB")

        sent = self.status(FbsMarketplaceMetadataTransfer.STATUS_SENT)
        self.assertEqual(sent["state"], "checking")
        self.assertIn("WB проверяет", sent["label"])

        confirmed = self.status(
            FbsMarketplaceMetadataTransfer.STATUS_CONFIRMED
        )
        self.assertTrue(confirmed["ok"])
        self.assertEqual(confirmed["label"], "Подтверждено WB")

        conflict = self.status(
            FbsMarketplaceMetadataTransfer.STATUS_CONFLICT
        )
        self.assertEqual(conflict["state"], "problem")
        self.assertEqual(conflict["css_class"], "status-problem")

    def test_controller_template_polls_without_an_operator_action(self):
        template = (
            Path(settings.BASE_DIR) / "templates/fbs/controller_check_tote.html"
        ).read_text(encoding="utf-8")

        self.assertIn("data-metadata-status-url", template)
        self.assertIn("metadata_status=1", template)
        self.assertIn("pollMetadata", template)
        self.assertIn("fbs:metadata-ready", template)


class WbMarketplaceQueuePriorityTests(SimpleTestCase):
    def test_priority_annotation_contains_all_interactive_groups(self):
        queryset = _prioritize_interactive_label_commands(
            FbsMarketplaceCommand.objects.none()
        )

        priority = queryset.query.annotations["interactive_priority"]
        self.assertEqual(len(priority.cases), 4)

    def test_wb_marking_write_and_readback_run_before_background_commands(self):
        self.assertEqual(
            _marketplace_queue_priority_value(WB_FETCH_ORDER_STICKER),
            1,
        )
        self.assertEqual(
            _marketplace_queue_priority_value(WB_SET_ORDER_EXPIRATION),
            2,
        )
        self.assertEqual(
            _marketplace_queue_priority_value(WB_SET_ORDER_SGTINS),
            2,
        )
        self.assertEqual(
            _marketplace_queue_priority_value(WB_READ_ORDER_METADATA),
            3,
        )
        self.assertEqual(
            _marketplace_queue_priority_value(WB_ADD_ORDER_TO_HANDOVER),
            4,
        )
        self.assertEqual(
            _marketplace_queue_priority_value(
                WB_ADD_ORDER_TO_HANDOVER,
                has_requested_label=True,
            ),
            0,
        )


class WbMarketplaceQueueOrderingDatabaseTests(TestCase):
    def setUp(self):
        agency = Agency.objects.create(agn_name="WB scan feedback test")
        self.profile = FbsIntegrationProfile.objects.create(
            agency=agency,
            marketplace=FbsIntegrationProfile.MARKETPLACE_WB,
            name="WB scan feedback",
            external_warehouse_id="1931120",
            outbox_enabled=True,
            marking_push_enabled=True,
        )
        self.order = FbsOrder.objects.create(
            profile=self.profile,
            external_order_id="WB-QUEUE-PRIORITY-1",
        )

    def command(self, command_type, key):
        return FbsMarketplaceCommand.objects.create(
            profile=self.profile,
            order=self.order,
            command_type=command_type,
            http_method=FbsMarketplaceCommand.METHOD_GET,
            endpoint=f"/test/{key}",
            endpoint_version="test",
            idempotency_key=f"wb-scan-feedback-{key}",
            payload={},
            payload_hash=f"hash-{key}",
        )

    def test_database_orders_wb_marking_before_background_commands(self):
        background = self.command(WB_ADD_ORDER_TO_HANDOVER, "background")
        readback = self.command(WB_READ_ORDER_METADATA, "readback")
        expiration = self.command(WB_SET_ORDER_EXPIRATION, "expiration")
        sgtin = self.command(WB_SET_ORDER_SGTINS, "sgtin")
        label = self.command(WB_FETCH_ORDER_STICKER, "label")

        rows = list(
            _prioritize_interactive_label_commands(
                FbsMarketplaceCommand.objects.filter(order=self.order)
            )
            .order_by("interactive_priority", "created_at", "id")
            .values_list("id", "interactive_priority")
        )

        self.assertEqual(
            rows,
            [
                (label.id, 1),
                (expiration.id, 2),
                (sgtin.id, 2),
                (readback.id, 3),
                (background.id, 4),
            ],
        )
