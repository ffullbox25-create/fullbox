from django.core.management.base import BaseCommand

from wms_new.services.sync import (
    sync_acceptances,
    sync_all,
    sync_boxes,
    sync_documents,
    sync_extra_fields,
    sync_inventories,
    sync_marking_codes,
    sync_logistics,
    sync_movements,
    sync_orders,
    sync_products,
    sync_returns,
    sync_shipments,
    sync_tasks,
    sync_waves,
)


class Command(BaseCommand):
    help = "Копирует актуальные данные Fullbox в изолированный пилотный контур WMS NEW."

    def add_arguments(self, parser):
        parser.add_argument(
            "--scope",
            choices=(
                "all",
                "orders",
                "waves",
                "shipments",
                "returns",
                "tasks",
                "products",
                "acceptances",
                "marking",
                "extra_fields",
                "inventories",
                "boxes",
                "movements",
                "logistics",
                "documents",
            ),
            default="all",
        )
        parser.add_argument("--limit", type=int)

    def handle(self, *args, **options):
        scope = options["scope"]
        limit = options.get("limit")
        if scope == "orders":
            result = sync_orders(limit=limit)
        elif scope == "waves":
            result = sync_waves(limit=limit)
        elif scope == "shipments":
            result = sync_shipments(limit=limit)
        elif scope == "returns":
            result = sync_returns(limit=limit)
        elif scope == "tasks":
            result = sync_tasks(limit=limit)
        elif scope == "products":
            result = sync_products(limit=limit)
        elif scope == "acceptances":
            result = sync_acceptances(limit=limit)
        elif scope == "marking":
            result = sync_marking_codes(limit=limit)
        elif scope == "extra_fields":
            result = sync_extra_fields(limit=limit)
        elif scope == "inventories":
            result = sync_inventories(limit=limit)
        elif scope == "boxes":
            result = sync_boxes(limit=limit)
        elif scope == "movements":
            result = sync_movements(limit=limit)
        elif scope == "logistics":
            result = sync_logistics(limit=limit)
        elif scope == "documents":
            result = sync_documents(limit=limit)
        elif limit:
            result = {
                "orders": sync_orders(limit=limit),
                "waves": sync_waves(limit=limit),
                "shipments": sync_shipments(limit=limit),
                "returns": sync_returns(limit=limit),
                "tasks": sync_tasks(limit=limit),
                "products": sync_products(limit=limit),
                "acceptances": sync_acceptances(limit=limit),
                "marking": sync_marking_codes(limit=limit),
                "extra_fields": sync_extra_fields(limit=limit),
                "inventories": sync_inventories(limit=limit),
                "boxes": sync_boxes(limit=limit),
                "movements": sync_movements(limit=limit),
                "logistics": sync_logistics(limit=limit),
                "documents": sync_documents(limit=limit),
            }
        else:
            result = sync_all()
        self.stdout.write(self.style.SUCCESS(str(result)))
