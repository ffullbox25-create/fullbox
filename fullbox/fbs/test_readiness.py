from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone

from sku.models import Agency

from .models import (
    FbsIntegrationProfile,
    FbsPickingCart,
    FbsSyncCursor,
    FbsToteBinding,
    FbsToteZone,
    FbsWorkstation,
)
from .readiness import (
    SCAN_ERROR_ACTIONS,
    add_equipment_readiness,
    build_fbs_readiness_report,
)


@override_settings(
    FBS_MODULE_ENABLED=True,
    FBS_WAREHOUSE_WRITES_ENABLED=True,
    FBS_ORDER_PULL_ENABLED=True,
    FBS_STATUS_PULL_ENABLED=True,
    FBS_OUTBOX_ENABLED=False,
    FBS_MARKING_PUSH_ENABLED=False,
    FBS_STOCK_PUSH_ENABLED=False,
    FBS_BILLING_ENABLED=False,
)
class FbsReadinessTests(TestCase):
    def test_empty_contour_is_blocked_without_writes(self):
        before = {
            "profiles": FbsIntegrationProfile.objects.count(),
            "workstations": FbsWorkstation.objects.count(),
            "totes": FbsPickingCart.objects.count(),
        }
        report = build_fbs_readiness_report()
        after = {
            "profiles": FbsIntegrationProfile.objects.count(),
            "workstations": FbsWorkstation.objects.count(),
            "totes": FbsPickingCart.objects.count(),
        }
        self.assertEqual(before, after)
        self.assertEqual(report["overall"]["severity"], "blocked")
        self.assertEqual(report["profile_count"], 0)

    @patch(
        "fbs.readiness.marketplace_credentials_status",
        return_value={"configured": True, "source": "client_cabinet", "error": ""},
    )
    def test_ready_profile_and_free_tote_are_reported(self, _credentials):
        agency = Agency.objects.create(agn_name="Readiness client")
        profile = FbsIntegrationProfile.objects.create(
            agency=agency,
            marketplace=FbsIntegrationProfile.MARKETPLACE_WB,
            name="WB readiness",
            external_warehouse_id="1931120",
            is_active=True,
            order_pull_enabled=True,
        )
        FbsSyncCursor.objects.create(
            profile=profile,
            stream=FbsSyncCursor.STREAM_ORDERS,
            cursor_key="default",
            last_polled_at=timezone.now(),
            last_success_at=timezone.now(),
        )
        FbsWorkstation.objects.create(
            barcode="FBS-WS-990001",
            name="Readiness workstation",
        )
        tote = FbsPickingCart.objects.create(
            barcode="FBS-CART-990001",
            name="Readiness tote",
        )
        zone = FbsToteZone.objects.create(
            barcode="FBS-ZONE-FREE-TEST",
            name="Test free tote zone",
            kind=FbsToteZone.KIND_FREE,
        )
        FbsToteBinding.objects.create(
            tote=tote,
            state=FbsToteBinding.STATE_FREE,
            zone=zone,
        )

        report = build_fbs_readiness_report()

        self.assertEqual(report["profiles"][0]["severity"], "ok")
        self.assertEqual(report["free_totes"], 1)
        self.assertEqual(report["unbound_totes"], 0)

    def test_equipment_result_replaces_generic_workstation_check(self):
        report = {"checks": [{"code": "workstations", "severity": "ok"}]}
        add_equipment_readiness(
            report,
            [{"ready": True}, {"ready": False}],
        )
        self.assertEqual(report["ready_workstations"], 1)
        self.assertEqual(report["checks"][0]["severity"], "warning")
        self.assertEqual(report["overall"]["severity"], "warning")

    def test_closed_workstations_do_not_reduce_shift_readiness(self):
        report = {"checks": [{"code": "workstations", "severity": "ok"}]}

        add_equipment_readiness(
            report,
            [
                {"planned": True, "ready": True},
                {"planned": False, "ready": False},
                {"planned": False, "ready": False},
            ],
        )

        self.assertEqual(report["planned_workstations"], 1)
        self.assertEqual(report["ready_workstations"], 1)
        self.assertEqual(report["closed_workstations"], 2)
        self.assertEqual(report["checks"][0]["severity"], "ok")

    def test_no_workstation_opened_for_shift_is_blocked(self):
        report = {"checks": [{"code": "workstations", "severity": "ok"}]}

        add_equipment_readiness(
            report,
            [{"planned": False, "ready": False}],
        )

        self.assertEqual(report["planned_workstations"], 0)
        self.assertEqual(report["checks"][0]["severity"], "blocked")

    def test_label_scan_action_keeps_strict_order_validation(self):
        self.assertIn(
            "QR текущего заказа",
            SCAN_ERROR_ACTIONS["Скан не совпадает с этикеткой этого заказа."],
        )
