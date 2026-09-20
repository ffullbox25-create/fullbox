from django.db.models import CharField, Count, F, IntegerField, OuterRef, Q, Subquery, Sum, Value
from django.db.models.functions import Coalesce

from audit.models import OrderAuditEntry
from marking.models import MarkingCode
from shipping.models import ShippingOrderItem
from sklad.models import WarehouseEvent, WarehouseStockSnapshot
from sklad.services.warehouse_transitions import WarehouseStateCode
from sku.models import SKU, SKUBarcode, SKUPhoto

from .models import GoodsProfile


_TERMINAL_STOCK_STATES = {
    WarehouseStateCode.PROCESSING_CONSUMED.value,
    WarehouseStateCode.SHIPPED.value,
    WarehouseStateCode.CANCELED.value,
}
_RECEIVING_STOCK_STATES = {
    WarehouseStateCode.RECEIVED_UNPLACED.value,
    WarehouseStateCode.PLACED_IN_RECEIVING.value,
}
_PROCESSING_STOCK_STATES = {
    WarehouseStateCode.MOVING_TO_PROCESSING.value,
    WarehouseStateCode.IN_PROCESSING_ZONE.value,
    WarehouseStateCode.PROCESSING_IN_PROGRESS.value,
}
_SHIPPING_STOCK_STATES = {
    WarehouseStateCode.MOVING_TO_OTG.value,
    WarehouseStateCode.IN_OTG.value,
    WarehouseStateCode.PALLETIZING.value,
    WarehouseStateCode.READY_FOR_LOADING.value,
    WarehouseStateCode.ASSIGNED_TO_TRIP.value,
    WarehouseStateCode.LOADING_IN_PROGRESS.value,
}


def stock_q_for_sku(sku: SKU) -> Q:
    linked = Q(sku_ref_id=sku.pk)
    fallback = Q(sku_ref_id__isnull=True, agency_id=sku.agency_id, sku_code=sku.sku_code)
    return linked | fallback


def stock_for_sku(sku: SKU):
    return WarehouseStockSnapshot.objects.filter(
        stock_q_for_sku(sku), is_archived=False, qty__gt=0
    ).exclude(warehouse_state_code__in=_TERMINAL_STOCK_STATES)


def _stock_metric_subquery(field: str):
    rows = (
        WarehouseStockSnapshot.objects.filter(is_archived=False, qty__gt=0)
        .exclude(warehouse_state_code__in=_TERMINAL_STOCK_STATES)
        .filter(
            Q(sku_ref_id=OuterRef("pk"))
            | Q(
                sku_ref_id__isnull=True,
                agency_id=OuterRef("agency_id"),
                sku_code=OuterRef("sku_code"),
            )
        )
        .annotate(group_key=Value(1))
        .values("group_key")
        .annotate(metric=Sum(field))
        .values("metric")[:1]
    )
    return Coalesce(Subquery(rows, output_field=IntegerField()), Value(0))


