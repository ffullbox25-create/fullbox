from __future__ import annotations

from dataclasses import dataclass, field

from django.db.models import Q

from audit.models import OrderAuditEntry
from logistics.models import LogisticsTrip, LogisticsTripOrder
from reachtruck.models import MoveTask
from shipping.models import ShippingOrder
from shipping.packing import _shipping_delivered_boxes
from sklad.models import WarehouseOperation, WarehouseReserve, WarehouseStockSnapshot

from .warehouse_transitions import WarehouseStateCode


@dataclass
class WarehouseMovementResult:
    has_active_tasks: bool
    active_task_count: int
    done_task_count: int
    blocked_task_count: int
    destination_zone: str
    operation_kind: str
    task_ids: list[str] = field(default_factory=list)


@dataclass
class WarehouseStateResult:
    code: WarehouseStateCode
    label_default: str
    label_client: str
    label_storekeeper: str
    label_logistician: str
    label_processing: str
    next_step_default: str = ""
    next_step_client: str = ""
    next_step_storekeeper: str = ""
    next_step_logistician: str = ""
    next_step_processing: str = ""
    source_facts: list[str] = field(default_factory=list)
    movement_summary: dict | None = None
    is_terminal: bool = False
    is_ready_for_next_step: bool = False

    def label_for(self, audience: str = "default") -> str:
        normalized = str(audience or "default").strip().lower()
        if normalized == "client":
            return self.label_client
        if normalized == "storekeeper":
            return self.label_storekeeper
        if normalized == "logistician":
            return self.label_logistician
        if normalized == "processing":
            return self.label_processing
        return self.label_default

    def next_step_for(self, audience: str = "default") -> str:
        normalized = str(audience or "default").strip().lower()
        if normalized == "client":
            return self.next_step_client
        if normalized == "storekeeper":
            return self.next_step_storekeeper
        if normalized == "logistician":
            return self.next_step_logistician
        if normalized == "processing":
            return self.next_step_processing
        return self.next_step_default

    def with_overrides(self, **overrides) -> "WarehouseStateResult":
        data = {
            "code": self.code,
            "label_default": self.label_default,
            "label_client": self.label_client,
            "label_storekeeper": self.label_storekeeper,
            "label_logistician": self.label_logistician,
            "label_processing": self.label_processing,
            "next_step_default": self.next_step_default,
            "next_step_client": self.next_step_client,
            "next_step_storekeeper": self.next_step_storekeeper,
            "next_step_logistician": self.next_step_logistician,
            "next_step_processing": self.next_step_processing,
            "source_facts": list(self.source_facts),
            "movement_summary": dict(self.movement_summary or {}),
            "is_terminal": self.is_terminal,
            "is_ready_for_next_step": self.is_ready_for_next_step,
        }
        data.update(overrides)
        return WarehouseStateResult(**data)


def _shipping_task_matches_order(order: ShippingOrder, payload: dict) -> bool:
    if not isinstance(payload, dict):
        return False
    order_number = str(order.number or "").strip()
    payload_number = str(payload.get("shipping_order_id") or payload.get("order_id") or "").strip()
    if payload_number and payload_number == order_number:
        return True
    payload_pk = payload.get("shipping_order_pk") or payload.get("order_pk")
    return str(payload_pk or "").strip() == str(order.pk)


def _active_trip_status_for_shipping_order(order: ShippingOrder) -> str:
    link = (
        LogisticsTripOrder.objects.filter(
            shipping_order=order,
            trip__status__in=[
                LogisticsTrip.STATUS_DRAFT,
                LogisticsTrip.STATUS_PLANNED,
                LogisticsTrip.STATUS_LOADING,
                LogisticsTrip.STATUS_DEPARTED,
                LogisticsTrip.STATUS_COMPLETED,
            ],
        )
        .select_related("trip")
        .order_by("-trip__created_at", "-id")
        .first()
    )
    if not link or not link.trip:
        return ""
    return str(link.trip.status or "").strip().lower()


