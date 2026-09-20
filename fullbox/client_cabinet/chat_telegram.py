"""Telegram-уведомления по чатам (односторонние, только сотрудникам)."""

from __future__ import annotations

import logging
import secrets
from datetime import timedelta
from typing import Any
from urllib.parse import quote

import requests
from django.conf import settings
from django.utils import timezone

from .models import (
    ChatNotificationPreference,
    ChatTelegramBotConfig,
    ChatThread,
    ClientChatMessage,
)

logger = logging.getLogger(__name__)

TG_API = "https://api.telegram.org/bot{token}/{method}"


def get_bot_config() -> ChatTelegramBotConfig | None:
    try:
        return ChatTelegramBotConfig.load()
    except Exception:
        return None


def resolved_bot_token() -> str:
    """Токен из кабинета директора (если задан), иначе из env. Выключенный DB-конфиг глушит бота."""
    cfg = get_bot_config()
    if cfg and str(cfg.bot_token or "").strip():
        if not cfg.is_enabled:
            return ""
        return cfg.bot_token.strip()
    return str(getattr(settings, "TELEGRAM_BOT_TOKEN", "") or "").strip()


def resolved_webhook_secret() -> str:
    cfg = get_bot_config()
    if cfg and cfg.webhook_secret:
        return cfg.webhook_secret.strip()
    return str(getattr(settings, "TELEGRAM_WEBHOOK_SECRET", "") or "").strip()


def resolved_alert_chat_id() -> str:
    cfg = get_bot_config()
    if cfg and cfg.alert_chat_id:
        return cfg.alert_chat_id.strip()
    return str(getattr(settings, "TELEGRAM_CHATS_ALERT_CHAT_ID", "") or "").strip()


def telegram_configured() -> bool:
    return bool(resolved_bot_token())


def bot_username() -> str:
    cfg = get_bot_config()
    if cfg and cfg.bot_username:
        return cfg.bot_username.strip().lstrip("@")
    return str(getattr(settings, "TELEGRAM_BOT_USERNAME", "") or "").strip().lstrip("@")


def public_lk_base() -> str:
    cfg = get_bot_config()
    if cfg and cfg.lk_base_url:
        return cfg.lk_base_url.strip().rstrip("/")
    base = str(getattr(settings, "FULLBOX_LK_BASE_URL", "") or "").strip().rstrip("/")
    if base:
        return base
    return "https://lk.fullbox.ru"


def get_or_create_pref(user) -> ChatNotificationPreference | None:
    if not user or not getattr(user, "pk", None):
        return None
    pref, _ = ChatNotificationPreference.objects.get_or_create(user=user)
    return pref


def issue_link_code(user) -> str:
    pref = get_or_create_pref(user)
    if not pref:
        return ""
    code = secrets.token_urlsafe(12).replace("-", "").replace("_", "")[:16]
    pref.telegram_link_code = code
    pref.telegram_link_expires_at = timezone.now() + timedelta(hours=2)
    pref.save(update_fields=["telegram_link_code", "telegram_link_expires_at", "updated_at"])
    return code


def bot_deep_link(code: str) -> str:
    name = bot_username()
    if not name or not code:
        return ""
    return f"https://t.me/{name}?start={quote(code)}"


def mask_secret(value: str, *, keep: int = 4) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    if len(raw) <= keep * 2:
        return "•" * min(8, len(raw))
    return f"{raw[:keep]}…{raw[-keep:]}"


def webhook_public_url() -> str:
    return f"{public_lk_base()}/client/api/v1/chat/telegram/webhook/"


def set_telegram_webhook() -> tuple[bool, str]:
    """Регистрирует webhook у Telegram. Возвращает (ok, message)."""
    token = resolved_bot_token()
    if not token:
        return False, "Сначала укажите токен бота и включите интеграцию"
    url = webhook_public_url()
    payload: dict[str, Any] = {"url": url}
    secret = resolved_webhook_secret()
    if secret:
        payload["secret_token"] = secret
    try:
        resp = requests.post(
            TG_API.format(token=token, method="setWebhook"),
            json=payload,
            timeout=10,
        )
        data = resp.json() if resp.content else {}
        if resp.status_code >= 400 or not data.get("ok"):
            desc = data.get("description") or resp.text[:200]
            return False, f"Telegram отклонил webhook: {desc}"
        return True, f"Webhook установлен: {url}"
    except Exception as exc:
        logger.exception("setWebhook failed")
        return False, f"Ошибка setWebhook: {exc}"


def send_telegram_message(chat_id: str | int, text: str, *, disable_preview: bool = True) -> bool:
    token = resolved_bot_token()
    if not token or not chat_id or not str(text or "").strip():
        return False
    try:
        resp = requests.post(
            TG_API.format(token=token, method="sendMessage"),
            json={
                "chat_id": str(chat_id),
                "text": str(text)[:3900],
                "disable_web_page_preview": bool(disable_preview),
            },
            timeout=8,
        )
        if resp.status_code >= 400:
            logger.warning("telegram send failed chat=%s status=%s body=%s", chat_id, resp.status_code, resp.text[:200])
            return False
        data = resp.json()
        return bool(data.get("ok"))
    except Exception:
        logger.exception("telegram send error chat=%s", chat_id)
        return False


