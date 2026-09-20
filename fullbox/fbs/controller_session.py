from __future__ import annotations

from .models import FbsWorkstation


CONTROLLER_WORKSTATION_SESSION_KEY = "fbs_controller_workstation_id"


def get_controller_workstation(request) -> FbsWorkstation | None:
    workstation_id = request.session.get(CONTROLLER_WORKSTATION_SESSION_KEY)
    if not workstation_id:
        return None
    workstation = (
        FbsWorkstation.objects.select_related("device_agent")
        .filter(pk=workstation_id, is_active=True)
        .first()
    )
    if workstation is None:
        request.session.pop(CONTROLLER_WORKSTATION_SESSION_KEY, None)
    return workstation
