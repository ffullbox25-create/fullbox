from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase

from client_cabinet.chat_bridge import on_order_audit


class StockMoveChatBridgeTests(SimpleTestCase):
    @patch("client_cabinet.chat_bridge._ensure_and_announce")
    @patch(
        "client_cabinet.chat_bridge._safe_agency",
        return_value=SimpleNamespace(pk=1),
    )
    def test_stock_move_create_does_not_open_order_chat(
        self,
        _safe_agency_mock,
        ensure_and_announce_mock,
    ):
        on_order_audit(
            action="create",
            order_id="MOVE-000001",
            order_type="stock_move",
            agency=object(),
        )

        ensure_and_announce_mock.assert_not_called()

    @patch("client_cabinet.chat_bridge._ensure_and_announce")
    @patch(
        "client_cabinet.chat_bridge._safe_agency",
        return_value=SimpleNamespace(pk=1),
    )
    def test_shipping_create_still_opens_order_chat(
        self,
        _safe_agency_mock,
        ensure_and_announce_mock,
    ):
        on_order_audit(
            action="create",
            order_id="OTG-000001",
            order_type="shipping",
            agency=object(),
        )

        ensure_and_announce_mock.assert_called_once()
