from datetime import date

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from django.utils.dateparse import parse_date

from billing.storage_billing import StorageBillingService
from fbs.exceptions import FbsFeatureDisabled
from fbs.services.billing import capture_daily_storage_usage
from sku.models import Agency


class Command(BaseCommand):
    help = "Зафиксировать отдельно общее и FBS-хранение — склад только читаем"

    def add_arguments(self, parser):
        parser.add_argument("--date", type=str, default="", help="Дата YYYY-MM-DD (по умолчанию сегодня)")
        parser.add_argument("--client", type=int, default=None, help="ID клиента (Agency)")

    def handle(self, *args, **options):
        day = None
        raw = (options.get("date") or "").strip()
        if raw:
            day = parse_date(raw)
            if not isinstance(day, date):
                raise CommandError(f"Некорректная дата: {raw}")
        client = None
        if options.get("client") is not None:
            client = Agency.objects.filter(pk=options["client"]).first()
            if client is None:
                raise CommandError(f"Клиент с ID {options['client']} не найден.")
        result = StorageBillingService.run_daily_for_all_clients(
            day=day,
            client_id=options.get("client"),
        )
        usage_day = day or timezone.localdate()
        try:
            fbs_result = capture_daily_storage_usage(
                usage_date=usage_day,
                agency=client,
            )
        except FbsFeatureDisabled as exc:
            fbs_result = None
            self.stdout.write(self.style.WARNING(f"FBS-хранение пропущено: {exc}"))
        self.stdout.write(
            self.style.SUCCESS(
                "Хранение {day}: клиентов {clients}, палет {total_pallets}, нулевых дней {zero_days}".format(**result)
            )
        )
        if fbs_result is not None:
            self.stdout.write(
                self.style.SUCCESS(
                    "FBS-хранение {day}: клиентов {clients}, строк {rows}, "
                    "строк без габаритов {incomplete}".format(
                        day=fbs_result.usage_date,
                        clients=fbs_result.agencies,
                        rows=fbs_result.rows,
                        incomplete=fbs_result.incomplete_dimension_rows,
                    )
                )
            )
