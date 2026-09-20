"""Controller self-service transfer of totes awaiting verification."""
from django.db.models import Q
from django.shortcuts import render, redirect
from django.contrib import messages
from django.views.decorators.http import require_http_methods
from employees.access import role_required
from .exceptions import FbsError
from .models import FbsToteBinding, FbsControllerSession
from .services.controller_shift import controller_shift_is_live
from .services.controller_tote_transfer import transfer_waiting_controller_tote
from .tsd_views import fbs_module_required, _base_context, _controller_settings_user_name


def waiting_tote_transfer_context(request, *, error=""):
    query = str(request.GET.get("q") or "").strip()
    # No source-operator filter: any controller may reassign an unclaimed tote.
    bindings = FbsToteBinding.objects.filter(
        state=FbsToteBinding.STATE_WAITING_CONTROL,
        workstation__isnull=False, pick_batch__isnull=False,
        pick_batch__status="verification", pick_batch__verification_started_at__isnull=True,
        pick_batch__verification_assigned_to__isnull=True,
        pick_batch__cart_released_at__isnull=True, pick_batch__completed_at__isnull=True,
    ).select_related("tote", "workstation", "pick_batch__agency")
    if query:
        criteria = Q(tote__barcode__icontains=query) | Q(tote__name__icontains=query) | Q(workstation__name__icontains=query) | Q(pick_batch__agency__agn_name__icontains=query)
        if query.isdigit(): criteria |= Q(pick_batch_id=int(query))
        bindings = bindings.filter(criteria)
    targets = []
    for session in FbsControllerSession.objects.filter(status="active", controller__is_active=True,
        workstation__is_active=True).select_related("controller__employee_profile", "workstation").order_by("workstation__name"):
        if session.workstation.shift_controller_id == session.controller_id and controller_shift_is_live(session.workstation):
            session.controller_name = _controller_settings_user_name(session.controller)
            targets.append(session)
    rows = list(bindings.order_by("workstation__name", "updated_at", "id"))
    for row in rows:
        row.transfer_targets = [s for s in targets if s.workstation_id != row.workstation_id]
    return _base_context(request, page_title="Передать тару", rows=rows, query=query, error=error)


@fbs_module_required
@role_required("fbs_controller", "storekeeper", "head_manager", "director", "admin")
@require_http_methods(["GET", "POST"])
def controller_tote_transfers(request):
    error = ""
    if request.method == "POST":
        try:
            binding = transfer_waiting_controller_tote(
                binding_id=int(request.POST.get("binding_id") or 0),
                expected_workstation_id=int(request.POST.get("source_workstation_id") or 0),
                target_session_id=int(request.POST.get("target_session_id") or 0),
                performed_by=request.user)
        except (TypeError, ValueError, FbsError) as exc:
            error = str(exc)
        else:
            messages.success(request, f"Тара {binding.tote.barcode} передана на {binding.workstation.name}. "
                "Новый контролер может принять ее сканированием QR.")
            return redirect("fbs:controller_tote_transfers")
    return render(request, "fbs/controller_tote_transfers.html",
        waiting_tote_transfer_context(request, error=error), status=400 if error else 200)
