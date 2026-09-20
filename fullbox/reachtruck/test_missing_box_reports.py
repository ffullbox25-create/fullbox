from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase

from employees.models import Employee
from reachtruck.models import BoxClaim, MoveRequest, MoveTask, PalletLock
from reachtruck.services.missing_box_reports import report_move_task_missing_box
from reachtruck_box_move.models import BoxMoveOperation
from reachtruck_box_move.services import report_destination_box_missing
from sklad.models import (
    WarehouseContainer,
    WarehouseLocation,
    WarehouseOperation,
    WarehouseReserve,
    WarehouseStockSnapshot,
)
from sku.models import Agency, SKU
from todo.models import Task


class MissingBoxReportTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.driver = User.objects.create_user(username="missing_box_driver", password="pass")
        self.driver_employee = Employee.objects.create(
            user=self.driver,
            full_name="Водитель Ричтрака",
            role="reachtruck_driver",
            is_active=True,
        )
        head_user = User.objects.create_user(username="missing_box_head", password="pass")
        self.head_employee = Employee.objects.create(
            user=head_user,
            full_name="Начальник Склада",
            role="head_manager",
            is_active=True,
        )
        self.agency = Agency.objects.create(agn_name="Тестовый клиент")

    def _stock_box(self, *, box_code: str, pallet_code: str, location, sku, qty: int = 5):
        pallet, _ = WarehouseContainer.objects.get_or_create(
            agency=self.agency,
            container_code=pallet_code,
            defaults={
                "container_type": WarehouseContainer.TYPE_PALLET,
                "current_location": location,
            },
        )
        box = WarehouseContainer.objects.create(
            agency=self.agency,
            container_type=WarehouseContainer.TYPE_BOX,
            container_code=box_code,
            parent_container=pallet,
            current_location=location,
        )
        stock = WarehouseStockSnapshot.objects.create(
            agency=self.agency,
            stock_unit_type="box",
            sku_ref=sku,
            sku_code=sku.sku_code,
            name=sku.name,
            size="M",
            barcode="460000000001",
            goods_type="ready",
            qty=qty,
            available_qty=qty,
            container=box,
            container_code=box_code,
            parent_container=pallet,
            location=location,
            zone_code=location.zone_code,
            zone_kind=location.zone_kind,
        )
        return pallet, box, stock

    @patch("reachtruck.services.missing_box_reports._replace_missing_main_warehouse_box")
    @patch("reachtruck.services.task_commands.build_mobile_execution_snapshot")
    def test_move_report_creates_one_urgent_task_without_changing_move(
        self, snapshot_mock, replacement_mock
    ):
        replacement_mock.return_value = {"replaced": False, "supported": True}
        request = MoveRequest.objects.create(
            context_type=MoveRequest.CONTEXT_MANUAL,
            agency=self.agency,
            status=MoveRequest.STATUS_IN_PROGRESS,
        )
        payload = {
            "assigned_to_id": self.driver_employee.id,
            "assigned_employee_id": self.driver_employee.id,
            "task_kind_label": "Перемещение в FBS",
            "pallet_code": "PAL-FBS-1",
            "source_location_scan_code": "C-9/1-2",
            "mobile_execution": {"pallet_confirmed": True},
        }
        move = MoveTask.objects.create(
            request=request,
            pallet_code="PAL-FBS-1",
            payload=payload,
            status=MoveTask.STATUS_IN_PROGRESS,
            assigned_to=self.driver,
            assigned_to_name=self.driver_employee.full_name,
            legacy_order_id="FBS-MOV-TEST-1",
        )
        snapshot_mock.return_value = {
            "current_step": "boxes",
            "boxes_pending": [{"box_code": "BOX-FBS-1"}],
        }
        original_values = (move.status, move.payload, move.qty_done, move.completed_at)

        first = report_move_task_missing_box(
            legacy_order_id=move.legacy_order_id,
            box_code="BOX-FBS-1",
            mobile_category="movement",
            mobile_request_key="fbs-test",
            user=self.driver,
            employee_id=self.driver_employee.id,
            employee_name=self.driver_employee.full_name,
        )
        second = report_move_task_missing_box(
            legacy_order_id=move.legacy_order_id,
            box_code="BOX-FBS-1",
            mobile_category="movement",
            mobile_request_key="fbs-test",
            user=self.driver,
            employee_id=self.driver_employee.id,
            employee_name=self.driver_employee.full_name,
        )

        self.assertTrue(first.ok)
        self.assertTrue(first.created)
        self.assertTrue(second.ok)
        self.assertFalse(second.created)
        self.assertEqual(Task.objects.count(), 1)
        verification = Task.objects.get()
        self.assertEqual(verification.assigned_to, self.head_employee)
        self.assertEqual(verification.priority, "urgent")
        self.assertIn("BOX-FBS-1", verification.description)
        self.assertIn("Идентичного свободного короба", verification.description)
        move.refresh_from_db()
        self.assertEqual((move.status, move.payload, move.qty_done, move.completed_at), original_values)

    @patch("reachtruck.services.task_commands.build_mobile_execution_snapshot")
    def test_main_move_without_identical_box_quarantines_missing_stock(self, snapshot_mock):
        location = WarehouseLocation.objects.create(
            zone_code="C",
            zone_kind=WarehouseLocation.ZONE_KIND_STORAGE,
            row_no=8,
            section_no=1,
            tier_no=1,
            cell_no=1,
            location_code="C-8/1-1",
            is_storage=True,
        )
        sku = SKU.objects.create(
            agency=self.agency,
            sku_code="SKU-NO-REPLACEMENT",
            name="Товар без замены",
        )
        pallet, missing_box, missing_stock = self._stock_box(
            box_code="BOX-WITHOUT-REPLACEMENT",
            pallet_code="PAL-WITHOUT-REPLACEMENT",
            location=location,
            sku=sku,
        )
        request = MoveRequest.objects.create(
            context_type=MoveRequest.CONTEXT_MANUAL,
            agency=self.agency,
            status=MoveRequest.STATUS_IN_PROGRESS,
        )
        move = MoveTask.objects.create(
            request=request,
            pallet_code=pallet.container_code,
            move_mode=MoveTask.MODE_BOX_FULL,
            payload={
                "assigned_to_id": self.driver_employee.id,
                "requested_box": missing_box.container_code,
                "requested_boxes": [missing_box.container_code],
                "from_location": {"zone": "C", "row": 8, "section": 1, "tier": 1, "cell": 1},
                "to_location": {"zone": "OS", "row": 1, "section": 1, "tier": 1, "cell": 1},
                "mobile_execution": {"pallet_confirmed": True, "boxes_scanned": []},
            },
            status=MoveTask.STATUS_IN_PROGRESS,
            assigned_to=self.driver,
            legacy_order_id="MOVE-NO-REPLACEMENT",
        )
        snapshot_mock.return_value = {
            "current_step": "boxes",
            "boxes": [{"box_code": missing_box.container_code}],
            "boxes_pending": [{"box_code": missing_box.container_code}],
        }

        result = report_move_task_missing_box(
            legacy_order_id=move.legacy_order_id,
            box_code=missing_box.container_code,
            mobile_category="movement",
            mobile_request_key="",
            user=self.driver,
            employee_id=self.driver_employee.id,
            employee_name=self.driver_employee.full_name,
        )

        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.replacement_box_code, "")
        self.assertIn("Похожего свободного короба", result.message)
        missing_stock.refresh_from_db()
        self.assertEqual((missing_stock.qty, missing_stock.available_qty), (5, 0))
        self.assertEqual(missing_stock.other_reserved_qty, 5)
        move.refresh_from_db()
        self.assertEqual(move.payload["requested_box"], missing_box.container_code)

    @patch("reachtruck.services.task_commands.build_mobile_execution_snapshot")
    def test_main_move_replaces_identical_box_and_preserves_accounting_qty(self, snapshot_mock):
        location = WarehouseLocation.objects.create(
            zone_code="C",
            zone_kind=WarehouseLocation.ZONE_KIND_STORAGE,
            row_no=1,
            section_no=2,
            tier_no=1,
            cell_no=1,
            location_code="C-1/1-1",
            display_name="C-1/1-1",
            is_storage=True,
        )
        sku = SKU.objects.create(
            agency=self.agency,
            sku_code="SKU-MISSING-BOX",
            name="Товар для замены",
        )
        pallet, missing_box, missing_stock = self._stock_box(
            box_code="BOX-MISSING",
            pallet_code="PAL-ONE",
            location=location,
            sku=sku,
        )
        _pallet, replacement_box, replacement_stock = self._stock_box(
            box_code="BOX-REPLACEMENT",
            pallet_code=pallet.container_code,
            location=location,
            sku=sku,
        )
        request = MoveRequest.objects.create(
            context_type=MoveRequest.CONTEXT_MANUAL,
            agency=self.agency,
            status=MoveRequest.STATUS_IN_PROGRESS,
        )
        payload = {
            "assigned_to_id": self.driver_employee.id,
            "assigned_employee_id": self.driver_employee.id,
            "task_kind_label": "Перемещение на основном складе",
            "pallet_code": pallet.container_code,
            "requested_boxes": [missing_box.container_code],
            "requested_box": missing_box.container_code,
            "planned_box_codes": [missing_box.container_code],
            "selected_box_codes": [missing_box.container_code],
            "reserved_box_codes": [missing_box.container_code],
            "from_location": {"zone": "C", "row": 1, "section": 2, "tier": 1, "cell": 1},
            "to_location": {"zone": "OS", "row": 1, "section": 1, "tier": 1, "cell": 1},
            "mobile_execution": {"pallet_confirmed": True, "boxes_scanned": []},
        }
        move = MoveTask.objects.create(
            request=request,
            pallet_code=pallet.container_code,
            move_mode=MoveTask.MODE_BOX_FULL,
            payload=payload,
            status=MoveTask.STATUS_IN_PROGRESS,
            assigned_to=self.driver,
            legacy_order_id="MOVE-MISSING-REPLACE-1",
        )
        snapshot_mock.return_value = {
            "current_step": "boxes",
            "boxes": [{"box_code": missing_box.container_code}],
            "boxes_pending": [{"box_code": missing_box.container_code}],
        }

        result = report_move_task_missing_box(
            legacy_order_id=move.legacy_order_id,
            box_code=missing_box.container_code,
            mobile_category="movement",
            mobile_request_key="",
            user=self.driver,
            employee_id=self.driver_employee.id,
            employee_name=self.driver_employee.full_name,
        )

        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.replacement_box_code, replacement_box.container_code)
        missing_stock.refresh_from_db()
        replacement_stock.refresh_from_db()
        self.assertEqual(missing_stock.qty, 5)
        self.assertEqual(missing_stock.available_qty, 0)
        self.assertEqual(missing_stock.other_reserved_qty, 5)
        self.assertEqual(replacement_stock.qty, 5)
        self.assertEqual(replacement_stock.available_qty, 5)
        self.assertEqual(replacement_stock.other_reserved_qty, 0)
        self.assertEqual(WarehouseReserve.objects.filter(context_type="missing_box_check").count(), 1)
        self.assertEqual(WarehouseOperation.objects.filter(context_type="missing_box_check").count(), 1)
        move.refresh_from_db()
        self.assertEqual(move.payload["requested_box"], replacement_box.container_code)
        self.assertEqual(move.payload["requested_boxes"], [replacement_box.container_code])
        self.assertFalse(move.payload["mobile_execution"]["pallet_confirmed"])
        self.assertTrue(
            BoxClaim.objects.filter(
                move_task=move,
                box_code=replacement_box.container_code,
                status=BoxClaim.STATUS_CLAIMED,
            ).exists()
        )
        self.assertTrue(
            PalletLock.objects.filter(
                move_task=move,
                pallet_code=pallet.container_code,
                status=PalletLock.STATUS_ACTIVE,
            ).exists()
        )

    @patch("reachtruck.services.task_commands.build_mobile_execution_snapshot")
    def test_move_report_rejects_box_that_is_not_pending(self, snapshot_mock):
        request = MoveRequest.objects.create(
            context_type=MoveRequest.CONTEXT_MANUAL,
            agency=self.agency,
            status=MoveRequest.STATUS_IN_PROGRESS,
        )
        move = MoveTask.objects.create(
            request=request,
            pallet_code="PAL-1",
            payload={"assigned_to_id": self.driver_employee.id},
            status=MoveTask.STATUS_IN_PROGRESS,
            assigned_to=self.driver,
            legacy_order_id="MOVE-TEST-2",
        )
        snapshot_mock.return_value = {
            "current_step": "boxes",
            "boxes_pending": [{"box_code": "BOX-EXPECTED"}],
        }

        result = report_move_task_missing_box(
            legacy_order_id=move.legacy_order_id,
            box_code="BOX-FOREIGN",
            mobile_category="movement",
            mobile_request_key="",
            user=self.driver,
            employee_id=self.driver_employee.id,
            employee_name=self.driver_employee.full_name,
        )

        self.assertFalse(result.ok)
        self.assertEqual(Task.objects.count(), 0)

    def test_box_verification_report_is_idempotent_and_quarantines_missing_box(self):
        location = WarehouseLocation.objects.create(
            zone_code="OS",
            zone_kind=WarehouseLocation.ZONE_KIND_STORAGE,
            row_no=4,
            section_no=1,
            tier_no=1,
            cell_no=1,
            location_code="OS-4-1-1-1",
            is_storage=True,
        )
        sku = SKU.objects.create(
            agency=self.agency,
            sku_code="SKU-VERIFY-MISSING",
            name="Товар проверки коробов",
        )
        _pallet, _box, missing_stock = self._stock_box(
            box_code="BOX-MISSING",
            pallet_code="PAL-DEST",
            location=location,
            sku=sku,
            qty=4,
        )
        operation = BoxMoveOperation.objects.create(
            agency=self.agency,
            driver=self.driver,
            status=BoxMoveOperation.STATUS_VERIFYING,
            source_pallet_code="PAL-SOURCE",
            destination_pallet_code="PAL-DEST",
            destination_location_scan_code="A-1/2-3",
            selected_boxes=[{"box_code": "BOX-MOVE"}],
            destination_expected_boxes=[{"box_code": "BOX-MISSING", "total_qty": 4}],
            destination_scanned_boxes=[],
        )
        request = RequestFactory().post("/reachtruck-box-move/")
        request.user = self.driver
        _operation, first_message, first_error = report_destination_box_missing(
            operation,
            "BOX-MISSING",
            request,
        )
        _operation, second_message, second_error = report_destination_box_missing(
            operation,
            "BOX-MISSING",
            request,
        )

        self.assertFalse(first_error)
        self.assertFalse(second_error)
        self.assertIn("отправлена", first_message)
        self.assertIn("уже отправлена", second_message)
        self.assertEqual(Task.objects.count(), 1)
        operation.refresh_from_db()
        self.assertEqual(operation.status, BoxMoveOperation.STATUS_VERIFYING)
        self.assertEqual(len(operation.discrepancies), 1)
        self.assertEqual(operation.discrepancies[0]["type"], "missing_box")
        self.assertTrue(operation.discrepancies[0]["quarantine_operation_id"])
        missing_stock.refresh_from_db()
        self.assertEqual((missing_stock.qty, missing_stock.available_qty), (4, 0))
        self.assertEqual(missing_stock.other_reserved_qty, 4)
        self.assertEqual(
            WarehouseOperation.objects.filter(context_type="missing_box_check").count(),
            1,
        )
