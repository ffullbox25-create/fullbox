from datetime import date
from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import RequestFactory, TestCase
from django.utils import timezone

from audit.models import OrderAuditEntry
from employees.models import Employee
from reachtruck.models import MoveTask
from reachtruck.services.task_commands import scan_move_task_step, take_move_task
from shipping.models import ShippingOrder, ShippingOrderItem
from sku.models import Agency
from sklad.models import (
    WarehouseContainer,
    WarehouseEvent,
    WarehouseLocation,
    WarehouseOperation,
    WarehouseStockSnapshot,
)
from sklad.services.problem_boxes import (
    daily_problem_control_route,
    problem_box_snapshot_ids,
    problem_box_snapshots,
    problem_pallet_rows,
)
from sklad.services.warehouse_events import WarehouseEventType
from sklad.services.warehouse_transitions import WarehouseStateCode
from sklad.services.warehouse_write_path import WarehouseWritePathService
from sklad.ui_services import build_inventory_journal_page
from todo.models import Task


class ProblemBoxReturnTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.storekeeper = user_model.objects.create_user(username="problem_box_storekeeper", password="x")
        self.other_storekeeper = user_model.objects.create_user(username="problem_box_other", password="x")
        self.reachtruck_user = user_model.objects.create_user(username="problem_box_reachtruck", password="x")
        self.reachtruck_employee = Employee.objects.create(
            user=self.reachtruck_user,
            full_name="Водитель Ричтрака",
            role="reachtruck_driver",
            is_active=True,
        )
        self.client_user = user_model.objects.create_user(username="problem_box_client", password="x")
        self.agency = Agency.objects.create(agn_name="Клиент проблемного короба", portal_user=self.client_user)
        self.otg_location = WarehouseLocation.objects.create(
            warehouse_code="MSK",
            zone_code="OTG",
            zone_kind=WarehouseLocation.ZONE_KIND_SHIPPING,
            location_code="OTG-PROBLEM",
            display_name="OTG",
            is_shipping=True,
        )
        self.storage_location = WarehouseLocation.objects.create(
            warehouse_code="MSK",
            zone_code="OS",
            zone_kind=WarehouseLocation.ZONE_KIND_STORAGE,
            row_no=1,
            section_no=1,
            tier_no=1,
            cell_no=1,
            location_code="OS-1-1-1-1",
            display_name="OS 1-1-1-1",
            is_storage=True,
        )
        self.snapshot = self._create_problem_snapshot("BOX-PROBLEM-1", qty=1)

    def _container(self, code: str, location: WarehouseLocation) -> WarehouseContainer:
        return WarehouseContainer.objects.create(
            agency=self.agency,
            container_type=WarehouseContainer.TYPE_BOX,
            container_code=code,
            current_location=location,
            status=WarehouseContainer.STATUS_ACTIVE,
        )

    def _event(self, *, container, event_type, payload=None, location=None):
        location = location or self.otg_location
        return WarehouseEvent.objects.create(
            agency=self.agency,
            event_type=event_type,
            stock_context_type="shipping",
            stock_context_id="OTG-000176",
            container=container,
            from_location=location,
            to_location=location,
            from_zone_code=location.zone_code,
            to_zone_code=location.zone_code,
            qty=1,
            payload=payload or {},
            occurred_at=timezone.now(),
        )

    def _create_problem_snapshot(self, box_code: str, *, qty: int) -> WarehouseStockSnapshot:
        container = self._container(box_code, self.otg_location)
        event = self._event(
            container=container,
            event_type=WarehouseEventType.WAREHOUSE_CONTEXT_CANCELED.value,
            payload={
                "reason": "snapshot was not included in shipping packing act",
                "operation_context_id": "OTG-000176",
            },
        )
        return WarehouseStockSnapshot.objects.create(
            agency=self.agency,
            source_context_type="receiving",
            source_context_id="PR-000147",
            sku_code="фикус003",
            name="Фикус",
            size="0",
            barcode="2051685885923",
            goods_type="gv",
            qty=qty,
            available_qty=0,
            container=container,
            container_code=box_code,
            location=self.otg_location,
            zone_code="OTG",
            zone_kind=WarehouseLocation.ZONE_KIND_SHIPPING,
            warehouse_state_code=WarehouseStateCode.IN_OTG.value,
            last_event=event,
        )

    def test_problem_snapshot_is_detected_but_regular_otg_snapshot_is_not(self):
        regular_container = self._container("BOX-REGULAR-1", self.otg_location)
        regular_event = self._event(
            container=regular_container,
            event_type=WarehouseEventType.READY_FOR_LOADING.value,
        )
        WarehouseStockSnapshot.objects.create(
            agency=self.agency,
            source_context_type="receiving",
            source_context_id="PR-000147",
            sku_code="фикус003",
            name="Фикус",
            size="0",
            barcode="2051685885923",
            goods_type="gv",
            qty=1,
            available_qty=0,
            container=regular_container,
            container_code=regular_container.container_code,
            location=self.otg_location,
            zone_code="OTG",
            zone_kind=WarehouseLocation.ZONE_KIND_SHIPPING,
            warehouse_state_code=WarehouseStateCode.READY_FOR_LOADING.value,
            last_event=regular_event,
        )

        self.assertEqual(problem_box_snapshot_ids(agency=self.agency), {self.snapshot.id})

    def test_problem_pallet_requires_confirmed_canceled_shipping_marker(self):
        pallet = WarehouseContainer.objects.create(
            agency=self.agency,
            container_type=WarehouseContainer.TYPE_PALLET,
            container_code="PALLET-PROBLEM-1",
            current_location=self.otg_location,
            status=WarehouseContainer.STATUS_ACTIVE,
        )
        self.snapshot.parent_container = pallet
        self.snapshot.save(update_fields=["parent_container", "updated_at"])

        rows = problem_pallet_rows(agency=self.agency)

        self.assertEqual(problem_box_snapshots(agency=self.agency), [])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["pallet_code"], pallet.container_code)
        self.assertEqual(rows[0]["box_codes"], [self.snapshot.container_code])
        self.assertEqual(rows[0]["box_count"], 1)
        self.assertEqual(rows[0]["qty"], 1)
        self.assertEqual(rows[0]["shipping_documents"], ["OTG-000176"])

        self.snapshot.last_event.payload = {"reason": "ordinary processing placement"}
        self.snapshot.last_event.save(update_fields=["payload"])

        self.assertEqual(problem_pallet_rows(agency=self.agency), [])

    def test_daily_control_command_is_idempotent_and_does_not_change_stock(self):
        Employee.objects.create(
            user=self.storekeeper,
            full_name="Кладовщик Проверяющий",
            role="storekeeper",
            is_active=True,
        )
        target_day = date(2026, 8, 4)
        snapshot_before = tuple(
            WarehouseStockSnapshot.objects.filter(pk=self.snapshot.pk).values_list(
                "qty",
                "available_qty",
                "zone_code",
                "warehouse_state_code",
                "active_operation_id",
                "last_event_id",
            )[0]
        )
        event_count_before = WarehouseEvent.objects.count()
        operation_count_before = WarehouseOperation.objects.count()

        first_output = StringIO()
        second_output = StringIO()
        call_command("sync_daily_problem_control", target_date=target_day.isoformat(), stdout=first_output)
        call_command("sync_daily_problem_control", target_date=target_day.isoformat(), stdout=second_output)

        tasks = Task.objects.filter(route=daily_problem_control_route(target_day))
        self.assertEqual(tasks.count(), 1)
        task = tasks.get()
        self.assertEqual(task.status, "in_progress")
        self.assertEqual(task.priority, "high")
        self.assertIn("Проблемные короба: 1", task.description)
        self.assertIn("BOX-PROBLEM-1", task.description)
        self.assertIn('"created": true', first_output.getvalue())
        self.assertIn('"created": false', second_output.getvalue())
        self.assertEqual(
            tuple(
                WarehouseStockSnapshot.objects.filter(pk=self.snapshot.pk).values_list(
                    "qty",
                    "available_qty",
                    "zone_code",
                    "warehouse_state_code",
                    "active_operation_id",
                    "last_event_id",
                )[0]
            ),
            snapshot_before,
        )
        self.assertEqual(WarehouseEvent.objects.count(), event_count_before)
        self.assertEqual(WarehouseOperation.objects.count(), operation_count_before)

    def test_daily_control_context_links_today_task(self):
        Employee.objects.create(
            user=self.storekeeper,
            full_name="Кладовщик Проверяющий",
            role="storekeeper",
            is_active=True,
        )
        today = timezone.localdate()
        call_command("sync_daily_problem_control", target_date=today.isoformat(), stdout=StringIO())
        request = RequestFactory().get("/sklad/journal/")
        request.user = self.storekeeper

        context = build_inventory_journal_page(request=request)["context"]

        self.assertEqual(context["daily_problem_control"]["box_count"], 1)
        self.assertEqual(context["daily_problem_control"]["pallet_count"], 0)
        self.assertEqual(context["daily_problem_control"]["status_label"], "Требует проверки")
        self.assertTrue(context["daily_problem_control"]["task_url"].startswith("/todo/"))

    def test_return_requires_claim_matching_box_and_storage_location(self):
        operation = WarehouseWritePathService.start_problem_box_return(
            snapshot_id=self.snapshot.id,
            expected_last_event_id=self.snapshot.last_event_id,
            performed_by=self.storekeeper,
        )
        self.snapshot.refresh_from_db()
        self.assertEqual(self.snapshot.active_operation_id, operation.id)
        self.assertEqual(operation.status, WarehouseOperation.STATUS_PLANNED)
        self.assertEqual(self.snapshot.available_qty, 0)
        move_task = MoveTask.objects.get(payload__problem_box_snapshot_id=self.snapshot.id)
        self.assertEqual(move_task.status, MoveTask.STATUS_CREATED)
        self.assertEqual(move_task.payload["problem_box_code"], self.snapshot.container_code)

        taken = take_move_task(
            legacy_order_id=move_task.legacy_order_id,
            user=self.reachtruck_user,
            employee_id=self.reachtruck_employee.id,
            employee_name=self.reachtruck_employee.full_name,
        )
        self.assertTrue(taken.ok, taken.error)
        operation.refresh_from_db()
        self.assertEqual(operation.status, WarehouseOperation.STATUS_IN_PROGRESS)

        wrong_box = scan_move_task_step(
            legacy_order_id=move_task.legacy_order_id,
            scan_value="BOX-OTHER",
            user=self.reachtruck_user,
            employee_id=self.reachtruck_employee.id,
            employee_name=self.reachtruck_employee.full_name,
        )
        self.assertFalse(wrong_box.ok)
        self.assertIn("Отсканирован другой короб", wrong_box.error)
        self.snapshot.refresh_from_db()
        self.assertEqual(self.snapshot.available_qty, 0)
        self.assertEqual(self.snapshot.zone_code, "OTG")

        box_scan = scan_move_task_step(
            legacy_order_id=move_task.legacy_order_id,
            scan_value=self.snapshot.container_code,
            user=self.reachtruck_user,
            employee_id=self.reachtruck_employee.id,
            employee_name=self.reachtruck_employee.full_name,
        )
        self.assertTrue(box_scan.ok, box_scan.error)
        self.assertFalse(box_scan.completed)

        wrong_location = scan_move_task_step(
            legacy_order_id=move_task.legacy_order_id,
            scan_value="OTG-PROBLEM",
            user=self.reachtruck_user,
            employee_id=self.reachtruck_employee.id,
            employee_name=self.reachtruck_employee.full_name,
        )
        self.assertFalse(wrong_location.ok)
        self.snapshot.refresh_from_db()
        self.assertEqual(self.snapshot.available_qty, 0)
        self.assertEqual(self.snapshot.zone_code, "OTG")

        location_scan = scan_move_task_step(
            legacy_order_id=move_task.legacy_order_id,
            scan_value=self.storage_location.location_code,
            user=self.reachtruck_user,
            employee_id=self.reachtruck_employee.id,
            employee_name=self.reachtruck_employee.full_name,
        )
        self.assertTrue(location_scan.ok, location_scan.error)
        self.assertTrue(location_scan.completed)
        self.snapshot.refresh_from_db()
        self.snapshot.container.refresh_from_db()
        operation.refresh_from_db()
        move_task.refresh_from_db()
        self.assertEqual(self.snapshot.available_qty, 1)
        self.assertEqual(self.snapshot.zone_code, "OS")
        self.assertEqual(self.snapshot.warehouse_state_code, WarehouseStateCode.STORED.value)
        self.assertIsNone(self.snapshot.active_operation_id)
        self.assertEqual(self.snapshot.container.current_location_id, self.storage_location.id)
        self.assertEqual(operation.status, WarehouseOperation.STATUS_DONE)
        self.assertEqual(move_task.status, MoveTask.STATUS_DONE)
        self.assertEqual(move_task.qty_done, 1)
        self.assertEqual(problem_box_snapshots(agency=self.agency), [])

    def test_stale_event_and_second_storekeeper_cannot_take_claim(self):
        with self.assertRaisesMessage(ValueError, "Состояние короба уже изменилось"):
            WarehouseWritePathService.start_problem_box_return(
                snapshot_id=self.snapshot.id,
                expected_last_event_id=self.snapshot.last_event_id + 1,
                performed_by=self.storekeeper,
            )
        operation = WarehouseWritePathService.start_problem_box_return(
            snapshot_id=self.snapshot.id,
            expected_last_event_id=self.snapshot.last_event_id,
            performed_by=self.storekeeper,
        )
        repeated = WarehouseWritePathService.start_problem_box_return(
            snapshot_id=self.snapshot.id,
            expected_last_event_id=self.snapshot.last_event_id,
            performed_by=self.other_storekeeper,
        )
        self.assertEqual(repeated.id, operation.id)
        self.assertEqual(MoveTask.objects.filter(payload__problem_box_snapshot_id=self.snapshot.id).count(), 1)

    def test_release_keeps_box_in_problem_queue_without_changing_stock(self):
        operation = WarehouseWritePathService.start_problem_box_return(
            snapshot_id=self.snapshot.id,
            expected_last_event_id=self.snapshot.last_event_id,
            performed_by=self.storekeeper,
        )
        WarehouseWritePathService.cancel_problem_box_return(
            operation_id=operation.id,
            performed_by=self.storekeeper,
        )
        self.snapshot.refresh_from_db()
        move_task = MoveTask.objects.get(payload__problem_box_snapshot_id=self.snapshot.id)
        self.assertIsNone(self.snapshot.active_operation_id)
        self.assertEqual(self.snapshot.available_qty, 0)
        self.assertEqual(self.snapshot.zone_code, "OTG")
        self.assertEqual(move_task.status, MoveTask.STATUS_CANCELED)
        self.assertEqual(problem_box_snapshot_ids(agency=self.agency), {self.snapshot.id})

    def test_client_balance_separates_available_shipping_and_problem_qty(self):
        available_container = self._container("BOX-AVAILABLE", self.storage_location)
        available_event = self._event(
            container=available_container,
            event_type=WarehouseEventType.STOCK_RETURNED_TO_STORAGE.value,
            location=self.storage_location,
        )
        WarehouseStockSnapshot.objects.create(
            agency=self.agency,
            source_context_type="receiving",
            source_context_id="PR-000147",
            sku_code="фикус003",
            name="Фикус",
            size="0",
            barcode="2051685885923",
            goods_type="gv",
            qty=8,
            available_qty=8,
            container=available_container,
            container_code=available_container.container_code,
            location=self.storage_location,
            zone_code="OS",
            zone_kind=WarehouseLocation.ZONE_KIND_STORAGE,
            warehouse_state_code=WarehouseStateCode.STORED.value,
            last_event=available_event,
        )
        shipping_container = self._container("BOX-SHIPPING", self.otg_location)
        shipping_event = self._event(
            container=shipping_container,
            event_type=WarehouseEventType.READY_FOR_LOADING.value,
        )
        WarehouseStockSnapshot.objects.create(
            agency=self.agency,
            source_context_type="receiving",
            source_context_id="PR-000147",
            sku_code="фикус003",
            name="Фикус",
            size="0",
            barcode="2051685885923",
            goods_type="gv",
            qty=1,
            available_qty=0,
            container=shipping_container,
            container_code=shipping_container.container_code,
            location=self.otg_location,
            zone_code="OTG",
            zone_kind=WarehouseLocation.ZONE_KIND_SHIPPING,
            warehouse_state_code=WarehouseStateCode.READY_FOR_LOADING.value,
            last_event=shipping_event,
        )
        shipping_order = ShippingOrder.objects.create(
            number="OTG-000176",
            agency=self.agency,
            status=ShippingOrder.STATUS_PACKED,
        )
        ShippingOrderItem.objects.create(
            order=shipping_order,
            sku_code="фикус003",
            name="Фикус",
            size="0",
            barcode="2051685885923",
            goods_type="gv",
            qty_requested=1,
            qty_reserved=1,
            qty_shipped=0,
        )
        request = RequestFactory().get("/sklad/journal/")
        request.user = self.client_user

        page = build_inventory_journal_page(request=request)

        rows = page["context"]["rows"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["available_qty"], 8)
        self.assertEqual(rows[0]["shipping_work_qty"], 1)
        self.assertEqual(rows[0]["shipping_request_qty"], 1)
        self.assertEqual(rows[0]["problem_qty"], 1)
        self.assertEqual(rows[0]["accounted_qty"], 10)
        self.assertEqual(rows[0]["free_real_qty"], 9)

    def test_client_balance_subtracts_submitted_processing_and_restores_after_cancel(self):
        available_container = self._container("BOX-PROCESSING-AVAILABLE", self.storage_location)
        available_event = self._event(
            container=available_container,
            event_type=WarehouseEventType.STOCK_RETURNED_TO_STORAGE.value,
            location=self.storage_location,
        )
        WarehouseStockSnapshot.objects.create(
            agency=self.agency,
            source_context_type="receiving",
            source_context_id="PR-000147",
            sku_code="фикус003",
            name="Фикус",
            size="0",
            barcode="2051685885923",
            goods_type="gv",
            qty=9,
            available_qty=9,
            container=available_container,
            container_code=available_container.container_code,
            location=self.storage_location,
            zone_code="OS",
            zone_kind=WarehouseLocation.ZONE_KIND_STORAGE,
            warehouse_state_code=WarehouseStateCode.STORED.value,
            last_event=available_event,
        )
        OrderAuditEntry.objects.create(
            order_id="25",
            order_type="processing",
            action="status",
            agency=self.agency,
            payload={
                "status": "submitted",
                "status_label": "Ждет подтверждения",
                "stock_rows": [
                    {
                        "sku": "фикус003",
                        "name": "Фикус",
                        "size": "0",
                        "goods_type": "gv",
                        "qty": 3,
                    }
                ],
            },
        )
        request = RequestFactory().get("/sklad/journal/")
        request.user = self.client_user

        active_page = build_inventory_journal_page(request=request)

        active_row = active_page["context"]["rows"][0]
        self.assertEqual(active_row["accounted_qty"], 10)
        self.assertEqual(active_row["processing_request_qty"], 3)
        self.assertEqual(active_row["free_real_qty"], 7)

        OrderAuditEntry.objects.create(
            order_id="25",
            order_type="processing",
            action="status",
            agency=self.agency,
            payload={"status": "cancelled", "status_label": "Отменена"},
        )

        canceled_page = build_inventory_journal_page(request=request)

        canceled_row = canceled_page["context"]["rows"][0]
        self.assertEqual(canceled_row["processing_request_qty"], 0)
        self.assertEqual(canceled_row["free_real_qty"], 10)

    def test_client_balance_ignores_draft_and_completed_shipping_requests(self):
        available_container = self._container("BOX-SHIPPING-AVAILABLE", self.storage_location)
        available_event = self._event(
            container=available_container,
            event_type=WarehouseEventType.STOCK_RETURNED_TO_STORAGE.value,
            location=self.storage_location,
        )
        WarehouseStockSnapshot.objects.create(
            agency=self.agency,
            source_context_type="receiving",
            source_context_id="PR-000147",
            sku_code="фикус003",
            name="Фикус",
            size="0",
            barcode="2051685885923",
            goods_type="gv",
            qty=9,
            available_qty=9,
            container=available_container,
            container_code=available_container.container_code,
            location=self.storage_location,
            zone_code="OS",
            zone_kind=WarehouseLocation.ZONE_KIND_STORAGE,
            warehouse_state_code=WarehouseStateCode.STORED.value,
            last_event=available_event,
        )
        for number, status in (
            ("OTG-DRAFT", ShippingOrder.STATUS_DRAFT),
            ("OTG-SHIPPED", ShippingOrder.STATUS_SHIPPED),
            ("OTG-CANCELED", ShippingOrder.STATUS_CANCELED),
        ):
            order = ShippingOrder.objects.create(number=number, agency=self.agency, status=status)
            ShippingOrderItem.objects.create(
                order=order,
                sku_code="фикус003",
                name="Фикус",
                size="0",
                goods_type="gv",
                qty_requested=1,
                qty_reserved=0,
                qty_shipped=1 if status == ShippingOrder.STATUS_SHIPPED else 0,
            )
        request = RequestFactory().get("/sklad/journal/")
        request.user = self.client_user

        page = build_inventory_journal_page(request=request)

        row = page["context"]["rows"][0]
        self.assertEqual(row["shipping_request_qty"], 0)
        self.assertEqual(row["accounted_qty"], 10)
        self.assertEqual(row["free_real_qty"], 10)

    def test_client_balance_subtracts_only_remaining_partial_shipping_qty(self):
        available_container = self._container("BOX-PARTIAL-AVAILABLE", self.storage_location)
        available_event = self._event(
            container=available_container,
            event_type=WarehouseEventType.STOCK_RETURNED_TO_STORAGE.value,
            location=self.storage_location,
        )
        WarehouseStockSnapshot.objects.create(
            agency=self.agency,
            source_context_type="receiving",
            source_context_id="PR-000147",
            sku_code="фикус003",
            name="Фикус",
            size="0",
            goods_type="gv",
            qty=4,
            available_qty=4,
            container=available_container,
            container_code=available_container.container_code,
            location=self.storage_location,
            zone_code="OS",
            zone_kind=WarehouseLocation.ZONE_KIND_STORAGE,
            warehouse_state_code=WarehouseStateCode.STORED.value,
            last_event=available_event,
        )
        order = ShippingOrder.objects.create(
            number="OTG-PARTIAL",
            agency=self.agency,
            status=ShippingOrder.STATUS_PARTIAL,
        )
        ShippingOrderItem.objects.create(
            order=order,
            sku_code="фикус003",
            name="Фикус",
            size="0",
            goods_type="gv",
            qty_requested=3,
            qty_reserved=3,
            qty_shipped=1,
        )
        request = RequestFactory().get("/sklad/journal/")
        request.user = self.client_user

        page = build_inventory_journal_page(request=request)

        row = page["context"]["rows"][0]
        self.assertEqual(row["accounted_qty"], 5)
        self.assertEqual(row["shipping_request_qty"], 2)
        self.assertEqual(row["free_real_qty"], 3)

    def test_client_balance_keeps_active_request_visible_without_stock(self):
        order = ShippingOrder.objects.create(
            number="OTG-NO-STOCK",
            agency=self.agency,
            status=ShippingOrder.STATUS_SUBMITTED,
        )
        ShippingOrderItem.objects.create(
            order=order,
            sku_code="NO-STOCK-SKU",
            name="Нет на складе",
            size="L",
            goods_type="gv",
            qty_requested=2,
            qty_reserved=0,
            qty_shipped=0,
        )
        request = RequestFactory().get("/sklad/journal/")
        request.user = self.client_user

        page = build_inventory_journal_page(request=request)

        row = next(item for item in page["context"]["rows"] if item["sku"] == "NO-STOCK-SKU")
        self.assertEqual(row["shipping_request_qty"], 2)
        self.assertEqual(row["accounted_qty"], 0)
        self.assertEqual(row["free_real_qty"], 0)
