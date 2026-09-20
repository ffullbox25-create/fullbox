from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from .warehouse_events import WarehouseEventType


class WarehouseStateCode(str, Enum):
    UNKNOWN = "unknown"
    RECEIVED_UNPLACED = "received_unplaced"
    PLACED_IN_RECEIVING = "placed_in_receiving"
    STORED = "stored"
    RESERVED_FOR_PROCESSING = "reserved_for_processing"
    MOVING_TO_PROCESSING = "moving_to_processing"
    IN_PROCESSING_ZONE = "in_processing_zone"
    PROCESSING_IN_PROGRESS = "processing_in_progress"
    PROCESSING_CONSUMED = "processing_consumed"
    PLACED_AFTER_PROCESSING = "placed_after_processing"
    RESERVED_FOR_SHIPPING = "reserved_for_shipping"
    MOVING_TO_OTG = "moving_to_otg"
    IN_OTG = "in_otg"
    PALLETIZING = "palletizing"
    READY_FOR_LOADING = "ready_for_loading"
    ASSIGNED_TO_TRIP = "assigned_to_trip"
    LOADING_IN_PROGRESS = "loading_in_progress"
    LOADED_TO_VEHICLE = "loaded_to_vehicle"
    SHIPPED = "shipped"
    PARTIALLY_SHIPPED = "partially_shipped"
    CANCELED = "canceled"


class WarehouseTransitionError(ValueError):
    """Raised when a warehouse event tries to perform an invalid state transition."""


@dataclass(frozen=True)
class WarehouseTransitionResult:
    previous_code: WarehouseStateCode
    code: WarehouseStateCode
    event_type: WarehouseEventType
    changed: bool
    is_noop: bool
    source_facts: list[str] = field(default_factory=list)


