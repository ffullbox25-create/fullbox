from __future__ import annotations

import json

from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from client_cabinet.web_ui import _get_client_for_request

from .models import BillingAct
from .serializers import billing_act_dict
from .services import BillingWorkflowService
from .statuses import ActStatus


def _json_error(message: str, status: int = 400):
    return JsonResponse({"ok": False, "error": str(message)}, status=status)


def _client_context(request):
    agency, _client_view, allowed = _get_client_for_request(request)
    if not allowed or not agency:
        return None
    return agency


def _json_body(request):
    if not request.body:
        return {}
    return json.loads(request.body.decode("utf-8"))


@login_required
@require_GET
def client_billing_acts(request):
    agency = _client_context(request)
    if not agency:
        return _json_error("Access denied", 403)
    qs = BillingAct.objects.filter(client=agency, status__in=[ActStatus.SENT, ActStatus.DISPUTED, ActStatus.CONFIRMED]).order_by("-sent_at", "-id")
    return JsonResponse({"ok": True, "data": [billing_act_dict(act, include_lines=True) for act in qs[:100]]})


@csrf_exempt
@login_required
@require_POST
def client_confirm_act(request, pk: int):
    agency = _client_context(request)
    if not agency:
        return _json_error("Access denied", 403)
    act = get_object_or_404(BillingAct, pk=pk, client=agency)
    try:
        data = _json_body(request)
        act = BillingWorkflowService.confirm_act(act, user=request.user, comment=data.get("comment", ""), request=request)
        return JsonResponse({"ok": True, "data": billing_act_dict(act, include_lines=True)})
    except (ValidationError, json.JSONDecodeError) as exc:
        return _json_error(exc)


@csrf_exempt
@login_required
@require_POST
def client_dispute_act(request, pk: int):
    agency = _client_context(request)
    if not agency:
        return _json_error("Access denied", 403)
    act = get_object_or_404(BillingAct, pk=pk, client=agency)
    try:
        data = _json_body(request)
        comment = str(data.get("comment") or "").strip()
        if not comment:
            raise ValidationError("Укажите комментарий к разногласиям.")
        dispute = BillingWorkflowService.dispute_act(
            act,
            user=request.user,
            comment=comment,
            expected_quantity=data.get("expected_quantity") or None,
            expected_amount=data.get("expected_amount") or None,
            request=request,
        )
        act.refresh_from_db()
        return JsonResponse({"ok": True, "data": {"dispute_id": dispute.id, "act": billing_act_dict(act, include_lines=True)}})
    except (ValidationError, json.JSONDecodeError) as exc:
        return _json_error(exc)
