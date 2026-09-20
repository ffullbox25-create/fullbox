from types import SimpleNamespace

from django.test import SimpleTestCase

from fbs.services.reachtruck_bridge import _is_concrete_container_location
from sklad.models import WarehouseLocation


def _location(
    *,
    zone_code: str,
    location_code: str,
    row_no: int = 0,
    section_no: int = 0,
    tier_no: int = 0,
    cell_no: int = 0,
):
    return SimpleNamespace(
        zone_code=zone_code,
        zone_kind=WarehouseLocation.ZONE_KIND_STORAGE,
        location_code=location_code,
        row_no=row_no,
        section_no=section_no,
        tier_no=tier_no,
        cell_no=cell_no,
    )


class ConcreteContainerLocationTests(SimpleTestCase):
    def test_named_mr_location_is_concrete_by_code(self):
        location = _location(zone_code="MR", location_code="C-D")

        self.assertTrue(_is_concrete_container_location(location))

    def test_os_location_without_coordinates_is_not_concrete(self):
        location = _location(zone_code="OS", location_code="OS")

        self.assertFalse(_is_concrete_container_location(location))

    def test_named_ab_floor_location_is_concrete_by_scan_code(self):
        location = _location(zone_code="OS", location_code="A-B")

        self.assertTrue(_is_concrete_container_location(location))

    def test_fbs_location_without_coordinates_is_not_concrete(self):
        location = _location(zone_code="FBS", location_code="FBS")

        self.assertFalse(_is_concrete_container_location(location))

    def test_os_location_with_complete_coordinates_is_concrete(self):
        location = _location(
            zone_code="OS",
            location_code="OS-3-4-2-2",
            row_no=3,
            section_no=4,
            tier_no=2,
            cell_no=2,
        )

        self.assertTrue(_is_concrete_container_location(location))

    def test_other_zone_without_location_code_is_not_concrete(self):
        location = _location(zone_code="MR", location_code="")

        self.assertFalse(_is_concrete_container_location(location))
