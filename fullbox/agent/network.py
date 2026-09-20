"""Network boundary for legacy shared Fullbox Agent credentials."""

from __future__ import annotations

from ipaddress import ip_address, ip_network
from typing import Iterable

from django.conf import settings


DEFAULT_LEGACY_ALLOWED_NETWORKS = (
    "192.168.1.0/24",
    "127.0.0.0/8",
    "::1/128",
)
TRUSTED_REVERSE_PROXY_NETWORKS = (
    "127.0.0.0/8",
    "::1/128",
)


def _network_values(raw_value) -> tuple[str, ...]:
    if isinstance(raw_value, str):
        values: Iterable = raw_value.split(",")
    elif isinstance(raw_value, (list, tuple, set, frozenset)):
        values = raw_value
    else:
        values = ()
    return tuple(str(value or "").strip() for value in values if str(value or "").strip())


def _address(value):
    text = str(value or "").strip()
    if not text or "," in text:
        return None
    try:
        return ip_address(text)
    except ValueError:
        return None


def _in_networks(address, network_values: Iterable[str]) -> bool:
    if address is None:
        return False
    for value in network_values:
        try:
            network = ip_network(str(value), strict=False)
        except ValueError:
            continue
        if address.version == network.version and address in network:
            return True
    return False


def _trusted_proxy(address) -> bool:
    return _in_networks(address, TRUSTED_REVERSE_PROXY_NETWORKS)


def _legacy_networks() -> tuple[str, ...]:
    configured = getattr(settings, "AGENT_LEGACY_ALLOWED_NETWORKS", DEFAULT_LEGACY_ALLOWED_NETWORKS)
    return _network_values(configured)


def _effective_client_address(*, peer_value, real_ip_value):
    peer = _address(peer_value)
    if _trusted_proxy(peer):
        # nginx overwrites X-Real-IP. Direct loopback calls have no such header.
        if not str(real_ip_value or "").strip():
            return peer
        # A malformed proxy-provided address must fail closed.
        return _address(real_ip_value)
    return peer


def legacy_agent_request_source_allowed(request) -> bool:
    peer_value = request.META.get("REMOTE_ADDR", "")
    real_ip_value = request.META.get("HTTP_X_REAL_IP", "")
    address = _effective_client_address(peer_value=peer_value, real_ip_value=real_ip_value)
    return _in_networks(address, _legacy_networks())


def _scope_header(scope, name: bytes) -> str:
    for key, value in scope.get("headers") or []:
        if key.lower() != name:
            continue
        try:
            return value.decode("utf-8").strip()
        except UnicodeDecodeError:
            return ""
    return ""


def legacy_agent_scope_source_allowed(scope) -> bool:
    client = scope.get("client") or ("", 0)
    peer_value = client[0] if isinstance(client, (list, tuple)) and client else ""
    real_ip_value = _scope_header(scope, b"x-real-ip")
    address = _effective_client_address(peer_value=peer_value, real_ip_value=real_ip_value)
    return _in_networks(address, _legacy_networks())
