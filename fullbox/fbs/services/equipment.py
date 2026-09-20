from __future__ import annotations

from collections.abc import Iterable

from django.contrib.auth import get_user_model
from django.db import transaction

from employees.models import Employee

from ..exceptions import FbsEquipmentError
from ..models import (
    FbsEquipmentCodeSequence,
    FbsPickBatch,
    FbsPickingCart,
    FbsToteBinding,
    FbsWorkstation,
)


MAX_WORKSTATIONS_PER_REQUEST = 50
MAX_CARTS_PER_REQUEST = 100
_PREFIXES = {
    FbsEquipmentCodeSequence.KIND_WORKSTATION: "FBS-WS",
    FbsEquipmentCodeSequence.KIND_CART: "FBS-CART",
}


def _require_head_manager(actor) -> None:
    user_model = get_user_model()
    if not isinstance(actor, user_model) or not actor.is_authenticated:
        raise FbsEquipmentError("Настройки FBS доступны только начальнику склада.")
    if actor.is_superuser or actor.username == "dev":
        return
    if not Employee.objects.filter(
        user=actor,
        is_active=True,
        role="head_manager",
    ).exists():
        raise FbsEquipmentError("Настройки FBS доступны только начальнику склада.")


def _require_authenticated(actor) -> None:
    user_model = get_user_model()
    if not isinstance(actor, user_model) or not actor.is_authenticated:
        raise FbsEquipmentError("Нужен авторизованный пользователь.")


def _require_tote_manager(actor) -> None:
    user_model = get_user_model()
    if not isinstance(actor, user_model) or not actor.is_authenticated:
        raise FbsEquipmentError("Настройки тары FBS доступны только кладовщику.")
    if actor.is_superuser or actor.username == "dev":
        return
    if Employee.objects.filter(
        user=actor,
        is_active=True,
        role__in=("storekeeper", "head_manager", "director", "admin"),
    ).exists():
        return
    raise FbsEquipmentError("Настройки тары FBS доступны только кладовщику.")


def _normalize_count(value: int, *, maximum: int) -> int:
    try:
        count = int(value)
    except (TypeError, ValueError) as exc:
        raise FbsEquipmentError("Количество должно быть целым числом.") from exc
    if count < 1 or count > maximum:
        raise FbsEquipmentError(f"За один раз можно создать от 1 до {maximum} объектов.")
    return count


def _normalize_name_prefix(value: str, *, fallback: str) -> str:
    prefix = " ".join(str(value or "").split()) or fallback
    if len(prefix) > 110:
        raise FbsEquipmentError("Название слишком длинное.")
    return prefix


def _allocate_codes(*, kind: str, count: int) -> list[tuple[int, str]]:
    if kind not in _PREFIXES:
        raise FbsEquipmentError("Неизвестный тип оборудования FBS.")
    sequence, _ = FbsEquipmentCodeSequence.objects.select_for_update().get_or_create(
        kind=kind,
        defaults={"last_value": 0},
    )
    start = sequence.last_value + 1
    sequence.last_value += count
    sequence.save(update_fields=["last_value", "updated_at"])
    prefix = _PREFIXES[kind]
    return [(number, f"{prefix}-{number:06d}") for number in range(start, start + count)]


@transaction.atomic
def create_fbs_workstations(
    *,
    actor,
    count: int,
    name_prefix: str = "Рабочее место",
    device_agent=None,
    printer_name: str = "",
    max_parallel_waves: int = 7,
    notes: str = "",
) -> tuple[FbsWorkstation, ...]:
    _require_head_manager(actor)
    count = _normalize_count(count, maximum=MAX_WORKSTATIONS_PER_REQUEST)
    prefix = _normalize_name_prefix(name_prefix, fallback="Рабочее место")
    codes = _allocate_codes(
        kind=FbsEquipmentCodeSequence.KIND_WORKSTATION,
        count=count,
    )
    created = []
    for number, barcode in codes:
        workstation = FbsWorkstation(
            barcode=barcode,
            name=f"{prefix} {number:02d}",
            device_agent=device_agent,
            printer_name=printer_name,
            max_parallel_waves=max_parallel_waves,
            notes=notes,
            created_by=actor,
            updated_by=actor,
        )
        workstation.full_clean()
        workstation.save()
        created.append(workstation)
    return tuple(created)


@transaction.atomic
def create_fbs_picking_carts(
    *,
    actor,
    count: int,
    name_prefix: str = "Тара",
    notes: str = "",
) -> tuple[FbsPickingCart, ...]:
    _require_tote_manager(actor)
    count = _normalize_count(count, maximum=MAX_CARTS_PER_REQUEST)
    prefix = _normalize_name_prefix(name_prefix, fallback="Тара")
    codes = _allocate_codes(
        kind=FbsEquipmentCodeSequence.KIND_CART,
        count=count,
    )
    created = []
    for number, barcode in codes:
        cart = FbsPickingCart(
            barcode=barcode,
            name=f"{prefix} {number:02d}",
            notes=notes,
            created_by=actor,
            updated_by=actor,
        )
        cart.full_clean()
        cart.save()
        created.append(cart)
    return tuple(created)


