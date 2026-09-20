from __future__ import annotations

from django.db import models
from django.http import HttpResponseRedirect
from django.shortcuts import get_object_or_404
from django.urls import reverse
from urllib.parse import urlencode

from audit.models import log_sku_change
from employees.access import get_request_role, resolve_cabinet_url
from labels.utils import load_label_settings
from sklad.models import WarehouseTemporaryNomenclature
from sklad.services.temporary_nomenclature import temporary_nomenclature_enabled

from .models import Agency, MarketplaceBinding, SKU


VIEW_MODES = ("table", "cards")
CATALOG_MODES = ("sku", "temporary")
SORT_FIELDS = {
    "sku_code": "sku_code",
    "name": "name",
    "brand": "brand",
    "agency": "agency__agn_name",
    "market": "market__name",
    "color": "color",
    "color_ref": "color_ref__name",
    "size": "size",
    "name_print": "name_print",
    "code": "code",
    "gender": "gender",
    "season": "season",
    "additional_name": "additional_name",
    "composition": "composition",
    "made_in": "made_in",
    "cr_product_date": "cr_product_date",
    "end_product_date": "end_product_date",
    "sign_akciz": "sign_akciz",
    "tovar_category": "tovar_category",
    "use_nds": "use_nds",
    "vid_tovar": "vid_tovar",
    "type_tovar": "type_tovar",
    "stor_unit": "stor_unit__stor_name",
    "weight_kg": "weight_kg",
    "weight_net_kg": "weight_net_kg",
    "weight_gross_kg": "weight_gross_kg",
    "volume": "volume",
    "length_mm": "length_mm",
    "honest_sign": "honest_sign",
    "source": "source",
    "source_reference": "source_reference",
    "created_at": "created_at",
    "updated_at": "updated_at",
}
FILTER_FIELDS = {
    "sku_code": "sku_code",
    "name": "name",
    "brand": "brand",
    "agency": "agency__agn_name",
    "market": "market__name",
    "color": "color",
    "color_ref": "color_ref__name",
    "size": "size",
    "name_print": "name_print",
    "code": "code",
    "gender": "gender",
    "season": "season",
    "additional_name": "additional_name",
    "composition": "composition",
    "made_in": "made_in",
    "tovar_category": "tovar_category",
    "vid_tovar": "vid_tovar",
    "type_tovar": "type_tovar",
    "stor_unit": "stor_unit__stor_name",
    "source_reference": "source_reference",
}
DEFAULT_SORT = "sku_code"
TEMP_SORT_FIELDS = {
    "item_code": "item_code",
    "name": "name",
    "agency": "agency__agn_name",
    "size": "size",
    "barcode": "barcode",
    "normalized_sku_ref": "normalized_sku_ref__sku_code",
    "created_at": "created_at",
    "updated_at": "updated_at",
}
DEFAULT_TEMP_SORT = "updated_at"


def get_catalog_mode(request) -> str:
    catalog_mode = (request.GET.get("catalog") or "").strip().lower()
    if catalog_mode not in CATALOG_MODES:
        return "sku"
    if catalog_mode == "temporary" and not temporary_nomenclature_enabled():
        return "sku"
    return catalog_mode


def normalize_size(value):
    return (value or "").strip()


