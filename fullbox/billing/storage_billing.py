"""Ежедневный биллинг хранения — запись только в billing (склад read-only)."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
import calendar

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from sku.models import Agency

from .models import (
    ApplicationCharge,
    BillingApplication,
    BillingService,
    BillingStorageDay,
    StorageBillingError,
    StorageSnapshotLine,
)
from .price_resolver import resolve_client_service_price
from .services import BillingWorkflowService
from .statuses import BillingStatus
from .storage import agencies_with_storage_stock, count_client_pallets
from .storage_engine import (
    calculate_storage_day,
    ensure_rule_for_version,
    money_quantize,
    service_code_for_mode,
)
from .storage_volume import liters_to_m3, quantize_volume, weight_to_storage_liters
from .tariff_services import active_tariff_version

STORAGE_SERVICE_CODE = "storage_pallet_day"


def snapshot_box_counts(rows: list[dict]) -> tuple[dict[str, int], int]:
    """Count unique boxes as they were linked to pallets at snapshot time."""
    boxes_by_pallet: dict[str, set[str]] = {}
    all_boxes: set[str] = set()
    for row in rows:
        pallet_code = str(row.get("pallet_code") or "").strip()
        box_code = str(row.get("box_code") or "").strip()
        if pallet_code:
            boxes_by_pallet.setdefault(pallet_code, set())
        if not box_code:
            continue
        all_boxes.add(box_code)
        if pallet_code:
            boxes_by_pallet[pallet_code].add(box_code)
    return (
        {code: len(boxes) for code, boxes in sorted(boxes_by_pallet.items())},
        len(all_boxes),
    )


def ensure_storage_service(code: str = STORAGE_SERVICE_CODE) -> BillingService:
    defaults = {
        "storage_pallet_day": ("Хранение палеты/день", "пал."),
        "storage_pallet_week": ("Хранение палеты/неделя", "пал."),
        "storage_pallet_month": ("Хранение палеты/месяц", "пал."),
        "storage_liter_day": ("Хранение, литр/сутки", "л"),
        "storage_liter_week": ("Хранение, литр/неделя", "л"),
        "storage_liter_month": ("Хранение, литр/месяц", "л"),
        "storage_m3_day": ("Хранение, м³/сутки", "м³"),
        "storage_m3_week": ("Хранение, м³/неделя", "м³"),
        "storage_m3_month": ("Хранение, м³/месяц", "м³"),
    }
    name, unit = defaults.get(code, ("Хранение", "ед."))
    service, _ = BillingService.objects.get_or_create(
        code=code,
        defaults={
            "name": name,
            "unit": unit,
            "vat_rate": "20",
            "is_active": True,
        },
    )
    return service


def storage_application_id(year: int, month: int) -> str:
    return f"STR-{year:04d}-{month:02d}"


class StorageBillingService:
    # Ступени палетного хранения выбираются из включённых бухгалтером строк
    # действующего тарифа. Базовая цена не должна подменять включённую цену
    # «от 10/100/150 палет».
    PALLET_DAY_TIER_CODES = (
        (Decimal("150"), "storage_pallet_day_150plus"),
        (Decimal("100"), "storage_pallet_day_100plus"),
        (Decimal("10"), "storage_pallet_day_10plus"),
    )

    @staticmethod
    def _service_for_storage_mode(
        application: BillingApplication,
        billing_mode: str,
        on_day: date,
        *,
        quantity: Decimal,
    ) -> BillingService:
        """Выбрать услугу хранения по режиму, количеству и активной ступени.

        Настройка остаётся в тарифе клиента: бухгалтер включает нужные
        строки. При палетном посуточном хранении выбирается самая высокая
        подходящая активная ступень; для прочих режимов сохраняется прежний
        выбор услуги.
        """
        base_code = service_code_for_mode(billing_mode)
        fallback = ensure_storage_service(base_code)
        if base_code != STORAGE_SERVICE_CODE:
            return fallback

        tariff_version = active_tariff_version(application.client, on_day)
        if tariff_version is None:
            return fallback

        normalized_quantity = Decimal(str(quantity or 0))
        for threshold, service_code in StorageBillingService.PALLET_DAY_TIER_CODES:
            if normalized_quantity < threshold:
                continue
            item = (
                tariff_version.items.select_related("service")
                .filter(service__code=service_code, is_active=True)
                .order_by("id")
                .first()
            )
            if item is not None:
                return item.service

        base_item = (
            tariff_version.items.select_related("service")
            .filter(service__code=base_code, is_active=True)
            .order_by("id")
            .first()
        )
        return base_item.service if base_item is not None else fallback

    @staticmethod
    def late_receiving_storage_preview(application: BillingApplication) -> dict:
        """Storage days missed before a receiving was entered in the system."""
        if application.application_type != BillingApplication.TYPE_RECEIVING:
            raise ValidationError("Корректировка хранения доступна только для приёмки.")
        payload = dict(application.source_payload or {})
        payload.update(payload.get("payload") or {})
        raw_received = payload.get("received_at")
        try:
            received = timezone.localdate(datetime.fromisoformat(str(raw_received).replace("Z", "+00:00")))
        except (TypeError, ValueError):
            raise ValidationError("В факте приёмки не указана фактическая дата.")
        completed = application.operations_completed_at or timezone.now()
        completed_day = timezone.localdate(completed)
        days = [received + timedelta(days=index) for index in range(max((completed_day - received).days, 0))]
        pallets = payload.get("actual_pallets") or payload.get("pallet_count") or payload.get("pallets_count")
        if not pallets and isinstance(payload.get("act_pallets"), list):
            pallets = len(payload["act_pallets"])
        try:
            pallets = Decimal(str(pallets or 0))
        except Exception:
            pallets = Decimal("0")
        if pallets <= 0:
            raise ValidationError("В факте приёмки не указано число принятых палет.")
        return {"received_day": received, "days": days, "pallets": pallets}

    @staticmethod
    @transaction.atomic
    def apply_late_receiving_storage(application: BillingApplication, *, user=None, dry_run: bool = False) -> dict:
        preview = StorageBillingService.late_receiving_storage_preview(application)
        created = []
        skipped = 0
        for day in preview["days"]:
            storage_app = StorageBillingService.ensure_month_application(application.client, year=day.year, month=day.month, user=user)
            source_key = f"storage-late-receiving:{application.id}:{day.isoformat()}"
            if ApplicationCharge.objects.filter(application=storage_app, source_key=source_key).exists():
                skipped += 1
                continue
            service = StorageBillingService._service_for_storage_mode(
                storage_app,
                "pallet_day",
                day,
                quantity=preview["pallets"],
            )
            resolved = StorageBillingService._resolve_tariff(storage_app, service, day, quantity=preview["pallets"])
            if not resolved.ok or resolved.tariff is None or Decimal(resolved.tariff) <= 0:
                raise ValidationError(f"Нет тарифа хранения на {day.strftime('%d.%m.%Y')}.")
            if dry_run:
                created.append({"day": day.isoformat(), "amount": str(money_quantize(preview["pallets"] * Decimal(resolved.tariff)))})
                continue
            charge = BillingWorkflowService.create_or_update_charge(storage_app, service=service, quantity=preview["pallets"], tariff=resolved.tariff, unit=resolved.unit or "пал.", vat_rate=resolved.vat_rate, source_type=ApplicationCharge.SOURCE_MANUAL, source_id=f"receiving:{application.id}:{day.isoformat()}", source_key=source_key, performed_at=timezone.make_aware(datetime.combine(day, datetime.min.time().replace(hour=23, minute=59))), billing_period=day.replace(day=1), operation_type="late_receiving_storage", operation_id=str(application.id), comment=f"Хранение по фактической дате приёмки {application.application_id}: {day:%d.%m.%Y}", user=user, client_tariff_version=resolved.tariff_version, client_tariff_item=resolved.tariff_item, resolve_from_agreed_tariff=False)
            created.append({"day": day.isoformat(), "amount": str(charge.amount), "charge_id": charge.id})
        return {"received_day": preview["received_day"].isoformat(), "pallets": str(preview["pallets"]), "days": created, "skipped": skipped, "applied": not dry_run}
    @staticmethod
    def _tariff_with_daily_minimum(*, tariff, quantity, rule):
        """Вернуть цену за единицу, обеспечивающую минимум за день без НДС."""
        qty = Decimal(str(quantity or 0))
        price = Decimal(str(tariff or 0))
        minimum = Decimal(str(getattr(rule, "min_amount_day", 0) or 0))
        raw_amount = money_quantize(qty * price)
        if minimum > 0 and qty > 0 and 0 < raw_amount < minimum:
            return (minimum / qty).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP), minimum
        return price, raw_amount

    @staticmethod
    def ensure_month_application(client: Agency, *, year: int, month: int, user=None) -> BillingApplication:
        app_id = storage_application_id(year, month)
        period_start = date(year, month, 1)
        created_at = timezone.make_aware(datetime(year, month, 1, 0, 0, 0))
        from .sync import _resolve_manager

        application = BillingWorkflowService.sync_application_from_source(
            application_type=BillingApplication.TYPE_STORAGE,
            application_id=app_id,
            client=client,
            legal_entity=client,
            manager=_resolve_manager(client),
            operational_status="storage",
            operational_status_label="Хранение",
            created_at_source=created_at,
            source_payload={"source": "storage_billing", "period": app_id},
            user=user,
        )
        if application.billing_status == BillingStatus.NOT_CALCULATED:
            application.billing_status = BillingStatus.CALCULATION_DRAFT
            application.save(update_fields=["billing_status", "updated_at"])
        if not application.source_payload.get("period_start"):
            payload = dict(application.source_payload or {})
            payload["period_start"] = period_start.isoformat()
            application.source_payload = payload
            application.save(update_fields=["source_payload", "updated_at"])
        return application

    @staticmethod
    def _resolve_tariff(application: BillingApplication, service: BillingService, on_day: date, *, quantity=None):
        return resolve_client_service_price(
            application,
            service,
            performed_at=timezone.make_aware(datetime.combine(on_day, datetime.min.time())),
            quantity=quantity,
        )

    @staticmethod
    def _period_closed(client: Agency, day: date) -> bool:
        from .models import StorageBillingPeriod

        return StorageBillingPeriod.objects.filter(
            client=client,
            year=day.year,
            month=day.month,
            status=StorageBillingPeriod.STATUS_CLOSED,
        ).exists()

    @staticmethod
    @transaction.atomic
    def recalculate_saved_daily_volume(day_row: BillingStorageDay, *, user=None) -> str:
        """Пересчитать дневной объём хранения по сохранённому снимку биллинга.

        Склад не читается и не изменяется. Формула использует сохранённые в
        снимке общий фактический объём и общий вес клиента.
        """
        mode = str(day_row.billing_mode or "")
        if mode not in {"m3_day", "liter_day"}:
            return "skipped_mode"

        payload = dict(day_row.payload or {})
        try:
            total_weight_kg = quantize_volume(payload["total_weight_kg"])
        except KeyError:
            return "skipped_missing_total_weight"
        except (ArithmeticError, TypeError, ValueError):
            return "skipped_invalid_total_weight"

        physical_l = Decimal(str(day_row.physical_volume_l or "0"))
        if physical_l <= 0 and day_row.lines.exists():
            physical_l = sum((Decimal(str(line.total_volume_l or 0)) for line in day_row.lines.all()), Decimal("0"))
        physical_l = quantize_volume(physical_l)
        use_m3 = mode == "m3_day"
        weight_l = weight_to_storage_liters(total_weight_kg)
        billable_l = quantize_volume(max(physical_l, weight_l))
        billable = liters_to_m3(billable_l) if use_m3 else billable_l
        if use_m3:
            billable_m3 = quantize_volume(billable)
            billable_l = quantize_volume(billable_m3 * Decimal("1000"))
        else:
            billable_l = quantize_volume(billable)
            billable_m3 = liters_to_m3(billable_l)

        payload.update(
            {
                "quantity": str(billable),
                "weight_norm_kg_per_pallet_place": "450",
                "pallet_place_volume_m3": "1.6",
                "weight_volume_l": str(weight_l),
                "weight_volume_m3": str(liters_to_m3(weight_l)),
                "volume_formula": "MAX(Actual_CBM, (Weight_KG / 450) * 1.6)",
                "billing_cbm_rule": {
                    "version": 1,
                    "weight_norm_kg": "450",
                    "weight_equivalent_m3": "1.6",
                    "actual_cbm": str(liters_to_m3(physical_l)),
                    "weight_kg": str(total_weight_kg),
                    "weight_based_cbm": str(liters_to_m3(weight_l)),
                    "billing_cbm": str(billable_m3),
                    "fractional_pallets": True,
                    "round_up": False,
                },
                "volume_recalculated_from_snapshot_at": timezone.now().isoformat(),
            }
        )
        day_row.physical_volume_l = physical_l
        day_row.physical_volume_m3 = liters_to_m3(physical_l)
        day_row.billable_volume_l = billable_l
        day_row.billable_volume_m3 = billable_m3
        day_row.payload = payload
        day_row.save(
            update_fields=[
                "physical_volume_l", "physical_volume_m3", "billable_volume_l",
                "billable_volume_m3", "payload", "updated_at",
            ]
        )
        return "volume_recalculated"

    @staticmethod
    @transaction.atomic
    def reprice_saved_storage_day(day_row: BillingStorageDay, *, user=None) -> str:
        """Пересчитать цену по сохранённому дневному снимку, не читая склад.

        Применяется к неотправленным документам. Объём и палеты остаются теми,
        которыми были в день снимка; меняются только тариф, НДС и итог суммы.
        """
        application = day_row.application or StorageBillingService.ensure_month_application(
            day_row.client, year=day_row.day.year, month=day_row.day.month, user=user
        )
        payload = dict(day_row.payload or {})
        raw_quantity = payload.get("quantity")
        quantity = Decimal(str(raw_quantity or (day_row.charge.quantity if day_row.charge_id else "0")))
        if quantity <= 0:
            return "skipped_zero"
        service = StorageBillingService._service_for_storage_mode(
            application,
            day_row.billing_mode,
            day_row.day,
            quantity=quantity,
        )
        resolved = StorageBillingService._resolve_tariff(application, service, day_row.day, quantity=quantity)
        if not resolved.ok or resolved.tariff is None:
            day_row.status = BillingStorageDay.STATUS_NEEDS_REVIEW
            day_row.save(update_fields=["status", "updated_at"])
            StorageBillingError.objects.update_or_create(
                client=day_row.client,
                day=day_row.day,
                error_type=StorageBillingError.TYPE_NO_TARIFF,
                resolved_at=None,
                defaults={
                    "storage_day": day_row,
                    "severity": StorageBillingError.SEVERITY_ERROR,
                    "message": resolved.note or f"Нет тарифа на услугу {service.code}",
                    "payload": {"service_code": service.code, "quantity": str(quantity)},
                },
            )
            return "missing_tariff"

        if day_row.charge_id:
            # Обновляем связанный объект из БД: в противном случае prefetch/cache
            # мог оставить старый флаг и строка из чернового акта не обновлялась.
            day_row.charge.refresh_from_db()
            if day_row.charge.is_excluded:
                payload["reprice_skipped"] = "charge_excluded"
                day_row.status = BillingStorageDay.STATUS_NEEDS_REVIEW
                day_row.payload = payload
                day_row.save(update_fields=["status", "payload", "updated_at"])
                StorageBillingError.objects.update_or_create(
                    client=day_row.client,
                    day=day_row.day,
                    error_type=StorageBillingError.TYPE_OTHER,
                    resolved_at=None,
                    defaults={
                        "storage_day": day_row,
                        "severity": StorageBillingError.SEVERITY_WARN,
                        "message": "Снимок хранения пересчитан, но начисление ранее исключено и не возвращено автоматически.",
                        "payload": {"charge_id": day_row.charge_id, "reason": "charge_excluded"},
                    },
                )
                return "excluded_charge"
            if day_row.charge.is_included_in_act and not day_row.charge.is_included_in_invoice:
                # Черновой акт освобождается, черновой счёт синхронизируется внутри
                # create_or_update_charge; отправленные документы вызывающий код уже
                # исключил из пересчёта.
                BillingWorkflowService.ensure_charge_editable(day_row.charge, user=user)
        rule = ensure_rule_for_version(resolved.tariff_version)
        effective_tariff, _expected_amount = StorageBillingService._tariff_with_daily_minimum(
            tariff=resolved.tariff,
            quantity=quantity,
            rule=rule,
        )
        performed_at = timezone.make_aware(datetime.combine(day_row.day, datetime.min.time().replace(hour=23, minute=59)))
        charge = BillingWorkflowService.create_or_update_charge(
            application,
            service=service,
            quantity=quantity,
            tariff=effective_tariff,
            unit=resolved.unit or (day_row.charge.unit if day_row.charge_id else service.unit),
            vat_rate=resolved.vat_rate,
            source_type=ApplicationCharge.SOURCE_STORAGE_DAY,
            source_id=day_row.day.isoformat(),
            source_key=f"storage:{day_row.client_id}:{day_row.day.isoformat()}",
            performed_at=performed_at,
            billing_period=day_row.day.replace(day=1),
            operation_type="storage",
            operation_id=day_row.day.isoformat(),
            comment=(day_row.charge.comment if day_row.charge_id else f"Хранение {day_row.day.isoformat()} [{day_row.billing_mode}]"),
            user=user,
            client_tariff_version=resolved.tariff_version,
            client_tariff_item=resolved.tariff_item,
            tariff_price=resolved.tariff_price,
            coefficient=resolved.coefficient,
            minimum_amount=max(
                Decimal(str(resolved.minimum_amount or 0)),
                Decimal(str(rule.min_amount_day or 0)),
            ) or None,
            tariff_source_label=resolved.tariff_source_label,
            tariff_basis=resolved.tariff_basis,
            service_name_snapshot=resolved.service_name or service.name,
            resolve_from_agreed_tariff=False,
        )
        payload["repriced_at"] = timezone.now().isoformat()
        payload["repriced_from_snapshot"] = True
        day_row.application = application
        day_row.charge = charge
        day_row.amount = money_quantize(charge.amount)
        day_row.vat_amount = money_quantize(charge.vat_amount)
        day_row.status = BillingStorageDay.STATUS_CALCULATED
        day_row.tariff_version = resolved.tariff_version
        day_row.calculation_rule = rule
        day_row.payload = payload
        day_row.save(
            update_fields=[
                "application", "charge", "amount", "vat_amount", "status",
                "tariff_version", "calculation_rule", "payload", "updated_at",
            ]
        )
        StorageBillingError.objects.filter(
            client=day_row.client,
            day=day_row.day,
            error_type=StorageBillingError.TYPE_NO_TARIFF,
            resolved_at__isnull=True,
        ).update(resolved_at=timezone.now(), resolved_by=user if getattr(user, "pk", None) else None)
        return "repriced"

    @staticmethod
    def _write_errors(client, day_row, day, errors: list, user=None):
        StorageBillingError.objects.filter(
            client=client,
            day=day,
            resolved_at__isnull=True,
            error_type=StorageBillingError.TYPE_NO_DIMS,
        ).delete()
        for err in errors:
            StorageBillingError.objects.create(
                client=client,
                day=day,
                storage_day=day_row,
                error_type=err.get("type") or StorageBillingError.TYPE_OTHER,
                severity=StorageBillingError.SEVERITY_WARN,
                message=err.get("message") or "",
                sku_code=err.get("sku_code") or "",
                box_code=err.get("box_code") or "",
                pallet_code=err.get("pallet_code") or "",
                payload=err,
            )

    @staticmethod
    def _write_lines(day_row: BillingStorageDay, items) -> None:
        day_row.lines.all().delete()
        bulk = []
        for item in items:
            bulk.append(
                StorageSnapshotLine(
                    day=day_row,
                    sku_id=item.sku_id,
                    sku_code=item.sku_code[:128],
                    name=(item.name or "")[:255],
                    barcode=(item.barcode or "")[:128],
                    box_code=(item.box_code or "")[:128],
                    pallet_code=(item.pallet_code or "")[:128],
                    zone_code=(item.zone_code or "")[:64],
                    cell_code=(item.cell_code or "")[:128],
                    quantity=item.quantity,
                    length_mm=item.length_mm,
                    width_mm=item.width_mm,
                    height_mm=item.height_mm,
                    unit_volume_l=item.unit_volume_l,
                    total_volume_l=item.total_volume_l,
                    total_volume_m3=item.total_volume_m3,
                    coefficient=item.coefficient,
                    billable_volume_l=item.billable_volume_l,
                    billable_volume_m3=item.billable_volume_m3,
                    volume_level=item.volume_level,
                    dims_source=item.dims_source,
                    status=item.status if item.status in {"ok", "no_dims"} else "ok",
                )
            )
        if bulk:
            StorageSnapshotLine.objects.bulk_create(bulk, batch_size=500)

    @staticmethod
    @transaction.atomic
    def record_storage_day(client: Agency, *, day: date | None = None, user=None) -> BillingStorageDay:
        day = day or timezone.localdate()

        if StorageBillingService._period_closed(client, day):
            existing = BillingStorageDay.objects.filter(client=client, day=day).first()
            if existing:
                return existing
            application = StorageBillingService.ensure_month_application(
                client, year=day.year, month=day.month, user=user
            )
            day_row, _ = BillingStorageDay.objects.update_or_create(
                client=client,
                day=day,
                defaults={
                    "application": application,
                    "status": BillingStorageDay.STATUS_CONFIRMED,
                    "payload": {"skipped": "period_closed"},
                },
            )
            StorageBillingError.objects.get_or_create(
                client=client,
                day=day,
                error_type=StorageBillingError.TYPE_CLOSED_PERIOD,
                resolved_at=None,
                defaults={
                    "storage_day": day_row,
                    "severity": StorageBillingError.SEVERITY_INFO,
                    "message": f"Период {day.year}-{day.month:02d} закрыт — пересчёт запрещён",
                },
            )
            return day_row

        counts = count_client_pallets(client)
        rows = counts.get("rows") or []
        pallet_box_counts, source_box_count = snapshot_box_counts(rows)
        application = StorageBillingService.ensure_month_application(
            client,
            year=day.year,
            month=day.month,
            user=user,
        )
        tariff_version = active_tariff_version(client, day)
        rule = ensure_rule_for_version(tariff_version)
        calc = calculate_storage_day(rows, rule, day=day, pallet_counts=counts)

        service = StorageBillingService._service_for_storage_mode(
            application,
            calc.billing_mode,
            day,
            quantity=calc.quantity,
        )
        source_key = f"storage:{client.id}:{day.isoformat()}"
        charge = None
        amount = Decimal("0")
        vat_amount = Decimal("0")
        status = BillingStorageDay.STATUS_CALCULATED

        quantity = calc.quantity
        resolved = StorageBillingService._resolve_tariff(application, service, day, quantity=quantity)

        if not calc.skip_charge and quantity > 0 and resolved.ok and resolved.tariff is not None and Decimal(resolved.tariff) > 0:
            performed_at = timezone.make_aware(datetime.combine(day, datetime.min.time().replace(hour=23, minute=59)))
            effective_tariff, expected_amount = StorageBillingService._tariff_with_daily_minimum(
                tariff=resolved.tariff,
                quantity=quantity,
                rule=rule,
            )
            if calc.unit == "пал.":
                charge_comment = (
                    f"Хранение {day.isoformat()} [{calc.billing_mode}]: "
                    f"qty={quantity} {calc.unit}; пал={calc.pallet_count}"
                )
            else:
                charge_comment = (
                    f"Хранение {day.isoformat()} [{calc.billing_mode}]: "
                    f"qty={quantity} {calc.unit}; "
                    f"пал={calc.pallet_count}; "
                    f"л={calc.billable_volume_l}; м³={calc.billable_volume_m3}"
                )
            charge = BillingWorkflowService.create_or_update_charge(
                application,
                service=service,
                quantity=quantity,
                tariff=effective_tariff,
                unit=resolved.unit or calc.unit or service.unit,
                vat_rate=resolved.vat_rate,
                source_type=ApplicationCharge.SOURCE_STORAGE_DAY,
                source_id=day.isoformat(),
                source_key=source_key,
                performed_at=performed_at,
                billing_period=day.replace(day=1),
                operation_type="storage",
                operation_id=day.isoformat(),
                comment=charge_comment,
                user=user,
                client_tariff_version=resolved.tariff_version or tariff_version,
                client_tariff_item=resolved.tariff_item,
                tariff_price=resolved.tariff_price,
                coefficient=resolved.coefficient,
                minimum_amount=max(
                    Decimal(str(resolved.minimum_amount or 0)),
                    Decimal(str(rule.min_amount_day or 0)),
                ) or None,
                tariff_source_label=resolved.tariff_source_label,
                tariff_basis=resolved.tariff_basis,
                service_name_snapshot=resolved.service_name or service.name,
                resolve_from_agreed_tariff=False,
            )
            if charge:
                amount = money_quantize(charge.amount)
                if expected_amount != money_quantize(quantity * Decimal(str(resolved.tariff))):
                    calc.payload["min_amount_applied"] = str(rule.min_amount_day)
                amount = money_quantize(getattr(charge, "amount", amount) or amount)
                vat_amount = money_quantize(getattr(charge, "vat_amount", 0) or 0)
        elif not calc.skip_charge and quantity > 0:
            payload = dict(application.source_payload or {})
            billing_meta = dict(payload.get("billing") or {})
            billing_meta["missing_tariffs"] = [
                {
                    "service_code": service.code,
                    "service_name": service.name,
                    "quantity": str(quantity),
                    "unit": resolved.unit or calc.unit or service.unit,
                    "reason": resolved.reason or "agreed_tariff_missing",
                    "note": resolved.note,
                    "status": "requires_tariff_setup",
                }
            ]
            billing_meta["missing_tariff_count"] = 1
            payload["billing"] = billing_meta
            application.source_payload = payload
            application.save(update_fields=["source_payload", "updated_at"])
            status = BillingStorageDay.STATUS_NEEDS_REVIEW
            StorageBillingError.objects.update_or_create(
                client=client,
                day=day,
                error_type=StorageBillingError.TYPE_NO_TARIFF,
                resolved_at=None,
                defaults={
                    "severity": StorageBillingError.SEVERITY_ERROR,
                    "message": f"Нет тарифа на услугу {service.code}",
                    "payload": {"service_code": service.code, "quantity": str(quantity)},
                },
            )
        else:
            existing = ApplicationCharge.objects.filter(application=application, source_key=source_key).first()
            if existing and not existing.is_included_in_act and not existing.is_included_in_invoice:
                existing.delete()
                BillingWorkflowService.recalculate_application_totals(application)
            if calc.skip_reason == "no_dims":
                status = BillingStorageDay.STATUS_NEEDS_REVIEW

        if calc.errors:
            status = BillingStorageDay.STATUS_NEEDS_REVIEW if status == BillingStorageDay.STATUS_CALCULATED else status

        day_row, _ = BillingStorageDay.objects.update_or_create(
            client=client,
            day=day,
            defaults={
                "application": application,
                "pallet_count": calc.pallet_count,
                "box_count": source_box_count,
                "sku_unit_count": calc.sku_unit_count,
                "zone_counts": counts.get("by_zone") or {},
                "physical_volume_l": calc.physical_volume_l,
                "physical_volume_m3": calc.physical_volume_m3,
                "billable_volume_l": calc.billable_volume_l,
                "billable_volume_m3": calc.billable_volume_m3,
                "coefficient_applied": calc.coefficient_applied,
                "billing_mode": calc.billing_mode,
                "amount": amount,
                "vat_amount": vat_amount,
                "status": status,
                "tariff_version": resolved.tariff_version or tariff_version,
                "calculation_rule": rule if getattr(rule, "pk", None) else None,
                "payload": {
                    **calc.payload,
                    "skip_charge": calc.skip_charge,
                    "skip_reason": calc.skip_reason,
                    "free_day": calc.free_day,
                    "quantity": str(quantity),
                    "unit": calc.unit,
                    "service_code": service.code,
                    "container_snapshot_version": 1,
                    "pallet_box_counts": pallet_box_counts,
                    "source_box_count": source_box_count,
                },
                "charge": charge,
            },
        )
        StorageBillingService._write_lines(day_row, calc.items)
        if calc.errors:
            StorageBillingService._write_errors(client, day_row, day, calc.errors, user=user)

        BillingWorkflowService.audit(
            action="storage_day_recorded",
            application=application,
            user=user,
            obj=day_row,
            new_value={
                "day": day.isoformat(),
                "pallet_count": calc.pallet_count,
                "billing_mode": calc.billing_mode,
                "quantity": str(quantity),
                "billable_volume_l": str(calc.billable_volume_l),
                "amount": str(amount),
            },
        )
        return day_row

    @staticmethod
    def run_daily_for_all_clients(*, day: date | None = None, client_id: int | None = None, user=None) -> dict:
        day = day or timezone.localdate()
        if client_id:
            clients = list(Agency.objects.filter(pk=client_id))
        else:
            clients = agencies_with_storage_stock()
        recorded = 0
        skipped = 0
        total_pallets = 0
        for client in clients:
            row = StorageBillingService.record_storage_day(client, day=day, user=user)
            recorded += 1
            total_pallets += row.pallet_count
            if row.pallet_count == 0:
                skipped += 1
        return {
            "day": day.isoformat(),
            "clients": recorded,
            "zero_days": skipped,
            "total_pallets": total_pallets,
        }

    @staticmethod
    def recalculate_open_period(
        *,
        year: int,
        month: int,
        client_id: int | None = None,
        client_ids: list[int] | None = None,
        user=None,
    ) -> dict:
        """Пересчитать дни хранения за период, пропуская отправленные/оплаченные счета."""
        from django.db.models import Q

        from .statuses import InvoiceStatus

        if month < 1 or month > 12:
            raise ValidationError("Некорректный месяц.")
        start = date(year, month, 1)
        days_in_month = calendar.monthrange(year, month)[1]
        today = timezone.localdate()
        if year == today.year and month == today.month:
            days_in_month = min(days_in_month, today.day)
        clients_qs = Agency.objects.all()
        if client_id:
            clients_qs = clients_qs.filter(pk=client_id)
        elif client_ids is not None:
            clients_qs = clients_qs.filter(pk__in=client_ids)
        clients = list(clients_qs)
        locked_statuses = {InvoiceStatus.SENT, InvoiceStatus.PARTIALLY_PAID, InvoiceStatus.PAID}
        stats = {
            "year": year,
            "month": month,
            "clients": len(clients),
            "days": 0,
            "recalculated": 0,
            "skipped_sent_invoice": 0,
            "errors": 0,
        }
        for client in clients:
            for day_no in range(1, days_in_month + 1):
                day = start.replace(day=day_no)
                stats["days"] += 1
                existing = BillingStorageDay.objects.select_related("charge").filter(client=client, day=day).first()
                if existing and existing.charge_id:
                    locked_invoice = (
                        BillingWorkflowService._active_invoices_for_charge(existing.charge)
                        .filter(
                            Q(status__in=locked_statuses)
                            | Q(sent_at__isnull=False)
                            | Q(paid_amount__gt=0)
                        )
                        .exists()
                    )
                    if locked_invoice:
                        stats["skipped_sent_invoice"] += 1
                        continue
                try:
                    if existing:
                        result = StorageBillingService.reprice_saved_storage_day(existing, user=user)
                        if result == "repriced":
                            stats["recalculated"] += 1
                        elif result == "missing_tariff":
                            stats["errors"] += 1
                    elif day == today:
                        # Для текущего дня снимок можно создать впервые. Для
                        # прошлого периода нельзя подменять историю текущими
                        # остатками склада.
                        StorageBillingService.record_storage_day(client, day=day, user=user)
                        stats["recalculated"] += 1
                    else:
                        stats["errors"] += 1
                except Exception:
                    stats["errors"] += 1
        return stats