@transaction.atomic
def create_supplier_fbs_picking_carts(
    *,
    actor,
    agency,
    count: int,
    name_prefix: str = "Тара",
    notes: str = "",
) -> tuple[FbsPickingCart, ...]:
    _require_authenticated(actor)
    count = _normalize_count(count, maximum=MAX_CARTS_PER_REQUEST)
    prefix = _normalize_name_prefix(name_prefix, fallback="Тара")
    codes = _allocate_codes(
        kind=FbsEquipmentCodeSequence.KIND_CART,
        count=count,
    )
    created = []
    for number, barcode in codes:
        cart = FbsPickingCart(
            barcode=barcode,
            name=f"{prefix} {number:02d}",
            owner_agency=agency,
            notes=notes,
            created_by=actor,
            updated_by=actor,
        )
        cart.full_clean()
        cart.save()
        created.append(cart)
    return tuple(created)


@transaction.atomic
def update_fbs_workstation(
    *,
    actor,
    workstation: FbsWorkstation,
    name: str,
    device_agent=None,
    printer_name: str = "",
    max_parallel_waves: int = 7,
    is_active: bool = True,
    notes: str = "",
) -> FbsWorkstation:
    _require_head_manager(actor)
    locked = FbsWorkstation.objects.select_for_update().get(pk=workstation.pk)
    locked.name = name
    locked.device_agent = device_agent
    locked.printer_name = printer_name
    locked.max_parallel_waves = max_parallel_waves
    locked.is_active = bool(is_active)
    locked.notes = notes
    locked.updated_by = actor
    locked.full_clean()
    locked.save(
        update_fields=[
            "name",
            "device_agent",
            "printer_name",
            "max_parallel_waves",
            "is_active",
            "notes",
            "updated_by",
            "updated_at",
        ]
    )
    return locked


@transaction.atomic
def update_fbs_picking_cart(
    *,
    actor,
    cart: FbsPickingCart,
    name: str,
    is_active: bool = True,
    notes: str = "",
) -> FbsPickingCart:
    _require_tote_manager(actor)
    locked = FbsPickingCart.objects.select_for_update().get(pk=cart.pk)
    next_active = bool(is_active)
    if locked.is_active and not next_active:
        binding = (
            FbsToteBinding.objects.select_for_update()
            .filter(tote=locked)
            .first()
        )
        if binding is not None and binding.state not in {
            FbsToteBinding.STATE_UNBOUND,
            FbsToteBinding.STATE_FREE,
        }:
            raise FbsEquipmentError(
                "Тара сейчас занята. Сначала завершите сборку и верните тару в свободную зону."
            )
        if FbsPickBatch.objects.select_for_update().filter(
            cart=locked,
            status__in=(
                FbsPickBatch.STATUS_IN_PROGRESS,
                FbsPickBatch.STATUS_VERIFICATION,
            ),
            cart_released_at__isnull=True,
        ).exists():
            raise FbsEquipmentError(
                "Тара закреплена за незавершённой волной и не может быть отключена."
            )
    locked.name = name
    locked.is_active = next_active
    locked.notes = notes
    locked.updated_by = actor
    locked.full_clean()
    locked.save(
        update_fields=[
            "name",
            "is_active",
            "notes",
            "updated_by",
            "updated_at",
        ]
    )
    return locked


@transaction.atomic
def update_supplier_fbs_picking_cart(
    *,
    actor,
    agency,
    cart: FbsPickingCart,
    name: str,
    is_active: bool = True,
    notes: str = "",
) -> FbsPickingCart:
    _require_authenticated(actor)
    locked = FbsPickingCart.objects.select_for_update().get(pk=cart.pk)
    if locked.owner_agency_id != agency.id:
        raise FbsEquipmentError("Можно настраивать только тару своей компании.")
    next_active = bool(is_active)
    if locked.is_active and not next_active:
        binding = (
            FbsToteBinding.objects.select_for_update()
            .filter(tote=locked)
            .first()
        )
        if binding is not None and binding.state not in {
            FbsToteBinding.STATE_UNBOUND,
            FbsToteBinding.STATE_FREE,
        }:
            raise FbsEquipmentError(
                "Тара сейчас занята. Сначала завершите сборку и верните тару в свободную зону."
            )
        if FbsPickBatch.objects.select_for_update().filter(
            cart=locked,
            status__in=(
                FbsPickBatch.STATUS_IN_PROGRESS,
                FbsPickBatch.STATUS_VERIFICATION,
            ),
            cart_released_at__isnull=True,
        ).exists():
            raise FbsEquipmentError(
                "Тара закреплена за незавершённой волной и не может быть отключена."
            )
    locked.name = name
    locked.is_active = next_active
    locked.notes = notes
    locked.updated_by = actor
    locked.full_clean()
    locked.save(
        update_fields=[
            "name",
            "is_active",
            "notes",
            "updated_by",
            "updated_at",
        ]
    )
    return locked


def equipment_label_queryset(*, kind: str, ids: Iterable[int] | None = None):
    if kind == FbsEquipmentCodeSequence.KIND_WORKSTATION:
        queryset = FbsWorkstation.objects.all()
    elif kind == FbsEquipmentCodeSequence.KIND_CART:
        queryset = FbsPickingCart.objects.all()
    else:
        raise FbsEquipmentError("Неизвестный тип оборудования FBS.")
    if ids is not None:
        queryset = queryset.filter(pk__in=tuple(ids))
    return queryset.order_by("id")
