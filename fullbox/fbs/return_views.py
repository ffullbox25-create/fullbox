from __future__ import annotations

from functools import wraps
import mimetypes

from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.http import FileResponse, Http404, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_POST

from employees.access import (
    get_request_employee,
    get_request_role,
    resolve_cabinet_url,
    role_required,
)

from .exceptions import FbsError
from .flags import feature_enabled, module_enabled
from .models import FbsOrder, FbsReturn, FbsReturnPhoto, FbsReturnUnit
from .services.returns import (
    add_return_photo,
    complete_fbs_return,
    confirm_return_order_scan,
    inspect_return_unit,
    register_fbs_return,
    remaining_returnable_qty,
    scan_return_unit,
)


RETURN_ROLES = ("storekeeper", "head_manager", "director", "admin")
PAGE_SIZES = (25, 50, 100, 200)


def return_module_required(view_func):
    @wraps(view_func)
    def wrapped(request, *args, **kwargs):
        if not module_enabled():
            raise Http404
        return view_func(request, *args, **kwargs)

    return wrapped


def _base_context(request, **extra):
    role = get_request_role(request)
    context = {
        "employee": get_request_employee(request),
        "request_role": role,
        "cabinet_url": resolve_cabinet_url(role),
        "warehouse_writes_enabled": feature_enabled("warehouse_writes"),
        "operator_section": "returns",
    }
    context.update(extra)
    return context


def _redirect_detail(return_record: FbsReturn):
    return redirect("fbs:operator_return_detail", return_id=return_record.id)


def _write_error(request, return_record: FbsReturn, action):
    try:
        action()
    except FbsError as exc:
        messages.error(request, str(exc))
    return _redirect_detail(return_record)


def _page_size(value) -> int:
    try:
        cleaned = int(value)
    except (TypeError, ValueError):
        cleaned = PAGE_SIZES[0]
    return cleaned if cleaned in PAGE_SIZES else PAGE_SIZES[0]


@return_module_required
@role_required(*RETURN_ROLES)
@require_GET
def operator_returns(request):
    query = str(request.GET.get("q") or "").strip()
    status = str(request.GET.get("status") or "").strip()
    page_size = _page_size(request.GET.get("page_size"))
    rows = FbsReturn.objects.select_related(
        "order__profile__agency", "created_by", "completed_by"
    ).annotate(
        scanned_qty=Count("units"),
        good_qty=Count("units", filter=Q(units__condition=FbsReturnUnit.CONDITION_GOOD)),
        problem_qty=Count(
            "units",
            filter=Q(
                units__condition__in=(
                    FbsReturnUnit.CONDITION_DAMAGED,
                    FbsReturnUnit.CONDITION_QUARANTINE,
                )
            ),
        ),
    )
    if query:
        rows = rows.filter(
            Q(order__external_order_id__icontains=query)
            | Q(order__profile__agency__agn_name__icontains=query)
            | Q(external_return_id__icontains=query)
        )
    valid_statuses = dict(FbsReturn.STATUS_CHOICES)
    if status in valid_statuses:
        rows = rows.filter(status=status)
    else:
        status = ""
    page = Paginator(rows.order_by("-created_at", "-id"), page_size).get_page(
        request.GET.get("page")
    )
    summary = FbsReturn.objects.aggregate(
        total=Count("id"),
        active=Count("id", filter=Q(status__in=("expected", "receiving", "inspection"))),
        inspection=Count("id", filter=Q(status="inspection")),
        completed=Count("id", filter=Q(status="completed")),
    )
    return render(
        request,
        "fbs/operator_returns.html",
        _base_context(
            request,
            page_title="FBS · Возвраты",
            back_url=reverse("fbs:tsd_storekeeper"),
            page=page,
            page_size=page_size,
            page_sizes=PAGE_SIZES,
            query=query,
            selected_status=status,
            status_choices=FbsReturn.STATUS_CHOICES,
            summary=summary,
        ),
    )


@return_module_required
@role_required(*RETURN_ROLES)
@require_POST
def operator_create_return(request):
    query = str(request.POST.get("order_code") or "").strip()
    orders = list(
        FbsOrder.objects.filter(
            Q(external_order_id=query)
            | Q(marketplace_labels__barcode=query)
            | Q(marketplace_labels__external_label_id=query)
        )
        .distinct()
        .order_by("id")[:2]
    )
    if not orders:
        messages.error(request, "FBS-заказ по указанному QR или номеру не найден.")
        return redirect("fbs:operator_returns")
    if len(orders) > 1:
        messages.error(request, "Код найден у нескольких FBS-заказов. Уточните номер заказа.")
        return redirect("fbs:operator_returns")
    order = orders[0]
    raw_qty = request.POST.get("expected_qty")
    try:
        expected_qty = int(raw_qty)
    except (TypeError, ValueError):
        expected_qty = remaining_returnable_qty(order.id)
    try:
        return_record = register_fbs_return(
            order_id=order.id,
            expected_qty=expected_qty,
            external_return_id=request.POST.get("external_return_id", ""),
            reason=request.POST.get("reason", ""),
            created_by=request.user,
        )
    except FbsError as exc:
        messages.error(request, str(exc))
        return redirect("fbs:operator_returns")
    return _redirect_detail(return_record)


