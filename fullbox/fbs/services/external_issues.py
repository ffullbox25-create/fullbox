"""External issues use free FBS stock only; never consume marketplace allocations."""
import hashlib
import json
import re
import uuid
from datetime import date
from types import SimpleNamespace

from django.db import transaction
from django.db.models import Exists, F, OuterRef, Q, Sum
from django.utils import timezone
from employees.access import get_request_role
from fbs.exceptions import FbsError
from fbs.flags import feature_enabled
from fbs.models import FbsBox, FbsPallet, FbsStockBalance, FbsExternalIssue, FbsExternalIssueLine, FbsExternalIssueEvent
from .inventory import assert_balance_unlocked
from .physical_locations import fbs_box_physical_location_label

ROLES = ("storekeeper", "head_manager", "director", "admin", "developer")
SUPERVISORS = ("head_manager", "director", "admin", "developer")
CLIENT_STAFF_ROLES = ("manager", "head_manager", "director", "admin", "developer")
OPEN = ("reserved", "picking")
PORTAL_SOURCES = ("client_portal", "staff_client_portal")
OUTBOUND_DELIVERY_TYPES = {
    "marketplace": "Маркетплейс",
    "pickup": "Самовывоз",
}
OUTBOUND_SUPPLY_TYPES = {
    "box": "Короб",
    "monopallet": "Монопаллет",
    "supersafe": "Супер сейф",
}
OUTBOUND_VEHICLE_TYPES = {
    "fulfillment": "Транспорт ФуллБокс",
    "client": "Транспорт клиента",
    "cdek": "СДЭК",
    "yandex": "Яндекс",
}


def _shipping_text(value, *, field_name, max_length):
    result = str(value or "").strip()
    if len(result) > max_length:
        raise FbsError(f"Поле «{field_name}» слишком длинное.")
    return result


def _shipping_date(value, *, field_name, required=False):
    result = _shipping_text(value, field_name=field_name, max_length=10)
    if not result:
        if required:
            raise FbsError(f"Укажите поле «{field_name}».")
        return ""
    try:
        parsed = date.fromisoformat(result)
    except ValueError as exc:
        raise FbsError(f"Поле «{field_name}» содержит неверную дату.") from exc
    if parsed < timezone.localdate():
        raise FbsError(f"Поле «{field_name}» не может быть в прошлом.")
    return parsed.isoformat()


def _shipping_time(value, *, field_name):
    result = _shipping_text(value, field_name=field_name, max_length=5)
    if result and not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", result):
        raise FbsError(f"Поле «{field_name}» содержит неверное время.")
    return result


