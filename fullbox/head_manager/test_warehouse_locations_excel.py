from io import BytesIO
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import RequestFactory, SimpleTestCase
from openpyxl import Workbook

from head_manager.web_ui import (
    HeadManagerWarehouseLocationsView,
    _warehouse_location_qr_value,
    _warehouse_locations_import_rows,
)
from sklad.services.operational_locations import normalize_operational_location_scan


def _workbook_upload(rows):
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["Зона", "Код места", "Название", "Вместимость", "Видно в FBS"])
    for row in rows:
        sheet.append(row)
    output = BytesIO()
    workbook.save(output)
    workbook.close()
    return SimpleUploadedFile(
        "locations.xlsx",
        output.getvalue(),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


class WarehouseLocationsExcelTests(SimpleTestCase):
    def test_reads_pr_obr_and_otg_rows(self):
        rows = _warehouse_locations_import_rows(
            _workbook_upload(
                [
                    ["PR", "PR-DOP-01", "Приёмка", 10, "Да"],
                    ["OBR", "OBR-DOP-01", "Обработка", 20, "Нет"],
                    ["OTG", "OTG-DOP-01", "Отгрузка", 30, "1"],
                ]
            )
        )

        self.assertEqual([row["zone_code"] for row in rows], ["PR", "OBR", "OTG"])
        self.assertEqual([row["capacity_containers"] for row in rows], [10, 20, 30])
        self.assertEqual([row["is_fbs_visible"] for row in rows], [True, False, True])

    def test_rejects_unknown_fbs_value(self):
        with self.assertRaisesMessage(ValidationError, "Да или Нет"):
            _warehouse_locations_import_rows(
                _workbook_upload([["PR", "PR-DOP-01", "Приёмка", 10, "иногда"]])
            )


class WarehouseLocationQrTests(SimpleTestCase):
    def _location(self, code="PR-F-07", zone="PR", pk=1):
        return SimpleNamespace(
            id=pk, warehouse_code="MSK", zone_code=zone,
            location_code=code, is_active=True, is_fbs_visible=True,
            fbs_rack=None,
        )

    def test_pr_payload_is_the_plain_address(self):
        self.assertEqual(_warehouse_location_qr_value(self._location()), "PR-F-07")

    def test_other_zones_keep_the_existing_payload(self):
        for zone in ("OBR", "OTG", "OS"):
            with self.subTest(zone=zone):
                self.assertEqual(
                    _warehouse_location_qr_value(self._location(f"{zone}-01", zone)),
                    f"LOC:MSK:{zone}-01",
                )

    def test_existing_and_new_labels_resolve_to_the_same_address(self):
        for scan in ("PR-F-07", "LOC:MSK:PR-F-07", "]Q3LOC:MSK:PR-F-07"):
            with self.subTest(scan=scan):
                self.assertEqual(normalize_operational_location_scan(scan), "PR-F-07")

    def _context(self, rows, params):
        query = Mock()
        for method in ("filter", "distinct", "select_related", "prefetch_related"):
            getattr(query, method).return_value = query
        query.order_by.return_value = rows
        view = HeadManagerWarehouseLocationsView()
        view.setup(RequestFactory().get("/head-manager/settings/warehouse-locations/", params))
        with patch("head_manager.web_ui.WarehouseLocation.objects.filter", return_value=query), patch(
            "head_manager.web_ui.operational_location_occupancy",
            return_value=SimpleNamespace(occupied=0),
        ), patch("head_manager.web_ui._head_manager_user_name", return_value="Test"):
            return view.get_context_data()

    def test_single_print_uses_plain_pr_address(self):
        context = self._context([self._location()], {"print": "1"})
        self.assertEqual(context["selected_location"].qr_value, "PR-F-07")

    def test_bulk_print_changes_only_pr(self):
        context = self._context(
            [self._location(), self._location("OTG-01", "OTG", 2)],
            {"print_selected": ["1", "2"]},
        )
        self.assertEqual(
            [row.qr_value for row in context["selected_locations"]],
            ["PR-F-07", "LOC:MSK:OTG-01"],
        )

    def test_rack_cell_print_uses_plain_pr_address(self):
        location = self._location()
        cell = SimpleNamespace(
            is_active=True,
            storage_cell=SimpleNamespace(is_active=True, location=self._location("PR-F-07-01")),
        )
        location.fbs_rack = SimpleNamespace(cells=Mock())
        location.fbs_rack.cells.all.return_value = [cell]
        context = self._context([location], {"print_rack": "1"})
        self.assertEqual(context["rack_print_cells"][0].qr_value, "PR-F-07-01")
