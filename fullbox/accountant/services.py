from __future__ import annotations

from decimal import Decimal
import re

from django.core.exceptions import ValidationError
from django.contrib.auth import get_user_model
from django.db.models import Count, ManyToOneRel, OneToOneRel, Max
from django.db import transaction
from django.utils import timezone

from billing.models import (
    BillingService,
    ClientBillingContract,
    ClientLogisticsTariff,
    ClientLogisticsTariffItem,
    ClientTariffItem,
    ClientTariffVersion,
    LogisticsPriceLine,
    StandardServicePrice,
    TariffCategory,
    TariffUnit,
)
from billing.standard_price_catalog import seed_standard_prices
from billing.tariff_services import (
    activate_tariff_version,
    active_tariff_version,
    archive_tariff_version,
    copy_tariff_version,
    create_tariff_version,
)
from head_manager.models import OwnCompany
from sku.models import Agency

from .models import ClientChangeLog, ClientLifecycle
from .selectors import ensure_lifecycle


def normalize_client_prefix(value) -> str:
    prefix = str(value or "").strip().upper()
    if not prefix:
        return ""
    if len(prefix) > 32:
        raise ValidationError("Префикс клиента должен быть не длиннее 32 символов")
    if not re.fullmatch(r"[A-Z0-9]+", prefix):
        raise ValidationError("Префикс клиента может содержать только латинские буквы и цифры")
    return prefix


CLIENT_DELETE_ALLOWED_RELATED = {
    # Черновая бухгалтерская настройка клиента: её можно удалить вместе с пустой карточкой.
    "lifecycle",
    "accountant_change_logs",
    "billing_tariffs",
    "billing_legal_entity_tariffs",
    "billing_contracts",
    "tariff_versions",
    "processing_container_code_sequence",
    "audit_entries",
    "portal_members",
    # Чат ЛК клиента не является рабочей заявкой/номенклатурой и не блокирует удаление пустой карточки.
    "chat_threads",
    "chat_messages",
    "chat_ai_suggestions",
    "chat_ai_kb_candidates",
}

CLIENT_DELETE_REASON_LABELS = {
    "sku_set": "артикулы / номенклатура",
    "orderauditentry_set": "история заявок",
    "other_requests": "другие заявки",
    "other_request_attachments": "файлы других заявок",
    "receiving_order_attachments": "файлы заявок на приёмку",
    "receiving_distribution_plans": "заявки на приёмку",
    "shipping_orders": "заявки на отгрузку",
    "shipping_reserves": "резервы отгрузки",
    "processing_order_attachments": "файлы заявок на обработку",
    "billing_applications": "заявки биллинга",
    "billing_legal_entity_applications": "заявки биллинга как юрлица",
    "billing_charges": "начисления",
    "billing_legal_entity_charges": "начисления как юрлица",
    "billing_acts": "акты",
    "billing_legal_entity_acts": "акты как юрлица",
    "billing_invoices": "счета",
    "billing_legal_entity_invoices": "счета как юрлица",
    "billing_storage_days": "расчёты хранения",
    "storage_billing_periods": "периоды хранения",
    "storage_billing_legal_periods": "периоды хранения как юрлица",
    "storage_adjustments": "корректировки хранения",
    "storage_billing_errors": "ошибки хранения",
    "extra_service_requests": "заявки на доп. услуги",
    "warehouse_service_facts": "складские услуги",
    "upd_requests": "запросы УПД",
    "payment_promises": "обещания оплат",
    "reconciliation_requests": "сверки",
    "billing_discrepancies": "споры/расхождения биллинга",
    "inventory_states": "складские остатки",
    "stock_pallet_states": "остатки по палетам",
    "warehouse_temporary_nomenclature": "временная номенклатура склада",
    "warehouse_containers": "складские контейнеры",
    "warehouse_reserves": "складские резервы",
    "warehouse_operations": "складские операции",
    "warehouse_events": "складские события",
    "warehouse_snapshots": "снимки складских остатков",
    "stores": "склады клиента",
    "market_credentials": "ключи маркетплейсов",
    "finance_documents": "финансовые документы",
    "service_charges": "начисления ЛК клиента",
    "lk_notifications": "уведомления ЛК клиента",
    "chat_threads": "чаты клиента",
    "chat_messages": "сообщения клиента",
    "chat_ai_suggestions": "AI-подсказки чата",
    "chat_ai_kb_candidates": "кандидаты базы знаний чата",
    "receiving_cz_units": "единицы приёмки ЧЗ",
    "move_requests": "заявки ричтрака",
    "reachtruck_box_claims": "задачи ричтрака по коробам",
    "reachtruck_pallet_locks": "блокировки палет ричтрака",
    "otg_delivery_requests": "заявки ОТГ",
    "reachtruck_box_move_operations": "перемещения коробов",
    "super_car_move_requests": "заявки суперкара",
    "super_car_box_claims": "задачи суперкара по коробам",
    "super_car_pallet_locks": "блокировки палет суперкара",
    "market_sync_reports": "отчёты синхронизации маркетплейсов",
    "billing_staff_notifications": "уведомления биллинга",
    "marking_codes": "коды маркировки",
}


