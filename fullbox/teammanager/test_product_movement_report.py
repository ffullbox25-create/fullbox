"""Отчёт «Движение товара»: документ-основание, событие, направление, тара.

Тесты проверяют результат строки отчёта, а не наличие текста в шаблоне.
База не нужна: проверяемые функции работают с объектами в памяти.
"""
from __future__ import annotations

from datetime import datetime, timezone as dt_timezone
from io import BytesIO
from unittest.mock import Mock, patch

from django.http import QueryDict
from django.test import SimpleTestCase, TestCase

from sklad.models import WarehouseContainer, WarehouseEvent, WarehouseReserve
from sku.models import Agency
from teammanager.product_reports import (
    MovementSelectionTooBroad,
    ProductReportData,
    _container_codes,
    _document_parts,
    _event_label,
    _event_product,
    _guard_movement,
    _column_specs,
    _csv_bytes,
    _movement_direction,
    _movement_journal_notice,
    _movement_kpis,
    _product_movement_report,
    _movement_queryset,
    _movement_row,
    _product_from_snapshot_bucket,
    _product_balance_report,
    _product_balance_xlsx_bytes,
    _resolve_container_products,
    _xlsx_bytes,
)
from teammanager.report_catalog import PRODUCT_REPORTS, ReportColumn, ReportDefinition

AT = datetime(2026, 9, 1, 8, 12, 44, tzinfo=dt_timezone.utc)


def make_event(**kwargs) -> WarehouseEvent:
    defaults = {
        "event_type": "shipped",
        "occurred_at": AT,
        "qty": 24,
        "payload": {},
        "source_document_type": "",
        "source_document_id": "",
        "stock_context_type": "",
        "stock_context_id": "",
        "from_zone_code": "",
        "to_zone_code": "",
        "performed_by_role": "",
    }
    defaults.update(kwargs)
    return WarehouseEvent(**defaults)


class DocumentPartsTests(SimpleTestCase):
    def test_source_document_wins_and_is_translated(self):
        event = make_event(source_document_type="shipping_order", source_document_id="4417")
        self.assertEqual(_document_parts(event), ("Заказ на отгрузку", "4417"))

    def test_falls_back_to_stock_context_and_marks_it(self):
        event = make_event(stock_context_type="receiving", stock_context_id="20914")
        doc_type, number = _document_parts(event)
        self.assertEqual(number, "20914")
        self.assertEqual(doc_type, "Приёмка · контекст")

    def test_receiving_and_shipping_with_same_number_are_distinguishable(self):
        receiving = make_event(stock_context_type="receiving", stock_context_id="4417")
        shipping = make_event(stock_context_type="shipping", stock_context_id="4417")
        self.assertNotEqual(_document_parts(receiving), _document_parts(shipping))

    def test_unknown_context_type_is_shown_as_is(self):
        event = make_event(stock_context_type="brand_new_flow", stock_context_id="7")
        self.assertEqual(_document_parts(event), ("brand_new_flow · контекст", "7"))

    def test_document_type_without_number_does_not_win_over_context(self):
        event = make_event(
            source_document_type="shipping_order",
            source_document_id="",
            stock_context_type="shipping",
            stock_context_id="4417",
        )
        self.assertEqual(_document_parts(event), ("Отгрузка · контекст", "4417"))

    def test_empty_event_gives_empty_document(self):
        self.assertEqual(_document_parts(make_event()), ("", ""))


class DirectionTests(SimpleTestCase):
    def test_known_directions(self):
        self.assertEqual(_movement_direction("placement_completed"), "income")
        self.assertEqual(_movement_direction("shipped"), "outcome")
        self.assertEqual(_movement_direction("movement_completed"), "transfer")
        self.assertEqual(_movement_direction("shipping_reserved"), "reserve")

    def test_reserve_suffix_is_recognised_without_explicit_listing(self):
        self.assertEqual(_movement_direction("some_new_flow_reserved"), "reserve")
        self.assertEqual(_movement_direction("some_new_flow_reserve_released"), "reserve")

    def test_unknown_event_is_service_and_does_not_raise(self):
        self.assertEqual(_movement_direction("totally_unknown_event"), "service")
        self.assertEqual(_movement_direction(""), "service")
        self.assertEqual(_movement_direction(None), "service")


class EventLabelTests(SimpleTestCase):
    def test_known_event_is_translated(self):
        self.assertEqual(_event_label("palletization_completed"), "Паллетизация завершена")

    def test_unknown_event_keeps_its_code(self):
        self.assertEqual(_event_label("brand_new_event"), "brand_new_event")


