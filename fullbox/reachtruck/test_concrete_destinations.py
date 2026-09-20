from django.test import TestCase

from reachtruck.services.move_requests import (
    _location_label,
    _location_scan_code,
    _normalize_location,
)
from reachtruck.services.task_commands import (
    _destination_from_scan_value,
    _same_location_scan,
)
from sklad.models import WarehouseLocation


class ReachtruckConcreteDestinationTests(TestCase):
    def test_operational_code_survives_task_payload_normalization(self):
        destination = _normalize_location(
            {
                "zone": "OTG",
                "code": "OTG1-1-1",
                "label": "Стол отгрузки 1",
            }
        )
        self.assertEqual(_location_scan_code(destination), "OTG1-1-1")
        self.assertEqual(_location_label(destination), "Стол отгрузки 1")

    def test_generic_zone_scan_is_not_a_destination(self):
        for code in ("PR", "OTG", "OS", "US", "LOC:MSK:OTG"):
            self.assertEqual(_destination_from_scan_value(code), {})

    def test_registered_operational_location_scan_is_resolved(self):
        WarehouseLocation.objects.create(
            warehouse_code="MSK",
            zone_code="OTG",
            zone_kind=WarehouseLocation.ZONE_KIND_SHIPPING,
            row_no=0,
            section_no=0,
            tier_no=0,
            cell_no=0,
            location_code="OTG1-1-1",
            display_name="Стол отгрузки 1",
            is_active=True,
            is_topology_visible=False,
            capacity_containers=10,
        )
        destination = _destination_from_scan_value("OTG1-1-1")
        self.assertEqual(destination["zone"], "OTG")
        self.assertEqual(destination["code"], "OTG1-1-1")
        self.assertEqual(destination["label"], "Стол отгрузки 1")

    def test_printed_operational_location_qr_is_resolved(self):
        WarehouseLocation.objects.create(
            warehouse_code="MSK",
            zone_code="OTG",
            zone_kind=WarehouseLocation.ZONE_KIND_SHIPPING,
            row_no=0,
            section_no=0,
            tier_no=0,
            cell_no=0,
            location_code="OTG-1-1",
            display_name="Отгрузка",
            is_active=True,
            is_topology_visible=False,
            capacity_containers=10,
        )

        for scan_value in ("LOC:MSK:OTG-1-1", "]Q3LOC:MSK:OTG-1-1"):
            destination = _destination_from_scan_value(scan_value)
            self.assertEqual(destination["zone"], "OTG")
            self.assertEqual(destination["code"], "OTG-1-1")
            self.assertEqual(destination["label"], "Отгрузка")
            self.assertTrue(
                _same_location_scan(
                    scan_value,
                    {
                        "zone": "OTG",
                        "code": "OTG-1-1",
                        "label": "Отгрузка",
                    },
                )
            )
