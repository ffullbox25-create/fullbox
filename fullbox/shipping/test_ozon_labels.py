from __future__ import annotations

import base64

import fitz
from django.test import SimpleTestCase

from .ozon_labels import build_ozon_gm_label_assets


class OzonGmLabelAssetsTests(SimpleTestCase):
    def _source_pdf(self, pages: int = 2) -> bytes:
        document = fitz.open()
        try:
            for index in range(pages):
                page = document.new_page(width=120 * 72 / 25.4, height=75 * 72 / 25.4)
                page.insert_text((24, 36), f"OZON GM {index + 1}", fontsize=18)
            return document.tobytes()
        finally:
            document.close()

    def test_builds_one_portrait_75x120_label_per_ozon_page(self):
        result = build_ozon_gm_label_assets(
            [{"supply_id": 101, "content": self._source_pdf()}],
            [
                {"supply_id": 101, "gm_barcode": "GM-001"},
                {"supply_id": 101, "gm_barcode": "GM-002"},
            ],
        )

        self.assertEqual(result["label_count"], 2)
        self.assertEqual([row["gm_barcode"] for row in result["labels"]], ["GM-001", "GM-002"])
        preview = fitz.Pixmap(base64.b64decode(result["labels"][0]["label_png_base64"]))
        self.assertEqual((preview.width, preview.height), (900, 1440))
        self.assertEqual(preview.pixel(0, 0), (255,))
        with fitz.open(stream=base64.b64decode(result["combined_pdf_base64"]), filetype="pdf") as pdf:
            self.assertEqual(pdf.page_count, 2)
            self.assertAlmostEqual(pdf[0].rect.width * 25.4 / 72, 75, places=2)
            self.assertAlmostEqual(pdf[0].rect.height * 25.4 / 72, 120, places=2)

    def test_rejects_pdf_page_count_that_does_not_match_gm_barcodes(self):
        with self.assertRaisesMessage(ValueError, "не совпадает"):
            build_ozon_gm_label_assets(
                [{"supply_id": 101, "content": self._source_pdf(pages=1)}],
                [
                    {"supply_id": 101, "gm_barcode": "GM-001"},
                    {"supply_id": 101, "gm_barcode": "GM-002"},
                ],
            )