class ContainerCodesTests(SimpleTestCase):
    def test_box_returns_box_and_parent_pallet(self):
        pallet = WarehouseContainer(container_type="pallet", container_code="П-88213")
        box = WarehouseContainer(container_type="box", container_code="К-004417-02")
        box.parent_container = pallet
        self.assertEqual(_container_codes(make_event(container=box)), ("К-004417-02", "П-88213"))

    def test_pallet_event_fills_pallet_only(self):
        pallet = WarehouseContainer(container_type="pallet", container_code="П-88213")
        self.assertEqual(_container_codes(make_event(container=pallet)), ("", "П-88213"))

    def test_event_without_container_gives_empty_codes(self):
        self.assertEqual(_container_codes(make_event()), ("", ""))


class MovementRowTests(SimpleTestCase):
    def test_row_carries_document_direction_role_and_tare(self):
        pallet = WarehouseContainer(container_type="pallet", container_code="П-88213")
        box = WarehouseContainer(container_type="box", container_code="К-004417-02")
        box.parent_container = pallet
        event = make_event(
            event_type="shipped",
            source_document_type="shipping_order",
            source_document_id="4417",
            performed_by_role="storekeeper",
            container=box,
        )
        row = _movement_row(event)
        self.assertEqual(row["event_label"], "Отгружен")
        self.assertEqual(row["direction"], "Расход")
        self.assertEqual(row["document_type"], "Заказ на отгрузку")
        self.assertEqual(row["document_number"], "4417")
        self.assertEqual(row["box_code"], "К-004417-02")
        self.assertEqual(row["pallet_code"], "П-88213")
        self.assertEqual(row["performed_by_role"], "storekeeper")
        self.assertEqual(row["operation_quantity"], 24)

    def test_dead_qty_columns_are_gone(self):
        row = _movement_row(make_event())
        self.assertNotIn("qty_before", row)
        self.assertNotIn("qty_after", row)

    def test_operation_type_is_preserved_for_neighbour_reports(self):
        # «Приход товара», «Расход товара» и «Перемещения товара» читают этот ключ.
        self.assertEqual(_movement_row(make_event(event_type="shipped"))["operation_type"], "Отгрузка")

    def test_document_number_matches_previous_behaviour(self):
        for kwargs, expected in (
            ({"source_document_id": "A-1"}, "A-1"),
            ({"stock_context_id": "B-2"}, "B-2"),
            ({"source_document_id": "A-1", "stock_context_id": "B-2"}, "A-1"),
        ):
            with self.subTest(kwargs=kwargs):
                self.assertEqual(_movement_row(make_event(**kwargs))["document_number"], expected)

    def test_broken_payload_does_not_raise(self):
        row = _movement_row(make_event(payload=["not", "a", "dict"], event_type="unknown_x"))
        self.assertEqual(row["direction"], "Служебное")
        self.assertEqual(row["event_label"], "unknown_x")


class MovementQuerysetTests(SimpleTestCase):
    def test_related_users_are_prefetched(self):
        # Без этих двух связей отчёт делал по запросу на строку: 1961 SQL на 5000 строк.
        select_related = _movement_queryset({}).query.select_related
        self.assertIn("requested_by", select_related["operation"])
        self.assertIn("assigned_to", select_related["operation_task"])
        self.assertIn("parent_container", select_related["container"])


class EventProductTests(SimpleTestCase):
    """Товар строки: payload -> резерв -> состав короба -> прочерк."""

    def test_payload_wins_over_reserve_and_container(self):
        event = make_event(
            event_type="placement_completed",
            payload={"sku_code": "7401203", "name": "Кофе Jardin", "barcode": "4600696210415"},
        )
        event.reserve = WarehouseReserve(sku_code="ДРУГОЙ")
        product = _event_product(event, {"article": "ТРЕТИЙ", "product_name": "Третий", "barcode": ""})
        self.assertEqual(product["article"], "7401203")
        self.assertEqual(product["product_name"], "Кофе Jardin")
        self.assertEqual(product["barcode"], "4600696210415")

    def test_reserve_wins_over_container(self):
        event = make_event(event_type="shipped")
        event.reserve = WarehouseReserve(sku_code="7401203", barcode="4600696210415")
        product = _event_product(event, {"article": "СМЕШ", "product_name": "3 SKU в коробе", "barcode": ""})
        self.assertEqual(product["article"], "7401203")
        self.assertEqual(product["barcode"], "4600696210415")

    def test_container_is_used_when_payload_and_reserve_are_empty(self):
        product = _event_product(
            make_event(event_type="movement_completed"),
            {"article": "7402781", "product_name": "Чай Greenfield", "barcode": "4605246006012"},
        )
        self.assertEqual(product["product_name"], "Чай Greenfield")

    def test_nothing_known_gives_dash_and_never_guesses(self):
        product = _event_product(make_event(event_type="receiving_arrived"), None)
        self.assertEqual(product, {"article": "", "product_name": "—", "barcode": ""})

    def test_row_carries_resolved_product(self):
        row = _movement_row(
            make_event(event_type="movement_completed"),
            {"article": "7402781", "product_name": "Чай Greenfield", "barcode": "4605246006012"},
        )
        self.assertEqual(row["article"], "7402781")
        self.assertEqual(row["product_name"], "Чай Greenfield")
        self.assertEqual(row["barcode"], "4605246006012")


