from __future__ import annotations

from dataclasses import dataclass
import logging
import time
from uuid import uuid4

from django.db import OperationalError, connection, transaction
from django.db.models import F, Q, Sum
from django.utils import timezone

from employees.access import (
    get_employee_for_user,
    get_employee_roles,
    is_developer_login,
)
from fbs.exceptions import (
    FbsError,
    FbsFeatureDisabled,
    FbsHandoverError,
    FbsPickingError,
)
from fbs.flags import feature_enabled
from fbs.models import (
    FbsControllerCheckTote,
    FbsControllerPolicy,
    FbsControllerPickTote,
    FbsControllerSession,
    FbsControllerToteOrder,
    FbsHandoverBatch,
    FbsHandoverBox,
    FbsHandoverOrder,
    FbsHandoverOrderAssignment,
    FbsIntegrationProfile,
    FbsMarketplaceMetadataTransfer,
    FbsOrder,
    FbsOrderItem,
    FbsOrderLabel,
    FbsOrderStockAllocation,
    FbsPickBatch,
    FbsProblemToteItem,
    FbsPickRestockRequest,
    FbsPickingCart,
    FbsPickTask,
    FbsPickVerificationProgress,
    FbsStockBalance,
    FbsToteBinding,
    FbsToteMovement,
    FbsToteZone,
    FbsUnknownToteItem,
    FbsWorkstation,
)


logger = logging.getLogger(__name__)


def _is_database_deadlock(error: BaseException) -> bool:
    current: BaseException | None = error
    visited: set[int] = set()
    while current is not None and id(current) not in visited:
        visited.add(id(current))
        sqlstate = str(
            getattr(current, "pgcode", "")
            or getattr(getattr(current, "diag", None), "sqlstate", "")
            or ""
        )
        if sqlstate == "40P01" or "deadlock detected" in str(current).lower():
            return True
        current = getattr(current, "__cause__", None) or getattr(
            current,
            "__context__",
            None,
        )
    return False


def _is_database_lock_unavailable(error: BaseException) -> bool:
    current: BaseException | None = error
    visited: set[int] = set()
    while current is not None and id(current) not in visited:
        visited.add(id(current))
        sqlstate = str(
            getattr(current, "pgcode", "")
            or getattr(getattr(current, "diag", None), "sqlstate", "")
            or ""
        )
        message = str(current).lower()
        if sqlstate == "55P03" or "could not obtain lock" in message:
            return True
        current = getattr(current, "__cause__", None) or getattr(
            current,
            "__context__",
            None,
        )
    return False


def _is_database_statement_timeout(error: BaseException) -> bool:
    current: BaseException | None = error
    visited: set[int] = set()
    while current is not None and id(current) not in visited:
        visited.add(id(current))
        sqlstate = str(
            getattr(current, "pgcode", "")
            or getattr(getattr(current, "diag", None), "sqlstate", "")
            or ""
        )
        message = str(current).lower()
        if sqlstate == "57014" or "statement timeout" in message:
            return True
        current = getattr(current, "__cause__", None) or getattr(
            current,
            "__context__",
            None,
        )
    return False


FREE_TOTE_ZONE_BARCODE = "FBS-TOTE-ZONE-FREE"
READY_TOTE_ZONE_BARCODE = "FBS-TOTE-ZONE-READY"
MAX_ACTIVE_CHECK_TOTES = 3
WB_LABEL_PREFETCH_BUDGET_SECONDS = 2.0
WB_LABEL_PREFETCH_HANDOVER_CONFLICT = "Заказ уже назначен в другую отгрузку."
SERVICE_TOTE_PROBLEM = "problem"
SERVICE_TOTE_CANCELED = "canceled"
ACTIVE_CHECK_TOTE_STATUSES = (
    FbsControllerCheckTote.STATUS_OPEN,
    FbsControllerCheckTote.STATUS_WAITING_KIZ,
    FbsControllerCheckTote.STATUS_READY,
    FbsControllerCheckTote.STATUS_COMPOSITION,
)
ACTIVE_PICK_TOTE_STATUSES = (
    FbsControllerPickTote.STATUS_PROCESSING,
    FbsControllerPickTote.STATUS_AWAITING_EMPTY,
)
ACTIVE_PICK_BATCH_TOTE_STATUSES = (
    FbsPickBatch.STATUS_QUEUED,
    FbsPickBatch.STATUS_IN_PROGRESS,
    FbsPickBatch.STATUS_VERIFICATION,
)
HANDOVER_STATES_ACCEPTING_ORDERS = (
    FbsHandoverBatch.MARKETPLACE_DRAFT,
    FbsHandoverBatch.MARKETPLACE_CREATING,
    FbsHandoverBatch.MARKETPLACE_OPEN,
)
SEPARATED_PICK_RESTOCK_STATUSES = (
    FbsPickRestockRequest.STATUS_WAITING_MARKETPLACE,
    FbsPickRestockRequest.STATUS_QUEUED,
    FbsPickRestockRequest.STATUS_IN_PROGRESS,
    FbsPickRestockRequest.STATUS_COMPLETED,
    FbsPickRestockRequest.STATUS_FAILED,
)
PROFILE_SPLIT_OPERATION = "mixed_profile_wave_split"

COMPOSITION_PRODUCT_BARCODE_MESSAGE = (
    "Отсканирован неверный ШК. Отсканируйте ШК маркетплейса"
)
COMPOSITION_ALREADY_PACKED_MESSAGE = (
    "Данный товар уже прошёл проверку и переложен в короб отгрузки"
)
COMPOSITION_ITEM_METADATA_PENDING_MESSAGE = (
    "Данный товар ещё не прошёл проверку КИЗ/ЧЗ. Отложите этот товар "
    "и отсканируйте для проверки позже"
)
COMPOSITION_ITEM_LABEL_PENDING_MESSAGE = (
    "Официальная этикетка marketplace ещё готовится. "
    "Проверка ЧЗ для этого заказа завершена."
)
EXTRA_PROBLEM_REASON = "Лишний товар"
CHECK_TOTE_OPERATOR_ROLES = frozenset(
    {"fbs_controller", "head_manager", "director", "admin"}
)


def controller_skips_repeat_wb_label_scan(actor) -> bool:
    """Return the explicit WB fast-control policy for an active controller."""
    if actor is None or not getattr(actor, "is_authenticated", False):
        return False
    return FbsControllerPolicy.objects.filter(
        controller_id=actor.id,
        controller__employee_profile__role="fbs_controller",
        controller__employee_profile__is_active=True,
        skip_repeat_wb_label_scan=True,
    ).exists()


class CompositionProblemToteRoutingRequired(FbsHandoverError):
    def __init__(self, *, label_scan: str, problem_tote_barcode: str):
        self.label_scan = label_scan
        self.problem_tote_barcode = problem_tote_barcode
        super().__init__(
            "На данном столе нет подходящей тары для этого товара. "
            f"Положите товар в проблемную тару ({problem_tote_barcode})"
        )


class _WbLabelPrefetchBudgetExceeded(Exception):
    pass


@dataclass(frozen=True)
class CheckToteReadiness:
    ready: bool
    status: str
    reasons: tuple[str, ...]
    tote_reasons: tuple[str, ...] = ()
    order_reasons: tuple[str, ...] = ()
    blocked_order_ids: frozenset[int] = frozenset()
    metadata_blocked_order_ids: frozenset[int] = frozenset()
    assignment_blocked_order_ids: frozenset[int] = frozenset()
    label_blocked_order_ids: frozenset[int] = frozenset()

    @property
    def composition_ready(self) -> bool:
        # Orders which have already passed the controller checks may be packed
        # while another pick tote is still being processed or awaits its empty
        # confirmation. Closing the shipment still requires ``ready`` below.
        return "В таре проверки пока нет заказов." not in self.tote_reasons

    @property
    def display_label(self) -> str:
        if self.status != FbsControllerCheckTote.STATUS_WAITING_KIZ:
            return dict(FbsControllerCheckTote.STATUS_CHOICES).get(
                self.status, self.status
            )
        metadata_waiting = any(
            "КИЗ или срока" in reason for reason in self.reasons
        )
        marketplace_waiting = any(
            "Маркетплейс подтверждает" in reason for reason in self.reasons
        )
        if metadata_waiting and marketplace_waiting:
            return "Ожидает КИЗ и marketplace"
        if metadata_waiting:
            return "Ожидает КИЗ или срок"
        if marketplace_waiting:
            return "Ожидает marketplace"
        return "Ожидает подтверждения"


def _require_writes() -> None:
    if not feature_enabled("module"):
        raise FbsFeatureDisabled("Модуль FBS выключен.")
    if not feature_enabled("warehouse_writes"):
        raise FbsFeatureDisabled("Операции с тарой FBS выключены.")


def _actor(user):
    if not getattr(user, "is_authenticated", False):
        raise FbsPickingError("Для операции с тарой нужен авторизованный сотрудник.")
    return user


def _assert_check_tote_operator(actor) -> None:
    if is_developer_login(actor):
        return
    employee = get_employee_for_user(actor)
    if not get_employee_roles(employee).intersection(CHECK_TOTE_OPERATOR_ROLES):
        raise FbsHandoverError(
            "Для проверки отгрузки нужна роль контролера."
        )


def _scan(value: str) -> str:
    return str(value or "").strip().upper()


def _get_zone(*, barcode: str, name: str, kind: str) -> FbsToteZone:
    zone, _ = FbsToteZone.objects.get_or_create(
        barcode=barcode,
        defaults={"name": name, "kind": kind, "is_active": True},
    )
    if not zone.is_active:
        raise FbsPickingError(f"Зона тары «{zone.name}» отключена.")
    return zone


def get_free_tote_zone() -> FbsToteZone:
    return _get_zone(
        barcode=FREE_TOTE_ZONE_BARCODE,
        name="Зона свободной тары",
        kind=FbsToteZone.KIND_FREE,
    )


def get_ready_tote_zone() -> FbsToteZone:
    return _get_zone(
        barcode=READY_TOTE_ZONE_BARCODE,
        name="Зона готового товара",
        kind=FbsToteZone.KIND_READY,
    )


def _resolve_tote_for_update(value: str) -> FbsPickingCart:
    value = _scan(value)
    if not value:
        raise FbsPickingError("Отсканируйте QR тары.")
    tote = FbsPickingCart.objects.select_for_update().filter(
        barcode=value,
        is_active=True,
    ).first()
    if tote is None:
        raise FbsPickingError("Тара FBS не найдена или отключена.")
    return tote


def _binding_position(binding: FbsToteBinding) -> tuple[str, str]:
    if binding.zone_id:
        return "zone", str(binding.zone.barcode)
    if binding.workstation_id:
        return "workstation", str(binding.workstation.barcode)
    if binding.employee_id:
        return "employee", str(binding.employee_id)
    return "", ""


def _binding_location_label(binding: FbsToteBinding) -> str:
    if binding.zone_id:
        return f"зона «{binding.zone.name}»"
    if binding.workstation_id:
        return f"рабочее место «{binding.workstation.name}»"
    if binding.employee_id:
        employee_name = binding.employee.get_full_name() or binding.employee.username
        return f"сотрудник «{employee_name}»"
    return "место не указано"


def _binding_for_update(tote: FbsPickingCart) -> FbsToteBinding:
    binding = (
        FbsToteBinding.objects.select_for_update(of=("self",))
        .select_related("zone", "workstation", "employee")
        .filter(tote=tote)
        .first()
    )
    if binding is not None:
        return binding
    return FbsToteBinding.objects.create(
        tote=tote,
        state=FbsToteBinding.STATE_UNBOUND,
    )


def _move_tote(
    *,
    tote: FbsPickingCart,
    state: str,
    performed_by,
    action: str,
    zone: FbsToteZone | None = None,
    workstation: FbsWorkstation | None = None,
    employee=None,
    pick_batch: FbsPickBatch | None = None,
    controller_session: FbsControllerSession | None = None,
    handover_batch: FbsHandoverBatch | None = None,
    quantity: int = 0,
    details: dict | None = None,
) -> FbsToteBinding:
    destinations = [zone, workstation, employee]
    if sum(value is not None for value in destinations) != 1:
        raise FbsPickingError(
            "Тара должна быть привязана к одной зоне, столу или сотруднику."
        )
    binding = _binding_for_update(tote)
    source_kind, source_code = _binding_position(binding)
    binding.state = state
    binding.zone = zone
    binding.workstation = workstation
    binding.employee = employee
    binding.pick_batch = pick_batch
    binding.controller_session = controller_session
    binding.updated_by = performed_by
    binding.full_clean()
    binding.save()
    target_kind, target_code = _binding_position(binding)
    FbsToteMovement.objects.create(
        tote=tote,
        action=action,
        source_kind=source_kind,
        source_code=source_code,
        target_kind=target_kind,
        target_code=target_code,
        pick_batch=pick_batch,
        controller_session=controller_session,
        handover_batch=handover_batch,
        quantity=max(int(quantity or 0), 0),
        details=details or {},
        performed_by=performed_by,
    )
    return binding


def _active_pick_batch_for_tote(
    tote: FbsPickingCart,
    *,
    allowed_pick_batch_id: int | None = None,
) -> FbsPickBatch | None:
    batches = FbsPickBatch.objects.filter(
        cart=tote,
        status__in=ACTIVE_PICK_BATCH_TOTE_STATUSES,
        cart_released_at__isnull=True,
    )
    if allowed_pick_batch_id is not None:
        batches = batches.exclude(pk=allowed_pick_batch_id)
    return batches.order_by("id").first()


def _assert_tote_not_service_reserved(tote: FbsPickingCart) -> None:
    unknown_session = (
        FbsControllerSession.objects.filter(
            unknown_tote=tote,
            status=FbsControllerSession.STATUS_ACTIVE,
        )
        .order_by("id")
        .first()
    )
    if unknown_session is not None:
        raise FbsPickingError(
            f"Тара «{tote.name}» закреплена за активной сменой "
            "как служебная тара неизвестного товара."
        )
    service_session = (
        FbsControllerSession.objects.filter(
            Q(problem_tote=tote) | Q(canceled_tote=tote),
            status=FbsControllerSession.STATUS_ACTIVE,
        )
        .order_by("id")
        .first()
    )
    if service_session is not None:
        purpose = (
            "проблемных заказов"
            if service_session.problem_tote_id == tote.id
            else "отмененных заказов"
        )
        raise FbsPickingError(
            f"Тара «{tote.name}» закреплена за активной сменой как тара {purpose}."
        )
    active_restock = (
        FbsPickRestockRequest.objects.filter(
            source_tote=tote,
            status__in=(
                FbsPickRestockRequest.STATUS_WAITING_MARKETPLACE,
                FbsPickRestockRequest.STATUS_QUEUED,
                FbsPickRestockRequest.STATUS_IN_PROGRESS,
                FbsPickRestockRequest.STATUS_FAILED,
            ),
        )
        .order_by("id")
        .first()
    )
    if active_restock is not None:
        raise FbsPickingError(
            f"В таре «{tote.name}» находится товар возврата #{active_restock.id}."
        )
    if FbsControllerCheckTote.objects.filter(
        tote=tote,
        status__in=ACTIVE_CHECK_TOTE_STATUSES,
    ).exists():
        raise FbsPickingError("Эта тара уже используется для проверки.")
    waiting_unknown_qty = FbsUnknownToteItem.objects.filter(
        unknown_tote=tote,
        status=FbsUnknownToteItem.STATUS_WAITING,
    ).count()
    if waiting_unknown_qty:
        raise FbsPickingError(
            f"В таре «{tote.name}» числятся проблемные товары: "
            f"{waiting_unknown_qty}. Сначала завершите их размещение."
        )
    problem_item_qty = int(
        FbsProblemToteItem.objects.filter(
            problem_tote=tote,
            status=FbsProblemToteItem.STATUS_IN_TOTE,
        ).aggregate(total=Sum("quantity"))["total"]
        or 0
    )
    if problem_item_qty:
        raise FbsPickingError(
            f"В таре «{tote.name}» числятся товары проблемных заказов: "
            f"{problem_item_qty}. Сначала завершите их обработку."
        )


def _assert_tote_available(
    tote: FbsPickingCart,
    *,
    allowed_pick_batch_id: int | None = None,
) -> None:
    binding = _binding_for_update(tote)
    _assert_tote_not_service_reserved(tote)
    active_batch = _active_pick_batch_for_tote(
        tote,
        allowed_pick_batch_id=allowed_pick_batch_id,
    )
    if active_batch is not None:
        raise FbsPickingError(
            f"Тара «{tote.name}» уже привязана к волне "
            f"#{active_batch.id}."
        )
    active_pick_context = (
        FbsControllerPickTote.objects.select_related(
            "pick_batch", "session__workstation"
        )
        .filter(tote=tote, status__in=ACTIVE_PICK_TOTE_STATUSES)
        .order_by("id")
        .first()
    )
    if active_pick_context is not None:
        raise FbsPickingError(
            f"Тара «{tote.name}» обрабатывается по волне "
            f"#{active_pick_context.pick_batch_id} на рабочем месте "
            f"«{active_pick_context.session.workstation.name}»."
        )
    if binding.state not in {
        FbsToteBinding.STATE_UNBOUND,
        FbsToteBinding.STATE_FREE,
    }:
        raise FbsPickingError(
            f"Тара «{tote.name}» сейчас занята: {binding.get_state_display()}, "
            f"{_binding_location_label(binding)}."
        )


