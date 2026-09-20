from django.urls import re_path

from .consumers import AgentRealtimeConsumer, BrowserAgentContextConsumer


websocket_urlpatterns = [
    re_path(r"^ws/agent/$", AgentRealtimeConsumer.as_asgi()),
    re_path(r"^ws/agent/context/(?P<context_id>[^/]+)/$", BrowserAgentContextConsumer.as_asgi()),
]
