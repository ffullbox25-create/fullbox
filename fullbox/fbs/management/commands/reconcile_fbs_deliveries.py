"""Explicit, bounded reconciliation; --apply always re-reads marketplace status."""
import json
from collections import Counter
from django.core.management.base import BaseCommand, CommandError
from fbs.models import FbsOrder, FbsIntegrationProfile
from fbs.integrations.http import RequestsMarketplaceReadTransport
from fbs.integrations.wb import build_wb_statuses_spec, parse_wb_statuses
from fbs.integrations.ozon import build_ozon_posting_status_spec, parse_ozon_posting_status
from fbs.services.delivery_reconciliation import (
    delivered_orders_q, inspect_delivered_order, reconcile_delivered_order,
)


class Command(BaseCommand):
    help = "Сверка доставленных FBS: без повторного списания; по умолчанию только проверка."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true")
        parser.add_argument("--order-id", action="append", type=int, default=[])
        parser.add_argument("--limit", type=int, default=5000)

    def handle(self, *args, **options):
        if not 1 <= options["limit"] <= 5000:
            raise CommandError("limit должен быть от 1 до 5000")
        if options["apply"] and not options["order_id"]:
            raise CommandError("Для --apply нужен явный список --order-id после проверки")
        qs=FbsOrder.objects.filter(delivered_orders_q()).exclude(internal_status="delivered").order_by("id")
        if options["order_id"]:
            qs=qs.filter(pk__in=options["order_id"])
        ids=list(qs.values_list("id",flat=True)[:options["limit"]])
        decisions=[inspect_delivered_order(oid) for oid in ids]
        if options["apply"]:
            eligible={d["order_id"] for d in decisions if d["action"]=="reconcile_status"}
            rows=list(FbsOrder.objects.filter(pk__in=eligible).select_related("profile").order_by("profile_id","id"))
            evidence={}
            transport=RequestsMarketplaceReadTransport(reuse_connections=True)
            try:
                profiles={r.profile_id:r.profile for r in rows}
                for pid,profile in profiles.items():
                    group=[r for r in rows if r.profile_id==pid]
                    if profile.marketplace==FbsIntegrationProfile.MARKETPLACE_WB:
                        for start in range(0,len(group),100):
                            chunk=group[start:start+100]
                            numeric=[int(r.external_order_id) for r in chunk]
                            response=transport.send(profile,build_wb_statuses_spec(numeric))
                            if response.status_code!=200:
                                raise CommandError(f"WB profile {pid}: HTTP {response.status_code}; no changes applied")
                            for status in parse_wb_statuses(response.json_payload):
                                evidence[(pid,str(status["external_order_id"]))]=status
                    else:
                        for row in group:
                            response=transport.send(profile,build_ozon_posting_status_spec(row.external_order_id))
                            if response.status_code!=200:
                                raise CommandError(f"Ozon order {row.pk}: HTTP {response.status_code}; no changes applied")
                            status=parse_ozon_posting_status(response.json_payload)
                            evidence[(pid,str(status["external_order_id"]))]=status
                    self.stderr.write(f"Live confirmation read: profile={pid}, orders={len(group)}")
            finally:
                transport.close()
            # No mutation before ALL marketplace reads have finished successfully.
            by_id={r.pk:r for r in rows}
            for index,decision in enumerate(decisions):
                if decision["order_id"] not in eligible:
                    continue
                row=by_id[decision["order_id"]]
                confirmation=evidence.get((row.profile_id,row.external_order_id))
                if confirmation is None:
                    decisions[index]={**decision,"action":"blocked","reason":"marketplace_order_missing"}
                    continue
                decisions[index]=reconcile_delivered_order(order_id=row.pk,confirmation={
                    **confirmation,"profile_id":row.profile_id,"source":"live_marketplace_read_reconciliation",
                })
        counts=Counter((d["action"],d["reason"]) for d in decisions)
        self.stdout.write(json.dumps({"apply":options["apply"],"orders":len(ids),
            "summary":[{"action":k[0],"reason":k[1],"orders":v} for k,v in counts.items()],
            "results":decisions},ensure_ascii=False))
