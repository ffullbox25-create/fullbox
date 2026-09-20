"""Read-only storage breakdown for a manager billing invoice."""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal, ROUND_HALF_UP
from urllib.parse import urlencode

from django.core.paginator import Paginator
from django.db.models import Q

from .models import (
    ApplicationCharge,
    BillingActLine,
    BillingStorageDay,
    ClientInvoiceAct,
    StorageSnapshotLine,
)


ZERO = Decimal("0")


def _decimal_text(value, places: str) -> str:
    number = Decimal(value or 0).quantize(Decimal(places), rounding=ROUND_HALF_UP)
    return format(number, "f")


def _historical_pallet_box_counts(storage_day) -> dict[str, int] | None:
    payload = storage_day.payload if isinstance(storage_day.payload, dict) else {}
    raw_counts = payload.get("pallet_box_counts")
    if not isinstance(raw_counts, dict):
        return None
    counts: dict[str, int] = {}
    for raw_code, raw_count in raw_counts.items():
        code = str(raw_code or "").strip()
        if not code:
            continue
        try:
            counts[code] = max(int(raw_count or 0), 0)
        except (TypeError, ValueError):
            continue
    return counts


def _payload_decimal(storage_day, key: str) -> Decimal:
    payload = storage_day.payload if isinstance(storage_day.payload, dict) else {}
    try:
        return Decimal(str(payload.get(key) or 0))
    except (TypeError, ValueError):
        return ZERO


def _payload_nested_decimal(storage_day, group: str, key: str) -> Decimal:
    payload = storage_day.payload if isinstance(storage_day.payload, dict) else {}
    nested = payload.get(group)
    if not isinstance(nested, dict):
        return ZERO
    try:
        return Decimal(str(nested.get(key) or 0))
    except (TypeError, ValueError):
        return ZERO


def _payload_nested_int(storage_day, group: str, key: str) -> int:
    payload = storage_day.payload if isinstance(storage_day.payload, dict) else {}
    nested = payload.get(group)
    if not isinstance(nested, dict):
        return 0
    try:
        return int(Decimal(str(nested.get(key) or 0)))
    except (TypeError, ValueError):
        return 0


def _is_m3_mode(mode: str) -> bool:
    return (mode or "").startswith("m3_")


def _volume_needs_attention(physical: Decimal, billable: Decimal) -> bool:
    return physical > 0 and billable > physical * Decimal("5")


def storage_report_invoice_ids(invoices) -> set[int]:
    """Return invoice ids containing storage charges linked to storage days."""
    invoice_list = list(invoices)
    if not invoice_list:
        return set()

    invoice_by_id = {invoice.id: invoice for invoice in invoice_list}
    linked_act_ids: dict[int, set[int]] = defaultdict(set)
    for invoice_id, act_id in ClientInvoiceAct.objects.filter(
        invoice_id__in=invoice_by_id,
    ).values_list("invoice_id", "act_id"):
        linked_act_ids[invoice_id].add(act_id)
    for invoice in invoice_list:
        if not linked_act_ids[invoice.id] and invoice.act_id:
            linked_act_ids[invoice.id].add(invoice.act_id)

    all_act_ids = {act_id for act_ids in linked_act_ids.values() for act_id in act_ids}
    if not all_act_ids:
        return set()
    storage_act_ids = set(
        BillingActLine.objects.filter(
            act_id__in=all_act_ids,
            charge__source_type=ApplicationCharge.SOURCE_STORAGE_DAY,
            charge__storage_days__isnull=False,
        )
        .values_list("act_id", flat=True)
        .distinct()
    )
    return {
        invoice_id
        for invoice_id, act_ids in linked_act_ids.items()
        if act_ids.intersection(storage_act_ids)
    }


def _report_url(invoice, *, day="", search="", page=None) -> str:
    params = {"storage_report": "1"}
    if day:
        params["day"] = day
    if search:
        params["q"] = search
    if page:
        params["page"] = page
    return f"/team-manager/billing/invoices/{invoice.id}/?{urlencode(params)}"


