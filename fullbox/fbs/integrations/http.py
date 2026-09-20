from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urljoin

import requests
from django.conf import settings

from fbs.exceptions import FbsIntegrationError
from fbs.models import FbsIntegrationProfile, FbsMarketplaceCommand, FbsOzonCredential
from sku.models import MarketCredential

from .contracts import MarketplaceReadSpec


MARKETPLACE_BASE_URLS = {
    FbsIntegrationProfile.MARKETPLACE_WB: "https://marketplace-api.wildberries.ru",
    FbsIntegrationProfile.MARKETPLACE_OZON: "https://api-seller.ozon.ru",
}
WB_CONTENT_BASE_URL = "https://content-api.wildberries.ru"


@dataclass(frozen=True)
class MarketplaceHttpResponse:
    status_code: int
    headers: dict[str, str]
    content: bytes
    json_payload: dict | list | None = None


class MarketplaceTransport(Protocol):
    def send(self, command: FbsMarketplaceCommand) -> MarketplaceHttpResponse: ...


class MarketplaceReadTransport(Protocol):
    def send(
        self,
        profile: FbsIntegrationProfile,
        spec: MarketplaceReadSpec,
    ) -> MarketplaceHttpResponse: ...


def _marketplace_code(value: str | None) -> str:
    text = str(value or "").strip().upper()
    if text in {"WB", "WILDBERRIES", "WILDBERRIES (WB)"}:
        return FbsIntegrationProfile.MARKETPLACE_WB
    if text in {"OZON", "O-ZON"}:
        return FbsIntegrationProfile.MARKETPLACE_OZON
    return ""


def _client_credentials_for(profile: FbsIntegrationProfile) -> dict | None:
    if profile.marketplace == FbsIntegrationProfile.MARKETPLACE_OZON:
        ozon_credentials = list(
            FbsOzonCredential.objects.filter(agency_id=profile.agency_id).order_by(
                "slot", "id"
            )
        )
        external_account_id = str(profile.external_account_id or "").strip()
        if external_account_id:
            matched = next(
                (
                    credential
                    for credential in ozon_credentials
                    if str(credential.client_id or "").strip() == external_account_id
                ),
                None,
            )
            if matched is not None:
                return {
                    "api_key": str(matched.api_key or "").strip(),
                    "client_id": str(matched.client_id or "").strip(),
                    "source": f"client_cabinet_ozon_slot_{matched.slot}",
                }
            if ozon_credentials:
                return None
        elif len(ozon_credentials) == 1:
            matched = ozon_credentials[0]
            return {
                "api_key": str(matched.api_key or "").strip(),
                "client_id": str(matched.client_id or "").strip(),
                "source": f"client_cabinet_ozon_slot_{matched.slot}",
            }
        elif len(ozon_credentials) > 1:
            return None

    credentials = (
        MarketCredential.objects.select_related("market")
        .filter(agency_id=profile.agency_id)
        .order_by("id")
    )
    for credential in credentials:
        if _marketplace_code(credential.market.name) != profile.marketplace:
            continue
        api_key = str(credential.market_key or "").strip()
        if not api_key:
            continue
        client_id = str(credential.client_id or "").strip()
        if (
            profile.marketplace == FbsIntegrationProfile.MARKETPLACE_OZON
            and str(profile.external_account_id or "").strip()
            and client_id != str(profile.external_account_id or "").strip()
        ):
            continue
        return {
            "api_key": api_key,
            "client_id": client_id,
            "source": "client_cabinet",
        }
    return None


def _credentials_for(profile: FbsIntegrationProfile) -> dict:
    client_credentials = _client_credentials_for(profile)
    if client_credentials is not None:
        return client_credentials
    configured = getattr(settings, "FBS_MARKETPLACE_CREDENTIALS", {})
    if not isinstance(configured, dict):
        raise FbsIntegrationError("Настройка FBS_MARKETPLACE_CREDENTIALS должна быть словарем.")
    credentials = configured.get(str(profile.id)) or configured.get(profile.external_account_id)
    if not isinstance(credentials, dict):
        raise FbsIntegrationError("Для профиля FBS не настроены API-реквизиты.")
    return {**credentials, "source": "server_config"}


