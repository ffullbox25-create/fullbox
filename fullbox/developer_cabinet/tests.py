from __future__ import annotations

from io import BytesIO
from types import SimpleNamespace
import tempfile
from unittest.mock import MagicMock, patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.http import HttpResponse
from django.test import RequestFactory, SimpleTestCase, override_settings
from openpyxl import Workbook

from . import chz_import, views


class ChzImportBarcodeResolutionTests(SimpleTestCase):
    agency = SimpleNamespace(
        id=7,
        agn_name="Клиент Тест",
        short_name="Клиент Тест",
        name="Клиент Тест",
    )
    location = {
        "id": 11,
        "zone_code": "PR",
        "zone_kind": "PR",
        "row": "1",
        "section": "1",
        "tier": "1",
        "cell": "1",
        "location_code": "PR-1-1-1-1",
        "display_name": "PR-1-1-1-1",
        "raw": "PR-1-1-1-1",
        "location_key": "PR-1-1-1-1",
    }

    def _sku(self, **overrides):
        values = {
            "id": 101,
            "agency_id": self.agency.id,
            "sku_code": "SKU-CANONICAL",
            "name": "Каноническое наименование",
            "size": "M",
            "code": "4600000000001",
            "honest_sign": False,
            "deleted": False,
        }
        values.update(overrides)
        return SimpleNamespace(**values)

    def _row(self, *, with_chz=False, **overrides):
        values = {
            "Паллета": "PALLET-1",
            "Короб": "BOX-1",
            "Клиент": self.agency.agn_name,
            "SKU": "",
            "Наименование": "",
            "Ширина, мм": "",
            "Высота, мм": "",
            "Глубина, мм": "",
            "Вес, г": "",
            "Тип товара": "gv",
            "Место": self.location["location_code"],
            "Всего": 1,
            "ШК": "4600000000001",
            "ЧЗ": "010460000000000121ABC" if with_chz else "",
        }
        values.update(overrides)
        return values

    @staticmethod
    def _workbook(headers, row):
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "Остатки"
        sheet.append(list(headers))
        sheet.append([row.get(header, "") for header in headers])
        stream = BytesIO()
        workbook.save(stream)
        stream.seek(0)
        return stream

    def _validate(self, headers, row, *, import_mode, sku, location=None):
        stream = self._workbook(headers, row)
        barcode_key = chz_import._key(row["ШК"])
        with (
            patch.object(chz_import, "_agency_lookup", return_value={chz_import._key(self.agency.agn_name): self.agency}),
            patch.object(chz_import, "_sku_lookup_by_barcode", return_value={barcode_key: [sku]}),
            patch.object(chz_import, "_resolve_location", return_value=location or self.location),
            patch.object(chz_import, "_validate_existing_containers", return_value=set()),
            patch.object(chz_import, "_save_plan") as save_plan,
            patch.object(chz_import.MarkingCode.objects, "filter") as marking_filter,
            patch.object(chz_import.WarehouseStockSnapshot.objects, "filter") as stock_filter,
        ):
            marking_filter.return_value.values_list.return_value = []
            stock_filter.return_value.values_list.return_value = []
            report = chz_import.validate_chz_import_file(stream, import_mode=import_mode)
        return report, save_plan

    def test_header_parser_accepts_legacy_and_barcode_only_templates(self):
        for headers in (chz_import.EXPECTED_HEADERS, chz_import.AUTO_PRODUCT_HEADERS):
            with self.subTest(headers=headers):
                stream = self._workbook(headers, self._row())
                workbook = chz_import.load_workbook(stream, read_only=True, data_only=True)
                self.assertEqual(
                    chz_import._find_header_row(workbook["Остатки"]),
                    (1, list(headers)),
                )

    def test_barcode_only_template_populates_sku_and_name_in_no_chz_mode(self):
        sku = self._sku()
        report, save_plan = self._validate(
            chz_import.AUTO_PRODUCT_HEADERS,
            self._row(),
            import_mode=chz_import.IMPORT_MODE_NO_CHZ,
            sku=sku,
        )

        self.assertTrue(report["ok"], report["errors"])
        self.assertEqual(report["preview_rows"][0]["sku"], sku.sku_code)
        self.assertEqual(report["preview_rows"][0]["name"], sku.name)
        self.assertEqual(report["summary"]["qty_total"], 1)
        save_plan.assert_called_once()

    def test_barcode_only_template_populates_sku_and_name_in_chz_mode(self):
        sku = self._sku(honest_sign=True)
        report, save_plan = self._validate(
            chz_import.AUTO_PRODUCT_HEADERS,
            self._row(with_chz=True),
            import_mode=chz_import.IMPORT_MODE_CHZ,
            sku=sku,
        )

        self.assertTrue(report["ok"], report["errors"])
        self.assertEqual(report["preview_rows"][0]["sku"], sku.sku_code)
        self.assertEqual(report["preview_rows"][0]["chz_count"], 1)
        save_plan.assert_called_once()

    def test_legacy_values_do_not_override_barcode_card(self):
        sku = self._sku()
        report, _save_plan = self._validate(
            chz_import.EXPECTED_HEADERS,
            self._row(SKU="SKU-FROM-FILE", Наименование="Наименование из файла"),
            import_mode=chz_import.IMPORT_MODE_NO_CHZ,
            sku=sku,
        )

        self.assertTrue(report["ok"], report["errors"])
        self.assertEqual(report["preview_rows"][0]["sku"], sku.sku_code)
        self.assertEqual(report["preview_rows"][0]["name"], sku.name)
        self.assertEqual(
            {warning["field"] for warning in report["warnings"]},
            {"SKU", "Наименование"},
        )

    def test_row_resolution_rejects_wrong_client_deleted_and_ambiguous_sku(self):
        row = {
            "row_number": 2,
            "agency_id": self.agency.id,
            "barcode": "4600000000001",
        }
        cases = [
            ("wrong client", [self._sku(agency_id=99)], "другого клиента"),
            ("deleted", [self._sku(deleted=True)], "удаленным SKU"),
            (
                "ambiguous",
                [self._sku(id=101), self._sku(id=102, sku_code="SKU-SECOND")],
                "неоднозначен",
            ),
        ]
        for label, matches, expected_message in cases:
            with self.subTest(label=label):
                report = {"errors": []}
                result = chz_import._resolve_row_sku(
                    row,
                    {chz_import._key(row["barcode"]): matches},
                    report,
                )
                self.assertIsNone(result)
                self.assertIn(expected_message, report["errors"][0]["message"])

    def test_lookup_deduplicates_same_sku_from_code_and_barcode_table(self):
        sku = self._sku()
        sku_manager = MagicMock()
        sku_codes = sku_manager.exclude.return_value.exclude.return_value.values_list.return_value
        sku_codes.iterator.return_value = iter([(sku.id, sku.code)])
        sku_manager.in_bulk.return_value = {sku.id: sku}

        barcode_manager = MagicMock()
        barcode_rows = barcode_manager.values_list.return_value
        barcode_rows.iterator.return_value = iter([(sku.id, sku.code)])

        with (
            patch.object(chz_import.SKU, "objects", sku_manager),
            patch.object(chz_import.SKUBarcode, "objects", barcode_manager),
        ):
            result = chz_import._sku_lookup_by_barcode({sku.code})

        self.assertEqual(result[chz_import._key(sku.code)], [sku])

    def test_validation_reports_non_numeric_location_coordinates(self):
        invalid_location = {
            **self.location,
            "id": "",
            "row": "P",
            "section": "27",
            "tier": "L",
            "cell": "6",
            "location_code": "",
            "display_name": "OS·P·27·L·6",
            "raw": "OS·P·27·L·6",
            "location_key": "OS:P:27:L:6",
        }
        report, save_plan = self._validate(
            chz_import.AUTO_PRODUCT_HEADERS,
            self._row(with_chz=True, Место="OS·P·27·L·6"),
            import_mode=chz_import.IMPORT_MODE_CHZ,
            sku=self._sku(honest_sign=True),
            location=invalid_location,
        )

        self.assertFalse(report["ok"])
        self.assertEqual(report["errors"][0]["row"], 2)
        self.assertEqual(report["errors"][0]["field"], "Место")
        self.assertIn("ряд «P»", report["errors"][0]["message"])
        self.assertIn("ярус «L»", report["errors"][0]["message"])
        save_plan.assert_not_called()

    def test_ready_plan_reports_location_error_before_writing(self):
        errors = chz_import._execution_item_errors(
            [
                {
                    "row_number": 978,
                    "location": {
                        "row": "P",
                        "section": "27",
                        "tier": "L",
                        "cell": "6",
                        "raw": "OS·P·27·L·6",
                    },
                }
            ]
        )

        self.assertEqual(errors[0]["row"], 978)
        self.assertEqual(errors[0]["field"], "Место")
        self.assertIn("ряд «P»", errors[0]["message"])


