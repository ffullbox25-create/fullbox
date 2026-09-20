from __future__ import annotations

from importlib import import_module


_EXPORTS = {
    "create_batch_move_tasks": ".move_requests",
    "create_shipping_pick_request": ".move_requests",
    "create_stock_move_task": ".move_requests",
    "sync_task_status_by_legacy_order_id": ".move_requests",
    "build_putaway_rows": ".putaway_planner",
    "normalize_putaway_location": ".putaway_planner",
    "normalize_zone_code": ".putaway_planner",
    "parse_int_value": ".putaway_planner",
    "parse_putaway_destinations": ".putaway_planner",
    "putaway_location_label": ".putaway_planner",
    "putaway_location_scan_code": ".putaway_planner",
    "suggest_putaway_destinations": ".putaway_planner",
    "build_mobile_execution_snapshot": ".task_commands",
    "build_mobile_request_execution_snapshot": ".task_commands",
    "complete_move_task": ".task_commands",
    "scan_move_request_step": ".task_commands",
    "scan_move_task_step": ".task_commands",
    "take_move_request": ".task_commands",
    "take_move_task": ".task_commands",
    "_mobile_request_route_summary": ".ui_flows",
    "_mobile_route_detail": ".ui_flows",
    "_short_agency_name": ".ui_flows",
    "_task_count_label": ".ui_flows",
    "_task_kind_label": ".ui_flows",
    "build_dashboard_context": ".ui_flows",
    "collect_moves": ".ui_flows",
    "create_move_request_response": ".ui_flows",
    "handle_dashboard_post": ".ui_flows",
    "lookup_item_pallets_response": ".ui_flows",
    "lookup_pallet_location_response": ".ui_flows",
    "mobile_category_key": ".ui_flows",
    "mobile_category_label": ".ui_flows",
    "mobile_number_label": ".ui_flows",
    "mobile_request_identity": ".ui_flows",
    "mobile_request_key": ".ui_flows",
    "mobile_request_url": ".ui_flows",
    "mobile_task_url": ".ui_flows",
}


__all__ = sorted(_EXPORTS)


def __getattr__(name: str):
    module_name = _EXPORTS.get(name)
    if not module_name:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module = import_module(module_name, __name__)
    value = getattr(module, name)
    globals()[name] = value
    return value


def __dir__():
    return sorted(set(globals()) | set(__all__))
