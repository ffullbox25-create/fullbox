from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import RequestFactory, SimpleTestCase, TestCase
from django.template.loader import render_to_string
from django.utils import timezone

from audit.models import OrderAuditEntry
from billing.models import BillingService, WarehouseServiceFact
from orders.services import ReceivingWorkflowService
from reachtruck.models import MoveRequest, MoveTask
from shipping.models import ShippingOrder, ShippingOrderItem
from sku.models import Agency
from sku.models import SKU, SKUBarcode
from sklad.models import (
    WarehouseContainer,
    WarehouseEvent,
    WarehouseLocation,
    WarehouseOperation,
    WarehouseOperationTask,
    WarehouseReserve,
    WarehouseStockSnapshot,
)
from sklad.services import OperationalStockService
from sklad.services import (
    WarehouseActionPolicy,
    WarehouseCommandService,
    WarehouseEventType,
    WarehouseMovementResolver,
    WarehouseStateCode,
    WarehouseTransitionError,
    WarehouseTransitionService,
    WarehouseWritePathService,
)
from sklad.services.stock_availability import StockAvailabilityService
from sklad.services.warehouse_stock_rows import snapshot_stock_rows
from sklad.ui_services import build_inventory_journal_page
from sklad.templatetags.sklad_request_journal import (
    _shipping_identifier_results,
    _table_row,
    storekeeper_request_journal,
)
from .stock_state import (
    _apply_processing_source_deductions,
    _ensure_pallet_location,
    _goods_type_label,
    _inventory_key,
    _normalize_goods_type,
    _normalize_location,
    _receiving_removed_qty_map,
    refresh_materialized_stock_state_for_agency,
    _shipping_shipped_qty_map,
)


def create_warehouse_snapshot_row(
    *,
    agency: Agency,
    order_type: str = "receiving",
    order_id: str,
    sku: str,
    name: str,
    size: str,
    barcode: str = "",
    marking_code: str = "",
    goods_type: str,
    qty: int,
    available_qty: int | None = None,
    processing_reserved_qty: int = 0,
    shipping_reserved_qty: int = 0,
    box_code: str = "",
    pallet_code: str = "",
    zone: str = "OS",
    row: int = 1,
    section: int = 1,
    tier: int = 1,
    cell: int = 1,
    warehouse_state_code: str = WarehouseStateCode.STORED.value,
) -> WarehouseStockSnapshot:
    location = WarehouseWritePathService.ensure_location(
        warehouse_code="MSK",
        zone_code=zone,
        row_no=row,
        section_no=section,
        tier_no=tier,
        cell_no=cell,
    )
    parent_container = None
    container = None
    if pallet_code:
        parent_container, _ = WarehouseContainer.objects.get_or_create(
            agency=agency,
            container_code=pallet_code,
            defaults={
                "container_type": WarehouseContainer.TYPE_PALLET,
                "current_location": location,
                "source_context_type": order_type,
                "source_context_id": str(order_id),
            },
        )
    if box_code:
        container, _ = WarehouseContainer.objects.get_or_create(
            agency=agency,
            container_code=box_code,
            defaults={
                "container_type": WarehouseContainer.TYPE_BOX,
                "parent_container": parent_container,
                "current_location": location,
                "source_context_type": order_type,
                "source_context_id": str(order_id),
            },
        )
        if parent_container and container.parent_container_id != parent_container.id:
            container.parent_container = parent_container
            container.save(update_fields=["parent_container", "updated_at"])
    elif parent_container:
        container = parent_container
    return WarehouseStockSnapshot.objects.create(
        agency=agency,
        source_context_type=order_type,
        source_context_id=order_id,
        sku_code=sku,
        name=name,
        size=size,
        barcode=barcode,
        marking_code=marking_code,
        goods_type=goods_type,
        qty=qty,
        available_qty=qty if available_qty is None else available_qty,
        processing_reserved_qty=processing_reserved_qty,
        shipping_reserved_qty=shipping_reserved_qty,
        container=container,
        container_code=container.container_code if container else "",
        parent_container=parent_container if container and container.container_type == WarehouseContainer.TYPE_BOX else None,
        location=location,
        zone_code=location.zone_code,
        zone_kind=location.zone_kind,
        warehouse_state_code=warehouse_state_code,
    )


class RequestJournalTemplateTests(SimpleTestCase):
    def test_shipping_supplement_uses_linked_order_number(self):
        task = SimpleNamespace(
            id=3137,
            filter_type="shipping",
            route="/shipping/562/packing/loose/",
            panel_title="СРОЧНО: упаковать добор OTG-000535",
            title="СРОЧНО: упаковать добор OTG-000535",
            order_status_label="Товар доставлен в OTG, ожидает паллетизации",
            order_status_tone="active",
            order_client_label='ООО "КЕЙЗИ"',
            assigned_to=SimpleNamespace(full_name="Кладовщик"),
            due_date=None,
            updated_at=None,
            panel_url="/shipping/562/packing/loose/",
            executor_label="",
        )
        shipping_order = SimpleNamespace(
            number="OTG-000535",
            delivery_type="",
            destination_warehouse="",
            destination_address="",
            transit_address="",
        )

        row = _table_row(
            task,
            "in_progress",
            shipping_order=shipping_order,
        )

        self.assertEqual(row["number"], "535_OTG")
        self.assertEqual(row["title"], "СРОЧНО: упаковать добор OTG-000535")

    def test_order_number_links_to_the_same_detail_page_as_open_action(self):
        row = {
            "task_id": 241,
            "number": "241_OTG",
            "title": "Заявка на отгрузку №241_OTG",
            "type_label": "Отгрузка",
            "client_label": "ИП Талеев Д. Т.",
            "date_label": "01.08.2026",
            "warehouse_label": "-",
            "responsible_label": "Кладовщик",
            "status_tone": "danger",
            "status_label": "Просрочено",
            "detail_url": "/shipping/268/",
        }
        html = render_to_string(
            "sklad/_request_journal.html",
            {
                "task_panel_list_url": "/todo/",
                "request_page_size": 10,
                "task_panel_filter_tabs": [],
                "task_panel_client_options": [],
                "task_panel_document_status_options": [],
                "task_panel_search_query": "",
                "task_panel_active_filter_type": "shipping",
                "task_panel_stats": [],
                "request_rows": [row],
                "request_start_index": 1,
                "request_end_index": 1,
                "request_total": 1,
                "request_page_obj": SimpleNamespace(has_previous=False, has_next=False),
                "request_page_links": [],
                "request_page_query_prefix": "",
                "request_hidden_query": [],
                "request_page_sizes": [10],
            },
        )

        self.assertIn('<a class="request-number" href="/shipping/268/">№241_OTG</a>', html)
        self.assertIn('<a class="request-open" href="/shipping/268/">Открыть</a>', html)

    def test_identifier_result_is_rendered_above_request_table(self):
        html = render_to_string(
            "sklad/_request_journal.html",
            {
                "task_panel_list_url": "/todo/",
                "request_page_size": 10,
                "task_panel_filter_tabs": [],
                "task_panel_client_options": [],
                "task_panel_document_status_options": [],
                "task_panel_search_query": "GM-SEARCH-001",
                "task_panel_active_filter_type": "shipping",
                "task_panel_stats": [],
                "identifier_search_performed": True,
                "identifier_search_query": "GM-SEARCH-001",
                "identifier_search_result_count": 1,
                "identifier_search_results": [
                    {
                        "number": "900_OTG",
                        "client_label": "Клиент",
                        "marketplace_label": "Ozon",
                        "destination_label": "Тверь",
                        "status_label": "В отборе",
                        "status_tone": "active",
                        "detail_url": "/shipping/900/",
                        "matches": [
                            {
                                "label": "ШК ГМ / данные поставки",
                                "value": "GM-SEARCH-001",
                            }
                        ],
                    }
                ],
                "request_rows": [],
                "request_start_index": 0,
                "request_end_index": 0,
                "request_total": 0,
                "request_page_obj": SimpleNamespace(has_previous=False, has_next=False),
                "request_page_links": [],
                "request_page_query_prefix": "",
                "request_hidden_query": [],
                "request_page_sizes": [10],
            },
        )

        self.assertIn("Найдено в заявках", html)
        self.assertIn("Заявка №900_OTG", html)
        self.assertIn("GM-SEARCH-001", html)
        self.assertIn('href="/shipping/900/"', html)


class StorekeeperIdentifierSearchTests(TestCase):
    def setUp(self):
        self.agency = Agency.objects.create(agn_name="Клиент поиска кладовщика")
        self.order = ShippingOrder.objects.create(
            number="OTG-000900",
            agency=self.agency,
            status=ShippingOrder.STATUS_PICKING,
            supply_number="2000090000001",
            wb_supply_barcode="WB-SUPPLY-900",
            shipping_barcode="SHIP-900",
            destination_warehouse="Тверь",
            comment="ШК ГМ Ozon: GM-SEARCH-001",
        )
        self.item = ShippingOrderItem.objects.create(
            order=self.order,
            sku_code="SKU-SEARCH",
            name="Товар поиска",
            barcode="4600000000900",
            qty_requested=10,
            comment="Коробов: 1; короба: BOX-FULLBOX-900; ШК ГМ Ozon: GM-ITEM-900",
        )
        WarehouseContainer.objects.create(
            agency=self.agency,
            container_type=WarehouseContainer.TYPE_BOX,
            container_code="BOX-FACT-900",
            source_context_type="shipping",
            source_context_id=self.order.number,
        )

    def _assert_order_found(self, query: str, label: str):
        before = (
            self.order.status,
            self.order.updated_at,
            self.item.qty_requested,
            self.item.qty_reserved,
            self.item.qty_shipped,
        )

        results = _shipping_identifier_results(query)

        self.order.refresh_from_db()
        self.item.refresh_from_db()
        after = (
            self.order.status,
            self.order.updated_at,
            self.item.qty_requested,
            self.item.qty_reserved,
            self.item.qty_shipped,
        )
        self.assertEqual(before, after)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["detail_url"], f"/shipping/{self.order.pk}/")
        self.assertIn(label, {row["label"] for row in results[0]["matches"]})

    def test_searches_ozon_supply_number(self):
        self._assert_order_found("2000090000001", "Номер поставки Ozon")

    def test_searches_wb_supply_number(self):
        self._assert_order_found("WB-SUPPLY-900", "Поставка WB")

    def test_searches_gm_barcode_from_order_and_item(self):
        self._assert_order_found("GM-SEARCH-001", "ШК ГМ / данные поставки")
        self._assert_order_found("GM-ITEM-900", "ШК ГМ / состав заявки")

    def test_searches_reserved_and_fact_box_codes(self):
        self._assert_order_found("BOX-FULLBOX-900", "Короб / состав заявки")
        self._assert_order_found("BOX-FACT-900", "Короб Fullbox")


class StorekeeperRequestJournalStatusTests(SimpleTestCase):
    def _task(self, task_id: int, number: str):
        return SimpleNamespace(
            id=task_id,
            filter_type="other",
            route=f"/orders/other/{number}/",
            panel_title=f"Заявка №{number}",
            title=f"Заявка №{number}",
            panel_updated_at_label="04.08.2026 09:00",
            order_status_label="",
            order_status_tone="neutral",
            order_client_label="Клиент",
            assigned_to=SimpleNamespace(full_name="Кладовщик"),
            panel_url=f"/orders/other/{number}/",
            executor_label="",
        )

    def _panel(self):
        columns = [
            {"status": "backlog", "tasks": [self._task(1, "1_OTH")]},
            {"status": "in_progress", "tasks": [self._task(2, "2_OTH")]},
            {"status": "blocked", "tasks": [self._task(3, "3_OTH")]},
            {"status": "done", "tasks": [self._task(4, "4_OTH")]},
        ]
        return {
            "task_panel_search_query": "",
            "task_panel_columns": columns,
            "task_panel_stats": [
                {"status": column["status"], "label": column["status"], "count": len(column["tasks"])}
                for column in columns
            ],
            "task_panel_active_filter_type": "all",
            "task_panel_selected_client": "",
            "task_panel_selected_document_status": "",
        }

    @patch("sklad.templatetags.sklad_request_journal.task_panel")
    def test_default_journal_hides_completed_tasks(self, task_panel_mock):
        task_panel_mock.return_value = self._panel()

        result = storekeeper_request_journal(
            {"request": RequestFactory().get("/sklad/")}
        )

        self.assertEqual(result["request_total"], 3)
        self.assertEqual(
            {row["task_id"] for row in result["request_rows"]},
            {1, 2, 3},
        )
        self.assertFalse(any(stat["active"] for stat in result["task_panel_stats"]))

    @patch("sklad.templatetags.sklad_request_journal.task_panel")
    def test_done_filter_shows_only_completed_tasks(self, task_panel_mock):
        task_panel_mock.return_value = self._panel()

        result = storekeeper_request_journal(
            {
                "request": RequestFactory().get(
                    "/sklad/",
                    {"todo_filter_schedule_status": "done"},
                )
            }
        )

        self.assertEqual(result["request_total"], 1)
        self.assertEqual(
            [row["task_id"] for row in result["request_rows"]],
            [4],
        )
        done_stat = next(
            stat for stat in result["task_panel_stats"] if stat["status"] == "done"
        )
        self.assertTrue(done_stat["active"])


class StockStateHelpersTests(SimpleTestCase):
    def test_normalize_goods_type_and_inventory_key(self):
        self.assertEqual(_normalize_goods_type("op"), "оптовый")
        self.assertEqual(_normalize_goods_type("  Готовый "), "готовый")
        self.assertEqual(_inventory_key(" SKU-1 ", " 42 ", " OP "), ("sku-1", "42", "оптовый"))

    def test_goods_type_label_prefers_explicit_label(self):
        self.assertEqual(_goods_type_label("receiving", {"goods_type_label": "Мой тип"}), "Мой тип")
        self.assertEqual(_goods_type_label("processing", {}), "Готовый")
        self.assertEqual(_goods_type_label("receiving", {}), "Оптовый")

    def test_legacy_processing_source_deductions_are_disabled(self):
        rows = [
            {
                "sku": "SKU-1",
                "size": "42",
                "goods_type": "Оптовый",
                "qty": 50,
                "order_type": "receiving",
                "created_at": 1,
            },
            {
                "sku": "SKU-1",
                "size": "42",
                "goods_type": "Оптовый",
                "qty": 30,
                "order_type": "processing",
                "created_at": 2,
            },
            {
                "sku": "SKU-1",
                "size": "42",
                "goods_type": "Готовый",
                "qty": 40,
                "order_type": "processing",
                "created_at": 3,
            },
        ]
        key = _inventory_key("SKU-1", "42", "Оптовый")
        adjusted = _apply_processing_source_deductions(rows, {key: 60}, {key: 10})

        self.assertIsNone(adjusted)

    def test_legacy_audit_qty_maps_do_not_rebuild_stock(self):
        entry = SimpleNamespace(
            order_type="receiving",
            order_id="9",
            payload={
                "act_items_removed": True,
                "act_items": [
                    {"sku": "SKU-1", "size": "42", "qty": 10},
                ],
                "act_boxes": [
                    {"items": [{"sku": "SKU-1", "size": "42", "qty": 7}]},
                ],
                "act_pallets": [],
            },
        )
        shipped = SimpleNamespace(
            order_type="shipping",
            order_id="SO-1",
            payload={
                "shipping_state": "shipped",
                "shipped_items": [
                    {"sku": "SKU-1", "size": "42", "goods_type": "Готовый", "qty": 10},
                ],
            },
        )

        self.assertEqual(_receiving_removed_qty_map([entry]), {})
        self.assertEqual(_shipping_shipped_qty_map([shipped]), {})

    def test_location_normalization_and_defaulting(self):
        normalized = _normalize_location({"zone": "os", "row": "2", "section": "3", "tier": "4", "cell": "5"})
        self.assertEqual(normalized["zone"], "OS")
        self.assertEqual(normalized["row"], 2)
        self.assertEqual(normalized["section"], 3)
        self.assertEqual(normalized["tier"], 4)
        self.assertEqual(normalized["cell"], 5)

        row = _ensure_pallet_location({"pallet_code": "P-1"}, default_zone="OBR")
        self.assertEqual(row["zone"], "OBR")


