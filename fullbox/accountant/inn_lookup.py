"""DaData lookup for the accountant's new-client form."""
from __future__ import annotations

import os

import requests
from django.core.exceptions import ValidationError


DADATA_PARTY_URL = "https://suggestions.dadata.ru/suggestions/api/4_1/rs/findById/party"
DADATA_BANK_URL = "https://suggestions.dadata.ru/suggestions/api/4_1/rs/findById/bank"


def _client_type(data: dict) -> str:
    if data.get("type") == "INDIVIDUAL":
        return "ip"
    opf = data.get("opf") or {}
    label = " ".join(str(opf.get(key) or "") for key in ("full", "short", "type")).lower()
    if "акционер" in label or label.startswith("ао"):
        return "ao"
    return "ooo"


def lookup_party_by_inn(inn: str) -> dict:
    """Return safe client-form fields from DaData without exposing the token."""
    token = (os.environ.get("DADATA_TOKEN") or os.environ.get("DADATA_API_KEY") or "").strip()
    if not token:
        raise ValidationError("Сервис заполнения по ИНН не настроен: отсутствует токен DaData.")
    try:
        response = requests.post(
            DADATA_PARTY_URL,
            json={"query": inn},
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Authorization": f"Token {token}",
            },
            timeout=8,
        )
        response.raise_for_status()
    except requests.Timeout as exc:
        raise ValidationError("Сервис DaData не ответил вовремя. Повторите попытку.") from exc
    except requests.RequestException as exc:
        raise ValidationError("Не удалось получить данные по ИНН. Повторите попытку позже.") from exc

    suggestions = (response.json() or {}).get("suggestions") or []
    if not suggestions:
        return {}
    data = suggestions[0].get("data") or {}
    address = (data.get("address") or {}).get("unrestricted_value") or ""
    management = data.get("management") or {}
    name = data.get("name") or {}
    return {
        "legal_name": name.get("full_with_opf") or suggestions[0].get("value") or "",
        "short_name": name.get("short_with_opf") or "",
        "inn": data.get("inn") or inn,
        "kpp": data.get("kpp") or "",
        "ogrn": data.get("ogrn") or "",
        "legal_address": address,
        "actual_address": address,
        "contact_person": management.get("name") or "",
        "client_type": _client_type(data),
    }


def lookup_bank_by_bik(bik: str) -> dict:
    """Return bank title and correspondent account by BIK from DaData."""
    token = (os.environ.get("DADATA_TOKEN") or os.environ.get("DADATA_API_KEY") or "").strip()
    if not token:
        raise ValidationError("Сервис заполнения по БИК не настроен: отсутствует токен DaData.")
    try:
        response = requests.post(
            DADATA_BANK_URL,
            json={"query": bik},
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Authorization": f"Token {token}",
            },
            timeout=8,
        )
        response.raise_for_status()
    except requests.Timeout as exc:
        raise ValidationError("Сервис DaData не ответил вовремя. Повторите попытку.") from exc
    except requests.RequestException as exc:
        raise ValidationError("Не удалось получить данные банка по БИК. Повторите попытку позже.") from exc

    suggestions = (response.json() or {}).get("suggestions") or []
    if not suggestions:
        return {}
    suggestion = suggestions[0]
    data = suggestion.get("data") or {}
    name = data.get("name") or {}
    return {
        "bank_name": name.get("payment") or name.get("full") or suggestion.get("value") or "",
        "bank_bik": data.get("bic") or bik,
        "correspondent_account": data.get("correspondent_account") or "",
    }
