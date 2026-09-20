"""Сериализация данных модуля хранения для API/UI."""
from __future__ import annotations

from decimal import Decimal

from .models import BillingStorageDay, StorageAdjustment, StorageBillingError, StorageBillingPeriod, StorageCalculationRule
from .storage_engine import ensure_rule_for_version


def _d(value) -> str:
    if value is None:
        return "0"
    if isinstance(value, Decimal):
        return format(value, "f")
    return str(value)


def storage_rule_dict(rule: StorageCalculationRule | None) -> dict | None:
    if rule is None:
        return None
    basis = rule.charge_basis or StorageCalculationRule.split_billing_mode(rule.billing_mode)[0]
    period = rule.charge_period or StorageCalculationRule.split_billing_mode(rule.billing_mode)[1]
    return {
        "id": rule.id,
        "tariff_version_id": rule.tariff_version_id,
        "billing_mode": rule.billing_mode,
        "billing_mode_label": rule.get_billing_mode_display(),
        "charge_basis": basis,
        "charge_basis_label": dict(StorageCalculationRule.CHARGE_BASIS_CHOICES).get(basis, basis),
        "charge_period": period,
        "charge_period_label": dict(StorageCalculationRule.CHARGE_PERIOD_CHOICES).get(period, period),
        "month_mode": rule.month_mode,
        "day_counting": rule.day_counting,
        "free_period_type": rule.free_period_type,
        "free_period_value": _d(rule.free_period_value),
        "volume_level": rule.volume_level,
        "space_coefficient_default": _d(rule.space_coefficient_default),
        "rounding_mode": rule.rounding_mode,
        "min_billable_volume": _d(rule.min_billable_volume),
        "min_amount_day": _d(rule.min_amount_day),
        "min_amount_month": _d(rule.min_amount_month),
        "missing_dims_policy": rule.missing_dims_policy,
        "snapshot_hour": rule.snapshot_hour,
        "timezone_name": rule.timezone_name,
        "accountant_comment": rule.accountant_comment or "",
    }


def storage_rule_choices() -> dict:
    return {
        "billing_mode": StorageCalculationRule.MODE_CHOICES,
        "charge_basis": StorageCalculationRule.CHARGE_BASIS_CHOICES,
        "charge_period": StorageCalculationRule.CHARGE_PERIOD_CHOICES,
        "month_mode": StorageCalculationRule.MONTH_MODE_CHOICES,
        "day_counting": StorageCalculationRule.DAY_COUNTING_CHOICES,
        "free_period_type": StorageCalculationRule.FREE_PERIOD_CHOICES,
        "volume_level": StorageCalculationRule.VOLUME_LEVEL_CHOICES,
        "rounding_mode": StorageCalculationRule.ROUNDING_CHOICES,
        "missing_dims_policy": StorageCalculationRule.MISSING_DIMS_CHOICES,
    }


RULE_PATCH_FIELDS = {
    "billing_mode",
    "charge_basis",
    "charge_period",
    "month_mode",
    "day_counting",
    "free_period_type",
    "free_period_value",
    "volume_level",
    "space_coefficient_default",
    "rounding_mode",
    "min_billable_volume",
    "min_amount_day",
    "min_amount_month",
    "missing_dims_policy",
    "snapshot_hour",
    "timezone_name",
    "accountant_comment",
}


def apply_rule_patch(rule: StorageCalculationRule, data: dict) -> StorageCalculationRule:
    for key in RULE_PATCH_FIELDS:
        if key not in data:
            continue
        val = data[key]
        if key in {
            "free_period_value",
            "space_coefficient_default",
            "min_billable_volume",
            "min_amount_day",
            "min_amount_month",
        }:
            setattr(rule, key, Decimal(str(val or "0")))
        elif key == "snapshot_hour":
            setattr(rule, key, int(val if val is not None else 23))
        else:
            setattr(rule, key, val if val is not None else "")
    # Приоритет: явные единица+период → billing_mode; иначе разбор billing_mode
    if "charge_basis" in data or "charge_period" in data:
        if not rule.charge_basis:
            rule.charge_basis = StorageCalculationRule.BASIS_PALLET
        if not rule.charge_period:
            rule.charge_period = StorageCalculationRule.PERIOD_DAY
        rule.sync_mode_from_basis_period()
    elif "billing_mode" in data:
        rule.sync_basis_period_from_mode()
    rule.save()
    return rule


