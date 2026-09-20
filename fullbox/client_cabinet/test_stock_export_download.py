from pathlib import Path

from django.test import SimpleTestCase


class StockJournalExportTemplateTests(SimpleTestCase):
    def test_export_uses_native_post_without_sending_all_rows(self):
        template = (
            Path(__file__).resolve().parent
            / "templates"
            / "client_cabinet"
            / "dashboard_lk_react.html"
        ).read_text(encoding="utf-8")

        start = template.index("    function exportRows(){")
        end = template.index("    function resetFilters(){", start)
        export_source = template[start:end]

        self.assertIn('form.method = "POST";', export_source)
        self.assertIn(
            'form.action = "/client/api/v1/stock-journal/export/" + clientQuery();',
            export_source,
        )
        self.assertIn('csrfInput.name = "csrfmiddlewaretoken";', export_source)
        self.assertIn("form.submit();", export_source)
        self.assertNotIn("JSON.stringify", export_source)
        self.assertNotIn("response.blob()", export_source)
