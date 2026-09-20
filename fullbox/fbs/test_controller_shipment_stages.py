from datetime import datetime, timezone
from types import SimpleNamespace

from django.test import SimpleTestCase

from .controller_shipment_ui import controller_shipment_stage, shipment_sent
from .models import FbsHandoverBatch
from .tsd_views import _handover_stage_rows


class ControllerShipmentStageTests(SimpleTestCase):
    def batch(self, **overrides):
        values = {
            "status": FbsHandoverBatch.STATUS_READY,
            "marketplace_state": FbsHandoverBatch.MARKETPLACE_COMPLETE,
            "supply_label_file": "handover-labels/wb-supply.png",
            "dispatched_at": None,
        }
        values.update(overrides)
        return SimpleNamespace(**values)

    def test_marketplace_complete_is_ready_not_in_transit(self):
        batch = self.batch()

        self.assertFalse(shipment_sent(batch))
        stage = controller_shipment_stage(batch)
        self.assertEqual(stage.key, "ready")
        self.assertEqual(stage.label, "Готово к отгрузке")

    def test_checked_waits_for_supply_label(self):
        stage = controller_shipment_stage(self.batch(supply_label_file=""))

        self.assertEqual(stage.key, "checked")
        self.assertEqual(stage.label, "Проверено")

    def test_ozon_internal_supply_label_ready_but_not_transit(self):
        batch = self.batch(profile=SimpleNamespace(marketplace='ozon'), supply_label_file='', marketplace_state='open')
        self.assertEqual(controller_shipment_stage(batch).key, 'ready')
        self.assertFalse(shipment_sent(batch))
        self.assertEqual(_handover_stage_rows(batch)[3].state, 'in_progress')

    def test_print_dispatch_is_in_transit(self):
        batch = self.batch(
            status=FbsHandoverBatch.STATUS_DISPATCHED,
            dispatched_at=datetime(2026, 9, 9, tzinfo=timezone.utc),
        )

        self.assertTrue(shipment_sent(batch))
        stage = controller_shipment_stage(batch)
        self.assertEqual(stage.key, "transit")
        self.assertEqual(stage.label, "В пути")

    def test_marketplace_acceptance_is_separate_stage(self):
        stage = controller_shipment_stage(
            self.batch(status=FbsHandoverBatch.STATUS_ACCEPTED)
        )

        self.assertEqual(stage.key, "accepted")
        self.assertEqual(stage.label, "Принято маркетплейсом")

    def test_detail_stage_strip_distinguishes_checked_and_ready(self):
        checked = _handover_stage_rows(self.batch(supply_label_file=""))
        ready = _handover_stage_rows(self.batch())

        self.assertEqual(checked[2].label, "Проверено")
        self.assertEqual(checked[2].state, "in_progress")
        self.assertEqual(ready[3].label, "Готово к отгрузке")
        self.assertEqual(ready[3].state, "in_progress")
        self.assertEqual(ready[4].label, "В пути")
        self.assertEqual(ready[5].label, "Принято маркетплейсом")
