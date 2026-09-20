from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase


class ClientRequestChannelFilterTemplateTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        template = (
            Path(settings.BASE_DIR)
            / "client_cabinet"
            / "templates"
            / "client_cabinet"
            / "dashboard_lk_react.html"
        )
        cls.source = template.read_text(encoding="utf-8")

    def test_request_list_has_requested_shipping_channel_filters(self):
        self.assertIn('class="requests-filter-row"', self.source)
        self.assertIn('id="shipping-kind-filter"', self.source)
        self.assertIn('Канал отгрузки', self.source)
        self.assertIn('{ id: "wb", label: "WB" }', self.source)
        self.assertIn('{ id: "ozon", label: "Ozon" }', self.source)
        self.assertIn('{ id: "courier", label: "Курьер" }', self.source)
        self.assertIn('{ id: "transfer", label: "Перемещение" }', self.source)

    def test_shipping_channel_filter_is_compact_select_next_to_request_types(self):
        self.assertIn('class="filter-block filter-block--type"', self.source)
        self.assertIn('class="filter-block filter-block--shipping"', self.source)
        self.assertIn('class="requests-filter-select"', self.source)
        self.assertIn('function renderShippingKindSelect(counts)', self.source)
        self.assertIn('shippingKindSelect.addEventListener("change"', self.source)

    def test_filter_uses_existing_shipping_kind_without_mutating_requests(self):
        self.assertIn('function shippingKind(row)', self.source)
        self.assertIn('row.shipping_kind_label', self.source)
        self.assertIn('function byShippingKind(rows, kindId)', self.source)
        self.assertIn('if(shippingKindFilter !== "all") typeFilter = "shipping";', self.source)
        self.assertNotIn('/client/api/v1/requests/filter/', self.source)
