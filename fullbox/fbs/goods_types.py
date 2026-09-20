from __future__ import annotations

from collections import defaultdict
import json
from types import SimpleNamespace

from django.db import connections
from django.db.models import Q

from sklad.services.dispatchable_stock import (
    dispatchable_warehouse_state_q,
    is_dispatchable_warehouse_state,
)

FBS_READY_GOODS_TYPE_KEY = "готовый"
FBS_READY_GOODS_TYPE_LABEL = "Готовый"
FBS_READY_GOODS_TYPE_VALUES = ("gv", "готовый")
FBS_CLIENT_MOVEMENT_SOURCE_GOODS_TYPE_LABEL = "Готовый товар"
FBS_CLIENT_MOVEMENT_SOURCE_GOODS_TYPE_VALUES = FBS_READY_GOODS_TYPE_VALUES
FBS_CLIENT_MOVEMENT_SOURCE_ZONE_KIND = "storage"
FBS_CLIENT_MOVEMENT_SOURCE_WAREHOUSE_STATE = "stored"
# Completed processing is a ready source in its actual zone; no fictitious
# putaway is required. Never include in-progress processing or shipping zones.
FBS_CLIENT_MOVEMENT_AFTER_PROCESSING_STATE = "placed_after_processing"
FBS_CLIENT_MOVEMENT_AFTER_PROCESSING_ZONES = ("receiving", "processing", "storage")
FBS_CLIENT_MOVEMENT_SOURCE_CONTAINER_TYPE = "box"
FBS_CLIENT_MOVEMENT_SOURCE_CONTAINER_STATUS = "active"
FBS_CLIENT_MOVEMENT_SOURCE_CONTAINER_SUFFIX = "-gv"
FBS_CLIENT_MOVEMENT_SOURCE_CONTAINER_SUFFIXES = (
    FBS_CLIENT_MOVEMENT_SOURCE_CONTAINER_SUFFIX,
    "_vp",
)


def _permission_audit_rows(queryset, state_keys):
    """Extract a receiving audit document once, rather than once per JSON key.

    Receiving documents may contain large marking-code arrays. PostgreSQL's
    record expansion avoids repeatedly decompressing those arrays for every
    projected key. No permission results are cached across calls or writes.
    """
    connection = connections[queryset.db]
    if connection.vendor != "postgresql":
        return queryset.annotate(
            has_flow_state=Q(payload__has_key="flow_state"),
            has_flow_boxes=Q(payload__has_key="flow_boxes"),
        ).values(
            "agency_id", "order_id", "has_flow_state", "has_flow_boxes",
            "payload__flow_state__pallets", *("payload__" + key for key in state_keys),
        )
    # The source remains the same scoped/ordered ORM query. All identifiers
    # below are fixed internal names; values stay in the compiler's parameters.
    sql, params = queryset.values("agency_id", "order_id", "payload", "created_at", "id").query.get_compiler(
        using=queryset.db,
    ).as_sql()
    quote = connection.ops.quote_name
    record_keys = (*state_keys, "flow_state", "flow_boxes")
    columns = ", ".join(f"{quote(key)} jsonb" for key in record_keys)
    projected = ", ".join(f"p.{quote(key)}" for key in state_keys)
    names = ("agency_id", "order_id", "has_flow_state", "has_flow_boxes",
             "payload__flow_state__pallets", *("payload__" + key for key in state_keys))
    with connection.cursor() as cursor:
        cursor.execute(
            f"SELECT a.agency_id, a.order_id, "
            f"a.payload ? 'flow_state', a.payload ? 'flow_boxes', "
            f"p.flow_state -> 'pallets', {projected} "
            f"FROM ({sql}) a CROSS JOIN LATERAL "
            f"jsonb_to_record(CASE WHEN jsonb_typeof(a.payload) = 'object' "
            f"THEN a.payload ELSE '{{}}'::jsonb END) AS p({columns}) "
            f"ORDER BY a.created_at, a.id",
            params,
        )
        rows = cursor.fetchall()
    return [dict(zip(names, (*row[:4], *(
        json.loads(value) if isinstance(value, str) else value for value in row[4:]
    )))) for row in rows]


