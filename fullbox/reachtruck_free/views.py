from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect
from django.views.generic import TemplateView

from employees.access import RoleRequiredMixin, get_request_role, resolve_cabinet_url

from .empty_location import (
    PHYSICAL_EMPTY_LOCATION_ROLES,
    confirm_physical_empty_location,
)
from .services import (
    ALLOWED_ROLES,
    SESSION_INSPECTION_KEY,
    active_move_same_pallet_scan,
    cancel_free_move,
    complete_free_move,
    inspect_pallet,
    inspect_scan,
    moving_context,
    resume_active_free_move,
    start_free_move,
)


class ReachtruckFreeDashboardView(RoleRequiredMixin, TemplateView):
    template_name = "reachtruck_free/dashboard.html"
    allowed_roles = ALLOWED_ROLES

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        request = self.request
        role = get_request_role(request)
        ctx.update(
            {
                "role": role,
                "cabinet_url": resolve_cabinet_url(role),
                "can_confirm_physical_empty": role in PHYSICAL_EMPTY_LOCATION_ROLES,
                "scan_value": "",
                "scan_info": kwargs.get("scan_info") or None,
                "error": kwargs.get("error") or "",
                "ok_message": kwargs.get("ok_message") or "",
                "done_message": (
                    "Перемещение завершено."
                    if request.method == "GET" and request.GET.get("done")
                    else ""
                ),
                "sound_event": kwargs.get("sound_event")
                or (str(request.GET.get("sound") or "").strip() if request.method == "GET" else ""),
            }
        )
        move_ctx = moving_context(request)
        if move_ctx:
            ctx.update(move_ctx)
            return ctx
        last_scan = request.session.get(SESSION_INSPECTION_KEY)
        if last_scan and not ctx.get("scan_info"):
            ctx["scan_info"] = inspect_scan(last_scan)
        return ctx

    def post(self, request, *args, **kwargs):
        action = str(request.POST.get("action") or "").strip()
        role = get_request_role(request)
        if action == "confirm_location_empty":
            code = str(request.POST.get("location_code") or "").strip()
            try:
                result = confirm_physical_empty_location(
                    scan_value=code,
                    user=request.user,
                    role=role or "",
                )
            except ValueError as exc:
                info = inspect_scan(code)
                ctx = self.get_context_data(
                    scan_info=info,
                    error=str(exc),
                    sound_event="game_over",
                )
                ctx["scan_info"] = info
                return self.render_to_response(ctx, status=400)
            info = inspect_scan(code)
            if result.released or result.already_free:
                request.session.pop(SESSION_INSPECTION_KEY, None)
            else:
                request.session[SESSION_INSPECTION_KEY] = code
            request.session.modified = True
            ctx = self.get_context_data(
                scan_info=info,
                ok_message=result.message if result.released or result.already_free else "",
                error="" if result.released or result.already_free else result.message,
                sound_event="confirm" if result.released or result.already_free else "game_over",
            )
            ctx["scan_info"] = info
            return self.render_to_response(ctx, status=200)
        if action in {"scan_object", "scan_pallet"}:
            code = str(request.POST.get("scan_value") or "").strip()
            info = inspect_scan(code)
            if (
                info.get("found")
                and info.get("object_type") in {"pallet", "box"}
                and resume_active_free_move(
                    request,
                    pallet_code=info.get("pallet_code") or info.get("code") or code,
                    role=role or "",
                )
            ):
                ctx = self.get_context_data(sound_event="info")
                ctx["scan_value"] = ""
                return self.render_to_response(ctx, status=200)
            if info.get("found"):
                request.session[SESSION_INSPECTION_KEY] = info.get("code") or code
                request.session.modified = True
            else:
                request.session.pop(SESSION_INSPECTION_KEY, None)
                request.session.modified = True
            ctx = self.get_context_data(scan_info=info)
            ctx["scan_info"] = info
            ctx["scan_value"] = ""
            if info.get("found") and info.get("object_type") in {"pallet", "box"} and info.get("can_move"):
                ctx["sound_event"] = "happy"
            elif info.get("found"):
                ctx["sound_event"] = "info"
            else:
                ctx["sound_event"] = "game_over"
            return self.render_to_response(ctx, status=200)
        if action == "decline_move":
            request.session.pop(SESSION_INSPECTION_KEY, None)
            request.session.modified = True
            return redirect("/reachtruck-free/")
        if action == "start_move":
            code = str(
                request.POST.get("object_code")
                or request.POST.get("pallet_code")
                or ""
            ).strip()
            try:
                result = start_free_move(request, pallet_code=code, role=role or "")
            except ValueError as exc:
                ctx = self.get_context_data(error=str(exc), sound_event="game_over")
                ctx["scan_info"] = inspect_scan(code)
                return self.render_to_response(ctx, status=400)
            ctx = self.get_context_data(ok_message=result.message, sound_event="confirm")
            return self.render_to_response(ctx)
        if action == "scan_destination":
            scan_value = str(request.POST.get("scan_value") or "").strip()
            if active_move_same_pallet_scan(request, scan_value):
                ctx = self.get_context_data(
                    ok_message="Паллета уже взята в перемещение. Сканируйте новое место.",
                    sound_event="info",
                )
                return self.render_to_response(ctx, status=200)
            try:
                complete_free_move(request, scan_value=scan_value)
            except ValueError as exc:
                ctx = self.get_context_data(error=str(exc), sound_event="game_over")
                return self.render_to_response(ctx, status=400)
            return redirect("/reachtruck-free/?done=1&sound=fanfare")
        if action == "cancel_move":
            message = cancel_free_move(request)
            ctx = self.get_context_data(ok_message=message)
            return self.render_to_response(ctx)
        return redirect("/reachtruck-free/")


@login_required
def dashboard(request):
    return ReachtruckFreeDashboardView.as_view()(request)
