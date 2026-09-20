from datetime import datetime, timezone
from unittest import TestCase

from .inspection_ai import (
    InspectionIncident,
    SEVERITY_CRITICAL,
    SEVERITY_WARNING,
    _incidents_from_fbs_report,
    build_ai_inspection_report,
)


class AIInspectionReportTests(TestCase):
    now = datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)

    def test_report_is_read_only_and_sorts_critical_first(self):
        def healthy(_now):
            return {"incidents": [], "metrics": {"value": 1}}

        def attention(_now):
            return {
                "incidents": [
                    InspectionIncident(
                        code="warn",
                        severity=SEVERITY_WARNING,
                        contour="Печать",
                        title="Предупреждение",
                        message="Требуется проверка.",
                        recommendation="Проверить вручную.",
                        source="test",
                    ),
                    InspectionIncident(
                        code="critical",
                        severity=SEVERITY_CRITICAL,
                        contour="Склад",
                        title="Критичный сигнал",
                        message="Нарушен инвариант.",
                        recommendation="Ничего не менять автоматически.",
                        source="test",
                    ),
                ],
                "metrics": {},
            }

        report = build_ai_inspection_report(
            now=self.now,
            source_builders=(
                ("healthy", "Здоровый источник", healthy),
                ("attention", "Источник с сигналами", attention),
            ),
        )

        self.assertTrue(report["read_only"])
        self.assertFalse(report["automatic_corrections"])
        self.assertEqual(report["overall"], SEVERITY_CRITICAL)
        self.assertEqual(report["summary"]["critical"], 1)
        self.assertEqual(report["summary"]["warning"], 1)
        self.assertEqual(report["incidents"][0]["code"], "critical")
        self.assertTrue(report["incidents"][0]["requires_human"])
        self.assertFalse(report["incidents"][0]["automatic_action"])

    def test_source_failure_is_isolated(self):
        def broken(_now):
            raise RuntimeError("sensitive database detail")

        report = build_ai_inspection_report(
            now=self.now,
            source_builders=(("broken", "Проверка", broken),),
        )

        self.assertEqual(report["summary"]["unavailable_sources"], 1)
        self.assertEqual(report["summary"]["warning"], 1)
        self.assertEqual(report["sources"][0]["status"], "unavailable")
        self.assertNotIn("sensitive database detail", report["incidents"][0]["message"])

    def test_fbs_checks_are_mapped_without_actions(self):
        incidents = _incidents_from_fbs_report(
            {
                "checks": [
                    {
                        "code": "stock",
                        "title": "Остаток",
                        "severity": "blocked",
                        "message": "Нарушена формула.",
                        "count": 2,
                    },
                    {
                        "code": "labels",
                        "title": "Этикетки",
                        "severity": "warning",
                        "message": "Есть ошибки.",
                        "count": 3,
                    },
                    {
                        "code": "scans",
                        "title": "Сканы",
                        "severity": "ok",
                        "message": "В норме.",
                        "count": 0,
                    },
                ],
                "inspection_evidence": {
                    "stock": {
                        "total": 2,
                        "items": [
                            {
                                "title": "Остаток #17",
                                "subtitle": "SKU TEST",
                                "facts": ("Факт: 1", "Доступно: 2"),
                                "message": "Нарушена формула.",
                            }
                        ],
                    }
                },
            }
        )

        self.assertEqual(len(incidents), 2)
        self.assertEqual(incidents[0].severity, SEVERITY_CRITICAL)
        self.assertFalse(incidents[0].automatic_action)
        self.assertIn("автоматически не исправлять", incidents[0].recommendation)
        self.assertIn("qty", incidents[0].check_rule)
        self.assertEqual(incidents[0].evidence_total, 2)
        self.assertEqual(incidents[0].evidence[0]["title"], "Остаток #17")
