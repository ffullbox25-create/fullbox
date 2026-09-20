from django.core.management.base import BaseCommand, CommandError

from fbs.exceptions import FbsError
from fbs.services import analyze_replenishment_demand, generate_replenishment_plans
from sku.models import Agency


class Command(BaseCommand):
    help = "Рассчитывает потребность FBS; с --apply создает предложенные планы подсорта."

    def add_arguments(self, parser):
        parser.add_argument("--agency-id", type=int)
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Создать планы в статусе proposed без резервов и складских движений.",
        )

    def handle(self, *args, **options):
        agency = None
        if options["agency_id"]:
            try:
                agency = Agency.objects.get(pk=options["agency_id"])
            except Agency.DoesNotExist as exc:
                raise CommandError("Клиент не найден.") from exc

        try:
            if options["apply"]:
                result = generate_replenishment_plans(agency=agency)
                analysis = result.analysis
            else:
                result = None
                analysis = analyze_replenishment_demand(agency=agency)
        except FbsError as exc:
            raise CommandError(str(exc)) from exc

        for row in analysis.rows:
            self.stdout.write(
                " | ".join(
                    [
                        row.agency_name,
                        row.sku_code,
                        f"orders={row.ordered_qty}",
                        f"fbs={row.fbs_qty}",
                        f"plans={row.open_plan_qty}",
                        f"general={row.general_available_qty}",
                        f"proposed={row.proposed_qty}",
                        f"blocked={row.general_shortage_qty + row.destination_blocked_qty}",
                    ]
                )
            )

        self.stdout.write(
            "TOTAL "
            f"orders={analysis.order_count} "
            f"qty={analysis.ordered_qty} "
            f"proposed={analysis.proposed_qty} "
            f"blocked={analysis.blocked_qty} "
            f"unmapped_lines={analysis.unmapped_line_count}"
        )
        if result is not None:
            self.stdout.write(
                self.style.SUCCESS(
                    f"CREATED plans={len(result.plans)} skipped={len(result.skipped_agencies)}"
                )
            )
            for reason in result.skipped_agencies:
                self.stdout.write(self.style.WARNING(f"SKIPPED {reason}"))
