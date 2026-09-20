from django.core.management.base import BaseCommand

from billing.tariff_catalog import seed_tariff_catalog


class Command(BaseCommand):
    help = "Seed client tariff categories and units"

    def handle(self, *args, **options):
        result = seed_tariff_catalog()
        self.stdout.write(
            self.style.SUCCESS(
                "Tariff catalog synced: "
                f"{result['categories_total']} categories, {result['units_total']} units "
                f"({result['categories_created']} + {result['units_created']} created)"
            )
        )
