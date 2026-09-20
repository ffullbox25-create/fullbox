from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError

from head_manager.inspection_ai import build_ai_inspection_report


class Command(BaseCommand):
    help = (
        "Read-only AI-инспектор WMS: собирает сигналы ошибок и рекомендации, "
        "но никогда не изменяет данные и не запускает исправления."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--format",
            choices=("table", "json"),
            default="table",
            help="Формат отчёта.",
        )
        parser.add_argument(
            "--deep",
            action="store_true",
            help="Добавить более тяжёлый read-only аудит активной обработки.",
        )
        parser.add_argument(
            "--fail-on-critical",
            action="store_true",
            help="Вернуть ошибку при критичных сигналах. Данные всё равно не меняются.",
        )

    def handle(self, *args, **options):
        report = build_ai_inspection_report(deep=bool(options.get("deep")))
        if options.get("format") == "json":
            self.stdout.write(json.dumps(report, ensure_ascii=False, default=str))
        else:
            summary = report["summary"]
            self.stdout.write("WMS AI INSPECTOR: READ-ONLY")
            self.stdout.write("automatic_corrections=false")
            self.stdout.write(f"generated_at={report['generated_at_iso']}")
            self.stdout.write(
                "incidents="
                f"{summary['total']} critical={summary['critical']} "
                f"warning={summary['warning']} info={summary['info']}"
            )
            self.stdout.write(
                "sources="
                f"{summary['sources']} healthy={summary['healthy_sources']} "
                f"attention={summary['attention_sources']} "
                f"unavailable={summary['unavailable_sources']}"
            )
            for incident in report["incidents"]:
                self.stdout.write(
                    "  "
                    f"[{incident['severity'].upper()}] "
                    f"{incident['contour']}: {incident['title']} "
                    f"(count={incident['count']})"
                )
                self.stdout.write(f"    {incident['message']}")
                self.stdout.write(f"    Действие: {incident['recommendation']}")
            if not report["incidents"]:
                self.stdout.write(self.style.SUCCESS("Активных сигналов не найдено."))
            self.stdout.write("Данные и код не изменялись.")

        if options.get("fail_on_critical") and report["summary"]["critical"]:
            raise CommandError(
                f"Критичных сигналов: {report['summary']['critical']}. "
                "Данные не изменялись."
            )
