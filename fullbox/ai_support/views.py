from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from employees.access import get_request_role, resolve_cabinet_url

from .forms import IncidentCreateForm, IncidentMessageForm
from .models import Incident, IncidentMessage
from .services import (
    create_incident,
    user_can_review_incidents,
    user_can_use_ai_support,
    visible_incidents,
)


def _access_denied(request):
    if user_can_use_ai_support(request):
        return None
    return HttpResponseForbidden("ИИ-поддержка недоступна для вашей роли")


def _navigation_context(request):
    return {"cabinet_url": resolve_cabinet_url(get_request_role(request))}


@login_required
def incident_list(request):
    denied = _access_denied(request)
    if denied:
        return denied
    incidents = visible_incidents(request)
    status = str(request.GET.get("status") or "").strip()
    if status in dict(Incident.STATUS_CHOICES):
        incidents = incidents.filter(status=status)
    return render(
        request,
        "ai_support/incident_list.html",
        {
            "incidents": incidents[:200],
            "selected_status": status,
            "status_choices": Incident.STATUS_CHOICES,
            "can_review": user_can_review_incidents(request),
            **_navigation_context(request),
        },
    )


@login_required
def incident_create(request):
    denied = _access_denied(request)
    if denied:
        return denied
    initial = {
        "zone": str(request.GET.get("zone") or "").strip()[:80],
        "object_type": str(request.GET.get("object_type") or "").strip()[:64],
        "object_id": str(request.GET.get("object_id") or "").strip()[:128],
        "source_url": str(request.GET.get("source_url") or "").strip()[:1000],
        "page_title": str(request.GET.get("page_title") or "").strip()[:255],
    }
    if request.method == "POST":
        form = IncidentCreateForm(request.POST)
        if form.is_valid():
            incident = create_incident(request=request, form=form)
            messages.success(request, f"Обращение {incident.number} создано")
            return redirect("ai_support:detail", pk=incident.pk)
    else:
        form = IncidentCreateForm(initial=initial)
    return render(
        request,
        "ai_support/incident_form.html",
        {"form": form, **_navigation_context(request)},
    )


@login_required
def incident_detail(request, pk: int):
    denied = _access_denied(request)
    if denied:
        return denied
    incident = get_object_or_404(visible_incidents(request), pk=pk)
    return render(
        request,
        "ai_support/incident_detail.html",
        {
            "incident": incident,
            "incident_messages": incident.messages.select_related("author"),
            "agent_actions": incident.agent_actions.select_related("approved_by"),
            "message_form": IncidentMessageForm(),
            "can_review": user_can_review_incidents(request),
            "status_choices": Incident.STATUS_CHOICES,
            **_navigation_context(request),
        },
    )


@login_required
@require_POST
def incident_message(request, pk: int):
    denied = _access_denied(request)
    if denied:
        return denied
    incident = get_object_or_404(visible_incidents(request), pk=pk)
    form = IncidentMessageForm(request.POST)
    if form.is_valid():
        message = form.save(commit=False)
        message.incident = incident
        message.author = request.user
        message.sender_type = (
            IncidentMessage.SENDER_PROGRAMMER
            if user_can_review_incidents(request)
            else IncidentMessage.SENDER_EMPLOYEE
        )
        message.save()
        if incident.status == Incident.STATUS_NEEDS_INFO:
            incident.status = Incident.STATUS_DIAGNOSING
            incident.save(update_fields=("status", "updated_at"))
    return redirect("ai_support:detail", pk=incident.pk)


@login_required
@require_POST
def incident_status(request, pk: int):
    denied = _access_denied(request)
    if denied:
        return denied
    if not user_can_review_incidents(request):
        return HttpResponseForbidden("Изменять статус может только ответственный сотрудник")
    incident = get_object_or_404(Incident, pk=pk)
    status = str(request.POST.get("status") or "").strip()
    if status not in dict(Incident.STATUS_CHOICES):
        messages.error(request, "Неизвестный статус")
        return redirect("ai_support:detail", pk=incident.pk)
    incident.status = status
    incident.closed_at = timezone.now() if status == Incident.STATUS_CLOSED else None
    incident.save(update_fields=("status", "closed_at", "updated_at"))
    IncidentMessage.objects.create(
        incident=incident,
        author=request.user,
        sender_type=IncidentMessage.SENDER_SYSTEM,
        body=f"Статус изменён: {incident.get_status_display()}.",
        metadata={"status": status},
    )
    return redirect("ai_support:detail", pk=incident.pk)
