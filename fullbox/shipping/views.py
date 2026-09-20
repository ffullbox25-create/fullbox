from __future__ import annotations

"""Thin compatibility facade for shipping HTTP views and UI helpers."""

from django.contrib.auth.decorators import login_required
from django.http import JsonResponse

from employees.access import get_request_role, is_staff_role
from sku.models import Agency

from . import web_ui as _web_ui

NEXT_DAY_DEADLINE_ERROR = _web_ui.NEXT_DAY_DEADLINE_ERROR
NEXT_DAY_DEADLINE_HOUR = _web_ui.NEXT_DAY_DEADLINE_HOUR
ShippingOrderAttachment = _web_ui.ShippingOrderAttachment
ShippingOrderForm = _web_ui.ShippingOrderForm
ShippingTransportNoteForm = _web_ui.ShippingTransportNoteForm
WORKDAY_END_HOUR = _web_ui.WORKDAY_END_HOUR
WORKDAY_HOURS_ERROR = _web_ui.WORKDAY_HOURS_ERROR
WORKDAY_START_HOUR = _web_ui.WORKDAY_START_HOUR

_active_shipping_attachments = _web_ui._active_shipping_attachments
_can_access_order = _web_ui._can_access_order
_can_cancel = _web_ui._can_cancel
_can_edit_items = _web_ui._can_edit_items
_can_edit_order_form = _web_ui._can_edit_order_form
_can_manager_approve = _web_ui._can_manager_approve
_can_manager_reopen = _web_ui._can_manager_reopen
_can_storekeeper_accept = _web_ui._can_storekeeper_accept
_can_storekeeper_manage_packing = _web_ui._can_storekeeper_manage_packing
_can_storekeeper_pack = _web_ui._can_storekeeper_pack
_can_storekeeper_pick = _web_ui._can_storekeeper_pick
_can_submit_for_approval = _web_ui._can_submit_for_approval
_can_write = _web_ui._can_write
_display_shipping_number = _web_ui._display_shipping_number
_is_logistician_role = _web_ui._is_logistician_role
_is_manager_role = _web_ui._is_manager_role
_is_storekeeper_role = _web_ui._is_storekeeper_role
_log_update = _web_ui._log_update
_order_box_count = _web_ui._order_box_count
_parse_box_count_from_comment = _web_ui._parse_box_count_from_comment
_parse_selected_stock_items = _web_ui._parse_selected_stock_items
_partial_box_count_from_request = _web_ui._partial_box_count_from_request
_partial_box_splits_json_for_order = _web_ui._partial_box_splits_json_for_order
_partial_box_splits_json_from_request = _web_ui._partial_box_splits_json_from_request
_request_scope = _web_ui._request_scope
_delete_shipping_attachments = _web_ui._delete_shipping_attachments
_save_shipping_attachments = _web_ui._save_shipping_attachments
_selected_box_values_for_order = _web_ui._selected_box_values_for_order
_selected_box_values_from_request = _web_ui._selected_box_values_from_request
_selected_stock_rows_with_boxes = _web_ui._selected_stock_rows_with_boxes
_shipping_attachment_names = _web_ui._shipping_attachment_names
_shipping_box_row_key = _web_ui._shipping_box_row_key
_shipping_boxes_from_packing_payload = _web_ui._shipping_boxes_from_packing_payload
_shipping_delivered_boxes = _web_ui._shipping_delivered_boxes
_shipping_dispatch_stage = _web_ui._shipping_dispatch_stage
_shipping_loose_packing_initial_state = _web_ui._shipping_loose_packing_initial_state
_shipping_manageable_packing_boxes = _web_ui._shipping_manageable_packing_boxes
_shipping_packing_initial_state = _web_ui._shipping_packing_initial_state
_shipping_packing_summary = _web_ui._shipping_packing_summary
_shipping_reachtruck_metrics = _web_ui._shipping_reachtruck_metrics
_shipping_stock_picker_rows = _web_ui._shipping_stock_picker_rows
_shipping_ui_status_label = _web_ui._shipping_ui_status_label
_to_int = _web_ui._to_int
_with_selected_boxes = _web_ui._with_selected_boxes

build_print_status_snapshot = _web_ui.build_print_status_snapshot
build_transport_note_preview_context = _web_ui.build_transport_note_preview_context
can_access_transport_note = _web_ui.can_access_transport_note
download_shipping_attachment = _web_ui.download_shipping_attachment
get_or_create_transport_note = _web_ui.get_or_create_transport_note
handle_shipping_create_request = _web_ui.handle_shipping_create_request
handle_shipping_detail_action = _web_ui.handle_shipping_detail_action
handle_shipping_dispatch_act_request = _web_ui.handle_shipping_dispatch_act_request
handle_shipping_dispatch_sign_logistician_request = _web_ui.handle_shipping_dispatch_sign_logistician_request
handle_shipping_dispatch_sign_manager_request = _web_ui.handle_shipping_dispatch_sign_manager_request
handle_shipping_documents_request = _web_ui.handle_shipping_documents_request
handle_shipping_loose_packing_request = _web_ui.handle_shipping_loose_packing_request
handle_shipping_packing_request = _web_ui.handle_shipping_packing_request
handle_shipping_packing_pallet_removal_request = _web_ui.handle_shipping_packing_pallet_removal_request
handle_shipping_packing_pallet_removal_review_request = _web_ui.handle_shipping_packing_pallet_removal_review_request
handle_shipping_packing_slips_request = _web_ui.handle_shipping_packing_slips_request
handle_shipping_packing_slips_status_request = _web_ui.handle_shipping_packing_slips_status_request
load_marketplace_warehouse_catalog = _web_ui.load_marketplace_warehouse_catalog
refresh_print_agent_printers = _web_ui.refresh_print_agent_printers
render_transport_note_docx = _web_ui.render_transport_note_docx
render_transport_note_pdf = _web_ui.render_transport_note_pdf
request_shipping_packing_pallet_removal = _web_ui.request_shipping_packing_pallet_removal
resolve_shipping_packing_pallet_removal = _web_ui.resolve_shipping_packing_pallet_removal
save_shipping_loose_packing = _web_ui.save_shipping_loose_packing
save_shipping_packing = _web_ui.save_shipping_packing
shipping_available_items = _web_ui.shipping_available_items
shipping_detail = _web_ui.shipping_detail
shipping_report = _web_ui.shipping_report
shipping_report_xlsx = _web_ui.shipping_report_xlsx
shipping_box_composition_xlsx = _web_ui.shipping_box_composition_xlsx
shipping_packing_list_xlsx = _web_ui.shipping_packing_list_xlsx
shipping_dispatch_act = _web_ui.shipping_dispatch_act
shipping_documents = _web_ui.shipping_documents
shipping_attachment_download = _web_ui.shipping_attachment_download
shipping_create = _web_ui.shipping_create
shipping_client_template = _web_ui.shipping_client_template
shipping_ozon_client_template = getattr(
    _web_ui,
    "shipping_ozon_client_template",
    shipping_client_template,
)


