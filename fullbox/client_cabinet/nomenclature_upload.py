"""Preview + commit номенклатуры из Excel в ЛК клиента (без мгновенной записи)."""
from __future__ import annotations

import re
import time
from typing import Any

from django.db import IntegrityError, transaction
from django.db.models import Q

from audit.models import log_sku_change
from sklad.models import WarehouseStockSnapshot
from sku.models import SKU, SKUBarcode

MAX_PREVIEW_ROWS = 1000
_HEADER_SCAN_ROWS = 15
# Внутренние ШК склада/ЛК (как в приёмке): префикс 29 + уникальный хвост.
_INTERNAL_BARCODE_PREFIX = "29"

_FIELD_KEYS = (
    "sku_code",
    "name",
    "brand",
    "size",
    "barcode",
    "color",
    "composition",
    "gender",
    "season",
    "made_in",
    "img",
    "barcode_mode",
    "expected_primary_barcode",
)

BARCODE_MODE_REPLACE_PRIMARY = "replace_primary"
BARCODE_MODE_ADD_SECONDARY = "add_secondary"
BARCODE_MODES = {
    BARCODE_MODE_REPLACE_PRIMARY,
    BARCODE_MODE_ADD_SECONDARY,
}


def _clean(value: Any) -> str:
    text = str(value or "").strip()
    if text.lower() in {"nan", "none", "null"}:
        return ""
    return text


def _clean_excel_code(value: Any) -> str:
    """Нормализация артикула/ШК из Excel (12345.0, 1.23E+12 и т.п.)."""
    text = _clean(value)
    if not text:
        return ""
    if re.fullmatch(r"\d+\.0+", text):
        return text.split(".", 1)[0]
    if re.fullmatch(r"\d+[eE][+-]?\d+", text):
        try:
            return str(int(float(text)))
        except (TypeError, ValueError):
            return text
    return text


