import re
import uuid

from django.core.exceptions import ValidationError
from django.db.models import Count, Q, Sum
from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect
from django.views.generic import TemplateView

from audit.models import OrderAuditEntry
from billing.permissions import filter_agencies_for_user
from employees.access import RoleRequiredMixin
from fbs.models import FbsClientMovementRequest, FbsExternalIssue
from fbs.exceptions import FbsError, FbsReplenishmentError
from fbs.client_portal import create_client_movement_request
from fbs.services.movement_edit import can_edit_movement, movement_edit_initial
from fbs.services.client_movements import (
    approve_client_movement_by_manager,
    reject_client_movement_request,
)
from fbs.services.external_issues import (
    client_portal_issue_queryset,
    issue_display_status,
    review_issue_by_manager,
)
from sklad.services.warehouse_write_path import WarehouseWritePathService
from sku.models import Agency, SKUBarcode

from .roles import CABINET_ROLES


_EXTERNAL_ISSUE_NUMBER_RE = re.compile(r"\AFBS-OUT-(\d+)\Z", re.IGNORECASE)


def _external_issue_number_id(query):
    match = _EXTERNAL_ISSUE_NUMBER_RE.fullmatch(str(query or "").strip())
    if not match:
        return None
    issue_id = int(match.group(1))
    return issue_id if issue_id > 0 else None


def _visible_requests(request):
    agencies = filter_agencies_for_user(Agency.objects.all(), request)
    return FbsClientMovementRequest.objects.filter(agency__in=agencies)


def _visible_external_requests(request):
    agencies = filter_agencies_for_user(Agency.objects.all(), request)
    return client_portal_issue_queryset(
        FbsExternalIssue.objects.filter(agency__in=agencies)
    )


class TeamManagerFbsMovementsView(RoleRequiredMixin, TemplateView):
    template_name = "teammanager/fbs_movements.html"
    allowed_roles = CABINET_ROLES

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        query = str(self.request.GET.get("q") or "").strip()
        status = str(self.request.GET.get("status") or "").strip()
        rows = _visible_requests(self.request).select_related(
            "agency", "reviewed_by", "warehouse_confirmed_by", "manager_confirmed_by"
        ).annotate(
            line_count=Count("lines", distinct=True),
            moved_qty=Sum("replenishment_plans__moved_qty"),
        )
        if query:
            rows = rows.filter(
                Q(agency__agn_name__icontains=query)
                | Q(agency__short_name__icontains=query)
                | Q(agency__inn__icontains=query)
            )
        if status:
            rows = rows.filter(status=status)
        external_rows = _visible_external_requests(self.request).select_related(
            "agency", "created_by", "completed_by"
        ).annotate(requested_qty_total=Sum("lines__requested_qty"))
        if query:
            external_filter = (
                Q(agency__agn_name__icontains=query)
                | Q(agency__short_name__icontains=query)
                | Q(agency__inn__icontains=query)
                | Q(reference__icontains=query)
                | Q(recipient__icontains=query)
            )
            external_issue_id = _external_issue_number_id(query)
            if external_issue_id is not None:
                external_filter |= Q(pk=external_issue_id)
            external_rows = external_rows.filter(external_filter)
        external_rows = list(external_rows.order_by("-created_at", "-id")[:100])
        for row in external_rows:
            row.display_status, row.display_status_label = issue_display_status(row)
            row.review_key = str(uuid.uuid4())
        ctx.update(
            {
                "active_nav": "fbs_movements",
                "rows": rows.order_by("-created_at", "-id")[:300],
                "status_choices": FbsClientMovementRequest.STATUS_CHOICES,
                "filter_status": status,
                "query": query,
                "external_rows": external_rows,
            }
        )
        return ctx

    def post(self, request, *args, **kwargs):
        action = str(request.POST.get("action") or "").strip()
        if action not in {"approve_outbound", "reject_outbound"}:
            messages.error(request, "Неизвестное действие с заявкой FBS.")
            return redirect("team-manager-fbs-movements")
        issue = get_object_or_404(
            _visible_external_requests(request),
            pk=request.POST.get("issue_id"),
        )
        try:
            review_issue_by_manager(
                user=request.user,
                issue_id=issue.pk,
                action="approve" if action == "approve_outbound" else "reject",
                request_key=request.POST.get("request_key"),
            )
            if action == "approve_outbound":
                messages.success(
                    request,
                    f"Заявка {issue.number} подтверждена и передана кладовщику.",
                )
            else:
                messages.success(
                    request,
                    f"Заявка {issue.number} отклонена, резерв вывоза снят.",
                )
        except FbsError as exc:
            messages.error(request, str(exc))
        return redirect("team-manager-fbs-movements")