def goods_list_queryset(*, search="", agency_id=None, stock_state="", sort="name", category="", reserves_only=False):
    primary_barcode = SKUBarcode.objects.filter(sku_id=OuterRef("pk")).order_by(
        "-is_primary", "value"
    )
    primary_photo = SKUPhoto.objects.filter(sku_id=OuterRef("pk")).order_by("sort_order", "id")
    profiles = GoodsProfile.objects.filter(sku_id=OuterRef("pk"))
    marking = (
        MarkingCode.objects.filter(sku_id=OuterRef("pk"))
        .values("sku_id")
        .annotate(total=Count("pk"))
        .values("total")[:1]
    )
    qs = (
        SKU.objects.filter(deleted=False)
        .select_related("agency", "market", "color_ref")
        .annotate(
            primary_barcode=Subquery(primary_barcode.values("value")[:1], output_field=CharField()),
            primary_photo=Subquery(primary_photo.values("url")[:1], output_field=CharField()),
            stock_total=_stock_metric_subquery("qty"),
            stock_available=_stock_metric_subquery("available_qty"),
            stock_processing=_stock_metric_subquery("processing_reserved_qty"),
            stock_shipping=_stock_metric_subquery("shipping_reserved_qty"),
            stock_other=_stock_metric_subquery("other_reserved_qty"),
            marking_count=Coalesce(Subquery(marking, output_field=IntegerField()), Value(0)),
            internal_notes=Coalesce(
                Subquery(profiles.values("internal_notes")[:1], output_field=CharField()),
                Value(""),
            ),
        )
    )
    search = str(search or "").strip()
    if search:
        matched_barcode = SKUBarcode.objects.filter(
            sku_id=OuterRef("pk"), value__icontains=search
        ).order_by("value")
        qs = qs.annotate(
            matched_barcode=Subquery(
                matched_barcode.values("value")[:1], output_field=CharField()
            ),
        ).annotate(
            display_barcode=Coalesce(F("matched_barcode"), F("primary_barcode"), Value("")),
        )
        text_filter = (
            Q(name__icontains=search)
            | Q(sku_code__icontains=search)
            | Q(code__icontains=search)
            | Q(barcodes__value__icontains=search)
            | Q(agency__agn_name__icontains=search)
        )
        if search.isdigit():
            text_filter |= Q(pk=int(search))
        qs = qs.filter(text_filter).distinct()
    else:
        qs = qs.annotate(
            matched_barcode=Value("", output_field=CharField()),
            display_barcode=Coalesce(F("primary_barcode"), Value("")),
        )
    if agency_id:
        qs = qs.filter(agency_id=agency_id)
    if category:
        qs = qs.filter(tovar_category=category)
    if stock_state == "positive":
        qs = qs.filter(stock_total__gt=0)
    elif stock_state == "zero":
        qs = qs.filter(stock_total=0)
    if reserves_only:
        qs = qs.filter(Q(stock_processing__gt=0) | Q(stock_shipping__gt=0) | Q(stock_other__gt=0))
    sort_map = {
        "name": ("name", "sku_code"),
        "-name": ("-name", "sku_code"),
        "sku": ("sku_code", "name"),
        "-sku": ("-sku_code", "name"),
        "stock": ("stock_total", "name"),
        "-stock": ("-stock_total", "name"),
        "updated": ("updated_at", "name"),
        "-updated": ("-updated_at", "name"),
    }
    return qs.order_by(*sort_map.get(sort, sort_map["name"]))


def stock_totals(sku: SKU) -> dict:
    groups = stock_for_sku(sku).values(
        "zone_code",
        "warehouse_state_code",
        "is_in_vehicle",
    ).annotate(
        qty=Sum("qty"),
        available_qty=Sum("available_qty"),
        processing_reserved_qty=Sum("processing_reserved_qty"),
        shipping_reserved_qty=Sum("shipping_reserved_qty"),
        other_reserved_qty=Sum("other_reserved_qty"),
    )

    totals = {
        "total": 0,
        "available": 0,
        "receiving": 0,
        "processing_zone": 0,
        "shipping_zone": 0,
        "loaded": 0,
        "processing_reserved": 0,
        "shipping_reserved": 0,
        "other_reserved": 0,
        "unclassified": 0,
        # Старые имена оставлены для обратной совместимости внешнего кода.
        "processing": 0,
        "shipping": 0,
        "other": 0,
    }
    for group in groups:
        qty = max(int(group.get("qty") or 0), 0)
        if qty <= 0:
            continue
        zone_code = str(group.get("zone_code") or "").strip().upper()
        state_code = str(group.get("warehouse_state_code") or "").strip().lower()
        totals["total"] += qty
        totals["processing"] += max(int(group.get("processing_reserved_qty") or 0), 0)
        totals["shipping"] += max(int(group.get("shipping_reserved_qty") or 0), 0)
        totals["other"] += max(int(group.get("other_reserved_qty") or 0), 0)

        if group.get("is_in_vehicle") or state_code == WarehouseStateCode.LOADED_TO_VEHICLE.value:
            totals["loaded"] += qty
            continue
        if zone_code == "OTG" or state_code in _SHIPPING_STOCK_STATES:
            totals["shipping_zone"] += qty
            continue
        if zone_code == "OBR" or state_code in _PROCESSING_STOCK_STATES:
            totals["processing_zone"] += qty
            continue
        if zone_code == "PR" or state_code in _RECEIVING_STOCK_STATES:
            totals["receiving"] += qty
            continue

        remaining = qty
        for source_field, target_field in (
            ("available_qty", "available"),
            ("processing_reserved_qty", "processing_reserved"),
            ("shipping_reserved_qty", "shipping_reserved"),
            ("other_reserved_qty", "other_reserved"),
        ):
            value = min(max(int(group.get(source_field) or 0), 0), remaining)
            totals[target_field] += value
            remaining -= value
        totals["unclassified"] += remaining

    component_specs = (
        ("available", "Свободно", "stock-good"),
        ("receiving", "На приёмке", ""),
        ("processing_zone", "В обработке", ""),
        ("shipping_zone", "В отгрузке", ""),
        ("loaded", "Погружено", ""),
        ("processing_reserved", "Резерв обработки", ""),
        ("shipping_reserved", "Резерв отгрузки", ""),
        ("other_reserved", "Другой резерв", ""),
        ("unclassified", "Прочее", ""),
    )
    totals["components"] = [
        {"key": key, "label": label, "value": totals[key], "css_class": css_class}
        for key, label, css_class in component_specs
    ]
    totals["explained_total"] = sum(item["value"] for item in totals["components"])
    totals["balanced"] = totals["explained_total"] == totals["total"]
    totals["equation"] = " + ".join(str(item["value"]) for item in totals["components"])
    return totals


