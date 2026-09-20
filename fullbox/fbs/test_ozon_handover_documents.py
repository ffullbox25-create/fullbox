from __future__ import annotations

from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from sku.models import Agency

from fbs.exceptions import FbsIntegrationError
from fbs.integrations.http import MarketplaceHttpResponse
from fbs.integrations.ozon import (
    OZON_HANDOVER_DOCUMENT_BARCODE,
    OZON_HANDOVER_DOCUMENT_PDF,
    build_ozon_carriage_list_spec,
    build_ozon_carriage_postings_spec,
    build_ozon_handover_document_spec,
    parse_ozon_carriage_list,
    parse_ozon_carriage_postings,
    parse_ozon_handover_document_response,
)
from fbs.models import (
    FbsHandoverBatch,
    FbsHandoverBox,
    FbsHandoverOrder,
    FbsHandoverOrderAssignment,
    FbsIntegrationProfile,
    FbsOrder,
    FbsOrderItem,
)
from fbs.services.ozon_handover_documents import (
    download_exact_ozon_handover_document,
    find_exact_ozon_carriage,
)
from fbs.services.printing import (
    ozon_handover_label_summary,
    render_ozon_handover_internal_label,
)


def _response(*, payload=None, content=b"", content_type="application/json"):
    return MarketplaceHttpResponse(
        status_code=200,
        headers={"Content-Type": content_type},
        content=content,
        json_payload=payload,
    )


class OzonHandoverContractTests(SimpleTestCase):
    def test_carriage_specs_are_read_only_and_use_expected_endpoints(self):
        list_spec = build_ozon_carriage_list_spec(departure_date="2026-09-08")
        postings_spec = build_ozon_carriage_postings_spec(701)
        barcode_spec = build_ozon_handover_document_spec(
            carriage_id=701,
            document_kind=OZON_HANDOVER_DOCUMENT_BARCODE,
        )
        pdf_spec = build_ozon_handover_document_spec(
            carriage_id=701,
            document_kind=OZON_HANDOVER_DOCUMENT_PDF,
        )

        self.assertEqual(list_spec.endpoint, "/v2/carriage/delivery/list")
        self.assertEqual(
            list_spec.body["filter"]["departure_date"],
            "2026-09-08",
        )
        self.assertEqual(
            postings_spec.endpoint,
            "/v2/posting/fbs/act/get-postings",
        )
        self.assertEqual(postings_spec.body, {"id": 701})
        self.assertEqual(
            barcode_spec.endpoint,
            "/v2/posting/fbs/act/get-barcode",
        )
        self.assertEqual(pdf_spec.endpoint, "/v2/posting/fbs/act/get-pdf")

    def test_carriage_parsers_accept_live_ozon_shapes(self):
        methods = parse_ozon_carriage_list(
            {
                "cursor": "",
                "has_next": False,
                "methods": [
                    {
                        "delivery_method_id": 42,
                        "departure_date": "2026-09-08",
                        "carriages": [{"id": 701}],
                    }
                ],
            }
        )
        postings = parse_ozon_carriage_postings(
            {"result": [{"posting_number": "100-1-1"}, "100-2-1"]}
        )

        self.assertEqual(methods[0]["delivery_method_id"], "42")
        self.assertEqual(methods[0]["carriages"], ({"id": 701},))
        self.assertEqual(postings, ("100-1-1", "100-2-1"))

    def test_document_parser_checks_real_file_signature(self):
        png = parse_ozon_handover_document_response(
            _response(
                content=b"\x89PNG\r\n\x1a\nfixture",
                content_type="image/png",
            ),
            document_kind=OZON_HANDOVER_DOCUMENT_BARCODE,
        )
        pdf = parse_ozon_handover_document_response(
            _response(content=b"%PDF-1.7 fixture", content_type="application/pdf"),
            document_kind=OZON_HANDOVER_DOCUMENT_PDF,
        )

        self.assertEqual(png[1], "image/png")
        self.assertEqual(pdf[1], "application/pdf")
        with self.assertRaisesRegex(FbsIntegrationError, "не в формате PNG"):
            parse_ozon_handover_document_response(
                _response(content=b"not-an-image", content_type="image/png"),
                document_kind=OZON_HANDOVER_DOCUMENT_BARCODE,
            )


class StubOzonHandoverTransport:
    def __init__(self, *, exact_carriage=True, document_kind="barcode"):
        self.exact_carriage = exact_carriage
        self.document_kind = document_kind
        self.specs = []

    def send(self, profile, spec):
        self.specs.append(spec)
        if spec.endpoint == "/v2/carriage/delivery/list":
            return _response(
                payload={
                    "methods": [
                        {
                            "delivery_method_id": 42,
                            "carriages": [{"id": 701}, {"id": 702}],
                        },
                        {
                            "delivery_method_id": 999,
                            "carriages": [{"id": 999}],
                        },
                    ]
                }
            )
        if spec.endpoint == "/v2/posting/fbs/act/get-postings":
            carriage_id = spec.body["id"]
            if carriage_id == 701:
                values = ["100-1-1", "100-2-1", "foreign-posting"]
            elif self.exact_carriage:
                values = ["100-2-1", "100-1-1"]
            else:
                values = ["100-1-1"]
            return _response(
                payload={"result": [{"posting_number": value} for value in values]}
            )
        if spec.endpoint == "/v2/posting/fbs/act/get-barcode":
            return _response(
                content=b"\x89PNG\r\n\x1a\nfixture",
                content_type="image/png",
            )
        if spec.endpoint == "/v2/posting/fbs/act/get-pdf":
            return _response(
                content=b"%PDF-1.7 fixture",
                content_type="application/pdf",
            )
        raise AssertionError(f"Unexpected endpoint: {spec.endpoint}")


