"""Кабинет директора: настройки Telegram для чатов."""

from __future__ import annotations

import secrets

from django.contrib.auth.mixins import LoginRequiredMixin
from django.shortcuts import redirect
from django.urls import reverse
from django.utils.http import urlencode
from django.views.generic import TemplateView

from employees.access import RoleRequiredMixin, get_request_role

from .chat_telegram import (
    mask_secret,
    public_lk_base,
    set_telegram_webhook,
    telegram_configured,
    webhook_public_url,
)
from .models import ChatTelegramBotConfig

DIRECTOR_TG_ROLES = {"director", "admin", "developer"}


def _director_nav(active_key: str = "settings"):
    from fullbox.director_dashboard import director_nav_items

    return director_nav_items(active_key=active_key)


class DirectorTelegramSettingsView(LoginRequiredMixin, RoleRequiredMixin, TemplateView):
    template_name = "director/telegram_settings.html"
    allowed_roles = DIRECTOR_TG_ROLES

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        cfg = ChatTelegramBotConfig.load()
        role = get_request_role(self.request) or "director"
        employee = getattr(self.request, "employee", None)
        full_name = (
            getattr(employee, "full_name", None)
            or self.request.session.get("employee_name")
            or self.request.user.get_username()
        )
        initials = "".join(part[:1] for part in str(full_name).split()[:2]).upper() or "Д"
        env_fallback = telegram_configured() and not bool(cfg.bot_token and cfg.is_enabled)
        from fullbox.director_dashboard import PERIOD_CHOICES

        ctx.update(
            {
                "role": role,
                "title": "Telegram чатов",
                "nav_items": _director_nav("settings"),
                "director_name": full_name,
                "director_initials": initials,
                "period": "month",
                "period_choices": PERIOD_CHOICES,
                "updated_label": "Интеграции",
                "notifications_count": 0,
                "tg_cfg": cfg,
                "tg_token_mask": mask_secret(cfg.bot_token),
                "tg_secret_mask": mask_secret(cfg.webhook_secret),
                "tg_webhook_url": webhook_public_url(),
                "tg_lk_base": cfg.lk_base_url or public_lk_base(),
                "tg_live": telegram_configured(),
                "tg_env_fallback": env_fallback,
                "flash_ok": self.request.GET.get("ok") or "",
                "flash_err": self.request.GET.get("err") or "",
            }
        )
        return ctx

    def post(self, request, *args, **kwargs):
        cfg = ChatTelegramBotConfig.load()
        action = (request.POST.get("action") or "save").strip()
        base = reverse("director-telegram-settings")

        try:
            if action == "generate_secret":
                cfg.webhook_secret = secrets.token_urlsafe(24)
                cfg.updated_by = request.user
                cfg.save(update_fields=["webhook_secret", "updated_by", "updated_at"])
                return redirect(f"{base}?{urlencode({'ok': 'Secret сгенерирован. Не забудьте сохранить остальные поля и установить webhook.'})}")

            if action == "set_webhook":
                ok, msg = set_telegram_webhook()
                key = "ok" if ok else "err"
                return redirect(f"{base}?{urlencode({key: msg})}")

            if action == "clear_token":
                cfg.bot_token = ""
                cfg.is_enabled = False
                cfg.updated_by = request.user
                cfg.save(update_fields=["bot_token", "is_enabled", "updated_by", "updated_at"])
                return redirect(f"{base}?{urlencode({'ok': 'Токен очищен'})}")

            # save
            token_in = str(request.POST.get("bot_token") or "").strip()
            if token_in and "…" not in token_in and not token_in.startswith("•"):
                cfg.bot_token = token_in
            username = str(request.POST.get("bot_username") or "").strip().lstrip("@")
            cfg.bot_username = username
            secret_in = str(request.POST.get("webhook_secret") or "").strip()
            if secret_in and "…" not in secret_in and not secret_in.startswith("•"):
                cfg.webhook_secret = secret_in
            alert = str(request.POST.get("alert_chat_id") or "").strip()
            if alert and not alert.lstrip("-").isdigit():
                raise ValueError("Alert chat id должен быть числом")
            cfg.alert_chat_id = alert
            lk = str(request.POST.get("lk_base_url") or "").strip().rstrip("/")
            cfg.lk_base_url = lk
            cfg.is_enabled = request.POST.get("is_enabled") == "1"
            cfg.updated_by = request.user
            cfg.save()
            return redirect(f"{base}?{urlencode({'ok': 'Настройки Telegram сохранены. Можно заполнить поля позже или включить сейчас.'})}")
        except ValueError as exc:
            return redirect(f"{base}?{urlencode({'err': str(exc)})}")
        except Exception as exc:
            return redirect(f"{base}?{urlencode({'err': f'Ошибка: {exc}'})}")
