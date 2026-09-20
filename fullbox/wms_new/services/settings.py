from __future__ import annotations

from secrets import token_urlsafe

from django.db import transaction
from django.utils import timezone

from wms_new.models import WmsNewEvent, WmsNewRecord


class SettingsOperationError(ValueError):
    pass


SYSTEM_DEFAULTS = {
    "acceptances_per_page": "100",
    "documents_per_page": "100",
    "products_per_page": "100",
    "session_timeout": "1",
    "mixed_partner_pallets": "no",
    "tab_direction": "horizontal",
    "table_mode": "full",
}

SYSTEM_SECTION_FIELDS = {
    "requisites": (
        "inn", "ogrn", "company_name", "phone", "email", "legal_address",
        "postal_address", "director_position", "director_last_name", "director_first_name",
        "director_middle_name", "do_not_decline", "bank_bik", "bank_name",
        "settlement_account", "correspondent_account",
    ),
    "tasks": (
        "auto_pick_places", "default_shipment_task_type", "writeoff_status",
        "default_place", "box_label_size", "picking_barcodes", "picking_articles",
        "box_code_type",
    ),
    "fbs": (
        "wb_product_barcode_print", "auto_add_to_shipment", "auto_assembled_place",
        "one_click_verification", "scan_each_unit", "strict_collection",
        "insufficient_stock_wave", "picking_barcodes", "picking_articles",
        "picking_font_size", "picking_orientation", "picking_extra_info", "clone_marking_code",
    ),
    "finance": (
        "balance_enabled", "minimum_invoice_amount", "balance_service_name",
        "primary_documents_enabled", "default_vat", "auto_invoices",
    ),
    "inventory": ("count_input_mode",),
}


DIRECTORY_TYPES = {
    "units": "Единицы измерения",
    "services": "Услуги фулфилмента",
    "tariff_categories": "Категории тарифов",
    "fbo_warehouses": "Склады FBO",
    "marking_codes": "Коды маркировки",
    "extra_fields": "Дополнительные поля",
    "cancel_statuses": "Статусы отмены заказов",
    "product_categories": "Категории товаров",
    "task_file_types": "Типы файлов задач",
}


def _event(record: WmsNewRecord, action: str, *, actor=None, before=None, metadata=None):
    WmsNewEvent.objects.create(
        entity_type=f"settings_{record.entity_type}",
        entity_id=record.pk,
        action=action,
        actor=actor,
        before=before or {},
        after={
            "status": record.status,
            "title": record.title,
            "pilot_revision": record.pilot_revision,
        },
        metadata=metadata or {},
    )


def system_settings() -> dict:
    record = WmsNewRecord.objects.filter(
        module="settings", entity_type="system", source_key="general"
    ).first()
    values = dict(SYSTEM_DEFAULTS)
    if record and isinstance(record.payload, dict):
        values.update({key: str(value) for key, value in record.payload.items() if key in values})
    return values


def system_section_settings(section: str, defaults: dict | None = None) -> dict:
    values = dict(defaults or {})
    record = WmsNewRecord.objects.filter(
        module="settings", entity_type="system", source_key=section
    ).first()
    if record and isinstance(record.payload, dict):
        values.update({key: value for key, value in record.payload.items() if key in SYSTEM_SECTION_FIELDS.get(section, ())})
    return values


@transaction.atomic
def save_system_section(*, section: str, values: dict, actor=None) -> WmsNewRecord:
    fields = SYSTEM_SECTION_FIELDS.get(section)
    if not fields:
        raise SettingsOperationError("Неизвестная вкладка системных настроек.")
    payload = {key: str(values.get(key) or "").strip()[:1000] for key in fields}
    record, _ = WmsNewRecord.objects.select_for_update().get_or_create(
        module="settings",
        entity_type="system",
        source_key=section,
        defaults={"title": section, "status": "active"},
    )
    before = dict(record.payload or {})
    record.payload = payload
    record.status = "active"
    record.pilot_revision += 1
    record.save()
    _event(record, "system_section_saved", actor=actor, before=before, metadata={"section": section})
    return record


@transaction.atomic
def save_system_settings(*, values: dict, actor=None) -> WmsNewRecord:
    allowed = {
        "acceptances_per_page": {"50", "100", "300", "500", "1000"},
        "documents_per_page": {"50", "100", "300", "500", "1000"},
        "products_per_page": {"50", "100", "300", "500", "1000"},
        "session_timeout": {"1", "2", "4", "8", "12", "24", "168"},
        "mixed_partner_pallets": {"no", "yes"},
        "tab_direction": {"horizontal", "vertical"},
        "table_mode": {"full", "compact"},
    }
    payload = {}
    for key, valid in allowed.items():
        value = str(values.get(key) or SYSTEM_DEFAULTS[key])
        if value not in valid:
            raise SettingsOperationError(f"Некорректное значение настройки: {key}.")
        payload[key] = value
    record, _ = WmsNewRecord.objects.select_for_update().get_or_create(
        module="settings",
        entity_type="system",
        source_key="general",
        defaults={"title": "Общие настройки", "status": "active", "payload": SYSTEM_DEFAULTS},
    )
    before = dict(record.payload or {})
    record.payload = payload
    record.status = "active"
    record.pilot_revision += 1
    record.save(update_fields=("payload", "status", "pilot_revision", "updated_at"))
    _event(record, "system_settings_saved", actor=actor, before=before)
    return record