def _session_for_update(*, session_id: int, actor) -> FbsControllerSession:
    session = (
        FbsControllerSession.objects.select_for_update(of=("self",))
        .select_related(
            "workstation",
            "unknown_tote",
            "free_zone",
        )
        .get(pk=session_id)
    )
    if session.status != FbsControllerSession.STATUS_ACTIVE:
        raise FbsPickingError("Смена контролера уже закрыта.")
    if session.controller_id != actor.id:
        raise FbsPickingError("Эта смена открыта другим контролером.")
    return session


def _check_tote_session_for_update(
    *, check_tote_id: int, actor
) -> FbsControllerSession:
    session = (
        FbsControllerSession.objects.select_for_update(of=("self",))
        .select_related("problem_tote", "workstation", "free_zone")
        .get(check_totes__pk=check_tote_id)
    )
    if session.status != FbsControllerSession.STATUS_ACTIVE:
        raise FbsHandoverError("Смена контролера уже закрыта.")
    if session.controller_id != actor.id:
        raise FbsHandoverError("Эта смена открыта другим контролером.")
    return session


@transaction.atomic
def start_controller_session(
    *,
    workstation_id: int,
    controller,
    unknown_tote_scan: str,
    problem_tote_scan: str = "",
    canceled_tote_scan: str = "",
    first_check_tote_scan: str = "",
) -> FbsControllerSession:
    _require_writes()
    actor = _actor(controller)
    # The deprecated argument is kept for compatibility with older callers.
    # The logical shipping flow is created after the first pick tote is scanned;
    # opening the session does not reserve a physical check tote.
    workstation = FbsWorkstation.objects.select_for_update().get(
        pk=workstation_id,
        is_active=True,
    )
    if FbsControllerSession.objects.filter(
        workstation=workstation,
        status=FbsControllerSession.STATUS_ACTIVE,
    ).exists():
        raise FbsPickingError("На этом рабочем месте уже открыта смена контролера.")
    if FbsControllerSession.objects.filter(
        controller=actor,
        status=FbsControllerSession.STATUS_ACTIVE,
    ).exists():
        raise FbsPickingError("У контролера уже открыта смена на другом столе.")
    unknown_tote = _resolve_tote_for_update(unknown_tote_scan)
    _assert_tote_available(unknown_tote)
    problem_tote = None
    if _scan(problem_tote_scan):
        problem_tote = _resolve_tote_for_update(problem_tote_scan)
        if problem_tote.id == unknown_tote.id:
            raise FbsPickingError(
                "Для неизвестного товара и проблемных заказов нужны разные тары."
            )
        _assert_tote_available(problem_tote)
    canceled_tote = unknown_tote
    if _scan(canceled_tote_scan):
        canceled_tote = _resolve_tote_for_update(canceled_tote_scan)
        if canceled_tote.id in {
            unknown_tote.id,
            getattr(problem_tote, "id", None),
        }:
            raise FbsPickingError(
                "Для неизвестного товара, проблемных и отмененных заказов нужны разные тары."
            )
        _assert_tote_available(canceled_tote)
    free_zone = get_free_tote_zone()
    session = FbsControllerSession.objects.create(
        workstation=workstation,
        controller=actor,
        unknown_tote=unknown_tote,
        problem_tote=problem_tote,
        canceled_tote=canceled_tote,
        free_zone=free_zone,
    )
    _move_tote(
        tote=unknown_tote,
        state=FbsToteBinding.STATE_UNKNOWN,
        workstation=workstation,
        performed_by=actor,
        action=FbsToteMovement.ACTION_ASSIGN,
        controller_session=session,
        details={"purpose": "unknown"},
    )
    if problem_tote is not None:
        _move_tote(
            tote=problem_tote,
            state=FbsToteBinding.STATE_AT_CONTROL,
            workstation=workstation,
            performed_by=actor,
            action=FbsToteMovement.ACTION_ASSIGN,
            controller_session=session,
            details={"purpose": SERVICE_TOTE_PROBLEM},
        )
    if canceled_tote.id != unknown_tote.id:
        _move_tote(
            tote=canceled_tote,
            state=FbsToteBinding.STATE_AT_CONTROL,
            workstation=workstation,
            performed_by=actor,
            action=FbsToteMovement.ACTION_ASSIGN,
            controller_session=session,
            details={"purpose": SERVICE_TOTE_CANCELED},
        )
    return session


def _service_tote_field(purpose: str) -> str:
    if purpose == SERVICE_TOTE_PROBLEM:
        return "problem_tote"
    if purpose == SERVICE_TOTE_CANCELED:
        return "canceled_tote"
    raise FbsPickingError("Неизвестное назначение служебной тары.")


def _session_tote_purpose_label(
    session: FbsControllerSession,
    tote_id: int,
) -> str:
    if session.unknown_tote_id == tote_id:
        return "неизвестного товара"
    if session.problem_tote_id == tote_id:
        return "проблемных заказов и Честного знака"
    if session.canceled_tote_id == tote_id:
        return "отмененных заказов"
    return "другой служебной цели"


def _assign_controller_service_tote(
    *,
    session: FbsControllerSession,
    purpose: str,
    tote_scan: str,
    actor,
) -> FbsPickingCart:
    field_name = _service_tote_field(purpose)
    current = getattr(session, field_name)
    if current is not None:
        if _scan(tote_scan) and _scan(tote_scan) != _scan(current.barcode):
            raise FbsPickingError("Эта служебная тара уже привязана к смене.")
        return current
    tote = _resolve_tote_for_update(tote_scan)
    used_ids = {
        session.unknown_tote_id,
        session.problem_tote_id,
        session.canceled_tote_id,
    }
    if tote.id in used_ids:
        assigned_purpose = _session_tote_purpose_label(session, tote.id)
        raise FbsPickingError(
            f"Тара «{tote.name}» ({tote.barcode}) уже назначена "
            f"для {assigned_purpose}. Отсканируйте другую свободную тару."
        )
    _assert_tote_available(tote)
    setattr(session, field_name, tote)
    session.save(update_fields=[field_name])
    _move_tote(
        tote=tote,
        state=FbsToteBinding.STATE_AT_CONTROL,
        workstation=session.workstation,
        performed_by=actor,
        action=FbsToteMovement.ACTION_ASSIGN,
        controller_session=session,
        details={"purpose": purpose, "attached_to_existing_session": True},
    )
    return tote


@transaction.atomic
def bind_controller_service_totes(
    *,
    session_id: int,
    problem_tote_scan: str,
    canceled_tote_scan: str = "",
    performed_by,
) -> FbsControllerSession:
    _require_writes()
    actor = _actor(performed_by)
    session = _session_for_update(session_id=session_id, actor=actor)
    _assign_controller_service_tote(
        session=session,
        purpose=SERVICE_TOTE_PROBLEM,
        tote_scan=problem_tote_scan,
        actor=actor,
    )
    if session.canceled_tote_id is None and not _scan(canceled_tote_scan):
        session.canceled_tote = session.unknown_tote
        session.save(update_fields=["canceled_tote"])
    else:
        _assign_controller_service_tote(
            session=session,
            purpose=SERVICE_TOTE_CANCELED,
            tote_scan=canceled_tote_scan,
            actor=actor,
        )
    return session


def controller_service_tote_for_actor(*, actor, purpose: str) -> tuple[FbsControllerSession, FbsPickingCart]:
    field_name = _service_tote_field(purpose)
    session = (
        FbsControllerSession.objects.select_for_update(of=("self",))
        .select_related(
            "workstation",
            "problem_tote",
            "canceled_tote",
        )
        .filter(
            controller=actor,
            status=FbsControllerSession.STATUS_ACTIVE,
        )
        .first()
    )
    if session is None:
        raise FbsPickingError("Сначала откройте рабочую сессию контролера.")
    tote = getattr(session, field_name)
    if tote is None:
        label = "проблемных" if purpose == SERVICE_TOTE_PROBLEM else "отмененных"
        raise FbsPickingError(f"Сначала привяжите тару {label} заказов.")
    binding = _binding_for_update(tote)
    allowed_states = {FbsToteBinding.STATE_AT_CONTROL}
    if (
        purpose == SERVICE_TOTE_CANCELED
        and session.canceled_tote_id == session.unknown_tote_id
    ):
        allowed_states.add(FbsToteBinding.STATE_UNKNOWN)
    if (
        binding.state not in allowed_states
        or binding.workstation_id != session.workstation_id
        or binding.controller_session_id != session.id
    ):
        raise FbsPickingError(
            "Служебная тара перемещена или занята другим процессом. Обратитесь к кладовщику."
        )
    return session, tote


def _create_check_tote(
    *, session: FbsControllerSession, tote: FbsPickingCart, actor
) -> FbsControllerCheckTote:
    active_count = session.check_totes.filter(
        status__in=ACTIVE_CHECK_TOTE_STATUSES
    ).count()
    if active_count >= MAX_ACTIVE_CHECK_TOTES:
        raise FbsPickingError("У контролера уже открыто три тары на проверку.")
    check_tote = FbsControllerCheckTote.objects.create(
        session=session,
        tote=tote,
        opened_by=actor,
    )
    _move_tote(
        tote=tote,
        state=FbsToteBinding.STATE_CHECKING,
        workstation=session.workstation,
        performed_by=actor,
        action=FbsToteMovement.ACTION_ASSIGN,
        controller_session=session,
        details={"purpose": "check", "check_tote_id": check_tote.id},
    )
    return check_tote


def _create_logical_check_tote(
    *,
    session: FbsControllerSession,
    profile: FbsIntegrationProfile,
    actor,
) -> FbsControllerCheckTote:
    active_count = session.check_totes.filter(
        status__in=ACTIVE_CHECK_TOTE_STATUSES
    ).count()
    if active_count >= MAX_ACTIVE_CHECK_TOTES:
        raise FbsPickingError(
            "У вас открыты 3 потока отгрузки. Вы не можете взять новую тару "
            "сборщика. Завершите одну из отгрузок, чтобы освободить место."
        )
    return FbsControllerCheckTote.objects.create(
        session=session,
        tote=None,
        agency=profile.agency,
        profile=profile,
        opened_by=actor,
    )


@transaction.atomic
def add_controller_check_tote(
    *, session_id: int, tote_scan: str, performed_by
) -> FbsControllerCheckTote:
    _require_writes()
    actor = _actor(performed_by)
    session = _session_for_update(session_id=session_id, actor=actor)
    tote = _resolve_tote_for_update(tote_scan)
    if tote.id == session.unknown_tote_id:
        raise FbsPickingError("Тара неизвестного товара не может быть тарой проверки.")
    _assert_tote_available(tote)
    return _create_check_tote(session=session, tote=tote, actor=actor)


def _batch_profile(batch: FbsPickBatch) -> FbsIntegrationProfile:
    profile_ids = set(batch.tasks.values_list("order__profile_id", flat=True))
    if len(profile_ids) != 1:
        raise FbsPickingError("В таре подбора смешаны разные кабинеты клиента.")
    return FbsIntegrationProfile.objects.select_related("agency").get(
        pk=profile_ids.pop()
    )


def _split_batch_active_quantity(tasks: list[FbsPickTask], field: str) -> int:
    return sum(
        int(getattr(task, field, 0) or 0)
        for task in tasks
        if task.status != FbsPickTask.STATUS_CANCELED
    )


@transaction.atomic
def split_mixed_profile_verification_batch(
    *, batch_id: int, performed_by=None
) -> tuple[FbsPickBatch, ...]:
    """Split an untouched picked legacy wave by profile without stock writes."""
    _require_writes()
    batch = (
        FbsPickBatch.objects.select_for_update(of=("self",))
        .select_related("agency", "cart", "workstation", "created_by", "assigned_to")
        .get(pk=batch_id)
    )
    existing_marker = (
        FbsToteMovement.objects.select_for_update(of=("self",))
        .filter(
            pick_batch=batch,
            details__operation=PROFILE_SPLIT_OPERATION,
        )
        .order_by("id")
        .first()
    )
    if existing_marker is not None:
        root_batch_id = int(existing_marker.details.get("root_batch_id") or batch.id)
        split_batch_ids = list(
            FbsToteMovement.objects.filter(
                details__operation=PROFILE_SPLIT_OPERATION,
                details__root_batch_id=root_batch_id,
            )
            .order_by("id")
            .values_list("pick_batch_id", flat=True)
        )
        return tuple(
            FbsPickBatch.objects.filter(pk__in=split_batch_ids).order_by("id")
        )
    if (
        batch.status != FbsPickBatch.STATUS_VERIFICATION
        or batch.picking_completed_at is None
        or batch.cart_id is None
        or batch.workstation_id is None
    ):
        raise FbsPickingError(
            "Разделить можно только собранную волну, ожидающую контролера."
        )
    if (
        batch.verification_assigned_to_id is not None
        or batch.verification_started_at is not None
        or FbsControllerPickTote.objects.filter(pick_batch=batch).exists()
        or FbsPickVerificationProgress.objects.filter(
            allocation__pick_task__batch=batch
        ).exists()
    ):
        raise FbsPickingError(
            "Проверка волны уже началась; автоматическое разделение запрещено."
        )
    tasks = list(
        FbsPickTask.objects.select_for_update()
        .select_related("order__profile")
        .filter(batch=batch)
        .order_by("sort_order", "id")
    )
    if not tasks or any(
        task.status not in {
            FbsPickTask.STATUS_PICKED,
            FbsPickTask.STATUS_CANCELED,
        }
        for task in tasks
    ):
        raise FbsPickingError("В волне есть незавершенные задания подбора.")
    profile_tasks: dict[int, list[FbsPickTask]] = {}
    for task in tasks:
        if task.order.profile.agency_id != batch.agency_id:
            raise FbsPickingError("В волне обнаружены заказы другого клиента.")
        profile_tasks.setdefault(task.order.profile_id, []).append(task)
    if len(profile_tasks) <= 1:
        return (batch,)
    if FbsOrderStockAllocation.objects.filter(
        pick_task__in=[
            task for task in tasks if task.status == FbsPickTask.STATUS_PICKED
        ]
    ).exclude(status=FbsOrderStockAllocation.STATUS_PICKED).exists():
        raise FbsPickingError("Не все резервы собранной волны завершены.")
    actor = _actor(performed_by or batch.created_by or batch.assigned_to)
    ordered_groups = list(profile_tasks.items())
    result = [batch]
    for _profile_id, group_tasks in ordered_groups[1:]:
        continuation = FbsPickBatch.objects.create(
            agency=batch.agency,
            status=FbsPickBatch.STATUS_VERIFICATION,
            planned_qty=_split_batch_active_quantity(group_tasks, "planned_qty"),
            picked_qty=_split_batch_active_quantity(group_tasks, "picked_qty"),
            assigned_to=batch.assigned_to,
            workstation=batch.workstation,
            cart=None,
            created_by=batch.created_by,
            started_at=batch.started_at,
            claimed_at=batch.claimed_at,
            picking_completed_at=batch.picking_completed_at,
        )
        for sort_order, task in enumerate(group_tasks, start=1):
            task.batch = continuation
            task.sort_order = sort_order
        FbsPickTask.objects.bulk_update(group_tasks, ["batch", "sort_order"])
        result.append(continuation)

    root_tasks = ordered_groups[0][1]
    for sort_order, task in enumerate(root_tasks, start=1):
        task.sort_order = sort_order
    FbsPickTask.objects.bulk_update(root_tasks, ["sort_order"])
    batch.planned_qty = _split_batch_active_quantity(root_tasks, "planned_qty")
    batch.picked_qty = _split_batch_active_quantity(root_tasks, "picked_qty")
    batch.save(update_fields=["planned_qty", "picked_qty", "updated_at"])

    from audit.models import OrderAuditEntry

    for sequence, split_batch in enumerate(result, start=1):
        split_tasks = list(
            split_batch.tasks.select_related("order__profile").order_by(
                "sort_order", "id"
            )
        )
        profile_id = split_tasks[0].order.profile_id
        FbsToteMovement.objects.create(
            tote=batch.cart,
            action=FbsToteMovement.ACTION_HANDOVER,
            source_kind="pick_batch",
            source_code=str(batch.id),
            target_kind="profile_batch",
            target_code=str(split_batch.id),
            pick_batch=split_batch,
            quantity=int(split_batch.picked_qty or 0),
            details={
                "operation": PROFILE_SPLIT_OPERATION,
                "root_batch_id": batch.id,
                "sequence": sequence,
                "profile_id": profile_id,
                "stock_mutated": False,
                "reservations_mutated": False,
            },
            performed_by=actor,
        )
        for task in split_tasks:
            OrderAuditEntry.objects.create(
                order_id=str(task.order_id),
                order_type="fbs_order",
                action="update",
                agency=batch.agency,
                user=actor,
                description=(
                    "Смешанная FBS-волна разделена по кабинету до начала контроля."
                ),
                payload={
                    "source": PROFILE_SPLIT_OPERATION,
                    "root_batch_id": batch.id,
                    "split_batch_id": split_batch.id,
                    "profile_id": profile_id,
                    "stock_mutated": False,
                    "reservations_mutated": False,
                    "skip_chat_bridge": True,
                },
            )
    return tuple(result)


