"""Pull Ozon FBO supply orders via Seller API for the client shipping form."""

from __future__ import annotations

import logging
import hashlib
import json
import re
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

from django.core.cache import cache
from django.core.exceptions import FieldDoesNotExist, FieldError
from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime
import urllib3

from market_sync.http import friendly_network_error, marketplace_request
from fbs.models import FbsOzonCredential
from sku.models import MarketCredential

logger = logging.getLogger(__name__)

OZON_API_BASE = "https://api-seller.ozon.ru"
OZON_API_HOST = "api-seller.ozon.ru"
OZON_DOH_URL = "https://1.1.1.1/dns-query"
OZON_DOH_FALLBACK_URL = "https://8.8.8.8/resolve"
_OZON_DNS_CACHE: dict[str, Any] = {"expires_at": 0.0, "ips": []}

# Active / preparable supply orders relevant when creating a Fullbox shipment.
DEFAULT_LIST_STATES = (
    "DATA_FILLING",
    "READY_TO_SUPPLY",
    "ACCEPTED_AT_SUPPLY_WAREHOUSE",
)

# Keep picker ids numeric for the existing URL and JavaScript contract while
# carrying the source cabinet explicitly. Ozon visible supply ids are currently
# around 2e12; this private 9e12..12e12 range is only used between our list and
# detail endpoints and remains well below JavaScript's safe integer limit.
_OZON_SELECTION_BASE = 8_000_000_000_000
_OZON_SELECTION_SLOT_STRIDE = 1_000_000_000_000
_OZON_GM_BUNDLE_CACHE_SECONDS = 6 * 60 * 60
_OZON_GM_MANIFEST_CACHE_SECONDS = 10 * 60


def allocate_reserve_box_codes(
    requested_items: list[dict],
    stock_rows: list[dict],
) -> dict:
    """Select safe box hints without touching warehouse data or other reserves.

    ``available_qty`` must already exclude active reservations of other orders.
    Fully free boxes are preferred; a positive residual of a partially reserved
    box is used only afterwards.  The function is intentionally pure so the
    caller can perform the actual reserve through the existing warehouse
    write-path after all validations have passed.
    """
    candidates_by_key: dict[object, dict[str, dict]] = {}
    for row in stock_rows or []:
        item_key = row.get("item_key")
        box_code = str(row.get("box_code") or "").strip()
        available_qty = max(int(row.get("available_qty") or 0), 0)
        physical_qty = max(int(row.get("qty") or 0), 0)
        if item_key is None or not box_code or available_qty <= 0:
            continue
        normalized_code = box_code.casefold()
        candidate = candidates_by_key.setdefault(item_key, {}).setdefault(
            normalized_code,
            {
                "box_code": box_code,
                "available_qty": 0,
                "qty": 0,
            },
        )
        candidate["available_qty"] += available_qty
        candidate["qty"] += physical_qty

    selected_by_item: dict[object, list[str]] = {}
    shortages: list[dict] = []
    for requested in requested_items or []:
        item_id = requested.get("item_id")
        item_key = requested.get("item_key")
        needed_qty = max(int(requested.get("qty") or 0), 0)
        preferred = {
            str(code or "").strip().casefold()
            for code in requested.get("preferred_box_codes") or []
            if str(code or "").strip()
        }
        candidates = list(candidates_by_key.get(item_key, {}).values())

        def candidate_priority(candidate: dict) -> tuple:
            available_qty = max(int(candidate.get("available_qty") or 0), 0)
            physical_qty = max(int(candidate.get("qty") or 0), 0)
            fully_free = physical_qty > 0 and available_qty >= physical_qty
            is_preferred = str(candidate.get("box_code") or "").casefold() in preferred
            # Valid existing hints stay stable inside each safety group, while
            # every fully free box remains ahead of every partial residual.
            group = (0 if fully_free else 2) + (0 if is_preferred else 1)
            exact_fit = 0 if available_qty == needed_qty else 1
            return (
                group,
                exact_fit,
                available_qty,
                str(candidate.get("box_code") or "").casefold(),
            )

        selected_codes: list[str] = []
        remaining_qty = needed_qty
        for candidate in sorted(candidates, key=candidate_priority):
            if remaining_qty <= 0:
                break
            available_qty = max(int(candidate.get("available_qty") or 0), 0)
            if available_qty <= 0:
                continue
            applied_qty = min(available_qty, remaining_qty)
            candidate["available_qty"] = available_qty - applied_qty
            remaining_qty -= applied_qty
            selected_codes.append(str(candidate.get("box_code") or "").strip())

        selected_by_item[item_id] = selected_codes
        if remaining_qty > 0:
            shortages.append(
                {
                    "item_id": item_id,
                    "item_key": item_key,
                    "requested_qty": needed_qty,
                    "available_qty": needed_qty - remaining_qty,
                    "shortage_qty": remaining_qty,
                }
            )

    return {
        "ok": not shortages,
        "box_codes_by_item": selected_by_item,
        "shortages": shortages,
    }


def _normalize_ozon_client_id(value: str) -> str:
    text = (value or "").strip()
    if not text:
        return ""
    if text.endswith(".0") and text[:-2].isdigit():
        return text[:-2]
    return text


def _encode_ozon_selection_id(order_id: Any, slot: int | None) -> int:
    raw_id = _as_int(order_id) or 0
    clean_slot = _as_int(slot)
    if (
        clean_slot is None
        or not 1 <= clean_slot <= 3
        or raw_id <= 0
        or raw_id >= _OZON_SELECTION_SLOT_STRIDE
    ):
        return raw_id
    return _OZON_SELECTION_BASE + clean_slot * _OZON_SELECTION_SLOT_STRIDE + raw_id


def _decode_ozon_selection_id(value: Any) -> tuple[int, int | None]:
    selection_id = _as_int(value) or 0
    offset = selection_id - _OZON_SELECTION_BASE
    if offset <= 0:
        return selection_id, None
    slot = offset // _OZON_SELECTION_SLOT_STRIDE
    raw_id = offset % _OZON_SELECTION_SLOT_STRIDE
    if 1 <= slot <= 3 and 0 < raw_id < _OZON_SELECTION_SLOT_STRIDE:
        return raw_id, int(slot)
    return selection_id, None


@dataclass(frozen=True)
class OzonCredentialRef:
    client_id: str
    api_key: str
    slot: int | None = None

    @property
    def label(self) -> str:
        return f"Кабинет {self.slot}" if self.slot else "Основной кабинет"


def resolve_ozon_credentials(agency) -> tuple[list[OzonCredentialRef], str]:
    """Return every configured Ozon cabinet, preserving the legacy fallback."""
    if agency is None:
        return [], "Клиент не выбран."

    slot_credentials = list(
        FbsOzonCredential.objects.filter(agency=agency).order_by("slot", "id")
    )
    result: list[OzonCredentialRef] = []
    seen_client_ids: set[str] = set()

    # The legacy credential is what the FBO picker used before multi-cabinet
    # support. Keep it authoritative for the primary client_id so rollout
    # cannot replace a known-working key with an older duplicate slot value.
    legacy, legacy_error = resolve_ozon_credential(agency)
    if legacy is not None:
        client_id = _normalize_ozon_client_id(legacy.client_id or "")
        api_key = str(legacy.market_key or "").strip()
        matching_slot = next(
            (
                int(item.slot)
                for item in slot_credentials
                if _normalize_ozon_client_id(item.client_id or "") == client_id
            ),
            None,
        )
        if client_id and api_key:
            result.append(
                OzonCredentialRef(
                    client_id=client_id,
                    api_key=api_key,
                    slot=matching_slot,
                )
            )
            seen_client_ids.add(client_id)

    for credential in slot_credentials:
        client_id = _normalize_ozon_client_id(credential.client_id or "")
        api_key = str(credential.api_key or "").strip()
        if not client_id or not api_key or client_id in seen_client_ids:
            continue
        result.append(
            OzonCredentialRef(
                client_id=client_id,
                api_key=api_key,
                slot=int(credential.slot),
            )
        )
        seen_client_ids.add(client_id)
    if result:
        result.sort(key=lambda item: (item.slot is None, item.slot or 999, item.client_id))
        return result, ""
    return [], legacy_error or "Для клиента не найдены реквизиты Ozon."


def _ordered_ozon_credentials(
    agency,
    *,
    client_id_hint: str = "",
) -> tuple[list[OzonCredentialRef], str]:
    credentials, error = resolve_ozon_credentials(agency)
    hint = _normalize_ozon_client_id(client_id_hint)
    if hint:
        credentials.sort(key=lambda item: item.client_id != hint)
    return credentials, error


def resolve_ozon_credential(agency) -> tuple[MarketCredential | None, str]:
    if agency is None:
        return None, "Клиент не выбран."
    qs = MarketCredential.objects.filter(agency=agency).select_related("market")
    for cred in qs:
        name = (getattr(cred.market, "name", "") or "").upper()
        if "OZON" in name:
            token = (cred.market_key or "").strip()
            client_id = _normalize_ozon_client_id(cred.client_id or "")
            if not token or not client_id:
                return None, "В ЛК не заданы Client ID и Api-Key Ozon."
            return cred, ""
    return None, "Для клиента не найден ключ API Ozon. Добавьте его в настройках маркетплейсов."


def _ozon_headers(client_id: str, api_key: str) -> dict[str, str]:
    return {
        "Client-Id": client_id,
        "Api-Key": api_key,
        "Content-Type": "application/json",
    }


def _parse_ozon_response(status_code: int, text: str) -> tuple[dict | None, str]:
    if status_code != 200:
        snippet = (text or "").strip()
        if len(snippet) > 220:
            snippet = f"{snippet[:220]}..."
        detail = f": {snippet}" if snippet else ""
        return None, f"Ozon API ошибка {status_code}{detail}"
    try:
        data = json.loads(text or "{}")
    except ValueError:
        return None, "Ozon API вернул некорректный JSON."
    if not isinstance(data, dict):
        return None, "Ozon API вернул неожиданный ответ."
    return data, ""


def _resolve_ozon_api_ips() -> tuple[list[str], str]:
    now = time.monotonic()
    cached_ips = list(_OZON_DNS_CACHE.get("ips") or [])
    if cached_ips and float(_OZON_DNS_CACHE.get("expires_at") or 0) > now:
        return cached_ips, ""

    last_error = ""
    for url in (OZON_DOH_URL, OZON_DOH_FALLBACK_URL):
        try:
            response = marketplace_request(
                "GET",
                url,
                params={"name": OZON_API_HOST, "type": "A"},
                headers={"Accept": "application/dns-json"},
                timeout=4,
            )
            if response.status_code != 200:
                last_error = f"DNS-over-HTTPS ошибка {response.status_code}"
                continue
            payload = response.json()
        except Exception as exc:
            last_error = friendly_network_error(exc, "Ozon DNS")
            continue

        answers = payload.get("Answer") if isinstance(payload, dict) else None
        ips: list[str] = []
        ttl_values: list[int] = []
        for row in answers or []:
            if not isinstance(row, dict) or int(row.get("type") or 0) != 1:
                continue
            ip = str(row.get("data") or "").strip()
            if ip and ip not in ips:
                ips.append(ip)
            try:
                ttl_values.append(int(row.get("TTL") or 60))
            except (TypeError, ValueError):
                pass
        if ips:
            ttl = max(30, min(min(ttl_values or [60]), 300))
            _OZON_DNS_CACHE["ips"] = ips
            _OZON_DNS_CACHE["expires_at"] = now + ttl
            return ips, ""
        last_error = "DNS-over-HTTPS не вернул IP Ozon."

    cached_ips = list(_OZON_DNS_CACHE.get("ips") or [])
    if cached_ips:
        return cached_ips, ""
    return [], last_error or "Не удалось разрешить адрес API Ozon."


def _ozon_post_via_resolved_ip(
    path: str,
    client_id: str,
    api_key: str,
    payload: dict,
    *,
    timeout: int,
) -> tuple[dict | None, str, bool]:
    ips, error = _resolve_ozon_api_ips()
    if not ips:
        return None, error, False

    headers = _ozon_headers(client_id, api_key)
    headers["Host"] = OZON_API_HOST
    body = json.dumps(payload).encode("utf-8")
    last_error = ""
    for ip in ips[:3]:
        try:
            pool = urllib3.HTTPSConnectionPool(
                ip,
                port=443,
                assert_hostname=OZON_API_HOST,
                server_hostname=OZON_API_HOST,
            )
            response = pool.request(
                "POST",
                path,
                body=body,
                headers=headers,
                timeout=urllib3.Timeout(connect=min(max(timeout, 1), 5), read=max(timeout, 1)),
                retries=False,
            )
            text = response.data.decode("utf-8", "ignore")
            data, parse_error = _parse_ozon_response(int(response.status), text)
            return data, parse_error, True
        except Exception as exc:
            last_error = friendly_network_error(exc, "Ozon")
            logger.info("Ozon API fallback via %s failed: %s", ip, exc)
    return None, last_error or "Ozon API недоступен.", False


