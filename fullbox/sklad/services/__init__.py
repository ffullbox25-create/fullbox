from __future__ import annotations

from importlib import import_module


_EXPORTS = {
    "StockAvailabilityService": ".stock_availability",
    "OperationalStockService": ".stock_operations",
    "StockPalletTree": ".stock_operations",
    "WarehouseEvent": ".warehouse_events",
    "WarehouseEventType": ".warehouse_events",
    "WarehouseCommandResult": ".warehouse_commands",
    "WarehouseCommandService": ".warehouse_commands",
    "WarehouseActionPolicy": ".warehouse_policy",
    "WarehouseActionPolicyResult": ".warehouse_policy",
    "WarehouseGoodsStateResolver": ".warehouse_state",
    "WarehouseMovementResolver": ".warehouse_state",
    "WarehouseStateCode": ".warehouse_state",
    "WarehouseStateResult": ".warehouse_state",
    "WarehouseTemporaryNomenclatureService": ".temporary_nomenclature",
    "WarehouseTransitionError": ".warehouse_transitions",
    "WarehouseTransitionResult": ".warehouse_transitions",
    "WarehouseTransitionService": ".warehouse_transitions",
    "WarehousePlacementResult": ".warehouse_write_path",
    "WarehouseWritePathService": ".warehouse_write_path",
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
