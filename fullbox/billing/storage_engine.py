"""Движок расчёта хранения: правило + объём → quantity/amount для ApplicationCharge."""
from __future__ import annotations

import calendar
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from .models import StorageCalculationRule
from .storage_volume import (
    PALLET_PLACE_VOLUME_L,
    ZERO,
    VolumeItem,
    apply_billing_cbm_rule,
    collect_volume_items,
    liters_to_m3,
    quantize_volume,
    summarize_items,
    storage_sku_reference_summary,
)

MONEY_Q = Decimal("0.01")
WEEK_DAYS = Decimal("7")

MODE_SERVICE_CODES = {
    StorageCalculationRule.MODE_LITER_DAY: "storage_liter_day",
    StorageCalculationRule.MODE_LITER_WEEK: "storage_liter_week",
    StorageCalculationRule.MODE_LITER_MONTH: "storage_liter_month",
    StorageCalculationRule.MODE_M3_DAY: "storage_m3_day",
    StorageCalculationRule.MODE_M3_WEEK: "storage_m3_week",
    StorageCalculationRule.MODE_M3_MONTH: "storage_m3_month",
    StorageCalculationRule.MODE_PALLET_DAY: "storage_pallet_day",
    StorageCalculationRule.MODE_PALLET_WEEK: "storage_pallet_week",
    StorageCalculationRule.MODE_PALLET_MONTH: "storage_pallet_month",
    StorageCalculationRule.MODE_CUSTOM: "storage_pallet_day",
}


def service_code_for_mode(billing_mode: str) -> str:
    return MODE_SERVICE_CODES.get(billing_mode or "", "storage_pallet_day")


def default_storage_rule() -> StorageCalculationRule:
    """Несохранённое правило по умолчанию (обратная совместимость = палето-день)."""
    return StorageCalculationRule(
        billing_mode=StorageCalculationRule.MODE_PALLET_DAY,
        charge_basis=StorageCalculationRule.BASIS_PALLET,
        charge_period=StorageCalculationRule.PERIOD_DAY,
        month_mode=StorageCalculationRule.MONTH_CALENDAR_PRORATE,
        day_counting=StorageCalculationRule.DAY_INCLUDE_BOTH,
        free_period_type=StorageCalculationRule.FREE_NONE,
        free_period_value=ZERO,
        volume_level=StorageCalculationRule.LEVEL_PALLET,
        space_coefficient_default=Decimal("1.000000"),
        rounding_mode=StorageCalculationRule.ROUND_NONE,
        min_billable_volume=ZERO,
        min_amount_day=ZERO,
        min_amount_month=ZERO,
        missing_dims_policy=StorageCalculationRule.MISSING_SKIP,
        snapshot_hour=23,
        timezone_name="Europe/Moscow",
    )


def ensure_rule_for_version(tariff_version) -> StorageCalculationRule:
    if tariff_version is None:
        return default_storage_rule()
    rule = getattr(tariff_version, "storage_rule", None)
    if rule is not None:
        return rule
    from .models import StorageCalculationRule as RuleModel

    rule, _ = RuleModel.objects.get_or_create(
        tariff_version=tariff_version,
        defaults={
            "billing_mode": RuleModel.MODE_PALLET_DAY,
            "charge_basis": RuleModel.BASIS_PALLET,
            "charge_period": RuleModel.PERIOD_DAY,
            "volume_level": RuleModel.LEVEL_PALLET,
            "missing_dims_policy": RuleModel.MISSING_SKIP,
        },
    )
    return rule


@dataclass
class StorageDayCalculation:
    billing_mode: str
    volume_level: str
    quantity: Decimal
    unit: str
    physical_volume_l: Decimal = ZERO
    physical_volume_m3: Decimal = ZERO
    billable_volume_l: Decimal = ZERO
    billable_volume_m3: Decimal = ZERO
    pallet_count: int = 0
    box_count: int = 0
    sku_unit_count: int = 0
    coefficient_applied: Decimal = Decimal("1")
    items: list[VolumeItem] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)
    free_day: bool = False
    skip_charge: bool = False
    skip_reason: str = ""
    payload: dict[str, Any] = field(default_factory=dict)


