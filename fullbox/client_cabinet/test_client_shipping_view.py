"""Unit tests for client shipping 6-step view (phases A–C)."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.template.loader import get_template
from django.test import SimpleTestCase

from client_cabinet.client_shipping_view import build_shipping_client_view, resolve_client_shipping_stage
from client_cabinet.lk_requests import (
    _can_client_request_change,
    _change_request_pending,
    apply_client_request_action,
)


class ClientShippingStageMapTests(SimpleTestCase):
    def test_client_detail_hides_internal_client_manager_responsible(self):
        source = get_template("client_cabinet/dashboard_lk_react.html").template.source

        self.assertIn('!== "менеджер клиента"', source)

    def test_stage_ladder(self):
        cases = [
            ("submitted", "", "", "confirmed", "Подтверждена"),
            ("reserved", "", "", "assembling", "Комплектуется"),
            ("storekeeper_accepted", "", "", "assembling", "Комплектуется"),
            ("picking", "", "", "assembling", "Комплектуется"),
            ("packed", "", "", "ready", "Готова"),
            ("packed", "loading", "routed", "ready", "Готова"),
            ("packed", "departed", "", "in_transit", "В пути"),
            ("shipped", "departed", "", "in_transit", "В пути"),
            ("shipped", "completed", "", "closed", "Закрыта"),
            ("packed", "completed", "delivered", "delivered", "Сдана на МП"),
            ("shipped", "departed", "delivery_failed", "not_delivered", "Не сдана на МП"),
            ("canceled", "", "", "canceled", "Отменена"),
        ]
        for status, trip, routing, key, label in cases:
            with self.subTest(status=status, trip=trip, routing=routing):
                got_key, got_label = resolve_client_shipping_stage(
                    order_status=status,
                    trip_status=trip,
                    routing_status=routing,
                )
                self.assertEqual(got_key, key)
                self.assertEqual(got_label, label)

    def test_delivered_label_uses_marketplace(self):
        key, label = resolve_client_shipping_stage(
            order_status="packed",
            trip_status="completed",
            routing_status="delivered",
            marketplace_name="Wildberries",
        )
        self.assertEqual(key, "delivered")
        self.assertEqual(label, "Сдана на WB")

    def test_route_assigned_stays_ready_until_departed(self):
        key, label = resolve_client_shipping_stage(
            order_status="packed",
            trip_status="planned",
            routing_status="routed",
        )
        self.assertEqual(key, "ready")
        self.assertEqual(label, "Готова")

    def test_build_view_assembling_metrics(self):
        item = SimpleNamespace(
            sku_code="SKU-1",
            name="Товар",
            size="M",
            barcode="200",
            qty_requested=10,
            qty_reserved=8,
            qty_shipped=0,
            comment="",
        )
        order = SimpleNamespace(
            number="SO-000098",
            status="picking",
            marketplace_id=1,
            marketplace=SimpleNamespace(name="WB"),
            supply_type="monopallet",
            get_supply_type_display=lambda self=None: "Монопаллета",
            delivery_type="marketplace",
            destination_warehouse="Коледино",
            slot_date=None,
            slot_time=None,
            planned_ship_date=None,
            expected_boxes=4,
            wb_supply_barcode="WB-1",
            shipping_barcode="",
            supply_number="",
            comment="Срочно",
            created_at=None,
            shipped_at=None,
            shipping_discrepancy_status="",
            items=SimpleNamespace(all=lambda: [item]),
        )
        packing = {
            "box_count": 2,
            "pallet_count": 1,
            "pallets": [
                {
                    "label": "P1",
                    "code": "PAL-1",
                    "box_count": 2,
                    "qty": 8,
                    "boxes": [
                        {"box_code": "BX-1", "qty": 4, "barcode_preview": "SKU-1", "pallet_label": "P1"},
                        {"box_code": "BX-2", "qty": 4, "barcode_preview": "SKU-1", "pallet_label": "P1"},
                    ],
                }
            ],
        }
        with patch("logistics.routing_services.routing_snapshot", return_value={}), patch(
            "shipping.packing._shipping_packing_summary",
            return_value=packing,
        ), patch(
            "client_cabinet.client_shipping_view._load_trip_details",
            return_value={},
        ):
            view = build_shipping_client_view(agency=None, order=order, trip_status="")
        self.assertTrue(view["enabled"])
        self.assertEqual(view["stage_key"], "assembling")
        self.assertEqual(view["status_label"], "Комплектуется")
        self.assertIn("коробов", view["current_operation"].lower() + " " + (view.get("progress_text") or "").lower())
        self.assertEqual(len(view["scale"]), 6)
        self.assertEqual(view["lines"][0]["qty_requested"], 10)
        self.assertEqual(view["lines"][0]["qty_picked"], 8)
        metrics = {m["key"]: m["value"] for m in view["metrics"]}
        self.assertEqual(metrics["requested"], "10")
        self.assertEqual(metrics["boxes"], "2 из 4")
        self.assertTrue(any(b == "WB" for b in view["badges"]))
        self.assertEqual(len(view["boxes"]), 2)
        self.assertEqual(view["boxes"][0]["code"], "BX-1")
        self.assertEqual(len(view["pallets"]), 1)
        self.assertTrue(view["logistics"])
        self.assertTrue(view["warehouse_info"])
        self.assertTrue(any(d["doc_type"] == "excel" for d in view["documents"]))
        self.assertEqual(len(view["tabs"]), 6)

    def test_change_request_pending_and_gate(self):
        self.assertFalse(_change_request_pending({}))
        self.assertTrue(_change_request_pending({"change_requested_by_client": True}))
        self.assertFalse(
            _can_client_request_change(
                bucket="warehouse",
                status_label="Комплектуется",
                payload={"change_requested_by_client": True},
                order_type="shipping",
            )
        )
        self.assertTrue(
            _can_client_request_change(
                bucket="warehouse",
                status_label="Комплектуется",
                payload={},
                order_type="shipping",
            )
        )
        self.assertFalse(
            _can_client_request_change(
                bucket="warehouse",
                status_label="Комплектуется",
                payload={},
                order_type="receiving",
            )
        )

    def test_change_request_action_creates_manager_appeal(self):
        agency = SimpleNamespace(id=1, agn_name="Клиент", inn="1")
        entry = SimpleNamespace(
            payload={"status": "submitted"},
            order_id="SO-CHG-1",
            order_type="shipping",
            agency_id=1,
        )
        with patch("client_cabinet.lk_requests.OrderAuditEntry") as audit_model, patch(
            "client_cabinet.lk_requests.log_order_action"
        ) as log_action, patch(
            "client_cabinet.lk_requests._create_change_approval_task"
        ) as create_task, patch(
            "shipping.models.ShippingOrder"
        ) as ship_model, patch(
            "client_cabinet.web_ui._is_status_entry", return_value=True
        ), patch(
            "client_cabinet.web_ui._order_status_label", return_value="Подтверждена"
        ), patch(
            "client_cabinet.web_ui._order_bucket", return_value="manager"
        ), patch(
            "client_cabinet.web_ui._repair_mojibake_text", side_effect=lambda x: x
        ), patch(
            "client_cabinet.web_ui._shipping_trip_status", return_value=""
        ):
            chain = MagicMock()
            chain.select_related.return_value.order_by.return_value = [entry]
            audit_model.objects.filter.return_value = chain
            ship_model.objects.filter.return_value.first.return_value = SimpleNamespace(
                status="submitted", number="SO-CHG-1", pk=11
            )
            ok, message = apply_client_request_action(
                agency=agency,
                order_type="shipping",
                order_id="SO-CHG-1",
                action="change_request",
                user=None,
                text="Нужно уменьшить количество",
            )
        self.assertTrue(ok, message)
        self.assertIn("менеджеру", message.lower())
        self.assertTrue(log_action.called)
        self.assertTrue(create_task.called)
        self.assertEqual(log_action.call_args.args[0], "change_request")
