"""AIOrchestrator: сбор контекста → политика → stub-черновик → журнал."""

from __future__ import annotations

import logging
from typing import Any

from django.utils import timezone

from ..models import (
    ChatAIKnowledgeCandidate,
    ChatAISuggestion,
    ChatThread,
    ClientChatMessage,
)
from .knowledge import kb_answer_for, search_knowledge_stub
from .policy import AIActionPolicy
from .types import AISource, AISuggestResult

logger = logging.getLogger(__name__)


class ChatAIError(Exception):
    def __init__(self, message: str, *, code: str = "error"):
        super().__init__(message)
        self.code = code
        self.message = message


def _last_client_message(thread: ChatThread) -> ClientChatMessage | None:
    return (
        ClientChatMessage.objects.filter(
            thread=thread,
            author_role=ClientChatMessage.ROLE_CLIENT,
            is_deleted=False,
        )
        .order_by("-created_at", "-id")
        .first()
    )


def _recent_messages(thread: ChatThread, *, limit: int = 12) -> list[dict[str, Any]]:
    rows = (
        ClientChatMessage.objects.filter(thread=thread, is_deleted=False)
        .order_by("-created_at", "-id")[:limit]
    )
    out = []
    for m in reversed(list(rows)):
        out.append(
            {
                "id": m.id,
                "role": m.author_role,
                "visibility": m.visibility,
                "text": (m.text or "")[:500],
            }
        )
    return out


def _build_stub_result(
    *,
    thread: ChatThread,
    trigger: ClientChatMessage | None,
    policy: AIActionPolicy,
) -> AISuggestResult:
    question = (trigger.text if trigger else "") or ""
    category = policy.classify_stub(question)
    sources = search_knowledge_stub(category=category, query=question)
    kb_text = kb_answer_for(category)
    agency_name = (
        getattr(thread.agency, "short_name", None)
        or getattr(thread.agency, "agn_name", None)
        or f"Клиент {thread.agency_id}"
    )
    order_bit = ""
    if thread.order_id:
        order_bit = f" по заявке {thread.order_id}"
        sources.insert(
            0,
            AISource(
                title=f"Заявка {thread.order_id}",
                kind="order",
                ref=str(thread.order_id),
            ),
        )

    warnings = [
        "Это черновик-заглушка (модель ещё не подключена). Проверьте текст перед отправкой.",
        "Автоответ клиенту на этапе 1 выключен.",
    ]
    needs_escalation = category in {
        "претензия",
        "биллинг",
        "тарифы",
        "расхождения",
        "брак",
    } or not question.strip()

    if needs_escalation and category in {"претензия", "расхождения", "биллинг", "тарифы"}:
        text = (
            f"Здравствуйте!\n\n"
            f"Спасибо за обращение{order_bit}. Вопрос требует проверки менеджера FullBox — "
            f"мы уточним детали по {agency_name} и ответим в ближайшее время.\n\n"
            f"Если есть номер заявки, SKU или документы — пришлите, пожалуйста, это ускорит разбор."
        )
        confidence = ChatAISuggestion.CONF_LOW
        actions = ["escalate", "create_task_draft", "ask_clarification"]
    elif kb_text:
        text = (
            f"Здравствуйте!\n\n"
            f"{kb_text}\n\n"
            f"Если нужно, подскажу по вашей ситуации{order_bit} точнее — "
            f"уточните детали, и я подключу коллег при необходимости."
        )
        confidence = ChatAISuggestion.CONF_MEDIUM
        actions = ["insert", "shorten", "more_formal"]
        warnings.append("Источник — утверждённая короткая шпаргалка stub-БЗ, не живые данные WMS.")
    else:
        text = (
            f"Здравствуйте!\n\n"
            f"Спасибо за сообщение{order_bit}. Чтобы ответить точнее, уточните, пожалуйста, "
            f"номер заявки и суть вопроса. При необходимости передам обращение ответственному менеджеру."
        )
        confidence = ChatAISuggestion.CONF_LOW
        actions = ["ask_clarification", "escalate"]
        needs_escalation = True

    # Этап 1: автоответ всегда запрещён политикой
    auto_ok = policy.auto_reply_allowed(category=category, confidence=confidence)

    return AISuggestResult(
        text=text.strip(),
        category=category,
        confidence=confidence,
        sources=sources,
        warnings=warnings,
        suggested_actions=actions,
        needs_escalation=needs_escalation,
        auto_reply_allowed=auto_ok,
        model_name="stub-v1",
    )


