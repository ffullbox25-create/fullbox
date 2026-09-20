from datetime import date, datetime
from decimal import Decimal
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase
from openpyxl import load_workbook

from .fbs_period_billing import (
    FbsClientRate,
    OPERATION_LABELS,
    SERVICE_BY_OPERATION,
    ShipmentRow,
    _pick_scan_quantities,
    _source_fingerprint,
    _unique_chz_quantities_by_item,
    build_period_workbook,
    fbs_applied_label_quantity,
    generate_period_invoice,
    generate_period_report,
)
from .fbs_standard_shipping import FBS_MARKING_LABEL


class _Client:
    pk = 77
    inn = "7700000000"

    def __str__(self):
        return "Тестовый клиент"


def _rate(pk, operation, price, liters_from="0", liters_to=None, unit="шт"):
    return SimpleNamespace(
        pk=pk,
        id=pk,
        operation=operation,
        price=Decimal(price),
        liters_from=Decimal(liters_from),
        liters_to=Decimal(liters_to) if liters_to is not None else None,
        vat_rate="5",
        vat_type="extra",
        unit=unit,
    )


class FbsPeriodWorkbookTests(SimpleTestCase):
    def setUp(self):
        self.receiving_rate = _rate(1, FbsClientRate.OP_RECEIVING, "2", "0", "1")
        self.picking_rate = _rate(2, FbsClientRate.OP_PICKING, "5", "0", "1")
        self.marking_rate = _rate(3, FbsClientRate.OP_MARKING, "5")
        self.chz_rate = _rate(4, FbsClientRate.OP_CHZ_CHECK, "5")
        self.shipping_rate = _rate(5, FbsClientRate.OP_SHIPPING, "3.76", "0", "1")
        self.storage_rate = _rate(6, FbsClientRate.OP_STORAGE, "0.16")
        self.sku = SimpleNamespace(
            pk=10,
            sku_code="SKU-10",
            name="Товар 10",
            honest_sign=True,
            length_mm=100,
            width_mm=100,
            height_mm=100,
        )
        self.detail = ShipmentRow(
            service_date=date(2026, 8, 17),
            marketplace="wb",
            external_sku="MP-10",
            product_name="Товар 10",
            sku=self.sku,
            quantity=Decimal("2"),
            marking_quantity=Decimal("2"),
            chz_quantity=Decimal("2"),
            barcodes={"460000000010"},
            liters=Decimal("1"),
            picking_rate=self.picking_rate,
            marking_rate=self.marking_rate,
            chz_rate=self.chz_rate,
            shipping_rate=self.shipping_rate,
        )
        self.groups = [
            self._group(FbsClientRate.OP_RECEIVING, self.receiving_rate, "3", "6"),
            self._group(FbsClientRate.OP_PICKING, self.picking_rate, "2", "10"),
            self._group(FbsClientRate.OP_MARKING, self.marking_rate, "2", "10"),
            self._group(FbsClientRate.OP_CHZ_CHECK, self.chz_rate, "2", "10"),
            self._group(FbsClientRate.OP_SHIPPING, self.shipping_rate, "2", "7.52"),
            self._group(FbsClientRate.OP_STORAGE, self.storage_rate, "8", "1.28"),
        ]
        self.operation_totals = {
            group["operation"]: {
                "quantity": group["quantity"],
                "amount": group["amount"],
                "vat_rate": "5",
                "vat_type": "extra",
            }
            for group in self.groups
        }

    @staticmethod
    def _group(operation, rate, quantity, amount):
        return {
            "operation": operation,
            "operation_label": OPERATION_LABELS[operation],
            "rate": rate,
            "rate_label": (
                "за штуку"
                if operation in (FbsClientRate.OP_MARKING, FbsClientRate.OP_CHZ_CHECK)
                else "0-1 л"
            ),
            "quantity": Decimal(quantity),
            "amount": Decimal(amount),
        }

    def _data(self):
        storage_rows = []
        storage_daily = []
        balance = Decimal("5")
        for day, incoming, shipped in (
            (date(2026, 8, 17), Decimal("3"), Decimal("2")),
            (date(2026, 8, 18), Decimal("0"), Decimal("1")),
        ):
            start = balance
            balance = start + incoming - shipped
            liters = balance
            amount = liters * self.storage_rate.price
            storage_rows.append(
                {
                    "date": day,
                    "sku": self.sku,
                    "liters": Decimal("1"),
                    "start_qty": start,
                    "incoming": incoming,
                    "shipped": shipped,
                    "end_qty": balance,
                    "liters_due": liters,
                    "rate": self.storage_rate,
                    "amount": amount,
                    "overdraw": Decimal("0"),
                }
            )
            storage_daily.append(
                {
                    "date": day,
                    "incoming": incoming,
                    "shipped": shipped,
                    "end_qty": balance,
                    "liters": liters,
                    "billable_quantity": liters,
                    "rate": self.storage_rate,
                    "amount": amount,
                }
            )
        invoice = SimpleNamespace(
            vat_rate_snapshot="5",
            vat_amount=Decimal("2.24"),
            total_amount=Decimal("47.04"),
        )
        return {
            "client": _Client(),
            "invoice": invoice,
            "date_from": date(2026, 8, 17),
            "date_to": date(2026, 8, 18),
            "batches": [SimpleNamespace(pk=100)],
            "orders_count": 1,
            "details": [self.detail],
            "groups": self.groups,
            "operation_totals": self.operation_totals,
            "receiving_movement_ids": [200],
            "reconciliation": [
                {
                    "sku": self.sku,
                    "name": self.sku.name,
                    "liters": Decimal("1"),
                    "incoming": Decimal("3"),
                    "shipped": Decimal("2"),
                    "receiving_amount": Decimal("6"),
                    "marking_amount": Decimal("10"),
                    "chz_amount": Decimal("10"),
                    "picking_amount": Decimal("10"),
                    "shipping_amount": Decimal("7.52"),
                }
            ],
            "storage_daily": storage_daily,
            "storage_rows": storage_rows,
            "storage_mode": "liters",
            "storage_service_code": "fbs_storage_liter_day",
            "storage_unit": "литро-дн.",
            "quantity": Decimal("2"),
            "received_quantity": Decimal("3"),
            "services_subtotal": Decimal("43.52"),
            "services_vat": Decimal("2.18"),
            "storage_subtotal": Decimal("1.28"),
            "storage_liter_days": Decimal("8"),
            "storage_quantity_days": Decimal("8"),
            "subtotal": Decimal("44.80"),
            "vat_amount": Decimal("2.24"),
            "total_amount": Decimal("47.04"),
        }

    def test_report_has_exact_six_sheets_and_expected_columns(self):
        workbook = load_workbook(BytesIO(build_period_workbook(self._data())), data_only=True)

        self.assertEqual(
            workbook.sheetnames,
            [
                "Итоги",
                "Услуги по КП",
                "Отгрузка детально",
                "Сверка SKU",
                "Хранение",
                "Хранение детально",
            ],
        )
        self.assertEqual(workbook["Отгрузка детально"].max_column, 17)
        self.assertEqual(workbook["Сверка SKU"].max_column, 10)
        self.assertEqual(workbook["Хранение"].max_column, 10)
        self.assertEqual(workbook["Хранение детально"].max_column, 12)
        self.assertEqual(workbook["Итоги"]["F14"].value, 47.04)
        self.assertEqual(workbook["Хранение"]["J5"].value, 1.28)

    def test_report_workbook_does_not_require_an_invoice(self):
        data = self._data()
        data.pop("invoice")

        workbook = load_workbook(BytesIO(build_period_workbook(data)), data_only=True)

        self.assertEqual(workbook["Итоги"]["F14"].value, 47.04)

    def test_report_generation_never_creates_an_invoice(self):
        data = self._data()
        data.pop("invoice")
        with (
            patch("billing.fbs_period_billing.collect_period_data", return_value=data),
            patch("billing.fbs_period_billing.build_period_workbook", return_value=b"xlsx"),
            patch("billing.fbs_period_billing.create_period_invoice") as create_invoice,
        ):
            result = generate_period_report(
                client=data["client"],
                date_from=data["date_from"],
                date_to=data["date_to"],
            )

        self.assertEqual(result["workbook"], b"xlsx")
        create_invoice.assert_not_called()

    def test_invoice_generation_is_separate_from_workbook(self):
        data = self._data()
        data.pop("invoice")
        invoice = SimpleNamespace(number="INV-TEST")
        application = SimpleNamespace(pk=123)
        with (
            patch("billing.fbs_period_billing.collect_period_data", return_value=data),
            patch(
                "billing.fbs_period_billing.create_period_invoice",
                return_value=(application, invoice, True),
            ),
            patch("billing.fbs_period_billing.build_period_workbook") as build_workbook,
        ):
            result = generate_period_invoice(
                client=data["client"],
                date_from=data["date_from"],
                date_to=data["date_to"],
                user=None,
            )

        self.assertIs(result["application"], application)
        self.assertIs(result["invoice"], invoice)
        self.assertTrue(result["invoice_created"])
        self.assertNotIn("workbook", result)
        build_workbook.assert_not_called()

    def test_fingerprint_changes_when_storage_changes(self):
        first = self._data()
        second = self._data()
        second["storage_daily"][0]["liters"] = Decimal("99")

        self.assertNotEqual(_source_fingerprint(first), _source_fingerprint(second))

    def test_invoice_services_cover_receiving_and_storage(self):
        self.assertEqual(SERVICE_BY_OPERATION[FbsClientRate.OP_RECEIVING], "fbs_receiving_goods")
        self.assertEqual(
            SERVICE_BY_OPERATION[FbsClientRate.OP_CHZ_CHECK],
            "fbs_honest_sign_check",
        )
        self.assertEqual(SERVICE_BY_OPERATION[FbsClientRate.OP_STORAGE], "fbs_storage_liter_day")

    def test_chz_counts_unique_confirmed_identity_per_order(self):
        short_code = "0104610469110859215c0rZU"
        full_code = f"{short_code}91EE1192signature"
        rows = [
            {"order_id": 1, "order_item_id": 11, "marking_code": full_code},
            {"order_id": 1, "order_item_id": 11, "marking_code": short_code},
            {"order_id": 1, "order_item_id": 12, "marking_code": "legacy-code"},
            {"order_id": 2, "order_item_id": 21, "marking_code": full_code},
        ]

        self.assertEqual(
            _unique_chz_quantities_by_item(rows),
            {11: 1, 12: 1, 21: 1},
        )

    def test_each_successful_controller_pick_scan_is_one_marking_unit(self):
        rows = [
            {"item_id": 11, "created_at": datetime(2026, 9, 15, 8, 0)},
            {"item_id": 11, "created_at": datetime(2026, 9, 15, 8, 1)},
            {"item_id": 12, "created_at": datetime(2026, 9, 15, 8, 2)},
            {"item_id": None, "created_at": datetime(2026, 9, 15, 8, 3)},
        ]

        by_day_item, by_item = _pick_scan_quantities(rows)

        self.assertEqual(by_day_item[(date(2026, 9, 15), 11)], Decimal("2"))
        self.assertEqual(by_day_item[(date(2026, 9, 15), 12)], Decimal("1"))
        self.assertEqual(by_item, {11: Decimal("2"), 12: Decimal("1")})

    def test_detail_amount_keeps_marking_and_chz_separate(self):
        self.assertEqual(self.detail.amount, Decimal("37.52"))

    def test_marking_is_billed_once_only_for_applied_order_label(self):
        labels = SimpleNamespace(all=lambda: [
            SimpleNamespace(status="canceled"),
            SimpleNamespace(status="applied"),
            SimpleNamespace(status="applied"),
        ])
        no_applied_labels = SimpleNamespace(all=lambda: [SimpleNamespace(status="ready")])

        self.assertEqual(
            fbs_applied_label_quantity(order=SimpleNamespace(marketplace_labels=labels)),
            Decimal("1"),
        )
        self.assertEqual(
            fbs_applied_label_quantity(
                order=SimpleNamespace(marketplace_labels=no_applied_labels)
            ),
            Decimal("0"),
        )
        self.assertEqual(
            OPERATION_LABELS[FbsClientRate.OP_MARKING],
            FBS_MARKING_LABEL,
        )

    def test_pallet_storage_workbook_uses_pallet_days(self):
        data = self._data()
        pallet_rate = _rate(55, FbsClientRate.OP_STORAGE, "65", unit="Палета")
        data.update(
            {
                "storage_mode": "pallets",
                "storage_service_code": "fbs_storage_pallet_day",
                "storage_unit": "паллето-дн.",
                "storage_quantity_days": Decimal("2"),
                "storage_liter_days": Decimal("0"),
                "storage_subtotal": Decimal("130"),
                "storage_daily": [
                    {
                        "date": date(2026, 8, 17),
                        "incoming": Decimal("10"),
                        "shipped": Decimal("0"),
                        "end_qty": Decimal("10"),
                        "liters": Decimal("0"),
                        "billable_quantity": Decimal("1"),
                        "rate": pallet_rate,
                        "amount": Decimal("65"),
                    },
                    {
                        "date": date(2026, 8, 18),
                        "incoming": Decimal("0"),
                        "shipped": Decimal("2"),
                        "end_qty": Decimal("8"),
                        "liters": Decimal("0"),
                        "billable_quantity": Decimal("1"),
                        "rate": pallet_rate,
                        "amount": Decimal("65"),
                    },
                ],
                "storage_rows": [
                    {
                        "date": date(2026, 8, 17),
                        "pallet_code": "FBS-PAL-001",
                        "name": "Паллетное хранение FBS",
                        "liters": Decimal("0"),
                        "start_qty": Decimal("0"),
                        "incoming": Decimal("10"),
                        "shipped": Decimal("0"),
                        "end_qty": Decimal("10"),
                        "billable_quantity": Decimal("1"),
                        "liters_due": Decimal("1"),
                        "rate": pallet_rate,
                        "amount": Decimal("65"),
                        "overdraw": Decimal("0"),
                    }
                ],
            }
        )

        workbook = load_workbook(BytesIO(build_period_workbook(data)), data_only=True)

        self.assertEqual(workbook["Хранение"]["E1"].value, "Паллето-мест")
        self.assertEqual(workbook["Хранение"]["I4"].value, "Паллето-дней")
        self.assertEqual(workbook["Хранение"]["J4"].value, 2)
        self.assertEqual(workbook["Хранение детально"]["B1"].value, "Паллета")
        self.assertEqual(workbook["Хранение детально"]["B2"].value, "FBS-PAL-001")