class WarehouseMovementResolver:
    @classmethod
    def active_for_shipping_order(cls, order: ShippingOrder) -> WarehouseMovementResult:
        tasks = (
            MoveTask.objects.filter(
                request__agency=order.agency,
                to_zone="OTG",
            )
            .order_by("id")
        )
        active = 0
        done = 0
        blocked = 0
        task_ids: list[str] = []
        for task in tasks:
            payload = task.payload if isinstance(task.payload, dict) else {}
            if not _shipping_task_matches_order(order, payload):
                continue
            task_id = str(task.legacy_order_id or "").strip()
            if task_id:
                task_ids.append(task_id)
            if task.status in {MoveTask.STATUS_CREATED, MoveTask.STATUS_IN_PROGRESS}:
                active += 1
            elif task.status == MoveTask.STATUS_DONE:
                done += 1
            elif task.status in {MoveTask.STATUS_FAILED, MoveTask.STATUS_CANCELED}:
                blocked += 1
        return WarehouseMovementResult(
            has_active_tasks=active > 0,
            active_task_count=active,
            done_task_count=done,
            blocked_task_count=blocked,
            destination_zone="OTG",
            operation_kind="shipping_otg_move",
            task_ids=task_ids,
        )

    @classmethod
    def active_for_shipping_orders(cls, orders) -> dict[int, WarehouseMovementResult]:
        normalized_orders = {
            int(order.pk): order
            for order in (orders or [])
            if getattr(order, "pk", None) and getattr(order, "agency_id", None)
        }
        tasks_by_order_id = {order_id: [] for order_id in normalized_orders}
        if normalized_orders:
            order_ids_by_number: dict[tuple[int, str], set[int]] = {}
            order_ids_by_pk: dict[tuple[int, str], set[int]] = {}
            for order_id, order in normalized_orders.items():
                agency_id = int(order.agency_id)
                order_number = str(order.number or "").strip()
                if order_number:
                    order_ids_by_number.setdefault((agency_id, order_number), set()).add(order_id)
                order_ids_by_pk.setdefault((agency_id, str(order_id)), set()).add(order_id)

            tasks = (
                MoveTask.objects.filter(
                    request__agency_id__in={int(order.agency_id) for order in normalized_orders.values()},
                    to_zone="OTG",
                )
                .select_related("request")
                .order_by("id")
            )
            for task in tasks:
                payload = task.payload if isinstance(task.payload, dict) else {}
                agency_id = int(task.request.agency_id)
                payload_number = str(payload.get("shipping_order_id") or payload.get("order_id") or "").strip()
                payload_pk = str(payload.get("shipping_order_pk") or payload.get("order_pk") or "").strip()
                matched_order_ids: set[int] = set()
                if payload_number:
                    matched_order_ids.update(order_ids_by_number.get((agency_id, payload_number), set()))
                if payload_pk:
                    matched_order_ids.update(order_ids_by_pk.get((agency_id, payload_pk), set()))
                for order_id in matched_order_ids:
                    tasks_by_order_id[order_id].append(task)

        results: dict[int, WarehouseMovementResult] = {}
        for order_id in normalized_orders:
            active = 0
            done = 0
            blocked = 0
            task_ids: list[str] = []
            for task in tasks_by_order_id[order_id]:
                task_id = str(task.legacy_order_id or "").strip()
                if task_id:
                    task_ids.append(task_id)
                if task.status in {MoveTask.STATUS_CREATED, MoveTask.STATUS_IN_PROGRESS}:
                    active += 1
                elif task.status == MoveTask.STATUS_DONE:
                    done += 1
                elif task.status in {MoveTask.STATUS_FAILED, MoveTask.STATUS_CANCELED}:
                    blocked += 1
            results[order_id] = WarehouseMovementResult(
                has_active_tasks=active > 0,
                active_task_count=active,
                done_task_count=done,
                blocked_task_count=blocked,
                destination_zone="OTG",
                operation_kind="shipping_otg_move",
                task_ids=task_ids,
            )
        return results

    @classmethod
    def active_for_receiving_order(cls, *, agency, order_id: str) -> WarehouseMovementResult:
        order_key = str(order_id or "").strip()
        if not agency or not order_key:
            return WarehouseMovementResult(
                has_active_tasks=False,
                active_task_count=0,
                done_task_count=0,
                blocked_task_count=0,
                destination_zone="OS",
                operation_kind="receiving_putaway",
                task_ids=[],
            )
        tasks = (
            MoveTask.objects.filter(
                request__agency=agency,
                request__context_type="receiving",
                request__context_id=order_key,
            )
            .order_by("id")
        )
        active = 0
        done = 0
        blocked = 0
        task_ids: list[str] = []
        for task in tasks:
            task_id = str(task.legacy_order_id or "").strip()
            if task_id:
                task_ids.append(task_id)
            if task.status in {MoveTask.STATUS_CREATED, MoveTask.STATUS_IN_PROGRESS}:
                active += 1
            elif task.status == MoveTask.STATUS_DONE:
                done += 1
            elif task.status in {MoveTask.STATUS_FAILED, MoveTask.STATUS_CANCELED}:
                blocked += 1
        return WarehouseMovementResult(
            has_active_tasks=active > 0,
            active_task_count=active,
            done_task_count=done,
            blocked_task_count=blocked,
            destination_zone="OS",
            operation_kind="receiving_putaway",
            task_ids=task_ids,
        )

    @classmethod
    def active_for_receiving_orders(cls, agency_by_order_id) -> dict[str, WarehouseMovementResult]:
        normalized_agencies = {
            str(order_id or "").strip(): agency
            for order_id, agency in (agency_by_order_id or {}).items()
            if str(order_id or "").strip()
        }
        lookup_keys = {
            (int(agency.pk), order_id)
            for order_id, agency in normalized_agencies.items()
            if agency and getattr(agency, "pk", None)
        }
        tasks_by_key = {key: [] for key in lookup_keys}
        if lookup_keys:
            agency_ids = {agency_id for agency_id, _order_id in lookup_keys}
            order_ids = {order_id for _agency_id, order_id in lookup_keys}
            tasks = (
                MoveTask.objects.filter(
                    request__agency_id__in=agency_ids,
                    request__context_type="receiving",
                    request__context_id__in=order_ids,
                )
                .select_related("request")
                .order_by("id")
            )
            for task in tasks:
                key = (int(task.request.agency_id), str(task.request.context_id or "").strip())
                if key in tasks_by_key:
                    tasks_by_key[key].append(task)

        results = {}
        for order_id, agency in normalized_agencies.items():
            key = (
                int(agency.pk),
                order_id,
            ) if agency and getattr(agency, "pk", None) else None
            active = 0
            done = 0
            blocked = 0
            task_ids: list[str] = []
            for task in tasks_by_key.get(key, []):
                task_id = str(task.legacy_order_id or "").strip()
                if task_id:
                    task_ids.append(task_id)
                if task.status in {MoveTask.STATUS_CREATED, MoveTask.STATUS_IN_PROGRESS}:
                    active += 1
                elif task.status == MoveTask.STATUS_DONE:
                    done += 1
                elif task.status in {MoveTask.STATUS_FAILED, MoveTask.STATUS_CANCELED}:
                    blocked += 1
            results[order_id] = WarehouseMovementResult(
                has_active_tasks=active > 0,
                active_task_count=active,
                done_task_count=done,
                blocked_task_count=blocked,
                destination_zone="OS",
                operation_kind="receiving_putaway",
                task_ids=task_ids,
            )
        return results

    @classmethod
    def active_for_processing_order(cls, *, agency, order_id: str) -> WarehouseMovementResult:
        order_key = str(order_id or "").strip()
        if not agency or not order_key:
            return WarehouseMovementResult(
                has_active_tasks=False,
                active_task_count=0,
                done_task_count=0,
                blocked_task_count=0,
                destination_zone="OBR",
                operation_kind="processing_move",
                task_ids=[],
            )
        tasks = (
            MoveTask.objects.filter(
                request__agency=agency,
                request__context_type="processing",
                request__context_id=order_key,
                to_zone="OBR",
            )
            .order_by("id")
        )
        active = 0
        done = 0
        blocked = 0
        task_ids: list[str] = []
        for task in tasks:
            task_id = str(task.legacy_order_id or task.id or "").strip()
            if task_id:
                task_ids.append(task_id)
            if task.status in {MoveTask.STATUS_CREATED, MoveTask.STATUS_IN_PROGRESS}:
                active += 1
            elif task.status == MoveTask.STATUS_DONE:
                done += 1
            elif task.status in {MoveTask.STATUS_FAILED, MoveTask.STATUS_CANCELED}:
                blocked += 1
        return WarehouseMovementResult(
            has_active_tasks=active > 0,
            active_task_count=active,
            done_task_count=done,
            blocked_task_count=blocked,
            destination_zone="OBR",
            operation_kind="processing_move",
            task_ids=task_ids,
        )