def serialize_suggestion(obj: ChatAISuggestion) -> dict[str, Any]:
    return {
        "id": obj.id,
        "thread_id": obj.thread_id,
        "agency_id": obj.agency_id,
        "status": obj.status,
        "text": obj.proposed_text,
        "final_text": obj.final_text,
        "category": obj.category,
        "confidence": obj.confidence,
        "sources": obj.sources or [],
        "warnings": obj.warnings or [],
        "suggested_actions": obj.suggested_actions or [],
        "needs_escalation": obj.needs_escalation,
        "auto_reply_allowed": obj.auto_reply_allowed,
        "model_name": obj.model_name,
        "manager_feedback": obj.manager_feedback,
        "created_at": obj.created_at.isoformat() if obj.created_at else "",
    }


def suggest_reply_for_thread(
    *,
    thread: ChatThread,
    user,
    role: str = "",
    agency_id: int | None = None,
) -> ChatAISuggestion:
    agency_id = int(agency_id or thread.agency_id)
    policy = AIActionPolicy(role=role or "", agency_id=agency_id, thread=thread)
    ok, reason = policy.copilot_allowed()
    if not ok:
        raise ChatAIError(reason, code="forbidden")

    trigger = _last_client_message(thread)
    recent = _recent_messages(thread)
    result = _build_stub_result(thread=thread, trigger=trigger, policy=policy)

    suggestion = ChatAISuggestion.objects.create(
        thread=thread,
        agency_id=agency_id,
        trigger_message=trigger,
        requested_by=user if getattr(user, "is_authenticated", False) else None,
        prompt_context={
            "agency_id": agency_id,
            "thread_id": thread.id,
            "order_id": thread.order_id or "",
            "order_type": thread.order_type or "",
            "role": role or "",
            "recent_messages": recent,
            "trigger_message_id": trigger.id if trigger else None,
            "forbidden_actions": policy.forbidden_actions(),
        },
        category=result.category,
        confidence=result.confidence,
        proposed_text=result.text,
        sources=result.to_payload()["sources"],
        warnings=result.warnings,
        suggested_actions=result.suggested_actions,
        needs_escalation=result.needs_escalation,
        auto_reply_allowed=False,  # жёстко на этапе 1
        status=ChatAISuggestion.STATUS_DRAFT,
        model_name=result.model_name,
    )
    return suggestion


def apply_suggestion_feedback(
    *,
    suggestion: ChatAISuggestion,
    user,
    action: str,
    final_text: str = "",
    feedback: str = "",
    note: str = "",
    enqueue_knowledge: bool = False,
) -> ChatAISuggestion:
    action = (action or "").strip().lower()
    now = timezone.now()
    text = str(final_text or "").strip()

    if action == "insert":
        suggestion.status = ChatAISuggestion.STATUS_INSERTED
        if text:
            suggestion.final_text = text
            if text != (suggestion.proposed_text or "").strip():
                suggestion.status = ChatAISuggestion.STATUS_EDITED
        suggestion.resolved_at = now
    elif action == "reject":
        suggestion.status = ChatAISuggestion.STATUS_REJECTED
        suggestion.resolved_at = now
    elif action == "sent":
        suggestion.status = ChatAISuggestion.STATUS_SENT
        if text:
            suggestion.final_text = text
        suggestion.resolved_at = now
    elif action == "escalate":
        suggestion.status = ChatAISuggestion.STATUS_ESCALATED
        suggestion.needs_escalation = True
        suggestion.resolved_at = now
    elif action == "feedback":
        pass
    else:
        raise ChatAIError("Неизвестное действие", code="bad_action")

    if feedback:
        allowed = {c[0] for c in ChatAISuggestion.FEEDBACK_CHOICES}
        if feedback not in allowed:
            raise ChatAIError("Некорректная оценка", code="bad_feedback")
        suggestion.manager_feedback = feedback
    if note:
        suggestion.feedback_note = note[:2000]
    suggestion.save()

    if enqueue_knowledge and suggestion.proposed_text:
        ChatAIKnowledgeCandidate.objects.create(
            suggestion=suggestion,
            agency=suggestion.agency,
            question=(suggestion.trigger_message.text if suggestion.trigger_message_id else "")[:4000],
            answer=(suggestion.final_text or suggestion.proposed_text)[:8000],
            category=suggestion.category,
            client_visible=False,
            created_by=user if getattr(user, "is_authenticated", False) else None,
        )
    return suggestion