def _existing_handover_batch_for_pick_batch(
    batch: FbsPickBatch,
) -> FbsHandoverBatch | None:
    """Return the single existing shipment shared by orders in a picked tote."""
    order_ids = FbsPickTask.objects.filter(batch_id=batch.id).values_list(
        "order_id", flat=True
    ).distinct()
    assignments = list(
        FbsHandoverOrderAssignment.objects.select_for_update(of=("self",))
        .select_related("batch")
        .filter(order_id__in=order_ids)
        .exclude(status=FbsHandoverOrderAssignment.STATUS_CANCELED)
        .order_by("batch_id", "id")
    )
    batch_ids = {assignment.batch_id for assignment in assignments}
    if len(batch_ids) > 1:
        raise FbsPickingError(
            "В одной таре находятся заказы из разных отгрузок WB. "
            "Разделите их по исходным отгрузкам до начала проверки."
        )
    if not assignments:
        return None
    handover_batch = assignments[0].batch
    if (
        handover_batch.status != FbsHandoverBatch.STATUS_OPEN
        or handover_batch.marketplace_state not in HANDOVER_STATES_ACCEPTING_ORDERS
    ):
        raise FbsPickingError(
            "Заказ из тары уже назначен в закрытую отгрузку. "
            "Передайте заказ оператору FBS для сверки с WB."
        )
    return handover_batch


def _expected_handover_keys_for_pick_batch(
    *,
    batch: FbsPickBatch,
    check_tote: FbsControllerCheckTote,
) -> set[str]:
    """Build the shipment keys a picked tote may use in this check flow."""
    from .handover import wb_handover_compatibility_key

    orders = {}
    for task in (
        batch.tasks.select_related("order__profile")
        .prefetch_related("order__items")
        .order_by("id")
    ):
        orders[task.order_id] = task.order

    keys = set()
    for order in orders.values():
        if order.profile.marketplace == FbsIntegrationProfile.MARKETPLACE_WB:
            keys.add(
                wb_handover_compatibility_key(
                    order,
                    workstation_id=batch.workstation_id,
                    pick_batch_id=batch.id,
                    check_tote_id=check_tote.id,
                )
            )
            continue
        key = (
            f"warehouse:{order.profile.external_warehouse_id}:marketplace:"
            f"{order.profile.marketplace}:workstation:"
            f"{int(batch.workstation_id or 0)}"
        )
        keys.add(f"{key}:check-tote:{check_tote.id}")
    return keys


def _check_tote_accepts_pick_batch_handover(
    *,
    check_tote: FbsControllerCheckTote,
    batch: FbsPickBatch,
) -> bool:
    """Reject a stale logical flow before a picked tote is assigned to it."""
    if check_tote.handover_batch_id is None:
        return True
    existing_handover = _existing_handover_batch_for_pick_batch(batch)
    if existing_handover is not None:
        return existing_handover.id == check_tote.handover_batch_id
    expected_keys = _expected_handover_keys_for_pick_batch(
        batch=batch,
        check_tote=check_tote,
    )
    return expected_keys == {str(check_tote.handover_batch.compatibility_key or "")}


def _existing_check_tote_for_pick_batch(
    *, session: FbsControllerSession, batch: FbsPickBatch,
    profile: FbsIntegrationProfile,
) -> FbsControllerCheckTote | None:
    """Resume only the already-owned shipment; never extend it with new orders.

    The caller holds the controller session and pick batch locks. Lock the
    shipment too, including when it has no flow yet, so concurrent sessions
    cannot both create the one-to-one controller flow.
    """
    handover = _existing_handover_batch_for_pick_batch(batch)
    if handover is None:
        return None
    handover = FbsHandoverBatch.objects.select_for_update().get(pk=handover.id)
    if (
        handover.status != FbsHandoverBatch.STATUS_OPEN
        or handover.marketplace_state not in HANDOVER_STATES_ACCEPTING_ORDERS
    ):
        raise FbsPickingError(
            "Отгрузка тары уже закрыта. Передайте заказ оператору FBS для сверки."
        )
    if handover.profile_id != profile.id:
        raise FbsPickingError("Существующая отгрузка относится к другому кабинету клиента.")
    target = (
        FbsControllerCheckTote.objects.select_for_update(of=("self",))
        .select_related("tote", "handover_batch")
        .filter(handover_batch=handover)
        .first()
    )
    if target is None:
        return None
    if target.session_id != session.id:
        raise FbsPickingError(
            "Эта отгрузка уже проверяется в другой смене контролера. "
            "Передайте тару ответственному контролеру; новый поток создавать не нужно."
        )
    if target.profile_id != profile.id:
        raise FbsPickingError("Поток отгрузки относится к другому кабинету клиента.")
    if target.status not in ACTIVE_CHECK_TOTE_STATUSES:
        raise FbsPickingError(
            "Проверка этой отгрузки уже закрыта или заблокирована. "
            "Передайте тару оператору FBS для сверки."
        )
    if target.pick_totes.exists() or target.status == FbsControllerCheckTote.STATUS_COMPOSITION:
        assigned_order_ids = handover.order_assignments.exclude(
            status=FbsHandoverOrderAssignment.STATUS_CANCELED
        ).values_list("order_id", flat=True)
        if batch.tasks.exclude(status=FbsPickTask.STATUS_CANCELED).exclude(
            order_id__in=assigned_order_ids
        ).exists():
            raise FbsPickingError(
                "В таре есть заказы, которые не закреплены за этой отгрузкой. "
                "Передайте тару оператору FBS; дополнять проверяемый состав нельзя."
            )
    return target


def _route_check_tote_to_existing_handover(
    *,
    session: FbsControllerSession,
    check_tote: FbsControllerCheckTote,
    batch: FbsPickBatch,
    profile: FbsIntegrationProfile,
    actor,
) -> FbsControllerCheckTote:
    """Choose the shipment flow before the first item in the pick tote is scanned."""
    target = _existing_check_tote_for_pick_batch(
        session=session, batch=batch, profile=profile,
    )
    if target is not None:
        return target
    handover_batch = _existing_handover_batch_for_pick_batch(batch)
    if handover_batch is None:
        return check_tote
    if handover_batch.profile_id != profile.id:
        raise FbsPickingError("Существующая отгрузка относится к другому кабинету клиента.")
    if check_tote.handover_batch_id in (None, handover_batch.id):
        changed_fields = []
        if check_tote.profile_id is None:
            check_tote.profile = profile
            check_tote.agency = profile.agency
            changed_fields.extend(["profile", "agency"])
        if check_tote.handover_batch_id is None:
            check_tote.handover_batch = handover_batch
            changed_fields.append("handover_batch")
        if changed_fields:
            check_tote.save(update_fields=[*changed_fields, "updated_at"])
        return check_tote

    target = _create_logical_check_tote(
        session=session,
        profile=profile,
        actor=actor,
    )
    target.handover_batch = handover_batch
    target.save(update_fields=["handover_batch", "updated_at"])
    return target


def _picked_batch_for_control(
    *,
    session: FbsControllerSession,
    pick_tote: FbsPickingCart,
) -> FbsPickBatch:
    batch = (
        FbsPickBatch.objects.select_for_update(of=("self",))
        .select_related("cart", "workstation")
        .filter(
            cart=pick_tote,
            status=FbsPickBatch.STATUS_VERIFICATION,
            picking_completed_at__isnull=False,
            cart_released_at__isnull=True,
        )
        .order_by("picking_completed_at", "id")
        .first()
    )
    if batch is None:
        raise FbsPickingError("В этой таре нет собранной волны, ожидающей контроля.")
    if batch.workstation_id != session.workstation_id:
        raise FbsPickingError("Тара подбора доставлена на другое рабочее место.")
    return batch


def _prefetch_wb_labels_for_pick_batch(
    *,
    batch_id: int,
    check_tote_id: int,
    workstation_id: int,
    requested_by,
) -> None:
    """Prepare WB stickers after a picked tote is accepted at control.

    This is an optimization only: failures are logged and the controller can
    continue with the regular per-order preparation path after verification.
    """
    deadline = time.monotonic() + WB_LABEL_PREFETCH_BUDGET_SECONDS
    try:
        tasks = list(
            FbsPickTask.objects.select_related("order__profile")
            .filter(
                batch_id=batch_id,
                status=FbsPickTask.STATUS_PICKED,
                order__internal_status=FbsOrder.STATUS_PICKED,
                order__profile__marketplace=FbsIntegrationProfile.MARKETPLACE_WB,
            )
            .exclude(
                order__pick_restock_requests__status__in=(
                    SEPARATED_PICK_RESTOCK_STATUSES
                )
            )
            .order_by("sort_order", "id")
        )
    except Exception:
        logger.exception(
            "FBS WB label prefetch failed to read pick batch %s",
            batch_id,
        )
        return

    if time.monotonic() >= deadline:
        logger.info(
            "FBS WB label prefetch deferred for pick batch %s: %.1fs budget exhausted",
            batch_id,
            WB_LABEL_PREFETCH_BUDGET_SECONDS,
        )
        return

    from .handover import _prefetch_wb_order_handover_assignment
    from .labels import prefetch_wb_order_label_request
    from .marketplace import schedule_label_preparation

    for task in tasks:
        if time.monotonic() >= deadline:
            logger.info(
                "FBS WB label prefetch deferred for pick batch %s: %.1fs budget exhausted",
                batch_id,
                WB_LABEL_PREFETCH_BUDGET_SECONDS,
            )
            return
        for attempt in range(3):
            try:
                with transaction.atomic():
                    if connection.vendor == "postgresql":
                        remaining_ms = max(
                            1,
                            int((deadline - time.monotonic()) * 1000),
                        )
                        with connection.cursor() as cursor:
                            cursor.execute(
                                "SELECT set_config('lock_timeout', %s, true)",
                                [f"{remaining_ms}ms"],
                            )
                            cursor.execute(
                                "SELECT set_config('statement_timeout', %s, true)",
                                [f"{remaining_ms}ms"],
                            )
                    prefetch_wb_order_label_request(
                        order_id=task.order_id,
                        requested_by=requested_by,
                    )
                    if time.monotonic() >= deadline:
                        raise _WbLabelPrefetchBudgetExceeded
                    _prefetch_wb_order_handover_assignment(
                        order_id=task.order_id,
                        assigned_by=requested_by,
                        workstation_id=workstation_id,
                        pick_batch_id=batch_id,
                        check_tote_id=check_tote_id,
                    )
                    if time.monotonic() >= deadline:
                        raise _WbLabelPrefetchBudgetExceeded
                    schedule_label_preparation(order_id=task.order_id)
            except _WbLabelPrefetchBudgetExceeded:
                logger.info(
                    "FBS WB label prefetch deferred for pick batch %s: %.1fs budget exhausted",
                    batch_id,
                    WB_LABEL_PREFETCH_BUDGET_SECONDS,
                )
                return
            except OperationalError as exc:
                if _is_database_lock_unavailable(exc):
                    logger.info(
                        "FBS WB label prefetch deferred for order %s in pick batch %s: database row is busy",
                        task.order_id,
                        batch_id,
                    )
                    break
                if _is_database_statement_timeout(exc):
                    logger.info(
                        "FBS WB label prefetch deferred for pick batch %s: %.1fs budget exhausted",
                        batch_id,
                        WB_LABEL_PREFETCH_BUDGET_SECONDS,
                    )
                    return
                if _is_database_deadlock(exc) and attempt < 2:
                    if time.monotonic() >= deadline:
                        logger.info(
                            "FBS WB label prefetch deferred for pick batch %s: %.1fs budget exhausted",
                            batch_id,
                            WB_LABEL_PREFETCH_BUDGET_SECONDS,
                        )
                        return
                    logger.warning(
                        "FBS WB label prefetch deadlock for order %s in pick batch %s; retry %s/2",
                        task.order_id,
                        batch_id,
                        attempt + 1,
                    )
                    time.sleep(0.05 * (attempt + 1))
                    continue
                logger.exception(
                    "FBS WB label prefetch failed for order %s in pick batch %s",
                    task.order_id,
                    batch_id,
                )
            except FbsHandoverError as exc:
                if str(exc) == WB_LABEL_PREFETCH_HANDOVER_CONFLICT:
                    logger.info(
                        "FBS WB label prefetch skipped for order %s in pick batch %s: already assigned to another handover",
                        task.order_id,
                        batch_id,
                    )
                else:
                    logger.exception(
                        "FBS WB label prefetch failed for order %s in pick batch %s",
                        task.order_id,
                        batch_id,
                    )
            except Exception:
                logger.exception(
                    "FBS WB label prefetch failed for order %s in pick batch %s",
                    task.order_id,
                    batch_id,
                )
            break


def _prefetch_ozon_order_barcodes_for_pick_batch(
    *, batch_id: int, requested_by
) -> None:
    """Persist Ozon order barcodes before the first controller product scan."""
    from .labels import prefetch_ozon_order_label_barcode

    order_ids = list(
        FbsPickTask.objects.filter(
            batch_id=batch_id,
            status=FbsPickTask.STATUS_PICKED,
            order__internal_status=FbsOrder.STATUS_PICKED,
            order__profile__marketplace=FbsIntegrationProfile.MARKETPLACE_OZON,
        )
        .exclude(
            order__pick_restock_requests__status__in=(
                SEPARATED_PICK_RESTOCK_STATUSES
            )
        )
        .order_by("sort_order", "id")
        .values_list("order_id", flat=True)
    )
    for order_id in order_ids:
        try:
            prefetch_ozon_order_label_barcode(
                order_id=order_id,
                requested_by=requested_by,
            )
        except Exception:
            # Missing/changed marketplace data is handled by the ordinary
            # label path after verification and must not reject the tote.
            logger.exception(
                "FBS Ozon order-barcode prefetch failed for order %s in pick batch %s",
                order_id,
                batch_id,
            )


def _attach_pick_tote_context(
    *,
    session: FbsControllerSession,
    pick_tote: FbsPickingCart,
    check_tote: FbsControllerCheckTote,
    batch: FbsPickBatch,
    actor,
) -> FbsControllerPickTote:
    profile = _batch_profile(batch)
    check_tote = _route_check_tote_to_existing_handover(
        session=session,
        check_tote=check_tote,
        batch=batch,
        profile=profile,
        actor=actor,
    )
    if not _check_tote_accepts_pick_batch_handover(
        check_tote=check_tote,
        batch=batch,
    ):
        raise FbsPickingError(
            "Тара проверки уже связана с другой отгрузкой и не может принять "
            "эту волну. Используйте новый поток отгрузки."
        )
    if check_tote.handover_batch_id:
        handover_batch = check_tote.handover_batch
        if (
            handover_batch.status != FbsHandoverBatch.STATUS_OPEN
            or handover_batch.marketplace_state
            not in HANDOVER_STATES_ACCEPTING_ORDERS
        ):
            raise FbsPickingError(
                "Связанная отгрузка уже закрыта на маркетплейсе. "
                "Используйте новый поток отгрузки."
            )
    _assert_tote_not_service_reserved(pick_tote)
    active_batch = _active_pick_batch_for_tote(
        pick_tote,
        allowed_pick_batch_id=batch.id,
    )
    if active_batch is not None:
        raise FbsPickingError(
            f"Тара «{pick_tote.name}» уже привязана к волне "
            f"#{active_batch.id}."
        )
    if pick_tote.id == session.unknown_tote_id or (
        check_tote.tote_id and pick_tote.id == check_tote.tote_id
    ):
        raise FbsPickingError("Тара подбора должна отличаться от служебных тар стола.")
    if check_tote.profile_id and check_tote.profile_id != profile.id:
        raise FbsPickingError(
            "В этом потоке отгрузки уже находится товар другого клиента или кабинета."
        )
    existing = FbsControllerPickTote.objects.filter(pick_batch=batch).first()
    if existing is not None:
        if existing.session_id == session.id and existing.check_tote_id == check_tote.id:
            return existing
        raise FbsPickingError("Эта тара подбора уже принята другим контролером.")

    from .picking import claim_pick_batch_verification_by_cart

    claim_pick_batch_verification_by_cart(
        batch_id=batch.id,
        assigned_to=actor,
        workstation_id=session.workstation_id,
        cart_scan=pick_tote.barcode,
    )
    if check_tote.profile_id is None:
        check_tote.profile = profile
        check_tote.agency = profile.agency
    check_tote.status = FbsControllerCheckTote.STATUS_OPEN
    check_tote.item_qty = int(check_tote.item_qty or 0) + int(batch.planned_qty or 0)
    check_tote.save(
        update_fields=["profile", "agency", "status", "item_qty", "updated_at"]
    )
    pick_context = FbsControllerPickTote.objects.create(
        session=session,
        check_tote=check_tote,
        pick_batch=batch,
        tote=pick_tote,
        planned_qty=int(batch.planned_qty or 0),
    )
    _move_tote(
        tote=pick_tote,
        state=FbsToteBinding.STATE_AT_CONTROL,
        workstation=session.workstation,
        performed_by=actor,
        action=FbsToteMovement.ACTION_HANDOVER,
        pick_batch=batch,
        controller_session=session,
        quantity=batch.planned_qty,
        details={
            "check_tote_id": check_tote.id,
            "logical_flow": check_tote.tote_id is None,
        },
    )
    transaction.on_commit(
        lambda: _prefetch_wb_labels_for_pick_batch(
            batch_id=batch.id,
            check_tote_id=check_tote.id,
            workstation_id=session.workstation_id,
            requested_by=actor,
        )
    )
    transaction.on_commit(
        lambda: _prefetch_ozon_order_barcodes_for_pick_batch(
            batch_id=batch.id,
            requested_by=actor,
        )
    )
    return pick_context


