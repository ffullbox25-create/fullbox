from django.http import JsonResponse
from django.shortcuts import redirect
from django.utils.decorators import method_decorator
from django.views.decorators.clickjacking import xframe_options_sameorigin
from django.views.generic import TemplateView
from django.views.decorators.http import require_GET, require_POST

from employees.access import (
    RoleRequiredMixin,
    get_employee_for_user,
    get_request_role,
    resolve_cabinet_url,
    role_required,
)
from .services import (
    AGENT_VERSION,
    ALLOWED_ROLES,
    build_label_settings_context,
    equipment_status_response,
    parse_json_body,
    save_label_settings_request,
    scanner_detect_start_response,
    scanner_detect_status_response,
    scanner_settings_apply_response,
    scanner_settings_save_response,
    scanner_test_response,
    scanner_test_status_response,
)


@method_decorator(xframe_options_sameorigin, name="dispatch")
class LabelSettingsView(RoleRequiredMixin, TemplateView):
    template_name = "labels/settings.html"
    allowed_roles = ALLOWED_ROLES

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx.update(build_label_settings_context())
        ctx["agent_version"] = AGENT_VERSION
        role = get_request_role(self.request)
        employee = get_employee_for_user(self.request.user)
        cabinet_url = resolve_cabinet_url(role)
        requested_return = str(self.request.GET.get("return") or "").strip()
        if requested_return.startswith("/") and not requested_return.startswith("//"):
            return_url = requested_return
        else:
            return_url = cabinet_url
        role_label = employee.get_role_display() if employee else "Сотрудник"
        ctx.update(
            {
                "cabinet_url": cabinet_url,
                "return_url": return_url,
                "workspace_role_label": role_label,
                "return_label": "Назад" if return_url != cabinet_url else "В кабинет",
            }
        )
        return ctx

    def post(self, request, *args, **kwargs):
        return save_label_settings_request(body=request.body)


@role_required(*ALLOWED_ROLES)
@require_POST
def scanner_settings_save(request):
    return scanner_settings_save_response(body=request.body, user=request.user)


@role_required(*ALLOWED_ROLES)
@require_POST
def scanner_settings_apply(request):
    return scanner_settings_apply_response(body=request.body)


@role_required(*ALLOWED_ROLES)
@require_POST
def scanner_test(request):
    return scanner_test_response(body=request.body)


@role_required(*ALLOWED_ROLES)
@require_GET
def scanner_test_status(request, command_id: int):
    return scanner_test_status_response(command_id=command_id)


@role_required(*ALLOWED_ROLES)
@require_POST
def scanner_detect_start(request):
    return scanner_detect_start_response(body=request.body)


@role_required(*ALLOWED_ROLES)
@require_GET
def scanner_detect_status(request):
    return scanner_detect_status_response(
        agent_id=request.GET.get("agent_id"),
        after_id=request.GET.get("after_id"),
    )


@role_required(*ALLOWED_ROLES)
@require_GET
def equipment_status(request):
    return equipment_status_response(
        agent_name=request.GET.get("agent_id"),
        printer_name=request.GET.get("printer"),
    )


@role_required(*ALLOWED_ROLES)
@require_GET
def download_fullbox_agent_bundle(request):
    # Backward-compatible URL; the protected endpoint is the sole installer source.
    return redirect("processing_app:processing-print-agent-setup")
