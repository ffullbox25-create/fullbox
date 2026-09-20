"""Подбор на отгрузку обязан видеть удержание ФБС так же, как видит обработку.

Задача reachtruck_process_split_20260920, блок 2.

До правки ворота источника резали только processing_reserved_qty, а
other_reserved_qty (удержание контура ФБС) не проверяли вовсе. Отказ прилетал
позже, на записи остатка, то есть в конце маршрута водителя.
"""
from django.test import TestCase

from sku.models import Agency
from sklad.models import WarehouseContainer, WarehouseLocation, WarehouseStockSnapshot

from .services import (
    _filter_live_warehouse_plans,
    _pallet_catalog,
    _snapshot_available_for_otg_source,
)


class FbsHoldSymmetryTests(TestCase):
    def setUp(self):
        self.agency = Agency.objects.create(agn_name="ООО Тест удержания ФБС")
        # get_or_create, как в reachtruck/tests.py: топология общая на всю базу и
        # переживает соседние тесты, а слот уникален по (склад, зона, ряд, секция, этаж, ячейка).
        self.location, _ = WarehouseLocation.objects.get_or_create(
            warehouse_code="MSK",
            zone_code="OS",
            row_no=91,
            section_no=91,
            tier_no=1,
            cell_no=1,
            defaults={"zone_kind": "storage", "display_name": "OS · 91-91-1-1"},
        )
        self.pallet = WarehouseContainer.objects.create(
            agency=self.agency,
            container_code="TST-2009-000001-gv",
            container_type=WarehouseContainer.TYPE_PALLET,
            current_location=self.location,
        )

    def _box(self, code):
        return WarehouseContainer.objects.create(
            agency=self.agency,
            container_code=code,
            container_type=WarehouseContainer.TYPE_BOX,
            parent_container=self.pallet,
            current_location=self.location,
        )

    def _snapshot(self, box, *, qty, available_qty, other=0, shipping=0, processing=0):
        return WarehouseStockSnapshot.objects.create(
            agency=self.agency,
            sku_code="TST-SKU",
            barcode="4600000000001",
            goods_type="gv",
            qty=qty,
            available_qty=available_qty,
            processing_reserved_qty=processing,
            shipping_reserved_qty=shipping,
            other_reserved_qty=other,
            container=box,
            container_code=box.container_code,
            parent_container=self.pallet,
            location=self.location,
            zone_code="OS",
            zone_kind="storage",
            warehouse_state_code="stored",
        )

    def _base_row(self, box, qty):
        return {
            "id": 0,
            "pallet_code": self.pallet.container_code,
            "box_code": box.container_code,
            "container_code": box.container_code,
            "zone": "OS",
            "zone_code": "OS",
            "qty": qty,
            "available_qty": qty,
            "order_id": "",
            "row": 91,
            "section": 91,
            "tier": 1,
            "cell": 1,
        }

    # --- ворота источника ------------------------------------------------

    def test_partial_fbs_hold_closes_the_source(self):
        """Короб 64 шт, из которых 32 держит ФБС, больше не источник отгрузки."""
        box = self._box("TST-2009-000101-gv")
        snapshot = self._snapshot(box, qty=64, available_qty=32, other=32)
        self.assertFalse(_snapshot_available_for_otg_source(snapshot))

    def test_free_stock_stays_available(self):
        box = self._box("TST-2009-000102-gv")
        snapshot = self._snapshot(box, qty=64, available_qty=64)
        self.assertTrue(_snapshot_available_for_otg_source(snapshot))

    def test_processing_hold_still_closes_the_source(self):
        """Регрессия: старая проверка обработки не сломана."""
        box = self._box("TST-2009-000103-gv")
        snapshot = self._snapshot(box, qty=64, available_qty=32, processing=32)
        self.assertFalse(_snapshot_available_for_otg_source(snapshot))

    def test_stock_reserved_for_shipping_stays_available(self):
        """Товар, уже закреплённый за отгрузкой, забирать можно — это её резерв."""
        box = self._box("TST-2009-000104-gv")
        snapshot = self._snapshot(box, qty=64, available_qty=0, shipping=64)
        self.assertTrue(_snapshot_available_for_otg_source(snapshot))

    # --- каталог паллет ---------------------------------------------------

    def test_held_box_leaves_available_boxes_but_stays_in_all_boxes(self):
        held = self._box("TST-2009-000201-gv")
        free = self._box("TST-2009-000202-gv")
        self._snapshot(held, qty=64, available_qty=32, other=32)
        self._snapshot(free, qty=64, available_qty=64)

        catalog = _pallet_catalog(
            [self._base_row(held, 32), self._base_row(free, 64)],
            agency_id=self.agency.id,
            blocked_pallets=set(),
        )
        entry = catalog[self.pallet.container_code]
        available_codes = {str(box.get("code")) for box in entry["available_boxes"]}
        all_codes = {str(box.get("code")) for box in entry["all_boxes"]}

        self.assertNotIn(held.container_code, available_codes)
        self.assertIn(free.container_code, available_codes)
        self.assertIn(held.container_code, all_codes, "полный состав паллеты должен остаться")

    def test_catalog_without_holds_is_unchanged(self):
        free = self._box("TST-2009-000203-gv")
        self._snapshot(free, qty=64, available_qty=64)
        catalog = _pallet_catalog(
            [self._base_row(free, 64)],
            agency_id=self.agency.id,
            blocked_pallets=set(),
        )
        entry = catalog[self.pallet.container_code]
        self.assertEqual(
            {str(box.get("code")) for box in entry["available_boxes"]},
            {free.container_code},
        )

    # --- живая перепроверка плана ----------------------------------------

    def test_plan_built_before_the_hold_becomes_shortage_not_an_exception(self):
        """Размен принят сознательно: честная недостача вместо отказа в конце маршрута."""
        box = self._box("TST-2009-000301-gv")
        self._snapshot(box, qty=64, available_qty=32, other=32)
        plan = {
            "pallet_code": self.pallet.container_code,
            "planned_box_codes": [box.container_code],
            "boxes_planned": 1,
            "qty_planned": 64,
        }

        verified, dropped_boxes, dropped_qty = _filter_live_warehouse_plans(
            [plan], agency_id=self.agency.id
        )

        self.assertEqual(verified, [])
        self.assertEqual(dropped_boxes, 1)
        self.assertEqual(dropped_qty, 64)