def normalize_issue_shipping_details(value, *, required=False):
    """Validate FBS outbound routing without entering ShippingOrder write paths."""
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise FbsError("Параметры отгрузки имеют неверный формат.")

    delivery_type = _shipping_text(
        value.get("delivery_type") or "pickup",
        field_name="Тип отгрузки",
        max_length=32,
    )
    if delivery_type not in OUTBOUND_DELIVERY_TYPES:
        raise FbsError("Выберите тип отгрузки: маркетплейс или самовывоз.")

    marketplace_id = None
    marketplace_name = ""
    shipping_barcode = ""
    supply_number = ""
    supply_type = ""
    destination_warehouse = ""
    slot_date = ""
    slot_time = ""
    if delivery_type == "marketplace":
        raw_marketplace_id = str(value.get("marketplace_id") or "").strip()
        if not raw_marketplace_id.isdigit():
            raise FbsError("Выберите маркетплейс.")
        from sku.models import Market

        marketplace = Market.objects.filter(pk=int(raw_marketplace_id)).first()
        if marketplace is None:
            raise FbsError("Выбранный маркетплейс не найден.")
        marketplace_id = marketplace.pk
        marketplace_name = str(marketplace.name or "").strip()
        shipping_barcode = _shipping_text(
            value.get("shipping_barcode"), field_name="ШК поставки", max_length=128
        )
        supply_number = _shipping_text(
            value.get("supply_number"), field_name="Номер поставки", max_length=128
        )
        supply_type = _shipping_text(
            value.get("supply_type"), field_name="Тип поставки", max_length=32
        )
        destination_warehouse = _shipping_text(
            value.get("destination_warehouse"),
            field_name="Склад назначения",
            max_length=255,
        )
        if required and not shipping_barcode:
            raise FbsError("Укажите ШК поставки.")
        if required and not supply_number:
            raise FbsError("Укажите номер поставки.")
        if supply_type not in OUTBOUND_SUPPLY_TYPES:
            raise FbsError("Выберите тип поставки.")
        if required and not destination_warehouse:
            raise FbsError("Укажите склад назначения.")
        slot_date = _shipping_date(value.get("slot_date"), field_name="Дата слота")
        slot_time = _shipping_time(value.get("slot_time"), field_name="Время слота")

    eta_date = _shipping_date(
        value.get("eta_date"), field_name="Дата отгрузки", required=required
    )
    vehicle_type = _shipping_text(
        value.get("vehicle_type"), field_name="Транспорт", max_length=32
    )
    if required and vehicle_type not in OUTBOUND_VEHICLE_TYPES:
        raise FbsError("Выберите транспорт.")
    if vehicle_type and vehicle_type not in OUTBOUND_VEHICLE_TYPES:
        raise FbsError("Выбран неверный тип транспорта.")
    vehicle_number = _shipping_text(
        value.get("vehicle_number"), field_name="Номер автомобиля", max_length=32
    )
    driver_phone = _shipping_text(
        value.get("driver_phone"), field_name="Телефон водителя", max_length=32
    )
    return {
        "delivery_type": delivery_type,
        "delivery_type_label": OUTBOUND_DELIVERY_TYPES[delivery_type],
        "marketplace_id": marketplace_id,
        "marketplace_name": marketplace_name,
        "shipping_barcode": shipping_barcode,
        "supply_number": supply_number,
        "supply_type": supply_type,
        "supply_type_label": OUTBOUND_SUPPLY_TYPES.get(supply_type, ""),
        "destination_warehouse": destination_warehouse,
        "slot_date": slot_date,
        "slot_time": slot_time,
        "eta_date": eta_date,
        "vehicle_type": vehicle_type,
        "vehicle_type_label": OUTBOUND_VEHICLE_TYPES.get(vehicle_type, ""),
        "vehicle_number": vehicle_number,
        "driver_phone": driver_phone,
    }


def issue_shipping_details(issue):
    details = dict(getattr(issue, "shipping_details", None) or {})
    delivery_type = str(details.get("delivery_type") or "pickup").strip()
    if delivery_type not in OUTBOUND_DELIVERY_TYPES:
        delivery_type = "pickup"
    details["delivery_type"] = delivery_type
    details["delivery_type_label"] = OUTBOUND_DELIVERY_TYPES[delivery_type]
    details.setdefault("marketplace_name", "")
    details.setdefault("destination_warehouse", "")
    details.setdefault("eta_date", "")
    details.setdefault("vehicle_type_label", "")
    details.setdefault("vehicle_number", "")
    details.setdefault("driver_phone", "")
    return details


def require_actor(user, *, historical=False):
    if not getattr(user, "is_authenticated", False):
        raise FbsError("Требуется вход в систему.")
    role = "admin" if user.is_superuser else get_request_role(SimpleNamespace(user=user, session={}))
    if role not in (SUPERVISORS if historical else ROLES):
        raise FbsError("Недостаточно прав для выдачи товара.")
    if not feature_enabled("module") or not feature_enabled("warehouse_writes"):
        raise FbsError("Складские операции ФБС выключены.")


