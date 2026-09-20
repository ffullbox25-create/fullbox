from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError

from processing_app.closed_discrepancy_audit import (
    audit_closed_processing_discrepancies,
)


class Command(BaseCommand):
    help = (
        "Read-only проверка закрытых заявок обработки: заявлено / обработано / "
        "в коробах. Команда никогда не исправляет данные."
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
            help="Максимальное количество проблемных заявок в выводе.",
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
            help="Вернуть ошибку, если найдены расхождения. Данные всё равно не меняются.",
        )

    def handle(self, *args, **options):
        report = audit_closed_processing_discrepancies(
            order_ids=options.get("order_ids") or [],
            sample_limit=max(int(options.get("limit") or 0), 0),
        )
        if options.get("format") == "json":
            self.stdout.write(json.dumps(report.as_dict(), ensure_ascii=False))
        else:
            self.stdout.write("PROCESSING CLOSED DISCREPANCY AUDIT: read-only")
            self.stdout.write(f"scanned={report.scanned_count}")
            self.stdout.write(f"closed={report.closed_count}")
            self.stdout.write(f"matching={report.matching_count}")
            self.stdout.write(f"discrepancies={report.discrepancy_count}")
            for row in report.rows:
                self.stdout.write(
                    "  "
                    f"order={row.order_id} agency={row.agency_name or row.agency_id or '-'} "
                    f"declared={row.declared_qty} processed={row.processed_qty} "
                    f"boxed={row.boxed_qty} status={row.discrepancy_status or '-'} "
                    f"differences={','.join(row.differences)}"
                )
            if report.sample_truncated:
                self.stdout.write(
                    self.style.WARNING(
                        f"Показаны не все расхождения. Лимит: {len(report.rows)}."
                    )
                )
            if report.discrepancy_count:
                self.stdout.write(
                    self.style.WARNING(
                        "Найдены расхождения. Данные не изменялись."
                    )
                )
            else:
                self.stdout.write(
                    self.style.SUCCESS("Расхождений не найдено. Данные не изменялись.")
                )

        if options.get("fail_on_issues") and report.discrepancy_count:
            raise CommandError(
                f"Найдено закрытых заявок с расхождениями: {report.discrepancy_count}."
            )
