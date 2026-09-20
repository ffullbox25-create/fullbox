from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.views.generic import TemplateView

from employees.access import RoleRequiredMixin

from .models import InventoryTask
from .services import (
    clear_discrepancy_containers,
    release_expired_tasks,
    release_task,
    save_box_count,
    scan_discrepancy_container,
    submit_discrepancy_report,
    take_task,
    _task_is_empty_count,
    touch_task_lease,
    verify_location,
    visible_tasks,
)


ALLOWED_ROLES = (
    "reachtruck_driver",
    "head_manager",
    "director",
    "admin",
)


class ReachtruckInventoryDashboardView(RoleRequiredMixin, TemplateView):
    template_name = "reachtruck_inventory/dashboard.html"
    allowed_roles = ALLOWED_ROLES

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        tasks = visible_tasks(self.request.user)
        context.update(
            {
                "tasks": tasks,
                "pending_count": tasks.filter(status=InventoryTask.STATUS_CREATED).count(),
                "in_progress_count": tasks.filter(status=InventoryTask.STATUS_IN_PROGRESS).count(),
                "recent_tasks": InventoryTask.objects.select_related("inventory", "location")
                .filter(status=InventoryTask.STATUS_COMPLETED, assigned_to=self.request.user)
                .order_by("-completed_at")[:5],
            }
        )
        return context


class ReachtruckInventoryTaskView(RoleRequiredMixin, TemplateView):
    template_name = "reachtruck_inventory/task.html"
    allowed_roles = ALLOWED_ROLES

    def _task(self):
        release_expired_tasks(task_id=self.kwargs["pk"])
        task = get_object_or_404(
            InventoryTask.objects.select_related(
                "inventory",
                "inventory__agency",
                "inventory__sku",
                "scope_location",
                "location",
                "assigned_to",
            ),
            pk=self.kwargs["pk"],
        )
        if (
            task.status == InventoryTask.STATUS_IN_PROGRESS
            and task.assigned_to_id != self.request.user.id
            and not self.request.user.is_superuser
        ):
            raise PermissionDenied("Задание выполняет другой сотрудник.")
        return task

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        task = kwargs.get("task") or self._task()
        lines = task.inventory.lines.filter(location_id=task.location_id).select_related("agency", "sku_ref")
        has_saved_discrepancy = (
            task.counted_at is not None
            and task.actual_box_count != task.planned_box_count
        )
        is_empty_count = has_saved_discrepancy and _task_is_empty_count(task)
        scanned_box_count = len(task.discrepancy_box_codes or [])
        context.update(
            {
                "task": task,
                "lines": lines,
                "error": kwargs.get("error") or "",
                "ok_message": kwargs.get("ok_message") or "",
                "can_take": task.status == InventoryTask.STATUS_CREATED,
                "can_count": task.status == InventoryTask.STATUS_IN_PROGRESS and task.assigned_to_id == self.request.user.id,
                "is_done": task.status == InventoryTask.STATUS_COMPLETED,
                "has_saved_discrepancy": has_saved_discrepancy,
                "is_empty_count": is_empty_count,
                "scanned_box_count": scanned_box_count,
                "can_submit_discrepancy": bool(
                    is_empty_count
                    or (
                        task.discrepancy_pallet_codes
                        and scanned_box_count == int(task.actual_box_count or 0)
                    )
                ),
            }
        )
        return context

    def _render(self, **kwargs):
        return self.render_to_response(self.get_context_data(**kwargs))

    def post(self, request, *args, **kwargs):
        task = self._task()
        action = str(request.POST.get("action") or "").strip()
        try:
            if action == "take":
                task = take_task(task, request.user)
                messages.success(request, "Задание принято. Отсканируйте складское место.")
                return redirect("reachtruck_inventory:task", pk=task.pk)
            if action == "verify_location":
                task = verify_location(task, request.user, request.POST.get("scan_value"))
                messages.success(request, "Место подтверждено. Выполните пересчет.")
                return redirect("reachtruck_inventory:task", pk=task.pk)
            if action == "heartbeat":
                task = touch_task_lease(task, request.user)
                return JsonResponse(
                    {
                        "ok": True,
                        "lease_expires_at": task.lease_expires_at.isoformat(),
                    }
                )
            if action == "release":
                release_task(task, request.user)
                messages.success(request, "Задание освобождено и снова доступно сотрудникам.")
                return redirect("reachtruck_inventory:dashboard")
            if action == "scan_discrepancy_pallet":
                scan_discrepancy_container(
                    task,
                    request.user,
                    container_type="pallet",
                    scan_code=request.POST.get("scan_code"),
                )
                messages.success(request, "Паллета добавлена к отчету о расхождении.")
                return redirect("reachtruck_inventory:task", pk=task.pk)
            if action == "scan_discrepancy_box":
                scan_discrepancy_container(
                    task,
                    request.user,
                    container_type="box",
                    scan_code=request.POST.get("scan_code"),
                )
                messages.success(request, "Короб добавлен к отчету о расхождении.")
                return redirect("reachtruck_inventory:task", pk=task.pk)
            if action == "clear_discrepancy_containers":
                clear_discrepancy_containers(task, request.user)
                messages.success(request, "Сканы паллет и коробов очищены.")
                return redirect("reachtruck_inventory:task", pk=task.pk)
            if action == "submit_discrepancy":
                task = submit_discrepancy_report(task, request.user)
                messages.success(request, "Вся информация отправлена начальнику склада.")
                return redirect(f"/reachtruck-inventory/{task.id}/?done=1")
            if action == "complete":
                task = save_box_count(
                    task,
                    request.user,
                    request.POST.get("box_count"),
                )
                if task.status != InventoryTask.STATUS_COMPLETED:
                    if int(task.actual_box_count or 0) == 0:
                        messages.warning(
                            request,
                            "Не совпало. Сообщите о пустом месте начальнику склада.",
                        )
                    else:
                        messages.warning(
                            request,
                            "Не совпало. Отсканируйте паллету и все найденные короба.",
                        )
                    return redirect("reachtruck_inventory:task", pk=task.pk)
                return redirect(f"/reachtruck-inventory/{task.id}/?done=1")
            raise ValidationError("Неизвестное действие.")
        except ValidationError as exc:
            if action == "heartbeat":
                return JsonResponse(
                    {"ok": False, "error": "; ".join(exc.messages)},
                    status=409,
                )
            return self._render(task=task, error="; ".join(exc.messages))
