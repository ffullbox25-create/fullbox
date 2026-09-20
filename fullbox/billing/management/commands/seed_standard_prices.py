from django.core.management.base import BaseCommand

from billing.standard_price_catalog import seed_standard_prices


class Command(BaseCommand):
    help = "Seed FullBox standard billing price catalog"

    def handle(self, *args, **options):
        result = seed_standard_prices()
        self.stdout.write(
            self.style.SUCCESS(
                f"Standard prices synced: {result['total']} total, {result['created']} created, {result['updated']} updated"
            )
        )