def build_size_ui(item):
    barcodes = list(item.barcodes.all())
    primary_display_value = (item.code or "").strip()
    display_barcode_pk = None
    if primary_display_value:
        matching_barcode = next((barcode for barcode in barcodes if barcode.value == primary_display_value), None)
        if matching_barcode is not None:
            display_barcode_pk = matching_barcode.pk
        else:
            primary_barcode = next((barcode for barcode in barcodes if barcode.is_primary), None)
            if primary_barcode is not None:
                display_barcode_pk = primary_barcode.pk
            elif barcodes:
                display_barcode_pk = barcodes[0].pk

    barcode_sizes = []
    seen_sizes = set()
    for barcode in barcodes:
        normalized_size = normalize_size(barcode.size)
        if normalized_size and normalized_size not in seen_sizes:
            barcode_sizes.append(normalized_size)
            seen_sizes.add(normalized_size)

    if barcode_sizes:
        size_options = barcode_sizes
    else:
        sku_size = normalize_size(item.size)
        size_options = [sku_size] if sku_size else []

    preferred_size = normalize_size(item.size)
    if preferred_size and preferred_size in size_options:
        selected_size = preferred_size
    elif size_options:
        selected_size = size_options[0]
    else:
        selected_size = ""

    has_sized_barcodes = bool(barcode_sizes)
    barcode_rows = []
    for barcode in barcodes:
        barcode_size = normalize_size(barcode.size)
        is_visible = True
        if has_sized_barcodes:
            is_visible = barcode_size == selected_size
        barcode_value = barcode.value
        is_primary = barcode.is_primary
        if primary_display_value and display_barcode_pk == barcode.pk:
            barcode_value = primary_display_value
            is_primary = True
        elif primary_display_value and barcode.is_primary and display_barcode_pk is not None and barcode.pk != display_barcode_pk:
            is_primary = False
        barcode_rows.append(
            {
                "value": barcode_value,
                "size": barcode_size,
                "is_primary": is_primary,
                "visible": is_visible,
            }
        )

    if not barcode_rows and primary_display_value:
        barcode_rows.append(
            {
                "value": primary_display_value,
                "size": selected_size or preferred_size,
                "is_primary": True,
                "visible": True,
            }
        )

    item.size_options_ui = size_options
    item.selected_size_ui = selected_size
    item.size_display_ui = selected_size or preferred_size
    item.has_size_selector_ui = bool(size_options)
    item.has_sized_barcodes_ui = has_sized_barcodes
    item.barcode_rows_ui = barcode_rows


def build_marketplace_binding_ui(item):
    item.ozon_sku_values_ui = [
        binding.external_id
        for binding in item.marketplace_bindings.all()
        if binding.marketplace == MarketplaceBinding.MARKETPLACE_OZON_SKU
    ]


def build_sku_list_queryset(request, *, base_qs):
    qs = base_qs
    show_deleted = request.GET.get("deleted") == "1"
    if show_deleted:
        qs = qs.filter(deleted=True)
    else:
        qs = qs.filter(deleted=False)
    search = request.GET.get("q")
    if search:
        qs = qs.filter(
            models.Q(sku_code__icontains=search)
            | models.Q(name__icontains=search)
            | models.Q(barcodes__value__icontains=search)
            | models.Q(
                marketplace_bindings__marketplace=MarketplaceBinding.MARKETPLACE_OZON_SKU,
                marketplace_bindings__external_id__icontains=search,
            )
        ).distinct()
    filter_field = request.GET.get("filter_field")
    filter_value = (request.GET.get("filter_value") or "").strip()
    if filter_field in FILTER_FIELDS and filter_value:
        lookup = FILTER_FIELDS[filter_field]
        qs = qs.filter(**{f"{lookup}__icontains": filter_value}).distinct()
    agency_filter = request.GET.get("agency")
    if agency_filter:
        qs = qs.filter(agency_id=agency_filter)
    sort_key = request.GET.get("sort", DEFAULT_SORT)
    direction = request.GET.get("dir", "asc")
    sort_field = SORT_FIELDS.get(sort_key, SORT_FIELDS[DEFAULT_SORT])
    order_by = f"-{sort_field}" if direction == "desc" else sort_field
    return qs.order_by(order_by).select_related(
        "market",
        "agency",
        "color_ref",
        "stor_unit",
    ).prefetch_related("barcodes", "photos", "marketplace_bindings")


def build_temporary_nomenclature_queryset(request, *, base_qs):
    if not temporary_nomenclature_enabled():
        return base_qs.none()
    qs = base_qs.filter(normalized_at__isnull=True)
    search = (request.GET.get("q") or "").strip()
    if search:
        qs = qs.filter(
            models.Q(item_code__icontains=search)
            | models.Q(name__icontains=search)
            | models.Q(size__icontains=search)
            | models.Q(barcode__icontains=search)
            | models.Q(normalized_sku_ref__sku_code__icontains=search)
        ).distinct()
    agency_filter = request.GET.get("agency")
    if agency_filter:
        qs = qs.filter(agency_id=agency_filter)
    sort_key = request.GET.get("sort", DEFAULT_TEMP_SORT)
    direction = request.GET.get("dir", "desc")
    sort_field = TEMP_SORT_FIELDS.get(sort_key, TEMP_SORT_FIELDS[DEFAULT_TEMP_SORT])
    order_by = f"-{sort_field}" if direction == "desc" else sort_field
    return qs.order_by(order_by).select_related("agency", "normalized_sku_ref")


