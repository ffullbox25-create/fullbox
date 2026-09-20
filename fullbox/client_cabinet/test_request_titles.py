from types import SimpleNamespace
from unittest import TestCase


from .request_titles import build_request_display_title


class ClientRequestDisplayTitleTests(TestCase):
    def test_receiving_title_uses_supply_summary(self):
        title = build_request_display_title(
            order_type="receiving",
            order_id="PR-000123",
            payload={"place_type": "box", "expected_boxes": 1},
            base_title="Заявка на приёмку",
            receiving_view={
                "sku_count": 33,
                "planned_units": 50,
                "lines": [{"sku_code": "LETNIE_LEN", "name": "летние лен"}],
            },
        )

        self.assertIn("Приёмка: Короба", title)
        self.assertIn("33 SKU", title)
        self.assertIn("1 короб", title)
        self.assertIn("50 ед.", title)
        self.assertIn("летние лен", title)

    def test_shipping_title_uses_marketplace_destination_and_amounts(self):
        marketplace = SimpleNamespace(name="Ozon")
        order = SimpleNamespace(
            delivery_type="marketplace",
            marketplace=marketplace,
            expected_boxes=27,
            title_units=180,
            destination_warehouse="ПВЗ Старая Купавна",
            transit_address="",
            destination_address="",
        )
        title = build_request_display_title(
            order_type="shipping",
            order_id="OTG-000269",
            base_title="Заявка на отгрузку",
            shipping_order=order,
        )

        self.assertEqual(title, "Отгрузка: Ozon → ПВЗ Старая Купавна · 27 коробов, 180 шт.")

    def test_transfer_title_names_transfer_without_marketplace_noise(self):
        order = SimpleNamespace(
            delivery_type="transfer",
            marketplace=None,
            expected_boxes=2,
            title_units=7,
            destination_address="ФБС",
            destination_warehouse="",
            transit_address="",
        )
        title = build_request_display_title(
            order_type="shipping",
            order_id="OTG-000240",
            base_title="Заявка на отгрузку",
            shipping_order=order,
        )

        self.assertEqual(title, "Перемещение → ФБС · 2 короба, 7 шт.")

    def test_processing_title_shows_article_change(self):
        title = build_request_display_title(
            order_type="processing",
            order_id="40",
            payload={
                "article": "MICELLAR_WATER_100",
                "article_change_target_article": "MICELLAR_WATER_300",
                "stock_rows": [{"article": "MICELLAR_WATER_100", "qty": 360, "name": "Мицеллярная вода"}],
            },
            base_title="Заявка на обработку",
        )

        self.assertIn("MICELLAR_WATER_100 → MICELLAR_WATER_300", title)
        self.assertIn("360 шт.", title)
