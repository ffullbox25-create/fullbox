from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from audit.models import OrderAuditEntry
from accountant.models import ClientLifecycle
from client_cabinet.client_drafts import find_client_draft, list_client_draft_order_ids
from client_cabinet.other_requests import create_other_request
from employees.models import Employee
from shipping.models import ShippingOrder
from sku.models import Agency, Market
from sklad.test_utils import create_warehouse_snapshot_row
from todo.models import Task


@override_settings(ALLOWED_HOSTS=["*"])
class ClientSingleDraftPerTypeTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.client_user = user_model.objects.create_user(username="draft_client", password="pwd")
        self.manager_user = user_model.objects.create_user(username="draft_manager", password="pwd")
        Employee.objects.create(
            full_name="Менеджер черновиков",
            role="manager",
            user=self.manager_user,
            is_active=True,
        )
        self.agency = Agency.objects.create(agn_name="Клиент один черновик", portal_user=self.client_user)
        ClientLifecycle.objects.create(
            agency=self.agency,
            status=ClientLifecycle.STATUS_ACTIVE,
        )
        self.market = Market.objects.create(id=701, name="Wildberries Draft")
        create_warehouse_snapshot_row(
            agency=self.agency,
            order_type="receiving",
            order_id="R-DRAFT-1",
            sku="SKU-DRAFT-1",
            name="Товар черновик",
            size="42",
            barcode="770000000001",
            goods_type="Готовый",
            qty=24,
            box_code="BX-DRAFT-1",
            pallet_code="PL-DRAFT-1",
            zone="OS",
            row=1,
            section=1,
            tier=1,
            cell=1,
        )
        self.http = Client()
        self.http.force_login(self.client_user)

    def test_receiving_autosave_reuses_single_draft(self):
        first = self.http.post(
            f"/orders/receiving/?client={self.agency.id}",
            data={
                "draft_autosave": "1",
                "submit_action": "draft",
                "eta_at": (timezone.localtime() + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M"),
                "expected_boxes": "2",
                "place_type": "box",
                "vehicle_number": "A111AA77",
                "driver_phone": "+7 900 111-11-11",
                "comment": "черновик 1",
            },
        )
        self.assertEqual(first.status_code, 200, first.content)
        first_data = first.json()
        self.assertTrue(first_data.get("ok"))
        draft_id = str(first_data.get("draft_order_id") or first_data.get("order_id") or "")
        self.assertTrue(draft_id)

        second = self.http.post(
            f"/orders/receiving/?client={self.agency.id}",
            data={
                "draft_autosave": "1",
                "submit_action": "draft",
                "eta_at": (timezone.localtime() + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M"),
                "expected_boxes": "3",
                "place_type": "box",
                "vehicle_number": "A222AA77",
                "driver_phone": "+7 900 222-22-22",
                "comment": "черновик 2",
            },
        )
        self.assertEqual(second.status_code, 200, second.content)
        second_data = second.json()
        self.assertEqual(str(second_data.get("draft_order_id") or ""), draft_id)
        self.assertEqual(list_client_draft_order_ids(agency=self.agency, order_type="receiving"), [draft_id])

        latest = (
            OrderAuditEntry.objects.filter(
                agency=self.agency,
                order_type="receiving",
                order_id=draft_id,
            )
            .order_by("-created_at")
            .first()
        )
        self.assertEqual((latest.payload or {}).get("comment"), "черновик 2")
        self.assertEqual((latest.payload or {}).get("status"), "draft")

    def test_receiving_new_form_redirects_to_existing_draft(self):
        OrderAuditEntry.objects.create(
            agency=self.agency,
            order_type="receiving",
            order_id="88",
            action="create",
            description="Черновик",
            payload={"status": "draft", "status_label": "Черновик", "submit_action": "draft", "items": []},
        )
        response = self.http.get(f"/orders/receiving/?client={self.agency.id}")
        self.assertEqual(response.status_code, 302)
        self.assertIn("edit=88", response["Location"])

    def test_processing_draft_is_shared_by_company_not_creator(self):
        legacy_employee = get_user_model().objects.create_user(
            username="legacy_processing_employee",
            password="pwd",
        )
        draft_id = "draft-processing-legacy-owner"
        OrderAuditEntry.objects.create(
            agency=self.agency,
            order_type="processing",
            order_id=draft_id,
            action="create",
            user=legacy_employee,
            description="Черновик заявки на обработку",
            payload={"status": "draft", "status_label": "Черновик", "submit_action": "draft"},
        )

        self.assertEqual(
            list_client_draft_order_ids(
                agency=self.agency,
                order_type="processing",
                user=legacy_employee,
            ),
            [draft_id],
        )
        self.assertEqual(
            list_client_draft_order_ids(
                agency=self.agency,
                order_type="processing",
                user=self.client_user,
            ),
            [draft_id],
        )

    def test_shipping_autosave_reuses_single_draft_and_submit_promotes(self):
        from shipping.views import _shipping_stock_picker_rows

        picker_key = _shipping_stock_picker_rows(self.agency)[0]["key"]
        eta = (timezone.localtime() + timedelta(days=2)).replace(hour=10, minute=0, second=0, microsecond=0)
        draft_payload = {
            "delivery_type": ShippingOrder.DELIVERY_MARKETPLACE,
            "slot_date": eta.strftime("%Y-%m-%d"),
            "slot_time": "12:30",
            "eta_at": eta.strftime("%Y-%m-%dT%H:%M"),
            "shipping_barcode": "SHIP-DRAFT-1",
            "marketplace": str(self.market.id),
            "wb_supply_barcode": "SUP-DRAFT-1",
            "destination_warehouse": "Склад WB",
            "supply_type": ShippingOrder.SUPPLY_BOX,
            "vehicle_type": ShippingOrder.VEHICLE_FULFILLMENT,
            "vehicle_number": "A123BC77",
            "driver_phone": "+7 900 000-00-00",
            "comment": "черновик отгрузки",
            "action": "save_draft",
            "draft_autosave": "1",
            "stock_key_all[]": [picker_key],
            "stock_boxes[]": ["1"],
        }
        first = self.http.post(f"/shipping/new/?client={self.agency.id}", data=draft_payload)
        self.assertEqual(first.status_code, 200, first.content)
        first_data = first.json()
        self.assertTrue(first_data.get("ok"))
        order_pk = first_data.get("order_id")
        self.assertTrue(order_pk)
        self.assertEqual(ShippingOrder.objects.filter(agency=self.agency, status=ShippingOrder.STATUS_DRAFT).count(), 1)

        draft_payload["comment"] = "черновик отгрузки 2"
        second = self.http.post(f"/shipping/new/?client={self.agency.id}", data=draft_payload)
        self.assertEqual(second.status_code, 200, second.content)
        self.assertEqual(second.json().get("order_id"), order_pk)
        self.assertEqual(ShippingOrder.objects.filter(agency=self.agency, status=ShippingOrder.STATUS_DRAFT).count(), 1)

        submit_payload = dict(draft_payload)
        submit_payload.pop("draft_autosave")
        submit_payload["action"] = "submit"
        submit_payload["comment"] = "отправлено"
        sent = self.http.post(f"/shipping/new/?client={self.agency.id}&order={order_pk}&edit=1", data=submit_payload)
        self.assertEqual(sent.status_code, 302, getattr(sent, "content", b"")[:500])
        order = ShippingOrder.objects.get(pk=order_pk)
        self.assertEqual(order.status, ShippingOrder.STATUS_SUBMITTED)
        self.assertEqual(ShippingOrder.objects.filter(agency=self.agency, status=ShippingOrder.STATUS_DRAFT).count(), 0)

    def test_other_draft_reuses_company_draft_and_submit_promotes_it(self):
        first = create_other_request(
            agency=self.agency,
            user=self.client_user,
            category="custom",
            description="черновик A",
            save_as_draft=True,
        )
        second = create_other_request(
            agency=self.agency,
            user=self.client_user,
            category="custom",
            description="черновик B",
            save_as_draft=True,
        )
        self.assertEqual(first["order_id"], second["order_id"])
        self.assertEqual(len(list_client_draft_order_ids(agency=self.agency, order_type="other")), 1)

        sent = create_other_request(
            agency=self.agency,
            user=self.client_user,
            category="custom",
            description="отправлено",
            save_as_draft=False,
            order_id=first["order_id"],
        )
        self.assertNotEqual(sent["order_id"], first["order_id"])
        self.assertEqual(sent["status"], "submitted")
        self.assertEqual(list_client_draft_order_ids(agency=self.agency, order_type="other"), [])
        self.assertIsNone(find_client_draft(agency=self.agency, order_type="other"))

    def test_staff_client_form_reuses_existing_company_shipping_draft(self):
        from shipping.views import _shipping_stock_picker_rows

        picker_key = _shipping_stock_picker_rows(self.agency)[0]["key"]
        eta = (timezone.localtime() + timedelta(days=2)).replace(hour=10, minute=0, second=0, microsecond=0)
        payload = {
            "agency": str(self.agency.id),
            "delivery_type": ShippingOrder.DELIVERY_MARKETPLACE,
            "slot_date": eta.strftime("%Y-%m-%d"),
            "slot_time": "12:30",
            "eta_at": eta.strftime("%Y-%m-%dT%H:%M"),
            "shipping_barcode": "SHIP-STAFF-DRAFT-1",
            "marketplace": str(self.market.id),
            "wb_supply_barcode": "SUP-STAFF-DRAFT-1",
            "destination_warehouse": "Склад WB",
            "supply_type": ShippingOrder.SUPPLY_BOX,
            "vehicle_type": ShippingOrder.VEHICLE_FULFILLMENT,
            "vehicle_number": "A123BC77",
            "driver_phone": "+7 900 000-00-00",
            "comment": "черновик менеджера",
            "action": "save_draft",
            "draft_autosave": "1",
            "stock_key_all[]": [picker_key],
            "stock_boxes[]": ["1"],
        }
        self.http.force_login(self.manager_user)
        first = self.http.post(f"/shipping/new/?client={self.agency.id}", data=payload)
        self.assertEqual(first.status_code, 200, first.content)
        first_pk = first.json().get("order_id")
        self.assertTrue(first_pk)

        payload["comment"] = "тот же черновик менеджера"
        second = self.http.post(f"/shipping/new/?client={self.agency.id}", data=payload)
        self.assertEqual(second.status_code, 200, second.content)
        self.assertEqual(second.json().get("order_id"), first_pk)
        self.assertEqual(
            ShippingOrder.objects.filter(
                agency=self.agency,
                status=ShippingOrder.STATUS_DRAFT,
            ).count(),
            1,
        )

    def test_other_request_ignores_foreign_or_non_draft_preferred_number(self):
        from client_cabinet.models import OtherRequest

        user_model = get_user_model()
        foreign_user = user_model.objects.create_user(username="foreign_draft_client", password="pwd")
        foreign_agency = Agency.objects.create(
            agn_name="Другой клиент с черновиком",
            portal_user=foreign_user,
        )
        foreign_draft = create_other_request(
            agency=foreign_agency,
            user=foreign_user,
            category="custom",
            description="чужой черновик",
            save_as_draft=True,
        )

        created = create_other_request(
            agency=self.agency,
            user=self.client_user,
            category="custom",
            description="новая заявка клиента",
            save_as_draft=False,
            order_id=foreign_draft["order_id"],
        )

        self.assertNotEqual(created["order_id"], foreign_draft["order_id"])
        foreign_obj = OtherRequest.objects.get(public_number=foreign_draft["order_id"])
        self.assertEqual(foreign_obj.agency_id, foreign_agency.id)
        self.assertEqual(foreign_obj.status, OtherRequest.STATUS_DRAFT)
        self.assertFalse(
            OrderAuditEntry.objects.filter(
                order_type="other",
                order_id=foreign_draft["order_id"],
                agency=self.agency,
            ).exists()
        )
        self.assertEqual(
            OtherRequest.objects.get(public_number=created["order_id"]).agency_id,
            self.agency.id,
        )

        repeated = create_other_request(
            agency=self.agency,
            user=self.client_user,
            category="custom",
            description="ещё одна заявка",
            save_as_draft=False,
            order_id=created["order_id"],
        )
        self.assertNotEqual(repeated["order_id"], created["order_id"])
        created_obj = OtherRequest.objects.get(public_number=created["order_id"])
        self.assertEqual(created_obj.description, "новая заявка клиента")

    def test_other_request_rolls_back_audit_and_task_when_canonical_write_fails(self):
        from client_cabinet.models import OtherRequest

        before = {
            "audit": OrderAuditEntry.objects.filter(order_type="other").count(),
            "requests": OtherRequest.objects.count(),
            "tasks": Task.objects.filter(route__startswith="/orders/other/").count(),
        }
        with patch(
            "client_cabinet.other_request_workflow.upsert_from_create",
            side_effect=RuntimeError("canonical write failed"),
        ):
            with self.assertRaisesRegex(RuntimeError, "canonical write failed"):
                create_other_request(
                    agency=self.agency,
                    user=self.client_user,
                    category="custom",
                    description="должно откатиться",
                    save_as_draft=False,
                )

        self.assertEqual(OrderAuditEntry.objects.filter(order_type="other").count(), before["audit"])
        self.assertEqual(OtherRequest.objects.count(), before["requests"])
        self.assertEqual(
            Task.objects.filter(route__startswith="/orders/other/").count(),
            before["tasks"],
        )

    def test_other_request_workflow_never_reassigns_public_number(self):
        from client_cabinet.models import OtherRequest
        from client_cabinet.other_request_workflow import OtherRequestError, upsert_from_create

        user_model = get_user_model()
        foreign_user = user_model.objects.create_user(username="foreign_workflow_client", password="pwd")
        foreign_agency = Agency.objects.create(
            agn_name="Другой клиент workflow",
            portal_user=foreign_user,
        )
        foreign = create_other_request(
            agency=foreign_agency,
            user=foreign_user,
            category="custom",
            description="исходный владелец",
            save_as_draft=True,
        )

        with self.assertRaises(OtherRequestError):
            upsert_from_create(
                public_number=foreign["order_id"],
                agency=self.agency,
                user=self.client_user,
                category="custom",
                description="попытка перепривязки",
                save_as_draft=False,
            )

        foreign_obj = OtherRequest.objects.get(public_number=foreign["order_id"])
        self.assertEqual(foreign_obj.agency_id, foreign_agency.id)
        self.assertEqual(foreign_obj.description, "исходный владелец")