class OzonHandoverDocumentTests(TestCase):
    def setUp(self):
        self.agency = Agency.objects.create(
            agn_name='ОБЩЕСТВО С ОГРАНИЧЕННОЙ ОТВЕТСТВЕННОСТЬЮ "КЕЙЗИ"'
        )
        self.profile = FbsIntegrationProfile.objects.create(
            agency=self.agency,
            marketplace=FbsIntegrationProfile.MARKETPLACE_OZON,
            name="Ozon documents",
            external_account_id="test-account",
            external_warehouse_id="42",
        )
        self.batch = FbsHandoverBatch.objects.create(
            profile=self.profile,
            status=FbsHandoverBatch.STATUS_DISPATCHED,
            marketplace_state=FbsHandoverBatch.MARKETPLACE_COMPLETE,
        )
        self.first_order = self._order("100-1-1", quantity=2)
        self.second_order = self._order("100-2-1", quantity=3)
        self._place_order(self.first_order, box_code="FBS-OZON-DOC-1")
        self._place_order(self.second_order, box_code="FBS-OZON-DOC-2")

        canceled = self._order("100-canceled-1", quantity=7)
        FbsHandoverOrderAssignment.objects.create(
            batch=self.batch,
            order=canceled,
            status=FbsHandoverOrderAssignment.STATUS_CANCELED,
        )

    def _order(self, external_order_id, *, quantity):
        order = FbsOrder.objects.create(
            profile=self.profile,
            external_order_id=external_order_id,
            internal_status=FbsOrder.STATUS_HANDED_OVER,
            raw_payload={
                "shipment_date": "2026-09-08T12:00:00Z",
                "delivery_method": {"id": 42},
            },
        )
        FbsOrderItem.objects.create(
            order=order,
            external_line_id=f"line-{external_order_id}",
            external_sku=f"sku-{external_order_id}",
            barcode="4600000000001",
            quantity=quantity,
        )
        return order

    def _place_order(self, order, *, box_code):
        FbsHandoverOrderAssignment.objects.create(
            batch=self.batch,
            order=order,
            status=FbsHandoverOrderAssignment.STATUS_CONFIRMED,
            confirmed_at=timezone.now(),
        )
        box = FbsHandoverBox.objects.create(
            batch=self.batch,
            qr_code=box_code,
            status=FbsHandoverBox.STATUS_DISPATCHED,
        )
        FbsHandoverOrder.objects.create(box=box, order=order)

    def test_internal_label_uses_exact_active_box_composition(self):
        summary = ozon_handover_label_summary(self.batch)
        image = render_ozon_handover_internal_label(self.batch)

        self.assertEqual(summary["order_count"], 2)
        self.assertEqual(summary["unit_count"], 5)
        self.assertEqual(summary["box_count"], 2)
        self.assertEqual(summary["agency_name"], 'ООО "КЕЙЗИ"')
        self.assertTrue(image.startswith(b"\x89PNG\r\n\x1a\n"))

    def test_only_exact_existing_carriage_is_selected(self):
        transport = StubOzonHandoverTransport()

        carriage_id = find_exact_ozon_carriage(self.batch, transport=transport)

        self.assertEqual(carriage_id, 702)
        self.assertEqual(
            [spec.endpoint for spec in transport.specs],
            [
                "/v2/carriage/delivery/list",
                "/v2/posting/fbs/act/get-postings",
                "/v2/posting/fbs/act/get-postings",
            ],
        )

    def test_overlap_without_exact_match_is_rejected(self):
        transport = StubOzonHandoverTransport(exact_carriage=False)

        with self.assertRaisesRegex(FbsIntegrationError, "точным составом"):
            find_exact_ozon_carriage(self.batch, transport=transport)

    def test_exact_official_document_is_downloaded(self):
        transport = StubOzonHandoverTransport()

        document = download_exact_ozon_handover_document(
            batch_id=self.batch.id,
            document_kind=OZON_HANDOVER_DOCUMENT_BARCODE,
            transport=transport,
        )

        self.assertEqual(document.carriage_id, 702)
        self.assertEqual(document.content_type, "image/png")
        self.assertEqual(
            document.filename,
            f"ozon-fbs-{self.batch.id}-702.png",
        )
        self.assertTrue(document.content.startswith(b"\x89PNG\r\n\x1a\n"))
