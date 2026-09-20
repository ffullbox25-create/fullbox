from urllib.parse import urlsplit, urlunsplit

from django.db import transaction

from employees.access import (
    get_request_role,
    is_developer_login,
    request_has_any_role,
)

from .models import AgentAction, Incident, IncidentMessage


REVIEW_ROLES = {
    "admin",
    "director",
    "developer",
    "head_manager",
    "manager",
    "processing_head",
    "storekeeper",
}

AI_SUPPORT_ALLOWED_ROLES = {
    "admin",
    "director",
    "accountant",
    "hr",
    "developer",
    "head_manager",
    "manager",
    "processing_head",
    "storekeeper",
}


def user_can_review_incidents(request) -> bool:
    return bool(
        request.user.is_superuser
        or request.user.username == "dev"
        or request_has_any_role(request, REVIEW_ROLES)
    )


def user_can_use_ai_support(request) -> bool:
    return bool(
        request.user.is_superuser
        or is_developer_login(request.user)
        or request_has_any_role(request, AI_SUPPORT_ALLOWED_ROLES)
    )


def visible_incidents(request):
    qs = Incident.objects.select_related("reporter")
    if user_can_review_incidents(request):
        return qs
    return qs.filter(reporter=request.user)


def _safe_referer(request) -> str:
    raw = str(request.META.get("HTTP_REFERER") or "").strip()
    if not raw:
        return ""
    parsed = urlsplit(raw)
    path = parsed.path or "/"
    if not path.startswith("/"):
        return ""
    return urlunsplit(("", "", path[:800], parsed.query[:180], ""))[:1000]


def build_incident_context(request, browser_context: dict | None = None) -> dict:
    context = dict(browser_context or {})
    context.update(
        {
            "reporter_role": get_request_role(request) or "",
            "user_agent": str(request.META.get("HTTP_USER_AGENT") or "")[:500],
            "referer": _safe_referer(request),
        }
    )
    return context


def initial_agent_message(incident: Incident) -> str:
    target = incident.object_label
    return (
        f"Обращение {incident.number} принято и поставлено в очередь безопасной диагностики. "
        f"Будут проверены {target}, журналы и связанные статусы. До отдельного подтверждения "
        "остатки, складские движения и данные заявки изменяться не будут."
    )


@transaction.atomic
def create_incident(*, request, form) -> Incident:
    incident = form.save(commit=False)
    incident.reporter = request.user
    incident.reporter_role = get_request_role(request) or ""
    incident.source_url = incident.source_url or _safe_referer(request)
    incident.context = build_incident_context(request, form.cleaned_data.get("context_json"))
    incident.save()
    IncidentMessage.objects.create(
        incident=incident,
        author=request.user,
        sender_type=IncidentMessage.SENDER_EMPLOYEE,
        body=incident.description,
        metadata={"initial_report": True},
    )
    IncidentMessage.objects.create(
        incident=incident,
        sender_type=IncidentMessage.SENDER_AGENT,
        body=initial_agent_message(incident),
        metadata={"automatic": True, "stage": "accepted"},
    )
    AgentAction.objects.create(
        incident=incident,
        action_type="inspect_request",
        title=f"Проверить обращение {incident.number} в режиме чтения",
        payload={
            "zone": incident.zone,
            "object_type": incident.object_type,
            "object_id": incident.object_id,
            "source_url": incident.source_url,
        },
    )
    return incident