class SnapshotBucketTests(SimpleTestCase):
    def test_single_sku_box_gives_article_and_name(self):
        bucket = {"7401203": {"sku_code": "7401203", "name": "Кофе Jardin", "barcode": "4600696210415"}}
        self.assertEqual(
            _product_from_snapshot_bucket(bucket),
            {"article": "7401203", "product_name": "Кофе Jardin", "barcode": "4600696210415"},
        )

    def test_box_without_name_falls_back_to_article(self):
        bucket = {"7401203": {"sku_code": "7401203", "name": "", "barcode": ""}}
        self.assertEqual(_product_from_snapshot_bucket(bucket)["product_name"], "7401203")

    def test_mixed_box_shows_composition_not_a_random_sku(self):
        bucket = {code: {"sku_code": code, "name": code, "barcode": ""} for code in ("A", "B", "C")}
        product = _product_from_snapshot_bucket(bucket)
        self.assertEqual(product["product_name"], "3 SKU в коробе")
        self.assertEqual(product["article"], "")

    def test_no_containers_means_no_query(self):
        self.assertEqual(_resolve_container_products([]), {})


class SelectionGuardTests(SimpleTestCase):
    def test_too_broad_message_names_the_way_out(self):
        message = str(MovementSelectionTooBroad(2500))
        self.assertIn("2500", message)
        self.assertIn("Уточните", message)

    def test_guard_turns_limit_into_a_hint_not_an_error(self):
        def builder():
            raise MovementSelectionTooBroad(2500)

        rows, notice = _guard_movement(builder)
        self.assertEqual(rows, [])
        self.assertIn("Уточните", notice)

    def test_guard_passes_rows_through_unchanged(self):
        self.assertEqual(_guard_movement(lambda: [{"a": 1}]), ([{"a": 1}], ""))


class MovementKpiTests(SimpleTestCase):
    def test_income_and_outcome_use_only_required_event_codes(self):
        rows = [
            {"event_code": "receiving_arrived", "operation_quantity": 100, "direction": "Приход"},
            {"event_code": "placement_completed", "operation_quantity": 12, "direction": "Приход"},
            {"event_code": "shipped", "operation_quantity": 4, "direction": "Расход"},
            {"event_code": "processing_consumed", "operation_quantity": 3, "direction": "Расход"},
            {"event_code": "movement_completed", "operation_quantity": 50, "direction": "Перемещение"},
        ]
        self.assertEqual(
            _movement_kpis(rows),
            [
                {"label": "Строк", "value": 5},
                {"label": "Приход, шт", "value": 12},
                {"label": "Расход, шт", "value": 7},
                {"label": "Перемещений", "value": 1},
            ],
        )

    def test_card_kpis_include_current_stock(self):
        rows = [{"event_code": "placement_completed", "operation_quantity": 2, "direction": "Приход"}]
        extra = {
            "movement_card_enabled": True,
            "movement_card": {"current_quantity": 9, "available_quantity": 6, "reserved_quantity": 3},
        }
        self.assertEqual(
            [item["value"] for item in _movement_kpis(rows, extra)],
            [2, 0, 0, 9, 6, 3],
        )


