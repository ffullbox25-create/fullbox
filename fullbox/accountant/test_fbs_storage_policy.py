from django.test import TestCase

from billing.models import FbsClientRate
from fbs.models import FbsClientStoragePolicy
from sku.models import Agency

from .fbs_tariffs import _ensure_fbs_storage_policy


class FbsStoragePolicyTests(TestCase):
    def setUp(self):
        self.client = Agency.objects.create(agn_name="Клиент FBS")

    def _rate(self, *, unit="л·сут", is_active=True):
        return FbsClientRate.objects.create(
            client=self.client,
            operation=FbsClientRate.OP_STORAGE,
            liters_from=0,
            price="0.20",
            unit=unit,
            valid_from="2026-09-01",
            is_active=is_active,
        )

    def test_active_liter_storage_rate_creates_liter_policy(self):
        _ensure_fbs_storage_policy(self._rate())

        policy = FbsClientStoragePolicy.objects.get(agency=self.client)
        self.assertEqual(policy.billing_mode, FbsClientStoragePolicy.BILLING_LITERS)
        self.assertTrue(policy.is_active)

    def test_existing_active_policy_is_not_overwritten(self):
        policy = FbsClientStoragePolicy.objects.create(
            agency=self.client,
            billing_mode=FbsClientStoragePolicy.BILLING_PALLETS,
            is_active=True,
        )

        _ensure_fbs_storage_policy(self._rate())

        policy.refresh_from_db()
        self.assertEqual(policy.billing_mode, FbsClientStoragePolicy.BILLING_PALLETS)

    def test_inactive_storage_rate_does_not_create_policy(self):
        _ensure_fbs_storage_policy(self._rate(is_active=False))

        self.assertFalse(FbsClientStoragePolicy.objects.filter(agency=self.client).exists())
