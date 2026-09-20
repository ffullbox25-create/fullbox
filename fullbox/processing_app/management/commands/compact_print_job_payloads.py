from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q
from django.utils import timezone

from processing_app.models import ProcessingPrintJob


class Command(BaseCommand):
    help = "Clear image payloads from completed print jobs while preserving job history."

    def add_arguments(self, parser):
        parser.add_argument("--older-than-days", type=int, default=7)
        parser.add_argument("--batch-size", type=int, default=500)
        parser.add_argument(
            "--include-stale-printing",
            action="store_true",
            help="Also reconcile old claimed jobs as printed before clearing their payloads.",
        )
        parser.add_argument(
            "--execute",
            action="store_true",
            help="Apply changes. Without this flag the command is a dry run.",
        )

    def handle(self, *args, **options):
        days = int(options["older_than_days"])
        batch_size = int(options["batch_size"])
        if days < 0:
            raise CommandError("--older-than-days must be non-negative")
        if batch_size < 1 or batch_size > 5000:
            raise CommandError("--batch-size must be between 1 and 5000")

        cutoff = timezone.now() - timedelta(days=days)
        statuses = [ProcessingPrintJob.STATUS_PRINTED]
        if options["include_stale_printing"]:
            statuses.append(ProcessingPrintJob.STATUS_PRINTING)
        candidates = ProcessingPrintJob.objects.filter(
            status__in=statuses,
            updated_at__lt=cutoff,
        ).filter(
            ~Q(label_png_base64="") | ~Q(label_png_base64_list=[])
        )
        candidate_count = candidates.count()
        mode = "EXECUTE" if options["execute"] else "DRY RUN"
        self.stdout.write(
            f"{mode}: {candidate_count} completed print-job payload(s) before {cutoff.isoformat()}"
        )
        if not options["execute"] or candidate_count == 0:
            return

        compacted = 0
        while True:
            job_ids = list(candidates.order_by("id").values_list("id", flat=True)[:batch_size])
            if not job_ids:
                break
            compacted += ProcessingPrintJob.objects.filter(id__in=job_ids).update(
                status=ProcessingPrintJob.STATUS_PRINTED,
                error="",
                label_png_base64="",
                label_png_base64_list=[],
            )
        self.stdout.write(self.style.SUCCESS(f"Compacted {compacted} print-job payload(s)."))