def receiving_placement_allowed_snapshot_ids(snapshots=None, *, agency_id=None) -> set[int]:
    """Use the existing per-pallet receiving permission, scoped to its owner.

    Read only materialized warehouse facts. Never turn a receiving draft into
    stock while displaying availability. Do not cache permission across writes.
    """
    from audit.models import OrderAuditEntry
    from orders.services import ReceivingWorkflowService
    from sklad.models import WarehouseStockSnapshot

    if snapshots is None:
        snapshots = WarehouseStockSnapshot.objects.filter(
            is_archived=False, qty__gt=0, source_context_type="receiving",
            zone_kind="receiving", zone_code="PR",
            warehouse_state_code="placed_in_receiving",
            container__container_type="box", container__status="active",
            container__container_code__iendswith=FBS_CLIENT_MOVEMENT_SOURCE_CONTAINER_SUFFIX,
        ).filter(fbs_ready_goods_type_q()).select_related("parent_container").only(
            "id", "agency_id", "source_context_id", "source_context_type",
            "zone_code", "warehouse_state_code", "parent_container__container_code",
        )
        if agency_id is not None:
            snapshots = snapshots.filter(agency_id=agency_id)
    grouped = defaultdict(list)
    for snapshot in snapshots:
        if (
            snapshot.source_context_type == "receiving"
            and snapshot.source_context_id
            and snapshot.zone_code == "PR"
            and snapshot.warehouse_state_code == "placed_in_receiving"
        ):
            grouped[(snapshot.agency_id, snapshot.source_context_id)].append(snapshot)
    if not grouped:
        return set()
    # Project permission/state fields, not the large box and marking-code lists
    # stored in receiving audit payloads. Batch all requested receiving orders.
    state_keys = ("status", "act", "act_state", "flow_closed", "flow_reopened",
                  "event", "pallet_code", "pallet_placement_allowed", "flow_pallets")
    entries_by_order = defaultdict(list)
    audit_rows = _permission_audit_rows(OrderAuditEntry.objects.filter(
        agency_id__in={key[0] for key in grouped}, order_type="receiving",
        order_id__in={key[1] for key in grouped},
    ).order_by("created_at", "id"), state_keys)
    for row in audit_rows:
        key = (row["agency_id"], row["order_id"])
        if key not in grouped:
            continue
        payload = {name: row["payload__" + name] for name in state_keys
                   if row["payload__" + name] is not None}
        if row["has_flow_state"]:
            payload["flow_state"] = {"pallets": row["payload__flow_state__pallets"] or []}
        elif row["has_flow_boxes"]:
            # Preserve an empty-pallet flow update without loading box contents.
            payload["flow_boxes"] = [True]
        entries_by_order[key].append(SimpleNamespace(payload=payload))
    allowed = set()
    for key, rows in grouped.items():
        entries = entries_by_order[key]
        if not entries:
            continue
        latest_status = next((
            str((entry.payload or {}).get("status") or "").casefold()
            for entry in reversed(entries) if (entry.payload or {}).get("status")
        ), "")
        if latest_status in {"cancelled", "canceled"}:
            continue
        flow_closed = ReceivingWorkflowService._flow_closed(entries)
        flow = ReceivingWorkflowService.find_receiving_flow_state(entries)
        pallets = {
            str(pallet.get("code") or "").strip().casefold(): pallet
            for pallet in flow.get("pallets", []) if isinstance(pallet, dict)
        }
        permissions = ReceivingWorkflowService._pallet_placement_permissions_from_entries(entries)
        for snapshot in rows:
            code = str(getattr(snapshot.parent_container, "container_code", "") or "").strip().casefold()
            pallet = pallets.get(code)
            # Closed legacy receiving may predate the pallet-flow draft.
            if not pallets and flow_closed:
                allowed.add(snapshot.pk)
            elif pallet and pallet.get("sealed") and (flow_closed or permissions.get(code, False)):
                allowed.add(snapshot.pk)
    return allowed


