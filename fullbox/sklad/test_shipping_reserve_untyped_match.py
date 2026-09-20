"""Резерв отгрузки без типа товара обязан закрываться прибытием типизированного остатка.

Случай с прода 20.09.2026: OTG-000577 и OTG-000578. Позиция заявки пришла без
goods_type, резерв унаследовал пустое значение, а физический остаток приехал в OTG
с типом 'gv'. Ключ сопоставления включает тип, поэтому 100 и 60 штук лежали в OTG
полностью отобранные, а резервы оставались открытыми и держали коробa занятыми.
"""
from django.contrib.auth import get_user_model
from django.test import TestCase

from sklad.models import (
    WarehouseContainer,
    WarehouseLocation,
    WarehouseReserve,
    WarehouseStockSnapshot,
)
from sklad.services.warehouse_write_path import WarehouseWritePathService
from sku.models import Agency

ORDER = "OTG-TEST-UNTYPED"


class UntypedShippingReserveMatchTests(TestCase):
    def setUp(self):
        self.agency = Agency.objects.create(agn_name="ООО Тест резерва без типа")
        self.location, _ = WarehouseLocation.objects.get_or_create(
            warehouse_code="MSK",
            zone_code="OTG",
            row_no=93,
            section_no=93,
            tier_no=1,
            cell_no=1,
            defaults={"zone_kind": "shipping", "display_name": "OTG · 93-93-1-1"},
        )

    def _reserve(self, *, sku, barcode, goods_type, qty, size="0"):
        return WarehouseReserve.objects.create(
            agency=self.agency,
            reserve_type=WarehouseReserve.TYPE_SHIPPING,
            context_type="shipping",
            context_id=ORDER,
            sku_code=sku,
            size=size,
            barcode=barcode,
            goods_type=goods_type,
            qty_reserved=qty,
            status=WarehouseReserve.STATUS_ACTIVE,
        )

    def _snapshot(self, *, sku, barcode, goods_type, qty, code, size="0"):
        box = WarehouseContainer.objects.create(
            agency=self.agency,
            container_code=code,
            container_type=WarehouseContainer.TYPE_BOX,
            current_location=self.location,
        )
        return WarehouseStockSnapshot.objects.create(
            agency=self.agency,
            sku_code=sku,
            size=size,
            barcode=barcode,
            goods_type=goods_type,
            qty=qty,
            available_qty=0,
            container=box,
            container_code=code,
            location=self.location,
            zone_code="OTG",
            zone_kind="shipping",
            warehouse_state_code="ready_for_loading",
        )

    def _arrive(self, snapshots):
        WarehouseWritePathService._mark_shipping_reserves_arrived_to_otg(
            snapshots=list(snapshots),
            order_id=ORDER,
        )

    def test_untyped_reserve_is_satisfied_by_typed_stock(self):
        """Ровно случай OTG-000577: резерв без типа, остаток с типом 'gv'."""
        reserve = self._reserve(
            sku="MICELLAR_WATER_100", barcode="4680175858826", goods_type="", qty=50
        )
        snapshot = self._snapshot(
            sku="MICELLAR_WATER_100",
            barcode="4680175858826",
            goods_type="gv",
            qty=50,
            code="TST-UNTYPED-1",
        )

        self._arrive([snapshot])

        reserve.refresh_from_db()
        self.assertEqual(reserve.status, WarehouseReserve.STATUS_SATISFIED)
        self.assertEqual(reserve.qty_satisfied, 50)

    def test_typed_reserve_still_matches_its_own_type_first(self):
        """Регрессия: типизированный резерв закрывается как раньше."""
        reserve = self._reserve(
            sku="EYE_DUO", barcode="4660406800206", goods_type="gv", qty=30
        )
        snapshot = self._snapshot(
            sku="EYE_DUO",
            barcode="4660406800206",
            goods_type="gv",
            qty=30,
            code="TST-UNTYPED-2",
        )

        self._arrive([snapshot])

        reserve.refresh_from_db()
        self.assertEqual(reserve.status, WarehouseReserve.STATUS_SATISFIED)

    def test_typed_reserve_wins_over_untyped_one(self):
        """Точное совпадение по типу имеет приоритет, запасной путь его не перебивает."""
        typed = self._reserve(
            sku="SAME_SKU", barcode="4600000000001", goods_type="gv", qty=10
        )
        untyped = self._reserve(
            sku="SAME_SKU", barcode="4600000000001", goods_type="", qty=10
        )
        snapshot = self._snapshot(
            sku="SAME_SKU",
            barcode="4600000000001",
            goods_type="gv",
            qty=10,
            code="TST-UNTYPED-3",
        )

        self._arrive([snapshot])

        typed.refresh_from_db()
        untyped.refresh_from_db()
        self.assertEqual(typed.status, WarehouseReserve.STATUS_SATISFIED)
        self.assertEqual(untyped.status, WarehouseReserve.STATUS_ACTIVE)
        self.assertEqual(untyped.qty_satisfied, 0)

    def test_untyped_reserve_of_another_size_is_not_touched(self):
        """Размер остаётся частью ключа: чужой размер закрывать нельзя."""
        reserve = self._reserve(
            sku="SIZED_SKU", barcode="4600000000002", goods_type="", qty=5, size="44"
        )
        snapshot = self._snapshot(
            sku="SIZED_SKU",
            barcode="4600000000002",
            goods_type="gv",
            qty=5,
            code="TST-UNTYPED-4",
            size="46",
        )

        self._arrive([snapshot])

        reserve.refresh_from_db()
        self.assertEqual(reserve.status, WarehouseReserve.STATUS_ACTIVE)
        self.assertEqual(reserve.qty_satisfied, 0)

    def test_untyped_reserve_matches_by_barcode_when_sku_changed(self):
        """Номенклатуру могли переименовать — штрихкод остаётся опорой."""
        reserve = self._reserve(
            sku="OLD_SKU_NAME", barcode="4600000000003", goods_type="", qty=7
        )
        snapshot = self._snapshot(
            sku="NEW_SKU_NAME",
            barcode="4600000000003",
            goods_type="gv",
            qty=7,
            code="TST-UNTYPED-5",
        )

        self._arrive([snapshot])

        reserve.refresh_from_db()
        self.assertEqual(reserve.status, WarehouseReserve.STATUS_SATISFIED)

    def test_reserve_of_another_order_is_never_touched(self):
        """Изоляция по заявке: чужой резерв закрывать нельзя."""
        foreign = WarehouseReserve.objects.create(
            agency=self.agency,
            reserve_type=WarehouseReserve.TYPE_SHIPPING,
            context_type="shipping",
            context_id="OTG-TEST-OTHER",
            sku_code="MICELLAR_WATER_100",
            size="0",
            barcode="4680175858826",
            goods_type="",
            qty_reserved=50,
            status=WarehouseReserve.STATUS_ACTIVE,
        )
        snapshot = self._snapshot(
            sku="MICELLAR_WATER_100",
            barcode="4680175858826",
            goods_type="gv",
            qty=50,
            code="TST-UNTYPED-6",
        )

        self._arrive([snapshot])

        foreign.refresh_from_db()
        self.assertEqual(foreign.status, WarehouseReserve.STATUS_ACTIVE)
