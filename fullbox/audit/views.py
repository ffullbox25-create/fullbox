from django.db import models
from django.views.generic import ListView

from employees.access import RoleRequiredMixin, STAFF_ROLES
from .models import (
    AuditEntry,
    get_agency_journal,
    get_sku_journal,
    get_staff_overactions_journal,
    get_stock_move_journal,
    OrderAuditEntry,
)


class AuditListView(RoleRequiredMixin, ListView):
    allowed_roles = tuple(STAFF_ROLES)
    template_name = "audit/list.html"
    model = AuditEntry
    context_object_name = "entries"
    paginate_by = 25

    def get_queryset(self):
        qs = (
            AuditEntry.objects.select_related("sku", "user", "journal")
            .filter(journal=get_sku_journal())
            .order_by("-created_at")
        )
        action = self.request.GET.get("action")
        search = (self.request.GET.get("q") or "").strip()
        if action:
            qs = qs.filter(action=action)
        if search:
            qs = qs.filter(
                models.Q(sku__sku_code__icontains=search)
                | models.Q(sku__name__icontains=search)
                | models.Q(description__icontains=search)
                | models.Q(user__username__icontains=search)
            )
        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["current_action"] = self.request.GET.get("action", "")
        ctx["search_value"] = self.request.GET.get("q", "")
        ctx["actions"] = dict(AuditEntry.ACTION_CHOICES)
        return ctx


class OrderAuditListView(RoleRequiredMixin, ListView):
    allowed_roles = tuple(STAFF_ROLES)
    template_name = "audit/orders_list.html"
    model = OrderAuditEntry
    context_object_name = "entries"
    paginate_by = 25

    def get_queryset(self):
        qs = OrderAuditEntry.objects.select_related("user", "agency").order_by("-created_at")
        order_type = self.request.GET.get("type")
        if order_type:
            qs = qs.filter(order_type=order_type)
        return qs


class ClientAuditListView(RoleRequiredMixin, ListView):
    allowed_roles = tuple(STAFF_ROLES)
    template_name = "audit/clients_list.html"
    model = AuditEntry
    context_object_name = "entries"
    paginate_by = 25

    def get_queryset(self):
        journal = get_agency_journal()
        qs = (
            AuditEntry.objects.select_related("user", "agency")
            .filter(journal=journal)
            .order_by("-created_at")
        )
        action = self.request.GET.get("action")
        search = (self.request.GET.get("q") or "").strip()
        if action:
            qs = qs.filter(action=action)
        if search:
            qs = qs.filter(
                models.Q(agency__agn_name__icontains=search)
                | models.Q(agency__inn__icontains=search)
                | models.Q(agency__email__icontains=search)
                | models.Q(description__icontains=search)
                | models.Q(user__username__icontains=search)
            )
        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["current_action"] = self.request.GET.get("action", "")
        ctx["search_value"] = self.request.GET.get("q", "")
        ctx["actions"] = dict(AuditEntry.ACTION_CHOICES)
        return ctx


class StaffOveractionsListView(RoleRequiredMixin, ListView):
    template_name = "audit/staff_overactions_list.html"
    model = AuditEntry
    context_object_name = "entries"
    paginate_by = 25
    allowed_roles = ("head_manager", "director", "admin")

    def get_queryset(self):
        journal = get_staff_overactions_journal()
        qs = (
            AuditEntry.objects.select_related("user", "agency")
            .filter(journal=journal)
            .order_by("-created_at")
        )
        action = self.request.GET.get("action")
        search = (self.request.GET.get("q") or "").strip()
        if action:
            qs = qs.filter(action=action)
        if search:
            qs = qs.filter(
                models.Q(description__icontains=search)
                | models.Q(user__username__icontains=search)
                | models.Q(agency__agn_name__icontains=search)
            )
        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["current_action"] = self.request.GET.get("action", "")
        ctx["search_value"] = self.request.GET.get("q", "")
        ctx["actions"] = dict(AuditEntry.ACTION_CHOICES)
        return ctx


class StockMoveAuditListView(RoleRequiredMixin, ListView):
    allowed_roles = tuple(STAFF_ROLES)
    template_name = "audit/stock_moves_list.html"
    model = AuditEntry
    context_object_name = "entries"
    paginate_by = 25

    def get_queryset(self):
        journal = get_stock_move_journal()
        qs = (
            AuditEntry.objects.select_related("user", "agency", "journal")
            .filter(journal=journal)
            .order_by("-created_at")
        )
        action = self.request.GET.get("action")
        search = (self.request.GET.get("q") or "").strip()
        if action:
            qs = qs.filter(action=action)
        if search:
            qs = qs.filter(
                models.Q(description__icontains=search)
                | models.Q(user__username__icontains=search)
                | models.Q(agency__agn_name__icontains=search)
            )
        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["current_action"] = self.request.GET.get("action", "")
        ctx["search_value"] = self.request.GET.get("q", "")
        ctx["actions"] = dict(AuditEntry.ACTION_CHOICES)
        return ctx

# Create your views here.
