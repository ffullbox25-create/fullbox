from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db.models import F, Sum

from sklad.models import WarehouseReserve, WarehouseStockSnapshot


ACTIVE_RESERVE_STATUSES = {
    WarehouseReserve.STATUS_ACTIVE,
    WarehouseReserve.STATUS_PARTIALLY_ALLOCATED,
    WarehouseReserve.STATUS_ALLOCATED,
    WarehouseReserve.STATUS_PARTIALLY_SATISFIED,
}

GROUP_FIELDS = ("agency_id", "sku_code", "size", "barcode", "goods_type")


class Command(BaseCommand):
    help = "Read-only диагностика складских остатков и резервов без исправления данных."

    def add_arguments(self, parser):
        parser.add_argument("--agency-id", type=int, default=None, help="Ограничить проверку одним клиентом.")
        parser.add_argument("--limit", type=int, default=50, help="Сколько проблемных строк вывести по каждому разделу.")
        parser.add_argument(
            "--fail-on-issues",
            action="store_true",
            help="Вернуть exit code 1, если найдены расхождения. По умолчанию команда только сообщает.",
        )

    def handle(self, *args, **options):
        agency_id = options.get("agency_id")
        limit = max(int(options.get("limit") or 50), 1)
        fail_on_issues = bool(options.get("fail_on_issues"))

        snapshots = WarehouseStockSnapshot.objects.filter(is_archived=False)
        reserves = WarehouseReserve.objects.filter(status__in=ACTIVE_RESERVE_STATUSES)
        if agency_id:
            snapshots = snapshots.filter(agency_id=agency_id)
            reserves = reserves.filter(agency_id=agency_id)

        invalid_snapshots = list(
            snapshots.filter(qty__lt=F("available_qty"))
            .values("id", "agency_id", "sku_code", "size", "barcode", "goods_type", "qty", "available_qty")[:limit]
        )

        available_by_key = {
            tuple(row[field] or "" for field in GROUP_FIELDS): int(row["available"] or 0)
            for row in snapshots.values(*GROUP_FIELDS).annotate(available=Sum("available_qty")).iterator(chunk_size=2000)
        }
        reserved_rows = reserves.values(*GROUP_FIELDS).annotate(reserved=Sum("qty_reserved")).iterator(chunk_size=2000)

        over_reserved = []
        reserve_without_stock = []
        for row in reserved_rows:
            key = tuple(row[field] or "" for field in GROUP_FIELDS)
            reserved = int(row["reserved"] or 0)
            available = available_by_key.get(key, 0)
            if available <= 0:
                reserve_without_stock.append((row, reserved, available))
            elif reserved > available:
                over_reserved.append((row, reserved, available))
            if len(over_reserved) >= limit and len(reserve_without_stock) >= limit:
                break

        self.stdout.write("WAREHOUSE CONSISTENCY CHECK: read-only")
        if agency_id:
            self.stdout.write(f"agency_id={agency_id}")
        self.stdout.write(f"invalid_snapshots={len(invalid_snapshots)}")
        self.stdout.write(f"over_reserved_sample={len(over_reserved[:limit])}")
        self.stdout.write(f"reserve_without_stock_sample={len(reserve_without_stock[:limit])}")

        if invalid_snapshots:
            self.stdout.write("\nINVALID SNAPSHOTS qty < available_qty:")
            for row in invalid_snapshots:
                self.stdout.write(
                    f"  snapshot={row['id']} agency={row['agency_id']} sku={row['sku_code']} "
                    f"size={row['size'] or '-'} barcode={row['barcode'] or '-'} "
                    f"qty={row['qty']} available={row['available_qty']}"
                )

        if over_reserved:
            self.stdout.write("\nOVER RESERVED active reserve > available:")
            for row, reserved, available in over_reserved[:limit]:
                self.stdout.write(
                    f"  agency={row['agency_id']} sku={row['sku_code']} size={row['size'] or '-'} "
                    f"barcode={row['barcode'] or '-'} goods_type={row['goods_type'] or '-'} "
                    f"reserved={reserved} available={available}"
                )

        if reserve_without_stock:
            self.stdout.write("\nRESERVE WITHOUT AVAILABLE STOCK:")
            for row, reserved, available in reserve_without_stock[:limit]:
                self.stdout.write(
                    f"  agency={row['agency_id']} sku={row['sku_code']} size={row['size'] or '-'} "
                    f"barcode={row['barcode'] or '-'} goods_type={row['goods_type'] or '-'} "
                    f"reserved={reserved} available={available}"
                )

        issues_found = bool(invalid_snapshots or over_reserved or reserve_without_stock)
        if issues_found:
            self.stdout.write(self.style.WARNING("\nНайдены расхождения. Данные не изменялись."))
            if fail_on_issues:
                raise SystemExit(1)
        else:
            self.stdout.write(self.style.SUCCESS("\nРасхождений не найдено."))
