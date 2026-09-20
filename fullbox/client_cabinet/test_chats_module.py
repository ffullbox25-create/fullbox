"""Единый контур чатов: клиент ↔ менеджер ↔ склад."""

from __future__ import annotations

import json

from django.contrib.auth import get_user_model
from django.test import Client, TestCase

from audit.models import log_order_action
from client_cabinet.chats import (
    ensure_client_general_thread,
    ensure_order_threads,
    get_thread_for_client,
    threads_for_client,
    threads_for_warehouse,
)
from client_cabinet.messaging_lk import list_chat_messages, post_chat_message
from client_cabinet.models import ChatThread, ClientChatMessage
from employees.models import Employee
from sku.models import Agency

User = get_user_model()


class ChatThreadsServiceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.client_user = User.objects.create_user(username="chat_client", password="pwd")
        cls.staff_user = User.objects.create_user(username="chat_staff", password="pwd", is_staff=True)
        cls.agency = Agency.objects.create(
            agn_name="Чат Клиент",
            short_name="ЧатКл",
            portal_user=cls.client_user,
        )

    def test_general_thread_created_once(self):
        t1 = ensure_client_general_thread(self.agency, user=self.staff_user)
        t2 = ensure_client_general_thread(self.agency, user=self.staff_user)
        self.assertIsNotNone(t1)
        self.assertEqual(t1.id, t2.id)
        self.assertEqual(
            ChatThread.objects.filter(agency=self.agency, kind=ChatThread.KIND_CLIENT_GENERAL).count(),
            1,
        )

    def test_order_threads_client_and_internal(self):
        client_t, internal_t = ensure_order_threads(
            agency=self.agency,
            order_type="receiving",
            order_id="PR-TEST-1",
            user=self.staff_user,
        )
        self.assertEqual(client_t.kind, ChatThread.KIND_ORDER_CLIENT)
        self.assertEqual(internal_t.kind, ChatThread.KIND_ORDER_INTERNAL)

    def test_client_cannot_see_internal_messages(self):
        general = ensure_client_general_thread(self.agency)
        _, internal = ensure_order_threads(
            agency=self.agency, order_type="shipping", order_id="OT-1", user=self.staff_user
        )
        post_chat_message(
            agency=self.agency,
            user=self.staff_user,
            text="Клиенту видно",
            author_role=ClientChatMessage.ROLE_STAFF,
            thread=general,
            visibility=ClientChatMessage.VISIBILITY_CLIENT,
        )
        post_chat_message(
            agency=self.agency,
            user=self.staff_user,
            text="Только склад",
            author_role=ClientChatMessage.ROLE_STAFF,
            thread=internal,
            visibility=ClientChatMessage.VISIBILITY_INTERNAL,
        )
        client_msgs = list_chat_messages(self.agency, for_client=True)
        texts = {m["text"] for m in client_msgs}
        self.assertIn("Клиенту видно", texts)
        self.assertNotIn("Только склад", texts)


class ChatBridgeUniteTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.client_user = User.objects.create_user(username="unite_client", password="pwd")
        cls.manager_user = User.objects.create_user(username="unite_mgr", password="pwd")
        cls.warehouse_user = User.objects.create_user(username="unite_wh", password="pwd")
        Employee.objects.create(full_name="Менеджер", user=cls.manager_user, role="manager", is_active=True)
        Employee.objects.create(
            full_name="Кладовщик", user=cls.warehouse_user, role="storekeeper", is_active=True
        )
        cls.agency = Agency.objects.create(
            agn_name="Unite Agency",
            short_name="Unite",
            portal_user=cls.client_user,
            mened_user_id=cls.manager_user.id,
        )

    def test_create_order_opens_client_and_internal_threads(self):
        log_order_action(
            "create",
            order_id="PR-UNITE-1",
            order_type="receiving",
            user=self.client_user,
            agency=self.agency,
            description="Заявка на приемку (заявка)",
            payload={"status": "sent_unconfirmed"},
        )
        client_t = ChatThread.objects.get(
            agency=self.agency, kind=ChatThread.KIND_ORDER_CLIENT, order_id="PR-UNITE-1"
        )
        internal_t = ChatThread.objects.get(
            agency=self.agency, kind=ChatThread.KIND_ORDER_INTERNAL, order_id="PR-UNITE-1"
        )
        self.assertTrue(
            ClientChatMessage.objects.filter(
                thread=client_t, author_role=ClientChatMessage.ROLE_SYSTEM
            ).exists()
        )
        self.assertTrue(
            ClientChatMessage.objects.filter(
                thread=internal_t, author_role=ClientChatMessage.ROLE_SYSTEM
            ).exists()
        )
        client_threads = threads_for_client(self.agency)
        kinds = {t.kind for t in client_threads}
        self.assertIn(ChatThread.KIND_ORDER_CLIENT, kinds)
        self.assertNotIn(ChatThread.KIND_ORDER_INTERNAL, kinds)

    def test_warehouse_comment_goes_internal_client_does_not_see(self):
        log_order_action(
            "create",
            order_id="PR-UNITE-2",
            order_type="receiving",
            user=self.client_user,
            agency=self.agency,
            description="Создана",
        )
        log_order_action(
            "comment",
            order_id="PR-UNITE-2",
            order_type="receiving",
            user=self.warehouse_user,
            agency=self.agency,
            description="Не хватает маркировки",
            payload={"comment": "Не хватает маркировки"},
        )
        internal = ChatThread.objects.get(
            agency=self.agency, kind=ChatThread.KIND_ORDER_INTERNAL, order_id="PR-UNITE-2"
        )
        self.assertTrue(
            ClientChatMessage.objects.filter(
                thread=internal,
                text="Не хватает маркировки",
                visibility=ClientChatMessage.VISIBILITY_INTERNAL,
            ).exists()
        )
        client_t = ChatThread.objects.get(
            agency=self.agency, kind=ChatThread.KIND_ORDER_CLIENT, order_id="PR-UNITE-2"
        )
        client_texts = {
            m["text"]
            for m in list_chat_messages(self.agency, thread=client_t, for_client=True)
        }
        self.assertNotIn("Не хватает маркировки", client_texts)

        wh_threads = threads_for_warehouse()
        self.assertTrue(any(t.order_id == "PR-UNITE-2" for t in wh_threads))

    def test_manager_client_visible_comment_in_order_client_thread(self):
        log_order_action(
            "create",
            order_id="OT-UNITE-3",
            order_type="shipping",
            user=self.manager_user,
            agency=self.agency,
            description="Отгрузка",
        )
        log_order_action(
            "comment",
            order_id="OT-UNITE-3",
            order_type="shipping",
            user=self.manager_user,
            agency=self.agency,
            description="Уточните адрес",
            payload={"comment": "Уточните адрес", "notify_client": True},
        )
        client_t = ChatThread.objects.get(
            agency=self.agency, kind=ChatThread.KIND_ORDER_CLIENT, order_id="OT-UNITE-3"
        )
        self.assertTrue(
            ClientChatMessage.objects.filter(
                thread=client_t,
                text="Уточните адрес",
                visibility=ClientChatMessage.VISIBILITY_CLIENT,
            ).exists()
        )

    def test_client_api_threads_and_order_thread_post(self):
        log_order_action(
            "create",
            order_id="PR-API-4",
            order_type="receiving",
            user=self.client_user,
            agency=self.agency,
            description="API create",
        )
        http = Client()
        http.force_login(self.client_user)
        threads_resp = http.get("/client/api/v1/chat/threads/")
        self.assertEqual(threads_resp.status_code, 200)
        cards = threads_resp.json()["data"]["threads"]
        order_card = next(c for c in cards if c.get("order_id") == "PR-API-4")
        thread_id = order_card["id"]

        post = http.post(
            "/client/api/v1/chat/messages/",
            data=json.dumps({"text": "Вопрос по заявке", "thread": thread_id}),
            content_type="application/json",
        )
        self.assertEqual(post.status_code, 200)
        self.assertTrue(
            ClientChatMessage.objects.filter(
                thread_id=thread_id,
                text="Вопрос по заявке",
                author_role=ClientChatMessage.ROLE_CLIENT,
            ).exists()
        )

        # Внутренний тред клиенту недоступен.
        internal = ChatThread.objects.get(
            agency=self.agency, kind=ChatThread.KIND_ORDER_INTERNAL, order_id="PR-API-4"
        )
        denied = http.get(f"/client/api/v1/chat/messages/?thread={internal.id}")
        self.assertEqual(denied.status_code, 404)

    def test_client_cannot_open_internal_via_get_thread_helper(self):
        _, internal = ensure_order_threads(
            agency=self.agency, order_type="receiving", order_id="PR-HIDE", user=self.manager_user
        )
        self.assertIsNone(get_thread_for_client(agency=self.agency, thread_id=internal.id))


class TeamManagerChatsViewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.manager_user = User.objects.create_user(username="chat_mgr", password="pwd")
        cls.warehouse_user = User.objects.create_user(username="chat_wh", password="pwd")
        Employee.objects.create(
            full_name="Менеджер Чатов",
            user=cls.manager_user,
            role="manager",
            is_active=True,
        )
        Employee.objects.create(
            full_name="Кладовщик Чатов",
            user=cls.warehouse_user,
            role="storekeeper",
            is_active=True,
        )
        cls.client_user = User.objects.create_user(username="chat_mgr_client", password="pwd")
        cls.agency = Agency.objects.create(
            agn_name="Портфель чатов",
            short_name="ПортЧат",
            portal_user=cls.client_user,
            mened_user_id=cls.manager_user.id,
        )

    def test_manager_chats_page_and_post(self):
        http = Client()
        http.force_login(self.manager_user)
        thread = ensure_client_general_thread(self.agency, user=self.manager_user)
        post_chat_message(
            agency=self.agency,
            user=self.client_user,
            text="Вопрос клиента",
            author_role=ClientChatMessage.ROLE_CLIENT,
            thread=thread,
        )
        page = http.get("/team-manager/chats/")
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "Чаты")

        api = http.post(
            f"/team-manager/api/chats/{thread.id}/messages/",
            data=json.dumps({"text": "Ответ менеджера", "visibility": "client"}),
            content_type="application/json",
        )
        self.assertEqual(api.status_code, 200)
        self.assertTrue(api.json()["ok"])

    def test_warehouse_sees_internal_only_and_cannot_post_to_client_thread(self):
        log_order_action(
            "create",
            order_id="PR-WH-5",
            order_type="receiving",
            user=self.manager_user,
            agency=self.agency,
            description="Для склада",
        )
        client_t = ChatThread.objects.get(
            agency=self.agency, kind=ChatThread.KIND_ORDER_CLIENT, order_id="PR-WH-5"
        )
        internal_t = ChatThread.objects.get(
            agency=self.agency, kind=ChatThread.KIND_ORDER_INTERNAL, order_id="PR-WH-5"
        )
        http = Client()
        http.force_login(self.warehouse_user)
        page = http.get("/team-manager/chats/?kind=internal")
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "PR-WH-5")

        denied = http.post(
            f"/team-manager/api/chats/{client_t.id}/messages/",
            data=json.dumps({"text": "Клиенту напрямую"}),
            content_type="application/json",
        )
        self.assertEqual(denied.status_code, 403)

        ok = http.post(
            f"/team-manager/api/chats/{internal_t.id}/messages/",
            data=json.dumps({"text": "Фото ворот приложил"}),
            content_type="application/json",
        )
        self.assertEqual(ok.status_code, 200)
        msg = ClientChatMessage.objects.get(thread=internal_t, text="Фото ворот приложил")
        self.assertEqual(msg.visibility, ClientChatMessage.VISIBILITY_INTERNAL)

        # Клиент не видит складское сообщение.
        client_http = Client()
        client_http.force_login(self.client_user)
        listed = client_http.get(f"/client/api/v1/chat/messages/?thread={client_t.id}")
        texts = {m["text"] for m in listed.json()["data"]["messages"]}
        self.assertNotIn("Фото ворот приложил", texts)

    def test_end_to_end_client_manager_warehouse(self):
        """Клиент пишет в чат заявки → менеджер отвечает клиенту → склад во внутренний."""
        log_order_action(
            "create",
            order_id="PR-E2E-6",
            order_type="receiving",
            user=self.client_user,
            agency=self.agency,
            description="E2E",
        )
        client_t = ChatThread.objects.get(
            agency=self.agency, kind=ChatThread.KIND_ORDER_CLIENT, order_id="PR-E2E-6"
        )
        internal_t = ChatThread.objects.get(
            agency=self.agency, kind=ChatThread.KIND_ORDER_INTERNAL, order_id="PR-E2E-6"
        )

        client_http = Client()
        client_http.force_login(self.client_user)
        client_http.post(
            "/client/api/v1/chat/messages/",
            data=json.dumps({"text": "Когда приёмка?", "thread": client_t.id}),
            content_type="application/json",
        )

        mgr = Client()
        mgr.force_login(self.manager_user)
        mgr.post(
            f"/team-manager/api/chats/{client_t.id}/messages/",
            data=json.dumps({"text": "Завтра с 10:00", "visibility": "client"}),
            content_type="application/json",
        )
        mgr.post(
            f"/team-manager/api/chats/{internal_t.id}/messages/",
            data=json.dumps({"text": "Подготовьте ворота 3", "visibility": "internal"}),
            content_type="application/json",
        )

        wh = Client()
        wh.force_login(self.warehouse_user)
        wh.post(
            f"/team-manager/api/chats/{internal_t.id}/messages/",
            data=json.dumps({"text": "Ворота готовы"}),
            content_type="application/json",
        )

        client_view = client_http.get(f"/client/api/v1/chat/messages/?thread={client_t.id}")
        texts = {m["text"] for m in client_view.json()["data"]["messages"]}
        self.assertIn("Когда приёмка?", texts)
        self.assertIn("Завтра с 10:00", texts)
        self.assertNotIn("Подготовьте ворота 3", texts)
        self.assertNotIn("Ворота готовы", texts)

        mgr_internal = mgr.get(f"/team-manager/api/chats/{internal_t.id}/messages/")
        internal_texts = {m["text"] for m in mgr_internal.json()["data"]["messages"]}
        self.assertIn("Подготовьте ворота 3", internal_texts)
        self.assertIn("Ворота готовы", internal_texts)


class ChatMessengerUpgradeTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.client_user = User.objects.create_user(username="msg_client", password="pwd")
        cls.staff_user = User.objects.create_user(username="msg_staff", password="pwd", is_staff=True)
        cls.agency = Agency.objects.create(
            agn_name="Messenger Agency",
            short_name="MsgAg",
            portal_user=cls.client_user,
        )

    def test_idempotency_prevents_duplicate(self):
        thread = ensure_client_general_thread(self.agency, user=self.client_user)
        m1 = post_chat_message(
            agency=self.agency,
            user=self.client_user,
            text="Один раз",
            thread=thread,
            idempotency_key="same-key-1",
        )
        m2 = post_chat_message(
            agency=self.agency,
            user=self.client_user,
            text="Один раз",
            thread=thread,
            idempotency_key="same-key-1",
        )
        self.assertEqual(m1.id, m2.id)
        self.assertEqual(
            ClientChatMessage.objects.filter(thread=thread, text="Один раз").count(),
            1,
        )

    def test_since_id_poll_and_reaction(self):
        from client_cabinet.messaging_lk import toggle_message_reaction

        thread = ensure_client_general_thread(self.agency)
        a = post_chat_message(agency=self.agency, user=self.client_user, text="A", thread=thread)
        b = post_chat_message(agency=self.agency, user=self.staff_user, text="B", thread=thread)
        newer = list_chat_messages(self.agency, thread=thread, for_client=True, since_id=a.id)
        texts = [m["text"] for m in newer]
        self.assertEqual(texts, ["B"])
        data = toggle_message_reaction(message=b, user=self.client_user, emoji="👍")
        self.assertTrue(any(r["emoji"] == "👍" for r in data["reactions"]))

    def test_delivery_status_after_read(self):
        thread = ensure_client_general_thread(self.agency)
        msg = post_chat_message(
            agency=self.agency,
            user=self.client_user,
            text="Прочти",
            thread=thread,
        )
        from client_cabinet.messaging_lk import mark_chat_read_for_staff, serialize_chat_message

        mark_chat_read_for_staff(thread=thread, user=self.staff_user)
        msg.refresh_from_db()
        data = serialize_chat_message(msg, agency_id=self.agency.id)
        self.assertEqual(data["delivery_status"], "read")

    def test_prefs_api(self):
        http = Client()
        http.force_login(self.client_user)
        resp = http.post(
            "/client/api/v1/chat/prefs/",
            data=json.dumps({"mute": "1h", "sound_enabled": True}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["ok"])


class ChatPhase2TaskResolveTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.client_user = User.objects.create_user(username="p2_client", password="pwd")
        cls.manager_user = User.objects.create_user(username="p2_mgr", password="pwd")
        Employee.objects.create(
            full_name="P2 Менеджер", user=cls.manager_user, role="manager", is_active=True
        )
        cls.agency = Agency.objects.create(
            agn_name="P2 Agency",
            short_name="P2",
            portal_user=cls.client_user,
            mened_user_id=cls.manager_user.id,
        )

    def test_create_task_from_message_and_system_event(self):
        from client_cabinet.chat_tasks import create_task_from_message
        from todo.models import Task

        thread = ensure_client_general_thread(self.agency, user=self.manager_user)
        msg = post_chat_message(
            agency=self.agency,
            user=self.client_user,
            text="Нужна проверка остатков",
            thread=thread,
        )
        result = create_task_from_message(message=msg, user=self.manager_user, title="Проверить остатки")
        self.assertIsNotNone(result)
        self.assertTrue(Task.objects.filter(pk=result["task_id"]).exists())
        self.assertTrue(
            ClientChatMessage.objects.filter(
                thread=thread,
                author_role=ClientChatMessage.ROLE_SYSTEM,
                text__startswith="Создана задача №",
            ).exists()
        )
        self.assertTrue(
            ChatThread.objects.filter(kind=ChatThread.KIND_TASK, task_id=result["task_id"]).exists()
        )

    def test_resolve_and_reopen_on_client_message(self):
        from client_cabinet.chat_tasks import resolve_conversation

        thread = ensure_client_general_thread(self.agency, user=self.manager_user)
        resolve_conversation(thread=thread, user=self.manager_user)
        thread.refresh_from_db()
        self.assertEqual(thread.conversation_status, ChatThread.STATUS_RESOLVED)
        post_chat_message(
            agency=self.agency,
            user=self.client_user,
            text="Ещё вопрос",
            thread=thread,
        )
        thread.refresh_from_db()
        self.assertEqual(thread.conversation_status, ChatThread.STATUS_NEEDS_STAFF)

    def test_manager_create_task_api(self):
        thread = ensure_client_general_thread(self.agency, user=self.manager_user)
        msg = post_chat_message(
            agency=self.agency,
            user=self.client_user,
            text="API задача",
            thread=thread,
        )
        http = Client()
        http.force_login(self.manager_user)
        resp = http.post(
            f"/team-manager/api/chats/messages/{msg.id}/create-task/",
            data=json.dumps({"title": "Задача из API"}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["ok"])

    def test_mark_all_read_manager(self):
        thread = ensure_client_general_thread(self.agency, user=self.manager_user)
        post_chat_message(
            agency=self.agency,
            user=self.client_user,
            text="Непрочитанное",
            thread=thread,
        )
        http = Client()
        http.force_login(self.manager_user)
        resp = http.post("/team-manager/api/chats/mark-all-read/")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["ok"])
        self.assertGreaterEqual(resp.json()["data"]["marked"], 1)


class ChatMentionsPinsEditTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.client_user = User.objects.create_user(username="act_client", password="pwd")
        cls.manager_user = User.objects.create_user(username="act_mgr", password="pwd")
        cls.other_mgr = User.objects.create_user(username="act_other", password="pwd")
        Employee.objects.create(
            full_name="Анна Менеджер", user=cls.manager_user, role="manager", is_active=True
        )
        Employee.objects.create(
            full_name="Борис Логист", user=cls.other_mgr, role="logistician", is_active=True
        )
        cls.agency = Agency.objects.create(
            agn_name="Act Agency",
            short_name="Act",
            portal_user=cls.client_user,
            mened_user_id=cls.manager_user.id,
        )

    def test_mention_sync_and_edit_delete_pin(self):
        from client_cabinet.chat_actions import (
            edit_message,
            pin_message,
            soft_delete_message,
            sync_mentions,
        )
        from client_cabinet.models import ChatMessageAudit, ChatMessageMention

        thread = ensure_client_general_thread(self.agency, user=self.manager_user)
        msg = post_chat_message(
            agency=self.agency,
            user=self.client_user,
            text="Здравствуйте, @Анна помогите",
            thread=thread,
        )
        sync_mentions(msg)
        self.assertTrue(
            ChatMessageMention.objects.filter(message=msg, user=self.manager_user).exists()
        )

        edited = edit_message(message=msg, user=self.client_user, text="Уточнение для @Анна")
        self.assertTrue(edited.edited_at)
        self.assertTrue(
            ChatMessageAudit.objects.filter(
                message=msg, action=ChatMessageAudit.ACTION_EDIT
            ).exists()
        )

        pin_message(thread=thread, message=msg, user=self.manager_user)
        thread.refresh_from_db()
        self.assertEqual(thread.pinned_message_id, msg.id)

        soft_delete_message(message=msg, user=self.manager_user)
        msg.refresh_from_db()
        self.assertTrue(msg.is_deleted)
        thread.refresh_from_db()
        self.assertIsNone(thread.pinned_message_id)

    def test_manager_api_edit_and_mention_candidates(self):
        thread = ensure_client_general_thread(self.agency, user=self.manager_user)
        msg = post_chat_message(
            agency=self.agency,
            user=self.manager_user,
            text="Черновик",
            author_role=ClientChatMessage.ROLE_STAFF,
            thread=thread,
            visibility=ClientChatMessage.VISIBILITY_CLIENT,
        )
        http = Client()
        http.force_login(self.manager_user)
        resp = http.post(
            f"/team-manager/api/chats/messages/{msg.id}/edit/",
            data=json.dumps({"text": "Готовый ответ @Борис"}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["ok"])
        cand = http.get("/team-manager/api/chats/mention-candidates/?q=Борис")
        self.assertEqual(cand.status_code, 200)
        labels = [c["label"] for c in cand.json()["data"]["candidates"]]
        self.assertTrue(any("Борис" in x for x in labels))


class ChatFilesTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.client_user = User.objects.create_user(username="files_client", password="pwd")
        cls.manager_user = User.objects.create_user(username="files_mgr", password="pwd")
        Employee.objects.create(
            full_name="Files Mgr", user=cls.manager_user, role="manager", is_active=True
        )
        cls.agency = Agency.objects.create(
            agn_name="Files Agency",
            short_name="Files",
            portal_user=cls.client_user,
            mened_user_id=cls.manager_user.id,
        )

    def test_reject_bad_extension(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        from client_cabinet.chat_files import ChatFileError, validate_chat_uploads
        from client_cabinet.chats import ensure_client_general_thread

        bad = SimpleUploadedFile("virus.exe", b"MZ", content_type="application/octet-stream")
        with self.assertRaises(ChatFileError):
            validate_chat_uploads([bad])

        thread = ensure_client_general_thread(self.agency, user=self.manager_user)
        with self.assertRaises(ChatFileError):
            post_chat_message(
                agency=self.agency,
                user=self.client_user,
                text="файл",
                files=[bad],
                thread=thread,
            )

    def test_accept_image_and_serialize_is_image(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        from client_cabinet.chats import ensure_client_general_thread

        # минимальный PNG 1x1
        png = (
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
            b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00"
            b"\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
        )
        uploaded = SimpleUploadedFile("photo.png", png, content_type="image/png")
        thread = ensure_client_general_thread(self.agency, user=self.manager_user)
        msg = post_chat_message(
            agency=self.agency,
            user=self.client_user,
            text="",
            files=[uploaded],
            thread=thread,
        )
        self.assertIsNotNone(msg)
        data = list_chat_messages(self.agency, thread=thread, for_client=True)
        att = next(m for m in data if m["id"] == msg.id)["attachments"]
        self.assertEqual(len(att), 1)
        self.assertTrue(att[0]["is_image"])
        self.assertIn("size_label", att[0])

    def test_manager_api_rejects_exe(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        from client_cabinet.chats import ensure_client_general_thread

        thread = ensure_client_general_thread(self.agency, user=self.manager_user)
        http = Client()
        http.force_login(self.manager_user)
        bad = SimpleUploadedFile("bad.exe", b"MZ123", content_type="application/octet-stream")
        resp = http.post(
            f"/team-manager/api/chats/{thread.id}/messages/",
            data={"text": "см. файл", "attachments": bad},
        )
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(resp.json()["ok"])


class ChatTripThreadsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.logistician = User.objects.create_user(username="trip_log", password="pwd")
        cls.storekeeper = User.objects.create_user(username="trip_wh", password="pwd")
        cls.client_user = User.objects.create_user(username="trip_client", password="pwd")
        Employee.objects.create(
            full_name="Логист рейса", user=cls.logistician, role="logistician", is_active=True
        )
        Employee.objects.create(
            full_name="Кладовщик рейса", user=cls.storekeeper, role="storekeeper", is_active=True
        )
        cls.agency = Agency.objects.create(
            agn_name="Trip Agency",
            short_name="TripA",
            portal_user=cls.client_user,
        )

    def test_ensure_trip_thread_internal_not_for_client(self):
        from datetime import date

        from client_cabinet.chat_trips import ensure_trip_thread
        from logistics.models import LogisticsTrip, LogisticsTripOrder
        from shipping.models import ShippingOrder

        trip = LogisticsTrip.objects.create(
            number="TEST-TRIP-1",
            trip_date=date(2026, 7, 19),
            status=LogisticsTrip.STATUS_PLANNED,
        )
        order = ShippingOrder.objects.create(
            number="OT-TRIP-1",
            agency=self.agency,
            status=ShippingOrder.STATUS_PACKED,
        )
        LogisticsTripOrder.objects.create(
            trip=trip,
            shipping_order=order,
            loading_sequence=1,
            delivery_sequence=1,
        )

        thread = ensure_trip_thread(trip, user=self.logistician)
        self.assertIsNotNone(thread)
        self.assertEqual(thread.kind, ChatThread.KIND_TRIP)
        self.assertFalse(thread.is_client_visible_thread)

        client_threads = threads_for_client(self.agency)
        self.assertFalse(any(t.id == thread.id for t in client_threads))

        wh = threads_for_warehouse()
        self.assertTrue(any(t.id == thread.id for t in wh))

        http = Client()
        http.force_login(self.storekeeper)
        page = http.get(f"/team-manager/chats/?kind=logistics&thread={thread.id}")
        self.assertEqual(page.status_code, 200)
        api = http.post(
            f"/team-manager/api/chats/{thread.id}/messages/",
            data=json.dumps({"text": "Паллеты готовы"}),
            content_type="application/json",
        )
        self.assertEqual(api.status_code, 200)
        self.assertTrue(api.json()["ok"])


class ChatLifecycleEventsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.client_user = User.objects.create_user(username="lc_client", password="pwd")
        cls.manager_user = User.objects.create_user(username="lc_mgr", password="pwd")
        cls.warehouse_user = User.objects.create_user(username="lc_wh", password="pwd")
        Employee.objects.create(
            full_name="LC Менеджер", user=cls.manager_user, role="manager", is_active=True
        )
        Employee.objects.create(
            full_name="LC Кладовщик", user=cls.warehouse_user, role="storekeeper", is_active=True
        )
        cls.agency = Agency.objects.create(
            agn_name="Lifecycle Agency",
            short_name="LC",
            portal_user=cls.client_user,
            mened_user_id=cls.manager_user.id,
        )

    def test_accepted_into_work_posts_client_safe_system_message(self):
        log_order_action(
            "create",
            order_id="PR-LC-1",
            order_type="receiving",
            user=self.client_user,
            agency=self.agency,
            description="Заявка на приемку",
            payload={"status": "sent_unconfirmed"},
        )
        log_order_action(
            "status",
            order_id="PR-LC-1",
            order_type="receiving",
            user=self.manager_user,
            agency=self.agency,
            description="Передана в работу кладовщику",
            payload={"status": "warehouse", "status_label": "Принята в работу складом"},
        )
        client_t = ChatThread.objects.get(
            agency=self.agency, kind=ChatThread.KIND_ORDER_CLIENT, order_id="PR-LC-1"
        )
        internal_t = ChatThread.objects.get(
            agency=self.agency, kind=ChatThread.KIND_ORDER_INTERNAL, order_id="PR-LC-1"
        )
        client_texts = list(
            ClientChatMessage.objects.filter(
                thread=client_t, author_role=ClientChatMessage.ROLE_SYSTEM
            ).values_list("text", flat=True)
        )
        self.assertTrue(any("Менеджер принял заявку в работу" in t for t in client_texts))
        # Клиент не видит складской формулировки «кладовщик/складом» в системных.
        self.assertFalse(any("кладовщик" in t.lower() for t in client_texts))

        internal_texts = list(
            ClientChatMessage.objects.filter(
                thread=internal_t, author_role=ClientChatMessage.ROLE_SYSTEM
            ).values_list("text", flat=True)
        )
        self.assertTrue(len(internal_texts) >= 2)

    def test_warehouse_internal_update_not_shown_to_client(self):
        log_order_action(
            "create",
            order_id="PR-LC-2",
            order_type="receiving",
            user=self.client_user,
            agency=self.agency,
            description="Создана",
        )
        log_order_action(
            "update",
            order_id="PR-LC-2",
            order_type="receiving",
            user=self.warehouse_user,
            agency=self.agency,
            description="Открыт акт размещения",
            payload={"status": "placement", "act": "placement", "status_label": "Размещение"},
        )
        client_t = ChatThread.objects.get(
            agency=self.agency, kind=ChatThread.KIND_ORDER_CLIENT, order_id="PR-LC-2"
        )
        internal_t = ChatThread.objects.get(
            agency=self.agency, kind=ChatThread.KIND_ORDER_INTERNAL, order_id="PR-LC-2"
        )
        client_sys = list(
            ClientChatMessage.objects.filter(
                thread=client_t, author_role=ClientChatMessage.ROLE_SYSTEM
            ).values_list("text", flat=True)
        )
        self.assertFalse(any("размещен" in t.lower() for t in client_sys))
        self.assertTrue(
            ClientChatMessage.objects.filter(
                thread=internal_t,
                author_role=ClientChatMessage.ROLE_SYSTEM,
                text__icontains="акт размещения",
            ).exists()
        )

    def test_done_posts_client_message_and_archives(self):
        log_order_action(
            "create",
            order_id="PR-LC-3",
            order_type="receiving",
            user=self.client_user,
            agency=self.agency,
            description="Создана",
        )
        log_order_action(
            "status",
            order_id="PR-LC-3",
            order_type="receiving",
            user=self.manager_user,
            agency=self.agency,
            description="Заявка выполнена",
            payload={"status": "done", "status_label": "Выполнена"},
        )
        client_t = ChatThread.objects.get(
            agency=self.agency, kind=ChatThread.KIND_ORDER_CLIENT, order_id="PR-LC-3"
        )
        self.assertTrue(
            ClientChatMessage.objects.filter(
                thread=client_t,
                author_role=ClientChatMessage.ROLE_SYSTEM,
                text="Заявка выполнена.",
            ).exists()
        )
        client_t.refresh_from_db()
        self.assertTrue(client_t.is_archived)


class ChatSlaTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.manager_user = User.objects.create_user(username="sla_mgr", password="pwd")
        cls.head_user = User.objects.create_user(username="sla_head", password="pwd")
        cls.warehouse_user = User.objects.create_user(username="sla_wh", password="pwd")
        cls.client_user = User.objects.create_user(username="sla_client", password="pwd")
        Employee.objects.create(
            full_name="SLA Менеджер", user=cls.manager_user, role="manager", is_active=True
        )
        Employee.objects.create(
            full_name="SLA Руководитель", user=cls.head_user, role="head_manager", is_active=True
        )
        Employee.objects.create(
            full_name="SLA Кладовщик", user=cls.warehouse_user, role="storekeeper", is_active=True
        )
        cls.agency = Agency.objects.create(
            agn_name="SLA Agency",
            short_name="SLA",
            portal_user=cls.client_user,
            mened_user_id=cls.manager_user.id,
        )

    def test_unanswered_and_avg_response(self):
        from datetime import timedelta

        from django.utils import timezone

        from client_cabinet.chat_sla import build_chat_sla_report

        thread = ensure_client_general_thread(self.agency, user=self.manager_user)
        client_msg = post_chat_message(
            agency=self.agency,
            user=self.client_user,
            text="Ждём ответа",
            thread=thread,
        )
        ClientChatMessage.objects.filter(pk=client_msg.id).update(
            created_at=timezone.now() - timedelta(minutes=180)
        )
        report = build_chat_sla_report(agency_ids=[self.agency.id], sla_minutes=120)
        self.assertGreaterEqual(report["kpi"]["unanswered"], 1)
        self.assertGreaterEqual(report["kpi"]["overdue"], 1)
        self.assertTrue(any(r["thread_id"] == thread.id for r in report["unanswered"]))

        post_chat_message(
            agency=self.agency,
            user=self.manager_user,
            text="Ответили",
            author_role=ClientChatMessage.ROLE_STAFF,
            thread=thread,
            visibility=ClientChatMessage.VISIBILITY_CLIENT,
        )
        report2 = build_chat_sla_report(agency_ids=[self.agency.id], sla_minutes=120)
        self.assertFalse(any(r["thread_id"] == thread.id for r in report2["unanswered"]))
        self.assertIsNotNone(report2["kpi"]["avg_first_response_min"])
        self.assertGreaterEqual(report2["kpi"]["avg_first_response_min"], 100)

    def test_sla_page_manager_ok_warehouse_denied(self):
        thread = ensure_client_general_thread(self.agency, user=self.manager_user)
        post_chat_message(
            agency=self.agency,
            user=self.client_user,
            text="Страница SLA",
            thread=thread,
        )
        http = Client()
        http.force_login(self.manager_user)
        page = http.get("/team-manager/chats/sla/")
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "SLA чатов")
        self.assertContains(page, "Без ответа")

        http.force_login(self.head_user)
        head_page = http.get("/team-manager/chats/sla/")
        self.assertEqual(head_page.status_code, 200)

        http.force_login(self.warehouse_user)
        denied = http.get("/team-manager/chats/sla/")
        self.assertIn(denied.status_code, {302, 403})


class ChatTelegramTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.client_user = User.objects.create_user(username="tg_client", password="pwd")
        cls.manager_user = User.objects.create_user(username="tg_mgr", password="pwd")
        cls.other_user = User.objects.create_user(username="tg_other", password="pwd")
        Employee.objects.create(
            full_name="TG Менеджер", user=cls.manager_user, role="manager", is_active=True
        )
        Employee.objects.create(
            full_name="TG Другой", user=cls.other_user, role="manager", is_active=True
        )
        cls.agency = Agency.objects.create(
            agn_name="TG Agency",
            short_name="TG",
            portal_user=cls.client_user,
            mened_user_id=cls.manager_user.id,
        )

    def test_bind_by_code_and_notify_client_message(self):
        from unittest.mock import patch

        from django.test import override_settings

        from client_cabinet.chat_telegram import bind_chat_by_code, issue_link_code, process_telegram_update
        from client_cabinet.models import ChatNotificationPreference

        code = issue_link_code(self.manager_user)
        self.assertTrue(code)
        pref = bind_chat_by_code(code=code, chat_id="991122")
        self.assertIsNotNone(pref)
        self.assertEqual(pref.telegram_chat_id, "991122")
        self.assertTrue(pref.telegram_enabled)

        with override_settings(TELEGRAM_BOT_TOKEN="test-token", FULLBOX_LK_BASE_URL="https://lk.test"):
            with patch("client_cabinet.chat_telegram.requests.post") as mocked:
                mocked.return_value.status_code = 200
                mocked.return_value.json.return_value = {"ok": True}
                thread = ensure_client_general_thread(self.agency, user=self.client_user)
                post_chat_message(
                    agency=self.agency,
                    user=self.client_user,
                    text="Клиент пишет в TG",
                    thread=thread,
                )
                self.assertTrue(mocked.called)
                body = mocked.call_args.kwargs.get("json") or {}
                self.assertEqual(str(body.get("chat_id")), "991122")
                self.assertIn("Клиент пишет в TG", body.get("text") or "")

        ChatNotificationPreference.objects.filter(user=self.manager_user).update(telegram_enabled=False)
        result = process_telegram_update(
            {"message": {"chat": {"id": 55}, "text": "/start expiredcodeXYZ"}}
        )
        self.assertTrue(result.get("ok"))

    def test_webhook_binds_and_secret(self):
        from unittest.mock import patch

        from django.test import override_settings

        from client_cabinet.chat_telegram import issue_link_code

        code = issue_link_code(self.manager_user)
        http = Client()
        with override_settings(
            TELEGRAM_BOT_TOKEN="tok",
            TELEGRAM_WEBHOOK_SECRET="sec",
            TELEGRAM_BOT_USERNAME="fullbox_bot",
        ):
            denied = http.post(
                "/client/api/v1/chat/telegram/webhook/",
                data=json.dumps({"message": {"chat": {"id": 777}, "text": f"/start {code}"}}),
                content_type="application/json",
            )
            self.assertEqual(denied.status_code, 403)

            with patch("client_cabinet.chat_telegram.requests.post") as mocked:
                mocked.return_value.status_code = 200
                mocked.return_value.json.return_value = {"ok": True}
                ok = http.post(
                    "/client/api/v1/chat/telegram/webhook/",
                    data=json.dumps({"message": {"chat": {"id": 777}, "text": f"/start {code}"}}),
                    content_type="application/json",
                    HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN="sec",
                )
            self.assertEqual(ok.status_code, 200)
            self.assertTrue(ok.json().get("bound"))

        from client_cabinet.models import ChatNotificationPreference

        pref = ChatNotificationPreference.objects.get(user=self.manager_user)
        self.assertEqual(pref.telegram_chat_id, "777")
        self.assertTrue(pref.telegram_enabled)

    def test_mention_notifies_mentioned_user(self):
        from unittest.mock import patch

        from django.test import override_settings

        from client_cabinet.models import ChatNotificationPreference

        ChatNotificationPreference.objects.update_or_create(
            user=self.other_user,
            defaults={
                "telegram_chat_id": "333",
                "telegram_enabled": True,
                "notify_mentions": True,
                "notify_client_messages": False,
            },
        )
        ChatNotificationPreference.objects.update_or_create(
            user=self.manager_user,
            defaults={"telegram_chat_id": "111", "telegram_enabled": True},
        )
        thread = ensure_client_general_thread(self.agency, user=self.manager_user)
        with override_settings(TELEGRAM_BOT_TOKEN="tok", FULLBOX_LK_BASE_URL="https://lk.test"):
            with patch("client_cabinet.chat_telegram.requests.post") as mocked:
                mocked.return_value.status_code = 200
                mocked.return_value.json.return_value = {"ok": True}
                post_chat_message(
                    agency=self.agency,
                    user=self.manager_user,
                    text="Смотри @TG сюда",
                    thread=thread,
                    author_role=ClientChatMessage.ROLE_STAFF,
                )
                chat_ids = {
                    str((c.kwargs.get("json") or {}).get("chat_id"))
                    for c in mocked.call_args_list
                }
                self.assertIn("333", chat_ids)

    def test_telegram_settings_page(self):
        http = Client()
        http.force_login(self.manager_user)
        page = http.get("/team-manager/chats/telegram/")
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "Telegram чатов")
        resp = http.post(
            "/team-manager/chats/telegram/",
            data={
                "action": "save_manual",
                "telegram_chat_id": "4242",
                "telegram_enabled": "1",
                "notify_client_messages": "1",
                "notify_mentions": "1",
            },
        )
        self.assertEqual(resp.status_code, 302)
        from client_cabinet.models import ChatNotificationPreference

        pref = ChatNotificationPreference.objects.get(user=self.manager_user)
        self.assertEqual(pref.telegram_chat_id, "4242")
        self.assertTrue(pref.telegram_enabled)


class DirectorTelegramSettingsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.director = User.objects.create_user(username="dir_tg", password="pwd")
        cls.manager = User.objects.create_user(username="mgr_no_dir", password="pwd")
        Employee.objects.create(
            full_name="Директор ТГ", user=cls.director, role="director", is_active=True
        )
        Employee.objects.create(
            full_name="Менеджер без доступа", user=cls.manager, role="manager", is_active=True
        )

    def test_director_can_save_bot_config_for_later(self):
        from client_cabinet.models import ChatTelegramBotConfig

        http = Client()
        http.force_login(self.director)
        page = http.get("/cabinet/director/integrations/telegram/")
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "Telegram")
        self.assertContains(page, "можно позже")

        resp = http.post(
            "/cabinet/director/integrations/telegram/",
            data={
                "action": "save",
                "bot_token": "123456:ABC-DEF",
                "bot_username": "fullbox_test_bot",
                "webhook_secret": "sec-dir-1",
                "alert_chat_id": "",
                "lk_base_url": "https://lk.fullbox.ru",
                # is_enabled intentionally off — заполнить сейчас, включить потом
            },
        )
        self.assertEqual(resp.status_code, 302)
        cfg = ChatTelegramBotConfig.load()
        self.assertEqual(cfg.bot_token, "123456:ABC-DEF")
        self.assertEqual(cfg.bot_username, "fullbox_test_bot")
        self.assertFalse(cfg.is_enabled)

        from client_cabinet.chat_telegram import resolved_bot_token, telegram_configured

        self.assertEqual(resolved_bot_token(), "")
        self.assertFalse(telegram_configured())

        cfg.is_enabled = True
        cfg.save(update_fields=["is_enabled", "updated_at"])
        self.assertEqual(resolved_bot_token(), "123456:ABC-DEF")
        self.assertTrue(telegram_configured())

    def test_manager_denied(self):
        http = Client()
        http.force_login(self.manager)
        denied = http.get("/cabinet/director/integrations/telegram/")
        self.assertIn(denied.status_code, {302, 403})


class ChatAIFoundationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.client_user = User.objects.create_user(username="ai_client", password="pwd")
        cls.manager_user = User.objects.create_user(username="ai_mgr", password="pwd")
        cls.wh_user = User.objects.create_user(username="ai_wh", password="pwd")
        Employee.objects.create(
            full_name="AI Менеджер", user=cls.manager_user, role="manager", is_active=True
        )
        Employee.objects.create(
            full_name="AI Кладовщик", user=cls.wh_user, role="storekeeper", is_active=True
        )
        cls.agency = Agency.objects.create(
            agn_name="AI Agency",
            short_name="AIAg",
            portal_user=cls.client_user,
            mened_user_id=cls.manager_user.id,
        )

    def test_suggest_stub_and_feedback(self):
        from client_cabinet.models import ChatAISuggestion

        thread = ensure_client_general_thread(self.agency, user=self.client_user)
        post_chat_message(
            agency=self.agency,
            user=self.client_user,
            text="Где посмотреть остатки на складе?",
            thread=thread,
        )
        http = Client()
        http.force_login(self.manager_user)
        page = http.get(f"/team-manager/chats/?thread={thread.id}")
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "Предложить ответ")

        resp = http.post(f"/team-manager/api/chats/{thread.id}/ai/suggest/")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["ok"])
        data = body["data"]
        self.assertFalse(data["auto_reply_allowed"])
        self.assertTrue(data["text"])
        self.assertEqual(data["category"], "остатки")
        sid = data["id"]

        fb = http.post(
            f"/team-manager/api/chats/ai/suggestions/{sid}/feedback/",
            data=json.dumps(
                {
                    "action": "insert",
                    "final_text": data["text"],
                    "feedback": "useful",
                    "enqueue_knowledge": True,
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(fb.status_code, 200)
        self.assertTrue(fb.json()["ok"])
        suggestion = ChatAISuggestion.objects.get(pk=sid)
        self.assertIn(suggestion.status, {"inserted", "edited"})
        self.assertEqual(suggestion.manager_feedback, "useful")
        self.assertTrue(suggestion.knowledge_candidates.exists())

    def test_warehouse_cannot_suggest(self):
        thread = ensure_client_general_thread(self.agency, user=self.client_user)
        http = Client()
        http.force_login(self.wh_user)
        denied = http.post(f"/team-manager/api/chats/{thread.id}/ai/suggest/")
        self.assertIn(denied.status_code, {302, 403})
