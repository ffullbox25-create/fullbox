from urllib.parse import urlencode

from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db.models import Count, F, Max, Q
from django.http import Http404, JsonResponse
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
from .flags import feature_enabled, feature_snapshot, module_enabled
from .integrations.contracts import (
    OZON_CREATE_OR_GET_EXEMPLARS,
    OZON_READ_EXEMPLAR_STATUS,
    OZON_SET_EXEMPLARS,
    WB_READ_ORDER_METADATA,
    WB_SET_ORDER_EXPIRATION,
    WB_SET_ORDER_SGTINS,
)
from .models import (
    FbsIntegrationProfile,
    FbsMarketplaceCommand,
    FbsMarketplaceMetadataTransfer,
    FbsOrder,
)
from .services import request_marketplace_metadata_readback
from .workspace import workspace_shell_enabled


METADATA_CONTROL_ROLES = ("manager", "storekeeper", "head_manager", "director", "admin")
TRANSFER_CONFLICT_STATUSES = (
    FbsMarketplaceMetadataTransfer.STATUS_FAILED,
    FbsMarketplaceMetadataTransfer.STATUS_CONFLICT,
)
TRANSFER_WAITING_STATUSES = (
    FbsMarketplaceMetadataTransfer.STATUS_PREPARED,
    FbsMarketplaceMetadataTransfer.STATUS_QUEUED,
    FbsMarketplaceMetadataTransfer.STATUS_SENT,
    FbsMarketplaceMetadataTransfer.STATUS_RETRY,
)
TRANSFER_READBACK_STATUSES = (
    FbsMarketplaceMetadataTransfer.STATUS_SENT,
    FbsMarketplaceMetadataTransfer.STATUS_RETRY,
    FbsMarketplaceMetadataTransfer.STATUS_FAILED,
    FbsMarketplaceMetadataTransfer.STATUS_CONFLICT,
)
METADATA_COMMAND_TYPES = (
    WB_SET_ORDER_SGTINS,
    WB_SET_ORDER_EXPIRATION,
    WB_READ_ORDER_METADATA,
    OZON_CREATE_OR_GET_EXEMPLARS,
    OZON_SET_EXEMPLARS,
    OZON_READ_EXEMPLAR_STATUS,
)
COMMAND_TITLES = {
    WB_SET_ORDER_SGTINS: "WB: передача КИЗ",
    WB_SET_ORDER_EXPIRATION: "WB: передача срока годности",
    WB_READ_ORDER_METADATA: "WB: сверка метаданных",
    OZON_CREATE_OR_GET_EXEMPLARS: "Ozon: получение экземпляров",
    OZON_SET_EXEMPLARS: "Ozon: передача КИЗ",
    OZON_READ_EXEMPLAR_STATUS: "Ozon: сверка КИЗ",
}


def module_status(request):
    if not module_enabled():
        raise Http404
    if not request.user.is_authenticated:
        return redirect_to_login(request.get_full_path())
    if not request.user.is_staff:
        raise PermissionDenied
    return JsonResponse(
        {
            "module": "fbs",
            "state": "scaffold",
            "features": feature_snapshot(),
        }
    )


def _require_fbs_module() -> None:
    if not module_enabled():
        raise Http404


def _control_context(request) -> dict:
    role = get_request_role(request)
    return {
        "employee": get_request_employee(request),
        "request_role": role,
        "workspace_shell": workspace_shell_enabled(role),
        "cabinet_url": resolve_cabinet_url(role),
        "warehouse_writes_enabled": feature_enabled("warehouse_writes"),
        "operator_section": "metadata",
        "workspace_section": "metadata",
        "workspace_title": "КИЗ и сроки",
        "workspace_subtitle": "Передача маркировки площадке",
        "workspace_crumb": "КИЗ и сроки",
        "page_title": "FBS · КИЗ и сроки",
    }


