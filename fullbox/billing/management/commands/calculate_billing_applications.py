from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.utils.dateparse import parse_date

from billing.calculators.engine import calculate_application
from billing.models import BillingApplication
from billing.selectors import billing_applications_queryset


class Command(BaseCommand):
    help = "Рассчитать начисления по складским заявкам биллинга"

    def add_arguments(self, parser):
        parser.add_argument("--type", dest="application_type", default="all", help="receiving|processing|shipping|storage|other|all")
        parser.add_argument("--date-from", default="", help="Дата заявки с YYYY-MM-DD")
        parser.add_argument("--date-to", default="", help="Дата заявки по YYYY-MM-DD")
        parser.add_argument("--client", type=int, default=None, help="ID клиента (Agency)")
        parser.add_argument("--limit", type=int, default=500, help="Максимум заявок за запуск")

    def handle(self, *args, **options):
        params = {}
        app_type = (options["application_type"] or "all").strip()
        if app_type != "all":
            valid_types = {value for value, _label in BillingApplication.APPLICATION_TYPE_CHOICES}
            if app_type not in valid_types:
                raise CommandError(f"Неизвестный тип заявки: {app_type}")
            params["application_type"] = app_type
        if options.get("client"):
            params["client"] = options["client"]
        if options.get("date_from"):
            if not parse_date(options["date_from"]):
                raise CommandError("Некорректная --date-from")
            params["date_from"] = options["date_from"]
        if options.get("date_to"):
            if not parse_date(options["date_to"]):
                raise CommandError("Некорректная --date-to")
            params["date_to"] = options["date_to"]

        qs = billing_applications_queryset(params).exclude(application_type=BillingApplication.TYPE_STORAGE)
        qs = qs.select_related("client", "legal_entity")[: max(int(options["limit"] or 500), 1)]

        apps = 0
        charges = 0
        missing = 0
        for application in qs:
            result = calculate_application(application)
            apps += 1
            charges += len(result["charges"])
            missing += len(result["missing_tariffs"])
        self.stdout.write(
            self.style.SUCCESS(
                f"Рассчитано заявок: {apps}, начислений: {charges}, услуг без тарифа: {missing}"
            )
        )
