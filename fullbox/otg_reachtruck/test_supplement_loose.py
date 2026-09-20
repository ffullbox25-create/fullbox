from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from django.template.loader import render_to_string
from django.utils import timezone

from otg_reachtruck import tests as planner_tests
from otg_reachtruck.models import OtgDeliveryRequest
from otg_reachtruck.services import (
    _subtract_delivered_otg_partial_picks,
    build_box_demands,
    create_otg_shipping_pick_request,
    create_otg_shipping_supplemental_pick,
    get_otg_supplemental_pick_preview,
)
from reachtruck.models import MoveTask
from shipping.box_splits import encode_partial_box_split
from shipping.models import ShippingOrder, ShippingOrderItem
from sklad.models import WarehouseEvent, WarehouseReserve, WarehouseStockSnapshot


@override_settings(USE_OTG_REACHTRUCK_PLANNER=True)
class SupplementWithLooseGoodsTests(TestCase):
    _create_box = planner_tests.OtgReachtruckPlannerTests._create_box
    _create_otg_destination = planner_tests.OtgReachtruckPlannerTests._create_otg_destination

    def setUp(self):
        planner_tests.OtgReachtruckPlannerTests.setUp(self)
        self._create_otg_destination()

    def prepare_order(self):
        self.missing_item = ShippingOrderItem.objects.create(
            order=self.order, sku_code="MISSING", name="Missing full box",
            size="42", barcode="460000000002", goods_type="Ready",
            qty_requested=20, comment="Коробов: 1; кратность: 20",
        )
        self.partial_item = ShippingOrderItem.objects.create(
            order=self.order, sku_code="SKU-OTG", name="OTG Item",
            size="42", barcode="460000000001", goods_type="Ready",
            qty_requested=3,
            comment=encode_partial_box_split({
                "kind": "partial_box_split", "group_key": "loose-supplement",
                "source_boxes": 1, "source_box_codes": ["BOX-PARTIAL"],
                "source_box_pattern": [{"sku": "SKU-OTG", "name": "OTG Item",
                    "size": "42", "barcode": "460000000001",
                    "goods_type": "Ready", "qty": 10}],
                "pick_pattern": [{"sku": "SKU-OTG", "name": "OTG Item",
                    "size": "42", "barcode": "460000000001",
                    "goods_type": "Ready", "qty": 3}],
                "item_pick_qty": 3, "item_source_qty": 10,
            }),
        )
        self.order.status = ShippingOrder.STATUS_PICKING
        self.order.expected_boxes = 2
        self.order.save(update_fields=["status", "expected_boxes", "updated_at"])
        self.missing = self._create_box(
            pallet_code="PAL-MISSING", box_code="BOX-MISSING", row=2,
            sku="MISSING", barcode="460000000002", qty=20,
        )
        self._create_box(pallet_code="PAL-PARTIAL", box_code="BOX-PARTIAL", row=3)
        create_otg_shipping_pick_request(order=self.order, user=self.user)
        self.source_request = OtgDeliveryRequest.objects.get(shipping_order=self.order)
        for task in MoveTask.objects.all():
            task.status = MoveTask.STATUS_FAILED if task.pallet_code == "PAL-MISSING" else MoveTask.STATUS_DONE
            task.payload = {**task.payload, "status": task.status}
            if task.status == MoveTask.STATUS_FAILED:
                task.payload["blocked_reason"] = "no_stock_at_planned_pallet"
            task.save(update_fields=["status", "payload", "updated_at"])
        self.loose_event = WarehouseEvent.objects.create(
            agency=self.agency, event_type="shipping_pick_arrived",
            stock_context_type="shipping", stock_context_id=self.order.number,
            qty=3, occurred_at=timezone.now(),
        )
        self.loose = WarehouseStockSnapshot.objects.create(
            agency=self.agency, source_context_type="shipping",
            source_context_id=self.order.number, sku_code="SKU-OTG", name="OTG Item",
            size="42", barcode="460000000001", goods_type="Ready",
            qty=3, available_qty=0, zone_code="OTG", warehouse_state_code="in_otg",
            last_event=self.loose_event,
        )

    def test_full_box_retry_does_not_require_packing_delivered_units(self):
        self.prepare_order()
        preview = get_otg_supplemental_pick_preview(self.order)
        self.assertTrue(preview["can_create"], preview)
        self.assertEqual(preview["requested_boxes"], 1)
        self.assertEqual(preview["requested_qty"], 20)
        self.assertEqual(preview["requested_qty_by_item_id"], {str(self.missing_item.pk): 20})
        self.assertEqual(preview["excluded_pallet_codes"], ["PAL-MISSING"])
        self.loose.refresh_from_db()
        self.assertEqual(self.loose.qty, 3)
        self.assertIsNone(self.loose.container_id)

    def test_detail_offers_supplement_before_final_packing(self):
        self.prepare_order()
        preview = get_otg_supplemental_pick_preview(self.order)
        html = render_to_string("shipping/detail.html", {
            "order": self.order, "can_write": True,
            "has_loose_packing_items": True, "loose_packing_qty": 3,
            "can_storekeeper_manage_packing": True,
            "can_create_otg_supplemental_pick": preview.get("can_create"),
            "supplemental_pick_preview": preview,
        })
        self.assertIn("Создать добор: 1 короб. / 20 шт.", html)
        self.assertIn("Сначала создайте добор недостающего товара", html)
        self.assertNotIn("проверка состава и создание добора недоступны", html)

    def test_completed_task_without_sufficient_warehouse_fact_does_not_hide_shortage(self):
        self.prepare_order()
        self.loose.qty = 2
        self.loose.save(update_fields=["qty"])
        self.assertFalse(get_otg_supplemental_pick_preview(self.order)["can_create"])

    def test_other_orders_loose_goods_do_not_cover_this_order(self):
        self.prepare_order()
        self.loose.source_context_id = "OTG-OTHER"
        self.loose.save(update_fields=["source_context_id"])
        self.loose_event.stock_context_id = "OTG-OTHER"
        self.loose_event.save(update_fields=["stock_context_id"])
        self.assertFalse(get_otg_supplemental_pick_preview(self.order)["can_create"])

    def test_packed_partial_goods_are_counted_by_picked_not_source_box_quantity(self):
        self.prepare_order()
        packed = self._create_box(pallet_code="PAL-PACKED", box_code="BOX-PACKED",
                                  qty=3, zone="OTG", warehouse_state_code="in_otg")
        packed.source_context_type = "shipping"
        packed.source_context_id = self.order.number
        packed.save(update_fields=["source_context_type", "source_context_id"])
        self.loose.qty = 0
        self.loose.save(update_fields=["qty"])
        preview = get_otg_supplemental_pick_preview(self.order)
        self.assertTrue(preview["can_create"], preview)
        self.assertEqual(preview["requested_qty"], 20)

    def test_command_creates_only_missing_box_and_prevents_duplicate_retry(self):
        self.prepare_order()
        replacement = self._create_box(pallet_code="PAL-REPLACEMENT", box_code="BOX-REPLACEMENT",
                                       row=4, sku="MISSING", barcode="460000000002", qty=20)
        before = list(WarehouseStockSnapshot.objects.order_by("pk").values(
            "pk", "qty", "available_qty", "zone_code", "container_id"))
        move_request, move_ids, preview = create_otg_shipping_supplemental_pick(order=self.order, user=self.user)
        self.assertEqual(len(move_ids), 1)
        task = MoveTask.objects.get(request=move_request)
        self.assertEqual(task.pallet_code, replacement.parent_container.container_code)
        self.assertEqual(preview["requested_qty"], 20)
        self.assertEqual(list(WarehouseStockSnapshot.objects.order_by("pk").values(
            "pk", "qty", "available_qty", "zone_code", "container_id")), before)
        with self.assertRaises(ValidationError):
            create_otg_shipping_supplemental_pick(order=self.order, user=self.user)

    def test_no_replacement_does_not_create_task_or_change_stock(self):
        self.prepare_order()
        before_tasks = MoveTask.objects.count()
        before = list(WarehouseStockSnapshot.objects.order_by("pk").values("pk", "qty", "available_qty"))
        with self.assertRaises(ValidationError):
            create_otg_shipping_supplemental_pick(order=self.order, user=self.user)
        self.assertEqual(MoveTask.objects.count(), before_tasks)
        self.assertEqual(list(WarehouseStockSnapshot.objects.order_by("pk").values("pk", "qty", "available_qty")), before)

    def test_same_loose_units_cannot_cover_two_demands(self):
        self.prepare_order()
        partial = next(row for row in build_box_demands(self.order) if row["pick_composition"])
        remaining = _subtract_delivered_otg_partial_picks(
            self.order, [partial, {**partial, "demand_key": "second-partial"}], {"boxes": []},
        )
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0]["demand_key"], "second-partial")

    def test_full_box_already_counted_cannot_also_cover_partial_units(self):
        self.prepare_order()
        self.loose.qty = 0
        self.loose.save(update_fields=["qty"])
        packed = self._create_box(pallet_code="PAL-PACKED", box_code="BOX-PACKED",
                                  qty=3, zone="OTG", warehouse_state_code="in_otg")
        packed.source_context_type = "shipping"
        packed.source_context_id = self.order.number
        packed.save(update_fields=["source_context_type", "source_context_id"])
        partial = next(row for row in build_box_demands(self.order) if row["pick_composition"])
        remaining = _subtract_delivered_otg_partial_picks(
            self.order, [partial], {"boxes": ["BOX-PACKED"]},
        )
        self.assertEqual(len(remaining), 1)

    def test_replacement_reserved_for_another_order_is_not_taken(self):
        self.prepare_order()
        other = ShippingOrder.objects.create(number="OTG-OTHER", agency=self.agency,
                                            created_by=self.user, status=ShippingOrder.STATUS_RESERVED)
        replacement = self._create_box(pallet_code="PAL-OTHER", box_code="BOX-OTHER",
                                       row=4, sku="MISSING", barcode="460000000002", qty=20)
        reserve = WarehouseReserve.objects.create(
            agency=self.agency, reserve_type=WarehouseReserve.TYPE_SHIPPING,
            context_type="shipping", context_id=other.number, sku_code="MISSING",
            size="42", barcode="460000000002", goods_type="Ready", qty_reserved=20,
            source_document_type="shipping", source_document_id=other.number,
        )
        replacement.available_qty = 0
        replacement.shipping_reserved_qty = 20
        replacement.save(update_fields=["available_qty", "shipping_reserved_qty"])
        before_tasks = MoveTask.objects.count()
        with self.assertRaises(ValidationError):
            create_otg_shipping_supplemental_pick(order=self.order, user=self.user)
        self.assertEqual(MoveTask.objects.count(), before_tasks)
        reserve.refresh_from_db()
        replacement.refresh_from_db()
        self.assertEqual((reserve.qty_reserved, reserve.qty_satisfied, reserve.status), (20, 0, "active"))
        self.assertEqual((replacement.qty, replacement.available_qty, replacement.shipping_reserved_qty), (20, 0, 20))