def _headers_for(profile: FbsIntegrationProfile, credentials: dict) -> dict[str, str]:
    api_key = str(credentials.get("api_key") or "").strip()
    if not api_key:
        raise FbsIntegrationError("Для профиля FBS не настроен API-ключ.")
    if profile.marketplace == FbsIntegrationProfile.MARKETPLACE_WB:
        return {"Authorization": api_key, "Accept": "application/json"}
    if profile.marketplace == FbsIntegrationProfile.MARKETPLACE_OZON:
        client_id = str(credentials.get("client_id") or profile.external_account_id or "").strip()
        if not client_id:
            raise FbsIntegrationError("Для профиля Ozon не настроен Client-Id.")
        return {
            "Client-Id": client_id,
            "Api-Key": api_key,
            "Accept": "application/json, application/pdf",
        }
    raise FbsIntegrationError("Маркетплейс профиля FBS не поддерживается.")


def marketplace_credentials_status(profile: FbsIntegrationProfile) -> dict[str, str | bool]:
    try:
        credentials = _credentials_for(profile)
        _headers_for(profile, credentials)
    except FbsIntegrationError as exc:
        return {"configured": False, "source": "", "error": str(exc)}
    return {
        "configured": True,
        "source": str(credentials.get("source") or "server_config"),
        "error": "",
    }


def _marketplace_url(profile: FbsIntegrationProfile, endpoint: str) -> str:
    endpoint = str(endpoint or "").strip()
    if not endpoint.startswith("/") or endpoint.startswith("//") or "://" in endpoint:
        raise FbsIntegrationError("В FBS-интеграции указан небезопасный endpoint.")
    base_url = (
        WB_CONTENT_BASE_URL
        if (
            profile.marketplace == FbsIntegrationProfile.MARKETPLACE_WB
            and endpoint.startswith("/content/")
        )
        else MARKETPLACE_BASE_URLS.get(profile.marketplace)
    )
    if not base_url:
        raise FbsIntegrationError("Для маркетплейса не настроен базовый API URL.")
    return urljoin(f"{base_url}/", endpoint.lstrip("/"))


def _command_url(command: FbsMarketplaceCommand) -> str:
    return _marketplace_url(command.profile, command.endpoint)


class RequestsMarketplaceTransport:
    def send(self, command: FbsMarketplaceCommand) -> MarketplaceHttpResponse:
        credentials = _credentials_for(command.profile)
        payload = command.payload if isinstance(command.payload, dict) else {}
        query = payload.get("query") if isinstance(payload.get("query"), dict) else {}
        raw_body = payload.get("body")
        body = raw_body if isinstance(raw_body, (dict, list)) else {}
        headers = _headers_for(command.profile, credentials)
        timeout = max(int(getattr(settings, "FBS_MARKETPLACE_TIMEOUT_SECONDS", 20)), 1)
        try:
            response = requests.request(
                command.http_method,
                _command_url(command),
                params=query,
                json=body or None,
                headers=headers,
                timeout=(5, timeout),
            )
        except requests.RequestException as exc:
            raise FbsIntegrationError("Маркетплейс временно недоступен.") from exc
        content_type = str(response.headers.get("Content-Type") or "").lower()
        json_payload = None
        if "json" in content_type and response.content:
            try:
                json_payload = response.json()
            except ValueError:
                json_payload = None
        return MarketplaceHttpResponse(
            status_code=response.status_code,
            headers={str(key): str(value) for key, value in response.headers.items()},
            content=response.content,
            json_payload=json_payload,
        )


class RequestsMarketplaceReadTransport:
    def __init__(self, *, reuse_connections: bool = False):
        self._session = requests.Session() if reuse_connections else None

    def close(self) -> None:
        if self._session is not None:
            self._session.close()

    def send(
        self,
        profile: FbsIntegrationProfile,
        spec: MarketplaceReadSpec,
    ) -> MarketplaceHttpResponse:
        credentials = _credentials_for(profile)
        headers = _headers_for(profile, credentials)
        headers["Content-Type"] = "application/json"
        timeout = max(int(getattr(settings, "FBS_MARKETPLACE_TIMEOUT_SECONDS", 20)), 1)
        try:
            response = (self._session or requests).request(
                spec.http_method,
                _marketplace_url(profile, spec.endpoint),
                params=spec.query,
                json=spec.body or None,
                headers=headers,
                timeout=(5, timeout),
            )
        except requests.RequestException as exc:
            raise FbsIntegrationError("Маркетплейс временно недоступен.") from exc
        json_payload = None
        if response.content:
            try:
                json_payload = response.json()
            except ValueError:
                json_payload = None
        return MarketplaceHttpResponse(
            status_code=response.status_code,
            headers={str(key): str(value) for key, value in response.headers.items()},
            content=response.content,
            json_payload=json_payload,
        )
