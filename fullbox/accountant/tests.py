from __future__ import annotations

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from billing.models import (
    ApplicationCharge,
    BillingApplication,
    BillingService,
    ClientTariffItem,
    ClientTariffVersion,
    StandardServicePrice,
    TariffCategory,
    TariffUnit,
)
from client_cabinet.models import ChatThread, ClientChatMessage
from employees.models import Employee
from head_manager.models import Carrier, OwnCompany
from sku.models import Agency, SKU

from accountant.models import ClientLifecycle
from accountant.selectors import ensure_lifecycle, is_client_billing_ready, manager_visible_agencies
from accountant.services import (
    activate_client,
    apply_standard_prices_to_tariff,
    create_tariff_from_catalog,
    publish_tariff,
    update_lifecycle_fields,
    validate_activate_client,
)


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class AccountantCabinetTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(username="acc_test", password="pwd")
        self.employee = Employee.objects.create(
            full_name="Бухгалтер Тест",
            user=self.user,
            role="accountant",
            is_active=True,
        )
        self.manager_user = User.objects.create_user(username="mgr_test", password="pwd")
        self.manager = Employee.objects.create(
            full_name="Менеджер Тест",
            user=self.manager_user,
            role="manager",
            is_active=True,
        )
        self.company = OwnCompany.objects.create(
            name="ООО FullBox НДС 5%",
            inn="7700000001",
            tax_mode=OwnCompany.TAX_MODE_VAT,
            vat_rate="5",
            is_active=True,
        )
        self.cat, _ = TariffCategory.objects.get_or_create(code="receiving", defaults={"name": "Приемка", "sort_order": 10})
        self.unit, _ = TariffUnit.objects.get_or_create(code="box", defaults={"name": "короб", "short_name": "короб"})
        self.service = BillingService.objects.create(
            code="acc_recv",
            name="Приемка товара",
            unit="короб",
            category=self.cat,
            default_price=Decimal("10"),
            used_in_billing=True,
            is_active=True,
        )
        self.client_http = Client()

    def _login_accountant(self):
        self.client_http.force_login(self.user)
        session = self.client_http.session
        session["employee_id"] = self.employee.id
        session["employee_role"] = "accountant"
        session.save()

    def _login_manager(self):
        self.client_http.force_login(self.manager_user)
        session = self.client_http.session
        session["employee_id"] = self.manager.id
        session["employee_role"] = "manager"
        session.save()

    def test_resolve_cabinet_url_accountant(self):
        from employees.access import resolve_cabinet_url

        self.assertEqual(resolve_cabinet_url("accountant"), "/accountant/")

    def test_accountant_overview(self):
        self._login_accountant()
        resp = self.client_http.get("/accountant/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Кабинет бухгалтера")
        self.assertContains(resp, "client-lk/assets/fullbox-logo.png")
        self.assertContains(resp, "--accent: #f89000")
        self.assertContains(resp, "Что требует внимания")
        self.assertContains(resp, "Быстрый старт")
        self.assertContains(resp, "Клиенты")  # группа меню

    def test_accountant_charges_registry_reads_billing_data(self):
        agency = Agency.objects.create(agn_name="ООО Начисления", inn="7701234509", archived=False)
        application = BillingApplication.objects.create(
            application_type=BillingApplication.TYPE_RECEIVING,
            application_id="ACC-CHARGE-1",
            client=agency,
            legal_entity=agency,
            billing_status="calculated",
        )
        ApplicationCharge.objects.create(
            application=application,
            client=agency,
            legal_entity=agency,
            service=self.service,
            quantity=Decimal("2"),
            unit="короб",
            tariff=Decimal("10"),
            tariff_price=Decimal("10"),
            amount=Decimal("20"),
            total_amount=Decimal("20"),
            billing_period=timezone.localdate(),
            service_name_snapshot="Приемка тестовая",
            tariff_source_label="Тариф клиента",
        )
        self._login_accountant()
        response = self.client_http.get("/accountant/charges/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Все начисления")
        self.assertContains(response, "ACC-CHARGE-1")
        self.assertContains(response, "Приемка тестовая")

    def test_manager_visible_requires_accountant_activation(self):
        from billing.permissions import filter_agencies_for_user

        active = Agency.objects.create(agn_name="Активный", inn="111", archived=False)
        active.mened_user_id = self.manager_user.id
        active.save(update_fields=["mened_user_id"])
        ensure_lifecycle(active, status=ClientLifecycle.STATUS_ACTIVE)
        draft = Agency.objects.create(agn_name="Черновик", inn="222", archived=False)
        draft.mened_user_id = self.manager_user.id
        draft.save(update_fields=["mened_user_id"])
        ensure_lifecycle(draft, status=ClientLifecycle.STATUS_DRAFT)
        review = Agency.objects.create(agn_name="На проверке", inn="333", archived=False)
        review.mened_user_id = self.manager_user.id
        review.save(update_fields=["mened_user_id"])
        ensure_lifecycle(review, status=ClientLifecycle.STATUS_ACCOUNTANT_REVIEW)
        without_lifecycle = Agency.objects.create(
            agn_name="Без активации",
            inn="444",
            archived=False,
            mened_user_id=self.manager_user.id,
        )
        visible_ids = set(manager_visible_agencies().values_list("id", flat=True))
        self.assertIn(active.id, visible_ids)
        self.assertNotIn(draft.id, visible_ids)
        self.assertNotIn(review.id, visible_ids)
        self.assertNotIn(without_lifecycle.id, visible_ids)
        self.assertTrue(is_client_billing_ready(active))
        self.assertFalse(is_client_billing_ready(draft))
        billing_ids = set(filter_agencies_for_user(Agency.objects.all(), self.manager_user).values_list("id", flat=True))
        self.assertIn(active.id, billing_ids)
        self.assertNotIn(draft.id, billing_ids)
        self.assertNotIn(review.id, billing_ids)
        self.assertNotIn(without_lifecycle.id, billing_ids)

    def test_activate_requires_fields(self):
        agency = Agency.objects.create(agn_name="", inn="", archived=False)
        ensure_lifecycle(agency, status=ClientLifecycle.STATUS_DRAFT)
        errors = validate_activate_client(agency)
        self.assertTrue(errors)
        self.assertIn("Не заполнен префикс клиента для коробов и паллет", errors)
        with self.assertRaises(Exception):
            activate_client(agency, user=self.user)

    def test_activate_rejects_duplicate_prefix_and_direct_status_bypass(self):
        Agency.objects.create(agn_name="Первый", inn="7701234501", pref="DUP", archived=False)
        agency = Agency.objects.create(agn_name="Второй", inn="7701234502", pref="dup", archived=False)
        ensure_lifecycle(agency, status=ClientLifecycle.STATUS_DRAFT)
        errors = validate_activate_client(agency)
        self.assertIn("Префикс клиента DUP уже используется другим клиентом", errors)
        with self.assertRaisesMessage(ValidationError, "Активируйте клиента отдельной кнопкой"):
            update_lifecycle_fields(agency, {"status": ClientLifecycle.STATUS_ACTIVE}, user=self.user)

    def test_create_tariff_from_catalog_and_publish_activate(self):
        agency = Agency.objects.create(
            agn_name="ООО Тест",
            inn="7701234567",
            pref="TEST",
            contract_numb="Д-1",
            archived=False,
        )
        lifecycle = ensure_lifecycle(agency, status=ClientLifecycle.STATUS_DRAFT, user=self.user)
        lifecycle.client_type = ClientLifecycle.TYPE_OOO
        lifecycle.serving_company = self.company
        lifecycle.commercial_offer_number = "КП-1"
        lifecycle.save()
        version = create_tariff_from_catalog(client=agency, user=self.user)
        self.assertEqual(version.status, ClientTariffVersion.STATUS_DRAFT)
        self.assertTrue(version.items.exists())
        for item in version.items.all():
            item.price = Decimal("12")
            item.is_active = True
            item.save()
        published = publish_tariff(version, user=self.user)
        self.assertIn(published.status, {ClientTariffVersion.STATUS_ACTIVE, ClientTariffVersion.STATUS_SCHEDULED})
        activate_client(agency, user=self.user)
        agency.refresh_from_db()
        self.assertEqual(agency.lifecycle.status, ClientLifecycle.STATUS_ACTIVE)
        self.assertIn(agency.id, manager_visible_agencies().values_list("id", flat=True))

    def test_accountant_can_edit_published_tariff_until_services_are_billed(self):
        agency = Agency.objects.create(
            agn_name="ООО Прямая правка тарифа",
            inn="7701234598",
            contract_numb="Д-DIRECT",
            archived=False,
        )
        lifecycle = ensure_lifecycle(agency, status=ClientLifecycle.STATUS_DRAFT, user=self.user)
        lifecycle.client_type = ClientLifecycle.TYPE_OOO
        lifecycle.serving_company = self.company
        lifecycle.commercial_offer_number = "КП-DIRECT"
        lifecycle.save()
        version = create_tariff_from_catalog(client=agency, user=self.user)
        version.items.update(price=Decimal("12"), is_active=True)
        item = version.items.order_by("id").first()
        published = publish_tariff(version, user=self.user)

        self._login_accountant()
        page = self.client_http.get(f"/accountant/tariffs/{published.id}/")
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "можно исправить")
        self.assertContains(page, "Сохранить тариф")

        resp = self.client_http.post(
            f"/accountant/api/client-tariffs/{published.id}/patch/",
            data='{"items":[{"id":%d,"price":"77.77","service_name":"Правка без начислений"}]}'
            % item.id,
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["ok"])
        item.refresh_from_db()
        self.assertEqual(item.price, Decimal("77.77"))
        self.assertEqual(item.service_name, "Правка без начислений")

    def test_accountant_edit_tariff_from_client_card_copies_used_current_version(self):
        agency = Agency.objects.create(
            agn_name="ООО Копия тарифа",
            inn="7701234599",
            contract_numb="Д-COPY",
            archived=False,
        )
        lifecycle = ensure_lifecycle(agency, status=ClientLifecycle.STATUS_DRAFT, user=self.user)
        lifecycle.client_type = ClientLifecycle.TYPE_OOO
        lifecycle.serving_company = self.company
        lifecycle.commercial_offer_number = "КП-COPY"
        lifecycle.save()
        version = create_tariff_from_catalog(client=agency, user=self.user)
        version.items.update(price=Decimal("12"), is_active=True)
        source_item = version.items.order_by("id").first()
        source_item.service_name = "Индивидуальная строка тарифа"
        source_item.price = Decimal("123.45")
        source_item.conditions = "особые условия"
        source_item.save()
        published = publish_tariff(version, user=self.user)
        application = BillingApplication.objects.create(
            application_type=BillingApplication.TYPE_OTHER,
            application_id="ACC-COPY-1",
            client=agency,
            legal_entity=agency,
            billing_status="calculated",
        )
        ApplicationCharge.objects.create(
            application=application,
            client=agency,
            legal_entity=agency,
            service=source_item.service,
            quantity=Decimal("1"),
            unit=source_item.unit.short_name,
            tariff=source_item.price,
            tariff_price=source_item.price,
            amount=source_item.price,
            total_amount=source_item.price,
            billing_period=timezone.localdate(),
            client_tariff_version=published,
            client_tariff_item=source_item,
            service_name_snapshot=source_item.service_name,
        )

        self._login_accountant()
        page = self.client_http.get(f"/accountant/clients/{agency.id}/")
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "Создать новую редакцию тарифа")
        self.assertContains(page, "Тарифы уже используются")
        self.assertContains(page, f"currentTariffId = {published.id}")

        blocked = self.client_http.post(
            f"/accountant/api/client-tariffs/{published.id}/patch/",
            data='{"items":[{"id":%d,"price":"777"}]}' % source_item.id,
            content_type="application/json",
        )
        self.assertEqual(blocked.status_code, 400)
        self.assertIn("уже используются", blocked.json()["error"])

        resp = self.client_http.post(
            f"/accountant/api/client-tariffs/{published.id}/duplicate/",
            data="{}",
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 201)
        body = resp.json()
        self.assertTrue(body["ok"])
        draft = ClientTariffVersion.objects.get(pk=body["data"]["id"])
        self.assertEqual(draft.status, ClientTariffVersion.STATUS_DRAFT)
        self.assertEqual(draft.version_number, published.version_number + 1)
        copied_item = draft.items.get(service_id=source_item.service_id, conditions="особые условия")
        self.assertEqual(copied_item.service_name, "Индивидуальная строка тарифа")
        self.assertEqual(copied_item.price, Decimal("123.45"))

    def test_accountant_can_delete_draft_tariff_version(self):
        agency = Agency.objects.create(agn_name="ООО Черновики", inn="7701234500", archived=False)
        version = create_tariff_from_catalog(client=agency, user=self.user)
        item_ids = list(version.items.values_list("id", flat=True))

        self._login_accountant()
        resp = self.client_http.post(
            f"/accountant/tariffs/{version.id}/delete/",
            {"next": "/accountant/tariffs/?status=draft"},
            follow=True,
        )

        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "удалён")
        self.assertFalse(ClientTariffVersion.objects.filter(pk=version.id).exists())
        self.assertFalse(ClientTariffItem.objects.filter(pk__in=item_ids).exists())

    def test_accountant_cannot_delete_published_tariff_version(self):
        agency = Agency.objects.create(
            agn_name="ООО Нельзя удалить",
            inn="7701234501",
            contract_numb="Д-DEL",
            archived=False,
        )
        lifecycle = ensure_lifecycle(agency, status=ClientLifecycle.STATUS_DRAFT, user=self.user)
        lifecycle.client_type = ClientLifecycle.TYPE_OOO
        lifecycle.serving_company = self.company
        lifecycle.commercial_offer_number = "КП-DEL"
        lifecycle.save()
        version = create_tariff_from_catalog(client=agency, user=self.user)
        version.items.update(price=Decimal("12"), is_active=True)
        published = publish_tariff(version, user=self.user)

        self._login_accountant()
        resp = self.client_http.post(
            f"/accountant/tariffs/{published.id}/delete/",
            {"next": "/accountant/tariffs/"},
            follow=True,
        )

        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Удалять можно только черновик")
        self.assertTrue(ClientTariffVersion.objects.filter(pk=published.id).exists())

    def test_accountant_can_delete_empty_client(self):
        User = get_user_model()
        portal_user = User.objects.create_user(username="empty_client_login", password="pwd")
        agency = Agency.objects.create(
            agn_name="ООО Пустой клиент",
            inn="7701234510",
            archived=False,
            portal_user=portal_user,
        )
        lifecycle = ensure_lifecycle(agency, status=ClientLifecycle.STATUS_DRAFT, user=self.user)
        lifecycle.serving_company = self.company
        lifecycle.save()
        version = create_tariff_from_catalog(client=agency, user=self.user)

        self._login_accountant()
        page = self.client_http.get("/accountant/clients/?q=7701234510")
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "/accountant/clients/%d/delete/" % agency.id)
        self.assertContains(page, "удалить")

        resp = self.client_http.post(
            f"/accountant/clients/{agency.id}/delete/",
            {"next": "/accountant/clients/?q=7701234510"},
            follow=True,
        )

        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Пустой клиент")
        self.assertContains(resp, "удалён")
        self.assertFalse(Agency.objects.filter(pk=agency.id).exists())
        self.assertFalse(ClientTariffVersion.objects.filter(pk=version.id).exists())
        self.assertFalse(User.objects.filter(pk=portal_user.id).exists())

    def test_accountant_cannot_delete_client_with_sku(self):
        agency = Agency.objects.create(agn_name="ООО С Артикулом", inn="7701234511", archived=False)
        ensure_lifecycle(agency, status=ClientLifecycle.STATUS_DRAFT, user=self.user)
        SKU.objects.create(agency=agency, sku_code="ART-1", name="Товар 1")

        self._login_accountant()
        page = self.client_http.get("/accountant/clients/?q=7701234511")
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "нельзя удалить: артикулы / номенклатура: 1")

        resp = self.client_http.post(
            f"/accountant/clients/{agency.id}/delete/",
            {"next": "/accountant/clients/?q=7701234511"},
            follow=True,
        )

        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Нельзя удалить клиента")
        self.assertContains(resp, "артикулы / номенклатура: 1")
        self.assertTrue(Agency.objects.filter(pk=agency.id).exists())

    def test_accountant_cannot_delete_client_with_application(self):
        agency = Agency.objects.create(agn_name="ООО С Заявкой", inn="7701234512", archived=False)
        ensure_lifecycle(agency, status=ClientLifecycle.STATUS_DRAFT, user=self.user)
        BillingApplication.objects.create(
            application_type=BillingApplication.TYPE_OTHER,
            application_id="ACC-DELETE-BLOCK",
            client=agency,
            legal_entity=agency,
            billing_status="draft",
        )

        self._login_accountant()
        resp = self.client_http.post(
            f"/accountant/clients/{agency.id}/delete/",
            {"next": "/accountant/clients/?q=7701234512"},
            follow=True,
        )

        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Нельзя удалить клиента")
        self.assertContains(resp, "заявки биллинга")
        self.assertTrue(Agency.objects.filter(pk=agency.id).exists())

    def test_accountant_can_delete_empty_client_with_chat(self):
        agency = Agency.objects.create(agn_name="ООО Только Чат", inn="7701234513", archived=False)
        ensure_lifecycle(agency, status=ClientLifecycle.STATUS_DRAFT, user=self.user)
        thread = ChatThread.objects.create(
            agency=agency,
            kind=ChatThread.KIND_CLIENT_GENERAL,
            title="Общий чат",
        )
        message = ClientChatMessage.objects.create(
            agency=agency,
            thread=thread,
            author_role=ClientChatMessage.ROLE_CLIENT,
            text="Здравствуйте",
        )

        self._login_accountant()
        page = self.client_http.get("/accountant/clients/?q=7701234513")
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "/accountant/clients/%d/delete/" % agency.id)
        self.assertNotContains(page, "чаты клиента")

        resp = self.client_http.post(
            f"/accountant/clients/{agency.id}/delete/",
            {"next": "/accountant/clients/?q=7701234513"},
            follow=True,
        )

        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Только Чат")
        self.assertContains(resp, "удалён")
        self.assertFalse(Agency.objects.filter(pk=agency.id).exists())
        self.assertFalse(ChatThread.objects.filter(pk=thread.id).exists())
        self.assertFalse(ClientChatMessage.objects.filter(pk=message.id).exists())

    def test_apply_standard_prices_to_draft(self):
        from billing.standard_price_catalog import seed_standard_prices

        seed_standard_prices()
        agency = Agency.objects.create(agn_name="АО СКУБИС", inn="7700001111", archived=False)
        lifecycle = ensure_lifecycle(agency, status=ClientLifecycle.STATUS_DRAFT, user=self.user)
        lifecycle.serving_company = self.company
        lifecycle.commercial_offer_number = "КП-SK"
        lifecycle.save()
        version = create_tariff_from_catalog(client=agency, user=self.user)
        # обнуляем цены как на проде после пустого создания
        version.items.update(price=Decimal("0"))
        result = apply_standard_prices_to_tariff(version, overwrite=True, user=self.user)
        self.assertGreater(result["filled"], 50)
        sample = version.items.filter(service__code="returns_video_recording").first()
        self.assertIsNotNone(sample)
        self.assertEqual(sample.price, Decimal("50"))
        self._login_accountant()
        resp = self.client_http.get(f"/accountant/tariffs/{version.id}/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "АО СКУБИС")
        self.assertContains(resp, "Клиент обслуживания")
        self.assertContains(resp, "Заполнить из прайса FullBox")

    def test_accountant_can_edit_tariff_item_service_and_name(self):
        agency = Agency.objects.create(agn_name="ООО Прайс Редактирование", inn="7700002222", archived=False)
        version = ClientTariffVersion.objects.create(
            client=agency,
            name="Черновик услуг",
            status=ClientTariffVersion.STATUS_DRAFT,
            valid_from=timezone.localdate(),
        )
        other_cat, _ = TariffCategory.objects.get_or_create(
            code="fulfillment",
            defaults={"name": "Фулфилмент", "sort_order": 20},
        )
        other_service = BillingService.objects.create(
            code="acc_pack",
            name="Упаковка товара",
            unit="палета",
            category=other_cat,
            default_price=Decimal("25"),
            used_in_billing=True,
            is_active=True,
        )
        item = ClientTariffItem.objects.create(
            tariff_version=version,
            category=self.cat,
            service=self.service,
            service_name="Старое название в прайсе",
            unit=self.unit,
            price=Decimal("10"),
            is_active=True,
        )

        self._login_accountant()
        page = self.client_http.get(f"/accountant/tariffs/{version.id}/")
        self.assertEqual(page.status_code, 200)
        self.assertNotContains(page, "Услуга из справочника")
        self.assertNotContains(page, "<th>Убрать</th>")
        self.assertNotContains(page, '<select class="field js-service"')
        self.assertNotContains(page, "querySelector('.js-service')")
        self.assertContains(page, "Наименование в прайсе")
        self.assertContains(page, "js-service-name")
        self.assertContains(page, "js-remove-item")
        self.assertContains(page, "min-width:420px")
        self.assertContains(page, "Добавить услугу клиенту")
        self.assertContains(page, "js-add-custom-item")

        resp = self.client_http.post(
            f"/accountant/api/client-tariffs/{version.id}/patch/",
            data=(
                '{"items":[{"id":%d,"service_id":%d,"service_name":"Индивидуальная упаковка",'
                '"price":"42","minimum_amount":"100","is_active":true,"comment":"по договору"}]}'
            )
            % (item.id, other_service.id),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["ok"])
        item.refresh_from_db()
        self.assertEqual(item.service_id, other_service.id)
        self.assertEqual(item.category_id, other_cat.id)
        self.assertEqual(item.unit.short_name, "палета")
        self.assertEqual(item.service_name, "Индивидуальная упаковка")
        self.assertEqual(item.price, Decimal("42"))
        self.assertEqual(item.minimum_amount, Decimal("100"))
        self.assertEqual(item.conditions, "по договору")

    def test_accountant_can_remove_tariff_item_from_editable_tariff(self):
        agency = Agency.objects.create(agn_name="ООО Удаление строки тарифа", inn="7700002233", archived=False)
        version = ClientTariffVersion.objects.create(
            client=agency,
            name="Черновик с лишней услугой",
            status=ClientTariffVersion.STATUS_DRAFT,
            valid_from=timezone.localdate(),
        )
        keep_item = ClientTariffItem.objects.create(
            tariff_version=version,
            category=self.cat,
            service=self.service,
            service_name="Оставить услугу",
            unit=self.unit,
            price=Decimal("10"),
            is_active=True,
        )
        remove_service = BillingService.objects.create(
            code="acc_remove",
            name="Лишняя услуга",
            unit="шт",
            category=self.cat,
            default_price=Decimal("5"),
            used_in_billing=True,
            is_active=True,
        )
        remove_item = ClientTariffItem.objects.create(
            tariff_version=version,
            category=self.cat,
            service=remove_service,
            service_name="Убрать услугу",
            unit=self.unit,
            price=Decimal("5"),
            is_active=True,
        )

        self._login_accountant()
        resp = self.client_http.post(
            f"/accountant/api/client-tariffs/{version.id}/patch/",
            data='{"items":[{"id":%d,"delete":true},{"id":%d,"price":"11"}]}'
            % (remove_item.id, keep_item.id),
            content_type="application/json",
        )

        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["ok"])
        self.assertFalse(ClientTariffItem.objects.filter(pk=remove_item.id).exists())
        keep_item.refresh_from_db()
        self.assertEqual(keep_item.price, Decimal("11"))

    def test_accountant_can_add_custom_tariff_item_not_in_standard_price_list(self):
        agency = Agency.objects.create(agn_name="ООО Индивидуальная услуга", inn="7700002244", archived=False)
        version = ClientTariffVersion.objects.create(
            client=agency,
            name="Черновик индивидуальных услуг",
            status=ClientTariffVersion.STATUS_DRAFT,
            valid_from=timezone.localdate(),
        )

        self._login_accountant()
        resp = self.client_http.post(
            f"/accountant/api/client-tariffs/{version.id}/patch/",
            data=(
                '{"new_items":[{"service_name":"Индивидуальная маркировка клиента",'
                '"category_id":%d,"unit":"усл","price":"123.45",'
                '"minimum_amount":"500","comment":"только этому клиенту"}]}'
            )
            % self.cat.id,
            content_type="application/json",
        )

        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["ok"])
        item = ClientTariffItem.objects.select_related("service", "unit", "category").get(
            tariff_version=version,
            service_name="Индивидуальная маркировка клиента",
        )
        self.assertEqual(item.category_id, self.cat.id)
        self.assertEqual(item.unit.short_name, "усл")
        self.assertEqual(item.price, Decimal("123.45"))
        self.assertEqual(item.minimum_amount, Decimal("500"))
        self.assertEqual(item.conditions, "только этому клиенту")
        self.assertTrue(item.service.code.startswith("client_"))
        self.assertEqual(item.service.name, "Индивидуальная маркировка клиента")
        self.assertFalse(StandardServicePrice.objects.filter(service=item.service).exists())

    def test_manager_cannot_open_accountant(self):
        self._login_manager()
        resp = self.client_http.get("/accountant/")
        self.assertEqual(resp.status_code, 403)

    def test_manager_with_accountant_access_can_open_accountant(self):
        self.manager.access_roles = ["accountant"]
        self.manager.save(update_fields=["access_roles"])
        self._login_manager()

        resp = self.client_http.get("/accountant/")

        self.assertEqual(resp.status_code, 200)

    def test_legacy_urls_still_work(self):
        self._login_accountant()
        for url in (
            "/accountant/clients/",
            "/accountant/tariffs/",
            "/accountant/contracts/",
            "/accountant/history/",
            "/accountant/tariff-check/",
            "/accountant/storage/",
            "/accountant/own-companies/",
        ):
            resp = self.client_http.get(url)
            self.assertEqual(resp.status_code, 200, url)

    def test_menu_groups_and_stub_sections(self):
        self._login_accountant()
        resp = self.client_http.get("/accountant/")
        self.assertContains(resp, "Начисления")
        self.assertContains(resp, "Поставщики")
        self.assertContains(resp, "Платёжный календарь")
        self.assertContains(resp, "Перевозчики")
        stub = self.client_http.get("/accountant/suppliers/")
        self.assertEqual(stub.status_code, 200)
        self.assertContains(stub, "Раздел готовится")
        bridge = self.client_http.get("/accountant/documents/invoices/")
        self.assertEqual(bridge.status_code, 200)
        self.assertContains(bridge, "/team-manager/billing/invoices/")
        alias = self.client_http.get("/accountant/legal-entities/")
        self.assertEqual(alias.status_code, 302)
        self.assertEqual(alias.url, "/accountant/own-companies/")

    def test_carriers_list_create_and_edit(self):
        self._login_accountant()
        list_resp = self.client_http.get("/accountant/carriers/")
        self.assertEqual(list_resp.status_code, 200)
        self.assertContains(list_resp, "Перевозчики")
        self.assertContains(list_resp, "Добавить перевозчика")

        create_page = self.client_http.get("/accountant/carriers/new/")
        self.assertEqual(create_page.status_code, 200)
        create = self.client_http.post(
            "/accountant/carriers/new/",
            {
                "name": "ООО Транс Логистик",
                "inn": "7701234567",
                "phone": "+7 900 111-22-33",
                "contact_person": "Иванов",
                "is_active": "on",
            },
        )
        self.assertEqual(create.status_code, 302, create.content)
        self.assertEqual(create.url, "/accountant/carriers/")
        carrier = Carrier.objects.get(inn="7701234567")
        self.assertTrue(carrier.is_active)
        self.assertEqual(carrier.name, "ООО Транс Логистик")

        edit = self.client_http.post(
            f"/accountant/carriers/{carrier.id}/",
            {
                "name": "ООО Транс Логистик",
                "inn": "7701234567",
                "phone": "+7 900 111-22-33",
                "contact_person": "Петров",
                "is_active": "on",
            },
        )
        self.assertEqual(edit.status_code, 302)
        carrier.refresh_from_db()
        self.assertEqual(carrier.contact_person, "Петров")
        again = self.client_http.get("/accountant/carriers/")
        self.assertContains(again, "Транс Логистик")

    def test_services_catalog_and_no_price(self):
        self._login_accountant()
        resp = self.client_http.get("/accountant/tariffs/catalog/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Справочник услуг")
        self.assertContains(resp, self.service.code)
        resp2 = self.client_http.get("/accountant/tariffs/no-price/")
        self.assertEqual(resp2.status_code, 200)

    def test_accountant_can_delete_unlinked_own_company(self):
        self._login_accountant()
        company = OwnCompany.objects.create(
            name="ООО FullBox Удаляемая",
            inn="7700000999",
            tax_mode=OwnCompany.TAX_MODE_NO_VAT,
            vat_rate="0",
            is_active=True,
        )
        resp = self.client_http.post(f"/accountant/own-companies/{company.id}/delete/", follow=True)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "удалена")
        self.assertFalse(OwnCompany.objects.filter(pk=company.id).exists())

    def test_accountant_cannot_delete_linked_own_company(self):
        self._login_accountant()
        agency = Agency.objects.create(agn_name="ООО Связанный клиент", inn="7700000222", archived=False)
        lifecycle = ensure_lifecycle(agency, status=ClientLifecycle.STATUS_DRAFT, user=self.user)
        lifecycle.serving_company = self.company
        lifecycle.save()
        resp = self.client_http.post(f"/accountant/own-companies/{self.company.id}/delete/", follow=True)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Нельзя удалить компанию")
        self.assertContains(resp, "Жизненные циклы клиентов")
        self.assertTrue(OwnCompany.objects.filter(pk=self.company.id).exists())

    def test_unknown_section_404(self):
        self._login_accountant()
        resp = self.client_http.get("/accountant/this-section-does-not-exist/")
        self.assertEqual(resp.status_code, 404)

    def test_head_manager_cannot_open_suppliers(self):
        User = get_user_model()
        hm_user = User.objects.create_user(username="hm_acc", password="pwd")
        hm = Employee.objects.create(full_name="HM", user=hm_user, role="head_manager", is_active=True)
        self.client_http.force_login(hm_user)
        session = self.client_http.session
        session["employee_id"] = hm.id
        session["employee_role"] = "head_manager"
        session.save()
        resp = self.client_http.get("/accountant/suppliers/")
        self.assertEqual(resp.status_code, 403)
        ok = self.client_http.get("/accountant/clients/")
        self.assertEqual(ok.status_code, 200)

    def test_api_create_client(self):
        self._login_accountant()
        resp = self.client_http.post(
            "/accountant/api/clients/create/",
            data='{"legal_name":"ООО API","pref":"api","inn":"7709998877","client_type":"ooo","serving_company_id":%d,"contract_number":"Д-API"}'
            % self.company.id,
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 201)
        body = resp.json()
        self.assertTrue(body["ok"])
        agency = Agency.objects.get(pk=body["data"]["id"])
        self.assertEqual(agency.pref, "API")
        self.assertEqual(body["data"]["pref"], "API")
        self.assertEqual(agency.lifecycle.status, ClientLifecycle.STATUS_DRAFT)
        detail = self.client_http.get(f"/accountant/clients/{agency.id}/")
        self.assertContains(detail, "Префикс клиента")
        self.assertContains(detail, 'data-k="pref"')

    def test_api_create_client_requires_prefix(self):
        self._login_accountant()
        page = self.client_http.get("/accountant/clients/")
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "Префикс клиента *")
        self.assertContains(page, 'id="c-pref" maxlength="32" autocomplete="off" placeholder="Например, TDT" required aria-required="true"')
        self.assertContains(page, "Укажите префикс клиента.")

        resp = self.client_http.post(
            "/accountant/api/clients/create/",
            data='{"legal_name":"ООО Без префикса","inn":"7709998899","client_type":"ooo"}',
            content_type="application/json",
        )

        self.assertEqual(resp.status_code, 400)
        self.assertFalse(resp.json()["ok"])
        self.assertIn("префикс клиента", resp.json()["error"].lower())
        self.assertFalse(Agency.objects.filter(inn="7709998899").exists())

    def test_accountant_can_create_joint_stock_company_client(self):
        self._login_accountant()
        page = self.client_http.get("/accountant/clients/")
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, 'value="ao"')
        self.assertContains(page, "Акционерное общество")

        resp = self.client_http.post(
            "/accountant/api/clients/create/",
            data='{"legal_name":"АО API","pref":"AOAPI","inn":"7709998800","client_type":"ao","serving_company_id":%d}'
            % self.company.id,
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 201)
        body = resp.json()
        self.assertTrue(body["ok"])
        agency = Agency.objects.get(pk=body["data"]["id"])
        self.assertEqual(agency.lifecycle.client_type, ClientLifecycle.TYPE_AO)
        self.assertEqual(body["data"]["client_type_label"], "Акционерное общество")

    def test_accountant_can_create_client_portal_access(self):
        User = get_user_model()
        User.objects.create_user(username="client1", password="pwd")
        self._login_accountant()
        page = self.client_http.get("/accountant/clients/")
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "Доступ ЛК")
        self.assertContains(page, "Логин ЛК клиента")
        self.assertContains(page, "Пароль ЛК клиента")
        self.assertContains(page, "создастся автоматически: clientN")
        self.assertContains(page, "Сгенерировать пароль")
        self.assertContains(page, "generatedPassword")

        resp = self.client_http.post(
            "/accountant/api/clients/create/",
            data=(
                '{"legal_name":"ООО ЛК Бухгалтер","pref":"ACCPO","inn":"7709998811","client_type":"ooo",'
                '"portal_login":"acc_portal_client","portal_password":"new-pass-123"}'
            ),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 201)
        body = resp.json()
        self.assertTrue(body["ok"])
        self.assertRegex(body["data"]["portal_login"], r"^client\d+$")
        self.assertNotEqual(body["data"]["portal_login"], "acc_portal_client")
        self.assertTrue(body["data"]["portal_has_user"])
        agency = Agency.objects.select_related("portal_user").get(pk=body["data"]["id"])
        self.assertEqual(agency.portal_user.username, body["data"]["portal_login"])
        self.assertTrue(agency.portal_user.check_password("new-pass-123"))

    def test_accountant_can_switch_client_vat_type(self):
        agency = Agency.objects.create(
            agn_name="ООО НДС Клиента",
            inn="7709998844",
            archived=False,
            use_nds=False,
        )
        lifecycle = ensure_lifecycle(agency, status=ClientLifecycle.STATUS_DRAFT, user=self.user)
        lifecycle.client_type = ClientLifecycle.TYPE_OOO
        lifecycle.serving_company = self.company
        lifecycle.save()
        version = ClientTariffVersion.objects.create(
            client=agency,
            name="Черновик НДС",
            status=ClientTariffVersion.STATUS_DRAFT,
            valid_from=timezone.localdate(),
            vat_type=ClientTariffVersion.VAT_NO,
        )

        self._login_accountant()
        page = self.client_http.get("/accountant/clients/")
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "js-client-vat")
        self.assertContains(page, "НДС клиента")
        detail = self.client_http.get(f"/accountant/clients/{agency.id}/")
        self.assertEqual(detail.status_code, 200)
        self.assertContains(detail, "НДС 5% сверху")

        resp = self.client_http.post(
            f"/accountant/api/clients/{agency.id}/patch/",
            data='{"vat_type":"vat_extra"}',
            content_type="application/json",
        )

        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["data"]["vat_type"], "vat_extra")
        self.assertIn("НДС сверху", body["data"]["vat_type_label"])
        agency.refresh_from_db()
        lifecycle.refresh_from_db()
        version.refresh_from_db()
        self.assertTrue(agency.use_nds)
        self.assertEqual(lifecycle.vat_type, ClientLifecycle.VAT_EXTRA)
        self.assertEqual(version.vat_type, ClientTariffVersion.VAT_EXTRA)

    def test_accountant_can_edit_client_portal_login_and_password(self):
        User = get_user_model()
        portal_user = User.objects.create_user(username="old_client_login", password="old-pass", email="old@example.com")
        agency = Agency.objects.create(
            agn_name="ООО Доступ ЛК",
            inn="7709998822",
            email="client@example.com",
            portal_user=portal_user,
        )
        ensure_lifecycle(agency, status=ClientLifecycle.STATUS_DRAFT, user=self.user)

        self._login_accountant()
        page = self.client_http.get(f"/accountant/clients/{agency.id}/")
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "Доступ в ЛК клиента")
        self.assertContains(page, "old_client_login")
        self.assertContains(page, "Новый пароль")
        self.assertContains(page, "Сгенерировать пароль")
        self.assertContains(page, "portal-password-hint")

        resp = self.client_http.post(
            f"/accountant/api/clients/{agency.id}/patch/",
            data='{"portal_login":"new_client_login","portal_password":"fresh-pass-456"}',
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["data"]["portal_login"], "new_client_login")
        portal_user.refresh_from_db()
        self.assertEqual(portal_user.username, "new_client_login")
        self.assertTrue(portal_user.check_password("fresh-pass-456"))

    def test_accountant_portal_access_validation_rolls_back_client_create(self):
        User = get_user_model()
        User.objects.create_user(username="busy_client_login", password="pwd")
        self._login_accountant()

        resp = self.client_http.post(
            "/accountant/api/clients/create/",
            data=(
                '{"legal_name":"ООО Не должен создаться","pref":"ROLLBACK","inn":"7709998833","client_type":"ooo",'
                '"portal_login":"busy_client_login"}'
            ),
            content_type="application/json",
        )

        self.assertEqual(resp.status_code, 400)
        self.assertIn("укажите пароль", resp.json()["error"].lower())
        self.assertFalse(Agency.objects.filter(inn="7709998833").exists())

    def test_api_create_client_rejects_duplicate_inn(self):
        Agency.objects.create(agn_name="ООО Уже есть", inn="7709998877")
        self._login_accountant()
        resp = self.client_http.post(
            "/accountant/api/clients/create/",
            data='{"legal_name":"ООО Дубль","inn":"7709998877","client_type":"ooo"}',
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 400)
        body = resp.json()
        self.assertFalse(body["ok"])
        self.assertIn("уже есть в системе", body["error"])
        self.assertEqual(Agency.objects.filter(inn="7709998877").count(), 1)

    def test_api_client_patch_rejects_duplicate_inn(self):
        target = Agency.objects.create(agn_name="ООО Редактируемый", inn="7701111111")
        Agency.objects.create(agn_name="ООО Существующий", inn="7702222222")
        self._login_accountant()
        resp = self.client_http.post(
            f"/accountant/api/clients/{target.id}/patch/",
            data='{"inn":"7702222222"}',
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 400)
        body = resp.json()
        self.assertFalse(body["ok"])
        self.assertIn("уже есть в системе", body["error"])
        target.refresh_from_db()
        self.assertEqual(target.inn, "7701111111")
