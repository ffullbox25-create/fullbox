from datetime import date
from decimal import Decimal
from io import BytesIO

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from openpyxl import Workbook

from employees.models import Employee
from sku.models import Agency, SKU, SKUBarcode

from .fbs_kp_upload import calculate_kp_upload, parse_receiving_act_text
from .models import ApplicationCharge, FbsClientRate


class FbsKpUploadParserTests(SimpleTestCase):
    def test_parse_receiving_act_text_handles_wrapped_product_name(self):
        rows = parse_receiving_act_text(
            """
            АКТ ПРИЕМА-ПЕРЕДАЧИ товарно-материальных ценностей на хранение № 58
            ОТ 04.08.2026
            № ТОВАР ШТРИХ-КОД АРТИКУЛ КОЛ-ВО
            ПЛАН
            КОЛ-ВО
            ФАКТ
            ЗАЯВЛЕННАЯ СТОИМОСТЬ ЕДИНИЦЫ ТОВАРА
            1 Вешалка для брюк многоуровневая трансформер
            (вешалка006)
            2041357606413 456291907 105 105 Не указана
            ВСЕГО: 105 шт. (план: 105 шт.)
            2. Основанием для приемки является: Задача на прием товара № 28
            """,
            file_name="act-58.pdf",
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].source_number, "ACT-58 / PR-28")
        self.assertEqual(rows[0].service_date, date(2026, 8, 4))
        self.assertEqual(rows[0].sku_code, "вешалка006")
        self.assertEqual(rows[0].barcode, "2041357606413")
        self.assertEqual(rows[0].quantity, Decimal("105.000"))


class FbsKpUploadCalculationTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="fbs-kp-manager")
        Employee.objects.create(user=self.user, full_name="Менеджер FBS КП", role="manager")
        self.client_agency = Agency.objects.create(
            agn_name="Клиент FBS КП",
            mened_user_id=self.user.id,
        )
        self.sku = SKU.objects.create(
            agency=self.client_agency,
            sku_code="SKU-KP",
            name="Товар КП",
            length_mm=100,
            width_mm=100,
            height_mm=100,
        )
        SKUBarcode.objects.create(sku=self.sku, value="4600000000001", is_primary=True)
        for operation, price in (
            (FbsClientRate.OP_PICKING, "20"),
            (FbsClientRate.OP_SHIPPING, "30"),
            (FbsClientRate.OP_MARKING, "5"),
        ):
            FbsClientRate.objects.create(
                client=self.client_agency,
                operation=operation,
                liters_from=0,
                liters_to=2,
                price=price,
                valid_from=date(2026, 8, 1),
                vat_rate="5",
            )

    def _shipping_report(self):
        workbook = Workbook()
        sheet = workbook.active
        sheet.append(["Дата", "Товар", "Артикул", "ШК", "WB", "OZON", "Yandex", "ВСЕГО"])
        sheet.append([date(2026, 8, 4), "Товар КП", "SKU-KP", "4600000000001", 0, 3, 0, 3])
        stream = BytesIO()
        workbook.save(stream)
        stream.seek(0)
        return stream

    def test_calculate_kp_upload_is_read_only_for_billing_charges(self):
        result = calculate_kp_upload(
            client=self.client_agency,
            receiving_files=[],
            shipping_report_file=self._shipping_report(),
            date_from=date(2026, 8, 1),
            date_to=date(2026, 8, 10),
        )

        self.assertEqual(result["summary"]["error_rows"], 0)
        self.assertEqual(result["summary"]["amount"], Decimal("165.00"))
        self.assertEqual(result["summary"]["vat_amount"], Decimal("8.25"))
        self.assertEqual(result["summary"]["total_amount"], Decimal("173.25"))
        self.assertEqual(ApplicationCharge.objects.count(), 0)

    def test_manager_can_open_kp_upload_page(self):
        self.client.force_login(self.user)
        response = self.client.get("/team-manager/billing/fbs/kp-upload/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "FBS расчет по КП из файлов")
        self.assertContains(response, "PDF-акты приемки")