def _annotated_orders():
    transfer_path = "items__metadata_transfers"
    return (
        FbsOrder.objects.filter(items__metadata_transfers__isnull=False)
        .select_related("profile__agency")
        .annotate(
            transfer_count=Count(transfer_path, distinct=True),
            conflict_count=Count(
                transfer_path,
                filter=Q(**{f"{transfer_path}__status__in": TRANSFER_CONFLICT_STATUSES}),
                distinct=True,
            ),
            waiting_count=Count(
                transfer_path,
                filter=Q(**{f"{transfer_path}__status__in": TRANSFER_WAITING_STATUSES}),
                distinct=True,
            ),
            confirmed_count=Count(
                transfer_path,
                filter=Q(
                    **{
                        f"{transfer_path}__status": (
                            FbsMarketplaceMetadataTransfer.STATUS_CONFIRMED
                        )
                    }
                ),
                distinct=True,
            ),
            unsupported_count=Count(
                transfer_path,
                filter=Q(
                    **{
                        f"{transfer_path}__status": (
                            FbsMarketplaceMetadataTransfer.STATUS_UNSUPPORTED
                        )
                    }
                ),
                distinct=True,
            ),
            required_unsupported_count=Count(
                transfer_path,
                filter=Q(
                    **{
                        f"{transfer_path}__status": (
                            FbsMarketplaceMetadataTransfer.STATUS_UNSUPPORTED
                        ),
                        f"{transfer_path}__is_required": True,
                    }
                ),
                distinct=True,
            ),
            last_transfer_at=Max(f"{transfer_path}__updated_at"),
        )
    )


def _decorate_order_status(order: FbsOrder) -> None:
    if order.conflict_count:
        order.control_status = "Требует решения"
        order.control_status_class = "conflict"
    elif order.required_unsupported_count:
        order.control_status = "Нет контракта API"
        order.control_status_class = "unsupported"
    elif order.waiting_count:
        order.control_status = "Ожидает площадку"
        order.control_status_class = "waiting"
    elif order.transfer_count and order.confirmed_count == order.transfer_count:
        order.control_status = "Подтверждено"
        order.control_status_class = "confirmed"
    else:
        order.control_status = "Смешанный статус"
        order.control_status_class = "neutral"


@require_GET
@role_required(*METADATA_CONTROL_ROLES)
def metadata_control_list(request):
    _require_fbs_module()
    all_orders = _annotated_orders()
    summary = {
        "total": all_orders.count(),
        "conflicts": all_orders.filter(conflict_count__gt=0).count(),
        "waiting": all_orders.filter(conflict_count=0, waiting_count__gt=0).count(),
        "unsupported": all_orders.filter(required_unsupported_count__gt=0).count(),
        "confirmed": all_orders.filter(
            transfer_count__gt=0,
            transfer_count=F("confirmed_count"),
        ).count(),
    }

    orders = all_orders
    status_filter = str(request.GET.get("status") or "open").strip()
    marketplace_filter = str(request.GET.get("marketplace") or "").strip()
    query = str(request.GET.get("q") or "").strip()
    if status_filter == "conflict":
        orders = orders.filter(conflict_count__gt=0)
    elif status_filter == "waiting":
        orders = orders.filter(conflict_count=0, waiting_count__gt=0)
    elif status_filter == "unsupported":
        orders = orders.filter(required_unsupported_count__gt=0)
    elif status_filter == "confirmed":
        orders = orders.filter(transfer_count__gt=0, transfer_count=F("confirmed_count"))
    elif status_filter == "open":
        orders = orders.filter(
            Q(conflict_count__gt=0)
            | Q(waiting_count__gt=0)
            | Q(required_unsupported_count__gt=0)
        )
    else:
        status_filter = "all"
    if marketplace_filter in {
        FbsIntegrationProfile.MARKETPLACE_WB,
        FbsIntegrationProfile.MARKETPLACE_OZON,
    }:
        orders = orders.filter(profile__marketplace=marketplace_filter)
    else:
        marketplace_filter = ""
    if query:
        orders = orders.filter(
            Q(external_order_id__icontains=query)
            | Q(profile__agency__agn_name__icontains=query)
            | Q(items__external_sku__icontains=query)
            | Q(items__product_name__icontains=query)
            | Q(items__metadata_transfers__last_error__icontains=query)
        ).distinct()

    page = Paginator(orders.order_by("-last_transfer_at", "-id"), 50).get_page(
        request.GET.get("page")
    )
    for order in page.object_list:
        _decorate_order_status(order)
    context = {
        **_control_context(request),
        "page": page,
        "summary": summary,
        "status_filter": status_filter,
        "marketplace_filter": marketplace_filter,
        "query": query,
    }
    return render(request, "fbs/metadata_control_list.html", context)