@transaction.atomic
def attach_pick_tote_to_check_tote(
    *,
    session_id: int,
    pick_tote_scan: str,
    check_tote_scan: str,
    performed_by,
) -> FbsControllerPickTote:
    _require_writes()
    actor = _actor(performed_by)
    session = _session_for_update(session_id=session_id, actor=actor)
    pick_tote = _resolve_tote_for_update(pick_tote_scan)
    check_tote_value = _scan(check_tote_scan)
    check_tote = (
        FbsControllerCheckTote.objects.select_for_update(of=("self",))
        .select_related("tote", "profile", "agency")
        .filter(
            session=session,
            tote__barcode=check_tote_value,
            status__in=ACTIVE_CHECK_TOTE_STATUSES,
        )
        .first()
    )
    if check_tote is None:
        raise FbsPickingError("Активная тара на проверку не найдена в этой смене.")
    batch = _picked_batch_for_control(session=session, pick_tote=pick_tote)
    return _attach_pick_tote_context(
        session=session,
        pick_tote=pick_tote,
        check_tote=check_tote,
        batch=batch,
        actor=actor,
    )


@transaction.atomic
def attach_pick_tote_to_available_check_tote(
    *,
    session_id: int,
    pick_tote_scan: str,
    performed_by,
) -> FbsControllerPickTote:
    """Route a picked tote without requiring a second physical tote scan."""
    _require_writes()
    actor = _actor(performed_by)
    session = _session_for_update(session_id=session_id, actor=actor)
    pick_tote = _resolve_tote_for_update(pick_tote_scan)
    batch = _picked_batch_for_control(session=session, pick_tote=pick_tote)
    profile = _batch_profile(batch)
    existing_context = (
        FbsControllerPickTote.objects.select_related("check_tote")
        .filter(pick_batch_id=batch.id).first()
    )
    if existing_context is not None:
        if existing_context.session_id != session.id:
            raise FbsPickingError("Эта тара подбора уже принята другим контролером.")
        check_tote = existing_context.check_tote
    else:
        # Resolve already assigned orders BEFORE allocating a new empty flow
        # or applying the three-flow limit. Their shipment owns a unique flow.
        check_tote = _existing_check_tote_for_pick_batch(
            session=session, batch=batch, profile=profile,
        )
    if check_tote is not None:
        return _attach_pick_tote_context(
            session=session, pick_tote=pick_tote, check_tote=check_tote,
            batch=batch, actor=actor,
        )
    available_statuses = (
        FbsControllerCheckTote.STATUS_OPEN,
        FbsControllerCheckTote.STATUS_WAITING_KIZ,
        FbsControllerCheckTote.STATUS_READY,
    )
    candidates = (
        FbsControllerCheckTote.objects.select_for_update(of=("self",))
        .select_related("tote", "handover_batch")
        .filter(session=session, status__in=available_statuses)
        # One physical pick tote owns one controller shipment flow.  A later
        # tote must never extend a flow whose contents are already being
        # checked or prepared for delivery.
        .filter(pick_totes__isnull=True)
        .filter(
            Q(handover_batch__isnull=True)
            | Q(
                handover_batch__status=FbsHandoverBatch.STATUS_OPEN,
                handover_batch__marketplace_state__in=(
                    HANDOVER_STATES_ACCEPTING_ORDERS
                ),
            )
        )
        .order_by("opened_at", "id")
    )
    check_tote = next(
        (
            candidate
            for candidate in candidates.filter(profile=profile)
            if _check_tote_accepts_pick_batch_handover(
                check_tote=candidate,
                batch=batch,
            )
        ),
        None,
    )
    if check_tote is None:
        check_tote = next(
            (
                candidate
                for candidate in candidates.filter(profile__isnull=True)
                if _check_tote_accepts_pick_batch_handover(
                    check_tote=candidate,
                    batch=batch,
                )
            ),
            None,
        )
    if check_tote is None:
        check_tote = _create_logical_check_tote(
            session=session,
            profile=profile,
            actor=actor,
        )
    return _attach_pick_tote_context(
        session=session,
        pick_tote=pick_tote,
        check_tote=check_tote,
        batch=batch,
        actor=actor,
    )


def controller_pick_tote_for_batch(*, batch_id: int) -> FbsControllerPickTote | None:
    return (
        FbsControllerPickTote.objects.select_related(
            "session__workstation", "check_tote", "tote"
        )
        .filter(pick_batch_id=batch_id, status__in=ACTIVE_PICK_TOTE_STATUSES)
        .first()
    )


@transaction.atomic
def align_controller_pick_tote_to_existing_handover(
    *, batch_id: int, order_id: int, performed_by
) -> FbsControllerPickTote | None:
    """Keep a re-wave order in its existing marketplace shipment flow.

    An invalid-KIZ re-wave can already belong to a replacement WB supply before
    the new pick tote reaches control.  If that tote was attached to another
    open logical flow, move the still-unprocessed tote context to a flow bound
    to the existing assignment instead of trying to assign the order twice.
    """
    _require_writes()
    actor = _actor(performed_by)
    pick_context = (
        FbsControllerPickTote.objects.select_for_update(of=("self",))
        .select_related(
            "session__workstation",
            "check_tote__tote",
            "check_tote__handover_batch",
            "tote",
        )
        .filter(
            pick_batch_id=batch_id,
            status__in=ACTIVE_PICK_TOTE_STATUSES,
        )
        .first()
    )
    if pick_context is None:
        return None
    assignment = (
        FbsHandoverOrderAssignment.objects.select_for_update(of=("self",))
        .select_related("batch", "order__profile__agency")
        .filter(order_id=order_id)
        .first()
    )
    if assignment is None:
        return pick_context
    source = (
        FbsControllerCheckTote.objects.select_for_update(of=("self",))
        .select_related("tote", "handover_batch")
        .get(pk=pick_context.check_tote_id)
    )
    if source.handover_batch_id in (None, assignment.batch_id):
        return pick_context
    target_batch = assignment.batch
    if (
        target_batch.status != FbsHandoverBatch.STATUS_OPEN
        or target_batch.marketplace_state not in HANDOVER_STATES_ACCEPTING_ORDERS
    ):
        raise FbsPickingError(
            "Заказ уже назначен в закрытую отгрузку. "
            "Передайте заказ оператору FBS для проверки статуса маркетплейса."
        )
    if int(pick_context.processed_qty or 0) > 0 or pick_context.orders.exclude(
        status=FbsControllerToteOrder.STATUS_REMOVED
    ).exists():
        raise FbsPickingError(
            "Тара уже частично обработана в другой отгрузке. "
            "Завершите текущий поток или передайте заказ оператору FBS."
        )
    target = (
        FbsControllerCheckTote.objects.select_for_update(of=("self",))
        .select_related("tote", "handover_batch")
        .filter(
            session_id=pick_context.session_id,
            profile_id=assignment.order.profile_id,
            handover_batch_id=assignment.batch_id,
            status__in=(
                FbsControllerCheckTote.STATUS_OPEN,
                FbsControllerCheckTote.STATUS_WAITING_KIZ,
                FbsControllerCheckTote.STATUS_READY,
            ),
        )
        .order_by("opened_at", "id")
        .first()
    )
    if target is None:
        target = _create_logical_check_tote(
            session=pick_context.session,
            profile=assignment.order.profile,
            actor=actor,
        )
        target.handover_batch = target_batch
        target.save(update_fields=["handover_batch", "updated_at"])

    units = max(
        int(pick_context.planned_qty or 0) - int(pick_context.processed_qty or 0),
        0,
    )
    source.item_qty = max(
        int(source.labeled_qty or 0),
        int(source.item_qty or 0) - units,
    )
    source.save(update_fields=["item_qty", "updated_at"])
    target.status = FbsControllerCheckTote.STATUS_OPEN
    target.item_qty = int(target.item_qty or 0) + units
    target.save(update_fields=["status", "item_qty", "updated_at"])
    pick_context.check_tote = target
    pick_context.save(update_fields=["check_tote", "updated_at"])

    source_code = (
        source.tote.barcode if source.tote_id else f"FBS-FLOW-{source.id:06d}"
    )
    target_code = (
        target.tote.barcode if target.tote_id else f"FBS-FLOW-{target.id:06d}"
    )
    FbsToteMovement.objects.create(
        tote=pick_context.tote,
        action=FbsToteMovement.ACTION_HANDOVER,
        source_kind="shipment_flow",
        source_code=source_code,
        target_kind="shipment_flow",
        target_code=target_code,
        pick_batch_id=batch_id,
        controller_session_id=pick_context.session_id,
        handover_batch=target_batch,
        quantity=units,
        details={
            "reason": "existing_handover_assignment",
            "order_id": order_id,
            "source_check_tote_id": source.id,
            "target_check_tote_id": target.id,
        },
        performed_by=actor,
    )
    pick_context.check_tote = target
    return pick_context


@transaction.atomic
def bind_pick_tote_to_picker(*, batch_id: int, performed_by) -> FbsToteBinding:
    """Track a free tote taken by a picker without touching product quantities."""
    actor = _actor(performed_by)
    batch = (
        FbsPickBatch.objects.select_for_update(of=("self",))
        .select_related("cart")
        .get(pk=batch_id)
    )
    if batch.cart_id is None:
        raise FbsPickingError("У волны не указана тара.")
    _assert_tote_available(batch.cart, allowed_pick_batch_id=batch.id)
    return _move_tote(
        tote=batch.cart,
        state=FbsToteBinding.STATE_PICKING,
        employee=actor,
        performed_by=actor,
        action=FbsToteMovement.ACTION_TAKE,
        pick_batch=batch,
        quantity=batch.planned_qty,
    )


@transaction.atomic
def bind_pick_tote_to_workstation(*, batch_id: int, performed_by) -> FbsToteBinding:
    """Track a completed tote delivered to its recommended controller desk."""
    actor = _actor(performed_by)
    batch = (
        FbsPickBatch.objects.select_for_update(of=("self",))
        .select_related("cart", "workstation")
        .get(pk=batch_id)
    )
    if batch.cart_id is None or batch.workstation_id is None:
        raise FbsPickingError("У волны не указаны тара и рабочее место.")
    _assert_tote_not_service_reserved(batch.cart)
    active_batch = _active_pick_batch_for_tote(
        batch.cart,
        allowed_pick_batch_id=batch.id,
    )
    if active_batch is not None:
        raise FbsPickingError(
            f"Тара «{batch.cart.name}» уже привязана к волне "
            f"#{active_batch.id}."
        )
    return _move_tote(
        tote=batch.cart,
        state=FbsToteBinding.STATE_WAITING_CONTROL,
        workstation=batch.workstation,
        performed_by=actor,
        action=FbsToteMovement.ACTION_HANDOVER,
        pick_batch=batch,
        quantity=batch.picked_qty,
    )


@transaction.atomic
def confirm_order_label_to_check_tote(
    *, label_id: int, label_scan: str, pick_batch_id: int, performed_by
) -> FbsControllerToteOrder:
    _require_writes()
    actor = _actor(performed_by)
    pick_context = (
        FbsControllerPickTote.objects.select_for_update(of=("self",))
        .select_related(
            "session__workstation",
            "check_tote__profile",
            "pick_batch",
        )
        .get(
            pick_batch_id=pick_batch_id,
            status=FbsControllerPickTote.STATUS_PROCESSING,
        )
    )
    if pick_context.session.controller_id != actor.id:
        raise FbsPickingError("Тару подбора обрабатывает другой контролер.")
    label = FbsOrderLabel.objects.select_for_update().select_related("order").get(pk=label_id)
    if not pick_context.pick_batch.tasks.filter(order_id=label.order_id).exists():
        raise FbsPickingError("Этикетка относится к заказу другой тары подбора.")
    marketplace = pick_context.check_tote.profile.marketplace
    trusted_wb_primary_scan = bool(
        marketplace == FbsIntegrationProfile.MARKETPLACE_WB
        and controller_skips_repeat_wb_label_scan(actor)
    )
    existing = (
        FbsControllerToteOrder.objects.exclude(
            status=FbsControllerToteOrder.STATUS_REMOVED
        )
        .select_for_update(of=("self",))
        .select_related("check_tote__profile", "check_tote__handover_batch", "label")
        .filter(order=label.order)
        .first()
    )
    if existing is not None:
        if existing.check_tote_id == pick_context.check_tote_id:
            if str(label_scan or "").strip() != str(
                existing.label.barcode or ""
            ).strip():
                raise FbsPickingError("Скан не совпадает с этикеткой этого заказа.")
            if (
                marketplace == FbsIntegrationProfile.MARKETPLACE_OZON
                or trusted_wb_primary_scan
            ):
                if trusted_wb_primary_scan:
                    assignment = (
                        FbsHandoverOrderAssignment.objects.exclude(
                            status=FbsHandoverOrderAssignment.STATUS_CANCELED
                        )
                        .filter(order_id=existing.order_id)
                        .first()
                    )
                    if assignment is None:
                        raise FbsPickingError(
                            "Поставка WB не была подготовлена до первичного скана. "
                            "Для этого заказа выполните обычную повторную проверку."
                        )
                    if existing.check_tote.handover_batch_id != assignment.batch_id:
                        raise FbsPickingError(
                            "Заказ уже назначен в другую отгрузку WB."
                        )
                else:
                    from .handover import ensure_order_handover_assignment

                    ensure_order_handover_assignment(
                        order_id=existing.order_id,
                        assigned_by=actor,
                        workstation_id=pick_context.session.workstation_id,
                        pick_batch_id=pick_context.pick_batch_id,
                        check_tote_id=existing.check_tote_id,
                    )
                existing.check_tote.refresh_from_db(
                    fields=["profile", "handover_batch", "status"]
                )
                existing.primary_order_label_scan_reused = True
                existing.save(
                    update_fields=[
                        "primary_order_label_scan_reused",
                        "updated_at",
                    ]
                )
                if marketplace == FbsIntegrationProfile.MARKETPLACE_OZON:
                    existing = _pack_check_tote_order(
                        check_tote=existing.check_tote,
                        tote_order=existing,
                        label_scan=label_scan,
                        actor=actor,
                        primary_scan_reused=True,
                    )
                refresh_check_tote_status(check_tote_id=existing.check_tote_id)
            return existing
        raise FbsPickingError("Заказ уже находится в другой таре проверки.")

    if trusted_wb_primary_scan:
        scan_value = str(label_scan or "").strip()
        if not scan_value or scan_value != str(label.barcode or "").strip():
            raise FbsPickingError("Скан не совпадает с этикеткой этого заказа.")
        # WB assignment must be prepared while the order is still in PICKED.
        # The marketplace confirmation itself remains asynchronous and is a
        # hard gate for the background finalizer below.
        from .handover import ensure_order_handover_assignment

        ensure_order_handover_assignment(
            order_id=label.order_id,
            assigned_by=actor,
            workstation_id=pick_context.session.workstation_id,
            pick_batch_id=pick_context.pick_batch_id,
            check_tote_id=pick_context.check_tote_id,
        )

    from .labels import confirm_order_label_scan

    label = confirm_order_label_scan(
        label_id=label.id,
        label_scan=label_scan,
        performed_by=actor,
    )
    tote_order = FbsControllerToteOrder.objects.create(
        check_tote=pick_context.check_tote,
        pick_tote=pick_context,
        order=label.order,
        label=label,
        units=sum(int(item.quantity or 0) for item in label.order.items.all()) or 1,
        label_confirmed_by=actor,
        primary_order_label_scan_reused=trusted_wb_primary_scan,
    )
    units = int(tote_order.units or 0)
    FbsControllerPickTote.objects.filter(pk=pick_context.pk).update(
        processed_qty=F("processed_qty") + units,
        updated_at=timezone.now(),
    )
    FbsControllerCheckTote.objects.filter(pk=pick_context.check_tote_id).update(
        labeled_qty=F("labeled_qty") + units,
        updated_at=timezone.now(),
    )
    is_logical_flow = pick_context.check_tote.tote_id is None
    movement_tote = (
        pick_context.tote
        if is_logical_flow
        else pick_context.check_tote.tote
    )
    target_code = (
        f"FBS-FLOW-{pick_context.check_tote_id:06d}"
        if is_logical_flow
        else pick_context.check_tote.tote.barcode
    )
    FbsToteMovement.objects.create(
        tote=movement_tote,
        action=FbsToteMovement.ACTION_CHECK,
        source_kind="pick_tote",
        source_code=pick_context.tote.barcode,
        target_kind="shipment_flow" if is_logical_flow else "check_tote",
        target_code=target_code,
        pick_batch=pick_context.pick_batch,
        controller_session=pick_context.session,
        handover_batch=pick_context.check_tote.handover_batch,
        quantity=units,
        details={
            "order_id": label.order_id,
            "label_id": label.id,
            "logical_flow": is_logical_flow,
        },
        performed_by=actor,
    )
    if marketplace == FbsIntegrationProfile.MARKETPLACE_OZON:
        # The controller has physically matched the checked product with the
        # exact Ozon order QR.  Reuse that scan as the box-composition fact so
        # shipment does not require the same order label a second time.  Ozon
        # metadata and the official label remain hard gates when the box is
        # closed; this path only creates local assignment/composition records.
        from .handover import ensure_order_handover_assignment

        ensure_order_handover_assignment(
            order_id=tote_order.order_id,
            assigned_by=actor,
            workstation_id=pick_context.session.workstation_id,
            pick_batch_id=pick_context.pick_batch_id,
            check_tote_id=pick_context.check_tote_id,
        )
        pick_context.check_tote.refresh_from_db(
            fields=["profile", "handover_batch", "status"]
        )
        tote_order = _pack_check_tote_order(
            check_tote=pick_context.check_tote,
            tote_order=tote_order,
            label_scan=label_scan,
            actor=actor,
            primary_scan_reused=True,
        )
    refresh_check_tote_status(check_tote_id=pick_context.check_tote_id)
    from .picking import refresh_pick_batch_verification

    refresh_pick_batch_verification(batch_id=pick_context.pick_batch_id)
    return tote_order


