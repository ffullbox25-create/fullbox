"""Historical pallet composition metadata regression tests."""

from django.test import SimpleTestCase

from billing.storage_billing import snapshot_box_counts


class StoragePalletBoxCountsTests(SimpleTestCase):
    def test_counts_unique_boxes_by_pallet_and_all_storage_boxes(self):
        counts, total = snapshot_box_counts(
            [
                {"pallet_code": "PAL-1", "box_code": "BOX-1"},
                {"pallet_code": "PAL-1", "box_code": "BOX-1"},
                {"pallet_code": "PAL-1", "box_code": "BOX-2"},
                {"pallet_code": "PAL-2", "box_code": "BOX-3"},
                {"pallet_code": "PAL-3", "box_code": ""},
                {"pallet_code": "", "box_code": "LOOSE-BOX"},
            ]
        )

        self.assertEqual(counts, {"PAL-1": 2, "PAL-2": 1, "PAL-3": 0})
        self.assertEqual(total, 4)
