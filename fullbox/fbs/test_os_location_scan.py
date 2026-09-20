from django.test import TestCase

from fbs.services.replenishment import _resolve_active_os_location
from sklad.models import WarehouseLocation


class FbsOsLocationScanTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.location = WarehouseLocation.objects.create(
            warehouse_code="MSK",
            zone_code="OS",
            zone_kind="storage",
            row_no=9,
            section_no=3,
            tier_no=1,
            cell_no=3,
            location_code="B-9/1-3",
            display_name="OS · Линия B · Стеллаж 9 · Этаж 1 · Ячейка 3",
            is_active=True,
            is_pickable=True,
            is_storage=True,
        )

    def test_resolves_os_scan_with_aim_prefix(self):
        self.assertEqual(
            _resolve_active_os_location("]C1B-9/1-3"),
            self.location,
        )

    def test_resolves_os_scan_with_cyrillic_b(self):
        self.assertEqual(
            _resolve_active_os_location("]C1В-9/1-3"),
            self.location,
        )