@transaction.atomic
def record_unknown_tote_item(
    *,
    pick_batch_id: int,
    scanned_value: str,
    comment: str,
    performed_by,
    deduplicate: bool = False,
) -> FbsUnknownToteItem:
    _require_writes()
    actor = _actor(performed_by)
    value = str(scanned_value or "").strip()
    if not value:
        raise FbsPickingError("Отсканируйте неизвестный товар.")
    pick_context = (
        FbsControllerPickTote.objects.select_for_update()
        .select_related("session__unknown_tote", "pick_batch", "tote")
        .get(pick_batch_id=pick_batch_id, status=FbsControllerPickTote.STATUS_PROCESSING)
    )
    if pick_context.session.controller_id != actor.id:
        raise FbsPickingError("Тару подбора обрабатывает другой контролер.")
    unknown_binding = _binding_for_update(pick_context.session.unknown_tote)
    conflicting_batch = _active_pick_batch_for_tote(
        pick_context.session.unknown_tote,
    )
    if (
        unknown_binding.state != FbsToteBinding.STATE_UNKNOWN
        or unknown_binding.workstation_id != pick_context.session.workstation_id
        or unknown_binding.controller_session_id != pick_context.session_id
        or conflicting_batch is not None
        or FbsControllerCheckTote.objects.filter(
            tote=pick_context.session.unknown_tote,
            status__in=ACTIVE_CHECK_TOTE_STATUSES,
        ).exists()
    ):
        raise FbsPickingError(
            "Служебная тара неизвестного товара занята другим процессом. "
            "Обратитесь к кладовщику."
        )
    if deduplicate:
        existing = (
            FbsUnknownToteItem.objects.select_for_update()
            .filter(
                session=pick_context.session,
                source_pick_tote=pick_context,
                scanned_value=value,
                status=FbsUnknownToteItem.STATUS_WAITING,
            )
            .order_by("id")
            .first()
        )
        if existing is not None:
            return existing
    row = FbsUnknownToteItem.objects.create(
        session=pick_context.session,
        unknown_tote=pick_context.session.unknown_tote,
        source_pick_tote=pick_context,
        scanned_value=value,
        comment=str(comment or "").strip(),
        reported_by=actor,
    )
    FbsControllerPickTote.objects.filter(pk=pick_context.pk).update(
        unknown_qty=F("unknown_qty") + 1,
        updated_at=timezone.now(),
    )
    FbsToteMovement.objects.create(
        tote=pick_context.session.unknown_tote,
        action=FbsToteMovement.ACTION_UNKNOWN,
        source_kind="pick_tote",
        source_code=pick_context.tote.barcode,
        target_kind="unknown_tote",
        target_code=pick_context.session.unknown_tote.barcode,
        pick_batch=pick_context.pick_batch,
        controller_session=pick_context.session,
        quantity=1,
        details={"unknown_item_id": row.id, "scan": value},
        performed_by=actor,
    )
    return row


def _create_problem_tote_item(
    *,
    session: FbsControllerSession,
    problem_tote: FbsPickingCart,
    scanned_value: str,
    reason: str,
    reported_by,
    quantity: int = 1,
    source_pick_tote: FbsControllerPickTote | None = None,
    source_check_tote: FbsControllerCheckTote | None = None,
    order=None,
    order_item=None,
    severity: str = FbsProblemToteItem.SEVERITY_NONCRITICAL,
) -> FbsProblemToteItem:
    return FbsProblemToteItem.objects.create(
        session=session,
        problem_tote=problem_tote,
        source_pick_tote=source_pick_tote,
        source_check_tote=source_check_tote,
        order=order,
        order_item=order_item,
        scanned_value=scanned_value,
        reason=reason,
        severity=severity,
        quantity=max(int(quantity or 0), 1),
        reported_by=reported_by,
    )


@transaction.atomic
def record_extra_problem_tote_item(
    *,
    pick_batch_id: int,
    scanned_value: str,
    performed_by,
) -> FbsProblemToteItem:
    """Register one physically extra unit in the controller's problem tote."""
    _require_writes()
    actor = _actor(performed_by)
    value = str(scanned_value or "").strip()
    if not value:
        raise FbsPickingError("Отсканируйте лишний товар.")
    session, problem_tote = controller_service_tote_for_actor(
        actor=actor,
        purpose=SERVICE_TOTE_PROBLEM,
    )
    pick_context = (
        FbsControllerPickTote.objects.select_for_update()
        .select_related("pick_batch", "tote", "check_tote")
        .filter(
            pick_batch_id=pick_batch_id,
            session=session,
            status=FbsControllerPickTote.STATUS_PROCESSING,
        )
        .first()
    )
    if pick_context is None:
        raise FbsPickingError("Активная тара подбора для лишнего товара не найдена.")
    row = _create_problem_tote_item(
        session=session,
        problem_tote=problem_tote,
        source_pick_tote=pick_context,
        source_check_tote=pick_context.check_tote,
        scanned_value=value,
        reason=EXTRA_PROBLEM_REASON,
        severity=FbsProblemToteItem.SEVERITY_NONCRITICAL,
        reported_by=actor,
    )
    FbsToteMovement.objects.create(
        tote=problem_tote,
        action=FbsToteMovement.ACTION_PLACE,
        source_kind="pick_tote",
        source_code=pick_context.tote.barcode,
        target_kind="problem_tote",
        target_code=problem_tote.barcode,
        pick_batch=pick_context.pick_batch,
        controller_session=session,
        quantity=1,
        details={
            "problem_item_id": row.id,
            "scan": value,
            "reason": EXTRA_PROBLEM_REASON,
            "severity": FbsProblemToteItem.SEVERITY_NONCRITICAL,
        },
        performed_by=actor,
    )
    return row


@transaction.atomic
def record_invalid_kiz_problem_tote_item(
    *,
    allocation_id: int,
    handover_batch_id: int,
    route: str,
    problem_tote_scan: str,
    performed_by,
) -> FbsProblemToteItem:
    """Record the exact rejected unit in the problem tote of its workstation.

    The binding is intentionally workstation-bound rather than controller-bound:
    another authorized operator may continue the same desk without changing its
    equipment, totes or assigned work.
    """
    _require_writes()
    actor = _actor(performed_by)
    _assert_check_tote_operator(actor)
    route = str(route or "").strip().casefold()
    if route not in {"rewave", "quarantine"}:
        raise FbsPickingError("Выберите маршрут проблемного заказа.")
    scanned_tote = _resolve_tote_for_update(problem_tote_scan)
    allocation = (
        FbsOrderStockAllocation.objects.select_for_update(of=("self",))
        .select_related(
            "order_item__order__profile",
            "balance__box__pallet__cell",
            "pick_task__batch__workstation",
            "traceability",
        )
        .get(pk=allocation_id)
    )
    if (
        allocation.status != FbsOrderStockAllocation.STATUS_PICKED
        or int(allocation.qty_picked or 0) <= 0
        or allocation.pick_task_id is None
    ):
        raise FbsPickingError(
            "Проблемная единица не подтверждена как физически отобранная."
        )
    order = allocation.order_item.order
    handover_batch = (
        FbsHandoverBatch.objects.select_for_update(of=("self",))
        .select_related("profile")
        .get(pk=handover_batch_id)
    )
    if order.profile_id != handover_batch.profile_id:
        raise FbsPickingError("Заказ относится к другому кабинету клиента.")

    source_pick_tote = (
        FbsControllerPickTote.objects.select_for_update(of=("self",))
        .select_related("session__workstation", "check_tote", "tote")
        .filter(pick_batch_id=allocation.pick_task.batch_id)
        .first()
    )
    workstation_id = allocation.pick_task.batch.workstation_id
    if workstation_id is None and source_pick_tote is not None:
        workstation_id = source_pick_tote.session.workstation_id
    if workstation_id is None:
        raise FbsPickingError(
            "У исходной волны не указан рабочий стол контролера. "
            "Обратитесь к начальнику склада."
        )
    session = (
        FbsControllerSession.objects.select_for_update(of=("self",))
        .select_related("workstation", "problem_tote")
        .filter(
            workstation_id=workstation_id,
            status=FbsControllerSession.STATUS_ACTIVE,
        )
        .first()
    )
    if session is None or session.problem_tote_id is None:
        raise FbsPickingError(
            "На рабочем столе исходной волны не открыта проблемная тара."
        )
    expected_tote = session.problem_tote
    if scanned_tote.id != expected_tote.id:
        raise FbsPickingError(
            f"Отсканирована другая тара. Положите товар в проблемную тару "
            f"«{expected_tote.name}» ({expected_tote.barcode}) этого стола и "
            "отсканируйте ее QR."
        )
    binding = _binding_for_update(expected_tote)
    if (
        binding.state != FbsToteBinding.STATE_AT_CONTROL
        or binding.workstation_id != workstation_id
        or binding.controller_session_id != session.id
    ):
        raise FbsPickingError(
            "Проблемная тара перемещена или отвязана от рабочего стола. "
            "Обратитесь к кладовщику."
        )

    scanned_value = str(allocation.traceability.marking_code or "").strip()
    if not scanned_value:
        raise FbsPickingError(
            "У физически отобранной единицы не найден КИЗ. "
            "Пересканируйте товар перед маршрутизацией."
        )
    existing = (
        FbsProblemToteItem.objects.select_for_update()
        .filter(
            order_item=allocation.order_item,
            scanned_value=scanned_value,
            status=FbsProblemToteItem.STATUS_IN_TOTE,
        )
        .order_by("id")
        .first()
    )
    if existing is not None:
        if existing.problem_tote_id != expected_tote.id:
            raise FbsPickingError(
                "Эта физическая единица уже зарегистрирована в другой "
                "проблемной таре. Обратитесь к кладовщику."
            )
        return existing
    if FbsProblemToteItem.objects.select_for_update().filter(
        scanned_value=scanned_value,
        status=FbsProblemToteItem.STATUS_IN_TOTE,
    ).exists():
        raise FbsPickingError(
            "Этот КИЗ уже зарегистрирован в проблемной таре другого заказа."
        )

    route_label = "новая волна" if route == "rewave" else "карантин кладовщика"
    reason = (
        f"Невалидный КИЗ заказа {order.external_order_id}; "
        f"выбран маршрут: {route_label}."
    )
    row = _create_problem_tote_item(
        session=session,
        problem_tote=expected_tote,
        source_pick_tote=source_pick_tote,
        source_check_tote=(source_pick_tote.check_tote if source_pick_tote else None),
        order=order,
        order_item=allocation.order_item,
        scanned_value=scanned_value,
        reason=reason,
        severity=FbsProblemToteItem.SEVERITY_CRITICAL,
        reported_by=actor,
    )
    source_box = allocation.balance.box
    source_cell = source_box.pallet.cell
    FbsToteMovement.objects.create(
        tote=expected_tote,
        action=FbsToteMovement.ACTION_PLACE,
        source_kind="pick_tote" if source_pick_tote is not None else "fbs_box",
        source_code=(
            source_pick_tote.tote.barcode
            if source_pick_tote is not None
            else source_box.box_code
        ),
        target_kind="problem_tote",
        target_code=expected_tote.barcode,
        pick_batch=allocation.pick_task.batch,
        controller_session=session,
        handover_batch=handover_batch,
        quantity=1,
        details={
            "problem_item_id": row.id,
            "allocation_id": allocation.id,
            "order_id": order.id,
            "order_item_id": allocation.order_item_id,
            "external_order_id": order.external_order_id,
            "scan": scanned_value,
            "route": route,
            "source_balance_id": allocation.balance_id,
            "source_box": source_box.box_code,
            "source_cell": source_cell.cell_code,
            "physically_confirmed": True,
        },
        performed_by=actor,
    )
    return row


def _pick_tote_unprocessed_order_ids(pick_context: FbsControllerPickTote) -> list[int]:
    picked_order_ids = set(
        pick_context.pick_batch.tasks.filter(status=FbsPickTask.STATUS_PICKED)
        .values_list("order_id", flat=True)
    )
    labeled_order_ids = set(
        pick_context.orders.exclude(status=FbsControllerToteOrder.STATUS_REMOVED)
        .values_list("order_id", flat=True)
    )
    separated_order_ids = set(
        FbsPickRestockRequest.objects.filter(
            batch=pick_context.pick_batch,
            order_id__in=picked_order_ids,
            status__in=SEPARATED_PICK_RESTOCK_STATUSES,
        ).values_list("order_id", flat=True)
    )
    return sorted(picked_order_ids - labeled_order_ids - separated_order_ids)


@transaction.atomic
def mark_pick_tote_awaiting_empty(*, pick_batch_id: int) -> bool:
    pick_context = (
        FbsControllerPickTote.objects.select_for_update()
        .filter(pick_batch_id=pick_batch_id)
        .first()
    )
    if pick_context is None or pick_context.status != FbsControllerPickTote.STATUS_PROCESSING:
        return False
    if _pick_tote_unprocessed_order_ids(pick_context):
        return False
    pick_context.status = FbsControllerPickTote.STATUS_AWAITING_EMPTY
    pick_context.save(update_fields=["status", "updated_at"])
    return True