def log_client_change(
    agency: Agency,
    *,
    field_name: str,
    old_value="",
    new_value="",
    comment="",
    user=None,
    lifecycle=None,
):
    ClientChangeLog.objects.create(
        agency=agency,
        lifecycle=lifecycle or getattr(agency, "lifecycle", None),
        user=user if getattr(user, "is_authenticated", False) else None,
        field_name=field_name,
        old_value=str(old_value or ""),
        new_value=str(new_value or ""),
        comment=comment or "",
    )


def _related_count(agency: Agency, rel) -> int:
    accessor = rel.get_accessor_name()
    if not accessor:
        return 0
    try:
        related = getattr(agency, accessor)
    except rel.related_model.DoesNotExist:
        return 0
    if isinstance(rel, OneToOneRel):
        return 1 if related is not None else 0
    if isinstance(rel, ManyToOneRel):
        return related.count()
    if hasattr(related, "count"):
        return related.count()
    return 0


def client_delete_blockers(agency: Agency) -> list[str]:
    """Возвращает понятные причины, почему клиента нельзя удалить как пустого."""
    reasons: list[str] = []
    for rel in Agency._meta.related_objects:
        accessor = rel.get_accessor_name()
        if not accessor or accessor in CLIENT_DELETE_ALLOWED_RELATED:
            continue
        count = _related_count(agency, rel)
        if not count:
            continue
        label = CLIENT_DELETE_REASON_LABELS.get(accessor) or rel.related_model._meta.verbose_name_plural
        reasons.append(f"{label}: {count}")
    return reasons


def client_delete_state(agency: Agency) -> dict:
    reasons = client_delete_blockers(agency)
    return {
        "can_delete": not reasons,
        "reasons": reasons,
        "reason_text": "; ".join(reasons),
    }


def client_delete_states_for_agencies(agencies) -> dict[int, dict]:
    """Быстрые статусы удаления для списка клиентов.

    На странице клиентов нельзя вызывать client_delete_state() на каждую строку:
    он делает count() по множеству обратных связей Agency. Для 300 клиентов это
    превращается в тысячи SQL-запросов и длинную загрузку страницы.

    Здесь каждая блокирующая связь считается пачкой по всем показанным клиентам.
    Полная защитная проверка всё равно остаётся в delete_empty_client().
    """
    agency_ids = [agency.id for agency in agencies if getattr(agency, "id", None)]
    states = {
        agency_id: {
            "can_delete": True,
            "reasons": [],
            "reason_text": "",
        }
        for agency_id in agency_ids
    }
    if not agency_ids:
        return states

    for rel in Agency._meta.related_objects:
        accessor = rel.get_accessor_name()
        if not accessor or accessor in CLIENT_DELETE_ALLOWED_RELATED:
            continue
        if not isinstance(rel, (ManyToOneRel, OneToOneRel)):
            continue
        field_name = getattr(getattr(rel, "field", None), "name", "")
        if not field_name:
            continue
        label = CLIENT_DELETE_REASON_LABELS.get(accessor) or rel.related_model._meta.verbose_name_plural
        filter_key = f"{field_name}_id__in"
        try:
            counts = (
                rel.related_model._default_manager.filter(**{filter_key: agency_ids})
                .values(f"{field_name}_id")
                .annotate(total=Count("pk"))
            )
        except Exception:
            continue
        for row in counts:
            agency_id = row.get(f"{field_name}_id")
            if agency_id not in states:
                continue
            reason = f"{label}: {int(row.get('total') or 0)}"
            states[agency_id]["reasons"].append(reason)

    for state in states.values():
        state["can_delete"] = not state["reasons"]
        state["reason_text"] = "; ".join(state["reasons"])
    return states


