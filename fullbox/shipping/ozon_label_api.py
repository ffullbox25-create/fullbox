from __future__ import annotations

import time
from urllib.parse import urlsplit

from market_sync.http import friendly_network_error, marketplace_request

from . import ozon_supplies


def _fallback_groups(
    gm_cargoes: list[dict],
    expected_gm_barcodes: list[str] | None,
) -> tuple[dict[int, list[int]], str]:
    expected = {
        str(value or "").strip().casefold()
        for value in (expected_gm_barcodes or [])
        if str(value or "").strip()
    }
    actual: set[str] = set()
    groups: dict[int, list[int]] = {}
    for row in gm_cargoes or []:
        if not isinstance(row, dict):
            return {}, "В заявке повреждены данные грузомест Ozon."
        try:
            supply_id = int(row.get("supply_id") or 0)
            cargo_id = int(row.get("cargo_id") or 0)
        except (TypeError, ValueError):
            return {}, "Не у всех ШК ГМ сохранены supply_id и cargo_id Ozon."
        barcode = str(row.get("gm_barcode") or "").strip()
        if supply_id <= 0 or cargo_id <= 0 or not barcode:
            return {}, "Не у всех ШК ГМ сохранены supply_id и cargo_id Ozon."
        key = barcode.casefold()
        if key in actual:
            return {}, "В заявке есть повторяющиеся грузоместа Ozon."
        actual.add(key)
        groups.setdefault(supply_id, []).append(cargo_id)
    if not groups:
        return {}, "В заявке нет грузомест Ozon для печати."
    if expected and actual != expected:
        return {}, "Сохранённый состав ШК ГМ неполный. Этикетки не сформированы."
    return groups, ""


def _pdf_response(content: bytes, status_code: int) -> tuple[bytes | None, str]:
    if status_code != 200:
        return None, f"Ozon API ошибка {status_code}"
    if not content or not content.lstrip().startswith(b"%PDF-"):
        return None, "Ozon API не вернул PDF с этикетками."
    if len(content) > 25 * 1024 * 1024:
        return None, "PDF с этикетками Ozon превышает допустимый размер 25 МБ."
    return bytes(content), ""


def _fallback_download_pdf(
    *,
    file_guid: str,
    file_url: str,
    client_id: str,
    api_key: str,
    timeout: int,
) -> tuple[bytes | None, str]:
    headers = {"Client-Id": client_id, "Api-Key": api_key, "Accept": "application/pdf"}
    url = f"{ozon_supplies.OZON_API_BASE}/v1/cargoes-label/file/{file_guid}"
    if file_url:
        try:
            parsed = urlsplit(file_url)
            hostname = str(parsed.hostname or "").lower()
            if (
                parsed.scheme != "https"
                or not (hostname.endswith(".ozone.ru") or hostname.endswith(".ozon.ru"))
                or parsed.username
                or parsed.password
                or parsed.port not in (None, 443)
            ):
                return None, "Ozon вернул некорректную ссылку на PDF с этикетками."
            url = parsed.geturl()
            headers = {}
        except ValueError:
            return None, "Ozon вернул некорректную ссылку на PDF с этикетками."
    try:
        response = marketplace_request("GET", url, headers=headers, timeout=max(timeout, 20))
    except Exception as exc:
        return None, friendly_network_error(exc, "Ozon")
    return _pdf_response(bytes(response.content or b""), int(response.status_code))


def _fallback_fetch_ozon_gm_label_documents(
    agency,
    gm_cargoes: list[dict],
    *,
    expected_gm_barcodes: list[str] | None = None,
    poll_attempts: int = 6,
    poll_interval: float = 1.1,
    timeout: int = 12,
) -> tuple[list[dict], str, bool]:
    credential, credential_error = ozon_supplies.resolve_ozon_credential(agency)
    if credential_error or credential is None:
        return [], credential_error or "Не найдены реквизиты Ozon.", False
    groups, groups_error = _fallback_groups(gm_cargoes, expected_gm_barcodes)
    if groups_error:
        return [], groups_error, False
    client_id = ozon_supplies._normalize_ozon_client_id(credential.client_id or "")
    api_key = str(credential.market_key or "").strip()
    documents: list[dict] = []
    for supply_id, cargo_ids in sorted(groups.items()):
        payload, error = ozon_supplies.ozon_post(
            "/v1/cargoes-label/create",
            client_id,
            api_key,
            {"supply_id": supply_id, "cargoes": [{"cargo_id": cargo_id} for cargo_id in cargo_ids]},
            timeout=timeout,
        )
        operation_id = str((payload or {}).get("operation_id") or "").strip()
        if error or not operation_id:
            return [], str(error or "Ozon не вернул идентификатор операции этикеток."), False
        file_guid = ""
        file_url = ""
        for attempt in range(max(1, min(int(poll_attempts or 1), 12))):
            payload, error = ozon_supplies.ozon_post(
                "/v1/cargoes-label/get",
                client_id,
                api_key,
                {"operation_id": operation_id},
                timeout=timeout,
            )
            if error:
                return [], str(error), False
            status = str((payload or {}).get("status") or "").strip().upper()
            result = (payload or {}).get("result")
            if status == "SUCCESS" and isinstance(result, dict):
                file_guid = str(result.get("file_guid") or "").strip()
                file_url = str(result.get("file_url") or "").strip()
                break
            if status == "FAILED":
                return [], "Ozon не смог сформировать этикетки.", False
            if attempt + 1 < poll_attempts:
                time.sleep(max(0.0, min(float(poll_interval or 0), 2.0)))
        if not file_guid:
            return [], "Ozon ещё формирует этикетки. Обновите страницу через несколько секунд.", True
        pdf, error = _fallback_download_pdf(
            file_guid=file_guid,
            file_url=file_url,
            client_id=client_id,
            api_key=api_key,
            timeout=timeout,
        )
        if error or pdf is None:
            return [], error or "Не удалось скачать PDF Ozon.", False
        documents.append({"supply_id": supply_id, "content": pdf})
    return documents, "", False


def fetch_ozon_gm_label_documents(*args, **kwargs):
    current = getattr(ozon_supplies, "fetch_ozon_gm_label_documents", None)
    if callable(current):
        return current(*args, **kwargs)
    return _fallback_fetch_ozon_gm_label_documents(*args, **kwargs)
