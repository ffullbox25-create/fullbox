"""Кабинет менеджера / склада: раздел «Чаты»."""

from __future__ import annotations

import json

from django.http import JsonResponse
from django.views import View
from django.views.generic import TemplateView

from billing.permissions import filter_agencies_for_user
from client_cabinet.chat_switch import chats_enabled
from client_cabinet.chat_sla import DEFAULT_SLA_MINUTES, build_chat_sla_report
from client_cabinet.chats import (
    ensure_client_general_thread,
    get_thread_for_staff,
    serialize_thread_card,
    threads_for_manager,
    threads_for_warehouse,
)
from client_cabinet.messaging_lk import (
    list_chat_messages,
    mark_chat_read_for_staff,
    post_chat_message,
    serialize_chat_message,
)
from client_cabinet.models import ChatThread, ClientChatMessage
from employees.access import RoleRequiredMixin, get_request_role
from sku.models import Agency

MANAGER_ROLES = {"manager", "logistician", "head_manager", "director", "admin", "developer"}
WAREHOUSE_ROLES = {
    "storekeeper",
    "picker",
    "processing_worker",
    "processing_head",
    "reachtruck_driver",
    "super_car",
}
CABINET_ROLES = MANAGER_ROLES | WAREHOUSE_ROLES
WAREHOUSE_CHAT_KINDS = {ChatThread.KIND_ORDER_INTERNAL, ChatThread.KIND_TRIP}


def _chat_disabled_json():
    return JsonResponse({"ok": False, "error": "Чаты временно отключены"}, status=503)


class ChatsEnabledApiMixin:
    def dispatch(self, request, *args, **kwargs):
        if not chats_enabled():
            return _chat_disabled_json()
        return super().dispatch(request, *args, **kwargs)


def _is_warehouse_role(role: str | None) -> bool:
    return (role or "") in WAREHOUSE_ROLES


def _warehouse_can_access(thread: ChatThread | None) -> bool:
    return bool(thread and thread.kind in WAREHOUSE_CHAT_KINDS)


def _portfolio_agency_ids(request) -> list[int] | None:
    role = get_request_role(request)
    if role in {"head_manager", "director", "admin", "developer"}:
        return None
    if _is_warehouse_role(role):
        return None
    qs = filter_agencies_for_user(Agency.objects.filter(archived=False), request)
    return list(qs.values_list("id", flat=True)[:2000])


