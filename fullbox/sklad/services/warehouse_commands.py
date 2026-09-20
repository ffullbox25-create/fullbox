from __future__ import annotations

from dataclasses import dataclass, field

from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.utils import timezone
from reachtruck.models import MoveTask
from reachtruck.services import create_batch_move_tasks
from sklad.models import WarehouseStockSnapshot
from sklad.location_occupancy import operational_location_occupancy_message
from sku.models import Agency

from .operational_locations import require_receiving_location
from .stock_operations import OperationalStockService
from .stock_availability import StockAvailabilityService
from .warehouse_policy import WarehouseActionPolicy
from .warehouse_state import WarehouseGoodsStateResolver
from .warehouse_transitions import WarehouseStateCode
from .warehouse_write_path import WarehouseWritePathService


@dataclass
class WarehouseCommandResult:
    status: str
    state_code: WarehouseStateCode
    reason: str = ""
    source_facts: list[str] = field(default_factory=list)
    payload_update: dict = field(default_factory=dict)
    meta: dict = field(default_factory=dict)


@dataclass
class WarehouseBatchCommandResult:
    status: str
    state_code: WarehouseStateCode
    created_count: int = 0
    skipped_existing_count: int = 0
    skipped_missing_destination_count: int = 0
    total_count: int = 0
    reason: str = ""
    source_facts: list[str] = field(default_factory=list)


