"""WebSocket consumer for chat threads."""

from __future__ import annotations

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer

from .chat_realtime import agency_group, thread_group


class ChatThreadConsumer(AsyncJsonWebsocketConsumer):
    async def connect(self):
        from .chat_switch import chats_enabled

        if not chats_enabled():
            await self.close(code=4400)
            return
        user = self.scope.get("user")
        if not user or not user.is_authenticated:
            await self.close(code=4401)
            return
        self.thread_id = int(self.scope["url_route"]["kwargs"]["thread_id"])
        allowed, client_side = await self._can_join(user.id, self.thread_id)
        if not allowed:
            await self.close(code=4403)
            return
        self.client_side = client_side
        self.agency_id = await self._agency_id(self.thread_id)
        await self.channel_layer.group_add(thread_group(self.thread_id), self.channel_name)
        if self.agency_id:
            await self.channel_layer.group_add(
                agency_group(self.agency_id, client_side=self.client_side),
                self.channel_name,
            )
        await self.accept()
        await self.send_json({"type": "realtime.ready", "thread_id": self.thread_id})

    async def disconnect(self, code):
        tid = getattr(self, "thread_id", None)
        if tid:
            await self.channel_layer.group_discard(thread_group(tid), self.channel_name)
        aid = getattr(self, "agency_id", None)
        if aid is not None:
            await self.channel_layer.group_discard(
                agency_group(aid, client_side=getattr(self, "client_side", True)),
                self.channel_name,
            )

    async def receive_json(self, content, **kwargs):
        event = str((content or {}).get("type") or "")
        if event == "typing":
            await self.channel_layer.group_send(
                thread_group(self.thread_id),
                {
                    "type": "chat.event",
                    "event": "typing",
                    "payload": {
                        "thread_id": self.thread_id,
                        "user_id": self.scope["user"].id,
                        "active": bool((content or {}).get("active", True)),
                    },
                },
            )

    async def chat_event(self, event):
        # Клиентский сокет не получает internal-only события.
        payload = event.get("payload") or {}
        if getattr(self, "client_side", True) and payload.get("visibility") == "internal":
            return
        await self.send_json({"type": event.get("event") or "chat.event", "data": payload})

    @database_sync_to_async
    def _can_join(self, user_id: int, thread_id: int):
        from django.contrib.auth import get_user_model

        from employees.models import Employee

        from .models import ChatThread

        User = get_user_model()
        user = User.objects.filter(pk=user_id).first()
        thread = ChatThread.objects.filter(pk=thread_id).select_related("agency").first()
        if not user or not thread:
            return False, True
        agency = thread.agency
        if agency and agency.portal_user_id == user.id:
            return thread.is_client_visible_thread, True
        try:
            from .models import AgencyPortalMember

            if AgencyPortalMember.objects.filter(agency=agency, user=user, is_active=True).exists():
                return thread.is_client_visible_thread, True
        except Exception:
            pass
        if user.is_staff or user.is_superuser:
            return True, False
        if Employee.objects.filter(user=user, is_active=True).exists():
            return True, False
        return False, True

    @database_sync_to_async
    def _agency_id(self, thread_id: int):
        from .models import ChatThread

        return ChatThread.objects.filter(pk=thread_id).values_list("agency_id", flat=True).first()
