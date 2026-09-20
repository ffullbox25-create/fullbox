from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Callable

import requests

from fbs.exceptions import FbsIntegrationError
from fbs.models import FbsIntegrationProfile, FbsOzonCredential
from sku.models import Agency, MarketCredential


@dataclass(frozen=True)
class MarketplaceWarehouse:
    marketplace: str
    warehouse_id: str
    name: str
    status: str = ""
    external_account_id: str = ""
    credential_slot: int = 0
    selection_key: str = ""

    @property
    def payload(self) -> dict:
        return asdict(self)


def marketplace_code(value: str | None) -> str:
    text = str(value or "").strip().upper()
    if text in {"WB", "WILDBERRIES", "WILDBERRIES (WB)"}:
        return FbsIntegrationProfile.MARKETPLACE_WB
    if text in {"OZON", "O-ZON"}:
        return FbsIntegrationProfile.MARKETPLACE_OZON
    return ""


def client_marketplace_credentials(agency: Agency) -> dict[str, MarketCredential]:
    result: dict[str, MarketCredential] = {}
    credentials = (
        MarketCredential.objects.select_related("market")
        .filter(agency=agency)
        .order_by("id")
    )
    for credential in credentials:
        code = marketplace_code(credential.market.name)
        if code and str(credential.market_key or "").strip() and code not in result:
            result[code] = credential
    return result


def client_ozon_fbs_credentials(agency: Agency) -> tuple[object, ...]:
    credentials = tuple(
        FbsOzonCredential.objects.filter(agency=agency).order_by("slot", "id")
    )
    if credentials:
        return credentials
    legacy = client_marketplace_credentials(agency).get(
        FbsIntegrationProfile.MARKETPLACE_OZON
    )
    return (legacy,) if legacy is not None else ()


def _credential_api_key(credential) -> str:
    return str(
        getattr(credential, "api_key", None)
        or getattr(credential, "market_key", None)
        or ""
    ).strip()


def _response_payload(response, marketplace_label: str):
    if not 200 <= int(response.status_code) < 300:
        raise FbsIntegrationError(
            f"{marketplace_label} не отдал список складов: HTTP {response.status_code}."
        )
    try:
        return response.json()
    except ValueError as exc:
        raise FbsIntegrationError(
            f"{marketplace_label} вернул список складов в неверном формате."
        ) from exc


def _list_payload(payload) -> list:
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    for key in ("result", "warehouses", "items", "data"):
        value = payload.get(key)
        if isinstance(value, list):
            return value
        if isinstance(value, dict):
            for nested_key in ("warehouses", "items", "data", "result"):
                nested = value.get(nested_key)
                if isinstance(nested, list):
                    return nested
    return []


def _clean_warehouse_rows(
    marketplace: str,
    payload,
    *,
    external_account_id: str = "",
    credential_slot: int = 0,
) -> tuple[MarketplaceWarehouse, ...]:
    result: list[MarketplaceWarehouse] = []
    seen: set[str] = set()
    for item in _list_payload(payload):
        if not isinstance(item, dict):
            continue
        warehouse_id = str(
            item.get("id")
            or item.get("warehouse_id")
            or item.get("warehouseId")
            or ""
        ).strip()
        if not warehouse_id or warehouse_id in seen:
            continue
        seen.add(warehouse_id)
        name = str(
            item.get("name")
            or item.get("warehouse_name")
            or item.get("warehouseName")
            or warehouse_id
        ).strip()
        status = str(item.get("status") or "").strip()
        if marketplace == FbsIntegrationProfile.MARKETPLACE_WB:
            if item.get("isDeleting") is True:
                status = "Удаляется"
            elif not status:
                status = "Доступен"
        result.append(
            MarketplaceWarehouse(
                marketplace=marketplace,
                warehouse_id=warehouse_id,
                name=name,
                status=status,
                external_account_id=external_account_id,
                credential_slot=credential_slot,
                selection_key=(
                    f"{external_account_id}:{warehouse_id}"
                    if marketplace == FbsIntegrationProfile.MARKETPLACE_OZON
                    else warehouse_id
                ),
            )
        )
    return tuple(sorted(result, key=lambda row: (row.name.lower(), row.warehouse_id)))