def _resolve_ozon_form_agency(request) -> Agency | None:
    user = getattr(request, "user", None)
    if not user or not user.is_authenticated:
        return None
    direct_client = Agency.objects.filter(portal_user=user, archived=False).first()
    if direct_client:
        return direct_client
    role = get_request_role(request)
    if not (user.is_staff or is_staff_role(role)):
        return None
    raw_client = str(request.GET.get("client") or request.POST.get("client") or "").strip()
    if raw_client.isdigit():
        return Agency.objects.filter(id=int(raw_client), archived=False).first()
    return None


@login_required
def _shipping_ozon_supplies_list_fallback(request):
    agency = _resolve_ozon_form_agency(request)
    if agency is None:
        return JsonResponse({"ok": False, "error": "Выберите клиента."}, status=400)
    from .ozon_supplies import list_ozon_supplies_for_agency

    exclude_order_id = _web_ui._ozon_exclude_order_id(request)
    result = list_ozon_supplies_for_agency(agency, exclude_order_id=exclude_order_id)
    return JsonResponse(result, status=200 if result.get("ok") else 400)


@login_required
def _shipping_ozon_supplies_detail_fallback(request, order_id: int):
    agency = _resolve_ozon_form_agency(request)
    if agency is None:
        return JsonResponse({"ok": False, "error": "Выберите клиента."}, status=400)
    from .ozon_supplies import get_ozon_supply_for_agency

    exclude_order_id = _web_ui._ozon_exclude_order_id(request)
    try:
        stock_rows = _web_ui._shipping_stock_picker_rows(agency)
    except Exception:
        stock_rows = []
    try:
        result = get_ozon_supply_for_agency(
            agency,
            int(order_id),
            stock_rows=stock_rows,
            enrich_gm=False,
            api_timeout=15,
            exclude_order_id=exclude_order_id,
        )
    except Exception:
        return JsonResponse(
            {
                "ok": False,
                "error": (
                    "Не удалось разобрать заявку Ozon. "
                    "Попробуйте ещё раз или загрузите Excel."
                ),
            },
            status=400,
        )
    if not isinstance(result, dict):
        return JsonResponse(
            {"ok": False, "error": "Не удалось разобрать заявку Ozon."},
            status=400,
        )
    return JsonResponse(result, status=200 if result.get("ok") else 400)


shipping_ozon_supplies_list = getattr(
    _web_ui,
    "shipping_ozon_supplies_list",
    _shipping_ozon_supplies_list_fallback,
)
shipping_ozon_supplies_detail = getattr(
    _web_ui,
    "shipping_ozon_supplies_detail",
    _shipping_ozon_supplies_detail_fallback,
)
shipping_list = _web_ui.shipping_list
shipping_loose_packing = _web_ui.shipping_loose_packing
shipping_loose_packing_container_code = _web_ui.shipping_loose_packing_container_code
shipping_packing = _web_ui.shipping_packing
shipping_packing_pallet_removal_request = _web_ui.shipping_packing_pallet_removal_request
shipping_packing_pallet_removal_review = _web_ui.shipping_packing_pallet_removal_review
shipping_packing_pending_pallet_removal = _web_ui.shipping_packing_pending_pallet_removal
shipping_packing_slip_meta = _web_ui.shipping_packing_slip_meta
shipping_packing_slips = _web_ui.shipping_packing_slips
shipping_packing_slips_data = _web_ui.shipping_packing_slips_data
shipping_packing_slips_status = _web_ui.shipping_packing_slips_status
shipping_pick_readiness = _web_ui.shipping_pick_readiness
shipping_return_act_doc = _web_ui.shipping_return_act_doc
shipping_sign_dispatch_act_logistician = _web_ui.shipping_sign_dispatch_act_logistician
shipping_sign_dispatch_act_manager = _web_ui.shipping_sign_dispatch_act_manager
shipping_transport_note = _web_ui.shipping_transport_note
shipping_transport_note_docx = _web_ui.shipping_transport_note_docx
shipping_transport_note_pdf = _web_ui.shipping_transport_note_pdf
sign_dispatch_act_logistician = _web_ui.sign_dispatch_act_logistician
sign_dispatch_act_manager = _web_ui.sign_dispatch_act_manager
transport_note_docx_filename = _web_ui.transport_note_docx_filename
transport_note_filename = _web_ui.transport_note_filename
