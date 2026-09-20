from __future__ import annotations

import re
import unicodedata


GS_SEPARATOR = "\x1d"
MAX_MARKING_CODE_LENGTH = 128


class MarkingCodeFormatError(ValueError):
    """Raised when a value cannot be safely used as a marking code."""


def normalize_marking_code(value: object) -> str:
    """Return one scanner-independent representation without losing crypto data."""
    text = str(value or "").strip("\r\n\t ")
    if text[:3].lower() == "]d2":
        text = text[3:]
    text = re.sub(r"_x001d_", GS_SEPARATOR, text, flags=re.IGNORECASE)
    text = re.sub(r"<\s*gs\s*>", GS_SEPARATOR, text, flags=re.IGNORECASE)
    text = re.sub(r"\{\s*fnc1\s*\}", GS_SEPARATOR, text, flags=re.IGNORECASE)
    text = re.sub(r"(?:\\u001d|\\x001d|\\x1d)", GS_SEPARATOR, text, flags=re.IGNORECASE)
    return text.strip("\r\n\t ")


def marking_code_identity(value: object) -> str:
    """Return the serialized-item identity used for global duplicate protection.

    For GS1 DataMatrix this is 01 + GTIN + 21 + serial. Crypto AIs are not
    part of the serialized-item identity, so a full scan and its shortened CIS
    are treated as the same physical code.
    """
    code = normalize_marking_code(value)
    if not _looks_like_gs1_marking(code):
        return code

    prefix = code[:18]
    tail = code[18:]
    if GS_SEPARATOR in tail:
        serial = tail.split(GS_SEPARATOR, 1)[0]
        return f"{prefix}{serial}"

    # Some scanners omit GS before crypto AIs 91/92. Match the same conservative
    # pattern already used by the True API adapter.
    for index in range(0, max(0, len(tail) - 7)):
        if tail[index : index + 2] == "91" and tail[index + 6 : index + 8] in {"92", "93"}:
            return f"{prefix}{tail[:index]}"
    return code


def marking_code_variants(value: object) -> set[str]:
    """Representations that may already exist in legacy tables."""
    code = normalize_marking_code(value)
    if not code:
        return set()
    identity = marking_code_identity(code)
    variants = {code, identity, f"]d2{code}", f"]D2{code}"}
    for item in tuple(variants):
        if GS_SEPARATOR in item:
            variants.add(item.replace(GS_SEPARATOR, "_x001D_"))
            variants.add(item.replace(GS_SEPARATOR, "<GS>"))
            variants.add(item.replace(GS_SEPARATOR, "\\u001d"))
            variants.add(item.replace(GS_SEPARATOR, "\\x1d"))
    return {item for item in variants if item}


def validate_import_marking_code(value: object, *, product_barcode: object = "") -> str:
    """Validate common import mistakes while retaining legacy non-GS1 codes."""
    code = normalize_marking_code(value)
    if not code:
        raise MarkingCodeFormatError("код ЧЗ не указан")
    if len(code) > MAX_MARKING_CODE_LENGTH:
        raise MarkingCodeFormatError(
            f"длина кода ЧЗ больше {MAX_MARKING_CODE_LENGTH} символов"
        )
    if product_barcode and code == str(product_barcode).strip():
        raise MarkingCodeFormatError("в колонке ЧЗ указан штрихкод товара")
    if any(unicodedata.category(char) == "Cc" and char != GS_SEPARATOR for char in code):
        raise MarkingCodeFormatError("код содержит недопустимый управляющий символ")
    if re.search(r"[А-Яа-яЁё]", code):
        raise MarkingCodeFormatError("код содержит кириллицу")
    if code.startswith("01"):
        if not _looks_like_gs1_marking(code):
            raise MarkingCodeFormatError("ожидается формат 01 + GTIN-14 + 21 + серийный номер")
        gtin = code[2:16]
        if not is_valid_gtin14(gtin):
            raise MarkingCodeFormatError(f"неверная контрольная цифра GTIN {gtin}")
        if not marking_code_identity(code)[18:]:
            raise MarkingCodeFormatError("в коде отсутствует серийный номер после AI 21")
    return code


def is_valid_gtin14(value: object) -> bool:
    digits = str(value or "").strip()
    if len(digits) != 14 or not digits.isdigit():
        return False
    total = 0
    for index, digit in enumerate(reversed(digits[:-1])):
        total += int(digit) * (3 if index % 2 == 0 else 1)
    check_digit = (10 - total % 10) % 10
    return check_digit == int(digits[-1])


def _looks_like_gs1_marking(code: str) -> bool:
    return bool(
        len(code) >= 19
        and code.startswith("01")
        and code[2:16].isdigit()
        and code[16:18] == "21"
    )