def ozon_post(path: str, client_id: str, api_key: str, payload: dict, *, timeout: int = 40):
    data, fallback_error, reached_ozon = _ozon_post_via_resolved_ip(
        path,
        client_id,
        api_key,
        payload,
        timeout=timeout,
    )
    if data is not None or reached_ozon:
        return data, fallback_error or None

    url = f"{OZON_API_BASE}{path}"
    try:
        response = marketplace_request(
            "POST",
            url,
            headers=_ozon_headers(client_id, api_key),
            json=payload,
            timeout=timeout,
        )
    except Exception as exc:
        return None, fallback_error or friendly_network_error(exc, "Ozon")
    data, error = _parse_ozon_response(response.status_code, response.text)
    return data, error or None


def _ozon_pdf_response(data: bytes, status_code: int) -> tuple[bytes | None, str]:
    if status_code != 200:
        snippet = data.decode("utf-8", "ignore").strip()
        if len(snippet) > 220:
            snippet = f"{snippet[:220]}..."
        detail = f": {snippet}" if snippet else ""
        return None, f"Ozon API ошибка {status_code}{detail}"
    if not data or not data.lstrip().startswith(b"%PDF-"):
        return None, "Ozon API не вернул PDF с этикетками."
    if len(data) > 25 * 1024 * 1024:
        return None, "PDF с этикетками Ozon превышает допустимый размер 25 МБ."
    return bytes(data), ""


def ozon_get_pdf(path: str, client_id: str, api_key: str, *, timeout: int = 30):
    """Download a PDF from Ozon without writing shipment or warehouse state."""
    headers = _ozon_headers(client_id, api_key)
    headers["Accept"] = "application/pdf"
    headers["Host"] = OZON_API_HOST
    ips, fallback_error = _resolve_ozon_api_ips()
    reached_ozon = False
    for ip in ips[:3]:
        try:
            pool = urllib3.HTTPSConnectionPool(
                ip,
                port=443,
                assert_hostname=OZON_API_HOST,
                server_hostname=OZON_API_HOST,
            )
            response = pool.request(
                "GET",
                path,
                headers=headers,
                timeout=urllib3.Timeout(connect=min(max(timeout, 1), 5), read=max(timeout, 1)),
                retries=False,
            )
            reached_ozon = True
            return _ozon_pdf_response(bytes(response.data or b""), int(response.status))
        except Exception as exc:
            fallback_error = friendly_network_error(exc, "Ozon")
            logger.info("Ozon PDF fallback via %s failed: %s", ip, exc)

    if reached_ozon:
        return None, fallback_error or "Ozon API недоступен."
    try:
        response = marketplace_request(
            "GET",
            f"{OZON_API_BASE}{path}",
            headers={key: value for key, value in headers.items() if key != "Host"},
            timeout=timeout,
        )
    except Exception as exc:
        return None, fallback_error or friendly_network_error(exc, "Ozon")
    return _ozon_pdf_response(bytes(response.content or b""), int(response.status_code))


def _download_ozon_signed_pdf(url: str, *, timeout: int = 30) -> tuple[bytes | None, str]:
    """Download an Ozon-provided label URL without forwarding Seller API credentials."""
    try:
        parsed = urlsplit(str(url or "").strip())
        hostname = str(parsed.hostname or "").lower()
        valid_host = hostname.endswith(".ozone.ru") or hostname.endswith(".ozon.ru")
        if parsed.scheme != "https" or not valid_host or parsed.username or parsed.password:
            return None, "Ozon вернул некорректную ссылку на PDF с этикетками."
        if parsed.port not in (None, 443):
            return None, "Ozon вернул некорректную ссылку на PDF с этикетками."
    except ValueError:
        return None, "Ozon вернул некорректную ссылку на PDF с этикетками."

    try:
        response = marketplace_request("GET", parsed.geturl(), timeout=timeout)
    except Exception as exc:
        return None, friendly_network_error(exc, "Ozon")
    return _ozon_pdf_response(bytes(response.content or b""), int(response.status_code))


_OZON_LABEL_ERROR_LABELS = {
    "INVALID_STATE": "Ozon ещё не разрешает печать этикеток в текущем статусе поставки.",
    "OPERATION_NOT_FOUND": "Ozon не нашёл операцию формирования этикеток.",
    "OPERATION_FAILED": "Ozon не смог сформировать этикетки.",
    "SUPPLY_NOT_BELONG_CONTRACTOR": "Поставка не принадлежит контрагенту ключа Ozon.",
    "SUPPLY_NOT_BELONG_COMPANY": "Поставка не принадлежит компании ключа Ozon.",
    "SUPPLY_IS_EMPTY": "В поставке Ozon нет грузомест.",
    "CARGOES_NOT_FOUND": "Ozon не нашёл указанные грузоместа.",
}


def _ozon_label_api_error(payload: dict | None) -> str:
    if not isinstance(payload, dict):
        return ""
    errors = payload.get("errors")
    reasons = errors.get("error_reasons") if isinstance(errors, dict) else []
    labels: list[str] = []
    for value in reasons or []:
        reason = str(value or "").strip()
        if not reason:
            continue
        label = _OZON_LABEL_ERROR_LABELS.get(reason, reason)
        if label not in labels:
            labels.append(label)
    return " ".join(labels)


def _ozon_label_groups(
    gm_cargoes: list[dict],
    *,
    expected_gm_barcodes: list[str] | None = None,
) -> tuple[dict[int, list[int]], str]:
    expected = {
        str(value or "").strip().casefold()
        for value in (expected_gm_barcodes or [])
        if str(value or "").strip()
    }
    actual: set[str] = set()
    groups: dict[int, list[int]] = {}
    seen_cargo_ids: set[int] = set()
    for row in gm_cargoes or []:
        if not isinstance(row, dict):
            return {}, "В заявке повреждены данные грузомест Ozon."
        gm_barcode = str(row.get("gm_barcode") or "").strip()
        supply_id = _as_int(row.get("supply_id"))
        cargo_id = _as_int(row.get("cargo_id"))
        if not gm_barcode or not supply_id or supply_id <= 0 or not cargo_id or cargo_id <= 0:
            return {}, "Не у всех ШК ГМ сохранены supply_id и cargo_id Ozon."
        gm_key = gm_barcode.casefold()
        if gm_key in actual or cargo_id in seen_cargo_ids:
            return {}, "В заявке есть повторяющиеся грузоместа Ozon."
        actual.add(gm_key)
        seen_cargo_ids.add(cargo_id)
        groups.setdefault(supply_id, []).append(cargo_id)
    if not groups:
        return {}, "В заявке нет грузомест Ozon для печати."
    if expected and actual != expected:
        return {}, "Сохранённый состав ШК ГМ неполный. Этикетки не сформированы."
    return groups, ""


def _ozon_label_operation_cache_key(
    agency_id: int,
    supply_id: int,
    cargo_ids: list[int],
    *,
    client_id: str = "",
) -> str:
    identity = (
        f"{agency_id}:{_normalize_ozon_client_id(client_id)}:{supply_id}:"
        + ",".join(str(value) for value in sorted(cargo_ids))
    )
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]
    return f"shipping:ozon-gm-label:{digest}"


def fetch_ozon_gm_label_documents(
    agency,
    gm_cargoes: list[dict],
    *,
    expected_gm_barcodes: list[str] | None = None,
    poll_attempts: int = 6,
    poll_interval: float = 1.1,
    timeout: int = 12,
    _client_id_hint: str = "",
) -> tuple[list[dict], str, bool]:
    """Generate official Ozon cargo labels and return PDFs without persistence."""
    row_client_ids = {
        _normalize_ozon_client_id(str(row.get("ozon_client_id") or ""))
        for row in gm_cargoes or []
        if isinstance(row, dict) and str(row.get("ozon_client_id") or "").strip()
    }
    if not _client_id_hint and len(row_client_ids) > 1:
        actual_barcodes = {
            str(row.get("gm_barcode") or "").strip().casefold()
            for row in gm_cargoes or []
            if isinstance(row, dict) and str(row.get("gm_barcode") or "").strip()
        }
        expected_barcodes = {
            str(value or "").strip().casefold()
            for value in (expected_gm_barcodes or [])
            if str(value or "").strip()
        }
        if expected_barcodes and actual_barcodes != expected_barcodes:
            return [], "Сохранённый состав ШК ГМ неполный. Этикетки не сформированы.", False
        documents: list[dict] = []
        any_pending = False
        for client_id in sorted(row_client_ids):
            rows = [
                row
                for row in gm_cargoes or []
                if isinstance(row, dict)
                and _normalize_ozon_client_id(str(row.get("ozon_client_id") or "")) == client_id
            ]
            expected = [
                str(row.get("gm_barcode") or "").strip()
                for row in rows
                if str(row.get("gm_barcode") or "").strip()
            ]
            account_documents, error, pending = fetch_ozon_gm_label_documents(
                agency,
                rows,
                expected_gm_barcodes=expected,
                poll_attempts=poll_attempts,
                poll_interval=poll_interval,
                timeout=timeout,
                _client_id_hint=client_id,
            )
            if error:
                return [], error, pending
            any_pending = any_pending or pending
            documents.extend(account_documents)
        return documents, "", any_pending

    client_id_hint = _normalize_ozon_client_id(
        _client_id_hint or (next(iter(row_client_ids)) if len(row_client_ids) == 1 else "")
    )
    if client_id_hint:
        credentials, credential_error = _ordered_ozon_credentials(
            agency,
            client_id_hint=client_id_hint,
        )
        credential = next(
            (item for item in credentials if item.client_id == client_id_hint),
            None,
        )
    else:
        credential, credential_error = resolve_ozon_credential(agency)
    if credential_error or credential is None:
        return [], credential_error or "Не найдены реквизиты Ozon.", False
    groups, groups_error = _ozon_label_groups(
        gm_cargoes,
        expected_gm_barcodes=expected_gm_barcodes,
    )
    if groups_error:
        return [], groups_error, False

    client_id = _normalize_ozon_client_id(credential.client_id or "")
    api_key = str(
        getattr(credential, "api_key", "")
        or getattr(credential, "market_key", "")
        or ""
    ).strip()
    operations: dict[int, tuple[str, str]] = {}
    for supply_id, cargo_ids in sorted(groups.items()):
        cache_key = _ozon_label_operation_cache_key(
            int(agency.id),
            supply_id,
            cargo_ids,
            client_id=client_id,
        )
        operation_id = str(cache.get(cache_key) or "").strip()
        if not operation_id:
            payload = None
            error = None
            for attempt in range(4):
                payload, error = ozon_post(
                    "/v1/cargoes-label/create",
                    client_id,
                    api_key,
                    {
                        "supply_id": supply_id,
                        "cargoes": [{"cargo_id": cargo_id} for cargo_id in cargo_ids],
                    },
                    timeout=timeout,
                )
                if not error or "Ozon API ошибка 429" not in str(error):
                    break
                if attempt < 3:
                    time.sleep(1.1 * (attempt + 1))
            if error:
                return [], error, False
            api_error = _ozon_label_api_error(payload)
            operation_id = str((payload or {}).get("operation_id") or "").strip()
            if api_error or not operation_id:
                return [], api_error or "Ozon не вернул идентификатор операции этикеток.", False
            cache.set(cache_key, operation_id, timeout=10 * 60)
        operations[supply_id] = (operation_id, cache_key)

    label_files: dict[int, tuple[str, str]] = {}
    attempts = max(min(int(poll_attempts or 1), 12), 1)
    for supply_id, (operation_id, cache_key) in operations.items():
        for attempt in range(attempts):
            payload, error = ozon_post(
                "/v1/cargoes-label/get",
                client_id,
                api_key,
                {"operation_id": operation_id},
                timeout=timeout,
            )
            if error:
                if "Ozon API ошибка 429" in str(error) and attempt + 1 < attempts:
                    time.sleep(max(min(float(poll_interval or 0), 2.0), 1.1))
                    continue
                return [], error, False
            api_error = _ozon_label_api_error(payload)
            status = str((payload or {}).get("status") or "").strip().upper()
            result = (payload or {}).get("result")
            file_guid = str(result.get("file_guid") or "").strip() if isinstance(result, dict) else ""
            file_url = str(result.get("file_url") or "").strip() if isinstance(result, dict) else ""
            if status == "SUCCESS" and file_guid:
                if not re.fullmatch(r"[A-Za-z0-9-]{8,128}", file_guid):
                    return [], "Ozon вернул некорректный идентификатор PDF.", False
                label_files[supply_id] = (file_guid, file_url)
                break
            if api_error or status == "FAILED":
                cache.delete(cache_key)
                return [], api_error or "Ozon не смог сформировать этикетки.", False
            if attempt + 1 < attempts:
                time.sleep(max(min(float(poll_interval or 0), 2.0), 0.0))
        if supply_id not in label_files:
            return [], "Ozon ещё формирует этикетки. Повторите скачивание через несколько секунд.", True

    documents: list[dict] = []
    for supply_id, (file_guid, file_url) in sorted(label_files.items()):
        if file_url:
            pdf, error = _download_ozon_signed_pdf(file_url, timeout=max(timeout, 20))
        else:
            pdf, error = ozon_get_pdf(
                f"/v1/cargoes-label/file/{file_guid}",
                client_id,
                api_key,
                timeout=max(timeout, 20),
            )
        if error or pdf is None:
            return [], error or "Не удалось скачать PDF Ozon.", False
        documents.append({"supply_id": supply_id, "content": pdf})

    for _operation_id, cache_key in operations.values():
        cache.delete(cache_key)
    return documents, "", False