def require_client_actor(user, *, agency_id):
    """Allow a client portal user to reserve only their own FBS stock."""
    if not getattr(user, "is_authenticated", False):
        raise FbsError("Требуется вход в личный кабинет клиента.")
    from client_cabinet.portal_access import SECTION_REQUESTS
    from client_cabinet.portal_members import (
        portal_user_has_section,
        resolve_portal_agency_for_user,
    )

    agency = resolve_portal_agency_for_user(user, required_section=SECTION_REQUESTS)
    if (
        agency is None
        or int(agency.pk) != int(agency_id)
        or not portal_user_has_section(user, agency, SECTION_REQUESTS)
    ):
        raise FbsError("Создавать выдачу можно только для своего клиента и при доступе к заявкам.")
    if not feature_enabled("module") or not feature_enabled("warehouse_writes"):
        raise FbsError("Складские операции ФБС выключены.")


def require_staff_client_actor(user, *, agency_id):
    """Allow authorized Fullbox staff to create a request for an active client."""
    if not getattr(user, "is_authenticated", False):
        raise FbsError("Требуется вход в кабинет сотрудника Fullbox.")
    role = "admin" if user.is_superuser else get_request_role(SimpleNamespace(user=user, session={}))
    if role not in CLIENT_STAFF_ROLES:
        raise FbsError("Недостаточно прав для создания выдачи от имени клиента.")
    from accountant.selectors import manager_visible_agencies
    from sku.models import Agency

    if not manager_visible_agencies(Agency.objects.all()).filter(pk=agency_id).exists():
        raise FbsError("Клиент недоступен менеджеру или ещё не активирован.")
    if not feature_enabled("module") or not feature_enabled("warehouse_writes"):
        raise FbsError("Складские операции ФБС выключены.")


def with_manager_approval_state(queryset):
    """Annotate external issues without adding approval columns to the stock ledger."""
    portal_request = FbsExternalIssueEvent.objects.filter(
        issue_id=OuterRef("pk"),
    ).filter(
        Q(action="created", payload__source__in=PORTAL_SOURCES)
        | Q(action="manager_review_required")
    )
    manager_approval = FbsExternalIssueEvent.objects.filter(
        issue_id=OuterRef("pk"),
        action="manager_approved",
    )
    return queryset.annotate(
        requires_manager_approval=Exists(portal_request),
        manager_approved=Exists(manager_approval),
    )


def client_portal_issue_queryset(queryset):
    return with_manager_approval_state(queryset).filter(requires_manager_approval=True)


def warehouse_visible_issue_queryset(queryset):
    return with_manager_approval_state(queryset).filter(
        Q(requires_manager_approval=False) | Q(manager_approved=True)
    )


def issue_requires_manager_approval(issue):
    annotated = getattr(issue, "requires_manager_approval", None)
    if annotated is not None:
        return bool(annotated)
    return issue.events.filter(
        Q(action="created", payload__source__in=PORTAL_SOURCES)
        | Q(action="manager_review_required")
    ).exists()


def issue_is_manager_approved(issue):
    annotated = getattr(issue, "manager_approved", None)
    if annotated is not None:
        return bool(annotated)
    return issue.events.filter(action="manager_approved").exists()


def issue_display_status(issue):
    if (
        issue.status in OPEN
        and issue_requires_manager_approval(issue)
        and not issue_is_manager_approved(issue)
    ):
        return "awaiting_manager", "Ожидает подтверждения менеджером"
    return issue.status, issue.get_status_display()


def _key(value):
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        raise FbsError("Обновите страницу: отсутствует идентификатор операции.")


def _hash(data):
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def _positive(value):
    text = str(value)
    if not text.isascii() or not text.isdigit() or len(text) > 8 or int(text) <= 0:
        raise FbsError("Количество должно быть целым положительным числом.")
    return int(text)


def assert_no_external_activity(balance_ids):
    if FbsExternalIssueLine.objects.filter(balance_id__in=balance_ids, issue__status__in=OPEN).exists():
        raise FbsError("Товар участвует в открытой выдаче ФБС. Завершите выдачу или возврат.")


