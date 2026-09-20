from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase

from client_cabinet.models import (
    ClientNotification,
    OtherRequest,
    OtherRequestAttachment,
    OtherRequestComment,
)
from client_cabinet.other_request_workflow import (
    OtherRequestError,
    accept_by_warehouse,
    approve_and_close,
    approve_and_send_to_department,
    cancel_request,
    client_accept_result,
    client_return_to_work,
    complete_by_executor,
    seed_default_categories,
    take_in_progress,
    update_meta,
    upsert_from_create,
    validate_result_requirements,
)
from client_cabinet.other_requests import create_other_request
from employees.models import Employee
from sku.models import Agency


class OtherRequestWorkflowTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.client_user = User.objects.create_user(username="oth_client", password="pwd")
        self.manager_user = User.objects.create_user(username="oth_mgr", password="pwd")
        self.store_user = User.objects.create_user(username="oth_sk", password="pwd")
        self.agency = Agency.objects.create(agn_name="OTH Client", portal_user=self.client_user)
        self.manager = Employee.objects.create(
            full_name="Менеджер OTH", user=self.manager_user, role="manager", is_active=True
        )
        self.storekeeper = Employee.objects.create(
            full_name="Кладовщик OTH", user=self.store_user, role="storekeeper", is_active=True
        )
        self.agency.mened_user_id = self.manager_user.id
        self.agency.save(update_fields=["mened_user_id"])
        seed_default_categories()

    def test_create_upserts_domain_row(self):
        created = create_other_request(
            agency=self.agency,
            user=self.client_user,
            category="measure_size",
            description="Нужен замер образца для маркировки",
        )
        obj = OtherRequest.objects.get(public_number=created["order_id"])
        self.assertEqual(obj.status, OtherRequest.STATUS_AWAITING_MANAGER)
        self.assertEqual(obj.category_code, "measure_size")
        self.assertIn("замер", obj.display_title.lower())

    def test_happy_path_manager_executor_close(self):
        created = create_other_request(
            agency=self.agency,
            user=self.client_user,
            category="photo_video",
            description="Сфотографировать товар на палете",
        )
        obj = OtherRequest.objects.get(public_number=created["order_id"])
        obj = approve_and_send_to_department(obj, user=self.manager_user)
        self.assertEqual(obj.status, OtherRequest.STATUS_AWAITING_DEPARTMENT)
        obj = accept_by_warehouse(obj, user=self.store_user)
        self.assertEqual(obj.status, OtherRequest.STATUS_WAREHOUSE_ACCEPTED)
        obj = take_in_progress(obj, user=self.store_user)
        self.assertEqual(obj.status, OtherRequest.STATUS_IN_PROGRESS)
        self.assertEqual(obj.assignee_id, self.storekeeper.id)
        with self.assertRaises(OtherRequestError):
            approve_and_close(obj, user=self.manager_user)
        obj = complete_by_executor(obj, user=self.store_user, result_comment="Готово")
        self.assertEqual(obj.status, OtherRequest.STATUS_AWAITING_MANAGER_CHECK)
        obj = approve_and_close(obj, user=self.manager_user)
        self.assertEqual(obj.status, OtherRequest.STATUS_CLOSED)
        obj.refresh_from_db()
        self.assertTrue(obj.billing_status)
        from billing.models import BillingApplication

        app = BillingApplication.objects.filter(
            application_type=BillingApplication.TYPE_OTHER,
            application_id=obj.public_number,
            client=self.agency,
        ).first()
        self.assertIsNotNone(app)
        self.assertTrue(app.is_operations_completed)

    def test_client_visible_comment_creates_notification(self):
        created = create_other_request(
            agency=self.agency,
            user=self.client_user,
            category="custom",
            description="Нужна помощь с документами",
        )
        obj = OtherRequest.objects.get(public_number=created["order_id"])
        from teammanager.other_requests_views import _notify_client_comment

        before = ClientNotification.objects.filter(agency=self.agency, notif_type="comment").count()
        _notify_client_comment(obj, "Уточните артикул, пожалуйста")
        self.assertEqual(
            ClientNotification.objects.filter(agency=self.agency, notif_type="comment").count(),
            before + 1,
        )
        OtherRequestComment.objects.create(
            request=obj,
            author=self.manager_user,
            visibility=OtherRequestComment.VISIBILITY_CLIENT,
            text="Уточните артикул, пожалуйста",
        )
        self.assertEqual(obj.comments.filter(visibility="client").count(), 1)

    def test_double_take_same_user_ok(self):
        obj = upsert_from_create(
            public_number="OTH-TEST-001",
            agency=self.agency,
            user=self.client_user,
            category="custom",
            description="Тест",
        )
        obj.status = OtherRequest.STATUS_WAREHOUSE_ACCEPTED
        obj.save(update_fields=["status"])
        first = take_in_progress(obj, user=self.store_user)
        second = take_in_progress(first, user=self.store_user)
        self.assertEqual(first.assignee_id, second.assignee_id)

    def test_manager_cannot_cancel_after_warehouse_accepted(self):
        obj = upsert_from_create(
            public_number="OTH-TEST-CANCEL-ACCEPTED",
            agency=self.agency,
            user=self.client_user,
            category="custom",
            description="Проверить отмену после принятия складом",
        )
        obj = approve_and_send_to_department(obj, user=self.manager_user)
        obj = accept_by_warehouse(obj, user=self.store_user)

        with self.assertRaises(OtherRequestError) as ctx:
            cancel_request(obj, user=self.manager_user, reason="Клиент передумал")

        self.assertEqual(ctx.exception.code, "forbidden")
        obj.refresh_from_db()
        self.assertEqual(obj.status, OtherRequest.STATUS_WAREHOUSE_ACCEPTED)

    def test_manager_cannot_cancel_after_warehouse_started_work(self):
        obj = upsert_from_create(
            public_number="OTH-TEST-CANCEL-INWORK",
            agency=self.agency,
            user=self.client_user,
            category="custom",
            description="Проверить отмену после старта склада",
        )
        obj = approve_and_send_to_department(obj, user=self.manager_user)
        obj = accept_by_warehouse(obj, user=self.store_user)
        obj = take_in_progress(obj, user=self.store_user)

        with self.assertRaises(OtherRequestError) as ctx:
            cancel_request(obj, user=self.manager_user, reason="Клиент передумал")

        self.assertEqual(ctx.exception.code, "forbidden")
        obj.refresh_from_db()
        self.assertEqual(obj.status, OtherRequest.STATUS_IN_PROGRESS)

    def test_complete_blocked_without_required_result_photo(self):
        # Категория "photo" в сидах требует фото результата (require_result_photo=True).
        # create_other_request доступен клиенту только для узкого списка категорий,
        # поэтому категория "photo" заводится напрямую через upsert_from_create.
        obj = upsert_from_create(
            public_number="OTH-TEST-PHOTO",
            agency=self.agency,
            user=self.client_user,
            category="photo",
            description="Сфотографировать поступивший товар",
        )
        obj = approve_and_send_to_department(obj, user=self.manager_user)
        obj = accept_by_warehouse(obj, user=self.store_user)
        obj = take_in_progress(obj, user=self.store_user)

        self.assertEqual(validate_result_requirements(obj), ["фото результата"])
        with self.assertRaises(OtherRequestError) as ctx:
            complete_by_executor(obj, user=self.store_user, result_comment="Готово")
        self.assertEqual(ctx.exception.code, "missing_result")

        OtherRequestAttachment.objects.create(
            order_id=obj.public_number,
            agency=self.agency,
            request=obj,
            purpose=OtherRequestAttachment.PURPOSE_RESULT,
            file=SimpleUploadedFile("result.jpg", b"fake-image-bytes", content_type="image/jpeg"),
        )
        self.assertEqual(validate_result_requirements(obj), [])
        obj = complete_by_executor(obj, user=self.store_user, result_comment="Готово")
        self.assertEqual(obj.status, OtherRequest.STATUS_AWAITING_MANAGER_CHECK)

    def test_complete_blocked_without_required_qty(self):
        # Категория "recount" требует фактическое количество (require_qty=True).
        obj = upsert_from_create(
            public_number="OTH-TEST-RECOUNT",
            agency=self.agency,
            user=self.client_user,
            category="recount",
            description="Пересчитать остаток на паллете",
        )
        obj = approve_and_send_to_department(obj, user=self.manager_user)
        obj = accept_by_warehouse(obj, user=self.store_user)
        obj = take_in_progress(obj, user=self.store_user)

        self.assertEqual(validate_result_requirements(obj), ["фактическое количество"])
        with self.assertRaises(OtherRequestError):
            complete_by_executor(obj, user=self.store_user, result_comment="Пересчитано")

        obj = complete_by_executor(
            obj,
            user=self.store_user,
            result_comment="Пересчитано",
            result_qty=12,
            result_unit="шт",
        )
        self.assertEqual(obj.status, OtherRequest.STATUS_AWAITING_MANAGER_CHECK)
        self.assertEqual(obj.result_qty, 12)

    def test_update_meta_records_history_and_requires_manager_role(self):
        obj = upsert_from_create(
            public_number="OTH-TEST-META",
            agency=self.agency,
            user=self.client_user,
            category="custom",
            description="Тест сводки",
        )
        with self.assertRaises(OtherRequestError):
            update_meta(obj, user=self.store_user, priority=OtherRequest.PRIORITY_HIGH)

        updated = update_meta(
            obj,
            user=self.manager_user,
            assignee=self.storekeeper,
            assignee_provided=True,
            priority=OtherRequest.PRIORITY_URGENT,
        )
        self.assertEqual(updated.assignee_id, self.storekeeper.id)
        self.assertEqual(updated.priority, OtherRequest.PRIORITY_URGENT)

        from audit.models import OrderAuditEntry

        last_entry = (
            OrderAuditEntry.objects.filter(order_type="other", order_id=obj.public_number)
            .order_by("-created_at")
            .first()
        )
        self.assertIn("приоритет", last_entry.description)

    def test_client_accept_completed_request_records_audit(self):
        obj = upsert_from_create(
            public_number="OTH-TEST-CLIENT-CHECK",
            agency=self.agency,
            user=self.client_user,
            category="custom",
            description="Проверка клиентского принятия",
        )
        obj = approve_and_send_to_department(obj, user=self.manager_user)
        obj = accept_by_warehouse(obj, user=self.store_user)
        obj = take_in_progress(obj, user=self.store_user)
        obj = complete_by_executor(obj, user=self.store_user, result_comment="Готово")
        obj = approve_and_close(obj, user=self.manager_user)

        accepted = client_accept_result(obj, user=self.client_user)
        self.assertEqual(accepted.status, OtherRequest.STATUS_CLOSED)

        from audit.models import OrderAuditEntry

        last_entry = (
            OrderAuditEntry.objects.filter(order_type="other", order_id=obj.public_number)
            .order_by("-created_at")
            .first()
        )
        self.assertEqual((last_entry.payload or {}).get("client_result_status"), "accepted")

    def test_client_return_completed_request_to_work(self):
        obj = upsert_from_create(
            public_number="OTH-TEST-CLIENT-RETURN",
            agency=self.agency,
            user=self.client_user,
            category="custom",
            description="Проверка возврата клиентом",
        )
        obj = approve_and_send_to_department(obj, user=self.manager_user)
        obj = accept_by_warehouse(obj, user=self.store_user)
        obj = take_in_progress(obj, user=self.store_user)
        obj = complete_by_executor(obj, user=self.store_user, result_comment="Готово")
        obj = approve_and_close(obj, user=self.manager_user)

        returned = client_return_to_work(obj, user=self.client_user, reason="Нужно переделать фото")
        self.assertEqual(returned.status, OtherRequest.STATUS_REWORK)
        self.assertEqual(returned.rework_reason, "Нужно переделать фото")