def storage_day_dict(day: BillingStorageDay, *, with_lines: bool = False) -> dict:
    payload = day.payload or {}
    sku_check = payload.get("sku_check") if isinstance(payload.get("sku_check"), dict) else {}
    charge = day.charge
    charge_excluded = bool(getattr(charge, "is_excluded", False))
    data = {
        "id": day.id,
        "client_id": day.client_id,
        "day": day.day.isoformat(),
        "billing_mode": day.billing_mode,
        "pallet_count": day.pallet_count,
        "box_count": day.box_count,
        "sku_unit_count": day.sku_unit_count,
        "zone_counts": day.zone_counts or {},
        "physical_volume_l": _d(day.physical_volume_l),
        "physical_volume_m3": _d(day.physical_volume_m3),
        "billable_volume_l": _d(day.billable_volume_l),
        "billable_volume_m3": _d(day.billable_volume_m3),
        "coefficient_applied": _d(day.coefficient_applied),
        "amount": _d(day.amount),
        "vat_amount": _d(day.vat_amount),
        "status": day.status,
        "status_label": day.get_status_display(),
        "charge_id": day.charge_id,
        "tariff_version_id": day.tariff_version_id,
        "payload": payload,
        "storage_report": {
            "total_weight_kg": str(payload.get("total_weight_kg") or "0"),
            "weight_volume_m3": str(payload.get("weight_volume_m3") or "0"),
            "physical_volume_m3": _d(day.physical_volume_m3),
            "billable_volume_m3": _d(day.billable_volume_m3),
            "is_m3_mode": bool(day.billing_mode.startswith("m3_")),
            "is_pallet_mode": bool(day.billing_mode.startswith("pallet_") or day.billing_mode == "custom"),
            "pallet_count": day.pallet_count,
            "box_count": day.box_count,
            "sku_unit_count": day.sku_unit_count,
            "tariff": _d(getattr(charge, "tariff", None)),
            "unit": getattr(charge, "unit", "") or ("м³" if day.billing_mode.startswith("m3_") else "л"),
            "amount": _d(0 if charge_excluded else (getattr(charge, "amount", None) if charge else day.amount)),
            "vat_amount": _d(0 if charge_excluded else (getattr(charge, "vat_amount", None) if charge else day.vat_amount)),
            "total_amount": _d(0 if charge_excluded else (getattr(charge, "total_amount", None) if charge else None)),
            "charge_excluded": charge_excluded,
            "formula": payload.get("volume_formula") or "",
            "sku_check": sku_check,
        },
    }
    if with_lines:
        data["lines"] = [
            {
                "id": line.id,
                "sku_code": line.sku_code,
                "name": line.name,
                "barcode": line.barcode,
                "box_code": line.box_code,
                "pallet_code": line.pallet_code,
                "zone_code": line.zone_code,
                "cell_code": line.cell_code,
                "quantity": _d(line.quantity),
                "length_mm": line.length_mm,
                "width_mm": line.width_mm,
                "height_mm": line.height_mm,
                "unit_volume_l": _d(line.unit_volume_l),
                "total_volume_l": _d(line.total_volume_l),
                "total_volume_m3": _d(line.total_volume_m3),
                "billable_volume_l": _d(line.billable_volume_l),
                "billable_volume_m3": _d(line.billable_volume_m3),
                "coefficient": _d(line.coefficient),
                "volume_level": line.volume_level,
                "dims_source": line.dims_source,
                "status": line.status,
            }
            for line in day.lines.all()
        ]
    return data


def storage_period_dict(period: StorageBillingPeriod) -> dict:
    return {
        "id": period.id,
        "client_id": period.client_id,
        "year": period.year,
        "month": period.month,
        "status": period.status,
        "status_label": period.get_status_display(),
        "total_amount": _d(period.total_amount),
        "total_vat": _d(period.total_vat),
        "checklist": period.checklist or {},
        "closed_at": period.closed_at.isoformat() if period.closed_at else None,
    }


def storage_error_dict(err: StorageBillingError) -> dict:
    return {
        "id": err.id,
        "error_type": err.error_type,
        "error_type_label": err.get_error_type_display(),
        "severity": err.severity,
        "client_id": err.client_id,
        "day": err.day.isoformat() if err.day else None,
        "sku_code": err.sku_code,
        "pallet_code": err.pallet_code,
        "box_code": err.box_code,
        "message": err.message,
        "resolved_at": err.resolved_at.isoformat() if err.resolved_at else None,
        "created_at": err.created_at.isoformat() if err.created_at else None,
    }


def storage_adjustment_dict(adj: StorageAdjustment) -> dict:
    return {
        "id": adj.id,
        "client_id": adj.client_id,
        "period_id": adj.period_id,
        "delta_volume_l": _d(adj.delta_volume_l),
        "delta_amount": _d(adj.delta_amount),
        "reason": adj.reason,
        "reason_label": adj.get_reason_display(),
        "comment": adj.comment,
        "status": adj.status,
        "status_label": adj.get_status_display(),
        "created_at": adj.created_at.isoformat() if adj.created_at else None,
    }


def rule_for_tariff_version(version) -> StorageCalculationRule:
    return ensure_rule_for_version(version)