def _delete_portal_user_if_safe(user_id: int | None) -> bool:
    if not user_id:
        return False
    User = get_user_model()
    user = User.objects.filter(pk=user_id).first()
    if not user or user.is_staff or user.is_superuser:
        return False
    try:
        if getattr(user, "employee_profile", None) is not None:
            return False
    except Exception:
        pass
    user.delete()
    return True


@transaction.atomic
def delete_empty_client(agency: Agency, *, user=None) -> dict:
    state = client_delete_state(agency)
    if not state["can_delete"]:
        raise ValidationError(
            "Нельзя удалить клиента: по нему уже есть рабочие данные. Причины: " + state["reason_text"]
        )

    from audit.models import agency_snapshot, log_agency_change

    agency_id = agency.id
    agency_name = agency.short_name or agency.agn_name or f"Клиент {agency.id}"
    portal_user_id = agency.portal_user_id
    log_agency_change(
        "delete",
        agency,
        user=user,
        description="Удалён пустой клиент из ЛК бухгалтера",
        snapshot=agency_snapshot(agency),
    )
    agency.delete()
    portal_user_deleted = _delete_portal_user_if_safe(portal_user_id)
    return {"id": agency_id, "name": agency_name, "portal_user_deleted": portal_user_deleted}


def sync_serving_contract(agency: Agency, serving_company: OwnCompany | None, *, user=None) -> ClientBillingContract | None:
    if not serving_company:
        return None
    contract = (
        ClientBillingContract.objects.filter(client=agency, is_active=True)
        .order_by("-valid_from", "-id")
        .first()
    )
    if contract:
        if contract.own_company_id != serving_company.id:
            old = contract.own_company_id
            contract.own_company = serving_company
            contract.pricing_mode = ClientBillingContract.PRICING_INDIVIDUAL
            contract.save(update_fields=["own_company", "pricing_mode", "updated_at"])
            log_client_change(
                agency,
                field_name="serving_company",
                old_value=old,
                new_value=serving_company.id,
                user=user,
            )
        return contract
    return ClientBillingContract.objects.create(
        client=agency,
        own_company=serving_company,
        pricing_mode=ClientBillingContract.PRICING_INDIVIDUAL,
        valid_from=timezone.localdate(),
        is_active=True,
        comment="Создано из ЛК бухгалтера",
    )


def sync_client_vat_to_editable_tariffs(agency: Agency, *, user=None) -> dict:
    lifecycle = ensure_lifecycle(agency, user=user)
    vat_type = lifecycle.vat_type or ClientLifecycle.VAT_NO
    updated = 0
    skipped = 0
    for version in ClientTariffVersion.objects.filter(client=agency).order_by("-id"):
        if not version.can_edit_directly():
            skipped += 1
            continue
        if version.vat_type == vat_type:
            continue
        old = version.vat_type
        version.vat_type = vat_type
        version.save(update_fields=["vat_type", "updated_at"])
        updated += 1
        log_client_change(
            agency,
            field_name="tariff_vat_type",
            old_value=old,
            new_value=vat_type,
            comment=f"НДС применён к тарифу #{version.id}",
            user=user,
            lifecycle=lifecycle,
        )
    return {"updated": updated, "skipped": skipped}


def _unit_for_service(service: BillingService) -> TariffUnit:
    raw = (service.unit or "шт").strip() or "шт"
    code = "".join(ch if ch.isalnum() else "-" for ch in raw.lower())[:64] or "sht"
    unit, _ = TariffUnit.objects.get_or_create(
        code=code,
        defaults={"name": raw, "short_name": raw[:64]},
    )
    return unit


