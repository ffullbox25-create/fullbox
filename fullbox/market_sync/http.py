from __future__ import annotations

import requests


def marketplace_session() -> requests.Session:
    """HTTP session that ignores HTTP(S)_PROXY from the process environment.

    Local Cursor/sandbox proxies often return 403 for marketplace tunnel requests.
    Marketplace APIs must be called directly.
    """
    session = requests.Session()
    session.trust_env = False
    return session


def marketplace_request(method: str, url: str, **kwargs):
    timeout = kwargs.pop("timeout", 40)
    with marketplace_session() as session:
        return session.request(method, url, timeout=timeout, **kwargs)


def friendly_network_error(exc: BaseException, marketplace: str) -> str:
    text = str(exc or "")
    lowered = text.lower()
    if "proxy" in lowered or "tunnel connection failed" in lowered:
        return (
            f"{marketplace}: нет прямого доступа к API "
            "(локальный прокси блокирует запрос). Обновление каталога/остатков "
            "нужно выполнять без прокси или с разрешением wildberries.ru / ozon.ru."
        )
    if "timed out" in lowered or "timeout" in lowered:
        return f"{marketplace}: API не ответил вовремя. Попробуйте ещё раз."
    if "nameresolution" in lowered or "nodename nor servname" in lowered or "failed to resolve" in lowered:
        return f"{marketplace}: не удалось разрешить адрес API. Проверьте интернет."
    return f"{marketplace} API недоступен: {text}"


def is_auth_http_status(status_code: int | None) -> bool:
    try:
        code = int(status_code or 0)
    except (TypeError, ValueError):
        return False
    return code in {401, 403}


def auth_error_message(marketplace: str, status_code: int | None = None) -> str:
    code = f" ({status_code})" if status_code else ""
    return (
        f"{marketplace}: ключ API недействителен или истёк{code}. "
        "Передайте менеджеру FullBox новый ключ для переподключения."
    )
