from django.shortcuts import redirect
from django.views.generic import TemplateView

from employees.access import RoleRequiredMixin

from .models import BoxMoveOperation
from .services import (
    ALLOWED_ROLES,
    active_operation,
    cancel_operation,
    clear_pending_box,
    complete_operation,
    confirm_source_box,
    finish_selection,
    operation_for_display,
    operation_stats,
    pending_box,
    report_destination_box_missing,
    scan_destination_box,
    scan_destination_pallet,
    scan_source_box,
)


class ReachtruckBoxMoveDashboardView(RoleRequiredMixin, TemplateView):
    template_name = "reachtruck_box_move/dashboard.html"
    allowed_roles = ALLOWED_ROLES

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        operation = kwargs.get("operation")
        if operation is None:
            operation = operation_for_display(self.request)
        last_box = kwargs.get("last_box")
        if (
            last_box is None
            and operation
            and operation.status == BoxMoveOperation.STATUS_SELECTING
        ):
            last_box = pending_box(self.request)
        ctx.update(
            {
                "operation": operation,
                "last_box": last_box,
                "error": kwargs.get("error") or "",
                "ok_message": kwargs.get("ok_message") or "",
                "done_message": kwargs.get("done_message") or "",
                "sound_event": kwargs.get("sound_event") or self.request.GET.get("sound") or "",
                "inventory_url": "/reachtruck/?mobile_category=inventory",
            }
        )
        if operation:
            ctx["stats"] = operation_stats(operation)
            ctx["is_final"] = operation.status in {
                BoxMoveOperation.STATUS_DONE,
                BoxMoveOperation.STATUS_DONE_WITH_DISCREPANCY,
                BoxMoveOperation.STATUS_CANCELED,
            }
        else:
            ctx["stats"] = {}
            ctx["is_final"] = False
        if self.request.GET.get("done") == "1" and not ctx["done_message"]:
            ctx["done_message"] = "Операция перемещения коробов завершена."
        return ctx

    def _render(self, **kwargs):
        return self.render_to_response(self.get_context_data(**kwargs))

    def post(self, request, *args, **kwargs):
        action = str(request.POST.get("action") or "").strip()
        operation = active_operation(request)

        if action == "new_operation":
            if operation:
                cancel_operation(operation, request)
            return redirect("/reachtruck-box-move/")

        if action == "scan_box":
            operation, card, error = scan_source_box(request, str(request.POST.get("scan_value") or "").strip())
            if error:
                return self._render(operation=operation, error=error, sound_event="game_over")
            return self._render(operation=operation, last_box=card, sound_event="info")

        if action == "confirm_box":
            if operation is None:
                return redirect("/reachtruck-box-move/")
            operation, error = confirm_source_box(operation, str(request.POST.get("box_code") or "").strip())
            if error:
                return self._render(operation=operation, error=error, sound_event="game_over")
            clear_pending_box(request)
            return self._render(operation=operation, ok_message="Короб выбран.", sound_event="confirm")

        if action == "finish_selection":
            if operation is None:
                return redirect("/reachtruck-box-move/")
            operation, error = finish_selection(operation)
            if error:
                return self._render(operation=operation, error=error, sound_event="game_over")
            clear_pending_box(request)
            return self._render(operation=operation, ok_message="Сканируйте паллету назначения.", sound_event="confirm")

        if action == "scan_destination_pallet":
            if operation is None:
                return redirect("/reachtruck-box-move/")
            operation, error = scan_destination_pallet(operation, str(request.POST.get("scan_value") or "").strip())
            if error:
                return self._render(operation=operation, error=error, sound_event="game_over")
            return self._render(operation=operation, ok_message="Паллета назначения выбрана. Проверьте ее короба.", sound_event="happy")

        if action == "scan_destination_box":
            if operation is None:
                return redirect("/reachtruck-box-move/")
            operation, ok_message, error = scan_destination_box(operation, str(request.POST.get("scan_value") or "").strip())
            if error:
                return self._render(operation=operation, error=error, sound_event="game_over")
            return self._render(operation=operation, ok_message=ok_message, sound_event="info")

        if action == "report_missing_box":
            if operation is None:
                return redirect("/reachtruck-box-move/")
            operation, ok_message, error = report_destination_box_missing(
                operation,
                str(request.POST.get("box_code") or "").strip(),
                request,
            )
            if error:
                return self._render(operation=operation, error=error, sound_event="game_over")
            return self._render(operation=operation, ok_message=ok_message, sound_event="info")

        if action == "complete":
            if operation is None:
                return redirect("/reachtruck-box-move/")
            try:
                operation, message = complete_operation(operation, request)
            except ValueError as exc:
                return self._render(operation=operation, error=str(exc), sound_event="game_over")
            sound = "game_over" if operation.status == BoxMoveOperation.STATUS_DONE_WITH_DISCREPANCY else "fanfare"
            return redirect(f"/reachtruck-box-move/?operation={operation.id}&done=1&sound={sound}")

        if action == "cancel":
            if operation:
                cancel_operation(operation, request)
            return redirect("/reachtruck/?mobile_category=inventory")

        return redirect("/reachtruck-box-move/")
