from types import SimpleNamespace

from django.test import SimpleTestCase

from fbs.goods_types import is_fbs_client_movement_source_stock
from sklad.services.dispatchable_stock import is_dispatchable_warehouse_state


class UniversalDispatchableStockTests(SimpleTestCase):
    def test_released_otg_stock_is_dispatchable(self):
        self.assertTrue(is_dispatchable_warehouse_state("in_otg"))
        self.assertTrue(is_dispatchable_warehouse_state("ready_for_loading"))

    def test_unfinished_and_reserved_stock_is_not_dispatchable(self):
        self.assertFalse(is_dispatchable_warehouse_state("in_processing_zone"))
        self.assertFalse(is_dispatchable_warehouse_state("processing_in_progress"))
        self.assertFalse(is_dispatchable_warehouse_state("reserved_for_shipping"))

    def test_fbs_can_use_released_otg_box(self):
        snapshot = SimpleNamespace(pk=51)
        self.assertTrue(
            is_fbs_client_movement_source_stock(
                goods_type="gv",
                zone_kind="shipping",
                warehouse_state_code="in_otg",
                container_type="box",
                container_code="TST-BOX-51-gv",
                container_status="active",
                snapshot=snapshot,
                receiving_allowed_ids=set(),
            )
        )

    def test_fbs_can_use_legacy_vp_box(self):
        snapshot = SimpleNamespace(pk=53)
        self.assertTrue(
            is_fbs_client_movement_source_stock(
                goods_type="gv",
                zone_kind="storage",
                warehouse_state_code="stored",
                container_type="box",
                container_code="FAV-15-10-57937_VP",
                container_status="active",
                snapshot=snapshot,
                receiving_allowed_ids=set(),
            )
        )

    def test_fbs_rejects_unrecognized_box_suffix(self):
        snapshot = SimpleNamespace(pk=54)
        self.assertFalse(
            is_fbs_client_movement_source_stock(
                goods_type="gv",
                zone_kind="storage",
                warehouse_state_code="stored",
                container_type="box",
                container_code="FAV-15-10-57937_OTHER",
                container_status="active",
                snapshot=snapshot,
                receiving_allowed_ids=set(),
            )
        )

    def test_receiving_needs_explicit_placement_permission(self):
        snapshot = SimpleNamespace(pk=52)
        common = {
            "goods_type": "gv",
            "zone_kind": "receiving",
            "warehouse_state_code": "placed_in_receiving",
            "container_type": "box",
            "container_code": "TST-BOX-52-gv",
            "container_status": "active",
            "snapshot": snapshot,
        }
        self.assertFalse(
            is_fbs_client_movement_source_stock(
                **common,
                receiving_allowed_ids=set(),
            )
        )
        self.assertTrue(
            is_fbs_client_movement_source_stock(
                **common,
                receiving_allowed_ids={52},
            )
        )