def _assert_stock(balance):
    from fbs.models import FbsOrderStockAllocation
    order_hold = FbsOrderStockAllocation.objects.filter(
        balance_id=balance.pk, status__in=("reserved", "picking"),
    ).aggregate(qty=Sum(F("qty_reserved") - F("qty_picked")))["qty"] or 0
    if order_hold > balance.reserved_qty:
        raise FbsError("Резервы заказов не совпадают со счётчиком остатка. Выдача запрещена до проверки.")
    held = FbsExternalIssueLine.objects.filter(balance_id=balance.pk, issue__status__in=OPEN).aggregate(
        qty=Sum(F("requested_qty") - F("picked_qty")))["qty"] or 0
    if balance.external_reserved_qty != held:
        raise FbsError("Резерв выдачи не совпадает с документами. Требуется проверка остатков.")
    if balance.qty < balance.available_qty + balance.reserved_qty + balance.external_reserved_qty:
        raise FbsError("Остаток изменился. Требуется проверка доступного количества.")


def _eligible(balance, *, execution=False):
    assert_balance_unlocked(balance.pk, for_execution=execution)
    if balance.box.status != FbsBox.STATUS_ACTIVE or balance.box.pallet.status != FbsPallet.STATUS_ACTIVE:
        raise FbsError("Короб или палета недоступны для выдачи.")
    if balance.agency_id != balance.box.agency_id or balance.agency_id != balance.box.pallet.agency_id:
        raise FbsError("Владелец товара не совпадает с владельцем контейнера.")
    _assert_stock(balance)


def _queue(agency_id):
    from .stock_sync import queue_agency_stock_exports
    # Only refresh the local export queue; no marketplace requests in a stock transaction.
    transaction.on_commit(lambda: queue_agency_stock_exports(agency_id=agency_id), robust=True)


def _save_stock(balance):
    balance.save(update_fields=["qty", "available_qty", "external_reserved_qty", "updated_at"])


