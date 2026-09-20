from django.test import TestCase

from .audit_history import (
    build_sku_audit_snapshot,
    sku_audit_change_rows,
    sku_audit_description,
    sku_audit_snapshot,
)
from .models import Agency, SKU


class SKUFieldAuditTests(TestCase):
    def test_snapshot_records_exact_parameter_and_barcode_changes(self):
        agency = Agency.objects.create(agn_name="Клиент аудита")
        sku = SKU.objects.create(
            agency=agency,
            sku_code="AUDIT-1",
            name="Товар",
            length_mm="100.0",
            weight_kg="0.300",
        )
        sku.barcodes.create(value="200000000001", is_primary=True)
        before = sku_audit_snapshot(sku)

        sku.length_mm = "440.0"
        sku.weight_kg = "1.250"
        sku.save()
        sku.barcodes.create(value="200000000002", size="M")
        snapshot = build_sku_audit_snapshot(
            sku,
            before=before,
            source="marketplace",
            marketplace="WB",
        )
        changes = {row["field"]: row for row in sku_audit_change_rows(snapshot)}

        self.assertEqual(changes["length_mm"]["before_display"], "100.0 мм")
        self.assertEqual(changes["length_mm"]["after_display"], "440.0 мм")
        self.assertEqual(changes["weight_kg"]["before_display"], "0.300 кг")
        self.assertEqual(changes["weight_kg"]["after_display"], "1.250 кг")
        self.assertIn("200000000002 (M)", changes["barcodes"]["after_display"])
        self.assertIn("Синхронизация WB", sku_audit_description("update", snapshot))