def _days_in_month(day: date) -> int:
    return calendar.monthrange(day.year, day.month)[1]


def _is_m3_mode(mode: str, basis: str = "") -> bool:
    if basis == StorageCalculationRule.BASIS_M3:
        return True
    return mode in {
        StorageCalculationRule.MODE_M3_DAY,
        StorageCalculationRule.MODE_M3_WEEK,
        StorageCalculationRule.MODE_M3_MONTH,
    }


def _is_pallet_mode(mode: str, basis: str = "") -> bool:
    if basis == StorageCalculationRule.BASIS_PALLET:
        return True
    return mode in {
        StorageCalculationRule.MODE_PALLET_DAY,
        StorageCalculationRule.MODE_PALLET_WEEK,
        StorageCalculationRule.MODE_PALLET_MONTH,
        StorageCalculationRule.MODE_CUSTOM,
    }


def _prorate_quantity(quantity: Decimal, *, period: str, day: date, month_mode: str) -> Decimal:
    """Ежедневный job: цена в тарифе за период → доля на один день снимка."""
    if period == StorageCalculationRule.PERIOD_WEEK:
        return (quantity / WEEK_DAYS).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
    if period == StorageCalculationRule.PERIOD_MONTH:
        if month_mode == StorageCalculationRule.MONTH_FULL:
            # полная месячная цена каждый день не применяется — только prorate/avg/max
            pass
        dim = Decimal(_days_in_month(day))
        if dim > 0:
            return (quantity / dim).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
    return quantity


def apply_free_period(rule: StorageCalculationRule, *, receive_day: date | None, day: date) -> bool:
    """True если день бесплатный (не тарифицировать)."""
    if rule.free_period_type in {"", StorageCalculationRule.FREE_NONE, None}:
        return False
    if receive_day is None:
        return False
    if day < receive_day:
        return True
    value = Decimal(str(rule.free_period_value or 0))
    if value <= 0:
        return False
    delta_days = (day - receive_day).days
    if rule.free_period_type == StorageCalculationRule.FREE_HOURS:
        free_days = int((value / Decimal("24")).to_integral_value(rounding=ROUND_HALF_UP))
        return delta_days < max(free_days, 1 if value > 0 else 0)
    if rule.free_period_type in {
        StorageCalculationRule.FREE_CALENDAR_DAYS,
        StorageCalculationRule.FREE_WORK_DAYS,
    }:
        return delta_days < int(value)
    return False


