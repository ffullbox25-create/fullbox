from __future__ import annotations

from django.db.models import Q

from .codes import marking_code_identity, marking_code_variants
from .models import MarkingCode


def _processing_flow_marking_conflict(identity: str) -> dict:
    from processing_app.models import ProcessingFlowSession

    sessions = ProcessingFlowSession.objects.only("id", "order_id", "flow_state").order_by("id")
    for session in sessions.iterator(chunk_size=500):
        state = session.flow_state if isinstance(session.flow_state, dict) else {}
        for box in state.get("boxes") or []:
            if not isinstance(box, dict):
                continue
            box_code = str(box.get("code") or "").strip()
            for item in box.get("items") or []:
                if not isinstance(item, dict):
                    continue
                raw_codes = item.get("marking_codes") or []
                if not isinstance(raw_codes, list):
                    raw_codes = [raw_codes]
                if item.get("marking_code"):
                    raw_codes = [*raw_codes, item.get("marking_code")]
                for raw_code in raw_codes:
                    if isinstance(raw_code, dict):
                        raw_code = raw_code.get("code")
                    if marking_code_identity(raw_code or "") == identity:
                        return {
                            "process": "processing_flow",
                            "process_label": "Обработка",
                            "order_id": session.order_id,
                            "box_code": box_code,
                            "sku_code": str(
                                item.get("sku_code") or item.get("sku") or ""
                            ).strip(),
                            "size": str(item.get("size") or "").strip(),
                        }
    return {}


def _first_marking_identity_match(
    queryset,
    *,
    field_name: str,
    identity: str,
    variants: set[str],
):
    lookup = Q(**{f"{field_name}__in": variants})
    if identity.startswith("01"):
        lookup |= Q(**{f"{field_name}__startswith": identity})
        lookup |= Q(**{f"{field_name}__startswith": f"]d2{identity}"})
        lookup |= Q(**{f"{field_name}__startswith": f"]D2{identity}"})
    for row in queryset.filter(lookup).order_by("id").iterator(chunk_size=200):
        if marking_code_identity(getattr(row, field_name, "") or "") == identity:
            return row
    return None


def _chunks(values, size: int):
    values = list(values)
    for index in range(0, len(values), size):
        yield values[index : index + size]


def _matching_storage_identities(
    queryset,
    *,
    field_name: str,
    identities: set[str],
    variants: set[str],
) -> set[str]:
    matched = set()

    def collect(query):
        values = queryset.filter(query).values_list(field_name, flat=True)
        for value in values.iterator(chunk_size=500):
            identity = marking_code_identity(value or "")
            if identity in identities:
                matched.add(identity)

    gs1_identities = {
        identity
        for identity in identities
        if identity.startswith("01")
        and len(identity) >= 18
        and identity[16:18] == "21"
    }
    non_gs1_variants = {
        variant
        for variant in variants
        if marking_code_identity(variant) not in gs1_identities
    }
    for variant_chunk in _chunks(non_gs1_variants, 400):
        collect(Q(**{f"{field_name}__in": variant_chunk}))

    # A file can contain hundreds of serials for one GTIN. Querying every
    # serialized identity as three OR-ed prefixes makes PostgreSQL repeatedly
    # scan the legacy storage tables and can exceed the web-worker timeout.
    # All equivalent GS1 representations share the stable 01+GTIN+21 prefix,
    # so fetch each product family once and keep the exact identity comparison
    # in Python below.
    product_prefixes = {identity[:18] for identity in gs1_identities}
    for prefix_chunk in _chunks(product_prefixes, 60):
        prefix_query = Q()
        for prefix in prefix_chunk:
            prefix_query |= Q(**{f"{field_name}__startswith": prefix})
            prefix_query |= Q(**{f"{field_name}__startswith": f"]d2{prefix}"})
            prefix_query |= Q(**{f"{field_name}__startswith": f"]D2{prefix}"})
        collect(prefix_query)
    return matched


def occupied_marking_identities(identities: set[str], variants: set[str]) -> set[str]:
    """Return physical CHZ identities already present in any active system contour."""
    identities = {identity for identity in identities if identity}
    variants = {variant for variant in variants if variant}
    occupied = set()
    for identity_chunk in _chunks(identities, 400):
        rows = MarkingCode.objects.filter(identity_key__in=identity_chunk).values_list(
            "identity_key", flat=True
        )
        occupied.update(identity for identity in rows if identity in identities)
    for variant_chunk in _chunks(variants, 400):
        rows = MarkingCode.objects.filter(code__in=variant_chunk).values_list("code", flat=True)
        occupied.update(
            identity
            for identity in (marking_code_identity(code) for code in rows)
            if identity in identities
        )

    from fbs.models import FbsStockBalance
    from processing_cz.models import ProcessingCzUnit
    from receiving_cz.models import ReceivingCzUnit
    from sklad.models import WarehouseStockSnapshot
    from wms_new.models import WmsNewBoxItem

    sources = (
        (ProcessingCzUnit.objects.all(), "marking_code"),
        (ReceivingCzUnit.objects.all(), "marking_code"),
        (WarehouseStockSnapshot.objects.filter(is_archived=False, qty__gt=0), "marking_code"),
        (FbsStockBalance.objects.filter(qty__gt=0), "marking_code"),
        (WmsNewBoxItem.objects.filter(qty__gt=0), "marking_code"),
    )
    for queryset, field_name in sources:
        occupied.update(
            _matching_storage_identities(
                queryset,
                field_name=field_name,
                identities=identities,
                variants=variants,
            )
        )

    from processing_app.models import ProcessingFlowSession

    for session in ProcessingFlowSession.objects.only("flow_state").iterator(chunk_size=300):
        state = session.flow_state if isinstance(session.flow_state, dict) else {}
        for box in state.get("boxes") or []:
            if not isinstance(box, dict):
                continue
            for item in box.get("items") or []:
                if not isinstance(item, dict):
                    continue
                raw_codes = item.get("marking_codes") or []
                if not isinstance(raw_codes, list):
                    raw_codes = [raw_codes]
                if item.get("marking_code"):
                    raw_codes = [*raw_codes, item.get("marking_code")]
                for raw_code in raw_codes:
                    if isinstance(raw_code, dict):
                        raw_code = raw_code.get("code")
                    identity = marking_code_identity(raw_code or "")
                    if identity in identities:
                        occupied.add(identity)
    return occupied


