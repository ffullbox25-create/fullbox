"""Тесты MVP приёмки с распределением — без склада/ШК/резервов."""
from __future__ import annotations

from io import BytesIO

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase
from openpyxl import Workbook

from sku.models import Agency, SKU

from .services.create import create_receiving_distribution
from .services.excel import parse_distribution_excel
from .services.validate import DistributionDraft, validate_distribution_draft
from .models import ReceivingDistributionPlan
from shipping.models import ShippingOrder


def _xlsx(rows):
    wb = Workbook()
    ws = wb.active
    for row in rows:
        ws.append(row)
    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


class ValidateDistributionTests(SimpleTestCase):
    def test_qty_must_match(self):
        draft = DistributionDraft(
            items=[{"sku_code": "A", "qty_total": 100}],
            directions=[{"key": "wb", "kind": "marketplace", "marketplace_name": "WB", "destination_warehouse": "K"}],
            allocations=[{"sku_code": "A", "direction_key": "wb", "qty": 60}],
        )
        issues = validate_distribution_draft(draft, allow_incomplete=False)
        self.assertTrue(any(i.code == "qty_mismatch" for i in issues))

    def test_storage_ok(self):
        from .services.validate import has_blocking_errors

        draft = DistributionDraft(
            items=[{"sku_code": "A", "qty_total": 100}],
            directions=[
                {
                    "key": "wb",
                    "kind": "marketplace",
                    "marketplace_name": "WB",
                    "destination_warehouse": "K",
                    "supply_number": "S1",
                },
                {"key": "storage_fullbox", "kind": "storage_fullbox", "title": "Хранение FullBox"},
            ],
            allocations=[
                {"sku_code": "A", "direction_key": "wb", "qty": 70},
                {"sku_code": "A", "direction_key": "storage_fullbox", "qty": 30},
            ],
        )
        issues = validate_distribution_draft(draft)
        self.assertFalse(has_blocking_errors(issues), [i.message for i in issues])
        self.assertEqual(issues, [])


class ExcelAndCreateTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(username="rd_client", password="pass")
        self.agency = Agency.objects.create(agn_name="RD Client", short_name="RD", inn="7700999888")
        self.sku = SKU.objects.create(agency=self.agency, sku_code="ART-001", name="Товар 1")

    def test_excel_preview_and_create_linked_shipping_drafts(self):
        file_obj = _xlsx(
            [
                [
                    "Артикул",
                    "Штрихкод товара",
                    "Наименование",
                    "Общее количество",
                    "Маркетплейс",
                    "Склад назначения",
                    "Номер поставки",
                    "Количество по направлению",
                    "Дата поставки",
                    "Таймслот",
                    "Комментарий",
                ],
                ["ART-001", "111", "Товар 1", 1000, "WB", "Коледино", "S1", 600, "", "", ""],
                ["ART-001", "111", "Товар 1", 1000, "WB", "Электросталь", "S2", 250, "", "", ""],
                ["ART-001", "111", "Товар 1", 1000, "Хранение FullBox", "", "", 150, "", "", ""],
            ]
        )
        known = {self.sku.sku_code.casefold()}
        preview = parse_distribution_excel(file_obj, known_sku_codes=known)
        self.assertTrue(preview.ok, [i.message for i in preview.issues])
        self.assertEqual(len(preview.draft.directions), 3)
        preview.draft.meta = {"eta_at": "2030-01-15T12:00"}

        result = create_receiving_distribution(
            agency=self.agency,
            draft=preview.draft,
            user=self.user,
            submit=True,
            client_request_id="req-1",
        )
        self.assertTrue(result.created)
        self.assertTrue(result.receiving_order_id.startswith("PR-"))
        plan = ReceivingDistributionPlan.objects.get(receiving_order_id=result.receiving_order_id)
        self.assertEqual(plan.items.count(), 1)
        self.assertEqual(plan.directions.count(), 3)
        storage = plan.directions.get(kind="storage_fullbox")
        self.assertIsNone(storage.shipping_order_id)
        ships = list(plan.directions.exclude(kind="storage_fullbox"))
        self.assertEqual(len(ships), 2)
        self.assertEqual(len(result.shipping_numbers), 2)
        for d in ships:
            self.assertIsNotNone(d.shipping_order_id)
            so = d.shipping_order
            self.assertEqual(so.status, ShippingOrder.STATUS_DRAFT)
            self.assertIn(result.receiving_order_id, so.comment)

        # Идемпотентность
        again = create_receiving_distribution(
            agency=self.agency,
            draft=preview.draft,
            user=self.user,
            submit=True,
            client_request_id="req-1",
        )
        self.assertFalse(again.created)
        self.assertEqual(again.receiving_order_id, result.receiving_order_id)
        self.assertEqual(ReceivingDistributionPlan.objects.filter(agency=self.agency).count(), 1)

    def test_unknown_sku_rejected(self):
        draft = DistributionDraft(
            meta={"eta_at": "2030-01-15T12:00"},
            items=[{"sku_code": "NOPE", "qty_total": 10}],
            directions=[{"key": "wb", "kind": "marketplace", "marketplace_name": "WB", "destination_warehouse": "K"}],
            allocations=[{"sku_code": "NOPE", "direction_key": "wb", "qty": 10}],
        )
        with self.assertRaises(ValidationError):
            create_receiving_distribution(agency=self.agency, draft=draft, user=self.user, submit=True)

    def test_lifecycle_confirm_and_accept_progress(self):
        from receiving_distribution.services.lifecycle import (
            on_receiving_confirmed_to_warehouse,
            record_accepted_for_direction,
        )
        from receiving_distribution.services.progress import build_distribution_panel

        draft = DistributionDraft(
            meta={"eta_at": "2030-01-15T12:00"},
            items=[{"sku_code": "ART-001", "qty_total": 100, "name": "Товар 1"}],
            directions=[
                {
                    "key": "wb",
                    "kind": "marketplace",
                    "marketplace_name": "WB",
                    "destination_warehouse": "Коледино",
                    "title": "WB Коледино",
                },
                {"key": "storage_fullbox", "kind": "storage_fullbox", "title": "Хранение FullBox"},
            ],
            allocations=[
                {"sku_code": "ART-001", "direction_key": "wb", "qty": 80},
                {"sku_code": "ART-001", "direction_key": "storage_fullbox", "qty": 20},
            ],
        )
        result = create_receiving_distribution(
            agency=self.agency, draft=draft, user=self.user, submit=True, client_request_id="req-life"
        )
        self.assertTrue(on_receiving_confirmed_to_warehouse(order_id=result.receiving_order_id, user=self.user))
        plan = ReceivingDistributionPlan.objects.get(receiving_order_id=result.receiving_order_id)
        self.assertEqual(plan.status, ReceivingDistributionPlan.STATUS_AWAITING_RECEIVING)
        direction = plan.directions.get(kind="marketplace")
        out = record_accepted_for_direction(
            receiving_order_id=result.receiving_order_id,
            direction_id=direction.id,
            sku_code="ART-001",
            qty=50,
            user=self.user,
        )
        self.assertEqual(out["qty_accepted"], 50)
        panel = build_distribution_panel(result.receiving_order_id)
        self.assertIsNotNone(panel)
        wb = next(d for d in panel.directions if d.id == direction.id)
        self.assertEqual(wb.qty_accepted, 50)
        self.assertEqual(wb.qty_remaining, 30)
        self.assertEqual(wb.shipping_status_label, "Ожидает приемки")
        with self.assertRaises(ValidationError):
            record_accepted_for_direction(
                receiving_order_id=result.receiving_order_id,
                direction_id=direction.id,
                sku_code="ART-001",
                qty=40,
                user=self.user,
            )

    def test_sync_accepted_from_flow_boxes_absolute(self):
        from receiving_distribution.services.lifecycle import sync_accepted_from_flow_boxes

        draft = DistributionDraft(
            meta={"eta_at": "2030-01-15T12:00"},
            items=[{"sku_code": "ART-001", "qty_total": 100, "name": "Товар 1"}],
            directions=[
                {
                    "key": "wb",
                    "kind": "marketplace",
                    "marketplace_name": "WB",
                    "destination_warehouse": "Коледино",
                    "title": "WB Коледино",
                },
                {"key": "storage_fullbox", "kind": "storage_fullbox", "title": "Хранение FullBox"},
            ],
            allocations=[
                {"sku_code": "ART-001", "direction_key": "wb", "qty": 80},
                {"sku_code": "ART-001", "direction_key": "storage_fullbox", "qty": 20},
            ],
        )
        result = create_receiving_distribution(
            agency=self.agency, draft=draft, user=self.user, submit=True, client_request_id="req-sync"
        )
        plan = ReceivingDistributionPlan.objects.get(receiving_order_id=result.receiving_order_id)
        wb = plan.directions.get(kind="marketplace")
        storage = plan.directions.get(kind="storage_fullbox")
        sync_accepted_from_flow_boxes(
            receiving_order_id=result.receiving_order_id,
            boxes=[
                {
                    "code": "BX1",
                    "distribution_direction_id": wb.id,
                    "items": [{"sku_code": "ART-001", "qty": 50}],
                },
                {
                    "code": "BX2",
                    "distribution_direction_id": storage.id,
                    "items": [{"sku_code": "ART-001", "qty": 20}],
                },
            ],
            pallets=[],
            user=self.user,
        )
        wb_alloc = plan.allocations.get(direction=wb)
        st_alloc = plan.allocations.get(direction=storage)
        wb_alloc.refresh_from_db()
        st_alloc.refresh_from_db()
        self.assertEqual(wb_alloc.qty_accepted, 50)
        self.assertEqual(st_alloc.qty_accepted, 20)
        # Повторная синхронизация с меньшим qty — абсолютная перезапись
        sync_accepted_from_flow_boxes(
            receiving_order_id=result.receiving_order_id,
            boxes=[
                {
                    "code": "BX1",
                    "distribution_direction_id": wb.id,
                    "items": [{"sku_code": "ART-001", "qty": 30}],
                }
            ],
            user=self.user,
        )
        wb_alloc.refresh_from_db()
        st_alloc.refresh_from_db()
        self.assertEqual(wb_alloc.qty_accepted, 30)
        self.assertEqual(st_alloc.qty_accepted, 0)

    def test_reserve_linked_skips_without_shipping_and_handles_storage(self):
        from receiving_distribution.services.lifecycle import sync_accepted_from_flow_boxes
        from receiving_distribution.services.reserve_linked import sync_linked_shipping_reserves

        draft = DistributionDraft(
            meta={"eta_at": "2030-01-15T12:00"},
            items=[{"sku_code": "ART-001", "qty_total": 10, "name": "Товар 1"}],
            directions=[
                {"key": "storage_fullbox", "kind": "storage_fullbox", "title": "Хранение FullBox"},
            ],
            allocations=[{"sku_code": "ART-001", "direction_key": "storage_fullbox", "qty": 10}],
        )
        result = create_receiving_distribution(
            agency=self.agency, draft=draft, user=self.user, submit=True, client_request_id="req-res-st"
        )
        plan = ReceivingDistributionPlan.objects.get(receiving_order_id=result.receiving_order_id)
        storage = plan.directions.get(kind="storage_fullbox")
        sync_accepted_from_flow_boxes(
            receiving_order_id=result.receiving_order_id,
            boxes=[
                {
                    "code": "BX-S",
                    "distribution_direction_id": storage.id,
                    "items": [{"sku_code": "ART-001", "qty": 10}],
                }
            ],
            user=self.user,
        )
        outcomes = sync_linked_shipping_reserves(
            receiving_order_id=result.receiving_order_id,
            user=self.user,
        )
        # Хранение без OTG — резерв отгрузки не создаётся
        self.assertEqual(outcomes, [])