def _pending_profile_split_continuation(
    pick_context: FbsControllerPickTote,
) -> FbsPickBatch | None:
    marker = (
        FbsToteMovement.objects.select_for_update(of=("self",))
        .filter(
            pick_batch_id=pick_context.pick_batch_id,
            tote_id=pick_context.tote_id,
            details__operation=PROFILE_SPLIT_OPERATION,
        )
        .order_by("id")
        .first()
    )
    if marker is None:
        return None
    root_batch_id = int(
        marker.details.get("root_batch_id") or pick_context.pick_batch_id
    )
    continuation_ids = list(
        FbsToteMovement.objects.filter(
            tote_id=pick_context.tote_id,
            details__operation=PROFILE_SPLIT_OPERATION,
            details__root_batch_id=root_batch_id,
        )
        .exclude(pick_batch_id=pick_context.pick_batch_id)
        .order_by("id")
        .values_list("pick_batch_id", flat=True)
    )
    if not continuation_ids:
        return None
    return (
        FbsPickBatch.objects.select_for_update(of=("self",))
        .filter(
            pk__in=continuation_ids,
            status=FbsPickBatch.STATUS_VERIFICATION,
            picking_completed_at__isnull=False,
            cart__isnull=True,
            cart_released_at__isnull=True,
            completed_at__isnull=True,
        )
        .order_by("id")
        .first()
    )


@transaction.atomic
def confirm_pick_tote_empty(*, pick_batch_id: int, performed_by) -> FbsControllerPickTote:
    _require_writes()
    actor = _actor(performed_by)
    pick_context = (
        FbsControllerPickTote.objects.select_for_update()
        .select_related("session__free_zone", "session__workstation", "pick_batch", "tote")
        .get(pick_batch_id=pick_batch_id)
    )
    if pick_context.session.controller_id != actor.id:
        raise FbsPickingError("Тару подбора обрабатывает другой контролер.")
    if pick_context.status == FbsControllerPickTote.STATUS_CLOSED:
        return pick_context
    missing_order_ids = _pick_tote_unprocessed_order_ids(pick_context)
    if missing_order_ids:
        raise FbsPickingError(
            f"В таре числится необработанных заказов: {len(missing_order_ids)}."
        )
    batch = FbsPickBatch.objects.select_for_update().get(pk=pick_context.pick_batch_id)
    continuation = _pending_profile_split_continuation(pick_context)
    now = timezone.now()
    batch.cart_released_at = now
    batch.save(update_fields=["cart_released_at", "updated_at"])
    pick_context.status = FbsControllerPickTote.STATUS_CLOSED
    pick_context.closed_at = now
    if continuation is None:
        pick_context.empty_confirmed_at = now
    pick_context.save(
        update_fields=["status", "empty_confirmed_at", "closed_at", "updated_at"]
    )
    if continuation is not None:
        continuation.cart = pick_context.tote
        continuation.save(update_fields=["cart", "updated_at"])
        _move_tote(
            tote=pick_context.tote,
            state=FbsToteBinding.STATE_AT_CONTROL,
            workstation=pick_context.session.workstation,
            performed_by=actor,
            action=FbsToteMovement.ACTION_HANDOVER,
            pick_batch=continuation,
            controller_session=pick_context.session,
            quantity=continuation.picked_qty,
            details={
                "operation": PROFILE_SPLIT_OPERATION,
                "continued_from_batch_id": batch.id,
                "physical_tote_empty": False,
            },
        )
        refresh_check_tote_status(check_tote_id=pick_context.check_tote_id)
        return pick_context
    _move_tote(
        tote=pick_context.tote,
        state=FbsToteBinding.STATE_FREE,
        zone=pick_context.session.free_zone,
        performed_by=actor,
        action=FbsToteMovement.ACTION_RELEASE,
        pick_batch=batch,
        controller_session=pick_context.session,
        quantity=pick_context.processed_qty,
        details={"empty_confirmed": True},
    )
    refresh_check_tote_status(check_tote_id=pick_context.check_tote_id)
    return pick_context


def auto_release_pick_tote_if_complete(
    *, pick_batch_id: int
) -> FbsControllerPickTote | None:
    """Освободить тару подбора, когда по волне не осталось необработанных заказов.

    Возвращает закрытую тару либо None, если освобождать нечего или рано.

    Исполнителем записывается контролер, которому принадлежит сессия тары:
    вызов идет из середины чужих транзакций, где своего актора нет.

    Блокировку и идемпотентность обеспечивает confirm_pick_tote_empty: она берет
    строку под select_for_update и выходит сразу, если тара уже закрыта.
    Отказ службы (выключенный флаг, чужой контролер) не должен ронять ни закрытие
    волны, ни скан контролера, поэтому FbsError гасится — тара просто остается
    ждать кнопку на рабочем столе, как было раньше.
    """
    pick_context = (
        FbsControllerPickTote.objects.select_related("session__controller", "pick_batch")
        .filter(pick_batch_id=pick_batch_id)
        .first()
    )
    if pick_context is None:
        return None
    if pick_context.status in (
        FbsControllerPickTote.STATUS_CLOSED,
        FbsControllerPickTote.STATUS_PROBLEM,
    ):
        return None
    if _pick_tote_unprocessed_order_ids(pick_context):
        return None
    controller = pick_context.session.controller
    if controller is None:
        return None
    try:
        return confirm_pick_tote_empty(
            pick_batch_id=pick_batch_id,
            performed_by=controller,
        )
    except FbsError:
        return None


def active_check_tote_orders(check_tote: FbsControllerCheckTote):
    return (
        check_tote.orders.exclude(status=FbsControllerToteOrder.STATUS_REMOVED)
        .exclude(
            order__handover_assignment__status=(
                FbsHandoverOrderAssignment.STATUS_CANCELED
            )
        )
        .exclude(
            order__pick_restock_requests__status__in=(
                SEPARATED_PICK_RESTOCK_STATUSES
            )
        )
        .distinct()
    )


def check_tote_readiness(check_tote: FbsControllerCheckTote) -> CheckToteReadiness:
    from .traceability import (
        marketplace_metadata_transfer_resolved,
        metadata_requirements,
    )

    tote_reasons: list[str] = []
    order_reasons: list[str] = []
    if check_tote.pick_totes.filter(
        status=FbsControllerPickTote.STATUS_PROCESSING
    ).exists():
        tote_reasons.append("Не завершена обработка тары подбора.")
    if check_tote.pick_totes.filter(
        status=FbsControllerPickTote.STATUS_AWAITING_EMPTY
    ).exists():
        tote_reasons.append("Не подтверждена пустота тары подбора.")
    pending_ozon_canceled_returns = (
        check_tote.orders.exclude(status=FbsControllerToteOrder.STATUS_REMOVED)
        .filter(
            order__profile__marketplace=FbsIntegrationProfile.MARKETPLACE_OZON,
            order__pick_restock_requests__status=(
                FbsPickRestockRequest.STATUS_QUEUED
            ),
            order__pick_restock_requests__source_tote__isnull=True,
        )
        .values("order_id")
        .distinct()
        .count()
    )
    if pending_ozon_canceled_returns:
        tote_reasons.append(
            "Переложите отмененные заказы Ozon в тару отмененных заказов: "
            f"{pending_ozon_canceled_returns}."
        )
    active_orders = active_check_tote_orders(check_tote)
    if not active_orders.exists():
        tote_reasons.append("В таре проверки пока нет заказов.")
    order_ids = list(active_orders.values_list("order_id", flat=True))
    current_pick_task_ids = list(
        active_orders.values_list(
            "pick_tote__pick_batch__tasks__id",
            flat=True,
        ).distinct()
    )
    order_items = list(
        FbsOrderItem.objects.filter(order_id__in=order_ids)
        .select_related("sku", "order__profile")
        .order_by("order_id", "id")
    )
    metadata_transfers = FbsMarketplaceMetadataTransfer.objects.filter(
        order_item_id__in=[item.id for item in order_items],
    )
    if current_pick_task_ids:
        metadata_transfers = metadata_transfers.filter(
            Q(traceability__isnull=True)
            | Q(
                traceability__allocation__pick_task_id__in=(
                    current_pick_task_ids
                )
            )
        )
    transfer_resolutions: dict[tuple[int, str], list[bool]] = {}
    transfer_types_by_item: dict[int, set[str]] = {}
    for transfer in metadata_transfers.select_related(
        "order_item__order__profile"
    ):
        key = (transfer.order_item.order_id, transfer.metadata_type)
        transfer_resolutions.setdefault(key, []).append(
            marketplace_metadata_transfer_resolved(transfer)
        )
        transfer_types_by_item.setdefault(
            transfer.order_item_id, set()
        ).add(transfer.metadata_type)

    required_metadata_keys: set[tuple[int, str]] = set()
    for item in order_items:
        requirements = metadata_requirements(item)
        item_transfer_types = transfer_types_by_item.get(item.id, set())
        if (
            requirements.marking_required
            or FbsMarketplaceMetadataTransfer.TYPE_MARKING_CODE
            in item_transfer_types
        ):
            required_metadata_keys.add(
                (
                    item.order_id,
                    FbsMarketplaceMetadataTransfer.TYPE_MARKING_CODE,
                )
            )
        if requirements.expiry_required:
            required_metadata_keys.add(
                (
                    item.order_id,
                    FbsMarketplaceMetadataTransfer.TYPE_EXPIRATION,
                )
            )

    unresolved = {
        key
        for key in required_metadata_keys
        if not transfer_resolutions.get(key)
        or not all(transfer_resolutions[key])
    }
    if unresolved:
        order_reasons.append(
            f"Ожидают подтверждения КИЗ или срока: {len(unresolved)}."
        )
    assignment_blocked_order_ids = set(
        active_orders.exclude(
            order__handover_assignment__status=(
                FbsHandoverOrderAssignment.STATUS_CONFIRMED
            )
        ).values_list("order_id", flat=True)
    )
    if assignment_blocked_order_ids:
        order_reasons.append(
            "Маркетплейс подтверждает заказов: "
            f"{len(assignment_blocked_order_ids)}."
        )
    label_blocked_order_ids = set(
        active_orders.exclude(
            label__status__in=(
                FbsOrderLabel.STATUS_READY,
                FbsOrderLabel.STATUS_APPLIED,
            )
        ).values_list("order_id", flat=True)
    )
    if label_blocked_order_ids:
        order_reasons.append(
            "Официальную этикетку marketplace ожидают заказов: "
            f"{len(label_blocked_order_ids)}."
        )
    metadata_blocked_order_ids = {order_id for order_id, _ in unresolved}
    blocked_order_ids = frozenset(
        metadata_blocked_order_ids
        | assignment_blocked_order_ids
        | label_blocked_order_ids
    )
    reasons = tote_reasons + order_reasons
    status = (
        FbsControllerCheckTote.STATUS_READY
        if not reasons
        else (
            FbsControllerCheckTote.STATUS_WAITING_KIZ
            if blocked_order_ids
            else FbsControllerCheckTote.STATUS_OPEN
        )
    )
    return CheckToteReadiness(
        ready=not reasons,
        status=status,
        reasons=tuple(reasons),
        tote_reasons=tuple(tote_reasons),
        order_reasons=tuple(order_reasons),
        blocked_order_ids=blocked_order_ids,
        metadata_blocked_order_ids=frozenset(metadata_blocked_order_ids),
        assignment_blocked_order_ids=frozenset(assignment_blocked_order_ids),
        label_blocked_order_ids=frozenset(label_blocked_order_ids),
    )


@transaction.atomic
def refresh_check_tote_status(*, check_tote_id: int) -> FbsControllerCheckTote:
    check_tote = FbsControllerCheckTote.objects.select_for_update().get(pk=check_tote_id)
    if check_tote.status in {
        FbsControllerCheckTote.STATUS_CLOSED,
        FbsControllerCheckTote.STATUS_PROBLEM,
    }:
        return check_tote
    readiness = check_tote_readiness(check_tote)
    next_status = (
        FbsControllerCheckTote.STATUS_COMPOSITION
        if check_tote.status == FbsControllerCheckTote.STATUS_COMPOSITION
        and not active_check_tote_orders(check_tote).filter(
            status=FbsControllerToteOrder.STATUS_LABELED
        ).exists()
        else readiness.status
    )
    if check_tote.status != next_status:
        check_tote.status = next_status
        check_tote.save(update_fields=["status", "updated_at"])
    return check_tote


@transaction.atomic
def _ready_transport_box(check_tote: FbsControllerCheckTote) -> FbsHandoverBox:
    if check_tote.handover_batch_id is None:
        raise FbsHandoverError("Отгрузка для тары проверки еще не создана.")
    batch = FbsHandoverBatch.objects.select_for_update().get(
        pk=check_tote.handover_batch_id
    )
    boxes = FbsHandoverBox.objects.select_for_update(of=("self",)).filter(
        batch_id=batch.id,
        status=FbsHandoverBox.STATUS_OPEN,
    ).exclude(qr_code__startswith="PENDING:")
    uses_marketplace_boxes = False
    if check_tote.profile.marketplace == FbsIntegrationProfile.MARKETPLACE_WB:
        from .handover import wb_handover_uses_marketplace_boxes

        uses_marketplace_boxes = wb_handover_uses_marketplace_boxes(batch)
        if uses_marketplace_boxes:
            boxes = boxes.exclude(external_box_id="").exclude(label_file="")
    box = boxes.order_by("id").first()
    if (
        box is None
        and check_tote.profile.marketplace == FbsIntegrationProfile.MARKETPLACE_WB
        and not uses_marketplace_boxes
        and batch.status == FbsHandoverBatch.STATUS_OPEN
        and batch.marketplace_state in HANDOVER_STATES_ACCEPTING_ORDERS
        and str(batch.external_supply_id or "").strip()
    ):
        suffix = uuid4().hex[:12].upper()
        box = FbsHandoverBox.objects.create(
            batch=batch,
            qr_code=f"FBS-WB-GI-BOX-{batch.id}-{suffix}",
        )
    if box is None:
        raise FbsHandoverError(
            "Транспортный короб marketplace еще не готов. Обработайте другую тару."
        )
    return box


def _check_tote_identifier(check_tote: FbsControllerCheckTote) -> str:
    return (
        check_tote.tote.barcode
        if check_tote.tote_id
        else f"FBS-FLOW-{check_tote.id:06d}"
    )


def _raise_unmatched_composition_scan_route(
    *,
    check_tote: FbsControllerCheckTote,
    session: FbsControllerSession,
    label_scan: str,
) -> None:
    scan_value = str(label_scan or "").strip()
    if not scan_value:
        raise FbsHandoverError(
            "Не тот заказ. Этого заказа нет в текущей таре проверки."
        )
    active_matches = (
        FbsControllerToteOrder.objects.select_related(
            "check_tote__tote",
            "check_tote__session__workstation",
        )
        .filter(
            label__barcode=scan_value,
            status__in=(
                FbsControllerToteOrder.STATUS_LABELED,
                FbsControllerToteOrder.STATUS_COMPOSITION,
                FbsControllerToteOrder.STATUS_PACKED,
            ),
            check_tote__status__in=ACTIVE_CHECK_TOTE_STATUSES,
            check_tote__session__status=FbsControllerSession.STATUS_ACTIVE,
        )
        .exclude(
            order__handover_assignment__status=(
                FbsHandoverOrderAssignment.STATUS_CANCELED
            )
        )
        .exclude(
            order__pick_restock_requests__status__in=(
                SEPARATED_PICK_RESTOCK_STATUSES
            )
        )
        .exclude(check_tote_id=check_tote.id)
        .distinct()
    )
    unpacked_matches = active_matches.exclude(
        status=FbsControllerToteOrder.STATUS_PACKED
    )
    same_table_order = (
        unpacked_matches.filter(check_tote__session_id=session.id)
        .order_by("check_tote_id", "id")
        .first()
    )
    if same_table_order is not None:
        raise FbsHandoverError(
            "Положите товар в тару проверки "
            f"{_check_tote_identifier(same_table_order.check_tote)}"
        )
    other_table_order = (
        unpacked_matches.exclude(check_tote__session_id=session.id)
        .order_by("check_tote__session__workstation_id", "check_tote_id", "id")
        .first()
    )
    if other_table_order is not None:
        raise FbsHandoverError(
            "Данный товар относится к отгрузке стола номер "
            f"{other_table_order.check_tote.session.workstation.name}. "
            "Передайте данный товар сотруднику, работающему за тем столом"
        )
    if active_matches.filter(status=FbsControllerToteOrder.STATUS_PACKED).exists():
        raise FbsHandoverError(COMPOSITION_ALREADY_PACKED_MESSAGE)
    if FbsStockBalance.objects.filter(barcode__iexact=scan_value).exclude(
        barcode=""
    ).exists() or FbsOrderItem.objects.filter(barcode__iexact=scan_value).exclude(
        barcode=""
    ).exists():
        raise FbsHandoverError(COMPOSITION_PRODUCT_BARCODE_MESSAGE)
    if session.problem_tote_id is None:
        raise FbsHandoverError(
            "Проблемная тара не привязана к текущей смене контролера."
        )
    raise CompositionProblemToteRoutingRequired(
        label_scan=scan_value,
        problem_tote_barcode=session.problem_tote.barcode,
    )


