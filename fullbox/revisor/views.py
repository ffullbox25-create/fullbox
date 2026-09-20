from django.http import HttpResponse, HttpResponseBadRequest
from django.views import View
from django.views.generic import TemplateView

from employees.access import RoleRequiredMixin

from .services import build_product_check_workbook, product_check_filename


REVISOR_ROLES = ("head_manager", "director", "admin", "developer")


class RevisorIndexView(RoleRequiredMixin, TemplateView):
    template_name = "revisor/index.html"
    allowed_roles = REVISOR_ROLES


class RevisorProductCheckView(RoleRequiredMixin, View):
    allowed_roles = REVISOR_ROLES

    def get(self, request, *args, **kwargs):
        article = str(request.GET.get("sku") or request.GET.get("article") or "").strip()
        if not article:
            return HttpResponseBadRequest("Укажите артикул или SKU")
        output = build_product_check_workbook(article)
        response = HttpResponse(
            output.getvalue(),
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        response["Content-Disposition"] = f'attachment; filename="{product_check_filename(article)}"'
        return response