def build_sku_sort_url(request, field: str, direction: str) -> str:
    params = request.GET.copy()
    if "view" not in params:
        params["view"] = "table"
    if get_catalog_mode(request) == "temporary":
        params["catalog"] = "temporary"
    params["sort"] = field
    params["dir"] = direction
    if request.GET.get("filter_field"):
        params["filter_field"] = request.GET.get("filter_field")
    if request.GET.get("filter_value"):
        params["filter_value"] = request.GET.get("filter_value")
    return f"?{params.urlencode()}"


def build_sku_list_context(request, *, items) -> dict:
    catalog_mode = get_catalog_mode(request)
    is_temporary_catalog = catalog_mode == "temporary"
    view = request.GET.get("view", "table")
    if view not in VIEW_MODES:
        view = "table"
    sort_fields = TEMP_SORT_FIELDS if is_temporary_catalog else SORT_FIELDS
    default_sort = DEFAULT_TEMP_SORT if is_temporary_catalog else DEFAULT_SORT
    current_sort = request.GET.get("sort", default_sort)
    current_dir = "desc" if request.GET.get("dir") == "desc" else "asc"
    sort_info = {}
    for field in sort_fields:
        is_current = current_sort == field
        next_dir = "desc" if is_current and current_dir == "asc" else "asc"
        sort_info[field] = {
            "url": build_sku_sort_url(request, field, next_dir),
            "active": is_current,
            "dir": current_dir if is_current else "",
            "next_dir": next_dir,
        }
    agency_filter = request.GET.get("agency") or ""
    request_user = getattr(request, "user", None)
    role = get_request_role(request) if request_user is not None else None
    label_settings_url = f"/labels/settings/?{urlencode({'return': request.get_full_path()})}"
    if not is_temporary_catalog:
        for item in items:
            build_size_ui(item)
            build_marketplace_binding_ui(item)
    return {
        "catalog_mode": catalog_mode,
        "is_temporary_catalog": is_temporary_catalog,
        "temporary_catalog_available": temporary_nomenclature_enabled(),
        "view_mode": view,
        "current_sort": current_sort,
        "current_dir": current_dir,
        "sort_info": sort_info,
        "filter_field": request.GET.get("filter_field") or "",
        "filter_value": request.GET.get("filter_value") or "",
        "show_deleted": (request.GET.get("deleted") == "1") and not is_temporary_catalog,
        "show_deleted_toggle": not is_temporary_catalog,
        "agency_filter": agency_filter,
        "hide_client_column": bool(agency_filter),
        "search_placeholder": (
            "\u041f\u043e\u0438\u0441\u043a \u043f\u043e \u0432\u0440\u0435\u043c\u0435\u043d\u043d\u043e\u043c\u0443 \u043a\u043e\u0434\u0443, \u043d\u0430\u0437\u0432\u0430\u043d\u0438\u044e, \u0440\u0430\u0437\u043c\u0435\u0440\u0443 \u0438\u043b\u0438 \u0448\u0442\u0440\u0438\u0445\u043a\u043e\u0434\u0443"
            if is_temporary_catalog
            else "\u041f\u043e\u0438\u0441\u043a \u043f\u043e \u0430\u0440\u0442\u0438\u043a\u0443\u043b\u0443, \u043d\u0430\u0437\u0432\u0430\u043d\u0438\u044e \u0438\u043b\u0438 \u0448\u0442\u0440\u0438\u0445\u043a\u043e\u0434\u0443"
        ),
        "hero_title": "\u041d\u043e\u043c\u0435\u043d\u043a\u043b\u0430\u0442\u0443\u0440\u0430",
        "hero_subtitle": (
            "\u0412\u0440\u0435\u043c\u0435\u043d\u043d\u0430\u044f \u0441\u043a\u043b\u0430\u0434\u0441\u043a\u0430\u044f \u043d\u043e\u043c\u0435\u043d\u043a\u043b\u0430\u0442\u0443\u0440\u0430 \u0434\u043b\u044f \u043e\u043f\u0442\u043e\u0432\u043e\u0433\u043e \u0442\u043e\u0432\u0430\u0440\u0430 \u0434\u043e \u043d\u043e\u0440\u043c\u0430\u043b\u0438\u0437\u0430\u0446\u0438\u0438 \u0432 \u043f\u043e\u043b\u043d\u043e\u0446\u0435\u043d\u043d\u044b\u0439 SKU."
            if is_temporary_catalog
            else "\u041f\u043e\u043b\u043d\u044b\u0439 \u043a\u0430\u0442\u0430\u043b\u043e\u0433 SKU \u043f\u043e \u0432\u0441\u0435\u043c \u043a\u043b\u0438\u0435\u043d\u0442\u0430\u043c. \u0422\u0430\u0431\u043b\u0438\u0446\u0430 \u043e\u0441\u0442\u0430\u0432\u043b\u0435\u043d\u0430 \u0434\u043b\u044f \u0431\u044b\u0441\u0442\u0440\u044b\u0445 \u043e\u043f\u0435\u0440\u0430\u0446\u0438\u0439 \u0438 \u043f\u043e\u0438\u0441\u043a\u0430, \u043a\u0430\u0440\u0442\u043e\u0447\u043a\u0438 \u0434\u043b\u044f \u043f\u0440\u043e\u0441\u043c\u043e\u0442\u0440\u0430 \u0440\u0430\u0441\u0448\u0438\u0440\u0435\u043d\u043d\u044b\u0445 \u0434\u0430\u043d\u043d\u044b\u0445."
        ),
        "panel_title": (
            "\u0412\u0440\u0435\u043c\u0435\u043d\u043d\u044b\u0435 \u043f\u043e\u0437\u0438\u0446\u0438\u0438 \u0441\u043a\u043b\u0430\u0434\u0430"
            if is_temporary_catalog and view == "table"
            else "\u041a\u0430\u0440\u0442\u043e\u0447\u043a\u0438 \u0432\u0440\u0435\u043c\u0435\u043d\u043d\u043e\u0439 \u043d\u043e\u043c\u0435\u043d\u043a\u043b\u0430\u0442\u0443\u0440\u044b"
            if is_temporary_catalog
            else "\u0422\u0430\u0431\u043b\u0438\u0447\u043d\u044b\u0439 \u0432\u0438\u0434 \u0441 \u0431\u044b\u0441\u0442\u0440\u044b\u043c\u0438 \u0434\u0435\u0439\u0441\u0442\u0432\u0438\u044f\u043c\u0438"
            if view == "table"
            else "\u041a\u0430\u0440\u0442\u043e\u0447\u043d\u044b\u0439 \u0432\u0438\u0434 \u0441 \u0440\u0430\u0441\u0448\u0438\u0440\u0435\u043d\u043d\u043e\u0439 \u0438\u043d\u0444\u043e\u0440\u043c\u0430\u0446\u0438\u0435\u0439"
        ),
        "panel_count_label": "\u0412\u0441\u0435\u0433\u043e \u0432\u0440\u0435\u043c\u0435\u043d\u043d\u044b\u0445 \u043f\u043e\u0437\u0438\u0446\u0438\u0439" if is_temporary_catalog else "\u0412\u0441\u0435\u0433\u043e SKU",
        "empty_eyebrow": "\u0412\u0440\u0435\u043c\u0435\u043d\u043d\u0430\u044f \u043d\u043e\u043c\u0435\u043d\u043a\u043b\u0430\u0442\u0443\u0440\u0430 \u043f\u043e\u043a\u0430 \u043f\u0443\u0441\u0442\u0430" if is_temporary_catalog else "\u041d\u043e\u043c\u0435\u043d\u043a\u043b\u0430\u0442\u0443\u0440\u0430 \u043f\u043e\u043a\u0430 \u043f\u0443\u0441\u0442\u0430",
        "empty_title": "\u0412\u0440\u0435\u043c\u0435\u043d\u043d\u044b\u0435 \u043f\u043e\u0437\u0438\u0446\u0438\u0438 \u043d\u0435 \u043d\u0430\u0439\u0434\u0435\u043d\u044b" if is_temporary_catalog else "\u0410\u043a\u0442\u0438\u0432\u043d\u044b\u0435 SKU \u043d\u0435 \u043d\u0430\u0439\u0434\u0435\u043d\u044b",
        "empty_text": (
            "\u0421\u0435\u0439\u0447\u0430\u0441 \u0432\u043e \u0432\u0440\u0435\u043c\u0435\u043d\u043d\u043e\u0439 \u0441\u043a\u043b\u0430\u0434\u0441\u043a\u043e\u0439 \u043d\u043e\u043c\u0435\u043d\u043a\u043b\u0430\u0442\u0443\u0440\u0435 \u043d\u0435\u0442 \u043d\u0435\u043d\u043e\u0440\u043c\u0430\u043b\u0438\u0437\u043e\u0432\u0430\u043d\u043d\u044b\u0445 \u043e\u043f\u0442\u043e\u0432\u044b\u0445 \u043f\u043e\u0437\u0438\u0446\u0438\u0439 \u043f\u043e\u0434 \u044d\u0442\u043e\u0442 \u0444\u0438\u043b\u044c\u0442\u0440."
            if is_temporary_catalog
            else "\u0421\u0435\u0439\u0447\u0430\u0441 \u0441\u0442\u0440\u0430\u043d\u0438\u0446\u0430 \u043f\u0443\u0441\u0442\u0430\u044f \u043d\u0435 \u0438\u0437-\u0437\u0430 \u0441\u043b\u043e\u043c\u0430\u043d\u043d\u043e\u0439 \u0432\u0435\u0440\u0441\u0442\u043a\u0438, \u0430 \u043f\u043e\u0442\u043e\u043c\u0443 \u0447\u0442\u043e \u0432 \u0431\u0430\u0437\u0435 \u043d\u0435\u0442 \u0430\u043a\u0442\u0438\u0432\u043d\u044b\u0445 \u0437\u0430\u043f\u0438\u0441\u0435\u0439 \u043d\u043e\u043c\u0435\u043d\u043a\u043b\u0430\u0442\u0443\u0440\u044b \u043f\u043e\u0434 \u044d\u0442\u043e\u0442 \u044d\u043a\u0440\u0430\u043d. \u041c\u043e\u0436\u043d\u043e \u0441\u043e\u0437\u0434\u0430\u0442\u044c SKU \u0432\u0440\u0443\u0447\u043d\u0443\u044e \u0438\u043b\u0438 \u0437\u0430\u0433\u0440\u0443\u0437\u0438\u0442\u044c/\u0441\u0438\u043d\u0445\u0440\u043e\u043d\u0438\u0437\u0438\u0440\u043e\u0432\u0430\u0442\u044c \u043a\u0430\u0442\u0430\u043b\u043e\u0433, \u043f\u043e\u0441\u043b\u0435 \u0447\u0435\u0433\u043e \u0437\u0430\u043f\u0438\u0441\u0438 \u0441\u0440\u0430\u0437\u0443 \u043f\u043e\u044f\u0432\u044f\u0442\u0441\u044f \u0437\u0434\u0435\u0441\u044c."
        ),
        "agency_options": Agency.objects.filter(archived=False)
        .only("id", "agn_name", "fio_agn")
        .order_by("agn_name", "fio_agn", "id"),
        "label_settings_url": label_settings_url,
        "label_settings_renderer_url": "/labels/settings/?renderer=1",
        "cabinet_url": resolve_cabinet_url(role),
    }