class TeamManagerFbsMovementDetailView(RoleRequiredMixin, TemplateView):
    template_name = "teammanager/fbs_movement_detail.html"
    allowed_roles = CABINET_ROLES

    def _item(self):
        return get_object_or_404(
            _visible_requests(self.request).select_related(
                "agency",
                "requested_by",
                "reviewed_by",
                "warehouse_accepted_by",
                "warehouse_confirmed_by",
                "manager_confirmed_by",
            ),
            pk=self.kwargs["request_id"],
        )

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        item = self._item()
        reserve_qty = 0
        quantity_reserve = False
        if item.uses_hard_reserve:
            from sklad.services.fbs_quantity_reserves import pool_reserves
            quantity_reserve = pool_reserves(item.agency_id, item.id).exists()
            reserve_qty = sum(
                int(row.get("qty") or 0)
                for row in WarehouseWritePathService.fbs_movement_reserve_allocations(
                    agency=item.agency,
                    request_id=item.id,
                )
            )
        can_edit = can_edit_movement(item, self.request.user)
        covered_qty = reserve_qty
        if quantity_reserve:
            from sklad.services.fbs_quantity_reserves import pool_coverage
            covered_qty = pool_coverage(item.agency_id,item.id)
        editing = can_edit and (self.request.GET.get("edit") == "1" or kwargs.get("edit_data") is not None)
        edit_data = kwargs.get("edit_data") or (movement_edit_initial(item) if editing else {})
        ctx.update(
            {
                "active_nav": "fbs_movements",
                "can_edit": can_edit, "editing": editing, "edit_data": edit_data,
                "edit_errors": kwargs.get("edit_errors", []),
                "edit_catalog": SKUBarcode.objects.select_related("sku").filter(sku__agency=item.agency,sku__deleted=False).order_by("sku__sku_code", "id")[:3000] if editing else [],
                "item": item,
                "lines": item.lines.select_related("sku").order_by("id"),
                "plans": item.replenishment_plans.select_related(
                    "target_cell", "target_pallet", "target_box", "assigned_to"
                ).order_by("id"),
                "history": OrderAuditEntry.objects.filter(
                    order_type="fbs_movement", order_id=item.number
                ).select_related("user").order_by("-created_at", "-id"),
                "reserve_qty": reserve_qty,
                "quantity_reserve": quantity_reserve,
                "covered_qty": covered_qty,
                "reserve_shortage": max(reserve_qty-covered_qty,0),
                "can_review": bool(
                    item.uses_hard_reserve
                    and item.status == FbsClientMovementRequest.STATUS_SUBMITTED
                ),
            }
        )
        return ctx

    def post(self, request, *args, **kwargs):
        item = self._item()
        action = str(request.POST.get("action") or "").strip()
        if action == "save_edit":
            barcodes=request.POST.getlist("barcode")
            quantities=request.POST.getlist("qty")
            units=request.POST.getlist("units_per_box")
            rows=[{"barcode":barcode,"qty":quantities[i] if i<len(quantities) else "",
                   "units_per_box":units[i] if i<len(units) else ""} for i,barcode in enumerate(barcodes)]
            data={"rows":rows,"mixed_codes":request.POST.getlist("mixed_box_code"),
                  "comment":request.POST.get("comment", ""),"version":request.POST.get("version", ""),
                  "requested_box_count":request.POST.get("requested_box_count", ""),
                  "requested_mixed_box_count":request.POST.get("requested_mixed_box_count", 0)}
            try:
                create_client_movement_request(agency=item.agency,mode=item.mode,raw_lines=rows,
                    requested_by=request.user,comment=data["comment"],edit_request_id=item.pk,
                    expected_updated_at=data["version"],mixed_box_codes=data["mixed_codes"],
                    requested_box_count=data["requested_box_count"] if item.mode=="item" else None,
                    requested_mixed_box_count=data["requested_mixed_box_count"] if item.mode=="item" else 0)
            except (ValidationError,FbsReplenishmentError) as exc:
                errors=getattr(exc,"messages",None) or [str(exc)]
                return self.render_to_response(self.get_context_data(edit_data=data,edit_errors=errors),status=400)
            messages.success(request,"Изменения FBS-заявки сохранены. Резерв пересчитан; заявка ожидает подтверждения менеджера.")
            return redirect("team-manager-fbs-movement-detail",request_id=item.pk)
        try:
            if action == "approve":
                approve_client_movement_by_manager(
                    request_id=item.id,
                    reviewed_by=request.user,
                )
                messages.success(request, "FBS-перемещение подтверждено и передано складу.")
            elif action == "reject":
                reject_client_movement_request(
                    request_id=item.id,
                    reviewed_by=request.user,
                )
                messages.success(request, "FBS-перемещение отклонено, резерв освобожден.")
            else:
                messages.error(request, "Неизвестное действие с FBS-перемещением.")
        except FbsReplenishmentError as exc:
            messages.error(request, str(exc))
        return redirect("team-manager-fbs-movement-detail", request_id=item.id)
