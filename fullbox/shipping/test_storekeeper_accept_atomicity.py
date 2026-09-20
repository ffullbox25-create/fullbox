from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase

from shipping.actions import (
    ShippingDetailActionPermissions,
    handle_shipping_detail_action,
)
from shipping.models import ShippingOrder
from sku.models import Agency


class StorekeeperAcceptAtomicityTests(TestCase):
    def setUp(self):
        self.agency = Agency.objects.create(agn_name="Atomic shipping client")
        self.order = ShippingOrder.objects.create(
            number="OTG-ATOMIC-001",
            agency=self.agency,
            status=ShippingOrder.STATUS_RESERVED,
        )
        self.permissions = ShippingDetailActionPermissions(
            can_edit_items=False,
            can_submit_for_approval=False,
            can_manager_approve=False,
            can_manager_reopen=False,
            can_storekeeper_accept=True,
            can_storekeeper_pick=False,
        )
        self.user = SimpleNamespace(
            username="storekeeper",
            get_full_name=lambda: "Store Keeper",
        )

    @patch(
        "shipping.actions.start_storekeeper_pick",
        side_effect=RuntimeError("planner failed"),
    )
    @patch("shipping.actions.accept_order_by_storekeeper")
    def test_planner_failure_rolls_back_storekeeper_accept(
        self,
        accept_mock,
        _start_pick_mock,
    ):
        def accept_order(order, _user):
            order.status = ShippingOrder.STATUS_STOREKEEPER_ACCEPTED
            order.save(update_fields=["status", "updated_at"])

        accept_mock.side_effect = accept_order

        result = handle_shipping_detail_action(
            action="accept_storekeeper",
            order=self.order,
            request=SimpleNamespace(user=self.user),
            role="storekeeper",
            stock_rows_for_order=[],
            permissions=self.permissions,
            parse_selected_stock_items=lambda *_args, **_kwargs: [],
            selected_stock_rows_with_boxes=lambda *_args, **_kwargs: [],
            parse_box_count_from_comment=lambda _value: 0,
            log_update=lambda *_args, **_kwargs: None,
        )

        self.order.refresh_from_db()
        self.assertEqual(self.order.status, ShippingOrder.STATUS_RESERVED)
        self.assertTrue(
            any(
                level == "error" and "Заявка не принята" in text
                for level, text in result.messages
            )
        )