class WarehouseGoodsStateResolver:
    _SHIPPING_STATE_PRIORITY = [
        WarehouseStateCode.LOADED_TO_VEHICLE,
        WarehouseStateCode.LOADING_IN_PROGRESS,
        WarehouseStateCode.ASSIGNED_TO_TRIP,
        WarehouseStateCode.READY_FOR_LOADING,
        WarehouseStateCode.PALLETIZING,
        WarehouseStateCode.IN_OTG,
        WarehouseStateCode.MOVING_TO_OTG,
        WarehouseStateCode.RESERVED_FOR_SHIPPING,
        WarehouseStateCode.STORED,
        WarehouseStateCode.PLACED_AFTER_PROCESSING,
        WarehouseStateCode.PLACED_IN_RECEIVING,
        WarehouseStateCode.RECEIVED_UNPLACED,
        WarehouseStateCode.UNKNOWN,
    ]
    _PROCESSING_STATE_PRIORITY = [
        WarehouseStateCode.PROCESSING_IN_PROGRESS,
        WarehouseStateCode.IN_PROCESSING_ZONE,
        WarehouseStateCode.MOVING_TO_PROCESSING,
        WarehouseStateCode.RESERVED_FOR_PROCESSING,
        WarehouseStateCode.PLACED_AFTER_PROCESSING,
        WarehouseStateCode.STORED,
        WarehouseStateCode.PLACED_IN_RECEIVING,
        WarehouseStateCode.RECEIVED_UNPLACED,
        WarehouseStateCode.UNKNOWN,
    ]
    _RECEIVING_STATE_PRIORITY = [
        WarehouseStateCode.PLACED_IN_RECEIVING,
        WarehouseStateCode.RECEIVED_UNPLACED,
        WarehouseStateCode.STORED,
        WarehouseStateCode.UNKNOWN,
    ]

    @classmethod
    def resolve_for_shipping_order(
        cls,
        order: ShippingOrder,
        *,
        trip_status: str | None = None,
        movement: WarehouseMovementResult | None = None,
    ) -> WarehouseStateResult:
        normalized_status = str(order.status or "").strip().lower()
        normalized_trip_status = str(trip_status or "").strip().lower() or _active_trip_status_for_shipping_order(order)
        movement = movement or WarehouseMovementResolver.active_for_shipping_order(order)
        source_facts = [f"shipping_status:{normalized_status or '-'}"]
        if normalized_trip_status:
            source_facts.append(f"trip_status:{normalized_trip_status}")
        if movement.active_task_count:
            source_facts.append(f"otg_active_tasks:{movement.active_task_count}")
        if movement.done_task_count:
            source_facts.append(f"otg_done_tasks:{movement.done_task_count}")

        # The physical stock may remain in OTG after cancellation until a
        # storekeeper returns it to storage. That location is still important,
        # but it must not override the terminal status of the shipping order.
        if normalized_status == ShippingOrder.STATUS_CANCELED:
            return cls._result(WarehouseStateCode.CANCELED, "Отменена", source_facts, movement, is_terminal=True)

        snapshots = cls._shipping_snapshots(order)
        if snapshots:
            dominant_code = cls._dominant_state_code(
                [snapshot.warehouse_state_code for snapshot in snapshots],
                priority=cls._SHIPPING_STATE_PRIORITY,
            )
            source_facts.append(f"shipping_snapshots:{len(snapshots)}")
            source_facts.append(f"warehouse_state:{dominant_code.value}")
            if dominant_code == WarehouseStateCode.LOADED_TO_VEHICLE:
                return cls._result(
                    dominant_code,
                    "Загружено в машину",
                    source_facts,
                    movement,
                    is_ready_for_next_step=True,
                )
            if dominant_code in {WarehouseStateCode.LOADING_IN_PROGRESS, WarehouseStateCode.ASSIGNED_TO_TRIP}:
                return cls._result(
                    dominant_code,
                    "Подготовка к рейсу",
                    source_facts,
                    movement,
                    is_ready_for_next_step=True,
                )
            if dominant_code == WarehouseStateCode.READY_FOR_LOADING:
                return cls._result(
                    dominant_code,
                    "Подготовлена складом, ожидает логиста",
                    source_facts,
                    movement,
                    is_ready_for_next_step=True,
                )
            if dominant_code == WarehouseStateCode.PALLETIZING:
                return cls._result(
                    dominant_code,
                    "Паллетизация отгрузки",
                    source_facts,
                    movement,
                    is_ready_for_next_step=True,
                )
            if dominant_code == WarehouseStateCode.IN_OTG:
                return cls._result(
                    dominant_code,
                    "Товар доставлен в OTG, ожидает паллетизации",
                    source_facts,
                    movement,
                    is_ready_for_next_step=True,
                )
            if dominant_code == WarehouseStateCode.MOVING_TO_OTG:
                return cls._result(
                    dominant_code,
                    "Доставка в зону отгрузки (ричтрак)",
                    source_facts,
                    movement,
                )
            if dominant_code == WarehouseStateCode.RESERVED_FOR_SHIPPING:
                if normalized_status == ShippingOrder.STATUS_PICKING and movement.has_active_tasks:
                    return cls._result(
                        WarehouseStateCode.MOVING_TO_OTG,
                        "Доставка в зону отгрузки (ричтрак)",
                        source_facts,
                        movement,
                    )
                reserved_label = "Зарезервирована под отгрузку"
                if normalized_status == ShippingOrder.STATUS_STOREKEEPER_ACCEPTED:
                    reserved_label = "Принята в работу складом"
                elif normalized_status == ShippingOrder.STATUS_RESERVED:
                    reserved_label = "Согласована и передана в работу кладовщику"
                elif normalized_status == ShippingOrder.STATUS_SUBMITTED:
                    reserved_label = "На согласовании менеджера"
                return cls._result(
                    dominant_code,
                    reserved_label,
                    source_facts,
                    movement,
                )

        if normalized_status == ShippingOrder.STATUS_SHIPPED:
            return cls._result(WarehouseStateCode.SHIPPED, "Отгружена", source_facts, movement, is_terminal=True)
        if normalized_status == ShippingOrder.STATUS_PARTIAL:
            return cls._result(
                WarehouseStateCode.PARTIALLY_SHIPPED,
                "Отгружена частично",
                source_facts,
                movement,
                is_terminal=True,
            )
        if normalized_status == ShippingOrder.STATUS_PACKED:
            if normalized_trip_status == LogisticsTrip.STATUS_DEPARTED:
                return cls._result(
                    WarehouseStateCode.LOADED_TO_VEHICLE,
                    "Загружено в машину",
                    source_facts,
                    movement,
                    is_ready_for_next_step=True,
                )
            if normalized_trip_status == LogisticsTrip.STATUS_LOADING:
                return cls._result(
                    WarehouseStateCode.LOADING_IN_PROGRESS,
                    "Подготовка к рейсу",
                    source_facts,
                    movement,
                    is_ready_for_next_step=True,
                )
            if normalized_trip_status in {LogisticsTrip.STATUS_DRAFT, LogisticsTrip.STATUS_PLANNED}:
                return cls._result(
                    WarehouseStateCode.ASSIGNED_TO_TRIP,
                    "Подготовка к рейсу",
                    source_facts,
                    movement,
                    is_ready_for_next_step=True,
                )
            return cls._result(
                WarehouseStateCode.READY_FOR_LOADING,
                "Подготовлена складом, ожидает логиста",
                source_facts,
                movement,
                is_ready_for_next_step=True,
            )
        if normalized_status == ShippingOrder.STATUS_PICKING:
            delivered_boxes = _shipping_delivered_boxes(order)
            if delivered_boxes:
                source_facts.append(f"otg_delivered_boxes:{len(delivered_boxes)}")
                return cls._result(
                    WarehouseStateCode.IN_OTG,
                    "Товар доставлен в OTG, ожидает паллетизации",
                    source_facts,
                    movement,
                    is_ready_for_next_step=True,
                )
            return cls._result(
                WarehouseStateCode.MOVING_TO_OTG,
                "Доставка в зону отгрузки (ричтрак)",
                source_facts,
                movement,
            )
        if normalized_status == ShippingOrder.STATUS_STOREKEEPER_ACCEPTED:
            return cls._result(
                WarehouseStateCode.RESERVED_FOR_SHIPPING,
                "Принята в работу складом",
                source_facts,
                movement,
            )
        if normalized_status == ShippingOrder.STATUS_RESERVED:
            return cls._result(
                WarehouseStateCode.RESERVED_FOR_SHIPPING,
                "Согласована и передана в работу кладовщику",
                source_facts,
                movement,
            )
        if normalized_status == ShippingOrder.STATUS_SUBMITTED:
            return cls._result(
                WarehouseStateCode.RESERVED_FOR_SHIPPING,
                "На согласовании менеджера",
                source_facts,
                movement,
            )
        if normalized_status == ShippingOrder.STATUS_DRAFT:
            return cls._result(
                WarehouseStateCode.UNKNOWN,
                "Черновик клиента",
                source_facts,
                movement,
            )
        return cls._result(
            WarehouseStateCode.UNKNOWN,
            order.get_status_display() or normalized_status or "-",
            source_facts,
            movement,
        )

    @classmethod
    def resolve_for_processing_order(
        cls,
        *,
        order_id: str,
        agency=None,
        payload: dict | None = None,
    ) -> WarehouseStateResult:
        order_key = str(order_id or "").strip()
        fallback_payload = payload if isinstance(payload, dict) else {}
        source_facts = [f"processing_order:{order_key or '-'}"]
        movement = WarehouseMovementResolver.active_for_processing_order(
            agency=agency,
            order_id=order_key,
        )
        if movement.active_task_count:
            source_facts.append(f"processing_active_tasks:{movement.active_task_count}")
        if movement.done_task_count:
            source_facts.append(f"processing_done_tasks:{movement.done_task_count}")
        if movement.blocked_task_count:
            source_facts.append(f"processing_blocked_tasks:{movement.blocked_task_count}")
        snapshots = cls._processing_snapshots(agency=agency, order_id=order_key)
        payload_status_value = str(
            fallback_payload.get("status") or fallback_payload.get("submit_action") or ""
        ).strip().lower()
        payload_status_label = str(fallback_payload.get("status_label") or "").strip().lower()
        payload_stage_value = str(fallback_payload.get("processing_stage") or "").strip().lower()
        if (
            payload_status_value in {"cancelled", "canceled"}
            or payload_stage_value in {"cancelled", "canceled"}
            or "отменен" in payload_status_label
        ):
            source_facts.append("processing_payload_cancelled")
            return cls._result(
                WarehouseStateCode.CANCELED,
                "Отменена",
                source_facts,
                movement,
                is_terminal=True,
            )
        waiting_for_manager_approval = (
            payload_status_value in {"sent_unconfirmed", "send", "submitted"}
            or "подтверждени" in payload_status_label
        )
        placement_act_closed = (
            str(fallback_payload.get("act") or "").strip().lower() == "placement"
            and str(fallback_payload.get("act_state") or "closed").strip().lower() == "closed"
        )
        payload_processing_stage = str(fallback_payload.get("processing_stage") or "").strip().lower()
        obr_delivery_act_closed = placement_act_closed and payload_processing_stage == "obr_arrived"
        processing_flow_closed = bool(fallback_payload.get("flow_closed")) or (
            placement_act_closed and not obr_delivery_act_closed
        )
        if snapshots:
            dominant_code = cls._dominant_state_code(
                [snapshot.warehouse_state_code for snapshot in snapshots],
                priority=cls._PROCESSING_STATE_PRIORITY,
            )
            source_facts.append(f"processing_snapshots:{len(snapshots)}")
            source_facts.append(f"warehouse_state:{dominant_code.value}")
            if processing_flow_closed and dominant_code != WarehouseStateCode.STORED:
                source_facts.append("processing_closed_in_obr")
                return cls._result(
                    WarehouseStateCode.PLACED_AFTER_PROCESSING,
                    "Обработка завершена",
                    source_facts,
                    movement,
                    is_ready_for_next_step=True,
                )
            if movement.has_active_tasks and dominant_code in {
                WarehouseStateCode.RESERVED_FOR_PROCESSING,
                WarehouseStateCode.MOVING_TO_PROCESSING,
            }:
                source_facts.append("processing_move_task_override")
                return cls._result(
                    WarehouseStateCode.MOVING_TO_PROCESSING,
                    "Доставка в зону обработки (ричтрак)",
                    source_facts,
                    movement,
                    next_step_default="Дождаться доставки в зону обработки",
                    next_step_processing="Дождаться доставки в зону обработки",
                )
            if dominant_code == WarehouseStateCode.PROCESSING_IN_PROGRESS:
                return cls._result(
                    dominant_code,
                    "Товар в обработке",
                    source_facts,
                    movement,
                    next_step_default="Завершить обработку и подготовить размещение",
                    next_step_processing="Завершить обработку и подготовить размещение",
                )
            if dominant_code == WarehouseStateCode.IN_PROCESSING_ZONE:
                return cls._result(
                    dominant_code,
                    "Товар в зоне обработки, ожидает старта",
                    source_facts,
                    movement,
                    processing_label="Ожидает начала обработки",
                    next_step_default="Начать обработку",
                    next_step_processing="Начать обработку",
                )
            if dominant_code == WarehouseStateCode.MOVING_TO_PROCESSING:
                return cls._result(
                    dominant_code,
                    "Доставка в зону обработки (ричтрак)",
                    source_facts,
                    movement,
                    next_step_default="Дождаться доставки в зону обработки",
                    next_step_processing="Дождаться доставки в зону обработки",
                )
            if dominant_code == WarehouseStateCode.RESERVED_FOR_PROCESSING:
                if waiting_for_manager_approval:
                    source_facts.append("processing_reserved_waiting_manager_approval")
                    return cls._result(
                        WarehouseStateCode.UNKNOWN,
                        "Ждет подтверждения",
                        source_facts,
                        movement,
                        next_step_default="Дождаться подтверждения заявки менеджером",
                    )
                return cls._result(
                    dominant_code,
                    "Передано в обработку",
                    source_facts,
                    movement,
                    storekeeper_label="Ожидает доставки в зону обработки",
                    processing_label="Ожидает доставки в зону обработки",
                    next_step_default="Передать товар ричтраку в зону обработки",
                    next_step_storekeeper="Создать или выполнить доставку в зону обработки",
                    next_step_processing="Дождаться доставки в зону обработки",
                )
            if dominant_code == WarehouseStateCode.PLACED_AFTER_PROCESSING:
                return cls._result(
                    dominant_code,
                    "Обработка завершена",
                    source_facts,
                    movement,
                    next_step_default="Разместить товар на складе или передать в следующий процесс",
                    is_ready_for_next_step=True,
                )
            if dominant_code == WarehouseStateCode.STORED:
                processing_label = "Размещение завершено"
                next_step_processing = "Размещение завершено, товар уже на складе"
                fallback_result = cls._fallback_processing_result(
                    payload=fallback_payload,
                    source_facts=[*source_facts, "stored_processing_fallback"],
                    movement=movement,
                )
                if fallback_result.code in {
                    WarehouseStateCode.RESERVED_FOR_PROCESSING,
                    WarehouseStateCode.MOVING_TO_PROCESSING,
                    WarehouseStateCode.IN_PROCESSING_ZONE,
                    WarehouseStateCode.PROCESSING_IN_PROGRESS,
                }:
                    processing_label = fallback_result.label_for("processing") or processing_label
                    next_step_processing = (
                        fallback_result.next_step_for("processing")
                        or fallback_result.next_step_for("default")
                        or next_step_processing
                    )
                return cls._result(
                    dominant_code,
                    "Товар возвращен на склад",
                    source_facts,
                    movement,
                    processing_label=processing_label,
                    next_step_default="Товар доступен для следующей операции",
                    next_step_processing=next_step_processing,
                    is_ready_for_next_step=True,
                )
        if movement.has_active_tasks:
            source_facts.append("processing_move_task_without_snapshot")
            return cls._result(
                WarehouseStateCode.MOVING_TO_PROCESSING,
                "Доставка в зону обработки (ричтрак)",
                source_facts,
                movement,
                next_step_default="Дождаться доставки в зону обработки",
                next_step_processing="Дождаться доставки в зону обработки",
            )
        return cls._fallback_processing_result(
            payload=fallback_payload,
            source_facts=source_facts,
            movement=movement,
        )

    @classmethod
    def resolve_for_receiving_order(
        cls,
        *,
        order_id: str,
        agency=None,
        payload: dict | None = None,
        movement: WarehouseMovementResult | None = None,
        snapshots: list[WarehouseStockSnapshot] | None = None,
        active_putaway: bool | None = None,
        signature_flags: tuple[bool, bool] | None = None,
    ) -> WarehouseStateResult:
        order_key = str(order_id or "").strip()
        fallback_payload = payload if isinstance(payload, dict) else {}
        source_facts = [f"receiving_order:{order_key or '-'}"]
        if movement is None:
            movement = WarehouseMovementResolver.active_for_receiving_order(
                agency=agency,
                order_id=order_key,
            )
        if movement.active_task_count:
            source_facts.append(f"receiving_active_tasks:{movement.active_task_count}")
        if movement.done_task_count:
            source_facts.append(f"receiving_done_tasks:{movement.done_task_count}")
        if movement.blocked_task_count:
            source_facts.append(f"receiving_blocked_tasks:{movement.blocked_task_count}")
        payload_status_value = str(
            fallback_payload.get("status") or fallback_payload.get("submit_action") or ""
        ).strip().lower()
        payload_status_label = str(fallback_payload.get("status_label") or "").strip().lower()
        if payload_status_value in {"cancelled", "canceled"} or "отменен" in payload_status_label:
            source_facts.append("receiving_payload_cancelled")
            cancelled_label = (
                "Удалена как ошибочная"
                if fallback_payload.get("deleted_as_erroneous")
                else "Отменена"
            )
            return cls._result(
                WarehouseStateCode.CANCELED,
                cancelled_label,
                source_facts,
                movement,
                is_terminal=True,
            )
        if "взята в работу" in payload_status_label:
            source_facts.append("receiving_payload_in_progress")
            return cls._result(
                WarehouseStateCode.RECEIVED_UNPLACED,
                "Взята в работу",
                source_facts,
                movement,
                next_step_default="Продолжить приемку",
                next_step_storekeeper="Продолжить приемку",
            )
        base_result: WarehouseStateResult | None = None
        if snapshots is None:
            snapshots = list(
                WarehouseStockSnapshot.objects.filter(
                    agency=agency,
                    source_context_type="receiving",
                    source_context_id=order_key,
                    is_archived=False,
                )
                .order_by("id")
            ) if agency and order_key else []
        if snapshots:
            source_facts.append(f"receiving_snapshots:{len(snapshots)}")
            has_non_stored = any(
                str(snapshot.warehouse_state_code or "").strip().lower() in {
                    WarehouseStateCode.PLACED_IN_RECEIVING.value,
                    WarehouseStateCode.RECEIVED_UNPLACED.value,
                }
                for snapshot in snapshots
            )
            if active_putaway is None:
                active_putaway = WarehouseOperation.objects.filter(
                    agency=agency,
                    operation_type=WarehouseOperation.TYPE_PUTAWAY,
                    context_type="receiving",
                    context_id=order_key,
                    status__in=[
                        WarehouseOperation.STATUS_CREATED,
                        WarehouseOperation.STATUS_PLANNED,
                        WarehouseOperation.STATUS_IN_PROGRESS,
                        WarehouseOperation.STATUS_PARTIAL,
                    ],
                ).exists()
            if active_putaway:
                source_facts.append("putaway:active")
            if has_non_stored and not active_putaway and not movement.has_active_tasks:
                base_result = cls._result(
                    WarehouseStateCode.PLACED_IN_RECEIVING,
                    "Завершена приемка",
                    source_facts,
                    movement,
                    next_step_default="Передать паллеты на размещение в хранение",
                    next_step_storekeeper="Создать или завершить размещение в хранение",
                    is_ready_for_next_step=True,
                )
            elif has_non_stored or active_putaway:
                base_result = cls._result(
                    WarehouseStateCode.PLACED_IN_RECEIVING,
                    "Размещение на складе",
                    source_facts,
                    movement,
                    next_step_default="Передать паллеты на размещение в хранение",
                    next_step_storekeeper="Создать или завершить размещение в хранение",
                )
            else:
                base_result = cls._result(
                    WarehouseStateCode.STORED,
                    "Товар принят и размещен на складе",
                    source_facts,
                    movement,
                    next_step_default="Товар доступен на складе",
                    is_ready_for_next_step=True,
                )
        if base_result is None:
            base_result = cls._fallback_receiving_result(
                payload=fallback_payload,
                source_facts=source_facts,
                movement=movement,
            )
        return cls._apply_receiving_signature_overrides(
            base_result,
            order_id=order_key,
            payload=fallback_payload,
            movement=movement,
            signature_flags=signature_flags,
        )

    @classmethod
    def resolve_for_receiving_orders(
        cls,
        order_specs: dict[str, dict],
        *,
        entries_by_order: dict[str, list] | None = None,
    ) -> dict[str, WarehouseStateResult]:
        normalized_specs: dict[str, dict] = {}
        for raw_order_id, raw_spec in (order_specs or {}).items():
            order_key = str(raw_order_id or "").strip()
            if not order_key:
                continue
            spec = dict(raw_spec or {}) if isinstance(raw_spec, dict) else {}
            if entries_by_order and order_key in entries_by_order:
                spec["entries"] = entries_by_order.get(order_key) or []
            normalized_specs[order_key] = spec
        return cls.resolve_many_for_receiving_orders(normalized_specs)

    @classmethod
    def resolve_many_for_receiving_orders(cls, orders) -> dict[str, WarehouseStateResult]:
        normalized_orders = {
            str(order_id or "").strip(): data
            for order_id, data in (orders or {}).items()
            if str(order_id or "").strip()
        }
        if not normalized_orders:
            return {}

        agency_by_order_id = {
            order_id: (data or {}).get("agency")
            for order_id, data in normalized_orders.items()
        }
        movements_by_order_id = WarehouseMovementResolver.active_for_receiving_orders(
            agency_by_order_id
        )
        lookup_keys = {
            (int(agency.pk), order_id)
            for order_id, agency in agency_by_order_id.items()
            if agency and getattr(agency, "pk", None)
        }
        snapshots_by_key = {key: [] for key in lookup_keys}
        active_putaway_keys = set()
        if lookup_keys:
            agency_ids = {agency_id for agency_id, _order_id in lookup_keys}
            order_ids = {order_id for _agency_id, order_id in lookup_keys}
            snapshots = (
                WarehouseStockSnapshot.objects.filter(
                    agency_id__in=agency_ids,
                    source_context_type="receiving",
                    source_context_id__in=order_ids,
                    is_archived=False,
                )
                .order_by("id")
            )
            for snapshot in snapshots:
                key = (int(snapshot.agency_id), str(snapshot.source_context_id or "").strip())
                if key in snapshots_by_key:
                    snapshots_by_key[key].append(snapshot)
            active_putaway_keys = {
                (int(agency_id), str(context_id or "").strip())
                for agency_id, context_id in WarehouseOperation.objects.filter(
                    agency_id__in=agency_ids,
                    operation_type=WarehouseOperation.TYPE_PUTAWAY,
                    context_type="receiving",
                    context_id__in=order_ids,
                    status__in=[
                        WarehouseOperation.STATUS_CREATED,
                        WarehouseOperation.STATUS_PLANNED,
                        WarehouseOperation.STATUS_IN_PROGRESS,
                        WarehouseOperation.STATUS_PARTIAL,
                    ],
                ).values_list("agency_id", "context_id")
            }

        results = {}
        for order_id, data in normalized_orders.items():
            data = data or {}
            agency = data.get("agency")
            key = (
                int(agency.pk),
                order_id,
            ) if agency and getattr(agency, "pk", None) else None
            payload = data.get("payload") if isinstance(data.get("payload"), dict) else {}
            entries = list(data.get("entries") or [])
            signature_flags = cls._receiving_act_signature_flags_from_entries(
                entries=entries,
                payload=payload,
            )
            results[order_id] = cls.resolve_for_receiving_order(
                order_id=order_id,
                agency=agency,
                payload=payload,
                movement=movements_by_order_id.get(order_id),
                snapshots=snapshots_by_key.get(key, []),
                active_putaway=key in active_putaway_keys,
                signature_flags=signature_flags,
            )
        return results

    @staticmethod
    def _result(
        code: WarehouseStateCode,
        label: str,
        source_facts: list[str],
        movement: WarehouseMovementResult,
        *,
        client_label: str | None = None,
        storekeeper_label: str | None = None,
        logistician_label: str | None = None,
        processing_label: str | None = None,
        next_step_default: str = "",
        next_step_client: str | None = None,
        next_step_storekeeper: str | None = None,
        next_step_logistician: str | None = None,
        next_step_processing: str | None = None,
        is_terminal: bool = False,
        is_ready_for_next_step: bool = False,
    ) -> WarehouseStateResult:
        return WarehouseStateResult(
            code=code,
            label_default=label,
            label_client=client_label or label,
            label_storekeeper=storekeeper_label or label,
            label_logistician=logistician_label or label,
            label_processing=processing_label or label,
            next_step_default=next_step_default,
            next_step_client=next_step_client if next_step_client is not None else next_step_default,
            next_step_storekeeper=(
                next_step_storekeeper if next_step_storekeeper is not None else next_step_default
            ),
            next_step_logistician=(
                next_step_logistician if next_step_logistician is not None else next_step_default
            ),
            next_step_processing=(
                next_step_processing if next_step_processing is not None else next_step_default
            ),
            source_facts=source_facts,
            movement_summary={
                "has_active_tasks": movement.has_active_tasks,
                "active_task_count": movement.active_task_count,
                "done_task_count": movement.done_task_count,
                "blocked_task_count": movement.blocked_task_count,
                "destination_zone": movement.destination_zone,
                "operation_kind": movement.operation_kind,
                "task_ids": movement.task_ids,
            },
            is_terminal=is_terminal,
            is_ready_for_next_step=is_ready_for_next_step,
        )

    @staticmethod
    def _shipping_snapshots(order: ShippingOrder) -> list[WarehouseStockSnapshot]:
        cached_snapshots = getattr(order, "_shipping_flow_snapshots_cache", None)
        if cached_snapshots is not None:
            return [
                snapshot
                for snapshot in cached_snapshots
                if getattr(snapshot, "last_event", None) is not None
                and str(snapshot.last_event.stock_context_type or "").strip().lower() == "shipping"
                and str(snapshot.last_event.stock_context_id or "").strip() == str(order.number or "").strip()
                and not snapshot.is_archived
            ]
        return list(
            WarehouseStockSnapshot.objects.filter(
                agency=order.agency,
                last_event__stock_context_type="shipping",
                last_event__stock_context_id=order.number,
                is_archived=False,
            ).order_by("id")
        )

    @classmethod
    def _fallback_processing_result(
        cls,
        *,
        payload: dict,
        source_facts: list[str],
        movement: WarehouseMovementResult,
    ) -> WarehouseStateResult:
        status_value = str(payload.get("status") or payload.get("submit_action") or "").strip().lower()
        status_label = str(payload.get("status_label") or "").strip()
        lowered_label = status_label.lower()
        source_facts.append(f"processing_payload_status:{status_value or '-'}")
        if payload.get("flow_closed"):
            return cls._result(WarehouseStateCode.PLACED_AFTER_PROCESSING, "Размещение завершено", source_facts, movement)
        if str(payload.get("act") or "").strip().lower() == "placement":
            act_state = str(payload.get("act_state") or "closed").strip().lower()
            return cls._result(
                WarehouseStateCode.PLACED_AFTER_PROCESSING if act_state == "closed" else WarehouseStateCode.IN_PROCESSING_ZONE,
                "Размещение завершено" if act_state == "closed" else "Размещение открыто",
                source_facts,
                movement,
            )
        if status_value in {"done", "completed", "closed", "finished"} or "выполн" in lowered_label:
            return cls._result(WarehouseStateCode.PLACED_AFTER_PROCESSING, "Выполнена", source_facts, movement, is_terminal=True)
        if status_value in {"cancelled", "canceled"} or "отменен" in lowered_label:
            return cls._result(WarehouseStateCode.CANCELED, "Отменена", source_facts, movement, is_terminal=True)
        if status_value == "processing_in_work" or "взята" in lowered_label:
            return cls._result(WarehouseStateCode.PROCESSING_IN_PROGRESS, "Взята в работу", source_facts, movement)
        if status_value == "processing_head" or ("передан" in lowered_label and "обработ" in lowered_label):
            return cls._result(
                WarehouseStateCode.RESERVED_FOR_PROCESSING,
                "Передано в обработку",
                source_facts,
                movement,
                storekeeper_label="Ожидает доставки в зону обработки",
                processing_label="Ожидает доставки в зону обработки",
                next_step_default="Передать товар ричтраку в зону обработки",
                next_step_storekeeper="Создать или выполнить доставку в зону обработки",
                next_step_processing="Дождаться доставки в зону обработки",
            )
        if status_value in {"sent_unconfirmed", "send", "submitted"} or "подтверждени" in lowered_label:
            return cls._result(WarehouseStateCode.UNKNOWN, "Ждет подтверждения", source_facts, movement)
        if status_value == "draft" or "черновик" in lowered_label:
            return cls._result(WarehouseStateCode.UNKNOWN, "Черновик", source_facts, movement)
        return cls._result(WarehouseStateCode.UNKNOWN, status_label or status_value or "-", source_facts, movement)

    @classmethod
    def _fallback_receiving_result(
        cls,
        *,
        payload: dict,
        source_facts: list[str],
        movement: WarehouseMovementResult,
    ) -> WarehouseStateResult:
        status_value = str(payload.get("status") or payload.get("submit_action") or "").strip().lower()
        status_label = str(payload.get("status_label") or "").strip()
        lowered_label = status_label.lower()
        source_facts.append(f"receiving_payload_status:{status_value or '-'}")
        if status_value in {"cancelled", "canceled"} or "отменен" in lowered_label:
            return cls._result(WarehouseStateCode.CANCELED, "Отменена", source_facts, movement, is_terminal=True)
        if payload.get("act") == "placement":
            state = str(payload.get("act_state") or "closed").strip().lower()
            return cls._result(
                WarehouseStateCode.PLACED_IN_RECEIVING if state == "open" else WarehouseStateCode.STORED,
                "Размещение на складе" if state == "open" else "Товар принят и размещен на складе",
                source_facts,
                movement,
            )
        if "взята в работу" in lowered_label:
            return cls._result(WarehouseStateCode.RECEIVED_UNPLACED, "Взята в работу", source_facts, movement)
        if status_value in {"sent_unconfirmed", "send", "submitted"} or "подтверждени" in lowered_label:
            return cls._result(WarehouseStateCode.UNKNOWN, "Ждет подтверждения", source_facts, movement)
        if status_value in {"warehouse", "on_warehouse"} or "ожидании поставки" in lowered_label or "на складе" in lowered_label:
            return cls._result(WarehouseStateCode.UNKNOWN, "В ожидании поставки товара", source_facts, movement)
        return cls._result(WarehouseStateCode.UNKNOWN, status_label or status_value or "-", source_facts, movement)

    @classmethod
    def _apply_receiving_signature_overrides(
        cls,
        base_result: WarehouseStateResult,
        *,
        order_id: str,
        payload: dict | None,
        movement: WarehouseMovementResult,
        signature_flags: tuple[bool, bool] | None = None,
    ) -> WarehouseStateResult:
        if signature_flags is None:
            signature_flags = cls._receiving_act_signature_flags(
                order_id=order_id,
                payload=payload,
            )
        storekeeper_signed, manager_signed = signature_flags
        if not storekeeper_signed and not manager_signed:
            return base_result
        overrides: dict[str, object] = {}
        if manager_signed:
            overrides.update(
                {
                    "label_default": "Выполнена",
                    "label_client": "Выполнена",
                    "next_step_default": "",
                    "next_step_client": "",
                }
            )
        elif storekeeper_signed:
            overrides.update(
                {
                    "label_default": "Принято складом, акт приемки отправлен менеджеру",
                    "label_client": "Ожидает подписи менеджера",
                    "next_step_default": "Подписать акт приемки менеджером",
                    "next_step_client": "Ожидает подписи менеджера",
                }
            )
        if storekeeper_signed:
            if base_result.code == WarehouseStateCode.STORED:
                overrides.update(
                    {
                        "label_storekeeper": "Выполнена",
                        "next_step_storekeeper": "",
                    }
                )
            else:
                overrides.update(
                    {
                        "label_storekeeper": cls._receiving_storekeeper_label_after_sign(base_result, movement=movement),
                        "next_step_storekeeper": cls._receiving_storekeeper_next_step_after_sign(
                            base_result,
                            movement=movement,
                        ),
                    }
                )
        if manager_signed and base_result.code == WarehouseStateCode.STORED:
            overrides["is_terminal"] = True
        return base_result.with_overrides(**overrides)

    @classmethod
    def _receiving_act_signature_flags(
        cls,
        *,
        order_id: str,
        payload: dict | None,
    ) -> tuple[bool, bool]:
        payload = payload if isinstance(payload, dict) else {}
        storekeeper_signed = bool(payload.get("act_storekeeper_signed"))
        manager_signed = bool(payload.get("act_manager_signed"))
        if storekeeper_signed or manager_signed or not str(order_id or "").strip():
            return storekeeper_signed, manager_signed
        entries = (
            OrderAuditEntry.objects.filter(
                order_type="receiving",
                order_id=str(order_id or "").strip(),
            )
            .only("payload", "created_at", "id")
            .order_by("-created_at", "-id")
        )
        return cls._receiving_act_signature_flags_from_entries(
            entries=entries,
            payload=payload,
        )

    @staticmethod
    def _receiving_act_signature_flags_from_entries(
        *,
        entries,
        payload: dict | None,
    ) -> tuple[bool, bool]:
        payload = payload if isinstance(payload, dict) else {}
        storekeeper_signed = bool(payload.get("act_storekeeper_signed"))
        manager_signed = bool(payload.get("act_manager_signed"))
        if storekeeper_signed or manager_signed:
            return storekeeper_signed, manager_signed
        for entry in entries:
            entry_payload = entry.payload or {}
            if "act_storekeeper_signed" in entry_payload or "act_manager_signed" in entry_payload:
                return bool(entry_payload.get("act_storekeeper_signed")), bool(entry_payload.get("act_manager_signed"))
            act_name = str(entry_payload.get("act") or "").strip().lower()
            act_label = str(entry_payload.get("act_label") or "").strip().lower()
            if act_name == "receiving" or "акт приемки" in act_label:
                return bool(entry_payload.get("act_storekeeper_signed")), bool(entry_payload.get("act_manager_signed"))
        return False, False

    @classmethod
    def _receiving_storekeeper_label_after_sign(
        cls,
        base_result: WarehouseStateResult,
        *,
        movement: WarehouseMovementResult,
    ) -> str:
        return "Выполнена"

    @classmethod
    def _receiving_storekeeper_next_step_after_sign(
        cls,
        base_result: WarehouseStateResult,
        *,
        movement: WarehouseMovementResult,
    ) -> str:
        return ""

    @classmethod
    def _processing_snapshots(cls, *, agency, order_id: str) -> list[WarehouseStockSnapshot]:
        if not agency or not order_id:
            return []
        reserves = list(
            WarehouseReserve.objects.filter(
                agency=agency,
                reserve_type=WarehouseReserve.TYPE_PROCESSING,
                context_type="processing",
                context_id=order_id,
                status__in=[
                    WarehouseReserve.STATUS_ACTIVE,
                    WarehouseReserve.STATUS_PARTIALLY_ALLOCATED,
                    WarehouseReserve.STATUS_ALLOCATED,
                    WarehouseReserve.STATUS_PARTIALLY_SATISFIED,
                    WarehouseReserve.STATUS_SATISFIED,
                ],
            )
            .values_list("sku_code", "size", "barcode", "goods_type")
        )
        if not reserves:
            return []
        query = Q()
        for sku_code, size, barcode, goods_type in reserves:
            query |= Q(
                sku_code=sku_code,
                size=size,
                barcode=barcode,
                goods_type=goods_type,
            )
        return list(
            WarehouseStockSnapshot.objects.filter(
                agency=agency,
                is_archived=False,
            )
            .filter(query)
            .order_by("id")
        )

    @classmethod
    def _dominant_state_code(
        cls,
        codes: list[str],
        *,
        priority: list[WarehouseStateCode],
    ) -> WarehouseStateCode:
        normalized_codes = {
            WarehouseStateCode(str(code or "").strip().lower())
            for code in codes
            if str(code or "").strip()
            and str(code or "").strip().lower() in {member.value for member in WarehouseStateCode}
        }
        for candidate in priority:
            if candidate in normalized_codes:
                return candidate
        return WarehouseStateCode.UNKNOWN