class StockAvailabilityServiceTests(TestCase):
    def setUp(self):
        self.agency = Agency.objects.create(agn_name="Availability Agency")
        self.location = WarehouseWritePathService.ensure_location(
            warehouse_code="MSK",
            zone_code="OS",
            row_no=1,
            section_no=1,
            tier_no=1,
            cell_no=1,
        )
        self.snapshot = WarehouseStockSnapshot.objects.create(
            agency=self.agency,
            source_context_type="receiving",
            source_context_id="1000",
            sku_code="SKU-1",
            name="Товар 1",
            size="42",
            goods_type="Не обработанный",
            qty=50,
            available_qty=0,
            processing_reserved_qty=50,
            location=self.location,
            zone_code=self.location.zone_code,
            zone_kind=self.location.zone_kind,
            warehouse_state_code=WarehouseStateCode.STORED.value,
        )
        WarehouseReserve.objects.create(
            agency=self.agency,
            reserve_type=WarehouseReserve.TYPE_PROCESSING,
            context_type="processing",
            context_id="2000",
            sku_code="SKU-1",
            size="42",
            goods_type="Не обработанный",
            qty_reserved=50,
            status=WarehouseReserve.STATUS_ACTIVE,
        )

    def test_inventory_items_for_agency_excludes_current_processing_reserve(self):
        without_exclusion = StockAvailabilityService.inventory_items_for_agency(self.agency)
        self.assertEqual(without_exclusion, [])

        with_exclusion = StockAvailabilityService.inventory_items_for_agency(
            self.agency,
            exclude_processing_order_id="2000",
        )
        self.assertEqual(len(with_exclusion), 1)
        self.assertEqual(with_exclusion[0]["sku"], "SKU-1")
        self.assertEqual(with_exclusion[0]["qty"], 50)

    def test_inventory_items_for_agency_uses_warehouse_processing_reserve(self):
        without_exclusion = StockAvailabilityService.inventory_items_for_agency(self.agency)
        self.assertEqual(without_exclusion, [])

        with_exclusion = StockAvailabilityService.inventory_items_for_agency(
            self.agency,
            exclude_processing_order_id="2000",
        )
        self.assertEqual(len(with_exclusion), 1)
        self.assertEqual(with_exclusion[0]["sku"], "SKU-1")
        self.assertEqual(with_exclusion[0]["qty"], 50)

    def test_inventory_items_for_agency_excludes_shipping_reserve(self):
        WarehouseReserve.objects.filter(agency=self.agency).delete()
        WarehouseStockSnapshot.objects.filter(agency=self.agency).update(
            available_qty=30,
            processing_reserved_qty=0,
            shipping_reserved_qty=20,
        )
        WarehouseReserve.objects.create(
            agency=self.agency,
            reserve_type=WarehouseReserve.TYPE_SHIPPING,
            context_type="shipping",
            context_id="SO-TEST-1",
            sku_code="SKU-1",
            size="42",
            goods_type="Не обработанный",
            qty_reserved=20,
            status=WarehouseReserve.STATUS_ACTIVE,
        )

        items = StockAvailabilityService.inventory_items_for_agency(self.agency)

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["sku"], "SKU-1")
        self.assertEqual(items[0]["qty"], 30)

    def test_inventory_items_for_agency_does_not_rebuild_from_audit_when_stock_empty(self):
        WarehouseStockSnapshot.objects.filter(agency=self.agency).delete()
        OrderAuditEntry.objects.create(
            order_id="R-AUDIT-1",
            order_type="receiving",
            action="status",
            agency=self.agency,
            payload={
                "act": "placement",
                "act_state": "closed",
                "act_items": [
                    {
                        "sku": "SKU-1",
                        "name": "Товар 1",
                        "size": "42",
                        "qty": 50,
                    }
                ],
            },
        )

        items = StockAvailabilityService.inventory_items_for_agency(self.agency)

        self.assertEqual(items, [])
        self.assertFalse(WarehouseStockSnapshot.objects.filter(agency=self.agency).exists())

    def test_inventory_items_for_agency_uses_warehouse_snapshot_when_legacy_rows_missing(self):
        WarehouseStockSnapshot.objects.filter(agency=self.agency).delete()
        WarehouseReserve.objects.filter(agency=self.agency).delete()
        create_warehouse_snapshot_row(
            agency=self.agency,
            order_id="R-SNAP-INV",
            sku="SKU-1",
            name="Товар 1",
            size="42",
            goods_type="Не обработанный",
            qty=50,
            box_code="BOX-INV-1",
            pallet_code="PAL-INV-1",
        )

        items = StockAvailabilityService.inventory_items_for_agency(self.agency)

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["sku"], "SKU-1")
        self.assertEqual(items[0]["qty"], 50)

    def test_occupied_os_helpers_use_warehouse_snapshots_when_available(self):
        location = WarehouseLocation.objects.create(
            warehouse_code="MSK",
            zone_code="OS",
            zone_kind=WarehouseLocation.ZONE_KIND_STORAGE,
            row_no=2,
            section_no=3,
            tier_no=1,
            cell_no=2,
            display_name="OS · Ряд 2 · Секция 3 · Ярус 1 · Ячейка 2",
        )
        container = WarehouseContainer.objects.create(
            agency=self.agency,
            container_type=WarehouseContainer.TYPE_PALLET,
            container_code="PAL-SNAPSHOT-OS",
            current_location=location,
        )
        WarehouseStockSnapshot.objects.create(
            agency=self.agency,
            source_context_type="receiving",
            source_context_id="RCV-OS-1",
            sku_code="SKU-OS-1",
            name="Snapshot товар",
            qty=12,
            available_qty=12,
            container=container,
            container_code=container.container_code,
            location=location,
            zone_code="OS",
            zone_kind=location.zone_kind,
            warehouse_state_code="stored",
        )

        keys = StockAvailabilityService.occupied_os_cell_keys()
        cells = StockAvailabilityService.occupied_os_cells(include_agency=True)
        sections = StockAvailabilityService.occupied_os_section_agencies()

        self.assertIn((2, 3, 1, 2), keys)
        self.assertEqual(cells[0]["agency_id"], self.agency.id)
        self.assertIn(self.agency.id, sections[(2, 3)])


class WarehouseActionPolicyTests(TestCase):
    def setUp(self):
        self.agency = Agency.objects.create(agn_name="Policy Availability Agency")

    def _state(self, code: WarehouseStateCode):
        return SimpleNamespace(code=code)

    def test_can_create_receiving_act_allows_receiving_states(self):
        decision = WarehouseActionPolicy.can_create_receiving_act(
            self._state(WarehouseStateCode.PLACED_IN_RECEIVING),
            has_receiving_act=False,
            role="storekeeper",
        )

        self.assertTrue(decision.allowed)

    def test_can_create_receiving_act_blocks_after_storage(self):
        decision = WarehouseActionPolicy.can_create_receiving_act(
            self._state(WarehouseStateCode.STORED),
            has_receiving_act=False,
            role="storekeeper",
        )

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "state_forbidden")

    def test_can_open_receiving_flow_requires_active_receiving_context(self):
        decision = WarehouseActionPolicy.can_open_receiving_flow(
            self._state(WarehouseStateCode.PLACED_IN_RECEIVING),
            role="storekeeper",
            client_view=False,
            flow_closed=False,
            can_create_receiving_act=True,
            has_receiving_act=False,
            flow_has_data=False,
        )

        self.assertTrue(decision.allowed)

    def test_can_open_receiving_flow_blocks_after_storage(self):
        decision = WarehouseActionPolicy.can_open_receiving_flow(
            self._state(WarehouseStateCode.STORED),
            role="storekeeper",
            client_view=False,
            flow_closed=False,
            can_create_receiving_act=False,
            has_receiving_act=True,
            flow_has_data=True,
        )

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "state_forbidden")

    def test_can_send_receiving_to_storage_requires_open_work(self):
        decision = WarehouseActionPolicy.can_send_receiving_to_storage(
            self._state(WarehouseStateCode.PLACED_IN_RECEIVING),
            flow_closed=True,
            role_allowed=True,
            not_created_count=1,
        )

        self.assertTrue(decision.allowed)

    def test_can_send_receiving_to_storage_blocks_after_storage(self):
        decision = WarehouseActionPolicy.can_send_receiving_to_storage(
            self._state(WarehouseStateCode.STORED),
            flow_closed=True,
            role_allowed=True,
            not_created_count=1,
        )

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "state_forbidden")

    def test_can_take_processing_allows_processing_queue_states(self):
        decision = WarehouseActionPolicy.can_take_processing(
            self._state(WarehouseStateCode.IN_PROCESSING_ZONE),
            role="processing_head",
            client_view=False,
        )

        self.assertTrue(decision.allowed)

    def test_can_take_processing_blocks_active_processing(self):
        decision = WarehouseActionPolicy.can_take_processing(
            self._state(WarehouseStateCode.PROCESSING_IN_PROGRESS),
            role="processing_head",
            client_view=False,
        )

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "state_forbidden")

    def test_can_create_processing_placement_blocks_stored_goods(self):
        decision = WarehouseActionPolicy.can_create_processing_placement(
            self._state(WarehouseStateCode.STORED),
            has_items=True,
        )

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "state_forbidden")


class WarehouseCommandServiceTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="warehouse_command_user", password="pwd")
        self.agency = Agency.objects.create(agn_name="Warehouse Command Agency")

    def _create_receiving_service_fact(self, order_id: str):
        service, _ = BillingService.objects.get_or_create(
            code="receiving_goods",
            defaults={"name": "Приемка товара", "unit": "шт", "is_active": True},
        )
        return WarehouseServiceFact.objects.create(
            client=self.agency,
            order_type=WarehouseServiceFact.ORDER_RECEIVING,
            order_id=order_id,
            service=service,
            service_name_snapshot=service.name,
            quantity=1,
            unit="шт",
            reported_by=self.user,
        )

    def test_complete_processing_if_started_blocks_without_services_before_operation_change(self):
        order_id = "PROC-CMD-SERVICES"
        operation = WarehouseOperation.objects.create(
            agency=self.agency,
            operation_type=WarehouseOperation.TYPE_PROCESSING,
            context_type="processing",
            context_id=order_id,
            status=WarehouseOperation.STATUS_IN_PROGRESS,
            requested_by=self.user,
            requested_by_role="processing_head",
        )

        with self.assertRaisesMessage(ValidationError, "Перед завершением"):
            WarehouseWritePathService.complete_processing_if_started(
                agency=self.agency,
                order_id=order_id,
                performed_by=self.user,
                performed_by_role="processing_head",
            )

        operation.refresh_from_db()
        self.assertEqual(operation.status, WarehouseOperation.STATUS_IN_PROGRESS)
        self.assertFalse(
            WarehouseEvent.objects.filter(
                agency=self.agency,
                stock_context_type="processing",
                stock_context_id=order_id,
            ).exists()
        )

    def test_complete_processing_blocks_direct_call_without_services(self):
        order_id = "PROC-CMD-DIRECT-SERVICES"
        operation = WarehouseOperation.objects.create(
            agency=self.agency,
            operation_type=WarehouseOperation.TYPE_PROCESSING,
            context_type="processing",
            context_id=order_id,
            status=WarehouseOperation.STATUS_IN_PROGRESS,
            requested_by=self.user,
            requested_by_role="processing_head",
        )

        with self.assertRaisesMessage(ValidationError, "Перед завершением"):
            WarehouseWritePathService.complete_processing(
                operation=operation,
                performed_by=self.user,
                performed_by_role="processing_head",
            )

        operation.refresh_from_db()
        self.assertEqual(operation.status, WarehouseOperation.STATUS_IN_PROGRESS)

    def _create_processing_snapshot(self, order_id: str, state_code: str):
        location = WarehouseWritePathService.ensure_location(
            warehouse_code="MSK",
            zone_code="OBR" if state_code != "stored" else "OS",
        )
        snapshot = WarehouseStockSnapshot.objects.create(
            agency=self.agency,
            source_context_type="legacy_stock",
            source_context_id=f"legacy-{order_id}",
            sku_code="SKU-CMD",
            name="Командный товар",
            size="42",
            barcode=f"20000000{order_id[-4:]}",
            goods_type="gv",
            qty=10,
            available_qty=0 if state_code != "stored" else 10,
            processing_reserved_qty=10 if state_code != "stored" else 0,
            container_code=f"PAL-{order_id}",
            location=location,
            zone_code=location.zone_code,
            zone_kind=location.zone_kind,
            warehouse_state_code=state_code,
        )
        WarehouseReserve.objects.create(
            agency=self.agency,
            reserve_type=WarehouseReserve.TYPE_PROCESSING,
            context_type="processing",
            context_id=order_id,
            sku_code=snapshot.sku_code,
            size=snapshot.size,
            barcode=snapshot.barcode,
            goods_type=snapshot.goods_type,
            qty_reserved=10,
            status=WarehouseReserve.STATUS_ACTIVE,
        )
        return snapshot

    def _create_receiving_snapshot(self, order_id: str, state_code: str):
        location = WarehouseWritePathService.ensure_location(
            warehouse_code="MSK",
            zone_code="PR" if state_code != "stored" else "OS",
        )
        return WarehouseStockSnapshot.objects.create(
            agency=self.agency,
            source_context_type="receiving",
            source_context_id=order_id,
            sku_code="SKU-RCV",
            name="Товар приемки",
            size="42",
            barcode=f"10000000{order_id[-4:]}",
            goods_type="op",
            qty=10,
            available_qty=10,
            processing_reserved_qty=0,
            shipping_reserved_qty=0,
            container_code=f"PAL-RCV-{order_id}",
            location=location,
            zone_code=location.zone_code,
            zone_kind=location.zone_kind,
            warehouse_state_code=state_code,
        )

    def _receiving_completion_kwargs(self, order_id: str) -> dict:
        self._create_receiving_service_fact(order_id)
        SKU.objects.get_or_create(
            agency=self.agency,
            sku_code="SKU-RCV-PARTIAL",
            defaults={"name": "Товар частичной приемки", "size": "42"},
        )
        boxes = [
            {
                "code": "BOX-RCV-PARTIAL-1",
                "sealed": True,
                "items": [
                    {
                        "sku": "SKU-RCV-PARTIAL",
                        "name": "Товар частичной приемки",
                        "size": "42",
                        "barcode": "BAR-RCV-PARTIAL-1",
                        "qty": 5,
                    }
                ],
            },
            {
                "code": "BOX-RCV-PARTIAL-2",
                "sealed": True,
                "items": [
                    {
                        "sku": "SKU-RCV-PARTIAL",
                        "name": "Товар частичной приемки",
                        "size": "42",
                        "barcode": "BAR-RCV-PARTIAL-2",
                        "qty": 7,
                    }
                ],
            },
        ]
        pallets = [
            {
                "code": "PAL-RCV-PARTIAL-1",
                "sealed": True,
                "boxes": ["BOX-RCV-PARTIAL-1"],
                "items": [],
                "location": {"zone": "PR"},
            },
            {
                "code": "PAL-RCV-PARTIAL-2",
                "sealed": True,
                "boxes": ["BOX-RCV-PARTIAL-2"],
                "items": [],
                "location": {"zone": "PR"},
            },
        ]
        return {
            "order_id": order_id,
            "agency": self.agency,
            "status_payload": {"status": "warehouse", "goods_type": "gv"},
            "has_mismatch": False,
            "receiving_mode": "standard",
            "act_items": [
                {
                    "sku_code": "SKU-RCV-PARTIAL",
                    "name": "Товар частичной приемки",
                    "size": "42",
                    "planned_qty": 12,
                    "actual_qty": 12,
                }
            ],
            "placement_items": [
                {
                    "sku_code": "SKU-RCV-PARTIAL",
                    "name": "Товар частичной приемки",
                    "size": "42",
                    "actual_qty": 12,
                    "box_qty": 12,
                    "pallet_qty": 0,
                }
            ],
            "boxes": boxes,
            "pallets": pallets,
            "flow_state": {"boxes": boxes, "pallets": pallets},
            "performed_by": self.user,
        }

    def test_start_receiving_flow_command_starts_for_receiving_state(self):
        order_id = "RCV-3001"
        self._create_receiving_snapshot(order_id, "received_unplaced")

        result = WarehouseCommandService.start_receiving_flow(
            order_id=order_id,
            agency=self.agency,
            role="storekeeper",
            status_payload={"status": "warehouse", "status_label": "В ожидании поставки товара"},
            flow_closed=False,
        )

        self.assertEqual(result.status, "started")
        self.assertEqual(result.state_code, WarehouseStateCode.PLACED_IN_RECEIVING)
        self.assertEqual(result.payload_update.get("status_label"), "Взята в работу")

    def test_start_receiving_flow_command_returns_already_started(self):
        order_id = "RCV-3002"
        self._create_receiving_snapshot(order_id, "received_unplaced")

        result = WarehouseCommandService.start_receiving_flow(
            order_id=order_id,
            agency=self.agency,
            role="storekeeper",
            status_payload={"status": "warehouse", "status_label": "Взята в работу"},
            flow_closed=False,
        )

        self.assertEqual(result.status, "already_in_progress")
        self.assertEqual(result.state_code, WarehouseStateCode.PLACED_IN_RECEIVING)
        self.assertEqual(result.payload_update.get("status_label"), "Взята в работу")

    def test_start_receiving_flow_command_denies_for_stored_goods(self):
        order_id = "RCV-3003"
        self._create_receiving_snapshot(order_id, "stored")

        result = WarehouseCommandService.start_receiving_flow(
            order_id=order_id,
            agency=self.agency,
            role="storekeeper",
            status_payload={"status": "warehouse", "status_label": "В ожидании поставки товара"},
            flow_closed=False,
        )

        self.assertEqual(result.status, "denied")
        self.assertEqual(result.reason, "state_forbidden")

    def test_reopen_receiving_flow_command_returns_payload_patch(self):
        order_id = "RCV-3003A"
        self._create_receiving_snapshot(order_id, "placed_in_receiving")

        result = WarehouseCommandService.reopen_receiving_flow(
            order_id=order_id,
            agency=self.agency,
            role="storekeeper",
            status_payload={"status": "warehouse", "status_label": "Размещение на складе"},
            flow_closed=True,
            flow_closed_at="2026-04-22T10:00:00+03:00",
        )

        self.assertEqual(result.status, "reopened")
        self.assertTrue(result.payload_update.get("flow_reopened"))
        self.assertTrue(result.payload_update.get("flow_reopened_at"))
        self.assertEqual(result.meta.get("order_id"), order_id)
        self.assertEqual(result.meta.get("flow_closed_at"), "2026-04-22T10:00:00+03:00")

    def test_reopen_receiving_flow_command_denies_wrong_role(self):
        order_id = "RCV-3003B"
        self._create_receiving_snapshot(order_id, "placed_in_receiving")

        result = WarehouseCommandService.reopen_receiving_flow(
            order_id=order_id,
            agency=self.agency,
            role="manager",
            status_payload={"status": "warehouse", "status_label": "Размещение на складе"},
            flow_closed=True,
        )

        self.assertEqual(result.status, "denied")
        self.assertEqual(result.reason, "role_forbidden")

    def test_send_receiving_to_storage_command_allows_putaway_creation(self):
        order_id = "RCV-3004"
        self._create_receiving_snapshot(order_id, "placed_in_receiving")

        result = WarehouseCommandService.send_receiving_to_storage(
            order_id=order_id,
            agency=self.agency,
            role="storekeeper",
            status_payload={"status": "warehouse", "status_label": "Размещение на складе"},
            flow_closed=True,
            not_created_count=1,
        )

        self.assertEqual(result.status, "ready")
        self.assertEqual(result.state_code, WarehouseStateCode.PLACED_IN_RECEIVING)

    def test_send_receiving_to_storage_command_denies_for_stored_goods(self):
        order_id = "RCV-3005"
        self._create_receiving_snapshot(order_id, "stored")

        result = WarehouseCommandService.send_receiving_to_storage(
            order_id=order_id,
            agency=self.agency,
            role="storekeeper",
            status_payload={"status": "warehouse", "status_label": "Товар принят и размещен на складе"},
            flow_closed=True,
            not_created_count=1,
        )

        self.assertEqual(result.status, "denied")
        self.assertEqual(result.reason, "state_forbidden")

    def test_create_receiving_putaway_tasks_creates_move_and_warehouse_bridge(self):
        order_id = "RCV-3005A"
        SKU.objects.create(agency=self.agency, sku_code="SKU-BRIDGE-CMD", name="Товар моста", size="42")

        result = WarehouseCommandService.create_receiving_putaway_tasks(
            order_id=order_id,
            agency=self.agency,
            role="storekeeper",
            placement_payload={
                "act": "placement",
                "act_state": "closed",
                "act_boxes": [
                    {
                        "code": "BOX-BRIDGE-CMD",
                        "sealed": True,
                        "items": [
                            {
                                "sku": "SKU-BRIDGE-CMD",
                                "name": "Товар моста",
                                "size": "42",
                                "barcode": "BR-CMD-1",
                                "qty": 5,
                            }
                        ],
                    }
                ],
                "act_pallets": [
                    {
                        "code": "PAL-BRIDGE-CMD",
                        "sealed": True,
                        "boxes": ["BOX-BRIDGE-CMD"],
                        "items": [],
                        "location": {"zone": "OS", "row": 2, "section": 1, "tier": 1, "cell": 3},
                    }
                ],
            },
            requested_by=self.user,
            requested_by_name="Исполнитель склада",
            requested_by_role="storekeeper",
            latest_moves_by_pallet={},
            flow_closed=True,
            not_created_count=1,
        )

        self.assertEqual(result.status, "created")
        self.assertEqual(result.created_count, 1)
        task = MoveTask.objects.get()
        operation = WarehouseOperation.objects.get(context_type="receiving", context_id=order_id)
        warehouse_task = operation.tasks.get()
        self.assertEqual(task.pallet_code, "PAL-BRIDGE-CMD")
        self.assertEqual(operation.operation_type, WarehouseOperation.TYPE_PUTAWAY)
        self.assertEqual(warehouse_task.payload.get("legacy_move_id"), task.legacy_order_id)
        self.assertEqual((task.payload or {}).get("warehouse_operation_id"), operation.id)
        self.assertEqual((task.payload or {}).get("warehouse_operation_task_id"), warehouse_task.id)

    def test_create_receiving_putaway_tasks_skips_reserved_os_destination(self):
        SKU.objects.create(agency=self.agency, sku_code="SKU-RESERVED-CMD", name="Товар резерва", size="42")
        WarehouseWritePathService.create_receiving_placement(
            agency=self.agency,
            order_id="RCV-RESERVED-1",
            performed_by=self.user,
            items=[
                {
                    "sku_code": "SKU-RESERVED-CMD",
                    "name": "Товар резерва",
                    "size": "42",
                    "barcode": "BR-RES-1",
                    "goods_type": "gv",
                    "qty": 5,
                    "pallet_code": "PAL-RESERVED-1",
                }
            ],
        )
        WarehouseWritePathService.request_putaway_for_receiving(
            agency=self.agency,
            order_id="RCV-RESERVED-1",
            container_codes=["PAL-RESERVED-1"],
            destination_zone_code="OS",
            destination_row_no=2,
            destination_section_no=1,
            destination_tier_no=1,
            destination_cell_no=3,
            requested_by=self.user,
            requested_by_role="storekeeper",
        )

        result = WarehouseCommandService.create_receiving_putaway_tasks(
            order_id="RCV-RESERVED-2",
            agency=self.agency,
            role="storekeeper",
            placement_payload={
                "act": "placement",
                "act_state": "closed",
                "act_boxes": [
                    {
                        "code": "BOX-RESERVED-CMD-2",
                        "sealed": True,
                        "items": [
                            {
                                "sku": "SKU-RESERVED-CMD",
                                "name": "Товар резерва",
                                "size": "42",
                                "barcode": "BR-RES-2",
                                "qty": 5,
                            }
                        ],
                    }
                ],
                "act_pallets": [
                    {
                        "code": "PAL-RESERVED-CMD-2",
                        "sealed": True,
                        "boxes": ["BOX-RESERVED-CMD-2"],
                        "items": [],
                        "location": {"zone": "OS", "row": 2, "section": 1, "tier": 1, "cell": 3},
                    }
                ],
            },
            requested_by=self.user,
            requested_by_name="Исполнитель склада",
            requested_by_role="storekeeper",
            latest_moves_by_pallet={},
            flow_closed=True,
            not_created_count=1,
        )

        self.assertEqual(result.status, "noop")
        self.assertEqual(result.created_count, 0)
        self.assertEqual(result.skipped_existing_count, 1)
        self.assertFalse(MoveTask.objects.exists())

    def test_open_receiving_placement_command_clears_contexts(self):
        order_id = "RCV-3006"
        self._create_receiving_snapshot(order_id, "placed_in_receiving")
        OperationalStockService.replace_order_placement(
            self.agency,
            "receiving",
            order_id,
            {
                "act_items": [{"sku": "SKU-RCV", "name": "Товар приемки", "size": "42", "qty": 10}],
                "act_boxes": [],
                "act_pallets": [
                    {
                        "code": "PAL-RCV-3006",
                        "items": [{"sku": "SKU-RCV", "name": "Товар приемки", "size": "42", "qty": 10}],
                        "boxes": [],
                        "sealed": True,
                        "location": {"zone": "PR"},
                    }
                ],
            },
        )

        result = WarehouseCommandService.open_receiving_placement(
            order_id=order_id,
            agency=self.agency,
            role="storekeeper",
            act_state="closed",
            signed_by_storekeeper=False,
            status_payload={"status": "warehouse", "status_label": "Размещение на складе"},
        )

        self.assertEqual(result.status, "opened")
        self.assertEqual(result.payload_update.get("act_state"), "open")
        self.assertFalse(
            WarehouseStockSnapshot.objects.filter(
                agency=self.agency,
                source_context_type="receiving",
                source_context_id=order_id,
            ).exists()
        )

    def test_open_receiving_placement_command_returns_already_open(self):
        order_id = "RCV-3007"
        self._create_receiving_snapshot(order_id, "placed_in_receiving")

        result = WarehouseCommandService.open_receiving_placement(
            order_id=order_id,
            agency=self.agency,
            role="storekeeper",
            act_state="open",
            signed_by_storekeeper=False,
            status_payload={"status": "warehouse", "status_label": "Размещение на складе"},
        )

        self.assertEqual(result.status, "already_open")
        self.assertEqual(result.payload_update.get("act_state"), "open")

    def test_complete_receiving_flow_command_syncs_operational_and_warehouse_state(self):
        order_id = "RCV-3008"
        self._create_receiving_service_fact(order_id)
        SKU.objects.create(
            agency=self.agency,
            sku_code="SKU-RCV-COMP",
            name="Товар закрытия приемки",
            size="42",
        )

        result = WarehouseCommandService.complete_receiving_flow(
            order_id=order_id,
            agency=self.agency,
            status_payload={
                "status": "warehouse",
                "status_label": "Взята в работу",
                "goods_type": "op",
            },
            has_mismatch=False,
            receiving_mode="cz",
            act_items=[
                {
                    "sku_code": "SKU-RCV-COMP",
                    "name": "Товар закрытия приемки",
                    "size": "42",
                    "planned_qty": 1,
                    "actual_qty": 1,
                    "comment": "",
                }
            ],
            placement_items=[
                {
                    "sku_code": "SKU-RCV-COMP",
                    "name": "Товар закрытия приемки",
                    "size": "42",
                    "actual_qty": 1,
                    "box_qty": 1,
                    "pallet_qty": 1,
                    "comment": "",
                }
            ],
            boxes=[
                {
                    "code": "BOX-COMP-1",
                    "sealed": True,
                    "items": [
                        {
                            "sku": "SKU-RCV-COMP",
                            "name": "Товар закрытия приемки",
                            "size": "42",
                            "barcode": "CZ-COMP-1",
                            "marking_code": "CZ-3008-1",
                            "qty": 1,
                        }
                    ],
                }
            ],
            pallets=[
                {
                    "code": "PAL-COMP-1",
                    "sealed": True,
                    "boxes": ["BOX-COMP-1"],
                    "items": [],
                    "location": {"zone": "PR"},
                }
            ],
            flow_state={"boxes": [], "pallets": []},
            act_units=[
                {
                    "sku_code": "SKU-RCV-COMP",
                    "name": "Товар закрытия приемки",
                    "size": "42",
                    "barcode": "CZ-COMP-1",
                    "marking_code": "CZ-3008-1",
                    "box_code": "BOX-COMP-1",
                    "pallet_code": "PAL-COMP-1",
                }
            ],
            eta_at="2026-04-22T10:00:00+03:00",
            vehicle_number="A123AA790",
            has_closed_placement_act=False,
            performed_by=self.user,
        )

        self.assertEqual(result.status, "completed")
        self.assertEqual(result.state_code, WarehouseStateCode.PLACED_IN_RECEIVING)
        self.assertTrue(result.payload_update.get("flow_closed"))
        self.assertEqual(result.payload_update.get("act"), "receiving")
        self.assertEqual(result.payload_update.get("status_label"), "Товар принят")
        self.assertFalse(result.meta.get("placement_previously_closed"))

        placement_payload = result.meta.get("placement_payload") or {}
        self.assertEqual(placement_payload.get("act"), "placement")
        self.assertEqual(placement_payload.get("act_state"), "closed")
        self.assertEqual(len(placement_payload.get("act_units") or []), 1)

        snapshot = WarehouseStockSnapshot.objects.get(
            agency=self.agency,
            source_context_type="receiving",
            source_context_id=order_id,
            marking_code="CZ-3008-1",
        )
        self.assertEqual(snapshot.sku_code, "SKU-RCV-COMP")
        self.assertEqual(snapshot.qty, 1)
        self.assertEqual(snapshot.available_qty, 1)
        self.assertEqual(snapshot.container_code, "BOX-COMP-1")
        self.assertEqual(snapshot.container.container_code, "BOX-COMP-1")
        self.assertEqual(snapshot.parent_container.container_code, "PAL-COMP-1")
        self.assertEqual(snapshot.zone_code, "PR")
        self.assertEqual(snapshot.warehouse_state_code, "placed_in_receiving")

    def test_complete_receiving_flow_command_blocks_before_stock_write_without_services(self):
        order_id = "RCV-NO-SERVICES"
        kwargs = self._receiving_completion_kwargs(order_id)
        WarehouseServiceFact.objects.filter(client=self.agency, order_id=order_id).delete()

        with self.assertRaisesMessage(ValidationError, "Перед завершением"):
            WarehouseCommandService.complete_receiving_flow(**kwargs)

        self.assertFalse(
            WarehouseStockSnapshot.objects.filter(
                agency=self.agency,
                source_context_type="receiving",
                source_context_id=order_id,
            ).exists()
        )

    def test_complete_receiving_flow_requires_permission_for_each_pallet(self):
        order_id = "RCV-MISSING-PR-PALLET"
        kwargs = self._receiving_completion_kwargs(order_id)
        WarehouseLocation.objects.create(
            warehouse_code="MSK",
            zone_code="PR",
            zone_kind=WarehouseLocation.ZONE_KIND_RECEIVING,
            row_no=9,
            section_no=1,
            tier_no=1,
            cell_no=1,
            location_code="PR-COMMAND-09",
            display_name="PR · Командная проверка 9",
            is_active=True,
        )
        kwargs.update(
            {
                "receiving_location_code": "PR-COMMAND-09",
                "concrete_location_required": True,
            }
        )
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="receiving",
            action="update",
            agency=self.agency,
            user=self.user,
            description="Место указано только для первой палеты",
            payload={
                "event": "receiving_pallet_placement_permission",
                "pallet_code": "PAL-RCV-PARTIAL-1",
                "pallet_placement_allowed": True,
                "receiving_location_code": "PR-COMMAND-09",
            },
        )

        with self.assertRaisesMessage(
            ValueError,
            "PAL-RCV-PARTIAL-2",
        ):
            WarehouseCommandService.complete_receiving_flow(**kwargs)

        self.assertFalse(
            WarehouseStockSnapshot.objects.filter(
                agency=self.agency,
                source_context_type="receiving",
                source_context_id=order_id,
            ).exists()
        )

    def test_complete_receiving_flow_repairs_partial_stock_without_duplicates(self):
        order_id = "RCV-PARTIAL-REPAIR"
        kwargs = self._receiving_completion_kwargs(order_id)
        initial = WarehouseWritePathService.create_receiving_placement(
            agency=self.agency,
            order_id=order_id,
            items=[
                {
                    "sku_code": "SKU-RCV-PARTIAL",
                    "name": "Товар частичной приемки",
                    "size": "42",
                    "barcode": "BAR-RCV-PARTIAL-1",
                    "goods_type": "gv",
                    "qty": 5,
                    "pallet_code": "PAL-RCV-PARTIAL-1",
                    "box_code": "BOX-RCV-PARTIAL-1",
                }
            ],
            performed_by=self.user,
            source_document_type="placement_act",
            source_document_id=order_id,
            stock_context_type="receiving",
        )
        existing_snapshot_id = initial.snapshot_ids[0]

        result = WarehouseCommandService.complete_receiving_flow(**kwargs)

        self.assertEqual(result.status, "completed")
        snapshots = WarehouseStockSnapshot.objects.filter(
            agency=self.agency,
            source_context_type="receiving",
            source_context_id=order_id,
            is_archived=False,
        )
        self.assertEqual(snapshots.count(), 2)
        self.assertEqual(sum(snapshots.values_list("qty", flat=True)), 12)
        self.assertTrue(snapshots.filter(id=existing_snapshot_id).exists())
        self.assertTrue(snapshots.filter(container_code="BOX-RCV-PARTIAL-2").exists())
        self.assertTrue(
            WarehouseWritePathService.receiving_placement_coverage(
                agency=self.agency,
                order_id=order_id,
                placement_payload=result.meta["placement_payload"],
            )["complete"]
        )

        retry = WarehouseCommandService.complete_receiving_flow(**kwargs)

        self.assertEqual(retry.status, "already_completed")
        self.assertEqual(snapshots.count(), 2)

    def test_complete_receiving_flow_accepts_materialized_pallets_after_stock_left(self):
        order_id = "RCV-MATERIALIZED-LEFT"
        kwargs = self._receiving_completion_kwargs(order_id)
        for pallet_code in ("PAL-RCV-PARTIAL-1", "PAL-RCV-PARTIAL-2"):
            OrderAuditEntry.objects.create(
                order_id=order_id,
                order_type="receiving",
                action="update",
                agency=self.agency,
                user=self.user,
                description=f"Паллета {pallet_code} поставлена на остатки в зоне приемки",
                payload={
                    "event": ReceivingWorkflowService.PALLET_STOCK_MATERIALIZED_EVENT,
                    "pallet_code": pallet_code,
                    "pallet_stock_materialized": True,
                },
            )

        result = WarehouseCommandService.complete_receiving_flow(**kwargs)

        self.assertEqual(result.status, "already_completed")
        self.assertFalse(
            WarehouseStockSnapshot.objects.filter(
                agency=self.agency,
                source_context_type="receiving",
                source_context_id=order_id,
                is_archived=False,
                qty__gt=0,
            ).exists(),
            "Уехавшие материализованные паллеты нельзя оприходовать повторно",
        )

    def test_complete_receiving_flow_creates_only_unmaterialized_pallet(self):
        order_id = "RCV-MATERIALIZED-PARTIAL"
        kwargs = self._receiving_completion_kwargs(order_id)
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="receiving",
            action="update",
            agency=self.agency,
            user=self.user,
            description="Паллета PAL-RCV-PARTIAL-1 поставлена на остатки в зоне приемки",
            payload={
                "event": ReceivingWorkflowService.PALLET_STOCK_MATERIALIZED_EVENT,
                "pallet_code": "PAL-RCV-PARTIAL-1",
                "pallet_stock_materialized": True,
            },
        )

        result = WarehouseCommandService.complete_receiving_flow(**kwargs)

        self.assertEqual(result.status, "completed")
        snapshots = WarehouseStockSnapshot.objects.filter(
            agency=self.agency,
            source_context_type="receiving",
            source_context_id=order_id,
            is_archived=False,
            qty__gt=0,
        )
        self.assertFalse(snapshots.filter(container_code="BOX-RCV-PARTIAL-1").exists())
        self.assertEqual(
            list(snapshots.values_list("container_code", "qty")),
            [("BOX-RCV-PARTIAL-2", 7)],
        )

    def test_complete_receiving_flow_rolls_back_when_existing_stock_has_extra_box(self):
        order_id = "RCV-PARTIAL-CONFLICT"
        kwargs = self._receiving_completion_kwargs(order_id)
        WarehouseWritePathService.create_receiving_placement(
            agency=self.agency,
            order_id=order_id,
            items=[
                {
                    "sku_code": "SKU-RCV-PARTIAL",
                    "name": "Лишний товар",
                    "size": "42",
                    "barcode": "BAR-RCV-EXTRA",
                    "goods_type": "gv",
                    "qty": 3,
                    "pallet_code": "PAL-RCV-EXTRA",
                    "box_code": "BOX-RCV-EXTRA",
                }
            ],
            performed_by=self.user,
            source_document_type="placement_act",
            source_document_id=order_id,
            stock_context_type="receiving",
        )

        with self.assertRaisesRegex(ValueError, "лишние"):
            WarehouseCommandService.complete_receiving_flow(**kwargs)

        snapshots = WarehouseStockSnapshot.objects.filter(
            agency=self.agency,
            source_context_type="receiving",
            source_context_id=order_id,
            is_archived=False,
        )
        self.assertEqual(snapshots.count(), 1)
        self.assertTrue(snapshots.filter(container_code="BOX-RCV-EXTRA").exists())
        self.assertFalse(snapshots.filter(container_code="BOX-RCV-PARTIAL-1").exists())
        self.assertFalse(snapshots.filter(container_code="BOX-RCV-PARTIAL-2").exists())

    def test_close_receiving_placement_command_replaces_existing_receiving_context(self):
        order_id = "RCV-3009"
        SKU.objects.create(
            agency=self.agency,
            sku_code="SKU-RCV-CLOSE",
            name="Товар закрытия размещения",
            size="43",
        )
        self._create_receiving_snapshot(order_id, "placed_in_receiving")

        result = WarehouseCommandService.close_receiving_placement(
            order_id=order_id,
            agency=self.agency,
            status_payload={
                "status": "warehouse",
                "status_label": "Размещение на складе",
                "act": "placement",
                "act_state": "open",
                "goods_type": "op",
            },
            placement_items=[
                {
                    "sku_code": "SKU-RCV-CLOSE",
                    "name": "Товар закрытия размещения",
                    "size": "43",
                    "actual_qty": 4,
                    "box_qty": 4,
                    "pallet_qty": 4,
                    "comment": "",
                }
            ],
            boxes=[
                {
                    "code": "BOX-CLOSE-1",
                    "sealed": True,
                    "items": [
                        {
                            "sku": "SKU-RCV-CLOSE",
                            "name": "Товар закрытия размещения",
                            "size": "43",
                            "barcode": "CLOSE-1",
                            "qty": 4,
                        }
                    ],
                }
            ],
            pallets=[
                {
                    "code": "PAL-CLOSE-1",
                    "sealed": True,
                    "boxes": ["BOX-CLOSE-1"],
                    "items": [],
                    "location": {"zone": "PR"},
                }
            ],
            has_closed_act=True,
            performed_by=self.user,
        )

        self.assertEqual(result.status, "closed")
        self.assertEqual(result.state_code, WarehouseStateCode.PLACED_IN_RECEIVING)
        self.assertEqual(result.payload_update.get("act"), "placement")
        self.assertEqual(result.payload_update.get("act_state"), "closed")
        self.assertEqual(result.payload_update.get("status_label"), "Товар принят и размещен на складе")
        self.assertTrue(result.meta.get("placement_previously_closed"))
        self.assertFalse(
            WarehouseStockSnapshot.objects.filter(
                agency=self.agency,
                source_context_type="receiving",
                source_context_id=order_id,
                sku_code="SKU-RCV",
            ).exists()
        )

        snapshots = list(
            WarehouseStockSnapshot.objects.filter(
                agency=self.agency,
                source_context_type="receiving",
                source_context_id=order_id,
            ).order_by("id")
        )
        self.assertEqual(len(snapshots), 1)
        self.assertEqual(snapshots[0].sku_code, "SKU-RCV-CLOSE")
        self.assertEqual(snapshots[0].qty, 4)
        self.assertEqual(snapshots[0].container_code, "BOX-CLOSE-1")
        self.assertEqual(snapshots[0].container.container_code, "BOX-CLOSE-1")
        self.assertEqual(snapshots[0].parent_container.container_code, "PAL-CLOSE-1")
        self.assertEqual(snapshots[0].zone_code, "PR")
        self.assertEqual(snapshots[0].warehouse_state_code, "placed_in_receiving")

    def test_take_processing_command_starts_processing(self):
        order_id = "CMD-2001"
        snapshot = self._create_processing_snapshot(order_id, "in_processing_zone")

        result = WarehouseCommandService.take_processing(
            order_id=order_id,
            agency=self.agency,
            role="processing_head",
            status_payload={"status": "processing_head", "status_label": "Передано в обработку"},
            started_by=self.user,
        )

        self.assertEqual(result.status, "started")
        snapshot.refresh_from_db()
        self.assertEqual(snapshot.warehouse_state_code, "processing_in_progress")

    def test_take_processing_command_returns_already_started(self):
        order_id = "CMD-2002"
        self._create_processing_snapshot(order_id, "processing_in_progress")

        result = WarehouseCommandService.take_processing(
            order_id=order_id,
            agency=self.agency,
            role="processing_head",
            status_payload={"status": "processing_head", "status_label": "Передано в обработку"},
            started_by=self.user,
        )

        self.assertEqual(result.status, "already_in_progress")
        self.assertEqual(result.state_code, WarehouseStateCode.PROCESSING_IN_PROGRESS)

    def test_take_processing_command_denies_for_stored_goods(self):
        order_id = "CMD-2003"
        self._create_processing_snapshot(order_id, "stored")

        result = WarehouseCommandService.take_processing(
            order_id=order_id,
            agency=self.agency,
            role="processing_head",
            status_payload={"status": "processing_head", "status_label": "Передано в обработку"},
            started_by=self.user,
        )

        self.assertEqual(result.status, "denied")
        self.assertEqual(result.reason, "state_forbidden")

    def test_refresh_materialized_stock_state_reports_warehouse_core_totals(self):
        create_warehouse_snapshot_row(
            agency=self.agency,
            order_id="1000",
            sku="SKU-1",
            name="Товар 1",
            size="42",
            goods_type="Не обработанный",
            qty=50,
            available_qty=30,
            shipping_reserved_qty=20,
            pallet_code="PAL-1000",
        )
        WarehouseReserve.objects.create(
            agency=self.agency,
            reserve_type=WarehouseReserve.TYPE_SHIPPING,
            context_type="shipping",
            context_id="SO-TEST-2",
            sku_code="SKU-1",
            size="42",
            goods_type="Не обработанный",
            qty_reserved=20,
            status=WarehouseReserve.STATUS_ACTIVE,
        )

        result = refresh_materialized_stock_state_for_agency(self.agency)

        self.assertEqual(result["source"], "warehouse_core")
        self.assertEqual(result["snapshot_count"], 1)
        self.assertEqual(result["qty"], 50)
        self.assertEqual(result["available_qty"], 30)
        self.assertEqual(result["processing_reserved_qty"], 0)
        self.assertEqual(result["shipping_reserved_qty"], 20)
        self.assertEqual(result["reserve_count"], 1)

    def test_occupied_os_cells_deduplicates_coordinates(self):
        create_warehouse_snapshot_row(
            agency=self.agency,
            order_type="receiving",
            order_id="3000",
            sku="SKU-2",
            name="Товар 2",
            size="43",
            goods_type="Оптовый",
            qty=10,
            pallet_code="PAL-3000",
            zone="OS",
            row=1,
            section=2,
            tier=3,
            cell=4,
        )
        create_warehouse_snapshot_row(
            agency=self.agency,
            order_type="processing",
            order_id="3001",
            sku="SKU-3",
            name="Товар 3",
            size="44",
            goods_type="Готовый",
            qty=5,
            pallet_code="PAL-3001",
            zone="OS",
            row=1,
            section=2,
            tier=3,
            cell=4,
        )
        create_warehouse_snapshot_row(
            agency=self.agency,
            order_type="processing",
            order_id="3002",
            sku="SKU-4",
            name="Товар 4",
            size="45",
            goods_type="Готовый",
            qty=7,
            pallet_code="PAL-3002",
            zone="OS",
            row=1,
            section=2,
            tier=3,
            cell=5,
        )

        occupied_cells = StockAvailabilityService.occupied_os_cells()
        self.assertEqual(
            occupied_cells,
            [
                {"row": 1, "section": 2, "tier": 3, "cell": 4},
                {"row": 1, "section": 2, "tier": 3, "cell": 5},
            ],
        )

    def test_occupied_os_cells_excludes_current_order(self):
        create_warehouse_snapshot_row(
            agency=self.agency,
            order_type="processing",
            order_id="4001",
            sku="SKU-5",
            name="Товар 5",
            size="46",
            goods_type="Готовый",
            qty=9,
            pallet_code="PAL-4001",
            zone="OS",
            row=2,
            section=1,
            tier=1,
            cell=1,
        )

        keys = StockAvailabilityService.occupied_os_cell_keys(
            exclude_order_type="processing",
            exclude_order_id="4001",
        )
        self.assertNotIn((2, 1, 1, 1), keys)

    def test_occupied_os_cells_include_active_putaway_destinations(self):
        destination = WarehouseWritePathService.ensure_location(
            warehouse_code="MSK",
            zone_code="OS",
            row_no=4,
            section_no=2,
            tier_no=1,
            cell_no=3,
        )
        WarehouseOperation.objects.create(
            agency=self.agency,
            operation_type=WarehouseOperation.TYPE_PUTAWAY,
            context_type="receiving",
            context_id="PUTAWAY-RESERVED-1",
            destination_location=destination,
            destination_zone_code="OS",
            status=WarehouseOperation.STATUS_PLANNED,
        )

        keys = StockAvailabilityService.occupied_os_cell_keys()
        sections = StockAvailabilityService.occupied_os_section_agencies()

        self.assertIn((4, 2, 1, 3), keys)
        self.assertEqual(sections[(4, 2)], {self.agency.id})

    def test_occupied_os_cells_excludes_pallet_code(self):
        create_warehouse_snapshot_row(
            agency=self.agency,
            order_type="receiving",
            order_id="5001",
            sku="SKU-6",
            name="Товар 6",
            size="47",
            goods_type="Оптовый",
            qty=4,
            pallet_code="PAL-KEEP",
            zone="OS",
            row=3,
            section=1,
            tier=1,
            cell=1,
        )
        create_warehouse_snapshot_row(
            agency=self.agency,
            order_type="receiving",
            order_id="5002",
            sku="SKU-7",
            name="Товар 7",
            size="48",
            goods_type="Оптовый",
            qty=4,
            pallet_code="PAL-EXCLUDE",
            zone="OS",
            row=3,
            section=1,
            tier=1,
            cell=2,
        )

        keys = StockAvailabilityService.occupied_os_cell_keys(exclude_pallet_code="PAL-EXCLUDE")
        self.assertIn((3, 1, 1, 1), keys)
        self.assertNotIn((3, 1, 1, 2), keys)

    def test_suggest_os_cell_prefers_same_client_section(self):
        other_agency = Agency.objects.create(agn_name="Другой клиент")
        create_warehouse_snapshot_row(
            agency=self.agency,
            order_type="receiving",
            order_id="6001",
            sku="SKU-A",
            name="Товар A",
            size="42",
            goods_type="Оптовый",
            qty=4,
            pallet_code="PAL-A",
            zone="OS",
            row=1,
            section=1,
            tier=1,
            cell=1,
        )
        create_warehouse_snapshot_row(
            agency=other_agency,
            order_type="receiving",
            order_id="6002",
            sku="SKU-B",
            name="Товар B",
            size="42",
            goods_type="Оптовый",
            qty=4,
            pallet_code="PAL-B",
            zone="OS",
            row=1,
            section=2,
            tier=1,
            cell=1,
        )

        suggestion = StockAvailabilityService.suggest_os_cell_for_agency(
            agency_id=self.agency.id,
            row_sections={1: 3},
            tiers=1,
            cells_per_tier=2,
            occupied_keys=StockAvailabilityService.occupied_os_cell_keys(),
            section_agencies=StockAvailabilityService.occupied_os_section_agencies(),
        )

        self.assertEqual(
            suggestion,
            {"zone": "OS", "row": 1, "section": 1, "tier": 1, "cell": 2},
        )

    def test_suggest_os_cell_prefers_empty_section_before_other_client_section(self):
        other_agency = Agency.objects.create(agn_name="Клиент B")
        create_warehouse_snapshot_row(
            agency=other_agency,
            order_type="receiving",
            order_id="7001",
            sku="SKU-C",
            name="Товар C",
            size="42",
            goods_type="Оптовый",
            qty=4,
            pallet_code="PAL-C",
            zone="OS",
            row=1,
            section=1,
            tier=1,
            cell=1,
        )
        create_warehouse_snapshot_row(
            agency=other_agency,
            order_type="receiving",
            order_id="7002",
            sku="SKU-D",
            name="Товар D",
            size="42",
            goods_type="Оптовый",
            qty=4,
            pallet_code="PAL-D",
            zone="OS",
            row=1,
            section=3,
            tier=1,
            cell=1,
        )

        suggestion = StockAvailabilityService.suggest_os_cell_for_agency(
            agency_id=self.agency.id,
            row_sections={1: 3},
            tiers=1,
            cells_per_tier=2,
            occupied_keys=StockAvailabilityService.occupied_os_cell_keys(),
            section_agencies=StockAvailabilityService.occupied_os_section_agencies(),
        )

        self.assertEqual(
            suggestion,
            {"zone": "OS", "row": 1, "section": 2, "tier": 1, "cell": 1},
        )


class ClientInventoryJournalReserveColumnsTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="client_inventory_user",
            password="x",
        )
        self.agency = Agency.objects.create(
            agn_name="Индивидуальный предприниматель Тестов Тест",
            portal_user=self.user,
        )
        self.sku = SKU.objects.create(
            agency=self.agency,
            sku_code="SKU-1",
            name="Джинсы",
            size="42",
        )
        self.client.force_login(self.user)

    def test_client_inventory_journal_shows_separate_obr_and_otg_reserves(self):
        create_warehouse_snapshot_row(
            agency=self.agency,
            order_id="1001",
            sku="SKU-1",
            name="Джинсы",
            size="42",
            goods_type="Оптовый",
            qty=10,
        )
        WarehouseReserve.objects.create(
            agency=self.agency,
            reserve_type=WarehouseReserve.TYPE_PROCESSING,
            context_type="processing",
            context_id="2001",
            sku_code="SKU-1",
            size="42",
            goods_type="Оптовый",
            qty_reserved=2,
            status=WarehouseReserve.STATUS_ACTIVE,
        )
        WarehouseReserve.objects.create(
            agency=self.agency,
            reserve_type=WarehouseReserve.TYPE_SHIPPING,
            context_type="shipping",
            context_id="SO-000001",
            sku_code="SKU-1",
            size="42",
            goods_type="Оптовый",
            qty_reserved=3,
            status=WarehouseReserve.STATUS_ACTIVE,
        )
        WarehouseReserve.objects.create(
            agency=self.agency,
            reserve_type=WarehouseReserve.TYPE_SHIPPING,
            context_type="shipping",
            context_id="SO-000002",
            sku_code="SKU-1",
            size="42",
            goods_type="Оптовый",
            qty_reserved=9,
            status=WarehouseReserve.STATUS_CANCELED,
        )

        response = self.client.get("/sklad/journal/")

        self.assertEqual(response.status_code, 200)
        rows = response.context["rows"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["qty"], 10)
        self.assertEqual(rows[0]["processing_reserved_qty"], 2)
        self.assertEqual(rows[0]["shipping_reserved_qty"], 3)
        self.assertEqual(rows[0]["available_qty"], 5)
        self.assertContains(response, "Резерв OBR")
        self.assertContains(response, "Резерв OTG")
        self.assertContains(response, "Доступный остаток")

    def test_client_inventory_journal_uses_warehouse_processing_reserve_when_present(self):
        create_warehouse_snapshot_row(
            agency=self.agency,
            order_id="1002",
            sku="SKU-1",
            name="Джинсы",
            size="42",
            goods_type="Оптовый",
            qty=10,
        )
        WarehouseReserve.objects.create(
            agency=self.agency,
            reserve_type=WarehouseReserve.TYPE_PROCESSING,
            context_type="processing",
            context_id="2002",
            sku_code="SKU-1",
            size="42",
            goods_type="Оптовый",
            qty_reserved=4,
            status=WarehouseReserve.STATUS_ACTIVE,
        )

        response = self.client.get("/sklad/journal/")

        self.assertEqual(response.status_code, 200)
        rows = response.context["rows"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["qty"], 10)
        self.assertEqual(rows[0]["processing_reserved_qty"], 4)
        self.assertEqual(rows[0]["available_qty"], 6)

    def test_client_inventory_journal_uses_warehouse_snapshot_rows_when_legacy_rows_missing(self):
        location = WarehouseLocation.objects.create(
            warehouse_code="MSK",
            zone_code="OS",
            zone_kind=WarehouseLocation.ZONE_KIND_STORAGE,
            row_no=3,
            section_no=2,
            tier_no=1,
            cell_no=1,
            display_name="OS · Ряд 3 · Секция 2 · Ярус 1 · Ячейка 1",
        )
        pallet = WarehouseContainer.objects.create(
            agency=self.agency,
            container_type=WarehouseContainer.TYPE_PALLET,
            container_code="PAL-JRN-1",
            current_location=location,
        )
        box = WarehouseContainer.objects.create(
            agency=self.agency,
            container_type=WarehouseContainer.TYPE_BOX,
            container_code="BOX-JRN-1",
            parent_container=pallet,
            current_location=location,
        )
        WarehouseStockSnapshot.objects.create(
            agency=self.agency,
            source_context_type="receiving",
            source_context_id="R-JRN-1",
            sku_code="SKU-3",
            name="Худи",
            size="46",
            goods_type="Готовый",
            qty=7,
            available_qty=7,
            container=box,
            container_code=box.container_code,
            parent_container=pallet,
            location=location,
            zone_code="OS",
            zone_kind=location.zone_kind,
            warehouse_state_code=WarehouseStateCode.STORED.value,
        )

        response = self.client.get("/sklad/journal/")

        self.assertEqual(response.status_code, 200)
        rows = response.context["rows"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["sku"], "SKU-3")
        self.assertEqual(rows[0]["qty"], 7)
        self.assertEqual(rows[0]["pallet_code"], "PAL-JRN-1")

    def test_client_inventory_journal_shows_reserved_only_row(self):
        WarehouseReserve.objects.create(
            agency=self.agency,
            reserve_type=WarehouseReserve.TYPE_SHIPPING,
            context_type="shipping",
            context_id="SO-000003",
            sku_code="SKU-2",
            size="44",
            goods_type="Готовый",
            qty_reserved=5,
            status=WarehouseReserve.STATUS_ACTIVE,
        )

        response = self.client.get("/sklad/journal/")

        self.assertEqual(response.status_code, 200)
        rows = response.context["rows"]
        reserve_only_row = next(row for row in rows if row["sku"] == "SKU-2")
        self.assertEqual(reserve_only_row["qty"], 0)
        self.assertEqual(reserve_only_row["processing_reserved_qty"], 0)
        self.assertEqual(reserve_only_row["shipping_reserved_qty"], 5)
        self.assertEqual(reserve_only_row["available_qty"], 0)

    def test_client_inventory_journal_shows_goods_already_in_processing(self):
        storage_location = WarehouseWritePathService.ensure_location(
            warehouse_code="MSK",
            zone_code="OS",
            row_no=1,
            section_no=1,
            tier_no=1,
            cell_no=1,
        )
        processing_location = WarehouseWritePathService.ensure_location(warehouse_code="MSK", zone_code="OBR")
        operation = WarehouseOperation.objects.create(
            agency=self.agency,
            operation_type=WarehouseOperation.TYPE_PROCESSING,
            context_type="processing",
            context_id="2002",
            source_location=processing_location,
            destination_location=processing_location,
            source_zone_code=processing_location.zone_code,
            destination_zone_code=processing_location.zone_code,
            status=WarehouseOperation.STATUS_IN_PROGRESS,
        )
        WarehouseStockSnapshot.objects.create(
            agency=self.agency,
            source_context_type="receiving",
            source_context_id="1002",
            sku_code="SKU-1",
            name="Джинсы",
            size="42",
            goods_type="Оптовый",
            qty=2,
            available_qty=2,
            location=storage_location,
            zone_code=storage_location.zone_code,
            zone_kind=storage_location.zone_kind,
            warehouse_state_code=WarehouseStateCode.STORED.value,
        )
        WarehouseStockSnapshot.objects.create(
            agency=self.agency,
            source_context_type="legacy_stock",
            source_context_id="legacy-2002",
            sku_code="SKU-1",
            name="Джинсы",
            size="42",
            goods_type="Оптовый",
            qty=8,
            available_qty=0,
            processing_reserved_qty=8,
            active_operation=operation,
            active_operation_type=operation.operation_type,
            location=processing_location,
            zone_code=processing_location.zone_code,
            zone_kind=processing_location.zone_kind,
            warehouse_state_code=WarehouseStateCode.PROCESSING_IN_PROGRESS.value,
        )
        WarehouseReserve.objects.create(
            agency=self.agency,
            reserve_type=WarehouseReserve.TYPE_PROCESSING,
            context_type="processing",
            context_id="2002",
            sku_code="SKU-1",
            size="42",
            goods_type="Оптовый",
            qty_reserved=10,
            status=WarehouseReserve.STATUS_ACTIVE,
        )

        response = self.client.get("/sklad/journal/")

        self.assertEqual(response.status_code, 200)
        rows = response.context["rows"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["qty"], 2)
        self.assertEqual(rows[0]["processing_reserved_qty"], 2)
        self.assertEqual(rows[0]["processing_in_progress_qty"], 8)
        self.assertEqual(rows[0]["available_qty"], 0)
        self.assertContains(response, "Товар в обработке")
        self.assertContains(response, ">8<", html=False)

    def test_client_inventory_journal_uses_warehouse_in_progress_qty_without_double_counting_reserve(self):
        location = WarehouseWritePathService.ensure_location(warehouse_code="MSK", zone_code="OBR")
        operation = WarehouseOperation.objects.create(
            agency=self.agency,
            operation_type=WarehouseOperation.TYPE_PROCESSING,
            context_type="processing",
            context_id="2003",
            source_location=location,
            destination_location=location,
            source_zone_code=location.zone_code,
            destination_zone_code=location.zone_code,
            status=WarehouseOperation.STATUS_IN_PROGRESS,
        )
        WarehouseStockSnapshot.objects.create(
            agency=self.agency,
            source_context_type="legacy_stock",
            source_context_id="legacy-2003",
            sku_code="SKU-1",
            name="Джинсы",
            size="42",
            goods_type="Оптовый",
            qty=8,
            available_qty=0,
            processing_reserved_qty=8,
            container_code="PAL-PROC-1",
            location=location,
            zone_code=location.zone_code,
            zone_kind=location.zone_kind,
            warehouse_state_code=WarehouseStateCode.PROCESSING_IN_PROGRESS.value,
            active_operation=operation,
            active_operation_type=WarehouseOperation.TYPE_PROCESSING,
        )
        WarehouseReserve.objects.create(
            agency=self.agency,
            reserve_type=WarehouseReserve.TYPE_PROCESSING,
            context_type="processing",
            context_id="2003",
            sku_code="SKU-1",
            size="42",
            goods_type="Оптовый",
            qty_reserved=8,
            status=WarehouseReserve.STATUS_ACTIVE,
        )

        response = self.client.get("/sklad/journal/")

        self.assertEqual(response.status_code, 200)
        rows = response.context["rows"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["sku"], "SKU-1")
        self.assertEqual(rows[0]["qty"], 0)
        self.assertEqual(rows[0]["processing_reserved_qty"], 0)
        self.assertEqual(rows[0]["processing_in_progress_qty"], 8)
        self.assertEqual(rows[0]["available_qty"], 0)
        self.assertContains(response, "Товар в обработке")

    def test_client_inventory_journal_uses_warehouse_processing_zone_qty_as_processing_reserve(self):
        location = WarehouseWritePathService.ensure_location(warehouse_code="MSK", zone_code="OBR")
        WarehouseStockSnapshot.objects.create(
            agency=self.agency,
            source_context_type="legacy_stock",
            source_context_id="legacy-2004",
            sku_code="SKU-1",
            name="Джинсы",
            size="42",
            goods_type="Оптовый",
            qty=5,
            available_qty=0,
            processing_reserved_qty=5,
            container_code="PAL-PROC-2",
            location=location,
            zone_code=location.zone_code,
            zone_kind=location.zone_kind,
            warehouse_state_code=WarehouseStateCode.IN_PROCESSING_ZONE.value,
        )
        WarehouseReserve.objects.create(
            agency=self.agency,
            reserve_type=WarehouseReserve.TYPE_PROCESSING,
            context_type="processing",
            context_id="2004",
            sku_code="SKU-1",
            size="42",
            goods_type="Оптовый",
            qty_reserved=5,
            status=WarehouseReserve.STATUS_ACTIVE,
        )

        response = self.client.get("/sklad/journal/")

        self.assertEqual(response.status_code, 200)
        rows = response.context["rows"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["sku"], "SKU-1")
        self.assertEqual(rows[0]["qty"], 0)
        self.assertEqual(rows[0]["processing_reserved_qty"], 5)
        self.assertEqual(rows[0]["processing_in_progress_qty"], 0)
        self.assertEqual(rows[0]["available_qty"], 0)


class StaffInventoryJournalAvailableQtyTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="staff_inventory_user",
            password="x",
            is_staff=True,
        )
        self.agency_a = Agency.objects.create(agn_name="Клиент А")
        self.agency_b = Agency.objects.create(agn_name="Клиент Б")
        self.client.force_login(self.user)

    def test_staff_inventory_journal_uses_agency_specific_available_qty(self):
        create_warehouse_snapshot_row(
            agency=self.agency_a,
            order_id="A-1",
            sku="SKU-X",
            name="Товар X",
            size="42",
            goods_type="Оптовый",
            qty=4,
            box_code="BOX-A1",
            pallet_code="PAL-A1",
            zone="OS",
            row=1,
            section=1,
            tier=1,
            cell=1,
        )
        create_warehouse_snapshot_row(
            agency=self.agency_a,
            order_id="A-2",
            sku="SKU-X",
            name="Товар X",
            size="42",
            goods_type="Оптовый",
            qty=6,
            box_code="BOX-A2",
            pallet_code="PAL-A2",
            zone="OS",
            row=1,
            section=1,
            tier=1,
            cell=2,
        )
        create_warehouse_snapshot_row(
            agency=self.agency_b,
            order_id="B-1",
            sku="SKU-X",
            name="Товар X",
            size="42",
            goods_type="Оптовый",
            qty=5,
            box_code="BOX-B1",
            pallet_code="PAL-B1",
            zone="OS",
            row=2,
            section=1,
            tier=1,
            cell=1,
        )
        WarehouseReserve.objects.create(
            agency=self.agency_a,
            reserve_type=WarehouseReserve.TYPE_PROCESSING,
            context_type="processing",
            context_id="P-A",
            sku_code="SKU-X",
            size="42",
            goods_type="Оптовый",
            qty_reserved=3,
            status=WarehouseReserve.STATUS_ACTIVE,
        )
        WarehouseReserve.objects.create(
            agency=self.agency_b,
            reserve_type=WarehouseReserve.TYPE_SHIPPING,
            context_type="shipping",
            context_id="SO-009999",
            sku_code="SKU-X",
            size="42",
            goods_type="Оптовый",
            qty_reserved=2,
            status=WarehouseReserve.STATUS_ACTIVE,
        )

        response = self.client.get("/sklad/journal/")

        self.assertEqual(response.status_code, 200)
        rows = response.context["rows"]
        agency_a_rows = [row for row in rows if row["agency_id"] == self.agency_a.id]
        agency_b_rows = [row for row in rows if row["agency_id"] == self.agency_b.id]
        self.assertEqual(sum(row["available_qty"] for row in agency_a_rows), 7)
        self.assertEqual(sum(row["available_qty"] for row in agency_b_rows), 3)


class InventoryJournalUiServiceTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        user_model = get_user_model()
        self.client_user = user_model.objects.create_user(
            username="inventory_service_client",
            password="x",
        )
        self.staff_user = user_model.objects.create_user(
            username="inventory_service_staff",
            password="x",
            is_staff=True,
        )
        self.client_agency = Agency.objects.create(
            agn_name="Клиент service journal",
            portal_user=self.client_user,
        )
        self.other_agency = Agency.objects.create(agn_name="Другой клиент service journal")

    def test_build_inventory_journal_page_for_client_uses_client_template_and_filters_by_portal_user(self):
        create_warehouse_snapshot_row(
            agency=self.client_agency,
            order_id="R-SVC-1",
            sku="SKU-SVC",
            name="Товар клиента",
            size="42",
            goods_type="Оптовый",
            qty=4,
        )
        create_warehouse_snapshot_row(
            agency=self.other_agency,
            order_id="R-SVC-2",
            sku="SKU-OTHER",
            name="Чужой товар",
            size="43",
            goods_type="Оптовый",
            qty=9,
            row=2,
        )
        request = self.factory.get("/sklad/journal/")
        request.user = self.client_user

        page = build_inventory_journal_page(request=request)

        self.assertEqual(page["template_name"], "client_cabinet/inventory_journal.html")
        self.assertEqual(page["context"]["client_agency"], self.client_agency)
        rows = page["context"]["rows"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["sku"], "SKU-SVC")

    def test_build_inventory_journal_page_for_staff_respects_agency_filter(self):
        create_warehouse_snapshot_row(
            agency=self.client_agency,
            order_id="R-SVC-3",
            sku="SKU-A",
            name="Товар А",
            size="42",
            goods_type="Оптовый",
            qty=5,
        )
        create_warehouse_snapshot_row(
            agency=self.other_agency,
            order_id="R-SVC-4",
            sku="SKU-B",
            name="Товар Б",
            size="44",
            goods_type="Оптовый",
            qty=7,
            row=2,
        )
        request = self.factory.get("/sklad/journal/", {"client": str(self.other_agency.id)})
        request.user = self.staff_user

        page = build_inventory_journal_page(request=request)

        self.assertEqual(page["template_name"], "sklad/inventory_journal.html")
        self.assertEqual(page["context"]["client_agency"], self.other_agency)
        rows = page["context"]["rows"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["agency_id"], self.other_agency.id)

    def test_build_inventory_journal_page_for_manager_roles_returns_to_manager_cabinet(self):
        manager_user = get_user_model().objects.create_user(
            username="inventory_head_manager",
            password="x",
        )
        Employee.objects.create(
            user=manager_user,
            full_name="Старший менеджер остатков",
            role="head_manager",
        )
        create_warehouse_snapshot_row(
            agency=self.other_agency,
            order_id="R-SVC-MANAGER",
            sku="SKU-MANAGER",
            name="Товар менеджера",
            size="42",
            goods_type="Оптовый",
            qty=5,
        )
        request = self.factory.get("/sklad/journal/", {"client": str(self.other_agency.id)})
        request.user = manager_user

        page = build_inventory_journal_page(request=request)

        self.assertEqual(page["template_name"], "teammanager/inventory_journal.html")
        self.assertEqual(page["context"]["inventory_home_url"], "/team-manager/inventory/")
        self.assertEqual(page["context"]["inventory_cabinet_url"], "/team-manager/")

    def test_build_inventory_journal_page_uses_common_order_and_location_display_for_staff(self):
        create_warehouse_snapshot_row(
            agency=self.other_agency,
            order_id="7",
            sku="SKU-B",
            name="Товар Б",
            size="44",
            goods_type="Оптовый",
            qty=7,
            box_code="BOX-TD-1",
            pallet_code="ТДТ-0405-289465",
            zone="OS",
            row=2,
            section=3,
            tier=1,
            cell=4,
        )
        request = self.factory.get("/sklad/journal/", {"client": str(self.other_agency.id), "q": "7_PR"})
        request.user = self.staff_user

        page = build_inventory_journal_page(request=request)

        rows = page["context"]["rows"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["order_display"], "7_PR")
        self.assertEqual(rows[0]["location_short"], "OS-2/3-1-4")
        self.assertEqual(page["context"]["journal_summary"]["total_qty"], 7)
        self.assertEqual(page["context"]["journal_focus"]["order_label"], "7_PR")

    def test_staff_inventory_journal_marks_fbs_as_a_separate_non_fbo_contour(self):
        from fbs.models import FbsBox, FbsPallet, FbsStockBalance, FbsStorageCell

        location = WarehouseLocation.objects.create(
            warehouse_code="MSK",
            zone_code="OS",
            zone_kind=WarehouseLocation.ZONE_KIND_STORAGE,
            row_no=9,
            section_no=2,
            tier_no=2,
            cell_no=3,
            location_code="OS-9-2-2-3",
            is_storage=True,
        )
        pallet_container = WarehouseContainer.objects.create(
            agency=self.client_agency,
            container_type=WarehouseContainer.TYPE_PALLET,
            container_code="FBS-CONTOUR-PALLET",
            current_location=location,
        )
        box_container = WarehouseContainer.objects.create(
            agency=self.client_agency,
            container_type=WarehouseContainer.TYPE_BOX,
            container_code="FBS-CONTOUR-BOX",
            parent_container=pallet_container,
            current_location=location,
        )
        cell = FbsStorageCell.objects.create(
            cell_code="FBS@OS-9-2-2-3",
            location=location,
        )
        pallet = FbsPallet.objects.create(
            agency=self.client_agency,
            pallet_code="FBS-CONTOUR-PALLET",
            cell=cell,
            warehouse_container=pallet_container,
            status=FbsPallet.STATUS_ACTIVE,
        )
        box = FbsBox.objects.create(
            agency=self.client_agency,
            pallet=pallet,
            box_code="FBS-CONTOUR-BOX",
            source_container=box_container,
            status=FbsBox.STATUS_ACTIVE,
        )
        FbsStockBalance.objects.create(
            agency=self.client_agency,
            box=box,
            identity_key="fbs-contour-stock",
            sku_code="SKU-FBS-CONTOUR",
            name="Товар только в FBS",
            qty=29,
            available_qty=29,
        )
        request = self.factory.get(
            "/sklad/journal/",
            {"client": str(self.client_agency.id), "q": box.box_code},
        )
        request.user = self.staff_user

        page = build_inventory_journal_page(request=request)

        rows = page["context"]["rows"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["storage_contour_code"], "FBS")
        self.assertEqual(rows[0]["storage_contour_label"], "FBS (не FBO)")
        self.assertEqual(rows[0]["stock_main_qty"], 0)
        self.assertEqual(rows[0]["fbs_qty"], 29)
        self.assertEqual(page["context"]["journal_summary"]["total_stock_main_qty"], 0)
        self.assertEqual(page["context"]["journal_summary"]["total_fbs_qty"], 29)
        self.assertEqual(
            page["context"]["journal_focus"]["storage_contour_label"],
            "FBS (не FBO)",
        )
        html = render_to_string(
            page["template_name"],
            page["context"],
            request=request,
        )
        self.assertIn("Контур", html)
        self.assertIn("FBS (не FBO)", html)

    def test_staff_inventory_journal_uses_fbs_box_physical_location(self):
        from fbs.models import FbsBox, FbsPallet, FbsStockBalance, FbsStorageCell

        physical_location = WarehouseLocation.objects.create(
            warehouse_code="MSK",
            zone_code="OS",
            zone_kind=WarehouseLocation.ZONE_KIND_STORAGE,
            location_code="BS-1/1-1",
            display_name="Стеллаж белый",
            is_storage=True,
        )
        plan_location = WarehouseLocation.objects.create(
            warehouse_code="MSK",
            zone_code="FBS",
            zone_kind=WarehouseLocation.ZONE_KIND_VIRTUAL,
            location_code="FBS-PLAN-JOURNAL-TEST",
            display_name="План FBS · место определяется сканом ричтрака",
            is_storage=False,
        )
        pallet_container = WarehouseContainer.objects.create(
            agency=self.client_agency,
            container_type=WarehouseContainer.TYPE_PALLET,
            container_code="FBS-PHYSICAL-JOURNAL-PALLET",
            current_location=plan_location,
        )
        box_container = WarehouseContainer.objects.create(
            agency=self.client_agency,
            container_type=WarehouseContainer.TYPE_BOX,
            container_code="FBS-PHYSICAL-JOURNAL-BOX",
            parent_container=pallet_container,
            current_location=physical_location,
        )
        plan_cell = FbsStorageCell.objects.create(
            cell_code="FBS@PLAN-JOURNAL-TEST",
            location=plan_location,
        )
        pallet = FbsPallet.objects.create(
            agency=self.client_agency,
            pallet_code="FBS-PHYSICAL-JOURNAL-PALLET",
            cell=plan_cell,
            warehouse_container=pallet_container,
            status=FbsPallet.STATUS_ACTIVE,
        )
        box = FbsBox.objects.create(
            agency=self.client_agency,
            pallet=pallet,
            box_code="FBS-PHYSICAL-JOURNAL-BOX",
            source_container=box_container,
            status=FbsBox.STATUS_ACTIVE,
        )
        FbsStockBalance.objects.create(
            agency=self.client_agency,
            box=box,
            identity_key="fbs-physical-journal-stock",
            sku_code="SKU-FBS-PHYSICAL-JOURNAL",
            name="Товар на физическом месте FBS",
            qty=12,
            available_qty=12,
        )
        physical_request = self.factory.get(
            "/sklad/journal/",
            {"client": str(self.client_agency.id), "q": "BS-1/1-1"},
        )
        physical_request.user = self.staff_user

        page = build_inventory_journal_page(request=physical_request)

        rows = page["context"]["rows"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["box_code"], box.box_code)
        self.assertEqual(rows[0]["location_id"], physical_location.id)
        self.assertEqual(rows[0]["location_short"], "BS-1/1-1")
        self.assertIn("BS-1/1-1", rows[0]["location"])
        self.assertNotIn("FBS@PLAN", rows[0]["location"])

        plan_request = self.factory.get(
            "/sklad/journal/",
            {"client": str(self.client_agency.id), "q": "FBS@PLAN-JOURNAL-TEST"},
        )
        plan_request.user = self.staff_user
        plan_page = build_inventory_journal_page(request=plan_request)
        self.assertEqual(plan_page["context"]["rows"], [])


