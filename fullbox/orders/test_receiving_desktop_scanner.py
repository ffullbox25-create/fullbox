from pathlib import Path

from django.test import SimpleTestCase


class ReceivingDesktopScannerTemplateTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.template_source = (
            Path(__file__).resolve().parent
            / "templates"
            / "orders"
            / "receiving_flow.html"
        ).read_text(encoding="utf-8")

    def test_desktop_shell_is_detected_without_waiting_for_bridge(self):
        self.assertIn("FullboxDesktop\\/[\\w.-]+", self.template_source)
        self.assertIn(
            "let supportsDesktopMode = isFullboxDesktopShell || hasDesktopScannerBridge();",
            self.template_source,
        )

    def test_desktop_scan_listener_is_bound_before_bridge_ready(self):
        self.assertIn(
            "window.addEventListener('fullbox:scan', handleDesktopScanEvent);",
            self.template_source,
        )
        self.assertIn(
            "window.addEventListener('fullbox:desktop-ready'",
            self.template_source,
        )

    def test_desktop_scan_recovers_from_stale_agent_mode(self):
        self.assertIn(
            "if (!isDesktopMode()) {\n          activateDesktopScanner();\n        }",
            self.template_source,
        )
        self.assertLess(
            self.template_source.index("activateDesktopScanner();"),
            self.template_source.index("detail.fullboxHandled = true;"),
        )

    def test_legacy_agent_controls_are_hidden_in_desktop_mode(self):
        self.assertIn(
            "scannerModeField?.classList.add('is-hidden');",
            self.template_source,
        )
        self.assertIn(
            "agentPanel.classList.toggle('is-hidden', scannerModeValue !== 'agent');",
            self.template_source,
        )
        self.assertIn(
            "agentActions.classList.toggle('is-hidden', scannerModeValue !== 'agent');",
            self.template_source,
        )