def _confirm_ozon_handover_order_label(
    *,
    check_tote: FbsControllerCheckTote,
    tote_order: FbsControllerToteOrder,
    actor,
    link: FbsHandoverOrder | None = None,
) -> FbsHandoverOrder:
    """Persist the controller's exact Ozon QR scan on the handover link."""
    if check_tote.profile.marketplace != FbsIntegrationProfile.MARKETPLACE_OZON:
        raise FbsHandoverError("Подтверждение Ozon вызвано для другого marketplace.")
    if check_tote.handover_batch_id is None:
        raise FbsHandoverError("Для тары проверки еще не создана отгрузка Ozon.")

    links = FbsHandoverOrder.objects.select_for_update(of=("self",)).select_related(
        "box"
    )
    if link is None:
        link = links.filter(
            box__batch_id=check_tote.handover_batch_id,
            order_id=tote_order.order_id,
            status=FbsHandoverOrder.STATUS_ACTIVE,
        ).first()
    else:
        link = links.get(pk=link.pk)
    if link is None:
        raise FbsHandoverError(
            "Заказ по Ozon-этикетке не добавлен в короб текущей отгрузки."
        )
    if (
        link.box.batch_id != check_tote.handover_batch_id
        or link.order_id != tote_order.order_id
        or link.status != FbsHandoverOrder.STATUS_ACTIVE
    ):
        raise FbsHandoverError(
            "Ozon-этикетка не соответствует заказу и коробу текущей отгрузки."
        )
    if (
        link.verified_at is not None
        and link.verified_label_id not in (None, tote_order.label_id)
    ):
        raise FbsHandoverError("Заказ уже проверен по другой Ozon-этикетке.")
    if (
        link.verified_label_id == tote_order.label_id
        and link.verified_by_id is not None
        and link.verified_at is not None
    ):
        return link

    link.verified_label = tote_order.label
    link.verified_by = actor
    link.verified_at = timezone.now()
    link.save(update_fields=["verified_label", "verified_by", "verified_at"])
    return link


def _pack_check_tote_order(
    *,
    check_tote: FbsControllerCheckTote,
    tote_order: FbsControllerToteOrder,
    label_scan: str,
    actor,
    primary_scan_reused: bool = False,
) -> FbsControllerToteOrder:
    """Persist one physical order-label scan as the box composition fact."""
    scan_value = str(label_scan or "").strip()
    if not scan_value or scan_value != str(tote_order.label.barcode or "").strip():
        raise FbsHandoverError(
            "Не тот заказ. Этого заказа нет в текущей таре проверки."
        )

    from .handover import add_order_to_handover_box, verify_handover_order_label

    if tote_order.status == FbsControllerToteOrder.STATUS_PACKED:
        if check_tote.profile.marketplace == FbsIntegrationProfile.MARKETPLACE_WB:
            link = verify_handover_order_label(
                batch_id=check_tote.handover_batch_id,
                order_label_scan=scan_value,
                verified_by=actor,
            )
            if tote_order.transport_box_id != link.box_id:
                tote_order.transport_box = link.box
                tote_order.save(update_fields=["transport_box", "updated_at"])
        elif check_tote.profile.marketplace == FbsIntegrationProfile.MARKETPLACE_OZON:
            _confirm_ozon_handover_order_label(
                check_tote=check_tote,
                tote_order=tote_order,
                actor=actor,
            )
        if primary_scan_reused and not tote_order.primary_order_label_scan_reused:
            tote_order.primary_order_label_scan_reused = True
            tote_order.save(
                update_fields=[
                    "primary_order_label_scan_reused",
                    "updated_at",
                ]
            )
        tote_order.composition_scan_message = COMPOSITION_ALREADY_PACKED_MESSAGE
        return tote_order

    if tote_order.status not in {
        FbsControllerToteOrder.STATUS_LABELED,
        FbsControllerToteOrder.STATUS_COMPOSITION,
    }:
        raise FbsHandoverError("Заказ недоступен для добавления в транспортный короб.")

    from .labels import is_preconfirmed_ozon_order_label

    preconfirmed_ozon_label = bool(
        check_tote.profile.marketplace == FbsIntegrationProfile.MARKETPLACE_OZON
        and is_preconfirmed_ozon_order_label(tote_order.label)
    )
    if tote_order.label.status != FbsOrderLabel.STATUS_APPLIED:
        if preconfirmed_ozon_label:
            # Persist the physical composition scan without waiting for an
            # external request. The official label and Ozon state remain hard
            # gates for closing the check tote and shipment.
            from .marketplace import schedule_label_preparation

            schedule_label_preparation(order_id=tote_order.order_id)
        elif tote_order.label.status != FbsOrderLabel.STATUS_READY:
            raise FbsHandoverError(COMPOSITION_ITEM_LABEL_PENDING_MESSAGE)
        else:
            from .labels import confirm_order_label_scan

            confirm_order_label_scan(
                label_id=tote_order.label_id,
                label_scan=scan_value,
                performed_by=actor,
            )
            tote_order.label.refresh_from_db()
            tote_order.order.refresh_from_db()

    box = _ready_transport_box(check_tote)
    link = add_order_to_handover_box(
        box_id=box.id,
        order_label_scan=scan_value,
        added_by=actor,
    )
    if check_tote.profile.marketplace == FbsIntegrationProfile.MARKETPLACE_WB:
        link = verify_handover_order_label(
            batch_id=check_tote.handover_batch_id,
            order_label_scan=scan_value,
            verified_by=actor,
        )
    elif check_tote.profile.marketplace == FbsIntegrationProfile.MARKETPLACE_OZON:
        link = _confirm_ozon_handover_order_label(
            check_tote=check_tote,
            tote_order=tote_order,
            actor=actor,
            link=link,
        )

    now = timezone.now()
    tote_order.status = FbsControllerToteOrder.STATUS_PACKED
    tote_order.transport_box = link.box
    tote_order.composition_checked_by = actor
    tote_order.composition_checked_at = now
    if primary_scan_reused:
        tote_order.primary_order_label_scan_reused = True
    tote_order.save(
        update_fields=[
            "status",
            "transport_box",
            "composition_checked_by",
            "composition_checked_at",
            "primary_order_label_scan_reused",
            "updated_at",
        ]
    )
    FbsControllerCheckTote.objects.filter(pk=check_tote.pk).update(
        status=FbsControllerCheckTote.STATUS_COMPOSITION,
        composition_qty=F("composition_qty") + int(tote_order.units or 0),
        updated_at=now,
    )
    check_tote.refresh_from_db(fields=["status", "composition_qty"])
    return tote_order


@transaction.atomic
def confirm_check_tote_composition_item(
    *, check_tote_id: int, label_scan: str, performed_by
) -> FbsControllerToteOrder:
    _require_writes()
    actor = _actor(performed_by)
    _assert_check_tote_operator(actor)
    session = _check_tote_session_for_update(
        check_tote_id=check_tote_id,
        actor=actor,
    )
    check_tote = (
        FbsControllerCheckTote.objects.select_for_update(of=("self",))
        .select_related("profile", "handover_batch", "tote")
        .get(pk=check_tote_id)
    )
    # The session can hold up to MAX_ACTIVE_CHECK_TOTES independent shipping
    # flows. Each flow resolves and locks its own marketplace transport box, so
    # an unfinished composition in one flow must not stop the other two.
    readiness = check_tote_readiness(check_tote)
    if not readiness.composition_ready:
        detail = (
            readiness.tote_reasons[0]
            if readiness.tote_reasons
            else "Поток отгрузки не готов."
        )
        raise FbsHandoverError(
            "Сначала завершите обработку и подтвердите пустоту всех тар подбора. "
            f"{detail}"
        )
    scan_value = str(label_scan or "").strip()
    active_order_ids = active_check_tote_orders(check_tote).values("pk")
    tote_order = (
        FbsControllerToteOrder.objects.select_for_update(of=("self",))
        .select_related("label", "order")
        .filter(pk__in=active_order_ids, label__barcode=scan_value)
        .first()
    )
    if tote_order is None:
        _raise_unmatched_composition_scan_route(
            check_tote=check_tote,
            session=session,
            label_scan=scan_value,
        )
    if tote_order.status != FbsControllerToteOrder.STATUS_PACKED:
        if tote_order.order_id in readiness.metadata_blocked_order_ids:
            raise FbsHandoverError(COMPOSITION_ITEM_METADATA_PENDING_MESSAGE)
        if tote_order.order_id in readiness.assignment_blocked_order_ids:
            raise FbsHandoverError(
                "Маркетплейс ещё не подтвердил заказ в этой отгрузке."
            )
        if tote_order.order_id in readiness.label_blocked_order_ids:
            from .labels import is_preconfirmed_ozon_order_label

            preconfirmed_ozon_label = bool(
                check_tote.profile.marketplace
                == FbsIntegrationProfile.MARKETPLACE_OZON
                and is_preconfirmed_ozon_order_label(tote_order.label)
            )
            if not preconfirmed_ozon_label:
                raise FbsHandoverError(COMPOSITION_ITEM_LABEL_PENDING_MESSAGE)
    return _pack_check_tote_order(
        check_tote=check_tote,
        tote_order=tote_order,
        label_scan=scan_value,
        actor=actor,
    )


@transaction.atomic
def confirm_composition_item_in_problem_tote(
    *,
    check_tote_id: int,
    label_scan: str,
    problem_tote_scan: str,
    performed_by,
) -> FbsToteMovement:
    _require_writes()
    actor = _actor(performed_by)
    _assert_check_tote_operator(actor)
    session = _check_tote_session_for_update(
        check_tote_id=check_tote_id,
        actor=actor,
    )
    check_tote = (
        FbsControllerCheckTote.objects.select_for_update(of=("self",))
        .select_related("tote")
        .get(pk=check_tote_id)
    )
    if check_tote.status not in ACTIVE_CHECK_TOTE_STATUSES:
        raise FbsHandoverError("Тара проверки уже закрыта.")
    scan_value = str(label_scan or "").strip()
    try:
        _raise_unmatched_composition_scan_route(
            check_tote=check_tote,
            session=session,
            label_scan=scan_value,
        )
    except CompositionProblemToteRoutingRequired:
        pass
    problem_tote = _resolve_tote_for_update(problem_tote_scan)
    if problem_tote.id != session.problem_tote_id:
        raise FbsHandoverError(
            f"Отсканируйте проблемную тару {session.problem_tote.barcode}."
        )
    existing = (
        FbsToteMovement.objects.select_for_update()
        .filter(
            tote=problem_tote,
            action=FbsToteMovement.ACTION_PLACE,
            controller_session=session,
            details__source_check_tote_id=check_tote.id,
            details__label_scan=scan_value,
            details__manual_decision=True,
        )
        .order_by("id")
        .first()
    )
    if existing is not None:
        return existing
    source_tote_order = (
        FbsControllerToteOrder.objects.select_related("order")
        .filter(label__barcode=scan_value)
        .exclude(status=FbsControllerToteOrder.STATUS_PACKED)
        .order_by("-updated_at", "-id")
        .first()
    )
    details = {
        "reason_code": "marketplace_label_not_found",
        "reason": "ШК маркетплейса не найден в активных тарах проверки.",
        "manual_decision": True,
        "label_scan": scan_value,
        "source_check_tote_id": check_tote.id,
    }
    if source_tote_order is not None:
        details.update(
            {
                "source_tote_order_id": source_tote_order.id,
                "order_id": source_tote_order.order_id,
            }
        )
    source_order_item = None
    if source_tote_order is not None:
        source_order_items = list(source_tote_order.order.items.order_by("id")[:2])
        if len(source_order_items) == 1:
            source_order_item = source_order_items[0]
    problem_item = _create_problem_tote_item(
        session=session,
        problem_tote=problem_tote,
        source_check_tote=check_tote,
        order=source_tote_order.order if source_tote_order is not None else None,
        order_item=source_order_item,
        scanned_value=scan_value,
        reason=details["reason"],
        severity=FbsProblemToteItem.SEVERITY_NONCRITICAL,
        quantity=(
            max(int(source_tote_order.units or 0), 1)
            if source_tote_order is not None
            else 1
        ),
        reported_by=actor,
    )
    details.update(
        {
            "problem_item_id": problem_item.id,
            "severity": problem_item.severity,
        }
    )
    return FbsToteMovement.objects.create(
        tote=problem_tote,
        action=FbsToteMovement.ACTION_PLACE,
        source_kind="check_tote",
        source_code=_check_tote_identifier(check_tote),
        target_kind="problem_tote",
        target_code=problem_tote.barcode,
        controller_session=session,
        quantity=problem_item.quantity,
        details=details,
        performed_by=actor,
    )


def _close_controller_check_tote_locked(
    *, check_tote: FbsControllerCheckTote, actor
) -> FbsControllerCheckTote:
    """Close a locked, fully checked tote without changing its order set."""
    if check_tote.status == FbsControllerCheckTote.STATUS_CLOSED:
        return check_tote
    readiness = check_tote_readiness(check_tote)
    if not readiness.ready:
        raise FbsHandoverError(readiness.reasons[0])
    unpacked = active_check_tote_orders(check_tote).exclude(
        status=FbsControllerToteOrder.STATUS_PACKED
    ).count()
    if unpacked:
        raise FbsHandoverError(f"Не отсканировано при проверке состава: {unpacked}.")
    if check_tote.profile.marketplace == FbsIntegrationProfile.MARKETPLACE_WB:
        unverified_wb = active_check_tote_orders(check_tote).exclude(
            order__handover_order__status=FbsHandoverOrder.STATUS_ACTIVE,
            order__handover_order__box__batch_id=check_tote.handover_batch_id,
            order__handover_order__verified_at__isnull=False,
        ).count()
        if unverified_wb:
            raise FbsHandoverError(f"Не отсканировано WB-этикеток: {unverified_wb}.")
    boxes = list(
        FbsHandoverBox.objects.filter(
            batch=check_tote.handover_batch,
            orders__status=FbsHandoverOrder.STATUS_ACTIVE,
        ).distinct()
    )
    if not boxes:
        raise FbsHandoverError("В транспортировочном коробе нет заказов.")
    from .handover import close_handover_box, request_wb_handover_delivery

    for box in boxes:
        close_handover_box(box_id=box.id)
    batch = FbsHandoverBatch.objects.select_for_update().get(
        pk=check_tote.handover_batch_id
    )
    if batch.status == FbsHandoverBatch.STATUS_OPEN:
        batch.status = FbsHandoverBatch.STATUS_READY
        batch.save(update_fields=["status", "updated_at"])
    now = timezone.now()
    check_tote.status = FbsControllerCheckTote.STATUS_CLOSED
    check_tote.closed_by = actor
    check_tote.closed_at = now
    check_tote.save(update_fields=["status", "closed_by", "closed_at", "updated_at"])
    if check_tote.tote_id:
        _move_tote(
            tote=check_tote.tote,
            state=FbsToteBinding.STATE_FREE,
            zone=check_tote.session.free_zone,
            performed_by=actor,
            action=FbsToteMovement.ACTION_CLOSE,
            controller_session=check_tote.session,
            handover_batch=batch,
            quantity=check_tote.composition_qty,
            details={"check_tote_id": check_tote.id, "shipment_ready": True},
        )
    if (
        check_tote.profile.marketplace == FbsIntegrationProfile.MARKETPLACE_WB
        and batch.marketplace_state != FbsHandoverBatch.MARKETPLACE_COMPLETE
    ):
        request_wb_handover_delivery(
            batch_id=batch.id,
            requested_by=actor,
        )
    return check_tote


@transaction.atomic
def close_controller_check_tote(
    *, check_tote_id: int, performed_by
) -> FbsControllerCheckTote:
    _require_writes()
    actor = _actor(performed_by)
    _assert_check_tote_operator(actor)
    _check_tote_session_for_update(
        check_tote_id=check_tote_id,
        actor=actor,
    )
    check_tote = (
        FbsControllerCheckTote.objects.select_for_update(of=("self",))
        .select_related("session__free_zone", "profile", "handover_batch", "tote")
        .get(pk=check_tote_id)
    )
    return _close_controller_check_tote_locked(check_tote=check_tote, actor=actor)