def bind_chat_by_code(*, code: str, chat_id: str | int) -> ChatNotificationPreference | None:
    code = str(code or "").strip()
    if not code:
        return None
    pref = (
        ChatNotificationPreference.objects.filter(telegram_link_code=code)
        .select_related("user")
        .first()
    )
    if not pref:
        return None
    if pref.telegram_link_expires_at and pref.telegram_link_expires_at < timezone.now():
        return None
    pref.telegram_chat_id = str(chat_id)
    pref.telegram_enabled = True
    pref.telegram_link_code = ""
    pref.telegram_link_expires_at = None
    pref.save(
        update_fields=[
            "telegram_chat_id",
            "telegram_enabled",
            "telegram_link_code",
            "telegram_link_expires_at",
            "updated_at",
        ]
    )
    return pref


def process_telegram_update(update: dict[str, Any]) -> dict[str, Any]:
    """Обработка webhook update: /start CODE → привязка."""
    message = (update or {}).get("message") or {}
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    text = str(message.get("text") or "").strip()
    if not chat_id or not text:
        return {"ok": True, "handled": False}
    if text.startswith("/start"):
        parts = text.split(maxsplit=1)
        code = parts[1].strip() if len(parts) > 1 else ""
        if not code:
            send_telegram_message(
                chat_id,
                "Чтобы подключить уведомления FullBox, откройте ссылку привязки в кабинете менеджера "
                "(Настройки → Telegram) или отправьте /start КОД.",
            )
            return {"ok": True, "handled": True, "need_code": True}
        pref = bind_chat_by_code(code=code, chat_id=chat_id)
        if pref:
            send_telegram_message(
                chat_id,
                "Готово. Telegram привязан к FullBox.\n"
                "Буду писать о новых сообщениях клиентов и @упоминаниях в чатах.",
            )
            return {"ok": True, "handled": True, "bound": True, "user_id": pref.user_id}
        send_telegram_message(chat_id, "Код не найден или устарел. Сгенерируйте новый в кабинете менеджера.")
        return {"ok": True, "handled": True, "bound": False}
    if text in {"/stop", "/unlink"}:
        ChatNotificationPreference.objects.filter(telegram_chat_id=str(chat_id)).update(
            telegram_enabled=False,
            updated_at=timezone.now(),
        )
        send_telegram_message(chat_id, "Уведомления выключены. Включить снова можно в кабинете менеджера.")
        return {"ok": True, "handled": True, "unlinked": True}
    return {"ok": True, "handled": False}


def _agency_title(agency) -> str:
    return str(
        getattr(agency, "short_name", None)
        or getattr(agency, "agn_name", None)
        or f"Клиент {getattr(agency, 'id', '?')}"
    )


def _thread_url(thread: ChatThread | None) -> str:
    if not thread:
        return f"{public_lk_base()}/team-manager/chats/"
    return f"{public_lk_base()}/team-manager/chats/?thread={thread.id}"


def _pref_for_user(user_id: int) -> ChatNotificationPreference | None:
    return (
        ChatNotificationPreference.objects.filter(user_id=user_id, telegram_enabled=True)
        .exclude(telegram_chat_id="")
        .first()
    )


def notify_telegram_about_message(message: ClientChatMessage) -> int:
    """Шлёт TG менеджеру клиента и упомянутым. Не блокирует чат при ошибках."""
    if not telegram_configured() or not message or message.is_deleted:
        return 0
    if message.author_role == ClientChatMessage.ROLE_SYSTEM:
        return 0

    recipients: dict[int, str] = {}  # user_id -> reason
    agency = message.agency
    thread = message.thread

    # Клиент написал во видимый чат → менеджер клиента
    if (
        message.author_role == ClientChatMessage.ROLE_CLIENT
        and message.visibility in {
            ClientChatMessage.VISIBILITY_CLIENT,
            ClientChatMessage.VISIBILITY_SYSTEM,
        }
        and agency
        and getattr(agency, "mened_user_id", None)
    ):
        recipients[int(agency.mened_user_id)] = "client_message"

    # Упоминания
    try:
        for mention in message.mentions.all():
            if mention.user_id and mention.user_id != message.author_id:
                recipients[int(mention.user_id)] = "mention"
    except Exception:
        pass

    # Не уведомлять автора
    if message.author_id:
        recipients.pop(int(message.author_id), None)

    preview = (message.text or "Вложение").strip().replace("\n", " ")
    if len(preview) > 220:
        preview = preview[:217] + "…"
    agency_name = _agency_title(agency) if agency else "Клиент"
    title = thread.title if thread else "Чат"
    url = _thread_url(thread)

    sent = 0
    for user_id, reason in recipients.items():
        pref = _pref_for_user(user_id)
        if not pref:
            continue
        if reason == "mention" and not pref.notify_mentions:
            continue
        if reason == "client_message" and not pref.notify_client_messages:
            continue
        if reason == "mention":
            head = "Вас упомянули в чате FullBox"
        else:
            head = "Новое сообщение клиента"
        body = f"{head}\n{agency_name} · {title}\n\n{preview}\n\nОткрыть: {url}"
        if send_telegram_message(pref.telegram_chat_id, body):
            sent += 1

    # Опциональный общий чат/группа
    alert_chat = resolved_alert_chat_id()
    if (
        alert_chat
        and message.author_role == ClientChatMessage.ROLE_CLIENT
        and message.visibility == ClientChatMessage.VISIBILITY_CLIENT
    ):
        body = f"Чат клиента\n{agency_name} · {title}\n\n{preview}\n\n{url}"
        if send_telegram_message(alert_chat, body):
            sent += 1
    return sent
