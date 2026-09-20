import re

from django import template

from fbs.models import FbsClientMovementRequest


register = template.Library()


@register.simple_tag
def pending_fbs_movement_count() -> int:
    """Return client movements that have not yet been accepted by the warehouse."""
    return FbsClientMovementRequest.objects.filter(
        status__in=(
            FbsClientMovementRequest.STATUS_SUBMITTED,
            FbsClientMovementRequest.STATUS_APPROVED,
        )
    ).count()


@register.simple_tag
def fbs_desktop_needs_update(request) -> bool:
    """Old Desktop clients keep polling after their login session expires."""
    user_agent = str(getattr(request, "META", {}).get("HTTP_USER_AGENT", ""))
    match = re.search(r"FullboxDesktop/(\d+)\.(\d+)\.(\d+)(?![\d.])", user_agent)
    return bool(match and tuple(map(int, match.groups())) < (1, 0, 21))
