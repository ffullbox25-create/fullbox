from __future__ import annotations

import re

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils.dateparse import parse_date, parse_datetime
from django.utils import timezone

from fullbox.order_numbers import format_container_order_number
from processing_app.models import ProcessingContainerCodeSequence
from sku.models import Agency


_GOODS_TYPE_CODE_ALIASES = {
    "op": "op",
    "оптовый": "op",
    "opt": "op",
    "gv": "gv",
    "готовый": "gv",
    "готовые": "gv",
    "br": "br",
    "брак": "br",
    "vz": "vz",
    "возврат": "vz",
    "votg": "votg",
    "возврат с отгрузки": "votg",
    "rh": "rh",
    "расходный": "rh",
    "расходные": "rh",
    "no": "no",
    "не обработанный": "no",
    "необработанный": "no",
}


def normalize_container_code_prefix(value) -> str:
    text = str(value or "").strip().upper()
    return "".join(ch for ch in text if ch.isalnum())


def normalize_container_goods_type(value) -> str:
    text = " ".join(str(value or "").strip().lower().split())
    if not text:
        return "na"
    if text in _GOODS_TYPE_CODE_ALIASES:
        return _GOODS_TYPE_CODE_ALIASES[text]
    normalized = re.sub(r"[^a-z0-9]+", "", text)
    return normalized or "na"


def normalize_container_kind(value) -> str:
    text = str(value or "").strip().lower()
    if text in {"pallet", "pallet_code", "pal", "plt", "палета", "паллета"}:
        return "pallet"
    return "box"


def _container_code_prefix_for_agency(agency: Agency) -> str:
    prefix = normalize_container_code_prefix(getattr(agency, "pref", ""))
    if prefix:
        return prefix
    agency_id = getattr(agency, "pk", None)
    if not agency_id:
        raise ValidationError("Нельзя создать код короба: клиент ещё не сохранён.")
    return f"CL{int(agency_id)}"


def ensure_container_code_available_for_agency(*, code: str, agency: Agency) -> None:
    """Reject a generated code already owned by another client.

    Historical and archived codes are included because a physical label can still
    be present on the warehouse floor after the corresponding row was archived.
    """

    normalized_code = str(code or "").strip()
    agency_id = getattr(agency, "pk", None)
    if not normalized_code or not agency_id:
        raise ValidationError("Нельзя проверить код короба: не указан код или клиент.")

    from sklad.models import WarehouseContainer, WarehouseStockSnapshot

    used_by_other_client = WarehouseContainer.objects.filter(
        container_code__iexact=normalized_code,
    ).exclude(agency_id=agency_id).exists()
    if not used_by_other_client:
        used_by_other_client = WarehouseStockSnapshot.objects.filter(
            Q(container_code__iexact=normalized_code)
            | Q(container__container_code__iexact=normalized_code)
            | Q(parent_container__container_code__iexact=normalized_code)
        ).exclude(agency_id=agency_id).exists()
    if used_by_other_client:
        raise ValidationError(
            f"Код {normalized_code} уже существует у другого клиента. "
            "Новый короб или паллета не созданы."
        )


