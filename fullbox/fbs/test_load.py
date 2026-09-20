import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from unittest import skipUnless

from django.contrib.auth import get_user_model
from django.core.management.color import no_style
from django.db import close_old_connections, connection
from django.test import TransactionTestCase, override_settings
from employees.models import Employee

from fbs.models import (
    FbsIntegrationProfile,
    FbsOrder,
    FbsOrderItem,
    FbsOrderLabel,
    FbsPickBatch,
    FbsPickScanEvent,
    FbsPickVerificationProgress,
    FbsPickingCart,
    FbsStockBalance,
    FbsStorageCell,
    FbsWorkstation,
)
from fbs.services import (
    claim_pick_batch,
    complete_pick_allocation,
    confirm_order_label_scan,
    create_fbs_box,
    create_fbs_pallet,
    prepare_pick_queue,
    register_marketplace_label,
    validate_pick_box_scan,
    validate_pick_cell_scan,
    verify_pick_allocation_unit,
)
from sku.models import Agency, SKU, SKUBarcode
from sklad.models import WarehouseLocation


RUN_LOAD_TESTS = os.environ.get("FBS_RUN_LOAD_TESTS") == "1"


@skipUnless(RUN_LOAD_TESTS, "Set FBS_RUN_LOAD_TESTS=1 to run the 10k FBS load profile.")
@override_settings(
    FBS_MODULE_ENABLED=True,
    FBS_WAREHOUSE_WRITES_ENABLED=True,
    FBS_OUTBOX_ENABLED=False,
    FBS_MARKING_PUSH_ENABLED=False,
    FBS_ZONE_CODE="FBS",
)
class FbsTenThousandOrderLoadTests(TransactionTestCase):
    reset_sequences = True

    ORDER_COUNT = 10_000
    PICKER_COUNT = 20
    CLIENT_COUNT = 10

    def test_10k_orders_with_20_concurrent_pickers(self):
        started = time.perf_counter()
        users = get_user_model()
        with connection.cursor() as cursor:
            for statement in connection.ops.sequence_reset_sql(
                no_style(),
                [users, Employee],
            ):
                cursor.execute(statement)
        pickers = users.objects.bulk_create(
            [users(username=f"load_picker_{index:02d}") for index in range(self.PICKER_COUNT)]
        )
        Employee.objects.bulk_create(
            [
                Employee(
                    user=picker,
                    full_name=f"Load picker {index + 1}",
                    role="picker",
                )
                for index, picker in enumerate(pickers)
            ]
        )
        workstations = FbsWorkstation.objects.bulk_create(
            [
                FbsWorkstation(
                    barcode=f"FBS-WS-{600000 + index:06d}",
                    name=f"Load workstation {index + 1}",
                    printer_name=f"LOAD-PRINTER-{index + 1}",
                    max_parallel_waves=2,
                )
                for index in range(self.PICKER_COUNT)
            ]
        )
        carts = FbsPickingCart.objects.bulk_create(
            [
                FbsPickingCart(
                    barcode=f"FBS-CART-{600000 + index:06d}",
                    name=f"Load cart {index + 1}",
                )
                for index in range(self.PICKER_COUNT)
            ]
        )

        orders = []
        items = []
        sku_by_profile_id = {}
        barcode_by_profile_id = {}
        for client_index in range(self.CLIENT_COUNT):
            agency = Agency.objects.create(agn_name=f"Load client {client_index + 1}")
            sku = SKU.objects.create(
                agency=agency,
                sku_code=f"LOAD-SKU-{client_index + 1}",
                name=f"Load product {client_index + 1}",
            )
            marketplace = (
                FbsIntegrationProfile.MARKETPLACE_WB
                if client_index % 2 == 0
                else FbsIntegrationProfile.MARKETPLACE_OZON
            )
            profile = FbsIntegrationProfile.objects.create(
                agency=agency,
                marketplace=marketplace,
                name=f"Load {marketplace} {client_index + 1}",
                external_account_id=f"load-account-{client_index + 1}",
                external_warehouse_id=f"load-warehouse-{client_index + 1}",
            )
            sku_by_profile_id[profile.id] = sku
            barcode = f"LOAD-BARCODE-{client_index + 1}"
            SKUBarcode.objects.create(sku=sku, value=barcode, is_primary=True)
            barcode_by_profile_id[profile.id] = barcode
            location = WarehouseLocation.objects.create(
                warehouse_code="LOAD",
                zone_code="FBS",
                zone_kind=WarehouseLocation.ZONE_KIND_STORAGE,
                row_no=client_index + 1,
                section_no=1,
                tier_no=1,
                cell_no=1,
                location_code=f"FBS-LOAD-{client_index + 1}-1-1-1",
                is_storage=True,
                is_pickable=True,
            )
            cell = FbsStorageCell.objects.create(
                cell_code=location.location_code,
                location=location,
                purpose=FbsStorageCell.PURPOSE_PICK,
                client_cluster=client_index + 1,
            )
            pallet = create_fbs_pallet(
                agency=agency,
                cell=cell,
                pallet_code=f"LOAD-PALLET-{client_index + 1}",
            )
            box = create_fbs_box(
                agency=agency,
                pallet=pallet,
                box_code=f"LOAD-BOX-{client_index + 1}",
            )
            FbsStockBalance.objects.create(
                agency=agency,
                box=box,
                sku_ref=sku,
                identity_key=f"{client_index + 1:064x}",
                sku_code=sku.sku_code,
                name=sku.name,
                barcode=barcode,
                qty=self.ORDER_COUNT // self.CLIENT_COUNT,
                available_qty=self.ORDER_COUNT // self.CLIENT_COUNT,
            )
            for local_index in range(self.ORDER_COUNT // self.CLIENT_COUNT):
                sequence = client_index * (self.ORDER_COUNT // self.CLIENT_COUNT) + local_index
                orders.append(
                    FbsOrder(
                        profile=profile,
                        external_order_id=f"LOAD-ORDER-{sequence + 1:05d}",
                    )
                )
        FbsOrder.objects.bulk_create(orders, batch_size=1000)
        for order in orders:
            sku = sku_by_profile_id[order.profile_id]
            items.append(
                FbsOrderItem(
                    order=order,
                    external_line_id=f"LOAD-LINE-{order.id}",
                    external_sku=sku.sku_code,
                    barcode=barcode_by_profile_id[order.profile_id],
                    sku=sku,
                    product_name=sku.name,
                    quantity=1,
                )
            )
        FbsOrderItem.objects.bulk_create(items, batch_size=1000)
        imported_at = time.perf_counter()

        queue_result = prepare_pick_queue(
            limit=self.ORDER_COUNT,
            max_orders_per_batch=100,
        )
        queued_at = time.perf_counter()
        batches = list(FbsPickBatch.objects.order_by("id"))
        assignments = [batches[index :: self.PICKER_COUNT] for index in range(self.PICKER_COUNT)]
        barrier = threading.Barrier(self.PICKER_COUNT)

        def run_picker(index):
            close_old_connections()
            picker = users.objects.get(pk=pickers[index].pk)
            workstation = FbsWorkstation.objects.get(pk=workstations[index].pk)
            cart = FbsPickingCart.objects.get(pk=carts[index].pk)
            completed_orders = 0
            barrier.wait(timeout=30)
            try:
                for assigned_batch in assignments[index]:
                    claim_pick_batch(
                        batch_id=assigned_batch.id,
                        assigned_to=picker,
                        workstation_scan=workstation.barcode,
                        cart_scan=cart.barcode,
                    )
                    allocations = list(
                        FbsOrderItem.objects.filter(
                            stock_allocations__pick_task__batch_id=assigned_batch.id
                        )
                        .order_by(
                            "stock_allocations__pick_task__sort_order",
                            "stock_allocations__id",
                        )
                        .values_list(
                            "stock_allocations__id",
                            "stock_allocations__balance__box__pallet__cell__cell_code",
                            "stock_allocations__balance__box__box_code",
                            "stock_allocations__balance__barcode",
                            "stock_allocations__order_item__order_id",
                        )
                    )
                    for allocation_id, cell_code, box_code, barcode, _ in allocations:
                        validate_pick_cell_scan(
                            allocation_id=allocation_id,
                            cell_scan=cell_code,
                            performed_by=picker,
                        )
                        validate_pick_box_scan(
                            allocation_id=allocation_id,
                            box_scan=box_code,
                            performed_by=picker,
                        )
                        complete_pick_allocation(
                            allocation_id=allocation_id,
                            cell_scan=cell_code,
                            box_scan=box_code,
                            item_scan=barcode,
                            performed_by=picker,
                        )
                    for allocation_id, _, _, barcode, order_id in allocations:
                        verify_pick_allocation_unit(
                            allocation_id=allocation_id,
                            item_scan=barcode,
                            performed_by=picker,
                        )
                        label_barcode = f"LOAD-LABEL-{order_id}"
                        label = register_marketplace_label(
                            order_id=order_id,
                            external_label_id=label_barcode,
                            barcode=label_barcode,
                            file_url=f"https://load.test/{order_id}.pdf",
                        )
                        confirm_order_label_scan(
                            label_id=label.id,
                            label_scan=label_barcode,
                            performed_by=picker,
                        )
                        completed_orders += 1
            finally:
                close_old_connections()
            return index, completed_orders

        picker_results = {}
        with ThreadPoolExecutor(max_workers=self.PICKER_COUNT) as executor:
            futures = [executor.submit(run_picker, index) for index in range(self.PICKER_COUNT)]
            for future in as_completed(futures, timeout=900):
                index, count = future.result()
                picker_results[index] = count
        picked_at = time.perf_counter()

        self.assertEqual(queue_result.reserved_orders, self.ORDER_COUNT)
        self.assertEqual(len(batches), 100)
        self.assertEqual(sum(picker_results.values()), self.ORDER_COUNT)
        self.assertEqual(set(picker_results.values()), {500})
        self.assertEqual(
            FbsPickBatch.objects.exclude(status=FbsPickBatch.STATUS_DONE).count(),
            0,
        )
        self.assertEqual(
            FbsOrder.objects.exclude(
                internal_status=FbsOrder.STATUS_READY_FOR_HANDOVER
            ).count(),
            0,
        )
        self.assertEqual(FbsOrderLabel.objects.count(), self.ORDER_COUNT)
        self.assertEqual(FbsPickVerificationProgress.objects.count(), self.ORDER_COUNT)
        self.assertFalse(
            FbsOrderLabel.objects.exclude(status=FbsOrderLabel.STATUS_APPLIED).exists()
        )
        self.assertFalse(FbsStockBalance.objects.exclude(qty=0).exists())
        self.assertEqual(
            FbsIntegrationProfile.objects.filter(
                marketplace=FbsIntegrationProfile.MARKETPLACE_WB
            ).count(),
            self.CLIENT_COUNT // 2,
        )
        self.assertEqual(
            FbsIntegrationProfile.objects.filter(
                marketplace=FbsIntegrationProfile.MARKETPLACE_OZON
            ).count(),
            self.CLIENT_COUNT // 2,
        )
        expected_scan_counts = {
            FbsPickScanEvent.STAGE_WORKSTATION: len(batches),
            FbsPickScanEvent.STAGE_CART: len(batches),
            FbsPickScanEvent.STAGE_CELL: self.ORDER_COUNT,
            FbsPickScanEvent.STAGE_BOX: self.ORDER_COUNT,
            FbsPickScanEvent.STAGE_PICK_ITEM: self.ORDER_COUNT,
            FbsPickScanEvent.STAGE_VERIFY_ITEM: self.ORDER_COUNT,
            FbsPickScanEvent.STAGE_ORDER_LABEL: self.ORDER_COUNT,
        }
        for stage, expected_count in expected_scan_counts.items():
            self.assertEqual(
                FbsPickScanEvent.objects.filter(
                    stage=stage,
                    result=FbsPickScanEvent.RESULT_SUCCESS,
                ).count(),
                expected_count,
                stage,
            )
        self.assertFalse(
            FbsPickScanEvent.objects.filter(result=FbsPickScanEvent.RESULT_ERROR).exists()
        )

        import_seconds = imported_at - started
        queue_seconds = queued_at - imported_at
        flow_seconds = picked_at - queued_at
        total_seconds = picked_at - started
        print(
            "FBS_LOAD_RESULT "
            f"orders={self.ORDER_COUNT} pickers={self.PICKER_COUNT} clients={self.CLIENT_COUNT} "
            f"waves={len(batches)} import_s={import_seconds:.3f} queue_s={queue_seconds:.3f} "
            f"pick_verify_s={flow_seconds:.3f} total_s={total_seconds:.3f} "
            f"orders_per_second={self.ORDER_COUNT / total_seconds:.2f} "
            f"picker_min={min(picker_results.values())} picker_max={max(picker_results.values())}"
        )