def fetch_client_warehouses(
    *,
    agency: Agency,
    marketplace: str,
    request_func: Callable | None = None,
    credential=None,
) -> tuple[MarketplaceWarehouse, ...]:
    request_func = request_func or requests.request
    if credential is None:
        if marketplace == FbsIntegrationProfile.MARKETPLACE_OZON:
            credential = next(iter(client_ozon_fbs_credentials(agency)), None)
        else:
            credential = client_marketplace_credentials(agency).get(marketplace)
    if credential is None:
        raise FbsIntegrationError("Для клиента не настроен API-ключ маркетплейса.")
    api_key = _credential_api_key(credential)
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    method = "GET"
    url = ""
    body = None
    label = "Маркетплейс"
    if marketplace == FbsIntegrationProfile.MARKETPLACE_WB:
        label = "WB"
        url = "https://marketplace-api.wildberries.ru/api/v3/warehouses"
        headers["Authorization"] = api_key
    elif marketplace == FbsIntegrationProfile.MARKETPLACE_OZON:
        label = "Ozon"
        client_id = str(credential.client_id or "").strip()
        if not client_id:
            raise FbsIntegrationError("Для Ozon не указан Client ID.")
        method = "POST"
        url = "https://api-seller.ozon.ru/v2/warehouse/list"
        headers.update({"Client-Id": client_id, "Api-Key": api_key})
        body = {"limit": 200, "cursor": ""}
    else:
        raise FbsIntegrationError("Маркетплейс не поддерживается в FBS.")
    if marketplace == FbsIntegrationProfile.MARKETPLACE_OZON:
        items = []
        cursor = ""
        for _ in range(20):
            try:
                response = request_func(
                    method,
                    url,
                    headers=headers,
                    json={"limit": 200, "cursor": cursor},
                    timeout=(5, 25),
                )
            except requests.RequestException as exc:
                raise FbsIntegrationError(f"{label} временно недоступен.") from exc
            payload = _response_payload(response, label)
            items.extend(_list_payload(payload))
            if not isinstance(payload, dict) or not payload.get("has_next"):
                break
            next_cursor = str(payload.get("cursor") or "").strip()
            if not next_cursor or next_cursor == cursor:
                raise FbsIntegrationError("Ozon не вернул курсор следующей страницы складов.")
            cursor = next_cursor
        else:
            raise FbsIntegrationError("Ozon вернул слишком много страниц складов.")
        rows = _clean_warehouse_rows(
            marketplace,
            items,
            external_account_id=client_id,
            credential_slot=int(getattr(credential, "slot", 1) or 1),
        )
    else:
        try:
            response = request_func(
                method,
                url,
                headers=headers,
                json=body,
                timeout=(5, 25),
            )
        except requests.RequestException as exc:
            raise FbsIntegrationError(f"{label} временно недоступен.") from exc
        rows = _clean_warehouse_rows(
            marketplace,
            _response_payload(response, label),
        )
    if not rows:
        raise FbsIntegrationError(f"{label} не вернул доступные склады клиента.")
    return rows


def fetch_client_warehouse_catalog(agency: Agency) -> dict[str, dict]:
    credentials = client_marketplace_credentials(agency)
    catalog: dict[str, dict] = {}
    labels = {
        FbsIntegrationProfile.MARKETPLACE_WB: "Wildberries",
        FbsIntegrationProfile.MARKETPLACE_OZON: "Ozon",
    }
    for marketplace in (
        FbsIntegrationProfile.MARKETPLACE_WB,
        FbsIntegrationProfile.MARKETPLACE_OZON,
    ):
        marketplace_credentials = (
            client_ozon_fbs_credentials(agency)
            if marketplace == FbsIntegrationProfile.MARKETPLACE_OZON
            else tuple(
                credential
                for credential in (credentials.get(marketplace),)
                if credential is not None
            )
        )
        entry = {
            "marketplace": marketplace,
            "label": labels[marketplace],
            "credentials_configured": bool(marketplace_credentials),
            "client_id_configured": bool(marketplace_credentials) and all(
                str(getattr(credential, "client_id", None) or "").strip()
                for credential in marketplace_credentials
            ),
            "credential_count": len(marketplace_credentials),
            "warehouses": [],
            "error": "",
        }
        errors = []
        warehouse_rows = []
        for credential in marketplace_credentials:
            try:
                warehouse_rows.extend(
                    warehouse.payload
                    for warehouse in fetch_client_warehouses(
                        agency=agency,
                        marketplace=marketplace,
                        credential=credential,
                    )
                )
            except FbsIntegrationError as exc:
                slot = int(getattr(credential, "slot", 1) or 1)
                prefix = f"Ozon кабинет {slot}: " if marketplace == "ozon" else ""
                errors.append(prefix + str(exc))
        entry["warehouses"] = sorted(
            warehouse_rows,
            key=lambda row: (
                int(row.get("credential_slot") or 0),
                str(row.get("name") or "").lower(),
                str(row.get("warehouse_id") or ""),
            ),
        )
        entry["error"] = " ".join(errors)
        catalog[marketplace] = entry
    return catalog
