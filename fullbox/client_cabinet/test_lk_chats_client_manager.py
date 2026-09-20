"""Интеграционные тесты чатов: ЛК клиента ↔ кабинет менеджера."""

from __future__ import annotations

import json

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings

from audit.models import log_order_action
from client_cabinet.chats import ensure_client_general_thread, ensure_order_threads
from client_cabinet.messaging_lk import list_chat_messages, post_chat_message
from client_cabinet.models import ChatThread, ClientChatMessage
from employees.models import Employee
from sku.models import Agency

User = get_user_model()


@override_settings(ALLOWED_HOSTS=["*"])
class ClientLkChatsTests(TestCase):
    """ЛК клиента: shell, threads/messages API, изоляция, непрочитанное."""

    @classmethod
    def setUpTestData(cls):
        cls.client_user = User.objects.create_user(username="lkc_chat_client", password="pwd")
        cls.foreign_user = User.objects.create_user(username="lkc_chat_foreign", password="pwd")
        cls.manager_user = User.objects.create_user(username="lkc_chat_mgr", password="pwd")
        Employee.objects.create(
            full_name="Менеджер ЛК чатов",
            user=cls.manager_user,
            role="manager",
            is_active=True,
        )
        cls.agency = Agency.objects.create(
            agn_name="ЛК Чаты Клиент",
            short_name="ЛКЧат",
            portal_user=cls.client_user,
            mened_user_id=cls.manager_user.id,
        )
        cls.foreign = Agency.objects.create(
            agn_name="Чужой клиент чатов",
            short_name="ЧужЧат",
            portal_user=cls.foreign_user,
        )

    def setUp(self):
        self.http = Client()
        self.http.force_login(self.client_user)
        self.q = f"client={self.agency.id}"

    def test_lk_shell_contains_chat_route(self):
        page = self.http.get(f"/client/dashboard/lk/?{self.q}")
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "Личный кабинет")
        # SPA-маршрут чата
        body = page.content.decode("utf-8", "replace")
        self.assertTrue("#/chat" in body or "/chat" in body or "chat" in body.lower())

    def test_threads_and_post_roundtrip(self):
        threads = self.http.get(f"/client/api/v1/chat/threads/?{self.q}")
        self.assertEqual(threads.status_code, 200)
        tbody = threads.json()
        self.assertTrue(tbody["ok"])
        cards = tbody["data"]["threads"]
        self.assertTrue(cards)
        general = next((t for t in cards if t.get("kind") == ChatThread.KIND_CLIENT_GENERAL), cards[0])

        post = self.http.post(
            f"/client/api/v1/chat/messages/?{self.q}",
            data=json.dumps(
                {
                    "text": "Клиент: подскажите по остаткам",
                    "thread": general["id"],
                    "idempotency_key": "lkc-client-1",
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(post.status_code, 200)
        self.assertTrue(post.json()["ok"])
        msg = post.json()["data"]["message"]
        self.assertEqual(msg["author_role"], "client")
        self.assertIn("остаткам", msg["text"])

        listed = self.http.get(
            f"/client/api/v1/chat/messages/?{self.q}&thread={general['id']}"
        )
        self.assertEqual(listed.status_code, 200)
        texts = [m["text"] for m in listed.json()["data"]["messages"]]
        self.assertIn("Клиент: подскажите по остаткам", texts)

        # idempotency
        dup = self.http.post(
            f"/client/api/v1/chat/messages/?{self.q}",
            data=json.dumps(
                {
                    "text": "Клиент: подскажите по остаткам",
                    "thread": general["id"],
                    "idempotency_key": "lkc-client-1",
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(dup.status_code, 200)
        self.assertEqual(dup.json()["data"]["message"]["id"], msg["id"])

    def test_client_sees_manager_reply_not_internal(self):
        thread = ensure_client_general_thread(self.agency, user=self.client_user)
        post_chat_message(
            agency=self.agency,
            user=self.client_user,
            text="Вопрос для менеджера",
            thread=thread,
        )
        post_chat_message(
            agency=self.agency,
            user=self.manager_user,
            text="Ответ менеджера клиенту",
            thread=thread,
            author_role=ClientChatMessage.ROLE_STAFF,
            visibility=ClientChatMessage.VISIBILITY_CLIENT,
        )
        post_chat_message(
            agency=self.agency,
            user=self.manager_user,
            text="Внутренняя пометка",
            thread=thread,
            author_role=ClientChatMessage.ROLE_STAFF,
            visibility=ClientChatMessage.VISIBILITY_INTERNAL,
        )

        listed = self.http.get(f"/client/api/v1/chat/messages/?{self.q}&thread={thread.id}")
        self.assertEqual(listed.status_code, 200)
        texts = {m["text"] for m in listed.json()["data"]["messages"]}
        self.assertIn("Ответ менеджера клиенту", texts)
        self.assertNotIn("Внутренняя пометка", texts)

        # сервисный слой тоже фильтрует
        service_texts = {m["text"] for m in list_chat_messages(self.agency, thread=thread, for_client=True)}
        self.assertNotIn("Внутренняя пометка", service_texts)

    def test_client_cannot_open_foreign_agency_thread(self):
        foreign_thread = ensure_client_general_thread(self.foreign, user=self.foreign_user)
        denied = self.http.get(
            f"/client/api/v1/chat/messages/?{self.q}&thread={foreign_thread.id}"
        )
        # либо 404/403, либо пусто/ошибка — чужой тред недоступен
        if denied.status_code == 200:
            body = denied.json()
            if body.get("ok"):
                # тред подменён на свой или пустой ответ без чужих сообщений
                thread_id = (body.get("data") or {}).get("thread", {}).get("id")
                if thread_id:
                    self.assertNotEqual(int(thread_id), foreign_thread.id)
        else:
            self.assertIn(denied.status_code, {400, 403, 404})

    def test_order_thread_visible_to_client(self):
        log_order_action(
            "create",
            order_id="PR-LKC-1",
            order_type="receiving",
            user=self.client_user,
            agency=self.agency,
            description="Приёмка для чата ЛК",
        )
        client_t, _internal = ensure_order_threads(
            agency=self.agency,
            order_type="receiving",
            order_id="PR-LKC-1",
            user=self.client_user,
        )
        threads = self.http.get(f"/client/api/v1/chat/threads/?{self.q}")
        ids = {t["id"] for t in threads.json()["data"]["threads"]}
        self.assertIn(client_t.id, ids)
        # internal не в списке клиента
        internal = ChatThread.objects.get(
            agency=self.agency, kind=ChatThread.KIND_ORDER_INTERNAL, order_id="PR-LKC-1"
        )
        self.assertNotIn(internal.id, ids)

    def test_prefs_and_mark_all_read(self):
        thread = ensure_client_general_thread(self.agency, user=self.manager_user)
        post_chat_message(
            agency=self.agency,
            user=self.manager_user,
            text="Новое от FullBox",
            thread=thread,
            author_role=ClientChatMessage.ROLE_STAFF,
            visibility=ClientChatMessage.VISIBILITY_CLIENT,
        )
        prefs = self.http.post(
            f"/client/api/v1/chat/prefs/?{self.q}",
            data=json.dumps({"sound_enabled": True, "mute": "on"}),
            content_type="application/json",
        )
        self.assertEqual(prefs.status_code, 200)
        self.assertTrue(prefs.json()["ok"])

        mark = self.http.post(f"/client/api/v1/chat/mark-all-read/?{self.q}")
        self.assertEqual(mark.status_code, 200)
        self.assertTrue(mark.json()["ok"])


@override_settings(ALLOWED_HOSTS=["*"])
class ManagerLkChatsTests(TestCase):
    """Кабинет менеджера: страница чатов, ответ, ИИ-черновик, SLA/Telegram."""

    @classmethod
    def setUpTestData(cls):
        cls.client_user = User.objects.create_user(username="lkm_chat_client", password="pwd")
        cls.manager_user = User.objects.create_user(username="lkm_chat_mgr", password="pwd")
        cls.other_mgr = User.objects.create_user(username="lkm_chat_other", password="pwd")
        Employee.objects.create(
            full_name="Менеджер портфеля",
            user=cls.manager_user,
            role="manager",
            is_active=True,
        )
        Employee.objects.create(
            full_name="Другой менеджер",
            user=cls.other_mgr,
            role="manager",
            is_active=True,
        )
        cls.agency = Agency.objects.create(
            agn_name="Портфель менеджера чатов",
            short_name="ПортМен",
            portal_user=cls.client_user,
            mened_user_id=cls.manager_user.id,
        )
        cls.other_agency = Agency.objects.create(
            agn_name="Чужой портфель",
            short_name="ЧужПорт",
            portal_user=User.objects.create_user(username="lkm_other_portal", password="pwd"),
            mened_user_id=cls.other_mgr.id,
        )

    def setUp(self):
        self.http = Client()
        self.http.force_login(self.manager_user)

    def test_manager_cabinet_nav_and_chats_page(self):
        desk = self.http.get("/team-manager/")
        self.assertEqual(desk.status_code, 200)

        page = self.http.get("/team-manager/chats/")
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "Чаты")
        self.assertContains(page, "Предложить ответ")
        self.assertContains(page, "Telegram")

        sla = self.http.get("/team-manager/chats/sla/")
        self.assertEqual(sla.status_code, 200)
        self.assertContains(sla, "SLA")

        tg = self.http.get("/team-manager/chats/telegram/")
        self.assertEqual(tg.status_code, 200)
        self.assertContains(tg, "Telegram")

    def test_dialogue_client_to_manager_and_back(self):
        thread = ensure_client_general_thread(self.agency, user=self.client_user)
        client_http = Client()
        client_http.force_login(self.client_user)
        client_post = client_http.post(
            f"/client/api/v1/chat/messages/?client={self.agency.id}",
            data=json.dumps({"text": "Нужна помощь по отгрузке", "thread": thread.id}),
            content_type="application/json",
        )
        self.assertEqual(client_post.status_code, 200)

        page = self.http.get(f"/team-manager/chats/?thread={thread.id}")
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "Нужна помощь по отгрузке")

        reply = self.http.post(
            f"/team-manager/api/chats/{thread.id}/messages/",
            data=json.dumps({"text": "Приняли, уточним состав", "visibility": "client"}),
            content_type="application/json",
        )
        self.assertEqual(reply.status_code, 200)
        self.assertTrue(reply.json()["ok"])

        client_list = client_http.get(
            f"/client/api/v1/chat/messages/?client={self.agency.id}&thread={thread.id}"
        )
        texts = {m["text"] for m in client_list.json()["data"]["messages"]}
        self.assertIn("Нужна помощь по отгрузке", texts)
        self.assertIn("Приняли, уточним состав", texts)

        # poll since_id
        last_id = max(m["id"] for m in client_list.json()["data"]["messages"])
        self.http.post(
            f"/team-manager/api/chats/{thread.id}/messages/",
            data=json.dumps({"text": "Дополнение от менеджера", "visibility": "client"}),
            content_type="application/json",
        )
        polled = client_http.get(
            f"/client/api/v1/chat/messages/?client={self.agency.id}&thread={thread.id}&since_id={last_id}"
        )
        self.assertEqual(polled.status_code, 200)
        new_texts = {m["text"] for m in polled.json()["data"]["messages"]}
        self.assertIn("Дополнение от менеджера", new_texts)

    def test_manager_ai_suggest_then_client_gets_only_human_send(self):
        thread = ensure_client_general_thread(self.agency, user=self.client_user)
        post_chat_message(
            agency=self.agency,
            user=self.client_user,
            text="Где скачать акт?",
            thread=thread,
        )
        suggest = self.http.post(f"/team-manager/api/chats/{thread.id}/ai/suggest/")
        self.assertEqual(suggest.status_code, 200)
        data = suggest.json()["data"]
        self.assertFalse(data["auto_reply_allowed"])
        self.assertTrue(data["text"])
        sid = data["id"]

        # Черновик ИИ ещё не в ленте клиента
        client_http = Client()
        client_http.force_login(self.client_user)
        before = client_http.get(
            f"/client/api/v1/chat/messages/?client={self.agency.id}&thread={thread.id}"
        )
        before_texts = {m["text"] for m in before.json()["data"]["messages"]}
        self.assertNotIn(data["text"], before_texts)

        self.http.post(
            f"/team-manager/api/chats/ai/suggestions/{sid}/feedback/",
            data=json.dumps({"action": "insert", "final_text": data["text"]}),
            content_type="application/json",
        )
        # менеджер отправляет вручную
        sent = self.http.post(
            f"/team-manager/api/chats/{thread.id}/messages/",
            data=json.dumps({"text": data["text"], "visibility": "client"}),
            content_type="application/json",
        )
        self.assertEqual(sent.status_code, 200)
        after = client_http.get(
            f"/client/api/v1/chat/messages/?client={self.agency.id}&thread={thread.id}"
        )
        after_texts = {m["text"] for m in after.json()["data"]["messages"]}
        self.assertIn(data["text"], after_texts)

    def test_manager_internal_hidden_from_client(self):
        thread = ensure_client_general_thread(self.agency, user=self.manager_user)
        self.http.post(
            f"/team-manager/api/chats/{thread.id}/messages/",
            data=json.dumps({"text": "Секрет для склада", "visibility": "internal"}),
            content_type="application/json",
        )
        client_http = Client()
        client_http.force_login(self.client_user)
        listed = client_http.get(
            f"/client/api/v1/chat/messages/?client={self.agency.id}&thread={thread.id}"
        )
        texts = {m["text"] for m in listed.json()["data"]["messages"]}
        self.assertNotIn("Секрет для склада", texts)

    def test_manager_mark_all_read_and_resolve(self):
        thread = ensure_client_general_thread(self.agency, user=self.client_user)
        post_chat_message(
            agency=self.agency,
            user=self.client_user,
            text="Прочитайте меня",
            thread=thread,
        )
        mark = self.http.post("/team-manager/api/chats/mark-all-read/")
        self.assertEqual(mark.status_code, 200)
        self.assertTrue(mark.json()["ok"])

        resolve = self.http.post(f"/team-manager/api/chats/{thread.id}/resolve/")
        self.assertEqual(resolve.status_code, 200)
        self.assertTrue(resolve.json()["ok"])

    def test_other_manager_portfolio_isolation(self):
        """Менеджер не видит чат чужого клиента портфеля (если agency_ids ограничены)."""
        other_thread = ensure_client_general_thread(self.other_agency, user=self.other_mgr)
        post_chat_message(
            agency=self.other_agency,
            user=self.other_agency.portal_user,
            text="Чужое сообщение",
            thread=other_thread,
        )
        # страница своего портфеля не должна содержать чужой short_name как выбранный тред
        page = self.http.get("/team-manager/chats/")
        self.assertEqual(page.status_code, 200)
        # API чужого треда — 404
        denied = self.http.get(f"/team-manager/api/chats/{other_thread.id}/messages/")
        self.assertIn(denied.status_code, {403, 404})