class WarehouseCommandService:
    _RECEIVING_MOVE_BLOCKING_STATUSES = {"created", "in_progress", "done"}
    _RECEIVING_PALLET_PLACEMENT_PERMISSION_EVENT = (
        "receiving_pallet_placement_permission"
    )

    @staticmethod
    def acquire_receiving_order_lock(order_id: str) -> None:
        order_key = str(order_id or "").strip()
        if not order_key or connection.vendor != "postgresql":
            return
        if not connection.in_atomic_block:
            raise RuntimeError("receiving_order_lock_requires_atomic")
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                [f"fullbox:receiving:{order_key}"],
            )

    @classmethod
    def _missing_receiving_pallet_location_permissions(
        cls,
        *,
        order_id: str,
        pallets,
    ) -> list[str]:
        required_by_key = {
            str(pallet.get("code") or "").strip().casefold(): str(
                pallet.get("code") or ""
            ).strip()
            for pallet in (pallets or [])
            if isinstance(pallet, dict)
            and pallet.get("sealed")
            and str(pallet.get("code") or "").strip()
        }
        if not required_by_key:
            return []

        from audit.models import OrderAuditEntry

        allowed_keys: set[str] = set()
        payloads = (
            OrderAuditEntry.objects.filter(
                order_type="receiving",
                order_id=str(order_id or "").strip(),
                payload__event=cls._RECEIVING_PALLET_PLACEMENT_PERMISSION_EVENT,
            )
            .order_by("created_at", "id")
            .values_list("payload", flat=True)
        )
        for payload in payloads:
            source = payload if isinstance(payload, dict) else {}
            pallet_code = str(source.get("pallet_code") or "").strip()
            location_code = str(source.get("receiving_location_code") or "").strip()
            if (
                pallet_code
                and location_code
                and bool(source.get("pallet_placement_allowed"))
            ):
                allowed_keys.add(pallet_code.casefold())
        return [
            required_by_key[key]
            for key in sorted(required_by_key)
            if key not in allowed_keys
        ]

    @staticmethod
    def _receiving_payload_has_legacy_warehouse_status(payload: dict | None) -> bool:
        source = payload if isinstance(payload, dict) else {}
        status_value = str(source.get("status") or source.get("submit_action") or "").strip().lower()
        status_label = str(source.get("status_label") or "").strip().lower()
        return (
            status_value in {"warehouse", "on_warehouse"}
            or "склад" in status_label
            or "ожидании поставки" in status_label
        )

    @classmethod
    def start_receiving_flow(
        cls,
        *,
        order_id: str,
        agency: Agency | None,
        role: str,
        status_payload: dict | None = None,
        flow_closed: bool,
    ) -> WarehouseCommandResult:
        payload = status_payload if isinstance(status_payload, dict) else {}
        state_result = WarehouseGoodsStateResolver.resolve_for_receiving_order(
            order_id=str(order_id or ""),
            agency=agency,
            payload=payload,
        )
        facts = list(state_result.source_facts)
        facts.append(f"role:{role or '-'}")
        facts.append(f"flow_closed:{int(bool(flow_closed))}")

        status_label = str(payload.get("status_label") or "").strip().lower()
        if "взята в работу" in status_label:
            return WarehouseCommandResult(
                status="already_in_progress",
                state_code=state_result.code,
                source_facts=facts,
                payload_update={
                    "status": "warehouse",
                    "status_label": "Взята в работу",
                },
            )

        decision = WarehouseActionPolicy.can_start_receiving_flow(
            state_result,
            flow_closed=flow_closed,
            allow_legacy_warehouse=bool(
                state_result.code == WarehouseStateCode.UNKNOWN
                and cls._receiving_payload_has_legacy_warehouse_status(payload)
            ),
        )
        facts.extend(decision.source_facts)
        if not decision.allowed:
            return WarehouseCommandResult(
                status="denied",
                state_code=state_result.code,
                reason=decision.reason,
                source_facts=facts,
            )
        return WarehouseCommandResult(
            status="started",
            state_code=state_result.code,
            source_facts=facts,
            payload_update={
                "status": "warehouse",
                "status_label": "Взята в работу",
            },
        )

    @classmethod
    def reopen_receiving_flow(
        cls,
        *,
        order_id: str,
        agency: Agency | None,
        role: str,
        status_payload: dict | None = None,
        flow_closed: bool,
        flow_closed_at: str | None = None,
    ) -> WarehouseCommandResult:
        payload = status_payload if isinstance(status_payload, dict) else {}
        state_result = WarehouseGoodsStateResolver.resolve_for_receiving_order(
            order_id=str(order_id or ""),
            agency=agency,
            payload=payload,
        )
        facts = list(state_result.source_facts)
        facts.append(f"role:{role or '-'}")
        facts.append(f"flow_closed:{int(bool(flow_closed))}")

        if role != "storekeeper":
            return WarehouseCommandResult(
                status="denied",
                state_code=state_result.code,
                reason="role_forbidden",
                source_facts=facts,
            )
        if not flow_closed:
            return WarehouseCommandResult(
                status="already_open",
                state_code=state_result.code,
                source_facts=facts,
            )

        decision = WarehouseActionPolicy.can_start_receiving_flow(
            state_result,
            flow_closed=False,
            allow_legacy_warehouse=False,
        )
        facts.extend(decision.source_facts)
        if not decision.allowed:
            return WarehouseCommandResult(
                status="denied",
                state_code=state_result.code,
                reason=decision.reason,
                source_facts=facts,
            )
        reopened_at = timezone.localtime().isoformat()
        return WarehouseCommandResult(
            status="reopened",
            state_code=state_result.code,
            source_facts=facts,
            payload_update={
                "flow_reopened": True,
                "flow_reopened_at": reopened_at,
            },
            meta={
                "order_id": str(order_id or ""),
                "flow_closed_at": flow_closed_at,
            },
        )

    @classmethod
    def send_receiving_to_storage(
        cls,
        *,
        order_id: str,
        agency: Agency | None,
        role: str,
        status_payload: dict | None = None,
        flow_closed: bool,
        not_created_count: int,
    ) -> WarehouseCommandResult:
        payload = status_payload if isinstance(status_payload, dict) else {}
        state_result = WarehouseGoodsStateResolver.resolve_for_receiving_order(
            order_id=str(order_id or ""),
            agency=agency,
            payload=payload,
        )
        facts = list(state_result.source_facts)
        facts.append(f"role:{role or '-'}")

        decision = WarehouseActionPolicy.can_send_receiving_to_storage(
            state_result,
            flow_closed=flow_closed,
            role_allowed=role in {"storekeeper", "manager", "head_manager", "director", "admin"},
            not_created_count=int(not_created_count or 0),
        )
        facts.extend(decision.source_facts)
        if not decision.allowed:
            return WarehouseCommandResult(
                status="denied",
                state_code=state_result.code,
                reason=decision.reason,
                source_facts=facts,
            )
        return WarehouseCommandResult(
            status="ready",
            state_code=state_result.code,
            source_facts=facts,
        )

    @staticmethod
    def _parse_int_value(raw) -> int:
        try:
            return int(str(raw).strip())
        except (TypeError, ValueError):
            return 0

    @classmethod
    def _normalize_zone_code(cls, raw: str | None) -> str:
        text = str(raw or "").strip().upper()
        if not text:
            return "PR"
        return text

    @classmethod
    def _normalize_receiving_move_location(cls, raw_location, fallback_payload=None) -> dict:
        source = raw_location if isinstance(raw_location, dict) else {}
        fallback = fallback_payload if isinstance(fallback_payload, dict) else {}
        zone = cls._normalize_zone_code(
            source.get("zone")
            or source.get("location")
            or fallback.get("zone")
            or fallback.get("location")
            or ""
        )
        row = cls._parse_int_value(source.get("row") or fallback.get("row")) if zone in {"MR", "OS"} else 0
        section = cls._parse_int_value(source.get("section") or fallback.get("section")) if zone == "OS" else 0
        tier = cls._parse_int_value(source.get("tier") or fallback.get("tier")) if zone == "OS" else 0
        cell = cls._parse_int_value(source.get("cell") or fallback.get("cell")) if zone == "OS" else 0
        return {
            "zone": zone,
            "row": row if zone in {"MR", "OS"} else "",
            "section": section if zone == "OS" else "",
            "tier": tier if zone == "OS" else "",
            "cell": cell if zone == "OS" else "",
        }

    @classmethod
    def _stock_move_location_label(cls, location: dict | None) -> str:
        source = location if isinstance(location, dict) else {}
        zone = cls._normalize_zone_code(source.get("zone") or "") or "PR"
        row = cls._parse_int_value(source.get("row"))
        section = cls._parse_int_value(source.get("section"))
        tier = cls._parse_int_value(source.get("tier"))
        cell = cls._parse_int_value(source.get("cell"))
        if zone == "PR":
            return "PR · Зона приемки"
        if zone == "OTG":
            return "OTG · Зона отгрузки"
        if zone == "MR":
            return f"MR · Между рядами · Ряд {row}" if row else "MR · Между рядами"
        if zone == "OS":
            if row and section and tier and cell:
                return f"OS · Ряд {row} · Секция {section} · Ярус {tier} · Ячейка {cell}"
            if row:
                return f"OS · Ряд {row}"
            return "OS · Основной склад"
        return zone

    @classmethod
    def _build_receiving_flow_payload(
        cls,
        *,
        status_payload: dict | None,
        has_mismatch: bool,
        receiving_mode: str,
        act_items: list[dict],
        flow_state: dict,
        act_units: list[dict] | None = None,
        eta_at: str = "",
        received_at: str = "",
        vehicle_number: str = "",
    ) -> dict:
        payload = dict(status_payload or {})
        if payload.get("flow_was_reopened"):
            for key in tuple(payload):
                if str(key).startswith("act_"):
                    payload.pop(key, None)
        if not payload.get("status"):
            payload["status"] = "warehouse"
        payload["status_label"] = "Товар принят (с расхождениями)" if has_mismatch else "Товар принят"
        if eta_at:
            payload["eta_at"] = eta_at
        if received_at:
            payload["received_at"] = received_at
        payload["vehicle_number"] = str(vehicle_number or "").strip()
        payload["act"] = "receiving"
        payload["act_label"] = "Акт приемки с расхождениями" if has_mismatch else "Акт приемки"
        payload["act_state"] = "closed"
        payload["act_mismatch"] = bool(has_mismatch)
        payload["receiving_mode"] = receiving_mode
        payload["act_items"] = list(act_items or [])
        if act_units:
            payload["act_units"] = list(act_units)
        payload["flow_state"] = dict(flow_state or {})
        payload["flow_closed"] = True
        payload["flow_closed_at"] = timezone.localtime().isoformat()
        payload.pop("flow_reopened", None)
        return payload

    @classmethod
    def _build_receiving_placement_payload(
        cls,
        *,
        act_payload: dict,
        receiving_mode: str,
        placement_items: list[dict],
        boxes: list[dict],
        pallets: list[dict],
        act_units: list[dict] | None = None,
    ) -> dict:
        payload = dict(act_payload or {})
        payload.pop("status", None)
        payload.pop("status_label", None)
        payload.pop("submit_action", None)
        payload["act"] = "placement"
        payload["act_label"] = "Акт размещения"
        payload["act_state"] = "closed"
        payload["receiving_mode"] = receiving_mode
        payload["act_items"] = list(placement_items or [])
        if act_units:
            payload["act_units"] = list(act_units)
        payload["act_boxes"] = list(boxes or [])
        payload["act_pallets"] = list(pallets or [])
        return payload

    @classmethod
    @transaction.atomic
    def complete_receiving_flow(
        cls,
        *,
        order_id: str,
        agency: Agency | None,
        status_payload: dict | None,
        has_mismatch: bool,
        receiving_mode: str,
        act_items: list[dict],
        placement_items: list[dict],
        boxes: list[dict],
        pallets: list[dict],
        flow_state: dict,
        act_units: list[dict] | None = None,
        eta_at: str = "",
        received_at: str = "",
        vehicle_number: str = "",
        has_closed_placement_act: bool = False,
        receiving_location_code: str = "",
        concrete_location_required: bool = False,
        performed_by=None,
    ) -> WarehouseCommandResult:
        order_key = str(order_id or "").strip()
        if not order_key or not agency:
            return WarehouseCommandResult(
                status="denied",
                state_code=WarehouseStateCode.UNKNOWN,
                reason="missing_context",
            )
        cls.acquire_receiving_order_lock(order_key)
        from billing.warehouse_services import require_completion_facts

        # Защита стоит до первого изменения остатков, чтобы прямой вызов
        # складской команды не мог закрыть приемку без факта услуг.
        require_completion_facts(
            client=agency,
            order_type="receiving",
            order_id=order_key,
        )
        act_payload = cls._build_receiving_flow_payload(
            status_payload=status_payload,
            has_mismatch=has_mismatch,
            receiving_mode=receiving_mode,
            act_items=act_items,
            flow_state=flow_state,
            act_units=act_units,
            eta_at=eta_at,
            received_at=received_at,
            vehicle_number=vehicle_number,
        )
        placement_payload = cls._build_receiving_placement_payload(
            act_payload=act_payload,
            receiving_mode=receiving_mode,
            placement_items=placement_items,
            boxes=boxes,
            pallets=pallets,
            act_units=act_units,
        )
        exact_receiving_code = str(receiving_location_code or "").strip()
        if concrete_location_required:
            if not exact_receiving_code:
                raise ValueError(
                    "Отсканируйте или выберите конкретное место приемки PR."
                )
            receiving_location = WarehouseWritePathService.concrete_movement_destination(
                warehouse_code="MSK",
                zone_code="PR",
                location_code=exact_receiving_code,
            )
            try:
                require_receiving_location(receiving_location)
            except ValidationError as exc:
                raise ValueError("; ".join(exc.messages)) from exc
            occupancy_error = operational_location_occupancy_message(
                receiving_location,
                exclude_context_type="receiving",
                exclude_context_id=order_key,
            )
            if occupancy_error:
                raise ValueError(occupancy_error)
            exact_receiving_code = str(receiving_location.location_code or "").strip()
            exact_receiving_label = str(
                receiving_location.display_name or exact_receiving_code
            ).strip()
            for target_payload in (act_payload, placement_payload):
                target_payload["receiving_concrete_location_required"] = True
                target_payload["concrete_location_version"] = 1
                target_payload["receiving_location_code"] = exact_receiving_code
                target_payload["receiving_location_label"] = exact_receiving_label
            missing_location_codes = cls._missing_receiving_pallet_location_permissions(
                order_id=order_key,
                pallets=pallets,
            )
            if missing_location_codes:
                raise ValueError(
                    "Не указано конкретное место PR для палет: "
                    + ", ".join(missing_location_codes)
                    + ". Отсканируйте место каждой закрытой палеты."
                )
        has_existing_receiving_stock = WarehouseStockSnapshot.objects.filter(
            agency=agency,
            source_context_type="receiving",
            source_context_id=order_key,
            is_archived=False,
            qty__gt=0,
        ).exists()
        coverage_before = WarehouseWritePathService.receiving_placement_coverage(
            agency=agency,
            order_id=order_key,
            placement_payload=placement_payload,
        )
        # A materialized pallet may already have left the receiving context.
        # Replacing the full placement here would put that stock on balance a
        # second time.  The incremental sync below creates only the pallets
        # that have not been materialized yet.
        if (
            not has_existing_receiving_stock
            and not coverage_before["materialized_pallet_count"]
        ):
            OperationalStockService.replace_order_placement(
                agency,
                "receiving",
                order_key,
                placement_payload,
                performed_by=performed_by,
            )
        WarehouseWritePathService.sync_receiving_placement(
            agency=agency,
            order_id=order_key,
            placement_payload=placement_payload,
            performed_by=performed_by,
            preserve_existing_snapshots=True,
            raise_on_conflicts=True,
        )
        WarehouseWritePathService.ensure_receiving_placement_complete(
            agency=agency,
            order_id=order_key,
            placement_payload=placement_payload,
        )
        state_result = WarehouseGoodsStateResolver.resolve_for_receiving_order(
            order_id=order_key,
            agency=agency,
            payload=placement_payload,
        )
        return WarehouseCommandResult(
            status="already_completed" if coverage_before["complete"] else "completed",
            state_code=state_result.code,
            source_facts=list(state_result.source_facts),
            payload_update=act_payload,
            meta={
                "placement_payload": placement_payload,
                "placement_previously_closed": bool(has_closed_placement_act),
                "already_completed": bool(coverage_before["complete"]),
            },
        )

    @classmethod
    def close_receiving_placement(
        cls,
        *,
        order_id: str,
        agency: Agency | None,
        status_payload: dict | None,
        placement_items: list[dict],
        boxes: list[dict],
        pallets: list[dict],
        has_closed_act: bool = False,
        performed_by=None,
    ) -> WarehouseCommandResult:
        order_key = str(order_id or "").strip()
        if not order_key or not agency:
            return WarehouseCommandResult(
                status="denied",
                state_code=WarehouseStateCode.UNKNOWN,
                reason="missing_context",
            )
        payload = dict(status_payload or {})
        if not payload.get("status"):
            payload["status"] = "warehouse"
        payload["status_label"] = "Товар принят и размещен на складе"
        payload["act"] = "placement"
        payload["act_label"] = "Акт размещения"
        payload["act_state"] = "closed"
        payload["act_items"] = list(placement_items or [])
        payload["act_boxes"] = list(boxes or [])
        payload["act_pallets"] = list(pallets or [])
        OperationalStockService.replace_order_placement(
            agency,
            "receiving",
            order_key,
            payload,
            performed_by=performed_by,
        )
        WarehouseWritePathService.sync_receiving_placement(
            agency=agency,
            order_id=order_key,
            placement_payload=payload,
            performed_by=performed_by,
        )
        state_result = WarehouseGoodsStateResolver.resolve_for_receiving_order(
            order_id=order_key,
            agency=agency,
            payload=payload,
        )
        return WarehouseCommandResult(
            status="closed",
            state_code=state_result.code,
            source_facts=list(state_result.source_facts),
            payload_update=payload,
            meta={"placement_previously_closed": bool(has_closed_act)},
        )

    @classmethod
    def create_receiving_putaway_tasks(
        cls,
        *,
        order_id: str,
        agency: Agency | None,
        role: str,
        placement_payload: dict | None,
        requested_by=None,
        requested_by_name: str = "",
        requested_by_role: str = "",
        destinations_by_pallet: dict[str, dict] | None = None,
        latest_moves_by_pallet: dict[str, dict] | None = None,
        flow_closed: bool = True,
        not_created_count: int = 1,
        exclude_draft_token: str = "",
    ) -> WarehouseBatchCommandResult:
        payload = placement_payload if isinstance(placement_payload, dict) else {}
        order_key = str(order_id or "").strip()
        if not order_key or not agency:
            return WarehouseBatchCommandResult(
                status="denied",
                state_code=WarehouseStateCode.UNKNOWN,
                reason="missing_context",
                source_facts=[],
            )

        has_warehouse_snapshots = WarehouseStockSnapshot.objects.filter(
            agency=agency,
            source_context_type="receiving",
            source_context_id=order_key,
            is_archived=False,
        ).exists()
        has_closed_placement_payload = (
            str(payload.get("act") or "").strip().lower() == "placement"
            and str(payload.get("act_state") or "closed").strip().lower() == "closed"
            and isinstance(payload.get("act_pallets"), list)
            and bool(payload.get("act_pallets"))
        )
        if not has_warehouse_snapshots:
            WarehouseWritePathService.sync_receiving_placement(
                agency=agency,
                order_id=order_key,
                placement_payload=payload,
                performed_by=requested_by,
            )

        state_result = WarehouseGoodsStateResolver.resolve_for_receiving_order(
            order_id=order_key,
            agency=agency,
            payload=payload,
        )
        facts = list(state_result.source_facts)
        facts.append(f"role:{role or '-'}")

        decision = WarehouseActionPolicy.can_send_receiving_to_storage(
            state_result,
            flow_closed=flow_closed,
            role_allowed=role in {"storekeeper", "manager", "head_manager", "director", "admin"},
            not_created_count=int(not_created_count or 0),
        )
        facts.extend(decision.source_facts)
        allow_legacy_putaway = (
            not decision.allowed
            and decision.reason == "state_forbidden"
            and has_closed_placement_payload
            and not has_warehouse_snapshots
        )
        allow_reopened_flow_putaway = (
            not decision.allowed
            and decision.reason == "state_forbidden"
            and bool(payload.get("flow_reopened") or payload.get("flow_was_reopened"))
            and bool(payload.get("act_pallets"))
        )
        if allow_legacy_putaway:
            facts.append("legacy_putaway_fallback:1")
        if allow_reopened_flow_putaway:
            facts.append("reopened_flow_putaway:1")
        if not decision.allowed and not allow_legacy_putaway and not allow_reopened_flow_putaway:
            return WarehouseBatchCommandResult(
                status="denied",
                state_code=state_result.code,
                reason=decision.reason,
                source_facts=facts,
            )

        placement_pallets = payload.get("act_pallets") or []
        if not isinstance(placement_pallets, list):
            placement_pallets = []

        normalized_destinations: dict[str, dict] = {}
        if isinstance(destinations_by_pallet, dict):
            for raw_pallet_code, raw_destination in destinations_by_pallet.items():
                pallet_code = str(raw_pallet_code or "").strip()
                if not pallet_code:
                    continue
                normalized_destinations[pallet_code] = cls._normalize_receiving_move_location(raw_destination)

        latest_by_pallet = latest_moves_by_pallet if isinstance(latest_moves_by_pallet, dict) else {}

        created = 0
        skipped_existing = 0
        skipped_missing_destination = 0
        total = 0
        task_specs_by_destination: dict[tuple[str, int, int, int, int], list[dict]] = {}
        reserved_os_cells = StockAvailabilityService.occupied_os_cell_keys(
            exclude_order_type="receiving",
            exclude_order_id=order_key,
            exclude_draft_token=exclude_draft_token,
        )
        pending_os_cells: set[tuple[int, int, int, int]] = set()
        for pallet in placement_pallets:
            if not isinstance(pallet, dict):
                continue
            pallet_code = str(pallet.get("code") or "").strip()
            if not pallet_code:
                continue
            if normalized_destinations and pallet_code not in normalized_destinations:
                continue
            total += 1
            latest_status = str((latest_by_pallet.get(pallet_code) or {}).get("status") or "").strip().lower()
            if latest_status in cls._RECEIVING_MOVE_BLOCKING_STATUSES:
                skipped_existing += 1
                continue
            to_location = normalized_destinations.get(
                pallet_code,
                cls._normalize_receiving_move_location(pallet.get("location"), pallet),
            )
            destination_key = (
                str(to_location.get("zone") or "PR"),
                cls._parse_int_value(to_location.get("row")),
                cls._parse_int_value(to_location.get("section")),
                cls._parse_int_value(to_location.get("tier")),
                cls._parse_int_value(to_location.get("cell")),
            )
            if destination_key == ("PR", 0, 0, 0, 0):
                skipped_missing_destination += 1
                continue
            if destination_key[0] == "OS":
                os_key = (
                    destination_key[1],
                    destination_key[2],
                    destination_key[3],
                    destination_key[4],
                )
                if os_key in reserved_os_cells or os_key in pending_os_cells:
                    skipped_existing += 1
                    continue
                pending_os_cells.add(os_key)
            from_location = {"zone": "PR", "row": "", "section": "", "tier": "", "cell": ""}
            move_payload = {
                "status": "created",
                "status_label": "Ожидает перевозки",
                "pallet_code": pallet_code,
                "from_location": from_location,
                "to_location": to_location,
                "from_label": cls._stock_move_location_label(from_location),
                "to_label": cls._stock_move_location_label(to_location),
                "receiving_order_id": order_key,
                "requested_by_name": requested_by_name,
                "requested_by_role": requested_by_role,
                "pick_mode": "full",
                "requested_qty": "",
                "requested_sku": "",
                "requested_barcodes": [],
                "requested_goods_type": "",
                "available_qty": "",
                "processing_order_id": "",
            }
            task_specs_by_destination.setdefault(destination_key, []).append(
                {
                    "pallet_code": pallet_code,
                    "destination": dict(to_location),
                    "description": f"Задание на перемещение паллеты {pallet_code} на склад",
                    "payload": move_payload,
                }
            )

        for destination_key, task_specs in task_specs_by_destination.items():
            if not task_specs:
                continue
            destination = {
                "zone": destination_key[0],
                "row": destination_key[1] or "",
                "section": destination_key[2] or "",
                "tier": destination_key[3] or "",
                "cell": destination_key[4] or "",
            }
            _move_request, move_ids = create_batch_move_tasks(
                context_type="receiving",
                context_id=order_key,
                agency=agency,
                user=requested_by if getattr(requested_by, "is_authenticated", False) else None,
                requested_by_name=requested_by_name,
                requested_by_role=requested_by_role or "",
                destination=destination,
                comment=f"Автозапрос перемещения паллет по приемке #{order_key}",
                task_specs=task_specs,
            )
            for task_spec, move_id in zip(task_specs, move_ids):
                pallet_code = str(task_spec.get("pallet_code") or "").strip()
                destination_payload = task_spec.get("destination") or {}
                try:
                    operation = WarehouseWritePathService.request_putaway_for_receiving(
                        agency=agency,
                        order_id=order_key,
                        container_codes=[pallet_code],
                        destination_zone_code=str(destination_payload.get("zone") or "OS"),
                        destination_row_no=cls._parse_int_value(destination_payload.get("row")),
                        destination_section_no=cls._parse_int_value(destination_payload.get("section")),
                        destination_tier_no=cls._parse_int_value(destination_payload.get("tier")),
                        destination_cell_no=cls._parse_int_value(destination_payload.get("cell")),
                        requested_by=requested_by if getattr(requested_by, "is_authenticated", False) else None,
                        requested_by_role=role or "storekeeper",
                        source_document_type="stock_move",
                        source_document_id=move_id,
                        exclude_draft_token=exclude_draft_token,
                    )
                except ValueError:
                    continue
                warehouse_task = operation.tasks.order_by("id").first()
                if warehouse_task:
                    warehouse_task.payload = {
                        **dict(warehouse_task.payload or {}),
                        "legacy_move_id": move_id,
                        "legacy_move_task_id": warehouse_task.id,
                        "pallet_code": pallet_code,
                    }
                    warehouse_task.save(update_fields=["payload", "updated_at"])
                move_task = MoveTask.objects.filter(legacy_order_id=move_id).first()
                if move_task:
                    move_task.payload = {
                        **dict(move_task.payload or {}),
                        "warehouse_operation_id": operation.id,
                        "warehouse_operation_task_id": warehouse_task.id if warehouse_task else "",
                    }
                    move_task.save(update_fields=["payload", "updated_at"])
            created += len(move_ids)

        return WarehouseBatchCommandResult(
            status="created" if created > 0 else "noop",
            state_code=state_result.code,
            created_count=created,
            skipped_existing_count=skipped_existing,
            skipped_missing_destination_count=skipped_missing_destination,
            total_count=total,
            source_facts=facts,
        )

    @classmethod
    def open_receiving_placement(
        cls,
        *,
        order_id: str,
        agency: Agency | None,
        role: str,
        act_state: str,
        signed_by_storekeeper: bool,
        status_payload: dict | None = None,
    ) -> WarehouseCommandResult:
        payload = status_payload if isinstance(status_payload, dict) else {}
        state_result = WarehouseGoodsStateResolver.resolve_for_receiving_order(
            order_id=str(order_id or ""),
            agency=agency,
            payload=payload,
        )
        facts = list(state_result.source_facts)
        facts.append(f"role:{role or '-'}")

        normalized_act_state = str(act_state or "").strip().lower()
        if normalized_act_state == "open":
            return WarehouseCommandResult(
                status="already_open",
                state_code=state_result.code,
                source_facts=facts,
                payload_update={
                    "status": "warehouse",
                    "status_label": "Размещение на складе",
                    "act": "placement",
                    "act_label": "Акт размещения",
                    "act_state": "open",
                },
            )

        decision = WarehouseActionPolicy.can_open_receiving_placement(
            state_result,
            role=role,
            act_state=normalized_act_state or "closed",
            signed_by_storekeeper=bool(signed_by_storekeeper),
        )
        facts.extend(decision.source_facts)
        if not decision.allowed:
            return WarehouseCommandResult(
                status="denied",
                state_code=state_result.code,
                reason=decision.reason,
                source_facts=facts,
            )

        if agency:
            OperationalStockService.clear_order_placement(
                agency,
                "receiving",
                str(order_id or ""),
            )
            WarehouseWritePathService.clear_receiving_context(
                agency=agency,
                order_id=str(order_id or ""),
            )
        return WarehouseCommandResult(
            status="opened",
            state_code=state_result.code,
            source_facts=facts,
            payload_update={
                "status": "warehouse",
                "status_label": "Размещение на складе",
                "act": "placement",
                "act_label": "Акт размещения",
                "act_state": "open",
            },
        )

    @classmethod
    def take_processing(
        cls,
        *,
        order_id: str,
        agency: Agency | None,
        role: str,
        status_payload: dict | None = None,
        started_by=None,
    ) -> WarehouseCommandResult:
        payload = status_payload if isinstance(status_payload, dict) else {}
        state_result = WarehouseGoodsStateResolver.resolve_for_processing_order(
            order_id=str(order_id or ""),
            agency=agency,
            payload=payload,
        )
        facts = list(state_result.source_facts)
        facts.append(f"role:{role or '-'}")

        if state_result.code == WarehouseStateCode.PROCESSING_IN_PROGRESS:
            return WarehouseCommandResult(
                status="already_in_progress",
                state_code=state_result.code,
                source_facts=facts,
            )

        decision = WarehouseActionPolicy.can_take_processing(
            state_result,
            role=role,
            client_view=False,
        )
        facts.extend(decision.source_facts)
        if not decision.allowed:
            return WarehouseCommandResult(
                status="denied",
                state_code=state_result.code,
                reason=decision.reason,
                source_facts=facts,
            )

        if agency:
            WarehouseWritePathService.start_processing_if_ready(
                agency=agency,
                order_id=str(order_id or ""),
                started_by=started_by,
                started_by_role=role,
            )
        return WarehouseCommandResult(
            status="started",
            state_code=state_result.code,
            source_facts=facts,
        )