def validate_scanned_box_identity(
    *,
    code: str,
    agency_id: int | None,
    pallet_code: str,
    barcodes,
) -> None:
    """Require client, pallet and product agreement for an old duplicate QR."""

    normalized_code = str(code or "").strip()
    if not normalized_code:
        raise ValidationError("Не указан QR короба.")

    from sklad.models import WarehouseContainer, WarehouseStockSnapshot

    active_matches = list(
        WarehouseContainer.objects.filter(
            container_type=WarehouseContainer.TYPE_BOX,
            status=WarehouseContainer.STATUS_ACTIVE,
            container_code__iexact=normalized_code,
        )
        .select_related("agency", "parent_container")
        .order_by("id")
    )
    if len({row.agency_id for row in active_matches}) <= 1:
        return

    try:
        expected_agency_id = int(agency_id or 0)
    except (TypeError, ValueError):
        expected_agency_id = 0
    expected_pallet = str(pallet_code or "").strip().casefold()
    expected_barcodes = {
        str(value or "").strip().casefold()
        for value in (barcodes or [])
        if str(value or "").strip()
    }
    if not expected_agency_id or not expected_pallet or not expected_barcodes:
        raise ValidationError(
            f"QR {normalized_code} используется у нескольких клиентов. "
            "В задании не хватает клиента, паллеты или ШК товара; продолжение запрещено."
        )

    client_matches = [row for row in active_matches if row.agency_id == expected_agency_id]
    if len(client_matches) != 1:
        raise ValidationError(
            f"QR {normalized_code} используется у нескольких клиентов и не принадлежит клиенту задания."
        )
    matched_box = client_matches[0]

    snapshots = WarehouseStockSnapshot.objects.filter(
        agency_id=expected_agency_id,
        is_archived=False,
        qty__gt=0,
    ).filter(
        Q(container_id=matched_box.id)
        | Q(container_code__iexact=normalized_code)
    )
    actual_pallets = {
        str(value or "").strip().casefold()
        for value in snapshots.exclude(parent_container__isnull=True).values_list(
            "parent_container__container_code", flat=True
        )
        if str(value or "").strip()
    }
    if matched_box.parent_container_id:
        actual_pallets.add(str(matched_box.parent_container.container_code or "").strip().casefold())
    if expected_pallet not in actual_pallets:
        raise ValidationError(
            f"QR {normalized_code} дублируется у другого клиента, но паллета не совпала. "
            "Проверьте клиента и физическую этикетку короба."
        )

    actual_barcodes = {
        str(value or "").strip().casefold()
        for value in snapshots.exclude(barcode="").values_list("barcode", flat=True)
        if str(value or "").strip()
    }
    if not actual_barcodes.intersection(expected_barcodes):
        raise ValidationError(
            f"QR {normalized_code} дублируется у другого клиента, но ШК товара не совпал. "
            "Проверьте короб и товар задания."
        )


def _issue_container_sequence_number(*, agency: Agency, kind: str = "box") -> int:
    sequence_field = "last_pallet_number" if normalize_container_kind(kind) == "pallet" else "last_box_number"
    with transaction.atomic():
        sequence, _ = ProcessingContainerCodeSequence.objects.select_for_update().get_or_create(
            agency=agency,
            defaults={"last_number": 0, "last_box_number": 0, "last_pallet_number": 0},
        )
        next_number = int(getattr(sequence, sequence_field, 0) or 0) + 1
        setattr(sequence, sequence_field, next_number)
        sequence.save(update_fields=[sequence_field, "updated_at"])
        return next_number


def _container_code_date_part(value) -> str:
    if value:
        text = str(value or "").strip()
        parsed_datetime = parse_datetime(text)
        if parsed_datetime is not None:
            if timezone.is_aware(parsed_datetime):
                return timezone.localtime(parsed_datetime).strftime("%d%m")
            return parsed_datetime.strftime("%d%m")
        parsed_date = parse_date(text)
        if parsed_date is not None:
            return parsed_date.strftime("%d%m")
    return timezone.localtime().strftime("%d%m")


def issue_container_code(*, agency: Agency, goods_type: str, received_at: str | None = None) -> dict:
    prefix = _container_code_prefix_for_agency(agency)
    date_part = _container_code_date_part(received_at)
    type_part = normalize_container_goods_type(goods_type)
    number = _issue_container_sequence_number(agency=agency, kind="box")
    digits = str(number).zfill(6)
    code = f"{prefix}-{date_part}-{digits}-{type_part}"
    ensure_container_code_available_for_agency(code=code, agency=agency)
    return {
        "code": code,
        "prefix": prefix,
        "date_part": date_part,
        "number": number,
        "digits": digits,
        "goods_type": type_part,
        "kind": "box",
    }


def issue_pallet_code(*, agency: Agency, goods_type: str, order_type: str | None, order_id: str | None) -> dict:
    prefix = _container_code_prefix_for_agency(agency)
    order_part = format_container_order_number(order_type, order_id)
    type_part = normalize_container_goods_type(goods_type)
    number = _issue_container_sequence_number(agency=agency, kind="pallet")
    digits = str(number).zfill(7)
    code = f"{prefix}-{order_part}-{digits}-{type_part}"
    ensure_container_code_available_for_agency(code=code, agency=agency)
    return {
        "code": code,
        "prefix": prefix,
        "order_part": order_part,
        "number": number,
        "digits": digits,
        "goods_type": type_part,
        "kind": "pallet",
    }


def rewrite_container_code_goods_type(code: str, goods_type: str) -> str:
    raw_code = str(code or "").strip()
    if not raw_code:
        return ""
    parts = raw_code.split("-")
    if len(parts) < 2:
        return raw_code
    parts[-1] = normalize_container_goods_type(goods_type)
    return "-".join(parts)