def location_rows(sku: SKU):
    return (
        stock_for_sku(sku)
        .values(
            "location_id",
            "location__location_code",
            "location__display_name",
            "location__zone_kind",
            "zone_code",
            "container_code",
            "parent_container__container_code",
            "goods_type",
        )
        .annotate(
            qty=Sum("qty"),
            available=Sum("available_qty"),
            reserved=Sum("processing_reserved_qty") + Sum("shipping_reserved_qty") + Sum("other_reserved_qty"),
        )
        .order_by("zone_code", "location__location_code", "container_code")
    )


def movement_rows(sku: SKU, limit=250, *, barcode: str = ""):
    barcodes = list(sku.barcodes.values_list("value", flat=True))
    selected_barcode = str(barcode or "").strip()
    if selected_barcode and selected_barcode not in barcodes:
        return WarehouseEvent.objects.none()

    identity_filter = (
        Q(reserve__sku_ref_id=sku.pk)
        | Q(reserve__sku_code=sku.sku_code)
        | Q(payload__sku_code=sku.sku_code)
        | Q(payload__sku=sku.sku_code)
        | Q(payload__article=sku.sku_code)
        | Q(payload__barcode__in=barcodes)
    )
    if selected_barcode and len(barcodes) > 1:
        # For a SKU with several barcodes, show only events that can be
        # attributed to the selected barcode without ambiguity.
        identity_filter = (
            Q(reserve__barcode=selected_barcode)
            | Q(payload__barcode=selected_barcode)
            | Q(payload__bar_code=selected_barcode)
        )
    events = (
        WarehouseEvent.objects.filter(agency_id=sku.agency_id)
        .filter(identity_filter)
        .select_related("from_location", "to_location", "performed_by")
        .order_by("-occurred_at", "-id")
    )
    return events[:limit]


def order_rows(sku: SKU):
    shipping = (
        ShippingOrderItem.objects.filter(Q(sku_id=sku.pk) | Q(sku_code=sku.sku_code))
        .select_related("order")
        .order_by("-order__created_at", "-id")[:100]
    )
    receiving_entries = (
        OrderAuditEntry.objects.filter(agency_id=sku.agency_id, order_type="receiving")
        .order_by("-created_at", "-id")[:500]
    )
    seen = set()
    receiving = []
    needle = sku.sku_code.strip().lower()
    barcode_values = {value.lower() for value in sku.barcodes.values_list("value", flat=True)}
    for entry in receiving_entries:
        if entry.order_id in seen:
            continue
        payload = entry.payload if isinstance(entry.payload, dict) else {}
        rows = []
        for key in ("items", "planned_items", "act_items"):
            value = payload.get(key)
            if isinstance(value, list):
                rows.extend(item for item in value if isinstance(item, dict))
        matched = []
        for item in rows:
            article = str(item.get("sku_code") or item.get("sku") or item.get("article") or "").strip().lower()
            barcode = str(item.get("barcode") or item.get("bar_code") or "").strip().lower()
            if article == needle or (barcode and barcode in barcode_values):
                matched.append(item)
        if matched:
            seen.add(entry.order_id)
            receiving.append({"entry": entry, "items": matched})
            if len(receiving) >= 50:
                break
    return {"shipping": shipping, "receiving": receiving}


def marking_rows(sku: SKU):
    return MarkingCode.objects.filter(
        Q(sku_id=sku.pk) | Q(sku_id__isnull=True, agency_id=sku.agency_id, sku_code=sku.sku_code)
    ).select_related("created_by", "printed_by", "used_by")
