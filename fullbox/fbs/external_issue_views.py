import uuid
from urllib.parse import urlencode
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db import IntegrityError
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.http import require_http_methods
from employees.access import get_request_role, role_required
from sku.models import Agency
from .exceptions import FbsError
from .models import FbsExternalIssue, FbsStockBalance, FbsBox, FbsPallet
from .services.external_issues import (
    OPEN,
    ROLES,
    SUPERVISORS,
    create_issue,
    issue_command,
    warehouse_visible_issue_queryset,
)
from .services.inventory import filter_unlocked_balances
from .services.physical_locations import fbs_box_physical_location_label
from .tsd_views import _base_context, fbs_module_required


@login_required
@fbs_module_required
@role_required(*ROLES)
@require_http_methods(["GET", "POST"])
def issue_list(request):
    error = ""
    values = request.POST if request.method == "POST" else request.GET
    client_id = str(values.get("client") or "")
    can_historical = request.user.is_superuser or get_request_role(request) in SUPERVISORS
    if request.method == "POST":
        try:
            selections = [{"balance_id": key[4:], "qty": value} for key, value in request.POST.items()
                          if key.startswith("qty_") and str(value).strip() not in {"", "0"}]
            occurred_at = parse_datetime(request.POST.get("occurred_at") or "")
            if occurred_at and timezone.is_naive(occurred_at):
                occurred_at = timezone.make_aware(occurred_at)
            issue = create_issue(user=request.user, agency_id=int(client_id),
                reference=values.get("reference"), recipient=values.get("recipient"), purpose=values.get("purpose"),
                basis=values.get("basis"), selections=selections, request_key=values.get("request_key"),
                historical=values.get("historical") == "1", occurred_at=occurred_at,
                historical_confirmed=values.get("historical_confirmed") == "1")
            return redirect("fbs:external_issue_detail", issue_id=issue.pk)
        except (FbsError, ValueError, TypeError, ValidationError) as exc:
            error = str(exc)
        except IntegrityError:
            error = "Остаток или номер документа изменился во время сохранения. Обновите страницу и проверьте журнал выдач."
    balances = FbsStockBalance.objects.none()
    if client_id.isdigit():
        balances = filter_unlocked_balances(FbsStockBalance.objects.filter(
            agency_id=int(client_id), available_qty__gt=0, box__status=FbsBox.STATUS_ACTIVE,
            box__pallet__status=FbsPallet.STATUS_ACTIVE).select_related(
            "box__pallet__cell__location", "box__source_container__current_location"))
        query = str(values.get("q") or "").strip()
        if query:
            balances = balances.filter(Q(sku_code__icontains=query) | Q(barcode__icontains=query)
                | Q(name__icontains=query) | Q(box__box_code__icontains=query) | Q(marking_code__icontains=query))
    page = Paginator(balances.order_by("sku_code", "box_id", "id"), 100).get_page(request.GET.get("page"))
    for balance in page:
        balance.place_label = fbs_box_physical_location_label(balance.box)
        balance.entered_qty = values.get(f"qty_{balance.id}", "") if request.method == "POST" else ""
    documents = warehouse_visible_issue_queryset(
        FbsExternalIssue.objects.select_related("agency", "created_by")
    ).order_by("-id")
    if client_id.isdigit():
        documents = documents.filter(agency_id=int(client_id))
    return render(request, "fbs/external_issue_list.html", _base_context(request,
        page_title="Выдача из ФБС", workspace_title="Выдача из ФБС", operator_section="stock",
        back_url=reverse("fbs:tsd_storekeeper_stock"), error=error, values=values, client_id=client_id,
        clients=Agency.objects.filter(pk__in=FbsStockBalance.objects.values("agency_id")).order_by("agn_name"),
        balances=page, documents=documents[:100], can_historical=can_historical,
        request_key=values.get("request_key") or str(uuid.uuid4())))


@login_required
@fbs_module_required
@role_required(*ROLES)
@require_http_methods(["GET", "POST"])
def issue_detail(request, issue_id):
    issue = get_object_or_404(
        warehouse_visible_issue_queryset(
            FbsExternalIssue.objects.select_related("agency", "created_by", "completed_by")
        ),
        pk=issue_id,
    )
    error = ""
    if request.method == "POST":
        try:
            issue_command(user=request.user, issue_id=issue.pk, action=request.POST.get("action"),
                request_key=request.POST.get("request_key"), line_id=request.POST.get("line_id"),
                cell_scan=request.POST.get("cell_scan", ""), box_scan=request.POST.get("box_scan", ""),
                item_scan=request.POST.get("item_scan", ""), confirmed=request.POST.get("confirmed") == "1",
                expected_qty=request.POST.get("expected_qty"))
            if request.POST.get("action") == "pick":
                query = urlencode({"line": request.POST.get("line_id", ""),
                                   "cell": request.POST.get("cell_scan", ""), "box": request.POST.get("box_scan", "")})
                return redirect(reverse("fbs:external_issue_detail", args=[issue.pk]) + "?" + query)
            return redirect("fbs:external_issue_detail", issue_id=issue.pk)
        except (FbsError, ValueError, TypeError, ValidationError) as exc:
            error = str(exc)
        except IntegrityError:
            error = "Конкурирующая операция: проверьте состояние документа. Повторное списание не выполнено."
        issue.refresh_from_db()
    lines = list(issue.lines.select_related("balance__box__pallet__cell__location", "balance__box__source_container__current_location"))
    for line in lines:
        line.place_label = fbs_box_physical_location_label(line.balance.box)
        line.scan_key = str(uuid.uuid4())
        line.return_key = str(uuid.uuid4())
        line.cell_prefill = request.GET.get("cell", "") if request.GET.get("line") == str(line.pk) else ""
        line.box_prefill = request.GET.get("box", "") if request.GET.get("line") == str(line.pk) else ""
    # Preserve the idempotency token on an error, including an uncertain network retry.
    if error:
        for line in lines:
            if str(line.pk) == request.POST.get("line_id"):
                if request.POST.get("action") == "pick": line.scan_key = request.POST.get("request_key")
                if request.POST.get("action") == "return": line.return_key = request.POST.get("request_key")
    return render(request, "fbs/external_issue_detail.html", _base_context(request,
        page_title=issue.number, workspace_title=issue.number, operator_section="stock",
        back_url=reverse("fbs:external_issues"), issue=issue, lines=lines, error=error,
        total_requested=sum(line.requested_qty for line in lines), total_picked=sum(line.in_hand_qty for line in lines),
        total_shipped=sum(line.shipped_qty for line in lines), total_unpicked=sum(line.unpicked_qty for line in lines),
        is_open=issue.status in OPEN, events=issue.events.select_related("performed_by"),
        complete_key=str(uuid.uuid4()), cancel_key=str(uuid.uuid4())))
