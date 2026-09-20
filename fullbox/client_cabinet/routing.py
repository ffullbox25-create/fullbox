from django.urls import re_path

from .consumers import ChatThreadConsumer

websocket_urlpatterns = [
    re_path(r"^ws/chat/(?P<thread_id>\d+)/$", ChatThreadConsumer.as_asgi()),
]