@transaction.atomic
def create_issue(*, user, agency_id, reference, recipient, purpose, basis, selections, request_key,
                 historical=False, occurred_at=None, historical_confirmed=False,
                 client_request=False, staff_client_request=False, shipping_details=None):
    if client_request and staff_client_request:
        raise FbsError("Неверный режим создания выдачи.")
    if client_request or staff_client_request:
        if historical:
            raise FbsError("Через личный кабинет нельзя регистрировать выдачу задним числом.")
        if staff_client_request:
            require_staff_client_actor(user, agency_id=agency_id)
        else:
            require_client_actor(user, agency_id=agency_id)
    else:
        require_actor(user, historical=historical)
    key = _key(request_key)
    client_origin = client_request or staff_client_request
    normalized_shipping_details = normalize_issue_shipping_details(
        shipping_details,
        required=client_origin and shipping_details is not None,
    )
    if client_origin:
        from sku.models import Agency

        agency_name = (
            Agency.objects.filter(pk=agency_id).values_list("agn_name", flat=True).first()
            or f"Клиент #{agency_id}"
        )
        reference = f"LK-{agency_id}-{key.hex[:12].upper()}"
        recipient = agency_name
        purpose = (
            "external"
            if normalized_shipping_details["delivery_type"] == "marketplace"
            else "owner"
        )
    reference, recipient, basis = (str(value or "").strip() for value in (reference, recipient, basis))
    if not reference or len(reference) > 128 or not recipient or len(recipient) > 255 or not basis or len(basis) > 4000:
        raise FbsError("Укажите номер документа, получателя и основание выдачи.")
    if purpose not in {"owner", "external"}:
        raise FbsError("Выберите назначение выдачи.")
    if not isinstance(selections, list) or not 1 <= len(selections) <= 100:
        raise FbsError("Выберите от 1 до 100 строк товара.")
    selected = {}
    for row in selections:
        balance_id, qty = _positive(row.get("balance_id")), _positive(row.get("qty"))
        if balance_id in selected:
            raise FbsError("Одна строка остатка указана повторно.")
        selected[balance_id] = qty
    fingerprint = _hash([
        agency_id,
        reference,
        recipient,
        purpose,
        basis,
        normalized_shipping_details,
        selected,
        historical,
        occurred_at,
    ])
    previous = FbsExternalIssue.objects.filter(request_key=key).first()
    if previous:
        if previous.request_hash != fingerprint or previous.created_by_id != user.id:
            raise FbsError("Этот идентификатор уже использован для другой выдачи.")
        return previous
    if FbsExternalIssue.objects.filter(agency_id=agency_id, reference=reference).exists():
        raise FbsError("Документ с таким номером уже зарегистрирован у клиента. Повторная выдача запрещена.")
    if historical and (not historical_confirmed or occurred_at is None or timezone.is_naive(occurred_at)
                       or occurred_at > timezone.now() or occurred_at.year < 2020):
        raise FbsError("Укажите дату выполненной передачи и подтвердите проверку отсутствия прежнего списания.")
    box_ids = FbsStockBalance.objects.filter(pk__in=selected).values_list("box_id", flat=True)
    list(FbsBox.objects.select_for_update().filter(pk__in=box_ids).order_by("id"))
    balances = list(FbsStockBalance.objects.select_for_update(of=("self",)).select_related(
        "box__pallet__cell__location", "box__source_container__current_location").filter(pk__in=selected).order_by("id"))
    if len(balances) != len(selected):
        raise FbsError("Часть выбранных остатков не найдена.")
    for balance in balances:
        _eligible(balance)
        if balance.agency_id != int(agency_id):
            raise FbsError("Нельзя выдавать товар другого клиента.")
        qty = selected[balance.pk]
        free = min(balance.available_qty, balance.qty - balance.reserved_qty - balance.external_reserved_qty)
        if qty > free:
            raise FbsError(f"{balance.sku_code}: свободно {free} шт., запрошено {qty}. Резервы заказов не трогаем.")
        if balance.marking_code and qty != 1:
            raise FbsError("Маркированный товар выдаётся по одному конкретному коду.")
        if balance.expiry_date and balance.expiry_date < timezone.localdate():
            raise FbsError("Просроченный товар требует отдельного согласованного списания.")
    issue = FbsExternalIssue.objects.create(agency_id=agency_id, reference=reference, recipient=recipient,
        purpose=purpose, basis=basis, shipping_details=normalized_shipping_details,
        request_key=key, request_hash=fingerprint, historical=historical,
        occurred_at=occurred_at if historical else None, created_by=user)
    for balance in balances:
        qty = selected[balance.pk]
        snapshot = {name: str(getattr(balance, name) or "") for name in (
            "sku_code", "name", "size", "barcode", "goods_type", "marking_code", "lot_code", "expiry_date")}
        snapshot.update(box_code=balance.box.box_code, location=fbs_box_physical_location_label(balance.box))
        FbsExternalIssueLine.objects.create(issue=issue, balance=balance, item_snapshot=snapshot,
            requested_qty=qty, picked_qty=qty if historical else 0, shipped_qty=qty if historical else 0)
        balance.available_qty -= qty
        if historical:
            balance.qty -= qty
        else:
            balance.external_reserved_qty += qty
        _save_stock(balance)
    if historical:
        issue.status = "completed"
        issue.completed_at = timezone.now()
        issue.completed_by = user
        issue.save(update_fields=["status", "completed_at", "completed_by"])
    source = (
        "client_portal"
        if client_request
        else "staff_client_portal"
        if staff_client_request
        else "warehouse"
    )
    FbsExternalIssueEvent.objects.create(issue=issue, request_key=key, request_hash=fingerprint,
        action="historical" if historical else "created",
        payload={"selections": selected, "reference": reference, "source": source,
                 "shipping_details": normalized_shipping_details}, performed_by=user)
    _queue(issue.agency_id)
    return issue