def _category_for_service(service: BillingService) -> TariffCategory:
    if service.category_id:
        return service.category
    cat, _ = TariffCategory.objects.get_or_create(
        code="other",
        defaults={"name": "Прочее", "sort_order": 900},
    )
    return cat


def _minimum_from_note(note: str) -> Decimal | None:
    text = (note or "").lower().replace(" ", "")
    if "мин.заказ" in text or "минзаказ" in text:
        digits = "".join(ch if ch.isdigit() or ch == "." else " " for ch in note)
        parts = [p for p in digits.split() if p]
        if parts:
            try:
                return Decimal(parts[0])
            except Exception:
                return None
    return None


def _catalog_rows():
    seed_standard_prices()
    rows = list(
        StandardServicePrice.objects.select_related("service", "service__category")
        .filter(is_active=True, service__is_active=True)
        .order_by("section_code", "line_no", "service__code")
    )
    if rows:
        return rows
    # fallback без прайса — активные услуги биллинга
    return list(
        BillingService.objects.filter(is_active=True, used_in_billing=True)
        .select_related("category")
        .order_by("sort_order", "name", "id")
    )


@transaction.atomic
def create_tariff_from_catalog(
    *,
    client: Agency,
    valid_from=None,
    name: str = "",
    commercial_offer_number: str = "",
    items_active: bool = True,
    user=None,
) -> ClientTariffVersion:
    lifecycle = ensure_lifecycle(client, user=user)
    contract = sync_serving_contract(client, lifecycle.serving_company, user=user)
    version = create_tariff_version(
        client=client,
        contract=contract,
        name=name or f"Тарифы с {timezone.localdate().strftime('%d.%m.%Y')}",
        valid_from=valid_from,
        user=user,
    )
    version.vat_type = lifecycle.vat_type or ClientLifecycle.VAT_NO
    if commercial_offer_number:
        version.additional_agreement_number = commercial_offer_number
    elif lifecycle.commercial_offer_number:
        version.additional_agreement_number = lifecycle.commercial_offer_number
    if lifecycle.contract_date:
        version.contract_date = lifecycle.contract_date
    version.contract_number = getattr(client, "contract_numb", "") or version.contract_number
    version.save()

    catalog = _catalog_rows()
    for idx, row in enumerate(catalog, start=1):
        if isinstance(row, StandardServicePrice):
            service = row.service
            price = row.base_price if row.base_price is not None else (service.default_price or Decimal("0"))
            service_name = row.name or service.name
            note = row.price_note or ""
            sort_order = int(row.section_code or 0) * 1000 + row.line_no * 10
            minimum = _minimum_from_note(note)
        else:
            service = row
            price = service.default_price if service.default_price is not None else Decimal("0")
            service_name = service.name
            note = service.description or ""
            sort_order = service.sort_order or idx * 10
            minimum = None
        ClientTariffItem.objects.create(
            tariff_version=version,
            category=_category_for_service(service),
            service=service,
            service_name=service_name,
            description=note,
            unit=_unit_for_service(service),
            price=price or Decimal("0"),
            minimum_amount=minimum,
            sort_order=sort_order,
            is_active=items_active,
        )
    log_client_change(
        client,
        field_name="tariff_version",
        new_value=version.id,
        comment="Создан тариф из прайса FullBox",
        user=user,
        lifecycle=lifecycle,
    )
    return version


