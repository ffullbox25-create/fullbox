"""Cabinet ↔ shipping linkages and status visibility (client LK + manager).

Covers the status chain that clients see in «Мои заявки» / dashboard API,
manager review tasks, Ozon single-SO destination bindings, and cancel.
"""

from datetime import timedelta
from io import BytesIO
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.test import Client, RequestFactory, TestCase, override_settings
from django.utils import timezone
from openpyxl import Workbook

from audit.models import OrderAuditEntry
from employees.models import Employee
from shipping.models import (
    ShippingDestination,
    ShippingDestinationItem,
    ShippingOrder,
    ShippingOrderItem,
)
from shipping.ozon_template import OZON_TEMPLATE_HEADERS, apply_ozon_shipping_template_by_warehouse
from shipping.services import (
    _create_ozon_batch_shipping_orders,
    build_shipping_detail_page_context,
)
from shipping.views import _shipping_stock_picker_rows
from sklad.test_utils import create_warehouse_snapshot_row
from sku.models import Agency, Market, MarketCredential
from todo.models import Task


User = get_user_model()


def _xlsx(rows):
    wb = Workbook()
    ws = wb.active
    ws.title = "Состав ГМ поставки"
    for row in rows:
        ws.append(row)
    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


@override_settings(ALLOWED_HOSTS=["*"])
class CabinetShippingStatusLinkTests(TestCase):
    """Live ShippingOrder status → client LK labels / filters / pills."""

    def setUp(self):
        self.client_user = User.objects.create_user(username="cab_ship_client", password="pwd")
        self.manager_user = User.objects.create_user(username="cab_ship_manager", password="pwd")
        self.storekeeper_user = User.objects.create_user(username="cab_ship_storekeeper", password="pwd")
        self.manager = Employee.objects.create(
            full_name="Менеджер кабинет",
            role="manager",
            user=self.manager_user,
            is_active=True,
        )
        self.storekeeper = Employee.objects.create(
            full_name="Кладовщик кабинет",
            role="storekeeper",
            user=self.storekeeper_user,
            is_active=True,
        )
        self.agency = Agency.objects.create(
            agn_name="Клиент кабинет статусы",
            portal_user=self.client_user,
        )
        self.market = Market.objects.create(id=8801, name="Ozon Cabinet")
        create_warehouse_snapshot_row(
            agency=self.agency,
            order_type="receiving",
            order_id="R-CAB-1",
            sku="SKU-CAB-1",
            name="Товар кабинет",
            size="42",
            barcode="310000000001",
            goods_type="Готовый",
            qty=24,
            box_code="BX-CAB-1",
            pallet_code="PL-CAB-1",
            zone="OS",
            row=1,
            section=1,
            tier=1,
            cell=1,
        )
        self.http = Client()
        self.http.force_login(self.client_user)

    def _submit_payload(self):
        picker_key = _shipping_stock_picker_rows(self.agency)[0]["key"]
        eta = (timezone.localtime() + timedelta(days=2)).replace(hour=10, minute=0, second=0, microsecond=0)
        return {
            "delivery_type": ShippingOrder.DELIVERY_MARKETPLACE,
            "slot_date": eta.strftime("%Y-%m-%d"),
            "slot_time": "12:30",
            "eta_at": eta.strftime("%Y-%m-%dT%H:%M"),
            "shipping_barcode": "SHIP-CAB-1",
            "marketplace": str(self.market.id),
            "wb_supply_barcode": "SUP-CAB-1",
            "destination_warehouse": "Склад Ozon",
            "supply_type": ShippingOrder.SUPPLY_BOX,
            "vehicle_type": ShippingOrder.VEHICLE_FULFILLMENT,
            "vehicle_number": "A100BC77",
            "driver_phone": "+7 900 111-22-33",
            "comment": "Кабинет статус тест",
            "action": "submit",
            "stock_key_all[]": [picker_key],
            "stock_boxes[]": ["1"],
        }

    def _lk_shipping_row(self, order_number: str) -> dict:
        response = self.http.get(f"/client/api/v1/dashboard/?client={self.agency.id}")
        self.assertEqual(response.status_code, 200, response.content)
        rows = response.json()["data"]["requests"]
        match = next((row for row in rows if row.get("order_id") == order_number), None)
        self.assertIsNotNone(match, f"order {order_number} missing from LK dashboard: {rows}")
        return match

    def _assert_lk_status(
        self,
        order_number: str,
        *,
        status_label: str,
        filter_status: str,
        status_pill: str,
        bucket: str | None = None,
    ):
        row = self._lk_shipping_row(order_number)
        self.assertEqual(row.get("status_label") or row.get("status_text"), status_label)
        # API may expose filter key as status or filter_status depending on payload shape.
        status_key = row.get("status") or row.get("filter_status")
        self.assertEqual(status_key, filter_status)
        pill = row.get("status_pill") or row.get("pill") or ""
        if pill:
            self.assertEqual(pill, status_pill)
        if bucket is not None and "bucket" in row:
            self.assertEqual(row["bucket"], bucket)

    def test_submit_shows_manager_review_in_lk_and_creates_manager_task(self):
        response = self.http.post(f"/shipping/new/?client={self.agency.id}", data=self._submit_payload())
        self.assertEqual(response.status_code, 302, getattr(response, "content", b"")[:400])
        order = ShippingOrder.objects.get(agency=self.agency)
        self.assertEqual(order.status, ShippingOrder.STATUS_SUBMITTED)

        task = Task.objects.get(route=f"/shipping/{order.pk}/")
        self.assertEqual(task.assigned_to_id, self.manager.id)
        self.assertEqual(task.status, "backlog")

        self._assert_lk_status(
            order.number,
            status_label="На согласовании менеджера",
            filter_status="waiting",
            status_pill="Ожидает",
            bucket="manager",
        )
        detail = self.http.get(
            f"/client/api/v1/requests/shipping/{order.number}/?client={self.agency.id}"
        )
        self.assertEqual(detail.status_code, 200)
        body = detail.json()["data"]
        self.assertEqual(body["status_label"], "На согласовании менеджера")
        self.assertIn(f"/shipping/{order.id}/", body["wms_url"])

    def test_status_chain_draft_submit_reserve_accept_labels(self):
        # Draft via autosave
        draft_payload = self._submit_payload()
        draft_payload["action"] = "save_draft"
        draft_payload["draft_autosave"] = "1"
        draft = self.http.post(f"/shipping/new/?client={self.agency.id}", data=draft_payload)
        self.assertEqual(draft.status_code, 200, draft.content)
        order_pk = draft.json()["order_id"]
        order = ShippingOrder.objects.get(pk=order_pk)
        self.assertEqual(order.status, ShippingOrder.STATUS_DRAFT)
        self._assert_lk_status(
            order.number,
            status_label="Черновик клиента",
            filter_status="waiting",
            status_pill="Ожидает",
            bucket="client",
        )

        # Submit to manager
        submit_payload = self._submit_payload()
        submit_payload["action"] = "submit"
        sent = self.http.post(
            f"/shipping/new/?client={self.agency.id}&order={order_pk}&edit=1",
            data=submit_payload,
        )
        self.assertEqual(sent.status_code, 302, getattr(sent, "content", b"")[:400])
        order.refresh_from_db()
        self.assertEqual(order.status, ShippingOrder.STATUS_SUBMITTED)
        self._assert_lk_status(
            order.number,
            status_label="На согласовании менеджера",
            filter_status="waiting",
            status_pill="Ожидает",
        )

        # Manager approve → reserved
        mgr = Client()
        mgr.force_login(self.manager_user)
        reserved = mgr.post(f"/shipping/{order.pk}/", data={"action": "reserve"})
        self.assertEqual(reserved.status_code, 302)
        order.refresh_from_db()
        self.assertEqual(order.status, ShippingOrder.STATUS_RESERVED)
        self._assert_lk_status(
            order.number,
            status_label="Согласована и передана в работу кладовщику",
            filter_status="processing",
            status_pill="В обработке",
            bucket="warehouse",
        )

        # Storekeeper accept → pick tasks (status becomes picking)
        sk = Client()
        sk.force_login(self.storekeeper_user)
        accepted = sk.post(f"/shipping/{order.pk}/", data={"action": "accept_storekeeper"})
        self.assertEqual(accepted.status_code, 302)
        order.refresh_from_db()
        self.assertIn(
            order.status,
            {ShippingOrder.STATUS_STOREKEEPER_ACCEPTED, ShippingOrder.STATUS_PICKING},
        )
        expected_label = (
            "Доставка в зону отгрузки (ричтрак)"
            if order.status == ShippingOrder.STATUS_PICKING
            else "Принята в работу складом"
        )
        self._assert_lk_status(
            order.number,
            status_label=expected_label,
            filter_status="processing",
            status_pill="В обработке",
        )

    def test_client_cancel_submitted_returns_to_draft_in_lk(self):
        """Before manager approval client cancel unreserves and returns to draft."""
        response = self.http.post(f"/shipping/new/?client={self.agency.id}", data=self._submit_payload())
        self.assertEqual(response.status_code, 302)
        order = ShippingOrder.objects.get(agency=self.agency)
        cancel = self.http.post(f"/shipping/{order.pk}/?client={self.agency.id}", data={"action": "cancel"})
        self.assertEqual(cancel.status_code, 302)
        order.refresh_from_db()
        self.assertEqual(order.status, ShippingOrder.STATUS_DRAFT)
        self.assertFalse(Task.objects.filter(route=f"/shipping/{order.pk}/", status="backlog").exists())
        self._assert_lk_status(
            order.number,
            status_label="Черновик клиента",
            filter_status="waiting",
            status_pill="Ожидает",
            bucket="client",
        )

    def test_manager_hard_cancel_shows_cancelled_in_lk(self):
        response = self.http.post(f"/shipping/new/?client={self.agency.id}", data=self._submit_payload())
        self.assertEqual(response.status_code, 302)
        order = ShippingOrder.objects.get(agency=self.agency)
        mgr = Client()
        mgr.force_login(self.manager_user)
        cancel = mgr.post(
            f"/shipping/{order.pk}/",
            data={"action": "cancel", "cancel_reason": "Отмена заявки менеджером"},
        )
        self.assertEqual(cancel.status_code, 302)
        order.refresh_from_db()
        self.assertEqual(order.status, ShippingOrder.STATUS_CANCELED)
        self._assert_lk_status(
            order.number,
            status_label="ОТМЕНЕНА",
            filter_status="cancelled",
            status_pill="Отменена",
            bucket="done",
        )

    def test_manager_sees_same_order_via_review_task_route_with_staff_label(self):
        self.http.post(f"/shipping/new/?client={self.agency.id}", data=self._submit_payload())
        order = ShippingOrder.objects.get(agency=self.agency)
        task = Task.objects.get(route=f"/shipping/{order.pk}/", assigned_to=self.manager)
        # Client LK wording vs staff DB label for the same submitted status.
        self.assertEqual(order.get_status_display(), "Новая")
        lk = self._lk_shipping_row(order.number)
        self.assertEqual(lk["status_label"], "На согласовании менеджера")
        self.assertNotEqual(lk["status_label"], order.get_status_display())

        mgr = Client()
        mgr.force_login(self.manager_user)
        page = mgr.get(task.route)
        self.assertEqual(page.status_code, 200)
        # UI shows the same public shipping order number as warehouse/audit.
        self.assertContains(page, order.number)
        self.assertContains(page, "Новая")


