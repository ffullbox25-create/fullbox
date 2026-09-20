from django.urls import path

from .views import (
    agent_command_ack,
    agent_commands,
    agent_context_claim,
    agent_context_release,
    agent_event,
    agent_events_poll,
    agent_enrollment_create,
    agent_enrollment_exchange,
    agent_ping,
    agent_status,
    desktop_credential_verify,
)

app_name = "agent"

urlpatterns = [
    path("enrollment-codes/", agent_enrollment_create, name="enrollment-create"),
    path("enroll/", agent_enrollment_exchange, name="enrollment-exchange"),
    path("desktop/verify/", desktop_credential_verify, name="desktop-credential-verify"),
    path("ping/", agent_ping, name="ping"),
    path("commands/", agent_commands, name="commands"),
    path("commands/<int:command_id>/ack/", agent_command_ack, name="command-ack"),
    path("events/", agent_event, name="events"),
    path("events/poll/", agent_events_poll, name="events-poll"),
    path("contexts/claim/", agent_context_claim, name="context-claim"),
    path("contexts/release/", agent_context_release, name="context-release"),
    path("status/", agent_status, name="status"),
]