def suggest_sku_payload(query: str, *, catalog_mode: str = "sku") -> dict:
    query = (query or "").strip()
    if len(query) < 2:
        return {"items": []}
    if catalog_mode == "temporary" and temporary_nomenclature_enabled():
        qs = (
            WarehouseTemporaryNomenclature.objects.filter(
                normalized_at__isnull=True,
            )
            .filter(
                models.Q(item_code__icontains=query)
                | models.Q(name__icontains=query)
                | models.Q(size__icontains=query)
                | models.Q(barcode__icontains=query)
            )
            .select_related("agency")
            .order_by("item_code", "name")[:10]
        )
        return {
            "items": [
                {
                    "value": item.item_code or item.name,
                    "label": " \u00b7 ".join(
                        [
                            part
                            for part in [
                                item.item_code or "",
                                item.name or "",
                                item.size or "",
                            ]
                            if part
                        ]
                    ),
                }
                for item in qs
            ]
        }
    qs = (
        SKU.objects.filter(
            models.Q(sku_code__icontains=query)
            | models.Q(name__icontains=query)
            | models.Q(barcodes__value__icontains=query)
            | models.Q(
                marketplace_bindings__marketplace=MarketplaceBinding.MARKETPLACE_OZON_SKU,
                marketplace_bindings__external_id__icontains=query,
            )
        )
        .distinct()
        .order_by("sku_code")[:10]
    )
    return {
        "items": [{"value": sku.sku_code, "label": f"{sku.sku_code} \u2014 {sku.name}"} for sku in qs]
    }