@transaction.atomic
def review_issue_by_manager(*, user, issue_id, action, request_key):
    """Approve a client-portal withdrawal for warehouse work, or reject it safely."""
    key = _key(request_key)
    fingerprint = _hash([issue_id, action])
    issue = FbsExternalIssue.objects.select_for_update().get(pk=issue_id)
    require_staff_client_actor(user, agency_id=issue.agency_id)
    previous = FbsExternalIssueEvent.objects.filter(request_key=key).first()
    if previous:
        if previous.issue_id != issue.id or previous.request_hash != fingerprint or previous.performed_by_id != user.id:
            raise FbsError("Этот запрос уже использован для другой операции.")
        return issue
    if not issue_requires_manager_approval(issue):
        raise FbsError("Этот документ не является заявкой клиента на вывоз из FBS.")
    if issue.status not in OPEN:
        raise FbsError("Заявка уже закрыта.")
    if issue_is_manager_approved(issue):
        raise FbsError("Заявка уже подтверждена и передана складу.")
    if action not in {"approve", "reject"}:
        raise FbsError("Неизвестное решение менеджера.")
    lines = list(issue.lines.select_for_update().order_by("balance_id"))
    if any(line.picked_qty for line in lines):
        raise FbsError("По заявке уже начат отбор. Решение менеджера заблокировано до проверки.")
    balance_ids = [line.balance_id for line in lines]
    box_ids = FbsStockBalance.objects.filter(pk__in=balance_ids).values_list("box_id", flat=True)
    list(FbsBox.objects.select_for_update().filter(pk__in=box_ids).order_by("id"))
    balances = {
        balance.pk: balance
        for balance in FbsStockBalance.objects.select_for_update(of=("self",)).filter(
            pk__in=balance_ids
        ).order_by("id")
    }
    for balance in balances.values():
        _assert_stock(balance)
    if action == "reject":
        for line in lines:
            balance = balances[line.balance_id]
            release = line.unpicked_qty
            if balance.external_reserved_qty < release:
                raise FbsError("Резерв изменился. Отклонение не проведено.")
            balance.external_reserved_qty -= release
            balance.available_qty += release
            _save_stock(balance)
        issue.status = "canceled"
        issue.completed_at = timezone.now()
        issue.completed_by = user
        issue.save(update_fields=["status", "completed_at", "completed_by"])
    FbsExternalIssueEvent.objects.create(
        issue=issue,
        action="manager_approved" if action == "approve" else "manager_rejected",
        request_key=key,
        request_hash=fingerprint,
        payload={"requested_qty": sum(line.requested_qty for line in lines)},
        performed_by=user,
    )
    if action == "reject":
        _queue(issue.agency_id)
    return issue