@transaction.atomic
def create_logistics_tariff_from_price_list(
    *,
    client: Agency,
    name: str = "",
    items_active: bool = True,
    user=None,
) -> ClientLogisticsTariff:
    """Создаёт редактируемый тариф доставки клиента из основного прайса."""
    latest = ClientLogisticsTariff.objects.filter(client=client).aggregate(max_version=Max("version_number"))["max_version"] or 0
    tariff = ClientLogisticsTariff.objects.create(
        client=client,
        name=name or f"Логистика с {timezone.localdate().strftime('%d.%m.%Y')}",
        version_number=latest + 1,
        created_by=user if getattr(user, "is_authenticated", False) else None,
    )
    lines = LogisticsPriceLine.objects.filter(is_active=True).order_by("marketplace", "sort_order", "warehouse_name", "id")
    ClientLogisticsTariffItem.objects.bulk_create(
        [
            ClientLogisticsTariffItem(
                tariff=tariff,
                source_line=line,
                marketplace=line.marketplace,
                warehouse_name=line.warehouse_name,
                pickup_days=line.pickup_days,
                delivery_days=line.delivery_days,
                price_per_pallet=line.price_per_pallet,
                minimum_pallets=line.minimum_pallets,
                discount_percent=line.discount_percent,
                comment=line.comment,
                sort_order=line.sort_order,
                is_active=items_active,
            )
            for line in lines
        ]
    )
    log_client_change(
        client,
        field_name="logistics_tariff_version",
        new_value=tariff.id,
        comment="Создан тариф логистики из основного прайса",
        user=user,
    )
    return tariff


@transaction.atomic
def apply_standard_prices_to_tariff(
    version: ClientTariffVersion,
    *,
    overwrite: bool = True,
    user=None,
) -> dict:
    """Заполняет цены черновика из прайса FullBox; цены остаются редактируемыми."""
    if version.status != ClientTariffVersion.STATUS_DRAFT:
        raise ValidationError("Заполнить из прайса можно только черновик тарифа.")
    seed_standard_prices()
    by_service = {
        sp.service_id: sp
        for sp in StandardServicePrice.objects.select_related("service").filter(is_active=True)
    }
    filled = 0
    skipped = 0
    missing_created = 0
    existing_ids = set(version.items.values_list("service_id", flat=True))

    for item in version.items.select_related("service"):
        sp = by_service.get(item.service_id)
        if not sp or sp.base_price is None:
            skipped += 1
            continue
        if not overwrite and item.price is not None and item.price > 0:
            skipped += 1
            continue
        item.price = sp.base_price
        if sp.price_note and not item.conditions:
            item.conditions = sp.price_note
        minimum = _minimum_from_note(sp.price_note)
        if minimum is not None and item.minimum_amount is None:
            item.minimum_amount = minimum
        if sp.name:
            item.service_name = sp.name
        item.save()
        filled += 1

    # Добавить строки прайса, которых ещё нет в черновике
    for sp in StandardServicePrice.objects.select_related("service", "service__category").filter(is_active=True).order_by(
        "section_code", "line_no"
    ):
        if sp.service_id in existing_ids or not sp.service.is_active:
            continue
        ClientTariffItem.objects.create(
            tariff_version=version,
            category=_category_for_service(sp.service),
            service=sp.service,
            service_name=sp.name or sp.service.name,
            description=sp.price_note or "",
            unit=_unit_for_service(sp.service),
            price=sp.base_price or Decimal("0"),
            minimum_amount=_minimum_from_note(sp.price_note),
            sort_order=int(sp.section_code or 0) * 1000 + sp.line_no * 10,
            is_active=True,
        )
        missing_created += 1

    log_client_change(
        version.client,
        field_name="tariff_prices",
        new_value=f"filled={filled};added={missing_created}",
        comment="Цены заполнены из прайса FullBox 2026-07-17",
        user=user,
    )
    return {"filled": filled, "skipped": skipped, "added": missing_created}


def validate_publish_tariff(version: ClientTariffVersion) -> list[str]:
    errors: list[str] = []
    if not version.valid_from:
        errors.append("Не указана дата начала действия")
    if not version.client_id:
        errors.append("Не выбран клиент")
    lifecycle = getattr(version.client, "lifecycle", None)
    if lifecycle is None:
        try:
            lifecycle = version.client.lifecycle
        except ClientLifecycle.DoesNotExist:
            lifecycle = None
    if not lifecycle or not lifecycle.serving_company_id:
        errors.append("Не выбрана компания обслуживания FullBox")
    contract_ok = bool(getattr(version.client, "contract_numb", "") or version.contract_number)
    kp_ok = bool(version.additional_agreement_number or (lifecycle and lifecycle.commercial_offer_number))
    if not contract_ok and not kp_ok:
        errors.append("Не заполнен договор или КП")
    active_items = list(version.items.filter(is_active=True))
    if not active_items:
        errors.append("В тарифе нет активных услуг")
    for item in active_items:
        if item.price is None or item.price <= 0:
            errors.append(f"Не заполнена цена: {item.service_name}")
    return errors


