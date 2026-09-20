"""Production-safe FBS-NEW smoke: exercise writes and roll the transaction back."""

from __future__ import annotations

import os
import sys
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "fullbox.settings")

import django  # noqa: E402

django.setup()

from django.contrib.auth import get_user_model  # noqa: E402
from django.db import transaction  # noqa: E402
from django.utils import timezone  # noqa: E402

from fbs.models import FbsOrder, FbsStockBalance  # noqa: E402
from sklad.models import WarehouseLocation, WarehouseStockSnapshot  # noqa: E402
from todo.models import Task  # noqa: E402
from wms_new.models import (  # noqa: E402
    WmsNewBox,
    WmsNewDocument,
    WmsNewEvent,
    WmsNewLogisticsManifest,
    WmsNewLogisticsOrder,
    WmsNewLogisticsPackage,
    WmsNewMovement,
    WmsNewOrder,
    WmsNewOrderItem,
    WmsNewProduct,
    WmsNewReturn,
    WmsNewShipment,
    WmsNewTask,
    WmsNewTaskBox,
)
from wms_new.services.documents import (  # noqa: E402
    add_item as add_document_item,
    create_document,
    post_document,
)
from wms_new.services.logistics import (  # noqa: E402
    add_orders_to_package,
    create_manifest,
    create_package,
    import_fbs_shipments,
    measure_order,
    transition_manifest,
    update_packages,
)
from wms_new.services.returns import (  # noqa: E402
    complete_return,
    create_return,
    record_return_line,
    start_receiving,
)
from wms_new.services.shipments import (  # noqa: E402
    add_box as add_shipment_box,
    add_order as add_shipment_order,
    create_shipment,
    transition_shipment,
    verify_order,
)
from wms_new.services.tasks import (  # noqa: E402
    add_task_item,
    add_task_service,
    apply_board_action,
    assign_task_items_to_box,
    create_task,
    create_task_box,
    finalize_task_tariff,
    record_task_item,
    save_task_details,
    set_task_box_status,
    set_task_client_confirmation,
)


class SmokeRollback(Exception):
    pass


def counts() -> dict[str, int]:
    return {
        "legacy_fbs_orders": FbsOrder.objects.count(),
        "legacy_tasks": Task.objects.count(),
        "legacy_stock_snapshots": WarehouseStockSnapshot.objects.count(),
        "legacy_fbs_balances": FbsStockBalance.objects.count(),
        "new_orders": WmsNewOrder.objects.count(),
        "new_tasks": WmsNewTask.objects.count(),
        "new_shipments": WmsNewShipment.objects.count(),
        "new_returns": WmsNewReturn.objects.count(),
        "new_logistics_orders": WmsNewLogisticsOrder.objects.count(),
        "new_logistics_packages": WmsNewLogisticsPackage.objects.count(),
        "new_logistics_manifests": WmsNewLogisticsManifest.objects.count(),
        "new_documents": WmsNewDocument.objects.count(),
        "new_boxes": WmsNewBox.objects.count(),
        "new_movements": WmsNewMovement.objects.count(),
        "new_events": WmsNewEvent.objects.count(),
    }