class StockSnapshotBarcodeRecoveryTests(TestCase):
    def setUp(self):
        self.agency = Agency.objects.create(agn_name="Snapshot Agency")
        self.sku = SKU.objects.create(
            agency=self.agency,
            sku_code="SKU-BC-1",
            name="Товар для штрихкода",
            size="42",
        )
        SKUBarcode.objects.create(
            sku=self.sku,
            value="2000999000001",
            size="42",
            is_primary=True,
        )

    def test_replace_order_placement_restores_missing_barcode_from_nomenclature(self):
        OperationalStockService.replace_order_placement(
            self.agency,
            "receiving",
            "R-100",
            {
                "act": "placement",
                "act_state": "closed",
                "act_items": [
                    {
                        "sku": "SKU-BC-1",
                        "name": "Товар для штрихкода",
                        "size": "42",
                        "barcode": "",
                        "qty": 10,
                    }
                ],
            },
        )

        snapshot = WarehouseStockSnapshot.objects.get(agency=self.agency, sku_code="SKU-BC-1", size="42")
        self.assertEqual(snapshot.barcode, "2000999000001")

    def test_replace_order_placement_keeps_explicit_payload_barcode(self):
        OperationalStockService.replace_order_placement(
            self.agency,
            "receiving",
            "R-101",
            {
                "act": "placement",
                "act_state": "closed",
                "act_items": [
                    {
                        "sku": "SKU-BC-1",
                        "name": "Товар для штрихкода",
                        "size": "42",
                        "barcode": "5550001112223",
                        "qty": 5,
                    }
                ],
            },
        )

        snapshot = WarehouseStockSnapshot.objects.get(agency=self.agency, sku_code="SKU-BC-1", size="42")
        self.assertEqual(snapshot.barcode, "5550001112223")


class StockSnapshotMarkedUnitsTests(TestCase):
    def setUp(self):
        self.agency = Agency.objects.create(agn_name="Marked Snapshot Agency")
        self.sku = SKU.objects.create(
            agency=self.agency,
            sku_code="SKU-CZ-2",
            name="Маркируемый товар",
            size="43",
            honest_sign=True,
        )

    def test_replace_order_placement_creates_separate_rows_per_marking_code(self):
        OperationalStockService.replace_order_placement(
            self.agency,
            "receiving",
            "R-CZ-200",
            {
                "act": "placement",
                "act_state": "closed",
                "act_boxes": [
                    {"code": "BOX-1", "items": []},
                ],
                "act_pallets": [
                    {
                        "code": "PAL-1",
                        "boxes": ["BOX-1"],
                        "items": [],
                        "location": {"zone": "PR"},
                    }
                ],
                "act_units": [
                    {
                        "sku_code": "SKU-CZ-2",
                        "name": "Маркируемый товар",
                        "size": "43",
                        "barcode": "2200000000011",
                        "marking_code": "CZ-200-1",
                        "box_code": "BOX-1",
                        "pallet_code": "PAL-1",
                    },
                    {
                        "sku_code": "SKU-CZ-2",
                        "name": "Маркируемый товар",
                        "size": "43",
                        "barcode": "2200000000011",
                        "marking_code": "CZ-200-2",
                        "box_code": "BOX-1",
                        "pallet_code": "PAL-1",
                    },
                ],
            },
        )

        rows = list(
            WarehouseStockSnapshot.objects.filter(
                agency=self.agency,
                sku_code="SKU-CZ-2",
                size="43",
            ).order_by("marking_code")
        )
        self.assertEqual(len(rows), 2)
        self.assertEqual([row.qty for row in rows], [1, 1])
        self.assertEqual([row.marking_code for row in rows], ["CZ-200-1", "CZ-200-2"])


class OperationalStockReadApiTests(TestCase):
    def setUp(self):
        self.agency = Agency.objects.create(agn_name="Operational Stock Read API")
        create_warehouse_snapshot_row(
            agency=self.agency,
            order_type="receiving",
            order_id="R-API-1",
            sku="SKU-READ-1",
            name="Товар 1",
            size="42",
            barcode="2000000011111",
            goods_type="gv",
            qty=5,
            box_code="BOX-READ-1",
            pallet_code="PAL-READ-1",
            zone="OS",
            row=2,
            section=1,
            tier=1,
            cell=3,
        )
        create_warehouse_snapshot_row(
            agency=self.agency,
            order_type="receiving",
            order_id="R-API-1",
            sku="SKU-READ-1",
            name="Товар 1",
            size="42",
            barcode="2000000011111",
            marking_code="CZ-READ-1",
            goods_type="gv",
            qty=1,
            box_code="BOX-READ-1",
            pallet_code="PAL-READ-1",
            zone="OS",
            row=2,
            section=1,
            tier=1,
            cell=3,
        )
        create_warehouse_snapshot_row(
            agency=self.agency,
            order_type="receiving",
            order_id="R-API-1",
            sku="SKU-READ-2",
            name="Товар 2",
            size="43",
            barcode="2000000012222",
            goods_type="op",
            qty=7,
            box_code="BOX-READ-2",
            pallet_code="PAL-READ-1",
            zone="OS",
            row=2,
            section=1,
            tier=1,
            cell=3,
        )

    def test_get_pallet_boxes_returns_grouped_box_structure(self):
        boxes = OperationalStockService.get_pallet_boxes("PAL-READ-1", agency_id=self.agency.id)

        self.assertEqual([box["code"] for box in boxes], ["BOX-READ-1", "BOX-READ-2"])
        self.assertEqual(boxes[0]["qty"], 6)
        self.assertEqual(boxes[0]["barcode_qty"], {"2000000011111": 6})
        self.assertEqual(boxes[0]["marked_units"][0]["marking_code"], "CZ-READ-1")

    def test_get_box_items_returns_atomic_rows_from_operational_stock(self):
        items = OperationalStockService.get_box_items(
            "BOX-READ-1",
            agency_id=self.agency.id,
            pallet_code="PAL-READ-1",
        )

        self.assertEqual(len(items), 2)
        self.assertEqual(sum(int(item.get("qty") or 0) for item in items), 6)
        self.assertEqual(
            [item.get("marking_code") for item in items if item.get("marking_code")],
            ["CZ-READ-1"],
        )

    def test_get_marked_units_returns_unit_to_box_pallet_location_mapping(self):
        units = OperationalStockService.get_marked_units(
            agency_id=self.agency.id,
            pallet_code="PAL-READ-1",
        )

        self.assertEqual(len(units), 1)
        self.assertEqual(units[0]["marking_code"], "CZ-READ-1")
        self.assertEqual(units[0]["box_code"], "BOX-READ-1")
        self.assertEqual(units[0]["pallet_code"], "PAL-READ-1")
        self.assertEqual((units[0]["location"] or {}).get("zone"), "OS")


class WarehouseCoreModelsTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="warehouse_core", password="pwd")
        self.agency = Agency.objects.create(agn_name="Тестовый клиент ядра")
        self.sku = SKU.objects.create(agency=self.agency, sku_code="CORE-001", name="Ядро SKU")

    def _create_direct_processing_move(self, *, order_id: str, box_rows: list[tuple[str, str, int]]):
        receiving_order_id = f"RCV-{order_id}"
        placement = WarehouseWritePathService.create_receiving_placement(
            agency=self.agency,
            order_id=receiving_order_id,
            performed_by=self.user,
            items=[
                {
                    "sku_code": self.sku.sku_code,
                    "name": self.sku.name,
                    "barcode": barcode,
                    "goods_type": "gv",
                    "qty": qty,
                    "pallet_code": f"PAL-{order_id}",
                    "box_code": box_code,
                }
                for box_code, barcode, qty in box_rows
            ],
        )
        putaway = WarehouseWritePathService.request_putaway_for_receiving(
            agency=self.agency,
            order_id=receiving_order_id,
            destination_zone_code="OS",
            destination_row_no=8,
            destination_section_no=1,
            destination_tier_no=1,
            destination_cell_no=1,
            requested_by=self.user,
        )
        WarehouseWritePathService.complete_putaway_operation(
            operation=putaway,
            performed_by=self.user,
        )
        WarehouseWritePathService.reserve_for_processing(
            agency=self.agency,
            order_id=order_id,
            items=[
                {
                    "sku_code": self.sku.sku_code,
                    "barcode": barcode,
                    "goods_type": "gv",
                    "qty": qty,
                }
                for _box_code, barcode, qty in box_rows
            ],
            created_by=self.user,
        )
        operation = WarehouseWritePathService.request_move_to_processing(
            agency=self.agency,
            order_id=order_id,
            container_codes=[box_code for box_code, _barcode, _qty in box_rows],
            requested_by=self.user,
            source_document_type="processing",
            source_document_id=order_id,
        )
        return placement, operation

    def test_can_create_first_wave_warehouse_core_entities(self):
        location = WarehouseLocation.objects.create(
            warehouse_code="MSK",
            zone_code="OS",
            zone_kind=WarehouseLocation.ZONE_KIND_STORAGE,
            row_no=1,
            section_no=1,
            tier_no=1,
            cell_no=1,
            location_code="OS-1-1-1-1",
            display_name="OS · Ряд 1 · Секция 1 · Ярус 1 · Ячейка 1",
            is_active=True,
            is_pickable=True,
            is_storage=True,
        )
        container = WarehouseContainer.objects.create(
            agency=self.agency,
            container_type=WarehouseContainer.TYPE_PALLET,
            container_code="PL-CORE-1",
            current_location=location,
            created_by=self.user,
        )
        reserve = WarehouseReserve.objects.create(
            agency=self.agency,
            reserve_type=WarehouseReserve.TYPE_SHIPPING,
            context_type="shipping",
            context_id="SO-CORE-1",
            sku_ref=self.sku,
            sku_code=self.sku.sku_code,
            qty_reserved=10,
            status=WarehouseReserve.STATUS_ACTIVE,
            created_by=self.user,
        )
        operation = WarehouseOperation.objects.create(
            agency=self.agency,
            operation_type=WarehouseOperation.TYPE_MOVE_TO_OTG,
            context_type="shipping",
            context_id="SO-CORE-1",
            reserve=reserve,
            source_location=location,
            destination_zone_code="OTG",
            status=WarehouseOperation.STATUS_CREATED,
            requested_by=self.user,
            requested_by_role="storekeeper",
            assigned_executor_role="reachtruck",
            planned_qty=10,
        )
        task = WarehouseOperationTask.objects.create(
            operation=operation,
            task_type=WarehouseOperationTask.TYPE_PALLET_MOVE,
            container=container,
            from_location=location,
            from_zone_code="OS",
            to_zone_code="OTG",
            qty_planned=10,
            status=WarehouseOperationTask.STATUS_CREATED,
            assigned_to=self.user,
            assigned_to_name="Исполнитель склада",
            executor_role="reachtruck",
        )
        event = WarehouseEvent.objects.create(
            agency=self.agency,
            event_type="shipping_reserved",
            stock_context_type="shipping",
            stock_context_id="SO-CORE-1",
            container=container,
            reserve=reserve,
            operation=operation,
            operation_task=task,
            from_location=location,
            from_zone_code="OS",
            qty=10,
            performed_by=self.user,
            performed_by_role="storekeeper",
            occurred_at=timezone.now(),
        )
        snapshot = WarehouseStockSnapshot.objects.create(
            agency=self.agency,
            source_context_type="shipping",
            source_context_id="SO-CORE-1",
            sku_ref=self.sku,
            sku_code=self.sku.sku_code,
            name=self.sku.name,
            qty=10,
            available_qty=0,
            shipping_reserved_qty=10,
            container=container,
            container_code=container.container_code,
            location=location,
            zone_code=location.zone_code,
            zone_kind=location.zone_kind,
            warehouse_state_code="reserved_for_shipping",
            active_operation=operation,
            active_operation_type=operation.operation_type,
            last_event=event,
        )

        self.assertEqual(snapshot.container_id, container.id)
        self.assertEqual(snapshot.location_id, location.id)
        self.assertEqual(snapshot.active_operation_id, operation.id)
        self.assertEqual(snapshot.last_event_id, event.id)
        self.assertEqual(operation.reserve_id, reserve.id)
        self.assertEqual(task.operation_id, operation.id)

    def test_receiving_sync_keeps_product_box_pallet_and_goods_type_truth(self):
        placement = WarehouseWritePathService.sync_receiving_placement(
            agency=self.agency,
            order_id="RCV-TRUTH-1",
            performed_by=self.user,
            placement_payload={
                "goods_type": "op",
                "act_boxes": [
                    {
                        "code": "BOX-TRUTH-1",
                        "items": [
                            {
                                "sku": self.sku.sku_code,
                                "name": self.sku.name,
                                "size": "42",
                                "qty": 5,
                            }
                        ],
                    }
                ],
                "act_pallets": [
                    {
                        "code": "PAL-TRUTH-1",
                        "boxes": ["BOX-TRUTH-1"],
                        "items": [],
                    }
                ],
            },
        )

        self.assertEqual(len(placement.snapshot_ids), 1)
        snapshot = WarehouseStockSnapshot.objects.select_related(
            "container",
            "parent_container",
        ).get(id=placement.snapshot_ids[0])
        self.assertEqual(snapshot.container_code, "BOX-TRUTH-1")
        self.assertEqual(snapshot.container.container_type, WarehouseContainer.TYPE_BOX)
        self.assertEqual(snapshot.parent_container.container_code, "PAL-TRUTH-1")
        self.assertEqual(snapshot.goods_type, "op")

        rows = snapshot_stock_rows(agency=self.agency)
        row = next(row for row in rows if row["order_id"] == "RCV-TRUTH-1")
        self.assertEqual(row["sku"], self.sku.sku_code)
        self.assertEqual(row["box_code"], "BOX-TRUTH-1")
        self.assertEqual(row["pallet_code"], "PAL-TRUTH-1")
        self.assertEqual(row["goods_type"], "op")
        self.assertEqual(row["zone"], "PR")

    def test_receiving_putaway_by_pallet_moves_child_boxes_and_container_location(self):
        placement = WarehouseWritePathService.create_receiving_placement(
            agency=self.agency,
            order_id="RCV-TRUTH-2",
            performed_by=self.user,
            items=[
                {
                    "sku_code": self.sku.sku_code,
                    "name": self.sku.name,
                    "size": "43",
                    "goods_type": "gv",
                    "qty": 7,
                    "pallet_code": "PAL-TRUTH-2",
                    "box_code": "BOX-TRUTH-2",
                }
            ],
        )
        snapshot = WarehouseStockSnapshot.objects.select_related(
            "container",
            "parent_container",
        ).get(id=placement.snapshot_ids[0])

        operation = WarehouseWritePathService.request_putaway_for_receiving(
            agency=self.agency,
            order_id="RCV-TRUTH-2",
            container_codes=["PAL-TRUTH-2"],
            destination_zone_code="OS",
            destination_row_no=3,
            destination_section_no=2,
            destination_tier_no=1,
            destination_cell_no=4,
            requested_by=self.user,
            requested_by_role="storekeeper",
        )

        task = operation.tasks.select_related("container").get()
        self.assertEqual(task.task_type, WarehouseOperationTask.TYPE_PALLET_MOVE)
        self.assertEqual(task.container.container_code, "PAL-TRUTH-2")
        self.assertEqual(task.qty_planned, 7)
        snapshot.refresh_from_db()
        self.assertEqual(snapshot.active_operation_id, operation.id)

        WarehouseWritePathService.complete_putaway_operation(
            operation=operation,
            performed_by=self.user,
        )

        snapshot.refresh_from_db()
        snapshot.container.refresh_from_db()
        snapshot.parent_container.refresh_from_db()
        completed_event = WarehouseEvent.objects.filter(
            stock_context_type="receiving",
            stock_context_id="RCV-TRUTH-2",
            event_type=WarehouseEventType.PUTAWAY_COMPLETED.value,
        ).latest("id")
        self.assertEqual(snapshot.zone_code, "OS")
        self.assertEqual(snapshot.location.row_no, 3)
        self.assertEqual(snapshot.container.current_location_id, snapshot.location_id)
        self.assertEqual(snapshot.parent_container.current_location_id, snapshot.location_id)
        self.assertEqual(completed_event.container.container_code, "PAL-TRUTH-2")

    def test_receiving_write_path_moves_snapshot_from_placement_to_stored(self):
        placement = WarehouseWritePathService.create_receiving_placement(
            agency=self.agency,
            order_id="RCV-CORE-1",
            performed_by=self.user,
            items=[
                {
                    "sku_code": self.sku.sku_code,
                    "name": self.sku.name,
                    "size": "42",
                    "barcode": "2000000099999",
                    "goods_type": "gv",
                    "qty": 12,
                    "pallet_code": "PAL-RCV-1",
                }
            ],
        )

        self.assertEqual(len(placement.snapshot_ids), 1)
        snapshot = WarehouseStockSnapshot.objects.get(id=placement.snapshot_ids[0])
        self.assertEqual(snapshot.warehouse_state_code, "placed_in_receiving")
        self.assertEqual(snapshot.zone_code, "PR")
        self.assertEqual(snapshot.available_qty, 12)

        operation = WarehouseWritePathService.request_putaway_for_receiving(
            agency=self.agency,
            order_id="RCV-CORE-1",
            destination_zone_code="OS",
            destination_row_no=3,
            destination_section_no=2,
            destination_tier_no=1,
            destination_cell_no=4,
            requested_by=self.user,
            requested_by_role="storekeeper",
        )

        snapshot.refresh_from_db()
        self.assertEqual(snapshot.active_operation_id, operation.id)
        self.assertEqual(snapshot.active_operation_type, WarehouseOperation.TYPE_PUTAWAY)
        self.assertEqual(operation.status, WarehouseOperation.STATUS_PLANNED)
        self.assertEqual(operation.tasks.count(), 1)

        WarehouseWritePathService.complete_putaway_operation(
            operation=operation,
            performed_by=self.user,
        )

        snapshot.refresh_from_db()
        operation.refresh_from_db()
        task = operation.tasks.get()

        self.assertEqual(snapshot.warehouse_state_code, "stored")
        self.assertEqual(snapshot.zone_code, "OS")
        self.assertEqual(snapshot.active_operation_id, None)
        self.assertEqual(snapshot.location.row_no, 3)
        self.assertEqual(snapshot.location.section_no, 2)
        self.assertEqual(snapshot.location.tier_no, 1)
        self.assertEqual(snapshot.location.cell_no, 4)
        self.assertEqual(operation.status, WarehouseOperation.STATUS_DONE)
        self.assertEqual(operation.done_qty, 12)
        self.assertEqual(task.status, WarehouseOperationTask.STATUS_DONE)
        self.assertEqual(task.qty_done, 12)
        self.assertTrue(
            WarehouseEvent.objects.filter(
                stock_context_type="receiving",
                stock_context_id="RCV-CORE-1",
                event_type="putaway_completed",
            ).exists()
        )

    def test_processing_write_path_consumes_only_scanned_obr_box(self):
        placement = WarehouseWritePathService.create_receiving_placement(
            agency=self.agency,
            order_id="RCV-PRC-1",
            performed_by=self.user,
            items=[
                {
                    "sku_code": self.sku.sku_code,
                    "name": self.sku.name,
                    "size": "44",
                    "barcode": "2000000088888",
                    "goods_type": "gv",
                    "qty": 8,
                    "pallet_code": "PAL-PRC-1",
                    "box_code": "BOX-PRC-1",
                }
            ],
        )
        putaway = WarehouseWritePathService.request_putaway_for_receiving(
            agency=self.agency,
            order_id="RCV-PRC-1",
            destination_zone_code="OS",
            destination_row_no=5,
            destination_section_no=1,
            destination_tier_no=2,
            destination_cell_no=1,
            requested_by=self.user,
        )
        WarehouseWritePathService.complete_putaway_operation(operation=putaway, performed_by=self.user)

        snapshot = WarehouseStockSnapshot.objects.get(id=placement.snapshot_ids[0])
        snapshot.refresh_from_db()
        self.assertEqual(snapshot.warehouse_state_code, "stored")
        self.assertEqual(snapshot.available_qty, 8)

        moved_qty = WarehouseWritePathService.mark_processing_boxes_arrived_to_obr(
            agency=self.agency,
            order_id="PROC-1",
            box_codes=["BOX-PRC-1"],
            performed_by=self.user,
        )
        self.assertEqual(moved_qty, 8)
        snapshot.refresh_from_db()
        self.assertEqual(snapshot.warehouse_state_code, "in_processing_zone")
        self.assertEqual(snapshot.zone_code, "OBR")
        self.assertEqual(snapshot.source_context_type, "processing")
        self.assertEqual(snapshot.source_context_id, "PROC-1")
        self.assertEqual(snapshot.processing_reserved_qty, 0)
        self.assertEqual(snapshot.available_qty, 0)
        self.assertFalse(
            WarehouseReserve.objects.filter(
                agency=self.agency,
                reserve_type=WarehouseReserve.TYPE_PROCESSING,
                context_id="PROC-1",
            ).exists()
        )

        processing_op = WarehouseWritePathService.start_processing(
            agency=self.agency,
            order_id="PROC-1",
            started_by=self.user,
        )
        snapshot.refresh_from_db()
        self.assertEqual(snapshot.warehouse_state_code, "processing_in_progress")
        self.assertEqual(snapshot.active_operation_id, processing_op.id)
        self.assertEqual(snapshot.active_operation_type, WarehouseOperation.TYPE_PROCESSING)

        service, _ = BillingService.objects.get_or_create(
            code="processing_core_complete_test",
            defaults={"name": "Завершение обработки", "unit": "шт", "is_active": True},
        )
        WarehouseServiceFact.objects.create(
            client=self.agency,
            order_type=WarehouseServiceFact.ORDER_PROCESSING,
            order_id="PROC-1",
            service=service,
            service_name_snapshot=service.name,
            quantity=8,
            unit="шт",
            reported_by=self.user,
        )
        WarehouseWritePathService.complete_processing(operation=processing_op, performed_by=self.user)
        snapshot.refresh_from_db()
        processing_op.refresh_from_db()

        self.assertEqual(snapshot.warehouse_state_code, "processing_consumed")
        self.assertEqual(snapshot.processing_reserved_qty, 0)
        self.assertEqual(snapshot.available_qty, 0)
        self.assertEqual(snapshot.active_operation_id, None)
        self.assertEqual(snapshot.zone_code, "OBR")
        self.assertEqual(processing_op.status, WarehouseOperation.STATUS_DONE)
        self.assertEqual(processing_op.done_qty, 8)
        self.assertTrue(
            WarehouseEvent.objects.filter(
                stock_context_type="processing",
                stock_context_id="PROC-1",
                event_type="processing_consumed",
            ).exists()
        )

    def test_direct_processing_arrival_completes_operation_and_is_idempotent(self):
        _placement, operation = self._create_direct_processing_move(
            order_id="PROC-DIRECT-1",
            box_rows=[("BOX-DIRECT-1", "BAR-DIRECT-1", 8)],
        )

        first_qty = WarehouseWritePathService.mark_processing_boxes_arrived_to_obr(
            agency=self.agency,
            order_id="PROC-DIRECT-1",
            box_codes=["BOX-DIRECT-1"],
            performed_by=self.user,
            source_document_id="MOVE-DIRECT-1",
            operation_id=operation.id,
        )
        second_qty = WarehouseWritePathService.mark_processing_boxes_arrived_to_obr(
            agency=self.agency,
            order_id="PROC-DIRECT-1",
            box_codes=["BOX-DIRECT-1"],
            performed_by=self.user,
            source_document_id="MOVE-DIRECT-1",
            operation_id=operation.id,
        )

        operation.refresh_from_db()
        operation_task = operation.tasks.get()
        snapshot = WarehouseStockSnapshot.objects.get(
            agency=self.agency,
            container__container_code="BOX-DIRECT-1",
            is_archived=False,
        )
        arrival_events = WarehouseEvent.objects.filter(
            stock_context_type="processing",
            stock_context_id="PROC-DIRECT-1",
            event_type=WarehouseEventType.PROCESSING_ZONE_ARRIVED.value,
            source_document_type="reachtruck_processing_task",
            source_document_id="MOVE-DIRECT-1",
        )

        self.assertEqual(first_qty, 8)
        self.assertEqual(second_qty, 8)
        self.assertEqual(arrival_events.count(), 1)
        self.assertEqual(arrival_events.get().operation_id, operation.id)
        self.assertEqual(snapshot.zone_code, "OBR")
        self.assertEqual(snapshot.active_operation_id, None)
        self.assertEqual(operation.status, WarehouseOperation.STATUS_DONE)
        self.assertEqual(operation.done_qty, 8)
        self.assertIsNotNone(operation.completed_at)
        self.assertEqual(operation_task.status, WarehouseOperationTask.STATUS_DONE)
        self.assertEqual(operation_task.qty_done, 8)

    def test_partial_direct_processing_arrival_is_idempotent_and_completes_operation(self):
        create_warehouse_snapshot_row(
            agency=self.agency,
            order_type="receiving",
            order_id="RCV-PROC-DIRECT-PARTIAL",
            sku=self.sku.sku_code,
            name=self.sku.name,
            size="42",
            barcode="BAR-DIRECT-PARTIAL",
            goods_type="gv",
            qty=100,
            available_qty=100,
            box_code="BOX-DIRECT-PARTIAL",
            pallet_code="PAL-DIRECT-PARTIAL",
            zone="OS",
            row=8,
            section=1,
            tier=1,
            cell=2,
        )
        WarehouseWritePathService.reserve_for_processing(
            agency=self.agency,
            order_id="PROC-DIRECT-PARTIAL",
            items=[
                {
                    "sku_code": self.sku.sku_code,
                    "barcode": "BAR-DIRECT-PARTIAL",
                    "goods_type": "gv",
                    "qty": 20,
                }
            ],
            created_by=self.user,
        )
        operation = WarehouseWritePathService.request_move_to_processing(
            agency=self.agency,
            order_id="PROC-DIRECT-PARTIAL",
            container_codes=["BOX-DIRECT-PARTIAL"],
            destination_row_no=1,
            destination_section_no=1,
            destination_tier_no=1,
            destination_cell_no=1,
            requested_by=self.user,
        )
        picked_rows = [
            {
                "box_code": "BOX-DIRECT-PARTIAL",
                "qty": 20,
                "picked_qty": 20,
                "barcode_qty": {"BAR-DIRECT-PARTIAL": 20},
                "sku_code": self.sku.sku_code,
            }
        ]

        first_snapshot_ids = WarehouseWritePathService.complete_partial_processing_pick_to_obr(
            agency=self.agency,
            order_id="PROC-DIRECT-PARTIAL",
            source_pallet_code="PAL-DIRECT-PARTIAL",
            picked_rows=picked_rows,
            destination_row_no=1,
            destination_section_no=1,
            destination_tier_no=1,
            destination_cell_no=1,
            performed_by=self.user,
            source_document_id="MOVE-DIRECT-PARTIAL",
            operation_id=operation.id,
        )
        second_snapshot_ids = WarehouseWritePathService.complete_partial_processing_pick_to_obr(
            agency=self.agency,
            order_id="PROC-DIRECT-PARTIAL",
            source_pallet_code="PAL-DIRECT-PARTIAL",
            picked_rows=picked_rows,
            destination_row_no=1,
            destination_section_no=1,
            destination_tier_no=1,
            destination_cell_no=1,
            performed_by=self.user,
            source_document_id="MOVE-DIRECT-PARTIAL",
            operation_id=operation.id,
        )

        operation.refresh_from_db()
        operation_task = operation.tasks.get()
        source_snapshot = WarehouseStockSnapshot.objects.get(
            agency=self.agency,
            container__container_code="BOX-DIRECT-PARTIAL",
            zone_code="OS",
            is_archived=False,
        )
        loose_snapshot = WarehouseStockSnapshot.objects.get(id=first_snapshot_ids[0])
        arrival_events = WarehouseEvent.objects.filter(
            stock_context_type="processing",
            stock_context_id="PROC-DIRECT-PARTIAL",
            event_type=WarehouseEventType.PROCESSING_ZONE_ARRIVED.value,
            source_document_id="MOVE-DIRECT-PARTIAL",
        )
        self.assertEqual(first_snapshot_ids, second_snapshot_ids)
        self.assertEqual(len(first_snapshot_ids), 1)
        self.assertEqual(arrival_events.count(), 1)
        self.assertEqual(arrival_events.get().operation_id, operation.id)
        self.assertIsNone(arrival_events.get().container_id)
        self.assertIsNone(loose_snapshot.container_id)
        self.assertEqual(loose_snapshot.container_code, "")
        self.assertIsNone(loose_snapshot.parent_container_id)
        self.assertEqual(source_snapshot.qty, 80)
        self.assertEqual(source_snapshot.available_qty, 80)
        self.assertEqual(operation.status, WarehouseOperation.STATUS_DONE)
        self.assertEqual(operation.done_qty, 20)
        self.assertEqual(operation_task.status, WarehouseOperationTask.STATUS_DONE)
        self.assertEqual(operation_task.qty_done, 20)

    def test_direct_processing_arrival_completes_multi_task_operation_incrementally(self):
        _placement, operation = self._create_direct_processing_move(
            order_id="PROC-DIRECT-MULTI",
            box_rows=[
                ("BOX-DIRECT-A", "BAR-DIRECT-A", 4),
                ("BOX-DIRECT-B", "BAR-DIRECT-B", 6),
            ],
        )

        WarehouseWritePathService.mark_processing_boxes_arrived_to_obr(
            agency=self.agency,
            order_id="PROC-DIRECT-MULTI",
            box_codes=["BOX-DIRECT-A"],
            performed_by=self.user,
            source_document_id="MOVE-DIRECT-A",
            operation_id=operation.id,
        )
        operation.refresh_from_db()
        self.assertEqual(operation.status, WarehouseOperation.STATUS_PARTIAL)
        self.assertEqual(operation.done_qty, 4)
        self.assertIsNone(operation.completed_at)
        self.assertEqual(
            list(operation.tasks.order_by("qty_planned").values_list("status", "qty_done")),
            [
                (WarehouseOperationTask.STATUS_DONE, 4),
                (WarehouseOperationTask.STATUS_CREATED, 0),
            ],
        )

        WarehouseWritePathService.mark_processing_boxes_arrived_to_obr(
            agency=self.agency,
            order_id="PROC-DIRECT-MULTI",
            box_codes=["BOX-DIRECT-B"],
            performed_by=self.user,
            source_document_id="MOVE-DIRECT-B",
            operation_id=operation.id,
        )
        operation.refresh_from_db()
        self.assertEqual(operation.status, WarehouseOperation.STATUS_DONE)
        self.assertEqual(operation.done_qty, 10)
        self.assertIsNotNone(operation.completed_at)
        self.assertFalse(operation.tasks.exclude(status=WarehouseOperationTask.STATUS_DONE).exists())

    def test_direct_processing_arrival_rolls_back_when_operation_task_does_not_match(self):
        _placement, operation = self._create_direct_processing_move(
            order_id="PROC-DIRECT-ROLLBACK",
            box_rows=[("BOX-DIRECT-ROLLBACK", "BAR-DIRECT-ROLLBACK", 7)],
        )
        operation_task = operation.tasks.get()
        operation_task.container = None
        operation_task.payload = {}
        operation_task.save(update_fields=["container", "payload", "updated_at"])

        with self.assertRaisesMessage(ValueError, "does not match any task"):
            WarehouseWritePathService.mark_processing_boxes_arrived_to_obr(
                agency=self.agency,
                order_id="PROC-DIRECT-ROLLBACK",
                box_codes=["BOX-DIRECT-ROLLBACK"],
                performed_by=self.user,
                source_document_id="MOVE-DIRECT-ROLLBACK",
                operation_id=operation.id,
            )

        operation.refresh_from_db()
        snapshot = WarehouseStockSnapshot.objects.get(
            agency=self.agency,
            container__container_code="BOX-DIRECT-ROLLBACK",
            is_archived=False,
        )
        reserve = WarehouseReserve.objects.get(
            agency=self.agency,
            context_type="processing",
            context_id="PROC-DIRECT-ROLLBACK",
        )
        self.assertEqual(operation.status, WarehouseOperation.STATUS_PLANNED)
        self.assertEqual(operation.done_qty, 0)
        self.assertEqual(snapshot.zone_code, "OS")
        self.assertEqual(snapshot.active_operation_id, operation.id)
        self.assertEqual(snapshot.processing_reserved_qty, 7)
        self.assertEqual(reserve.status, WarehouseReserve.STATUS_ACTIVE)
        self.assertFalse(
            WarehouseEvent.objects.filter(
                stock_context_type="processing",
                stock_context_id="PROC-DIRECT-ROLLBACK",
                event_type=WarehouseEventType.PROCESSING_ZONE_ARRIVED.value,
            ).exists()
        )

    def test_shipping_write_path_moves_snapshot_from_reserved_to_shipped(self):
        shipping_order = ShippingOrder.objects.create(
            number="SHIP-1",
            agency=self.agency,
            created_by=self.user,
            status=ShippingOrder.STATUS_PICKING,
        )
        placement = WarehouseWritePathService.create_receiving_placement(
            agency=self.agency,
            order_id="RCV-SHP-1",
            performed_by=self.user,
            items=[
                {
                    "sku_code": self.sku.sku_code,
                    "name": self.sku.name,
                    "size": "46",
                    "barcode": "2000000077777",
                    "goods_type": "gv",
                    "qty": 6,
                    "pallet_code": "PAL-SHP-1",
                    "box_code": "BOX-SHP-1",
                }
            ],
        )
        putaway = WarehouseWritePathService.request_putaway_for_receiving(
            agency=self.agency,
            order_id="RCV-SHP-1",
            destination_zone_code="OS",
            destination_row_no=6,
            destination_section_no=1,
            destination_tier_no=1,
            destination_cell_no=2,
            requested_by=self.user,
        )
        WarehouseWritePathService.complete_putaway_operation(operation=putaway, performed_by=self.user)

        snapshot = WarehouseStockSnapshot.objects.get(id=placement.snapshot_ids[0])
        snapshot.refresh_from_db()
        self.assertEqual(snapshot.warehouse_state_code, "stored")

        reserves = WarehouseWritePathService.reserve_for_shipping(
            agency=self.agency,
            order_id="SHIP-1",
            created_by=self.user,
            items=[
                {
                    "sku_code": self.sku.sku_code,
                    "size": "46",
                    "barcode": "2000000077777",
                    "goods_type": "gv",
                    "qty": 6,
                }
            ],
        )
        self.assertEqual(len(reserves), 1)
        snapshot.refresh_from_db()
        self.assertEqual(snapshot.warehouse_state_code, "reserved_for_shipping")
        self.assertEqual(snapshot.shipping_reserved_qty, 6)
        self.assertEqual(snapshot.available_qty, 0)

        move_op = WarehouseWritePathService.request_move_to_otg(
            agency=self.agency,
            order_id="SHIP-1",
            destination_row_no=1,
            destination_section_no=1,
            destination_tier_no=1,
            destination_cell_no=1,
            requested_by=self.user,
        )
        WarehouseWritePathService.start_move_to_otg(operation=move_op, performed_by=self.user)
        snapshot.refresh_from_db()
        self.assertEqual(snapshot.warehouse_state_code, "moving_to_otg")

        WarehouseWritePathService.complete_move_to_otg(operation=move_op, performed_by=self.user)
        snapshot.refresh_from_db()
        self.assertEqual(snapshot.warehouse_state_code, "in_otg")
        self.assertEqual(snapshot.zone_code, "OTG")

        palletization = WarehouseWritePathService.start_palletization(
            agency=self.agency,
            order_id="SHIP-1",
            started_by=self.user,
        )
        snapshot.refresh_from_db()
        self.assertEqual(snapshot.warehouse_state_code, "palletizing")

        shipping_pallet = WarehouseContainer.objects.create(
            agency=self.agency,
            container_code="SHIP-TEST-PALLET-1",
            container_type=WarehouseContainer.TYPE_MIXED_PALLET,
            current_location=snapshot.location,
            source_context_type="shipping",
            source_context_id=shipping_order.number,
            created_by=self.user,
        )
        snapshot.parent_container = shipping_pallet
        snapshot.save(update_fields=["parent_container", "updated_at"])
        snapshot.container.parent_container = shipping_pallet
        snapshot.container.save(update_fields=["parent_container", "updated_at"])

        self.assertFalse(
            WarehouseServiceFact.objects.filter(
                client=self.agency,
                order_type=WarehouseServiceFact.ORDER_SHIPPING,
                order_id=shipping_order.number,
            ).exists()
        )
        WarehouseWritePathService.complete_palletization(operation=palletization, performed_by=self.user)
        snapshot.refresh_from_db()
        self.assertEqual(snapshot.warehouse_state_code, "ready_for_loading")

        WarehouseWritePathService.assign_to_trip(
            agency=self.agency,
            order_id="SHIP-1",
            trip_id="TRIP-1",
            assigned_by=self.user,
        )
        snapshot.refresh_from_db()
        self.assertEqual(snapshot.warehouse_state_code, "assigned_to_trip")
        self.assertEqual(snapshot.current_trip_id, "TRIP-1")

        loading = WarehouseWritePathService.start_loading(
            agency=self.agency,
            order_id="SHIP-1",
            trip_id="TRIP-1",
            started_by=self.user,
        )
        snapshot.refresh_from_db()
        self.assertEqual(snapshot.warehouse_state_code, "loading_in_progress")
        self.assertEqual(snapshot.active_operation_id, loading.id)

        WarehouseWritePathService.complete_loading(operation=loading, performed_by=self.user)
        snapshot.refresh_from_db()
        self.assertEqual(snapshot.warehouse_state_code, "loaded_to_vehicle")
        self.assertTrue(snapshot.is_in_vehicle)

        WarehouseWritePathService.ship_order(
            agency=self.agency,
            order_id="SHIP-1",
            trip_id="TRIP-1",
            performed_by=self.user,
        )
        snapshot.refresh_from_db()
        reserve = WarehouseReserve.objects.get(id=reserves[0].id)

        self.assertEqual(snapshot.warehouse_state_code, "shipped")
        self.assertEqual(snapshot.shipping_reserved_qty, 0)
        self.assertEqual(snapshot.available_qty, 0)
        self.assertTrue(snapshot.is_archived)
        self.assertEqual(reserve.status, WarehouseReserve.STATUS_SATISFIED)
        self.assertEqual(reserve.qty_satisfied, 6)
        self.assertTrue(
            WarehouseEvent.objects.filter(
                stock_context_type="shipping",
                stock_context_id="SHIP-1",
                event_type="shipped",
            ).exists()
        )