class ChzImportExistingPalletTests(SimpleTestCase):
    agency_id = 7

    def setUp(self):
        self.location = SimpleNamespace(
            id=11,
            location_code="OS-1-2-3-4",
            display_name="OS · Ряд 1 · Секция 2 · Ярус 3 · Ячейка 4",
            zone_code="OS",
            row_no=1,
            section_no=2,
            tier_no=3,
            cell_no=4,
        )
        self.item = {
            "agency_id": self.agency_id,
            "box_code": "BOX-NEW",
            "pallet_code": "PALLET-EXISTING",
            "location": {
                "id": self.location.id,
                "location_code": self.location.location_code,
                "location_key": self.location.location_code,
            },
        }

    def _container(self, *, code, container_type, agency_id=None, location=None, status=None):
        return SimpleNamespace(
            agency_id=self.agency_id if agency_id is None else agency_id,
            container_code=code,
            container_type=container_type,
            status=status or chz_import.WarehouseContainer.STATUS_ACTIVE,
            current_location=self.location if location is None else location,
            parent_container_id=None,
            parent_container=None,
        )

    @staticmethod
    def _snapshot(*, agency_id, location, pallet_code, reserved=0, active_operation_id=None):
        return SimpleNamespace(
            agency_id=agency_id,
            container_code="BOX-OLD",
            container=SimpleNamespace(container_code="BOX-OLD"),
            parent_container=SimpleNamespace(container_code=pallet_code),
            location=location,
            processing_reserved_qty=reserved,
            shipping_reserved_qty=0,
            other_reserved_qty=0,
            active_operation_id=active_operation_id,
            active_operation_type="placement" if active_operation_id else "",
        )

    def _messages(self, *, containers, snapshots=None, items=None):
        container_qs = MagicMock()
        container_qs.select_related.return_value = containers
        snapshot_qs = MagicMock()
        snapshot_qs.select_related.return_value = snapshots or []
        with (
            patch.object(chz_import.WarehouseContainer.objects, "filter", return_value=container_qs),
            patch.object(chz_import.WarehouseStockSnapshot.objects, "filter", return_value=snapshot_qs),
        ):
            return chz_import._existing_container_messages(
                items or [self.item],
                import_mode=chz_import.IMPORT_MODE_CHZ,
            )

    def test_existing_pallet_accepts_new_boxes_for_same_client_and_location(self):
        pallet = self._container(
            code=self.item["pallet_code"],
            container_type=chz_import.WarehouseContainer.TYPE_PALLET,
        )

        errors, warnings, allowed = self._messages(containers=[pallet])

        self.assertEqual(errors, [])
        self.assertEqual(allowed, {self.item["pallet_code"]})
        self.assertIn("товар будет добавлен в него", warnings[0]["message"])

    def test_existing_box_remains_blocked_in_chz_mode(self):
        existing_box = self._container(
            code=self.item["box_code"],
            container_type=chz_import.WarehouseContainer.TYPE_BOX,
        )

        errors, warnings, allowed = self._messages(containers=[existing_box])

        self.assertEqual(warnings, [])
        self.assertEqual(allowed, set())
        self.assertIn("можно использовать повторно только существующую паллету", errors[0]["message"])

    def test_existing_pallet_of_another_client_is_blocked(self):
        pallet = self._container(
            code=self.item["pallet_code"],
            container_type=chz_import.WarehouseContainer.TYPE_PALLET,
            agency_id=99,
        )

        errors, _warnings, allowed = self._messages(containers=[pallet])

        self.assertEqual(allowed, set())
        self.assertIn("принадлежит другому клиенту", errors[0]["message"])

    def test_existing_pallet_at_another_location_is_blocked(self):
        another_location = SimpleNamespace(
            id=12,
            location_code="OS-9-9-9-9",
            display_name="OS · Ряд 9 · Секция 9 · Ярус 9 · Ячейка 9",
            zone_code="OS",
            row_no=9,
            section_no=9,
            tier_no=9,
            cell_no=9,
        )
        pallet = self._container(
            code=self.item["pallet_code"],
            container_type=chz_import.WarehouseContainer.TYPE_PALLET,
            location=another_location,
        )

        errors, _warnings, allowed = self._messages(containers=[pallet])

        self.assertEqual(allowed, set())
        self.assertIn("стоит на другом месте", errors[0]["message"])

    def test_existing_pallet_with_reserve_or_operation_is_blocked(self):
        pallet = self._container(
            code=self.item["pallet_code"],
            container_type=chz_import.WarehouseContainer.TYPE_PALLET,
        )
        snapshots = [
            self._snapshot(
                agency_id=self.agency_id,
                location=self.location,
                pallet_code=self.item["pallet_code"],
                reserved=1,
                active_operation_id=88,
            )
        ]

        errors, _warnings, allowed = self._messages(containers=[pallet], snapshots=snapshots)

        self.assertEqual(allowed, set())
        self.assertTrue(any("активный резерв" in item["message"] for item in errors))
        self.assertTrue(any("складская операция" in item["message"] for item in errors))

    def test_snapshot_rows_are_locked_by_id_without_outer_join(self):
        container_lock_qs = MagicMock()
        container_lock_qs.filter.return_value.order_by.return_value.values_list.return_value = []
        snapshot_id_qs = MagicMock()
        snapshot_id_qs.order_by.return_value.values_list.return_value = [12, 11, 12]
        snapshot_lock_qs = MagicMock()
        snapshot_lock_qs.filter.return_value.order_by.return_value.values_list.return_value = [11, 12]

        with (
            patch.object(
                chz_import.WarehouseContainer.objects,
                "select_for_update",
                return_value=container_lock_qs,
            ),
            patch.object(
                chz_import.WarehouseStockSnapshot.objects,
                "filter",
                return_value=snapshot_id_qs,
            ),
            patch.object(
                chz_import.WarehouseStockSnapshot.objects,
                "select_for_update",
                return_value=snapshot_lock_qs,
            ),
        ):
            chz_import._lock_import_containers([self.item])

        snapshot_lock_qs.filter.assert_called_once_with(id__in=[11, 12])