def printed_marking_storage_conflict(marking: MarkingCode) -> dict:
    identity = marking.identity_key or marking_code_identity(marking.code)
    variants = marking_code_variants(marking.code)
    if marking.used_at or marking.box_barcode:
        return {
            "process": marking.order_type or "marking",
            "process_label": (
                "Обработка" if marking.order_type == "processing" else "Учет ЧЗ"
            ),
            "order_id": marking.order_id,
            "box_code": marking.box_barcode,
            "sku_code": marking.sku_code,
            "size": marking.size,
            "used_at": marking.used_at.isoformat() if marking.used_at else "",
            "used_by": str(marking.used_by or ""),
        }

    from processing_cz.models import ProcessingCzUnit
    from receiving_cz.models import ReceivingCzUnit

    processing_unit = _first_marking_identity_match(
        ProcessingCzUnit.objects.select_related("accepted_by"),
        field_name="marking_code",
        identity=identity,
        variants=variants,
    )
    if processing_unit:
        return {
            "process": "processing",
            "process_label": "Обработка",
            "order_id": processing_unit.order_id,
            "box_code": processing_unit.box_code,
            "pallet_code": processing_unit.pallet_code,
            "sku_code": processing_unit.sku_code,
            "size": processing_unit.size,
            "used_at": processing_unit.accepted_at.isoformat(),
            "used_by": str(processing_unit.accepted_by or ""),
        }

    receiving_unit = _first_marking_identity_match(
        ReceivingCzUnit.objects.select_related("accepted_by"),
        field_name="marking_code",
        identity=identity,
        variants=variants,
    )
    if receiving_unit:
        return {
            "process": "receiving",
            "process_label": "Приемка",
            "order_id": receiving_unit.order_id,
            "box_code": receiving_unit.box_code,
            "pallet_code": receiving_unit.pallet_code,
            "sku_code": receiving_unit.sku_code,
            "size": receiving_unit.size,
            "used_at": receiving_unit.accepted_at.isoformat(),
            "used_by": str(receiving_unit.accepted_by or ""),
        }

    flow_conflict = _processing_flow_marking_conflict(identity)
    if flow_conflict:
        return flow_conflict

    from sklad.models import WarehouseStockSnapshot

    snapshot = _first_marking_identity_match(
        WarehouseStockSnapshot.objects.select_related(
            "container", "parent_container", "location"
        ).filter(is_archived=False, qty__gt=0),
        field_name="marking_code",
        identity=identity,
        variants=variants,
    )
    if snapshot:
        box_code = str(
            getattr(snapshot.container, "container_code", "")
            or snapshot.container_code
            or ""
        ).strip()
        pallet_code = str(
            getattr(snapshot.parent_container, "container_code", "") or ""
        ).strip()
        return {
            "process": "warehouse",
            "process_label": "Склад",
            "order_id": snapshot.source_context_id,
            "box_code": box_code,
            "pallet_code": pallet_code,
            "sku_code": snapshot.sku_code,
            "size": snapshot.size,
            "location": str(
                getattr(snapshot.location, "display_name", "") or snapshot.zone_code or ""
            ).strip(),
        }

    from fbs.models import FbsStockBalance

    fbs_balance = _first_marking_identity_match(
        FbsStockBalance.objects.select_related("box").filter(qty__gt=0),
        field_name="marking_code",
        identity=identity,
        variants=variants,
    )
    if fbs_balance:
        return {
            "process": "fbs",
            "process_label": "FBS",
            "order_id": "",
            "box_code": fbs_balance.box.box_code,
            "sku_code": fbs_balance.sku_code,
            "size": fbs_balance.size,
        }

    from wms_new.models import WmsNewBoxItem

    wms_item = _first_marking_identity_match(
        WmsNewBoxItem.objects.select_related("box").filter(qty__gt=0),
        field_name="marking_code",
        identity=identity,
        variants=variants,
    )
    if wms_item:
        return {
            "process": "wms_new",
            "process_label": "WMS",
            "order_id": "",
            "box_code": wms_item.box.code,
            "sku_code": wms_item.sku_code,
            "size": wms_item.size,
        }
    return {}


def marking_conflict_message(conflict: dict) -> str:
    process = str(conflict.get("process_label") or "учете").strip()
    order_id = str(conflict.get("order_id") or "").strip()
    box_code = str(conflict.get("box_code") or "").strip()
    pallet_code = str(conflict.get("pallet_code") or "").strip()
    parts = [f"Возврат запрещён: код ЧЗ уже находится в контуре «{process}»"]
    if order_id:
        parts.append(f"заявка {order_id}")
    if box_code:
        parts.append(f"короб {box_code}")
    if pallet_code:
        parts.append(f"паллета {pallet_code}")
    return ", ".join(parts) + ". Сначала освободите код из этого места."
