from django.core.management.base import BaseCommand, CommandError

from fbs.exceptions import FbsError
from fbs.integrations.contracts import WB_FETCH_ORDER_STICKER
from fbs.models import (
    FbsIntegrationProfile,
    FbsMarketplaceCommand,
    FbsOrder,
    FbsOrderLabel,
)
from fbs.services import process_wb_label_queue


class Command(BaseCommand):
    help = "Показывает или получает только этикетки заказов WB, ожидающие печати."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=50)
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Получить этикетки WB. Без флага работает только dry-run.",
        )

    def handle(self, *args, **options):
        limit = max(int(options["limit"]), 1)
        if not options["apply"]:
            requested = FbsOrderLabel.objects.filter(
                status=FbsOrderLabel.STATUS_REQUESTED,
                order__internal_status=FbsOrder.STATUS_PICKED,
                order__profile__marketplace=FbsIntegrationProfile.MARKETPLACE_WB,
                order__profile__is_active=True,
                order__profile__outbox_enabled=True,
            ).count()
            queued = FbsMarketplaceCommand.objects.filter(
                command_type=WB_FETCH_ORDER_STICKER,
                status__in=(
                    FbsMarketplaceCommand.STATUS_PENDING,
                    FbsMarketplaceCommand.STATUS_RETRY,
                ),
            ).count()
            self.stdout.write(f"DRY-RUN requested={requested} queued={queued}")
            return
        try:
            result = process_wb_label_queue(limit=limit)
        except FbsError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(
            self.style.SUCCESS(
                "WB LABELS "
                f"inspected={result.inspected} confirmed={result.confirmed} "
                f"retry={result.retry} failed={result.failed} "
                f"conflict={result.conflict}"
            )
        )