def calculate_storage_day(
    rows: list[dict[str, Any]],
    rule: StorageCalculationRule | None,
    *,
    day: date,
    pallet_counts: dict[str, Any] | None = None,
    receive_day: date | None = None,
) -> StorageDayCalculation:
    rule = rule or default_storage_rule()
    mode = rule.billing_mode or StorageCalculationRule.MODE_PALLET_DAY
    # Расчёт опирается на billing_mode; charge_* — UI-поля, синхронизируются при сохранении правила
    basis, period = StorageCalculationRule.split_billing_mode(mode)
    composed = StorageCalculationRule.compose_billing_mode(
        getattr(rule, "charge_basis", None) or basis,
        getattr(rule, "charge_period", None) or period,
    )
    if (
        getattr(rule, "charge_basis", None)
        and getattr(rule, "charge_period", None)
        and composed == mode
    ):
        basis = rule.charge_basis
        period = rule.charge_period
    level = rule.volume_level or StorageCalculationRule.LEVEL_PALLET
    coeff_default = Decimal(str(rule.space_coefficient_default or 1))

    pallet_counts = pallet_counts or {"total": 0, "by_zone": {}}
    pallet_total = int(pallet_counts.get("total") or 0)

    free_day = apply_free_period(rule, receive_day=receive_day, day=day)

    if _is_pallet_mode(mode, basis):
        items = collect_volume_items(
            rows,
            volume_level=StorageCalculationRule.LEVEL_PALLET,
            coefficient_default=coeff_default,
            apply_weight_equivalent=False,
        )
        summary = summarize_items(items)
        quantity = Decimal(pallet_total or summary["pallet_count"])
        quantity = _prorate_quantity(quantity, period=period, day=day, month_mode=rule.month_mode)
        unit = "пал."
        calc = StorageDayCalculation(
            billing_mode=mode,
            volume_level=StorageCalculationRule.LEVEL_PALLET,
            quantity=quantity,
            unit=unit,
            physical_volume_l=ZERO,
            physical_volume_m3=ZERO,
            billable_volume_l=ZERO,
            billable_volume_m3=ZERO,
            pallet_count=pallet_total or summary["pallet_count"],
            box_count=summary["box_count"],
            sku_unit_count=summary["sku_unit_count"],
            coefficient_applied=coeff_default,
            items=items,
            free_day=free_day,
            payload={
                "zone_counts": pallet_counts.get("by_zone") or {},
                "charge_basis": basis,
                "charge_period": period,
                "month_mode": rule.month_mode,
                "days_in_month": _days_in_month(day),
                "week_days": int(WEEK_DAYS),
            },
        )
        if free_day:
            calc.skip_charge = True
            calc.skip_reason = "free_period"
            calc.quantity = ZERO
        elif quantity <= 0:
            calc.skip_charge = True
            calc.skip_reason = "zero_quantity"
        return calc

    # Объёмные режимы (литр / м³)
    items = collect_volume_items(
        rows,
        volume_level=level,
        coefficient_default=Decimal("1"),
        apply_weight_equivalent=False,
    )
    billing_cbm_rule = apply_billing_cbm_rule(items)
    summary = summarize_items(items)
    errors: list[dict[str, Any]] = []
    for item in items:
        if item.status == "no_dims":
            errors.append(
                {
                    "type": "no_dims",
                    "sku_code": item.sku_code,
                    "box_code": item.box_code,
                    "pallet_code": item.pallet_code,
                    "message": f"Нет габаритов: {item.sku_code or item.box_code or item.pallet_code}",
                }
            )

    use_m3 = _is_m3_mode(mode, basis)
    physical_l = summary["physical_volume_l"]
    weight_l = summary["weight_volume_l"]
    sku_check = storage_sku_reference_summary(rows, coefficient_default=Decimal("1"))
    aggregate_billable_l = billing_cbm_rule["billable_volume_l"]
    aggregate_billable_m3 = billing_cbm_rule["billable_volume_m3"]
    billable = aggregate_billable_m3 if use_m3 else aggregate_billable_l
    min_vol = Decimal(str(rule.min_billable_volume or 0))

    if errors and rule.missing_dims_policy == StorageCalculationRule.MISSING_FALLBACK:
        if billable <= 0 and pallet_total > 0:
            fallback_l = Decimal(pallet_total) * PALLET_PLACE_VOLUME_L
            billable = liters_to_m3(fallback_l) if use_m3 else fallback_l
            errors.append({"type": "no_dims", "message": "Применён fallback по палетам", "severity": True})

    # Для отображения и повторного расчёта сохраняем Billing CBM до деления
    # на дни. Настройки округления объёма и минимального объёма здесь намеренно
    # не применяются: формула должна оставаться дробной и точной.
    if use_m3:
        aggregate_billable_m3 = quantize_volume(billable)
        aggregate_billable_l = quantize_volume(aggregate_billable_m3 * Decimal("1000"))
    else:
        aggregate_billable_l = quantize_volume(billable)
        aggregate_billable_m3 = liters_to_m3(aggregate_billable_l)

    quantity = quantize_volume(billable)
    quantity = _prorate_quantity(quantity, period=period, day=day, month_mode=rule.month_mode)

    unit = "м³" if use_m3 else "л"
    calc = StorageDayCalculation(
        billing_mode=mode,
        volume_level=level,
        quantity=quantity,
        unit=unit,
        physical_volume_l=physical_l,
        physical_volume_m3=summary["physical_volume_m3"],
        billable_volume_l=aggregate_billable_l,
        billable_volume_m3=aggregate_billable_m3,
        pallet_count=pallet_total or summary["pallet_count"],
        box_count=summary["box_count"],
        sku_unit_count=summary["sku_unit_count"],
        coefficient_applied=Decimal("1"),
        items=items,
        errors=errors,
        free_day=free_day,
        payload={
            "zone_counts": pallet_counts.get("by_zone") or {},
            "charge_basis": basis,
            "charge_period": period,
            "month_mode": rule.month_mode,
            "days_in_month": _days_in_month(day),
            "week_days": int(WEEK_DAYS),
            "rounding_mode": rule.rounding_mode,
            "min_billable_volume": str(min_vol),
            "rounding_mode_applied": False,
            "min_billable_volume_applied": False,
            "configured_space_coefficient": str(coeff_default),
            "space_coefficient_applied": "1",
            "no_dims_count": summary["no_dims_count"],
            "total_weight_kg": str(summary.get("total_weight_kg") or ZERO),
            "weight_volume_l": str(summary.get("weight_volume_l") or ZERO),
            "weight_volume_m3": str(summary.get("weight_volume_m3") or ZERO),
            "weight_norm_kg_per_pallet_place": "450",
            "pallet_place_volume_m3": "1.6",
            "volume_formula": "MAX(Actual_CBM, (Weight_KG / 450) * 1.6)",
            "billing_cbm_rule": {
                "version": 1,
                "weight_norm_kg": "450",
                "weight_equivalent_m3": "1.6",
                "actual_cbm": str(billing_cbm_rule["actual_cbm"]),
                "weight_kg": str(billing_cbm_rule["weight_kg"]),
                "weight_based_cbm": str(billing_cbm_rule["weight_based_cbm"]),
                "billing_cbm": str(billing_cbm_rule["billing_cbm"]),
                "fractional_pallets": True,
                "round_up": False,
            },
            "sku_check": {key: str(value) for key, value in sku_check.items()},
        },
    )

    if free_day:
        calc.skip_charge = True
        calc.skip_reason = "free_period"
        calc.quantity = ZERO
    elif quantity <= 0:
        calc.skip_charge = True
        calc.skip_reason = "zero_quantity"
        if errors and rule.missing_dims_policy == StorageCalculationRule.MISSING_ERROR:
            calc.skip_reason = "no_dims"
    elif errors and rule.missing_dims_policy == StorageCalculationRule.MISSING_ERROR and aggregate_billable_l <= 0:
        calc.skip_charge = True
        calc.skip_reason = "no_dims"

    if use_m3:
        calc.billable_volume_m3 = (
            aggregate_billable_m3
            if period != StorageCalculationRule.PERIOD_DAY
            else quantity
        )
        calc.billable_volume_l = quantize_volume(calc.billable_volume_m3 * Decimal("1000"))
    else:
        calc.billable_volume_l = (
            aggregate_billable_l
            if period != StorageCalculationRule.PERIOD_DAY
            else quantity
        )
        calc.billable_volume_m3 = liters_to_m3(calc.billable_volume_l)

    return calc


def money_quantize(value) -> Decimal:
    return Decimal(str(value or 0)).quantize(MONEY_Q, rounding=ROUND_HALF_UP)


def apply_min_amount(amount: Decimal, rule: StorageCalculationRule, *, is_month: bool = False) -> Decimal:
    floor = Decimal(str(rule.min_amount_month if is_month else rule.min_amount_day or 0))
    amount = money_quantize(amount)
    if floor > 0 and amount > 0 and amount < floor:
        return money_quantize(floor)
    return amount
