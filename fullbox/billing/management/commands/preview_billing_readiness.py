"""Bounded, PostgreSQL-enforced read-only readiness preview for one client."""
import json

from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from django.utils import timezone

from billing.models import BillingApplication
from billing.readiness import preview_application_readiness


class Command(BaseCommand):
    help = "Проверить факты и тарифы клиента без создания начислений и документов."

    def add_arguments(self, parser):
        parser.add_argument("--client", type=int, required=True)
        parser.add_argument("--application", type=int, help="PK заявки биллинга, не номер складской заявки")
        parser.add_argument("--limit", type=int, default=20)

    def handle(self, *args, **options):
        if options["client"] <= 0 or not 1 <= options["limit"] <= 100:
            raise CommandError("Укажите положительный ID клиента и limit от 1 до 100.")
        if options.get("application") is not None and options["application"] <= 0:
            raise CommandError("ID заявки должен быть положительным.")
        if connection.vendor != "postgresql" or connection.in_atomic_block:
            raise CommandError("Для безопасной проверки нужна отдельная read-only транзакция PostgreSQL.")
        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
                cursor.execute("SET LOCAL statement_timeout = '15s'")
            queryset = BillingApplication.objects.filter(client_id=options["client"]).select_related(
                "client", "legal_entity", "own_company"
            ).order_by("pk")
            if options.get("application"):
                queryset = queryset.filter(pk=options["application"])
            applications = list(queryset[:options["limit"] + 1])
            if not applications:
                raise CommandError("Заявки этого клиента по указанному фильтру не найдены.")
            output = {
                "generated_at": timezone.now().isoformat(),
                "read_only": True,
                "client_id": options["client"],
                "truncated": len(applications) > options["limit"],
                "scope": "Reported warehouse facts and tariff availability only; not approval, duplicate reconciliation or invoice calculation.",
                "applications": [preview_application_readiness(a) for a in applications[:options["limit"]]],
            }
        self.stdout.write(json.dumps(output, ensure_ascii=False, default=str))
