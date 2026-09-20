from django.core.management.base import BaseCommand

from audit.models import OrderAuditEntry
from client_cabinet.models import OtherRequest
from client_cabinet.other_request_workflow import (
    LEGACY_TO_NEW_STATUS,
    ensure_other_request_from_audit,
    seed_default_categories,
)
from client_cabinet.other_requests import ORDER_TYPE


class Command(BaseCommand):
    help = "Бэкафилл OtherRequest из OrderAuditEntry (other). Сначала --dry-run."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Только отчёт, без записи")
        parser.add_argument("--apply", action="store_true", help="Применить бэкафилл")
        parser.add_argument("--limit", type=int, default=0, help="Ограничить число заявок")

    def handle(self, *args, **options):
        dry = bool(options["dry_run"]) or not bool(options["apply"])
        if options["apply"]:
            dry = False
        seed_default_categories()
        order_ids = list(
            OrderAuditEntry.objects.filter(order_type=ORDER_TYPE)
            .values_list("order_id", flat=True)
            .distinct()
        )
        if options["limit"]:
            order_ids = order_ids[: int(options["limit"])]

        self.stdout.write(f"Найдено audit other: {len(order_ids)}")
        self.stdout.write("Соответствие статусов:")
        for legacy, new in LEGACY_TO_NEW_STATUS.items():
            self.stdout.write(f"  {legacy} → {new}")

        created = 0
        updated = 0
        skipped = 0
        for order_id in order_ids:
            existing = OtherRequest.objects.filter(public_number=order_id).first()
            if dry:
                latest = (
                    OrderAuditEntry.objects.filter(order_type=ORDER_TYPE, order_id=order_id)
                    .order_by("-created_at")
                    .first()
                )
                legacy = str((latest.payload or {}).get("status") or "").lower() if latest else ""
                new_status = LEGACY_TO_NEW_STATUS.get(legacy, OtherRequest.STATUS_AWAITING_MANAGER)
                mark = "exists" if existing else "create"
                self.stdout.write(f"  [{mark}] {order_id}: {legacy or '—'} → {new_status}")
                if existing:
                    updated += 1
                else:
                    created += 1
                continue
            before = existing is not None
            obj = ensure_other_request_from_audit(order_id)
            if obj is None:
                skipped += 1
                continue
            if before:
                updated += 1
            else:
                created += 1

        mode = "DRY-RUN" if dry else "APPLY"
        self.stdout.write(self.style.SUCCESS(
            f"{mode}: create≈{created}, update≈{updated}, skip={skipped}"
        ))
        if dry:
            self.stdout.write("Для записи запустите с --apply")
