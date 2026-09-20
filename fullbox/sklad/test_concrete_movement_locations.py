from django.core.exceptions import ValidationError
from django.test import TestCase

from sklad.models import WarehouseLocation
from sklad.services.operational_locations import (
    is_concrete_movement_location,
    require_concrete_movement_location,
    select_operational_location,
)
from sklad.services.warehouse_write_path import WarehouseWritePathService


class ConcreteMovementLocationTests(TestCase):
    def location(self, *, zone: str, code: str, **extra) -> WarehouseLocation:
        defaults = {
            "warehouse_code": "MSK",
            "zone_code": zone,
            "zone_kind": WarehouseLocation.ZONE_KIND_RECEIVING,
            "row_no": 0,
            "section_no": 0,
            "tier_no": 0,
            "cell_no": 0,
            "location_code": code,
            "display_name": code,
            "is_active": True,
            "is_topology_visible": False,
            "capacity_containers": 10,
        }
        defaults.update(extra)
        return WarehouseLocation.objects.create(**defaults)

    def test_zone_only_locations_are_rejected(self):
        for zone in ("PR", "OTG", "OS"):
            location = self.location(zone=zone, code=zone)
            self.assertFalse(is_concrete_movement_location(location))
            with self.assertRaisesMessage(ValidationError, f"Общая зона {zone} запрещена"):
                require_concrete_movement_location(location)

    def test_named_operational_location_is_concrete(self):
        location = self.location(zone="PR", code="PR1-1-1")
        self.assertTrue(is_concrete_movement_location(location))
        self.assertEqual(require_concrete_movement_location(location), location)

    def test_otg_destination_uses_named_location_and_never_zone_only(self):
        self.location(zone="OTG", code="OTG")
        self.assertIsNone(select_operational_location(zone_code="OTG"))
        with self.assertRaisesMessage(ValueError, "не настроено конкретное место"):
            WarehouseWritePathService.concrete_movement_destination(
                warehouse_code="MSK",
                zone_code="OTG",
            )

        exact = self.location(zone="OTG", code="OTG1-1-1")
        self.assertEqual(
            WarehouseWritePathService.concrete_movement_destination(
                warehouse_code="MSK",
                zone_code="OTG",
            ),
            exact,
        )

    def test_os_requires_exact_topology_address(self):
        exact = WarehouseWritePathService.concrete_movement_destination(
            warehouse_code="MSK",
            zone_code="OS",
            row_no=10,
            section_no=1,
            tier_no=1,
            cell_no=1,
        )
        self.assertEqual(exact.location_code, "OS-10-1-1-1")
        self.assertTrue(is_concrete_movement_location(exact))
        with self.assertRaisesMessage(ValueError, "Общая зона OS запрещена"):
            WarehouseWritePathService.concrete_movement_destination(
                warehouse_code="MSK",
                zone_code="OS",
            )
