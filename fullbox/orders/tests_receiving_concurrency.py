from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor

from django.contrib.auth import get_user_model
from django.db import connections
from django.db.models import Sum
from django.test import Client, TransactionTestCase

from audit.models import OrderAuditEntry
from employees.models import Employee
from orders.services import ReceivingWorkflowService
from receiving_cz.models import ReceivingCzUnit
from sklad.models import WarehouseStockSnapshot
from sklad.services.warehouse_commands import WarehouseCommandService
from sku.models import Agency, SKU, SKUBarcode


class ReceivingConcurrencyTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        self.agency = Agency.objects.create(agn_name="Receiving concurrency", pref="RCC")
        self.sku = SKU.objects.create(
            agency=self.agency,
            sku_code="SKU-RCC-1",
            name="Concurrency item",
            size="42",
            honest_sign=True,
        )
        SKUBarcode.objects.create(
            sku=self.sku,
            value="2200000099551",
            size="42",
            is_primary=True,
        )
        self.user_ids = []
        user_model = get_user_model()
        for index in range(4):
            user = user_model.objects.create_user(
                username=f"receiving_concurrency_{index}",
                password="pwd",
            )
            Employee.objects.create(
                user=user,
                role="storekeeper",
                full_name=f"Receiving Storekeeper {index + 1}",
                is_active=True,
            )
            self.user_ids.append(user.id)

    def _create_order(self, order_id: str, *, receiving_mode: str = "standard"):
        return OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="receiving",
            action="status",
            agency=self.agency,
            user_id=self.user_ids[0],
            description="Receiving concurrency status",
            payload={
                "status": "warehouse",
                "status_label": "В ожидании поставки товара",
                "goods_type": "op",
                "receiving_mode": receiving_mode,
                "items": [
                    {
                        "sku_code": self.sku.sku_code,
                        "name": self.sku.name,
                        "size": self.sku.size,
                        "barcode": "2200000099551",
                        "qty": 1,
                    }
                ],
            },
        )

    def _flow_payload(self, order_id: str, version: int, worker_index: int = 0):
        box_code = f"BOX-{order_id}-{worker_index + 1}"
        pallet_code = f"PAL-{order_id}-{worker_index + 1}"
        boxes = [
            {
                "code": box_code,
                "goods_type": "op",
                "items": [
                    {
                        "sku_code": self.sku.sku_code,
                        "name": self.sku.name,
                        "size": self.sku.size,
                        "barcode": "2200000099551",
                        "qty": 1,
                        "goods_type": "op",
                    }
                ],
                "sealed": True,
            }
        ]
        pallets = [
            {
                "code": pallet_code,
                "boxes": [box_code],
                "items": [],
                "sealed": True,
                "location": {"zone": "PR"},
            }
        ]
        return {
            "flow_action": "draft",
            "boxes_json": json.dumps(boxes),
            "pallets_json": json.dumps(pallets),
            "active_box": "",
            "active_pallet": "",
            "flow_client_version": str(version),
        }

    def _parallel(self, worker):
        barrier = threading.Barrier(4)

        def wrapped(index):
            connections.close_all()
            try:
                return worker(index, barrier)
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=4) as executor:
            return list(executor.map(wrapped, range(4)))

    def test_parallel_first_draft_creates_one_row_and_reports_conflicts(self):
        order_id = "R-RCC-DRAFT"
        self._create_order(order_id)

        def worker(index, barrier):
            user = get_user_model().objects.get(pk=self.user_ids[index])
            payload = self._flow_payload(order_id, 1, index)
            entries = list(
                OrderAuditEntry.objects.filter(
                    order_id=order_id,
                    order_type="receiving",
                ).order_by("created_at", "id")
            )
            barrier.wait()
            result = ReceivingWorkflowService.save_receiving_flow_draft(
                order_id=order_id,
                entries=entries,
                role="storekeeper",
                boxes_raw=payload["boxes_json"],
                pallets_raw=payload["pallets_json"],
                active_box="",
                active_pallet="",
                draft_version=1,
                user=user,
            )
            return bool(result.meta.get("stale_ignored"))

        results = self._parallel(worker)
        drafts = OrderAuditEntry.objects.filter(
            order_id=order_id,
            order_type="receiving",
            action="update",
            payload__flow_state__isnull=False,
        )
        self.assertEqual(drafts.count(), 1)
        self.assertEqual(results.count(False), 1)
        self.assertEqual(results.count(True), 3)

    def test_parallel_standard_close_records_stock_once(self):
        order_id = "R-RCC-CLOSE"
        self._create_order(order_id)

        def worker(index, barrier):
            client = Client()
            client.force_login(get_user_model().objects.get(pk=self.user_ids[index]))
            payload = self._flow_payload(order_id, 1)
            payload.pop("flow_action")
            barrier.wait()
            return client.post(f"/orders/receiving/{order_id}/flow/", payload).status_code

        self.assertEqual(self._parallel(worker), [302] * 4)
        snapshots = WarehouseStockSnapshot.objects.filter(
            source_context_type="receiving",
            source_context_id=order_id,
            sku_code=self.sku.sku_code,
            is_archived=False,
        )
        self.assertEqual(snapshots.count(), 1)
        self.assertEqual(snapshots.aggregate(total=Sum("qty"))["total"], 1)
        self.assertEqual(
            OrderAuditEntry.objects.filter(
                order_id=order_id,
                order_type="receiving",
                payload__act="placement",
                payload__act_state="closed",
            ).count(),
            1,
        )

    def test_parallel_cz_close_records_stock_once(self):
        order_id = "R-RCC-CZ-CLOSE"
        self._create_order(order_id, receiving_mode="cz")
        client = Client()
        client.force_login(get_user_model().objects.get(pk=self.user_ids[0]))
        response = client.post(
            f"/orders/receiving/{order_id}/cz-flow/scan/",
            data=json.dumps(
                {
                    "barcode": "2200000099551",
                    "marking_code": "010460000000000121RCC-CZ-CLOSE",
                    "box_code": "BOX-RCC-CZ-CLOSE",
                    "pallet_code": "PAL-RCC-CZ-CLOSE",
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)

        def worker(index, barrier):
            thread_client = Client()
            thread_client.force_login(get_user_model().objects.get(pk=self.user_ids[index]))
            barrier.wait()
            return thread_client.post(
                f"/orders/receiving/{order_id}/cz-flow/",
                {"action": "close"},
            ).status_code

        self.assertEqual(self._parallel(worker), [302] * 4)
        snapshots = WarehouseStockSnapshot.objects.filter(
            source_context_type="receiving",
            source_context_id=order_id,
            sku_code=self.sku.sku_code,
            is_archived=False,
        )
        self.assertEqual(snapshots.count(), 1)
        self.assertEqual(snapshots.aggregate(total=Sum("qty"))["total"], 1)
        self.assertEqual(ReceivingCzUnit.objects.filter(order_id=order_id).count(), 1)
        self.assertEqual(
            OrderAuditEntry.objects.filter(
                order_id=order_id,
                order_type="receiving",
                payload__act="placement",
                payload__act_state="closed",
            ).count(),
            1,
        )

    def test_parallel_cz_drafts_keep_one_normalized_row(self):
        order_id = "R-RCC-CZ-DRAFT"
        self._create_order(order_id, receiving_mode="cz")

        def worker(index, barrier):
            client = Client()
            client.force_login(get_user_model().objects.get(pk=self.user_ids[index]))
            payload = self._flow_payload(order_id, 1, index)
            barrier.wait()
            return client.post(
                f"/orders/receiving/{order_id}/cz-flow/",
                payload,
            ).status_code

        statuses = self._parallel(worker)
        self.assertEqual(statuses, [200] * 4)
        self.assertEqual(
            OrderAuditEntry.objects.filter(
                order_id=order_id,
                order_type="receiving",
                action="update",
                payload__flow_state__isnull=False,
            ).count(),
            1,
        )

    def test_low_level_receiving_completion_is_idempotent(self):
        order_id = "R-RCC-COMMAND"
        kwargs = {
            "order_id": order_id,
            "agency": self.agency,
            "status_payload": {
                "status": "warehouse",
                "status_label": "Взята в работу",
                "goods_type": "op",
            },
            "has_mismatch": False,
            "receiving_mode": "standard",
            "act_items": [
                {
                    "sku_code": self.sku.sku_code,
                    "name": self.sku.name,
                    "size": self.sku.size,
                    "planned_qty": 1,
                    "actual_qty": 1,
                }
            ],
            "placement_items": [
                {
                    "sku_code": self.sku.sku_code,
                    "name": self.sku.name,
                    "size": self.sku.size,
                    "actual_qty": 1,
                    "box_qty": 1,
                    "pallet_qty": 1,
                }
            ],
            "boxes": [
                {
                    "code": "BOX-RCC-COMMAND",
                    "sealed": True,
                    "items": [
                        {
                            "sku_code": self.sku.sku_code,
                            "name": self.sku.name,
                            "size": self.sku.size,
                            "barcode": "2200000099551",
                            "qty": 1,
                        }
                    ],
                }
            ],
            "pallets": [
                {
                    "code": "PAL-RCC-COMMAND",
                    "boxes": ["BOX-RCC-COMMAND"],
                    "items": [],
                    "sealed": True,
                    "location": {"zone": "PR"},
                }
            ],
            "flow_state": {},
        }

        first = WarehouseCommandService.complete_receiving_flow(**kwargs)
        second = WarehouseCommandService.complete_receiving_flow(**kwargs)

        self.assertEqual(first.status, "completed")
        self.assertEqual(second.status, "already_completed")
        self.assertTrue(second.meta.get("already_completed"))
        snapshots = WarehouseStockSnapshot.objects.filter(
            source_context_type="receiving",
            source_context_id=order_id,
            is_archived=False,
        )
        self.assertEqual(snapshots.count(), 1)
        self.assertEqual(snapshots.aggregate(total=Sum("qty"))["total"], 1)