def is_fbs_ready_goods_type(value: str | None) -> bool:
    return str(value or "").strip().casefold() in FBS_READY_GOODS_TYPE_VALUES


def fbs_ready_goods_type_q(field_name: str = "goods_type") -> Q:
    query = Q()
    for value in FBS_READY_GOODS_TYPE_VALUES:
        query |= Q(**{f"{field_name}__iexact": value})
    return query


def is_fbs_client_movement_source_goods_type(value: str | None) -> bool:
    return (
        str(value or "").strip().casefold()
        in FBS_CLIENT_MOVEMENT_SOURCE_GOODS_TYPE_VALUES
    )


def fbs_client_movement_source_goods_type_q(field_name: str = "goods_type") -> Q:
    query = Q()
    for value in FBS_CLIENT_MOVEMENT_SOURCE_GOODS_TYPE_VALUES:
        query |= Q(**{f"{field_name}__iexact": value})
    return query


def is_fbs_client_movement_source_stock(
    *,
    goods_type: str | None,
    zone_kind: str | None,
    warehouse_state_code: str | None,
    container_type: str | None,
    container_code: str | None,
    container_status: str | None,
    snapshot=None,
    receiving_allowed_ids=None,
) -> bool:
    resolved_receiving_allowed_ids = receiving_allowed_ids
    if resolved_receiving_allowed_ids is None:
        state_code = str(warehouse_state_code or "").strip().casefold()
        # The receiving audit is relevant only to stock that is still in PR.
        # Stored/processed stock is decided by its materialized warehouse state.
        resolved_receiving_allowed_ids = (
            receiving_placement_allowed_snapshot_ids([snapshot])
            if state_code == "placed_in_receiving" and snapshot is not None
            else ()
        )
    return (
        is_fbs_client_movement_source_goods_type(goods_type)
        and is_dispatchable_warehouse_state(
            warehouse_state_code,
            snapshot_id=getattr(snapshot, "pk", None),
            receiving_allowed_ids=resolved_receiving_allowed_ids,
        )
        and str(container_type or "").strip().casefold()
        == FBS_CLIENT_MOVEMENT_SOURCE_CONTAINER_TYPE
        and str(container_status or "").strip().casefold()
        == FBS_CLIENT_MOVEMENT_SOURCE_CONTAINER_STATUS
        and str(container_code or "").strip().casefold().endswith(
            FBS_CLIENT_MOVEMENT_SOURCE_CONTAINER_SUFFIXES
        )
    )


def fbs_client_movement_source_stock_q(
    *,
    agency_id=None,
    goods_type_field: str = "goods_type",
    zone_kind_field: str = "zone_kind",
    warehouse_state_field: str = "warehouse_state_code",
    container_type_field: str = "container__container_type",
    container_code_field: str = "container__container_code",
    container_status_field: str = "container__status",
    snapshot_id_field: str = "pk",
) -> Q:
    ready_state = dispatchable_warehouse_state_q(
        state_field=warehouse_state_field,
        snapshot_id_field=snapshot_id_field,
        receiving_allowed_ids=receiving_placement_allowed_snapshot_ids(
            agency_id=agency_id
        ),
    )
    container_code_query = Q()
    for suffix in FBS_CLIENT_MOVEMENT_SOURCE_CONTAINER_SUFFIXES:
        container_code_query |= Q(
            **{f"{container_code_field}__iendswith": suffix}
        )
    return (
        fbs_client_movement_source_goods_type_q(goods_type_field)
        & ready_state
        & Q(
            **{
                f"{container_type_field}__iexact": (
                    FBS_CLIENT_MOVEMENT_SOURCE_CONTAINER_TYPE
                )
            }
        )
        & Q(
            **{
                f"{container_status_field}__iexact": (
                    FBS_CLIENT_MOVEMENT_SOURCE_CONTAINER_STATUS
                )
            }
        )
        & container_code_query
    )