def _make_internal_barcode_candidates(count: int, reserved: set[str] | None = None) -> list[str]:
    """Детерминированно генерирует кандидаты внутренних ШК без запросов в БД."""
    reserved = reserved or set()
    result: list[str] = []
    seed = int(time.time() * 1000)
    counter = 0
    while len(result) < count and counter < max(count * 20, 500):
        timestamp_part = (seed + counter // 100) % 1_000_000_000
        suffix = counter % 100
        candidate = f"{_INTERNAL_BARCODE_PREFIX}{timestamp_part:09d}{suffix:02d}"
        counter += 1
        if candidate in reserved:
            continue
        reserved.add(candidate)
        result.append(candidate)
    return result


def generate_internal_barcode(reserved: set[str] | None = None) -> str:
    """Уникальный внутренний штрихкод для артикула без ШК в файле."""
    reserved = reserved or set()
    for candidate in _make_internal_barcode_candidates(200, reserved):
        if SKUBarcode.objects.filter(value=candidate).exists():
            continue
        reserved.add(candidate)
        return candidate
    raise ValueError("Не удалось сгенерировать внутренний штрихкод. Попробуйте ещё раз.")


def generate_internal_barcodes(count: int, reserved: set[str] | None = None) -> list[str]:
    """Массовая выдача внутренних ШК: один bulk-поиск занятых значений вместо N запросов."""
    if count <= 0:
        return []
    reserved = reserved or set()
    result: list[str] = []
    attempts = 0
    while len(result) < count and attempts < 10:
        attempts += 1
        need = count - len(result)
        candidates = _make_internal_barcode_candidates(max(need * 2, need + 20), reserved)
        occupied = set(SKUBarcode.objects.filter(value__in=candidates).values_list("value", flat=True))
        for candidate in candidates:
            if candidate in occupied:
                continue
            result.append(candidate)
            if len(result) >= count:
                break
    if len(result) < count:
        raise ValueError("Не удалось сгенерировать внутренние штрихкоды. Попробуйте ещё раз.")
    return result


def _primary_barcode_for_sku(sku: SKU | None) -> str:
    if not sku:
        return ""
    primary = sku.barcodes.filter(is_primary=True).first()
    if primary and primary.value:
        return _clean_excel_code(primary.value)
    any_bc = sku.barcodes.order_by("id").first()
    return _clean_excel_code(getattr(any_bc, "value", ""))


def _sku_import_snapshot(sku: SKU | None) -> dict[str, Any]:
    """Слепок ключевых полей SKU и всех ШК для атомарного audit before/after."""
    if not sku:
        return {}
    barcode_rows = list(
        sku.barcodes.order_by("-is_primary", "id").values(
            "id",
            "value",
            "size",
            "is_primary",
        )
    )
    return {
        "id": sku.id,
        "agency_id": sku.agency_id,
        "sku_code": sku.sku_code,
        "code": sku.code,
        "name": sku.name,
        "brand": sku.brand,
        "size": sku.size,
        "updated_at": sku.updated_at.isoformat() if sku.updated_at else None,
        "barcodes": barcode_rows,
    }


def _existing_sku_lookup(agency, sku_codes: list[str] | set[str]) -> dict[str, SKU]:
    """Возвращает SKU клиента по артикулам/кодам без N+1."""
    normalized_codes = {_clean_excel_code(code).lower() for code in sku_codes if _clean_excel_code(code)}
    if not normalized_codes:
        return {}
    # Для клиента обычно разумный объём каталога; один проход над его SKU быстрее сотен iexact-запросов.
    lookup: dict[str, SKU] = {}
    for sku in (
        SKU.objects.filter(agency=agency, deleted=False)
        .only(
            "id",
            "agency_id",
            "sku_code",
            "code",
            "name",
            "brand",
            "size",
            "color",
            "composition",
            "gender",
            "season",
            "made_in",
            "img",
        )
    ):
        sku_key = _clean_excel_code(sku.sku_code).lower()
        code_key = _clean_excel_code(sku.code).lower()
        if sku_key in normalized_codes:
            lookup.setdefault(sku_key, sku)
        if code_key in normalized_codes:
            lookup.setdefault(code_key, sku)
    return lookup


def _first_barcode_by_sku_id(sku_ids: list[int] | set[int]) -> dict[int, str]:
    """Первый/основной ШК по SKU одним запросом."""
    safe_ids = {int(value) for value in sku_ids if value}
    if not safe_ids:
        return {}
    result: dict[int, str] = {}
    for barcode in (
        SKUBarcode.objects.filter(sku_id__in=safe_ids)
        .only("sku_id", "value", "is_primary")
        .order_by("sku_id", "-is_primary", "id")
    ):
        result.setdefault(barcode.sku_id, _clean_excel_code(barcode.value))
    return result


def _ensure_item_barcodes(
    items: list[dict[str, Any]],
    agency,
    *,
    existing_sku_by_code: dict[str, SKU] | None = None,
) -> int:
    """
    Артикул не должен оставаться без ШК.
    Если в файле пусто — берём уже существующий ШК артикула или генерируем внутренний.
    """
    reserved = {
        _clean_excel_code(item.get("barcode"))
        for item in items
        if _clean_excel_code(item.get("barcode"))
    }
    sku_codes = [_clean_excel_code(item.get("sku_code")) for item in items]
    existing_sku_by_code = existing_sku_by_code or _existing_sku_lookup(agency, sku_codes)
    existing_ids = {
        int(item.get("existing_id"))
        for item in items
        if str(item.get("existing_id") or "").isdigit()
    }
    existing_ids.update(sku.id for sku in existing_sku_by_code.values() if sku and sku.id)
    existing_sku_by_id = {
        sku.id: sku
        for sku in SKU.objects.filter(id__in=existing_ids, agency=agency, deleted=False)
    } if existing_ids else {}
    primary_barcode_by_sku_id = _first_barcode_by_sku_id(existing_ids)
    needs_generated: list[dict[str, Any]] = []
    generated = 0
    for item in items:
        barcode = _clean_excel_code(item.get("barcode"))
        if barcode:
            item["barcode"] = barcode
            item["barcode_generated"] = bool(item.get("barcode_generated"))
            continue

        existing = None
        existing_id = item.get("existing_id")
        if existing_id:
            existing = existing_sku_by_id.get(int(existing_id)) if str(existing_id).isdigit() else None
        if not existing:
            existing = existing_sku_by_code.get(_clean_excel_code(item.get("sku_code")).lower())

        reused = primary_barcode_by_sku_id.get(existing.id) if existing else ""
        if reused and reused not in reserved:
            item["barcode"] = reused
            item["barcode_generated"] = False
            item["warning"] = item.get("warning") or ""
            reserved.add(reused)
            continue
        if reused and reused in reserved:
            # У другого артикула в файле уже стоит этот ШК — для текущей строки нужен новый.
            pass

        needs_generated.append(item)

    generated_values = generate_internal_barcodes(len(needs_generated), reserved)
    for item, value in zip(needs_generated, generated_values, strict=False):
        item["barcode"] = value
        item["barcode_generated"] = True
        if item.get("status") != "conflict":
            item["warning"] = "ШК не был указан — система присвоит внутренний штрихкод."
        generated += 1
    return generated


def _normalize_header(value: Any) -> str:
    text = _clean(value)
    if not text:
        return ""
    return re.sub(r"\s+", " ", text).lower()


def _header_index(header_map: dict[str, int], *names: str) -> int | None:
    for name in names:
        idx = header_map.get(name)
        if idx is not None:
            return idx
    return None


def _build_header_map(header_row) -> dict[str, int]:
    header_map: dict[str, int] = {}
    for idx, value in enumerate(list(header_row)):
        name = _normalize_header(value)
        if not name:
            continue
        header_map.setdefault(name, idx)
    return header_map


def _load_sku_sheet(uploaded_file):
    """Читает Excel и находит строку заголовков номенклатуры (не только первую)."""
    import pandas as pd

    try:
        uploaded_file.seek(0)
    except (AttributeError, OSError):
        pass
    df = pd.read_excel(uploaded_file, header=None, dtype=str)
    if df.empty:
        raise ValueError("Файл пустой")

    header_map: dict[str, int] = {}
    header_idx = None
    scan_until = min(len(df), _HEADER_SCAN_ROWS)
    for idx in range(scan_until):
        candidate = _build_header_map(df.iloc[idx].tolist())
        if "артикул заказчика" in candidate or (
            "артикул" in candidate and ("баркод" in candidate or "штрихкод" in candidate or "предмет" in candidate)
        ):
            header_map = candidate
            header_idx = idx
            break

    if header_idx is None or "артикул заказчика" not in header_map and "артикул" not in header_map:
        raise ValueError(
            "Файл не похож на шаблон номенклатуры. Скачайте шаблон и заполните колонку «Артикул Заказчика»."
        )

    rows = df.iloc[header_idx + 1 :].fillna("")
    return header_map, rows


def _barcode_owners(agency, barcodes: list[str] | set[str]) -> dict[str, SKUBarcode]:
    safe_barcodes = {_clean_excel_code(value) for value in barcodes if _clean_excel_code(value)}
    if not safe_barcodes:
        return {}
    return {
        barcode.value: barcode
        for barcode in (
            SKUBarcode.objects.select_related("sku", "sku__agency")
            .filter(
                agency=agency,
                value__in=safe_barcodes,
            )
            .only("id", "value", "sku_id", "sku__id", "sku__sku_code", "sku__agency_id", "sku__deleted")
        )
    }


def _barcode_conflict_from_owner(agency, barcode: str, sku_code: str, existing: SKUBarcode | None) -> dict[str, Any] | None:
    if not barcode or not existing or not existing.sku:
        return None
    owner = existing.sku
    if owner.deleted:
        if _deleted_barcode_can_be_reused(owner):
            return None
        owner_code = (owner.sku_code or "").strip() or f"#{owner.id}"
        return {
            "barcode_owner_sku": owner_code,
            "barcode_same_agency": owner.agency_id == getattr(agency, "id", None),
            "warning": (
                f"ШК принадлежит удалённому артикулу {owner_code}, но по нему есть складская история. "
                "Укажите другой штрихкод или обратитесь к менеджеру FullBox."
            ),
        }
    if (owner.sku_code or "").strip().lower() == sku_code.lower() and owner.agency_id == getattr(agency, "id", None):
        return None
    same_agency = owner.agency_id == getattr(agency, "id", None)
    owner_code = (owner.sku_code or "").strip() or f"#{owner.id}"
    if same_agency:
        warning = f"ШК уже привязан к вашему артикулу {owner_code}"
    else:
        warning = f"ШК уже занят в системе (артикул {owner_code}). Укажите другой штрихкод."
    return {
        "barcode_owner_sku": owner_code,
        "barcode_same_agency": same_agency,
        "warning": warning,
    }


def _barcode_conflict(agency, barcode: str, sku_code: str) -> dict[str, Any] | None:
    if not barcode:
        return None
    existing = (
        SKUBarcode.objects.select_related("sku", "sku__agency")
        .filter(agency=agency, value=barcode)
        .first()
    )
    return _barcode_conflict_from_owner(agency, barcode, sku_code, existing)


def _deleted_barcode_can_be_reused(sku: SKU | None) -> bool:
    """Удалённый SKU без складской истории не должен навсегда блокировать ШК."""
    if not sku or not sku.deleted:
        return False
    stock_filter = Q(sku_ref=sku)
    sku_code = _clean_excel_code(getattr(sku, "sku_code", ""))
    if sku_code:
        stock_filter |= Q(sku_code__iexact=sku_code)
    return not WarehouseStockSnapshot.objects.filter(agency_id=sku.agency_id).filter(stock_filter).exists()


def _free_reusable_deleted_barcode(barcode: SKUBarcode | None) -> bool:
    """Освобождает ШК от удалённого SKU, если по нему нет складской истории."""
    if not barcode or not barcode.sku:
        return False
    if not _deleted_barcode_can_be_reused(barcode.sku):
        return False
    barcode.delete()
    return True


def parse_sku_template_preview(uploaded_file, agency) -> dict[str, Any]:
    header_map, rows = _load_sku_sheet(uploaded_file)

    sku_code_idx = _header_index(header_map, "артикул заказчика", "артикул")
    if sku_code_idx is None:
        raise ValueError("В шаблоне номенклатуры не найдена колонка «Артикул Заказчика».")

    barcode_idx = _header_index(header_map, "баркод", "штрихкод", "шк товара", "шк")
    name_idx = _header_index(header_map, "предмет", "наименование", "товар")
    size_idx = _header_index(header_map, "размер")
    brand_idx = _header_index(header_map, "бренд")
    color_idx = _header_index(header_map, "цвет")
    composition_idx = _header_index(header_map, "состав")
    gender_idx = _header_index(header_map, "пол")
    season_idx = _header_index(header_map, "сезон")
    made_in_idx = _header_index(header_map, "страна пр-ва", "страна производства")
    img_idx = _header_index(header_map, "ссылка на товар", "ссылка")

    items: list[dict[str, Any]] = []
    for _, row in rows.iterrows():
        values = [_clean(value) for value in row.tolist()]
        if not any(values):
            continue
        sku_code = _clean_excel_code(row.iloc[sku_code_idx])
        if not sku_code:
            continue

        def cell(idx, *, as_code: bool = False):
            if idx is None:
                return ""
            raw = row.iloc[idx]
            return _clean_excel_code(raw) if as_code else _clean(raw)

        item = {
            "sku_code": sku_code,
            "name": cell(name_idx),
            "brand": cell(brand_idx),
            "size": cell(size_idx),
            "barcode": cell(barcode_idx, as_code=True),
            "color": cell(color_idx),
            "composition": cell(composition_idx),
            "gender": cell(gender_idx),
            "season": cell(season_idx),
            "made_in": cell(made_in_idx),
            "img": cell(img_idx),
            "warning": "",
            "barcode_generated": False,
        }
        item["status"] = "new"
        item["existing_id"] = None
        item["existing_sku_code"] = ""
        item["existing_name"] = ""

        items.append(item)
        if len(items) >= MAX_PREVIEW_ROWS:
            break

    if not items:
        raise ValueError(
            "В файле нет строк с артикулом для загрузки. "
            "Проверьте, что заполнена колонка «Артикул Заказчика» (не только номер строки)."
        )

    existing_sku_by_code = _existing_sku_lookup(agency, [item["sku_code"] for item in items])
    for item in items:
        existing = existing_sku_by_code.get(_clean_excel_code(item["sku_code"]).lower())
        if existing:
            item["status"] = "update"
            item["existing_id"] = existing.id
            item["existing_sku_code"] = existing.sku_code
            item["existing_name"] = existing.name or ""
    generated_count = _ensure_item_barcodes(items, agency, existing_sku_by_code=existing_sku_by_code)

    current_primary_by_sku_id = _first_barcode_by_sku_id(
        [sku.id for sku in existing_sku_by_code.values() if sku and sku.id]
    )
    for item in items:
        existing_id = item.get("existing_id")
        current_primary = current_primary_by_sku_id.get(int(existing_id), "") if existing_id else ""
        imported_barcode = _clean_excel_code(item.get("barcode"))
        item["current_primary_barcode"] = current_primary
        item["expected_primary_barcode"] = current_primary
        item["barcode_mode"] = ""
        item["requires_barcode_mode"] = bool(existing_id and imported_barcode != current_primary)

    # Один артикул может занимать несколько строк маркетплейс-шаблона
    # (например, один SKU с несколькими товарными ШК). Для нового SKU первая
    # строка задаёт основной ШК, остальные ШК того же артикула сохраняются как
    # дополнительные. Раньше preview показывал все строки как новые, а commit
    # на второй строке видел уже созданный SKU и откатывал весь пакет как
    # «изменившийся после предпросмотра».
    new_primary_by_sku_code: dict[str, str] = {}
    for item in items:
        if item.get("existing_id"):
            continue
        sku_key = _clean_excel_code(item.get("sku_code")).lower()
        imported_barcode = _clean_excel_code(item.get("barcode"))
        if not sku_key or not imported_barcode:
            continue
        first_barcode = new_primary_by_sku_code.setdefault(sku_key, imported_barcode)
        if imported_barcode == first_barcode:
            continue
        item["expected_primary_barcode"] = first_barcode
        item["barcode_mode"] = BARCODE_MODE_ADD_SECONDARY
        item["requires_barcode_mode"] = False
        item["warning"] = (
            f"Дополнительный ШК этого же нового артикула. "
            f"Основным будет ШК {first_barcode}."
        )

    barcode_owner_by_value = _barcode_owners(
        agency,
        [item.get("barcode") or "" for item in items],
    )
    for item in items:
        barcode = item.get("barcode") or ""
        conflict = _barcode_conflict_from_owner(
            agency,
            barcode,
            item.get("sku_code") or "",
            barcode_owner_by_value.get(barcode),
        )
        if conflict:
            item["status"] = "conflict"
            item["warning"] = conflict["warning"]
            item["barcode_owner_sku"] = conflict["barcode_owner_sku"]
            item["barcode_generated"] = False

    _mark_infile_barcode_duplicates(items)

    new_count = sum(1 for item in items if item["status"] == "new")
    update_count = sum(1 for item in items if item["status"] == "update")
    conflict_count = sum(1 for item in items if item["status"] == "conflict")
    barcode_choice_count = sum(1 for item in items if item.get("requires_barcode_mode"))
    update_codes = [item["sku_code"] for item in items if item["status"] == "update"][:8]
    conflict_codes = [item["sku_code"] for item in items if item["status"] == "conflict"][:8]

    parts = [f"Найдено позиций: {len(items)}."]
    parts.append(f"Новых: {new_count}.")
    if update_count:
        sample = ", ".join(update_codes)
        more = "…" if update_count > len(update_codes) else ""
        parts.append(f"Уже есть в вашем каталоге ({update_count}): {sample}{more}.")
    else:
        parts.append("Совпадений с вашим каталогом нет.")
    if generated_count:
        parts.append(
            f"Без ШК в файле: {generated_count} — система подставит внутренний штрихкод (можно изменить в таблице)."
        )
    if conflict_count:
        sample = ", ".join(conflict_codes)
        more = "…" if conflict_count > len(conflict_codes) else ""
        parts.append(
            f"Конфликт штрихкода ({conflict_count}): {sample}{more}. "
            "Исправьте повторяющиеся или занятые ШК в файле — иначе сохранение откатится и каталог останется пустым."
        )
    if barcode_choice_count:
        parts.append(
            f"Для существующих SKU с новым ШК ({barcode_choice_count}) выберите: "
            "заменить основной или добавить дополнительный."
        )

    return {
        "rows": items,
        "filename": str(getattr(uploaded_file, "name", "") or ""),
        "truncated": len(items) >= MAX_PREVIEW_ROWS,
        "counts": {
            "total": len(items),
            "new": new_count,
            "update": update_count,
            "conflict": conflict_count,
            "generated_barcodes": generated_count,
            "barcode_choice_required": barcode_choice_count,
        },
        "message": " ".join(parts),
    }


def _mark_infile_barcode_duplicates(items: list[dict[str, Any]]) -> None:
    """Помечает повторы ШК внутри одного файла (частая причина пустого каталога после ошибки)."""
    first_owner: dict[str, str] = {}
    for item in items:
        barcode = _clean_excel_code(item.get("barcode"))
        sku_code = _clean_excel_code(item.get("sku_code"))
        if not barcode or not sku_code:
            continue
        owner = first_owner.get(barcode)
        if owner is None:
            first_owner[barcode] = sku_code
            continue
        if owner.lower() == sku_code.lower():
            # тот же артикул + тот же ШК (повтор строки) — не конфликт БД, но лишнее
            if item.get("status") != "conflict":
                item["warning"] = item.get("warning") or f"Повтор строки: ШК уже указан у артикула {owner}"
            continue
        item["status"] = "conflict"
        item["warning"] = (
            f"ШК {barcode} повторяется в файле: уже указан у артикула {owner}. "
            "Оставьте ШК только в одной строке."
        )
        item["barcode_owner_sku"] = owner


def _normalize_commit_row(raw: Any) -> dict[str, str] | None:
    if not isinstance(raw, dict):
        return None
    row = {key: _clean(raw.get(key)) for key in _FIELD_KEYS}
    row["sku_code"] = _clean_excel_code(row.get("sku_code"))
    row["barcode"] = _clean_excel_code(row.get("barcode"))
    if not row["sku_code"]:
        return None
    if not row["name"]:
        row["name"] = row["sku_code"]
    return row


def commit_sku_template_rows(agency, rows: list[Any], *, actor=None) -> dict[str, Any]:
    normalized: list[dict[str, Any]] = []
    raw_items: list[tuple[dict[str, Any], Any]] = []
    for raw in rows or []:
        item = _normalize_commit_row(raw)
        if item:
            raw_items.append((item, raw))
    existing_sku_by_code = _existing_sku_lookup(agency, [item["sku_code"] for item, _raw in raw_items])
    for item, raw in raw_items:
        item["barcode_generated"] = bool(isinstance(raw, dict) and raw.get("barcode_generated"))
        item["status"] = "new"
        item["warning"] = ""
        existing = existing_sku_by_code.get(_clean_excel_code(item["sku_code"]).lower())
        if existing:
            item["status"] = "update"
            item["existing_id"] = existing.id
        normalized.append(item)
    if not normalized:
        raise ValueError("Нет строк для сохранения. В модалке должны быть строки с артикулом.")
    if len(normalized) > MAX_PREVIEW_ROWS:
        raise ValueError(f"Слишком много строк (макс. {MAX_PREVIEW_ROWS})")

    generated_count = _ensure_item_barcodes(normalized, agency, existing_sku_by_code=existing_sku_by_code)

    # Сначала проверяем повторы ШК — иначе часть строк создаётся и всё откатывается.
    seen_barcodes: dict[str, str] = {}
    pre_errors: list[str] = []
    for item in normalized:
        barcode = item.get("barcode") or ""
        if not barcode:
            pre_errors.append(f"{item['sku_code']}: не удалось назначить штрихкод.")
            continue
        owner = seen_barcodes.get(barcode)
        if owner is None:
            seen_barcodes[barcode] = item["sku_code"]
            continue
        if owner.lower() != item["sku_code"].lower():
            pre_errors.append(
                f"ШК {barcode} повторяется в файле у артикулов {owner} и {item['sku_code']}. "
                "Оставьте каждый штрихкод только в одной строке."
            )
    barcode_owner_by_value = _barcode_owners(
        agency,
        [item.get("barcode") or "" for item in normalized],
    )
    for item in normalized:
        barcode = item.get("barcode") or ""
        conflict = _barcode_conflict_from_owner(
            agency,
            barcode,
            item["sku_code"],
            barcode_owner_by_value.get(barcode),
        )
        if conflict:
            pre_errors.append(f"{item['sku_code']}: {conflict['warning']}")
    if pre_errors:
        uniq: list[str] = []
        for msg in pre_errors:
            if msg not in uniq:
                uniq.append(msg)
        raise ValueError("; ".join(uniq[:8]))

    before = SKU.objects.filter(agency=agency, deleted=False).count()
    created = 0
    updated = 0
    results: list[dict[str, Any]] = []
    created_sku_ids_in_batch: set[int] = set()

    try:
        with transaction.atomic():
            for item in normalized:
                sku_key = _clean_excel_code(item["sku_code"]).lower()
                existing_sku = existing_sku_by_code.get(sku_key)
                barcode = _clean_excel_code(item.get("barcode"))
                if not barcode:
                    raise ValueError(f"{item['sku_code']}: артикул нельзя сохранить без штрихкода.")

                sku = None
                locked_barcodes: list[SKUBarcode] = []
                before_snapshot: dict[str, Any] = {}
                if existing_sku:
                    sku = SKU.objects.select_for_update().get(
                        pk=existing_sku.pk,
                        agency=agency,
                        deleted=False,
                    )
                    locked_barcodes = list(
                        SKUBarcode.objects.select_for_update()
                        .filter(sku=sku)
                        .order_by("-is_primary", "id")
                    )
                    before_snapshot = _sku_import_snapshot(sku)

                known_barcode = barcode_owner_by_value.get(barcode)
                existing_barcode = None
                if known_barcode:
                    existing_barcode = (
                        SKUBarcode.objects.select_for_update()
                        .select_related("sku")
                        .filter(pk=known_barcode.pk)
                        .first()
                    )
                if existing_barcode and (not sku or existing_barcode.sku_id != sku.id):
                    if _free_reusable_deleted_barcode(existing_barcode):
                        existing_barcode = None
                    else:
                        raise ValueError(
                            f"ШК {barcode} уже привязан к артикулу {existing_barcode.sku.sku_code}. "
                            "Исправьте файл и загрузите снова — текущее сохранение отменено."
                        )

                if not sku:
                    sku = SKU.objects.create(
                        agency=agency,
                        sku_code=item["sku_code"],
                        code=barcode,
                        name=item["name"] or item["sku_code"],
                        brand=item["brand"] or None,
                        size=item["size"] or None,
                        color=item["color"] or None,
                        composition=item["composition"] or None,
                        gender=item["gender"] or None,
                        season=item["season"] or None,
                        made_in=item["made_in"] or None,
                        img=item["img"] or None,
                        source="manual",
                    )
                    SKUBarcode.objects.create(
                        sku=sku,
                        value=barcode,
                        size=item.get("size") or None,
                        is_primary=True,
                    )
                    created += 1
                    created_sku_ids_in_batch.add(sku.id)
                    existing_sku_by_code[sku_key] = sku
                    barcode_action = "created_primary"
                    row_status = "created"
                    previous_primary = ""
                    primary_after = barcode
                    old_primary_preserved = False
                    after_snapshot = _sku_import_snapshot(sku)
                    log_sku_change(
                        "create",
                        sku,
                        user=actor,
                        description="Создание SKU через импорт номенклатуры в ЛК клиента",
                        snapshot={
                            "before": {},
                            "after": after_snapshot,
                            "barcode_action": barcode_action,
                        },
                    )
                else:
                    created_in_batch = sku.id in created_sku_ids_in_batch
                    current_primary_obj = next((row for row in locked_barcodes if row.is_primary), None)
                    if current_primary_obj is None and locked_barcodes:
                        current_primary_obj = locked_barcodes[0]
                    current_primary = _clean_excel_code(getattr(current_primary_obj, "value", ""))
                    expected_primary = _clean_excel_code(item.get("expected_primary_barcode"))
                    barcode_changed = barcode != current_primary
                    mode = _clean(item.get("barcode_mode"))

                    # Повторная строка того же SKU в текущем импортируемом файле
                    # не является параллельным изменением каталога. Первый ШК
                    # уже создан как основной выше, следующий добавляем как
                    # дополнительный в рамках той же атомарной транзакции.
                    if created_in_batch and barcode_changed:
                        expected_primary = current_primary
                        mode = BARCODE_MODE_ADD_SECONDARY

                    if barcode_changed and expected_primary != current_primary:
                        raise ValueError(
                            f"{item['sku_code']}: основной ШК изменился после предпросмотра "
                            f"({expected_primary or 'не был'} → {current_primary or 'не задан'}). "
                            "Загрузите файл заново и подтвердите актуальный вариант."
                        )
                    if barcode_changed and mode not in BARCODE_MODES:
                        raise ValueError(
                            f"{item['sku_code']}: выберите «Заменить основной» "
                            "или «Добавить дополнительный»."
                        )
                    if barcode_changed and mode == BARCODE_MODE_ADD_SECONDARY and not current_primary_obj:
                        raise ValueError(
                            f"{item['sku_code']}: у SKU нет основного ШК — выберите «Заменить основной»."
                        )

                    changed_fields: list[str] = []
                    for field in (
                        "name",
                        "brand",
                        "size",
                        "color",
                        "composition",
                        "gender",
                        "season",
                        "made_in",
                        "img",
                    ):
                        value = item.get(field) or ""
                        if not value:
                            continue
                        current = _clean(getattr(sku, field, ""))
                        # Для повторных строк нового артикула карточку задаёт
                        # первая строка. Последующие строки могут добавить ШК
                        # (и свой размер в SKUBarcode), но не должны незаметно
                        # перезаписывать название/размер самой карточки.
                        if created_in_batch and current:
                            continue
                        if current != value:
                            setattr(sku, field, value)
                            changed_fields.append(field)

                    barcode_action = "unchanged"
                    barcode_relation_changed = False
                    if barcode_changed and mode == BARCODE_MODE_ADD_SECONDARY:
                        if existing_barcode is None:
                            existing_barcode = SKUBarcode.objects.create(
                                sku=sku,
                                value=barcode,
                                size=item.get("size") or None,
                                is_primary=False,
                            )
                            barcode_relation_changed = True
                            barcode_action = "added_secondary"
                        else:
                            barcode_action = "already_secondary"
                    elif barcode_changed and mode == BARCODE_MODE_REPLACE_PRIMARY:
                        target_barcode = existing_barcode
                        if target_barcode is None:
                            target_barcode = SKUBarcode.objects.create(
                                sku=sku,
                                value=barcode,
                                size=item.get("size") or None,
                                is_primary=False,
                            )
                            barcode_relation_changed = True
                        demoted = (
                            SKUBarcode.objects.filter(sku=sku, is_primary=True)
                            .exclude(pk=target_barcode.pk)
                            .update(is_primary=False)
                        )
                        if not target_barcode.is_primary:
                            target_barcode.is_primary = True
                            target_barcode.save(update_fields=["is_primary"])
                            barcode_relation_changed = True
                        if demoted:
                            barcode_relation_changed = True
                        if _clean_excel_code(sku.code) != barcode:
                            sku.code = barcode
                            changed_fields.append("code")
                        barcode_action = "replaced_primary"

                    row_changed = bool(changed_fields or barcode_relation_changed)
                    if row_changed:
                        changed_fields.append("updated_at")
                        sku.save(update_fields=list(dict.fromkeys(changed_fields)))
                        if created_in_batch:
                            row_status = "created_secondary"
                        else:
                            updated += 1
                            row_status = "updated"
                        after_snapshot = _sku_import_snapshot(sku)
                        log_sku_change(
                            "update",
                            sku,
                            user=actor,
                            description="Обновление SKU через импорт номенклатуры в ЛК клиента",
                            snapshot={
                                "before": before_snapshot,
                                "after": after_snapshot,
                                "barcode_action": barcode_action,
                            },
                        )
                    else:
                        row_status = "unchanged"

                    previous_primary = current_primary
                    primary_after = barcode if barcode_action == "replaced_primary" else current_primary
                    old_primary_preserved = bool(
                        barcode_action == "replaced_primary"
                        and previous_primary
                        and previous_primary != primary_after
                    )

                results.append(
                    {
                        "sku_id": sku.id,
                        "sku_code": sku.sku_code,
                        "status": row_status,
                        "barcode": barcode,
                        "barcode_action": barcode_action,
                        "previous_primary_barcode": previous_primary,
                        "primary_barcode": primary_after,
                        "old_primary_preserved": old_primary_preserved,
                    }
                )
    except IntegrityError as exc:
        raise ValueError(
            "Номенклатура изменилась параллельно или ШК уже успели занять. "
            "Сохранение полностью отменено — загрузите файл заново."
        ) from exc

    after = SKU.objects.filter(agency=agency, deleted=False).count()
    extra = f" Внутренних ШК выдано: {generated_count}." if generated_count else ""
    return {
        "created": created,
        "updated": updated,
        "total": after,
        "before": before,
        "generated_barcodes": generated_count,
        "results": results,
        "message": (
            f"Сохранено. Новых SKU: {created}, обновлено: {updated}. Всего в каталоге: {after}.{extra}"
        ),
    }
