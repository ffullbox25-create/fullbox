from types import SimpleNamespace

from django.test import SimpleTestCase

from fbs.services.traceability import metadata_requirements


class FbsWaveMarkingCaptureTests(SimpleTestCase):
    @staticmethod
    def _item(*, marketplace, requirements, honest_sign=False):
        return SimpleNamespace(
            order=SimpleNamespace(
                profile=SimpleNamespace(marketplace=marketplace),
            ),
            sku=SimpleNamespace(honest_sign=honest_sign),
            requirements=requirements,
        )

    def test_wb_optional_sgtin_alone_does_not_require_marking_scan(self):
        item = self._item(
            marketplace="wb",
            requirements={
                "required_meta": [],
                "optional_meta": ["sgtin", "customs_declaration"],
            },
        )

        self.assertFalse(metadata_requirements(item).marking_required)

    def test_wb_filled_sgtin_requires_marking_scan(self):
        item = self._item(
            marketplace="wb",
            requirements={
                "required_meta": [],
                "optional_meta": ["sgtin"],
                "wb_meta": {
                    "sgtin": {
                        "available": True,
                        "decision": "filled",
                        "has_value": True,
                        "has_errors": False,
                    }
                },
                "wb_marking_codes": ["010460000000000021ABC123"],
            },
        )

        self.assertTrue(metadata_requirements(item).marking_required)

    def test_wb_required_decision_requires_scan_even_before_value(self):
        item = self._item(
            marketplace="wb",
            requirements={
                "optional_meta": ["sgtin"],
                "wb_meta": {
                    "sgtin": {
                        "available": True,
                        "decision": "required",
                        "has_value": False,
                        "has_errors": False,
                    }
                },
            },
        )

        self.assertTrue(metadata_requirements(item).marking_required)

    def test_wb_optional_decision_with_existing_value_requires_exact_unit_scan(self):
        item = self._item(
            marketplace="wb",
            requirements={
                "optional_meta": ["sgtin"],
                "wb_meta": {
                    "sgtin": {
                        "available": True,
                        "decision": "optional",
                        "has_value": True,
                        "has_errors": False,
                    }
                },
            },
        )

        self.assertTrue(metadata_requirements(item).marking_required)

    def test_sku_honest_sign_remains_required_when_wb_decision_is_optional(self):
        item = self._item(
            marketplace="wb",
            honest_sign=True,
            requirements={
                "optional_meta": ["sgtin"],
                "wb_meta": {
                    "sgtin": {
                        "available": True,
                        "decision": "optional",
                        "has_value": False,
                        "has_errors": False,
                    }
                },
            },
        )

        self.assertTrue(metadata_requirements(item).marking_required)

    def test_non_wb_optional_sgtin_does_not_change_existing_rule(self):
        item = self._item(
            marketplace="ozon",
            requirements={"required_meta": [], "optional_meta": ["sgtin"]},
        )

        self.assertFalse(metadata_requirements(item).marking_required)

    def test_wb_unrelated_optional_metadata_does_not_require_marking(self):
        item = self._item(
            marketplace="wb",
            requirements={
                "required_meta": [],
                "optional_meta": ["customs_declaration"],
            },
        )

        self.assertFalse(metadata_requirements(item).marking_required)

    def test_existing_required_meta_rule_is_preserved(self):
        item = self._item(
            marketplace="ozon",
            requirements={"required_meta": {"marking": "uin"}},
        )

        self.assertTrue(metadata_requirements(item).marking_required)