class TeamManagerChatsView(RoleRequiredMixin, TemplateView):
    template_name = "teammanager/chats.html"
    allowed_roles = CABINET_ROLES

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        request = self.request
        role = get_request_role(request)
        if not chats_enabled():
            ctx.update(
                {
                    "role": role or "manager",
                    "title": "Чаты",
                    "active_nav": "chats",
                    "chats_enabled": False,
                    "chat_cards": [],
                    "chat_selected": None,
                    "chat_messages": [],
                    "chat_pinned": None,
                    "chat_current_user_id": getattr(request.user, "id", None),
                    "chat_warehouse_mode": _is_warehouse_role(role),
                    "chat_filters": {"kind": "all", "q": "", "unread": False, "archive": False, "thread": ""},
                    "chat_kind_tabs": [],
                    "chat_ai_copilot": False,
                }
            )
            return ctx
        warehouse_mode = _is_warehouse_role(role)
        agency_ids = _portfolio_agency_ids(request)
        kind = (request.GET.get("kind") or ("internal" if warehouse_mode else "all")).strip()
        q = (request.GET.get("q") or "").strip()
        unread_only = kind == "unread" or request.GET.get("unread") in {"1", "true", "True"}
        show_archive = kind == "archive" or request.GET.get("archive") in {"1", "true", "True"}
        filter_kind = "" if kind in {"all", "unread", "archive"} else kind

        if not warehouse_mode:
            seed_qs = Agency.objects.filter(archived=False)
            if agency_ids is not None:
                seed_qs = seed_qs.filter(id__in=agency_ids)
            for agency in seed_qs.order_by("agn_name")[:40]:
                ensure_client_general_thread(agency, user=request.user)

        if warehouse_mode:
            threads = threads_for_warehouse(limit=120, q=q)
            if filter_kind == "logistics":
                threads = [t for t in threads if t.kind == ChatThread.KIND_TRIP]
            elif filter_kind == "internal":
                threads = [t for t in threads if t.kind == ChatThread.KIND_ORDER_INTERNAL]
            elif filter_kind in {"receiving", "shipping", "processing"}:
                threads = [
                    t
                    for t in threads
                    if t.kind == ChatThread.KIND_ORDER_INTERNAL and t.order_type == filter_kind
                ]
            if unread_only:
                threads = [t for t in threads if int(getattr(t, "unread_staff", 0) or 0) > 0]
        else:
            threads = threads_for_manager(
                agency_ids=agency_ids,
                kind=filter_kind,
                q=q,
                unread_only=unread_only,
                archived=True if show_archive else False,
                limit=120,
            )

        cards = [serialize_thread_card(t) for t in threads]
        selected_id = request.GET.get("thread") or (str(cards[0]["id"]) if cards else "")
        selected = None
        messages = []
        if selected_id and str(selected_id).isdigit():
            selected = get_thread_for_staff(thread_id=int(selected_id), agency_ids=agency_ids)
            if selected and warehouse_mode and not _warehouse_can_access(selected):
                selected = None
            if selected:
                mark_chat_read_for_staff(thread=selected, user=request.user)
                messages = list_chat_messages(
                    selected.agency,
                    thread=selected,
                    for_client=False,
                    limit=200,
                )
        tabs = [
            {"key": "all", "label": "Все"},
            {"key": "unread", "label": "Непрочитанные"},
            {"key": "clients", "label": "Клиенты"},
            {"key": "orders", "label": "Заявки"},
            {"key": "internal", "label": "Внутренние"},
            {"key": "logistics", "label": "Рейсы"},
            {"key": "receiving", "label": "Приёмка"},
            {"key": "shipping", "label": "Отгрузка"},
            {"key": "processing", "label": "Обработка"},
            {"key": "archive", "label": "Архив"},
        ]
        if warehouse_mode:
            tabs = [
                {"key": "internal", "label": "Внутренние"},
                {"key": "logistics", "label": "Рейсы"},
                {"key": "receiving", "label": "Приёмка"},
                {"key": "shipping", "label": "Отгрузка"},
                {"key": "processing", "label": "Обработка"},
                {"key": "unread", "label": "Непрочитанные"},
            ]
        ctx.update(
            {
                "role": role or "manager",
                "title": "Чаты",
                "active_nav": "chats",
                "chats_enabled": True,
                "chat_cards": cards,
                "chat_selected": serialize_thread_card(selected) if selected else None,
                "chat_messages": messages,
                "chat_pinned": (
                    serialize_chat_message(selected.pinned_message, agency_id=selected.agency_id)
                    if selected and selected.pinned_message_id and selected.pinned_message
                    else None
                ),
                "chat_current_user_id": getattr(request.user, "id", None),
                "chat_warehouse_mode": warehouse_mode,
                "chat_filters": {
                    "kind": kind,
                    "q": q,
                    "unread": unread_only,
                    "archive": show_archive,
                    "thread": selected_id,
                },
                "chat_kind_tabs": tabs,
                "chat_ai_copilot": (not warehouse_mode),
            }
        )
        return ctx


class TeamManagerChatsSlaView(RoleRequiredMixin, TemplateView):
    """SLA чатов: без ответа, среднее время, просрочки, нагрузка по менеджерам."""

    template_name = "teammanager/chats_sla.html"
    allowed_roles = MANAGER_ROLES

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        request = self.request
        role = get_request_role(request)
        if not chats_enabled():
            ctx.update(
                {
                    "role": role or "manager",
                    "title": "SLA чатов",
                    "active_nav": "chats_sla",
                    "chats_enabled": False,
                    "sla_report": {
                        "sla_minutes": DEFAULT_SLA_MINUTES,
                        "total_open": 0,
                        "overdue": 0,
                        "avg_first_response_minutes": 0,
                        "rows": [],
                    },
                    "sla_minutes": DEFAULT_SLA_MINUTES,
                }
            )
            return ctx
        agency_ids = _portfolio_agency_ids(request)
        sla_raw = str(request.GET.get("sla") or "").strip()
        sla_minutes = int(sla_raw) if sla_raw.isdigit() else DEFAULT_SLA_MINUTES
        report = build_chat_sla_report(agency_ids=agency_ids, sla_minutes=sla_minutes)
        ctx.update(
            {
                "role": role or "manager",
                "title": "SLA чатов",
                "active_nav": "chats_sla",
                "sla_report": report,
                "sla_minutes": report["sla_minutes"],
            }
        )
        return ctx