def _as_int(value) -> int | None:
    try:
        if value is None or value == "":
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_dt(value) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip()
        if not text:
            return None
        dt = parse_datetime(text.replace("Z", "+00:00"))
        if dt is None:
            # Ozon sometimes returns date-only or local without tz.
            try:
                dt = datetime.fromisoformat(text)
            except ValueError:
                return None
    if timezone.is_naive(dt):
        return timezone.make_aware(dt, timezone.get_current_timezone())
    return dt


def _timeslot_parts(timeslot_node: dict | None) -> tuple[str, str]:
    """Return (slot_date ISO, slot_time HH:MM) from Ozon timeslot payload."""
    if not isinstance(timeslot_node, dict):
        return "", ""
    # v3 get: { timeslot: { from, to }, timezone_info }
    # v1 details: { value: { timeslot: { from, to }, ... } }
    inner = timeslot_node.get("timeslot")
    if not isinstance(inner, dict):
        value = timeslot_node.get("value")
        if isinstance(value, dict):
            inner = value.get("timeslot") if isinstance(value.get("timeslot"), dict) else value
    if not isinstance(inner, dict):
        inner = timeslot_node
    start = _parse_dt(inner.get("from") or inner.get("from_"))
    if start is None:
        return "", ""
    local = timezone.localtime(start)
    return local.date().isoformat(), local.strftime("%H:%M")


def _warehouse_label(node: dict | None) -> str:
    if not isinstance(node, dict):
        return ""
    name = str(node.get("name") or "").strip()
    address = str(node.get("address") or "").strip()
    if name and address and address.lower() not in name.lower():
        return f"{name} ({address})"
    return name or address


def list_ozon_supply_order_ids(
    client_id: str,
    api_key: str,
    *,
    states: tuple[str, ...] = DEFAULT_LIST_STATES,
    limit: int = 50,
) -> tuple[list[int], str]:
    payload = {
        "filter": {"states": list(states)},
        "limit": max(1, min(int(limit), 100)),
        "sort_by": "ORDER_STATE_UPDATED_AT",
        "sort_dir": "DESC",
    }
    data, error = ozon_post("/v3/supply-order/list", client_id, api_key, payload)
    if error:
        return [], error
    raw_ids = data.get("order_ids") or []
    ids: list[int] = []
    for item in raw_ids:
        parsed = _as_int(item)
        if parsed is not None:
            ids.append(parsed)
    return ids, ""


def fetch_ozon_supply_orders(
    client_id: str,
    api_key: str,
    order_ids: list[int],
    *,
    timeout: int = 40,
) -> tuple[list[dict], str]:
    if not order_ids:
        return [], ""
    data, error = ozon_post(
        "/v3/supply-order/get",
        client_id,
        api_key,
        {"order_ids": order_ids},
        timeout=timeout,
    )
    if error:
        return [], error
    orders = data.get("orders") or []
    return [o for o in orders if isinstance(o, dict)], ""


def fetch_ozon_supply_order_details(
    client_id: str,
    api_key: str,
    order_id: int,
    *,
    timeout: int = 40,
) -> tuple[dict | None, str]:
    data, error = ozon_post(
        "/v1/supply-order/details",
        client_id,
        api_key,
        {"order_id": int(order_id)},
        timeout=timeout,
    )
    if error:
        return None, error
    return data, ""


def fetch_ozon_supply_bundle_items(
    client_id: str,
    api_key: str,
    bundle_ids: list[str],
    *,
    limit: int = 100,
    max_pages: int = 20,
    timeout: int = 40,
) -> tuple[list[dict], str]:
    clean_ids = [str(x).strip() for x in bundle_ids if str(x or "").strip()]
    if not clean_ids:
        return [], ""
    items: list[dict] = []
    last_id = None
    # Cap pages to avoid long hangs on huge bundles.
    for _ in range(max(1, min(int(max_pages), 20))):
        payload: dict[str, Any] = {
            "bundle_ids": clean_ids,
            "limit": max(1, min(int(limit), 100)),
        }
        if last_id:
            payload["last_id"] = last_id
        data, error = ozon_post(
            "/v1/supply-order/bundle",
            client_id,
            api_key,
            payload,
            timeout=timeout,
        )
        if error:
            return items, error
        chunk = data.get("items") or []
        for row in chunk:
            if isinstance(row, dict):
                items.append(row)
        last_id = data.get("last_id")
        # Ozon may return a cursor even when the requested bundle is already
        # fully present in a short page.  Avoid a redundant second request: it
        # only consumes the strict Seller API per-second limit and can turn a
        # complete first page into a false 429 error.
        if not last_id or not chunk or len(chunk) < payload["limit"]:
            break
    else:
        return items, "Ozon: состав содержит больше страниц, чем загружено; проверка не завершена."
    return items, ""


def _extract_gm_from_supplies_cargoes(payload: dict | None) -> list[dict[str, Any]]:
    """Flatten cargo barcodes (ШК ГМ) from /v1/cargoes/supplies/get."""
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    if not isinstance(payload, dict):
        return rows

    def add_cargo(cargo: dict, *, supply_id: Any, transport_cargo_id: Any = None) -> None:
        if not isinstance(cargo, dict):
            return
        barcode = str(cargo.get("barcode") or cargo.get("cargo_id") or "").strip()
        if not barcode or barcode in seen:
            return
        seen.add(barcode)
        rows.append(
            {
                "gm_barcode": barcode,
                "cargo_id": _as_int(cargo.get("cargo_id")),
                "supply_id": _as_int(supply_id) if supply_id is not None else None,
                "bundle_id": str(cargo.get("bundle_id") or "").strip(),
                "transport_cargo_id": _as_int(transport_cargo_id) if transport_cargo_id is not None else None,
            }
        )

    for supply in payload.get("supplies_cargoes") or []:
        if not isinstance(supply, dict):
            continue
        supply_id = supply.get("supply_id")
        for cargo in supply.get("cargoes_without_transport_cargoes") or []:
            add_cargo(cargo, supply_id=supply_id)
        for transport in supply.get("transport_cargoes") or []:
            if not isinstance(transport, dict):
                continue
            tid = transport.get("transport_cargo_id")
            for cargo in transport.get("cargoes") or []:
                add_cargo(cargo, supply_id=supply_id, transport_cargo_id=tid)
    return rows


def fetch_ozon_gm_cargoes(
    client_id: str,
    api_key: str,
    supply_ids: list[str | int],
    *,
    timeout: int = 40,
) -> tuple[list[dict[str, Any]], str]:
    """Pull cargo-place barcodes (ШК ГМ) for FBO supplies — one barcode per box/cargo."""
    clean_ids: list[str] = []
    for value in supply_ids:
        text = str(value or "").strip()
        if text and text not in clean_ids:
            clean_ids.append(text)
    if not clean_ids:
        return [], ""

    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    errors: list[str] = []

    data, error = ozon_post(
        "/v1/cargoes/supplies/get",
        client_id,
        api_key,
        {"supply_ids": clean_ids},
        timeout=timeout,
    )
    if error:
        errors.append(error)
    else:
        for row in _extract_gm_from_supplies_cargoes(data):
            code = str(row.get("gm_barcode") or "").strip()
            if code and code not in seen:
                seen.add(code)
                rows.append(row)

    # Fallback / merge: /v1/cargoes/get sometimes lists BOX cargoes even when barcode
    # field is only available as cargo_id.
    data_v1, error_v1 = ozon_post(
        "/v1/cargoes/get",
        client_id,
        api_key,
        {"supply_ids": clean_ids},
        timeout=timeout,
    )
    if error_v1:
        errors.append(error_v1)
    elif isinstance(data_v1, dict):
        for supply in data_v1.get("supply") or []:
            if not isinstance(supply, dict):
                continue
            supply_id = supply.get("supply_id")
            for cargo in supply.get("cargoes") or []:
                if not isinstance(cargo, dict):
                    continue
                cargo_id = cargo.get("cargo_id")
                barcode = str(cargo.get("barcode") or cargo_id or "").strip()
                if not barcode or barcode in seen:
                    continue
                seen.add(barcode)
                rows.append(
                    {
                        "gm_barcode": barcode,
                        "cargo_id": _as_int(cargo_id),
                        "supply_id": _as_int(supply_id) if supply_id is not None else None,
                        "bundle_id": str(cargo.get("bundle_id") or "").strip(),
                        "cargo_type": str(cargo.get("type") or "").strip(),
                        "transport_cargo_id": None,
                    }
                )

    if not rows and errors:
        return [], errors[0]
    return rows, ""


def _ozon_gm_bundle_cache_key(client_id: str, bundle_id: str) -> str:
    digest = hashlib.sha256(
        f"{str(client_id or '').strip()}:{str(bundle_id or '').strip()}".encode("utf-8")
    ).hexdigest()
    return f"shipping:ozon:gm-bundle:v1:{digest}"


def _cached_ozon_gm_bundle_items(client_id: str, bundle_id: str) -> list[dict[str, Any]] | None:
    bundle_id = str(bundle_id or "").strip()
    if not bundle_id:
        return None
    cached = cache.get(_ozon_gm_bundle_cache_key(client_id, bundle_id))
    if not isinstance(cached, list):
        return None
    return [dict(item) for item in cached if isinstance(item, dict)]


def _cache_ozon_gm_bundle_items(
    client_id: str,
    bundle_id: str,
    items: list[dict[str, Any]],
) -> None:
    cache.set(
        _ozon_gm_bundle_cache_key(client_id, bundle_id),
        [dict(item) for item in items if isinstance(item, dict)],
        timeout=_OZON_GM_BUNDLE_CACHE_SECONDS,
    )


