import time

from django.core.management.base import BaseCommand, CommandError
from django.db import OperationalError, close_old_connections

from fbs.exceptions import FbsError
from fbs.models import FbsMarketplaceCommand
from fbs.services import process_marketplace_queue


DEFAULT_SCHEDULE_INTERVAL = 30.0


class Command(BaseCommand):
    help = "Показывает или обрабатывает очередь команд FBS для WB/Ozon."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=50)
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Выполнить реальные API-запросы. Без флага работает только dry-run.",
        )
        parser.add_argument(
            "--watch",
            action="store_true",
            help="Постоянно обрабатывать очередь без повторного запуска Django.",
        )
        parser.add_argument("--idle-sleep", type=float, default=0.5)
        parser.add_argument("--active-sleep", type=float, default=0.2)
        parser.add_argument(
            "--schedule-interval",
            type=float,
            default=DEFAULT_SCHEDULE_INTERVAL,
            help="Интервал запуска планировщиков очереди, не менее 30 секунд.",
        )

    def handle(self, *args, **options):
        limit = max(int(options["limit"]), 1)
        watch = bool(options["watch"])
        if not options["apply"]:
            if watch:
                raise CommandError("Режим --watch доступен только вместе с --apply.")
            rows = list(
                FbsMarketplaceCommand.objects.filter(
                    status__in=(
                        FbsMarketplaceCommand.STATUS_PENDING,
                        FbsMarketplaceCommand.STATUS_RETRY,
                    )
                )
                .select_related("profile", "order")
                .order_by("created_at", "id")[:limit]
            )
            self.stdout.write(f"DRY-RUN pending={len(rows)}")
            for row in rows:
                self.stdout.write(
                    f"#{row.id} {row.profile.get_marketplace_display()} "
                    f"{row.command_type} {row.http_method} {row.endpoint}"
                )
            return

        idle_sleep = max(float(options["idle_sleep"]), 0.1)
        active_sleep = max(float(options["active_sleep"]), 0.05)
        schedule_interval = max(
            float(options["schedule_interval"]),
            DEFAULT_SCHEDULE_INTERVAL,
        )
        next_schedule_at = 0.0
        if watch:
            self.stdout.write(
                self.style.SUCCESS(
                    "WATCHING FBS marketplace queue "
                    f"limit={limit} idle_sleep={idle_sleep:.2f}s "
                    f"active_sleep={active_sleep:.2f}s "
                    f"schedule_interval={schedule_interval:.2f}s"
                )
            )

        while True:
            now = time.monotonic()
            run_schedulers = not watch or now >= next_schedule_at
            if run_schedulers and watch:
                next_schedule_at = now + schedule_interval
            try:
                result = process_marketplace_queue(
                    limit=limit,
                    run_schedulers=run_schedulers,
                    run_interactive_schedulers=watch,
                )
            except FbsError as exc:
                if not watch:
                    raise CommandError(str(exc)) from exc
                self.stderr.write(self.style.ERROR(f"FBS QUEUE ERROR: {exc}"))
                time.sleep(idle_sleep)
                continue
            except OperationalError as exc:
                if not watch:
                    raise CommandError(str(exc)) from exc
                self.stderr.write(
                    self.style.ERROR(f"FBS QUEUE DATABASE ERROR: {exc}")
                )
                # A deadlock has already rolled its transaction back. Also
                # discard stale/broken connections so the long-running watch
                # process can safely execute the next independent queue pass.
                close_old_connections()
                time.sleep(idle_sleep)
                continue

            if not watch or result.inspected:
                self.stdout.write(
                    self.style.SUCCESS(
                        "PROCESSED "
                        f"inspected={result.inspected} confirmed={result.confirmed} "
                        f"retry={result.retry} failed={result.failed} "
                        f"conflict={result.conflict}"
                    )
                )
            if not watch:
                return
            time.sleep(active_sleep if result.inspected else idle_sleep)
