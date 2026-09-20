from types import SimpleNamespace

from django.test import SimpleTestCase

from fbs.services.traceability import (
    OZON_MARKING_NOT_REQUIRED,
    OZON_MARKING_REQUIRED,
    OZON_MARKING_UNKNOWN,
    controller_marking_scan_required,
    marketplace_marking_transfer_allowed,
    metadata_requirements,
    ozon_marking_requirement_decision,
)


def _item(
    *,
    marketplace="ozon",
    requirements=None,
    raw_payload=None,
    order_raw_payload=None,
    honest_sign=False,
):
    profile = SimpleNamespace(marketplace=marketplace)
    order = SimpleNamespace(
        profile=profile,
        raw_payload=order_raw_payload if order_raw_payload is not None else {},
    )
    return SimpleNamespace(
        order=order,
        sku=SimpleNamespace(honest_sign=honest_sign),
        external_line_id="123",
        external_sku="offer-123",
        barcode="4600000000123",
        requirements=requirements if requirements is not None else {},
        raw_payload=raw_payload if raw_payload is not None else {},
    )


def _allocation(item, *, marking_code=""):
    return SimpleNamespace(
        order_item=item,
        balance=SimpleNamespace(marking_code=marking_code),
    )


class OzonMarkingRequirementDecisionTests(SimpleTestCase):
    def test_normalized_import_defaults_without_raw_evidence_are_unknown(self):
        item = _item(requirements={"is_kiz": False, "mandatory_mark": []})

        self.assertEqual(
            ozon_marking_requirement_decision(item),
            OZON_MARKING_UNKNOWN,
        )

    def test_order_requirement_list_identifies_required_product(self):
        item = _item(
            raw_payload={"sku": 123, "offer_id": "offer-123"},
            order_raw_payload={
                "requirements": {"products_requiring_mandatory_mark": [123]}
            },
        )

        self.assertEqual(
            ozon_marking_requirement_decision(item),
            OZON_MARKING_REQUIRED,
        )

    def test_empty_order_requirement_list_is_explicit_not_required(self):
        item = _item(
            raw_payload={"sku": 123},
            order_raw_payload={
                "requirements": {"products_requiring_mandatory_mark": []}
            },
        )

        self.assertEqual(
            ozon_marking_requirement_decision(item),
            OZON_MARKING_NOT_REQUIRED,
        )

    def test_ozon_explicit_boolean_pair_is_not_required(self):
        item = _item(
            raw_payload={
                "is_mandatory_mark_needed": False,
                "is_mandatory_mark_possible": False,
            }
        )

        self.assertEqual(
            ozon_marking_requirement_decision(item),
            OZON_MARKING_NOT_REQUIRED,
        )

    def test_ozon_positive_product_signal_is_required(self):
        item = _item(raw_payload={"is_mandatory_mark_needed": True})

        self.assertEqual(
            ozon_marking_requirement_decision(item),
            OZON_MARKING_REQUIRED,
        )

    def test_possible_false_without_needed_false_is_unknown(self):
        item = _item(raw_payload={"is_mandatory_mark_possible": False})

        self.assertEqual(
            ozon_marking_requirement_decision(item),
            OZON_MARKING_UNKNOWN,
        )


class OzonModeBControllerTests(SimpleTestCase):
    def test_unknown_decision_uses_conservative_sku_fallback(self):
        item = _item(
            requirements={"is_kiz": False, "mandatory_mark": []},
            honest_sign=True,
        )

        self.assertTrue(metadata_requirements(item).marking_required)
        self.assertTrue(controller_marking_scan_required(_allocation(item)))

    def test_explicit_no_overrides_sku_but_scans_physical_balance(self):
        item = _item(
            order_raw_payload={
                "requirements": {"products_requiring_mandatory_mark": []}
            },
            honest_sign=True,
        )

        self.assertFalse(metadata_requirements(item).marking_required)
        self.assertTrue(
            controller_marking_scan_required(
                _allocation(item, marking_code="010460000000123421ABC")
            )
        )
        self.assertFalse(marketplace_marking_transfer_allowed(item))

    def test_explicit_no_without_physical_mark_does_not_scan(self):
        item = _item(
            order_raw_payload={
                "requirements": {"products_requiring_mandatory_mark": []}
            },
            honest_sign=True,
        )

        self.assertFalse(controller_marking_scan_required(_allocation(item)))

    def test_explicit_yes_requires_scan_and_allows_transfer(self):
        item = _item(raw_payload={"is_kiz": True})

        self.assertTrue(metadata_requirements(item).marking_required)
        self.assertTrue(controller_marking_scan_required(_allocation(item)))
        self.assertTrue(marketplace_marking_transfer_allowed(item))


class WildberriesRegressionTests(SimpleTestCase):
    def test_wb_honest_sign_requirement_is_unchanged(self):
        item = _item(
            marketplace="wb",
            requirements={"wb_meta": {"sgtin": {"decision": "optional"}}},
            honest_sign=True,
        )

        self.assertTrue(metadata_requirements(item).marking_required)
        self.assertTrue(controller_marking_scan_required(_allocation(item)))
        self.assertTrue(marketplace_marking_transfer_allowed(item))

    def test_wb_unmarked_item_without_balance_mark_remains_optional(self):
        item = _item(marketplace="wb", honest_sign=False)

        self.assertFalse(metadata_requirements(item).marking_required)
        self.assertFalse(controller_marking_scan_required(_allocation(item)))
        self.assertTrue(marketplace_marking_transfer_allowed(item))
