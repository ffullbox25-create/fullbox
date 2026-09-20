from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import RequestFactory, TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from audit.models import OrderAuditEntry
from client_cabinet.api_views import (
    _build_request_payloads,
    _latest_client_request_entries,
    api_requests,
)
from client_cabinet.lk_requests import build_request_detail
from shipping.models import ShippingOrder
from sku.models import Agency


class ClientRequestVisibilityTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="client_request_visibility",
            password="pwd",
        )
        self.agency = Agency.objects.create(
            agn_name="Клиент видимости заявок",
            portal_user=self.user,
        )

    def _entry(
        self,
        *,
        order_id,
        order_type="receiving",
        agency=None,
        created_at=None,
        status="submitted",
    ):
        return OrderAuditEntry(
            agency=agency or self.agency,
            order_type=order_type,
            order_id=str(order_id),
            action="status",
            payload={"status": status, "status_label": status},
            created_at=created_at or timezone.now(),
        )

    def test_noisy_history_does_not_hide_an_older_current_request(self):
        now = timezone.now()
        rows = [
            self._entry(
                order_id="NOISY-REQUEST",
                created_at=now - timedelta(seconds=index),
            )
            for index in range(520)
        ]
        rows.append(
            self._entry(
                order_id="OLDER-CURRENT-REQUEST",
                created_at=now - timedelta(days=2),
            )
        )
        OrderAuditEntry.objects.bulk_create(rows, batch_size=200)

        with CaptureQueriesContext(connection) as queries:
            entries = _latest_client_request_entries(self.agency)

        self.assertEqual(len(queries), 1)
        self.assertEqual(
            {entry.order_id for entry in entries},
            {"NOISY-REQUEST", "OLDER-CURRENT-REQUEST"},
        )

    def test_request_limit_is_not_applied_before_events_are_consolidated(self):
        now = timezone.now()
        OrderAuditEntry.objects.bulk_create(
            [
                self._entry(
                    order_id=f"REQUEST-{index:03d}",
                    created_at=now - timedelta(seconds=index),
                )
                for index in range(125)
            ],
            batch_size=200,
        )

        entries = _latest_client_request_entries(self.agency)

        self.assertEqual(len(entries), 125)
        self.assertIn("REQUEST-124", {entry.order_id for entry in entries})

    def test_same_number_for_another_client_is_not_merged(self):
        other = Agency.objects.create(agn_name="Другой клиент")
        OrderAuditEntry.objects.bulk_create(
            [
                self._entry(
                    agency=self.agency,
                    order_id="SAME-001",
                    status="submitted",
                ),
                self._entry(
                    agency=other,
                    order_id="SAME-001",
                    status="completed",
                ),
            ]
        )

        own = _latest_client_request_entries(self.agency)
        foreign = _latest_client_request_entries(other)

        self.assertEqual(len(own), 1)
        self.assertEqual(len(foreign), 1)
        self.assertEqual(own[0].agency_id, self.agency.id)
        self.assertEqual(foreign[0].agency_id, other.id)
        self.assertEqual((own[0].payload or {}).get("status"), "submitted")
        self.assertEqual((foreign[0].payload or {}).get("status"), "completed")

    def test_requests_api_has_bounded_safe_optional_pagination(self):
        rows = [{"id": index, "number": f"R-{index:03d}"} for index in range(205)]
        request = RequestFactory().get(
            "/client/api/v1/requests/",
            {"page": "999", "page_size": "9999"},
        )
        request.user = self.user

        with (
            patch(
                "client_cabinet.api_views._ctx",
                return_value=(self.agency, True, True),
            ),
            patch("client_cabinet.api_views._request_payloads", return_value=rows),
        ):
            response = api_requests(request)

        self.assertEqual(response.status_code, 200)
        import json

        data = json.loads(response.content)["data"]
        self.assertEqual(len(data["requests"]), 5)
        self.assertEqual(data["pagination"]["page"], 2)
        self.assertEqual(data["pagination"]["page_size"], 200)
        self.assertEqual(data["pagination"]["total_pages"], 2)
        self.assertEqual(data["pagination"]["total_count"], 205)

    def test_shipping_statuses_are_loaded_with_fixed_query_count(self):
        now = timezone.now()
        audits = []
        for index in range(40):
            number = f"OTG-CLIENT-BATCH-{index:03d}"
            ShippingOrder.objects.create(
                agency=self.agency,
                number=number,
                status=ShippingOrder.STATUS_SUBMITTED,
            )
            audits.append(
                self._entry(
                    order_id=number,
                    order_type="shipping",
                    created_at=now - timedelta(seconds=index),
                )
            )
        OrderAuditEntry.objects.bulk_create(audits, batch_size=200)

        with CaptureQueriesContext(connection) as queries:
            rows = _build_request_payloads(self.agency)

        self.assertEqual(len(rows), 40)
        self.assertLessEqual(len(queries), 4)

    def test_processing_technical_event_does_not_hide_latest_status(self):
        now = timezone.now()
        status_entry = OrderAuditEntry.objects.create(
            agency=self.agency,
            order_type="processing",
            order_id="42",
            action="status",
            description="Проверка качества пройдена",
            payload={
                "status": "processing_in_work",
                "status_label": "проверка качества пройдена",
                "processing_stage": "quality_approved",
                "processing_act": {
                    "version": 1,
                    "status": "awaiting_confirmations",
                    "manager_response": "pending",
                    "declared_qty": 380,
                    "processed_qty": 380,
                    "boxed_qty": 380,
                },
            },
            created_at=now,
        )
        OrderAuditEntry.objects.create(
            agency=self.agency,
            order_type="processing",
            order_id="42",
            action="update",
            description="Созданы задания ричтракеру",
            payload={"processing_warehouse_move_auto": {"created": 2}},
            created_at=now + timedelta(seconds=1),
        )

        entries = _latest_client_request_entries(self.agency)
        rows = _build_request_payloads(self.agency)

        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].id, status_entry.id)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status_label"], "Обработка подтверждена, ожидает проверки менеджера")
        self.assertEqual(rows[0]["bucket"], "manager")

    def test_processing_quality_result_is_visible_read_only(self):
        OrderAuditEntry.objects.create(
            agency=self.agency,
            user=self.user,
            order_type="processing",
            order_id="42",
            action="status",
            description="Проверка качества пройдена",
            payload={
                "status": "processing_in_work",
                "status_label": "проверка качества пройдена",
                "processing_stage": "quality_approved",
                "marketplace": "WB",
                "stock_rows": [
                    {
                        "article": "нож105",
                        "name": "Нож складной туристический",
                        "size": "0",
                        "barcode": "2039530939427",
                        "qty": 180,
                    }
                ],
                "processing_act": {
                    "version": 1,
                    "status": "awaiting_confirmations",
                    "manager_response": "pending",
                    "declared_qty": 180,
                    "processed_qty": 180,
                    "boxed_qty": 180,
                    "boxes_count": 1,
                    "pallets_count": 1,
                    "head_confirmed_by": "Ларионова Марина",
                    "items": [
                        {
                            "sku_code": "нож105",
                            "name": "Нож складной туристический",
                            "size": "0",
                            "barcode": "2039530939427",
                            "actual_qty": 180,
                            "box_qty": 1,
                            "pallet_qty": 1,
                            "condition_label": "Годный товар",
                        }
                    ],
                    "services": [
                        {
                            "operation_label": "Маркировка",
                            "performer_name": "Цыганова Ирина",
                            "planned_qty": 180,
                            "actual_qty": 180,
                        }
                    ],
                },
            },
        )

        detail = build_request_detail(
            agency=self.agency,
            order_type="processing",
            order_id="42",
        )

        self.assertEqual(detail["status_label"], "Обработка подтверждена, ожидает проверки менеджера")
        self.assertEqual(detail["bucket"], "manager")
        self.assertIsNone(detail["act"])
        processing_view = detail["processing_view"]
        self.assertEqual(processing_view["status_key"], "result_review")
        self.assertFalse(processing_view["show_success"])
        self.assertEqual(processing_view["act"]["processed_qty"], 180)
        self.assertEqual(processing_view["act"]["items"][0]["sku_code"], "нож105")
        self.assertEqual(processing_view["act"]["services"][0]["performer_name"], "Цыганова Ирина")
