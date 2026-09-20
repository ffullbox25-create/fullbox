import json
from types import SimpleNamespace
from datetime import datetime, timedelta
from types import SimpleNamespace

from django.contrib.auth import get_user_model
from django.template.loader import render_to_string
from django.test import Client, RequestFactory, SimpleTestCase, TestCase
from django.utils import timezone

from audit.models import AuditEntry, OrderAuditEntry
from billing.models import (
    BillingApplication,
    BillingService,
    BillingStaffNotification,
    WarehouseServiceFact,
)
from employees.models import Employee
from fullbox.container_codes import rewrite_container_code_goods_type
from fullbox.order_numbers import (
    build_public_order_number,
    format_container_order_number,
    next_public_order_number,
    order_number_sequence,
)
from marking.models import MarkingCode
from reachtruck.models import MoveRequest, MoveTask
from shipping.models import ShippingOrder
from todo.models import Task
from orders.services import ReceivingNomenclatureError, ReceivingWorkflowService
from sklad.models import (
    WarehouseContainer,
    WarehouseOperation,
    WarehouseReserve,
    WarehouseStockSnapshot,
)
from sklad.services.putaway_draft_reservations import PutawayDraftReservationService
from sklad.services.warehouse_state import WarehouseGoodsStateResolver
from sklad.services.warehouse_write_path import WarehouseWritePathService
from sku.models import Agency, SKU, SKUBarcode
from .views import (
    _build_receiving_summary,
    _can_access_receiving_attachment,
    _create_receiving_warehouse_moves,
    _current_responsible_label,
    _detail_history_actor_label,
    _display_order_number,
    _flow_closed_from_entries,
    _history_actor_label,
    _item_key,
    _default_receiving_eta,
    _min_receiving_eta,
    _parse_qty_value,
    _parse_receiving_template,
    _receiving_flow_box_action_meta,
    _status_label_from_entry,
    _template_type_for_header,
)


def _entry(payload: dict):
    return SimpleNamespace(payload=payload)


class OrdersDetailActIsolationTests(SimpleTestCase):
    def test_processing_client_detail_does_not_render_receiving_act_link(self):
        html = render_to_string(
            "orders/detail.html",
            {
                "agency": SimpleNamespace(id=3, agn_name="Processing Agency", fio_agn=""),
                "can_print_receiving_act": True,
                "client_cabinet_url": "",
                "client_orders_url": "",
                "client_view": True,
                "order_id": "27",
                "order_title": "Заявка на обработку",
                "order_type": "processing",
            },
        )

        self.assertNotIn("/orders/receiving/27/act/print/", html)

    def test_receiving_client_detail_keeps_receiving_act_link(self):
        html = render_to_string(
            "orders/detail.html",
            {
                "agency": SimpleNamespace(id=2763, agn_name="Receiving Agency", fio_agn=""),
                "can_print_receiving_act": True,
                "client_cabinet_url": "",
                "client_orders_url": "",
                "client_view": True,
                "order_id": "27",
                "order_title": "Заявка на приемку",
                "order_type": "receiving",
            },
        )

        self.assertIn("/orders/receiving/27/act/print/?client=2763", html)


class OrdersHelpersTests(SimpleTestCase):
    def test_item_key_normalizes_values(self):
        key = _item_key("  SKU-1 ", " Джинсы ", " 42 ")
        self.assertEqual(key, "sku-1|джинсы|42")

    def test_parse_qty_value(self):
        self.assertEqual(_parse_qty_value("10"), 10)
        self.assertEqual(_parse_qty_value(" 0 "), 0)
        self.assertIsNone(_parse_qty_value(""))
        self.assertIsNone(_parse_qty_value("abc"))

    def test_build_receiving_summary_fills_mode_status_and_totals(self):
        summary = _build_receiving_summary(
            receiving_mode="standard",
            status_label="Принято складом, акт приемки отправлен менеджеру",
            display_items=[
                {"sku_code": "A", "name": "Товар", "qty": 10, "actual_qty": 9},
                {"sku_code": "B", "name": "Товар 2", "qty": 5, "actual_qty": 5},
            ],
            placement_pallets=[{"code": "P1", "boxes": [{"code": "B1"}, {"code": "B2"}]}],
            placement_loose_boxes=[{"code": "B3"}],
            act_payload={},
            order_payload={"expected_boxes": 3},
        )
        self.assertEqual(summary["mode_badge"], "Обычная")
        self.assertEqual(summary["mode_label"], "Обычная приемка")
        self.assertIn("Принято складом", summary["status"])
        self.assertEqual(summary["planned_total"], 15)
        self.assertEqual(summary["actual_total"], 14)
        self.assertEqual(summary["accepted_total"], 14)
        self.assertEqual(summary["pallet_count"], 1)
        self.assertEqual(summary["box_count"], 3)
        self.assertEqual(summary["chz_label"], "Нет")
        self.assertEqual(summary["mismatch_badge"], "Есть · 1")
        self.assertEqual(summary["mismatch_tone"], "warn")

    def test_build_receiving_summary_does_not_double_count_pallet_and_flat_boxes(self):
        """Палеты со ссылками на короба + плоский act_boxes — один факт, без x2."""
        summary = _build_receiving_summary(
            receiving_mode="standard",
            status_label="Принято складом",
            display_items=[
                {"sku_code": "A", "name": "Товар", "qty": 400, "actual_qty": 400},
            ],
            placement_pallets=[
                {"code": "P1", "boxes": [{"code": "BX-1"}, {"code": "BX-2"}]},
                {"code": "P2", "boxes": ["BX-3"]},
            ],
            placement_loose_boxes=[],
            act_payload={},
            placement_payload={
                "act_pallets": [
                    {"code": "P1", "boxes": ["BX-1", "BX-2"]},
                    {"code": "P2", "boxes": ["BX-3"]},
                ],
                "act_boxes": [
                    {"code": "BX-1", "items": [{"qty": 100}]},
                    {"code": "BX-2", "items": [{"qty": 100}]},
                    {"code": "BX-3", "items": [{"qty": 200}]},
                ],
            },
            order_payload={"expected_boxes": 3},
        )
        self.assertEqual(summary["accepted_total"], 400)
        self.assertEqual(summary["pallet_count"], 2)
        self.assertEqual(summary["box_count"], 3)

    def test_build_receiving_summary_uses_expected_boxes_when_no_items(self):
        summary = _build_receiving_summary(
            receiving_mode="standard",
            status_label="Принято складом",
            display_items=[],
            placement_pallets=[],
            placement_loose_boxes=[],
            act_payload={"expected_boxes": 1},
            order_payload={"expected_boxes": 1, "place_type": "box"},
        )
        self.assertEqual(summary["accepted_total"], 1)
        self.assertEqual(summary["planned_total"], "—")
        self.assertEqual(summary["actual_total"], "—")
        self.assertEqual(summary["box_count"], 1)
        self.assertEqual(summary["mode_badge"], "Обычная")

    def test_flow_closed_from_entries_prefers_latest_reopen(self):
        entries = [
            _entry({"flow_closed": True}),
            _entry({"flow_reopened": True}),
        ]
        self.assertFalse(_flow_closed_from_entries(entries))

        entries = [
            _entry({"flow_reopened": True}),
            _entry({"flow_closed": True}),
        ]
        self.assertTrue(_flow_closed_from_entries(entries))

    def test_status_label_for_placement_state(self):
        self.assertEqual(
            _status_label_from_entry(_entry({"act": "placement", "act_state": "open"})),
            "Размещение на складе",
        )
        self.assertEqual(
            _status_label_from_entry(_entry({"act": "placement", "act_state": "closed"})),
            "Товар принят и размещен на складе",
        )

    def test_display_order_number_formats_known_types(self):
        self.assertEqual(_display_order_number("shipping", "SO-000001"), "SO-000001")
        self.assertEqual(_display_order_number("shipping", "OTG-000091"), "OTG-000091")
        self.assertEqual(_display_order_number("receiving", "12"), "12_PR")
        self.assertEqual(_display_order_number("processing", "7"), "7_OBR")
        self.assertEqual(_display_order_number("logistics_trip", "36"), "36_LOG")

    def test_display_order_number_keeps_non_numeric_ids(self):
        self.assertEqual(_display_order_number("receiving", "rcv-abcd1234"), "rcv-abcd1234")
        self.assertEqual(_display_order_number("processing", "draft-xyz"), "OBR · черновик xyz")
        self.assertEqual(_display_order_number("receiving", "draft-abc"), "PR · черновик abc")

    def test_format_container_order_number_removes_separators(self):
        self.assertEqual(format_container_order_number("receiving", "12"), "12PR")
        self.assertEqual(format_container_order_number("processing", "7"), "7OBR")
        self.assertEqual(format_container_order_number("shipping", "SO-000001"), "1OTG")
        self.assertEqual(format_container_order_number("shipping", "OTG-000091"), "91OTG")

    def test_public_order_number_helpers_continue_legacy_sequences(self):
        self.assertEqual(order_number_sequence("receiving", "122"), 122)
        self.assertEqual(order_number_sequence("shipping", "91_OTG"), 91)
        self.assertEqual(order_number_sequence("shipping", "SO-000091"), 91)
        self.assertEqual(order_number_sequence("processing", "OBR-000501"), 501)
        self.assertIsNone(order_number_sequence("receiving", "draft-abc"))
        self.assertEqual(build_public_order_number("receiving", 123), "PR-000123")
        self.assertEqual(next_public_order_number("processing", []), "OBR-000501")
        self.assertEqual(next_public_order_number("other", []), "OTH-000015")
        self.assertEqual(next_public_order_number("shipping", ["SO-000091", "91_OTG"]), "OTG-000092")
        self.assertEqual(next_public_order_number("other", ["14", "OTH-000015"]), "OTH-000016")

    def test_receiving_flow_box_action_meta_supports_batch_actions(self):
        self.assertEqual(
            _receiving_flow_box_action_meta("delete_batch"),
            ("delete", "Удаление группы коробов"),
        )
        self.assertEqual(
            _receiving_flow_box_action_meta("move_batch"),
            ("update", "Перемещение группы коробов"),
        )
        self.assertEqual(
            _receiving_flow_box_action_meta("print_batch"),
            ("update", "Печать этикеток группы коробов"),
        )

    def test_min_receiving_eta_allows_same_day_future_time(self):
        current = timezone.make_aware(datetime(2026, 4, 2, 11, 2))
        self.assertEqual(
            _min_receiving_eta(current),
            timezone.make_aware(datetime(2026, 4, 2, 11, 5)),
        )

    def test_default_receiving_eta_is_one_hour_ahead_and_rounded_up(self):
        current = timezone.make_aware(datetime(2026, 4, 2, 11, 2))
        self.assertEqual(
            _default_receiving_eta(current),
            timezone.make_aware(datetime(2026, 4, 2, 12, 5)),
        )

    def test_rewrite_container_code_goods_type_replaces_suffix(self):
        self.assertEqual(
            rewrite_container_code_goods_type("TDT-0106-000301-gv", "br"),
            "TDT-0106-000301-br",
        )
        self.assertEqual(
            rewrite_container_code_goods_type("TDT-1PR-0000299-gv", "br"),
            "TDT-1PR-0000299-br",
        )


class OrdersReceivingReachtruckTests(TestCase):
    def test_create_receiving_warehouse_moves_skips_pr_to_pr_tasks(self):
        user_model = get_user_model()
        user = user_model.objects.create_user(username="receiving_guard", password="pwd")
        agency = Agency.objects.create(agn_name="Тест Клиент")
        placement_entry = OrderAuditEntry.objects.create(
            order_id="R-PR-1",
            order_type="receiving",
            action="status",
            agency=agency,
            user=user,
            description="Акт размещения",
            payload={
                "act": "placement",
                "act_state": "closed",
                "act_pallets": [
                    {
                        "code": "PAL-PR-1",
                        "location": {"zone": "PR", "row": "", "section": "", "tier": "", "cell": ""},
                    }
                ],
            },
        )

        request = RequestFactory().post("/orders/receiving/R-PR-1/create-warehouse-moves/")
        request.user = user

        created, skipped_existing, skipped_missing, total = _create_receiving_warehouse_moves(
            "R-PR-1",
            [placement_entry],
            request,
        )

        self.assertEqual((created, skipped_existing, skipped_missing, total), (0, 0, 1, 1))
        self.assertEqual(MoveTask.objects.count(), 0)

    def test_create_receiving_warehouse_moves_creates_only_pallets_with_confirmed_destination(self):
        user_model = get_user_model()
        user = user_model.objects.create_user(username="receiving_partial", password="pwd")
        agency = Agency.objects.create(agn_name="Тест Клиент 2")
        placement_entry = OrderAuditEntry.objects.create(
            order_id="R-PR-2",
            order_type="receiving",
            action="status",
            agency=agency,
            user=user,
            description="Акт размещения",
            payload={
                "act": "placement",
                "act_state": "closed",
                "act_pallets": [
                    {
                        "code": "PAL-READY-1",
                        "location": {"zone": "OS", "row": 1, "section": 1, "tier": 1, "cell": 1},
                    },
                    {
                        "code": "PAL-PR-2",
                        "location": {"zone": "PR", "row": "", "section": "", "tier": "", "cell": ""},
                    },
                ],
            },
        )

        request = RequestFactory().post("/orders/receiving/R-PR-2/create-warehouse-moves/")
        request.user = user

        created, skipped_existing, skipped_missing, total = _create_receiving_warehouse_moves(
            "R-PR-2",
            [placement_entry],
            request,
        )

        self.assertEqual((created, skipped_existing, skipped_missing, total), (1, 0, 1, 2))
        self.assertEqual(MoveTask.objects.count(), 1)
        self.assertEqual(MoveTask.objects.get().pallet_code, "PAL-READY-1")

    def test_create_receiving_warehouse_moves_uses_selected_destinations(self):
        user_model = get_user_model()
        user = user_model.objects.create_user(username="receiving_modal", password="pwd")
        agency = Agency.objects.create(agn_name="Тест Клиент 3")
        placement_entry = OrderAuditEntry.objects.create(
            order_id="R-PR-3",
            order_type="receiving",
            action="status",
            agency=agency,
            user=user,
            description="Акт размещения",
            payload={
                "act": "placement",
                "act_state": "closed",
                "act_pallets": [
                    {
                        "code": "PAL-OVERRIDE-1",
                        "location": {"zone": "PR", "row": "", "section": "", "tier": "", "cell": ""},
                    }
                ],
            },
        )
        request = RequestFactory().post("/orders/receiving/R-PR-3/create-warehouse-moves/")
        request.user = user

        created, skipped_existing, skipped_missing, total = _create_receiving_warehouse_moves(
            "R-PR-3",
            [placement_entry],
            request,
            destinations_by_pallet={
                "PAL-OVERRIDE-1": {"zone": "OS", "row": 2, "section": 1, "tier": 1, "cell": 3},
            },
        )

        self.assertEqual((created, skipped_existing, skipped_missing, total), (1, 0, 0, 1))
        task = MoveTask.objects.get()
        self.assertEqual(task.pallet_code, "PAL-OVERRIDE-1")
        self.assertEqual(task.to_zone, "OS")
        self.assertEqual(task.to_row, 2)
        self.assertEqual(task.to_section, 1)
        self.assertEqual(task.to_tier, 1)
        self.assertEqual(task.to_cell, 3)

    def test_create_receiving_warehouse_moves_links_legacy_task_with_warehouse_putaway(self):
        user_model = get_user_model()
        user = user_model.objects.create_user(username="receiving_putaway_bridge", password="pwd")
        agency = Agency.objects.create(agn_name="Тест Клиент 4")
        SKU.objects.create(agency=agency, sku_code="SKU-BRIDGE-1", name="Товар моста", size="42")
        placement_entry = OrderAuditEntry.objects.create(
            order_id="R-PR-4",
            order_type="receiving",
            action="status",
            agency=agency,
            user=user,
            description="Акт размещения",
            payload={
                "act": "placement",
                "act_state": "closed",
                "act_boxes": [
                    {
                        "code": "BOX-BRIDGE-1",
                        "sealed": True,
                        "items": [
                            {
                                "sku": "SKU-BRIDGE-1",
                                "name": "Товар моста",
                                "size": "42",
                                "barcode": "BR-1",
                                "qty": 5,
                            }
                        ],
                    }
                ],
                "act_pallets": [
                    {
                        "code": "PAL-BRIDGE-1",
                        "sealed": True,
                        "boxes": ["BOX-BRIDGE-1"],
                        "items": [],
                        "location": {"zone": "OS", "row": 2, "section": 1, "tier": 1, "cell": 3},
                    }
                ],
            },
        )

        request = RequestFactory().post("/orders/receiving/R-PR-4/create-warehouse-moves/")
        request.user = user

        created, skipped_existing, skipped_missing, total = _create_receiving_warehouse_moves(
            "R-PR-4",
            [placement_entry],
            request,
        )

        self.assertEqual((created, skipped_existing, skipped_missing, total), (1, 0, 0, 1))
        task = MoveTask.objects.get()
        operation = WarehouseOperation.objects.get(context_type="receiving", context_id="R-PR-4")
        warehouse_task = operation.tasks.get()
        self.assertEqual(operation.operation_type, WarehouseOperation.TYPE_PUTAWAY)
        self.assertEqual(operation.destination_zone_code, "OS")
        self.assertEqual(warehouse_task.payload.get("legacy_move_id"), task.legacy_order_id)
        self.assertEqual((task.payload or {}).get("warehouse_operation_id"), operation.id)
        self.assertEqual((task.payload or {}).get("warehouse_operation_task_id"), warehouse_task.id)


class OrdersReceivingMarkingFlowTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.user = user_model.objects.create_user(username="receiving_cz", password="pwd")
        self.agency = Agency.objects.create(agn_name="Клиент ЧЗ")
        self.sku = SKU.objects.create(
            agency=self.agency,
            sku_code="SKU-CZ-1",
            name="Маркируемый товар",
            size="42",
            honest_sign=True,
        )
        Employee.objects.create(user=self.user, role="storekeeper", full_name="Кладовщик Тест", is_active=True)
        self.client.force_login(self.user)

    def _create_receiving_status(self, order_id: str, extra_payload: dict | None = None):
        payload = {
            "status": "warehouse",
            "status_label": "В ожидании поставки товара",
            "goods_type": "op",
            "goods_type_label": "Оптовый",
            "items": [
                {
                    "sku_code": self.sku.sku_code,
                    "name": self.sku.name,
                    "size": self.sku.size,
                    "qty": 2,
                }
            ],
        }
        if extra_payload:
            payload.update(extra_payload)
        return OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="receiving",
            action="status",
            agency=self.agency,
            user=self.user,
            description="Статус",
            payload=payload,
        )

    def test_opening_signed_act_does_not_create_manager_task(self):
        manager_user = get_user_model().objects.create_user(
            username="orders_act_get_manager", password="pwd"
        )
        Employee.objects.create(
            full_name="Менеджер акта без GET",
            role="manager",
            user=manager_user,
            is_active=True,
        )
        self.agency.mened_user_id = manager_user.id
        self.agency.save(update_fields=["mened_user_id"])
        order_id = "PR-ACT-GET-NO-WRITE"
        self._create_receiving_status(order_id)
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="receiving",
            action="status",
            agency=self.agency,
            user=self.user,
            description="Акт приемки подписан кладовщиком",
            payload={
                "act": "receiving",
                "act_label": "Акт приемки",
                "act_storekeeper_signed": True,
                "act_manager_signed": False,
                "act_items": [
                    {"sku_code": "SKU", "name": "Товар", "qty": 1, "actual_qty": 1}
                ],
            },
        )

        response = self.client.get(f"/orders/receiving/{order_id}/act/print/")

        self.assertEqual(response.status_code, 200)
        self.assertFalse(
            Task.objects.filter(route=f"/orders/receiving/{order_id}/act/print/").exists()
        )

    def test_receiving_flow_persists_cz_mode_in_status_payload(self):
        order_id = "R-CZ-MODE"
        self._create_receiving_status(order_id)

        response = self.client.post(
            f"/orders/receiving/{order_id}/",
            {
                "action": "create_receiving_act",
                "goods_type": "op",
                "receiving_mode": "cz",
            },
        )

        self.assertEqual(response.status_code, 302)
        latest = (
            OrderAuditEntry.objects.filter(order_id=order_id, order_type="receiving")
            .order_by("-id")
            .first()
        )
        self.assertEqual((latest.payload or {}).get("receiving_mode"), "cz")

    def test_receiving_flow_forces_standard_mode_for_unprocessed_goods(self):
        order_id = "R-NO-FORCES-STANDARD"
        self._create_receiving_status(order_id)

        response = self.client.post(
            f"/orders/receiving/{order_id}/",
            {
                "action": "create_receiving_act",
                "goods_type": "no",
                "receiving_mode": "cz",
            },
        )

        self.assertEqual(response.status_code, 302)
        latest = (
            OrderAuditEntry.objects.filter(order_id=order_id, order_type="receiving")
            .order_by("-id")
            .first()
        )
        self.assertEqual((latest.payload or {}).get("goods_type"), "no")
        self.assertEqual((latest.payload or {}).get("receiving_mode"), "standard")

    def test_receiving_flow_page_treats_unprocessed_goods_as_standard_mode(self):
        order_id = "R-NO-CZ-DISPLAY"
        self._create_receiving_status(order_id, {"goods_type": "no", "receiving_mode": "cz"})

        response = self.client.get(f"/orders/receiving/{order_id}/flow/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["receiving_mode"], "standard")
        self.assertContains(response, 'placeholder="Сканируйте ШК"', html=False)
        self.assertNotContains(response, 'placeholder="Сканируйте код ЧЗ"', html=False)

    def test_receiving_flow_page_contains_blocking_scan_issue_modal_and_layout_guard(self):
        order_id = "R-SCAN-LAYOUT-WARNING"
        self._create_receiving_status(
            order_id,
            {"goods_type": "op", "receiving_mode": "standard"},
        )

        response = self.client.get(f"/orders/receiving/{order_id}/flow/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="scan-issue-modal"', html=False)
        self.assertContains(response, 'Проверил, продолжить')
        self.assertContains(response, "Неправильная раскладка")
        self.assertContains(response, "hasCyrillicCharacters")
        self.assertContains(response, "scanIssueBlocked")

    def test_manager_receiving_flow_cabinet_link_opens_request_client(self):
        order_id = "R-MANAGER-CLIENT-CABINET"
        self._create_receiving_status(order_id)
        manager = get_user_model().objects.create_user(username="receiving_manager", password="pwd")
        Employee.objects.create(user=manager, role="manager", full_name="Менеджер приемки", is_active=True)
        self.client.force_login(manager)

        response = self.client.get(f"/orders/receiving/{order_id}/flow/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["cabinet_url"], f"/client/dashboard/lk/?client={self.agency.id}")

    def test_manager_can_confirm_receiving_after_late_draft_autosave(self):
        order_id = "R-MANAGER-LATE-DRAFT"
        manager = get_user_model().objects.create_user(username="receiving_confirm_manager", password="pwd")
        Employee.objects.create(user=manager, role="manager", full_name="Менеджер подтверждения", is_active=True)
        for payload in (
            {"status": "sent_unconfirmed", "status_label": "Ждет подтверждения", "items": []},
            {"status": "draft", "status_label": "Черновик", "items": []},
        ):
            OrderAuditEntry.objects.create(
                order_id=order_id,
                order_type="receiving",
                action="update",
                agency=self.agency,
                user=manager,
                description="Техническое сохранение",
                payload=payload,
            )
        self.client.force_login(manager)

        response = self.client.get(f"/orders/receiving/{order_id}/")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["can_send_to_warehouse"])
        self.assertContains(response, "Подтвердить и передать на склад")

    def test_receiving_flow_goods_type_retry_fixes_unprocessed_cz_mode(self):
        order_id = "R-NO-CZ-RETRY"
        self._create_receiving_status(order_id, {"goods_type": "no", "receiving_mode": "cz"})

        response = self.client.post(
            f"/orders/receiving/{order_id}/flow/",
            {
                "flow_action": "set_goods_type",
                "goods_type": "no",
                "receiving_mode": "cz",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertIn("goods_type_updated=1", response.url)
        latest = (
            OrderAuditEntry.objects.filter(order_id=order_id, order_type="receiving")
            .order_by("-id")
            .first()
        )
        self.assertEqual((latest.payload or {}).get("goods_type"), "no")
        self.assertEqual((latest.payload or {}).get("receiving_mode"), "standard")

    def test_receiving_flow_post_switches_cz_mode_to_standard_inside_flow(self):
        order_id = "R-CZ-TO-STANDARD"
        self._create_receiving_status(order_id, {"goods_type": "op", "receiving_mode": "cz"})

        response = self.client.post(
            f"/orders/receiving/{order_id}/flow/",
            {
                "flow_action": "set_goods_type",
                "goods_type": "op",
                "receiving_mode": "standard",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertIn("goods_type_updated=1", response.url)
        latest = (
            OrderAuditEntry.objects.filter(order_id=order_id, order_type="receiving")
            .order_by("-id")
            .first()
        )
        self.assertEqual((latest.payload or {}).get("goods_type"), "op")
        self.assertEqual((latest.payload or {}).get("receiving_mode"), "standard")

    def test_receiving_flow_settings_save_without_changes_is_not_error(self):
        order_id = "R-FLOW-SETTINGS-NOOP"
        self._create_receiving_status(order_id, {"goods_type": "op", "receiving_mode": "standard"})

        response = self.client.post(
            f"/orders/receiving/{order_id}/flow/",
            {
                "flow_action": "set_goods_type",
                "goods_type": "op",
                "receiving_mode": "standard",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertIn("goods_type_updated=1", response.url)
        latest = (
            OrderAuditEntry.objects.filter(order_id=order_id, order_type="receiving")
            .order_by("-id")
            .first()
        )
        self.assertEqual((latest.payload or {}).get("goods_type"), "op")
        self.assertEqual((latest.payload or {}).get("receiving_mode"), "standard")

    def test_receiving_flow_page_contains_receiving_mode_editor(self):
        order_id = "R-FLOW-MODE-EDITOR"
        self._create_receiving_status(order_id, {"goods_type": "op", "receiving_mode": "cz"})

        response = self.client.get(f"/orders/receiving/{order_id}/flow/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="change-receiving-mode-select"', html=False)
        self.assertContains(response, "Без ЧЗ")
        self.assertContains(response, "С ЧЗ")

    def test_receiving_detail_page_renders_with_marked_items(self):
        order_id = "R-CZ-DETAIL"
        self._create_receiving_status(order_id, {"receiving_mode": "cz"})

        response = self.client.get(f"/orders/receiving/{order_id}/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Потоковая приемка с ЧЗ")

    def test_client_required_chz_exposes_only_chz_receiving_start(self):
        order_id = "R-CLIENT-CZ-ONLY"
        self._create_receiving_status(
            order_id,
            {"receiving_chz_service": "required"},
        )

        response = self.client.get(f"/orders/receiving/{order_id}/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Сканировать Честный знак")
        self.assertContains(response, 'data-receiving-mode="cz"', count=1, html=False)
        self.assertNotContains(response, 'data-receiving-mode="standard"', html=False)

    def test_client_required_chz_starts_in_chz_mode_without_stock_writes(self):
        order_id = "R-CLIENT-CZ-START"
        SKUBarcode.objects.create(
            sku=self.sku,
            value="CLIENT-CZ-START-BARCODE",
            size=self.sku.size,
            is_primary=True,
        )
        self._create_receiving_status(
            order_id,
            {"receiving_chz_service": "required"},
        )
        stock_counts = (
            WarehouseStockSnapshot.objects.count(),
            WarehouseReserve.objects.count(),
            WarehouseContainer.objects.count(),
        )

        response = self.client.post(
            f"/orders/receiving/{order_id}/",
            {
                "action": "create_receiving_act",
                "goods_type": "op",
                "receiving_mode": "cz",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            (OrderAuditEntry.objects.filter(
                order_id=order_id,
                order_type="receiving",
            ).latest("id").payload or {}).get("receiving_mode"),
            "cz",
        )
        self.assertEqual(
            (
                WarehouseStockSnapshot.objects.count(),
                WarehouseReserve.objects.count(),
                WarehouseContainer.objects.count(),
            ),
            stock_counts,
        )

    def test_client_optional_chz_exposes_both_receiving_starts(self):
        order_id = "R-CLIENT-CHZ-OPTIONAL"
        self._create_receiving_status(
            order_id,
            {"receiving_chz_service": "not_required"},
        )

        response = self.client.get(f"/orders/receiving/{order_id}/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Честный знак по выбору приемщика")
        self.assertContains(response, 'data-receiving-mode="standard"', count=1, html=False)
        self.assertContains(response, 'data-receiving-mode="cz"', count=1, html=False)

    def test_client_required_chz_locks_mode_inside_receiving_flow(self):
        order_id = "R-CLIENT-CZ-LOCKED"
        self._create_receiving_status(
            order_id,
            {
                "receiving_chz_service": "required",
                "receiving_mode": "cz",
            },
        )

        response = self.client.get(f"/orders/receiving/{order_id}/flow/")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["receiving_mode_locked_by_client"])
        self.assertContains(response, "Услуга клиента: Сканировать Честный знак")
        self.assertNotContains(response, 'id="change-receiving-mode-select"', html=False)

    def test_mismatched_client_mode_does_not_start_receiving_or_touch_stock(self):
        order_id = "R-CLIENT-CZ-MISMATCH"
        self._create_receiving_status(
            order_id,
            {"receiving_chz_service": "required"},
        )
        entry_count = OrderAuditEntry.objects.filter(
            order_id=order_id,
            order_type="receiving",
        ).count()
        stock_counts = (
            WarehouseStockSnapshot.objects.count(),
            WarehouseReserve.objects.count(),
            WarehouseContainer.objects.count(),
        )

        response = self.client.post(
            f"/orders/receiving/{order_id}/",
            {
                "action": "create_receiving_act",
                "goods_type": "op",
                "receiving_mode": "standard",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            OrderAuditEntry.objects.filter(
                order_id=order_id,
                order_type="receiving",
            ).count(),
            entry_count,
        )
        self.assertEqual(
            (
                WarehouseStockSnapshot.objects.count(),
                WarehouseReserve.objects.count(),
                WarehouseContainer.objects.count(),
            ),
            stock_counts,
        )

    def test_client_required_chz_rejects_unprocessed_goods_without_writes(self):
        order_id = "R-CLIENT-CZ-UNPROCESSED"
        self._create_receiving_status(
            order_id,
            {"receiving_chz_service": "required"},
        )
        entry_count = OrderAuditEntry.objects.filter(
            order_id=order_id,
            order_type="receiving",
        ).count()

        response = self.client.post(
            f"/orders/receiving/{order_id}/",
            {
                "action": "create_receiving_act",
                "goods_type": "no",
                "receiving_mode": "cz",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            OrderAuditEntry.objects.filter(
                order_id=order_id,
                order_type="receiving",
            ).count(),
            entry_count,
        )

    def test_receiving_marked_items_fallbacks_to_order_items_without_honest_sign_flag(self):
        self.sku.honest_sign = False
        self.sku.save(update_fields=["honest_sign"])
        order_id = "R-CZ-FALLBACK"
        self._create_receiving_status(order_id, {"receiving_mode": "cz"})

        response = self.client.get(f"/orders/receiving/{order_id}/flow/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Выберите SKU и размер")
        self.assertContains(response, "SKU-CZ-1")

    def test_receiving_flow_closes_with_act_units_for_marked_goods(self):
        order_id = "R-CZ-FLOW"
        self._create_receiving_status(order_id, {"receiving_mode": "cz"})
        now = timezone.localtime()
        MarkingCode.objects.create(
            order_type="receiving",
            order_id=order_id,
            agency=self.agency,
            sku=self.sku,
            sku_code=self.sku.sku_code,
            size=self.sku.size,
            barcode="20001",
            box_barcode="BOX-CZ-1",
            code="CZ-0001",
            source="scan",
            created_by=self.user,
            used_at=now,
            used_by=self.user,
        )
        MarkingCode.objects.create(
            order_type="receiving",
            order_id=order_id,
            agency=self.agency,
            sku=self.sku,
            sku_code=self.sku.sku_code,
            size=self.sku.size,
            barcode="20001",
            box_barcode="BOX-CZ-1",
            code="CZ-0002",
            source="scan",
            created_by=self.user,
            used_at=now,
            used_by=self.user,
        )

        response = self.client.post(
            f"/orders/receiving/{order_id}/flow/",
            {
                "boxes_json": '[{"code":"BOX-CZ-1","items":[{"sku_code":"SKU-CZ-1","name":"Маркируемый товар","size":"42","qty":2}],"sealed":true}]',
                "pallets_json": '[{"code":"PAL-CZ-1","boxes":["BOX-CZ-1"],"items":[],"sealed":true,"location":{"zone":"PR"}}]',
            },
        )

        self.assertEqual(response.status_code, 302)
        act_entry = OrderAuditEntry.objects.filter(
            order_id=order_id,
            order_type="receiving",
            payload__act="receiving",
        ).latest("created_at")
        act_units = (act_entry.payload or {}).get("act_units") or []
        self.assertEqual(len(act_units), 2)
        self.assertEqual({unit["marking_code"] for unit in act_units}, {"CZ-0001", "CZ-0002"})
        self.assertTrue(all(unit["pallet_code"] == "PAL-CZ-1" for unit in act_units))

        placement_entry = OrderAuditEntry.objects.filter(
            order_id=order_id,
            order_type="receiving",
            payload__act="placement",
        ).latest("created_at")
        self.assertEqual(len((placement_entry.payload or {}).get("act_units") or []), 2)

    def test_print_receiving_act_shows_marking_column_for_act_units(self):
        order_id = "R-CZ-PRINT"
        self._create_receiving_status(order_id)
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="receiving",
            action="update",
            agency=self.agency,
            user=self.user,
            description="Поток закрыт",
            payload={
                "flow_closed": True,
                "flow_closed_at": timezone.localtime().isoformat(),
                "flow_boxes": [{"code": "BOX-R-HEAD-SIGN-1"}],
                "flow_pallets": [{"code": "PAL-R-HEAD-SIGN-1"}],
            },
        )
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="receiving",
            action="status",
            agency=self.agency,
            user=self.user,
            description="Акт приемки",
            payload={
                "act": "receiving",
                "eta_at": timezone.localtime().isoformat(),
                "vehicle_number": "A123BC",
                "act_units": [
                    {
                        "sku_code": "SKU-CZ-1",
                        "name": "Маркируемый товар",
                        "size": "42",
                        "barcode": "20001",
                        "marking_code": "CZ-PRINT-1",
                        "box_code": "BOX-CZ-1",
                    }
                ],
            },
        )
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="receiving",
            action="update",
            agency=self.agency,
            user=self.user,
            description="Акт размещения",
            payload={
                "act": "placement",
                "act_state": "closed",
                "act_boxes": [{"code": "BOX-CZ-1", "items": []}],
                "act_pallets": [{"code": "PAL-CZ-1", "boxes": ["BOX-CZ-1"], "items": []}],
            },
        )

        response = self.client.get(f"/orders/receiving/{order_id}/act/print/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "ЧЗ")
        self.assertContains(response, "CZ-PRINT-1")

    def test_print_receiving_act_splits_row_for_changed_goods_type(self):
        order_id = "R-CZ-PRINT-TYPE"
        self._create_receiving_status(order_id)
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="receiving",
            action="status",
            agency=self.agency,
            user=self.user,
            description="Акт приемки",
            payload={
                "act": "receiving",
                "goods_type": "op",
                "goods_type_label": "Оптовый",
                "act_items": [
                    {
                        "sku_code": "SKU-CZ-1",
                        "name": "Маркируемый товар",
                        "size": "42",
                        "planned_qty": 2,
                        "actual_qty": 2,
                    }
                ],
            },
        )
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="receiving",
            action="update",
            agency=self.agency,
            user=self.user,
            description="Акт размещения",
            payload={
                "act": "placement",
                "act_state": "closed",
                "goods_type": "op",
                "act_boxes": [
                    {
                        "code": "BOX-CZ-TYPE-0",
                        "goods_type": "op",
                        "items": [
                            {
                                "sku_code": "SKU-CZ-1",
                                "name": "Маркируемый товар",
                                "size": "42",
                                "qty": 1,
                                "goods_type": "op",
                            }
                        ],
                    },
                    {
                        "code": "BOX-CZ-TYPE-1",
                        "goods_type": "br",
                        "items": [
                            {
                                "sku_code": "SKU-CZ-1",
                                "name": "Маркируемый товар",
                                "size": "42",
                                "qty": 1,
                                "goods_type": "br",
                            }
                        ],
                    }
                ],
                "act_pallets": [{"code": "PAL-CZ-TYPE-1", "boxes": ["BOX-CZ-TYPE-0", "BOX-CZ-TYPE-1"], "items": []}],
            },
        )

        response = self.client.get(f"/orders/receiving/{order_id}/act/print/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["items"]), 2)
        self.assertEqual(response.context["items"][0]["planned"], 2)
        self.assertEqual(response.context["items"][0]["actual"], 1)
        self.assertEqual(response.context["items"][0]["mismatch"], -1)
        self.assertEqual(response.context["items"][1]["planned"], 0)
        self.assertEqual(response.context["items"][1]["actual"], 1)
        self.assertEqual(response.context["items"][1]["mismatch"], 1)
        self.assertContains(response, "Маркируемый товар")
        self.assertContains(response, "Маркируемый товар (Брак)")
        self.assertNotContains(response, "BOX-CZ-TYPE-1")
        self.assertNotContains(response, "Короба с измененным типом товара")

    def test_print_receiving_act_prefers_document_placement_over_later_move_snapshot(self):
        order_id = "R-CZ-PRINT-TYPE-MOVE"
        self._create_receiving_status(order_id)
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="receiving",
            action="status",
            agency=self.agency,
            user=self.user,
            description="Акт приемки",
            payload={
                "act": "receiving",
                "goods_type": "op",
                "goods_type_label": "Оптовый",
                "act_items": [
                    {
                        "sku_code": "SKU-CZ-1",
                        "name": "Маркируемый товар",
                        "size": "42",
                        "planned_qty": 2,
                        "actual_qty": 2,
                    }
                ],
            },
        )
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="receiving",
            action="update",
            agency=self.agency,
            user=self.user,
            description="Создан акт размещения",
            payload={
                "act": "placement",
                "act_label": "Акт размещения",
                "act_state": "closed",
                "goods_type": "op",
                "act_items": [
                    {
                        "sku_code": "SKU-CZ-1",
                        "name": "Маркируемый товар",
                        "size": "42",
                        "actual_qty": 2,
                        "box_qty": 2,
                        "pallet_qty": 2,
                    }
                ],
                "act_boxes": [
                    {
                        "code": "BOX-CZ-TYPE-MOVE-0",
                        "goods_type": "op",
                        "items": [
                            {
                                "sku_code": "SKU-CZ-1",
                                "name": "Маркируемый товар",
                                "size": "42",
                                "qty": 1,
                                "goods_type": "op",
                            }
                        ],
                    },
                    {
                        "code": "BOX-CZ-TYPE-MOVE-1",
                        "goods_type": "br",
                        "items": [
                            {
                                "sku_code": "SKU-CZ-1",
                                "name": "Маркируемый товар",
                                "size": "42",
                                "qty": 1,
                                "goods_type": "br",
                            }
                        ],
                    },
                ],
                "act_pallets": [
                    {"code": "PAL-CZ-TYPE-MOVE-0", "boxes": ["BOX-CZ-TYPE-MOVE-0"], "items": []},
                    {"code": "PAL-CZ-TYPE-MOVE-1", "boxes": ["BOX-CZ-TYPE-MOVE-1"], "items": []},
                ],
            },
        )
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="receiving",
            action="status",
            agency=self.agency,
            user=self.user,
            description="Перемещение паллеты PAL-CZ-TYPE-MOVE-0",
            payload={
                "act": "placement",
                "act_state": "closed",
                "act_boxes": [
                    {
                        "code": "BOX-CZ-TYPE-MOVE-0",
                        "items": [
                            {
                                "sku_code": "SKU-CZ-1",
                                "name": "Маркируемый товар",
                                "size": "42",
                                "qty": 1,
                                "goods_type": "op",
                            }
                        ],
                    }
                ],
                "act_pallets": [{"code": "PAL-CZ-TYPE-MOVE-0", "boxes": ["BOX-CZ-TYPE-MOVE-0"], "items": []}],
            },
        )

        response = self.client.get(f"/orders/receiving/{order_id}/act/print/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["items"]), 2)
        self.assertEqual(response.context["items"][0]["actual"], 1)
        self.assertEqual(response.context["items"][1]["actual"], 1)
        self.assertContains(response, "Маркируемый товар (Брак)")

    def test_receiving_detail_page_splits_row_for_changed_goods_type(self):
        order_id = "R-CZ-DETAIL-TYPE"
        self._create_receiving_status(order_id)
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="receiving",
            action="status",
            agency=self.agency,
            user=self.user,
            description="Акт приемки",
            payload={
                "act": "receiving",
                "goods_type": "op",
                "goods_type_label": "Оптовый",
                "act_items": [
                    {
                        "sku_code": "SKU-CZ-1",
                        "name": "Маркируемый товар",
                        "size": "42",
                        "planned_qty": 2,
                        "actual_qty": 2,
                    }
                ],
            },
        )
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="receiving",
            action="update",
            agency=self.agency,
            user=self.user,
            description="Акт размещения",
            payload={
                "act": "placement",
                "act_state": "closed",
                "goods_type": "op",
                "act_boxes": [
                    {
                        "code": "BOX-CZ-DETAIL-0",
                        "goods_type": "op",
                        "items": [
                            {
                                "sku_code": "SKU-CZ-1",
                                "name": "Маркируемый товар",
                                "size": "42",
                                "qty": 1,
                                "goods_type": "op",
                            }
                        ],
                    },
                    {
                        "code": "BOX-CZ-DETAIL-1",
                        "goods_type": "br",
                        "items": [
                            {
                                "sku_code": "SKU-CZ-1",
                                "name": "Маркируемый товар",
                                "size": "42",
                                "qty": 1,
                                "goods_type": "br",
                            }
                        ],
                    },
                ],
                "act_pallets": [{"code": "PAL-CZ-DETAIL-1", "boxes": ["BOX-CZ-DETAIL-0", "BOX-CZ-DETAIL-1"], "items": []}],
            },
        )

        response = self.client.get(f"/orders/receiving/{order_id}/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["items"]), 2)
        self.assertEqual(response.context["items"][0]["qty"], 2)
        self.assertEqual(response.context["items"][0]["actual_qty"], 1)
        self.assertEqual(response.context["items"][1]["qty"], 0)
        self.assertEqual(response.context["items"][1]["actual_qty"], 1)
        self.assertEqual(response.context["items"][1]["name"], "Маркируемый товар (Брак)")
        self.assertContains(response, "Маркируемый товар (Брак)")


    def test_receiving_detail_page_prefers_document_placement_over_later_move_snapshot(self):
        order_id = "R-CZ-DETAIL-TYPE-MOVE"
        self._create_receiving_status(order_id)
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="receiving",
            action="status",
            agency=self.agency,
            user=self.user,
            description="Акт приемки",
            payload={
                "act": "receiving",
                "goods_type": "op",
                "goods_type_label": "Оптовый",
                "act_items": [
                    {
                        "sku_code": "SKU-CZ-1",
                        "name": "Маркируемый товар",
                        "size": "42",
                        "planned_qty": 2,
                        "actual_qty": 2,
                    }
                ],
            },
        )
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="receiving",
            action="update",
            agency=self.agency,
            user=self.user,
            description="Создан акт размещения",
            payload={
                "act": "placement",
                "act_label": "Акт размещения",
                "act_state": "closed",
                "goods_type": "op",
                "act_items": [
                    {
                        "sku_code": "SKU-CZ-1",
                        "name": "Маркируемый товар",
                        "size": "42",
                        "actual_qty": 2,
                        "box_qty": 2,
                        "pallet_qty": 2,
                    }
                ],
                "act_boxes": [
                    {
                        "code": "BOX-CZ-DETAIL-TYPE-MOVE-0",
                        "goods_type": "op",
                        "items": [
                            {
                                "sku_code": "SKU-CZ-1",
                                "name": "Маркируемый товар",
                                "size": "42",
                                "qty": 1,
                                "goods_type": "op",
                            }
                        ],
                    },
                    {
                        "code": "BOX-CZ-DETAIL-TYPE-MOVE-1",
                        "goods_type": "br",
                        "items": [
                            {
                                "sku_code": "SKU-CZ-1",
                                "name": "Маркируемый товар",
                                "size": "42",
                                "qty": 1,
                                "goods_type": "br",
                            }
                        ],
                    },
                ],
                "act_pallets": [
                    {"code": "PAL-CZ-DETAIL-TYPE-MOVE-0", "boxes": ["BOX-CZ-DETAIL-TYPE-MOVE-0"], "items": []},
                    {"code": "PAL-CZ-DETAIL-TYPE-MOVE-1", "boxes": ["BOX-CZ-DETAIL-TYPE-MOVE-1"], "items": []},
                ],
            },
        )
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="receiving",
            action="status",
            agency=self.agency,
            user=self.user,
            description="Перемещение паллеты PAL-CZ-DETAIL-TYPE-MOVE-0",
            payload={
                "act": "placement",
                "act_state": "closed",
                "act_boxes": [
                    {
                        "code": "BOX-CZ-DETAIL-TYPE-MOVE-0",
                        "items": [
                            {
                                "sku_code": "SKU-CZ-1",
                                "name": "Маркируемый товар",
                                "size": "42",
                                "qty": 1,
                                "goods_type": "op",
                            }
                        ],
                    }
                ],
                "act_pallets": [{"code": "PAL-CZ-DETAIL-TYPE-MOVE-0", "boxes": ["BOX-CZ-DETAIL-TYPE-MOVE-0"], "items": []}],
            },
        )

        response = self.client.get(f"/orders/receiving/{order_id}/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["items"]), 2)
        self.assertEqual(response.context["items"][0]["actual_qty"], 1)
        self.assertEqual(response.context["items"][1]["actual_qty"], 1)
        self.assertEqual(response.context["items"][1]["name"], "Маркируемый товар (Брак)")
        self.assertContains(response, "Маркируемый товар (Брак)")

    def test_rebind_box_code_updates_receiving_marking_bindings(self):
        order_id = "R-CZ-REBIND"
        self._create_receiving_status(order_id, {"receiving_mode": "cz"})
        mark = MarkingCode.objects.create(
            order_type="receiving",
            order_id=order_id,
            agency=self.agency,
            sku=self.sku,
            sku_code=self.sku.sku_code,
            size=self.sku.size,
            barcode="20001",
            box_barcode="BOX-CZ-OLD-gv",
            code="CZ-REBIND-1",
            source="scan",
            created_by=self.user,
            used_at=timezone.localtime(),
            used_by=self.user,
        )

        response = self.client.post(
            f"/orders/receiving/{order_id}/flow/rebind-box-code/",
            data=json.dumps({
                "previous_code": "BOX-CZ-OLD-gv",
                "next_code": "BOX-CZ-OLD-br",
                "goods_type": "br",
            }),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertJSONEqual(response.content, {"ok": True, "updated": 1})
        mark.refresh_from_db()
        self.assertEqual(mark.box_barcode, "BOX-CZ-OLD-br")


class OrdersPlacementOperationalStockTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.user = user_model.objects.create_user(username="receiving_ops_stock", password="pwd")
        Employee.objects.create(user=self.user, role="storekeeper", full_name="Кладовщик OPS", is_active=True)
        self.client.force_login(self.user)
        self.agency = Agency.objects.create(agn_name="Клиент OPS")
        self.sku = SKU.objects.create(
            agency=self.agency,
            sku_code="SKU-OPS-1",
            name="Оперативный товар",
            size="42",
        )

    def _seed_receiving_order(self, order_id: str):
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="receiving",
            action="status",
            agency=self.agency,
            user=self.user,
            description="Статус",
            payload={
                "status": "warehouse",
                "status_label": "Размещение на складе",
                "goods_type": "op",
                "goods_type_label": "Оптовый",
                "items": [
                    {
                        "sku_code": self.sku.sku_code,
                        "name": self.sku.name,
                        "size": self.sku.size,
                        "qty": 5,
                    }
                ],
            },
        )
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="receiving",
            action="status",
            agency=self.agency,
            user=self.user,
            description="Акт приемки",
            payload={
                "act": "receiving",
                "act_label": "Акт приемки",
                "act_state": "closed",
                "goods_type": "op",
                "goods_type_label": "Оптовый",
                "act_items": [
                    {
                        "sku_code": self.sku.sku_code,
                        "name": self.sku.name,
                        "size": self.sku.size,
                        "actual_qty": 5,
                        "barcode": "OPS-BAR-1",
                    }
                ],
            },
        )

    def test_receiving_placement_close_and_open_update_operational_stock(self):
        order_id = "R-OPS-1"
        self._seed_receiving_order(order_id)

        close_response = self.client.post(
            f"/orders/receiving/{order_id}/placement/",
            {
                "boxes_json": (
                    '[{"code":"BOX-OPS-1","items":[{"sku":"SKU-OPS-1","name":"Оперативный товар",'
                    '"size":"42","barcode":"OPS-BAR-1","qty":5}],"sealed":true}]'
                ),
                "pallets_json": (
                    '[{"code":"PAL-OPS-1","boxes":["BOX-OPS-1"],"items":[],"sealed":true,'
                    '"location":{"zone":"OS","row":2,"section":1,"tier":1,"cell":3}}]'
                ),
            },
        )

        self.assertEqual(close_response.status_code, 302)
        self.assertIn("?ok=1", close_response.url)
        snapshot = WarehouseStockSnapshot.objects.get(
            agency=self.agency,
            source_context_type="receiving",
            source_context_id=order_id,
        )
        self.assertEqual(snapshot.sku_code, "SKU-OPS-1")
        self.assertEqual(snapshot.container_code, "BOX-OPS-1")
        self.assertEqual(snapshot.container.container_code, "BOX-OPS-1")
        self.assertEqual(snapshot.parent_container.container_code, "PAL-OPS-1")
        self.assertEqual(snapshot.zone_code, "PR")
        self.assertEqual(snapshot.warehouse_state_code, "placed_in_receiving")
        self.assertEqual(snapshot.available_qty, 5)

        open_response = self.client.post(
            f"/orders/receiving/{order_id}/placement/",
            {"action": "open"},
        )

        self.assertEqual(open_response.status_code, 302)
        self.assertFalse(
            WarehouseStockSnapshot.objects.filter(
                agency=self.agency,
                source_context_type="receiving",
                source_context_id=order_id,
            ).exists()
        )


class OrdersProcessingWarehouseDetailTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="orders_processing_detail", password="pwd")
        Employee.objects.create(
            full_name="Руководитель обработки деталки",
            role="processing_head",
            user=self.user,
            is_active=True,
        )
        self.client.login(username="orders_processing_detail", password="pwd")
        self.agency = Agency.objects.create(agn_name="Клиент обработки деталки")

    def test_processing_detail_prefers_warehouse_status_and_responsible(self):
        order_id = "P-ORD-1"
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="processing",
            action="status",
            agency=self.agency,
            payload={
                "status": "processing_head",
                "status_label": "Передано в обработку",
                "article": "SKU-DETAIL",
                "product_name": "Товар деталки",
            },
        )
        location = WarehouseWritePathService.ensure_location(
            warehouse_code="MSK",
            zone_code="OBR",
        )
        snapshot = WarehouseStockSnapshot.objects.create(
            agency=self.agency,
            source_context_type="legacy_stock",
            source_context_id=f"legacy-{order_id}",
            sku_code="SKU-DETAIL",
            name="Товар деталки",
            size="42",
            barcode="200000009001",
            goods_type="gv",
            qty=8,
            available_qty=0,
            processing_reserved_qty=8,
            container_code="PAL-DETAIL-1",
            location=location,
            zone_code=location.zone_code,
            zone_kind=location.zone_kind,
            warehouse_state_code="processing_in_progress",
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
            qty_reserved=8,
            status=WarehouseReserve.STATUS_ACTIVE,
        )

        response = self.client.get(f"/orders/processing/{order_id}/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["status_label"], "утверждено менеджером")
        self.assertIn("Руководитель обработки", response.context["responsible"])
        self.assertFalse(response.context["can_take_processing"])
        self.assertEqual(
            response.context["next_step_label"],
            "Завершить обработку и подготовить размещение",
        )

    def test_processing_status_helper_prefers_warehouse_state(self):
        agency = Agency.objects.create(agn_name="Helper Processing Agency")
        location = WarehouseWritePathService.ensure_location(warehouse_code="MSK", zone_code="OBR")
        snapshot = WarehouseStockSnapshot.objects.create(
            agency=agency,
            source_context_type="legacy_stock",
            source_context_id="legacy-P-H-1",
            sku_code="SKU-H-1",
            name="Helper Processing",
            size="42",
            barcode="200000009101",
            goods_type="gv",
            qty=3,
            available_qty=0,
            processing_reserved_qty=3,
            container_code="PAL-H-1",
            location=location,
            zone_code=location.zone_code,
            zone_kind=location.zone_kind,
            warehouse_state_code="processing_in_progress",
        )
        WarehouseReserve.objects.create(
            agency=agency,
            reserve_type=WarehouseReserve.TYPE_PROCESSING,
            context_type="processing",
            context_id="P-H-1",
            sku_code=snapshot.sku_code,
            size=snapshot.size,
            barcode=snapshot.barcode,
            goods_type=snapshot.goods_type,
            qty_reserved=3,
            status=WarehouseReserve.STATUS_ACTIVE,
        )
        entry = SimpleNamespace(
            payload={"status": "processing_head", "status_label": "Передано в обработку"},
            order_type="processing",
            order_id="P-H-1",
            agency=agency,
        )

        self.assertEqual(_status_label_from_entry(entry), "утверждено менеджером")

    def test_processing_history_actor_helper_prefers_warehouse_state(self):
        agency = Agency.objects.create(agn_name="History Processing Agency")
        location = WarehouseWritePathService.ensure_location(warehouse_code="MSK", zone_code="OBR")
        snapshot = WarehouseStockSnapshot.objects.create(
            agency=agency,
            source_context_type="legacy_stock",
            source_context_id="legacy-P-H-2",
            sku_code="SKU-H-2",
            name="History Processing",
            size="42",
            barcode="200000009102",
            goods_type="gv",
            qty=4,
            available_qty=0,
            processing_reserved_qty=4,
            container_code="PAL-H-2",
            location=location,
            zone_code=location.zone_code,
            zone_kind=location.zone_kind,
            warehouse_state_code="processing_in_progress",
        )
        WarehouseReserve.objects.create(
            agency=agency,
            reserve_type=WarehouseReserve.TYPE_PROCESSING,
            context_type="processing",
            context_id="P-H-2",
            sku_code=snapshot.sku_code,
            size=snapshot.size,
            barcode=snapshot.barcode,
            goods_type=snapshot.goods_type,
            qty_reserved=4,
            status=WarehouseReserve.STATUS_ACTIVE,
        )
        Employee.objects.create(full_name="Главный обработчик", role="processing_head", is_active=True)
        entry = SimpleNamespace(
            payload={"status": "processing_head", "status_label": "Передано в обработку"},
            order_type="processing",
            order_id="P-H-2",
            agency=agency,
            action="status",
            user=None,
        )

        self.assertIn("Руководитель обработки", _history_actor_label(entry))

    def test_processing_placement_page_prefers_warehouse_status(self):
        order_id = "P-PLACEMENT-WH-1"
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="processing",
            action="status",
            agency=self.agency,
            payload={
                "status": "processing_head",
                "status_label": "Передано в обработку",
                "article": "SKU-PLACEMENT",
                "product_name": "Товар размещения",
            },
        )
        location = WarehouseWritePathService.ensure_location(warehouse_code="MSK", zone_code="OBR")
        snapshot = WarehouseStockSnapshot.objects.create(
            agency=self.agency,
            source_context_type="legacy_stock",
            source_context_id=f"legacy-{order_id}",
            sku_code="SKU-PLACEMENT",
            name="Товар размещения",
            size="42",
            barcode="200000009103",
            goods_type="gv",
            qty=4,
            available_qty=0,
            processing_reserved_qty=4,
            container_code="PAL-P-PL1",
            location=location,
            zone_code=location.zone_code,
            zone_kind=location.zone_kind,
            warehouse_state_code="processing_in_progress",
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
            qty_reserved=4,
            status=WarehouseReserve.STATUS_ACTIVE,
        )

        response = self.client.get(f"/orders/processing/{order_id}/placement/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["status_label"], "утверждено менеджером")

    def test_processing_placement_page_shows_ready_goods_type_from_latest_payload(self):
        order_id = "P-PLACEMENT-GT-1"
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="processing",
            action="status",
            agency=self.agency,
            payload={
                "status": "processing_head",
                "status_label": "Передано в обработку",
                "article": "SKU-PLACEMENT-GT",
                "product_name": "Товар размещения GT",
            },
        )
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="processing",
            action="update",
            agency=self.agency,
            payload={
                "act": "placement",
                "act_label": "Акт размещения",
                "act_state": "open",
                "goods_type": "gv",
                "goods_type_label": "Готовый",
            },
        )

        response = self.client.get(f"/orders/processing/{order_id}/placement/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["goods_type"], "gv")
        self.assertEqual(response.context["goods_type_label"], "Готовый")
        self.assertContains(response, "Тип поставки: Готовый")

    def test_processing_placement_post_is_rejected_after_storage(self):
        order_id = "P-PLACEMENT-POST-STORED-1"
        storekeeper_user = get_user_model().objects.create_user(
            username="processing_placement_storekeeper",
            password="pwd",
        )
        Employee.objects.create(
            full_name="Кладовщик обработки",
            role="storekeeper",
            user=storekeeper_user,
            is_active=True,
        )
        self.client.login(username="processing_placement_storekeeper", password="pwd")
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="processing",
            action="status",
            agency=self.agency,
            user=self.user,
            payload={
                "status": "done",
                "status_label": "Выполнена",
                "cards": [{"id": "card-a", "article": "SKU-PLACEMENT-POST", "rows": [{"size": "42", "qty": "2"}]}],
                "processed_cards": ["card-a"],
                "placed_cards": ["card-a"],
                "processing_results": [
                    {
                        "card_id": "card-a",
                        "article": "SKU-PLACEMENT-POST",
                        "size": "42",
                        "destination": "-",
                        "processed": "2",
                    }
                ],
            },
        )
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="processing",
            action="update",
            agency=self.agency,
            user=self.user,
            payload={
                "act": "receiving",
                "act_label": "Акт приемки (обработка)",
                "act_state": "closed",
                "act_items": [
                    {
                        "sku_code": "SKU-PLACEMENT-POST",
                        "name": "Товар размещения post",
                        "size": "42",
                        "actual_qty": 2,
                    }
                ],
            },
        )
        location = WarehouseWritePathService.ensure_location(warehouse_code="MSK", zone_code="OS")
        WarehouseStockSnapshot.objects.create(
            agency=self.agency,
            source_context_type="legacy_stock",
            source_context_id=f"legacy-{order_id}",
            sku_code="SKU-PLACEMENT-POST",
            name="Товар размещения post",
            size="42",
            barcode="200000009215",
            goods_type="gv",
            qty=2,
            available_qty=2,
            processing_reserved_qty=0,
            container_code="PAL-P-PST1",
            location=location,
            zone_code=location.zone_code,
            zone_kind=location.zone_kind,
            warehouse_state_code="stored",
        )
        WarehouseReserve.objects.create(
            agency=self.agency,
            reserve_type=WarehouseReserve.TYPE_PROCESSING,
            context_type="processing",
            context_id=order_id,
            sku_code="SKU-PLACEMENT-POST",
            size="42",
            barcode="200000009215",
            goods_type="gv",
            qty_reserved=2,
            status=WarehouseReserve.STATUS_ACTIVE,
        )

        response = self.client.post(
            f"/orders/processing/{order_id}/placement/",
            {
                "boxes_json": (
                    '[{"code":"BOX-P-PST-1","items":[{"sku":"SKU-PLACEMENT-POST","name":"Товар размещения post",'
                    '"size":"42","qty":2}],"sealed":true}]'
                ),
                "pallets_json": (
                    '[{"code":"PAL-P-PST1","boxes":["BOX-P-PST-1"],"items":[],"sealed":true,'
                    '"location":{"zone":"OS","row":1,"section":1,"tier":1,"cell":1}}]'
                ),
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, f"/orders/processing/{order_id}/placement/?error=1")


class OrdersReceivingWorkflowServiceTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.user = user_model.objects.create_user(username="receiving_workflow_actor", password="pwd")
        self.manager_user = user_model.objects.create_user(username="receiving_workflow_manager", password="pwd")
        self.storekeeper_user = user_model.objects.create_user(
            username="receiving_workflow_storekeeper",
            password="pwd",
        )
        Employee.objects.create(
            full_name="Исполнитель приемки",
            role="storekeeper",
            user=self.user,
            is_active=True,
        )
        self.manager = Employee.objects.create(
            full_name="Менеджер приемки",
            role="manager",
            user=self.manager_user,
            is_active=True,
        )
        self.storekeeper = Employee.objects.create(
            full_name="Кладовщик приемки",
            role="storekeeper",
            user=self.storekeeper_user,
            is_active=True,
        )
        self.agency = Agency.objects.create(
            agn_name="Клиент workflow",
            mened_user_id=self.manager_user.id,
        )
        self.sku = SKU.objects.create(
            agency=self.agency,
            sku_code="SKU-WF-1",
            name="Товар workflow",
            size="42",
        )

    def _create_receiving_service_fact(self, order_id, *, quantity=1):
        service, _ = BillingService.objects.get_or_create(
            code="receiving_goods",
            defaults={"name": "Приемка товара", "unit": "шт"},
        )
        return WarehouseServiceFact.objects.create(
            client=self.agency,
            order_type=WarehouseServiceFact.ORDER_RECEIVING,
            order_id=order_id,
            service=service,
            service_name_snapshot=service.name,
            planned_quantity=quantity,
            quantity=quantity,
            unit=service.unit,
            status=WarehouseServiceFact.STATUS_SENT_TO_BILLING,
            source="warehouse_manual",
            reported_by=self.user,
            performed_at=timezone.now(),
            source_key=f"warehouse_fact:receiving:{order_id}:{service.code}",
        )

    def test_receiving_template_accepts_sku_and_qty_without_barcode(self):
        import pandas as pd

        SKUBarcode.objects.create(
            sku=self.sku,
            value="WF-BAR-PRIMARY",
            size="42",
            is_primary=True,
        )
        header_map = {"sku": 0, "кол-во": 1}
        rows = pd.DataFrame([["SKU-WF-1", "5"]])

        self.assertEqual(_template_type_for_header(header_map), "receiving")
        items, errors = _parse_receiving_template(rows, header_map, self.agency)

        self.assertEqual(errors, [])
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["sku_code"], "SKU-WF-1")
        self.assertEqual(items[0]["qty"], "5")

        ReceivingWorkflowService._attach_receiving_nomenclature_metadata(
            agency=self.agency,
            items=items,
            goods_type="",
            order_id="",
            order_type="receiving",
        )
        self.assertEqual(items[0]["barcode"], "WF-BAR-PRIMARY")

    def test_receiving_placeholder_barcode_uses_existing_primary(self):
        SKUBarcode.objects.create(
            sku=self.sku,
            value="WF-BAR-PLACEHOLDER",
            size="42",
            is_primary=True,
        )
        items = [
            {
                "sku_id": self.sku.id,
                "sku_code": self.sku.sku_code,
                "name": self.sku.name,
                "size": "42",
                "barcode": "-",
            }
        ]

        ReceivingWorkflowService._attach_receiving_nomenclature_metadata(
            agency=self.agency,
            items=items,
            goods_type="gv",
            order_id="R-PLACEHOLDER-BARCODE",
            order_type="receiving",
            validate_only=True,
        )

        self.assertEqual(items[0]["barcode"], "WF-BAR-PLACEHOLDER")
        self.assertEqual(items[0]["sku_id"], self.sku.id)

    def test_receiving_flow_reconciles_placeholder_barcode_without_mutating_source(self):
        SKUBarcode.objects.create(
            sku=self.sku,
            value="WF-BAR-RECONCILED",
            size="42",
            is_primary=True,
        )
        source_state = {
            "boxes": [
                {
                    "code": "BOX-PLACEHOLDER",
                    "sealed": True,
                    "items": [
                        {
                            "sku_code": self.sku.sku_code,
                            "sku": self.sku.sku_code,
                            "name": self.sku.name,
                            "size": "42",
                            "barcode": "—",
                            "qty": 1,
                            "goods_type": "gv",
                        }
                    ],
                }
            ],
            "pallets": [],
        }

        reconciled, changed = ReceivingWorkflowService._reconcile_receiving_flow_nomenclature(
            agency=self.agency,
            flow_state=source_state,
            goods_type="gv",
        )

        self.assertTrue(changed)
        self.assertEqual(source_state["boxes"][0]["items"][0]["barcode"], "—")
        item = reconciled["boxes"][0]["items"][0]
        self.assertEqual(item["barcode"], "WF-BAR-RECONCILED")
        self.assertEqual(item["sku_id"], self.sku.id)

    def test_receiving_does_not_create_unknown_sku_or_barcode(self):
        items = [
            {
                "sku_code": "SKU-UNKNOWN",
                "name": "Новый товар",
                "brand": "Brand",
                "color": "Black",
                "size": "42",
                "barcode": "",
            }
        ]

        with self.assertRaisesRegex(ReceivingNomenclatureError, "не найден в номенклатуре"):
            ReceivingWorkflowService._attach_receiving_nomenclature_metadata(
                agency=self.agency,
                items=items,
                goods_type="gv",
                order_id="R-UNKNOWN-SKU",
                order_type="receiving",
            )

        self.assertFalse(SKU.objects.filter(agency=self.agency, sku_code="SKU-UNKNOWN").exists())
        self.assertFalse(SKUBarcode.objects.filter(value__startswith="29").exists())

    def test_receiving_does_not_generate_barcode_for_existing_sku(self):
        items = [
            {
                "sku_id": self.sku.id,
                "sku_code": self.sku.sku_code,
                "name": self.sku.name,
                "size": self.sku.size,
                "barcode": "",
            }
        ]

        with self.assertRaisesRegex(ReceivingNomenclatureError, "не найден заранее созданный ШК"):
            ReceivingWorkflowService._attach_receiving_nomenclature_metadata(
                agency=self.agency,
                items=items,
                goods_type="gv",
                order_id="R-MISSING-BARCODE",
                order_type="receiving",
            )

        self.assertFalse(self.sku.barcodes.exists())

    def test_receiving_does_not_add_unknown_requested_barcode(self):
        existing = SKUBarcode.objects.create(
            sku=self.sku,
            value="WF-BAR-42",
            size="42",
            is_primary=True,
        )
        items = [
            {
                "sku_id": self.sku.id,
                "sku_code": self.sku.sku_code,
                "name": self.sku.name,
                "size": "42",
                "barcode": "WF-BAR-NEW",
            }
        ]

        with self.assertRaisesRegex(ReceivingNomenclatureError, "ШК WF-BAR-NEW не найден"):
            ReceivingWorkflowService._attach_receiving_nomenclature_metadata(
                agency=self.agency,
                items=items,
                goods_type="gv",
                order_id="R-UNKNOWN-BARCODE",
                order_type="receiving",
            )

        self.assertEqual(list(self.sku.barcodes.values_list("value", flat=True)), [existing.value])

    def test_receiving_does_not_generate_barcode_for_new_size(self):
        existing = SKUBarcode.objects.create(
            sku=self.sku,
            value="WF-BAR-42",
            size="42",
            is_primary=True,
        )
        items = [
            {
                "sku_id": self.sku.id,
                "sku_code": self.sku.sku_code,
                "name": self.sku.name,
                "size": "43",
                "barcode": "",
            }
        ]

        with self.assertRaisesRegex(ReceivingNomenclatureError, "для размера 43"):
            ReceivingWorkflowService._attach_receiving_nomenclature_metadata(
                agency=self.agency,
                items=items,
                goods_type="gv",
                order_id="R-NEW-SIZE",
                order_type="receiving",
            )

        self.assertEqual(list(self.sku.barcodes.values_list("value", flat=True)), [existing.value])

    def test_nomenclature_uses_barcode_owner_for_case_only_duplicate_sku(self):
        barcode_owner = SKU.objects.create(
            agency=self.agency,
            sku_code="нож007",
            name="Нож складной туристический",
            size="0",
        )
        case_duplicate = SKU.objects.create(
            agency=self.agency,
            sku_code="Нож007",
            name="Нож складной туристический",
            size="0",
        )
        SKUBarcode.objects.create(
            sku=barcode_owner,
            value="2958339941321",
            size="0",
            is_primary=True,
        )
        items = [
            {
                "sku_id": case_duplicate.id,
                "sku_code": "нож007",
                "sku": "нож007",
                "name": "Нож складной туристический",
                "size": "0",
                "barcode": "2958339941321",
            }
        ]

        ReceivingWorkflowService._attach_receiving_nomenclature_metadata(
            agency=self.agency,
            items=items,
            goods_type="gv",
            order_id="R-CASE-DUPLICATE",
            order_type="receiving",
        )

        self.assertEqual(items[0]["sku_id"], barcode_owner.id)
        self.assertEqual(items[0]["sku_code"], "нож007")
        self.assertFalse(case_duplicate.barcodes.exists())

    def test_nomenclature_keeps_barcode_block_for_different_sku(self):
        barcode_owner = SKU.objects.create(
            agency=self.agency,
            sku_code="SKU-BARCODE-OWNER",
            name="Владелец ШК",
            size="0",
        )
        another_sku = SKU.objects.create(
            agency=self.agency,
            sku_code="SKU-ANOTHER",
            name="Другой товар",
            size="0",
        )
        SKUBarcode.objects.create(
            sku=barcode_owner,
            value="BARCODE-OWNER-1",
            size="0",
            is_primary=True,
        )

        with self.assertRaisesRegex(ReceivingNomenclatureError, "SKU-BARCODE-OWNER"):
            ReceivingWorkflowService._attach_receiving_nomenclature_metadata(
                agency=self.agency,
                items=[
                    {
                        "sku_id": another_sku.id,
                        "sku_code": another_sku.sku_code,
                        "name": another_sku.name,
                        "size": "0",
                        "barcode": "BARCODE-OWNER-1",
                    }
                ],
                goods_type="gv",
                order_id="R-DIFFERENT-SKU",
                order_type="receiving",
            )

    def _create_receiving_snapshot(self, order_id: str, state_code: str = "placed_in_receiving"):
        location = WarehouseWritePathService.ensure_location(
            warehouse_code="MSK",
            zone_code="PR" if state_code != "stored" else "OS",
        )
        return WarehouseStockSnapshot.objects.create(
            agency=self.agency,
            source_context_type="receiving",
            source_context_id=order_id,
            sku_code=self.sku.sku_code,
            name=self.sku.name,
            size=self.sku.size,
            barcode=f"WF-{order_id}",
            goods_type="op",
            qty=2,
            available_qty=2,
            container_code=f"PAL-{order_id}",
            location=location,
            zone_code=location.zone_code,
            zone_kind=location.zone_kind,
            warehouse_state_code=state_code,
        )

    def test_submit_receiving_for_review_creates_manager_task(self):
        order_id = "R-WF-SUBMIT"

        result = ReceivingWorkflowService.submit_receiving_for_review(
            order_id=order_id,
            agency=self.agency,
            user=self.user,
            submitted_at=timezone.make_aware(datetime(2026, 4, 22, 11, 0)),
        )

        self.assertTrue(result.manager_task_created)
        task = Task.objects.get(
            route=f"/orders/receiving/{order_id}/",
            assigned_to=self.manager,
        )
        self.assertIn("Подтвердите заявку на приемку товара", task.title)
        self.assertEqual(task.created_by_id, self.user.id)

    def test_client_receiving_submit_persists_document_attachments(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        from orders.models import ReceivingOrderAttachment

        client_user = get_user_model().objects.create_user(username="rcv_attach_client", password="pwd")
        agency = Agency.objects.create(agn_name="Клиент вложений приёмки", portal_user=client_user)
        sku = SKU.objects.create(agency=agency, sku_code="RCV-ATT-1", name="Товар вложения")
        client = Client()
        client.force_login(client_user)
        upload = SimpleUploadedFile("invoice-rcv.pdf", b"%PDF-1.4 receiving attach", content_type="application/pdf")
        eta = (timezone.localtime() + timedelta(days=1)).replace(minute=0, second=0, microsecond=0)
        response = client.post(
            f"/orders/receiving/?client={agency.id}",
            data={
                "submit_action": "send",
                "eta_at": eta.strftime("%Y-%m-%dT%H:%M"),
                "expected_boxes": "1",
                "place_type": "pallet",
                "receiving_chz_service": "required",
                "vehicle_number": "A111AA77",
                "driver_phone": "+7 900 111-11-11",
                "comment": "с файлом",
                "sku_code[]": [sku.sku_code],
                "sku_id[]": [str(sku.id)],
                "item_name[]": [sku.name],
                "brand[]": [""],
                "color[]": [""],
                "size[]": ["42"],
                "barcode[]": [""],
                "qty[]": ["2"],
                "position_comment[]": [""],
                "documents": upload,
            },
        )
        self.assertEqual(response.status_code, 302, response.content[:500])
        latest = OrderAuditEntry.objects.filter(order_type="receiving", agency=agency).order_by("-id").first()
        self.assertIsNotNone(latest)
        attachment = ReceivingOrderAttachment.objects.get(order_id=latest.order_id, agency=agency)
        self.assertTrue(attachment.filename.startswith("invoice-rcv"))
        self.assertTrue(attachment.filename.endswith(".pdf"))
        self.assertTrue(attachment.file)
        self.assertTrue(any(str(name).endswith(".pdf") for name in (latest.payload.get("documents") or [])))
        self.client.force_login(self.user)
        detail = self.client.get(f"/orders/receiving/{latest.order_id}/")
        self.assertEqual(detail.status_code, 200)
        self.assertContains(detail, ".pdf")
        download = self.client.get(
            f"/orders/receiving/{latest.order_id}/attachments/{attachment.pk}/"
        )
        self.assertEqual(download.status_code, 200)

    def test_submit_receiving_order_creates_entry_and_dispatches_review(self):
        order_id = "R-WF-CREATE"

        result = ReceivingWorkflowService.submit_receiving_order(
            order_id=order_id,
            agency=self.agency,
            payload={
                "status": "sent_unconfirmed",
                "status_label": "Ждет подтверждения",
                "submit_action": "send",
                "receiving_chz_service": "required",
                "eta_at": "2026-04-22T10:00:00+03:00",
                "items": [{"sku_code": self.sku.sku_code, "qty": 2}],
            },
            submit_action="send",
            user=self.user,
            submitted_at=timezone.make_aware(datetime(2026, 4, 22, 11, 0)),
            dispatch_review=True,
        )

        self.assertEqual(result.order_id, order_id)
        self.assertFalse(result.was_update)
        self.assertTrue(result.review_dispatched)
        entry = OrderAuditEntry.objects.get(order_id=order_id, order_type="receiving")
        self.assertEqual(entry.description, f"Заявка на приемку №{order_id} (заявка)")
        self.assertEqual(entry.payload.get("status"), "sent_unconfirmed")
        manager_task = Task.objects.get(
            route=f"/orders/receiving/{order_id}/",
            assigned_to=self.manager,
        )
        self.assertIn("Подтвердите заявку", manager_task.title)

    def test_submit_receiving_order_rejects_missing_chz_choice_without_audit_write(self):
        order_id = "R-WF-MISSING-CHZ-CHOICE"

        with self.assertRaisesRegex(
            ValueError,
            "Укажите, требуется ли сканирование Честного знака",
        ):
            ReceivingWorkflowService.submit_receiving_order(
                order_id=order_id,
                agency=self.agency,
                payload={
                    "status": "sent_unconfirmed",
                    "status_label": "Ждет подтверждения",
                    "submit_action": "send",
                    "items": [],
                },
                submit_action="send",
                user=self.user,
            )

        self.assertFalse(
            OrderAuditEntry.objects.filter(
                order_id=order_id,
                order_type="receiving",
            ).exists()
        )

    def test_submit_receiving_order_updates_entry_and_dispatches_review(self):
        order_id = "R-WF-EDIT"
        old_payload = {
            "status": "draft",
            "status_label": "Черновик",
            "eta_at": "2026-04-21T10:00:00+03:00",
            "items": [{"sku_code": self.sku.sku_code, "qty": 1}],
        }

        result = ReceivingWorkflowService.submit_receiving_order(
            order_id=order_id,
            agency=self.agency,
            payload={
                "status": "sent_unconfirmed",
                "status_label": "Ждет подтверждения",
                "submit_action": "send",
                "receiving_chz_service": "not_required",
                "eta_at": "2026-04-22T12:00:00+03:00",
                "items": [{"sku_code": self.sku.sku_code, "qty": 2}],
            },
            submit_action="send",
            user=self.user,
            submitted_at=timezone.make_aware(datetime(2026, 4, 22, 12, 0)),
            existing_order_id=order_id,
            old_payload=old_payload,
            dispatch_review=True,
        )

        self.assertTrue(result.was_update)
        self.assertTrue(result.review_dispatched)
        entry = OrderAuditEntry.objects.get(order_id=order_id, order_type="receiving")
        self.assertIn("Отправлено менеджеру.", entry.description)
        self.assertIn("Состав поставки: 1 → 1 поз.", entry.description)
        manager_task = Task.objects.get(
            route=f"/orders/receiving/{order_id}/",
            assigned_to=self.manager,
        )
        self.assertIn("Подтвердите заявку", manager_task.title)

    def test_configure_receiving_act_updates_goods_type_and_mode(self):
        order_id = "R-WF-ACTCFG"
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="receiving",
            action="status",
            agency=self.agency,
            user=self.user,
            description="Статус приемки",
            payload={
                "status": "warehouse",
                "status_label": "В ожидании поставки товара",
                "items": [{"sku_code": self.sku.sku_code, "qty": 1}],
            },
        )

        result = ReceivingWorkflowService.configure_receiving_act(
            order_id=order_id,
            entries=list(OrderAuditEntry.objects.filter(order_id=order_id, order_type="receiving").order_by("created_at")),
            role="storekeeper",
            goods_type="op",
            receiving_mode="cz",
            user=self.user,
        )

        self.assertTrue(result.applied)
        self.assertEqual(result.payload.get("goods_type"), "op")
        self.assertEqual(result.payload.get("goods_type_label"), "Оптовый")
        self.assertEqual(result.payload.get("receiving_mode"), "cz")
        entry = OrderAuditEntry.objects.filter(order_id=order_id, order_type="receiving").latest("id")
        self.assertEqual(entry.description, "Тип товара: Оптовый")
        self.assertEqual(entry.payload.get("receiving_mode"), "cz")

    def test_configure_receiving_act_allows_optional_client_chz(self):
        order_id = "R-WF-ACTCFG-CLIENT-OPTIONAL"
        SKUBarcode.objects.create(
            sku=self.sku,
            agency=self.agency,
            value="WF-BAR-OPTIONAL-CHZ",
            size="42",
            is_primary=True,
        )
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="receiving",
            action="status",
            agency=self.agency,
            user=self.user,
            description="Статус приемки",
            payload={
                "status": "warehouse",
                "status_label": "В ожидании поставки товара",
                "receiving_chz_service": "not_required",
                "items": [{"sku_code": self.sku.sku_code, "qty": 1}],
            },
        )
        audit_count = OrderAuditEntry.objects.filter(
            order_id=order_id,
            order_type="receiving",
        ).count()

        result = ReceivingWorkflowService.configure_receiving_act(
            order_id=order_id,
            entries=list(
                OrderAuditEntry.objects.filter(
                    order_id=order_id,
                    order_type="receiving",
                ).order_by("created_at")
            ),
            role="storekeeper",
            goods_type="op",
            receiving_mode="cz",
            user=self.user,
        )

        self.assertTrue(result.applied)
        self.assertEqual(result.payload.get("receiving_mode"), "cz")
        self.assertGreater(
            OrderAuditEntry.objects.filter(
                order_id=order_id,
                order_type="receiving",
            ).count(),
            audit_count,
        )

    def test_configure_receiving_act_creates_primary_nomenclature_for_new_item(self):
        order_id = "R-WF-ACTCFG-SKU"
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="receiving",
            action="status",
            agency=self.agency,
            user=self.user,
            description="Статус приемки",
            payload={
                "status": "warehouse",
                "status_label": "В ожидании поставки товара",
                "items": [
                    {
                        "sku_code": "028",
                        "name": "Джинсы синие",
                        "brand": "Levis",
                        "color": "Синий",
                        "size": "27-35",
                        "qty": 250,
                    }
                ],
            },
        )

        result = ReceivingWorkflowService.configure_receiving_act(
            order_id=order_id,
            entries=list(OrderAuditEntry.objects.filter(order_id=order_id, order_type="receiving").order_by("created_at")),
            role="storekeeper",
            goods_type="op",
            receiving_mode="standard",
            user=self.user,
        )

        self.assertTrue(result.applied)
        created_sku = SKU.objects.get(agency=self.agency, sku_code="028", deleted=False)
        created_barcode = SKUBarcode.objects.get(sku=created_sku, size="27-35")
        self.assertEqual(created_sku.name, "Джинсы синие")
        self.assertEqual(created_sku.brand, "Levis")
        self.assertEqual(created_sku.color, "Синий")
        payload_item = (result.payload.get("items") or [])[0]
        self.assertEqual(payload_item.get("nomenclature_kind"), "sku")
        self.assertEqual(payload_item.get("sku_id"), created_sku.id)
        self.assertEqual(payload_item.get("barcode"), created_barcode.value)

    def test_configure_receiving_act_adds_new_size_to_existing_client_article(self):
        order_id = "R-WF-ACTCFG-SIZE"
        SKUBarcode.objects.create(sku=self.sku, value="WF-BAR-42", size="42", is_primary=True)
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="receiving",
            action="status",
            agency=self.agency,
            user=self.user,
            description="Статус приемки",
            payload={
                "status": "warehouse",
                "status_label": "В ожидании поставки товара",
                "items": [
                    {
                        "sku_code": self.sku.sku_code,
                        "name": self.sku.name,
                        "brand": "Brand existing",
                        "color": "Черный",
                        "size": "44",
                        "qty": 5,
                    }
                ],
            },
        )

        result = ReceivingWorkflowService.configure_receiving_act(
            order_id=order_id,
            entries=list(OrderAuditEntry.objects.filter(order_id=order_id, order_type="receiving").order_by("created_at")),
            role="storekeeper",
            goods_type="gv",
            receiving_mode="standard",
            user=self.user,
        )

        self.assertTrue(result.applied)
        self.assertEqual(SKU.objects.filter(agency=self.agency, sku_code=self.sku.sku_code, deleted=False).count(), 1)
        size_barcode = SKUBarcode.objects.get(sku=self.sku, size="44")
        payload_item = (result.payload.get("items") or [])[0]
        self.assertEqual(payload_item.get("sku_id"), self.sku.id)
        self.assertEqual(payload_item.get("barcode"), size_barcode.value)

    def test_start_receiving_work_logs_status_with_storekeeper_data(self):
        order_id = "R-WF-START"
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="receiving",
            action="status",
            agency=self.agency,
            user=self.user,
            description="Статус приемки",
            payload={
                "status": "warehouse",
                "status_label": "В ожидании поставки товара",
                "items": [{"sku_code": self.sku.sku_code, "qty": 1}],
            },
        )

        result = ReceivingWorkflowService.start_receiving_work(
            order_id=order_id,
            entries=list(OrderAuditEntry.objects.filter(order_id=order_id, order_type="receiving").order_by("created_at")),
            role="storekeeper",
            user=self.user,
        )

        self.assertEqual(result.status, "started")
        self.assertEqual(result.payload.get("status_label"), "Взята в работу")
        self.assertTrue(result.payload.get("storekeeper_employee_id"))
        entry = OrderAuditEntry.objects.filter(order_id=order_id, order_type="receiving").latest("id")
        self.assertEqual(entry.description, "Заявка взята в работу кладовщиком")
        self.assertEqual(entry.payload.get("status_label"), "Взята в работу")

    def test_reopen_receiving_flow_logs_overaction_and_status(self):
        order_id = "R-WF-REOPEN"
        self._create_receiving_snapshot(order_id, "placed_in_receiving")
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="receiving",
            action="status",
            agency=self.agency,
            user=self.user,
            description="Статус приемки",
            payload={
                "status": "warehouse",
                "status_label": "Размещение на складе",
                "flow_closed": True,
                "flow_closed_at": "2026-04-22T10:00:00+03:00",
            },
        )

        result = ReceivingWorkflowService.reopen_receiving_flow(
            order_id=order_id,
            entries=list(OrderAuditEntry.objects.filter(order_id=order_id, order_type="receiving").order_by("created_at")),
            role="storekeeper",
            user=self.user,
        )

        self.assertEqual(result.status, "reopened")
        self.assertTrue(result.payload.get("flow_reopened"))
        entry = OrderAuditEntry.objects.filter(order_id=order_id, order_type="receiving").latest("id")
        self.assertEqual(entry.description, "Повторное открытие приемки потоком")
        overaction = AuditEntry.objects.filter(journal__code="staff_overactions").latest("id")
        self.assertIn("повторное открытие приемки потоком", overaction.description.lower())

    def test_open_receiving_placement_logs_open_status(self):
        order_id = "R-WF-OPEN-PL"
        self._create_receiving_snapshot(order_id, "placed_in_receiving")
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="receiving",
            action="status",
            agency=self.agency,
            user=self.user,
            description="Статус приемки",
            payload={
                "status": "warehouse",
                "status_label": "Размещение на складе",
            },
        )
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="receiving",
            action="update",
            agency=self.agency,
            user=self.user,
            description="Акт размещения",
            payload={
                "act": "placement",
                "act_label": "Акт размещения",
                "act_state": "closed",
            },
        )

        result = ReceivingWorkflowService.open_receiving_placement(
            order_id=order_id,
            entries=list(OrderAuditEntry.objects.filter(order_id=order_id, order_type="receiving").order_by("created_at")),
            role="storekeeper",
            user=self.user,
        )

        self.assertEqual(result.status, "opened")
        self.assertEqual(result.payload.get("act_state"), "open")
        entry = OrderAuditEntry.objects.filter(order_id=order_id, order_type="receiving").latest("id")
        self.assertEqual(entry.description, "Открыт акт размещения")
        self.assertEqual(entry.payload.get("act_state"), "open")

    def test_save_receiving_flow_draft_updates_existing_entry(self):
        order_id = "R-WF-DRAFT"
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="receiving",
            action="status",
            agency=self.agency,
            user=self.user,
            description="Статус приемки",
            payload={
                "status": "warehouse",
                "status_label": "В ожидании поставки товара",
                "items": [{"sku_code": self.sku.sku_code, "qty": 1}],
            },
        )
        draft_entry = OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="receiving",
            action="update",
            agency=self.agency,
            user=self.user,
            description="Старый черновик",
            payload={"flow_state": {"boxes": [], "pallets": []}},
        )

        result = ReceivingWorkflowService.save_receiving_flow_draft(
            order_id=order_id,
            entries=list(OrderAuditEntry.objects.filter(order_id=order_id, order_type="receiving").order_by("created_at")),
            role="storekeeper",
            boxes_raw=(
                '[{"code":"BOX-WF-DRAFT-1","items":[{"sku_code":"SKU-WF-1",'
                '"name":"Товар workflow","size":"42","qty":1}],"sealed":false}]'
            ),
            pallets_raw=(
                '[{"code":"PAL-WF-DRAFT-1","boxes":["BOX-WF-DRAFT-1"],'
                '"items":[],"sealed":false,"location":{"zone":"PR"}}]'
            ),
            active_box="BOX-WF-DRAFT-1",
            active_pallet="PAL-WF-DRAFT-1",
            user=self.user,
        )

        self.assertEqual(result.status, "saved")
        self.assertTrue(result.meta.get("updated_existing"))
        draft_entry.refresh_from_db()
        self.assertEqual(draft_entry.description, "Черновик приемки потоком")
        self.assertEqual(draft_entry.payload["flow_state"]["activeBox"], "BOX-WF-DRAFT-1")
        self.assertEqual(draft_entry.payload["flow_state"]["activePallet"], "PAL-WF-DRAFT-1")
        self.assertEqual(draft_entry.payload["flow_state"]["boxes"][0]["code"], "BOX-WF-DRAFT-1")
        self.assertEqual(
            OrderAuditEntry.objects.filter(order_id=order_id, order_type="receiving").count(),
            2,
        )

    def test_save_receiving_flow_draft_keeps_box_goods_type_override(self):
        order_id = "R-WF-DRAFT-TYPE"
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="receiving",
            action="status",
            agency=self.agency,
            user=self.user,
            description="Статус приемки",
            payload={
                "status": "warehouse",
                "status_label": "В ожидании поставки товара",
                "goods_type": "op",
                "goods_type_label": "Оптовый",
                "items": [{"sku_code": self.sku.sku_code, "qty": 1}],
            },
        )

        result = ReceivingWorkflowService.save_receiving_flow_draft(
            order_id=order_id,
            entries=list(OrderAuditEntry.objects.filter(order_id=order_id, order_type="receiving").order_by("created_at")),
            role="storekeeper",
            boxes_raw=(
                '[{"code":"BOX-WF-DRAFT-TYPE-1","goods_type":"br","items":[{"sku_code":"SKU-WF-1",'
                '"name":"Товар workflow","size":"42","qty":1,"goods_type":"br"}],"sealed":false}]'
            ),
            pallets_raw=(
                '[{"code":"PAL-WF-DRAFT-TYPE-1","boxes":["BOX-WF-DRAFT-TYPE-1"],'
                '"items":[],"sealed":false,"location":{"zone":"PR"}}]'
            ),
            active_box="BOX-WF-DRAFT-TYPE-1",
            active_pallet="PAL-WF-DRAFT-TYPE-1",
            user=self.user,
        )

        self.assertEqual(result.status, "saved")
        draft_entry = OrderAuditEntry.objects.filter(order_id=order_id, order_type="receiving").latest("id")
        saved_box = draft_entry.payload["flow_state"]["boxes"][0]
        self.assertEqual(saved_box["goods_type"], "br")
        self.assertEqual(saved_box["items"][0]["goods_type"], "br")

    def test_save_receiving_flow_draft_persists_box_characteristics_to_container_truth(self):
        order_id = "R-WF-DRAFT-DIMS"
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="receiving",
            action="status",
            agency=self.agency,
            user=self.user,
            description="Статус приемки",
            payload={
                "status": "warehouse",
                "status_label": "В ожидании поставки товара",
                "goods_type": "op",
                "goods_type_label": "Оптовый",
                "items": [{"sku_code": self.sku.sku_code, "qty": 1}],
            },
        )

        result = ReceivingWorkflowService.save_receiving_flow_draft(
            order_id=order_id,
            entries=list(OrderAuditEntry.objects.filter(order_id=order_id, order_type="receiving").order_by("created_at")),
            role="storekeeper",
            boxes_raw=(
                '[{"code":"BOX-WF-DIMS-1","gross_weight_g":1250,"width_mm":320,"height_mm":210,"depth_mm":430,'
                '"items":[{"sku_code":"SKU-WF-1","name":"Товар workflow","size":"42","qty":1}],"sealed":true}]'
            ),
            pallets_raw='[{"code":"PAL-WF-DIMS-1","boxes":["BOX-WF-DIMS-1"],"items":[],"sealed":false,"location":{"zone":"PR"}}]',
            active_box="",
            active_pallet="PAL-WF-DIMS-1",
            user=self.user,
        )

        self.assertEqual(result.status, "saved")
        draft_entry = OrderAuditEntry.objects.filter(order_id=order_id, order_type="receiving").latest("id")
        saved_box = draft_entry.payload["flow_state"]["boxes"][0]
        self.assertEqual(saved_box["gross_weight_g"], 1250)
        self.assertEqual(saved_box["width_mm"], 320)
        self.assertEqual(saved_box["height_mm"], 210)
        self.assertEqual(saved_box["depth_mm"], 430)
        container = WarehouseContainer.objects.get(agency=self.agency, container_code="BOX-WF-DIMS-1")
        self.assertEqual(container.gross_weight_g, 1250)
        self.assertEqual(container.width_mm, 320)
        self.assertEqual(container.height_mm, 210)
        self.assertEqual(container.depth_mm, 430)

    def test_save_receiving_flow_draft_creates_primary_nomenclature_for_manual_item(self):
        order_id = "R-WF-DRAFT-SKU"
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="receiving",
            action="status",
            agency=self.agency,
            user=self.user,
            description="Статус приемки",
            payload={
                "status": "warehouse",
                "status_label": "В ожидании поставки товара",
                "goods_type": "op",
                "goods_type_label": "Оптовый",
                "items": [{"sku_code": "028", "name": "Базовая позиция", "qty": 1}],
            },
        )

        result = ReceivingWorkflowService.save_receiving_flow_draft(
            order_id=order_id,
            entries=list(OrderAuditEntry.objects.filter(order_id=order_id, order_type="receiving").order_by("created_at")),
            role="storekeeper",
            boxes_raw=(
                '[{"code":"BOX-WF-DRAFT-SKU-1","items":[{"sku_code":"028","name":"Брюки","brand":"Mavi",'
                '"color":"Серый","size":"31-33","barcode":"DRAFT-BAR-31","qty":4}],"sealed":false}]'
            ),
            pallets_raw=(
                '[{"code":"PAL-WF-DRAFT-SKU-1","boxes":["BOX-WF-DRAFT-SKU-1"],'
                '"items":[],"sealed":false,"location":{"zone":"PR"}}]'
            ),
            active_box="BOX-WF-DRAFT-SKU-1",
            active_pallet="PAL-WF-DRAFT-SKU-1",
            user=self.user,
        )

        self.assertEqual(result.status, "saved")
        created_sku = SKU.objects.get(agency=self.agency, sku_code="028", deleted=False)
        created_barcode = SKUBarcode.objects.get(sku=created_sku, size="31-33")
        self.assertEqual(created_sku.name, "Брюки")
        self.assertEqual(created_barcode.value, "DRAFT-BAR-31")

    def test_prepare_receiving_placement_close_builds_normalized_payload(self):
        order_id = "R-WF-PLACEMENT-PREP"
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="receiving",
            action="status",
            agency=self.agency,
            user=self.user,
            description="Статус приемки",
            payload={"status": "warehouse", "status_label": "Размещение на складе"},
        )
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="receiving",
            action="status",
            agency=self.agency,
            user=self.user,
            description="Акт приемки",
            payload={
                "act": "receiving",
                "act_label": "Акт приемки",
                "act_state": "closed",
                "act_items": [
                    {
                        "sku_code": self.sku.sku_code,
                        "name": self.sku.name,
                        "size": self.sku.size,
                        "actual_qty": 2,
                    }
                ],
            },
        )

        result = ReceivingWorkflowService.prepare_receiving_placement_close(
            order_id=order_id,
            entries=list(OrderAuditEntry.objects.filter(order_id=order_id, order_type="receiving").order_by("created_at")),
            boxes_raw=(
                '[{"code":"BOX-WF-PL-1","items":[{"sku":"SKU-WF-1","name":"Товар workflow","size":"42","qty":2}],"sealed":true}]'
            ),
            pallets_raw=(
                '[{"code":"PAL-WF-PL-1","boxes":["BOX-WF-PL-1"],"items":[],"sealed":true,'
                '"location":{"zone":"OS","row":2,"section":1,"tier":1,"cell":3}}]'
            ),
        )

        self.assertEqual(result.status, "ok")
        self.assertFalse(result.has_closed_act)
        self.assertEqual(result.placement_items[0]["actual_qty"], 2)
        self.assertEqual(result.placement_items[0]["box_qty"], 2)
        self.assertEqual(result.pallets[0]["location"]["zone"], "OS")
        self.assertEqual(result.pallets[0]["location"]["row"], 2)

    def test_prepare_receiving_placement_close_rejects_unassigned_boxes(self):
        order_id = "R-WF-PLACEMENT-BAD"
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="receiving",
            action="status",
            agency=self.agency,
            user=self.user,
            description="Акт приемки",
            payload={
                "act": "receiving",
                "act_label": "Акт приемки",
                "act_state": "closed",
                "act_items": [
                    {
                        "sku_code": self.sku.sku_code,
                        "name": self.sku.name,
                        "size": self.sku.size,
                        "actual_qty": 1,
                    }
                ],
            },
        )

        result = ReceivingWorkflowService.prepare_receiving_placement_close(
            order_id=order_id,
            entries=list(OrderAuditEntry.objects.filter(order_id=order_id, order_type="receiving").order_by("created_at")),
            boxes_raw=(
                '[{"code":"BOX-WF-PL-BAD","items":[{"sku":"SKU-WF-1","name":"Товар workflow","size":"42","qty":1}],"sealed":true}]'
            ),
            pallets_raw='[]',
        )

        self.assertEqual(result.status, "invalid")
        self.assertEqual(result.reason, "unassigned_boxes")

    def test_log_receiving_flow_box_action_writes_staff_overaction(self):
        order_id = "R-WF-BOX-ACTION"
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="receiving",
            action="status",
            agency=self.agency,
            user=self.user,
            description="Статус приемки",
            payload={"status": "warehouse", "status_label": "В ожидании поставки товара"},
        )

        result = ReceivingWorkflowService.log_receiving_flow_box_action(
            order_id=order_id,
            action="move_batch",
            payload={
                "box_codes": ["BOX-1", "BOX-2"],
                "source_pallet_index": 1,
                "target_pallet_index": 2,
            },
            user=self.user,
        )

        self.assertEqual(result.status, "logged")
        self.assertEqual(result.snapshot["box_count"], 2)
        overaction = AuditEntry.objects.filter(journal__code="staff_overactions").latest("id")
        self.assertIn("Перемещение группы коробов", overaction.description)
        self.assertEqual((overaction.snapshot or {}).get("order_id"), order_id)

    def test_build_receiving_flow_page_context_prefers_warehouse_state(self):
        order_id = "R-WF-CONTEXT"
        self._create_receiving_snapshot(order_id, "placed_in_receiving")
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="receiving",
            action="status",
            agency=self.agency,
            user=self.user,
            description="Статус приемки",
            payload={
                "status": "warehouse",
                "status_label": "Размещение на складе",
                "items": [{"sku_code": self.sku.sku_code, "name": self.sku.name, "size": self.sku.size, "qty": 2}],
            },
        )

        context = ReceivingWorkflowService.build_receiving_flow_page_context(
            order_id=order_id,
            entries=list(OrderAuditEntry.objects.filter(order_id=order_id, order_type="receiving").order_by("created_at")),
            role="storekeeper",
        )

        self.assertEqual(context["status_label"], "Завершена приемка")
        self.assertEqual(context["items"][0]["sku_code"], self.sku.sku_code)
        self.assertEqual(context["warehouse_not_created_pallets"], 0)

    def test_build_box_metric_lookup_key_ignores_unstable_receiving_fields(self):
        history_box = {
            "items": [
                {
                    "sku_code": self.sku.sku_code,
                    "name": self.sku.name,
                    "brand": "Brand A",
                    "color": "Black",
                    "size": self.sku.size,
                    "barcode": "BC-HISTORY-1",
                    "goods_type": "op",
                    "qty": 2,
                }
            ]
        }
        current_box = {
            "items": [
                {
                    "sku_code": self.sku.sku_code,
                    "name": self.sku.name,
                    "brand": "Brand A",
                    "color": "Black",
                    "size": self.sku.size,
                    "qty": 2,
                }
            ]
        }

        self.assertEqual(
            ReceivingWorkflowService._build_box_metric_lookup_key(history_box, mode="receiving"),
            ReceivingWorkflowService._build_box_metric_lookup_key(current_box, mode="receiving"),
        )

    def test_build_box_metric_lookup_key_ignores_unstable_processing_fields(self):
        history_box = {
            "items": [
                {
                    "sku_code": self.sku.sku_code,
                    "name": self.sku.name,
                    "size": self.sku.size,
                    "barcode": "BC-HISTORY-1",
                    "goods_type": "gv",
                    "qty": 2,
                }
            ]
        }
        current_box = {
            "items": [
                {
                    "sku_code": self.sku.sku_code,
                    "name": self.sku.name,
                    "size": self.sku.size,
                    "qty": 2,
                }
            ]
        }

        self.assertEqual(
            ReceivingWorkflowService._build_box_metric_lookup_key(history_box, mode="processing"),
            ReceivingWorkflowService._build_box_metric_lookup_key(current_box, mode="processing"),
        )

    def test_build_known_box_metrics_defaults_ignores_boxes_older_than_fifteen_days(self):
        old_entry = OrderAuditEntry.objects.create(
            order_id="R-WF-OLD-METRICS",
            order_type="receiving",
            action="update",
            agency=self.agency,
            user=self.user,
            description="РЎС‚Р°СЂС‹Р№ Р°РєС‚ СЂР°Р·РјРµС‰РµРЅРёСЏ",
            payload={
                "act": "placement",
                "act_state": "closed",
                "act_boxes": [
                    {
                        "code": "BOX-OLD-METRICS",
                        "gross_weight_g": 1110,
                        "width_mm": 310,
                        "height_mm": 210,
                        "depth_mm": 410,
                        "items": [
                            {
                                "sku_code": self.sku.sku_code,
                                "name": self.sku.name,
                                "brand": "Brand A",
                                "color": "Black",
                                "size": self.sku.size,
                                "barcode": "BC-OLD-1",
                                "goods_type": "op",
                                "qty": 2,
                            }
                        ],
                    }
                ],
            },
        )
        OrderAuditEntry.objects.filter(pk=old_entry.pk).update(created_at=timezone.now() - timedelta(days=16))

        result = ReceivingWorkflowService.build_known_box_metrics_defaults(
            agency=self.agency,
            mode="receiving",
        )

        self.assertEqual(result, {})

    def test_build_receiving_flow_page_context_includes_known_box_metrics(self):
        history_item = {
            "sku_code": self.sku.sku_code,
            "name": self.sku.name,
            "brand": "Brand A",
            "color": "Black",
            "size": self.sku.size,
            "barcode": "BC-HISTORY-1",
            "goods_type": "op",
            "qty": 2,
        }
        OrderAuditEntry.objects.create(
            order_id="R-WF-HISTORY-METRICS",
            order_type="receiving",
            action="update",
            agency=self.agency,
            user=self.user,
            description="РђРєС‚ СЂР°Р·РјРµС‰РµРЅРёСЏ",
            payload={
                "act": "placement",
                "act_state": "closed",
                "act_boxes": [
                    {
                        "code": "BOX-HISTORY-1",
                        "gross_weight_g": 1250,
                        "width_mm": 320,
                        "height_mm": 210,
                        "depth_mm": 430,
                        "items": [history_item],
                    }
                ],
            },
        )
        order_id = "R-WF-METRICS-CONTEXT"
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="receiving",
            action="status",
            agency=self.agency,
            user=self.user,
            description="РЎС‚Р°С‚СѓСЃ РїСЂРёРµРјРєРё",
            payload={
                "status": "accepted",
                "status_label": "Р’ РїСЂРёРµРјРєРµ",
                "goods_type": "op",
                "items": [dict(history_item)],
            },
        )

        context = ReceivingWorkflowService.build_receiving_flow_page_context(
            order_id=order_id,
            entries=list(OrderAuditEntry.objects.filter(order_id=order_id, order_type="receiving").order_by("created_at")),
            role="storekeeper",
        )
        expected_key = ReceivingWorkflowService._build_box_metric_lookup_key(
            {"items": [history_item]},
            mode="receiving",
        )

        self.assertEqual(context["known_box_metrics"][expected_key]["gross_weight_g"], 1250)
        self.assertEqual(context["known_box_metrics"][expected_key]["width_mm"], 320)
        self.assertEqual(context["known_box_metrics"][expected_key]["height_mm"], 210)
        self.assertEqual(context["known_box_metrics"][expected_key]["depth_mm"], 430)

    def test_build_receiving_act_page_context_hides_submit_after_storage(self):
        order_id = "R-WF-ACT-CONTEXT"
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="receiving",
            action="status",
            agency=self.agency,
            user=self.user,
            description="Статус приемки",
            payload={
                "status": "warehouse",
                "status_label": "В ожидании поставки товара",
                "items": [{"sku_code": self.sku.sku_code, "name": self.sku.name, "size": self.sku.size, "qty": 2}],
            },
        )
        self._create_receiving_snapshot(order_id, "stored")

        context = ReceivingWorkflowService.build_receiving_act_page_context(
            order_id=order_id,
            entries=list(OrderAuditEntry.objects.filter(order_id=order_id, order_type="receiving").order_by("created_at")),
            role="storekeeper",
        )

        self.assertEqual(context["status_label"], "Товар принят и размещен на складе")
        self.assertFalse(context["can_submit"])
        self.assertFalse(context["can_add_items"])

    def test_build_receiving_act_page_context_splits_special_goods_type_into_rows(self):
        order_id = "R-WF-ACT-TYPE"
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="receiving",
            action="status",
            agency=self.agency,
            user=self.user,
            description="Статус приемки",
            payload={
                "status": "warehouse",
                "status_label": "В ожидании поставки товара",
                "goods_type": "op",
                "goods_type_label": "Оптовый",
                "items": [{"sku_code": self.sku.sku_code, "name": self.sku.name, "size": self.sku.size, "qty": 2}],
            },
        )
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="receiving",
            action="status",
            agency=self.agency,
            user=self.user,
            description="Акт приемки",
            payload={
                "act": "receiving",
                "goods_type": "op",
                "goods_type_label": "Оптовый",
                "act_state": "closed",
                "act_items": [
                    {
                        "sku_code": self.sku.sku_code,
                        "name": self.sku.name,
                        "size": self.sku.size,
                        "planned_qty": 2,
                        "actual_qty": 2,
                    }
                ],
            },
        )
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="receiving",
            action="update",
            agency=self.agency,
            user=self.user,
            description="Акт размещения",
            payload={
                "act": "placement",
                "act_state": "closed",
                "goods_type": "op",
                "act_boxes": [
                    {
                        "code": "BOX-WF-ACT-TYPE-0",
                        "goods_type": "op",
                        "items": [
                            {
                                "sku_code": self.sku.sku_code,
                                "name": self.sku.name,
                                "size": self.sku.size,
                                "qty": 1,
                                "goods_type": "op",
                            }
                        ],
                    },
                    {
                        "code": "BOX-WF-ACT-TYPE-1",
                        "goods_type": "br",
                        "items": [
                            {
                                "sku_code": self.sku.sku_code,
                                "name": self.sku.name,
                                "size": self.sku.size,
                                "qty": 1,
                                "goods_type": "br",
                            }
                        ],
                    }
                ],
                "act_pallets": [
                    {
                        "code": "PAL-WF-ACT-TYPE-1",
                        "boxes": ["BOX-WF-ACT-TYPE-0", "BOX-WF-ACT-TYPE-1"],
                        "items": [],
                    }
                ],
            },
        )

        context = ReceivingWorkflowService.build_receiving_act_page_context(
            order_id=order_id,
            entries=list(OrderAuditEntry.objects.filter(order_id=order_id, order_type="receiving").order_by("created_at")),
            role="storekeeper",
        )

        self.assertEqual(len(context["items"]), 2)
        self.assertEqual(context["items"][0]["name"], self.sku.name)
        self.assertEqual(context["items"][0]["planned_qty"], 2)
        self.assertEqual(context["items"][0]["actual_qty"], 1)
        self.assertEqual(context["items"][1]["name"], f"{self.sku.name} (Брак)")
        self.assertEqual(context["items"][1]["planned_qty"], 0)
        self.assertEqual(context["items"][1]["actual_qty"], 1)

    def test_build_receiving_act_page_context_prefers_document_placement_over_later_move_snapshot(self):
        order_id = "R-WF-ACT-TYPE-MOVE"
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="receiving",
            action="status",
            agency=self.agency,
            user=self.user,
            description="Статус приемки",
            payload={
                "status": "warehouse",
                "status_label": "В ожидании поставки товара",
                "goods_type": "op",
                "goods_type_label": "Оптовый",
                "items": [{"sku_code": self.sku.sku_code, "name": self.sku.name, "size": self.sku.size, "qty": 2}],
            },
        )
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="receiving",
            action="status",
            agency=self.agency,
            user=self.user,
            description="Акт приемки",
            payload={
                "act": "receiving",
                "goods_type": "op",
                "goods_type_label": "Оптовый",
                "act_state": "closed",
                "act_items": [
                    {
                        "sku_code": self.sku.sku_code,
                        "name": self.sku.name,
                        "size": self.sku.size,
                        "planned_qty": 2,
                        "actual_qty": 2,
                    }
                ],
            },
        )
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="receiving",
            action="update",
            agency=self.agency,
            user=self.user,
            description="Создан акт размещения",
            payload={
                "act": "placement",
                "act_label": "Акт размещения",
                "act_state": "closed",
                "goods_type": "op",
                "act_items": [
                    {
                        "sku_code": self.sku.sku_code,
                        "name": self.sku.name,
                        "size": self.sku.size,
                        "actual_qty": 2,
                        "box_qty": 2,
                        "pallet_qty": 2,
                    }
                ],
                "act_boxes": [
                    {
                        "code": "BOX-WF-ACT-TYPE-MOVE-0",
                        "goods_type": "op",
                        "items": [
                            {
                                "sku_code": self.sku.sku_code,
                                "name": self.sku.name,
                                "size": self.sku.size,
                                "qty": 1,
                                "goods_type": "op",
                            }
                        ],
                    },
                    {
                        "code": "BOX-WF-ACT-TYPE-MOVE-1",
                        "goods_type": "br",
                        "items": [
                            {
                                "sku_code": self.sku.sku_code,
                                "name": self.sku.name,
                                "size": self.sku.size,
                                "qty": 1,
                                "goods_type": "br",
                            }
                        ],
                    },
                ],
                "act_pallets": [
                    {
                        "code": "PAL-WF-ACT-TYPE-MOVE-0",
                        "boxes": ["BOX-WF-ACT-TYPE-MOVE-0"],
                        "items": [],
                    },
                    {
                        "code": "PAL-WF-ACT-TYPE-MOVE-1",
                        "boxes": ["BOX-WF-ACT-TYPE-MOVE-1"],
                        "items": [],
                    },
                ],
            },
        )
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="receiving",
            action="status",
            agency=self.agency,
            user=self.user,
            description="Перемещение паллеты PAL-WF-ACT-TYPE-MOVE-0",
            payload={
                "act": "placement",
                "act_state": "closed",
                "act_boxes": [
                    {
                        "code": "BOX-WF-ACT-TYPE-MOVE-0",
                        "items": [
                            {
                                "sku_code": self.sku.sku_code,
                                "name": self.sku.name,
                                "size": self.sku.size,
                                "qty": 1,
                                "goods_type": "op",
                            }
                        ],
                    }
                ],
                "act_pallets": [
                    {
                        "code": "PAL-WF-ACT-TYPE-MOVE-0",
                        "boxes": ["BOX-WF-ACT-TYPE-MOVE-0"],
                        "items": [],
                    }
                ],
            },
        )

        context = ReceivingWorkflowService.build_receiving_act_page_context(
            order_id=order_id,
            entries=list(OrderAuditEntry.objects.filter(order_id=order_id, order_type="receiving").order_by("created_at")),
            role="storekeeper",
        )

        self.assertEqual(len(context["items"]), 2)
        self.assertEqual(context["items"][0]["actual_qty"], 1)
        self.assertEqual(context["items"][1]["name"], f"{self.sku.name} (Брак)")
        self.assertEqual(context["items"][1]["actual_qty"], 1)

    def test_confirm_receiving_to_warehouse_logs_and_reassigns_tasks(self):
        order_id = "R-WF-CONFIRM"
        manager_task = Task.objects.create(
            title=f"Подтвердите заявку на приемку товара №{order_id}",
            description="Задача менеджеру",
            route=f"/orders/receiving/{order_id}/",
            assigned_to=self.manager,
            created_by=self.user,
        )

        result = ReceivingWorkflowService.confirm_receiving_to_warehouse(
            order_id=order_id,
            agency=self.agency,
            status_payload={
                "status": "sent_unconfirmed",
                "status_label": "Ждет подтверждения",
                "receiving_chz_service": "required",
                "items": [{"sku_code": self.sku.sku_code, "qty": 1}],
            },
            user=self.user,
            submitted_at=timezone.make_aware(datetime(2026, 4, 22, 12, 0)),
        )

        self.assertEqual(result.payload.get("status"), "warehouse")
        self.assertEqual(result.payload.get("status_label"), "В ожидании поставки товара")
        self.assertEqual(result.manager_tasks_closed, 1)
        self.assertTrue(result.storekeeper_task_created)

        manager_task.refresh_from_db()
        self.assertEqual(manager_task.status, "done")
        storekeeper_task = (
            Task.objects.filter(
                route=f"/orders/receiving/{order_id}/",
                assigned_to__role="storekeeper",
            )
            .exclude(id=manager_task.id)
            .get()
        )
        self.assertIn("Принять заявку на приемку товара", storekeeper_task.title)
        self.assertIn("Честный знак: Сканировать Честный знак", storekeeper_task.description)
        self.assertEqual(storekeeper_task.observer.user_id, self.user.id)

        entry = OrderAuditEntry.objects.get(order_id=order_id, order_type="receiving")
        self.assertEqual(entry.description, "Подтверждено и отправлено на склад")
        self.assertEqual(entry.payload.get("status"), "warehouse")

    def test_send_receiving_to_storage_creates_move_tasks_with_selected_destination(self):
        order_id = "R-WF-STORAGE"
        self._create_receiving_snapshot(order_id, "placed_in_receiving")
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="receiving",
            action="status",
            agency=self.agency,
            user=self.user,
            description="Статус приемки",
            payload={
                "status": "warehouse",
                "status_label": "Размещение на складе",
                "flow_closed": True,
            },
        )
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="receiving",
            action="update",
            agency=self.agency,
            user=self.user,
            description="Акт размещения",
            payload={
                "act": "placement",
                "act_label": "Акт размещения",
                "act_state": "closed",
                "act_pallets": [
                    {
                        "code": "PAL-R-WF-STORAGE",
                        "boxes": [],
                        "items": [],
                        "location": {"zone": "PR"},
                    }
                ],
            },
        )

        result = ReceivingWorkflowService.send_receiving_to_storage(
            order_id=order_id,
            entries=list(OrderAuditEntry.objects.filter(order_id=order_id, order_type="receiving").order_by("created_at")),
            role="storekeeper",
            user=self.user,
            destinations_raw=(
                '[{"pallet_code":"PAL-R-WF-STORAGE","destination":{"zone":"OS","row":2,"section":1,"tier":1,"cell":3}}]'
            ),
        )

        self.assertEqual(result.status, "ok")
        self.assertEqual(result.created_count, 1)
        task = MoveTask.objects.get()
        self.assertEqual(task.pallet_code, "PAL-R-WF-STORAGE")
        self.assertEqual(task.to_zone, "OS")
        self.assertEqual(task.to_row, 2)
        self.assertEqual(task.to_section, 1)
        self.assertEqual(task.to_tier, 1)
        self.assertEqual(task.to_cell, 3)

    def test_send_receiving_to_storage_ignores_own_draft_token_and_releases_it(self):
        order_id = "R-WF-STORAGE-DRAFT"
        draft_token = "draft-own-storage-1"
        self._create_receiving_snapshot(order_id, "placed_in_receiving")
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="receiving",
            action="status",
            agency=self.agency,
            user=self.user,
            description="Статус приемки",
            payload={
                "status": "warehouse",
                "status_label": "Размещение на складе",
                "flow_closed": True,
            },
        )
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="receiving",
            action="update",
            agency=self.agency,
            user=self.user,
            description="Акт размещения",
            payload={
                "act": "placement",
                "act_label": "Акт размещения",
                "act_state": "closed",
                "act_pallets": [
                    {
                        "code": "PAL-R-WF-STORAGE-DRAFT",
                        "boxes": [],
                        "items": [],
                        "location": {"zone": "PR"},
                    }
                ],
            },
        )
        draft_result = PutawayDraftReservationService.sync_receiving_putaway_draft(
            draft_token=draft_token,
            order_id=order_id,
            agency=self.agency,
            rows=[
                {
                    "pallet_code": "PAL-R-WF-STORAGE-DRAFT",
                    "destination": {"zone": "OS", "row": 2, "section": 1, "tier": 1, "cell": 3},
                }
            ],
            requested_by=self.user,
            requested_by_role="storekeeper",
        )
        self.assertTrue(draft_result.ok)

        result = ReceivingWorkflowService.send_receiving_to_storage(
            order_id=order_id,
            entries=list(OrderAuditEntry.objects.filter(order_id=order_id, order_type="receiving").order_by("created_at")),
            role="storekeeper",
            user=self.user,
            destinations_raw=(
                '[{"pallet_code":"PAL-R-WF-STORAGE-DRAFT","destination":{"zone":"OS","row":2,"section":1,"tier":1,"cell":3}}]'
            ),
            draft_token=draft_token,
        )

        self.assertEqual(result.status, "ok")
        self.assertEqual(result.created_count, 1)
        self.assertFalse(
            WarehouseOperation.objects.filter(
                context_type=PutawayDraftReservationService.DRAFT_CONTEXT_TYPE,
                context_id=draft_token,
            ).exists()
        )

    def test_send_receiving_to_storage_rejects_invalid_destination_payload(self):
        order_id = "R-WF-STORAGE-BAD"
        self._create_receiving_snapshot(order_id, "placed_in_receiving")
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="receiving",
            action="status",
            agency=self.agency,
            user=self.user,
            description="Статус приемки",
            payload={
                "status": "warehouse",
                "status_label": "Размещение на складе",
                "flow_closed": True,
            },
        )
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="receiving",
            action="update",
            agency=self.agency,
            user=self.user,
            description="Акт размещения",
            payload={
                "act": "placement",
                "act_label": "Акт размещения",
                "act_state": "closed",
                "act_pallets": [
                    {
                        "code": "PAL-R-WF-STORAGE-BAD",
                        "boxes": [],
                        "items": [],
                        "location": {"zone": "PR"},
                    }
                ],
            },
        )

        result = ReceivingWorkflowService.send_receiving_to_storage(
            order_id=order_id,
            entries=list(OrderAuditEntry.objects.filter(order_id=order_id, order_type="receiving").order_by("created_at")),
            role="storekeeper",
            user=self.user,
            destinations_raw='[{"pallet_code":"PAL-R-WF-STORAGE-BAD","destination":{"zone":"OS","row":2}}]',
        )

        self.assertEqual(result.status, "invalid_destination")
        self.assertIn("для зоны OS укажите ряд, секцию, ярус и ячейку", result.error_message)
        self.assertEqual(MoveTask.objects.count(), 0)

    def test_suggest_receiving_destinations_keeps_os_and_falls_back_to_mr(self):
        result = ReceivingWorkflowService.suggest_receiving_destinations(
            [
                {"code": "PAL-KEEP", "location": {"zone": "OS", "row": 2, "section": 1, "tier": 1, "cell": 3}},
                {"code": "PAL-FALLBACK", "location": {"zone": "PR"}},
            ],
            exclude_order_type="receiving",
            exclude_order_id="R-WF-SUGGEST",
            agency_id=self.agency.id,
        )

        self.assertEqual(result["PAL-KEEP"]["zone"], "OS")
        self.assertEqual(result["PAL-KEEP"]["row"], 2)
        self.assertEqual(result["PAL-FALLBACK"]["zone"], "MR")
        self.assertEqual(result["PAL-FALLBACK"]["row"], 1)

    def test_build_receiving_warehouse_move_progress_counts_existing_statuses(self):
        order_id = "R-WF-PROGRESS"
        placement_pallets = [
            {"code": "PAL-PROGRESS-1"},
            {"code": "PAL-PROGRESS-2"},
            {"code": "PAL-PROGRESS-3"},
            {"code": "PAL-PROGRESS-4"},
        ]
        OrderAuditEntry.objects.create(
            order_id="9001",
            order_type="stock_move",
            action="status",
            agency=self.agency,
            user=self.user,
            description="Move created",
            payload={"receiving_order_id": order_id, "pallet_code": "PAL-PROGRESS-1", "status": "created"},
        )
        OrderAuditEntry.objects.create(
            order_id="9002",
            order_type="stock_move",
            action="status",
            agency=self.agency,
            user=self.user,
            description="Move in progress",
            payload={"receiving_order_id": order_id, "pallet_code": "PAL-PROGRESS-2", "status": "in_progress"},
        )
        OrderAuditEntry.objects.create(
            order_id="9003",
            order_type="stock_move",
            action="status",
            agency=self.agency,
            user=self.user,
            description="Move done",
            payload={"receiving_order_id": order_id, "pallet_code": "PAL-PROGRESS-3", "status": "done"},
        )

        progress = ReceivingWorkflowService.build_receiving_warehouse_move_progress(order_id, placement_pallets)

        self.assertEqual(progress["total_pallets"], 4)
        self.assertEqual(progress["created_count"], 1)
        self.assertEqual(progress["in_progress_count"], 1)
        self.assertEqual(progress["done_count"], 1)
        self.assertEqual(progress["not_created_count"], 1)
        self.assertTrue(progress["has_any_task"])

    def test_build_receiving_warehouse_move_rows_prefers_existing_move_destination(self):
        order_id = "R-WF-ROWS"
        OrderAuditEntry.objects.create(
            order_id="9101",
            order_type="stock_move",
            action="status",
            agency=self.agency,
            user=self.user,
            description="Move created",
            payload={
                "receiving_order_id": order_id,
                "pallet_code": "PAL-ROWS-1",
                "status": "created",
                "status_label": "Передано ричтракеру",
                "to_zone": "OS",
                "to_row": 2,
                "to_section": 1,
                "to_tier": 1,
                "to_cell": 3,
            },
        )

        rows = ReceivingWorkflowService.build_receiving_warehouse_move_rows(
            order_id,
            [{"code": "PAL-ROWS-1", "location": {"zone": "PR"}}],
            agency_id=self.agency.id,
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "created")
        self.assertEqual(rows[0]["status_class"], "created")
        self.assertFalse(rows[0]["selectable"])
        self.assertEqual(rows[0]["destination"]["zone"], "OS")
        self.assertEqual(rows[0]["destination"]["row"], 2)
        self.assertIn("OS", rows[0]["to_label"])

    def test_build_receiving_warehouse_move_panel_calculates_can_send(self):
        order_id = "R-WF-PANEL"
        receiving_entry = self._create_receiving_snapshot(order_id, "placed_in_receiving")
        panel = ReceivingWorkflowService.build_receiving_warehouse_move_panel(
            order_id=order_id,
            placement_pallets=[{"code": "PAL-PANEL-1", "location": {"zone": "PR"}}],
            receiving_result=WarehouseGoodsStateResolver.resolve_for_receiving_order(
                order_id=order_id,
                agency=self.agency,
                payload={"status": "warehouse", "status_label": "Размещение на складе"},
            ),
            flow_locked=True,
            role_allowed=True,
            agency_id=self.agency.id,
        )

        self.assertTrue(panel.can_send)
        self.assertEqual(panel.progress["not_created_count"], 1)
        self.assertEqual(panel.rows[0]["destination"]["zone"], "MR")
        self.assertEqual(receiving_entry.warehouse_state_code, "placed_in_receiving")

    def test_send_receiving_act_to_client_marks_order_done_and_closes_manager_tasks(self):
        order_id = "R-WF-SEND-ACT"
        manager_task = Task.objects.create(
            title=f"Проверьте размещение по заявке на приемку товара №{order_id}",
            description="Финальная задача менеджеру",
            route=f"/orders/receiving/{order_id}/",
            assigned_to=self.manager,
            created_by=self.user,
        )
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="receiving",
            action="status",
            agency=self.agency,
            user=self.user,
            description="Статус приемки",
            payload={"status": "warehouse", "status_label": "Размещение на складе"},
        )
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="receiving",
            action="status",
            agency=self.agency,
            user=self.user,
            description="Акт приемки",
            payload={
                "act": "receiving",
                "act_label": "Акт приемки",
                "act_storekeeper_signed": True,
                "act_manager_signed": True,
            },
        )
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="receiving",
            action="update",
            agency=self.agency,
            user=self.user,
            description="Акт размещения",
            payload={
                "act": "placement",
                "act_label": "Акт размещения",
                "act_state": "closed",
            },
        )

        result = ReceivingWorkflowService.send_receiving_act_to_client(
            order_id=order_id,
            entries=list(OrderAuditEntry.objects.filter(order_id=order_id, order_type="receiving").order_by("created_at")),
            user=self.user,
        )

        self.assertTrue(result.applied)
        self.assertEqual(result.payload.get("status"), "done")
        self.assertEqual(result.payload.get("status_label"), "Выполнена")
        self.assertEqual(result.payload.get("act_sent"), "Акт приемки")
        self.assertEqual(result.manager_tasks_closed, 1)
        manager_task.refresh_from_db()
        self.assertEqual(manager_task.status, "done")
        descriptions = list(
            OrderAuditEntry.objects.filter(order_id=order_id, order_type="receiving")
            .order_by("created_at")
            .values_list("description", flat=True)
        )
        self.assertIn("Акт отправлен клиенту", descriptions)
        self.assertIn("Акт приемки отправлен клиенту", descriptions)

    def test_prepare_receiving_flow_completion_builds_marked_units_and_payloads(self):
        order_id = "R-WF-PREP-CZ"
        self.sku.honest_sign = True
        self.sku.save(update_fields=["honest_sign"])
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="receiving",
            action="status",
            agency=self.agency,
            user=self.user,
            description="Статус приемки",
            payload={
                "status": "warehouse",
                "status_label": "Размещение на складе",
                "goods_type": "op",
                "receiving_mode": "cz",
                "eta_at": "2026-04-22T10:00:00",
                "vehicle_number": "A123AA790",
                "items": [
                    {
                        "sku_code": self.sku.sku_code,
                        "name": self.sku.name,
                        "size": self.sku.size,
                        "qty": 2,
                    }
                ],
            },
        )
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="receiving",
            action="update",
            agency=self.agency,
            user=self.user,
            description="Акт размещения",
            payload={
                "act": "placement",
                "act_label": "Акт размещения",
                "act_state": "closed",
            },
        )
        now = timezone.localtime()
        for idx in range(1, 3):
            MarkingCode.objects.create(
                order_type="receiving",
                order_id=order_id,
                agency=self.agency,
                sku=self.sku,
                sku_code=self.sku.sku_code,
                size=self.sku.size,
                barcode="WF-MARKED-BAR",
                box_barcode="BOX-WF-PREP-1",
                code=f"WF-CZ-{idx:04d}",
                source="scan",
                created_by=self.user,
                used_at=now,
                used_by=self.user,
            )

        result = ReceivingWorkflowService.prepare_receiving_flow_completion(
            order_id=order_id,
            entries=list(OrderAuditEntry.objects.filter(order_id=order_id, order_type="receiving").order_by("created_at")),
            boxes_raw=(
                '[{"code":"BOX-WF-PREP-1","items":[{"sku_code":"SKU-WF-1",'
                '"name":"Товар workflow","size":"42","qty":2}],"sealed":true}]'
            ),
            pallets_raw=(
                '[{"code":"PAL-WF-PREP-1","boxes":["BOX-WF-PREP-1"],'
                '"items":[],"sealed":true,"location":{"zone":"PR"}}]'
            ),
        )

        self.assertEqual(result.status, "ok")
        self.assertEqual(result.receiving_mode, "cz")
        self.assertEqual(result.status_payload.get("status"), "warehouse")
        self.assertFalse(result.has_mismatch)
        self.assertEqual(result.vehicle_number, "A123AA790")
        self.assertIn("2026-04-22T10:00:00", result.normalized_eta)
        self.assertTrue(result.has_closed_placement_act)
        self.assertEqual(len(result.act_units), 2)
        self.assertEqual(
            {unit["marking_code"] for unit in result.act_units},
            {"WF-CZ-0001", "WF-CZ-0002"},
        )
        self.assertTrue(all(unit["pallet_code"] == "PAL-WF-PREP-1" for unit in result.act_units))
        self.assertEqual(result.act_items[0]["actual_qty"], 2)
        self.assertEqual(result.placement_items[0]["box_qty"], 2)
        self.assertEqual(result.flow_state["boxes"][0]["code"], "BOX-WF-PREP-1")

    def test_prepare_receiving_flow_completion_rejects_unassigned_boxes(self):
        order_id = "R-WF-PREP-INVALID"
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="receiving",
            action="status",
            agency=self.agency,
            user=self.user,
            description="Статус приемки",
            payload={
                "status": "warehouse",
                "status_label": "Размещение на складе",
                "goods_type": "op",
                "items": [
                    {
                        "sku_code": self.sku.sku_code,
                        "name": self.sku.name,
                        "size": self.sku.size,
                        "qty": 1,
                    }
                ],
            },
        )

        result = ReceivingWorkflowService.prepare_receiving_flow_completion(
            order_id=order_id,
            entries=list(OrderAuditEntry.objects.filter(order_id=order_id, order_type="receiving").order_by("created_at")),
            boxes_raw=(
                '[{"code":"BOX-WF-PREP-INVALID","items":[{"sku_code":"SKU-WF-1",'
                '"name":"Товар workflow","size":"42","qty":1}],"sealed":true}]'
            ),
            pallets_raw=(
                '[{"code":"PAL-WF-PREP-INVALID","boxes":[],"items":[{"sku_code":"SKU-WF-1",'
                '"name":"Товар workflow","size":"42","qty":1}],"sealed":true,'
                '"location":{"zone":"PR"}}]'
            ),
        )

        self.assertEqual(result.status, "invalid")
        self.assertEqual(result.reason, "unassigned_boxes")

    def test_complete_receiving_flow_handles_audit_and_followup_tasks(self):
        order_id = "R-WF-1"
        service_fact = self._create_receiving_service_fact(order_id, quantity=2)
        Task.objects.create(
            title="Принять заявку на приемку товара",
            description="Исходная задача кладовщику",
            route=f"/orders/receiving/{order_id}/",
            assigned_to=self.storekeeper,
            created_by=self.user,
        )

        result = ReceivingWorkflowService.complete_receiving_flow(
            order_id=order_id,
            agency=self.agency,
            status_payload={
                "status": "warehouse",
                "status_label": "Взята в работу",
                "goods_type": "op",
            },
            has_mismatch=False,
            receiving_mode="standard",
            act_items=[
                {
                    "sku_code": self.sku.sku_code,
                    "name": self.sku.name,
                    "size": self.sku.size,
                    "planned_qty": 2,
                    "actual_qty": 2,
                    "comment": "",
                }
            ],
            placement_items=[
                {
                    "sku_code": self.sku.sku_code,
                    "name": self.sku.name,
                    "size": self.sku.size,
                    "actual_qty": 2,
                    "box_qty": 2,
                    "pallet_qty": 2,
                    "comment": "",
                }
            ],
            boxes=[
                {
                    "code": "BOX-WF-1",
                    "sealed": True,
                    "items": [
                        {
                            "sku": self.sku.sku_code,
                            "name": self.sku.name,
                            "size": self.sku.size,
                            "barcode": "WF-BOX-1",
                            "qty": 2,
                        }
                    ],
                }
            ],
            pallets=[
                {
                    "code": "PAL-WF-1",
                    "sealed": True,
                    "boxes": ["BOX-WF-1"],
                    "items": [],
                    "location": {"zone": "PR"},
                }
            ],
            flow_state={"boxes": [], "pallets": []},
            eta_at="2026-04-22T10:00:00+03:00",
            vehicle_number="A123AA790",
            has_closed_placement_act=False,
            user=self.user,
            submitted_at=timezone.make_aware(datetime(2026, 4, 22, 11, 0)),
        )

        self.assertFalse(result.placement_previously_closed)
        self.assertEqual(result.storekeeper_tasks_closed, 1)
        self.assertTrue(result.manager_followup_created)
        self.assertEqual(result.act_payload.get("act"), "receiving")
        self.assertEqual(result.placement_payload.get("act"), "placement")

        storekeeper_task = Task.objects.get(
            route=f"/orders/receiving/{order_id}/",
            assigned_to=self.storekeeper,
        )
        self.assertEqual(storekeeper_task.status, "done")

        manager_task = Task.objects.get(
            route=f"/orders/receiving/{order_id}/",
            assigned_to=self.manager,
        )
        self.assertIn("Проверьте размещение", manager_task.title)
        self.assertEqual(manager_task.observer.user_id, self.user.id)

        entries = list(
            OrderAuditEntry.objects.filter(order_id=order_id, order_type="receiving").order_by("created_at")
        )
        self.assertEqual(len(entries), 2)
        entries_by_act = {entry.payload.get("act"): entry for entry in entries}
        self.assertEqual(entries_by_act["receiving"].description, "Создан акт приемки")
        self.assertEqual(entries_by_act["placement"].description, "Создан акт размещения")

        application = BillingApplication.objects.get(
            application_type=BillingApplication.TYPE_RECEIVING,
            application_id=order_id,
            client=self.agency,
        )
        self.assertTrue(application.is_operations_completed)
        self.assertEqual(application.charges.count(), 0)
        service_fact.refresh_from_db()
        self.assertEqual(service_fact.application_id, application.id)
        notification = BillingStaffNotification.objects.get(recipient=self.manager_user)
        self.assertIn(order_id, notification.title)
        self.assertEqual(
            notification.link_url,
            f"/team-manager/billing/applications/{application.pk}/",
        )

    def test_complete_receiving_flow_requires_current_draft_version(self):
        order_id = "R-WF-STALE-CLOSE"
        barcode = "WF-STALE-CLOSE"
        SKUBarcode.objects.create(
            sku=self.sku,
            value=barcode,
            size=self.sku.size,
            is_primary=True,
        )
        self._create_receiving_service_fact(order_id, quantity=1)
        boxes = [
            {
                "code": "BOX-WF-STALE-CLOSE",
                "sealed": True,
                "items": [
                    {
                        "sku": self.sku.sku_code,
                        "name": self.sku.name,
                        "size": self.sku.size,
                        "barcode": barcode,
                        "qty": 1,
                    }
                ],
            }
        ]
        pallets = [
            {
                "code": "PAL-WF-STALE-CLOSE",
                "sealed": True,
                "boxes": ["BOX-WF-STALE-CLOSE"],
                "items": [],
                "location": {"zone": "PR"},
            }
        ]
        flow_state = {"boxes": boxes, "pallets": pallets}
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="receiving",
            action="status",
            agency=self.agency,
            user=self.user,
            description="Статус приемки",
            payload={
                "status": "warehouse",
                "status_label": "Взята в работу",
                "goods_type": "op",
            },
        )
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="receiving",
            action="update",
            agency=self.agency,
            user=self.user,
            description="Черновик приемки потоком",
            payload={
                "flow_state": flow_state,
                "flow_client_version": 2,
            },
        )
        completion_kwargs = {
            "order_id": order_id,
            "agency": self.agency,
            "status_payload": {"status": "warehouse", "goods_type": "op"},
            "has_mismatch": False,
            "receiving_mode": "standard",
            "act_items": [
                {
                    "sku_code": self.sku.sku_code,
                    "barcode": barcode,
                    "name": self.sku.name,
                    "size": self.sku.size,
                    "planned_qty": 1,
                    "actual_qty": 1,
                    "comment": "",
                }
            ],
            "placement_items": [
                {
                    "sku_code": self.sku.sku_code,
                    "barcode": barcode,
                    "name": self.sku.name,
                    "size": self.sku.size,
                    "actual_qty": 1,
                    "box_qty": 1,
                    "pallet_qty": 1,
                    "comment": "",
                }
            ],
            "boxes": boxes,
            "pallets": pallets,
            "flow_state": flow_state,
            "user": self.user,
        }

        with self.assertRaisesMessage(ValueError, "изменен в другой вкладке"):
            ReceivingWorkflowService.complete_receiving_flow(
                **completion_kwargs,
                flow_client_version=1,
            )

        self.assertEqual(
            OrderAuditEntry.objects.filter(order_id=order_id, order_type="receiving").count(),
            2,
        )
        self.assertFalse(
            WarehouseStockSnapshot.objects.filter(
                agency=self.agency,
                source_context_type="receiving",
                source_context_id=order_id,
            ).exists()
        )

        result = ReceivingWorkflowService.complete_receiving_flow(
            **completion_kwargs,
            flow_client_version=2,
        )

        self.assertFalse(result.already_completed)
        self.assertEqual(
            WarehouseStockSnapshot.objects.filter(
                agency=self.agency,
                source_context_type="receiving",
                source_context_id=order_id,
            ).count(),
            1,
        )

    def test_receiving_act_post_saves_extra_item_into_primary_nomenclature(self):
        order_id = "R-WF-ACT-SKU"
        self.client.force_login(self.user)
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="receiving",
            action="status",
            agency=self.agency,
            user=self.user,
            description="Статус приемки",
            payload={
                "status": "warehouse",
                "status_label": "В ожидании поставки товара",
                "goods_type": "op",
                "goods_type_label": "Оптовый",
                "items": [],
            },
        )

        response = self.client.post(
            f"/orders/receiving/{order_id}/act/",
            {
                "eta_at": "2026-04-22T10:00",
                "vehicle_number": "A123AA790",
                "extra_sku_code[]": ["028"],
                "extra_barcode[]": ["ACT-BAR-028"],
                "extra_name[]": ["Джинсы синие"],
                "extra_brand[]": ["Levis"],
                "extra_color[]": ["Синий"],
                "extra_size[]": ["27-35"],
                "extra_planned_qty[]": ["250"],
                "extra_actual_qty[]": ["250"],
            },
        )

        self.assertEqual(response.status_code, 302)
        created_sku = SKU.objects.get(agency=self.agency, sku_code="028", deleted=False)
        created_barcode = SKUBarcode.objects.get(sku=created_sku, size="27-35")
        latest = OrderAuditEntry.objects.filter(order_id=order_id, order_type="receiving").latest("id")
        self.assertEqual((latest.payload or {}).get("act"), "receiving")
        act_item = ((latest.payload or {}).get("act_items") or [])[0]
        self.assertEqual(act_item.get("sku_id"), created_sku.id)
        self.assertEqual(act_item.get("barcode"), created_barcode.value)
        self.assertEqual(act_item.get("nomenclature_kind"), "sku")

    def test_complete_receiving_flow_saves_new_item_into_primary_nomenclature_and_snapshot(self):
        order_id = "R-WF-FLOW-SKU"
        self._create_receiving_service_fact(order_id, quantity=250)

        result = ReceivingWorkflowService.complete_receiving_flow(
            order_id=order_id,
            agency=self.agency,
            status_payload={
                "status": "warehouse",
                "status_label": "Взята в работу",
                "goods_type": "op",
            },
            has_mismatch=False,
            receiving_mode="standard",
            act_items=[
                {
                    "sku_code": "ART-NEW-1",
                    "barcode": "FLOW-BAR-001",
                    "name": "Оптовая позиция без SKU",
                    "brand": "NoBrand",
                    "color": "Черный",
                    "size": "27-35",
                    "planned_qty": 250,
                    "actual_qty": 250,
                    "comment": "",
                }
            ],
            placement_items=[
                {
                    "sku_code": "ART-NEW-1",
                    "barcode": "FLOW-BAR-001",
                    "name": "Оптовая позиция без SKU",
                    "brand": "NoBrand",
                    "color": "Черный",
                    "size": "27-35",
                    "actual_qty": 250,
                    "box_qty": 250,
                    "pallet_qty": 250,
                    "comment": "",
                }
            ],
            boxes=[
                {
                    "code": "BOX-WF-TEMP-1",
                    "sealed": True,
                    "items": [
                        {
                            "sku": "ART-NEW-1",
                            "barcode": "FLOW-BAR-001",
                            "name": "Оптовая позиция без SKU",
                            "brand": "NoBrand",
                            "color": "Черный",
                            "size": "27-35",
                            "qty": 250,
                        }
                    ],
                }
            ],
            pallets=[
                {
                    "code": "PAL-WF-TEMP-1",
                    "sealed": True,
                    "boxes": ["BOX-WF-TEMP-1"],
                    "items": [],
                    "location": {"zone": "PR"},
                }
            ],
            flow_state={"boxes": [], "pallets": []},
            eta_at="2026-04-22T10:00:00+03:00",
            vehicle_number="A123AA790",
            has_closed_placement_act=False,
            user=self.user,
            submitted_at=timezone.make_aware(datetime(2026, 4, 22, 11, 0)),
        )

        created_sku = SKU.objects.get(agency=self.agency, sku_code="ART-NEW-1", deleted=False)
        created_barcode = SKUBarcode.objects.get(sku=created_sku, size="27-35")
        snapshot = WarehouseStockSnapshot.objects.get(
            agency=self.agency,
            source_context_type="receiving",
            source_context_id=order_id,
        )
        self.assertEqual(created_sku.brand, "NoBrand")
        self.assertEqual(created_sku.color, "Черный")
        self.assertEqual(created_barcode.value, "FLOW-BAR-001")
        self.assertEqual(snapshot.sku_code, "ART-NEW-1")
        placement_item = ((result.placement_payload.get("act_boxes") or [])[0].get("items") or [])[0]
        self.assertEqual(placement_item.get("sku"), "ART-NEW-1")
        self.assertEqual(placement_item.get("barcode"), "FLOW-BAR-001")

    def test_receiving_flow_page_shows_new_opt_fields_inline(self):
        order_id = "R-FLOW-OPT-FIELDS"
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="receiving",
            action="status",
            agency=self.agency,
            user=self.user,
            description="Статус приемки",
            payload={
                "status": "warehouse",
                "status_label": "В ожидании поставки товара",
                "goods_type": "op",
                "goods_type_label": "Оптовый",
                "receiving_mode": "standard",
                "items": [
                    {
                        "sku_code": "028",
                        "name": "Брюки",
                        "brand": "Mavi",
                        "color": "Серый",
                        "size": "31-33",
                        "qty": 4,
                    }
                ],
            },
        )
        self.client.force_login(self.user)

        response = self.client.get(f"/orders/receiving/{order_id}/flow/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="opt-add-item-btn"', html=False)
        self.assertContains(response, 'id="item-size-context-input"', html=False)
        self.assertContains(response, 'data-action="add-size"', html=False)
        self.assertContains(response, 'id="box-view-print"', html=False)
        self.assertContains(response, "Бренд")
        self.assertContains(response, "Цвет")
        self.assertContains(response, "ШК")
        self.assertContains(response, "Для новой позиции")

    def test_close_receiving_placement_reuses_existing_manager_followup(self):
        order_id = "R-WF-2"
        Task.objects.create(
            title=f"Проверьте размещение по заявке на приемку товара №{order_id}",
            description="Уже существует follow-up",
            route=f"/orders/receiving/{order_id}/",
            assigned_to=self.manager,
            created_by=self.user,
        )
        open_storekeeper_task = Task.objects.create(
            title="Принять заявку на приемку товара",
            description="Открытая задача кладовщику",
            route=f"/orders/receiving/{order_id}/",
            assigned_to=self.storekeeper,
            created_by=self.user,
        )

        result = ReceivingWorkflowService.close_receiving_placement(
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
                    "sku_code": self.sku.sku_code,
                    "name": self.sku.name,
                    "size": self.sku.size,
                    "actual_qty": 1,
                    "box_qty": 1,
                    "pallet_qty": 1,
                    "comment": "",
                }
            ],
            boxes=[
                {
                    "code": "BOX-WF-2",
                    "sealed": True,
                    "items": [
                        {
                            "sku": self.sku.sku_code,
                            "name": self.sku.name,
                            "size": self.sku.size,
                            "barcode": "WF-BOX-2",
                            "qty": 1,
                        }
                    ],
                }
            ],
            pallets=[
                {
                    "code": "PAL-WF-2",
                    "sealed": True,
                    "boxes": ["BOX-WF-2"],
                    "items": [],
                    "location": {"zone": "PR"},
                }
            ],
            has_closed_act=True,
            user=self.user,
            submitted_at=timezone.make_aware(datetime(2026, 4, 22, 12, 0)),
        )

        self.assertTrue(result.placement_previously_closed)
        self.assertEqual(result.storekeeper_tasks_closed, 0)
        self.assertFalse(result.manager_followup_created)
        self.assertEqual(
            Task.objects.filter(
                route=f"/orders/receiving/{order_id}/",
                assigned_to=self.manager,
            ).count(),
            1,
        )
        open_storekeeper_task.refresh_from_db()
        self.assertNotEqual(open_storekeeper_task.status, "done")

        entry = OrderAuditEntry.objects.get(order_id=order_id, order_type="receiving")
        self.assertEqual(entry.description, "Обновлен акт размещения")
        self.assertEqual(entry.payload.get("act_state"), "closed")


class OrdersReceivingWarehouseDetailTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="orders_receiving_detail", password="pwd")
        self.storekeeper = Employee.objects.create(
            full_name="Кладовщик деталей",
            role="storekeeper",
            user=self.user,
            is_active=True,
        )
        self.client.login(username="orders_receiving_detail", password="pwd")
        self.agency = Agency.objects.create(agn_name="Клиент приемки деталки", pref="КЗИ")

    def _create_receiving_status(self, order_id: str, extra_payload: dict | None = None):
        payload = {
            "status": "warehouse",
            "status_label": "В ожидании поставки товара",
            "goods_type": "op",
            "goods_type_label": "Оптовый",
            "items": [
                {
                    "sku_code": "SKU-R-BASE",
                    "name": "Receiving Base",
                    "size": "42",
                    "qty": 2,
                }
            ],
        }
        if extra_payload:
            payload.update(extra_payload)
        return OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="receiving",
            action="status",
            agency=self.agency,
            user=self.user,
            description="Статус приемки",
            payload=payload,
        )

    def _create_receiving_task(self, order_id: str) -> Task:
        return Task.objects.create(
            title=f"Принять заявку №{order_id}",
            route=f"/orders/receiving/{order_id}/",
            assigned_to=self.storekeeper,
            status="backlog",
        )

    def test_receiving_flow_get_is_read_only_for_assignment_and_task(self):
        order_id = "R-FLOW-GET-READ-ONLY-1"
        self._create_receiving_status(order_id)
        task = self._create_receiving_task(order_id)
        audit_count = OrderAuditEntry.objects.filter(order_id=order_id, order_type="receiving").count()

        response = self.client.get(f"/orders/receiving/{order_id}/flow/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            OrderAuditEntry.objects.filter(order_id=order_id, order_type="receiving").count(),
            audit_count,
        )
        self.assertFalse(
            OrderAuditEntry.objects.filter(
                order_id=order_id,
                order_type="receiving",
                description="Заявка взята в работу кладовщиком",
            ).exists()
        )
        task.refresh_from_db()
        self.assertEqual(task.status, "backlog")

    def test_receiving_act_get_is_read_only_for_assignment_and_task(self):
        order_id = "R-ACT-GET-READ-ONLY-1"
        self._create_receiving_status(order_id)
        task = self._create_receiving_task(order_id)
        audit_count = OrderAuditEntry.objects.filter(order_id=order_id, order_type="receiving").count()

        response = self.client.get(f"/orders/receiving/{order_id}/act/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            OrderAuditEntry.objects.filter(order_id=order_id, order_type="receiving").count(),
            audit_count,
        )
        self.assertFalse(
            OrderAuditEntry.objects.filter(
                order_id=order_id,
                order_type="receiving",
                description="Заявка взята в работу кладовщиком",
            ).exists()
        )
        task.refresh_from_db()
        self.assertEqual(task.status, "backlog")

    def test_receiving_flow_start_post_assigns_storekeeper_and_task(self):
        order_id = "R-FLOW-POST-START-1"
        self._create_receiving_status(order_id)
        task = self._create_receiving_task(order_id)
        before_start = timezone.localtime()

        response = self.client.post(
            f"/orders/receiving/{order_id}/flow/",
            {"flow_action": "start"},
        )

        self.assertRedirects(
            response,
            f"/orders/receiving/{order_id}/flow/",
            fetch_redirect_response=False,
        )
        start_entry = OrderAuditEntry.objects.get(
            order_id=order_id,
            order_type="receiving",
            description="Заявка взята в работу кладовщиком",
        )
        self.assertEqual(start_entry.payload.get("storekeeper_employee_id"), self.storekeeper.id)
        task.refresh_from_db()
        self.assertEqual(task.status, "in_progress")
        self.assertEqual(task.assigned_to_id, self.storekeeper.id)
        after_start = timezone.localtime()
        self.assertGreaterEqual(task.due_date, before_start + timedelta(hours=24))
        self.assertLessEqual(task.due_date, after_start + timedelta(hours=24))

    def test_receiving_reopen_preserves_manager_corrected_deadline(self):
        order_id = "R-FLOW-KEEP-MANUAL-DUE-1"
        self._create_receiving_status(order_id)
        task = self._create_receiving_task(order_id)
        first_response = self.client.post(
            f"/orders/receiving/{order_id}/flow/",
            {"flow_action": "start"},
        )
        self.assertEqual(first_response.status_code, 302)
        manual_due_date = timezone.localtime() + timedelta(days=4)
        Task.objects.filter(pk=task.pk).update(due_date=manual_due_date)

        repeated_response = self.client.post(
            f"/orders/receiving/{order_id}/flow/",
            {"flow_action": "start"},
        )

        self.assertEqual(repeated_response.status_code, 302)
        task.refresh_from_db()
        self.assertEqual(task.due_date, manual_due_date)

    def test_receiving_flow_start_post_requires_csrf(self):
        order_id = "R-FLOW-POST-CSRF-1"
        self._create_receiving_status(order_id)
        task = self._create_receiving_task(order_id)
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.user)

        response = csrf_client.post(
            f"/orders/receiving/{order_id}/flow/",
            {"flow_action": "start"},
        )

        self.assertEqual(response.status_code, 403)
        self.assertFalse(
            OrderAuditEntry.objects.filter(
                order_id=order_id,
                order_type="receiving",
                description="Заявка взята в работу кладовщиком",
            ).exists()
        )
        task.refresh_from_db()
        self.assertEqual(task.status, "backlog")

    def test_receiving_detail_post_starts_work_before_opening_flow(self):
        order_id = "R-DETAIL-POST-START-1"
        self._create_receiving_status(
            order_id,
            {
                "items": [
                    {
                        "sku_code": "SKU-R-DETAIL-POST",
                        "name": "Receiving Detail Post",
                        "brand": "Fullbox Test",
                        "color": "Черный",
                        "size": "42",
                        "qty": 2,
                    }
                ]
            },
        )
        task = self._create_receiving_task(order_id)

        response = self.client.post(
            f"/orders/receiving/{order_id}/",
            {
                "action": "create_receiving_act",
                "goods_type": "op",
                "receiving_mode": "standard",
            },
        )

        self.assertRedirects(
            response,
            f"/orders/receiving/{order_id}/flow/",
            fetch_redirect_response=False,
        )
        self.assertEqual(
            OrderAuditEntry.objects.filter(
                order_id=order_id,
                order_type="receiving",
                description="Заявка взята в работу кладовщиком",
            ).count(),
            1,
        )
        task.refresh_from_db()
        self.assertEqual(task.status, "in_progress")
        self.assertEqual(task.assigned_to_id, self.storekeeper.id)

    def test_receiving_act_post_starts_work_before_creating_act(self):
        order_id = "R-ACT-POST-START-1"
        self._create_receiving_status(
            order_id,
            {
                "items": [
                    {
                        "sku_code": "SKU-R-ACT-POST",
                        "name": "Receiving Act Post",
                        "brand": "Fullbox Test",
                        "color": "Черный",
                        "size": "42",
                        "qty": 2,
                    }
                ]
            },
        )
        task = self._create_receiving_task(order_id)

        response = self.client.post(
            f"/orders/receiving/{order_id}/act/",
            {
                "eta_at": (timezone.localtime() + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M"),
                "vehicle_number": "А123ВС77",
                "actual_qty[]": "2",
            },
        )

        self.assertRedirects(
            response,
            f"/orders/receiving/{order_id}/placement/?ok=1",
            fetch_redirect_response=False,
        )
        self.assertEqual(
            OrderAuditEntry.objects.filter(
                order_id=order_id,
                order_type="receiving",
                description="Заявка взята в работу кладовщиком",
            ).count(),
            1,
        )
        self.assertTrue(
            OrderAuditEntry.objects.filter(
                order_id=order_id,
                order_type="receiving",
                description="Создан акт приемки",
            ).exists()
        )
        task.refresh_from_db()
        self.assertEqual(task.status, "in_progress")
        self.assertEqual(task.assigned_to_id, self.storekeeper.id)

    def test_receiving_status_helper_prefers_warehouse_state(self):
        entry = SimpleNamespace(
            payload={"act": "placement", "act_state": "open"},
            order_type="receiving",
            order_id="R-H-1",
            agency=self.agency,
        )
        location = WarehouseWritePathService.ensure_location(warehouse_code="MSK", zone_code="PR")
        WarehouseStockSnapshot.objects.create(
            agency=self.agency,
            source_context_type="receiving",
            source_context_id="R-H-1",
            sku_code="SKU-R-1",
            name="Receiving Helper",
            size="42",
            barcode="200000009201",
            goods_type="gv",
            qty=5,
            available_qty=5,
            container_code="PAL-R-1",
            location=location,
            zone_code=location.zone_code,
            zone_kind=location.zone_kind,
            warehouse_state_code="placed_in_receiving",
        )

        self.assertEqual(_status_label_from_entry(entry), "Завершена приемка")

    def test_receiving_current_responsible_prefers_warehouse_state(self):
        location = WarehouseWritePathService.ensure_location(warehouse_code="MSK", zone_code="PR")
        WarehouseStockSnapshot.objects.create(
            agency=self.agency,
            source_context_type="receiving",
            source_context_id="R-H-2",
            sku_code="SKU-R-2",
            name="Receiving Responsible",
            size="42",
            barcode="200000009202",
            goods_type="gv",
            qty=6,
            available_qty=6,
            container_code="PAL-R-2",
            location=location,
            zone_code=location.zone_code,
            zone_kind=location.zone_kind,
            warehouse_state_code="stored",
        )
        entry = SimpleNamespace(
            payload={"status": "warehouse"},
            order_type="receiving",
            order_id="R-H-2",
            agency=self.agency,
        )

        self.assertIn("Кладовщик", _current_responsible_label(entry))

    def test_receiving_history_actor_prefers_warehouse_state(self):
        location = WarehouseWritePathService.ensure_location(warehouse_code="MSK", zone_code="PR")
        WarehouseStockSnapshot.objects.create(
            agency=self.agency,
            source_context_type="receiving",
            source_context_id="R-H-3",
            sku_code="SKU-R-3",
            name="Receiving History",
            size="42",
            barcode="200000009203",
            goods_type="gv",
            qty=7,
            available_qty=7,
            container_code="PAL-R-3",
            location=location,
            zone_code=location.zone_code,
            zone_kind=location.zone_kind,
            warehouse_state_code="placed_in_receiving",
        )
        entry = SimpleNamespace(
            payload={"act": "placement", "act_state": "open"},
            order_type="receiving",
            order_id="R-H-3",
            agency=self.agency,
            action="status",
            user=None,
        )

        self.assertIn("Кладовщик", _history_actor_label(entry))

    def test_receiving_detail_page_prefers_warehouse_status_and_responsible(self):
        order_id = "R-DETAIL-WH-1"
        self._create_receiving_status(order_id)
        location = WarehouseWritePathService.ensure_location(warehouse_code="MSK", zone_code="PR")
        WarehouseStockSnapshot.objects.create(
            agency=self.agency,
            source_context_type="receiving",
            source_context_id=order_id,
            sku_code="SKU-R-DETAIL",
            name="Receiving Detail",
            size="42",
            barcode="200000009204",
            goods_type="gv",
            qty=4,
            available_qty=4,
            container_code="PAL-R-D1",
            location=location,
            zone_code=location.zone_code,
            zone_kind=location.zone_kind,
            warehouse_state_code="placed_in_receiving",
        )

        response = self.client.get(f"/orders/receiving/{order_id}/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["status_label"], "Завершена приемка")
        self.assertTrue(response.context["can_create_receiving_act"])
        self.assertTrue(response.context["can_open_flow"])
        self.assertEqual(
            response.context["receiving_flow_url"],
            f"/orders/receiving/{order_id}/flow/",
        )
        self.assertFalse(response.context["can_manage_storage_placement"])
        self.assertEqual(response.context["reachtruck_request_url"], "")
        self.assertEqual(
            response.context["next_step_label"],
            "Создать или завершить размещение в хранение",
        )

    def test_receiving_detail_summary_and_manager_act_sign_cta(self):
        manager_user = get_user_model().objects.create_user(
            username="orders_receiving_summary_manager",
            password="pwd",
        )
        Employee.objects.create(
            full_name="Менеджер сводки",
            role="manager",
            user=manager_user,
            is_active=True,
        )
        self.client.logout()
        self.client.login(username="orders_receiving_summary_manager", password="pwd")
        order_id = "130"
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="receiving",
            action="create",
            agency=self.agency,
            user=manager_user,
            description="Создана заявка",
            payload={
                "goods_type": "no",
                "receiving_mode": "standard",
                "expected_boxes": 1,
                "place_type": "box",
                "items": [],
            },
        )
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="receiving",
            action="status",
            agency=self.agency,
            user=self.user,
            description="Акт приёмки подписан кладовщиком и отправлен менеджеру",
            payload={
                "act": "receiving",
                "act_label": "Акт приемки",
                "status": "warehouse",
                "status_label": "Принято складом, акт приемки отправлен менеджеру",
                "receiving_mode": "standard",
                "goods_type": "no",
                "expected_boxes": 1,
                "act_storekeeper_signed": True,
                "act_manager_signed": False,
                "act_items": [{"sku_code": "", "name": "Место", "qty": 1, "actual_qty": 1}],
                "act_boxes": [{"code": "BX-130-1", "qty_total": 1}],
            },
        )

        response = self.client.get(f"/orders/receiving/{order_id}/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Результат приемки")
        self.assertContains(response, "Менеджер должен подписать акт приемки")
        self.assertContains(response, "Подписать акт приемки")
        summary = response.context["receiving_summary"]
        self.assertEqual(summary["mode_badge"], "Обычная")
        self.assertEqual(summary["accepted_total"], 1)
        self.assertTrue(response.context["needs_manager_act_sign"])

    def test_receiving_detail_marks_manager_done_right_after_manager_sign(self):
        manager_user = get_user_model().objects.create_user(username="orders_receiving_detail_manager", password="pwd")
        Employee.objects.create(
            full_name="Менеджер деталки",
            role="manager",
            user=manager_user,
            is_active=True,
        )
        self.client.logout()
        self.client.login(username="orders_receiving_detail_manager", password="pwd")
        order_id = "R-DETAIL-MANAGER-DONE-1"
        self._create_receiving_status(order_id)
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="receiving",
            action="status",
            agency=self.agency,
            user=self.user,
            description="Акт приемки",
            payload={
                "act": "receiving",
                "act_label": "Акт приемки",
                "act_storekeeper_signed": True,
                "act_manager_signed": True,
            },
        )
        location = WarehouseWritePathService.ensure_location(warehouse_code="MSK", zone_code="PR")
        WarehouseStockSnapshot.objects.create(
            agency=self.agency,
            source_context_type="receiving",
            source_context_id=order_id,
            sku_code="SKU-R-MANAGER-DONE",
            name="Receiving Manager Done",
            size="42",
            barcode="200000009212",
            goods_type="gv",
            qty=4,
            available_qty=4,
            container_code="PAL-R-MANAGER-D1",
            location=location,
            zone_code=location.zone_code,
            zone_kind=location.zone_kind,
            warehouse_state_code="placed_in_receiving",
        )

        response = self.client.get(f"/orders/receiving/{order_id}/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["status_label"], "Выполнена")
        self.assertEqual(response.context["next_step_label"], "")

    def test_receiving_detail_keeps_storekeeper_open_after_sign_until_storage(self):
        order_id = "R-DETAIL-SKLAD-OPEN-1"
        self._create_receiving_status(order_id)
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="receiving",
            action="status",
            agency=self.agency,
            user=self.user,
            description="Акт приемки",
            payload={
                "act": "receiving",
                "act_label": "Акт приемки",
                "act_storekeeper_signed": True,
            },
        )
        location = WarehouseWritePathService.ensure_location(warehouse_code="MSK", zone_code="PR")
        WarehouseStockSnapshot.objects.create(
            agency=self.agency,
            source_context_type="receiving",
            source_context_id=order_id,
            sku_code="SKU-R-SKLAD-OPEN",
            name="Receiving Storekeeper Open",
            size="42",
            barcode="200000009213",
            goods_type="gv",
            qty=4,
            available_qty=4,
            container_code="PAL-R-SKLAD-OPEN-1",
            location=location,
            zone_code=location.zone_code,
            zone_kind=location.zone_kind,
            warehouse_state_code="placed_in_receiving",
        )

        response = self.client.get(f"/orders/receiving/{order_id}/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["status_label"], "Завершена приемка")
        self.assertEqual(response.context["next_step_label"], "ВЫДАЙ ЗАДАНИЕ РИЧТРАКЕРУ")
        self.assertEqual(response.context["next_step_tone"], "danger")

    def test_receiving_detail_storekeeper_waits_for_reachtruck_even_if_task_issued_before_manager_sign(self):
        order_id = "R-DETAIL-SKLAD-WAIT-1"
        self._create_receiving_status(order_id)
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="receiving",
            action="status",
            agency=self.agency,
            user=self.user,
            description="Акт приемки",
            payload={
                "act": "receiving",
                "act_label": "Акт приемки",
                "act_storekeeper_signed": True,
            },
        )
        location = WarehouseWritePathService.ensure_location(warehouse_code="MSK", zone_code="PR")
        WarehouseStockSnapshot.objects.create(
            agency=self.agency,
            source_context_type="receiving",
            source_context_id=order_id,
            sku_code="SKU-R-SKLAD-WAIT",
            name="Receiving Storekeeper Wait",
            size="42",
            barcode="200000009214",
            goods_type="gv",
            qty=4,
            available_qty=4,
            container_code="PAL-R-SKLAD-WAIT-1",
            location=location,
            zone_code=location.zone_code,
            zone_kind=location.zone_kind,
            warehouse_state_code="placed_in_receiving",
        )
        move_request = MoveRequest.objects.create(
            context_type=MoveRequest.CONTEXT_RECEIVING,
            context_id=order_id,
            agency=self.agency,
            destination_zone="OS",
            status=MoveRequest.STATUS_IN_PROGRESS,
        )
        MoveTask.objects.create(
            request=move_request,
            pallet_code="PAL-R-SKLAD-WAIT-1",
            to_zone="OS",
            status=MoveTask.STATUS_CREATED,
        )

        response = self.client.get(f"/orders/receiving/{order_id}/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["status_label"], "Размещение на складе")
        self.assertEqual(response.context["next_step_label"], "ОЖИДАЕМ РИЧТРАКЕР")
        self.assertEqual(response.context["next_step_tone"], "pending")

    def test_receiving_detail_hides_receiving_actions_after_storage(self):
        order_id = "R-DETAIL-STORED-1"
        self._create_receiving_status(order_id)
        location = WarehouseWritePathService.ensure_location(warehouse_code="MSK", zone_code="OS")
        WarehouseStockSnapshot.objects.create(
            agency=self.agency,
            source_context_type="receiving",
            source_context_id=order_id,
            sku_code="SKU-R-STORED",
            name="Receiving Stored",
            size="42",
            barcode="200000009209",
            goods_type="gv",
            qty=4,
            available_qty=4,
            container_code="PAL-R-S1",
            location=location,
            zone_code=location.zone_code,
            zone_kind=location.zone_kind,
            warehouse_state_code="stored",
        )

        response = self.client.get(f"/orders/receiving/{order_id}/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["status_label"], "Товар принят и размещен на складе")
        self.assertEqual(response.context["next_step_label"], "Товар доступен на складе")
        self.assertFalse(response.context["can_create_receiving_act"])
        self.assertFalse(response.context["can_open_flow"])
        self.assertFalse(response.context["can_manage_storage_placement"])

    def test_receiving_detail_offers_storekeeper_sign_act_after_flow_closed(self):
        order_id = "R-DETAIL-SIGN-ACT-1"
        self._create_receiving_status(order_id)
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="receiving",
            action="update",
            agency=self.agency,
            user=self.user,
            description="Поток закрыт",
            payload={
                "flow_closed": True,
                "flow_closed_at": timezone.localtime().isoformat(),
                "flow_boxes": [{"code": "BOX-R-SIGN-1"}],
                "flow_pallets": [{"code": "PAL-R-SIGN-1"}],
            },
        )
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="receiving",
            action="status",
            agency=self.agency,
            user=self.user,
            description="Акт приемки",
            payload={
                "act": "receiving",
                "act_label": "Акт приемки",
                "act_items": [
                    {
                        "sku_code": "SKU-R-SIGN",
                        "name": "Receiving Sign Act",
                        "planned_qty": 4,
                        "actual_qty": 4,
                    }
                ],
            },
        )
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="receiving",
            action="status",
            agency=self.agency,
            user=self.user,
            description="Акт размещения",
            payload={
                "act": "placement",
                "act_label": "Акт размещения",
                "act_state": "closed",
            },
        )
        location = WarehouseWritePathService.ensure_location(warehouse_code="MSK", zone_code="PR")
        WarehouseStockSnapshot.objects.create(
            agency=self.agency,
            source_context_type="receiving",
            source_context_id=order_id,
            sku_code="SKU-R-SIGN",
            name="Receiving Sign Act",
            size="42",
            barcode="200000009215",
            goods_type="gv",
            qty=4,
            available_qty=4,
            container_code="PAL-R-SIGN-1",
            location=location,
            zone_code=location.zone_code,
            zone_kind=location.zone_kind,
            warehouse_state_code="placed_in_receiving",
        )

        response = self.client.get(f"/orders/receiving/{order_id}/")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["can_print_receiving_act"], "print flag")
        self.assertTrue(response.context["flow_closed"], "flow closed")
        self.assertTrue(response.context["can_storekeeper_sign_act"], "sign CTA")
        self.assertContains(response, "Подписать и отправить акт")

        # после подписи кладовщиком CTA пропадает
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="receiving",
            action="status",
            agency=self.agency,
            user=self.user,
            description="Акт подписан складом",
            payload={
                "act": "receiving",
                "act_label": "Акт приемки",
                "act_items": [
                    {
                        "sku_code": "SKU-R-SIGN",
                        "name": "Receiving Sign Act",
                        "planned_qty": 4,
                        "actual_qty": 4,
                    }
                ],
                "act_storekeeper_signed": True,
            },
        )
        response2 = self.client.get(f"/orders/receiving/{order_id}/")
        self.assertEqual(response2.status_code, 200)
        self.assertFalse(response2.context["can_storekeeper_sign_act"])

    def test_head_manager_can_sign_receiving_act_as_storekeeper_with_history_marker(self):
        from io import BytesIO

        from django.core.files.base import ContentFile
        from PIL import Image

        head_user = get_user_model().objects.create_user(username="orders_receiving_head_manager", password="pwd")
        head_employee = Employee.objects.create(
            full_name="Начальник склада",
            role="head_manager",
            user=head_user,
            is_active=True,
        )
        signature = BytesIO()
        Image.new("RGBA", (24, 12), (248, 144, 0, 255)).save(signature, format="PNG")
        head_employee.facsimile.save("head-manager-sign.png", ContentFile(signature.getvalue()), save=True)
        order_id = "R-DETAIL-HEAD-SIGN"
        self._create_receiving_status(order_id)
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="receiving",
            action="status",
            agency=self.agency,
            user=self.user,
            description="Акт приемки",
            payload={
                "act": "receiving",
                "act_label": "Акт приемки",
                "act_items": [{"sku_code": "SKU-R-BASE", "name": "Receiving Base", "actual_qty": 2}],
            },
        )
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="receiving",
            action="status",
            agency=self.agency,
            user=self.user,
            description="Акт размещения",
            payload={"act": "placement", "act_label": "Акт размещения", "act_state": "closed"},
        )

        self.client.logout()
        self.client.login(username="orders_receiving_head_manager", password="pwd")
        detail_response = self.client.get(f"/orders/receiving/{order_id}/")
        self.assertEqual(detail_response.status_code, 200)
        print_response = self.client.get(f"/orders/receiving/{order_id}/act/print/")
        self.assertEqual(print_response.status_code, 200)
        self.assertTrue(print_response.context["can_storekeeper_sign"])

        response = self.client.post(f"/orders/receiving/{order_id}/act/sign/storekeeper/")

        self.assertEqual(response.status_code, 302)
        entry = OrderAuditEntry.objects.filter(
            order_id=order_id,
            order_type="receiving",
            payload__act_storekeeper_signed=True,
        ).latest("created_at")
        self.assertEqual(entry.user, head_user)
        self.assertEqual(entry.description, "Акт приемки подписан начальником склада за кладовщика и отправлен менеджеру")
        self.assertEqual(entry.payload["act_storekeeper_employee_id"], head_employee.id)
        self.assertEqual(entry.payload["act_storekeeper_role"], "head_manager")
        self.assertEqual(entry.payload["act_storekeeper_name"], "Начальник склада")
        self.assertTrue(entry.payload["act_storekeeper_signed_by_head_manager"])
        self.assertEqual(
            _detail_history_actor_label(entry),
            "Начальник склада Начальник склада (за кладовщика)",
        )

    def test_receiving_detail_page_offers_storage_placement_after_flow_closed(self):
        order_id = "R-DETAIL-FLOW-CLOSED-1"
        self._create_receiving_status(order_id)
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="receiving",
            action="update",
            agency=self.agency,
            user=self.user,
            description="Поток закрыт",
            payload={
                "flow_closed": True,
                "flow_closed_at": timezone.localtime().isoformat(),
                "flow_boxes": [{"code": "BOX-R-CLOSED-1"}],
                "flow_pallets": [{"code": "PAL-R-CLOSED-1"}],
            },
        )
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="receiving",
            action="status",
            agency=self.agency,
            user=self.user,
            description="Акт приемки",
            payload={
                "act": "receiving",
                "act_label": "Акт приемки",
                "act_items": [],
            },
        )
        location = WarehouseWritePathService.ensure_location(warehouse_code="MSK", zone_code="PR")
        WarehouseStockSnapshot.objects.create(
            agency=self.agency,
            source_context_type="receiving",
            source_context_id=order_id,
            sku_code="SKU-R-CLOSED",
            name="Receiving Detail Closed",
            size="42",
            barcode="200000009211",
            goods_type="gv",
            qty=4,
            available_qty=4,
            container_code="PAL-R-CLOSED-1",
            location=location,
            zone_code=location.zone_code,
            zone_kind=location.zone_kind,
            warehouse_state_code="placed_in_receiving",
        )

        response = self.client.get(f"/orders/receiving/{order_id}/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["status_label"], "Завершена приемка")
        self.assertTrue(response.context["can_manage_storage_placement"])
        self.assertEqual(
            response.context["storage_placement_action_label"],
            "Создать размещение в хранение",
        )
        self.assertEqual(
            response.context["receiving_flow_url"],
            f"/orders/receiving/{order_id}/flow/",
        )
        self.assertEqual(response.context["reachtruck_request_url"], "")

    def test_receiving_detail_page_links_to_reachtruck_when_move_exists(self):
        order_id = "R-DETAIL-MOVE-1"
        self._create_receiving_status(order_id)
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="receiving",
            action="update",
            agency=self.agency,
            user=self.user,
            description="Поток закрыт",
            payload={
                "flow_closed": True,
                "flow_closed_at": timezone.localtime().isoformat(),
            },
        )
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="receiving",
            action="status",
            agency=self.agency,
            user=self.user,
            description="Акт приемки",
            payload={
                "act": "receiving",
                "act_label": "Акт приемки",
                "act_items": [],
            },
        )
        location = WarehouseWritePathService.ensure_location(warehouse_code="MSK", zone_code="PR")
        WarehouseStockSnapshot.objects.create(
            agency=self.agency,
            source_context_type="receiving",
            source_context_id=order_id,
            sku_code="SKU-R-MOVE",
            name="Receiving Detail Move",
            size="42",
            barcode="200000009210",
            goods_type="gv",
            qty=4,
            available_qty=4,
            container_code="PAL-R-M1",
            location=location,
            zone_code=location.zone_code,
            zone_kind=location.zone_kind,
            warehouse_state_code="placed_in_receiving",
        )
        move_request = MoveRequest.objects.create(
            context_type=MoveRequest.CONTEXT_RECEIVING,
            context_id=order_id,
            agency=self.agency,
            destination_zone="OS",
        )
        MoveTask.objects.create(
            request=move_request,
            pallet_code="PAL-R-M1",
            to_zone="OS",
            status=MoveTask.STATUS_CREATED,
            legacy_order_id="MOVE-R-DETAIL-1",
        )

        response = self.client.get(f"/orders/receiving/{order_id}/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["status_label"], "Размещение на складе")
        self.assertTrue(response.context["can_manage_storage_placement"])
        self.assertEqual(
            response.context["storage_placement_action_label"],
            "Контроль размещения в хранение",
        )
        self.assertEqual(
            response.context["reachtruck_request_url"],
            f"/reachtruck/?mobile_category=movement&mobile_request=receiving:{order_id}",
        )

    def test_receiving_detail_keeps_planned_items_after_flow_state_update(self):
        order_id = "4"
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="receiving",
            action="create",
            agency=self.agency,
            user=self.user,
            description="Создана заявка",
            payload={
                "status": "sent_unconfirmed",
                "status_label": "Ждет подтверждения",
                "items": [
                    {
                        "sku_code": "HANGER",
                        "name": "Вешалка для одежды",
                        "size": "0",
                        "qty": "200",
                    },
                    {
                        "sku_code": "ICEFORM",
                        "name": "Форма для льда",
                        "size": "0",
                        "qty": "89",
                    },
                ],
                "eta_at": "2026-05-08T12:00:00+03:00",
                "vehicle_number": "A123BC77",
            },
        )
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="receiving",
            action="update",
            agency=self.agency,
            user=self.user,
            description="Исправлен состав",
            payload={
                "items": [
                    {
                        "sku_code": "HANGER",
                        "name": "Вешалка для одежды",
                        "size": "0",
                        "qty": "200",
                    },
                    {
                        "sku_code": "ICEFORM",
                        "name": "Форма для льда",
                        "size": "0",
                        "qty": "80",
                    },
                ],
                "eta_at": "2026-05-08T12:00:00+03:00",
                "vehicle_number": "A123BC77",
            },
        )
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="receiving",
            action="status",
            agency=self.agency,
            user=self.user,
            description="Передано на склад",
            payload={
                "status": "warehouse",
                "status_label": "В ожидании поставки товара",
                "goods_type": "gv",
                "goods_type_label": "Готовый",
                "receiving_mode": "standard",
            },
        )
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="receiving",
            action="update",
            agency=self.agency,
            user=self.user,
            description="Состояние потока",
            payload={
                "flow_state": {
                    "boxes": [{"code": "BOX-4-1", "items": [], "sealed": False}],
                    "pallets": [{"code": "PAL-4-1", "boxes": ["BOX-4-1"], "items": [], "sealed": False}],
                    "activeBox": "BOX-4-1",
                    "activePallet": "PAL-4-1",
                }
            },
        )

        response = self.client.get(f"/orders/receiving/{order_id}/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["order_title"], "Заявка на приемку")
        self.assertEqual(len(response.context["items"]), 2)
        self.assertEqual(response.context["items"][1]["sku_code"], "ICEFORM")
        self.assertEqual(response.context["items"][1]["qty"], "80")

    def test_receiving_detail_recomputes_title_without_stale_mismatch_cache(self):
        order_id = "6"
        created_entry = OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="receiving",
            action="status",
            agency=self.agency,
            user=self.user,
            description="Статус приемки",
            payload={
                "status": "warehouse",
                "status_label": "В ожидании поставки товара",
                "goods_type": "op",
                "goods_type_label": "Оптовый",
                "items": [
                    {
                        "sku_code": "SKU-R-FIX",
                        "name": "Receiving Fixed",
                        "size": "42",
                        "qty": 5,
                    }
                ],
            },
        )
        clean_entry = OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="receiving",
            action="status",
            agency=self.agency,
            user=self.user,
            description="Создан акт приемки",
            payload={
                "status": "warehouse",
                "status_label": "Товар принят",
                "act": "receiving",
                "act_label": "Акт приемки",
                "act_mismatch": False,
                "act_items": [
                    {"sku_code": "SKU-R-FIX", "planned_qty": 5, "actual_qty": 5},
                ],
            },
        )
        OrderAuditEntry.objects.filter(pk=created_entry.pk).update(
            created_at=timezone.make_aware(datetime(2026, 5, 8, 10, 0))
        )
        stale_payload = dict(clean_entry.payload or {})
        stale_payload["order_title"] = "Заявка на приемку с расхождениями"
        stale_payload["order_display_title"] = "Заявка на приемку с расхождениями №6_PR"
        OrderAuditEntry.objects.filter(pk=clean_entry.pk).update(
            created_at=timezone.make_aware(datetime(2026, 5, 13, 12, 0)),
            payload=stale_payload,
        )

        response = self.client.get(f"/orders/receiving/{order_id}/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["order_title"], "Заявка на приемку")


class OrdersJournalWarehouseStatusTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="orders_journal_wh", password="pwd")
        Employee.objects.create(
            full_name="Кладовщик журнала",
            role="storekeeper",
            user=self.user,
            is_active=True,
        )
        Employee.objects.create(
            full_name="Руководитель обработки журнала",
            role="processing_head",
            is_active=True,
        )
        self.client.login(username="orders_journal_wh", password="pwd")
        self.agency = Agency.objects.create(agn_name="Клиент журнала")

    def _create_receiving_status(self, order_id: str, extra_payload: dict | None = None):
        payload = {
            "status": "warehouse",
            "status_label": "В ожидании поставки товара",
            "goods_type": "op",
            "goods_type_label": "Оптовый",
            "items": [
                {
                    "sku_code": "SKU-R-J-BASE",
                    "name": "Receiving Journal Base",
                    "brand": "Journal Brand",
                    "color": "Черный",
                    "size": "42",
                    "qty": 2,
                }
            ],
        }
        if extra_payload:
            payload.update(extra_payload)
        return OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="receiving",
            action="status",
            agency=self.agency,
            user=self.user,
            description="Статус приемки",
            payload=payload,
        )

    def test_journal_receiving_entry_prefers_warehouse_status(self):
        order_id = "R-JOURNAL-1"
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="receiving",
            action="status",
            agency=self.agency,
            user=self.user,
            description="Статус приемки",
            payload={
                "status": "warehouse",
                "status_label": "В ожидании поставки товара",
                "goods_type": "op",
                "items": [
                    {
                        "sku_code": "SKU-R-J",
                        "name": "Товар журнала приемки",
                        "size": "42",
                        "qty": 2,
                    }
                ],
            },
        )
        location = WarehouseWritePathService.ensure_location(warehouse_code="MSK", zone_code="PR")
        WarehouseStockSnapshot.objects.create(
            agency=self.agency,
            source_context_type="receiving",
            source_context_id=order_id,
            sku_code="SKU-R-J",
            name="Товар журнала приемки",
            size="42",
            barcode="200000009207",
            goods_type="gv",
            qty=2,
            available_qty=2,
            container_code="PAL-R-J1",
            location=location,
            zone_code=location.zone_code,
            zone_kind=location.zone_kind,
            warehouse_state_code="placed_in_receiving",
        )

        response = self.client.get("/orders/?tab=journal")

        self.assertEqual(response.status_code, 200)
        entries = list(response.context["entries"])
        row = next(item for item in entries if item["order_type"] == "receiving" and item["order_id"] == order_id)
        self.assertEqual(row["status_label"], "Завершена приемка")
        self.assertIn("кладовщика", row["action_label"].lower())
        self.assertEqual(row["next_step_label"], "Создать или завершить размещение в хранение")

    def test_journal_processing_entry_prefers_warehouse_status(self):
        order_id = "P-JOURNAL-1"
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="processing",
            action="status",
            agency=self.agency,
            user=self.user,
            description="Статус обработки",
            payload={
                "status": "processing_head",
                "status_label": "Передано в обработку",
                "article": "SKU-P-J",
                "product_name": "Товар журнала обработки",
            },
        )
        location = WarehouseWritePathService.ensure_location(warehouse_code="MSK", zone_code="OBR")
        snapshot = WarehouseStockSnapshot.objects.create(
            agency=self.agency,
            source_context_type="legacy_stock",
            source_context_id=f"legacy-{order_id}",
            sku_code="SKU-P-J",
            name="Товар журнала обработки",
            size="42",
            barcode="200000009208",
            goods_type="gv",
            qty=5,
            available_qty=0,
            processing_reserved_qty=5,
            container_code="PAL-P-J1",
            location=location,
            zone_code=location.zone_code,
            zone_kind=location.zone_kind,
            warehouse_state_code="processing_in_progress",
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
            qty_reserved=5,
            status=WarehouseReserve.STATUS_ACTIVE,
        )

        response = self.client.get("/orders/?tab=journal")

        self.assertEqual(response.status_code, 200)
        entries = list(response.context["entries"])
        row = next(item for item in entries if item["order_type"] == "processing" and item["order_id"] == order_id)
        self.assertEqual(row["status_label"], "утверждено менеджером")
        self.assertIn("руководителя обработки", row["action_label"].lower())
        self.assertEqual(row["next_step_label"], "Завершить обработку и подготовить размещение")

    def test_processing_head_can_access_receiving_section(self):
        head_user = get_user_model().objects.create_user(
            username="processing_head_receiving",
            password="pwd",
        )
        Employee.objects.create(
            full_name="Руководитель обработки с приемкой",
            role="processing_head",
            user=head_user,
            is_active=True,
        )
        self.client.force_login(head_user)

        receiving_response = self.client.get("/orders/receiving/")
        journal_response = self.client.get("/orders/?tab=journal")

        self.assertEqual(receiving_response.status_code, 200)
        self.assertEqual(journal_response.status_code, 200)

    def test_processing_head_can_open_receiving_detail_and_flow(self):
        order_id = "R-FLOW-PROCESSING-HEAD-1"
        self._create_receiving_status(order_id)
        head_user = get_user_model().objects.create_user(
            username="processing_head_receiving_flow",
            password="pwd",
        )
        Employee.objects.create(
            full_name="Руководитель обработки в приемке",
            role="processing_head",
            user=head_user,
            is_active=True,
        )
        self.client.force_login(head_user)

        detail_response = self.client.get(f"/orders/receiving/{order_id}/")
        flow_response = self.client.get(f"/orders/receiving/{order_id}/flow/")

        self.assertEqual(detail_response.status_code, 200)
        self.assertEqual(flow_response.status_code, 200)

    def test_processing_head_can_access_receiving_attachment(self):
        head_user = get_user_model().objects.create_user(
            username="processing_head_receiving_attachment",
            password="pwd",
        )
        Employee.objects.create(
            full_name="Руководитель обработки с файлами приемки",
            role="processing_head",
            user=head_user,
            is_active=True,
        )
        request = RequestFactory().get(
            "/orders/receiving/R-ATTACH-PROCESSING-HEAD/attachments/1/"
        )
        request.user = head_user
        request.session = {}
        attachment = SimpleNamespace(agency_id=self.agency.id)

        self.assertTrue(_can_access_receiving_attachment(request, attachment))

    def test_receiving_flow_page_prefers_warehouse_status(self):
        order_id = "R-FLOW-WH-1"
        self._create_receiving_status(order_id)
        location = WarehouseWritePathService.ensure_location(warehouse_code="MSK", zone_code="PR")
        WarehouseStockSnapshot.objects.create(
            agency=self.agency,
            source_context_type="receiving",
            source_context_id=order_id,
            sku_code="SKU-R-FLOW",
            name="Receiving Flow",
            size="42",
            barcode="200000009205",
            goods_type="gv",
            qty=3,
            available_qty=3,
            container_code="PAL-R-F1",
            location=location,
            zone_code=location.zone_code,
            zone_kind=location.zone_kind,
            warehouse_state_code="placed_in_receiving",
        )

        response = self.client.get(f"/orders/receiving/{order_id}/flow/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["status_label"], "Завершена приемка")

    def test_receiving_flow_page_contains_client_sku_link(self):
        order_id = "R-FLOW-CLIENT-SKU-1"
        self._create_receiving_status(order_id)

        response = self.client.get(f"/orders/receiving/{order_id}/flow/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["current_agency_id"], self.agency.id)
        self.assertContains(response, "Номенклатура клиента")

    def test_receiving_flow_serializes_and_deduplicates_agent_scan_events(self):
        order_id = "R-FLOW-AGENT-DEDUPE-1"
        self._create_receiving_status(order_id)

        response = self.client.get(f"/orders/receiving/{order_id}/flow/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "const processedAgentEventIds = new Set();", html=False)
        self.assertContains(response, "const queuedAgentEventIds = new Set();", html=False)
        self.assertContains(response, "let agentEventQueue = Promise.resolve();", html=False)
        self.assertContains(response, "processedAgentEventIds.has(eventId)", html=False)
        self.assertContains(response, "agentEventQueue = agentEventQueue", html=False)
        self.assertContains(response, "handleAgentScanEvent(evt);", html=False)

    def test_receiving_flow_page_contains_required_services_modal_for_warehouse(self):
        order_id = "R-FLOW-SERVICES-MODAL-1"
        self._create_receiving_status(order_id)

        response = self.client.get(f"/orders/receiving/{order_id}/flow/")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["can_capture_receiving_services"])
        self.assertContains(response, 'id="warehouse-services-modal"', html=False)
        self.assertContains(response, 'data-order-type="receiving"', html=False)
        self.assertContains(response, "Сохранить услуги и завершить")

    def test_receiving_flow_page_uses_client_prefix_for_stockmap_picker(self):
        order_id = "R-FLOW-CLIENT-PREFIX-1"
        self._create_receiving_status(order_id)

        response = self.client.get(f"/orders/receiving/{order_id}/flow/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'const currentClientMarker = clientPrefixRaw ? clientPrefixRaw.toUpperCase() : \'\';', html=False)
        self.assertContains(response, "client_marker: currentClientMarker,", html=False)
        self.assertContains(response, "url.searchParams.set('client_marker', String(currentClientMarker));", html=False)

    def test_receiving_flow_page_contains_goods_type_change_button(self):
        order_id = "R-FLOW-GOODS-TYPE-BTN-1"
        self._create_receiving_status(
            order_id,
            {
                "goods_type": "gv",
                "goods_type_label": "Готовая продукция",
                "receiving_mode": "standard",
            },
        )

        response = self.client.get(f"/orders/receiving/{order_id}/flow/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="change-goods-type-toggle"', html=False)
        self.assertContains(response, 'id="change-goods-type-form"', html=False)
        self.assertContains(response, 'id="change-receiving-mode-select"', html=False)
        self.assertContains(response, "Изменить тип/режим")

    def test_receiving_flow_page_contains_pallet_close_count_modal(self):
        order_id = "R-FLOW-PALLET-CLOSE-1"
        self._create_receiving_status(order_id)

        response = self.client.get(f"/orders/receiving/{order_id}/flow/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="pallet-close-count-modal"', html=False)
        self.assertContains(response, 'id="pallet-close-count-form"', html=False)
        self.assertContains(response, "openPalletCloseConfirm", html=False)
        self.assertContains(response, "Закрыть паллету")
        self.assertContains(response, "Сколько коробов в паллете?")

    def test_receiving_flow_close_pallet_prints_box_labels(self):
        order_id = "R-FLOW-PALLET-CLOSE-PRINT-BOXES"
        self._create_receiving_status(order_id)

        response = self.client.get(f"/orders/receiving/{order_id}/flow/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "closedPalletBoxCodes", html=False)
        self.assertContains(response, "enqueueReceivingBoxLabels(closedPalletBoxCodes);", html=False)
        self.assertContains(response, "supportsBatchPrintJobs", html=False)
        self.assertContains(response, "isAgentVersionAtLeast(localAgent.version, 1, 0, 54)", html=False)
        self.assertContains(response, "label_png_base64: labelImages[index] || '',", html=False)
        self.assertContains(response, "label_png_base64_list: labelImages,", html=False)
        self.assertNotContains(response, "enqueueReceivingPalletLabel(closedPalletCode);", html=False)

    def test_receiving_flow_post_updates_goods_type(self):
        order_id = "R-FLOW-GOODS-TYPE-POST-1"
        self._create_receiving_status(
            order_id,
            {
                "goods_type": "gv",
                "goods_type_label": "Готовая продукция",
                "receiving_mode": "standard",
            },
        )

        response = self.client.post(
            f"/orders/receiving/{order_id}/flow/",
            {
                "flow_action": "set_goods_type",
                "goods_type": "op",
                "receiving_mode": "standard",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertIn("goods_type_updated=1", response.url)
        latest = OrderAuditEntry.objects.filter(order_id=order_id, order_type="receiving").latest("id")
        self.assertEqual((latest.payload or {}).get("goods_type"), "op")
        self.assertEqual((latest.payload or {}).get("goods_type_label"), "Оптовый")
        self.assertEqual((latest.payload or {}).get("receiving_mode"), "standard")

    def test_client_receiving_page_uses_moscow_time_for_default_eta(self):
        client_user = get_user_model().objects.create_user(username="client_receiving_page_eta", password="pwd")
        client_agency = Agency.objects.create(agn_name="Клиент ETA Page", portal_user=client_user)
        self.client.force_login(client_user)

        response = self.client.get(f"/orders/receiving/?client={client_agency.id}")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(str(response.context["current_time"].tzinfo), "Europe/Moscow")
        current_time_input = response.context["current_time_input"]
        default_eta_input = response.context["default_receiving_eta_input"]
        utc_render = timezone.localtime(response.context["current_time"]).strftime("%Y-%m-%dT%H:%M")
        self.assertContains(response, f'data-now="{current_time_input}"')
        self.assertContains(response, f'data-default-value="{default_eta_input}"')
        self.assertContains(response, "Состав поставки можно не указывать")
        self.assertContains(response, 'name="receiving_chz_service"', html=False)
        self.assertContains(response, "Требуется — сканировать Data Matrix")
        self.assertContains(response, "Не требуется — обычная приёмка")
        self.assertNotContains(response, "<th>Артикул *</th>", html=False)
        self.assertContains(response, "Скачать шаблон приёмки")
        self.assertContains(response, "Загрузить заполненный шаблон")
        self.assertContains(response, 'name="template_receiving_file"', html=False)
        self.assertContains(response, 'href="/orders/templates/receiving/"', html=False)
        self.assertNotContains(response, "Номенклатура клиента")
        self.assertNotContains(response, 'name="template_sku_file"', html=False)
        self.assertNotContains(response, "/orders/templates/sku-upload/", html=False)
        template_response = self.client.get("/orders/templates/receiving/")
        self.assertEqual(template_response.status_code, 200)
        self.assertIn("attachment", template_response.get("Content-Disposition", ""))
        self.assertIn(".xlsx", template_response.get("Content-Disposition", ""))
        if utc_render != current_time_input:
            self.assertNotContains(response, f'data-now="{utc_render}"')

    def test_client_receiving_past_eta_shows_validation_instead_of_500(self):
        client_user = get_user_model().objects.create_user(username="client_receiving_past_eta", password="pwd")
        client_agency = Agency.objects.create(agn_name="Клиент Past ETA", portal_user=client_user)
        self.client.force_login(client_user)

        response = self.client.post(
            f"/orders/receiving/?client={client_agency.id}",
            {
                "submit_action": "send",
                "agency_id": str(client_agency.id),
                "eta_at": "2000-01-01T12:00",
                "expected_boxes": "3",
                "place_type": "box",
                "receiving_chz_service": "required",
                "vehicle_number": "A123BC77",
                "driver_phone": "+79000000000",
                "sku_code[]": [""],
                "sku_id[]": [""],
                "item_name[]": [""],
                "size[]": [""],
                "qty[]": [""],
                "position_comment[]": [""],
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Плановая дата/время не может быть в прошлом.")
        self.assertFalse(OrderAuditEntry.objects.filter(order_type="receiving", agency=client_agency).exists())

    def test_client_receiving_send_requires_chz_choice_without_stock_writes(self):
        client_user = get_user_model().objects.create_user(
            username="client_receiving_chz_required",
            password="pwd",
        )
        client_agency = Agency.objects.create(
            agn_name="Клиент выбора ЧЗ",
            portal_user=client_user,
        )
        self.client.force_login(client_user)
        stock_counts = (
            WarehouseStockSnapshot.objects.count(),
            WarehouseReserve.objects.count(),
            WarehouseContainer.objects.count(),
        )

        response = self.client.post(
            f"/orders/receiving/?client={client_agency.id}",
            {
                "submit_action": "send",
                "agency_id": str(client_agency.id),
                "eta_at": "2099-04-23T12:00",
                "expected_boxes": "3",
                "place_type": "box",
                "vehicle_number": "A123BC77",
                "driver_phone": "+79000000000",
                "sku_code[]": [""],
                "sku_id[]": [""],
                "item_name[]": [""],
                "size[]": [""],
                "qty[]": [""],
                "position_comment[]": [""],
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Укажите, требуется ли сканирование Честного знака.")
        self.assertFalse(
            OrderAuditEntry.objects.filter(
                order_type="receiving",
                agency=client_agency,
            ).exists()
        )
        self.assertEqual(
            (
                WarehouseStockSnapshot.objects.count(),
                WarehouseReserve.objects.count(),
                WarehouseContainer.objects.count(),
            ),
            stock_counts,
        )

    def test_client_receiving_submit_stores_eta_in_moscow_timezone(self):
        client_user = get_user_model().objects.create_user(username="client_receiving_eta", password="pwd")
        client_agency = Agency.objects.create(agn_name="Клиент ETA", portal_user=client_user)
        Employee.objects.create(full_name="Менеджер ETA", role="manager", is_active=True)
        self.client.force_login(client_user)

        response = self.client.post(
            f"/orders/receiving/?client={client_agency.id}",
            {
                "submit_action": "send",
                "agency_id": str(client_agency.id),
                "eta_at": "2099-04-23T12:00",
                "expected_boxes": "3",
                "place_type": "box",
                "receiving_chz_service": "not_required",
                "vehicle_number": "A123BC77",
                "driver_phone": "+79000000000",
                "sku_code[]": [""],
                "sku_id[]": [""],
                "item_name[]": [""],
                "size[]": [""],
                "qty[]": [""],
                "position_comment[]": [""],
            },
        )

        self.assertEqual(response.status_code, 302)
        entry = OrderAuditEntry.objects.filter(order_type="receiving", agency=client_agency).latest("id")
        self.assertEqual(entry.payload.get("eta_at"), "2099-04-23T12:00:00+03:00")
        self.assertEqual(entry.payload.get("receiving_chz_service"), "not_required")

    def test_client_receiving_submit_normalizes_driver_phone(self):
        client_user = get_user_model().objects.create_user(username="client_receiving_phone", password="pwd")
        client_agency = Agency.objects.create(agn_name="Клиент Phone", portal_user=client_user)
        Employee.objects.create(full_name="Менеджер Phone", role="manager", is_active=True)
        self.client.force_login(client_user)

        response = self.client.post(
            f"/orders/receiving/?client={client_agency.id}",
            {
                "submit_action": "send",
                "agency_id": str(client_agency.id),
                "eta_at": "2099-04-23T12:00",
                "expected_boxes": "3",
                "place_type": "box",
                "receiving_chz_service": "required",
                "vehicle_number": "A123BC77",
                "driver_phone": "9000000000",
                "sku_code[]": [""],
                "sku_id[]": [""],
                "item_name[]": [""],
                "size[]": [""],
                "qty[]": [""],
                "position_comment[]": [""],
            },
        )

        self.assertEqual(response.status_code, 302)
        entry = OrderAuditEntry.objects.filter(order_type="receiving", agency=client_agency).latest("id")
        self.assertEqual(entry.payload.get("driver_phone"), "+79000000000")

    def test_client_receiving_submit_creates_primary_nomenclature_for_new_item(self):
        client_user = get_user_model().objects.create_user(username="client_receiving_sku", password="pwd")
        client_agency = Agency.objects.create(agn_name="Клиент SKU", portal_user=client_user)
        Employee.objects.create(full_name="Менеджер SKU", role="manager", is_active=True)
        self.client.force_login(client_user)

        response = self.client.post(
            f"/orders/receiving/?client={client_agency.id}",
            {
                "submit_action": "send",
                "agency_id": str(client_agency.id),
                "eta_at": "2099-04-23T12:00",
                "expected_boxes": "3",
                "place_type": "box",
                "receiving_chz_service": "not_required",
                "vehicle_number": "A123BC77",
                "driver_phone": "+79000000000",
                "sku_code[]": ["ART-CLI-1"],
                "sku_id[]": [""],
                "item_name[]": ["Футболка"],
                "brand[]": ["Nike"],
                "color[]": ["Белый"],
                "size[]": ["L"],
                "barcode[]": ["CLI-BAR-001"],
                "qty[]": ["3"],
                "position_comment[]": [""],
            },
        )

        self.assertEqual(response.status_code, 302)
        created_sku = SKU.objects.get(agency=client_agency, sku_code="ART-CLI-1", deleted=False)
        created_barcode = SKUBarcode.objects.get(sku=created_sku, size="L")
        self.assertEqual(created_sku.name, "Футболка")
        self.assertEqual(created_sku.brand, "Nike")
        self.assertEqual(created_sku.color, "Белый")
        self.assertEqual(created_barcode.value, "CLI-BAR-001")
        entry = OrderAuditEntry.objects.filter(order_type="receiving", agency=client_agency).latest("id")
        payload_item = (entry.payload.get("items") or [])[0]
        self.assertEqual(payload_item.get("sku_id"), created_sku.id)
        self.assertEqual(payload_item.get("barcode"), "CLI-BAR-001")

    def test_receiving_flow_page_shows_storage_placement_after_reachtruck_task_created(self):
        order_id = "R-FLOW-WH-2"
        self._create_receiving_status(order_id)
        location = WarehouseWritePathService.ensure_location(warehouse_code="MSK", zone_code="PR")
        WarehouseStockSnapshot.objects.create(
            agency=self.agency,
            source_context_type="receiving",
            source_context_id=order_id,
            sku_code="SKU-R-FLOW-2",
            name="Receiving Flow Reachtruck",
            size="42",
            barcode="200000009215",
            goods_type="gv",
            qty=3,
            available_qty=3,
            container_code="PAL-R-F2",
            location=location,
            zone_code=location.zone_code,
            zone_kind=location.zone_kind,
            warehouse_state_code="placed_in_receiving",
        )
        move_request = MoveRequest.objects.create(
            context_type=MoveRequest.CONTEXT_RECEIVING,
            context_id=order_id,
            agency=self.agency,
            destination_zone="OS",
        )
        MoveTask.objects.create(
            request=move_request,
            pallet_code="PAL-R-F2",
            to_zone="OS",
            status=MoveTask.STATUS_CREATED,
            legacy_order_id="MOVE-R-FLOW-2",
        )

        response = self.client.get(f"/orders/receiving/{order_id}/flow/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["status_label"], "Размещение на складе")

    def test_receiving_flow_page_contains_separate_pallet_label_template(self):
        order_id = "R-FLOW-LABELS-1"
        self._create_receiving_status(order_id)

        response = self.client.get(f"/orders/receiving/{order_id}/flow/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="receiving-pallet-label-template"', html=False)
        self.assertContains(response, 'data-label-type="pallet"', html=False)

    def test_receiving_flow_page_contains_stockmap_picker_for_warehouse_move(self):
        order_id = "R-FLOW-STOCKMAP-1"
        self._create_receiving_status(order_id)

        response = self.client.get(f"/orders/receiving/{order_id}/flow/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "warehouse-move-picker-btn", html=False)
        self.assertContains(response, "stockmap_location_pick", html=False)
        self.assertContains(response, "openWarehouseStockMapPicker", html=False)

    def test_receiving_act_page_prefers_warehouse_status(self):
        order_id = "R-ACT-WH-1"
        self._create_receiving_status(order_id)
        location = WarehouseWritePathService.ensure_location(warehouse_code="MSK", zone_code="PR")
        WarehouseStockSnapshot.objects.create(
            agency=self.agency,
            source_context_type="receiving",
            source_context_id=order_id,
            sku_code="SKU-R-ACT",
            name="Receiving Act",
            size="42",
            barcode="200000009206",
            goods_type="gv",
            qty=2,
            available_qty=2,
            container_code="PAL-R-A1",
            location=location,
            zone_code=location.zone_code,
            zone_kind=location.zone_kind,
            warehouse_state_code="placed_in_receiving",
        )

        response = self.client.get(f"/orders/receiving/{order_id}/act/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["status_label"], "Завершена приемка")
        self.assertTrue(response.context["can_submit"])
        self.assertTrue(response.context["can_add_items"])

    def test_receiving_act_page_hides_submit_after_storage(self):
        order_id = "R-ACT-STORED-1"
        self._create_receiving_status(order_id)
        location = WarehouseWritePathService.ensure_location(warehouse_code="MSK", zone_code="OS")
        WarehouseStockSnapshot.objects.create(
            agency=self.agency,
            source_context_type="receiving",
            source_context_id=order_id,
            sku_code="SKU-R-ACT-ST",
            name="Receiving Act Stored",
            size="42",
            barcode="200000009210",
            goods_type="gv",
            qty=2,
            available_qty=2,
            container_code="PAL-R-AS1",
            location=location,
            zone_code=location.zone_code,
            zone_kind=location.zone_kind,
            warehouse_state_code="stored",
        )

        response = self.client.get(f"/orders/receiving/{order_id}/act/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["status_label"], "Товар принят и размещен на складе")
        self.assertFalse(response.context["can_submit"])
        self.assertFalse(response.context["can_add_items"])

    def test_receiving_placement_page_hides_reopen_after_storage(self):
        order_id = "R-PLACEMENT-STORED-1"
        self._create_receiving_status(order_id)
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="receiving",
            action="status",
            agency=self.agency,
            user=self.user,
            description="Акт размещения",
            payload={
                "act": "placement",
                "act_label": "Акт размещения",
                "act_state": "closed",
                "goods_type": "op",
                "goods_type_label": "Оптовый",
                "act_items": [
                    {
                        "sku_code": "SKU-R-PL-ST",
                        "name": "Receiving Placement Stored",
                        "size": "42",
                        "actual_qty": 2,
                    }
                ],
                "act_boxes": [],
                "act_pallets": [],
            },
        )
        location = WarehouseWritePathService.ensure_location(warehouse_code="MSK", zone_code="OS")
        WarehouseStockSnapshot.objects.create(
            agency=self.agency,
            source_context_type="receiving",
            source_context_id=order_id,
            sku_code="SKU-R-PL-ST",
            name="Receiving Placement Stored",
            size="42",
            barcode="200000009211",
            goods_type="gv",
            qty=2,
            available_qty=2,
            container_code="PAL-R-PS1",
            location=location,
            zone_code=location.zone_code,
            zone_kind=location.zone_kind,
            warehouse_state_code="stored",
        )

        response = self.client.get(f"/orders/receiving/{order_id}/placement/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["status_label"], "Товар принят и размещен на складе")
        self.assertFalse(response.context["can_open_act"])

    def test_receiving_placement_post_is_rejected_after_storage(self):
        order_id = "R-PLACEMENT-POST-STORED-1"
        self._create_receiving_status(order_id)
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="receiving",
            action="status",
            agency=self.agency,
            user=self.user,
            description="Акт приемки",
            payload={
                "act": "receiving",
                "act_label": "Акт приемки",
                "act_state": "closed",
                "act_items": [
                    {
                        "sku_code": "SKU-R-POST-ST",
                        "name": "Receiving Placement Post Stored",
                        "size": "42",
                        "actual_qty": 2,
                    }
                ],
            },
        )
        location = WarehouseWritePathService.ensure_location(warehouse_code="MSK", zone_code="OS")
        WarehouseStockSnapshot.objects.create(
            agency=self.agency,
            source_context_type="receiving",
            source_context_id=order_id,
            sku_code="SKU-R-POST-ST",
            name="Receiving Placement Post Stored",
            size="42",
            barcode="200000009214",
            goods_type="gv",
            qty=2,
            available_qty=2,
            container_code="PAL-R-PST1",
            location=location,
            zone_code=location.zone_code,
            zone_kind=location.zone_kind,
            warehouse_state_code="stored",
        )

        response = self.client.post(
            f"/orders/receiving/{order_id}/placement/",
            {
                "boxes_json": (
                    '[{"code":"BOX-R-PST-1","items":[{"sku":"SKU-R-POST-ST","name":"Receiving Placement Post Stored",'
                    '"size":"42","qty":2}],"sealed":true}]'
                ),
                "pallets_json": (
                    '[{"code":"PAL-R-PST-1","boxes":["BOX-R-PST-1"],"items":[],"sealed":true,'
                    '"location":{"zone":"OS","row":1,"section":1,"tier":1,"cell":1}}]'
                ),
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertIn("/orders/receiving/", response.url)
        self.assertIn("?error=1", response.url)

    def test_receiving_flow_hides_move_action_after_storage(self):
        order_id = "R-FLOW-STORED-1"
        self._create_receiving_status(order_id)
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="receiving",
            action="update",
            agency=self.agency,
            user=self.user,
            description="Поток закрыт",
            payload={
                "flow_closed": True,
                "flow_closed_at": timezone.localtime().isoformat(),
            },
        )
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="receiving",
            action="status",
            agency=self.agency,
            user=self.user,
            description="Акт размещения",
            payload={
                "act": "placement",
                "act_label": "Акт размещения",
                "act_state": "closed",
                "act_pallets": [
                    {
                        "code": "PAL-R-FS-1",
                        "boxes": [],
                        "items": [],
                        "sealed": True,
                        "location": {"zone": "OS"},
                    }
                ],
            },
        )
        location = WarehouseWritePathService.ensure_location(warehouse_code="MSK", zone_code="OS")
        WarehouseStockSnapshot.objects.create(
            agency=self.agency,
            source_context_type="receiving",
            source_context_id=order_id,
            sku_code="SKU-R-FLOW-ST",
            name="Receiving Flow Stored",
            size="42",
            barcode="200000009212",
            goods_type="gv",
            qty=2,
            available_qty=2,
            container_code="PAL-R-FS-1",
            location=location,
            zone_code=location.zone_code,
            zone_kind=location.zone_kind,
            warehouse_state_code="stored",
        )

        response = self.client.get(f"/orders/receiving/{order_id}/flow/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["status_label"], "Товар принят и размещен на складе")
        self.assertFalse(response.context["can_send_to_warehouse_action"])

    def test_receiving_flow_item_weight_endpoint_updates_sku_weight(self):
        order_id = "R-FLOW-WEIGHT-1"
        sku = SKU.objects.create(
            agency=self.agency,
            sku_code="SKU-R-WEIGHT",
            name="Receiving Weight",
            size="43",
        )
        self._create_receiving_status(
            order_id,
            {
                "items": [
                    {
                        "sku_code": sku.sku_code,
                        "name": sku.name,
                        "size": sku.size,
                        "qty": 3,
                    }
                ]
            },
        )

        response = self.client.post(
            f"/orders/receiving/{order_id}/flow/item-weight/",
            data=json.dumps({"sku_code": sku.sku_code, "weight_kg": "0.245"}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertJSONEqual(
            response.content,
            {
                "ok": True,
                "sku_code": sku.sku_code,
                "weight_kg": "0.245",
            },
        )
        sku.refresh_from_db()
        self.assertEqual(str(sku.weight_kg), "0.245")
        self.assertEqual(str(sku.weight_gross_kg), "0.245")

    def test_receiving_flow_rejects_draft_save_after_storage(self):
        order_id = "R-FLOW-DRAFT-STORED-1"
        self._create_receiving_status(order_id)
        location = WarehouseWritePathService.ensure_location(warehouse_code="MSK", zone_code="OS")
        WarehouseStockSnapshot.objects.create(
            agency=self.agency,
            source_context_type="receiving",
            source_context_id=order_id,
            sku_code="SKU-R-FLOW-DR",
            name="Receiving Flow Draft Stored",
            size="42",
            barcode="200000009213",
            goods_type="gv",
            qty=2,
            available_qty=2,
            container_code="PAL-R-FD-1",
            location=location,
            zone_code=location.zone_code,
            zone_kind=location.zone_kind,
            warehouse_state_code="stored",
        )

        response = self.client.post(
            f"/orders/receiving/{order_id}/flow/",
            {
                "flow_action": "draft",
                "boxes_json": "[]",
                "pallets_json": "[]",
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertJSONEqual(response.content, {"ok": False, "error": "not_allowed"})


class ContainerCodeEndpointTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.receiving_user = user_model.objects.create_user(username="receiving_code_user", password="pwd")
        self.processing_user = user_model.objects.create_user(username="processing_code_user", password="pwd")
        self.shipping_user = user_model.objects.create_user(username="shipping_code_user", password="pwd")
        Employee.objects.create(
            full_name="Приемщик кода",
            role="storekeeper",
            user=self.receiving_user,
            is_active=True,
        )
        Employee.objects.create(
            full_name="Обработка кода",
            role="processing_head",
            user=self.processing_user,
            is_active=True,
        )
        Employee.objects.create(
            full_name="Отгрузка кода",
            role="storekeeper",
            user=self.shipping_user,
            is_active=True,
        )
        self.agency = Agency.objects.create(agn_name="Клиент кодов", pref="abc")
        OrderAuditEntry.objects.create(
            order_id="1",
            order_type="receiving",
            action="status",
            agency=self.agency,
            user=self.receiving_user,
            description="Приемка для теста кодов",
            payload={"goods_type": "gv"},
        )
        OrderAuditEntry.objects.create(
            order_id="2",
            order_type="processing",
            action="status",
            agency=self.agency,
            user=self.processing_user,
            description="Обработка для теста кодов",
            payload={"goods_type": "op"},
        )
        self.shipping_order = ShippingOrder.objects.create(
            number="SO-900001",
            agency=self.agency,
            created_by=self.shipping_user,
            status=ShippingOrder.STATUS_PICKING,
        )

    def test_receiving_processing_and_shipping_use_process_specific_pallet_codes(self):
        self.client.force_login(self.receiving_user)
        response = self.client.post(
            "/orders/receiving/1/flow/container-code/",
            data=json.dumps({"goods_type": "gv"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        receiving_payload = response.json()
        self.assertTrue(receiving_payload["ok"])
        self.assertEqual(receiving_payload["digits"], "000001")
        self.assertTrue(receiving_payload["code"].startswith("ABC-"))
        self.assertTrue(receiving_payload["code"].endswith("-gv"))

        self.client.force_login(self.processing_user)
        response = self.client.post(
            "/orders/processing/2/flow/container-code/",
            data=json.dumps({"goods_type": "op"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        processing_payload = response.json()
        self.assertTrue(processing_payload["ok"])
        self.assertEqual(processing_payload["digits"], "000002")
        self.assertTrue(processing_payload["code"].endswith("-op"))

        self.client.force_login(self.shipping_user)
        response = self.client.post(
            f"/shipping/{self.shipping_order.pk}/packing/loose/container-code/",
            data=json.dumps({"goods_type": ""}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        shipping_payload = response.json()
        self.assertTrue(shipping_payload["ok"])
        self.assertEqual(shipping_payload["digits"], "000003")
        self.assertTrue(shipping_payload["code"].endswith("-na"))

        self.client.force_login(self.receiving_user)
        response = self.client.post(
            "/orders/receiving/1/flow/container-code/",
            data=json.dumps({"goods_type": "br", "kind": "pallet"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        receiving_pallet_payload = response.json()
        self.assertTrue(receiving_pallet_payload["ok"])
        self.assertEqual(receiving_pallet_payload["digits"], "0000001")
        self.assertEqual(receiving_pallet_payload["order_part"], "1PR")
        self.assertEqual(receiving_pallet_payload["code"], "ABC-1PR-0000001-br")

        self.client.force_login(self.processing_user)
        response = self.client.post(
            "/orders/processing/2/flow/container-code/",
            data=json.dumps({"goods_type": "op", "kind": "pallet"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        processing_pallet_payload = response.json()
        self.assertTrue(processing_pallet_payload["ok"])
        self.assertEqual(processing_pallet_payload["digits"], "0000002")
        self.assertEqual(processing_pallet_payload["order_part"], "2OBR")
        self.assertEqual(processing_pallet_payload["code"], "ABC-2OBR-0000002-op")

        self.client.force_login(self.shipping_user)
        response = self.client.post(
            f"/shipping/{self.shipping_order.pk}/packing/loose/container-code/",
            data=json.dumps({"goods_type": "gv", "kind": "pallet"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        shipping_pallet_payload = response.json()
        self.assertTrue(shipping_pallet_payload["ok"])
        self.assertEqual(shipping_pallet_payload["digits"], "0000003")
        self.assertEqual(shipping_pallet_payload["order_part"], "900001OTG")
        self.assertEqual(shipping_pallet_payload["code"], "ABC-900001OTG-0000003-gv")


class OrdersReceivingDraftAutosaveTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="receiving_draft_manager", password="pwd")
        Employee.objects.create(
            full_name="Менеджер черновиков приёмки",
            role="manager",
            user=self.user,
            is_active=True,
        )
        self.agency = Agency.objects.create(agn_name="Клиент автосохранения приёмки")
        self.client.force_login(self.user)

    def test_manager_receiving_draft_autosave_creates_draft(self):
        response = self.client.post(
            "/orders/receiving/",
            {
                "draft_autosave": "1",
                "agency_id": str(self.agency.id),
                "comment": "Черновик приёмки",
                "sku_code[]": [""],
                "sku_id[]": [""],
                "item_name[]": [""],
                "size[]": [""],
                "qty[]": [""],
                "position_comment[]": [""],
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["status"], "draft")
        self.assertTrue(data["order_id"])
        entry = OrderAuditEntry.objects.filter(
            order_id=data["order_id"],
            order_type="receiving",
        ).latest("id")
        self.assertEqual((entry.payload or {}).get("status"), "draft")
        self.assertEqual((entry.payload or {}).get("comment"), "Черновик приёмки")

    def test_manager_receiving_draft_autosave_updates_existing_draft(self):
        create_response = self.client.post(
            "/orders/receiving/",
            {
                "draft_autosave": "1",
                "agency_id": str(self.agency.id),
                "comment": "Первый",
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        order_id = create_response.json()["order_id"]

        response = self.client.post(
            f"/orders/receiving/?edit={order_id}",
            {
                "draft_autosave": "1",
                "agency_id": str(self.agency.id),
                "edit_order_id": order_id,
                "comment": "Второй",
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["order_id"], order_id)
        entry = OrderAuditEntry.objects.filter(
            order_id=order_id,
            order_type="receiving",
        ).latest("id")
        self.assertEqual((entry.payload or {}).get("status"), "draft")
        self.assertEqual((entry.payload or {}).get("comment"), "Второй")

    def test_manager_can_submit_client_receiving_draft_from_edit_page(self):
        order_id = "R-DRAFT-MANAGER-SEND"
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="receiving",
            action="status",
            agency=self.agency,
            user=self.user,
            description="Черновик приемки",
            payload={
                "status": "draft",
                "status_label": "Черновик",
                "submit_action": "draft",
                "eta_at": "2099-04-23T12:00:00+03:00",
                "expected_boxes": "2",
                "place_type": "box",
                "receiving_chz_service": "not_required",
                "vehicle_number": "A123BC77",
                "driver_phone": "+79000000000",
                "items": [],
            },
        )

        response = self.client.get(f"/orders/receiving/?client={self.agency.id}&edit={order_id}")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["edit_is_draft"])
        self.assertContains(response, 'name="submit_action" value="send"', html=False)
        self.assertContains(response, "Отправить")

        response = self.client.post(
            f"/orders/receiving/?client={self.agency.id}&edit={order_id}",
            {
                "submit_action": "send",
                "agency_id": str(self.agency.id),
                "edit_order_id": order_id,
                "eta_at": "2099-04-23T12:00",
                "expected_boxes": "2",
                "place_type": "box",
                "receiving_chz_service": "not_required",
                "vehicle_number": "A123BC77",
                "driver_phone": "+79000000000",
                "sku_code[]": [""],
                "sku_id[]": [""],
                "item_name[]": [""],
                "size[]": [""],
                "qty[]": [""],
                "position_comment[]": [""],
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertFalse(OrderAuditEntry.objects.filter(order_id=order_id, order_type="receiving").exists())
        latest = OrderAuditEntry.objects.filter(order_id="PR-000122", order_type="receiving").latest("id")
        self.assertEqual((latest.payload or {}).get("status"), "sent_unconfirmed")
        self.assertEqual((latest.payload or {}).get("status_label"), "Ждет подтверждения")
class OrdersFacsimileEmbeddingTests(TestCase):
    def setUp(self):
        from io import BytesIO
        from tempfile import TemporaryDirectory
        from django.core.files.base import ContentFile
        from django.test import override_settings
        from PIL import Image
        self._media_dir = TemporaryDirectory()
        self._media_override = override_settings(MEDIA_ROOT=self._media_dir.name); self._media_override.enable()
        self.employee = Employee.objects.create(full_name="Подпись для акта", role="storekeeper", is_active=True)
        signature = BytesIO(); Image.new("RGBA", (24, 12), (248, 144, 0, 255)).save(signature, format="PNG")
        self.employee.facsimile.save("act-embedded-sign.png", ContentFile(signature.getvalue()), save=True)
    def tearDown(self):
        self._media_override.disable(); self._media_dir.cleanup(); super().tearDown()
    def test_receiving_facsimile_is_embedded_without_raw_media_url(self):
        import base64
        from orders.web_ui import _facsimile_url
        embedded = _facsimile_url(self.employee)
        self.assertTrue(embedded.startswith("data:image/png;base64,")); self.assertNotIn("/media/", embedded)
        self.assertTrue(base64.b64decode(embedded.split(",", 1)[1]).startswith(b"\x89PNG"))