@transaction.atomic
def publish_tariff(version: ClientTariffVersion, *, user=None) -> ClientTariffVersion:
    errors = validate_publish_tariff(version)
    if errors:
        raise ValidationError({"errors": errors, "message": "Нельзя опубликовать тариф. " + "; ".join(errors)})
    return activate_tariff_version(version, user=user)


@transaction.atomic
def delete_draft_tariff_version(version: ClientTariffVersion, *, user=None) -> dict:
    if version.status != ClientTariffVersion.STATUS_DRAFT:
        raise ValidationError("Удалять можно только черновик прайса. Опубликованные прайсы архивируйте.")

    usage_reasons: list[str] = []
    charges_count = version.charges.count()
    if charges_count:
        usage_reasons.append(f"начисления: {charges_count}")
    storage_days_count = version.storage_days.count()
    if storage_days_count:
        usage_reasons.append(f"дни хранения: {storage_days_count}")
    if usage_reasons:
        raise ValidationError("Нельзя удалить прайс: он уже связан с расчётами. Причины: " + "; ".join(usage_reasons))

    client = version.client
    version_id = version.id
    version_name = version.name
    items_count = version.items.count()
    conditions_count = version.conditions.count()
    log_client_change(
        client,
        field_name="tariff_version_deleted",
        old_value=version_id,
        comment=f"Удалён черновик прайса «{version_name}»",
        user=user,
        lifecycle=getattr(client, "lifecycle", None),
    )
    version.delete()
    return {
        "id": version_id,
        "name": version_name,
        "items_deleted": items_count,
        "conditions_deleted": conditions_count,
    }


def validate_activate_client(agency: Agency) -> list[str]:
    errors: list[str] = []
    lifecycle = ensure_lifecycle(agency)
    if not (agency.agn_name or "").strip():
        errors.append("Не заполнено юридическое название")
    if not (agency.inn or "").strip():
        errors.append("Не заполнен ИНН")
    raw_prefix = str(agency.pref or "").strip()
    if not raw_prefix:
        errors.append("Не заполнен префикс клиента для коробов и паллет")
    else:
        try:
            prefix = normalize_client_prefix(raw_prefix)
        except ValidationError as exc:
            errors.extend(exc.messages)
        else:
            if Agency.objects.exclude(pk=agency.pk).filter(pref__iexact=prefix).exists():
                errors.append(f"Префикс клиента {prefix} уже используется другим клиентом")
    if not lifecycle.client_type:
        errors.append("Не выбран тип клиента")
    if not lifecycle.serving_company_id:
        errors.append("Не выбрана компания обслуживания FullBox")
    else:
        company = lifecycle.serving_company
        if not company.tax_mode:
            errors.append("Не выбран формат НДС у компании обслуживания")
    contract_ok = bool((agency.contract_numb or "").strip())
    kp_ok = bool((lifecycle.commercial_offer_number or "").strip())
    if not contract_ok and not kp_ok:
        errors.append("Не заполнен договор или КП")
    version = active_tariff_version(agency)
    if not version:
        errors.append("Не опубликован тариф клиента")
    else:
        for item in version.items.filter(is_active=True):
            if item.price is None or item.price <= 0:
                errors.append(f"В тарифе активная услуга без цены: {item.service_name}")
    return errors


