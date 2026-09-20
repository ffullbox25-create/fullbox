import json

from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase

from audit.models import OrderAuditEntry
from employees.models import Employee
from reachtruck.models import BoxClaim, MoveRequest, MoveTask
from reachtruck.services.claims import claim_boxes_for_task
from reachtruck.services.task_commands import scan_move_task_step, take_move_task
from sklad.models import WarehouseOperation, WarehouseReserve, WarehouseStockSnapshot
from sklad.services import WarehouseStateCode
from sklad.services.warehouse_write_path import WarehouseWritePathService
from sklad.test_utils import create_warehouse_snapshot_row
from sku.models import Agency

from .services import build_obr_requested_rows, create_obr_move_request_response


class ProcessingReachtruckFactFlowTests(TestCase):
    def setUp(self):
        self.agency = Agency.objects.create(agn_name="Processing fact agency")
        self.head_user = get_user_model().objects.create_user(username="processing_fact_head", password="x")
        Employee.objects.create(
            full_name="Руководитель обработки",
            user=self.head_user,
            role="processing_head",
            is_active=True,
        )
        self.driver_one = get_user_model().objects.create_user(username="processing_fact_driver_1", password="x")
        self.driver_two = get_user_model().objects.create_user(username="processing_fact_driver_2", password="x")
        self.driver_one_employee = Employee.objects.create(
            full_name="Водитель ричтрака 1",
            user=self.driver_one,
            role="reachtruck_driver",
            is_active=True,
        )
        self.driver_two_employee = Employee.objects.create(
            full_name="Водитель ричтрака 2",
            user=self.driver_two,
            role="reachtruck_driver",
            is_active=True,
        )
        self.request_factory = RequestFactory()

    def _box(
        self,
        *,
        box_code: str,
        pallet_code: str,
        qty: int,
        sku: str = "SKU-PROC",
        barcode: str = "BAR-PROC",
        goods_type: str = "op",
        zone: str = "OS",
    ) -> WarehouseStockSnapshot:
        return create_warehouse_snapshot_row(
            agency=self.agency,
            order_type="receiving",
            order_id=f"REC-{box_code}",
            sku=sku,
            barcode=barcode,
            goods_type=goods_type,
            qty=qty,
            available_qty=qty,
            pallet_code=pallet_code,
            box_code=box_code,
            zone=zone,
        )

    def test_planning_with_same_barcode_selects_only_requested_goods_type(self):
        self._box(
            box_code="BOX-A-GV",
            pallet_code="PAL-GV",
            qty=1,
            goods_type="gv",
        )
        self._box(
            box_code="BOX-Z-VZ",
            pallet_code="PAL-VZ",
            qty=1,
            goods_type="vz",
        )

        rows = build_obr_requested_rows(
            agency=self.agency,
            processing_order_id="PROC-STRICT-TYPE",
            request_items=[
                {
                    "requested_article": "SKU-PROC",
                    "requested_barcodes": ["BAR-PROC"],
                    "requested_goods_type": "vz",
                    "requested_qty": 1,
                }
            ],
        )

        self.assertEqual(
            [(row["box_code"], row["requested_goods_type"]) for row in rows],
            [("BOX-Z-VZ", "vz")],
        )

    def test_planning_does_not_substitute_other_goods_type_with_same_barcode(self):
        self._box(
            box_code="BOX-ONLY-GV",
            pallet_code="PAL-GV",
            qty=1,
            goods_type="gv",
        )

        rows = build_obr_requested_rows(
            agency=self.agency,
            processing_order_id="PROC-NO-TYPE-SUBSTITUTION",
            request_items=[
                {
                    "requested_article": "SKU-PROC",
                    "requested_barcodes": ["BAR-PROC"],
                    "requested_goods_type": "vz",
                    "requested_qty": 1,
                }
            ],
        )

        self.assertEqual(rows, [])

    def test_saved_pr_boxes_without_strict_flag_are_selected_exactly(self):
        for box_code, sku, barcode in (
            ("BOX-PR-GRAY-1", "SKU-GRAY", "BAR-GRAY"),
            ("BOX-PR-GRAY-2", "SKU-GRAY", "BAR-GRAY"),
            ("BOX-PR-BEAR-1", "SKU-BEAR", "BAR-BEAR"),
        ):
            self._box(
                box_code=box_code,
                pallet_code="PAL-PR-RETURNS",
                qty=1,
                sku=sku,
                barcode=barcode,
                goods_type="vz",
                zone="PR",
            )
        OrderAuditEntry.objects.create(
            order_id="PROC-LEGACY-PR",
            order_type="processing",
            action="create",
            agency=self.agency,
            payload={
                "stock_rows": [
                    {
                        "article": "SKU-GRAY",
                        "barcode": "BAR-GRAY",
                        "goods_type": "vz",
                        "qty": 2,
                        "box_codes": ["BOX-PR-GRAY-1", "BOX-PR-GRAY-2"],
                    },
                    {
                        "article": "SKU-BEAR",
                        "barcode": "BAR-BEAR",
                        "goods_type": "vz",
                        "qty": 1,
                        "box_codes": ["BOX-PR-BEAR-1"],
                    },
                ]
            },
        )

        rows = build_obr_requested_rows(
            agency=self.agency,
            processing_order_id="PROC-LEGACY-PR",
            request_items=[
                {
                    "requested_article": "SKU-GRAY",
                    "requested_barcodes": ["BAR-GRAY"],
                    "requested_goods_type": "vz",
                    "requested_qty": 2,
                },
                {
                    "requested_article": "SKU-BEAR",
                    "requested_barcodes": ["BAR-BEAR"],
                    "requested_goods_type": "vz",
                    "requested_qty": 1,
                },
            ],
        )

        self.assertEqual(sum(int(row.get("qty") or 0) for row in rows), 3)
        self.assertEqual(
            {row.get("box_code") for row in rows},
            {"BOX-PR-GRAY-1", "BOX-PR-GRAY-2", "BOX-PR-BEAR-1"},
        )
        self.assertTrue(
            all(str((row.get("from_location") or {}).get("zone") or "") == "PR" for row in rows)
        )

    def test_strict_selection_can_move_all_items_from_mixed_box(self):
        self._box(
            box_code="BOX-MIXED",
            pallet_code="PAL-MIXED",
            qty=4,
            sku="SKU-MIXED-A",
            barcode="BAR-MIXED-A",
        )
        self._box(
            box_code="BOX-MIXED",
            pallet_code="PAL-MIXED",
            qty=6,
            sku="SKU-MIXED-B",
            barcode="BAR-MIXED-B",
        )
        OrderAuditEntry.objects.create(
            order_id="PROC-MIXED",
            order_type="processing",
            action="create",
            agency=self.agency,
            payload={
                "stock_rows": [
                    {
                        "article": "SKU-MIXED-A",
                        "barcode": "BAR-MIXED-A",
                        "goods_type": "op",
                        "qty": 4,
                        "box_codes": ["BOX-MIXED"],
                        "strict_selected_box": True,
                    },
                    {
                        "article": "SKU-MIXED-B",
                        "barcode": "BAR-MIXED-B",
                        "goods_type": "op",
                        "qty": 6,
                        "box_codes": ["BOX-MIXED"],
                        "strict_selected_box": True,
                    },
                ]
            },
        )

        rows = build_obr_requested_rows(
            agency=self.agency,
            processing_order_id="PROC-MIXED",
            request_items=[
                {
                    "requested_article": "SKU-MIXED-A",
                    "requested_barcodes": ["BAR-MIXED-A"],
                    "requested_goods_type": "op",
                    "requested_qty": 4,
                },
                {
                    "requested_article": "SKU-MIXED-B",
                    "requested_barcodes": ["BAR-MIXED-B"],
                    "requested_goods_type": "op",
                    "requested_qty": 6,
                },
            ],
        )

        self.assertEqual(sum(int(row.get("qty") or 0) for row in rows), 10)
        self.assertEqual({row.get("box_code") for row in rows}, {"BOX-MIXED"})
        self.assertEqual({row.get("requested_article") for row in rows}, {"SKU-MIXED-A", "SKU-MIXED-B"})
        self.assertTrue(all(not row.get("is_partial_pick") for row in rows))

    def test_strict_selection_keeps_unrequested_mixed_box_item_in_source_box(self):
        self._box(
            box_code="BOX-MIXED-PARTIAL",
            pallet_code="PAL-MIXED-PARTIAL",
            qty=4,
            sku="SKU-PICKED",
            barcode="BAR-PICKED",
        )
        self._box(
            box_code="BOX-MIXED-PARTIAL",
            pallet_code="PAL-MIXED-PARTIAL",
            qty=6,
            sku="SKU-LEFT",
            barcode="BAR-LEFT",
        )
        OrderAuditEntry.objects.create(
            order_id="PROC-MIXED-PARTIAL",
            order_type="processing",
            action="create",
            agency=self.agency,
            payload={
                "stock_rows": [
                    {
                        "article": "SKU-PICKED",
                        "barcode": "BAR-PICKED",
                        "goods_type": "op",
                        "qty": 4,
                        "box_codes": ["BOX-MIXED-PARTIAL"],
                        "strict_selected_box": True,
                    }
                ]
            },
        )

        rows = build_obr_requested_rows(
            agency=self.agency,
            processing_order_id="PROC-MIXED-PARTIAL",
            request_items=[
                {
                    "requested_article": "SKU-PICKED",
                    "requested_barcodes": ["BAR-PICKED"],
                    "requested_goods_type": "op",
                    "requested_qty": 4,
                }
            ],
        )

        self.assertEqual([(row["requested_article"], row["qty"]) for row in rows], [("SKU-PICKED", 4)])
        self.assertTrue(rows[0]["is_partial_pick"])

    def test_planning_creates_direction_without_processing_reserve(self):
        first = self._box(box_code="BOX-PROC-1", pallet_code="PAL-PROC-1", qty=5)
        second = self._box(box_code="BOX-PROC-2", pallet_code="PAL-PROC-1", qty=5)
        request = self.request_factory.post("/reachtruck/requests/create/", {"priority": "normal"})
        request.user = self.head_user

        response = create_obr_move_request_response(
            request=request,
            agency=self.agency,
            processing_order_id="PROC-FACT-1",
            request_items=[
                {
                    "requested_article": "SKU-PROC",
                    "requested_barcodes": ["BAR-PROC"],
                    "requested_goods_type": "op",
                    "requested_qty": 10,
                }
            ],
        )

        self.assertEqual(response.status_code, 200, response.content.decode("utf-8"))
        self.assertTrue(json.loads(response.content)["ok"])
        self.assertFalse(
            WarehouseReserve.objects.filter(
                agency=self.agency,
                reserve_type=WarehouseReserve.TYPE_PROCESSING,
                context_id="PROC-FACT-1",
            ).exists()
        )
        self.assertFalse(
            WarehouseOperation.objects.filter(
                agency=self.agency,
                operation_type=WarehouseOperation.TYPE_MOVE_TO_PROCESSING,
                context_id="PROC-FACT-1",
            ).exists()
        )
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(first.available_qty, 5)
        self.assertEqual(second.available_qty, 5)
        task = MoveTask.objects.get(request__context_id="PROC-FACT-1")
        self.assertEqual((task.payload or {}).get("processing_pick_fact_mode"), "scanned_boxes_v1")
        self.assertNotIn("warehouse_operation_id", task.payload or {})
        self.assertEqual((task.payload or {}).get("requested_box_count"), 2)
        self.assertEqual((task.payload or {}).get("requested_box_selection"), "")
        self.assertEqual(
            set((task.payload or {}).get("requested_boxes") or []),
            {"BOX-PROC-1", "BOX-PROC-2"},
        )
        self.assertTrue((task.payload or {}).get("requested_box_patterns"))

    def test_replanning_subtracts_already_delivered_quantity(self):
        self._box(box_code="BOX-REPLAN-REMAINDER", pallet_code="PAL-REPLAN-REMAINDER", qty=5)
        previous_request = MoveRequest.objects.create(
            context_type=MoveRequest.CONTEXT_PROCESSING,
            context_id="PROC-REPLAN-REMAINDER",
            agency=self.agency,
            destination_zone="OBR",
            status=MoveRequest.STATUS_PARTIAL,
        )
        MoveTask.objects.create(
            request=previous_request,
            pallet_code="PAL-DELIVERED",
            qty_planned=5,
            qty_done=5,
            status=MoveTask.STATUS_DONE,
            payload={
                "requested_qty": 5,
                "requested_rows": [
                    {
                        "requested_article": "SKU-PROC",
                        "requested_barcodes": ["BAR-PROC"],
                        "requested_goods_type": "op",
                        "qty": 5,
                        "box_code": "BOX-ALREADY-DELIVERED",
                    }
                ],
            },
        )
        MoveTask.objects.create(
            request=previous_request,
            pallet_code="PAL-CANCELED",
            status=MoveTask.STATUS_CANCELED,
        )
        request = self.request_factory.post("/reachtruck/requests/create/", {"priority": "normal"})
        request.user = self.head_user

        response = create_obr_move_request_response(
            request=request,
            agency=self.agency,
            processing_order_id="PROC-REPLAN-REMAINDER",
            request_items=[
                {
                    "requested_article": "SKU-PROC",
                    "requested_barcodes": ["BAR-PROC"],
                    "requested_goods_type": "op",
                    "requested_qty": 10,
                }
            ],
        )

        self.assertEqual(response.status_code, 200, response.content.decode("utf-8"))
        new_request = MoveRequest.objects.get(
            context_type=MoveRequest.CONTEXT_PROCESSING,
            context_id="PROC-REPLAN-REMAINDER",
            status=MoveRequest.STATUS_PLANNED,
        )
        self.assertEqual(new_request.items.get().qty_requested, 5)
        self.assertEqual(new_request.tasks.get().qty_planned, 5)
        self.assertEqual(
            (new_request.tasks.get().payload or {}).get("requested_boxes"),
            ["BOX-REPLAN-REMAINDER"],
        )

    def test_two_drivers_cannot_claim_same_processing_box(self):
        request_one = MoveRequest.objects.create(
            context_type=MoveRequest.CONTEXT_PROCESSING,
            context_id="PROC-CLAIM-1",
            agency=self.agency,
            destination_zone="OBR",
        )
        request_two = MoveRequest.objects.create(
            context_type=MoveRequest.CONTEXT_PROCESSING,
            context_id="PROC-CLAIM-2",
            agency=self.agency,
            destination_zone="OBR",
        )
        task_one = MoveTask.objects.create(
            request=request_one,
            pallet_code="PAL-CLAIM-1",
            legacy_order_id="PROC-CLAIM-TASK-1",
        )
        task_two = MoveTask.objects.create(
            request=request_two,
            pallet_code="PAL-CLAIM-2",
            legacy_order_id="PROC-CLAIM-TASK-2",
        )

        claim_boxes_for_task(task_one, ["BOX-CLAIM"], claimed_by=self.driver_one)

        with self.assertRaisesMessage(ValueError, "Короб BOX-CLAIM уже взят"):
            claim_boxes_for_task(task_two, ["BOX-CLAIM"], claimed_by=self.driver_two)
        self.assertEqual(
            BoxClaim.objects.filter(box_code="BOX-CLAIM", status=BoxClaim.STATUS_CLAIMED).count(),
            1,
        )

    def test_scanned_box_arrival_is_recorded_without_processing_reserve(self):
        snapshot = self._box(box_code="BOX-ARRIVAL", pallet_code="PAL-ARRIVAL", qty=25)

        moved_qty = WarehouseWritePathService.mark_processing_boxes_arrived_to_obr(
            agency=self.agency,
            order_id="PROC-ARRIVAL",
            box_codes=["BOX-ARRIVAL"],
            performed_by=self.driver_one,
            source_document_id="PROC-ARRIVAL-TASK",
        )

        self.assertEqual(moved_qty, 25)
        snapshot.refresh_from_db()
        self.assertEqual(snapshot.zone_code, "OBR")
        self.assertEqual(snapshot.source_context_type, "processing")
        self.assertEqual(snapshot.source_context_id, "PROC-ARRIVAL")
        self.assertEqual(snapshot.processing_reserved_qty, 0)
        self.assertEqual(snapshot.available_qty, 0)
        self.assertFalse(
            WarehouseReserve.objects.filter(
                agency=self.agency,
                reserve_type=WarehouseReserve.TYPE_PROCESSING,
                context_id="PROC-ARRIVAL",
            ).exists()
        )

    def test_processing_consumes_only_scanned_obr_fact(self):
        target = self._box(box_code="BOX-TARGET", pallet_code="PAL-TARGET", qty=10)
        unrelated = self._box(box_code="BOX-OTHER", pallet_code="PAL-OTHER", qty=50)
        WarehouseWritePathService.mark_processing_boxes_arrived_to_obr(
            agency=self.agency,
            order_id="PROC-CONSUME",
            box_codes=["BOX-TARGET"],
            performed_by=self.driver_one,
        )
        operation = WarehouseWritePathService.start_processing(
            agency=self.agency,
            order_id="PROC-CONSUME",
            started_by=self.head_user,
        )

        WarehouseWritePathService.complete_processing(
            operation=operation,
            performed_by=self.head_user,
        )

        target.refresh_from_db()
        unrelated.refresh_from_db()
        operation.refresh_from_db()
        self.assertEqual(target.warehouse_state_code, WarehouseStateCode.PROCESSING_CONSUMED.value)
        self.assertEqual(unrelated.warehouse_state_code, WarehouseStateCode.STORED.value)
        self.assertEqual(unrelated.available_qty, 50)
        self.assertEqual(operation.done_qty, 10)

    def test_plan_handles_ten_thousand_units_without_reserve(self):
        for index in range(10):
            self._box(
                box_code=f"BOX-10K-{index}",
                pallet_code="PAL-10K",
                qty=1000,
                sku="SKU-10K",
                barcode="BAR-10K",
            )

        rows = build_obr_requested_rows(
            agency=self.agency,
            processing_order_id="PROC-10K",
            request_items=[
                {
                    "requested_article": "SKU-10K",
                    "requested_barcodes": ["BAR-10K"],
                    "requested_goods_type": "op",
                    "requested_qty": 10000,
                }
            ],
        )

        self.assertEqual(sum(int(row.get("qty") or 0) for row in rows), 10000)
        self.assertEqual(len({row["box_code"] for row in rows}), 10)
        self.assertFalse(
            WarehouseReserve.objects.filter(
                agency=self.agency,
                reserve_type=WarehouseReserve.TYPE_PROCESSING,
            ).exists()
        )

    def test_piece_pick_plan_creates_fixed_full_and_partial_tasks_without_reserve(self):
        self._box(box_code="BOX-PIECE-10", pallet_code="PAL-PIECE", qty=10)
        self._box(box_code="BOX-PIECE-7", pallet_code="PAL-PIECE", qty=7)
        self._box(box_code="BOX-PIECE-6", pallet_code="PAL-PIECE", qty=6)
        request = self.request_factory.post("/reachtruck/requests/create/", {"priority": "normal"})
        request.user = self.head_user

        response = create_obr_move_request_response(
            request=request,
            agency=self.agency,
            processing_order_id="PROC-PIECE-PLAN",
            request_items=[
                {
                    "requested_article": "SKU-PROC",
                    "requested_barcodes": ["BAR-PROC"],
                    "requested_goods_type": "op",
                    "requested_qty": 15,
                }
            ],
        )

        self.assertEqual(response.status_code, 200, response.content.decode("utf-8"))
        self.assertEqual(json.loads(response.content)["tasks_created"], 2)
        tasks = list(MoveTask.objects.filter(request__context_id="PROC-PIECE-PLAN").order_by("id"))
        full_task = next(task for task in tasks if task.move_mode == "box_full")
        partial_task = next(task for task in tasks if task.move_mode == "box_partial")
        self.assertEqual((full_task.payload or {}).get("requested_boxes"), ["BOX-PIECE-10"])
        self.assertEqual((full_task.payload or {}).get("requested_box_selection"), "")
        self.assertEqual((full_task.payload or {}).get("processing_pick_fact_mode"), "scanned_boxes_v1")
        self.assertEqual((partial_task.payload or {}).get("requested_boxes"), ["BOX-PIECE-6"])
        self.assertEqual((partial_task.payload or {}).get("requested_box_selection"), "")
        self.assertEqual((partial_task.payload or {}).get("processing_pick_fact_mode"), "scanned_units_v1")
        self.assertEqual(
            [
                (row["box_code"], row["qty"], row["barcode_qty"])
                for row in (partial_task.payload or {}).get("requested_rows") or []
            ],
            [("BOX-PIECE-6", 5, {"BAR-PROC": 5})],
        )
        self.assertFalse(
            WarehouseReserve.objects.filter(
                agency=self.agency,
                reserve_type=WarehouseReserve.TYPE_PROCESSING,
                context_id="PROC-PIECE-PLAN",
            ).exists()
        )

    def test_piece_pick_scan_is_exact_idempotent_and_rejects_second_driver(self):
        source = self._box(box_code="BOX-PIECE-TARGET", pallet_code="PAL-PIECE-TARGET", qty=10)
        unrelated = self._box(box_code="BOX-PIECE-OTHER", pallet_code="PAL-PIECE-OTHER", qty=20)
        request = self.request_factory.post("/reachtruck/requests/create/", {"priority": "urgent"})
        request.user = self.head_user
        response = create_obr_move_request_response(
            request=request,
            agency=self.agency,
            processing_order_id="PROC-PIECE-FACT",
            request_items=[
                {
                    "requested_article": "SKU-PROC",
                    "requested_barcodes": ["BAR-PROC"],
                    "requested_goods_type": "op",
                    "requested_qty": 5,
                }
            ],
        )
        self.assertEqual(response.status_code, 200, response.content.decode("utf-8"))
        task = MoveTask.objects.get(request__context_id="PROC-PIECE-FACT")
        self.assertEqual(task.move_mode, "box_partial")
        self.assertEqual((task.payload or {}).get("requested_boxes"), ["BOX-PIECE-TARGET"])

        take_result = take_move_task(
            legacy_order_id=task.legacy_order_id,
            user=self.driver_one,
            employee_id=self.driver_one_employee.id,
            employee_name=self.driver_one_employee.full_name,
        )
        self.assertTrue(take_result.ok, take_result.error)
        second_take = take_move_task(
            legacy_order_id=task.legacy_order_id,
            user=self.driver_two,
            employee_id=self.driver_two_employee.id,
            employee_name=self.driver_two_employee.full_name,
        )
        self.assertFalse(second_take.ok)

        self.assertTrue(
            scan_move_task_step(
                legacy_order_id=task.legacy_order_id,
                scan_value="OS-1-1-1-1",
                user=self.driver_one,
                employee_id=self.driver_one_employee.id,
                employee_name=self.driver_one_employee.full_name,
            ).ok
        )
        self.assertTrue(
            scan_move_task_step(
                legacy_order_id=task.legacy_order_id,
                scan_value="PAL-PIECE-TARGET",
                user=self.driver_one,
                employee_id=self.driver_one_employee.id,
                employee_name=self.driver_one_employee.full_name,
            ).ok
        )
        wrong_box = scan_move_task_step(
            legacy_order_id=task.legacy_order_id,
            scan_value="BOX-PIECE-OTHER",
            user=self.driver_one,
            employee_id=self.driver_one_employee.id,
            employee_name=self.driver_one_employee.full_name,
        )
        self.assertFalse(wrong_box.ok)
        self.assertTrue(
            scan_move_task_step(
                legacy_order_id=task.legacy_order_id,
                scan_value="BOX-PIECE-TARGET",
                user=self.driver_one,
                employee_id=self.driver_one_employee.id,
                employee_name=self.driver_one_employee.full_name,
            ).ok
        )
        for _index in range(5):
            unit_scan = scan_move_task_step(
                legacy_order_id=task.legacy_order_id,
                scan_value="BAR-PROC",
                user=self.driver_one,
                employee_id=self.driver_one_employee.id,
                employee_name=self.driver_one_employee.full_name,
            )
            self.assertTrue(unit_scan.ok, unit_scan.error)
        extra_scan = scan_move_task_step(
            legacy_order_id=task.legacy_order_id,
            scan_value="BAR-PROC",
            user=self.driver_one,
            employee_id=self.driver_one_employee.id,
            employee_name=self.driver_one_employee.full_name,
        )
        self.assertFalse(extra_scan.ok)

        completed = scan_move_task_step(
            legacy_order_id=task.legacy_order_id,
            scan_value="OBR",
            user=self.driver_one,
            employee_id=self.driver_one_employee.id,
            employee_name=self.driver_one_employee.full_name,
        )
        self.assertTrue(completed.ok, completed.error)
        self.assertTrue(completed.completed)
        obr_qty = WarehouseStockSnapshot.objects.filter(
            agency=self.agency,
            source_context_type="processing",
            source_context_id="PROC-PIECE-FACT",
            zone_code="OBR",
            is_archived=False,
        ).values_list("qty", flat=True)
        self.assertEqual(sum(obr_qty), 5)

        repeated_completion = scan_move_task_step(
            legacy_order_id=task.legacy_order_id,
            scan_value="OBR",
            user=self.driver_one,
            employee_id=self.driver_one_employee.id,
            employee_name=self.driver_one_employee.full_name,
        )
        self.assertTrue(repeated_completion.ok, repeated_completion.error)
        self.assertTrue(repeated_completion.completed)
        self.assertEqual(
            sum(
                WarehouseStockSnapshot.objects.filter(
                    agency=self.agency,
                    source_context_type="processing",
                    source_context_id="PROC-PIECE-FACT",
                    zone_code="OBR",
                    is_archived=False,
                ).values_list("qty", flat=True)
            ),
            5,
        )
        source.refresh_from_db()
        unrelated.refresh_from_db()
        self.assertEqual(source.zone_code, "OS")
        self.assertEqual(source.qty, 5)
        self.assertEqual(source.available_qty, 5)
        self.assertEqual(unrelated.zone_code, "OS")
        self.assertEqual(unrelated.qty, 20)
        self.assertEqual(unrelated.available_qty, 20)
        self.assertEqual(
            BoxClaim.objects.filter(
                agency=self.agency,
                box_code="BOX-PIECE-TARGET",
                status=BoxClaim.STATUS_DELIVERED,
            ).count(),
            1,
        )