def clone_sku_to_admin(*, pk: int, user=None):
    orig = get_object_or_404(SKU, pk=pk)
    base_code = f"{orig.sku_code}-copy"
    new_code = base_code
    counter = 1
    while SKU.objects.filter(sku_code=new_code).exists():
        new_code = f"{base_code}{counter}"
        counter += 1

    clone = SKU.objects.create(
        sku_code=new_code,
        name=orig.name,
        brand=orig.brand,
        market=orig.market,
        agency=orig.agency,
        color=orig.color,
        color_ref=orig.color_ref,
        size=orig.size,
        name_print=orig.name_print,
        code=None,
        img=orig.img,
        img_comment=orig.img_comment,
        gender=orig.gender,
        season=orig.season,
        additional_name=orig.additional_name,
        composition=orig.composition,
        made_in=orig.made_in,
        cr_product_date=orig.cr_product_date,
        end_product_date=orig.end_product_date,
        sign_akciz=orig.sign_akciz,
        tovar_category=orig.tovar_category,
        use_nds=orig.use_nds,
        vid_tovar=orig.vid_tovar,
        type_tovar=orig.type_tovar,
        stor_unit=orig.stor_unit,
        weight_kg=orig.weight_kg,
        weight_net_kg=orig.weight_net_kg,
        weight_gross_kg=orig.weight_gross_kg,
        volume=orig.volume,
        length_mm=orig.length_mm,
        width_mm=orig.width_mm,
        height_mm=orig.height_mm,
        honest_sign=orig.honest_sign,
        description=orig.description,
        source=orig.source,
        source_reference=None,
    )
    for photo in orig.photos.all():
        clone.photos.create(url=photo.url, sort_order=photo.sort_order)
    log_sku_change(
        "clone",
        clone,
        user=user if getattr(user, "is_authenticated", False) else None,
        description=f"\u041a\u043e\u043f\u0438\u044f \u0438\u0437 {orig.sku_code}",
    )
    return reverse("admin:sku_sku_change", args=[clone.pk])


