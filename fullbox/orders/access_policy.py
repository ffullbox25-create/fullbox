"""Server-side authorization for receiving, packing and processing order details."""

from audit.models import OrderAuditEntry
from client_cabinet.portal_access import SECTION_REQUESTS
from client_cabinet.portal_members import resolve_portal_agency_for_user
from employees.access import is_developer_login, request_has_any_role


ORDER_DETAIL_ROLES = (
    "manager", "storekeeper", "processing_head", "head_manager", "director", "admin",
)


def order_detail_access_allowed(request, *, order_id, order_type):
    """Resolve the principal, never trust a submitted agency or cached UI context.

    Latest non-null audit agency is the current known owner. A historical entry
    for a former owner must not authorize access to a transferred order.
    """
    user = getattr(request, "user", None)
    if not user or not user.is_authenticated or not getattr(user, "is_active", False):
        return False
    if order_type not in {"receiving", "packing", "processing"}:
        return False
    if is_developer_login(user) or request_has_any_role(request, ORDER_DETAIL_ROLES):
        return True
    agency = resolve_portal_agency_for_user(user, required_section=SECTION_REQUESTS)
    if agency is None:
        return False
    for param in ("client", "agency"):
        selected = request.GET.get(param)
        if selected and str(selected) != str(agency.pk):
            return False
    order_key = str(order_id or "").strip()
    if not order_key:
        return False
    owner_id = (
        OrderAuditEntry.objects.filter(order_id=order_key, order_type=order_type)
        .exclude(agency_id__isnull=True)
        .order_by("-created_at", "-id")
        .values_list("agency_id", flat=True)
        .first()
    )
    if owner_id is None or owner_id != agency.pk:
        return False
    request._client_agency = agency
    return True