@transaction.atomic
def save_user_overlay(*, employee, notifications: bool, active: bool, actor=None) -> WmsNewRecord:
    record, _ = WmsNewRecord.objects.select_for_update().get_or_create(
        module="settings",
        entity_type="user",
        source_key=str(employee.pk),
        defaults={"title": employee.full_name, "status": "active" if employee.is_active else "inactive"},
    )
    before = dict(record.payload or {})
    record.title = employee.full_name
    record.status = "active" if active else "inactive"
    record.payload = {
        **before,
        "notifications": bool(notifications),
        "pilot_active": bool(active),
        "source_role": employee.role,
        "source_active": employee.is_active,
    }
    record.pilot_revision += 1
    record.save()
    _event(record, "user_overlay_saved", actor=actor, before=before)
    return record


@transaction.atomic
def create_user_invitation(*, name: str, role: str, email: str = "", actor=None) -> WmsNewRecord:
    name = str(name or "").strip()
    if not name:
        raise SettingsOperationError("Укажите имя пользователя.")
    source_key = f"invite-{timezone.now().strftime('%Y%m%d%H%M%S%f')}"
    record = WmsNewRecord.objects.create(
        module="settings",
        entity_type="user_invitation",
        source_key=source_key,
        title=name[:255],
        status="pending",
        payload={"role": str(role or "").strip(), "email": str(email or "").strip()},
        pilot_revision=1,
    )
    _event(record, "user_invitation_created", actor=actor)
    return record


@transaction.atomic
def save_place_overlay(*, source_key: str = "", title: str, payload: dict, active: bool, actor=None):
    title = str(title or "").strip()
    if not title:
        raise SettingsOperationError("Укажите название места хранения.")
    source_key = str(source_key or "").strip() or f"manual-{timezone.now().strftime('%Y%m%d%H%M%S%f')}"
    record, _ = WmsNewRecord.objects.select_for_update().get_or_create(
        module="settings", entity_type="place", source_key=source_key
    )
    before = dict(record.payload or {})
    record.title = title[:255]
    record.status = "active" if active else "hidden"
    record.payload = {**before, **payload}
    record.pilot_revision += 1
    record.save()
    _event(record, "place_overlay_saved", actor=actor, before=before)
    return record


@transaction.atomic
def create_printer_workstation(*, name: str, printer: str, actor=None):
    name = str(name or "").strip()
    if not name:
        raise SettingsOperationError("Укажите рабочее место.")
    record = WmsNewRecord.objects.create(
        module="settings",
        entity_type="printer_workstation",
        source_key=f"workstation-{timezone.now().strftime('%Y%m%d%H%M%S%f')}",
        title=name[:255],
        status="active",
        payload={"printer": str(printer or "").strip(), "token": token_urlsafe(18)},
        pilot_revision=1,
    )
    _event(record, "printer_workstation_created", actor=actor)
    return record


@transaction.atomic
def rotate_printer_token(*, actor=None):
    record, _ = WmsNewRecord.objects.select_for_update().get_or_create(
        module="settings",
        entity_type="printer_token",
        source_key="main",
        defaults={"title": "Токен агента печати", "status": "active"},
    )
    before = dict(record.payload or {})
    record.payload = {"token": token_urlsafe(24), "rotated_at": timezone.now().isoformat()}
    record.status = "active"
    record.pilot_revision += 1
    record.save()
    _event(record, "printer_token_rotated", actor=actor, before=before)
    return record


@transaction.atomic
def create_print_job(*, title: str, actor=None):
    title = str(title or "Тестовая этикетка").strip()
    record = WmsNewRecord.objects.create(
        module="settings",
        entity_type="print_job",
        source_key=f"print-{timezone.now().strftime('%Y%m%d%H%M%S%f')}",
        title=title[:255],
        status="waiting",
        payload={},
        pilot_revision=1,
    )
    _event(record, "print_job_created", actor=actor)
    return record


@transaction.atomic
def advance_print_job(*, record_id: int, actor=None):
    record = WmsNewRecord.objects.select_for_update().get(
        pk=record_id, module="settings", entity_type="print_job"
    )
    before = {"status": record.status}
    statuses = {"waiting": "printing", "printing": "printed", "printed": "confirmed"}
    if record.status not in statuses:
        raise SettingsOperationError("Задание печати уже подтверждено.")
    record.status = statuses[record.status]
    record.pilot_revision += 1
    record.save(update_fields=("status", "pilot_revision", "updated_at"))
    _event(record, "print_job_advanced", actor=actor, before=before)
    return record