def build_sku_form_context(*, mode: str, title: str, submit_label: str) -> dict:
    return {
        "mode": mode,
        "title": title,
        "submit_label": submit_label,
        "label_settings": load_label_settings(),
    }


def build_sku_duplicate_initial(*, pk: int) -> dict:
    orig = get_object_or_404(SKU, pk=pk)
    initial = {
        "name": orig.name,
        "brand": orig.brand,
        "agency": orig.agency_id,
        "market": orig.market_id,
        "color": orig.color,
        "color_ref": orig.color_ref_id,
        "size": orig.size,
        "name_print": orig.name_print,
        "img": orig.img,
        "img_comment": orig.img_comment,
        "gender": orig.gender,
        "season": orig.season,
        "additional_name": orig.additional_name,
        "composition": orig.composition,
        "made_in": orig.made_in,
        "cr_product_date": orig.cr_product_date,
        "end_product_date": orig.end_product_date,
        "sign_akciz": orig.sign_akciz,
        "tovar_category": orig.tovar_category,
        "use_nds": orig.use_nds,
        "vid_tovar": orig.vid_tovar,
        "type_tovar": orig.type_tovar,
        "stor_unit": orig.stor_unit_id,
        "weight_kg": orig.weight_kg,
        "weight_net_kg": orig.weight_net_kg,
        "weight_gross_kg": orig.weight_gross_kg,
        "volume": orig.volume,
        "length_mm": orig.length_mm,
        "width_mm": orig.width_mm,
        "height_mm": orig.height_mm,
        "honest_sign": orig.honest_sign,
        "description": orig.description,
        "source": orig.source,
        "source_reference": None,
        "deleted": False,
    }
    base_code = f"{orig.sku_code}-copy"
    candidate = base_code
    counter = 1
    while SKU.objects.filter(sku_code=candidate).exists():
        candidate = f"{base_code}{counter}"
        counter += 1
    initial["sku_code"] = candidate
    return initial


def mark_sku_deleted(*, pk: int, user=None):
    sku = get_object_or_404(SKU, pk=pk)
    if not sku.deleted:
        sku.deleted = True
        sku.save(update_fields=["deleted"])
        log_sku_change(
            "delete",
            sku,
            user=user if getattr(user, "is_authenticated", False) else None,
            description="\u041f\u043e\u043c\u0435\u0442\u043a\u0430 \u043a\u0430\u043a \u0443\u0434\u0430\u043b\u0435\u043d\u043d\u044b\u0439",
        )
    return HttpResponseRedirect("/sku/")
