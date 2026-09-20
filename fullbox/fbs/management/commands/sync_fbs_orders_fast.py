from django.core.management.base import BaseCommand, CommandError

from fbs.exceptions import FbsError
from fbs.models import FbsIntegrationProfile
from fbs.services import pull_profile_orders


class Command(BaseCommand):
    help = "Быстро получает только новые и изменившиеся FBS-заказы WB/Ozon."

    def add_arguments(self, parser):
        parser.add_argument("--profile", type=int, action="append", dest="profile_ids")
        parser.add_argument("--limit", type=int, default=250)

    def handle(self, *args, **options):
        limit = max(int(options["limit"]), 1)
        profiles = (
            FbsIntegrationProfile.objects.select_related("agency")
            .filter(is_active=True, order_pull_enabled=True)
            .order_by("id")
        )
        if options.get("profile_ids"):
            profiles = profiles.filter(pk__in=options["profile_ids"])
        profiles = list(profiles)
        if not profiles:
            self.stdout.write("Нет активных профилей FBS для получения заказов.")
            return

        failures = []
        for profile in profiles:
            try:
                result = pull_profile_orders(
                    profile_id=profile.id,
                    limit=limit,
                    loaded_profile=profile,
                )
                self.stdout.write(
                    f"profile={profile.id} stream=orders_fast requests={result.requests} "
                    f"received={result.received} created={result.created} "
                    f"updated={result.updated} duplicate={result.duplicate} "
                    f"skipped={result.skipped}"
                )
            except FbsError as exc:
                failures.append(f"Профиль {profile.id}, заказы: {exc}")

        if failures:
            raise CommandError("; ".join(failures))