@transaction.atomic
def auto_finalize_trusted_wb_check_tote(
    *, check_tote_id: int
) -> FbsHandoverBatch | None:
    """Finish a trusted WB flow only after every existing safety gate passes."""
    _require_writes()
    check_tote = (
        FbsControllerCheckTote.objects.select_for_update(of=("self",))
        .select_related(
            "session__controller",
            "session__free_zone",
            "profile",
            "handover_batch",
            "tote",
        )
        .filter(pk=check_tote_id)
        .first()
    )
    if (
        check_tote is None
        or check_tote.profile_id is None
        or check_tote.handover_batch_id is None
        or check_tote.profile.marketplace
        != FbsIntegrationProfile.MARKETPLACE_WB
        or check_tote.status not in ACTIVE_CHECK_TOTE_STATUSES
        or check_tote.handover_batch.status
        not in {FbsHandoverBatch.STATUS_OPEN, FbsHandoverBatch.STATUS_READY}
    ):
        return None

    tote_orders = list(
        active_check_tote_orders(check_tote)
        .select_for_update(of=("self",))
        .select_related("label", "order", "label_confirmed_by")
        .order_by("label_confirmed_at", "id")
    )
    allowed_row_statuses = {
        FbsControllerToteOrder.STATUS_LABELED,
        FbsControllerToteOrder.STATUS_COMPOSITION,
        FbsControllerToteOrder.STATUS_PACKED,
    }
    if not tote_orders or any(
        row.status not in allowed_row_statuses
        or not row.primary_order_label_scan_reused
        or row.label_confirmed_by_id != check_tote.session.controller_id
        or row.label.status != FbsOrderLabel.STATUS_APPLIED
        or row.order.internal_status != FbsOrder.STATUS_READY_FOR_HANDOVER
        for row in tote_orders
    ):
        return None

    readiness = check_tote_readiness(check_tote)
    if (
        readiness.metadata_blocked_order_ids
        or readiness.assignment_blocked_order_ids
        or readiness.label_blocked_order_ids
    ):
        # KIZ/expiry, marketplace assignment and the official label are never
        # bypassed by the controller setting. A failed order stays blocked.
        refresh_check_tote_status(check_tote_id=check_tote.id)
        return None

    actor = check_tote.session.controller
    for tote_order in tote_orders:
        if tote_order.status == FbsControllerToteOrder.STATUS_PACKED:
            continue
        _pack_check_tote_order(
            check_tote=check_tote,
            tote_order=tote_order,
            label_scan=tote_order.label.barcode,
            actor=actor,
            primary_scan_reused=True,
        )

    check_tote.refresh_from_db(
        fields=["status", "composition_qty", "handover_batch"]
    )
    readiness = check_tote_readiness(check_tote)
    if not readiness.ready:
        return None

    tote_orders = list(
        active_check_tote_orders(check_tote)
        .select_related("label")
        .order_by("id")
    )
    tote_order_ids = {row.order_id for row in tote_orders}
    assignment_order_ids = set(
        check_tote.handover_batch.order_assignments.exclude(
            status=FbsHandoverOrderAssignment.STATUS_CANCELED
        )
        .filter(status=FbsHandoverOrderAssignment.STATUS_CONFIRMED)
        .values_list("order_id", flat=True)
    )
    links = list(
        FbsHandoverOrder.objects.select_for_update(of=("self",)).filter(
            box__batch_id=check_tote.handover_batch_id,
            status=FbsHandoverOrder.STATUS_ACTIVE,
        )
    )
    if (
        not assignment_order_ids
        or assignment_order_ids != tote_order_ids
        or {link.order_id for link in links} != tote_order_ids
    ):
        return None
    tote_order_by_order_id = {row.order_id: row for row in tote_orders}
    if any(
        row.status != FbsControllerToteOrder.STATUS_PACKED
        or row.transport_box_id is None
        or row.composition_checked_by_id != row.label_confirmed_by_id
        or row.composition_checked_at is None
        or not row.primary_order_label_scan_reused
        for row in tote_orders
    ) or any(
        link.verified_label_id
        != tote_order_by_order_id[link.order_id].label_id
        or link.verified_by_id
        != tote_order_by_order_id[link.order_id].label_confirmed_by_id
        or link.verified_at is None
        for link in links
    ):
        return None

    _close_controller_check_tote_locked(check_tote=check_tote, actor=actor)
    return FbsHandoverBatch.objects.get(pk=check_tote.handover_batch_id)


def auto_finalize_trusted_wb_handovers(
    *, profile_ids: list[int] | tuple[int, ...] | None = None, limit: int = 100
) -> tuple[int, ...]:
    """Best-effort background WB sweep; blocked orders remain untouched."""
    candidates = FbsControllerCheckTote.objects.filter(
        profile__marketplace=FbsIntegrationProfile.MARKETPLACE_WB,
        profile__is_active=True,
        handover_batch__status__in=(
            FbsHandoverBatch.STATUS_OPEN,
            FbsHandoverBatch.STATUS_READY,
        ),
        status__in=ACTIVE_CHECK_TOTE_STATUSES,
        orders__primary_order_label_scan_reused=True,
    ).distinct().order_by("-updated_at", "-id")
    if profile_ids is not None:
        candidates = candidates.filter(profile_id__in=profile_ids)
    candidate_ids = list(
        candidates.values_list("id", flat=True)[: max(int(limit), 1)]
    )
    ready_batch_ids: list[int] = []
    for check_tote_id in candidate_ids:
        try:
            batch = auto_finalize_trusted_wb_check_tote(
                check_tote_id=check_tote_id
            )
        except FbsError as exc:
            logger.warning(
                "Unable to auto-finalize trusted WB check tote %s: %s",
                check_tote_id,
                exc,
            )
            continue
        except Exception:
            logger.exception(
                "Unexpected failure auto-finalizing trusted WB check tote %s",
                check_tote_id,
            )
            continue
        if batch is not None:
            ready_batch_ids.append(batch.id)
    return tuple(ready_batch_ids)


@transaction.atomic
def auto_finalize_verified_ozon_check_tote(
    *, check_tote_id: int
) -> FbsHandoverBatch | None:
    """Close and dispatch an Ozon flow using its already recorded final scan."""
    _require_writes()
    check_tote = (
        FbsControllerCheckTote.objects.select_for_update(of=("self",))
        .select_related(
            "session__free_zone",
            "profile",
            "handover_batch",
            "tote",
            "closed_by",
        )
        .filter(pk=check_tote_id)
        .first()
    )
    if (
        check_tote is None
        or check_tote.profile_id is None
        or check_tote.handover_batch_id is None
        or check_tote.profile.marketplace
        != FbsIntegrationProfile.MARKETPLACE_OZON
        or check_tote.handover_batch.status
        not in {FbsHandoverBatch.STATUS_OPEN, FbsHandoverBatch.STATUS_READY}
    ):
        return None

    already_closed = (
        check_tote.status == FbsControllerCheckTote.STATUS_CLOSED
    )
    if not already_closed and check_tote.status not in ACTIVE_CHECK_TOTE_STATUSES:
        return None
    readiness = check_tote_readiness(check_tote)
    if not readiness.ready:
        return None
    tote_orders = list(
        active_check_tote_orders(check_tote)
        .select_related("label", "composition_checked_by")
        .order_by("composition_checked_at", "id")
    )
    if not tote_orders or any(
        row.status != FbsControllerToteOrder.STATUS_PACKED
        or row.transport_box_id is None
        or row.composition_checked_by_id is None
        or row.composition_checked_at is None
        or row.label.status != FbsOrderLabel.STATUS_APPLIED
        for row in tote_orders
    ):
        return None

    assignment_order_ids = set(
        check_tote.handover_batch.order_assignments.exclude(
            status=FbsHandoverOrderAssignment.STATUS_CANCELED
        ).filter(
            status=FbsHandoverOrderAssignment.STATUS_CONFIRMED
        ).values_list("order_id", flat=True)
    )
    tote_order_ids = {row.order_id for row in tote_orders}
    links = list(
        FbsHandoverOrder.objects.select_for_update(of=("self",))
        .filter(
            box__batch_id=check_tote.handover_batch_id,
            status=FbsHandoverOrder.STATUS_ACTIVE,
        )
    )
    if (
        not assignment_order_ids
        or assignment_order_ids != tote_order_ids
        or {link.order_id for link in links} != assignment_order_ids
    ):
        return None
    tote_order_by_order_id = {row.order_id: row for row in tote_orders}
    if any(
        link.verified_label_id
        != tote_order_by_order_id[link.order_id].label_id
        or link.verified_by_id is None
        or link.verified_at is None
        for link in links
    ):
        return None

    actor = check_tote.closed_by if already_closed else (
        tote_orders[-1].composition_checked_by
    )
    if actor is None:
        return None
    if not already_closed:
        _close_controller_check_tote_locked(check_tote=check_tote, actor=actor)

    from .handover import dispatch_handover_batch

    return dispatch_handover_batch(
        batch_id=check_tote.handover_batch_id,
        dispatched_by=actor,
        verified_ozon_auto=True,
    )


def auto_finalize_verified_ozon_handovers(
    *, profile_ids: list[int] | tuple[int, ...] | None = None, limit: int = 100
) -> tuple[int, ...]:
    """Best-effort background sweep; one blocked shipment cannot stop sync."""
    candidates = FbsControllerCheckTote.objects.filter(
        profile__marketplace=FbsIntegrationProfile.MARKETPLACE_OZON,
        profile__is_active=True,
        handover_batch__status__in=(
            FbsHandoverBatch.STATUS_OPEN,
            FbsHandoverBatch.STATUS_READY,
        ),
        status__in=ACTIVE_CHECK_TOTE_STATUSES
        + (FbsControllerCheckTote.STATUS_CLOSED,),
    ).order_by("-updated_at", "-id")
    if profile_ids is not None:
        candidates = candidates.filter(profile_id__in=profile_ids)
    candidate_ids = list(
        candidates.values_list("id", flat=True)[: max(int(limit), 1)]
    )
    dispatched_batch_ids: list[int] = []
    for check_tote_id in candidate_ids:
        try:
            batch = auto_finalize_verified_ozon_check_tote(
                check_tote_id=check_tote_id
            )
        except FbsError as exc:
            logger.warning(
                "Unable to auto-finalize verified Ozon check tote %s: %s",
                check_tote_id,
                exc,
            )
            continue
        except Exception:
            logger.exception(
                "Unexpected failure auto-finalizing Ozon check tote %s",
                check_tote_id,
            )
            continue
        if batch is not None:
            dispatched_batch_ids.append(batch.id)
    return tuple(dispatched_batch_ids)


@transaction.atomic
def release_confirmed_reroute_from_source_check_tote(
    *, source_batch_id: int, order_id: int, performed_by
) -> FbsControllerCheckTote | None:
    """Detach one WB-confirmed reroute and finish only its source shipment.

    The problem order keeps its target handover assignment and its new pick wave.
    Only the stale controller-tote row that belonged to the source shipment is
    removed.  If every remaining source order was already packed and verified,
    the source check tote is closed so those good orders can be sent to WB.
    """
    _require_writes()
    actor = _actor(performed_by)
    moved_assignment = (
        FbsHandoverOrderAssignment.objects.select_for_update(of=("self",))
        .filter(
            order_id=order_id,
            status=FbsHandoverOrderAssignment.STATUS_CONFIRMED,
        )
        .exclude(batch_id=source_batch_id)
        .first()
    )
    if moved_assignment is None:
        raise FbsHandoverError(
            "WB еще не подтвердил перенос проблемного заказа в новую поставку."
        )
    check_tote = (
        FbsControllerCheckTote.objects.select_for_update(of=("self",))
        .select_related("session__free_zone", "profile", "handover_batch", "tote")
        .filter(handover_batch_id=source_batch_id)
        .first()
    )
    if check_tote is None:
        return None
    FbsControllerToteOrder.objects.select_for_update(of=("self",)).filter(
        check_tote=check_tote,
        order_id=order_id,
    ).exclude(status=FbsControllerToteOrder.STATUS_REMOVED).update(
        status=FbsControllerToteOrder.STATUS_REMOVED,
        updated_at=timezone.now(),
    )
    if check_tote.status == FbsControllerCheckTote.STATUS_CLOSED:
        return check_tote
    if check_tote.status not in ACTIVE_CHECK_TOTE_STATUSES:
        return check_tote
    if check_tote.pick_totes.filter(status__in=ACTIVE_PICK_TOTE_STATUSES).exists():
        refresh_check_tote_status(check_tote_id=check_tote.id)
        check_tote.refresh_from_db()
        return check_tote
    readiness = check_tote_readiness(check_tote)
    if not readiness.ready:
        refresh_check_tote_status(check_tote_id=check_tote.id)
        check_tote.refresh_from_db()
        return check_tote
    if active_check_tote_orders(check_tote).exclude(
        status=FbsControllerToteOrder.STATUS_PACKED
    ).exists():
        refresh_check_tote_status(check_tote_id=check_tote.id)
        check_tote.refresh_from_db()
        return check_tote
    if check_tote.profile.marketplace == FbsIntegrationProfile.MARKETPLACE_WB and (
        active_check_tote_orders(check_tote).exclude(
            order__handover_order__status=FbsHandoverOrder.STATUS_ACTIVE,
            order__handover_order__box__batch_id=source_batch_id,
            order__handover_order__verified_at__isnull=False,
        ).exists()
    ):
        refresh_check_tote_status(check_tote_id=check_tote.id)
        check_tote.refresh_from_db()
        return check_tote
    return _close_controller_check_tote_locked(check_tote=check_tote, actor=actor)


@transaction.atomic
def close_controller_session(*, session_id: int, performed_by) -> FbsControllerSession:
    _require_writes()
    actor = _actor(performed_by)
    session = _session_for_update(session_id=session_id, actor=actor)
    if session.pick_totes.filter(status__in=ACTIVE_PICK_TOTE_STATUSES).exists():
        raise FbsPickingError("Сначала завершите все тары подбора.")
    if session.check_totes.filter(status__in=ACTIVE_CHECK_TOTE_STATUSES).exists():
        raise FbsPickingError("Сначала завершите все потоки отгрузки.")
    unresolved_problem_qty = int(
        session.problem_items.filter(
            status=FbsProblemToteItem.STATUS_IN_TOTE,
        ).aggregate(total=Sum("quantity"))["total"]
        or 0
    )
    if unresolved_problem_qty:
        raise FbsPickingError(
            "Сначала завершите обработку товаров в проблемной таре: "
            f"{unresolved_problem_qty}."
        )
    service_tote_ids = [
        tote_id
        for tote_id in (session.problem_tote_id, session.canceled_tote_id)
        if tote_id is not None
    ]
    active_restock_count = FbsPickRestockRequest.objects.filter(
        source_tote_id__in=service_tote_ids,
        status__in=(
            FbsPickRestockRequest.STATUS_WAITING_MARKETPLACE,
            FbsPickRestockRequest.STATUS_QUEUED,
            FbsPickRestockRequest.STATUS_IN_PROGRESS,
            FbsPickRestockRequest.STATUS_FAILED,
        ),
    ).count()
    if active_restock_count:
        raise FbsPickingError(
            f"Сначала завершите возвраты из служебных тар: {active_restock_count}."
        )
    _move_tote(
        tote=session.unknown_tote,
        state=FbsToteBinding.STATE_FREE,
        zone=session.free_zone,
        performed_by=actor,
        action=FbsToteMovement.ACTION_RELEASE,
        controller_session=session,
        quantity=session.unknown_items.filter(status=FbsUnknownToteItem.STATUS_WAITING).count(),
        details={"purpose": "unknown", "session_closed": True},
    )
    released_tote_ids = {session.unknown_tote_id}
    for purpose, tote in (
        (SERVICE_TOTE_PROBLEM, session.problem_tote),
        (SERVICE_TOTE_CANCELED, session.canceled_tote),
    ):
        if tote is None or tote.id in released_tote_ids:
            continue
        _move_tote(
            tote=tote,
            state=FbsToteBinding.STATE_FREE,
            zone=session.free_zone,
            performed_by=actor,
            action=FbsToteMovement.ACTION_RELEASE,
            controller_session=session,
            details={"purpose": purpose, "session_closed": True},
        )
        released_tote_ids.add(tote.id)
    session.status = FbsControllerSession.STATUS_CLOSED
    session.closed_at = timezone.now()
    session.save(update_fields=["status", "closed_at"])
    return session


def controller_session_kpis(session: FbsControllerSession) -> dict[str, int]:
    today = timezone.localdate()
    active_check_totes = session.check_totes.filter(status__in=ACTIVE_CHECK_TOTE_STATUSES)
    active_order_rows = (
        FbsControllerToteOrder.objects.filter(
            check_tote__session=session,
            check_tote__status__in=ACTIVE_CHECK_TOTE_STATUSES,
        )
        .exclude(status=FbsControllerToteOrder.STATUS_REMOVED)
        .exclude(
            order__handover_assignment__status=(
                FbsHandoverOrderAssignment.STATUS_CANCELED
            )
        )
        .exclude(
            order__pick_restock_requests__status__in=(
                SEPARATED_PICK_RESTOCK_STATUSES
            )
        )
        .distinct()
    )
    return {
        "check_totes": active_check_totes.count(),
        "items_on_check": int(
            active_order_rows.aggregate(total=Sum("units"))["total"] or 0
        ),
        "processed_today": FbsControllerToteOrder.objects.filter(
            label_confirmed_by=session.controller,
            label_confirmed_at__date=today,
        ).count(),
        "shipments_today": FbsControllerCheckTote.objects.filter(
            closed_by=session.controller,
            closed_at__date=today,
        ).count(),
    }