def hydrate_gm_cargoes_from_cache(
    client_id: str,
    gm_cargoes: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Attach previously verified per-cargo compositions without API calls."""
    hydrated: list[dict[str, Any]] = []
    for cargo in gm_cargoes or []:
        row = dict(cargo or {})
        bundle_id = str(row.get("bundle_id") or "").strip()
        items = [dict(item) for item in row.get("items") or [] if isinstance(item, dict)]
        if not items and bundle_id:
            cached = _cached_ozon_gm_bundle_items(client_id, bundle_id)
            if cached is not None:
                items = cached
        row["items"] = items
        row["product_barcodes"] = [item["barcode"] for item in items if item.get("barcode")]
        row["offer_ids"] = [item["offer_id"] for item in items if item.get("offer_id")]
        row["quantity"] = sum(int(item.get("quantity") or 0) for item in items)
        hydrated.append(row)
    return hydrated


def enrich_gm_cargoes_with_items(
    client_id: str,
    api_key: str,
    gm_cargoes: list[dict[str, Any]],
    *,
    pause_seconds: float = 0.45,
    max_fetch: int | None = None,
    timeout: int = 40,
    budget_seconds: float | None = None,
) -> list[dict[str, Any]]:
    """Attach exact cargo composition with bounded, cached Ozon API calls.

    Large supplies are loaded over several short HTTP requests.  Successful
    bundle reads are shared through Django's cache, so a later progress request
    and the final submit reuse the same verified bundle-to-cargo mapping.
    """
    import time

    deadline = time.monotonic() + max(float(budget_seconds), 1) if budget_seconds is not None else float("inf")
    enriched = hydrate_gm_cargoes_from_cache(client_id, gm_cargoes)
    bundle_cache: dict[str, list[dict[str, Any]]] = {}
    bundle_errors: dict[str, str] = {}
    fetched_bundle_count = 0
    fetch_limit = None if max_fetch is None else max(int(max_fetch or 0), 0)
    result: list[dict[str, Any]] = []
    for cargo in enriched:
        row = dict(cargo or {})
        bundle_id = str(row.get("bundle_id") or "").strip()
        items = [dict(item) for item in row.get("items") or [] if isinstance(item, dict)]
        can_fetch = (fetch_limit is None or fetched_bundle_count < fetch_limit) and time.monotonic() < deadline
        if not items and bundle_id and can_fetch:
            if bundle_id not in bundle_cache and bundle_id not in bundle_errors:
                # The Seller API enforces a strict per-second limit.  Pace even
                # the first cargo call because order/cargo endpoints were read
                # immediately before this function.
                time.sleep(max(float(pause_seconds or 0), 0))
                fetched_bundle_count += 1
                raw_items: list[dict] = []
                error = ""
                for attempt in range(2 if budget_seconds is not None else 3):
                    if time.monotonic() >= deadline:
                        error = "Ozon: загрузка продолжится следующим запросом"
                        break
                    raw_items, error = fetch_ozon_supply_bundle_items(
                        client_id,
                        api_key,
                        [bundle_id],
                        timeout=timeout,
                        max_pages=4 if budget_seconds is not None else 20,
                    )
                    if not error or "429" not in error:
                        break
                    time.sleep(max(float(pause_seconds or 0), 0.2) * (attempt + 1))
                if error:
                    bundle_errors[bundle_id] = error
                    logger.info("Ozon cargo bundle %s skipped: %s", bundle_id, error)
                else:
                    normalized = [_bundle_item_row(item) for item in raw_items]
                    bundle_cache[bundle_id] = normalized
                    _cache_ozon_gm_bundle_items(client_id, bundle_id, normalized)
            items = [dict(item) for item in bundle_cache.get(bundle_id, [])]
        row["items"] = items
        row["product_barcodes"] = [item["barcode"] for item in items if item.get("barcode")]
        row["offer_ids"] = [item["offer_id"] for item in items if item.get("offer_id")]
        row["quantity"] = sum(int(item.get("quantity") or 0) for item in items)
        result.append(row)
    return result


def flatten_bundle_items_from_gm(gm_cargoes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Aggregate product lines from all GM cargoes (for stock matching)."""
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for cargo in gm_cargoes or []:
        for item in cargo.get("items") or []:
            offer = str(item.get("offer_id") or "").strip()
            barcode = str(item.get("barcode") or "").strip()
            key = (offer.lower(), barcode.lower())
            current = merged.get(key)
            if current is None:
                merged[key] = {
                    "offer_id": offer,
                    "name": str(item.get("name") or "").strip(),
                    "barcode": barcode,
                    "ozon_sku": item.get("ozon_sku"),
                    "quantity": int(item.get("quantity") or 0),
                    "quant": int(item.get("quant") or 0),
                }
            else:
                current["quantity"] = int(current.get("quantity") or 0) + int(item.get("quantity") or 0)
    return list(merged.values())


def _gm_item_key(item: dict[str, Any]) -> tuple[str, str]:
    return (
        str(item.get("offer_id") or "").strip().lower(),
        str(item.get("barcode") or "").strip().lower(),
    )


def _gm_item_box_meta(gm_cargoes: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    """Return cargo counts and GM codes per product line when cargo items are known."""
    meta: dict[tuple[str, str], dict[str, Any]] = {}
    for cargo in gm_cargoes or []:
        gm_barcode = str(cargo.get("gm_barcode") or "").strip()
        seen_in_cargo: set[tuple[str, str]] = set()
        for item in cargo.get("items") or []:
            if not isinstance(item, dict):
                continue
            key = _gm_item_key(item)
            if not key[0] and not key[1]:
                continue
            bucket = meta.setdefault(key, {"boxes": 0, "quantity": 0, "gm_barcodes": []})
            bucket["quantity"] = int(bucket.get("quantity") or 0) + int(item.get("quantity") or 0)
            if key not in seen_in_cargo:
                bucket["boxes"] = int(bucket.get("boxes") or 0) + 1
                seen_in_cargo.add(key)
            if gm_barcode and gm_barcode not in bucket["gm_barcodes"]:
                bucket["gm_barcodes"].append(gm_barcode)
    return meta


def supply_ids_from_order(order: dict, details: dict | None = None) -> list[str]:
    ids: list[str] = []
    for source in (order, details if isinstance(details, dict) else None):
        if not isinstance(source, dict):
            continue
        for supply in source.get("supplies") or []:
            if not isinstance(supply, dict):
                continue
            sid = supply.get("supply_id")
            if sid is None:
                continue
            text = str(sid).strip()
            if text and text not in ids:
                ids.append(text)
        order_number = str(source.get("order_number") or "").strip()
        if order_number.isdigit() and order_number not in ids:
            ids.append(order_number)
    return ids


def _dropoff_node(order: dict | None) -> dict | None:
    """Ozon may return either dropoff_warehouse or drop_off_warehouse."""
    if not isinstance(order, dict):
        return None
    for key in ("dropoff_warehouse", "drop_off_warehouse"):
        node = order.get(key)
        if isinstance(node, dict):
            return node
    return None


def _summarize_order(order: dict) -> dict[str, Any]:
    supplies = [s for s in (order.get("supplies") or []) if isinstance(s, dict)]
    warehouses: list[str] = []
    for supply in supplies:
        label = _warehouse_label(supply.get("storage_warehouse"))
        if label and label not in warehouses:
            warehouses.append(label)
    slot_date, slot_time = _timeslot_parts(order.get("timeslot") if isinstance(order.get("timeslot"), dict) else None)
    dropoff = _warehouse_label(_dropoff_node(order))
    is_crossdock = any(bool(s.get("is_crossdock")) for s in supplies)
    supply_ids = supply_ids_from_order(order)
    # Cross-dock payloads often omit storage_warehouse names; drop-off hub is still useful as transit.
    return {
        "order_id": _as_int(order.get("order_id")),
        "order_number": str(order.get("order_number") or "").strip(),
        "state": str(order.get("state") or "").strip(),
        "created_date": str(order.get("created_date") or "").strip(),
        "dropoff_warehouse": dropoff,
        "destination_warehouses": warehouses,
        "supplies_count": len(supplies),
        "supply_ids": supply_ids,
        "is_crossdock": is_crossdock,
        "slot_date": slot_date,
        "slot_time": slot_time,
        "gm_count": 0,
        "gm_barcodes": [],
    }


def _bundle_item_row(item: dict) -> dict[str, Any]:
    qty = _as_int(item.get("quantity")) or 0
    quant = _as_int(item.get("quant")) or 0
    return {
        "offer_id": str(item.get("offer_id") or item.get("contractor_item_code") or "").strip(),
        "name": str(item.get("name") or "").strip(),
        "barcode": str(item.get("barcode") or "").strip(),
        "ozon_sku": _as_int(item.get("sku")),
        "quantity": max(qty, 0),
        "quant": max(quant, 0),
    }


def _bundle_quantity_map(items: list[dict] | None) -> dict[tuple[str, str], int]:
    """Aggregate an Ozon composition for reliable source comparison."""
    quantities: dict[tuple[str, str], int] = {}
    for raw_item in items or []:
        item = _bundle_item_row(raw_item)
        key = (
            str(item.get("offer_id") or "").strip().lower(),
            str(item.get("barcode") or "").strip().lower(),
        )
        quantities[key] = int(quantities.get(key) or 0) + max(
            int(item.get("quantity") or 0),
            0,
        )
    return quantities


def _pool_for_ozon_item(
    *,
    barcode: str,
    offer_id: str,
    stock_rows: list[dict],
    agency=None,
) -> tuple[list[dict], str]:
    """Find picker rows for one Ozon product without changing its barcode.

    Ozon barcodes are compared case-insensitively because the marketplace sends
    ``OZN...`` while older Fullbox rows may contain ``ozn...``.  An exact
    barcode match is authoritative: rows with another barcode must never be
    merged merely because they share the same offer/SKU.  In particular this
    prevents processed OZN stock from being silently completed with raw stock.
    """
    from .client_template import _norm_barcode, _rows_for_barcode

    clean_barcode = _norm_barcode(barcode)
    stock_rows = list(stock_rows or [])
    barcode_pool: list[dict] = []
    if clean_barcode:
        barcode_pool = _rows_for_barcode(clean_barcode, stock_rows)
        barcode_key = clean_barcode.casefold()
        barcode_pool_keys = {str(row.get("key") or "") for row in barcode_pool}
        for row in stock_rows:
            row_key = str(row.get("key") or "")
            row_barcode = _norm_barcode(row.get("barcode"))
            if (
                row.get("is_mixed_box")
                or not row_barcode
                or row_barcode.casefold() != barcode_key
                or row_key in barcode_pool_keys
            ):
                continue
            barcode_pool.append(row)
            barcode_pool_keys.add(row_key)

    if barcode_pool:
        return barcode_pool, clean_barcode

    # OZN is a marketplace barcode assigned to the processed unit.  Falling
    # back to the client's ordinary barcode would put unprocessed goods into an
    # Ozon shipment.  Report the exact OZN stock as missing instead.
    if clean_barcode.casefold().startswith("ozn"):
        return [], clean_barcode

    offer = str(offer_id or "").strip()
    offer_pool: list[dict] = []
    if offer:
        offer_pool = [
            row
            for row in stock_rows
            if str(row.get("sku_code") or "").strip().lower() == offer.lower()
            and not row.get("is_mixed_box")
        ]
        if offer_pool:
            return offer_pool, clean_barcode or _norm_barcode(offer_pool[0].get("barcode"))

    if offer:
        # Catalog aliases: Ozon ШК / артикул → другие ШК того же SKU на остатке.
        if agency is not None:
            from sku.models import SKU, SKUBarcode

            sku_ids = list(
                SKU.objects.filter(agency=agency, deleted=False, sku_code__iexact=offer).values_list(
                    "id", flat=True
                )[:20]
            )
            if clean_barcode:
                sku_ids.extend(
                    list(
                        SKUBarcode.objects.filter(
                            sku__agency=agency,
                            value__iexact=clean_barcode,
                        ).values_list(
                            "sku_id", flat=True
                        )[:20]
                    )
                )
            sku_ids = list(dict.fromkeys(int(x) for x in sku_ids if x))
            if sku_ids:
                pool = [
                    row
                    for row in stock_rows
                    if int(row.get("sku_id") or 0) in sku_ids
                    and not row.get("is_mixed_box")
                ]
                if pool:
                    return pool, clean_barcode or _norm_barcode(pool[0].get("barcode"))

                alt_barcodes = {
                    _norm_barcode(value).casefold()
                    for value in SKUBarcode.objects.filter(sku_id__in=sku_ids)
                    .exclude(value="")
                    .values_list("value", flat=True)[:50]
                    if _norm_barcode(value)
                }
                pool = [
                    row
                    for row in stock_rows
                    if _norm_barcode(row.get("barcode")).casefold() in alt_barcodes
                    and not row.get("is_mixed_box")
                ]
                if pool:
                    return pool, clean_barcode or _norm_barcode(pool[0].get("barcode"))
    return [], clean_barcode


def _ozon_piece_pick_split(
    *,
    pool: list[dict],
    remaining_qty: int,
    already_selected: dict[str, int],
    excluded_row_keys: set[str],
) -> dict[str, Any] | None:
    """Build a read-only proposal for picking a remainder from one physical box.

    The proposal uses the existing client-form partial-box contract. It does not
    reserve, move or mutate warehouse stock. Mixed boxes are deliberately left
    to the existing manual flow because their composition contains several SKUs.
    """
    pick_qty = max(int(remaining_qty or 0), 0)
    if pick_qty <= 0:
        return None

    candidates: list[tuple[int, int, str, dict]] = []
    for row in pool or []:
        key = str(row.get("key") or "").strip()
        box_qty = max(int(row.get("box_qty") or 0), 0)
        split_available_value = row.get("split_available_qty")
        split_available_qty = max(
            int(box_qty if split_available_value is None else split_available_value),
            0,
        )
        selected_boxes = max(int(already_selected.get(key, 0) or 0), 0)
        available_boxes = max(int(row.get("available_boxes") or 0), 0) - selected_boxes
        box_codes = [str(code or "").strip() for code in (row.get("box_codes") or []) if str(code or "").strip()]
        if (
            not key
            or key in excluded_row_keys
            or row.get("is_mixed_box")
            or box_qty <= pick_qty
            or split_available_qty < pick_qty
            or available_boxes <= 0
            or len(box_codes) <= selected_boxes
        ):
            continue
        candidates.append((box_qty, -available_boxes, key, row))

    if not candidates:
        return None

    box_qty, _negative_available, key, _row = sorted(candidates, key=lambda item: item[:3])[0]
    return {
        "identity": f"key:{key}",
        "group_key": f"partial:{key}",
        "row_key": key,
        "group": "",
        "boxes": 1,
        "items": [{"key": key, "qty": pick_qty}],
        "piece_qty": pick_qty,
        "source_box_qty": box_qty,
    }


def _consume_open_ozon_remainders(
    *,
    pool: list[dict],
    requested_qty: int,
    already_selected: dict[str, int],
) -> tuple[dict[str, int], list[dict[str, Any]], int]:
    """Use opened box remainders before untouched full boxes.

    A fully available remainder is represented as a regular picker row whose
    box size is its current physical quantity. A box that is only partly free
    because another quantity has already been picked is represented by a
    ``partial_only`` row and must always use the piece-pick contract.

    This helper is read-only. It returns provisional claims; the caller adds
    them to the shared per-request occupancy before selecting full boxes.
    """
    remaining = max(int(requested_qty or 0), 0)
    if remaining <= 0:
        return {}, [], 0

    candidates: list[tuple[int, str, dict]] = []
    for row in pool or []:
        key = str(row.get("key") or "").strip()
        if not key or row.get("is_mixed_box") or not row.get("is_open_remainder"):
            continue
        box_qty = max(int(row.get("box_qty") or 0), 0)
        split_available_value = row.get("split_available_qty")
        effective_qty = max(
            int(box_qty if split_available_value is None else split_available_value),
            0,
        )
        if not row.get("partial_only"):
            effective_qty = box_qty
        if effective_qty <= 0:
            continue
        candidates.append((effective_qty, key, row))

    whole_boxes: dict[str, int] = {}
    piece_splits: list[dict[str, Any]] = []
    piece_qty = 0
    local_selected: dict[str, int] = {}
    for effective_qty, key, row in sorted(candidates, key=lambda item: item[:2]):
        if remaining <= 0:
            break
        available_boxes = max(int(row.get("available_boxes") or 0), 0)
        open_boxes = max(int(row.get("open_remainder_boxes") or 0), 0)
        available_boxes = min(available_boxes, open_boxes or available_boxes)
        available_boxes -= max(int(already_selected.get(key, 0) or 0), 0)
        available_boxes -= max(int(local_selected.get(key, 0) or 0), 0)
        box_codes = [
            str(code or "").strip()
            for code in row.get("box_codes") or []
            if str(code or "").strip()
        ]
        unused_box_codes = max(
            len(box_codes) - int(already_selected.get(key, 0) or 0),
            0,
        )
        available_boxes = min(available_boxes, unused_box_codes)
        if available_boxes <= 0:
            continue

        for _index in range(available_boxes):
            if remaining <= 0:
                break
            if not row.get("partial_only") and remaining >= effective_qty:
                whole_boxes[key] = int(whole_boxes.get(key, 0)) + 1
                local_selected[key] = int(local_selected.get(key, 0)) + 1
                remaining -= effective_qty
                continue

            pick_qty = min(remaining, effective_qty)
            if pick_qty <= 0:
                break
            split_index = len(piece_splits) + 1
            piece_splits.append(
                {
                    "identity": f"key:{key}",
                    "group_key": f"partial:open:{key}:{split_index}",
                    "row_key": key,
                    "group": "",
                    "boxes": 1,
                    "items": [{"key": key, "qty": pick_qty}],
                    "piece_qty": pick_qty,
                    "source_box_qty": max(int(row.get("box_qty") or 0), 0),
                }
            )
            local_selected[key] = int(local_selected.get(key, 0)) + 1
            piece_qty += pick_qty
            remaining -= pick_qty

    return whole_boxes, piece_splits, piece_qty


def apply_ozon_bundle_to_stock(
    bundle_items: list[dict],
    stock_rows: list[dict],
    *,
    agency=None,
    already_selected: dict[str, int] | None = None,
) -> dict[str, Any]:
    """Map Ozon composition (ШК + шт) onto Fullbox box picker, like Excel import."""
    from .client_template import _allocate_pieces_across_pack_rows

    shared_selected = already_selected if already_selected is not None else {}
    selected: dict[str, int] = {}
    stock_by_key = {str(row.get("key") or ""): row for row in stock_rows or []}
    composition: list[dict[str, Any]] = []
    suggestions: list[dict[str, Any]] = []
    discrepancies: list[str] = []
    partial_box_splits: list[dict[str, Any]] = []
    partial_source_keys: set[str] = set()
    piece_pick_total_qty = 0

    for idx, item in enumerate(bundle_items or [], start=1):
        offer = str(item.get("offer_id") or "").strip()
        barcode = str(item.get("barcode") or "").strip()
        name = str(item.get("name") or "").strip()
        qty = int(item.get("quantity") or 0)
        composition_row = {
            "offer_id": offer,
            "barcode": barcode,
            "name": name,
            "quantity": max(qty, 0),
            "ozon_sku": item.get("ozon_sku"),
            "matched": False,
            "applied_boxes": 0,
            "applied_qty": 0,
            "status": "pending",
        }
        if qty <= 0:
            composition_row["status"] = "empty"
            composition.append(composition_row)
            continue

        pool, match_barcode = _pool_for_ozon_item(
            barcode=barcode,
            offer_id=offer,
            stock_rows=stock_rows or [],
            agency=agency,
        )
        if not pool:
            composition_row["status"] = "not_in_stock"
            composition_row["reason"] = "нет свободных коробов на складе"
            composition.append(composition_row)
            suggestions.append(
                {
                    "offer_id": offer,
                    "barcode": barcode,
                    "quantity": qty,
                    "matched": False,
                    "reason": composition_row["reason"],
                }
            )
            discrepancies.append(
                f"{offer or barcode or 'позиция'}: в Ozon {qty} шт (ШК {barcode or '—'}) — "
                f"на складе Fullbox сейчас нет свободных коробов."
            )
            continue

        # Never open another box when the requested quantity can already be
        # taken as an exact combination of physical Fullbox boxes.  The old
        # order (opened remainders first, exact search second) could select two
        # 60-piece remnants and then split a 180-piece box for a 180-piece
        # Ozon line.  Final server validation correctly rejected that plan as
        # an unnecessary split, so preview and submit disagreed.
        exact_addition = _allocate_exact_ozon_boxes(
            qty=qty,
            pool=pool,
            already_selected=shared_selected,
        )
        if exact_addition is not None:
            addition = exact_addition
            open_piece_splits: list[dict[str, Any]] = []
            open_piece_qty = 0
        else:
            addition, open_piece_splits, open_piece_qty = _consume_open_ozon_remainders(
                pool=pool,
                requested_qty=qty,
                already_selected=shared_selected,
            )
        for key, boxes in addition.items():
            shared_selected[key] = int(shared_selected.get(key, 0)) + int(boxes)
        for split in open_piece_splits:
            split_key = str(split.get("row_key") or "").strip()
            split_boxes = max(int(split.get("boxes") or 0), 0)
            if split_key and split_boxes > 0:
                shared_selected[split_key] = int(shared_selected.get(split_key, 0)) + split_boxes
                partial_source_keys.add(split_key)
            partial_box_splits.append(split)
        piece_pick_total_qty += open_piece_qty

        open_whole_qty = sum(
            int(boxes) * max(int((stock_by_key.get(key) or {}).get("box_qty") or 0), 0)
            for key, boxes in addition.items()
        )
        remaining_after_open = max(qty - open_whole_qty - open_piece_qty, 0)
        extra_addition: dict[str, int] | None = {}
        line_disc: list[str] = []
        if remaining_after_open > 0:
            extra_addition = _allocate_exact_ozon_boxes(
                qty=remaining_after_open,
                pool=pool,
                already_selected=shared_selected,
            )
        if extra_addition is None:
            extra_addition, line_disc, _applied = _allocate_pieces_across_pack_rows(
                identity=match_barcode or barcode or offer,
                qty=remaining_after_open,
                pool=pool,
                row_no=idx,
                already_selected=shared_selected,
            )
        for key, boxes in (extra_addition or {}).items():
            addition[key] = int(addition.get(key, 0)) + int(boxes)
            shared_selected[key] = int(shared_selected.get(key, 0)) + int(boxes)
        applied_boxes = 0
        applied_qty = 0
        for key, boxes in addition.items():
            selected[key] = int(selected.get(key, 0)) + int(boxes)
            row = stock_by_key.get(key) or {}
            box_qty = max(int(row.get("box_qty") or 0), 0)
            applied_boxes += int(boxes)
            applied_qty += int(boxes) * box_qty
            suggestions.append(
                {
                    "offer_id": offer,
                    "barcode": match_barcode or barcode,
                    "quantity": qty,
                    "matched": True,
                    "stock_key": key,
                    "sku_code": row.get("sku_code"),
                    "boxes": int(boxes),
                    "box_qty": box_qty,
                    "applied_qty": int(boxes) * box_qty,
                }
            )
        whole_boxes = applied_boxes
        whole_box_qty = applied_qty
        remaining_qty = max(qty - whole_box_qty - open_piece_qty, 0)
        piece_split = _ozon_piece_pick_split(
            pool=pool,
            remaining_qty=remaining_qty,
            already_selected=shared_selected,
            excluded_row_keys=partial_source_keys,
        )
        piece_pick_qty = open_piece_qty
        if piece_split:
            split_key = str(piece_split.get("row_key") or "").strip()
            final_piece_pick_qty = max(int(piece_split.get("piece_qty") or 0), 0)
            shared_selected[split_key] = int(shared_selected.get(split_key, 0)) + 1
            partial_source_keys.add(split_key)
            partial_box_splits.append(piece_split)
            piece_pick_total_qty += final_piece_pick_qty
            piece_pick_qty += final_piece_pick_qty

        total_applied_qty = whole_box_qty + piece_pick_qty
        total_source_boxes = whole_boxes + len(open_piece_splits) + (1 if piece_split else 0)
        remaining_qty = max(qty - total_applied_qty, 0)
        exact_match = total_applied_qty == qty
        composition_row["matched"] = exact_match
        composition_row["whole_boxes"] = whole_boxes
        composition_row["whole_box_qty"] = whole_box_qty
        composition_row["piece_pick_qty"] = piece_pick_qty
        composition_row["applied_boxes"] = total_source_boxes
        composition_row["applied_qty"] = total_applied_qty
        composition_row["remaining_qty"] = remaining_qty
        composition_row["status"] = (
            ("matched_with_piece_pick" if piece_pick_qty else "matched")
            if exact_match
            else ("needs_split" if applied_qty > 0 or pool else "not_in_stock")
        )
        if piece_pick_qty:
            composition_row["reason"] = (
                f"целыми коробами {whole_box_qty} шт.; поштучный добор {piece_pick_qty} шт."
            )
        elif not exact_match:
            discrepancies.extend(line_disc)
            composition_row["reason"] = (
                f"нужно добрать поштучно: {remaining_qty} шт."
                if remaining_qty > 0
                else "количество не кратно целым коробам"
            )
            discrepancies.append(
                f"{offer or barcode or 'позиция'}: требуется {qty} шт., "
                f"целыми коробами подобрано {applied_qty} шт.; "
                f"поштучно нужно добрать {remaining_qty} шт."
            )
        composition_row["barcode"] = match_barcode or barcode
        composition.append(composition_row)

    return {
        "composition": composition,
        "stock_suggestions": suggestions,
        "selected_boxes": {key: str(boxes) for key, boxes in selected.items() if boxes > 0},
        "partial_box_splits": partial_box_splits,
        "piece_pick_total_qty": piece_pick_total_qty,
        "piece_pick_positions": len(partial_box_splits),
        "requires_piece_pick_confirmation": bool(partial_box_splits),
        "discrepancies": discrepancies,
    }


def _allocate_exact_ozon_boxes(
    *,
    qty: int,
    pool: list[dict],
    already_selected: dict[str, int],
) -> dict[str, int] | None:
    """Find an exact whole-box combination without changing warehouse data.

    Ozon can request a quantity that is achievable only by mixing box
    multiplicities. The older greedy allocator could miss such a combination.
    This bounded subset-sum is intentionally limited; large/unusual requests
    safely fall back to the existing allocator and are marked for piece picking.
    """
    target = max(int(qty or 0), 0)
    if target <= 0 or target > 200_000:
        return None

    chunks: list[tuple[str, int, int]] = []
    for row in pool or []:
        key = str(row.get("key") or "").strip()
        box_qty = max(int(row.get("box_qty") or 0), 0)
        available = max(int(row.get("available_boxes") or 0), 0)
        box_codes = [
            str(code or "").strip()
            for code in row.get("box_codes") or []
            if str(code or "").strip()
        ]
        # Every proposed box must resolve to one concrete physical code.  Do
        # not let a stale aggregate availability counter create a selection
        # that the write path cannot reserve later.
        if box_codes:
            available = min(available, len(box_codes))
        available -= max(int(already_selected.get(key, 0) or 0), 0)
        # Opened-box remainders expose the original pack size in ``box_qty``
        # but contain only ``split_available_qty`` real pieces. They may be
        # used by the piece-pick path below, never as exact whole boxes.
        if not key or row.get("partial_only") or box_qty <= 0 or available <= 0:
            continue
        step = 1
        left = available
        while left > 0:
            take = min(step, left)
            chunks.append((key, take, take * box_qty))
            left -= take
            step *= 2

    states: dict[int, dict[str, int]] = {0: {}}
    for key, boxes, value in chunks:
        for subtotal, allocation in list(states.items())[::-1]:
            candidate = subtotal + value
            if candidate > target or candidate in states:
                continue
            next_allocation = dict(allocation)
            next_allocation[key] = int(next_allocation.get(key, 0)) + boxes
            states[candidate] = next_allocation
        if target in states:
            return states[target]
    return None


def match_bundle_to_stock(
    bundle_items: list[dict],
    stock_rows: list[dict],
    *,
    agency=None,
) -> list[dict[str, Any]]:
    """Backward-compatible wrapper around apply_ozon_bundle_to_stock suggestions."""
    return apply_ozon_bundle_to_stock(bundle_items, stock_rows, agency=agency)["stock_suggestions"]


def _ozon_composition_identity(item: dict) -> tuple[str, str]:
    offer = str(item.get("offer_id") or "").strip().casefold()
    if offer:
        return ("offer", offer)
    barcode = str(item.get("barcode") or "").strip().casefold()
    if barcode:
        return ("barcode", barcode)
    ozon_sku = str(item.get("ozon_sku") or item.get("sku") or "").strip()
    return ("ozon_sku", ozon_sku)


def rebalance_ozon_supply_payloads(
    payloads: list[dict[str, Any]],
    stock_rows: list[dict],
    *,
    agency=None,
) -> list[dict[str, Any]]:
    """Allocate one shared Fullbox stock plan for all selected Ozon supplies.

    Each Ozon supply describes its own quantities, while the client creates one
    combined Fullbox request.  Allocating supply-by-supply can unnecessarily
    split a source box (for example 90 + 90 from one 180-piece box) and can also
    make preview disagree with final validation.  Aggregate by product first,
    run the normal read-only allocator once, then project the result back onto
    each supply for status display.  Only the first payload carries the shared
    physical-box selection so downstream combiners cannot double-count it.
    """
    rows = [dict(payload) for payload in payloads or [] if isinstance(payload, dict)]
    if not rows:
        return []

    aggregate_by_identity: dict[tuple[str, str], dict[str, Any]] = {}
    source_rows: list[list[tuple[tuple[str, str], dict[str, Any]]]] = []
    for payload in rows:
        raw_items = payload.get("composition")
        if not isinstance(raw_items, list) or not raw_items:
            raw_items = payload.get("items")
        per_payload: list[tuple[tuple[str, str], dict[str, Any]]] = []
        for raw_item in raw_items or []:
            if not isinstance(raw_item, dict):
                continue
            quantity = max(_as_int(raw_item.get("quantity")) or 0, 0)
            identity = _ozon_composition_identity(raw_item)
            if quantity <= 0 or not identity[1]:
                continue
            item = dict(raw_item)
            item["quantity"] = quantity
            current = aggregate_by_identity.get(identity)
            if current is None:
                aggregate_by_identity[identity] = {
                    "offer_id": str(item.get("offer_id") or "").strip(),
                    "name": str(item.get("name") or "").strip(),
                    "barcode": str(item.get("barcode") or "").strip(),
                    "ozon_sku": item.get("ozon_sku") or item.get("sku"),
                    "quantity": quantity,
                }
            else:
                current["quantity"] = int(current.get("quantity") or 0) + quantity
            per_payload.append((identity, item))
        source_rows.append(per_payload)

    # Preserve the original payloads when Ozon genuinely supplied no product
    # rows.  Callers will keep the explicit ``empty`` status and block submit.
    if not aggregate_by_identity:
        return rows
    if not any(
        str(row.get("key") or "").strip()
        and max(_as_int(row.get("box_qty")) or 0, 0) > 0
        for row in stock_rows or []
        if isinstance(row, dict)
    ):
        return rows

    allocation = apply_ozon_bundle_to_stock(
        list(aggregate_by_identity.values()),
        stock_rows or [],
        agency=agency,
        already_selected={},
    )
    allocated_by_identity = {
        _ozon_composition_identity(item): item
        for item in allocation.get("composition") or []
        if isinstance(item, dict)
    }
    remaining: dict[tuple[str, str], dict[str, int]] = {}
    for identity, demand in aggregate_by_identity.items():
        allocated = allocated_by_identity.get(identity) or {}
        remaining[identity] = {
            "applied": max(_as_int(allocated.get("applied_qty")) or 0, 0),
            "whole": max(_as_int(allocated.get("whole_box_qty")) or 0, 0),
            "piece": max(_as_int(allocated.get("piece_pick_qty")) or 0, 0),
            "boxes": max(_as_int(allocated.get("applied_boxes")) or 0, 0),
            "demand": max(_as_int(demand.get("quantity")) or 0, 0),
        }

    rebalanced: list[dict[str, Any]] = []
    for payload_index, payload in enumerate(rows):
        composition: list[dict[str, Any]] = []
        for identity, source in source_rows[payload_index]:
            counters = remaining.get(identity) or {
                "applied": 0,
                "whole": 0,
                "piece": 0,
                "boxes": 0,
                "demand": 0,
            }
            quantity = max(_as_int(source.get("quantity")) or 0, 0)
            applied_qty = min(quantity, counters["applied"])
            whole_qty = min(applied_qty, counters["whole"])
            piece_qty = min(max(applied_qty - whole_qty, 0), counters["piece"])
            applied_boxes = counters["boxes"]
            counters["applied"] -= applied_qty
            counters["whole"] -= whole_qty
            counters["piece"] -= piece_qty
            counters["boxes"] = 0

            projected = dict(source)
            projected["quantity"] = quantity
            projected["whole_box_qty"] = whole_qty
            projected["piece_pick_qty"] = piece_qty
            projected["applied_qty"] = applied_qty
            projected["applied_boxes"] = applied_boxes
            projected["remaining_qty"] = max(quantity - applied_qty, 0)
            projected["matched"] = bool(quantity > 0 and applied_qty == quantity)
            if projected["matched"]:
                projected["status"] = "matched_with_piece_pick" if piece_qty else "matched"
            elif applied_qty > 0:
                projected["status"] = "needs_split"
                projected["reason"] = f"не хватает {quantity - applied_qty} шт."
            else:
                projected["status"] = "not_in_stock"
                projected["reason"] = "нет свободных коробов на складе"
            composition.append(projected)

        next_payload = dict(payload)
        next_payload["composition"] = composition
        next_payload["stock_match"] = _stock_match_summary(composition)
        next_payload["selected_boxes"] = {}
        next_payload["partial_box_splits"] = []
        next_payload["piece_pick_total_qty"] = 0
        next_payload["piece_pick_positions"] = 0
        next_payload["requires_piece_pick_confirmation"] = False
        next_payload["stock_suggestions"] = []
        next_payload["discrepancies"] = []
        rebalanced.append(next_payload)

    first = rebalanced[0]
    first["selected_boxes"] = dict(allocation.get("selected_boxes") or {})
    first["partial_box_splits"] = list(allocation.get("partial_box_splits") or [])
    first["piece_pick_total_qty"] = max(
        _as_int(allocation.get("piece_pick_total_qty")) or 0,
        0,
    )
    first["piece_pick_positions"] = max(
        _as_int(allocation.get("piece_pick_positions")) or 0,
        0,
    )
    first["requires_piece_pick_confirmation"] = bool(
        allocation.get("requires_piece_pick_confirmation")
    )
    first["stock_suggestions"] = list(allocation.get("stock_suggestions") or [])
    first["discrepancies"] = list(allocation.get("discrepancies") or [])
    return rebalanced


def build_form_payload_from_order(
    *,
    order: dict,
    details: dict | None,
    bundle_items: list[dict],
    stock_rows: list[dict] | None = None,
    agency=None,
    gm_cargoes: list[dict] | None = None,
    already_selected: dict[str, int] | None = None,
) -> dict[str, Any]:
    summary = _summarize_order(order)
    supplies = [s for s in (order.get("supplies") or []) if isinstance(s, dict)]
    destinations = []
    for supply in supplies:
        destinations.append(
            {
                "supply_id": _as_int(supply.get("supply_id")),
                "bundle_id": str(supply.get("bundle_id") or "").strip(),
                "warehouse": _warehouse_label(supply.get("storage_warehouse")),
                "is_crossdock": bool(supply.get("is_crossdock")),
                "state": str(supply.get("state") or "").strip(),
            }
        )

    vehicle = {}
    if isinstance(details, dict):
        vehicle_node = details.get("vehicle")
        if isinstance(vehicle_node, dict):
            value = vehicle_node.get("value") if isinstance(vehicle_node.get("value"), dict) else vehicle_node
            if isinstance(value, dict):
                vehicle = {
                    "driver_phone": str(value.get("driver_phone") or "").strip(),
                    "vehicle_number": str(value.get("vehicle_number") or "").strip(),
                    "vehicle_model": str(value.get("vehicle_model") or "").strip(),
                    "driver_name": str(value.get("driver_name") or "").strip(),
                }
        # Prefer details timeslot if get() lacked one.
        if not summary["slot_date"]:
            d_slot, d_time = _timeslot_parts(
                details.get("timeslot") if isinstance(details.get("timeslot"), dict) else None
            )
            summary["slot_date"] = d_slot
            summary["slot_time"] = d_time

    destination_warehouses = summary["destination_warehouses"]
    primary_destination = destination_warehouses[0] if destination_warehouses else ""
    dropoff = summary["dropoff_warehouse"]
    is_multi = len(destination_warehouses) > 1
    is_crossdock = bool(summary["is_crossdock"])
    # Cross-dock / multi-hub: drop-off is the transit hub.  Ozon can return
    # ``storage_warehouse=null`` for an otherwise ready cross-dock order.  In
    # that case use the exact Ozon drop-off value in the visible destination
    # field as a safe fallback instead of leaving the client with an empty
    # mandatory-looking field.  Keep ``destination_warehouses`` unchanged so
    # downstream code can still distinguish a real final warehouse from the
    # receiving-point fallback.
    use_transit = bool(dropoff) and (is_crossdock or is_multi)
    if is_multi:
        destination_value = ""
    elif primary_destination:
        destination_value = primary_destination
    elif dropoff:
        destination_value = dropoff
    else:
        destination_value = ""

    normalized_items = [_bundle_item_row(x) for x in bundle_items]
    stock_apply = apply_ozon_bundle_to_stock(
        normalized_items,
        stock_rows or [],
        agency=agency,
        already_selected=already_selected,
    )
    stock_suggestions = stock_apply["stock_suggestions"]
    composition = stock_apply["composition"]
    gm_cargoes = list(gm_cargoes or [])
    gm_item_meta = _gm_item_box_meta(gm_cargoes)
    if gm_item_meta:
        for row in composition:
            key = _gm_item_key(
                {
                    "offer_id": row.get("offer_id"),
                    "barcode": row.get("barcode"),
                }
            )
            meta = gm_item_meta.get(key)
            if not meta:
                continue
            row["ozon_boxes"] = int(meta.get("boxes") or 0)
            row["ozon_gm_barcodes"] = list(meta.get("gm_barcodes") or [])
    elif gm_cargoes and len(composition) == 1:
        composition[0]["ozon_boxes"] = len(gm_cargoes)
        composition[0]["ozon_gm_barcodes"] = [
            str(row.get("gm_barcode") or "").strip()
            for row in gm_cargoes
            if str(row.get("gm_barcode") or "").strip()
        ]
    gm_barcodes = [
        str(row.get("gm_barcode") or "").strip()
        for row in gm_cargoes
        if str(row.get("gm_barcode") or "").strip()
    ]
    supply_barcode = gm_barcodes[0] if gm_barcodes else str(summary["order_number"] or "").strip()

    return {
        "order_id": summary["order_id"],
        "order_number": summary["order_number"],
        "state": summary["state"],
        "form": {
            "shipping_barcode": supply_barcode,
            "supply_number": summary["order_number"],
            "slot_date": summary["slot_date"],
            "slot_time": summary["slot_time"],
            "wb_transit_warehouse": use_transit,
            "transit_address": dropoff if use_transit else "",
            "destination_warehouse": destination_value,
            "vehicle_number": vehicle.get("vehicle_number") or "",
            "driver_phone": vehicle.get("driver_phone") or "",
            "eta_date": summary["slot_date"],
            "ozon_gm_comment": (
                f"ШК ГМ Ozon: {', '.join(gm_barcodes)}" if gm_barcodes else ""
            ),
        },
        "destinations": destinations,
        "destination_warehouses": destination_warehouses,
        "is_multi_destination": is_multi,
        "items": normalized_items,
        "composition": composition,
        "gm_cargoes": gm_cargoes,
        "gm_barcodes": gm_barcodes,
        "ozon_boxes_total": len(gm_barcodes),
        "selected_boxes": stock_apply["selected_boxes"],
        "partial_box_splits": stock_apply["partial_box_splits"],
        "piece_pick_total_qty": stock_apply["piece_pick_total_qty"],
        "piece_pick_positions": stock_apply["piece_pick_positions"],
        "requires_piece_pick_confirmation": stock_apply["requires_piece_pick_confirmation"],
        "stock_suggestions": stock_suggestions,
        "discrepancies": stock_apply["discrepancies"],
        "stock_match": _stock_match_summary(composition),
        "hints": _build_hints(
            summary,
            is_multi,
            composition,
            use_transit=use_transit,
            discrepancies=stock_apply["discrepancies"],
            gm_barcodes=gm_barcodes,
        ),
    }


def _stock_match_summary(composition: list[dict] | None) -> dict[str, Any]:
    rows = composition or []
    total = len(rows)
    matched = sum(1 for row in rows if row.get("matched"))
    missing = max(total - matched, 0)
    if total <= 0:
        status = "empty"
    elif missing <= 0:
        status = "full"
    elif matched <= 0:
        status = "none"
    else:
        status = "partial"
    return {
        "total": total,
        "matched": matched,
        "missing": missing,
        "status": status,
    }


def _build_hints(
    summary: dict,
    is_multi: bool,
    composition: list[dict],
    *,
    use_transit: bool = False,
    discrepancies: list[str] | None = None,
    gm_barcodes: list[str] | None = None,
) -> list[str]:
    hints: list[str] = []
    match = _stock_match_summary(composition)
    total_items = int(match["total"])
    matched = int(match["matched"])
    missing = int(match["missing"])
    # Always lead with success of the Ozon pull itself — stock gaps are guidance, not failure.
    if total_items:
        if match["status"] == "full":
            hints.append(
                f"Заявка Ozon подставлена. Состав: {total_items} поз., все подобраны из доступных коробов."
            )
        elif match["status"] == "none":
            hints.append(
                f"Заявка Ozon подставлена (номер, ШК, слот). "
                f"На складе Fullbox пока нет свободных коробов по {total_items} поз. "
                "Товар может быть в резерве другой заявки или в неполном коробе."
            )
        else:
            hints.append(
                f"Заявка Ozon подставлена. На складе подобрано {matched} из {total_items} поз. — "
                f"ещё {missing} без свободных коробов; проверьте резервы или доберите вручную."
            )
    else:
        hints.append("Заявка Ozon подставлена. Состав позиций в ответе Ozon пуст — проверьте в кабинете Ozon.")
    if is_multi:
        names = ", ".join(summary.get("destination_warehouses") or [])
        hints.append(
            "В заявке Ozon несколько конечных складов"
            + (f": {names}." if names else ".")
            + " Номер поставки и транзит подставлены; распределение по складам "
            "загрузите через Excel Ozon или заполните вручную."
        )
    elif summary.get("is_crossdock") and use_transit and not (summary.get("destination_warehouses") or []):
        hints.append(
            "Кросс-док: транзитный хаб подставлен из Ozon. "
            "Отдельный конечный склад в API не пришёл, поэтому точка приёмки Ozon "
            "автоматически подставлена и в поле склада назначения."
        )
    if gm_barcodes:
        hints.append(f"ШК ГМ Ozon: {len(gm_barcodes)} шт. — для наклейки на короба склада.")
    if discrepancies and match["status"] != "full":
        missing_labels = []
        for row in composition or []:
            if row.get("matched"):
                continue
            label = str(row.get("offer_id") or row.get("barcode") or "").strip()
            if label and label not in missing_labels:
                missing_labels.append(label)
            if len(missing_labels) >= 3:
                break
        if missing_labels:
            extra = f" и ещё {missing - len(missing_labels)}" if missing > len(missing_labels) else ""
            hints.append("Нет свободного остатка: " + ", ".join(missing_labels) + extra + ".")
    if not summary.get("slot_date"):
        hints.append("У поставки нет таймслота — укажите дату слота вручную.")
    return hints


def lookup_existing_ozon_supply_shipments(
    agency,
    supply_numbers: list[str] | tuple[str, ...] | set[str],
    *,
    exclude_order_id: int | None = None,
) -> dict[str, dict[str, Any]]:
    """Map Ozon supply_number → existing non-canceled Fullbox shipping order."""
    from .models import ShippingOrder

    cleaned = sorted(
        {
            str(value or "").strip()
            for value in (supply_numbers or [])
            if str(value or "").strip()
        }
    )
    if not agency or not cleaned:
        return {}
    lookup_field = "supply_number"
    try:
        ShippingOrder._meta.get_field(lookup_field)
    except FieldDoesNotExist:
        # Production can briefly run newer Ozon picker code before the Ozon
        # distribution migration is applied. Fall back to the legacy barcode
        # field so the picker still returns JSON instead of HTTP 500.
        lookup_field = "wb_supply_barcode"
    lookup = Q(**{f"{lookup_field}__in": cleaned})
    for supply_number in cleaned:
        lookup |= Q(comment__icontains=supply_number)
    try:
        qs = (
            ShippingOrder.objects.filter(agency=agency)
            .exclude(status=ShippingOrder.STATUS_CANCELED)
            .filter(lookup)
            .order_by("-id")
            .values("id", "number", "status", lookup_field, "comment")
        )
        if exclude_order_id:
            qs = qs.exclude(pk=int(exclude_order_id))
    except FieldError:
        logger.exception("Ozon existing-shipping lookup failed for field=%s", lookup_field)
        return {}
    status_labels = dict(ShippingOrder.STATUS_CHOICES)
    found: dict[str, dict[str, Any]] = {}
    for row in qs:
        row_supply_numbers = [str(row.get(lookup_field) or "").strip()]
        for line in str(row.get("comment") or "").splitlines():
            text = line.strip()
            for label in ("Номер поставки Ozon:", "Номера поставок Ozon:"):
                if not text.startswith(label):
                    continue
                row_supply_numbers.extend(
                    part.strip()
                    for part in re.split(r"[,;]", text[len(label):])
                    if part.strip()
                )
        row_supply_numbers = list(dict.fromkeys(row_supply_numbers))
        status = str(row.get("status") or "").strip()
        for key in row_supply_numbers:
            if not key or key not in cleaned or key in found:
                continue
            found[key] = {
                "id": int(row["id"]),
                "number": str(row.get("number") or "").strip(),
                "status": status,
                "status_label": status_labels.get(status, status),
            }
    return found


def attach_existing_shipping_to_ozon_orders(
    orders: list[dict[str, Any]],
    existing_by_supply: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    for order in orders or []:
        supply_no = str(order.get("order_number") or "").strip()
        hit = existing_by_supply.get(supply_no) if supply_no else None
        order["already_uploaded"] = bool(hit)
        order["existing_shipping"] = hit
    return orders


def annotate_payload_existing_shipping(
    payload: dict[str, Any],
    agency,
    *,
    exclude_order_id: int | None = None,
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return payload
    supply_no = str(
        (payload.get("form") or {}).get("supply_number")
        or payload.get("order_number")
        or ""
    ).strip()
    existing = lookup_existing_ozon_supply_shipments(
        agency,
        [supply_no] if supply_no else [],
        exclude_order_id=exclude_order_id,
    )
    hit = existing.get(supply_no) if supply_no else None
    payload["already_uploaded"] = bool(hit)
    payload["existing_shipping"] = hit
    if hit:
        label = hit.get("number") or f"#{hit.get('id')}"
        status_label = hit.get("status_label") or hit.get("status") or ""
        warning = (
            f"Внимание: поставка Ozon уже загружена в заявку Fullbox {label}"
            + (f" ({status_label})" if status_label else "")
            + ". Не создавайте дубликат — откройте существующую заявку или выберите другую поставку."
        )
        hints = list(payload.get("hints") or [])
        hints.insert(0, warning)
        payload["hints"] = hints
    return payload


def list_ozon_supplies_for_agency(
    agency,
    *,
    limit: int = 40,
    exclude_order_id: int | None = None,
) -> dict[str, Any]:
    credentials, credentials_error = resolve_ozon_credentials(agency)
    if credentials_error:
        return {"ok": False, "error": credentials_error, "orders": []}

    summaries: list[dict[str, Any]] = []
    account_errors: list[str] = []
    successful_accounts = 0
    for credential in credentials:
        client_id = credential.client_id
        api_key = credential.api_key
        order_ids, error = list_ozon_supply_order_ids(client_id, api_key, limit=limit)
        if error:
            account_errors.append(f"{credential.label}: {error}")
            continue
        if not order_ids:
            successful_accounts += 1
            continue
        orders, error = fetch_ozon_supply_orders(client_id, api_key, order_ids)
        if error:
            account_errors.append(f"{credential.label}: {error}")
            continue
        successful_accounts += 1

        account_summaries = [_summarize_order(order) for order in orders]
        all_supply_ids: list[str] = []
        for summary in account_summaries:
            raw_order_id = _as_int(summary.get("order_id")) or 0
            summary["ozon_order_id"] = raw_order_id
            summary["order_id"] = _encode_ozon_selection_id(
                raw_order_id,
                credential.slot,
            )
            summary["ozon_client_id"] = client_id
            summary["ozon_credential_slot"] = credential.slot
            summary["ozon_account_label"] = credential.label
            for supply_id in summary.get("supply_ids") or []:
                value = str(supply_id)
                if value not in all_supply_ids:
                    all_supply_ids.append(value)
        if all_supply_ids:
            gm_rows, gm_error = fetch_ozon_gm_cargoes(
                client_id,
                api_key,
                all_supply_ids,
            )
            if gm_error:
                logger.info(
                    "Ozon GM list enrich skipped for agency=%s slot=%s: %s",
                    getattr(agency, "id", None),
                    credential.slot,
                    gm_error,
                )
                gm_rows = []
            by_supply: dict[str, list[str]] = {}
            for row in gm_rows:
                supply_id = str(row.get("supply_id") or "").strip()
                code = str(row.get("gm_barcode") or "").strip()
                if not supply_id or not code:
                    continue
                bucket = by_supply.setdefault(supply_id, [])
                if code not in bucket:
                    bucket.append(code)
            for summary in account_summaries:
                codes: list[str] = []
                for supply_id in summary.get("supply_ids") or []:
                    for code in by_supply.get(str(supply_id), []):
                        if code not in codes:
                            codes.append(code)
                summary["gm_barcodes"] = codes
                summary["gm_count"] = len(codes)
        summaries.extend(account_summaries)

    if not successful_accounts and account_errors:
        return {"ok": False, "error": " ".join(account_errors), "orders": []}
    if not summaries:
        result = {
            "ok": True,
            "orders": [],
            "message": "Активных заявок на отгрузку в Ozon не найдено.",
        }
        if account_errors:
            result["warnings"] = account_errors
        return result
    summaries.sort(
        key=lambda row: (
            int(row.get("gm_count") or 0),
            row.get("slot_date") or "",
            row.get("order_number") or "",
        ),
        reverse=True,
    )
    existing = lookup_existing_ozon_supply_shipments(
        agency,
        [str(row.get("order_number") or "") for row in summaries],
        exclude_order_id=exclude_order_id,
    )
    attach_existing_shipping_to_ozon_orders(summaries, existing)
    result = {"ok": True, "orders": summaries}
    if account_errors:
        result["warnings"] = account_errors
    return result


def get_ozon_supply_for_agency(
    agency,
    order_id: int,
    *,
    stock_rows: list[dict] | None = None,
    enrich_gm: bool = False,
    api_timeout: int = 15,
    bundle_max_pages: int = 8,
    include_gm_cargoes: bool = True,
    gm_enrich_limit: int = 4,
    gm_pause_seconds: float = 0.45,
    exclude_order_id: int | None = None,
    already_selected: dict[str, int] | None = None,
) -> dict[str, Any]:
    """Load one Ozon supply into shipping-form payload.

    Detail path must stay under reverse-proxy/gunicorn limits: shorter API
    timeouts, limited bundle pages, no slow per-cargo enrich by default.
    """
    credentials, credentials_error = resolve_ozon_credentials(agency)
    if credentials_error:
        return {"ok": False, "error": credentials_error}
    timeout = max(5, min(int(api_timeout), 40))
    requested_order_id = int(order_id)
    api_order_id, credential_slot_hint = _decode_ozon_selection_id(requested_order_id)
    if credential_slot_hint is not None:
        credentials = [
            credential
            for credential in credentials
            if credential.slot == credential_slot_hint
        ]
        if not credentials:
            return {"ok": False, "error": "Для выбранной заявки не найден кабинет Ozon."}
    selected_credential: OzonCredentialRef | None = None
    order: dict | None = None
    errors: list[str] = []
    for credential in credentials:
        candidate_orders, error = fetch_ozon_supply_orders(
            credential.client_id,
            credential.api_key,
            [api_order_id],
            timeout=timeout,
        )
        if candidate_orders:
            order = candidate_orders[0]
            selected_credential = credential
            break
        if error:
            errors.append(f"{credential.label}: {error}")

        if credential_slot_hint is not None or len(str(api_order_id)) < 11:
            continue
        # UI and users can pass the visible Ozon supply number (2000...), while
        # /v3/supply-order/get expects the internal order_id. Resolve it within
        # every configured cabinet and keep the matching cabinet for all later
        # bundle, cargo and label calls.
        active_ids, list_error = list_ozon_supply_order_ids(
            credential.client_id,
            credential.api_key,
            limit=100,
        )
        if list_error:
            errors.append(f"{credential.label}: {list_error}")
            continue
        if not active_ids:
            continue
        active_orders, active_error = fetch_ozon_supply_orders(
            credential.client_id,
            credential.api_key,
            active_ids,
            timeout=timeout,
        )
        if active_error:
            errors.append(f"{credential.label}: {active_error}")
            continue
        requested_supply_no = str(api_order_id)
        for candidate in active_orders:
            summary = _summarize_order(candidate)
            supply_numbers = {str(value) for value in summary.get("supply_ids") or []}
            if (
                str(summary.get("order_number") or "") == requested_supply_no
                or requested_supply_no in supply_numbers
            ):
                order = candidate
                selected_credential = credential
                break
        if order is not None:
            break

    if order is None or selected_credential is None:
        if len(credentials) == 1 and errors:
            return {"ok": False, "error": errors[-1]}
        return {"ok": False, "error": "Заявка Ozon не найдена."}
    client_id = selected_credential.client_id
    api_key = selected_credential.api_key
    resolved_order_id = _as_int(order.get("order_id")) or api_order_id
    details, details_error = fetch_ozon_supply_order_details(
        client_id,
        api_key,
        resolved_order_id,
        timeout=timeout,
    )
    if details_error:
        logger.info("Ozon supply details skipped for %s: %s", order_id, details_error)
        details = None

    bundle_ids: list[str] = []
    for supply in order.get("supplies") or []:
        if not isinstance(supply, dict):
            continue
        bid = str(supply.get("bundle_id") or "").strip()
        if bid:
            bundle_ids.append(bid)
        content = supply.get("content")
        if isinstance(content, dict):
            cbid = str(content.get("bundle_id") or "").strip()
            if cbid:
                bundle_ids.append(cbid)
    if isinstance(details, dict):
        for supply in details.get("supplies") or []:
            if not isinstance(supply, dict):
                continue
            content = supply.get("content")
            if isinstance(content, dict):
                cbid = str(content.get("bundle_id") or "").strip()
                if cbid:
                    bundle_ids.append(cbid)
    bundle_ids = list(dict.fromkeys(bundle_ids))

    bundle_items, bundle_error = fetch_ozon_supply_bundle_items(
        client_id,
        api_key,
        bundle_ids,
        max_pages=bundle_max_pages,
        timeout=timeout,
    )
    if bundle_error:
        # Bundle is useful but not mandatory: still return form slots / GM barcodes.
        logger.info("Ozon supply bundle skipped for %s: %s", order_id, bundle_error)
        bundle_items = bundle_items or []

    supply_ids = supply_ids_from_order(order, details)
    gm_cargoes = []
    if include_gm_cargoes:
        gm_cargoes, gm_error = fetch_ozon_gm_cargoes(
            client_id,
            api_key,
            supply_ids,
            timeout=timeout,
        )
        if gm_error:
            logger.info("Ozon GM cargoes skipped for %s: %s", order_id, gm_error)
            gm_cargoes = []

    # Ozon has two bundle levels.  The aggregate order bundle may stay stale
    # after Ozon changes the contents of already created cargo places.  The
    # bundle ids attached to every actual GM cargo are therefore authoritative
    # whenever all returned cargoes have a bundle id.  Apart from fixing stale
    # totals, this still preserves the old fallback for an empty order bundle.
    order_bundle_items = list(bundle_items or [])
    composition_reconciliation: dict[str, Any] | None = None
    composition_source = "order_bundle"
    cargo_bundle_ids = list(
        dict.fromkeys(
            str(row.get("bundle_id") or "").strip()
            for row in gm_cargoes
            if str(row.get("bundle_id") or "").strip()
        )
    )
    cargo_bundles_complete = bool(gm_cargoes) and all(
        str(row.get("bundle_id") or "").strip()
        for row in gm_cargoes
    )
    should_fetch_cargo_bundles = bool(cargo_bundle_ids) and (
        bundle_error
        or not bundle_items
        or (
            cargo_bundles_complete
            and set(cargo_bundle_ids) != set(bundle_ids)
        )
    )
    cached_cargoes = hydrate_gm_cargoes_from_cache(client_id, gm_cargoes)
    cached_complete = bool(cached_cargoes) and all(c.get("items") for c in cached_cargoes)
    if should_fetch_cargo_bundles:
        if cached_complete:
            cargo_bundle_items, cargo_bundle_error = flatten_bundle_items_from_gm(cached_cargoes), ""
        else:
            cargo_bundle_items, cargo_bundle_error = fetch_ozon_supply_bundle_items(
                client_id,
                api_key,
                cargo_bundle_ids,
                max_pages=bundle_max_pages,
                timeout=timeout,
            )
        if cargo_bundle_items and not cargo_bundle_error and (cargo_bundles_complete or not bundle_items):
            order_quantities = _bundle_quantity_map(order_bundle_items)
            cargo_quantities = _bundle_quantity_map(cargo_bundle_items)
            if order_quantities and order_quantities != cargo_quantities:
                composition_reconciliation = {
                    "source": "gm_cargoes",
                    "order_bundle_total": sum(order_quantities.values()),
                    "gm_cargo_total": sum(cargo_quantities.values()),
                }
                logger.warning(
                    "Ozon supply composition reconciled by GM cargoes for %s: %s -> %s items",
                    order_id,
                    composition_reconciliation["order_bundle_total"],
                    composition_reconciliation["gm_cargo_total"],
                )
            bundle_items = cargo_bundle_items
            bundle_error = ""
            composition_source = "gm_cargoes"
            logger.info(
                "Ozon supply composition loaded from %s cargo bundle(s) for %s",
                len(cargo_bundle_ids),
                order_id,
            )
        elif cargo_bundle_error and not bundle_error:
            bundle_error = cargo_bundle_error

    # Always reuse previously verified cargo composition.  New API reads are
    # bounded so one large supply cannot exceed the gunicorn request timeout;
    # the client form repeats this call and displays progress.
    gm_cargoes = hydrate_gm_cargoes_from_cache(client_id, gm_cargoes)
    safe_gm_enrich_limit = max(0, min(int(gm_enrich_limit or 0), 20))
    if enrich_gm and safe_gm_enrich_limit > 0 and any(
        not cargo.get("items") for cargo in gm_cargoes
    ):
        gm_cargoes = enrich_gm_cargoes_with_items(
            client_id,
            api_key,
            gm_cargoes,
            pause_seconds=gm_pause_seconds,
            max_fetch=safe_gm_enrich_limit,
            timeout=5, budget_seconds=12,
        )
        if not bundle_items:
            enriched_bundle_items = flatten_bundle_items_from_gm(gm_cargoes)
            if enriched_bundle_items:
                bundle_items = enriched_bundle_items
                bundle_error = ""

    for cargo in gm_cargoes:
        if not isinstance(cargo, dict):
            continue
        cargo["ozon_client_id"] = client_id
        cargo["ozon_credential_slot"] = selected_credential.slot

    payload = build_form_payload_from_order(
        order=order,
        details=details,
        bundle_items=bundle_items,
        stock_rows=stock_rows,
        agency=agency,
        gm_cargoes=gm_cargoes,
        already_selected=already_selected,
    )
    payload["ozon_client_id"] = client_id
    payload["ozon_credential_slot"] = selected_credential.slot
    payload["ozon_account_label"] = selected_credential.label
    payload["ozon_order_id"] = resolved_order_id
    payload["composition_source"] = composition_source
    if composition_reconciliation:
        payload["composition_reconciliation"] = composition_reconciliation
        payload.setdefault("hints", []).insert(
            0,
            "Состав Ozon уточнён по грузоместам: "
            f"{composition_reconciliation['gm_cargo_total']} шт. "
            f"(общий состав API: {composition_reconciliation['order_bundle_total']} шт.).",
        )
    payload["order_id"] = _encode_ozon_selection_id(
        resolved_order_id,
        selected_credential.slot,
    )
    if bundle_error and not bundle_items:
        payload.setdefault("hints", []).insert(
            0,
            "Состав позиций Ozon не загрузился полностью — проверьте вручную или Excel.",
        )
    cache.set(
        _ozon_gm_manifest_key(agency.pk, requested_order_id),
        {"client_id": client_id, "cargoes": gm_cargoes},
        timeout=_OZON_GM_MANIFEST_CACHE_SECONDS,
    )
    gm_items_ready = sum(1 for cargo in gm_cargoes if cargo.get("items"))
    payload["gm_items_ready"] = gm_items_ready
    payload["gm_items_total"] = len(gm_cargoes)
    payload["gm_items_complete"] = bool(gm_cargoes) and gm_items_ready == len(gm_cargoes)
    annotate_payload_existing_shipping(
        payload,
        agency,
        exclude_order_id=exclude_order_id,
    )
    return {"ok": True, **payload}



def _ozon_gm_manifest_key(agency_id, order_id) -> str:
    return f"shipping:ozon:gm-manifest:v1:{int(agency_id)}:{int(order_id)}"


def preload_ozon_gm_for_agency(agency, order_id: int, *, exclude_order_id=None) -> dict:
    """Continue composition loading without repeatedly fetching routes or stock.

    This cache is only a progress aid. Preview and submission still read the
    live Ozon manifest and validate stock via the existing submission service.
    """
    credentials, error = resolve_ozon_credentials(agency)
    if error:
        return {"ok": False, "error": error}
    key = _ozon_gm_manifest_key(agency.pk, order_id)
    manifest = cache.get(key)
    if not isinstance(manifest, dict):
        result = get_ozon_supply_for_agency(
            agency, order_id, stock_rows=[], enrich_gm=False,
            api_timeout=5, bundle_max_pages=4, exclude_order_id=exclude_order_id,
        )
        if not result.get("ok"):
            return result
        manifest = cache.get(key) or {}
    credential = next((c for c in credentials if c.client_id == manifest.get("client_id")), None)
    if credential is None:
        return {"ok": False, "error": "Подключение к кабинету Ozon изменилось. Выберите поставку заново."}
    cargoes = manifest.get("cargoes") or []
    if not cargoes:
        return {"ok": False, "error": "В Ozon ещё нет грузомест. Создайте короба в поставке Ozon и повторите проверку."}
    # One reader per Ozon account avoids competing tabs exhausting API limits.
    lock_key = _ozon_gm_bundle_cache_key(credential.client_id, "preload-lock")
    lock_token = str(time.time_ns())
    if cache.add(lock_key, lock_token, timeout=45):
        try:
            cargoes = enrich_gm_cargoes_with_items(
                credential.client_id, credential.api_key, cargoes,
                max_fetch=8, pause_seconds=1.05, timeout=5, budget_seconds=12,
            )
        finally:
            if cache.get(lock_key) == lock_token:
                cache.delete(lock_key)
    else:
        cargoes = hydrate_gm_cargoes_from_cache(credential.client_id, cargoes)
    ready = sum(bool(c.get("items")) for c in cargoes)
    return {"ok": True, "order_id": order_id, "ready": ready,
            "total": len(cargoes), "complete": ready == len(cargoes), "retry_after": 2}


def get_ozon_supplies_batch_for_agency(
    agency,
    order_ids: list[int],
    *,
    stock_rows: list[dict],
    exclude_order_id: int | None = None,
    combine_for_one_request: bool = False,
) -> dict[str, Any]:
    """Preview several Ozon supplies against one shared Fullbox stock pool.

    The client form combines selected Ozon supplies into one Fullbox request;
    that mode rebalances their summed demand.  The older mass-submit endpoint
    creates one Fullbox request per Ozon supply and therefore keeps selections
    separate while sharing occupancy.  Both modes are read-only here.
    """
    clean_ids: list[int] = []
    for value in order_ids or []:
        try:
            order_id = int(value)
        except (TypeError, ValueError):
            continue
        if order_id > 0 and order_id not in clean_ids:
            clean_ids.append(order_id)
    if not clean_ids:
        return {"ok": False, "error": "Выберите хотя бы одну поставку Ozon."}
    if len(clean_ids) > 10:
        return {"ok": False, "error": "За один раз можно выбрать не более 10 поставок Ozon."}

    shared_selected: dict[str, int] = {}
    payloads: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for order_id in clean_ids:
        selected_before = dict(shared_selected)
        try:
            result = get_ozon_supply_for_agency(
                agency,
                order_id,
                stock_rows=stock_rows,
                enrich_gm=False,
                api_timeout=10,
                bundle_max_pages=4,
                include_gm_cargoes=True,
                exclude_order_id=exclude_order_id,
                already_selected={} if combine_for_one_request else shared_selected,
            )
        except Exception:
            logger.exception(
                "Ozon batch preview failed for agency=%s order_id=%s",
                getattr(agency, "id", None),
                order_id,
            )
            shared_selected.clear()
            shared_selected.update(selected_before)
            errors.append({"order_id": order_id, "error": "Не удалось загрузить поставку Ozon."})
            continue
        if not result.get("ok"):
            shared_selected.clear()
            shared_selected.update(selected_before)
            errors.append({"order_id": order_id, "error": str(result.get("error") or "Ошибка Ozon")})
            continue
        payloads.append(result)

    if combine_for_one_request:
        # A client preview becomes one Fullbox request, so its physical source
        # boxes are planned against the summed quantities, not independently.
        payloads = rebalance_ozon_supply_payloads(
            payloads,
            stock_rows,
            agency=agency,
        )
    batch_has_selection = any(
        row.get("selected_boxes") or row.get("partial_box_splits")
        for row in payloads
    )
    for result in payloads:
        form_values = result.get("form") or {}
        result["ready_for_batch"] = bool(
            (result.get("stock_match") or {}).get("status") == "full"
            and not result.get("already_uploaded")
            and not result.get("is_multi_destination")
            and not result.get("requires_piece_pick_confirmation")
            and (
                batch_has_selection
                if combine_for_one_request
                else bool(result.get("selected_boxes"))
            )
            and form_values.get("slot_date")
            and (form_values.get("destination_warehouse") or result.get("destination_warehouses"))
        )

    ready_ids = [
        int(row["order_id"])
        for row in payloads
        if row.get("ready_for_batch") and row.get("order_id")
    ]
    return {
        "ok": bool(payloads),
        "orders": payloads,
        "errors": errors,
        "ready_order_ids": ready_ids,
        "selected_count": len(clean_ids),
        "ready_count": len(ready_ids),
        "combined_request": bool(combine_for_one_request),
    }
