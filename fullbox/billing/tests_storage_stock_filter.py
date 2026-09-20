from django.test import SimpleTestCase

from .storage import _is_storage_billable_row


class StorageStockRowBillingFilterTests(SimpleTestCase):
    def test_stored_rows_in_storage_zones_are_billable(self):
        self.assertTrue(_is_storage_billable_row({"zone": "PR", "qty": 10, "warehouse_state_code": "received_unplaced"}))
        self.assertTrue(_is_storage_billable_row({"zone": "OS", "qty": 10, "warehouse_state_code": "stored"}))
        self.assertTrue(_is_storage_billable_row({"zone": "OBR", "qty": 10, "warehouse_state_code": "in_processing_zone"}))
        self.assertTrue(_is_storage_billable_row({"zone": "OTG", "qty": 10, "warehouse_state_code": "ready_for_loading"}))

    def test_loaded_to_vehicle_is_not_storage_billing(self):
        self.assertFalse(
            _is_storage_billable_row({"zone": "OTG", "qty": 10, "warehouse_state_code": "loaded_to_vehicle"})
        )
        self.assertFalse(_is_storage_billable_row({"zone": "OTG", "qty": 10, "is_in_vehicle": True}))

    def test_terminal_or_consumed_rows_are_not_storage_billing(self):
        for state in ("shipped", "partially_shipped", "canceled", "processing_consumed"):
            with self.subTest(state=state):
                self.assertFalse(_is_storage_billable_row({"zone": "OS", "qty": 10, "warehouse_state_code": state}))

    def test_stock_bucket_fallback_still_counts_storage(self):
        self.assertTrue(_is_storage_billable_row({"stock_main_qty": 1, "qty": 1}))
        self.assertTrue(_is_storage_billable_row({"stock_processing_qty": 1, "qty": 1}))
        self.assertTrue(_is_storage_billable_row({"stock_otg_qty": 1, "qty": 1}))