class WarehouseTransitionService:
    @classmethod
    def apply_event(
        cls,
        current_code: WarehouseStateCode | str | None,
        event_type: WarehouseEventType | str,
        *,
        operation_type: str = "",
        zone_to: str = "",
    ) -> WarehouseTransitionResult:
        previous_code = cls._normalize_state(current_code)
        normalized_event = cls._normalize_event(event_type)
        normalized_operation = cls._normalize_token(operation_type)
        normalized_zone = cls._normalize_zone(zone_to)
        next_code = cls._resolve_next_state(
            previous_code,
            normalized_event,
            operation_type=normalized_operation,
            zone_to=normalized_zone,
        )
        return WarehouseTransitionResult(
            previous_code=previous_code,
            code=next_code,
            event_type=normalized_event,
            changed=next_code != previous_code,
            is_noop=next_code == previous_code,
            source_facts=[
                f"state:{previous_code.value}",
                f"event:{normalized_event.value}",
                f"operation:{normalized_operation or '-'}",
                f"zone_to:{normalized_zone or '-'}",
            ],
        )

    @classmethod
    def _resolve_next_state(
        cls,
        current_code: WarehouseStateCode,
        event_type: WarehouseEventType,
        *,
        operation_type: str,
        zone_to: str,
    ) -> WarehouseStateCode:
        if event_type == WarehouseEventType.WAREHOUSE_CONTEXT_CANCELED:
            return WarehouseStateCode.CANCELED

        if current_code == WarehouseStateCode.UNKNOWN:
            if event_type == WarehouseEventType.RECEIVING_ARRIVED:
                return WarehouseStateCode.RECEIVED_UNPLACED
            raise cls._invalid(current_code, event_type, operation_type, zone_to)

        if current_code == WarehouseStateCode.RECEIVED_UNPLACED:
            if event_type == WarehouseEventType.PLACEMENT_STARTED:
                return current_code
            if event_type == WarehouseEventType.PLACEMENT_COMPLETED:
                return WarehouseStateCode.PLACED_IN_RECEIVING
            raise cls._invalid(current_code, event_type, operation_type, zone_to)

        if current_code == WarehouseStateCode.PLACED_IN_RECEIVING:
            if event_type in {
                WarehouseEventType.PUTAWAY_REQUESTED,
                WarehouseEventType.MOVEMENT_STARTED,
            } and cls._is_putaway(operation_type):
                return current_code
            if event_type == WarehouseEventType.MOVEMENT_STARTED and operation_type == "internal_relocation":
                return current_code
            if event_type in {
                WarehouseEventType.OTG_REQUESTED,
                WarehouseEventType.MOVEMENT_REQUESTED,
            } and cls._is_move_to_otg(operation_type):
                return current_code
            if event_type == WarehouseEventType.MOVEMENT_STARTED and cls._is_move_to_otg(operation_type):
                return WarehouseStateCode.MOVING_TO_OTG
            if event_type == WarehouseEventType.SHIPPING_RESERVED:
                return WarehouseStateCode.RESERVED_FOR_SHIPPING
            if event_type == WarehouseEventType.PUTAWAY_COMPLETED:
                return WarehouseStateCode.STORED
            if event_type == WarehouseEventType.MOVEMENT_COMPLETED and cls._is_storage_zone(zone_to):
                return WarehouseStateCode.STORED
            if event_type == WarehouseEventType.OTG_ARRIVED and cls._is_otg_zone(zone_to):
                return WarehouseStateCode.IN_OTG
            raise cls._invalid(current_code, event_type, operation_type, zone_to)

        if current_code == WarehouseStateCode.STORED:
            if event_type == WarehouseEventType.PROCESSING_CONSUMED:
                return WarehouseStateCode.PROCESSING_CONSUMED
            if event_type == WarehouseEventType.PROCESSING_RESERVED:
                return WarehouseStateCode.RESERVED_FOR_PROCESSING
            if event_type == WarehouseEventType.SHIPPING_RESERVED:
                return WarehouseStateCode.RESERVED_FOR_SHIPPING
            if event_type in {
                WarehouseEventType.OTG_REQUESTED,
                WarehouseEventType.MOVEMENT_REQUESTED,
            } and cls._is_move_to_otg(operation_type):
                return current_code
            if event_type == WarehouseEventType.MOVEMENT_STARTED and cls._is_move_to_otg(operation_type):
                return WarehouseStateCode.MOVING_TO_OTG
            if event_type in {
                WarehouseEventType.MOVEMENT_REQUESTED,
                WarehouseEventType.MOVEMENT_STARTED,
                WarehouseEventType.MOVEMENT_COMPLETED,
            } and operation_type == "internal_relocation":
                return current_code
            raise cls._invalid(current_code, event_type, operation_type, zone_to)

        if current_code == WarehouseStateCode.RESERVED_FOR_PROCESSING:
            if event_type == WarehouseEventType.PROCESSING_CONSUMED:
                return WarehouseStateCode.PROCESSING_CONSUMED
            if event_type == WarehouseEventType.PROCESSING_RESERVED:
                return current_code
            if event_type == WarehouseEventType.PROCESSING_RESERVE_RELEASED:
                return WarehouseStateCode.STORED
            if event_type == WarehouseEventType.MOVEMENT_REQUESTED and cls._is_move_to_processing(operation_type):
                return current_code
            if event_type == WarehouseEventType.MOVEMENT_STARTED and cls._is_move_to_processing(operation_type):
                return WarehouseStateCode.MOVING_TO_PROCESSING
            if event_type == WarehouseEventType.MOVEMENT_CANCELED and cls._is_move_to_processing(operation_type):
                return current_code
            raise cls._invalid(current_code, event_type, operation_type, zone_to)

        if current_code == WarehouseStateCode.MOVING_TO_PROCESSING:
            if event_type == WarehouseEventType.PROCESSING_CONSUMED:
                return WarehouseStateCode.PROCESSING_CONSUMED
            if event_type == WarehouseEventType.PROCESSING_ZONE_ARRIVED:
                return WarehouseStateCode.IN_PROCESSING_ZONE
            if event_type == WarehouseEventType.MOVEMENT_COMPLETED and cls._is_processing_zone(zone_to):
                return WarehouseStateCode.IN_PROCESSING_ZONE
            if event_type == WarehouseEventType.MOVEMENT_CANCELED:
                return WarehouseStateCode.RESERVED_FOR_PROCESSING
            if event_type == WarehouseEventType.STOCK_RETURNED_TO_STORAGE:
                return WarehouseStateCode.STORED
            raise cls._invalid(current_code, event_type, operation_type, zone_to)

        if current_code == WarehouseStateCode.IN_PROCESSING_ZONE:
            if event_type == WarehouseEventType.PROCESSING_CONSUMED:
                return WarehouseStateCode.PROCESSING_CONSUMED
            if event_type == WarehouseEventType.PROCESSING_STARTED:
                return WarehouseStateCode.PROCESSING_IN_PROGRESS
            if event_type == WarehouseEventType.MOVEMENT_STARTED and operation_type == "internal_relocation":
                return current_code
            if event_type == WarehouseEventType.MOVEMENT_COMPLETED and cls._is_storage_zone(zone_to):
                return WarehouseStateCode.STORED
            if event_type in {
                WarehouseEventType.OTG_REQUESTED,
                WarehouseEventType.MOVEMENT_REQUESTED,
            } and cls._is_move_to_otg(operation_type):
                return current_code
            if event_type == WarehouseEventType.MOVEMENT_STARTED and cls._is_move_to_otg(operation_type):
                return WarehouseStateCode.MOVING_TO_OTG
            if event_type == WarehouseEventType.STOCK_RETURNED_TO_STORAGE:
                return WarehouseStateCode.STORED
            raise cls._invalid(current_code, event_type, operation_type, zone_to)

        if current_code == WarehouseStateCode.PROCESSING_IN_PROGRESS:
            if event_type == WarehouseEventType.PROCESSING_CONSUMED:
                return WarehouseStateCode.PROCESSING_CONSUMED
            if event_type == WarehouseEventType.PROCESSING_COMPLETED:
                return WarehouseStateCode.PLACED_AFTER_PROCESSING
            if event_type == WarehouseEventType.STOCK_RETURNED_TO_STORAGE:
                return WarehouseStateCode.STORED
            raise cls._invalid(current_code, event_type, operation_type, zone_to)

        if current_code == WarehouseStateCode.PLACED_AFTER_PROCESSING:
            if event_type == WarehouseEventType.PUTAWAY_REQUESTED:
                return current_code
            if event_type == WarehouseEventType.MOVEMENT_STARTED and cls._is_putaway(operation_type):
                return current_code
            if event_type == WarehouseEventType.MOVEMENT_STARTED and operation_type == "internal_relocation":
                return current_code
            if event_type == WarehouseEventType.MOVEMENT_COMPLETED and cls._is_storage_zone(zone_to):
                return WarehouseStateCode.STORED
            if event_type == WarehouseEventType.SHIPPING_RESERVED:
                return WarehouseStateCode.RESERVED_FOR_SHIPPING
            if event_type in {
                WarehouseEventType.OTG_REQUESTED,
                WarehouseEventType.MOVEMENT_REQUESTED,
            } and cls._is_move_to_otg(operation_type):
                return current_code
            if event_type == WarehouseEventType.MOVEMENT_STARTED and cls._is_move_to_otg(operation_type):
                return WarehouseStateCode.MOVING_TO_OTG
            raise cls._invalid(current_code, event_type, operation_type, zone_to)

        if current_code == WarehouseStateCode.RESERVED_FOR_SHIPPING:
            if event_type == WarehouseEventType.SHIPPING_RESERVED:
                return current_code
            if event_type == WarehouseEventType.SHIPPING_RESERVE_RELEASED:
                return WarehouseStateCode.STORED
            if event_type in {
                WarehouseEventType.OTG_REQUESTED,
                WarehouseEventType.MOVEMENT_REQUESTED,
            } and cls._is_move_to_otg(operation_type):
                return current_code
            if event_type == WarehouseEventType.MOVEMENT_STARTED and cls._is_move_to_otg(operation_type):
                return WarehouseStateCode.MOVING_TO_OTG
            if event_type == WarehouseEventType.MOVEMENT_CANCELED and cls._is_move_to_otg(operation_type):
                return current_code
            raise cls._invalid(current_code, event_type, operation_type, zone_to)

        if current_code == WarehouseStateCode.MOVING_TO_OTG:
            if event_type == WarehouseEventType.OTG_ARRIVED:
                return WarehouseStateCode.IN_OTG
            if event_type == WarehouseEventType.MOVEMENT_COMPLETED and cls._is_otg_zone(zone_to):
                return WarehouseStateCode.IN_OTG
            if event_type == WarehouseEventType.MOVEMENT_CANCELED:
                return WarehouseStateCode.RESERVED_FOR_SHIPPING
            if event_type == WarehouseEventType.STOCK_RETURNED_TO_STORAGE:
                return WarehouseStateCode.STORED
            raise cls._invalid(current_code, event_type, operation_type, zone_to)

        if current_code == WarehouseStateCode.IN_OTG:
            if event_type in {
                WarehouseEventType.SHIPPING_RESERVED,
                WarehouseEventType.SHIPPING_RESERVE_RELEASED,
            }:
                # Reservation state and physical location are independent for
                # stock being returned after a canceled shipping order.
                return current_code
            if event_type == WarehouseEventType.PALLETIZATION_STARTED:
                return WarehouseStateCode.PALLETIZING
            if event_type == WarehouseEventType.READY_FOR_LOADING:
                return WarehouseStateCode.READY_FOR_LOADING
            if event_type == WarehouseEventType.STOCK_RETURNED_TO_STORAGE:
                return WarehouseStateCode.STORED
            raise cls._invalid(current_code, event_type, operation_type, zone_to)

        if current_code == WarehouseStateCode.PALLETIZING:
            if event_type in {
                WarehouseEventType.SHIPPING_RESERVED,
                WarehouseEventType.SHIPPING_RESERVE_RELEASED,
            }:
                return current_code
            if event_type == WarehouseEventType.PALLETIZATION_COMPLETED:
                return WarehouseStateCode.READY_FOR_LOADING
            if event_type == WarehouseEventType.STOCK_RETURNED_TO_STORAGE:
                return WarehouseStateCode.STORED
            raise cls._invalid(current_code, event_type, operation_type, zone_to)

        if current_code == WarehouseStateCode.READY_FOR_LOADING:
            if event_type in {
                WarehouseEventType.SHIPPING_RESERVED,
                WarehouseEventType.SHIPPING_RESERVE_RELEASED,
            }:
                return current_code
            if event_type == WarehouseEventType.ASSIGNED_TO_TRIP:
                return WarehouseStateCode.ASSIGNED_TO_TRIP
            if event_type == WarehouseEventType.LOADING_STARTED:
                return WarehouseStateCode.LOADING_IN_PROGRESS
            if event_type == WarehouseEventType.STOCK_RETURNED_TO_STORAGE:
                return WarehouseStateCode.STORED
            raise cls._invalid(current_code, event_type, operation_type, zone_to)

        if current_code == WarehouseStateCode.ASSIGNED_TO_TRIP:
            if event_type == WarehouseEventType.LOADING_STARTED:
                return WarehouseStateCode.LOADING_IN_PROGRESS
            if event_type == WarehouseEventType.STOCK_RETURNED_TO_STORAGE:
                return WarehouseStateCode.STORED
            raise cls._invalid(current_code, event_type, operation_type, zone_to)

        if current_code == WarehouseStateCode.LOADING_IN_PROGRESS:
            if event_type == WarehouseEventType.LOADED_TO_VEHICLE:
                return WarehouseStateCode.LOADED_TO_VEHICLE
            if event_type == WarehouseEventType.STOCK_RETURNED_TO_STORAGE:
                return WarehouseStateCode.STORED
            raise cls._invalid(current_code, event_type, operation_type, zone_to)

        if current_code == WarehouseStateCode.LOADED_TO_VEHICLE:
            if event_type == WarehouseEventType.SHIPPED:
                return WarehouseStateCode.SHIPPED
            if event_type == WarehouseEventType.STOCK_RETURNED_TO_STORAGE:
                return WarehouseStateCode.STORED
            raise cls._invalid(current_code, event_type, operation_type, zone_to)

        if current_code == WarehouseStateCode.PARTIALLY_SHIPPED:
            if event_type == WarehouseEventType.LOADED_TO_VEHICLE:
                return current_code
            if event_type == WarehouseEventType.SHIPPED:
                return WarehouseStateCode.SHIPPED
            if event_type == WarehouseEventType.STOCK_RETURNED_TO_STORAGE:
                return WarehouseStateCode.STORED
            raise cls._invalid(current_code, event_type, operation_type, zone_to)

        if current_code == WarehouseStateCode.PROCESSING_CONSUMED:
            if event_type == WarehouseEventType.PROCESSING_CONSUMED:
                return current_code
            raise cls._invalid(current_code, event_type, operation_type, zone_to)

        if current_code in {WarehouseStateCode.SHIPPED, WarehouseStateCode.CANCELED}:
            raise cls._invalid(current_code, event_type, operation_type, zone_to)

        raise cls._invalid(current_code, event_type, operation_type, zone_to)

    @staticmethod
    def _normalize_state(value: WarehouseStateCode | str | None) -> WarehouseStateCode:
        if isinstance(value, WarehouseStateCode):
            return value
        normalized = str(value or "").strip().lower()
        if not normalized:
            return WarehouseStateCode.UNKNOWN
        return WarehouseStateCode(normalized)

    @staticmethod
    def _normalize_event(value: WarehouseEventType | str) -> WarehouseEventType:
        if isinstance(value, WarehouseEventType):
            return value
        return WarehouseEventType(str(value or "").strip().lower())

    @staticmethod
    def _normalize_token(value: str | None) -> str:
        return str(value or "").strip().lower()

    @classmethod
    def _normalize_zone(cls, value: str | None) -> str:
        return cls._normalize_token(value).upper()

    @staticmethod
    def _is_storage_zone(zone_to: str) -> bool:
        return zone_to in {"OS", "MR"}

    @staticmethod
    def _is_processing_zone(zone_to: str) -> bool:
        return zone_to == "OBR"

    @staticmethod
    def _is_otg_zone(zone_to: str) -> bool:
        return zone_to == "OTG"

    @staticmethod
    def _is_putaway(operation_type: str) -> bool:
        return operation_type == "putaway"

    @staticmethod
    def _is_move_to_processing(operation_type: str) -> bool:
        return operation_type == "move_to_processing"

    @staticmethod
    def _is_move_to_otg(operation_type: str) -> bool:
        return operation_type in {"move_to_otg", ""}

    @staticmethod
    def _invalid(
        current_code: WarehouseStateCode,
        event_type: WarehouseEventType,
        operation_type: str,
        zone_to: str,
    ) -> WarehouseTransitionError:
        return WarehouseTransitionError(
            "Недопустимый warehouse transition: "
            f"{current_code.value} + {event_type.value} "
            f"(operation={operation_type or '-'}, zone_to={zone_to or '-'})"
        )
