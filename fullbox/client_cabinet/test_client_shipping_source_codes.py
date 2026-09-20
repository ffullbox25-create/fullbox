from django.test import SimpleTestCase

from client_cabinet.client_shipping_view import _client_item_comment


class ClientShippingSourceCodeVisibilityTests(SimpleTestCase):
    def test_hides_source_codes_but_keeps_quantity_layout(self):
        comment = "Коробов: 2; кратность: 180; короба: BOX-001, BOX-002"

        self.assertEqual(
            _client_item_comment(comment),
            "Коробов: 2; кратность: 180",
        )

    def test_hides_partial_source_codes_and_opaque_split_metadata(self):
        comment = (
            "Исходные короба: BOX-003; Разбить короб: 1; "
            "отбор из короба: 100 из 180; split_box:b64:ZXhhbXBsZQ=="
        )

        self.assertEqual(
            _client_item_comment(comment),
            "Разбить короб: 1; отбор из короба: 100 из 180",
        )

    def test_keeps_ordinary_client_safe_comment_text(self):
        comment = "Поштучный отбор; нужны короба с маркировкой клиента"

        self.assertEqual(_client_item_comment(comment), comment)

    def test_source_only_comment_becomes_empty(self):
        self.assertEqual(_client_item_comment("короба: BOX-001"), "")
