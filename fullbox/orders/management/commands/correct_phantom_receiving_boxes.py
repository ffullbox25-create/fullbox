from __future__ import annotations

import json
from dataclasses import asdict

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from orders.receiving_corrections import ReceivingCorrectionService


class Command(BaseCommand):
    help = (
        "Safely archive boxes never physically received and append corrected receiving acts. "
        "The default mode is a read-only dry run."
    )

    def add_arguments(self, parser):
        parser.add_argument("--order-id", required=True)
        parser.add_argument("--box-code", action="append", dest="box_codes", required=True)
        parser.add_argument("--reason", required=True)
        parser.add_argument("--actor-username", required=True)
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Commit the correction. Without this flag the command only validates and reports.",
        )

    def handle(self, *args, **options):
        username = str(options.get("actor_username") or "").strip()
        actor = get_user_model().objects.filter(username=username, is_active=True).first()
        if actor is None:
            raise CommandError(f"Active user {username!r} was not found.")
        try:
            result = ReceivingCorrectionService.correct_phantom_boxes(
                order_id=options.get("order_id"),
                box_codes=options.get("box_codes") or [],
                reason=options.get("reason"),
                user=actor,
                apply=bool(options.get("apply")),
            )
        except ValueError as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write(json.dumps(asdict(result), ensure_ascii=False, indent=2))
        if result.status == "already_applied":
            self.stdout.write(self.style.WARNING("Correction was already applied; no data changed."))
        elif result.applied:
            self.stdout.write(self.style.SUCCESS("Correction committed."))
        else:
            self.stdout.write(self.style.WARNING("DRY RUN: read-only validation complete; no data changed."))