@transaction.atomic
def save_directory_item(*, directory_type: str, title: str, code: str = "", actor=None):
    if directory_type not in DIRECTORY_TYPES:
        raise SettingsOperationError("Неизвестный справочник.")
    title = str(title or "").strip()
    if not title:
        raise SettingsOperationError("Укажите значение справочника.")
    source_key = f"{directory_type}-{timezone.now().strftime('%Y%m%d%H%M%S%f')}"
    record = WmsNewRecord.objects.create(
        module="settings",
        entity_type=f"directory_{directory_type}",
        source_key=source_key,
        title=title[:255],
        status="active",
        payload={"code": str(code or "").strip()},
        pilot_revision=1,
    )
    _event(record, "directory_item_created", actor=actor)
    return record


@transaction.atomic
def archive_record(*, record_id: int, entity_prefix: str, actor=None):
    record = WmsNewRecord.objects.select_for_update().get(pk=record_id, module="settings")
    if not record.entity_type.startswith(entity_prefix):
        raise SettingsOperationError("Объект не относится к выбранному разделу.")
    before = {"status": record.status}
    record.status = "archived"
    record.pilot_revision += 1
    record.save(update_fields=("status", "pilot_revision", "updated_at"))
    _event(record, "record_archived", actor=actor, before=before)
    return record


@transaction.atomic
def save_label_template(*, record_id=None, name: str, sheet_size: str, elements: str, actor=None):
    name = str(name or "").strip() or "Новый шаблон"
    if sheet_size not in {"30x40", "58x40", "75x120"}:
        raise SettingsOperationError("Некорректный размер листа.")
    if record_id:
        record = WmsNewRecord.objects.select_for_update().get(
            pk=record_id, module="settings", entity_type="label_template"
        )
        before = dict(record.payload or {})
    else:
        record = WmsNewRecord(
            module="settings",
            entity_type="label_template",
            source_key=f"label-{timezone.now().strftime('%Y%m%d%H%M%S%f')}",
        )
        before = {}
    record.title = name[:255]
    record.status = "active"
    record.payload = {"sheet_size": sheet_size, "elements": str(elements or "")[:10000]}
    record.pilot_revision += 1
    record.save()
    _event(record, "label_template_saved", actor=actor, before=before)
    return record


@transaction.atomic
def save_product_mapping(*, product, ozon: str, wb: str, actor=None):
    record, _ = WmsNewRecord.objects.select_for_update().get_or_create(
        module="settings",
        entity_type="goods_mapping",
        source_key=str(product.pk),
        defaults={"title": product.name, "agency": product.agency},
    )
    before = dict(record.payload or {})
    payload = {"ozon": str(ozon or "").strip(), "wb": str(wb or "").strip()}
    record.title = product.name
    record.agency = product.agency
    record.payload = payload
    record.status = "matched" if payload["ozon"] or payload["wb"] else "unmatched"
    record.pilot_revision += 1
    record.save()
    _event(record, "goods_mapping_saved", actor=actor, before=before)
    return record


@transaction.atomic
def create_billing_request(*, request_type: str, amount: str, actor=None):
    if request_type not in {"invoice", "card", "promised"}:
        raise SettingsOperationError("Неизвестный способ пополнения.")
    try:
        numeric_amount = max(0.0, float(str(amount or 0).replace(",", ".")))
    except (TypeError, ValueError) as exc:
        raise SettingsOperationError("Некорректная сумма.") from exc
    record = WmsNewRecord.objects.create(
        module="settings",
        entity_type="billing_request",
        source_key=f"billing-{timezone.now().strftime('%Y%m%d%H%M%S%f')}",
        title={"invoice": "Оплата по счету", "card": "Оплата картой", "promised": "Обещанный платеж"}[request_type],
        status="pending",
        payload={"request_type": request_type, "amount": numeric_amount},
        pilot_revision=1,
    )
    _event(record, "billing_request_created", actor=actor)
    return record


@transaction.atomic
def create_development_task(*, title: str, description: str, actor=None):
    title = str(title or "").strip()
    if not title:
        raise SettingsOperationError("Укажите название задачи.")
    record = WmsNewRecord.objects.create(
        module="help",
        entity_type="development_task",
        source_key=f"dev-{timezone.now().strftime('%Y%m%d%H%M%S%f')}",
        title=title[:255],
        status="new",
        payload={"description": str(description or "").strip()},
        pilot_revision=1,
    )
    WmsNewEvent.objects.create(
        entity_type="help_development_task",
        entity_id=record.pk,
        action="development_task_created",
        actor=actor,
        after={"status": record.status, "title": record.title},
    )
    return record
