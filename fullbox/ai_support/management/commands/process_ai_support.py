from django.conf import settings
from django.core.management.base import BaseCommand

from ai_support.llm import DiagnosticConfigurationError, OpenAIResponsesDiagnosticClient
from ai_support.models import AgentAction
from ai_support.worker import process_next_incident


class Command(BaseCommand):
    help = "Обрабатывает очередь ИИ-поддержки в строго read-only диагностическом режиме."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=3)
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--quiet-if-disabled", action="store_true")

    def handle(self, *args, **options):
        limit = min(max(int(options["limit"]), 1), 20)
        pending = AgentAction.objects.filter(
            action_type="inspect_request",
            status=AgentAction.STATUS_PROPOSED,
        ).count()
        enabled = bool(getattr(settings, "AI_SUPPORT_DIAGNOSTICS_ENABLED", False))
        if options["dry_run"]:
            self.stdout.write(
                f"DRY-RUN enabled={str(enabled).lower()} pending={pending} limit={limit}"
            )
            return
        if not enabled:
            if not options["quiet_if_disabled"]:
                self.stdout.write(f"SKIPPED enabled=false pending={pending}")
            return
        try:
            diagnoser = OpenAIResponsesDiagnosticClient.from_settings()
        except DiagnosticConfigurationError as exc:
            if not options["quiet_if_disabled"]:
                self.stdout.write(self.style.WARNING(f"SKIPPED configuration: {exc}"))
            return

        processed = 0
        succeeded = 0
        failed = 0
        for _index in range(limit):
            action = process_next_incident(diagnoser=diagnoser)
            if not action:
                break
            processed += 1
            if action.status == AgentAction.STATUS_SUCCEEDED:
                succeeded += 1
            else:
                failed += 1
        self.stdout.write(
            self.style.SUCCESS(
                f"PROCESSED processed={processed} succeeded={succeeded} failed={failed}"
            )
        )
