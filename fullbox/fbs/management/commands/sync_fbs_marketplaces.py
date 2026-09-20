from django.core.management.base import BaseCommand, CommandError

from django.db.models import Q

from fbs.exceptions import FbsError
from fbs.models import (
    FbsHandoverBatch,
    FbsHandoverOrderAssignment,
    FbsIntegrationProfile,
    FbsOrder,
)
from fbs.services import pull_profile_orders, pull_profile_statuses
from fbs.services.marketplace import recover_stalled_client_cancellations
from fbs.services.pick_restock import (
    WB_CLIENT_CANCEL_STATUSES,
    ensure_cancelled_order_pick_restock,
    order_is_client_canceled_by_marketplace,
)
from fbs.services.totes import (
    auto_finalize_trusted_wb_handovers,
    auto_finalize_verified_ozon_handovers,
)


class Command(BaseCommand):
    help = "Получает заказы и статусы FBS из включенных профилей WB/Ozon."

    def add_arguments(self, parser):
        parser.add_argument("--profile", type=int, action="append", dest="profile_ids")
        parser.add_argument("--limit", type=int, default=100)
        parser.add_argument("--orders-only", action="store_true")
        parser.add_argument("--statuses-only", action="store_true")

    def handle(self, *args, **options):
        if options["orders_only"] and options["statuses_only"]:
            raise CommandError("Нельзя одновременно указать --orders-only и --statuses-only.")
        limit = max(int(options["limit"]), 1)
        profiles = (
            FbsIntegrationProfile.objects.select_related("agency")
            .filter(is_active=True)
            .order_by("id")
        )
        if options.get("profile_ids"):
            profiles = profiles.filter(pk__in=options["profile_ids"])
        profiles = list(profiles)
        if not profiles:
            self.stdout.write("Нет активных профилей FBS для синхронизации.")
            return

        ozon_profile_ids = [
            profile.id
            for profile in profiles
            if profile.marketplace == FbsIntegrationProfile.MARKETPLACE_OZON
        ]
        wb_profile_ids = [
            profile.id
            for profile in profiles
            if profile.marketplace == FbsIntegrationProfile.MARKETPLACE_WB
        ]
        try:
            recovered = recover_stalled_client_cancellations(limit=min(limit, 20), profile_ids=wb_profile_ids)
            if recovered:
                self.stdout.write(f"stream=wb_cancel_readback recovered={recovered}")
        except FbsError as exc:
            self.stderr.write(f"stream=wb_cancel_readback error={exc}")
        wb_ready_before_sync = auto_finalize_trusted_wb_handovers(
            profile_ids=wb_profile_ids,
            limit=limit,
        )
        self.stdout.write(
            "stream=wb_trusted_auto_finalize phase=before_sync "
            f"ready={len(wb_ready_before_sync)}"
        )
        dispatched_before_sync = auto_finalize_verified_ozon_handovers(
            profile_ids=ozon_profile_ids,
            limit=limit,
        )
        self.stdout.write(
            "stream=ozon_auto_dispatch phase=before_sync "
            f"dispatched={len(dispatched_before_sync)}"
        )

        failures = []
        for profile in profiles:
            if not options["statuses_only"] and profile.order_pull_enabled:
                try:
                    result = pull_profile_orders(
                        profile_id=profile.id,
                        limit=limit,
                        loaded_profile=profile,
                    )
                    self.stdout.write(
                        f"profile={profile.id} stream=orders requests={result.requests} "
                        f"received={result.received} created={result.created} "
                        f"updated={result.updated} duplicate={result.duplicate} "
                        f"skipped={result.skipped}"
                    )
                except FbsError as exc:
                    failures.append(f"Профиль {profile.id}, заказы: {exc}")
            if not options["orders_only"] and profile.status_pull_enabled:
                try:
                    result = pull_profile_statuses(
                        profile_id=profile.id,
                        limit=limit,
                        loaded_profile=profile,
                    )
                    self.stdout.write(
                        f"profile={profile.id} stream=statuses requests={result.requests} "
                        f"received={result.received} updated={result.updated} "
                        f"duplicate={result.duplicate} skipped={result.skipped}"
                    )
                except FbsError as exc:
                    failures.append(f"Профиль {profile.id}, статусы: {exc}")
        dispatched_after_sync = auto_finalize_verified_ozon_handovers(
            profile_ids=ozon_profile_ids,
            limit=limit,
        )
        self.stdout.write(
            "stream=ozon_auto_dispatch phase=after_sync "
            f"dispatched={len(dispatched_after_sync)}"
        )
        wb_ready_after_sync = auto_finalize_trusted_wb_handovers(
            profile_ids=wb_profile_ids,
            limit=limit,
        )
        self.stdout.write(
            "stream=wb_trusted_auto_finalize phase=after_sync "
            f"ready={len(wb_ready_after_sync)}"
        )
        # A cancellation queues the physical return only when an import pass
        # happens to catch the transition.  When that pass never runs again --
        # a cancelled order usually stops coming back in the active feed -- the
        # order keeps sitting in an open shipment with no return staged, and the
        # controller is never told to move the goods to the canceled tote.
        # Re-check the open shipments on every run; the service is idempotent.
        staged_returns = 0
        open_assignment_order_ids = list(
            FbsHandoverOrderAssignment.objects.filter(
                status__in=(
                    FbsHandoverOrderAssignment.STATUS_CONFIRMED,
                    FbsHandoverOrderAssignment.STATUS_ERROR,
                ),
                batch__status__in=(
                    FbsHandoverBatch.STATUS_OPEN,
                    FbsHandoverBatch.STATUS_READY,
                ),
            )
            .values_list("order_id", flat=True)
            .distinct()
        )
        # This runs every 30 seconds, so narrow the candidates in SQL instead of
        # asking about every order of every open shipment.  The marketplace
        # helper still makes the final call: WB and Ozon spell cancellation
        # differently, and some WB spellings carry no "cancel" substring.
        cancel_lookup = (
            Q(marketplace_status__icontains="cancel")
            | Q(marketplace_substatus__icontains="cancel")
            | Q(marketplace_status__in=WB_CLIENT_CANCEL_STATUSES)
            | Q(marketplace_substatus__in=WB_CLIENT_CANCEL_STATUSES)
        )
        for order in (
            FbsOrder.objects.filter(id__in=open_assignment_order_ids)
            .filter(cancel_lookup)
            .filter(pick_restock_requests__isnull=True)
            .select_related("profile")
            .order_by("id")
            .distinct()[:limit]
        ):
            if not order_is_client_canceled_by_marketplace(order):
                continue
            try:
                if ensure_cancelled_order_pick_restock(order_id=order.id) is not None:
                    staged_returns += 1
            except FbsError as exc:
                failures.append(
                    f"Заказ {order.external_order_id}, снятие отменённого: {exc}"
                )
        self.stdout.write(
            f"stream=canceled_orders phase=after_sync staged={staged_returns}"
        )
        if failures:
            raise CommandError("; ".join(failures))
