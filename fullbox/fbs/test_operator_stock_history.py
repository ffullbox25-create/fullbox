from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from employees.models import Employee
from fbs.models import (
    FbsBox,
    FbsClientMovementRequest,
    FbsClientMovementRequestLine,
    FbsIntegrationProfile,
    FbsOrder,
    FbsOrderItem,
    FbsOrderStockAllocation,
    FbsPallet,
    FbsPickBatch,
    FbsPickTask,
    FbsReplenishmentAllocation,
    FbsReplenishmentLine,
    FbsReplenishmentPlan,
    FbsStockBalance,
    FbsStockMovement,
    FbsStorageCell,
)
from sklad.models import (
    WarehouseContainer,
    WarehouseEvent,
    WarehouseLocation,
    WarehouseOperation,
    WarehouseOperationTask,
    WarehouseReserve,
    WarehouseStockSnapshot,
)
from sku.models import Agency, SKU


@override_settings(FBS_MODULE_ENABLED=True)
class OperatorStockHistoryTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="storekeeper_stock_history",
            password="pwd",
        )
        Employee.objects.create(
            full_name="Кладовщик отчёта FBS",
            user=self.user,
            role="storekeeper",
            is_active=True,
        )
        self.agency = Agency.objects.create(
            agn_name="Клиент отчёта FBS",
            short_name="Клиент FBS",
        )
        self.sku = SKU.objects.create(
            agency=self.agency,
            sku_code="ART-0152",
            name="Товар для отчёта",
        )
        source_location = WarehouseLocation.objects.create(
            warehouse_code="MSK",
            zone_code="PR",
            zone_kind=WarehouseLocation.ZONE_KIND_RECEIVING,
            location_code="PR-HISTORY",
            display_name="Приёмка отчёта",
        )
        fbs_location = WarehouseLocation.objects.create(
            warehouse_code="MSK",
            zone_code="OS",
            zone_kind=WarehouseLocation.ZONE_KIND_STORAGE,
            row_no=91,
            section_no=1,
            tier_no=1,
            cell_no=1,
            location_code="OS-91-1-1-1",
            display_name="Ячейка отчёта FBS",
        )
        source_container = WarehouseContainer.objects.create(
            agency=self.agency,
            container_type=WarehouseContainer.TYPE_BOX,
            container_code="SRC-BOX-0152",
            current_location=source_location,
        )
        cell = FbsStorageCell.objects.create(
            cell_code="FBS@HISTORY-1",
            location=fbs_location,
            is_active=True,
        )
        pallet = FbsPallet.objects.create(
            agency=self.agency,
            pallet_code="FBS-PAL-HISTORY",
            cell=cell,
            status=FbsPallet.STATUS_ACTIVE,
        )
        box = FbsBox.objects.create(
            agency=self.agency,
            pallet=pallet,
            box_code="FBS-BOX-HISTORY",
            status=FbsBox.STATUS_ACTIVE,
        )
        self.balance = FbsStockBalance.objects.create(
            agency=self.agency,
            box=box,
            sku_ref=self.sku,
            identity_key="a" * 64,
            sku_code=self.sku.sku_code,
            name=self.sku.name,
            barcode="4660406800152",
            goods_type="gv",
            qty=0,
            available_qty=0,
            reserved_qty=0,
        )
        movement_request = FbsClientMovementRequest.objects.create(
            agency=self.agency,
            mode=FbsClientMovementRequest.MODE_BOX,
            status=FbsClientMovementRequest.STATUS_COMPLETED,
            requested_qty=1,
            requested_box_count=1,
            actual_moved_qty=1,
            actual_moved_box_count=1,
        )
        movement_line = FbsClientMovementRequestLine.objects.create(
            request=movement_request,
            sku=self.sku,
            barcode="4660406800152",
            sku_code=self.sku.sku_code,
            product_name=self.sku.name,
            requested_qty=1,
            units_per_box=1,
            requested_box_count=1,
        )
        reserve = WarehouseReserve.objects.create(
            agency=self.agency,
            reserve_type=WarehouseReserve.TYPE_MANUAL,
            context_type="fbs_replenishment",
            context_id="history",
            sku_ref=self.sku,
            sku_code=self.sku.sku_code,
            barcode="4660406800152",
            goods_type="gv",
            qty_reserved=1,
            qty_allocated=1,
            qty_satisfied=1,
            status=WarehouseReserve.STATUS_SATISFIED,
        )
        operation = WarehouseOperation.objects.create(
            agency=self.agency,
            operation_type=WarehouseOperation.TYPE_INTERNAL_RELOCATION,
            context_type="fbs_replenishment",
            context_id="history",
            reserve=reserve,
            source_location=source_location,
            destination_location=fbs_location,
            source_zone_code="PR",
            destination_zone_code="OS",
            status=WarehouseOperation.STATUS_DONE,
            planned_qty=1,
            done_qty=1,
        )
        plan = FbsReplenishmentPlan.objects.create(
            agency=self.agency,
            client_movement_request=movement_request,
            mode=FbsReplenishmentPlan.MODE_BOX,
            status=FbsReplenishmentPlan.STATUS_DONE,
            target_cell=cell,
            target_pallet=pallet,
            target_box=box,
            warehouse_operation=operation,
            planned_qty=1,
            moved_qty=1,
        )
        replenishment_line = FbsReplenishmentLine.objects.create(
            plan=plan,
            client_movement_line=movement_line,
            source_container=source_container,
            target_box=box,
            qty_requested=1,
            qty_planned=1,
            qty_moved=1,
            status=FbsReplenishmentLine.STATUS_DONE,
        )
        source_snapshot = WarehouseStockSnapshot.objects.create(
            agency=self.agency,
            source_context_type="receiving",
            source_context_id="history",
            sku_ref=self.sku,
            sku_code=self.sku.sku_code,
            name=self.sku.name,
            barcode="4660406800152",
            goods_type="gv",
            qty=0,
            available_qty=0,
            container=source_container,
            container_code=source_container.container_code,
            location=source_location,
            zone_code="PR",
            zone_kind=WarehouseLocation.ZONE_KIND_RECEIVING,
            is_archived=True,
        )
        task = WarehouseOperationTask.objects.create(
            operation=operation,
            task_type=WarehouseOperationTask.TYPE_BOX_MOVE,
            container=source_container,
            from_location=source_location,
            to_location=fbs_location,
            from_zone_code="PR",
            to_zone_code="OS",
            qty_planned=1,
            qty_done=1,
            status=WarehouseOperationTask.STATUS_DONE,
        )
        allocation = FbsReplenishmentAllocation.objects.create(
            line=replenishment_line,
            source_snapshot=source_snapshot,
            target_box=box,
            warehouse_reserve=reserve,
            warehouse_task=task,
            qty_planned=1,
            qty_moved=1,
            source_snapshot_version=1,
            status=FbsReplenishmentAllocation.STATUS_DONE,
        )
        arrived_at = timezone.now() - timedelta(hours=2)
        event = WarehouseEvent.objects.create(
            agency=self.agency,
            event_type="fbs_replenishment_completed",
            stock_context_type="fbs_replenishment",
            stock_context_id=str(plan.id),
            container=source_container,
            operation=operation,
            operation_task=task,
            reserve=reserve,
            from_location=source_location,
            to_location=fbs_location,
            from_zone_code="PR",
            to_zone_code="OS",
            qty=1,
            occurred_at=arrived_at,
        )
        FbsStockMovement.objects.create(
            allocation=allocation,
            source_snapshot=source_snapshot,
            target_balance=self.balance,
            warehouse_event=event,
            qty=1,
            occurred_at=arrived_at,
        )
        profile = FbsIntegrationProfile.objects.create(
            agency=self.agency,
            marketplace=FbsIntegrationProfile.MARKETPLACE_WB,
            name="WB отчёт",
            external_warehouse_id="WB-HISTORY",
            is_active=True,
        )
        order = FbsOrder.objects.create(
            profile=profile,
            external_order_id="5592540237",
            internal_status=FbsOrder.STATUS_HANDED_OVER,
            marketplace_status="complete",
        )
        order_item = FbsOrderItem.objects.create(
            order=order,
            external_line_id="history-line",
            external_sku="ART-MP-0152",
            barcode="4660406800152",
            sku=self.sku,
            product_name=self.sku.name,
            quantity=1,
        )
        batch = FbsPickBatch.objects.create(
            agency=self.agency,
            status=FbsPickBatch.STATUS_DONE,
            planned_qty=1,
            picked_qty=1,
        )
        pick_task = FbsPickTask.objects.create(
            batch=batch,
            order=order,
            status=FbsPickTask.STATUS_PICKED,
            planned_qty=1,
            picked_qty=1,
            completed_at=timezone.now() - timedelta(hours=1),
        )
        FbsOrderStockAllocation.objects.create(
            order_item=order_item,
            balance=self.balance,
            pick_task=pick_task,
            qty_reserved=1,
            qty_picked=1,
            status=FbsOrderStockAllocation.STATUS_PICKED,
            picked_at=timezone.now() - timedelta(hours=1),
        )
        self.movement_number = movement_request.number
        self.client.force_login(self.user)

    def test_storekeeper_sees_arrival_and_outgoing_history(self):
        before = (
            FbsStockBalance.objects.count(),
            FbsStockMovement.objects.count(),
            FbsOrderStockAllocation.objects.count(),
        )

        response = self.client.get(
            "/fbs/operator/stock-history/",
            {"agency": self.agency.id, "q": "0152"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "История товара")
        self.assertContains(response, "Когда товар пришёл и куда ушёл")
        self.assertContains(response, self.movement_number)
        self.assertContains(response, "5592540237")
        self.assertContains(response, "ART-0152")
        self.assertContains(response, "4660406800152")
        self.assertContains(response, "+1 шт.")
        self.assertContains(response, "−1 шт.")
        self.assertEqual(
            before,
            (
                FbsStockBalance.objects.count(),
                FbsStockMovement.objects.count(),
                FbsOrderStockAllocation.objects.count(),
            ),
        )

    def test_article_filter_hides_unrelated_history(self):
        response = self.client.get(
            "/fbs/operator/stock-history/",
            {"agency": self.agency.id, "q": "НЕИЗВЕСТНЫЙ-АРТИКУЛ"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Операций по выбранным фильтрам не найдено")
        self.assertNotContains(response, "5592540237")