class WarehouseShippingReserveValidationTests(SimpleTestCase):
    @staticmethod
    def _snapshot(*, qty: int = 6):
        return SimpleNamespace(
            sku_code="SKU-SHIP",
            size="46",
            barcode="2000000077777",
            goods_type="gv",
            warehouse_state_code=WarehouseStateCode.LOADED_TO_VEHICLE.value,
            qty=qty,
            shipping_reserved_qty=qty,
        )

    @staticmethod
    def _reserve(*, qty_satisfied: int):
        return SimpleNamespace(
            sku_code="SKU-SHIP",
            size="46",
            barcode="2000000077777",
            goods_type="gv",
            qty_satisfied=qty_satisfied,
        )

    def test_duplicate_reserves_are_validated_by_aggregate_satisfied_qty(self):
        reserves = [self._reserve(qty_satisfied=3), self._reserve(qty_satisfied=3)]

        result = WarehouseWritePathService._validated_shipping_reserves_for_ship(
            snapshots=[self._snapshot(qty=6)],
            reserves=reserves,
        )

        self.assertEqual(len(result[("SKU-SHIP", "46", "2000000077777", "gv")]), 2)

    def test_shipping_is_blocked_when_otg_reserve_reconciliation_is_incomplete(self):
        with self.assertRaisesRegex(ValueError, "в резервах 5, к отгрузке 6"):
            WarehouseWritePathService._validated_shipping_reserves_for_ship(
                snapshots=[self._snapshot(qty=6)],
                reserves=[self._reserve(qty_satisfied=2), self._reserve(qty_satisfied=3)],
            )


class WarehouseShipOrderReserveWritePathTests(TestCase):
    ORDER_ID = "SHIP-RESERVE-P0E1"
    TRIP_ID = "TRIP-RESERVE-P0E1"

    def setUp(self):
        self.agency = Agency.objects.create(agn_name="Клиент проверки резервов отгрузки")

    def _create_loaded_snapshot(self) -> WarehouseStockSnapshot:
        snapshot = create_warehouse_snapshot_row(
            agency=self.agency,
            order_type="shipping",
            order_id=self.ORDER_ID,
            sku="SKU-SHIP",
            name="Товар отгрузки",
            size="46",
            barcode="2000000077777",
            goods_type="gv",
            qty=6,
            available_qty=0,
            shipping_reserved_qty=6,
            box_code="BOX-SHIP-P0E1",
            pallet_code="PAL-SHIP-P0E1",
            zone="VEH",
            warehouse_state_code=WarehouseStateCode.LOADED_TO_VEHICLE.value,
        )
        loaded_event = WarehouseEvent.objects.create(
            agency=self.agency,
            event_type=WarehouseEventType.LOADED_TO_VEHICLE.value,
            stock_context_type="shipping",
            stock_context_id=self.ORDER_ID,
            container=snapshot.container,
            from_location=snapshot.location,
            to_location=snapshot.location,
            from_zone_code="LOAD",
            to_zone_code="VEH",
            qty=6,
            occurred_at=timezone.now(),
            payload={"trip_id": self.TRIP_ID},
        )
        snapshot.current_trip_id = self.TRIP_ID
        snapshot.is_in_vehicle = True
        snapshot.last_event = loaded_event
        snapshot.save(update_fields=["current_trip_id", "is_in_vehicle", "last_event", "updated_at"])
        return snapshot

    def _create_reserve(self, *, qty_reserved: int, qty_satisfied: int, status: str) -> WarehouseReserve:
        return WarehouseReserve.objects.create(
            agency=self.agency,
            reserve_type=WarehouseReserve.TYPE_SHIPPING,
            context_type="shipping",
            context_id=self.ORDER_ID,
            sku_code="SKU-SHIP",
            size="46",
            barcode="2000000077777",
            goods_type="gv",
            qty_reserved=qty_reserved,
            qty_allocated=qty_satisfied,
            qty_satisfied=qty_satisfied,
            status=status,
        )

    def test_ship_order_does_not_rewrite_duplicate_reserves_settled_at_otg(self):
        snapshot = self._create_loaded_snapshot()
        first = self._create_reserve(
            qty_reserved=6,
            qty_satisfied=3,
            status=WarehouseReserve.STATUS_PARTIALLY_SATISFIED,
        )
        second = self._create_reserve(
            qty_reserved=3,
            qty_satisfied=3,
            status=WarehouseReserve.STATUS_SATISFIED,
        )
        before = list(
            WarehouseReserve.objects.filter(id__in=[first.id, second.id])
            .order_by("id")
            .values_list("id", "qty_allocated", "qty_satisfied", "status", "updated_at")
        )

        WarehouseWritePathService.ship_order(
            agency=self.agency,
            order_id=self.ORDER_ID,
            trip_id=self.TRIP_ID,
        )

        after = list(
            WarehouseReserve.objects.filter(id__in=[first.id, second.id])
            .order_by("id")
            .values_list("id", "qty_allocated", "qty_satisfied", "status", "updated_at")
        )
        snapshot.refresh_from_db()
        self.assertEqual(after, before)
        self.assertEqual(snapshot.warehouse_state_code, WarehouseStateCode.SHIPPED.value)
        self.assertTrue(snapshot.is_archived)

    def test_ship_order_rolls_back_before_events_when_reserve_reconciliation_is_incomplete(self):
        snapshot = self._create_loaded_snapshot()
        self._create_reserve(
            qty_reserved=6,
            qty_satisfied=5,
            status=WarehouseReserve.STATUS_PARTIALLY_SATISFIED,
        )

        with self.assertRaisesRegex(ValueError, "в резервах 5, к отгрузке 6"):
            WarehouseWritePathService.ship_order(
                agency=self.agency,
                order_id=self.ORDER_ID,
                trip_id=self.TRIP_ID,
            )

        snapshot.refresh_from_db()
        self.assertEqual(snapshot.warehouse_state_code, WarehouseStateCode.LOADED_TO_VEHICLE.value)
        self.assertFalse(snapshot.is_archived)
        self.assertFalse(
            WarehouseEvent.objects.filter(
                stock_context_type="shipping",
                stock_context_id=self.ORDER_ID,
                event_type=WarehouseEventType.SHIPPED.value,
            ).exists()
        )

    def test_ship_order_retry_does_not_create_a_second_shipped_event(self):
        snapshot = self._create_loaded_snapshot()
        self._create_reserve(
            qty_reserved=6,
            qty_satisfied=6,
            status=WarehouseReserve.STATUS_SATISFIED,
        )

        WarehouseWritePathService.ship_order(
            agency=self.agency,
            order_id=self.ORDER_ID,
            trip_id=self.TRIP_ID,
        )
        with self.assertRaisesRegex(ValueError, "No loaded snapshots ready for shipping"):
            WarehouseWritePathService.ship_order(
                agency=self.agency,
                order_id=self.ORDER_ID,
                trip_id=self.TRIP_ID,
            )

        snapshot.refresh_from_db()
        shipped_events = WarehouseEvent.objects.filter(
            agency=self.agency,
            stock_context_type="shipping",
            stock_context_id=self.ORDER_ID,
            event_type=WarehouseEventType.SHIPPED.value,
        )
        self.assertEqual(shipped_events.count(), 1)
        self.assertEqual(snapshot.last_event_id, shipped_events.get().id)


class WarehouseTransitionServiceTests(SimpleTestCase):
    def test_receiving_flow_reaches_stored(self):
        result = WarehouseTransitionService.apply_event(
            WarehouseStateCode.UNKNOWN,
            WarehouseEventType.RECEIVING_ARRIVED,
        )
        self.assertEqual(result.code, WarehouseStateCode.RECEIVED_UNPLACED)

        result = WarehouseTransitionService.apply_event(
            result.code,
            WarehouseEventType.PLACEMENT_COMPLETED,
        )
        self.assertEqual(result.code, WarehouseStateCode.PLACED_IN_RECEIVING)

        result = WarehouseTransitionService.apply_event(
            result.code,
            WarehouseEventType.PUTAWAY_REQUESTED,
            operation_type="putaway",
        )
        self.assertEqual(result.code, WarehouseStateCode.PLACED_IN_RECEIVING)
        self.assertTrue(result.is_noop)

        result = WarehouseTransitionService.apply_event(
            result.code,
            WarehouseEventType.PUTAWAY_COMPLETED,
            operation_type="putaway",
            zone_to="OS",
        )
        self.assertEqual(result.code, WarehouseStateCode.STORED)

    def test_processing_flow_requires_obr_arrival(self):
        result = WarehouseTransitionService.apply_event(
            WarehouseStateCode.STORED,
            WarehouseEventType.PROCESSING_RESERVED,
        )
        self.assertEqual(result.code, WarehouseStateCode.RESERVED_FOR_PROCESSING)

        with self.assertRaises(WarehouseTransitionError):
            WarehouseTransitionService.apply_event(
                result.code,
                WarehouseEventType.PROCESSING_STARTED,
            )

        result = WarehouseTransitionService.apply_event(
            result.code,
            WarehouseEventType.MOVEMENT_STARTED,
            operation_type="move_to_processing",
        )
        self.assertEqual(result.code, WarehouseStateCode.MOVING_TO_PROCESSING)

        result = WarehouseTransitionService.apply_event(
            result.code,
            WarehouseEventType.PROCESSING_ZONE_ARRIVED,
            zone_to="OBR",
        )
        self.assertEqual(result.code, WarehouseStateCode.IN_PROCESSING_ZONE)

        result = WarehouseTransitionService.apply_event(
            result.code,
            WarehouseEventType.PROCESSING_STARTED,
        )
        self.assertEqual(result.code, WarehouseStateCode.PROCESSING_IN_PROGRESS)

    def test_shipping_flow_reaches_shipped(self):
        result = WarehouseTransitionService.apply_event(
            WarehouseStateCode.STORED,
            WarehouseEventType.SHIPPING_RESERVED,
        )
        self.assertEqual(result.code, WarehouseStateCode.RESERVED_FOR_SHIPPING)

        result = WarehouseTransitionService.apply_event(
            result.code,
            WarehouseEventType.MOVEMENT_STARTED,
            operation_type="move_to_otg",
        )
        self.assertEqual(result.code, WarehouseStateCode.MOVING_TO_OTG)

        result = WarehouseTransitionService.apply_event(
            result.code,
            WarehouseEventType.OTG_ARRIVED,
            zone_to="OTG",
        )
        self.assertEqual(result.code, WarehouseStateCode.IN_OTG)

        result = WarehouseTransitionService.apply_event(
            result.code,
            WarehouseEventType.PALLETIZATION_STARTED,
        )
        self.assertEqual(result.code, WarehouseStateCode.PALLETIZING)

        result = WarehouseTransitionService.apply_event(
            result.code,
            WarehouseEventType.PALLETIZATION_COMPLETED,
        )
        self.assertEqual(result.code, WarehouseStateCode.READY_FOR_LOADING)

        result = WarehouseTransitionService.apply_event(
            result.code,
            WarehouseEventType.ASSIGNED_TO_TRIP,
        )
        self.assertEqual(result.code, WarehouseStateCode.ASSIGNED_TO_TRIP)

        result = WarehouseTransitionService.apply_event(
            result.code,
            WarehouseEventType.LOADING_STARTED,
        )
        self.assertEqual(result.code, WarehouseStateCode.LOADING_IN_PROGRESS)

        result = WarehouseTransitionService.apply_event(
            result.code,
            WarehouseEventType.LOADED_TO_VEHICLE,
        )
        self.assertEqual(result.code, WarehouseStateCode.LOADED_TO_VEHICLE)

        result = WarehouseTransitionService.apply_event(
            result.code,
            WarehouseEventType.SHIPPED,
        )
        self.assertEqual(result.code, WarehouseStateCode.SHIPPED)


class StorekeeperKnowledgeBaseTests(TestCase):
    def setUp(self):
        from employees.models import Employee

        self.user = get_user_model().objects.create_user(username="sk_kb", password="pwd")
        Employee.objects.create(
            full_name="Кладовщик БЗ",
            user=self.user,
            role="storekeeper",
            is_active=True,
        )
        self.client.force_login(self.user)

    def test_storekeeper_knowledge_base_palletization_guide(self):
        dash = self.client.get("/sklad/")
        self.assertEqual(dash.status_code, 200)
        self.assertContains(dash, "База знаний")
        self.assertContains(dash, 'href="/sklad/knowledge/"')

        catalog = self.client.get("/sklad/knowledge/")
        self.assertEqual(catalog.status_code, 200)
        self.assertContains(catalog, "Приёмка и хранение товара")
        self.assertContains(catalog, "Весовые спецификации и упаковочные листы")
        self.assertContains(catalog, "Сборка монопаллет")
        self.assertContains(catalog, "Паллеты Wildberries (WB)")
        self.assertContains(catalog, "Паллеты Ozon")
        self.assertContains(catalog, "Паллетизация отгрузки в системе")
        self.assertContains(catalog, 'href="/sklad/knowledge/palletization/"')
        self.assertContains(catalog, 'href="/sklad/knowledge/wb-pallets/"')
        self.assertContains(catalog, 'href="/sklad/knowledge/ozon-pallets/"')

        article = self.client.get("/sklad/knowledge/palletization/")
        self.assertEqual(article.status_code, 200)
        self.assertContains(article, "Разложить короба по новым паллетам")
        self.assertContains(article, "/shipping/")
        self.assertContains(article, "Когда кнопки нет")

        wb = self.client.get("/sklad/knowledge/wb-pallets/")
        self.assertEqual(wb.status_code, 200)
        self.assertContains(wb, "не более 3 разных баркодов")
        self.assertContains(wb, "упаковочный лист")

        receiving = self.client.get("/sklad/knowledge/receiving-storage/")
        self.assertEqual(receiving.status_code, 200)
        self.assertContains(receiving, "Завершить приемку")


class WarehouseMovementResolverBatchTests(TestCase):
    def setUp(self):
        self.agency = Agency.objects.create(agn_name="Клиент пакетного статуса")
        self.other_agency = Agency.objects.create(agn_name="Другой клиент")
        self.first_order = ShippingOrder.objects.create(
            number="OTG-BATCH-1",
            agency=self.agency,
            status=ShippingOrder.STATUS_PICKING,
        )
        self.second_order = ShippingOrder.objects.create(
            number="OTG-BATCH-2",
            agency=self.agency,
            status=ShippingOrder.STATUS_PICKING,
        )
        self.request = MoveRequest.objects.create(agency=self.agency, destination_zone="OTG")

    def _task(self, *, payload: dict, status: str, legacy_order_id: str) -> MoveTask:
        return MoveTask.objects.create(
            request=self.request,
            pallet_code=f"PAL-{legacy_order_id}",
            to_zone="OTG",
            payload=payload,
            status=status,
            legacy_order_id=legacy_order_id,
        )

    def test_shipping_orders_are_resolved_with_one_task_query_without_cross_order_leak(self):
        self._task(
            payload={"shipping_order_id": self.first_order.number},
            status=MoveTask.STATUS_CREATED,
            legacy_order_id="MOVE-1",
        )
        self._task(
            payload={"shipping_order_pk": self.first_order.pk},
            status=MoveTask.STATUS_DONE,
            legacy_order_id="MOVE-2",
        )
        self._task(
            payload={"order_id": self.second_order.number},
            status=MoveTask.STATUS_FAILED,
            legacy_order_id="MOVE-3",
        )
        other_request = MoveRequest.objects.create(agency=self.other_agency, destination_zone="OTG")
        MoveTask.objects.create(
            request=other_request,
            pallet_code="PAL-OTHER",
            to_zone="OTG",
            payload={"shipping_order_id": self.first_order.number},
            status=MoveTask.STATUS_IN_PROGRESS,
            legacy_order_id="MOVE-OTHER",
        )

        with self.assertNumQueries(1):
            results = WarehouseMovementResolver.active_for_shipping_orders(
                [self.first_order, self.second_order]
            )

        first = results[self.first_order.pk]
        self.assertTrue(first.has_active_tasks)
        self.assertEqual(first.active_task_count, 1)
        self.assertEqual(first.done_task_count, 1)
        self.assertEqual(first.blocked_task_count, 0)
        self.assertEqual(first.task_ids, ["MOVE-1", "MOVE-2"])

        second = results[self.second_order.pk]
        self.assertFalse(second.has_active_tasks)
        self.assertEqual(second.active_task_count, 0)
        self.assertEqual(second.done_task_count, 0)
        self.assertEqual(second.blocked_task_count, 1)
        self.assertEqual(second.task_ids, ["MOVE-3"])
