"""Manager invoice storage report regression tests."""

from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase
from django.utils import timezone

from billing.models import (
    ApplicationCharge,
    BillingAct,
    BillingActLine,
    BillingApplication,
    BillingService,
    BillingStorageDay,
    ClientInvoice,
    StorageSnapshotLine,
)
from billing.statuses import ActStatus, InvoiceStatus
from billing.storage_invoice_report import build_storage_invoice_report, storage_report_invoice_ids
from billing.views import BillingInvoiceDetailView
from sku.models import Agency


User = get_user_model()


class ManagerStorageInvoiceReportTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_superuser("storage_report_manager", "manager@example.com", "pwd")
        cls.agency = Agency.objects.create(agn_name="Клиент хранения менеджера", short_name="Хранение")
        cls.service = BillingService.objects.create(
            code="manager_storage_report_test",
            name="Хранение палето-день",
            unit="палето-день",
        )
        cls.application = BillingApplication.objects.create(
            application_type=BillingApplication.TYPE_STORAGE,
            application_id="STORAGE-MANAGER-REPORT",
            client=cls.agency,
            legal_entity=cls.agency,
        )
        cls.day = timezone.localdate() - timedelta(days=1)
        cls.charge = ApplicationCharge.objects.create(
            application=cls.application,
            client=cls.agency,
            legal_entity=cls.agency,
            service=cls.service,
            quantity=Decimal("4.000"),
            unit="палето-день",
            tariff=Decimal("25.0000"),
            amount=Decimal("100.00"),
            total_amount=Decimal("100.00"),
            source_type=ApplicationCharge.SOURCE_STORAGE_DAY,
            source_key=f"storage:{cls.agency.id}:{cls.day.isoformat()}",
            billing_period=cls.day.replace(day=1),
        )
        cls.storage_day = BillingStorageDay.objects.create(
            client=cls.agency,
            application=cls.application,
            day=cls.day,
            pallet_count=4,
            box_count=3,
            sku_unit_count=20,
            zone_counts={"OS": 4},
            payload={
                "container_snapshot_version": 1,
                "pallet_box_counts": {"PAL-MANAGER-1": 3},
                "source_box_count": 3,
            },
            charge=cls.charge,
        )
        StorageSnapshotLine.objects.create(
            day=cls.storage_day,
            pallet_code="PAL-MANAGER-1",
            box_code="BOX-MANAGER-1",
            sku_code="SKU-MANAGER-1",
            barcode="4600000000099",
            name="Товар менеджерского отчёта",
            quantity=Decimal("20.000"),
            zone_code="OS",
            cell_code="A-1-1",
        )
        cls.act = BillingAct.objects.create(
            application=cls.application,
            client=cls.agency,
            legal_entity=cls.agency,
            number="ACT-MANAGER-STORAGE",
            act_date=cls.day,
            status=ActStatus.DRAFT,
            total_amount=Decimal("100.00"),
        )
        BillingActLine.objects.create(
            act=cls.act,
            charge=cls.charge,
            service_name=cls.service.name,
            quantity=Decimal("4.000"),
            unit="палето-день",
            tariff=Decimal("25.0000"),
            amount=Decimal("100.00"),
            total_amount=Decimal("100.00"),
        )
        cls.invoice = ClientInvoice.objects.create(
            application=cls.application,
            client=cls.agency,
            legal_entity=cls.agency,
            act=cls.act,
            number="INV-MANAGER-STORAGE",
            billing_period=cls.day.replace(day=1),
            invoice_date=cls.day,
            due_date=cls.day + timedelta(days=7),
            total_amount=Decimal("100.00"),
            debt_amount=Decimal("100.00"),
            status=InvoiceStatus.DRAFT,
        )

    def test_draft_invoice_has_manager_storage_report(self):
        self.assertEqual(storage_report_invoice_ids([self.invoice]), {self.invoice.id})
        report = build_storage_invoice_report(self.invoice)
        self.assertEqual(report["storage_total"], "100.00")
        self.assertEqual(report["pallet_days"], 4)
        self.assertEqual(report["page_obj"].paginator.count, 1)

    def test_manager_invoice_detail_renders_storage_contents(self):
        request = RequestFactory().get(
            f"/team-manager/billing/invoices/{self.invoice.id}/?storage_report=1"
        )
        request.user = self.user
        response = BillingInvoiceDetailView.as_view()(request, pk=self.invoice.id)
        response.render()
        self.assertEqual(response.status_code, 200)
        content = response.content.decode("utf-8")
        self.assertIn("Отчёт по выставленной услуге хранения", content)
        self.assertIn("PAL-MANAGER-1", content)
        self.assertIn("BOX-MANAGER-1", content)
        self.assertIn("Коробов в палете", content)
        self.assertIn(">3</strong>", content)

    def test_m3_invoice_report_uses_volume_metrics_and_explains_formula(self):
        self.storage_day.billing_mode = "m3_day"
        self.storage_day.physical_volume_m3 = Decimal("1.000000")
        self.storage_day.billable_volume_m3 = Decimal("10.000000")
        self.storage_day.payload = {
            "weight_volume_m3": "10.000000",
            "no_dims_count": 0,
        }
        self.storage_day.save(
            update_fields=[
                "billing_mode",
                "physical_volume_m3",
                "billable_volume_m3",
                "payload",
            ]
        )
        self.charge.quantity = Decimal("10.000")
        self.charge.unit = "м3"
        self.charge.amount = Decimal("250.00")
        self.charge.total_amount = Decimal("250.00")
        self.charge.save(update_fields=["quantity", "unit", "amount", "total_amount"])
        BillingActLine.objects.filter(charge=self.charge).update(
            quantity=Decimal("10.000"),
            unit="м3",
            tariff=Decimal("25.0000"),
            amount=Decimal("250.00"),
            total_amount=Decimal("250.00"),
        )
        StorageSnapshotLine.objects.filter(day=self.storage_day).update(
            length_mm=600,
            width_mm=400,
            height_mm=400,
            total_volume_m3=Decimal("0.096000"),
            billable_volume_m3=Decimal("0.096000"),
            coefficient=Decimal("1.000000"),
        )

        report = build_storage_invoice_report(self.invoice)
        self.assertTrue(report["is_m3_report"])
        self.assertEqual(report["billed_quantity_total"], "10.000")
        self.assertEqual(report["selected_physical_m3"], "1.000")
        self.assertEqual(report["selected_billable_m3"], "10.000")
        self.assertTrue(report["day_rows"][0]["box_count_available"])
        self.assertTrue(report["day_rows"][0]["volume_needs_attention"])

        request = RequestFactory().get(
            f"/team-manager/billing/invoices/{self.invoice.id}/?storage_report=1"
        )
        request.user = self.user
        response = BillingInvoiceDetailView.as_view()(request, pk=self.invoice.id)
        response.render()
        content = response.content.decode("utf-8")
        self.assertIn("Отчёт по хранению в кубических метрах", content)
        self.assertIn("Начисленный объём", content)
        self.assertIn("Физ. объём дня, м³", content)
        self.assertIn("Весовой объём дня, м³", content)
        self.assertIn("Проверьте весовые данные", content)
        self.assertIn("600 × 400 × 400 мм", content)