def build_storage_invoice_report(
    invoice,
    *,
    selected_day_value: str = "",
    search: str = "",
    page_number=1,
) -> dict | None:
    """Build report only from storage charge lines included in the invoice acts."""
    act_ids = list(invoice.get_linked_acts().values_list("id", flat=True))
    billed_lines = list(
        BillingActLine.objects.filter(
            act_id__in=act_ids,
            charge__source_type=ApplicationCharge.SOURCE_STORAGE_DAY,
            charge__storage_days__client=invoice.client,
        )
        .select_related("charge", "charge__service")
        .order_by("id")
        .distinct()
    )
    charge_ids = {line.charge_id for line in billed_lines}
    storage_days = list(
        BillingStorageDay.objects.filter(client=invoice.client, charge_id__in=charge_ids)
        .select_related("charge")
        .order_by("day", "id")
    )
    if not storage_days:
        return None

    lines_by_charge: dict[int, list[BillingActLine]] = defaultdict(list)
    for line in billed_lines:
        lines_by_charge[line.charge_id].append(line)

    day_rows = []
    billed_quantities: list[Decimal] = []
    for storage_day in storage_days:
        invoice_lines = lines_by_charge.get(storage_day.charge_id, [])
        billed_quantity = sum((line.quantity for line in invoice_lines), ZERO)
        billed_quantities.append(billed_quantity)
        billed_amount = sum((line.total_amount for line in invoice_lines), ZERO)
        tariff = invoice_lines[0].tariff if invoice_lines else ZERO
        unit = invoice_lines[0].unit if invoice_lines else ""
        zone_counts = storage_day.zone_counts if isinstance(storage_day.zone_counts, dict) else {}
        zone_summary = ", ".join(f"{key}: {value}" for key, value in sorted(zone_counts.items())) or "—"
        pallet_box_counts = _historical_pallet_box_counts(storage_day)
        physical_m3 = Decimal(storage_day.physical_volume_m3 or 0)
        billable_m3 = Decimal(storage_day.billable_volume_m3 or 0)
        total_weight_kg = _payload_decimal(storage_day, "total_weight_kg")
        weight_m3 = _payload_decimal(storage_day, "weight_volume_m3")
        sku_physical_m3 = _payload_nested_decimal(storage_day, "sku_check", "sku_physical_volume_m3")
        sku_billable_m3 = _payload_nested_decimal(storage_day, "sku_check", "sku_billable_volume_m3")
        sku_mismatch_rows = _payload_nested_int(storage_day, "sku_check", "box_sku_severe_mismatch_rows")
        sku_missing_dims_rows = _payload_nested_int(storage_day, "sku_check", "missing_sku_dims_rows")
        is_m3_day = _is_m3_mode(storage_day.billing_mode)
        day_rows.append(
            {
                "value": storage_day.day.isoformat(),
                "date": storage_day.day.strftime("%d.%m.%Y"),
                "pallet_count": storage_day.pallet_count,
                "box_count": storage_day.box_count,
                "box_count_available": (
                    pallet_box_counts is not None
                    or is_m3_day
                    or storage_day.box_count > 0
                ),
                "sku_unit_count": storage_day.sku_unit_count,
                "zone_summary": zone_summary,
                "physical_m3": _decimal_text(physical_m3, "0.001"),
                "total_weight_kg": _decimal_text(total_weight_kg, "0.001"),
                "weight_m3": _decimal_text(weight_m3, "0.001"),
                "billable_m3": _decimal_text(billable_m3, "0.001"),
                "sku_physical_m3": _decimal_text(sku_physical_m3, "0.001"),
                "sku_billable_m3": _decimal_text(sku_billable_m3, "0.001"),
                "sku_mismatch_rows": sku_mismatch_rows,
                "sku_missing_dims_rows": sku_missing_dims_rows,
                "volume_needs_attention": _volume_needs_attention(physical_m3, billable_m3),
                "no_dims_count": int(_payload_decimal(storage_day, "no_dims_count")),
                "status": storage_day.status,
                "status_label": storage_day.get_status_display(),
                "billed_quantity": _decimal_text(billed_quantity, "0.001"),
                "unit": unit,
                "tariff": _decimal_text(tariff, "0.0001"),
                "amount": _decimal_text(billed_amount, "0.01"),
                "open_url": _report_url(invoice, day=storage_day.day.isoformat()),
            }
        )

    selected_day = None
    if selected_day_value:
        selected_day = next(
            (item for item in storage_days if item.day.isoformat() == selected_day_value),
            None,
        )
    if selected_day is None:
        selected_day = storage_days[-1]

    search = (search or "").strip()[:200]
    details = selected_day.lines.all().order_by("pallet_code", "box_code", "sku_code", "id")
    if search:
        details = details.filter(
            Q(pallet_code__icontains=search)
            | Q(box_code__icontains=search)
            | Q(sku_code__icontains=search)
            | Q(barcode__icontains=search)
            | Q(name__icontains=search)
            | Q(zone_code__icontains=search)
            | Q(cell_code__icontains=search)
        )
    page_obj = Paginator(details, 200).get_page(page_number)
    selected_pallet_box_counts = _historical_pallet_box_counts(selected_day)
    for line in page_obj.object_list:
        pallet_code = str(line.pallet_code or "").strip()
        value = None
        if selected_pallet_box_counts is not None and pallet_code in selected_pallet_box_counts:
            value = selected_pallet_box_counts[pallet_code]
        line.pallet_box_count = value
        line.pallet_box_count_available = value is not None
        dimensions = [line.length_mm, line.width_mm, line.height_mm]
        line.dimensions_text = (
            " × ".join(str(value) for value in dimensions) + " мм"
            if all(dimensions)
            else "Нет данных"
        )
        line.physical_volume_text = _decimal_text(line.total_volume_m3, "0.000001")
        line.calculated_volume_text = _decimal_text(line.billable_volume_m3, "0.000001")
        line.coefficient_text = _decimal_text(line.coefficient, "0.001")
        line.volume_status_label = "Нет габаритов" if line.status == "no_dims" else "Рассчитано"

    pallet_days = sum(day.pallet_count for day in storage_days)
    days_count = len(storage_days)
    storage_total = sum((line.total_amount for line in billed_lines), ZERO)
    unique_pallets = (
        StorageSnapshotLine.objects.filter(day_id__in=[day.id for day in storage_days])
        .exclude(pallet_code="")
        .values("pallet_code")
        .distinct()
        .count()
    )
    selected_date_value = selected_day.day.isoformat()
    is_m3_report = bool(storage_days) and all(_is_m3_mode(day.billing_mode) for day in storage_days)
    billed_quantity_total = sum(billed_quantities, ZERO)
    selected_physical_m3 = Decimal(selected_day.physical_volume_m3 or 0)
    selected_billable_m3 = Decimal(selected_day.billable_volume_m3 or 0)
    selected_total_weight_kg = _payload_decimal(selected_day, "total_weight_kg")
    selected_weight_m3 = _payload_decimal(selected_day, "weight_volume_m3")
    selected_sku_physical_m3 = _payload_nested_decimal(selected_day, "sku_check", "sku_physical_volume_m3")
    selected_sku_billable_m3 = _payload_nested_decimal(selected_day, "sku_check", "sku_billable_volume_m3")
    selected_sku_mismatch_rows = _payload_nested_int(selected_day, "sku_check", "box_sku_severe_mismatch_rows")
    selected_sku_missing_dims_rows = _payload_nested_int(selected_day, "sku_check", "missing_sku_dims_rows")
    attention_days = [row for row in day_rows if row["volume_needs_attention"]]
    return {
        "report_title": (
            "Отчёт по хранению в кубических метрах"
            if is_m3_report
            else "Отчёт по выставленной услуге хранения"
        ),
        "is_m3_report": is_m3_report,
        "day_rows": day_rows,
        "days_count": days_count,
        "period_start": storage_days[0].day.strftime("%d.%m.%Y"),
        "period_end": storage_days[-1].day.strftime("%d.%m.%Y"),
        "pallet_days": pallet_days,
        "average_pallets": _decimal_text(Decimal(pallet_days) / Decimal(days_count), "0.01"),
        "pallet_weeks": _decimal_text(Decimal(pallet_days) / Decimal("7"), "0.001"),
        "min_pallets": min(day.pallet_count for day in storage_days),
        "max_pallets": max(day.pallet_count for day in storage_days),
        "unique_pallets": unique_pallets,
        "storage_total": _decimal_text(storage_total, "0.01"),
        "billed_quantity_total": _decimal_text(billed_quantity_total, "0.001"),
        "billed_quantity_average": _decimal_text(
            billed_quantity_total / Decimal(days_count),
            "0.001",
        ),
        "billed_quantity_min": _decimal_text(min(billed_quantities), "0.001"),
        "billed_quantity_max": _decimal_text(max(billed_quantities), "0.001"),
        "selected_physical_m3": _decimal_text(selected_physical_m3, "0.001"),
        "selected_total_weight_kg": _decimal_text(selected_total_weight_kg, "0.001"),
        "selected_weight_m3": _decimal_text(selected_weight_m3, "0.001"),
        "selected_billable_m3": _decimal_text(selected_billable_m3, "0.001"),
        "selected_sku_physical_m3": _decimal_text(selected_sku_physical_m3, "0.001"),
        "selected_sku_billable_m3": _decimal_text(selected_sku_billable_m3, "0.001"),
        "selected_sku_mismatch_rows": selected_sku_mismatch_rows,
        "selected_sku_missing_dims_rows": selected_sku_missing_dims_rows,
        "attention_days": attention_days,
        "selected_day": selected_day,
        "selected_date": selected_day.day.strftime("%d.%m.%Y"),
        "page_obj": page_obj,
        "pallet_box_counts_available": selected_pallet_box_counts is not None,
        "search": search,
        "clear_url": _report_url(invoice, day=selected_date_value),
        "page_url_prefix": _report_url(invoice, day=selected_date_value, search=search) + "&page=",
    }
