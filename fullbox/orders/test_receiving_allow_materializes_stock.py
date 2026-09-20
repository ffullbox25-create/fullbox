"""Разрешение размещения паллеты ставит товар на остатки в зоне приемки.

Решение владельца процесса 11.09.2026: остаток создается в момент разрешения,
товар физически остается в PR, ричтрак исключен из оприходования, а акт
размещения становится документом и повторно ничего не приходует.
"""
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase

from audit.models import OrderAuditEntry
from employees.models import Employee
from fbs.goods_types import (
    fbs_client_movement_source_stock_q,
    receiving_placement_allowed_snapshot_ids,
)
from orders.services import ReceivingWorkflowService
from sklad.models import WarehouseLocation, WarehouseStockSnapshot
from sklad.services.operational_locations import receiving_locations
from sklad.services.warehouse_commands import WarehouseCommandService
from sklad.services.warehouse_write_path import WarehouseWritePathService
from sku.models import SKU, Agency, SKUBarcode

ORDER_ID = "PR-TEST-MAT-1"
BARCODE = "4600000000017"


class ReceivingAllowMaterializesStockTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.user = user_model.objects.create_user(username="mat_actor", password="pwd")
        Employee.objects.create(
            full_name="Кладовщик приемки",
            role="storekeeper",
            user=self.user,
            is_active=True,
        )
        self.agency = Agency.objects.create(agn_name="Клиент материализации")
        self.sku = SKU.objects.create(
            agency=self.agency,
            sku_code="SKU-MAT-1",
            name="Товар материализации",
            size="42",
        )
        SKUBarcode.objects.create(sku=self.sku, value=BARCODE, size=self.sku.size)
        self.flow_state = self._flow_state()
        OrderAuditEntry.objects.create(
            order_id=ORDER_ID,
            order_type="receiving",
            action="status",
            agency=self.agency,
            user=self.user,
            description="Статус приемки",
            payload={"status": "warehouse", "goods_type": "gv"},
        )
        OrderAuditEntry.objects.create(
            order_id=ORDER_ID,
            order_type="receiving",
            action="update",
            agency=self.agency,
            user=self.user,
            description="Черновик приемки потоком",
            payload={"flow_state": self.flow_state, "flow_client_version": 1},
        )

    # --- фикстуры ---------------------------------------------------------

    def _box(self, code, *, qty=3):
        return {
            "code": code,
            "sealed": True,
            "goods_type": "gv",
            "items": [
                {
                    "sku": self.sku.sku_code,
                    "sku_code": self.sku.sku_code,
                    "name": self.sku.name,
                    "size": self.sku.size,
                    "barcode": BARCODE,
                    "goods_type": "gv",
                    "qty": qty,
                }
            ],
        }

    def _pallet(self, code, box_codes, *, sealed=True):
        return {
            "code": code,
            "sealed": sealed,
            "goods_type": "gv",
            "boxes": list(box_codes),
            "items": [],
            "location": {"zone": "PR"},
        }

    def _flow_state(self):
        return {
            "boxes": [self._box("BOX-MAT-1-gv"), self._box("BOX-MAT-2-gv")],
            "pallets": [
                self._pallet("PAL-MAT-1-gv", ["BOX-MAT-1-gv"]),
                self._pallet("PAL-MAT-2-gv", ["BOX-MAT-2-gv"]),
            ],
        }

    def allow(self, pallet_code, *, receiving_location_code=""):
        return ReceivingWorkflowService.allow_receiving_flow_pallet_placement(
            order_id=ORDER_ID,
            pallet_code=pallet_code,
            receiving_location_code=receiving_location_code,
            user=self.user,
        )

    def require_concrete_location(self):
        OrderAuditEntry.objects.create(
            order_id=ORDER_ID,
            order_type="receiving",
            action="update",
            agency=self.agency,
            user=self.user,
            description="Требуется конкретное место приёмки",
            payload={"receiving_concrete_location_required": True},
        )

    def create_receiving_location(self, code="PR-1-01", *, is_fbs_visible=False):
        return WarehouseLocation.objects.create(
            warehouse_code="MSK",
            zone_code="PR",
            zone_kind=WarehouseLocation.ZONE_KIND_RECEIVING,
            location_code=code,
            display_name=code,
            is_topology_visible=False,
            is_fbs_visible=is_fbs_visible,
            is_active=True,
        )

    def snapshots(self, pallet_code=None):
        queryset = WarehouseStockSnapshot.objects.filter(
            agency=self.agency,
            source_context_type="receiving",
            source_context_id=ORDER_ID,
        )
        if pallet_code:
            queryset = queryset.filter(parent_container__container_code=pallet_code)
        return queryset

    def close_placement_act(self):
        """Закрыть акт размещения тем же путем, что и боевой код."""
        return WarehouseWritePathService.sync_receiving_placement(
            agency=self.agency,
            order_id=ORDER_ID,
            placement_payload={
                "act": "placement",
                "act_state": "closed",
                "act_boxes": self.flow_state["boxes"],
                "act_pallets": self.flow_state["pallets"],
                "goods_type": "gv",
            },
            performed_by=self.user,
        )

    # --- тесты ------------------------------------------------------------

    def test_allow_puts_pallet_on_stock_in_receiving_zone(self):
        result = self.allow("PAL-MAT-1-gv")
        self.assertEqual(result["status"], "allowed")
        self.assertEqual(result["materialized"]["created"], 1)
        rows = list(self.snapshots())
        self.assertTrue(rows)
        for row in rows:
            self.assertEqual(row.warehouse_state_code, "placed_in_receiving")
            self.assertEqual(row.zone_code, "PR")
            self.assertEqual(row.agency_id, self.agency.id)
        self.assertEqual(sum(int(row.qty or 0) for row in rows), 3)

    def test_required_concrete_location_blocks_empty_value(self):
        self.require_concrete_location()

        result = self.allow("PAL-MAT-1-gv")

        self.assertEqual(result["status"], "receiving_location_required")
        self.assertFalse(self.snapshots().exists())
        self.assertFalse(
            OrderAuditEntry.objects.filter(
                order_id=ORDER_ID,
                payload__event=ReceivingWorkflowService.PALLET_PLACEMENT_PERMISSION_EVENT,
            ).exists()
        )

    def test_required_concrete_location_materializes_at_selected_place(self):
        self.require_concrete_location()
        location = self.create_receiving_location()

        result = self.allow(
            "PAL-MAT-1-gv",
            receiving_location_code=location.location_code,
        )

        self.assertEqual(result["status"], "allowed")
        rows = list(self.snapshots("PAL-MAT-1-gv"))
        self.assertTrue(rows)
        self.assertTrue(all(row.location_id == location.id for row in rows))
        permission = OrderAuditEntry.objects.get(
            order_id=ORDER_ID,
            payload__event=ReceivingWorkflowService.PALLET_PLACEMENT_PERMISSION_EVENT,
        )
        self.assertEqual(permission.payload["receiving_location_code"], location.location_code)

    def test_required_concrete_location_rejects_unknown_place(self):
        self.require_concrete_location()

        result = self.allow(
            "PAL-MAT-1-gv",
            receiving_location_code="PR-NOT-FOUND",
        )

        self.assertEqual(result["status"], "invalid_receiving_location")
        self.assertFalse(self.snapshots().exists())

    def test_required_concrete_location_rejects_fbs_place(self):
        """Приёмка работает только с общим складом: FBS-место PR не принимается."""
        self.require_concrete_location()
        fbs_place = self.create_receiving_location("P-3-1/2-1", is_fbs_visible=True)

        result = self.allow(
            "PAL-MAT-1-gv",
            receiving_location_code=fbs_place.location_code,
        )

        self.assertEqual(result["status"], "fbs_receiving_location")
        self.assertIn("относится к FBS", result["message"])
        self.assertFalse(self.snapshots().exists())
        self.assertFalse(
            OrderAuditEntry.objects.filter(
                order_id=ORDER_ID,
                payload__event=ReceivingWorkflowService.PALLET_PLACEMENT_PERMISSION_EVENT,
            ).exists()
        )

    def test_receiving_locations_offer_only_general_warehouse_pr_places(self):
        general = self.create_receiving_location("PR-1-05")
        fbs_place = self.create_receiving_location("P-4-1/1-1", is_fbs_visible=True)
        shipping = WarehouseLocation.objects.create(
            warehouse_code="MSK",
            zone_code="OTG",
            zone_kind=WarehouseLocation.ZONE_KIND_SHIPPING,
            location_code="OTG-1-1",
            display_name="OTG-1-1",
            is_topology_visible=False,
            is_active=True,
        )
        zone_only = WarehouseLocation.objects.create(
            warehouse_code="MSK",
            zone_code="PR",
            zone_kind=WarehouseLocation.ZONE_KIND_RECEIVING,
            location_code="PR",
            display_name="PR · Зона приемки",
            is_topology_visible=True,
            is_active=True,
        )

        offered = set(receiving_locations().values_list("id", flat=True))

        self.assertIn(general.id, offered)
        self.assertNotIn(fbs_place.id, offered)
        self.assertNotIn(shipping.id, offered)
        self.assertNotIn(zone_only.id, offered)

    def test_placement_act_rejects_fbs_receiving_place(self):
        fbs_place = self.create_receiving_location("P-5-1/1-1", is_fbs_visible=True)

        with mock.patch("billing.warehouse_services.require_completion_facts"):
            with self.assertRaisesMessage(ValueError, "относится к FBS"):
                WarehouseCommandService.complete_receiving_flow(
                    order_id=ORDER_ID,
                    agency=self.agency,
                    status_payload={"goods_type": "gv"},
                    has_mismatch=False,
                    receiving_mode="standard",
                    act_items=[],
                    placement_items=[],
                    boxes=self.flow_state["boxes"],
                    pallets=self.flow_state["pallets"],
                    flow_state=self.flow_state,
                    receiving_location_code=fbs_place.location_code,
                    concrete_location_required=True,
                    performed_by=self.user,
                )

        self.assertFalse(self.snapshots().exists())

    def test_second_allow_of_same_pallet_creates_no_duplicates(self):
        self.allow("PAL-MAT-1-gv")
        before = list(self.snapshots().values_list("id", "qty"))
        again = self.allow("PAL-MAT-1-gv")
        self.assertEqual(again["status"], "already_allowed")
        self.assertEqual(sorted(self.snapshots().values_list("id", "qty")), sorted(before))

    def test_permission_granted_before_the_change_is_materialized_on_retry(self):
        """Разрешения, выданные до правки, дооприходуются при повторном вызове."""
        OrderAuditEntry.objects.create(
            order_id=ORDER_ID,
            order_type="receiving",
            action="update",
            agency=self.agency,
            user=self.user,
            description="Разрешение, выданное старым кодом",
            payload={
                "event": ReceivingWorkflowService.PALLET_PLACEMENT_PERMISSION_EVENT,
                "pallet_code": "PAL-MAT-1-gv",
                "pallet_placement_allowed": True,
            },
        )
        self.assertFalse(self.snapshots().exists())

        result = self.allow("PAL-MAT-1-gv")

        self.assertEqual(result["status"], "already_allowed")
        self.assertEqual(result["materialized"]["created"], 1)
        rows = list(self.snapshots("PAL-MAT-1-gv"))
        self.assertTrue(rows)
        self.assertEqual(sum(int(row.qty or 0) for row in rows), 3)

    def test_only_allowed_pallet_is_materialized(self):
        self.allow("PAL-MAT-1-gv")
        self.assertTrue(self.snapshots("PAL-MAT-1-gv").exists())
        self.assertFalse(self.snapshots("PAL-MAT-2-gv").exists())

    def test_open_pallet_is_not_materialized(self):
        OrderAuditEntry.objects.create(
            order_id=ORDER_ID,
            order_type="receiving",
            action="update",
            agency=self.agency,
            user=self.user,
            description="Паллета распломбирована",
            payload={
                "flow_state": {
                    "boxes": self.flow_state["boxes"],
                    "pallets": [
                        self._pallet("PAL-MAT-1-gv", ["BOX-MAT-1-gv"], sealed=False),
                        self._pallet("PAL-MAT-2-gv", ["BOX-MAT-2-gv"]),
                    ],
                },
                "flow_client_version": 2,
            },
        )
        result = self.allow("PAL-MAT-1-gv")
        self.assertEqual(result["status"], "pallet_open")
        self.assertFalse(self.snapshots().exists())

    def test_materialized_stock_is_visible_to_fbs(self):
        self.allow("PAL-MAT-1-gv")
        allowed_ids = receiving_placement_allowed_snapshot_ids(agency_id=self.agency.id)
        rows = list(self.snapshots())
        self.assertTrue(allowed_ids)
        for row in rows:
            self.assertIn(row.pk, allowed_ids)
        source_rows = self.snapshots().filter(
            fbs_client_movement_source_stock_q(agency_id=self.agency.id)
        )
        self.assertEqual(source_rows.count(), len(rows))

    def test_placement_act_does_not_create_stock_twice_after_goods_left(self):
        self.allow("PAL-MAT-1-gv")
        created_ids = set(self.snapshots("PAL-MAT-1-gv").values_list("id", flat=True))
        self.assertTrue(created_ids)
        # Товар уехал в FBS: строки обнуляются и уходят в архив.
        self.snapshots().update(qty=0, available_qty=0, is_archived=True)

        self.close_placement_act()

        live = self.snapshots("PAL-MAT-1-gv").filter(is_archived=False, qty__gt=0)
        self.assertFalse(
            live.exists(),
            "Акт размещения повторно оприходовал уже отданный товар",
        )
        self.assertEqual(
            set(self.snapshots("PAL-MAT-1-gv").values_list("id", flat=True)),
            created_ids,
            "Строки оприходованной паллеты не должны пересоздаваться актом",
        )
        # Паллета, которую не разрешали, приходуется актом штатно.
        self.assertTrue(
            self.snapshots("PAL-MAT-2-gv").filter(is_archived=False, qty__gt=0).exists()
        )

    def test_placement_act_keeps_live_stock_of_materialized_pallet(self):
        """Акт не должен стирать товар, который еще лежит в приемке."""
        self.allow("PAL-MAT-1-gv")
        before = {
            row_id: qty
            for row_id, qty in self.snapshots("PAL-MAT-1-gv").values_list("id", "qty")
        }
        self.assertTrue(before)

        self.close_placement_act()

        after = {
            row_id: qty
            for row_id, qty in self.snapshots("PAL-MAT-1-gv").values_list("id", "qty")
        }
        self.assertEqual(after, before)

    def test_reachtruck_pickup_does_not_create_stock_twice(self):
        self.allow("PAL-MAT-1-gv")
        created_ids = set(self.snapshots("PAL-MAT-1-gv").values_list("id", flat=True))
        self.snapshots().update(qty=0, available_qty=0, is_archived=True)

        result = ReceivingWorkflowService.materialize_receiving_flow_pallets(
            order_id=ORDER_ID,
            agency=self.agency,
            flow_state=self.flow_state,
            pallet_codes=["PAL-MAT-1-gv"],
            user=self.user,
        )

        self.assertEqual(result["created"], 0)
        self.assertEqual(
            set(self.snapshots("PAL-MAT-1-gv").values_list("id", flat=True)),
            created_ids,
        )

    def test_depleted_pallet_is_not_offered_for_takeout(self):
        self.allow("PAL-MAT-1-gv")
        self.snapshots().update(qty=0, available_qty=0, is_archived=True)
        statuses = ReceivingWorkflowService.build_receiving_flow_pallet_takeout_statuses(
            ORDER_ID,
            self.flow_state["pallets"],
        )
        self.assertEqual(statuses["PAL-MAT-1-gv"]["status"], "depleted")
        self.assertEqual(statuses["PAL-MAT-2-gv"]["status"], "locked")

    def test_failed_materialization_rolls_back_permission(self):
        def boom(**_kwargs):
            raise RuntimeError("складское ядро недоступно")

        original = ReceivingWorkflowService.materialize_receiving_flow_pallets
        ReceivingWorkflowService.materialize_receiving_flow_pallets = staticmethod(boom)
        try:
            with self.assertRaises(RuntimeError):
                self.allow("PAL-MAT-1-gv")
        finally:
            ReceivingWorkflowService.materialize_receiving_flow_pallets = original

        self.assertFalse(
            OrderAuditEntry.objects.filter(
                order_id=ORDER_ID,
                payload__event=ReceivingWorkflowService.PALLET_PLACEMENT_PERMISSION_EVENT,
            ).exists()
        )
        self.assertFalse(self.snapshots().exists())
