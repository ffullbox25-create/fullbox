from django.core.management.base import BaseCommand

from billing.sync import sync_billing_applications


class Command(BaseCommand):
    help = "Подтянуть операционные заявки (audit/shipping) в регистр биллинга"

    def add_arguments(self, parser):
        parser.add_argument(
            "--limit",
            type=int,
            default=20000,
            help="Сколько последних записей аудита просмотреть",
        )

    def handle(self, *args, **options):
        result = sync_billing_applications(limit=options["limit"])
        self.stdout.write(
            self.style.SUCCESS(
                "Готово: создано {created}, обновлено {updated}, пропущено {skipped}, всего в регистре {total}".format(
                    **result
                )
            )
        )