def run() -> None:
    actor = get_user_model().objects.get(username="dev")
    product = (
        WmsNewProduct.objects.select_related("agency")
        .filter(source_sku_id__isnull=False, is_archived=False, marking_required=False)
        .order_by("id")
        .first()
    )
    location = WarehouseLocation.objects.filter(is_active=True, is_storage=True).order_by("id").first()
    if product is None or location is None:
        raise RuntimeError("Для smoke нужны синхронизированный товар и активное место хранения.")
    before = counts()
    token = f"ROLLBACK-{timezone.now():%Y%m%d%H%M%S}"
    try:
        with transaction.atomic():
            task = create_task(
                agency=product.agency,
                workflow_type=WmsNewTask.TYPE_PROCESSING,
                title=f"{token}-TASK",
                due_date=timezone.localdate(),
                actor=actor,
            )
            save_task_details(
                task_id=task.id,
                delivery_address="Rollback smoke",
                contact_name="Smoke",
                actor=actor,
            )
            task_item = add_task_item(
                task_id=task.id,
                product_id=product.id,
                planned_qty=1,
                unit_price="1.00",
                actor=actor,
            )
            task_box = create_task_box(task_id=task.id, code=f"{token}-TASK-BOX", actor=actor)
            assign_task_items_to_box(
                task_id=task.id,
                box_id=task_box.id,
                item_ids=[task_item.id],
                actor=actor,
            )
            record_task_item(
                task_id=task.id,
                item_id=task_item.id,
                processed_qty=1,
                actor=actor,
            )
            set_task_box_status(
                task_id=task.id,
                box_id=task_box.id,
                status=WmsNewTaskBox.STATUS_CLOSED,
                actor=actor,
            )
            add_task_service(
                task_id=task.id,
                item_id=task_item.id,
                name="Smoke service",
                unit_price="2.50",
                quantity=1,
                actor=actor,
            )
            finalize_task_tariff(task_id=task.id, actor=actor)
            set_task_client_confirmation(task_id=task.id, confirmed=True, actor=actor)
            apply_board_action(task_id=task.id, action="advance", actor=actor)

            order = WmsNewOrder.objects.create(
                agency=product.agency,
                external_order_id=f"{token}-ORDER",
                tracking_number=f"{token}-TRACK",
                delivery_type="Wildberries FBS",
                status=WmsNewOrder.STATUS_READY,
                availability_state="ready",
                is_manual=True,
                pilot_revision=1,
                source_snapshot={"in_supply": True, "supply_status": "in_supply"},
            )
            WmsNewOrderItem.objects.create(
                order=order,
                sku_id=product.source_sku_id,
                external_line_id=f"{token}-LINE",
                external_sku=product.article,
                barcode=product.barcode,
                product_name=product.name,
                quantity=1,
            )
            shipment = create_shipment(
                agency=product.agency,
                delivery_type="Wildberries FBS",
                integration_name="Smoke",
                actor=actor,
            )
            shipment_link = add_shipment_order(
                shipment_id=shipment.id,
                order_id=order.id,
                actor=actor,
            )
            shipment_box = add_shipment_box(
                shipment_id=shipment.id,
                qr_code=f"{token}-SHIP-BOX",
                actor=actor,
            )
            verify_order(
                shipment_id=shipment.id,
                shipment_order_id=shipment_link.id,
                box_id=shipment_box.id,
                in_supply=True,
                actor=actor,
            )
            transition_shipment(shipment_id=shipment.id, action="finish_check", actor=actor)
            transition_shipment(shipment_id=shipment.id, action="send", actor=actor)

            return_request = create_return(scan=order.external_order_id, reason="Smoke", actor=actor)
            start_receiving(
                return_id=return_request.id,
                location_id=location.id,
                box_code=f"{token}-RETURN-BOX",
                actor=actor,
            )
            return_line = return_request.lines.get()
            record_return_line(
                return_id=return_request.id,
                line_id=return_line.id,
                received_qty=1,
                accepted_qty=1,
                condition="good",
                actor=actor,
            )
            complete_return(return_id=return_request.id, actor=actor)

            logistics_order = import_fbs_shipments(
                shipment_ids=[shipment.id], actor=actor
            )[0]
            measure_order(
                order_id=logistics_order.id,
                weight_g=100,
                width_mm=100,
                height_mm=100,
                depth_mm=100,
                actor=actor,
            )
            package = create_package(
                carrier_code=WmsNewLogisticsPackage.CARRIER_GBS,
                delivery_service="Wildberries FBS",
                warehouse_code="MSK",
                actor=actor,
            )
            add_orders_to_package(
                package_id=package.id,
                order_ids=[logistics_order.id],
                actor=actor,
            )
            manifest = create_manifest(name=f"{token}-TRIP", actor=actor)
            update_packages(
                package_ids=[package.id],
                action="manifest",
                manifest_id=manifest.id,
                actor=actor,
            )
            transition_manifest(manifest_id=manifest.id, action="send", actor=actor)
            transition_manifest(manifest_id=manifest.id, action="complete", actor=actor)

            document = create_document(
                document_type=WmsNewDocument.TYPE_RECEIPT,
                agency_id=product.agency_id,
                actor=actor,
            )
            add_document_item(
                document_id=document.id,
                product_id=product.id,
                quantity=1,
                location_id=location.id,
                actor=actor,
            )
            post_document(document_id=document.id, actor=actor)

            print(
                {
                    "token": token,
                    "task": task.id,
                    "shipment": shipment.id,
                    "return": return_request.id,
                    "package": package.id,
                    "manifest": manifest.id,
                    "document": document.id,
                    "status": "complete_before_rollback",
                }
            )
            raise SmokeRollback()
    except SmokeRollback:
        pass
    after = counts()
    print({"before": before})
    print({"after": after})
    if before != after:
        raise RuntimeError({"rollback_count_mismatch": {key: (before[key], after[key]) for key in before if before[key] != after[key]}})
    if WmsNewOrder.objects.filter(external_order_id__startswith="ROLLBACK-").exists():
        raise RuntimeError("После rollback остался синтетический заказ.")
    print("ROLLBACK_SMOKE_OK")


if __name__ == "__main__":
    run()