def _detail_context(request, return_record: FbsReturn):
    units = list(
        return_record.units.select_related(
            "order_item__sku",
            "target_box__pallet__cell",
            "released_balance",
            "scanned_by",
            "inspected_by",
        )
        .prefetch_related("photos")
        .order_by("scanned_at", "id")
    )
    good_qty = sum(unit.condition == FbsReturnUnit.CONDITION_GOOD for unit in units)
    damaged_qty = sum(unit.condition == FbsReturnUnit.CONDITION_DAMAGED for unit in units)
    quarantine_qty = sum(
        unit.condition == FbsReturnUnit.CONDITION_QUARANTINE for unit in units
    )
    pending_qty = sum(unit.condition == FbsReturnUnit.CONDITION_PENDING for unit in units)
    return _base_context(
        request,
        page_title=f"FBS · {return_record.number}",
        back_url=reverse("fbs:operator_returns"),
        return_record=return_record,
        units=units,
        scanned_qty=len(units),
        remaining_qty=max(int(return_record.expected_qty or 0) - len(units), 0),
        good_qty=good_qty,
        damaged_qty=damaged_qty,
        quarantine_qty=quarantine_qty,
        pending_qty=pending_qty,
        can_complete=(
            return_record.status != FbsReturn.STATUS_COMPLETED
            and len(units) == int(return_record.expected_qty or 0)
            and pending_qty == 0
        ),
        condition_choices=(
            (FbsReturnUnit.CONDITION_GOOD, "Годен"),
            (FbsReturnUnit.CONDITION_DAMAGED, "Поврежден"),
            (FbsReturnUnit.CONDITION_QUARANTINE, "Карантин"),
        ),
        photo_kinds=FbsReturnPhoto.KIND_CHOICES,
    )


@return_module_required
@role_required(*RETURN_ROLES)
@require_GET
def operator_return_detail(request, return_id: int):
    return_record = get_object_or_404(
        FbsReturn.objects.select_related(
            "order__profile__agency", "created_by", "received_by", "completed_by"
        ).prefetch_related("order__marketplace_labels"),
        pk=return_id,
    )
    return render(
        request,
        "fbs/operator_return_detail.html",
        _detail_context(request, return_record),
    )


@return_module_required
@role_required(*RETURN_ROLES)
@require_POST
def operator_return_confirm_order(request, return_id: int):
    return_record = get_object_or_404(FbsReturn, pk=return_id)
    return _write_error(
        request,
        return_record,
        lambda: confirm_return_order_scan(
            return_id=return_id,
            order_scan=request.POST.get("order_scan", ""),
            performed_by=request.user,
        ),
    )


@return_module_required
@role_required(*RETURN_ROLES)
@require_POST
def operator_return_scan_unit(request, return_id: int):
    return_record = get_object_or_404(FbsReturn, pk=return_id)
    return _write_error(
        request,
        return_record,
        lambda: scan_return_unit(
            return_id=return_id,
            item_scan=request.POST.get("item_scan", ""),
            performed_by=request.user,
        ),
    )


@return_module_required
@role_required(*RETURN_ROLES)
@require_POST
def operator_return_photo(request, return_id: int, unit_id: int):
    return_record = get_object_or_404(FbsReturn, pk=return_id)
    unit = get_object_or_404(FbsReturnUnit, pk=unit_id, return_record=return_record)
    uploaded_file = request.FILES.get("photo")
    if uploaded_file is None:
        messages.error(request, "Выберите фотографию.")
        return _redirect_detail(return_record)
    return _write_error(
        request,
        return_record,
        lambda: add_return_photo(
            unit_id=unit.id,
            uploaded_file=uploaded_file,
            kind=request.POST.get("kind", ""),
            performed_by=request.user,
        ),
    )


@return_module_required
@role_required(*RETURN_ROLES)
@require_POST
def operator_return_inspect_unit(request, return_id: int, unit_id: int):
    return_record = get_object_or_404(FbsReturn, pk=return_id)
    unit = get_object_or_404(FbsReturnUnit, pk=unit_id, return_record=return_record)
    return _write_error(
        request,
        return_record,
        lambda: inspect_return_unit(
            unit_id=unit.id,
            condition=request.POST.get("condition", ""),
            condition_reason=request.POST.get("condition_reason", ""),
            target_box_scan=request.POST.get("target_box_scan", ""),
            performed_by=request.user,
        ),
    )


@return_module_required
@role_required(*RETURN_ROLES)
@require_POST
def operator_return_complete(request, return_id: int):
    return_record = get_object_or_404(FbsReturn, pk=return_id)
    return _write_error(
        request,
        return_record,
        lambda: complete_fbs_return(return_id=return_id, performed_by=request.user),
    )


@return_module_required
@role_required(*RETURN_ROLES)
@require_GET
def operator_return_photo_file(request, return_id: int, photo_id: int):
    photo = get_object_or_404(
        FbsReturnPhoto.objects.select_related("unit"),
        pk=photo_id,
        unit__return_record_id=return_id,
    )
    if not photo.file:
        raise Http404
    content_type = mimetypes.guess_type(photo.file.name)[0] or "application/octet-stream"
    response = FileResponse(photo.file.open("rb"), content_type=content_type)
    response["Content-Disposition"] = f'inline; filename="return-photo-{photo.id}"'
    response["X-Content-Type-Options"] = "nosniff"
    response["Cache-Control"] = "private, no-store"
    return response
