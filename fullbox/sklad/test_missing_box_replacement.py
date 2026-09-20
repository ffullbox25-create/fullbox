from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from sku.models import Agency
from sklad.models import (
    WarehouseContainer,
    WarehouseLocation,
    WarehouseOperation,
    WarehouseReserve,
    WarehouseStockSnapshot,
)
from sklad.services.missing_box_replacement import (
    find_exact_free_box_replacement,
    quarantine_missing_box,
)


class MissingBoxReplacementSearchTests(TestCase):
    def setUp(self):
        self.agency = Agency.objects.create(agn_name="Клиент поиска замены")
        self.location = WarehouseLocation.objects.create(
            warehouse_code="MSK",
            zone_code="OS",
            zone_kind=WarehouseLocation.ZONE_KIND_STORAGE,
            row_no=1,
            section_no=1,
            tier_no=1,
            cell_no=1,
            location_code="A-1/1-1",
            display_name="A-1/1-1",
            is_active=True,
            is_storage=True,
        )
        self.pallet = WarehouseContainer.objects.create(
            agency=self.agency,
            container_type=WarehouseContainer.TYPE_PALLET,
            container_code="PAL-SEARCH-1",
            current_location=self.location,
        )

    def _create_box(self, code: str, *, sku_code: str, qty: int = 60):
        box = WarehouseContainer.objects.create(
            agency=self.agency,
            container_type=WarehouseContainer.TYPE_BOX,
            container_code=code,
            parent_container=self.pallet,
            current_location=self.location,
        )
        WarehouseStockSnapshot.objects.create(
            agency=self.agency,
            source_context_type="receiving",
            source_context_id=code,
            sku_code=sku_code,
            name="Тестовый товар",
            size="M",
            barcode=f"BC-{sku_code}",
            goods_type="Готовый",
            qty=qty,
            available_qty=qty,
            container=box,
            container_code=code,
            parent_container=self.pallet,
            location=self.location,
            zone_code="OS",
            zone_kind=WarehouseLocation.ZONE_KIND_STORAGE,
            warehouse_state_code="stored",
        )
        return box

    def test_search_batches_large_candidate_set_and_returns_exact_box(self):
        missing = self._create_box("BOX-MISSING", sku_code="TARGET")
        for index in range(60):
            self._create_box(f"BOX-OTHER-{index:03d}", sku_code=f"OTHER-{index:03d}")
        replacement = self._create_box("BOX-REPLACEMENT", sku_code="TARGET")

        with CaptureQueriesContext(connection) as queries:
            result = find_exact_free_box_replacement(
                agency_id=self.agency.id,
                missing_container_id=missing.id,
            )

        self.assertIsNotNone(result)
        self.assertEqual(result.container.id, replacement.id)
        self.assertLessEqual(
            len(queries),
            8,
            "Поиск замены не должен выполнять запросы отдельно для каждого короба.",
        )

    def test_quarantine_missing_box_creates_compatible_reserve(self):
        missing = self._create_box("BOX-NO-REPLACEMENT", sku_code="UNIQUE")

        operation = quarantine_missing_box(
            container_id=missing.id,
            agency_id=self.agency.id,
            context_type="reachtruck_move",
            context_id="move-1",
        )

        self.assertEqual(operation.status, WarehouseOperation.STATUS_BLOCKED)
        reserve = WarehouseReserve.objects.get(
            context_type="missing_box_check",
            context_id=str(operation.id),
        )
        self.assertEqual(reserve.qty_reserved, 60)
        self.assertEqual(reserve.qty_allocated, 60)
        snapshot = WarehouseStockSnapshot.objects.get(container=missing)
        self.assertEqual(snapshot.available_qty, 0)
        self.assertEqual(snapshot.other_reserved_qty, 60)