@transaction.atomic
def activate_client(agency: Agency, *, user=None) -> ClientLifecycle:
    errors = validate_activate_client(agency)
    if errors:
        raise ValidationError(
            {
                "errors": errors,
                "message": "Нельзя активировать клиента. Заполните обязательные поля:\n- " + "\n- ".join(errors),
            }
        )
    lifecycle = ensure_lifecycle(agency, user=user)
    old = lifecycle.status
    lifecycle.status = ClientLifecycle.STATUS_ACTIVE
    lifecycle.activated_by = user if getattr(user, "is_authenticated", False) else None
    lifecycle.activated_at = timezone.now()
    lifecycle.save()
    if agency.archived:
        agency.archived = False
        agency.save(update_fields=["archived"])
    sync_serving_contract(agency, lifecycle.serving_company, user=user)
    log_client_change(
        agency,
        field_name="status",
        old_value=old,
        new_value=lifecycle.status,
        comment="Клиент активирован бухгалтером",
        user=user,
        lifecycle=lifecycle,
    )
    return lifecycle


def update_lifecycle_fields(agency: Agency, data: dict, *, user=None) -> ClientLifecycle:
    lifecycle = ensure_lifecycle(agency, user=user)
    requested_status = str(data.get("status") or "")
    if requested_status == ClientLifecycle.STATUS_ACTIVE and lifecycle.status != ClientLifecycle.STATUS_ACTIVE:
        raise ValidationError("Активируйте клиента отдельной кнопкой после проверки обязательных полей")
    tracked = [
        "status",
        "client_type",
        "postal_address",
        "email_documents",
        "email_notifications",
        "accountant_comment",
        "commercial_offer_number",
        "contract_date",
        "vat_type",
    ]
    for field in tracked:
        if field not in data:
            continue
        old = getattr(lifecycle, field)
        new = data[field]
        if field == "vat_type":
            allowed = {choice[0] for choice in ClientLifecycle.VAT_TYPE_CHOICES}
            if new not in allowed:
                raise ValidationError("Выберите корректный режим НДС клиента.")
        if str(old or "") != str(new or ""):
            setattr(lifecycle, field, new)
            log_client_change(agency, field_name=field, old_value=old, new_value=new, user=user, lifecycle=lifecycle)
            if field == "vat_type":
                agency.use_nds = new != ClientLifecycle.VAT_NO
                agency.save(update_fields=["use_nds"])
    if "serving_company_id" in data:
        company_id = data.get("serving_company_id") or None
        old = lifecycle.serving_company_id
        if company_id:
            company = OwnCompany.objects.filter(pk=company_id, is_active=True).first()
            if not company:
                raise ValidationError("Компания обслуживания не найдена")
            lifecycle.serving_company = company
            sync_serving_contract(agency, company, user=user)
        else:
            lifecycle.serving_company = None
        if old != lifecycle.serving_company_id:
            log_client_change(
                agency,
                field_name="serving_company",
                old_value=old,
                new_value=lifecycle.serving_company_id,
                user=user,
                lifecycle=lifecycle,
            )
    lifecycle.save()
    if "vat_type" in data:
        sync_client_vat_to_editable_tariffs(agency, user=user)
    return lifecycle


def create_requisites_change_request(*, agency: Agency, comment: str, user=None):
    from employees.models import Employee
    from todo.models import Task

    accountant = Employee.objects.filter(role="accountant", is_active=True).order_by("id").first()
    title = f"Изменение реквизитов: {agency.short_name or agency.agn_name or agency.id}"
    description = (
        f"Клиент #{agency.id} запросил изменение реквизитов.\n"
        f"ИНН: {agency.inn or '—'}\n"
        f"Комментарий: {comment or '—'}"
    )
    return Task.objects.create(
        title=title,
        description=description,
        route=f"/accountant/clients/{agency.id}/",
        assigned_to=accountant,
        created_by=user if getattr(user, "is_authenticated", False) else None,
        status="in_progress",
        priority="high",
    )


# re-export for API convenience
__all__ = [
    "activate_client",
    "apply_standard_prices_to_tariff",
    "archive_tariff_version",
    "client_delete_blockers",
    "client_delete_state",
    "client_delete_states_for_agencies",
    "copy_tariff_version",
    "create_requisites_change_request",
    "create_tariff_from_catalog",
    "delete_draft_tariff_version",
    "delete_empty_client",
    "log_client_change",
    "publish_tariff",
    "sync_serving_contract",
    "sync_client_vat_to_editable_tariffs",
    "update_lifecycle_fields",
    "validate_activate_client",
    "validate_publish_tariff",
]
