"""Платёжный QR по стандарту ЦБ РФ (ST00012) для счетов на оплату."""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from typing import Any


def _clean(value: Any, *, max_len: int | None = None) -> str:
    text = " ".join(str(value or "").replace("|", " ").replace("\n", " ").split())
    if max_len is not None:
        return text[:max_len]
    return text


def _sum_kopecks(amount) -> str:
    value = Decimal(str(amount or "0")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return str(int(value * 100))


def build_payment_qr_payload(
    *,
    name: str,
    personal_acc: str,
    bank_name: str,
    bic: str,
    corresp_acc: str,
    payee_inn: str,
    purpose: str,
    amount,
    kpp: str = "",
) -> str:
    """Собрать строку ST00012. Пустая строка, если не хватает обязательных реквизитов."""
    fields = {
        "Name": _clean(name, max_len=160),
        "PersonalAcc": _clean(personal_acc, max_len=20),
        "BankName": _clean(bank_name, max_len=45),
        "BIC": _clean(bic, max_len=9),
        "CorrespAcc": _clean(corresp_acc, max_len=20),
        "PayeeINN": _clean(payee_inn, max_len=12),
        "Purpose": _clean(purpose, max_len=210),
        "Sum": _sum_kopecks(amount),
    }
    required = ("Name", "PersonalAcc", "BankName", "BIC", "CorrespAcc", "PayeeINN", "Purpose", "Sum")
    if any(not fields[key] for key in required):
        return ""
    if fields["Sum"] == "0":
        return ""
    kpp_clean = _clean(kpp, max_len=9)
    if kpp_clean:
        fields["KPP"] = kpp_clean

    parts = ["ST00012"]
    for key in (
        "Name",
        "PersonalAcc",
        "BankName",
        "BIC",
        "CorrespAcc",
        "PayeeINN",
        "KPP",
        "Purpose",
        "Sum",
    ):
        if key in fields and fields[key]:
            parts.append(f"{key}={fields[key]}")
    return "|".join(parts)


def build_payment_qr_for_invoice(*, supplier, invoice, purpose: str) -> str:
    if not supplier:
        return ""
    return build_payment_qr_payload(
        name=getattr(supplier, "name", None) or getattr(supplier, "short_name", None) or "",
        personal_acc=getattr(supplier, "settlement_account", None) or "",
        bank_name=getattr(supplier, "bank_name", None) or "",
        bic=getattr(supplier, "bank_bik", None) or "",
        corresp_acc=getattr(supplier, "correspondent_account", None) or "",
        payee_inn=getattr(supplier, "inn", None) or "",
        purpose=purpose,
        amount=invoice.total_amount,
        kpp=getattr(supplier, "kpp", None) or "",
    )