def _metadata_detail_context(request, order_id: int) -> dict:
    order = get_object_or_404(
        FbsOrder.objects.select_related("profile__agency").distinct(),
        pk=order_id,
        items__metadata_transfers__isnull=False,
    )
    transfers = list(
        FbsMarketplaceMetadataTransfer.objects.filter(order_item__order=order)
        .select_related("order_item", "traceability")
        .order_by("order_item_id", "metadata_type", "id")
    )
    commands = list(
        FbsMarketplaceCommand.objects.filter(
            order=order,
            command_type__in=METADATA_COMMAND_TYPES,
        )
        .select_related("requested_by")
        .order_by("-created_at", "-id")[:50]
    )
    for command in commands:
        command.control_title = COMMAND_TITLES.get(command.command_type, command.command_type)

    unresolved = [
        transfer for transfer in transfers if transfer.status in TRANSFER_READBACK_STATUSES
    ]
    if order.profile.marketplace == FbsIntegrationProfile.MARKETPLACE_OZON:
        unresolved = [
            transfer
            for transfer in unresolved
            if transfer.metadata_type == FbsMarketplaceMetadataTransfer.TYPE_MARKING_CODE
        ]
        readback_type = OZON_READ_EXEMPLAR_STATUS
    else:
        readback_type = WB_READ_ORDER_METADATA
    active_readback = next(
        (
            command
            for command in commands
            if command.command_type == readback_type
            and command.status
            in {
                FbsMarketplaceCommand.STATUS_PENDING,
                FbsMarketplaceCommand.STATUS_RETRY,
                FbsMarketplaceCommand.STATUS_SENT,
            }
        ),
        None,
    )
    readback_enabled = (
        feature_enabled("outbox")
        and feature_enabled("marking_push")
        and order.profile.is_active
        and order.profile.outbox_enabled
        and order.profile.marking_push_enabled
    )
    return {
        **_control_context(request),
        "order": order,
        "transfers": transfers,
        "commands": commands,
        "active_readback": active_readback,
        "can_request_readback": bool(unresolved and readback_enabled and not active_readback),
        "readback_disabled_reason": (
            "Сверка уже стоит в очереди."
            if active_readback
            else "Нет переданных данных для безопасной сверки."
            if not unresolved
            else "Передача метаданных отключена глобально или в профиле."
            if not readback_enabled
            else ""
        ),
        "ok_message": (
            f"Сверка поставлена в очередь, команда #{request.GET.get('command')}."
            if request.GET.get("result") == "queued"
            and str(request.GET.get("command") or "").isdigit()
            else ""
        ),
        "error": str(request.GET.get("error") or "")[:500],
    }


@require_GET
@role_required(*METADATA_CONTROL_ROLES)
def metadata_control_detail(request, order_id: int):
    _require_fbs_module()
    return render(
        request,
        "fbs/metadata_control_detail.html",
        _metadata_detail_context(request, order_id),
    )


@require_POST
@role_required(*METADATA_CONTROL_ROLES)
def metadata_control_readback(request, order_id: int):
    _require_fbs_module()
    get_object_or_404(
        FbsOrder.objects.distinct(),
        pk=order_id,
        items__metadata_transfers__isnull=False,
    )
    try:
        command = request_marketplace_metadata_readback(
            order_id=order_id,
            requested_by=request.user,
        )
    except FbsError as exc:
        query = urlencode({"error": str(exc)})
    else:
        query = urlencode({"result": "queued", "command": command.id})
    detail_url = reverse("fbs:metadata_control_detail", kwargs={"order_id": order_id})
    return redirect(f"{detail_url}?{query}")
