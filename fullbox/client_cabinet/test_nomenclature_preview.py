"""Preview/commit загрузки номенклатуры в ЛК (без мгновенной записи)."""
from __future__ import annotations

from datetime import timedelta
from io import BytesIO
from unittest.mock import patch

from audit.models import AuditEntry
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, connection
from django.test import Client, TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from openpyxl import Workbook

from sku.models import Agency, SKU, SKUBarcode
from .nomenclature_upload import commit_sku_template_rows, parse_sku_template_preview

User = get_user_model()


def _xlsx_bytes(rows):
    wb = Workbook()
    ws = wb.active
    for row in rows:
        ws.append(row)
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


class NomenclaturePreviewCommitTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="nom_preview_client", password="pwd")
        self.other_user = User.objects.create_user(username="nom_preview_other", password="pwd")
        self.agency = Agency.objects.create(
            agn_name="Номенклатура Preview",
            portal_user=self.user,
            short_name="NomPrev",
        )
        self.other_agency = Agency.objects.create(
            agn_name="Чужой клиент",
            portal_user=self.other_user,
            short_name="Other",
        )
        self.client = Client()
        self.client.force_login(self.user)

    def test_upload_returns_preview_without_writing(self):
        payload = _xlsx_bytes([
            ["Артикул Заказчика", "Предмет", "Бренд", "Размер", "Баркод"],
            ["PREV-1", "Куртка", "BrandX", "M", "4600000000001"],
        ])
        upload = SimpleUploadedFile(
            "sku.xlsx",
            payload,
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        before = SKU.objects.filter(agency=self.agency, deleted=False).count()
        response = self.client.post(
            f"/client/api/v1/nomenclature/upload/?client={self.agency.id}",
            {"file": upload},
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["ok"])
        rows = body["data"]["rows"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["sku_code"], "PREV-1")
        self.assertEqual(rows[0]["status"], "new")
        self.assertIn("Совпадений с вашим каталогом нет", body["data"]["message"])
        self.assertEqual(SKU.objects.filter(agency=self.agency, deleted=False).count(), before)

    def test_upload_skips_title_row_and_lists_existing_codes(self):
        SKU.objects.create(agency=self.agency, sku_code="PREV-3", name="Старое", brand="Old")
        payload = _xlsx_bytes([
            ["Шаблон номенклатуры для клиента"],
            ["Артикул Заказчика", "Предмет", "Бренд", "Размер", "Баркод"],
            ["PREV-3", "Новое имя", "NewBrand", "S", ""],
            ["PREV-4", "Новый товар", "NewBrand", "M", ""],
        ])
        upload = SimpleUploadedFile(
            "sku.xlsx",
            payload,
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        response = self.client.post(
            f"/client/api/v1/nomenclature/upload/?client={self.agency.id}",
            {"file": upload},
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertEqual(len(data["rows"]), 2)
        by_code = {row["sku_code"]: row for row in data["rows"]}
        self.assertEqual(by_code["PREV-3"]["status"], "update")
        self.assertEqual(by_code["PREV-4"]["status"], "new")
        self.assertIn("PREV-3", data["message"])

    def test_upload_marks_foreign_barcode_conflict(self):
        foreign = SKU.objects.create(agency=self.other_agency, sku_code="OTHER-1", name="Чужой")
        SKUBarcode.objects.create(sku=foreign, value="4600000000099", is_primary=True)
        payload = _xlsx_bytes([
            ["Артикул Заказчика", "Предмет", "Бренд", "Размер", "Баркод"],
            ["PREV-5", "Товар", "Brand", "M", "4600000000099"],
        ])
        upload = SimpleUploadedFile(
            "sku.xlsx",
            payload,
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        response = self.client.post(
            f"/client/api/v1/nomenclature/upload/?client={self.agency.id}",
            {"file": upload},
        )
        self.assertEqual(response.status_code, 200)
        row = response.json()["data"]["rows"][0]
        self.assertEqual(row["status"], "conflict")
        self.assertIn("OTHER-1", row["warning"])
        self.assertIn("Конфликт штрихкода", response.json()["data"]["message"])

    def test_commit_saves_edited_rows(self):
        response = self.client.post(
            f"/client/api/v1/nomenclature/commit/?client={self.agency.id}",
            data={
                "rows": [
                    {
                        "sku_code": "PREV-2",
                        "name": "Пальто",
                        "brand": "BrandY",
                        "size": "L",
                        "barcode": "4600000000002",
                    }
                ]
            },
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["data"]["created"], 1)
        sku = SKU.objects.get(agency=self.agency, sku_code="PREV-2", deleted=False)
        self.assertEqual(sku.name, "Пальто")
        self.assertEqual(sku.brand, "BrandY")
        self.assertTrue(SKUBarcode.objects.filter(sku=sku, value="4600000000002").exists())
        self.assertEqual(sku.code, "4600000000002")
        self.assertTrue(SKUBarcode.objects.get(sku=sku, value="4600000000002").is_primary)
        self.assertEqual(body["data"]["results"][0]["barcode_action"], "created_primary")

    def test_preview_existing_sku_requires_explicit_barcode_mode(self):
        sku = SKU.objects.create(
            agency=self.agency,
            sku_code="PREV-PRIMARY-1",
            code="4600000000100",
            name="Товар",
        )
        SKUBarcode.objects.create(sku=sku, value="4600000000100", is_primary=True)
        upload = SimpleUploadedFile(
            "sku.xlsx",
            _xlsx_bytes([
                ["Артикул Заказчика", "Предмет", "Бренд", "Размер", "Баркод"],
                ["PREV-PRIMARY-1", "Товар", "Brand", "M", "4600000000101"],
            ]),
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

        response = self.client.post(
            f"/client/api/v1/nomenclature/upload/?client={self.agency.id}",
            {"file": upload},
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        row = data["rows"][0]
        self.assertEqual(row["current_primary_barcode"], "4600000000100")
        self.assertEqual(row["expected_primary_barcode"], "4600000000100")
        self.assertTrue(row["requires_barcode_mode"])
        self.assertEqual(row["barcode_mode"], "")
        self.assertEqual(data["counts"]["barcode_choice_required"], 1)

    def test_commit_existing_sku_rejects_missing_barcode_mode(self):
        sku = SKU.objects.create(
            agency=self.agency,
            sku_code="PREV-PRIMARY-2",
            code="4600000000200",
            name="Товар",
        )
        SKUBarcode.objects.create(sku=sku, value="4600000000200", is_primary=True)

        response = self.client.post(
            f"/client/api/v1/nomenclature/commit/?client={self.agency.id}",
            data={
                "rows": [{
                    "sku_code": sku.sku_code,
                    "name": sku.name,
                    "barcode": "4600000000201",
                    "expected_primary_barcode": "4600000000200",
                }]
            },
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("выберите", response.json()["error"].lower())
        sku.refresh_from_db()
        self.assertEqual(sku.code, "4600000000200")
        self.assertFalse(SKUBarcode.objects.filter(value="4600000000201").exists())

    def test_commit_replace_primary_preserves_old_alias_and_writes_audit(self):
        sku = SKU.objects.create(
            agency=self.agency,
            sku_code="PREV-PRIMARY-3",
            code="4600000000300",
            name="Товар",
        )
        old_barcode = SKUBarcode.objects.create(sku=sku, value="4600000000300", is_primary=True)
        old_updated_at = timezone.now() - timedelta(days=1)
        SKU.objects.filter(pk=sku.pk).update(updated_at=old_updated_at)

        response = self.client.post(
            f"/client/api/v1/nomenclature/commit/?client={self.agency.id}",
            data={
                "rows": [{
                    "sku_code": sku.sku_code,
                    "name": sku.name,
                    "barcode": "4600000000301",
                    "expected_primary_barcode": "4600000000300",
                    "barcode_mode": "replace_primary",
                }]
            },
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        sku.refresh_from_db()
        old_barcode.refresh_from_db()
        new_barcode = SKUBarcode.objects.get(sku=sku, value="4600000000301")
        self.assertEqual(sku.code, "4600000000301")
        self.assertGreater(sku.updated_at, old_updated_at)
        self.assertFalse(old_barcode.is_primary)
        self.assertTrue(new_barcode.is_primary)
        self.assertEqual(SKUBarcode.objects.filter(sku=sku).count(), 2)
        result = response.json()["data"]["results"][0]
        self.assertEqual(result["barcode_action"], "replaced_primary")
        self.assertTrue(result["old_primary_preserved"])
        audit = AuditEntry.objects.filter(sku=sku, action="update").latest("created_at")
        self.assertEqual(audit.user, self.user)
        self.assertEqual(audit.snapshot["before"]["code"], "4600000000300")
        self.assertEqual(audit.snapshot["after"]["code"], "4600000000301")

    def test_commit_add_secondary_keeps_primary_and_legacy_code(self):
        sku = SKU.objects.create(
            agency=self.agency,
            sku_code="PREV-PRIMARY-4",
            code="4600000000400",
            name="Товар",
        )
        primary = SKUBarcode.objects.create(sku=sku, value="4600000000400", is_primary=True)

        response = self.client.post(
            f"/client/api/v1/nomenclature/commit/?client={self.agency.id}",
            data={
                "rows": [{
                    "sku_code": sku.sku_code,
                    "name": sku.name,
                    "barcode": "4600000000401",
                    "expected_primary_barcode": "4600000000400",
                    "barcode_mode": "add_secondary",
                }]
            },
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        sku.refresh_from_db()
        primary.refresh_from_db()
        secondary = SKUBarcode.objects.get(sku=sku, value="4600000000401")
        self.assertEqual(sku.code, "4600000000400")
        self.assertTrue(primary.is_primary)
        self.assertFalse(secondary.is_primary)
        result = response.json()["data"]["results"][0]
        self.assertEqual(result["barcode_action"], "added_secondary")
        self.assertEqual(result["primary_barcode"], "4600000000400")

    def test_commit_rejects_stale_primary_without_partial_changes(self):
        sku = SKU.objects.create(
            agency=self.agency,
            sku_code="PREV-PRIMARY-5",
            code="4600000000501",
            name="Товар",
        )
        SKUBarcode.objects.create(sku=sku, value="4600000000500", is_primary=False)
        SKUBarcode.objects.create(sku=sku, value="4600000000501", is_primary=True)

        response = self.client.post(
            f"/client/api/v1/nomenclature/commit/?client={self.agency.id}",
            data={
                "rows": [{
                    "sku_code": sku.sku_code,
                    "name": "Изменение не должно сохраниться",
                    "barcode": "4600000000502",
                    "expected_primary_barcode": "4600000000500",
                    "barcode_mode": "replace_primary",
                }]
            },
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("изменился после предпросмотра", response.json()["error"])
        sku.refresh_from_db()
        self.assertEqual(sku.name, "Товар")
        self.assertEqual(sku.code, "4600000000501")
        self.assertFalse(SKUBarcode.objects.filter(value="4600000000502").exists())

    def test_commit_integrity_race_returns_clear_error_and_rolls_back(self):
        sku = SKU.objects.create(
            agency=self.agency,
            sku_code="PREV-PRIMARY-6",
            code="4600000000600",
            name="Товар",
        )
        SKUBarcode.objects.create(sku=sku, value="4600000000600", is_primary=True)

        with patch(
            "client_cabinet.nomenclature_upload.SKUBarcode.objects.create",
            side_effect=IntegrityError("duplicate barcode"),
        ):
            response = self.client.post(
                f"/client/api/v1/nomenclature/commit/?client={self.agency.id}",
                data={
                    "rows": [{
                        "sku_code": sku.sku_code,
                        "name": "Не должно сохраниться",
                        "barcode": "4600000000601",
                        "expected_primary_barcode": "4600000000600",
                        "barcode_mode": "replace_primary",
                    }]
                },
                content_type="application/json",
            )

        self.assertEqual(response.status_code, 400)
        self.assertIn("Сохранение полностью отменено", response.json()["error"])
        sku.refresh_from_db()
        self.assertEqual(sku.name, "Товар")
        self.assertEqual(sku.code, "4600000000600")
        self.assertFalse(SKUBarcode.objects.filter(value="4600000000601").exists())

    def test_upload_generates_internal_barcode_when_empty(self):
        payload = _xlsx_bytes([
            ["Артикул Заказчика", "Предмет", "Бренд", "Размер", "Баркод"],
            ["NO-BC-1", "Товар без ШК", "Brand", "M", ""],
        ])
        upload = SimpleUploadedFile(
            "sku.xlsx",
            payload,
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        response = self.client.post(
            f"/client/api/v1/nomenclature/upload/?client={self.agency.id}",
            {"file": upload},
        )
        self.assertEqual(response.status_code, 200)
        row = response.json()["data"]["rows"][0]
        self.assertEqual(row["sku_code"], "NO-BC-1")
        self.assertTrue(row["barcode"])
        self.assertTrue(row["barcode"].startswith("29"))
        self.assertTrue(row["barcode_generated"])
        self.assertEqual(response.json()["data"]["counts"]["generated_barcodes"], 1)

    def test_commit_creates_sku_with_generated_barcode(self):
        response = self.client.post(
            f"/client/api/v1/nomenclature/commit/?client={self.agency.id}",
            data={
                "rows": [
                    {
                        "sku_code": "NO-BC-2",
                        "name": "Без ШК",
                        "barcode": "",
                    }
                ]
            },
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["ok"])
        self.assertGreaterEqual(body["data"]["generated_barcodes"], 1)
        sku = SKU.objects.get(agency=self.agency, sku_code="NO-BC-2", deleted=False)
        self.assertTrue(SKUBarcode.objects.filter(sku=sku).exists())
        bc = SKUBarcode.objects.get(sku=sku)
        self.assertTrue(bc.value.startswith("29"))

    def test_preview_many_rows_without_barcodes_uses_bulk_checks(self):
        rows = [["Артикул Заказчика", "Предмет", "Бренд", "Размер", "Баркод"]]
        rows.extend([f"BULK-PREV-{idx}", f"Товар {idx}", "Brand", "0", ""] for idx in range(80))
        upload = SimpleUploadedFile(
            "sku_many.xlsx",
            _xlsx_bytes(rows),
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

        with CaptureQueriesContext(connection) as queries:
            data = parse_sku_template_preview(upload, self.agency)

        self.assertEqual(data["counts"]["total"], 80)
        self.assertEqual(data["counts"]["generated_barcodes"], 80)
        self.assertTrue(all(row["barcode"].startswith("29") for row in data["rows"]))
        self.assertLess(len(queries), 25)

    def test_commit_many_rows_without_barcodes_uses_bulk_checks(self):
        rows = [
            {
                "sku_code": f"BULK-COMMIT-{idx}",
                "name": f"Товар {idx}",
                "brand": "Brand",
                "size": "0",
                "barcode": "",
            }
            for idx in range(80)
        ]

        with CaptureQueriesContext(connection) as queries:
            result = commit_sku_template_rows(self.agency, rows)

        self.assertEqual(result["created"], 80)
        self.assertEqual(result["generated_barcodes"], 80)
        self.assertEqual(
            SKU.objects.filter(agency=self.agency, sku_code__startswith="BULK-COMMIT-").count(),
            80,
        )
        self.assertEqual(
            SKUBarcode.objects.filter(
                sku__agency=self.agency,
                sku__sku_code__startswith="BULK-COMMIT-",
            ).count(),
            80,
        )
        # На каждый новый SKU теперь обязательно пишется отдельный audit before/after.
        # Контроль оставляем линейным и ограниченным, чтобы не вернуть прежний N+1 preview.
        self.assertLess(len(queries), 450)

    def test_upload_marks_duplicate_barcode_inside_file(self):
        payload = _xlsx_bytes([
            ["Артикул Заказчика", "Предмет", "Бренд", "Размер", "Баркод"],
            ["61-1", "Товар 1", "Brand", "M", "4670246670259"],
            ["61-2", "Товар 2", "Brand", "L", "4670246670259"],
        ])
        upload = SimpleUploadedFile(
            "sku.xlsx",
            payload,
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        response = self.client.post(
            f"/client/api/v1/nomenclature/upload/?client={self.agency.id}",
            {"file": upload},
        )
        self.assertEqual(response.status_code, 200)
        rows = response.json()["data"]["rows"]
        by_code = {row["sku_code"]: row for row in rows}
        self.assertEqual(by_code["61-1"]["status"], "new")
        self.assertEqual(by_code["61-2"]["status"], "conflict")
        self.assertIn("повторяется в файле", by_code["61-2"]["warning"])
        self.assertIn("61-1", by_code["61-2"]["warning"])

    def test_commit_rejects_duplicate_barcode_inside_payload(self):
        response = self.client.post(
            f"/client/api/v1/nomenclature/commit/?client={self.agency.id}",
            data={
                "rows": [
                    {"sku_code": "61-1", "name": "A", "barcode": "4670246670259"},
                    {"sku_code": "61-2", "name": "B", "barcode": "4670246670259"},
                ]
            },
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("повторяется в файле", response.json()["error"])
        self.assertFalse(SKU.objects.filter(agency=self.agency, sku_code__in=["61-1", "61-2"]).exists())

    def test_commit_rejects_foreign_barcode(self):
        foreign = SKU.objects.create(agency=self.other_agency, sku_code="OTHER-2", name="Чужой")
        SKUBarcode.objects.create(sku=foreign, value="4600000000088", is_primary=True)
        response = self.client.post(
            f"/client/api/v1/nomenclature/commit/?client={self.agency.id}",
            data={
                "rows": [
                    {
                        "sku_code": "PREV-6",
                        "name": "Товар",
                        "barcode": "4600000000088",
                    }
                ]
            },
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(SKU.objects.filter(agency=self.agency, sku_code="PREV-6").exists())

    def test_preview_same_new_sku_marks_later_barcode_as_secondary(self):
        upload = SimpleUploadedFile(
            "sku.xlsx",
            _xlsx_bytes([
                ["Артикул Заказчика", "Предмет", "Бренд", "Размер", "Баркод"],
                ["MULTI-1", "Товар", "Brand", "M", "4600000010001"],
                ["MULTI-1", "Товар", "Brand", "L", "4600000010002"],
            ]),
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

        response = self.client.post(
            f"/client/api/v1/nomenclature/upload/?client={self.agency.id}",
            {"file": upload},
        )

        self.assertEqual(response.status_code, 200)
        rows = response.json()["data"]["rows"]
        self.assertEqual(rows[0]["barcode_mode"], "")
        self.assertEqual(rows[1]["barcode_mode"], "add_secondary")
        self.assertEqual(rows[1]["expected_primary_barcode"], "4600000010001")
        self.assertIn("Дополнительный ШК", rows[1]["warning"])

    def test_commit_same_new_sku_saves_primary_and_secondary_barcodes(self):
        response = self.client.post(
            f"/client/api/v1/nomenclature/commit/?client={self.agency.id}",
            data={
                "rows": [
                    {
                        "sku_code": "MULTI-2",
                        "name": "Товар",
                        "size": "M",
                        "barcode": "4600000020001",
                    },
                    {
                        "sku_code": "MULTI-2",
                        "name": "Товар",
                        "size": "L",
                        "barcode": "4600000020002",
                    },
                ]
            },
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertEqual(data["created"], 1)
        self.assertEqual(data["updated"], 0)
        sku = SKU.objects.get(agency=self.agency, sku_code="MULTI-2", deleted=False)
        self.assertEqual(sku.code, "4600000020001")
        self.assertEqual(sku.size, "M")
        self.assertEqual(
            list(sku.barcodes.order_by("value").values_list("value", "size", "is_primary")),
            [("4600000020001", "M", True), ("4600000020002", "L", False)],
        )
        self.assertEqual(data["results"][1]["barcode_action"], "added_secondary")
