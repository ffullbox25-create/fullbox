from __future__ import annotations

from django.core.management.base import BaseCommand

from market_sync.sync_all import run_sync_all_clients


class Command(BaseCommand):
    help = (
        "Ежедневная синхронизация карточек WB/Ozon для всех клиентов "
        "с настроенными API-ключами."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--pause",
            type=float,
            default=2.0,
            help="Пауза между клиентами в секундах (по умолчанию 2, против лимитов API).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Только показать, сколько клиентов будет обработано.",
        )

    def handle(self, *args, **options):
        pause = max(0.0, float(options.get("pause") or 0))
        if options.get("dry_run"):
            from market_sync.sync_all import configured_global_sync_targets

            targets = configured_global_sync_targets()
            self.stdout.write(
                self.style.WARNING(
                    f"dry-run: клиентов с ключами = {len(targets)}; pause={pause}s"
                )
            )
            for target in targets[:50]:
                agency = target["agency"]
                name = getattr(agency, "agn_name", None) or agency.id
                self.stdout.write(f"  - {agency.id}: {name} → {', '.join(target['markets'])}")
            if len(targets) > 50:
                self.stdout.write(f"  ... и ещё {len(targets) - 50}")
            return

        self.stdout.write(f"Старт sync карточек маркетплейсов (pause={pause}s)...")
        summary = run_sync_all_clients(pause_seconds=pause)
        self.stdout.write(
            "clients={clients_processed}/{clients_total} "
            "processed={processed} created={created} updated={updated} "
            "errors={clients_with_errors}".format(**summary)
        )
        for err in (summary.get("errors") or [])[:30]:
            self.stdout.write(self.style.WARNING(f"  ! {err}"))
        if summary.get("clients_total") == 0:
            self.stdout.write(self.style.ERROR("Нет клиентов для синхронизации."))
            return
        if summary.get("ok"):
            self.stdout.write(self.style.SUCCESS("Готово без критичных ошибок."))
        else:
            self.stdout.write(
                self.style.WARNING(
                    f"Готово с замечаниями у {summary.get('clients_with_errors')} клиентов."
                )
            )
