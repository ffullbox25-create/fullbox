"""Проверка распределения: сумма по направлениям = общее количество артикула."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class ValidationIssue:
    code: str
    message: str
    row_no: int | None = None
    sku_code: str = ""
    severity: str = "error"  # error | warning
    column: str = ""
    value: str = ""
    recommendation: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class DistributionDraft:
    """Нормализованный черновик из формы или Excel."""

    meta: dict[str, Any] = field(default_factory=dict)
    items: list[dict[str, Any]] = field(default_factory=list)
    directions: list[dict[str, Any]] = field(default_factory=list)
    # allocations: list of {sku_code, direction_key, qty, ...row meta}
    allocations: list[dict[str, Any]] = field(default_factory=list)


def _norm(value: Any) -> str:
    return str(value or "").strip()


def validate_distribution_draft(
    draft: DistributionDraft,
    *,
    allow_incomplete: bool = False,
    known_sku_codes: set[str] | None = None,
) -> list[ValidationIssue]:
    """
    allow_incomplete=True — черновик (неполное распределение допускается).
    known_sku_codes — артикулы клиента (casefold); None = не проверять чужие.
    """
    issues: list[ValidationIssue] = []
    if not draft.items:
        if not allow_incomplete:
            issues.append(
                ValidationIssue(
                    "no_items",
                    "Добавьте хотя бы один артикул.",
                    recommendation="Заполните шаблон и загрузите файл.",
                )
            )
    if not draft.directions:
        if not allow_incomplete:
            issues.append(
                ValidationIssue(
                    "no_directions",
                    "Добавьте хотя бы одно направление.",
                    recommendation="Укажите маркетплейс/склад или «Хранение FullBox».",
                )
            )

    item_totals: dict[str, int] = {}
    for idx, item in enumerate(draft.items, start=1):
        code = _norm(item.get("sku_code"))
        if not code:
            issues.append(
                ValidationIssue(
                    "empty_sku",
                    "Пустой артикул.",
                    row_no=idx,
                    column="Артикул",
                    recommendation="Укажите артикул из номенклатуры клиента.",
                )
            )
            continue
        key = code.casefold()
        try:
            qty = int(item.get("qty_total") or 0)
        except (TypeError, ValueError):
            issues.append(
                ValidationIssue(
                    "bad_qty",
                    f"Некорректное количество у {code}.",
                    row_no=idx,
                    sku_code=code,
                    column="Общее количество",
                    value=str(item.get("qty_total") or ""),
                    recommendation="Укажите целое положительное число.",
                )
            )
            continue
        if qty <= 0:
            issues.append(
                ValidationIssue(
                    "non_positive_qty",
                    f"Количество артикула {code} должно быть > 0.",
                    row_no=idx,
                    sku_code=code,
                    column="Общее количество",
                    value=str(qty),
                    recommendation="Укажите целое положительное число.",
                )
            )
            continue
        if key in item_totals:
            issues.append(
                ValidationIssue(
                    "dup_item",
                    f"Артикул {code} указан дважды в номенклатуре.",
                    sku_code=code,
                    column="Артикул",
                    recommendation="Объедините строки одного артикула.",
                )
            )
            continue
        if known_sku_codes is not None and key not in known_sku_codes:
            issues.append(
                ValidationIssue(
                    "unknown_sku",
                    f"Артикул {code} не найден в номенклатуре клиента.",
                    sku_code=code,
                    column="Артикул",
                    value=code,
                    recommendation="Проверьте артикул в разделе «Номенклатура».",
                )
            )
        item_totals[key] = qty

    dir_keys: set[str] = set()
    for idx, direction in enumerate(draft.directions, start=1):
        key = _norm(direction.get("key") or direction.get("title") or f"d{idx}")
        if key.casefold() in dir_keys:
            issues.append(
                ValidationIssue(
                    "dup_direction",
                    f"Дубль направления «{key}».",
                    row_no=idx,
                    recommendation="Объедините одинаковые направления.",
                )
            )
            continue
        dir_keys.add(key.casefold())
        kind = _norm(direction.get("kind") or "marketplace")
        if kind != "storage_fullbox":
            mp = _norm(direction.get("marketplace_name") or direction.get("marketplace"))
            wh = _norm(direction.get("destination_warehouse"))
            supply = _norm(direction.get("supply_number"))
            if not mp and not wh:
                if not allow_incomplete:
                    issues.append(
                        ValidationIssue(
                            "bad_direction",
                            f"Направление «{key}»: укажите маркетплейс или склад назначения.",
                            row_no=idx,
                            column="Маркетплейс",
                            recommendation="Для маркетплейса заполните маркетплейс и склад.",
                        )
                    )
            if not allow_incomplete and mp and not supply:
                issues.append(
                    ValidationIssue(
                        "missing_supply",
                        f"Направление «{key}»: укажите номер поставки.",
                        severity="warning",
                        row_no=idx,
                        column="Номер поставки",
                        recommendation="Для направлений маркетплейса укажите номер поставки.",
                    )
                )
            if not allow_incomplete and mp and not wh:
                issues.append(
                    ValidationIssue(
                        "missing_warehouse",
                        f"Направление «{key}»: укажите склад назначения.",
                        row_no=idx,
                        column="Склад назначения",
                        recommendation="Укажите склад назначения маркетплейса.",
                    )
                )

    allocated: dict[str, int] = {k: 0 for k in item_totals}
    seen_pairs: set[tuple[str, str]] = set()
    for idx, row in enumerate(draft.allocations, start=1):
        sku = _norm(row.get("sku_code")).casefold()
        dkey = _norm(row.get("direction_key")).casefold()
        excel_row = row.get("row_no") or idx
        try:
            qty = int(row.get("qty") or 0)
        except (TypeError, ValueError):
            issues.append(
                ValidationIssue(
                    "bad_alloc_qty",
                    "Некорректное количество в распределении.",
                    row_no=excel_row if isinstance(excel_row, int) else idx,
                    column="Количество по направлению",
                    value=str(row.get("qty") or ""),
                    recommendation="Укажите положительное целое число.",
                )
            )
            continue
        if qty <= 0:
            issues.append(
                ValidationIssue(
                    "non_positive_alloc",
                    "Количество по направлению должно быть > 0.",
                    row_no=excel_row if isinstance(excel_row, int) else idx,
                    sku_code=sku,
                    column="Количество по направлению",
                    value=str(qty),
                    recommendation="Укажите положительное целое число.",
                )
            )
            continue
        if sku not in item_totals:
            issues.append(
                ValidationIssue(
                    "alloc_unknown_sku",
                    "Распределение ссылается на неизвестный артикул.",
                    row_no=excel_row if isinstance(excel_row, int) else idx,
                    sku_code=sku,
                    column="Артикул",
                )
            )
            continue
        if dkey not in dir_keys:
            issues.append(
                ValidationIssue(
                    "alloc_unknown_dir",
                    f"Неизвестное направление «{dkey}».",
                    row_no=excel_row if isinstance(excel_row, int) else idx,
                )
            )
            continue
        pair = (sku, dkey)
        if pair in seen_pairs:
            issues.append(
                ValidationIssue(
                    "dup_alloc",
                    "Дубль распределения артикула по направлению.",
                    severity="warning",
                    row_no=excel_row if isinstance(excel_row, int) else idx,
                    sku_code=sku,
                    recommendation="Объедините одинаковые строки вручную или оставьте одну.",
                )
            )
            continue
        seen_pairs.add(pair)
        allocated[sku] = allocated.get(sku, 0) + qty

    if not allow_incomplete:
        for sku, total in item_totals.items():
            got = allocated.get(sku, 0)
            if got < total:
                issues.append(
                    ValidationIssue(
                        "qty_mismatch",
                        f"По товару {sku} не распределено {total - got} единиц.",
                        sku_code=sku,
                        column="Количество по направлению",
                        recommendation="Сумма по направлениям должна равняться общему количеству.",
                    )
                )
            elif got > total:
                issues.append(
                    ValidationIssue(
                        "qty_mismatch",
                        f"По товару {sku} распределено на {got - total} единиц больше общего количества.",
                        sku_code=sku,
                        column="Количество по направлению",
                        recommendation="Уменьшите количество по направлениям или увеличьте общее.",
                    )
                )
    else:
        for sku, total in item_totals.items():
            got = allocated.get(sku, 0)
            if got > total:
                issues.append(
                    ValidationIssue(
                        "qty_over",
                        f"Артикул {sku}: распределено {got} больше общего {total}.",
                        sku_code=sku,
                    )
                )

    return issues


def has_blocking_errors(issues: list[ValidationIssue]) -> bool:
    return any(i.severity != "warning" for i in issues)
