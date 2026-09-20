from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from logistics.models import LogisticsTrip
from logistics.services import (
    _ship_trip_orders_after_departure,
    _sync_trip_loading_completion_to_warehouse,
)


class Command(BaseCommand):
    help = (
        "Синхронизирует уже ушедшие в рейс логистические рейсы со складским контуром "
        "и закрывает незавершенные отгрузки."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--trip",
            dest="trip_number",
            default="",
            help="Номер конкретного рейса для исправления, например 2_RS.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Только показать, какие рейсы будут исправлены, без изменений в базе.",
        )

    def handle(self, *args, **options):
        trip_number = str(options.get("trip_number") or "").strip()
        dry_run = bool(options.get("dry_run"))
        trips = LogisticsTrip.objects.filter(status=LogisticsTrip.STATUS_DEPARTED).order_by("id")
        if trip_number:
            trips = trips.filter(number=trip_number)
        if not trips.exists():
            lookup = f" с номером {trip_number}" if trip_number else ""
            raise CommandError(f"Не найдено рейсов в статусе 'В рейсе'{lookup}.")

        processed = 0
        repaired = 0
        skipped = 0
        for trip in trips:
            processed += 1
            open_orders = [
                link.shipping_order
                for link in trip.orders.select_related("shipping_order").order_by("loading_sequence", "id")
                if not link.shipping_order.is_closed() and link.shipping_order.items.exists()
            ]
            if not open_orders:
                skipped += 1
                self.stdout.write(
                    self.style.WARNING(
                        f"[skip] {trip.number}: нет открытых заявок с товарными позициями для списания."
                    )
                )
                continue
            if dry_run:
                repaired += 1
                self.stdout.write(
                    self.style.SUCCESS(
                        f"[dry-run] {trip.number}: будет исправлено заявок {len(open_orders)}."
                    )
                )
                continue

            _sync_trip_loading_completion_to_warehouse(trip, user=None)
            _ship_trip_orders_after_departure(trip, user=None)
            repaired += 1
            self.stdout.write(
                self.style.SUCCESS(
                    f"[ok] {trip.number}: закрыто заявок {len(open_orders)}."
                )
            )

        self.stdout.write(
            self.style.SUCCESS(
                f"Готово. Проверено рейсов: {processed}, исправлено: {repaired}, пропущено: {skipped}."
            )
        )