@override_settings(ALLOWED_HOSTS=["*"])
class CabinetOzonSvyazkiTests(TestCase):
    """Ozon one-SO + destinations bindings visible in cabinets."""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(username="cab_ozon_client", password="pwd")
        cls.manager_user = User.objects.create_user(username="cab_ozon_manager", password="pwd")
        Employee.objects.create(
            full_name="Менеджер Ozon кабинет",
            role="manager",
            user=cls.manager_user,
            is_active=True,
        )
        cls.agency = Agency.objects.create(agn_name="Ozon Cabinet Agency", portal_user=cls.user)
        cls.ozon = Market.objects.create(id=8802, name="Ozon Svyazki")

    def _stock(self):
        return [
            {
                "key": "k30",
                "sku_code": "SKU-OZ-1",
                "name": "Item",
                "size": "",
                "barcode": "2039698778296",
                "goods_type": "",
                "box_qty": 30,
                "available_boxes": 20,
                "is_mixed_box": False,
            }
        ]

    def _ozon_result(self):
        file_obj = _xlsx(
            [
                list(OZON_TEMPLATE_HEADERS),
                ["2039698778296", "SKU-OZ-1", 60, "Склад А", "GM-A", ""],
                ["2039698778296", "SKU-OZ-1", 30, "Склад Б", "GM-B", ""],
            ]
        )
        return apply_ozon_shipping_template_by_warehouse(
            file_obj=file_obj,
            stock_rows=self._stock(),
            file_name="ozon-cab.xlsx",
        )

    def _fake_form(self):
        form = MagicMock()
        form.cleaned_data = {
            "comment": "ozon cabinet",
            "marketplace": self.ozon,
            "transit_address": "Пушкино",
            "supply_number": "OZ-CAB-100",
            "wb_supply_barcode": "",
        }
        proto = ShippingOrder(
            agency=self.agency,
            created_by=self.user,
            delivery_type=ShippingOrder.DELIVERY_MARKETPLACE,
            marketplace=self.ozon,
            supply_type=ShippingOrder.SUPPLY_BOX,
            vehicle_type=ShippingOrder.VEHICLE_FULFILLMENT,
            comment="ozon cabinet; ШК ГМ Ozon: GM-A, GM-B",
            transit_address="Пушкино",
            wb_transit_warehouse=True,
            supply_number="OZ-CAB-100",
        )
        form.save = MagicMock(return_value=proto)
        return form

    @patch("shipping.services.ensure_manager_review_task")
    @patch("shipping.services.apply_client_shipping_reserve_on_submit")
    def test_ozon_single_so_destinations_in_manager_and_lk(self, _reserve, ensure_task):
        request = RequestFactory().post("/")
        request.user = self.user
        order = _create_ozon_batch_shipping_orders(
            request=request,
            form=self._fake_form(),
            selected_client=self.agency,
            stock_rows=self._stock(),
            ozon_result=self._ozon_result(),
            scope="client",
            edit_order=None,
        )
        ensure_task.assert_called()
        self.assertIsNone(order.parent_id)
        self.assertEqual(order.child_orders.count(), 0)
        self.assertEqual(order.destinations.count(), 2)
        self.assertEqual(order.distribution_status, ShippingOrder.DISTRIBUTION_COMPLETE)
        self.assertIn("ШК ГМ", order.comment or "")
        self.assertIn("GM-A", order.comment or "")
        self.assertIn("GM-B", order.comment or "")

        # Manager WMS detail bindings
        detail_request = RequestFactory().get("/")
        detail_request.user = self.manager_user
        context = build_shipping_detail_page_context(
            request=detail_request,
            order=order,
            scope="staff",
            role="manager",
        )
        self.assertTrue(context["ozon_has_distribution"])
        self.assertEqual(len(context["ozon_directions"]), 2)
        warehouses = {row["warehouse"] for row in context["ozon_directions"]}
        self.assertEqual(warehouses, {"Склад А", "Склад Б"})

        # Audit + status so LK lists the order
        OrderAuditEntry.objects.create(
            agency=self.agency,
            order_type="shipping",
            order_id=order.number,
            action="status",
            user=self.user,
            description="Ozon заявка",
            payload={"status": order.status, "shipping_state": order.status},
        )
        http = Client()
        http.force_login(self.user)
        dash = http.get(f"/client/api/v1/dashboard/?client={self.agency.id}")
        self.assertEqual(dash.status_code, 200)
        rows = dash.json()["data"]["requests"]
        own = next(r for r in rows if r["order_id"] == order.number)
        self.assertEqual(own["type"], "shipping")
        self.assertIn(f"/shipping/{order.id}/", own["wms_url"])

        detail = http.get(
            f"/client/api/v1/requests/shipping/{order.number}/?client={self.agency.id}"
        )
        self.assertEqual(detail.status_code, 200)
        body = detail.json()["data"]
        self.assertEqual(len(body["lines"]), 1)
        self.assertEqual(body["lines"][0]["sku_code"], "SKU-OZ-1")
        self.assertEqual(body["lines"][0]["qty_requested"], 90)
        # Single-warehouse field may stay empty for multi-dest Ozon; supply stays on order.
        meta_by_label = {m["label"]: m["value"] for m in body["meta"]}
        self.assertIn("Комментарий", meta_by_label)
        self.assertIn("ШК ГМ", meta_by_label["Комментарий"])
        self.assertIn("GM-A", meta_by_label["Комментарий"])

    def test_destination_items_bind_to_order_lines(self):
        order = ShippingOrder.objects.create(
            number="SO-CAB-OZ-BIND",
            agency=self.agency,
            created_by=self.user,
            status=ShippingOrder.STATUS_SUBMITTED,
            marketplace=self.ozon,
            delivery_type=ShippingOrder.DELIVERY_MARKETPLACE,
            distribution_status=ShippingOrder.DISTRIBUTION_COMPLETE,
            supply_number="OZ-BIND",
            transit_address="Хаб",
            comment="ШК ГМ Ozon: GM-1, GM-2",
        )
        item = ShippingOrderItem.objects.create(
            order=order,
            sku_code="SKU-OZ-1",
            name="Item",
            barcode="2039698778296",
            qty_requested=90,
        )
        dest_a = ShippingDestination.objects.create(
            order=order,
            warehouse_name="Склад А",
            planned_boxes=2,
            sort_order=1,
        )
        dest_b = ShippingDestination.objects.create(
            order=order,
            warehouse_name="Склад Б",
            planned_boxes=1,
            sort_order=2,
        )
        ShippingDestinationItem.objects.create(destination=dest_a, order_item=item, quantity=60)
        ShippingDestinationItem.objects.create(destination=dest_b, order_item=item, quantity=30)

        self.assertEqual(order.destinations.count(), 2)
        self.assertEqual(
            ShippingDestinationItem.objects.filter(destination__order=order).count(),
            2,
        )
        self.assertEqual(
            sum(ShippingDestinationItem.objects.filter(destination__order=order).values_list("quantity", flat=True)),
            item.qty_requested,
        )


