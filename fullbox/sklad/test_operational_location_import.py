from django.core.exceptions import ValidationError
from django.test import TestCase

from sklad.models import WarehouseLocation
from sklad.services.operational_locations import (
    create_operational_location,
    create_operational_locations,
)


class OperationalLocationImportTests(TestCase):
    def test_obr_location_is_physical_processing_location(self):
        location = create_operational_location(
            zone_code="OBR",
            location_code="OBR-DOP-01",
            display_name="Обработка 1",
            capacity_containers=20,
            is_fbs_visible=False,
        )

        self.assertEqual(location.zone_kind, WarehouseLocation.ZONE_KIND_PROCESSING)
        self.assertTrue(location.is_processing)
        self.assertFalse(location.is_fbs_visible)
        self.assertFalse(location.is_topology_visible)

    def test_obr_location_rejects_fbs_flag(self):
        with self.assertRaisesMessage(ValidationError, "OBR"):
            create_operational_location(
                zone_code="OBR",
                location_code="OBR-DOP-02",
                display_name="Обработка 2",
                capacity_containers=20,
                is_fbs_visible=True,
            )

    def test_bulk_import_rolls_back_all_rows_on_duplicate(self):
        rows = [
            {
                "zone_code": "PR",
                "location_code": "PR-DOP-11",
                "display_name": "Приёмка 11",
                "capacity_containers": 10,
                "is_fbs_visible": False,
            },
            {
                "zone_code": "OTG",
                "location_code": "PR-DOP-11",
                "display_name": "Отгрузка 11",
                "capacity_containers": 10,
                "is_fbs_visible": False,
            },
        ]

        with self.assertRaisesMessage(ValidationError, "повторяется"):
            create_operational_locations(rows)

        self.assertFalse(
            WarehouseLocation.objects.filter(location_code="PR-DOP-11").exists()
        )
