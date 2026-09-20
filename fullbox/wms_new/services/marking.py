from __future__ import annotations

from django.db import IntegrityError, transaction
from django.utils import timezone

from ..models import WmsNewEvent, WmsNewMarkingCode, WmsNewProduct


class MarkingOperationError(ValueError):
    pass


def parse_codes(value: str) -> list[str]:
    rows = str(value or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    result = []
    seen = set()
    for row in rows:
        code = row.strip(" \t")
        if not code or code in seen:
            continue
        seen.add(code)
        result.append(code)
    return result


def _snapshot(item: WmsNewMarkingCode) -> dict:
    return {
        "agency_id": item.agency_id,
        "product_id": item.product_id,
        "code_type": item.code_type,
        "source": item.source,
        "received_reference": item.received_reference,
        "retired_reference": item.retired_reference,
        "received_at": item.received_at.isoformat() if item.received_at else None,
        "retired_at": item.retired_at.isoformat() if item.retired_at else None,
        "processed_at": item.processed_at.isoformat() if item.processed_at else None,
        "printed_at": item.printed_at.isoformat() if item.printed_at else None,
        "print_count": item.print_count,
        "file_name": item.file_name,
        "is_returned": item.is_returned,
        "pilot_revision": item.pilot_revision,
    }


def _event(*, item: WmsNewMarkingCode, action: str, actor, before: dict) -> None:
    WmsNewEvent.objects.create(
        entity_type="marking_code",
        entity_id=item.id,
        action=action,
        actor=actor,
        before=before,
        after=_snapshot(item),
    )


def add_codes(
    *,
    agency,
    product: WmsNewProduct,
    codes: str | list[str],
    code_type: str = WmsNewMarkingCode.TYPE_UNIT,
    file_name: str = "",
    actor=None,
) -> list[WmsNewMarkingCode]:
    if product.agency_id != agency.id or product.is_archived:
        raise MarkingOperationError("Товар не относится к выбранному партнеру.")
    if code_type not in dict(WmsNewMarkingCode.TYPE_CHOICES):
        raise MarkingOperationError("Неизвестный тип кода маркировки.")
    parsed = parse_codes("\n".join(codes) if isinstance(codes, list) else codes)
    if not parsed:
        raise MarkingOperationError("Добавьте хотя бы один код маркировки.")
    if len(parsed) > 5000:
        raise MarkingOperationError("За одну операцию можно добавить не более 5 000 кодов.")
    existing = set(
        WmsNewMarkingCode.objects.filter(code__in=parsed).values_list("code", flat=True)
    )
    if existing:
        raise MarkingOperationError(
            f"Код уже существует в WMS NEW: {next(iter(existing))[:80]}"
        )

    now = timezone.now()
    created = []
    try:
        with transaction.atomic():
            for code in parsed:
                item = WmsNewMarkingCode.objects.create(
                    agency=agency,
                    product=product,
                    product_name=product.name,
                    article=product.article,
                    barcode=product.barcode,
                    code_type=code_type,
                    code=code,
                    source=WmsNewMarkingCode.SOURCE_MANUAL,
                    received_reference="WMS-NEW",
                    received_at=now,
                    file_name=str(file_name or "").strip(),
                    pilot_revision=1,
                    created_by=actor,
                )
                _event(item=item, action="add", actor=actor, before={})
                created.append(item)
    except IntegrityError as exc:
        raise MarkingOperationError("Один из кодов уже существует в WMS NEW.") from exc
    return created


def mark_printed(*, code_ids: list[int], actor=None) -> list[WmsNewMarkingCode]:
    unique_ids = list(dict.fromkeys(int(value) for value in code_ids))
    if not unique_ids:
        raise MarkingOperationError("Выберите коды для печати.")
    if len(unique_ids) > 500:
        raise MarkingOperationError("За одну операцию можно распечатать не более 500 кодов.")
    now = timezone.now()
    with transaction.atomic():
        items = list(
            WmsNewMarkingCode.objects.select_for_update()
            .select_related("agency", "product")
            .filter(pk__in=unique_ids)
            .order_by("id")
        )
        if len(items) != len(unique_ids):
            raise MarkingOperationError("Часть выбранных кодов не найдена.")
        for item in items:
            before = _snapshot(item)
            item.printed_at = now
            item.print_count += 1
            item.pilot_revision += 1
            item.save(update_fields=("printed_at", "print_count", "pilot_revision", "updated_at"))
            _event(item=item, action="print", actor=actor, before=before)
    return items


def return_codes(*, code_ids: list[int], actor=None) -> int:
    unique_ids = list(dict.fromkeys(int(value) for value in code_ids))
    if not unique_ids:
        raise MarkingOperationError("Выберите коды для возврата.")
    changed = 0
    with transaction.atomic():
        items = list(WmsNewMarkingCode.objects.select_for_update().filter(pk__in=unique_ids))
        if len(items) != len(unique_ids):
            raise MarkingOperationError("Часть выбранных кодов не найдена.")
        for item in items:
            before = _snapshot(item)
            item.retired_reference = ""
            item.retired_at = None
            item.processed_at = None
            item.is_returned = True
            item.pilot_revision += 1
            item.save(
                update_fields=(
                    "retired_reference",
                    "retired_at",
                    "processed_at",
                    "is_returned",
                    "pilot_revision",
                    "updated_at",
                )
            )
            _event(item=item, action="return", actor=actor, before=before)
            changed += 1
    return changed