class ProductBalanceManualWriteoffTests(TestCase):
    def setUp(self):
        self.agency = Agency.objects.create(agn_name="Клиент ручного списания")
        WarehouseEvent.objects.create(
            agency=self.agency,
            event_type="placement_completed",
            qty=24180,
            payload={"sku_code": "нож007", "name": "Нож складной туристический"},
            occurred_at=AT,
        )
        WarehouseEvent.objects.create(
            agency=self.agency,
            event_type="shipped",
            qty=19740,
            payload={"sku_code": "нож007", "name": "Нож складной туристический"},
            occurred_at=AT,
        )
        WarehouseEvent.objects.create(
            agency=self.agency,
            event_type="manual_writeoff",
            qty=240,
            source_document_type="manual_writeoff",
            source_document_id="WRITE-OFF-1",
            payload={
                "previous": {
                    "sku_code": "нож007",
                    "name": "Нож складной туристический",
                }
            },
            occurred_at=AT,
        )

    @patch("teammanager.product_reports.StockAvailabilityService.stock_rows_with_availability")
    @patch("teammanager.product_reports.manager_visible_agencies")
    def test_manual_writeoff_is_in_rows_totals_balance_and_xlsx(self, visible_agencies, stock_rows):
        visible_agencies.return_value = Agency.objects.filter(pk=self.agency.pk)
        stock_rows.return_value = [
            {
                "sku": "нож007",
                "name": "Нож складной туристический",
                "qty": 3300,
                "available_qty": 3300,
                "processing_reserved_qty": 0,
                "shipping_reserved_qty": 0,
                "other_reserved_qty": 0,
                "warehouse_state_code": "stored",
            },
            {
                "sku": "нож007",
                "name": "Нож складной туристический",
                "qty": 900,
                "available_qty": 0,
                "processing_reserved_qty": 0,
                "shipping_reserved_qty": 0,
                "other_reserved_qty": 0,
                "warehouse_state_code": "ready_for_loading",
            },
        ]
        params = {
            "client_id": str(self.agency.pk),
            "article": "нож007",
            "date_from": "2026-05-01",
            "date_to": "2026-09-07",
        }

        rows, notice, extra = _product_balance_report(params)

        self.assertEqual(notice, "")
        summary = extra["summary"]
        self.assertEqual(summary["incoming_quantity"], 24180)
        self.assertEqual(summary["shipped_quantity"], 19740)
        self.assertEqual(summary["processing_consumed_quantity"], 0)
        self.assertEqual(summary["manual_writeoff_quantity"], 240)
        self.assertEqual(summary["outgoing_quantity"], 19980)
        self.assertEqual(summary["physical_quantity"], 4200)
        self.assertEqual(summary["available_quantity"], 3300)
        self.assertEqual(summary["control_balance"], 0)
        manual_rows = [row for row in rows if row["operation_type"] == "Ручное списание"]
        self.assertEqual(len(manual_rows), 1)
        self.assertEqual(manual_rows[0]["outgoing_quantity"], 240)
        self.assertEqual(manual_rows[0]["document_number"], "WRITE-OFF-1")

        data = ProductReportData(
            rows=rows,
            page_rows=rows,
            visible_columns=[],
            hidden_columns=[],
            totals={},
            kpis=[],
            generated_at=AT,
            row_count=len(rows),
            page_obj=None,
            page_range=[],
            extra=extra,
        )
        from openpyxl import load_workbook

        workbook = load_workbook(BytesIO(_product_balance_xlsx_bytes(data)), read_only=True, data_only=True)
        xlsx_summary = dict(workbook["Сводка"].iter_rows(min_row=2, values_only=True))
        self.assertEqual(xlsx_summary["Ручное списание"], 240)
        self.assertEqual(xlsx_summary["Расход всего"], 19980)
        self.assertEqual(xlsx_summary["Контрольный баланс"], 0)
        xlsx_operations = [
            row[1]
            for row in workbook["Движения"].iter_rows(min_row=2, min_col=1, max_col=2, values_only=True)
        ]
        self.assertIn("Ручное списание", xlsx_operations)


