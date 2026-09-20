"""Расчёт объёмов хранения (литры / м³) — Decimal, без записи в склад."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_HALF_UP
from typing import Any

from .models import StorageCalculationRule

VOLUME_Q = Decimal("0.000001")
LITER_PER_M3 = Decimal("1000")
PALLET_PLACE_VOLUME_L = Decimal("1600")
PALLET_PLACE_WEIGHT_KG = Decimal("450")
ZERO = Decimal("0")
SKU_BOX_RATIO_LOW = Decimal("0.5")
SKU_BOX_RATIO_HIGH = Decimal("2.0")


def quantize_volume(value) -> Decimal:
    return Decimal(str(value or "0")).quantize(VOLUME_Q, rounding=ROUND_HALF_UP)


def mm_to_liters(length_mm, width_mm, height_mm) -> Decimal:
    """Объём в литрах из мм: (мм³) / 1_000_000 = литры? 

    Спека: размеры в см → литры = L*W*H/1000.
    мм → см = /10, значит литры = (Lmm/10)*(Wmm/10)*(Hmm/10)/1000 = L*W*H / 1_000_000.
    """
    l = Decimal(str(length_mm or 0))
    w = Decimal(str(width_mm or 0))
    h = Decimal(str(height_mm or 0))
    if l <= 0 or w <= 0 or h <= 0:
        return ZERO
    return quantize_volume((l * w * h) / Decimal("1000000"))


def liters_to_m3(liters) -> Decimal:
    return quantize_volume(Decimal(str(liters or 0)) / LITER_PER_M3)


def m3_to_liters(m3) -> Decimal:
    return quantize_volume(Decimal(str(m3 or 0)) * LITER_PER_M3)


def weight_to_storage_liters(weight_kg) -> Decimal:
    """Весовой эквивалент объёма: 450 кг = 1,6 м³ (без округления палет)."""
    weight = Decimal(str(weight_kg or 0))
    if weight <= 0:
        return ZERO
    return quantize_volume(weight / PALLET_PLACE_WEIGHT_KG * PALLET_PLACE_VOLUME_L)


def calculate_billing_cbm(actual_cbm, weight_kg) -> dict[str, Decimal]:
    """Рассчитать тарифицируемый объём по агрегированным объёму и весу.

    Количество расчётных палет всегда дробное. Значение 450 кг используется
    как коэффициент пересчёта для любого веса, а не как пороговое условие.
    """
    actual = quantize_volume(actual_cbm)
    weight = quantize_volume(weight_kg)
    weight_based = liters_to_m3(weight_to_storage_liters(weight))
    return {
        "actual_cbm": actual,
        "weight_kg": weight,
        "weight_based_cbm": weight_based,
        "billing_cbm": quantize_volume(max(actual, weight_based)),
    }


def cm_box_example_ok() -> bool:
    """60×40×40 см = 96 л = 0.096 м³."""
    # 600×400×400 mm
    lit = mm_to_liters(600, 400, 400)
    return lit == Decimal("96.000000") and liters_to_m3(lit) == Decimal("0.096000")


def round_volume(value: Decimal, mode: str, *, unit_is_m3: bool = False) -> Decimal:
    v = quantize_volume(value)
    if mode in {"", StorageCalculationRule.ROUND_NONE, None}:
        return v
    if mode == StorageCalculationRule.ROUND_MATH:
        return v.quantize(Decimal("1") if not unit_is_m3 else Decimal("0.01"), rounding=ROUND_HALF_UP)
    if mode == StorageCalculationRule.ROUND_CEIL:
        step = Decimal("1") if not unit_is_m3 else Decimal("0.01")
        return (v / step).to_integral_value(rounding=ROUND_CEILING) * step
    steps = {
        StorageCalculationRule.ROUND_LITER: Decimal("1"),
        StorageCalculationRule.ROUND_10_L: Decimal("10"),
        StorageCalculationRule.ROUND_50_L: Decimal("50"),
        StorageCalculationRule.ROUND_100_L: Decimal("100"),
        StorageCalculationRule.ROUND_001_M3: Decimal("0.01"),
        StorageCalculationRule.ROUND_01_M3: Decimal("0.1"),
        StorageCalculationRule.ROUND_1_M3: Decimal("1"),
    }
    step = steps.get(mode)
    if not step:
        return v
    # Modes with _m3 operate on m3; liter modes on liters. Caller passes correct unit.
    return (v / step).to_integral_value(rounding=ROUND_CEILING) * step


@dataclass
class VolumeItem:
    sku_id: int | None
    sku_code: str
    name: str
    barcode: str
    box_code: str
    pallet_code: str
    zone_code: str
    cell_code: str
    quantity: Decimal
    length_mm: int | None
    width_mm: int | None
    height_mm: int | None
    unit_volume_l: Decimal
    total_volume_l: Decimal
    total_volume_m3: Decimal
    coefficient: Decimal
    billable_volume_l: Decimal
    billable_volume_m3: Decimal
    volume_level: str
    dims_source: str
    status: str  # ok | no_dims
    total_weight_kg: Decimal = ZERO
    weight_volume_l: Decimal = ZERO
    weight_volume_m3: Decimal = ZERO


def _row_dims(row: dict[str, Any]) -> tuple[int | None, int | None, int | None, str]:
    """Извлечь L/W/H мм из stock row + связанные SKU/container при наличии."""
    for prefix in ("", "sku_", "box_", "container_"):
        l = row.get(f"{prefix}length_mm") or row.get(f"{prefix}depth_mm")
        w = row.get(f"{prefix}width_mm")
        h = row.get(f"{prefix}height_mm")
        if l and w and h:
            src = "container" if "box" in prefix or "container" in prefix else ("sku" if "sku" in prefix else "row")
            return int(l), int(w), int(h), src
    # box_size like "60x40x40" cm
    box_size = str(row.get("box_size") or "").lower().replace("×", "x").replace("*", "x")
    if "x" in box_size:
        parts = [p.strip() for p in box_size.split("x") if p.strip()]
        if len(parts) >= 3:
            try:
                # assume cm if values look like cm (< 500)
                nums = [Decimal(p.replace(",", ".")) for p in parts[:3]]
                if max(nums) < 500:
                    return int(nums[0] * 10), int(nums[1] * 10), int(nums[2] * 10), "box_size_cm"
                return int(nums[0]), int(nums[1]), int(nums[2]), "box_size_mm"
            except Exception:
                pass
    return None, None, None, ""


def _decimal_from_row(row: dict[str, Any], *keys: str) -> Decimal:
    for key in keys:
        raw = row.get(key)
        if raw in {None, ""}:
            continue
        try:
            return Decimal(str(raw).replace(",", ".").replace("кг", "").replace("kg", "").strip())
        except Exception:
            continue
    return ZERO


def _row_weight_kg(row: dict[str, Any], qty: Decimal, *, container_prefix: str = "") -> Decimal:
    if container_prefix:
        grams = _decimal_from_row(row, f"{container_prefix}gross_weight_g")
        if grams > 0:
            return quantize_volume(grams / Decimal("1000"))
    unit_weight = _decimal_from_row(
        row,
        "weight_gross_kg",
        "sku_weight_gross_kg",
        "weight_kg",
        "sku_weight_kg",
        "weight_net_kg",
        "sku_weight_net_kg",
    )
    if unit_weight > 0 and qty > 0:
        return quantize_volume(unit_weight * qty)
    grams = _decimal_from_row(row, "gross_weight_g", "box_gross_weight_g", "pallet_gross_weight_g")
    if grams > 0:
        return quantize_volume(grams / Decimal("1000"))
    return ZERO


def _has_sku_dims(row: dict[str, Any]) -> bool:
    return bool(row.get("sku_length_mm") and row.get("sku_width_mm") and row.get("sku_height_mm"))


def _has_box_dims(row: dict[str, Any]) -> bool:
    return bool(row.get("box_length_mm") and row.get("box_width_mm") and row.get("box_height_mm"))


def _has_sku_weight(row: dict[str, Any]) -> bool:
    return bool(row.get("sku_weight_gross_kg") or row.get("sku_weight_kg") or row.get("sku_weight_net_kg"))


def _sku_reference_row(row: dict[str, Any]) -> dict[str, Any]:
    """Справочная строка: считаем объём товара по SKU, не меняя расчёт коробов."""
    out = dict(row)
    if _has_sku_dims(row):
        out["length_mm"] = row.get("sku_length_mm")
        out["width_mm"] = row.get("sku_width_mm")
        out["height_mm"] = row.get("sku_height_mm")
        for key in (
            "box_length_mm",
            "box_width_mm",
            "box_height_mm",
            "container_length_mm",
            "container_width_mm",
            "container_height_mm",
        ):
            out.pop(key, None)
    return out


def _row_sku_box_ratio(row: dict[str, Any]) -> Decimal:
    if not (_has_sku_dims(row) and _has_box_dims(row)):
        return ZERO
    sku_l = mm_to_liters(row.get("sku_length_mm"), row.get("sku_width_mm"), row.get("sku_height_mm"))
    box_l = mm_to_liters(row.get("box_length_mm"), row.get("box_width_mm"), row.get("box_height_mm"))
    if sku_l <= 0 or box_l <= 0:
        return ZERO
    return quantize_volume(box_l / sku_l)


def storage_sku_reference_summary(
    rows: list[dict[str, Any]],
    *,
    coefficient_default: Decimal = Decimal("1"),
) -> dict[str, Any]:
    """Read-only контроль: SKU-габариты против коробов. Не влияет на начисление."""
    sku_rows = [_sku_reference_row(row) for row in rows]
    items = collect_volume_items(
        sku_rows,
        volume_level=StorageCalculationRule.LEVEL_UNIT,
        coefficient_default=Decimal("1"),
        apply_weight_equivalent=False,
    )
    billing_cbm_rule = apply_billing_cbm_rule(items)
    summary = summarize_items(items)
    physical_m3 = summary["physical_volume_m3"]
    weight_m3 = summary["weight_volume_m3"]
    billable_m3 = billing_cbm_rule["billable_volume_m3"]
    ratios = [_row_sku_box_ratio(row) for row in rows if _row_sku_box_ratio(row) > 0]
    severe = [
        ratio
        for ratio in ratios
        if ratio < SKU_BOX_RATIO_LOW or ratio > SKU_BOX_RATIO_HIGH
    ]
    missing_sku_dims = sum(1 for row in rows if not _has_sku_dims(row))
    return {
        "sku_physical_volume_m3": quantize_volume(physical_m3),
        "sku_weight_kg": summary["total_weight_kg"],
        "sku_weight_volume_m3": quantize_volume(weight_m3),
        "sku_billable_volume_m3": quantize_volume(billable_m3),
        "sku_dims_rows": sum(1 for row in rows if _has_sku_dims(row)),
        "sku_weight_rows": sum(1 for row in rows if _has_sku_weight(row)),
        "box_dims_rows": sum(1 for row in rows if _has_box_dims(row)),
        "missing_sku_dims_rows": missing_sku_dims,
        "box_sku_ratio_rows": len(ratios),
        "box_sku_severe_mismatch_rows": len(severe),
        "box_sku_ratio_low": str(SKU_BOX_RATIO_LOW),
        "box_sku_ratio_high": str(SKU_BOX_RATIO_HIGH),
        "needs_attention": bool(severe or missing_sku_dims),
    }


def _billable_liters(
    total_l: Decimal,
    weight_kg: Decimal,
    coeff: Decimal,
    *,
    apply_weight_equivalent: bool = True,
) -> tuple[Decimal, Decimal]:
    weight_l = weight_to_storage_liters(weight_kg)
    base_l = max(quantize_volume(total_l), weight_l) if apply_weight_equivalent else quantize_volume(total_l)
    return quantize_volume(base_l * coeff), weight_l


def apply_pallet_weight_rule(items: list[VolumeItem]) -> dict[str, Any]:
    """Устаревший построчный helper, оставленный для совместимости импортов.

    Основной расчёт хранения его не вызывает: Billing CBM рассчитывается
    агрегатно через :func:`apply_billing_cbm_rule` для всего дневного снимка.
    """
    groups: dict[str, list[VolumeItem]] = {}
    for index, item in enumerate(items):
        if item.status != "ok":
            continue
        if item.pallet_code:
            key = f"pallet:{item.pallet_code}"
        elif item.box_code:
            key = f"box:{item.box_code}"
        else:
            key = f"item:{index}"
        groups.setdefault(key, []).append(item)

    heavy_groups = 0
    volume_groups = 0
    missing_volume_groups = 0
    group_details: list[dict[str, str]] = []
    for group_key, group_items in groups.items():
        group_weight_kg = quantize_volume(
            sum((item.total_weight_kg for item in group_items), ZERO)
        )
        group_physical_l = quantize_volume(
            sum((item.total_volume_l for item in group_items), ZERO)
        )
        use_weight = group_weight_kg > PALLET_PLACE_WEIGHT_KG
        if not use_weight and group_physical_l <= 0 and group_weight_kg > 0:
            # Без габаритов объём рассчитать невозможно. Сохраняем прежний
            # безопасный fallback по весу и отмечаем его в диагностике.
            use_weight = True
            missing_volume_groups += 1
        if use_weight:
            heavy_groups += 1
        else:
            volume_groups += 1

        group_billable_l = ZERO
        for item in group_items:
            base_l = item.weight_volume_l if use_weight else item.total_volume_l
            item.billable_volume_l = quantize_volume(base_l * item.coefficient)
            item.billable_volume_m3 = liters_to_m3(item.billable_volume_l)
            group_billable_l += item.billable_volume_l

        group_details.append(
            {
                "key": group_key,
                "weight_kg": str(group_weight_kg),
                "physical_volume_l": str(group_physical_l),
                "billable_volume_l": str(quantize_volume(group_billable_l)),
                "basis": "weight" if use_weight else "volume",
            }
        )

    summary = summarize_items(items)
    return {
        "billable_volume_l": summary["billable_volume_l"],
        "billable_volume_m3": summary["billable_volume_m3"],
        "pallet_groups": len(groups),
        "heavy_pallet_groups": heavy_groups,
        "volume_pallet_groups": volume_groups,
        "missing_volume_fallback_groups": missing_volume_groups,
        "group_details": group_details,
    }


def apply_billing_cbm_rule(items: list[VolumeItem]) -> dict[str, Any]:
    """Применить единую формулу Billing CBM ко всему дневному снимку.

    Billing_CBM = MAX(Actual_CBM, (Weight_KG / 450) * 1.6).
    Группировка по палетам и округление количества палет не используются.
    """
    summary = summarize_items(items)
    values = calculate_billing_cbm(
        summary["physical_volume_m3"],
        summary["total_weight_kg"],
    )
    return {
        **values,
        "billable_volume_m3": values["billing_cbm"],
        "billable_volume_l": m3_to_liters(values["billing_cbm"]),
    }


def collect_volume_items(
    rows: list[dict[str, Any]],
    *,
    volume_level: str,
    coefficient_default: Decimal = Decimal("1"),
    apply_weight_equivalent: bool = True,
) -> list[VolumeItem]:
    """Построить строки объёма без двойного учёта unit/box/pallet."""
    items: list[VolumeItem] = []
    seen_pallets: set[str] = set()
    seen_boxes: set[str] = set()

    for row in rows:
        qty = Decimal(str(row.get("qty") or 0))
        if qty <= 0:
            continue
        zone = str(row.get("zone") or row.get("zone_code") or "").strip().upper()
        pallet = str(row.get("pallet_code") or "").strip()
        box = str(row.get("box_code") or "").strip()
        sku_code = str(row.get("sku_code") or row.get("sku") or "").strip()
        name = str(row.get("sku_name") or row.get("name") or sku_code).strip()
        barcode = str(row.get("barcode") or "").strip()
        cell = str(row.get("cell") or row.get("location") or row.get("cell_code") or "").strip()
        sku_id = row.get("sku_id") or row.get("sku_ref_id")
        coeff = Decimal(str(row.get("storage_coefficient") or coefficient_default or 1))

        if volume_level == StorageCalculationRule.LEVEL_PALLET:
            if not pallet or pallet in seen_pallets:
                continue
            seen_pallets.add(pallet)
            l, w, h, src = _row_dims(row)
            # fallback standard euro pallet footprint 1200x800x1800 if no dims
            if not (l and w and h):
                l, w, h, src = 1200, 800, 1800, "default_pallet"
            unit_l = mm_to_liters(l, w, h)
            total_weight_kg = _row_weight_kg(row, Decimal("1"), container_prefix="pallet_")
            bill_l, weight_l = _billable_liters(
                unit_l,
                total_weight_kg,
                coeff,
                apply_weight_equivalent=apply_weight_equivalent,
            )
            items.append(
                VolumeItem(
                    sku_id=int(sku_id) if sku_id else None,
                    sku_code=sku_code,
                    name=name or f"Палета {pallet}",
                    barcode=barcode,
                    box_code="",
                    pallet_code=pallet,
                    zone_code=zone,
                    cell_code=cell,
                    quantity=Decimal("1"),
                    length_mm=l,
                    width_mm=w,
                    height_mm=h,
                    unit_volume_l=unit_l,
                    total_volume_l=unit_l,
                    total_volume_m3=liters_to_m3(unit_l),
                    total_weight_kg=total_weight_kg,
                    weight_volume_l=weight_l,
                    weight_volume_m3=liters_to_m3(weight_l),
                    coefficient=coeff,
                    billable_volume_l=bill_l,
                    billable_volume_m3=liters_to_m3(bill_l),
                    volume_level=volume_level,
                    dims_source=src,
                    status="ok",
                )
            )
            continue

        if volume_level == StorageCalculationRule.LEVEL_BOX:
            key = box or f"{pallet}:{sku_code}:{qty}"
            if box and box in seen_boxes:
                continue
            if box:
                seen_boxes.add(box)
            l, w, h, src = _row_dims(row)
            if not (l and w and h):
                total_weight_kg = _row_weight_kg(row, qty, container_prefix="box_")
                weight_l = weight_to_storage_liters(total_weight_kg)
                items.append(
                    VolumeItem(
                        sku_id=int(sku_id) if sku_id else None,
                        sku_code=sku_code,
                        name=name,
                        barcode=barcode,
                        box_code=box,
                        pallet_code=pallet,
                        zone_code=zone,
                        cell_code=cell,
                        quantity=qty,
                        length_mm=None,
                        width_mm=None,
                        height_mm=None,
                        unit_volume_l=ZERO,
                        total_volume_l=ZERO,
                        total_volume_m3=ZERO,
                        total_weight_kg=total_weight_kg,
                        weight_volume_l=weight_l,
                        weight_volume_m3=liters_to_m3(weight_l),
                        coefficient=coeff,
                        billable_volume_l=quantize_volume(weight_l * coeff),
                        billable_volume_m3=liters_to_m3(quantize_volume(weight_l * coeff)),
                        volume_level=volume_level,
                        dims_source="",
                        status="ok" if weight_l > 0 else "no_dims",
                    )
                )
                continue
            unit_l = mm_to_liters(l, w, h)
            # one box row counts as 1 box unless qty represents boxes
            box_qty = qty if not box else Decimal("1")
            total_l = quantize_volume(unit_l * box_qty)
            total_weight_kg = _row_weight_kg(row, box_qty, container_prefix="box_")
            bill_l, weight_l = _billable_liters(
                total_l,
                total_weight_kg,
                coeff,
                apply_weight_equivalent=apply_weight_equivalent,
            )
            items.append(
                VolumeItem(
                    sku_id=int(sku_id) if sku_id else None,
                    sku_code=sku_code,
                    name=name,
                    barcode=barcode,
                    box_code=box,
                    pallet_code=pallet,
                    zone_code=zone,
                    cell_code=cell,
                    quantity=box_qty,
                    length_mm=l,
                    width_mm=w,
                    height_mm=h,
                    unit_volume_l=unit_l,
                    total_volume_l=total_l,
                    total_volume_m3=liters_to_m3(total_l),
                    total_weight_kg=total_weight_kg,
                    weight_volume_l=weight_l,
                    weight_volume_m3=liters_to_m3(weight_l),
                    coefficient=coeff,
                    billable_volume_l=bill_l,
                    billable_volume_m3=liters_to_m3(bill_l),
                    volume_level=volume_level,
                    dims_source=src,
                    status="ok",
                )
            )
            continue

        # unit / place — по SKU qty
        l, w, h, src = _row_dims(row)
        if not (l and w and h):
            total_weight_kg = _row_weight_kg(row, qty)
            weight_l = weight_to_storage_liters(total_weight_kg)
            items.append(
                VolumeItem(
                    sku_id=int(sku_id) if sku_id else None,
                    sku_code=sku_code,
                    name=name,
                    barcode=barcode,
                    box_code=box,
                    pallet_code=pallet,
                    zone_code=zone,
                    cell_code=cell,
                    quantity=qty,
                    length_mm=None,
                    width_mm=None,
                    height_mm=None,
                    unit_volume_l=ZERO,
                    total_volume_l=ZERO,
                    total_volume_m3=ZERO,
                    total_weight_kg=total_weight_kg,
                    weight_volume_l=weight_l,
                    weight_volume_m3=liters_to_m3(weight_l),
                    coefficient=coeff,
                    billable_volume_l=quantize_volume(weight_l * coeff),
                    billable_volume_m3=liters_to_m3(quantize_volume(weight_l * coeff)),
                    volume_level=volume_level or StorageCalculationRule.LEVEL_UNIT,
                    dims_source="weight_only" if weight_l > 0 else "",
                    status="ok" if weight_l > 0 else "no_dims",
                )
            )
            continue
        unit_l = mm_to_liters(l, w, h)
        total_l = quantize_volume(unit_l * qty)
        total_weight_kg = _row_weight_kg(row, qty)
        bill_l, weight_l = _billable_liters(
            total_l,
            total_weight_kg,
            coeff,
            apply_weight_equivalent=apply_weight_equivalent,
        )
        items.append(
            VolumeItem(
                sku_id=int(sku_id) if sku_id else None,
                sku_code=sku_code,
                name=name,
                barcode=barcode,
                box_code=box,
                pallet_code=pallet,
                zone_code=zone,
                cell_code=cell,
                quantity=qty,
                length_mm=l,
                width_mm=w,
                height_mm=h,
                unit_volume_l=unit_l,
                total_volume_l=total_l,
                total_volume_m3=liters_to_m3(total_l),
                total_weight_kg=total_weight_kg,
                weight_volume_l=weight_l,
                weight_volume_m3=liters_to_m3(weight_l),
                coefficient=coeff,
                billable_volume_l=bill_l,
                billable_volume_m3=liters_to_m3(bill_l),
                volume_level=volume_level or StorageCalculationRule.LEVEL_UNIT,
                dims_source=src,
                status="ok",
            )
        )
    return items


def summarize_items(items: list[VolumeItem]) -> dict[str, Any]:
    phys_l = sum((i.total_volume_l for i in items), ZERO)
    bill_l = sum((i.billable_volume_l for i in items if i.status == "ok"), ZERO)
    weight_kg = sum((i.total_weight_kg for i in items if i.status == "ok"), ZERO)
    weight_l = sum((i.weight_volume_l for i in items if i.status == "ok"), ZERO)
    no_dims = sum(1 for i in items if i.status == "no_dims")
    pallets = {i.pallet_code for i in items if i.pallet_code}
    boxes = {i.box_code for i in items if i.box_code}
    return {
        "physical_volume_l": quantize_volume(phys_l),
        "physical_volume_m3": liters_to_m3(phys_l),
        "billable_volume_l": quantize_volume(bill_l),
        "billable_volume_m3": liters_to_m3(bill_l),
        "total_weight_kg": quantize_volume(weight_kg),
        "weight_volume_l": quantize_volume(weight_l),
        "weight_volume_m3": liters_to_m3(weight_l),
        "pallet_count": len(pallets),
        "box_count": len(boxes),
        "sku_unit_count": int(sum((i.quantity for i in items), ZERO)),
        "no_dims_count": no_dims,
    }
