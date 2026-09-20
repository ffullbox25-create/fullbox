from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field

from django.db import transaction
from django.utils import timezone

from audit.models import OrderAuditEntry, log_staff_overaction
from sklad.services.warehouse_commands import WarehouseCommandService
from sklad.services.warehouse_write_path import WarehouseWritePathService

from .services import ReceivingWorkflowService


@dataclass(frozen=True)
class ReceivingCorrectionResult:
    status: str
    order_id: str
    agency_id: int
    box_codes: list[str] = field(default_factory=list)
    original_box_count: int = 0
    corrected_box_count: int = 0
    original_pallet_count: int = 0
    corrected_pallet_count: int = 0
    removed_qty: int = 0
    snapshot_ids: list[int] = field(default_factory=list)
    event_ids: list[int] = field(default_factory=list)
    placement_entry_id: int = 0
    receiving_entry_id: int = 0
    applied: bool = False


class ReceivingCorrectionService:
    CORRECTION_KIND = "phantom_receiving_boxes"

    @staticmethod
    def _authenticated_user(user):
        return user if getattr(user, "is_authenticated", False) else None

    @staticmethod
    def _normalize_codes(box_codes: list[str] | None) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for raw_code in box_codes or []:
            code = str(raw_code or "").strip()
            folded = code.casefold()
            if code and folded not in seen:
                result.append(code)
                seen.add(folded)
        return result

    @staticmethod
    def _corrected_flow_state(flow_state: dict, box_codes: list[str]) -> tuple[list[dict], list[dict]]:
        target_codes = {code.casefold() for code in box_codes}
        boxes = [
            copy.deepcopy(box)
            for box in (flow_state.get("boxes") or [])
            if isinstance(box, dict)
            and str(box.get("code") or "").strip().casefold() not in target_codes
        ]
        original_boxes = [box for box in (flow_state.get("boxes") or []) if isinstance(box, dict)]
        removed_codes = {
            str(box.get("code") or "").strip().casefold()
            for box in original_boxes
            if str(box.get("code") or "").strip().casefold() in target_codes
        }
        missing_codes = [code for code in box_codes if code.casefold() not in removed_codes]
        if missing_codes:
            raise ValueError(
                "Boxes are absent from the current receiving act: " + ", ".join(missing_codes)
            )

        pallets: list[dict] = []
        for raw_pallet in flow_state.get("pallets") or []:
            if not isinstance(raw_pallet, dict):
                continue
            pallet = copy.deepcopy(raw_pallet)
            pallet["boxes"] = [
                str(code or "").strip()
                for code in (pallet.get("boxes") or [])
                if str(code or "").strip()
                and str(code or "").strip().casefold() not in target_codes
            ]
            if pallet["boxes"] or pallet.get("items"):
                pallets.append(pallet)
        if not boxes or not pallets:
            raise ValueError("Correction would leave the receiving without boxes or pallets.")
        return boxes, pallets

    @classmethod
    @transaction.atomic
    def correct_phantom_boxes(
        cls,
        *,
        order_id: str,
        box_codes: list[str],
        reason: str,
        user=None,
        apply: bool = False,
    ) -> ReceivingCorrectionResult:
        order_key = str(order_id or "").strip()
        normalized_codes = cls._normalize_codes(box_codes)
        normalized_reason = str(reason or "").strip()
        if not order_key:
            raise ValueError("Receiving order is required.")
        if not normalized_codes:
            raise ValueError("At least one box code is required.")
        if not normalized_reason:
            raise ValueError("Correction reason is required.")

        WarehouseCommandService.acquire_receiving_order_lock(order_key)
        entries = list(
            OrderAuditEntry.objects.select_for_update()
            .filter(order_id=order_key, order_type="receiving")
            .order_by("created_at", "id")
        )
        if not entries:
            raise ValueError(f"Receiving order {order_key} was not found.")
        agency_ids = {entry.agency_id for entry in entries if entry.agency_id}
        if len(agency_ids) != 1:
            raise ValueError("Receiving order has no single unambiguous agency.")
        agency = next(entry.agency for entry in reversed(entries) if entry.agency_id)

        target_set = {code.casefold() for code in normalized_codes}
        for entry in reversed(entries):
            payload = entry.payload if isinstance(entry.payload, dict) else {}
            correction = payload.get("receiving_correction")
            if not isinstance(correction, dict) or correction.get("kind") != cls.CORRECTION_KIND:
                continue
            corrected_set = {
                str(code or "").strip().casefold()
                for code in (correction.get("box_codes") or [])
                if str(code or "").strip()
            }
            if target_set == corrected_set:
                return ReceivingCorrectionResult(
                    status="already_applied",
                    order_id=order_key,
                    agency_id=agency.id,
                    box_codes=normalized_codes,
                    corrected_box_count=len((payload.get("flow_state") or {}).get("boxes") or []),
                    corrected_pallet_count=len((payload.get("flow_state") or {}).get("pallets") or []),
                    applied=True,
                )

        flow_state = ReceivingWorkflowService.find_receiving_flow_state(entries)
        if not isinstance(flow_state, dict) or not flow_state.get("boxes") or not flow_state.get("pallets"):
            raise ValueError("Current receiving flow state is unavailable.")
        original_box_count = len(flow_state.get("boxes") or [])
        original_pallet_count = len(flow_state.get("pallets") or [])
        corrected_boxes, corrected_pallets = cls._corrected_flow_state(flow_state, normalized_codes)

        preparation = ReceivingWorkflowService.prepare_receiving_flow_completion(
            order_id=order_key,
            entries=entries,
            boxes_raw=json.dumps(corrected_boxes, ensure_ascii=False),
            pallets_raw=json.dumps(corrected_pallets, ensure_ascii=False),
        )
        if preparation.status != "ok":
            raise ValueError(
                "Corrected receiving act did not pass validation: "
                + str(preparation.reason or preparation.status)
            )
        if flow_state.get("received_at"):
            preparation.flow_state["received_at"] = flow_state.get("received_at")

        placement_source = ReceivingWorkflowService._find_document_act_entry(
            entries, "placement", "акт размещения"
        )
        receiving_source = ReceivingWorkflowService._find_document_act_entry(
            entries, "receiving", "акт приемки"
        )
        if not placement_source or not receiving_source:
            raise ValueError("Signed receiving or placement act was not found.")

        actor = cls._authenticated_user(user)
        correction_at = timezone.now().isoformat()
        correction_meta = {
            "kind": cls.CORRECTION_KIND,
            "box_codes": normalized_codes,
            "reason": normalized_reason,
            "corrected_at": correction_at,
            "corrected_by_user_id": getattr(actor, "id", None),
            "original_box_count": original_box_count,
            "corrected_box_count": len(preparation.boxes),
            "original_pallet_count": original_pallet_count,
            "corrected_pallet_count": len(preparation.pallets),
        }

        stock_result = WarehouseWritePathService.archive_phantom_receiving_boxes(
            agency=agency,
            order_id=order_key,
            box_codes=normalized_codes,
            reason=normalized_reason,
            performed_by=actor,
            apply=apply,
        )

        if not apply:
            return ReceivingCorrectionResult(
                status="dry_run",
                order_id=order_key,
                agency_id=agency.id,
                box_codes=normalized_codes,
                original_box_count=original_box_count,
                corrected_box_count=len(preparation.boxes),
                original_pallet_count=original_pallet_count,
                corrected_pallet_count=len(preparation.pallets),
                removed_qty=stock_result.removed_qty,
                snapshot_ids=stock_result.snapshot_ids,
                applied=False,
            )

        placement_payload = copy.deepcopy(placement_source.payload or {})
        placement_payload.update(
            {
                "act": "placement",
                "act_items": preparation.placement_items,
                "act_boxes": preparation.boxes,
                "act_pallets": preparation.pallets,
                "act_mismatch": preparation.has_mismatch,
                "flow_state": preparation.flow_state,
                "receiving_correction": {
                    **correction_meta,
                    "supersedes_audit_entry_id": placement_source.id,
                    "warehouse_event_ids": stock_result.event_ids,
                },
            }
        )
        placement_entry = OrderAuditEntry.objects.create(
            order_id=order_key,
            order_type="receiving",
            action="update",
            user=actor,
            agency=agency,
            description="Исправлен акт размещения: исключены ошибочно сохранённые короба",
            payload=placement_payload,
        )

        receiving_payload = copy.deepcopy(receiving_source.payload or {})
        receiving_payload.update(
            {
                "act": "receiving",
                "act_items": preparation.act_items,
                "act_units": preparation.act_units,
                "act_mismatch": preparation.has_mismatch,
                "flow_state": preparation.flow_state,
                "receiving_correction": {
                    **correction_meta,
                    "supersedes_audit_entry_id": receiving_source.id,
                    "warehouse_event_ids": stock_result.event_ids,
                    "placement_correction_entry_id": placement_entry.id,
                },
            }
        )
        receiving_entry = OrderAuditEntry.objects.create(
            order_id=order_key,
            order_type="receiving",
            action="status",
            user=actor,
            agency=agency,
            description="Исправлен акт приёмки: исключены ошибочно сохранённые короба",
            payload=receiving_payload,
        )
        log_staff_overaction(
            "update",
            user=actor,
            agency=agency,
            description=f"Корректировка приёмки {order_key}: исключены фантомные короба",
            snapshot={
                **correction_meta,
                "order_id": order_key,
                "snapshot_ids": stock_result.snapshot_ids,
                "warehouse_event_ids": stock_result.event_ids,
                "placement_correction_entry_id": placement_entry.id,
                "receiving_correction_entry_id": receiving_entry.id,
            },
        )

        return ReceivingCorrectionResult(
            status="applied",
            order_id=order_key,
            agency_id=agency.id,
            box_codes=normalized_codes,
            original_box_count=original_box_count,
            corrected_box_count=len(preparation.boxes),
            original_pallet_count=original_pallet_count,
            corrected_pallet_count=len(preparation.pallets),
            removed_qty=stock_result.removed_qty,
            snapshot_ids=stock_result.snapshot_ids,
            event_ids=stock_result.event_ids,
            placement_entry_id=placement_entry.id,
            receiving_entry_id=receiving_entry.id,
            applied=True,
        )