class MovementCardModeTests(SimpleTestCase):
    def _visible(self, agency):
        visible = Mock()
        visible.filter.return_value.first.return_value = agency
        return visible

    @patch("teammanager.product_reports._movement_journal_notice", return_value="граница")
    @patch("teammanager.product_reports._movement_rows", return_value=[])
    @patch("teammanager.product_reports._movement_card_stock_summary", return_value={"current_quantity": 5})
    @patch("teammanager.product_reports._movement_card_sku", return_value="SKU-1")
    @patch("teammanager.product_reports.manager_visible_agencies")
    def test_card_turns_on_only_with_client_and_product(
        self, visible_agencies, card_sku, stock_summary, movement_rows, _journal_notice
    ):
        agency = Mock(id=7)
        visible_agencies.return_value = self._visible(agency)
        _rows, _notice, extra = _product_movement_report({"client_id": "7", "article": "SKU-1"})
        self.assertTrue(extra["movement_card_enabled"])
        card_sku.assert_called_once_with(agency, "article", "SKU-1")
        stock_summary.assert_called_once_with(agency, "SKU-1")
        self.assertTrue(movement_rows.call_args.kwargs["exact_product"])

    @patch("teammanager.product_reports._movement_journal_notice", return_value="граница")
    @patch("teammanager.product_reports._movement_rows", return_value=[])
    @patch("teammanager.product_reports.StockAvailabilityService.stock_rows_with_availability")
    @patch("teammanager.product_reports.manager_visible_agencies")
    def test_client_without_product_stays_in_journal_mode(
        self, visible_agencies, stock_rows, movement_rows, _journal_notice
    ):
        visible_agencies.return_value = self._visible(Mock(id=7))
        _rows, _notice, extra = _product_movement_report({"client_id": "7"})
        self.assertFalse(extra["movement_card_enabled"])
        stock_rows.assert_not_called()
        self.assertFalse(movement_rows.call_args.kwargs["exact_product"])

    @patch("teammanager.product_reports._movement_journal_notice", return_value="граница")
    @patch("teammanager.product_reports._movement_rows", return_value=[])
    @patch("teammanager.product_reports.StockAvailabilityService.stock_rows_with_availability")
    def test_journal_mode_does_not_read_stock_availability(self, stock_rows, _movement_rows, _journal_notice):
        _product_movement_report({})
        stock_rows.assert_not_called()

    @patch("teammanager.product_reports._movement_rows")
    @patch("teammanager.product_reports.manager_visible_agencies")
    def test_inaccessible_client_returns_hint_and_no_rows(self, visible_agencies, movement_rows):
        visible_agencies.return_value = self._visible(None)
        rows, notice, _extra = _product_movement_report({"client_id": "999", "article": "SKU-1"})
        self.assertEqual(rows, [])
        self.assertIn("Клиент не найден", notice)
        movement_rows.assert_not_called()


class MovementJournalNoticeTests(SimpleTestCase):
    @patch("teammanager.product_reports.WarehouseEvent.objects.aggregate")
    def test_notice_uses_date_from_data(self, aggregate):
        aggregate.return_value = {"started_at": AT}
        notice = _movement_journal_notice()
        self.assertIn("01.09.2026", notice)
        aggregate.assert_called_once()

    @patch("teammanager.product_reports._movement_journal_notice")
    @patch("teammanager.product_reports._movement_rows", side_effect=MovementSelectionTooBroad(2501))
    def test_broad_selection_hint_has_priority(self, _movement_rows, journal_notice):
        rows, notice, _extra = _product_movement_report({})
        self.assertEqual(rows, [])
        self.assertIn("Уточните", notice)
        journal_notice.assert_not_called()


class MovementColumnTests(SimpleTestCase):
    def setUp(self):
        self.report = next(report for report in PRODUCT_REPORTS if report.code == "product-movement")

    def test_default_set_is_exactly_ten_columns(self):
        visible, hidden = _column_specs(self.report, QueryDict(""))
        self.assertEqual(
            [column.code for column in visible],
            [
                "operation_at",
                "event_label",
                "direction",
                "document_type",
                "document_number",
                "article",
                "product_name",
                "operation_quantity",
                "cell_from",
                "cell_to",
            ],
        )
        self.assertIn("barcode", [column.code for column in hidden])

    def test_explicit_selection_overrides_default(self):
        visible, hidden = _column_specs(self.report, QueryDict("columns=barcode&columns=article"))
        self.assertEqual([column.code for column in visible], ["article", "barcode"])
        self.assertNotIn("barcode", [column.code for column in hidden])

    def test_report_without_defaults_keeps_all_columns(self):
        report = ReportDefinition(
            code="plain",
            section="products",
            title="Обычный",
            description="",
            columns=(ReportColumn("a", "A"), ReportColumn("b", "B")),
        )
        visible, hidden = _column_specs(report, QueryDict(""))
        self.assertEqual([column.code for column in visible], ["a", "b"])
        self.assertEqual(hidden, [])

    def test_csv_and_xlsx_have_same_visible_columns_and_rows(self):
        from openpyxl import load_workbook

        visible, _hidden = _column_specs(self.report, QueryDict(""))
        rows = [{column.code: f"r{row_index}-{column.code}" for column in visible} for row_index in range(2)]
        csv_lines = _csv_bytes(visible, rows).decode("utf-8-sig").splitlines()
        workbook = load_workbook(BytesIO(_xlsx_bytes(visible, rows)), read_only=True)
        sheet = workbook.active
        xlsx_rows = list(sheet.iter_rows(values_only=True))
        self.assertEqual(csv_lines[0].split(";"), [column.title for column in visible])
        self.assertEqual(list(xlsx_rows[0]), [column.title for column in visible])
        self.assertEqual(len(csv_lines) - 1, len(rows))
        self.assertEqual(len(xlsx_rows) - 1, len(rows))