class ChzImportViewTests(SimpleTestCase):
    def test_unexpected_validation_error_is_rendered_instead_of_http_500(self):
        request = RequestFactory().post(
            "/dev/chz-import/",
            {"action": "validate", "import_mode": chz_import.IMPORT_MODE_CHZ},
        )
        request.FILES["file"] = SimpleUploadedFile(
            "broken.xlsx",
            b"not-an-excel-file",
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        request.user = SimpleNamespace(
            is_authenticated=True,
            username="dev",
            is_superuser=True,
        )

        with (
            patch.object(views, "_is_developer_request", return_value=True),
            patch.object(views, "validate_chz_import_file", side_effect=RuntimeError("Ошибка проверки")),
            patch.object(views, "render", return_value=HttpResponse("ok")) as render_mock,
        ):
            response = views.chz_import(request)

        self.assertEqual(response.status_code, 200)
        context = render_mock.call_args.args[2]
        self.assertFalse(context["report"]["ok"])
        self.assertIn("Ошибка проверки", context["report"]["errors"][0]["message"])

    def test_unexpected_import_error_is_rendered_instead_of_http_500(self):
        request = RequestFactory().post(
            "/dev/chz-import/",
            {"action": "import", "token": "a" * 32},
        )
        request.user = SimpleNamespace(
            is_authenticated=True,
            username="dev",
            is_superuser=True,
        )

        with (
            patch.object(views, "_is_developer_request", return_value=True),
            patch.object(views, "execute_chz_import", side_effect=RuntimeError("Тестовая причина")),
            patch.object(views, "render", return_value=HttpResponse("ok")) as render_mock,
        ):
            response = views.chz_import(request)

        self.assertEqual(response.status_code, 200)
        context = render_mock.call_args.args[2]
        self.assertFalse(context["import_result"]["ok"])
        self.assertIn("Тестовая причина", context["import_result"]["errors"][0]["message"])


class ChzImportExecutionLockTests(SimpleTestCase):
    def test_parallel_execution_of_same_token_is_rejected_before_writes(self):
        token = "a" * 32
        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            with chz_import._plan_execution_lock(token) as acquired:
                self.assertTrue(acquired)
                result = chz_import.execute_chz_import(token)

        self.assertFalse(result["ok"])
        self.assertIn("уже выполняется", result["errors"][0]["message"])
