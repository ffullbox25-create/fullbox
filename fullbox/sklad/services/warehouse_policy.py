from __future__ import annotations

from dataclasses import dataclass, field

from .warehouse_state import WarehouseStateResult
from .warehouse_transitions import WarehouseStateCode

RECEIVING_WAREHOUSE_ROLES = {"storekeeper", "processing_head"}


@dataclass
class WarehouseActionPolicyResult:
    allowed: bool
    reason: str = ""
    source_facts: list[str] = field(default_factory=list)


class WarehouseActionPolicy:
    @staticmethod
    def can_create_receiving_act(
        state_result: WarehouseStateResult | None,
        *,
        has_receiving_act: bool,
        role: str,
        allow_legacy_warehouse: bool = False,
    ) -> WarehouseActionPolicyResult:
        facts = [f"role:{role or '-'}", f"has_receiving_act:{int(bool(has_receiving_act))}"]
        if role not in RECEIVING_WAREHOUSE_ROLES:
            return WarehouseActionPolicyResult(False, "role_forbidden", facts)
        if has_receiving_act:
            return WarehouseActionPolicyResult(False, "receiving_act_exists", facts)
        if not state_result:
            return WarehouseActionPolicyResult(False, "missing_state", facts)
        facts.append(f"state:{state_result.code.value}")
        if state_result.code in {
            WarehouseStateCode.RECEIVED_UNPLACED,
            WarehouseStateCode.PLACED_IN_RECEIVING,
        }:
            return WarehouseActionPolicyResult(True, "", facts)
        if allow_legacy_warehouse and state_result.code == WarehouseStateCode.UNKNOWN:
            return WarehouseActionPolicyResult(True, "legacy_warehouse_fallback", facts)
        return WarehouseActionPolicyResult(False, "state_forbidden", facts)

    @staticmethod
    def can_open_receiving_flow(
        state_result: WarehouseStateResult | None,
        *,
        role: str,
        client_view: bool,
        flow_closed: bool,
        can_create_receiving_act: bool,
        has_receiving_act: bool,
        flow_has_data: bool,
    ) -> WarehouseActionPolicyResult:
        facts = [
            f"role:{role or '-'}",
            f"client_view:{int(bool(client_view))}",
            f"flow_closed:{int(bool(flow_closed))}",
            f"has_receiving_act:{int(bool(has_receiving_act))}",
            f"flow_has_data:{int(bool(flow_has_data))}",
        ]
        if role not in RECEIVING_WAREHOUSE_ROLES or client_view:
            return WarehouseActionPolicyResult(False, "role_forbidden", facts)
        if flow_closed:
            return WarehouseActionPolicyResult(False, "flow_closed", facts)
        if state_result:
            facts.append(f"state:{state_result.code.value}")
            if state_result.code == WarehouseStateCode.STORED:
                return WarehouseActionPolicyResult(False, "state_forbidden", facts)
        allowed = bool(can_create_receiving_act or has_receiving_act or flow_has_data)
        return WarehouseActionPolicyResult(allowed, "" if allowed else "missing_flow_context", facts)

    @staticmethod
    def can_start_receiving_flow(
        state_result: WarehouseStateResult | None,
        *,
        flow_closed: bool,
        allow_legacy_warehouse: bool = False,
    ) -> WarehouseActionPolicyResult:
        facts = [f"flow_closed:{int(bool(flow_closed))}"]
        if flow_closed:
            return WarehouseActionPolicyResult(False, "flow_closed", facts)
        if not state_result:
            return WarehouseActionPolicyResult(False, "missing_state", facts)
        facts.append(f"state:{state_result.code.value}")
        if state_result.code in {
            WarehouseStateCode.RECEIVED_UNPLACED,
            WarehouseStateCode.PLACED_IN_RECEIVING,
        }:
            return WarehouseActionPolicyResult(True, "", facts)
        if allow_legacy_warehouse and state_result.code == WarehouseStateCode.UNKNOWN:
            return WarehouseActionPolicyResult(True, "legacy_warehouse_fallback", facts)
        return WarehouseActionPolicyResult(False, "state_forbidden", facts)

    @staticmethod
    def can_send_receiving_to_storage(
        state_result: WarehouseStateResult | None,
        *,
        flow_closed: bool,
        role_allowed: bool,
        not_created_count: int,
    ) -> WarehouseActionPolicyResult:
        facts = [
            f"flow_closed:{int(bool(flow_closed))}",
            f"role_allowed:{int(bool(role_allowed))}",
            f"not_created_count:{int(not_created_count or 0)}",
        ]
        if not role_allowed:
            return WarehouseActionPolicyResult(False, "role_forbidden", facts)
        if int(not_created_count or 0) <= 0:
            return WarehouseActionPolicyResult(False, "nothing_to_create", facts)
        if state_result:
            facts.append(f"state:{state_result.code.value}")
            if state_result.code == WarehouseStateCode.STORED:
                return WarehouseActionPolicyResult(False, "state_forbidden", facts)
        if not flow_closed:
            if not state_result or state_result.code not in {
                WarehouseStateCode.RECEIVED_UNPLACED,
                WarehouseStateCode.PLACED_IN_RECEIVING,
            }:
                return WarehouseActionPolicyResult(False, "flow_open", facts)
            facts.append("flow_open_but_goods_in_pr:1")
        return WarehouseActionPolicyResult(True, "", facts)

    @staticmethod
    def can_open_receiving_placement(
        state_result: WarehouseStateResult | None,
        *,
        role: str,
        act_state: str,
        signed_by_storekeeper: bool,
    ) -> WarehouseActionPolicyResult:
        facts = [
            f"role:{role or '-'}",
            f"act_state:{act_state or '-'}",
            f"signed:{int(bool(signed_by_storekeeper))}",
        ]
        if role != "storekeeper":
            return WarehouseActionPolicyResult(False, "role_forbidden", facts)
        if str(act_state or "").strip().lower() != "closed":
            return WarehouseActionPolicyResult(False, "act_not_closed", facts)
        if signed_by_storekeeper:
            return WarehouseActionPolicyResult(False, "signed", facts)
        if not state_result:
            return WarehouseActionPolicyResult(False, "missing_state", facts)
        facts.append(f"state:{state_result.code.value}")
        if state_result.code in {
            WarehouseStateCode.RECEIVED_UNPLACED,
            WarehouseStateCode.PLACED_IN_RECEIVING,
        }:
            return WarehouseActionPolicyResult(True, "", facts)
        return WarehouseActionPolicyResult(False, "state_forbidden", facts)

    @staticmethod
    def can_create_receiving_placement(
        state_result: WarehouseStateResult | None,
        *,
        has_receiving_act: bool,
    ) -> WarehouseActionPolicyResult:
        facts = [f"has_receiving_act:{int(bool(has_receiving_act))}"]
        if not has_receiving_act:
            return WarehouseActionPolicyResult(False, "receiving_act_missing", facts)
        if not state_result:
            return WarehouseActionPolicyResult(True, "missing_state_fallback", facts)
        facts.append(f"state:{state_result.code.value}")
        if state_result.code == WarehouseStateCode.STORED:
            return WarehouseActionPolicyResult(False, "state_forbidden", facts)
        return WarehouseActionPolicyResult(True, "", facts)

    @staticmethod
    def can_take_processing(
        state_result: WarehouseStateResult | None,
        *,
        role: str,
        client_view: bool,
    ) -> WarehouseActionPolicyResult:
        facts = [f"role:{role or '-'}", f"client_view:{int(bool(client_view))}"]
        if client_view or role not in {"storekeeper", "processing_head"}:
            return WarehouseActionPolicyResult(False, "role_forbidden", facts)
        if not state_result:
            return WarehouseActionPolicyResult(False, "missing_state", facts)
        facts.append(f"state:{state_result.code.value}")
        if state_result.code in {
            WarehouseStateCode.RESERVED_FOR_PROCESSING,
            WarehouseStateCode.MOVING_TO_PROCESSING,
            WarehouseStateCode.IN_PROCESSING_ZONE,
        }:
            return WarehouseActionPolicyResult(True, "", facts)
        return WarehouseActionPolicyResult(False, "state_forbidden", facts)

    @staticmethod
    def can_create_processing_placement(
        state_result: WarehouseStateResult | None,
        *,
        has_items: bool,
    ) -> WarehouseActionPolicyResult:
        facts = [f"has_items:{int(bool(has_items))}"]
        if not has_items:
            return WarehouseActionPolicyResult(False, "items_missing", facts)
        if not state_result:
            return WarehouseActionPolicyResult(True, "missing_state_fallback", facts)
        facts.append(f"state:{state_result.code.value}")
        if state_result.code == WarehouseStateCode.STORED:
            return WarehouseActionPolicyResult(False, "state_forbidden", facts)
        return WarehouseActionPolicyResult(True, "", facts)