class TeamManagerChatTelegramView(RoleRequiredMixin, TemplateView):
    """Привязка Telegram для уведомлений по чатам."""

    template_name = "teammanager/chats_telegram.html"
    allowed_roles = CABINET_ROLES

    def get_context_data(self, **kwargs):
        from client_cabinet.chat_telegram import (
            bot_deep_link,
            bot_username,
            get_or_create_pref,
            telegram_configured,
        )

        ctx = super().get_context_data(**kwargs)
        role = get_request_role(self.request)
        pref = get_or_create_pref(self.request.user)
        deep_link = bot_deep_link(pref.telegram_link_code) if pref and pref.telegram_link_code else ""
        ctx.update(
            {
                "role": role or "manager",
                "title": "Telegram чатов",
                "active_nav": "chats_telegram",
                "tg_configured": telegram_configured(),
                "tg_bot_username": bot_username(),
                "tg_pref": pref,
                "tg_deep_link": deep_link,
                "flash_ok": self.request.GET.get("ok") or "",
                "flash_err": self.request.GET.get("err") or "",
            }
        )
        return ctx

    def post(self, request, *args, **kwargs):
        from urllib.parse import urlencode

        from django.shortcuts import redirect

        from client_cabinet.chat_telegram import (
            get_or_create_pref,
            issue_link_code,
            send_telegram_message,
            telegram_configured,
        )

        pref = get_or_create_pref(request.user)
        if not pref:
            return redirect("/team-manager/chats/telegram/?" + urlencode({"err": "Нет профиля"}))

        action = (request.POST.get("action") or "").strip()
        try:
            if action == "issue_link":
                if not telegram_configured():
                    raise ValueError("Бот Telegram не настроен на сервере (TELEGRAM_BOT_TOKEN)")
                code = issue_link_code(request.user)
                if not code:
                    raise ValueError("Не удалось создать код")
                return redirect(
                    "/team-manager/chats/telegram/?" + urlencode({"ok": "Код создан. Откройте бота по ссылке ниже."})
                )

            if action == "save_manual":
                chat_id = str(request.POST.get("telegram_chat_id") or "").strip()
                if chat_id and not chat_id.lstrip("-").isdigit():
                    raise ValueError("chat_id должен быть числом (можно с минусом для группы)")
                pref.telegram_chat_id = chat_id
                pref.telegram_enabled = bool(chat_id) and request.POST.get("telegram_enabled") == "1"
                if not chat_id:
                    pref.telegram_enabled = False
                pref.notify_client_messages = request.POST.get("notify_client_messages") == "1"
                pref.notify_mentions = request.POST.get("notify_mentions") == "1"
                pref.save(
                    update_fields=[
                        "telegram_chat_id",
                        "telegram_enabled",
                        "notify_client_messages",
                        "notify_mentions",
                        "updated_at",
                    ]
                )
                return redirect("/team-manager/chats/telegram/?" + urlencode({"ok": "Настройки сохранены"}))

            if action == "unlink":
                pref.telegram_chat_id = ""
                pref.telegram_enabled = False
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
                return redirect("/team-manager/chats/telegram/?" + urlencode({"ok": "Telegram отвязан"}))

            if action == "test_ping":
                if not pref.telegram_chat_id or not pref.telegram_enabled:
                    raise ValueError("Сначала привяжите Telegram и включите уведомления")
                if not send_telegram_message(pref.telegram_chat_id, "FullBox: тест уведомлений чата. Всё работает."):
                    raise ValueError("Не удалось отправить. Проверьте токен бота и chat_id.")
                return redirect("/team-manager/chats/telegram/?" + urlencode({"ok": "Тестовое сообщение отправлено"}))

            raise ValueError("Неизвестное действие")
        except ValueError as exc:
            return redirect("/team-manager/chats/telegram/?" + urlencode({"err": str(exc)}))
        except Exception as exc:
            return redirect("/team-manager/chats/telegram/?" + urlencode({"err": f"Ошибка: {exc}"}))


