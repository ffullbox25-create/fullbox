from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class WarehouseEventType(str, Enum):
    RECEIVING_ARRIVED = "receiving_arrived"
    PLACEMENT_STARTED = "placement_started"
    PLACEMENT_COMPLETED = "placement_completed"
    PUTAWAY_REQUESTED = "putaway_requested"
    PUTAWAY_COMPLETED = "putaway_completed"

    PROCESSING_REQUESTED = "processing_requested"
    PROCESSING_RESERVED = "processing_reserved"
    PROCESSING_RESERVE_RELEASED = "processing_reserve_released"
    PROCESSING_ZONE_ARRIVED = "processing_zone_arrived"
    PROCESSING_STARTED = "processing_started"
    PROCESSING_COMPLETED = "processing_completed"
    PROCESSING_CONSUMED = "processing_consumed"

    SHIPPING_RESERVED = "shipping_reserved"
    SHIPPING_RESERVE_RELEASED = "shipping_reserve_released"
    OTG_REQUESTED = "otg_requested"
    OTG_ARRIVED = "otg_arrived"
    PALLETIZATION_STARTED = "palletization_started"
    PALLETIZATION_COMPLETED = "palletization_completed"
    READY_FOR_LOADING = "ready_for_loading"

    ASSIGNED_TO_TRIP = "assigned_to_trip"
    LOADING_STARTED = "loading_started"
    LOADED_TO_VEHICLE = "loaded_to_vehicle"
    SHIPPED = "shipped"

    MOVEMENT_REQUESTED = "movement_requested"
    MOVEMENT_TASK_CREATED = "movement_task_created"
    MOVEMENT_STARTED = "movement_started"
    MOVEMENT_COMPLETED = "movement_completed"
    MOVEMENT_CANCELED = "movement_canceled"

    STOCK_RETURNED_TO_STORAGE = "stock_returned_to_storage"
    STOCK_CORRECTED = "stock_corrected"
    WAREHOUSE_CONTEXT_CANCELED = "warehouse_context_canceled"


@dataclass(frozen=True)
class WarehouseEvent:
    event_type: WarehouseEventType
    stock_context_type: str = ""
    stock_context_id: str = ""
    source_document_type: str = ""
    source_document_id: str = ""
    operation_type: str = ""
    zone_from: str = ""
    zone_to: str = ""
    payload: dict = field(default_factory=dict)
