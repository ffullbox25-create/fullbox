"""Публичный webhook Telegram для привязки уведомлений чата."""

from __future__ import annotations

import json
import logging

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

logger = logging.getLogger(__name__)


@csrf_exempt
@require_POST
def telegram_chat_webhook(request):
    from .chat_telegram import resolved_bot_token, resolved_webhook_secret

    secret = resolved_webhook_secret()
    if secret:
        got = request.headers.get("X-Telegram-Bot-Api-Secret-Token") or ""
        if got != secret:
            return JsonResponse({"ok": False, "error": "forbidden"}, status=403)
    if not resolved_bot_token():
        return JsonResponse({"ok": False, "error": "bot_not_configured"}, status=503)
    try:
        update = json.loads(request.body.decode("utf-8") or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"ok": False, "error": "bad_json"}, status=400)
    try:
        from .chat_telegram import process_telegram_update

        result = process_telegram_update(update if isinstance(update, dict) else {})
    except Exception:
        logger.exception("telegram webhook failed")
        return JsonResponse({"ok": True})
    return JsonResponse(result if isinstance(result, dict) else {"ok": True})