class TeamManagerChatMessagesApi(ChatsEnabledApiMixin, RoleRequiredMixin, View):
    allowed_roles = CABINET_ROLES

    def get(self, request, thread_id: int, *args, **kwargs):
        role = get_request_role(request)
        warehouse_mode = _is_warehouse_role(role)
        agency_ids = _portfolio_agency_ids(request)
        thread = get_thread_for_staff(thread_id=thread_id, agency_ids=agency_ids)
        if not thread:
            return JsonResponse({"ok": False, "error": "Чат не найден"}, status=404)
        if warehouse_mode and not _warehouse_can_access(thread):
            return JsonResponse({"ok": False, "error": "Доступ только к внутренним чатам"}, status=403)
        since_raw = str(request.GET.get("since_id") or "").strip()
        since_id = int(since_raw) if since_raw.isdigit() else None
        if since_id is None:
            mark_chat_read_for_staff(thread=thread, user=request.user)
        return JsonResponse(
            {
                "ok": True,
                "data": {
                    "thread": serialize_thread_card(thread),
                    "messages": list_chat_messages(
                        thread.agency,
                        thread=thread,
                        for_client=False,
                        limit=200,
                        since_id=since_id,
                    ),
                },
            }
        )

    def post(self, request, thread_id: int, *args, **kwargs):
        role = get_request_role(request)
        warehouse_mode = _is_warehouse_role(role)
        agency_ids = _portfolio_agency_ids(request)
        thread = get_thread_for_staff(thread_id=thread_id, agency_ids=agency_ids)
        if not thread:
            return JsonResponse({"ok": False, "error": "Чат не найден"}, status=404)
        if warehouse_mode and not _warehouse_can_access(thread):
            return JsonResponse({"ok": False, "error": "Склад не пишет в клиентский чат"}, status=403)
        content_type = (request.content_type or "").lower()
        text = ""
        payload = {}
        if "application/json" in content_type:
            try:
                payload = json.loads(request.body.decode("utf-8") or "{}")
            except json.JSONDecodeError:
                payload = {}
            text = str(payload.get("text") or payload.get("message") or "").strip()
            raw_vis = str(payload.get("visibility") or "").strip().lower()
        else:
            text = str(request.POST.get("text") or request.POST.get("message") or "").strip()
            raw_vis = str(request.POST.get("visibility") or "").strip().lower()
            payload = request.POST
        if warehouse_mode or thread.kind in WAREHOUSE_CHAT_KINDS:
            visibility = ClientChatMessage.VISIBILITY_INTERNAL
        elif raw_vis == "internal":
            visibility = ClientChatMessage.VISIBILITY_INTERNAL
        else:
            visibility = ClientChatMessage.VISIBILITY_CLIENT
        files = list(request.FILES.getlist("attachments")) or list(request.FILES.getlist("files"))
        if request.FILES.get("attachment"):
            files.append(request.FILES["attachment"])
        idem = str(payload.get("idempotency_key") or payload.get("client_key") or "").strip()
        reply_to_id = None
        raw_reply = payload.get("reply_to") or payload.get("reply_to_id")
        if raw_reply and str(raw_reply).isdigit():
            reply_to_id = int(raw_reply)
        try:
            message = post_chat_message(
                agency=thread.agency,
                user=request.user,
                text=text,
                files=files,
                author_role=ClientChatMessage.ROLE_STAFF,
                thread=thread,
                visibility=visibility,
                idempotency_key=idem,
                reply_to_id=reply_to_id,
            )
        except Exception as exc:
            from client_cabinet.chat_files import ChatFileError

            if isinstance(exc, ChatFileError):
                return JsonResponse({"ok": False, "error": str(exc)}, status=400)
            raise
        if not message:
            return JsonResponse({"ok": False, "error": "Введите текст или прикрепите файл"}, status=400)
        return JsonResponse(
            {
                "ok": True,
                "data": {
                    "message": serialize_chat_message(message, agency_id=thread.agency_id),
                    "messages": list_chat_messages(
                        thread.agency, thread=thread, for_client=False, limit=200
                    ),
                },
            }
        )


