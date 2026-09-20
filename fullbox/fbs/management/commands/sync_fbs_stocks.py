from django.core.management.base import BaseCommand, CommandError

from fbs.exceptions import FbsError
from fbs.models import FbsIntegrationProfile, FbsStockExportState
from fbs.services.stock_sync import sync_enabled_stock_exports


class Command(BaseCommand):
    help = "Выгружает отдельные FBS-остатки только во включенные профили складов WB/Ozon."

    def add_arguments(self, parser):
        parser.add_argument("--profile", type=int, action="append", dest="profile_ids")
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Выполнить реальные API-запросы. Без флага выводится только очередь.",
        )

    def handle(self, *args, **options):
        profile_ids = tuple(options.get("profile_ids") or ())
        profiles = FbsIntegrationProfile.objects.filter(
            is_active=True,
            stock_push_enabled=True,
            stock_mode=FbsIntegrationProfile.STOCK_MODE_MANAGED,
        ).order_by("id")
        if profile_ids:
            profiles = profiles.filter(id__in=profile_ids)
        if not options["apply"]:
            states = FbsStockExportState.objects.filter(profile__in=profiles).exclude(
                status=FbsStockExportState.STATUS_SYNCED
            )
            self.stdout.write(
                f"DRY-RUN profiles={profiles.count()} pending_states={states.count()}"
            )
            return
        try:
            result = sync_enabled_stock_exports(profile_ids=profile_ids)
        except FbsError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(
            self.style.SUCCESS(
                "FBS_STOCK_SYNC "
                f"profiles={result.profiles} inspected={result.inspected} "
                f"requests={result.requests} synced={result.synced} "
                f"retry={result.retry} blocked={result.blocked} skipped={result.skipped}"
            )
        )
