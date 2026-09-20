"""HTTP view for the manager queue."""

from django.views.generic import TemplateView

from employees.access import RoleRequiredMixin

from .queue import manager_queue
from .roles import CABINET_ROLES
from .views import _team_request_role


class TeamManagerQueueView(RoleRequiredMixin, TemplateView):
    template_name = "teammanager/queue.html"
    allowed_roles = CABINET_ROLES

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        queue_context = manager_queue(self.request)
        context.update(queue_context)
        context.update(
            {
                "role": _team_request_role(self.request) or "manager",
                "title": "Очередь менеджера",
                "active_nav": "queue",
                "manager_queue_nav_count": queue_context["active_total"],
                "employee_name": (
                    getattr(getattr(self.request.user, "employee_profile", None), "full_name", None)
                    or self.request.session.get("employee_name")
                    or self.request.user.get_full_name()
                    or self.request.user.username
                ),
            }
        )
        return context