class TeamManagerChatCreateTaskApi(ChatsEnabledApiMixin, RoleRequiredMixin, View):
    allowed_roles = MANAGER_ROLES

    def post(self, request, message_id: int, *args, **kwargs):
        from client_cabinet.chat_tasks import create_task_from_message

        agency_ids = _portfolio_agency_ids(request)
        message = (
            ClientChatMessage.objects.select_related("agency", "thread")
            .filter(pk=message_id)
            .first()
        )
        if not message:
            return JsonResponse({"ok": False, "error": "Сообщение не найдено"}, status=404)
        if agency_ids is not None and message.agency_id not in agency_ids:
            return JsonResponse({"ok": False, "error": "Нет доступа"}, status=403)
        try:
            payload = json.loads(request.body.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            payload = {}
        result = create_task_from_message(
            message=message,
            user=request.user,
            title=str(payload.get("title") or "").strip(),
            assignee_id=int(payload["assignee_id"]) if str(payload.get("assignee_id") or "").isdigit() else None,
            priority=str(payload.get("priority") or "normal"),
            due_hours=int(payload.get("due_hours") or 24),
            description_extra=str(payload.get("description") or "").strip(),
        )
        if not result:
            return JsonResponse({"ok": False, "error": "Не удалось создать задачу"}, status=400)
        return JsonResponse({"ok": True, "data": result})


class TeamManagerChatResolveApi(ChatsEnabledApiMixin, RoleRequiredMixin, View):
    allowed_roles = MANAGER_ROLES

    def post(self, request, thread_id: int, *args, **kwargs):
        from client_cabinet.chat_tasks import resolve_conversation

        agency_ids = _portfolio_agency_ids(request)
        thread = get_thread_for_staff(thread_id=thread_id, agency_ids=agency_ids)
        if not thread:
            return JsonResponse({"ok": False, "error": "Чат не найден"}, status=404)
        resolve_conversation(thread=thread, user=request.user)
        return JsonResponse(
            {
                "ok": True,
                "data": {"thread": serialize_thread_card(thread)},
            }
        )


class TeamManagerChatMessageEditApi(ChatsEnabledApiMixin, RoleRequiredMixin, View):
    allowed_roles = CABINET_ROLES

    def post(self, request, message_id: int, *args, **kwargs):
        from client_cabinet.chat_actions import ChatActionError, edit_message

        agency_ids = _portfolio_agency_ids(request)
        message = ClientChatMessage.objects.select_related("agency", "thread").filter(pk=message_id).first()
        if not message:
            return JsonResponse({"ok": False, "error": "Сообщение не найдено"}, status=404)
        if message.thread and message.thread.kind == ChatThread.KIND_TRIP:
            pass
        elif agency_ids is not None and message.agency_id not in agency_ids:
            return JsonResponse({"ok": False, "error": "Нет доступа"}, status=403)
        try:
            payload = json.loads(request.body.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            payload = {}
        try:
            message = edit_message(
                message=message,
                user=request.user,
                text=str(payload.get("text") or "").strip(),
            )
        except ChatActionError as exc:
            return JsonResponse({"ok": False, "error": str(exc)}, status=400)
        return JsonResponse(
            {
                "ok": True,
                "data": {"message": serialize_chat_message(message, agency_id=message.agency_id)},
            }
        )


class TeamManagerChatMessageDeleteApi(ChatsEnabledApiMixin, RoleRequiredMixin, View):
    allowed_roles = CABINET_ROLES

    def post(self, request, message_id: int, *args, **kwargs):
        from client_cabinet.chat_actions import ChatActionError, soft_delete_message

        agency_ids = _portfolio_agency_ids(request)
        message = ClientChatMessage.objects.select_related("agency", "thread").filter(pk=message_id).first()
        if not message:
            return JsonResponse({"ok": False, "error": "Сообщение не найдено"}, status=404)
        if message.thread and message.thread.kind == ChatThread.KIND_TRIP:
            pass
        elif agency_ids is not None and message.agency_id not in agency_ids:
            return JsonResponse({"ok": False, "error": "Нет доступа"}, status=403)
        try:
            message = soft_delete_message(message=message, user=request.user)
        except ChatActionError as exc:
            return JsonResponse({"ok": False, "error": str(exc)}, status=400)
        return JsonResponse(
            {
                "ok": True,
                "data": {"message": serialize_chat_message(message, agency_id=message.agency_id)},
            }
        )


class TeamManagerChatPinApi(ChatsEnabledApiMixin, RoleRequiredMixin, View):
    allowed_roles = MANAGER_ROLES

    def post(self, request, thread_id: int, *args, **kwargs):
        from client_cabinet.chat_actions import ChatActionError, pin_message

        agency_ids = _portfolio_agency_ids(request)
        thread = get_thread_for_staff(thread_id=thread_id, agency_ids=agency_ids)
        if not thread:
            return JsonResponse({"ok": False, "error": "Чат не найден"}, status=404)
        try:
            payload = json.loads(request.body.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            payload = {}
        mid = payload.get("message_id")
        if mid in (None, "", 0, "0"):
            message = None
        else:
            message = ClientChatMessage.objects.filter(pk=mid, thread=thread).first()
            if not message:
                return JsonResponse({"ok": False, "error": "Сообщение не найдено"}, status=404)
        try:
            pin_message(thread=thread, message=message, user=request.user)
        except ChatActionError as exc:
            return JsonResponse({"ok": False, "error": str(exc)}, status=400)
        thread.refresh_from_db()
        return JsonResponse({"ok": True, "data": {"thread": serialize_thread_card(thread)}})


class TeamManagerChatMentionCandidatesApi(ChatsEnabledApiMixin, RoleRequiredMixin, View):
    allowed_roles = CABINET_ROLES

    def get(self, request, *args, **kwargs):
        from client_cabinet.chat_actions import mention_candidates

        q = str(request.GET.get("q") or "").strip()
        agency_id = request.GET.get("agency")
        agency = None
        if agency_id and str(agency_id).isdigit():
            agency = Agency.objects.filter(pk=int(agency_id)).first()
        return JsonResponse({"ok": True, "data": {"candidates": mention_candidates(q=q, agency=agency)}})


class TeamManagerChatMarkAllReadApi(ChatsEnabledApiMixin, RoleRequiredMixin, View):
    allowed_roles = CABINET_ROLES

    def post(self, request, *args, **kwargs):
        agency_ids = _portfolio_agency_ids(request)
        role = get_request_role(request)
        if _is_warehouse_role(role):
            threads = threads_for_warehouse(limit=300)
        else:
            threads = threads_for_manager(agency_ids=agency_ids, limit=300)
        total = 0
        for thread in threads:
            total += mark_chat_read_for_staff(thread=thread, user=request.user)
        return JsonResponse({"ok": True, "data": {"marked": total}})


class TeamManagerChatAISuggestApi(ChatsEnabledApiMixin, RoleRequiredMixin, View):
    """Этап 1: черновик ответа ИИ для менеджера (без автоотправки клиенту)."""

    allowed_roles = MANAGER_ROLES

    def post(self, request, thread_id: int, *args, **kwargs):
        from client_cabinet.chat_ai import suggest_reply_for_thread
        from client_cabinet.chat_ai.orchestrator import ChatAIError, serialize_suggestion

        agency_ids = _portfolio_agency_ids(request)
        thread = get_thread_for_staff(thread_id=thread_id, agency_ids=agency_ids)
        if not thread:
            return JsonResponse({"ok": False, "error": "Чат не найден"}, status=404)
        role = get_request_role(request) or ""
        try:
            suggestion = suggest_reply_for_thread(
                thread=thread,
                user=request.user,
                role=role,
                agency_id=thread.agency_id,
            )
        except ChatAIError as exc:
            return JsonResponse({"ok": False, "error": exc.message, "code": exc.code}, status=400)
        except Exception as exc:
            return JsonResponse({"ok": False, "error": f"Ошибка ИИ: {exc}"}, status=500)
        return JsonResponse({"ok": True, "data": serialize_suggestion(suggestion)})


class TeamManagerChatAIFeedbackApi(ChatsEnabledApiMixin, RoleRequiredMixin, View):
    allowed_roles = MANAGER_ROLES

    def post(self, request, suggestion_id: int, *args, **kwargs):
        from client_cabinet.chat_ai import apply_suggestion_feedback
        from client_cabinet.chat_ai.orchestrator import ChatAIError, serialize_suggestion
        from client_cabinet.models import ChatAISuggestion

        agency_ids = _portfolio_agency_ids(request)
        suggestion = (
            ChatAISuggestion.objects.select_related("thread", "agency", "trigger_message")
            .filter(pk=suggestion_id)
            .first()
        )
        if not suggestion:
            return JsonResponse({"ok": False, "error": "Черновик не найден"}, status=404)
        thread = get_thread_for_staff(thread_id=suggestion.thread_id, agency_ids=agency_ids)
        if not thread:
            return JsonResponse({"ok": False, "error": "Нет доступа к чату"}, status=403)

        try:
            payload = json.loads(request.body.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            payload = {}
        action = str(payload.get("action") or request.POST.get("action") or "").strip()
        try:
            suggestion = apply_suggestion_feedback(
                suggestion=suggestion,
                user=request.user,
                action=action,
                final_text=str(payload.get("final_text") or ""),
                feedback=str(payload.get("feedback") or ""),
                note=str(payload.get("note") or ""),
                enqueue_knowledge=bool(payload.get("enqueue_knowledge")),
            )
        except ChatAIError as exc:
            return JsonResponse({"ok": False, "error": exc.message, "code": exc.code}, status=400)
        return JsonResponse({"ok": True, "data": serialize_suggestion(suggestion)})
