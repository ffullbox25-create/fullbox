"""Queue worker for constrained, read-only AI incident diagnosis."""

from __future__ import annotations

from django.db import connection, transaction

from .diagnostics import collect_safe_diagnostics
from .models import AgentAction, Incident, IncidentMessage
from .policy import POLICY_AUTOMATIC


def _claim_next_action():
    with transaction.atomic():
        queryset = AgentAction.objects.filter(
            action_type="inspect_request",
            policy_level=POLICY_AUTOMATIC,
            status=AgentAction.STATUS_PROPOSED,
            incident__status__in=(Incident.STATUS_NEW, Incident.STATUS_DIAGNOSING),
        ).select_related("incident")
        if connection.features.has_select_for_update_skip_locked:
            queryset = queryset.select_for_update(skip_locked=True)
        else:
            queryset = queryset.select_for_update()
        action = queryset.order_by("created_at", "id").first()
        if not action:
            return None
        action.status = AgentAction.STATUS_RUNNING
        action.error = ""
        action.save(update_fields=("status", "error", "policy_level", "updated_at"))
        incident = action.incident
        incident.status = Incident.STATUS_DIAGNOSING
        incident.save(update_fields=("status", "updated_at"))
        return action


def _model_input(incident: Incident, diagnostics: dict) -> dict:
    messages = list(
        incident.messages.order_by("-created_at", "-id").values(
            "sender_type", "body", "created_at"
        )[:8]
    )
    messages.reverse()
    return {
        "incident": {
            "number": incident.number,
            "zone": incident.zone,
            "object_type": incident.object_type,
            "object_id": incident.object_id,
            "source_url": incident.source_url,
            "description": incident.description,
            "expected_result": incident.expected_result,
            "severity": incident.severity,
            "reproducible": incident.reproducible,
        },
        "recent_messages": messages,
        "diagnostics": diagnostics,
        "constraints": {
            "read_only": True,
            "production_changes_allowed": False,
            "stock_changes_allowed": False,
            "movement_changes_allowed": False,
        },
    }


def _target_status(diagnosis: dict) -> str:
    if diagnosis.get("needs_more_info"):
        return Incident.STATUS_NEEDS_INFO
    if (
        diagnosis.get("risk_area") != "none"
        or diagnosis.get("recommended_action") == "escalate_programmer"
    ):
        return Incident.STATUS_ESCALATED
    return Incident.STATUS_CAUSE_FOUND


def _employee_message(diagnosis: dict) -> str:
    body = str(
        diagnosis.get("employee_reply")
        or diagnosis.get("summary")
        or "Диагностика завершена."
    ).strip()
    question = str(diagnosis.get("question") or "").strip()
    if diagnosis.get("needs_more_info") and question:
        body = f"{body}\n\nУточните, пожалуйста: {question}"
    return (
        f"{body}\n\n"
        "Проверка выполнена в режиме чтения. Остатки, движения, рабочие заявки и код не изменялись."
    )[:6000]


def process_next_incident(*, diagnoser, diagnostics_collector=collect_safe_diagnostics):
    action = _claim_next_action()
    if not action:
        return None
    incident = action.incident
    try:
        diagnostics = diagnostics_collector(incident)
        result = diagnoser.diagnose(_model_input(incident, diagnostics))
        diagnosis = result.diagnosis
        with transaction.atomic():
            locked_action = AgentAction.objects.select_for_update().get(pk=action.pk)
            locked_incident = Incident.objects.select_for_update().get(pk=incident.pk)
            locked_action.status = AgentAction.STATUS_SUCCEEDED
            locked_action.result = {
                "diagnosis": diagnosis,
                "provider": result.provider_metadata,
                "diagnostics": {
                    "read_only": True,
                    "services": diagnostics.get("services") or {},
                    "log_line_count": int(diagnostics.get("log_line_count") or 0),
                },
            }
            locked_action.error = ""
            locked_action.save()
            locked_incident.status = _target_status(diagnosis)
            locked_incident.diagnosis = str(
                diagnosis.get("probable_cause") or ""
            )[:8000]
            locked_incident.save(update_fields=("status", "diagnosis", "updated_at"))
            IncidentMessage.objects.create(
                incident=locked_incident,
                sender_type=IncidentMessage.SENDER_AGENT,
                body=_employee_message(diagnosis),
                metadata={
                    "automatic": True,
                    "stage": "diagnosis_completed",
                    "confidence": diagnosis.get("confidence"),
                    "recommended_action": diagnosis.get("recommended_action"),
                },
            )
        return locked_action
    except Exception as exc:
        with transaction.atomic():
            locked_action = AgentAction.objects.select_for_update().get(pk=action.pk)
            locked_incident = Incident.objects.select_for_update().get(pk=incident.pk)
            locked_action.status = AgentAction.STATUS_FAILED
            locked_action.error = f"{type(exc).__name__}: {str(exc)}"[:1000]
            locked_action.save()
            locked_incident.status = Incident.STATUS_ESCALATED
            locked_incident.save(update_fields=("status", "updated_at"))
            IncidentMessage.objects.create(
                incident=locked_incident,
                sender_type=IncidentMessage.SENDER_SYSTEM,
                body=(
                    "Автоматическую диагностику выполнить не удалось. Обращение передано "
                    "ответственному программисту; рабочие данные не изменялись."
                ),
                metadata={"automatic": True, "stage": "diagnosis_failed"},
            )
        return locked_action
