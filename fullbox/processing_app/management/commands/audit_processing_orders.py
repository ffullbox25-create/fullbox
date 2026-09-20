from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError

from processing_app.order_audit import audit_processing_orders


class Command(BaseCommand):
    help = (
        "Read-only аудит живых заявок обработки: этап, открытые задачи "
        "и блокеры закрытия. Команда никогда не исправляет данные."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--order-id",
            action="append",
            dest="order_ids",
            default=[],
            help="Проверить конкретную заявку. Аргумент можно повторять.",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=100,
            help="Максимальное количество строк в выводе.",
        )
        parser.add_argument(
            "--include-done",
            action="store_true",
            help="Показывать закрытые и отмененные заявки.",
        )
        parser.add_argument(
            "--only-issues",
            action="store_true",
            help="Показывать только заявки с проблемами.",
        )
        parser.add_argument(
            "--format",
            choices=("table", "json"),
            default="table",
            help="Формат вывода отчёта.",
        )
        parser.add_argument(
            "--fail-on-issues",
            action="store_true",
            help="Вернуть ошибку, если найдены проблемные заявки. Данные всё равно не меняются.",
        )

    def handle(self, *args, **options):
        report = audit_processing_orders(
            order_ids=options.get("order_ids") or [],
            limit=max(int(options.get("limit") or 0), 1),
            include_done=bool(options.get("include_done")),
            only_issues=bool(options.get("only_issues")),
        )
        if options.get("format") == "json":
            self.stdout.write(json.dumps(report.as_dict(), ensure_ascii=False))
        else:
            self.stdout.write("PROCESSING ORDER AUDIT: read-only")
            self.stdout.write(f"scanned={report.scanned_count}")
            self.stdout.write(f"rows={len(report.rows)}")
            self.stdout.write(f"issues={report.issue_count}")
            for row in report.rows:
                blockers = " | ".join(row.blockers[:3]) if row.blockers else "-"
                self.stdout.write(
                    "  "
                    f"order={row.external_number or row.order_id} "
                    f"client={row.agency_name or '-'} "
                    f"stage={row.stage or '-'} "
                    f"label={row.stage_label or '-'} "
                    f"open_tasks={row.open_tasks_count}/{row.tasks_count} "
                    f"qty={row.declared_qty}/{row.processed_qty}/{row.boxed_qty} "
                    f"blockers={blockers}"
                )
            if report.sample_truncated:
                self.stdout.write(
                    self.style.WARNING(
                        f"Показаны не все строки. Лимит: {len(report.rows)}."
                    )
                )
            if report.issue_count:
                self.stdout.write(
                    self.style.WARNING(
                        "Найдены проблемные заявки. Данные не изменялись."
                    )
                )
            else:
                self.stdout.write(
                    self.style.SUCCESS("Проблемных заявок не найдено. Данные не изменялись.")
                )

        if options.get("fail_on_issues") and report.issue_count:
            raise CommandError(f"Найдено проблемных заявок: {report.issue_count}.")
