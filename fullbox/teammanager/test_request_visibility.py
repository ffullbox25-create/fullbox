from datetime import timedelta

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from audit.models import OrderAuditEntry
from shipping.models import ShippingOrder
from sku.models import Agency
from teammanager.views import (
    _audit_meta_by_order,
    _latest_request_entries,
    _shipping_orders_by_number,
)


class TeamManagerRequestVisibilityTests(TestCase):
    def setUp(self):
        self.agency = Agency.objects.create(agn_name="Клиент менеджера A")
        self.other_agency = Agency.objects.create(agn_name="Клиент менеджера B")

    def _entry(
        self,
        *,
        order_id,
        agency=None,
        order_type="receiving",
        created_at=None,
        status="submitted",
        marker="",
    ):
        return OrderAuditEntry(
            agency=agency or self.agency,
            order_type=order_type,
            order_id=str(order_id),
            action="status",
            payload={
                "status": status,
                "status_label": status,
                "marker": marker,
                "marketplace": marker,
            },
            created_at=created_at or timezone.now(),
        )

    def test_limit_is_applied_after_event_consolidation(self):
        now = timezone.now()
        rows = [
            self._entry(
                order_id="NOISY-REQUEST",
                created_at=now - timedelta(seconds=index),
            )
            for index in range(1100)
        ]
        rows.extend(
            [
                self._entry(
                    order_id="OLDER-CURRENT-1",
                    created_at=now - timedelta(days=2),
                ),
                self._entry(
                    order_id="OLDER-CURRENT-2",
                    created_at=now - timedelta(days=3),
                ),
            ]
        )
        OrderAuditEntry.objects.bulk_create(rows, batch_size=200)

        entries = _latest_request_entries(limit=3, order_type="receiving")

        self.assertEqual(
            {entry.order_id for entry in entries},
            {"NOISY-REQUEST", "OLDER-CURRENT-1", "OLDER-CURRENT-2"},
        )

    def test_same_request_number_for_different_clients_stays_separate(self):
        OrderAuditEntry.objects.bulk_create(
            [
                self._entry(
                    agency=self.agency,
                    order_id="SAME-REQUEST",
                    marker="client-a",
                ),
                self._entry(
                    agency=self.other_agency,
                    order_id="SAME-REQUEST",
                    marker="client-b",
                ),
            ]
        )

        entries = _latest_request_entries(limit=10, order_type="receiving")
        meta = _audit_meta_by_order(entries)

        self.assertEqual(len(entries), 2)
        self.assertEqual(
            meta[(self.agency.id, "receiving", "SAME-REQUEST")].get("marketplace"),
            "client-a",
        )
        self.assertEqual(
            meta[(self.other_agency.id, "receiving", "SAME-REQUEST")].get("marketplace"),
            "client-b",
        )

    def test_shipping_draft_is_excluded_by_model_state_before_limit(self):
        draft = ShippingOrder.objects.create(
            agency=self.agency,
            number="OTG-DRAFT-MODEL",
            status=ShippingOrder.STATUS_DRAFT,
        )
        active = ShippingOrder.objects.create(
            agency=self.agency,
            number="OTG-ACTIVE-MODEL",
            status=ShippingOrder.STATUS_SUBMITTED,
        )
        second_active = ShippingOrder.objects.create(
            agency=self.agency,
            number="OTG-ACTIVE-MODEL-2",
            status=ShippingOrder.STATUS_SUBMITTED,
        )
        now = timezone.now()
        OrderAuditEntry.objects.bulk_create(
            [
                self._entry(
                    order_id=draft.number,
                    order_type="shipping",
                    status="submitted",
                    created_at=now,
                ),
                self._entry(
                    order_id=active.number,
                    order_type="shipping",
                    created_at=now - timedelta(minutes=1),
                ),
                self._entry(
                    order_id=second_active.number,
                    order_type="shipping",
                    created_at=now - timedelta(minutes=2),
                ),
            ]
        )

        entries = _latest_request_entries(limit=2, order_type="shipping")

        self.assertEqual(
            [entry.order_id for entry in entries],
            [active.number, second_active.number],
        )

    def test_related_status_and_draft_data_are_loaded_in_fixed_query_count(self):
        now = timezone.now()
        rows = []
        for index in range(40):
            number = f"OTG-BATCH-{index:03d}"
            ShippingOrder.objects.create(
                agency=self.agency,
                number=number,
                status=ShippingOrder.STATUS_SUBMITTED,
            )
            rows.append(
                self._entry(
                    order_id=number,
                    order_type="shipping",
                    created_at=now - timedelta(seconds=index),
                )
            )
        OrderAuditEntry.objects.bulk_create(rows, batch_size=200)

        with CaptureQueriesContext(connection) as queries:
            entries = _latest_request_entries(limit=40, order_type="shipping")

        self.assertEqual(len(entries), 40)
        self.assertLessEqual(len(queries), 4)

    def test_shipping_lookup_is_scoped_by_client(self):
        order = ShippingOrder.objects.create(
            agency=self.agency,
            number="OTG-SCOPED",
            status=ShippingOrder.STATUS_SUBMITTED,
        )
        entries = [
            self._entry(
                agency=self.agency,
                order_id=order.number,
                order_type="shipping",
            ),
            self._entry(
                agency=self.other_agency,
                order_id=order.number,
                order_type="shipping",
            ),
        ]

        lookup = _shipping_orders_by_number(entries)

        self.assertEqual(lookup[(self.agency.id, order.number)].pk, order.pk)
        self.assertNotIn((self.other_agency.id, order.number), lookup)