@override_settings(ALLOWED_HOSTS=["*"])
class CabinetOzonPullLinkTests(TestCase):
    """Ozon pull endpoint → form payload fields that feed create/submit."""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(username="cab_pull_client", password="pwd")
        cls.agency = Agency.objects.create(agn_name="Ozon Pull Cabinet", portal_user=cls.user)
        cls.ozon = Market.objects.create(id=8803, name="OZON")
        MarketCredential.objects.create(
            id=88031,
            agency=cls.agency,
            market=cls.ozon,
            client_id="555",
            market_key="pull-key",
        )

    @patch("shipping.ozon_supplies.get_ozon_supply_for_agency")
    def test_pull_detail_endpoint_returns_form_and_gm_bindings(self, mock_get):
        mock_get.return_value = {
            "ok": True,
            "order_id": 42,
            "order_number": "2000059517576",
            "gm_barcodes": ["GM-1", "GM-2", "GM-3"],
            "gm_cargoes": [
                {"gm_barcode": "GM-1"},
                {"gm_barcode": "GM-2"},
                {"gm_barcode": "GM-3"},
            ],
            "form": {
                "supply_number": "2000059517576",
                "slot_date": "2026-07-16",
                "slot_time": "18:00",
                "ozon_gm_comment": "ШК ГМ Ozon: GM-1, GM-2, GM-3",
                "transit_address": "МО_ЩЕРБИНКА_ХАБ",
                "wb_transit_warehouse": True,
            },
            "composition": [{"offer_id": "sku1", "barcode": "111", "quantity": 5}],
            "selected_boxes": {},
            "hints": ["ШК ГМ Ozon: 3 шт."],
        }
        http = Client()
        http.force_login(self.user)
        response = http.get(f"/shipping/ozon-supplies/42/?client={self.agency.id}")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["form"]["supply_number"], "2000059517576")
        self.assertEqual(data["gm_barcodes"], ["GM-1", "GM-2", "GM-3"])
        self.assertEqual(len(data["gm_cargoes"]), 3)
        self.assertIn("ШК ГМ Ozon", data["form"]["ozon_gm_comment"])
        mock_get.assert_called_once()
        # Agency from ?client= must be passed through for cabinet scoping.
        called_agency = mock_get.call_args.args[0]
        self.assertEqual(called_agency.id, self.agency.id)
