"""Thin compatibility facade for stock map views and UI helpers."""

from . import web_ui as _web_ui

StockMapPrView = _web_ui.StockMapPrView
StockMapPickerDraftView = _web_ui.StockMapPickerDraftView
StockMapRowView = _web_ui.StockMapRowView
StockMapVisualView = _web_ui.StockMapVisualView
StockMapView = _web_ui.StockMapView

_OS_CELLS_PER_TIER = _web_ui._OS_CELLS_PER_TIER
_OS_ROW_SECTIONS = _web_ui._OS_ROW_SECTIONS
_OS_TIERS = _web_ui._OS_TIERS
_os_max_tiers = _web_ui._os_max_tiers
_os_max_tiers_for_row = _web_ui._os_max_tiers_for_row
_os_max_tiers_for_section = _web_ui._os_max_tiers_for_section
_os_is_passage_position = _web_ui._os_is_passage_position
_os_tiers_for_position = _web_ui._os_tiers_for_position
_os_total_slots_for_row = _web_ui._os_total_slots_for_row

_active_stockmap_rows = _web_ui._active_stockmap_rows
_active_os_reservation_rows = _web_ui._active_os_reservation_rows
_blend_rgb = _web_ui._blend_rgb
_clean_text = _web_ui._clean_text
_int_value = _web_ui._int_value
_latest_tasks_by_pallet = _web_ui._latest_tasks_by_pallet
_location_label = _web_ui._location_label
_os_location_code = _web_ui._os_location_code
_normalize_zone = _web_ui._normalize_zone
_os_cell_key = _web_ui._os_cell_key
_os_row_badge_style = _web_ui._os_row_badge_style
_os_row_cell_details = _web_ui._os_row_cell_details
_pr_zone_rows = _web_ui._pr_zone_rows
_process_label = _web_ui._process_label
_rgb_css = _web_ui._rgb_css
_stockmap_row_from_snapshot = _web_ui._stockmap_row_from_snapshot
_suggest_os_destinations_for_pr = _web_ui._suggest_os_destinations_for_pr
_warehouse_pallet_locations = _web_ui._warehouse_pallet_locations
_warehouse_snapshot_rows = _web_ui._warehouse_snapshot_rows