@transaction.atomic
def issue_command(*, user, issue_id, action, request_key, line_id=None, cell_scan="", box_scan="", item_scan="", confirmed=False, expected_qty=None):
    require_actor(user)
    key = _key(request_key)
    fingerprint = _hash([issue_id, action, line_id, cell_scan, box_scan, item_scan, confirmed, expected_qty])
    issue = FbsExternalIssue.objects.select_for_update().get(pk=issue_id)
    previous = FbsExternalIssueEvent.objects.filter(request_key=key).first()
    if previous:
        if previous.issue_id != issue.id or previous.request_hash != fingerprint or previous.performed_by_id != user.id:
            raise FbsError("Этот запрос уже использован для другой операции.")
        return issue
    if issue.status not in OPEN:
        raise FbsError("Документ уже закрыт. Повторное списание запрещено.")
    if issue_requires_manager_approval(issue) and not issue_is_manager_approved(issue):
        raise FbsError("Заявка клиента ожидает подтверждения менеджером.")
    lines = list(issue.lines.select_for_update().order_by("balance_id"))
    box_ids = FbsStockBalance.objects.filter(pk__in=[line.balance_id for line in lines]).values_list("box_id", flat=True)
    list(FbsBox.objects.select_for_update().filter(pk__in=box_ids).order_by("id"))
    balances = {b.pk: b for b in FbsStockBalance.objects.select_for_update(of=("self",)).select_related(
        "box__pallet__cell__location", "box__source_container__current_location").filter(pk__in=[line.balance_id for line in lines]).order_by("id")}
    for balance in balances.values():
        _assert_stock(balance)
    event_payload = {}
    if action in {"pick", "return"}:
        line = next((line for line in lines if line.pk == int(line_id or 0)), None)
        if line is None:
            raise FbsError("Позиция не принадлежит этой выдаче.")
        balance = balances[line.balance_id]
        _eligible(balance, execution=True)
        from .picking import _assert_cell_scan
        _assert_cell_scan(SimpleNamespace(balance=balance), cell_scan)
        if str(box_scan).strip().casefold() != balance.box.box_code.casefold():
            raise FbsError("Отсканируйте исходный короб этой позиции.")
        if balance.marking_code:
            from marking.codes import marking_code_identity
            if marking_code_identity(item_scan) != marking_code_identity(balance.marking_code):
                raise FbsError("Требуется конкретный ЧЗ этой позиции, а не обычный штрихкод.")
        elif str(item_scan).strip() != balance.barcode:
            raise FbsError("Штрихкод товара не совпадает с позицией.")
        if action == "pick":
            if line.unpicked_qty <= 0 or balance.external_reserved_qty <= 0 or balance.qty <= balance.reserved_qty:
                raise FbsError("Доступное количество уже отобрано или резерв изменился.")
            balance.qty -= 1
            balance.external_reserved_qty -= 1
            line.picked_qty += 1
        else:
            if line.in_hand_qty <= 0:
                raise FbsError("Нет отобранного товара для возврата.")
            balance.qty += 1
            balance.available_qty += 1
            line.returned_qty += 1
        _save_stock(balance)
        line.save(update_fields=["picked_qty", "returned_qty"])
        issue.status = "picking"
        issue.save(update_fields=["status"])
        event_payload = {"line_id": line.pk, "qty": 1, "cell_scan": cell_scan, "box_scan": box_scan, "item_scan": item_scan}
    elif action in {"complete", "cancel"}:
        if not confirmed:
            raise FbsError("Подтвердите фактическую передачу или отмену документа.")
        in_hand = sum(line.in_hand_qty for line in lines)
        if action == "complete" and in_hand <= 0:
            raise FbsError("Сначала отберите товар. Нечего выдавать.")
        if action == "complete" and _positive(expected_qty) != in_hand:
            raise FbsError("Отобранное количество изменилось. Проверьте фактическую передачу и подтвердите заново.")
        if action == "cancel" and in_hand:
            raise FbsError("Сначала верните отобранный товар сканированием в исходный короб.")
        if action == "complete":
            for balance in balances.values():
                _eligible(balance, execution=True)
        for line in lines:
            balance = balances[line.balance_id]
            if action == "complete":
                # Stock left its source on pick, not here: no second subtraction.
                line.shipped_qty = line.in_hand_qty
                line.save(update_fields=["shipped_qty"])
            release = line.unpicked_qty
            if balance.external_reserved_qty < release:
                raise FbsError("Резерв изменился. Выдача не проведена.")
            balance.external_reserved_qty -= release
            balance.available_qty += release
            _save_stock(balance)
        issue.status = "completed" if action == "complete" else "canceled"
        issue.completed_at = timezone.now()
        issue.completed_by = user
        if action == "complete":
            issue.occurred_at = issue.completed_at
        issue.save(update_fields=["status", "completed_at", "completed_by", "occurred_at"])
        event_payload = {"shipped_qty": in_hand if action == "complete" else 0,
                         "released_unpicked_qty": sum(line.unpicked_qty for line in lines)}
    else:
        raise FbsError("Неизвестная операция выдачи.")
    FbsExternalIssueEvent.objects.create(issue=issue, action=action, request_key=key,
        request_hash=fingerprint, payload=event_payload, performed_by=user)
    _queue(issue.agency_id)
    return issue
