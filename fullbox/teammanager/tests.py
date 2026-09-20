from datetime import timedelta
from decimal import Decimal
import tempfile

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from audit.models import OrderAuditEntry
from employees.models import Employee
from shipping.models import ShippingOrder, ShippingOrderItem
from sklad.models import WarehouseReserve

try:
    from shipping.models import ShippingDestination
except ImportError:  # pragma: no cover - модель может отсутствовать до merge-миграций
    ShippingDestination = None
from sklad.test_utils import create_warehouse_snapshot_row
from sku.models import Agency, Market, SKU, SKUBarcode
from todo.models import Task


class TeamManagerClientsTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="mgr_clients", password="pwd")
        Employee.objects.create(
            full_name="Менеджер Клиенты",
            user=self.user,
            role="manager",
            is_active=True,
        )
        self.agency = Agency.objects.create(
            agn_name="Клиент для менеджера",
            inn="7700000000",
            pref="KLM",
            mened_user_id=self.user.id,
        )
        from accountant.selectors import ensure_lifecycle
        from accountant.models import ClientLifecycle

        ensure_lifecycle(self.agency, status=ClientLifecycle.STATUS_ACTIVE, user=self.user)
        self.ozon = Market.objects.create(id=9701, name="OZON")
        self.client.force_login(self.user)

    def test_manager_dashboard_shell_matches_template(self):
        dash = self.client.get("/team-manager/")
        self.assertEqual(dash.status_code, 200)
        self.assertContains(dash, "Задачи")
        self.assertContains(dash, "Рабочая очередь и контроль заявок")
        self.assertContains(dash, "Все статусы")
        self.assertContains(dash, "На сегодня")
        self.assertContains(dash, "Зависло")
        self.assertContains(dash, "Без ответственного")
        self.assertContains(dash, "Документы")
        self.assertContains(dash, "SLA")
        self.assertContains(dash, "Следующее действие")
        self.assertContains(dash, "Новые события")
        self.assertContains(dash, "Уведомления")
        self.assertContains(dash, "Быстрые действия")
        self.assertContains(dash, 'href="/team-manager/clients/"')
        self.assertContains(dash, 'href="/team-manager/orders/"')
        self.assertContains(dash, 'href="/shipping/new/"')
        self.assertContains(dash, 'href="/reachtruck/"')
        self.assertContains(dash, "Перемещение паллет")

    def test_manager_reports_catalog_routes_and_search(self):
        page = self.client.get("/team-manager/reports/")

        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "Отчёты и аналитика")
        self.assertContains(page, "Выберите направление, затем конкретный отчёт")
        self.assertContains(page, "Отчёты по клиентам")
        self.assertContains(page, "Отчёты по товарам")
        self.assertContains(page, "Отчёты по номенклатуре")
        self.assertContains(page, "Складские отчёты")
        self.assertContains(page, "Операционные отчёты")
        self.assertContains(page, "Финансовые отчёты")
        self.assertContains(page, 'href="/team-manager/reports/warehouse/"')
        self.assertContains(page, 'href="/team-manager/reports/history/"')
        self.assertContains(page, 'href="/team-manager/reports/saved/"')
        self.assertNotContains(page, "Всего заявок")
        self.assertNotContains(page, '<div class="nav-title">Остатки</div>')

        self.assertContains(page, "Главные рабочие отчёты")
        self.assertContains(page, "Все отчёты ниже формируются по актуальным данным")
        self.assertNotContains(page, 'name="q"')

    def test_manager_reports_section_and_detail_pages(self):
        section = self.client.get("/team-manager/reports/warehouse/")
        self.assertEqual(section.status_code, 200)
        self.assertContains(section, "Складские отчёты")
        self.assertContains(section, "Остатки на текущую дату")
        self.assertContains(section, "Движение товаров")
        self.assertContains(section, 'href="/team-manager/inventory/"')
        self.assertNotContains(section, 'href="/team-manager/reports/warehouse/current-stock/"')

        detail = self.client.get(
            "/team-manager/reports/warehouse/current-stock/",
            {"client_id": self.agency.id, "only_available": "1", "page_size": "50"},
        )
        self.assertEqual(detail.status_code, 200)
        self.assertContains(detail, "Остатки на текущую дату")
        self.assertContains(detail, "Только доступный остаток")
        self.assertContains(detail, "Сформировать")
        self.assertContains(detail, "Экспорт XLSX")
        self.assertContains(detail, "Доступно")
        self.assertNotContains(detail, "Каркас отчёта готов")
        self.assertContains(detail, "По выбранным условиям данных нет")

    def test_manager_product_reports_show_required_catalog(self):
        section = self.client.get("/team-manager/reports/products/")

        self.assertEqual(section.status_code, 200)
        self.assertContains(section, "Отчёты по товарам")
        self.assertContains(section, "Найдено: 15 из 15")
        for title in (
            "Остатки товаров",
            "Движение товара",
            "Приход товара",
            "Расход товара",
            "Перемещения товара",
            "Оборачиваемость товаров",
            "Популярные товары",
            "Товары без движения",
            "Дефицит товаров",
            "Избыточные остатки",
            "Сроки годности",
            "Товары по ячейкам",
            "Резервы товаров",
        ):
            self.assertContains(section, title)
        self.assertNotContains(section, "Товар по складам")

        search = self.client.get("/team-manager/reports/products/", {"q": "срок годности"})
        self.assertEqual(search.status_code, 200)
        self.assertContains(search, "Сроки годности")
        self.assertContains(search, "Найдено: 2 из 15")

    def test_manager_product_stock_detail_uses_real_rows_filters_and_sorting(self):
        create_warehouse_snapshot_row(
            agency=self.agency,
            sku="SKU-777",
            name="Товар для отчёта",
            barcode="BAR-777",
            qty=12,
            available_qty=9,
            shipping_reserved_qty=3,
            zone="OS",
            row=2,
            section=3,
            tier=4,
            cell=5,
        )

        detail = self.client.get(
            "/team-manager/reports/products/product-stock/",
            {
                "client_id": self.agency.id,
                "sku": "SKU-777",
                "page_size": "20",
                "sort": "-actual_quantity",
                "columns": ["article", "product_name", "actual_quantity", "available_quantity", "reserved_quantity"],
            },
        )

        self.assertEqual(detail.status_code, 200)
        self.assertContains(detail, "Товар для отчёта")
        self.assertContains(detail, "SKU-777")
        self.assertContains(detail, "12")
        self.assertContains(detail, "9")
        self.assertContains(detail, "3")
        self.assertContains(detail, "Экспорт XLSX")
        self.assertNotContains(detail, "Каркас отчёта готов")

    def test_manager_product_reports_favorite_saved_export_and_history(self):
        create_warehouse_snapshot_row(
            agency=self.agency,
            sku="SKU-EXPORT",
            name="Товар для выгрузки",
            barcode="BAR-EXPORT",
            qty=5,
            available_qty=4,
            shipping_reserved_qty=1,
        )

        favorite = self.client.post(
            "/team-manager/reports/products/product-stock/favorite/",
            {"next": "/team-manager/reports/products/"},
        )
        self.assertEqual(favorite.status_code, 302)
        section = self.client.get("/team-manager/reports/products/")
        self.assertNotContains(section, "Избранное в разделе")
        self.assertContains(section, "Остатки товаров")

        saved = self.client.post(
            "/team-manager/reports/products/product-stock/save/",
            {
                "title": "Мой отчёт по остаткам",
                "filter_client_id": str(self.agency.id),
                "filter_sku": "SKU-EXPORT",
                "filter_sort": "-actual_quantity",
                "filter_columns": ["article", "product_name", "actual_quantity"],
                "export_format": "csv",
            },
        )
        self.assertEqual(saved.status_code, 302)
        saved_page = self.client.get("/team-manager/reports/saved/")
        self.assertContains(saved_page, "Мой отчёт по остаткам")
        self.assertContains(saved_page, "sku=SKU-EXPORT")
        self.assertContains(saved_page, "columns=article")

        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            export = self.client.get(
                "/team-manager/reports/products/product-stock/export/csv/",
                {"client_id": self.agency.id, "sku": "SKU-EXPORT", "columns": ["article", "product_name", "actual_quantity"]},
            )
            self.assertEqual(export.status_code, 200)
            self.assertIn("text/csv", export["Content-Type"])
            self.assertIn("Товар для выгрузки", export.content.decode("utf-8-sig"))

            history = self.client.get("/team-manager/reports/history/")
            self.assertContains(history, "Остатки товаров")
            self.assertContains(history, "Готов")

    def test_manager_product_reserve_and_stock_value_states(self):
        WarehouseReserve.objects.create(
            agency=self.agency,
            reserve_type=WarehouseReserve.TYPE_SHIPPING,
            context_type="shipping",
            context_id="OTG-TEST",
            sku_code="SKU-RES",
            qty_reserved=7,
            qty_allocated=2,
            qty_satisfied=3,
            status=WarehouseReserve.STATUS_PARTIALLY_SATISFIED,
        )
        reserve = self.client.get("/team-manager/reports/products/product-reserves/", {"sku": "SKU-RES"})
        self.assertEqual(reserve.status_code, 200)
        self.assertContains(reserve, "OTG-TEST")
        self.assertContains(reserve, "7")
        self.assertContains(reserve, "4")

        value = self.client.get("/team-manager/reports/products/stock-value/")
        self.assertEqual(value.status_code, 404)

    def test_manager_reports_catalog_api(self):
        catalog = self.client.get("/team-manager/api/reports/catalog/")
        self.assertEqual(catalog.status_code, 200)
        payload = catalog.json()
        self.assertIn("sections", payload)
        self.assertEqual(payload["total_reports"], 62)

        metadata = self.client.get("/team-manager/api/reports/warehouse/current-stock/metadata/")
        self.assertEqual(metadata.status_code, 200)
        report = metadata.json()["report"]
        self.assertEqual(report["code"], "current-stock")
        self.assertEqual(report["section"], "warehouse")
        self.assertIn("filters", report)
        self.assertIn("columns", report)
        self.assertEqual(report["status"], "live")
        self.assertEqual(report["url"], "/team-manager/inventory/")

    def test_manager_nomenclature_report_uses_real_rows_and_export(self):
        sku = SKU.objects.create(
            agency=self.agency,
            sku_code="SKU-CATALOG-1",
            name="Полезная карточка товара",
            brand="FULLBOX TEST",
            tovar_category="Тестовая категория",
            weight_kg=Decimal("1.250"),
            length_mm=100,
            width_mm=200,
            height_mm=300,
        )
        SKUBarcode.objects.create(sku=sku, value="CATALOG-BAR-1", is_primary=True)

        detail = self.client.get(
            "/team-manager/reports/nomenclature/catalog/",
            {"client_id": self.agency.id, "sku": "SKU-CATALOG-1"},
        )
        self.assertEqual(detail.status_code, 200)
        self.assertContains(detail, "Полезная карточка товара")
        self.assertContains(detail, "CATALOG-BAR-1")
        self.assertNotContains(detail, "Каркас отчёта готов")

        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            export = self.client.get(
                "/team-manager/reports/nomenclature/catalog/export/csv/",
                {"client_id": self.agency.id, "sku": "SKU-CATALOG-1"},
            )
            self.assertEqual(export.status_code, 200)
            self.assertIn("Полезная карточка товара", export.content.decode("utf-8-sig"))

    def test_all_custom_manager_reports_render_without_placeholders(self):
        from teammanager.decision_reports import CUSTOM_REPORTS

        for section, code in sorted(CUSTOM_REPORTS):
            with self.subTest(section=section, code=code):
                response = self.client.get(f"/team-manager/reports/{section}/{code}/")
                self.assertEqual(response.status_code, 200)
                self.assertNotContains(response, "Каркас отчёта готов")

    def test_manager_knowledge_base_shows_logistics_guide(self):
        catalog = self.client.get("/team-manager/knowledge/")
        self.assertEqual(catalog.status_code, 200)
        self.assertContains(catalog, "База знаний")
        self.assertContains(catalog, "Логистика: заявка, рейс, маршрут, ТТН")
        self.assertContains(catalog, 'href="/team-manager/knowledge/logistics/"')
        self.assertContains(catalog, "Биллинг: акт, счёт, УПД и закрытие сделки")
        self.assertContains(catalog, 'href="/team-manager/knowledge/billing-close/"')
        self.assertContains(catalog, "Приёмка с распределением")
        self.assertContains(catalog, 'href="/team-manager/knowledge/receiving-distribution/"')
        self.assertContains(catalog, "Wildberries FBS: как создать API-токен")
        self.assertContains(catalog, 'href="/team-manager/knowledge/wb-fbs-api/"')
        self.assertContains(catalog, "Ozon FBS: как создать API-ключ")
        self.assertContains(catalog, 'href="/team-manager/knowledge/ozon-fbs-api/"')
        self.assertContains(catalog, "Wildberries: API-токен для номенклатуры")
        self.assertContains(catalog, 'href="/team-manager/knowledge/wb-nomenclature-token/"')
        self.assertContains(catalog, "Ozon: Seller API для номенклатуры")
        self.assertContains(catalog, 'href="/team-manager/knowledge/ozon-nomenclature-api/"')

        article = self.client.get("/team-manager/knowledge/logistics/")
        self.assertEqual(article.status_code, 200)
        self.assertContains(article, "Создать рейс из готовых заявок")
        self.assertContains(article, "Погрузка и ТТН")
        self.assertContains(article, "Сформировать рейс")

        billing_kb = self.client.get("/team-manager/knowledge/billing-close/")
        self.assertEqual(billing_kb.status_code, 200)
        self.assertContains(billing_kb, "Выставить счёт")
        self.assertContains(billing_kb, "УПД")
        self.assertContains(billing_kb, "Создать акт")
        self.assertContains(billing_kb, "/team-manager/billing/upd/")

        wb_kb = self.client.get("/team-manager/knowledge/wb-fbs-api/")
        self.assertEqual(wb_kb.status_code, 200)
        self.assertContains(wb_kb, "Создать новый токен")
        self.assertContains(wb_kb, "Контент")
        self.assertContains(wb_kb, "Маркетплейс")
        self.assertContains(wb_kb, "/static/docs/api-wildberries-fbs.pdf")
        self.assertContains(wb_kb, "download")

        ozon_kb = self.client.get("/team-manager/knowledge/ozon-fbs-api/")
        self.assertEqual(ozon_kb.status_code, 200)
        self.assertContains(ozon_kb, "Seller API")
        self.assertContains(ozon_kb, "Client ID")
        self.assertContains(ozon_kb, "API Key")
        self.assertContains(ozon_kb, "Posting FBS")
        self.assertContains(ozon_kb, "/static/docs/api-ozon-fbs.pdf")
        self.assertContains(ozon_kb, "download")

        wb_nom_kb = self.client.get("/team-manager/knowledge/wb-nomenclature-token/")
        self.assertEqual(wb_nom_kb.status_code, 200)
        self.assertContains(wb_nom_kb, "Интеграции по API")
        self.assertContains(wb_nom_kb, "Персональный токен")
        self.assertContains(wb_nom_kb, "Контент")
        self.assertContains(wb_nom_kb, "Только чтение")
        self.assertContains(wb_nom_kb, "/static/docs/wb-nomenclature-token.pdf")
        self.assertContains(wb_nom_kb, "download")

        ozon_nom_kb = self.client.get("/team-manager/knowledge/ozon-nomenclature-api/")
        self.assertEqual(ozon_nom_kb.status_code, 200)
        self.assertContains(ozon_nom_kb, "Seller API")
        self.assertContains(ozon_nom_kb, "Admin read only")
        self.assertContains(ozon_nom_kb, "Client ID")
        self.assertContains(ozon_nom_kb, "180 дней")
        self.assertContains(ozon_nom_kb, "/static/docs/ozon-nomenclature-seller-api.pdf")
        self.assertContains(ozon_nom_kb, "download")

        help_page = self.client.get("/logistics/help/")
        self.assertEqual(help_page.status_code, 200)
        self.assertContains(help_page, "Инструкции")
        self.assertContains(help_page, "транспортная накладная")

    def test_manager_side_nav_collapses_billing_into_one_item(self):
        dash = self.client.get("/team-manager/")
        self.assertEqual(dash.status_code, 200)
        html = dash.content.decode("utf-8")
        aside = html.split("<aside", 1)[-1].split("</aside>", 1)[0] if "<aside" in html else html
        self.assertIn("Тарифы, начисления, счета и документы", aside)
        self.assertIn("База знаний", aside)
        self.assertNotIn("К выставлению", aside)
        self.assertNotIn("Номенклатура товаров", aside)
        self.assertNotIn("/team-manager/billing/clients/", aside)
        billing = self.client.get("/team-manager/billing/")
        self.assertEqual(billing.status_code, 200)
        self.assertContains(billing, "Клиенты и договоры")
        self.assertContains(billing, "Акты и УПД")
        self.assertContains(billing, "Ещё")
        self.assertContains(billing, 'href="/team-manager/billing/requests/"')
        self.assertContains(billing, 'href="/team-manager/billing/tariffs/"')

    def test_manager_orders_other_opens_other_detail_url(self):
        order_id = "OTH-000215"
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="other",
            action="submit",
            agency=self.agency,
            user=self.user,
            description="Прочая заявка ожидает менеджера",
            payload={
                "status": "submitted",
                "status_label": "Ждет подтверждения",
                "category": "measure_size",
                "category_label": "Измерить размер",
                "description": "Нужен замер образца",
            },
        )
        Task.objects.create(
            title=f"Подтвердите прочую заявку №{order_id}",
            route=f"/orders/other/{order_id}/",
            status="backlog",
            assigned_to=self.user.employee_profile,
        )

        orders = self.client.get("/team-manager/orders/", {"type": "other"})
        self.assertEqual(orders.status_code, 200)
        self.assertContains(orders, "Другие")
        self.assertContains(orders, f"/team-manager/other-requests/{order_id}/")
        self.assertNotContains(orders, f"#/request/other/{order_id}")

        detail = self.client.get(f"/team-manager/other-requests/{order_id}/")
        self.assertEqual(detail.status_code, 200)
        self.assertContains(detail, "Другая заявка")
        self.assertContains(detail, "Нужен замер образца")
        self.assertContains(detail, "Подтвердить и отправить на склад")

        dashboard = self.client.get("/team-manager/")
        self.assertEqual(dashboard.status_code, 200)
        self.assertContains(dashboard, f"№{order_id}")
        self.assertContains(dashboard, "Клиент для менеджера")
        self.assertContains(dashboard, "Ждет подтверждения")

    def test_manager_dashboard_table_shows_audit_backed_task_row(self):
        order_id = "701"
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="receiving",
            action="status",
            agency=self.agency,
            user=self.user,
            description="Приемка ожидает менеджера",
            payload={
                "status": "sent_unconfirmed",
                "status_label": "Ждет подтверждения",
                "marketplace": "Ozon",
                "warehouse": "Щербинка",
            },
        )
        Task.objects.create(
            title="Проверить приемку",
            route=f"/orders/receiving/{order_id}/",
            status="in_progress",
            priority="high",
            assigned_to=self.user.employee_profile,
            due_date=timezone.now() - timedelta(hours=2),
        )

        dash = self.client.get("/team-manager/")
        self.assertEqual(dash.status_code, 200)
        self.assertContains(dash, "№701_PR")
        self.assertContains(dash, "Приемка")
        self.assertContains(dash, "Клиент для менеджера")
        self.assertContains(dash, "OZON")
        self.assertContains(dash, "Щербинка")
        self.assertContains(dash, "Ждет подтверждения")
        self.assertContains(dash, "Просрочено")
        self.assertContains(dash, "Подтвердить приемку")

    def test_manager_dashboard_links_shipping_task_pk_to_public_order_number(self):
        market = Market.objects.create(id=2, name="WB")
        order = ShippingOrder.objects.create(
            number="SO-000091",
            agency=self.agency,
            created_by=self.user,
            status=ShippingOrder.STATUS_SUBMITTED,
            marketplace=market,
            destination_warehouse="Чехов 2",
            expected_boxes=12,
            delivery_type=ShippingOrder.DELIVERY_MARKETPLACE,
        )
        OrderAuditEntry.objects.create(
            order_id=order.number,
            order_type="shipping",
            action="status",
            agency=self.agency,
            user=self.user,
            description="Заявка отправлена менеджеру на согласование",
            payload={
                "shipping_state": "submitted",
                "status_label": "На согласовании менеджера",
            },
        )
        Task.objects.create(
            title=f"Заявка на отгрузку №{order.number}",
            route=f"/shipping/{order.pk}/",
            status="backlog",
            assigned_to=self.user.employee_profile,
        )

        dash = self.client.get("/team-manager/", {"type": "shipping", "q": "91"})

        self.assertEqual(dash.status_code, 200)
        self.assertContains(dash, "№SO-000091")
        self.assertNotContains(dash, f"№{order.pk}_OTG")
        self.assertContains(dash, "Клиент для менеджера")
        self.assertContains(dash, "На согласовании менеджера")
        self.assertContains(dash, "Подтвердить отгрузку")

    def test_manager_dashboard_prioritizes_client_shipping_cancel_request(self):
        order = ShippingOrder.objects.create(
            number="OTG-000145",
            agency=self.agency,
            created_by=self.user,
            status=ShippingOrder.STATUS_RESERVED,
        )
        OrderAuditEntry.objects.create(
            order_id=order.number,
            order_type="shipping",
            action="cancel_request",
            agency=self.agency,
            user=self.user,
            description="Отменено клиентом",
            payload={
                "status": "cancel_requested",
                "status_label": "Отмена на согласовании менеджера",
                "shipping_state": ShippingOrder.STATUS_RESERVED,
                "cancel_requested_by_client": True,
            },
        )
        Task.objects.create(
            title=f"Согласуйте отмену заявки №{order.number}",
            route=f"/shipping/{order.pk}/",
            status="backlog",
            assigned_to=self.user.employee_profile,
        )

        dash = self.client.get("/team-manager/", {"type": "shipping", "q": "145"})

        self.assertEqual(dash.status_code, 200)
        self.assertContains(dash, "Отмена на согласовании менеджера")
        self.assertContains(dash, "Подтвердить отмену")
        self.assertNotContains(dash, "Подтвердить отгрузку")

    def test_manager_dashboard_keeps_processing_status_with_stock_snapshot(self):
        order_id = "501"
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="processing",
            action="status",
            agency=self.agency,
            user=self.user,
            description="Обработка ожидает подтверждения",
            payload={
                "status": "sent_unconfirmed",
                "status_label": "Ждет подтверждения",
            },
        )
        create_warehouse_snapshot_row(
            agency=self.agency,
            order_type="processing",
            order_id=order_id,
            sku="SKU-MGR-501",
            name="Товар для менеджера",
            barcode="290000000501",
            qty=10,
            available_qty=0,
            processing_reserved_qty=10,
            box_code="BOX-MGR-501",
            pallet_code="PAL-MGR-501",
            warehouse_state_code="reserved_for_processing",
        )
        Task.objects.create(
            title="Обработка",
            route=f"/orders/processing/{order_id}/",
            status="in_progress",
            assigned_to=self.user.employee_profile,
        )

        dash = self.client.get("/team-manager/")
        self.assertEqual(dash.status_code, 200)
        self.assertContains(dash, "№501_OBR")
        self.assertContains(dash, "Ждет подтверждения")
        self.assertNotContains(dash, "Передано в обработку")

    def test_clients_section_in_manager_cabinet(self):
        dash = self.client.get("/team-manager/")
        self.assertEqual(dash.status_code, 200)
        self.assertContains(dash, 'href="/team-manager/clients/"')
        self.assertContains(dash, "Клиенты")

        page = self.client.get("/team-manager/clients/")
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "Клиент для менеджера")
        self.assertContains(page, f"/client/dashboard/lk/?client={self.agency.id}")
        self.assertContains(page, "Открыть ЛК")
        self.assertContains(page, "/client/new/?next=/team-manager/clients/")
        self.assertContains(page, f"/client/{self.agency.id}/edit/?next=/team-manager/clients/")
        self.assertContains(page, "Редактировать")
        self.assertContains(page, "Архивировать")

    def test_clients_section_hides_draft_client_until_accountant_activation(self):
        from accountant.models import ClientLifecycle
        from accountant.selectors import ensure_lifecycle

        draft = Agency.objects.create(
            agn_name="Новый клиент черновик",
            inn="7700000011",
            mened_user_id=self.user.id,
        )
        ensure_lifecycle(draft, status=ClientLifecycle.STATUS_DRAFT, user=self.user)

        page = self.client.get("/team-manager/clients/")
        self.assertEqual(page.status_code, 200)
        self.assertNotContains(page, "Новый клиент черновик")
        self.assertNotContains(page, f"/client/{draft.id}/edit/?next=/team-manager/clients/")

        lifecycle = draft.lifecycle
        lifecycle.status = ClientLifecycle.STATUS_ACTIVE
        lifecycle.save(update_fields=["status", "updated_at"])

        active_page = self.client.get("/team-manager/clients/")
        self.assertEqual(active_page.status_code, 200)
        self.assertContains(active_page, "Новый клиент черновик")
        self.assertContains(active_page, f"/client/{draft.id}/edit/?next=/team-manager/clients/")

    def test_manager_orders_journal_lists_and_filters_requests(self):
        shipping_order = ShippingOrder.objects.create(
            number="SO-000082",
            agency=self.agency,
            created_by=self.user,
            status=ShippingOrder.STATUS_SUBMITTED,
            delivery_type=ShippingOrder.DELIVERY_MARKETPLACE,
            marketplace=self.ozon,
            destination_warehouse="МО_ЩЕРБИНКА_ХАБ",
            shipping_barcode="GM-001",
            supply_number="2000059613908",
        )
        if ShippingDestination is not None:
            ShippingDestination.objects.create(
                order=shipping_order,
                warehouse_name="Пушкино",
                planned_boxes=2,
                planned_units=24,
                comment="ШК ГМ: GM-001, GM-002",
            )
        ShippingOrderItem.objects.create(
            order=shipping_order,
            sku_code="SKU-OZON",
            name="Ozon товар",
            barcode="2048056437522",
            qty_requested=24,
            qty_reserved=24,
            comment="Коробов: 2; кратность: 12; ШК ГМ Ozon: GM-001, GM-002",
        )
        OrderAuditEntry.objects.create(
            order_id="SO-000082",
            order_type="shipping",
            action="status",
            agency=self.agency,
            user=self.user,
            description="Отгрузка ожидает менеджера",
            payload={
                "status": "submitted",
                "shipping_state": "submitted",
                "status_label": "На согласовании менеджера",
                "marketplace": "OZON",
                "destination_warehouse": "Щербинка",
            },
        )
        OrderAuditEntry.objects.create(
            order_id="702",
            order_type="receiving",
            action="status",
            agency=self.agency,
            user=self.user,
            description="Приемка передана на склад",
            payload={
                "status": "warehouse",
                "status_label": "В работе склада",
                "marketplace": "Ozon",
                "warehouse": "Новосибирск",
            },
        )

        # Статус без marketplace/склада — данные должны подтянуться из ShippingOrder / истории.
        OrderAuditEntry.objects.create(
            order_id="SO-000099",
            order_type="shipping",
            action="create",
            agency=self.agency,
            user=self.user,
            description="Создана отгрузка с транзитом",
            payload={
                "status": "draft",
                "marketplace": "OZON",
                "transit_address": "СТАРАЯ_КУПАВНА_28 (Россия, Московская область)",
            },
        )
        transit_order = ShippingOrder.objects.create(
            number="SO-000099",
            agency=self.agency,
            created_by=self.user,
            status=ShippingOrder.STATUS_SUBMITTED,
            delivery_type=ShippingOrder.DELIVERY_MARKETPLACE,
            marketplace=self.ozon,
            destination_warehouse="",
            transit_address="СТАРАЯ_КУПАВНА_28 (Россия, Московская область)",
        )
        OrderAuditEntry.objects.create(
            order_id="SO-000099",
            order_type="shipping",
            action="status",
            agency=self.agency,
            user=self.user,
            description="Отгрузка без склада в status-payload",
            payload={"status": "submitted", "status_label": "На согласовании менеджера"},
        )

        page = self.client.get("/team-manager/orders/")
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "Реестр всех клиентских процессов")
        self.assertContains(page, "№SO-000082")
        self.assertContains(page, "Клиент для менеджера")
        self.assertContains(page, "На согласовании менеджера")
        self.assertContains(page, f'href="/shipping/{shipping_order.id}/?client={self.agency.id}"')
        self.assertContains(page, "Поставка Ozon: 2000059613908")
        self.assertContains(page, "ШК ГМ: GM-001, GM-002")
        self.assertContains(page, "ЛК клиента")
        self.assertContains(page, "№SO-000099")
        self.assertContains(page, "СТАРАЯ_КУПАВНА_28")
        self.assertContains(page, f'href="/shipping/{transit_order.id}/?client={self.agency.id}"')
        # Приёмка: marketplace/склад из payload.
        self.assertContains(page, "Новосибирск")

        filtered = self.client.get("/team-manager/orders/", {"type": "shipping", "q": "82"})
        self.assertEqual(filtered.status_code, 200)
        self.assertContains(filtered, "№SO-000082")
        self.assertNotContains(filtered, "№702_PR")

    def test_manager_orders_status_uses_latest_status_not_edit_event(self):
        OrderAuditEntry.objects.create(
            order_id="115",
            order_type="receiving",
            action="status",
            agency=self.agency,
            user=self.user,
            description="Заявка передана в работу",
            payload={
                "status": "warehouse",
                "status_label": "В работе склада",
                "marketplace": "Ozon",
                "warehouse": "Щербинка",
            },
        )
        OrderAuditEntry.objects.create(
            order_id="115",
            order_type="receiving",
            action="update",
            agency=self.agency,
            user=self.user,
            description="Исправление заявки №115",
            payload={
                "status": "draft",
                "status_label": "Черновик",
                "marketplace": "Ozon",
                "warehouse": "Щербинка",
            },
        )

        page = self.client.get("/team-manager/orders/")
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "№115_PR")
        self.assertContains(page, "Исправление заявки №115")
        self.assertContains(page, "В ожидании поставки товара")
        self.assertNotContains(page, "<span class=\"badge gray\">Черновик</span>", html=False)

    def test_manager_orders_shows_submission_date_instead_of_latest_update(self):
        submitted = OrderAuditEntry.objects.create(
            order_id="716",
            order_type="receiving",
            action="create",
            agency=self.agency,
            user=self.user,
            description="Заявка подана клиентом",
            payload={
                "status": "sent_unconfirmed",
                "status_label": "Ждет подтверждения",
                "submit_action": "send",
            },
        )
        submitted_at = timezone.now() - timedelta(days=4)
        OrderAuditEntry.objects.filter(pk=submitted.pk).update(created_at=submitted_at)
        latest = OrderAuditEntry.objects.create(
            order_id="716",
            order_type="receiving",
            action="status",
            agency=self.agency,
            user=self.user,
            description="Заявка передана на склад",
            payload={
                "status": "warehouse",
                "status_label": "В ожидании поставки товара",
            },
        )

        page = self.client.get("/team-manager/orders/", {"type": "receiving"})

        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "Дата подачи")
        self.assertNotContains(page, "Обновлено")
        row = next(item for item in page.context["order_rows"] if item["order_id"] == "716")
        self.assertEqual(row["submitted_at"].date(), timezone.localtime(submitted_at).date())
        self.assertEqual(row["updated_at"].date(), timezone.localtime(latest.created_at).date())

        today = self.client.get(
            "/team-manager/orders/",
            {"type": "receiving", "period": "today"},
        )
        self.assertNotIn(
            "716",
            {item["order_id"] for item in today.context["order_rows"]},
        )
        week = self.client.get(
            "/team-manager/orders/",
            {"type": "receiving", "period": "week"},
        )
        self.assertIn(
            "716",
            {item["order_id"] for item in week.context["order_rows"]},
        )

    def test_manager_orders_replaces_question_noise_event_with_shipping_fallback(self):
        ShippingOrder.objects.create(
            number="SO-000082",
            agency=self.agency,
            created_by=self.user,
            status=ShippingOrder.STATUS_SHIPPED,
            delivery_type=ShippingOrder.DELIVERY_MARKETPLACE,
            marketplace=self.ozon,
            destination_warehouse="Екатеринбург-Перспективная",
        )
        OrderAuditEntry.objects.create(
            order_id="SO-000082",
            order_type="shipping",
            action="status",
            agency=self.agency,
            user=self.user,
            description="?????? ??????? ???????: ?????????? ???? ??????????????? ? ????????? ?????",
            payload={
                "shipping_state": "shipped",
                "manual_close_reason": "factually_shipped_legacy_order",
                "marketplace": "WB",
                "destination_warehouse": "Екатеринбург-Перспективная",
            },
        )

        page = self.client.get("/team-manager/orders/")
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "№SO-000082")
        self.assertContains(page, "Заявка отгружена")
        self.assertNotContains(page, "?????? ???????")

    def test_manager_orders_hides_processing_client_draft_after_submit(self):
        """Клиент отправил обработку — в журнале менеджера нет ложного черновика."""
        OrderAuditEntry.objects.create(
            order_id="17",
            order_type="processing",
            action="create",
            agency=self.agency,
            user=self.user,
            description="Заявка на обработку №17",
            payload={
                "status": "sent_unconfirmed",
                "status_label": "Ждет подтверждения",
                "submit_action": "submitted",
                "order_title": "Заявка на обработку",
            },
        )
        OrderAuditEntry.objects.create(
            order_id="draft-f04844d889b2",
            order_type="processing",
            action="create",
            agency=self.agency,
            user=self.user,
            description="Черновик заявки на обработку",
            payload={
                "status": "draft",
                "status_label": "Черновик",
                "submit_action": "draft",
                "order_title": "Черновик заявки на обработку",
            },
        )

        page = self.client.get("/team-manager/orders/", {"type": "processing"})
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "№17_OBR")
        self.assertContains(page, "Ждет подтверждения")
        self.assertNotContains(page, "draft-f04844d889b2")
        self.assertNotContains(page, "OBR · черновик")
        self.assertNotContains(page, "Черновик заявки на обработку")

    def test_manager_dashboard_hides_client_drafts_from_kpi_and_tasks(self):
        OrderAuditEntry.objects.create(
            order_id="draft-manager-hidden",
            order_type="receiving",
            action="create",
            agency=self.agency,
            user=self.user,
            description="Черновик заявки на приемку",
            payload={"status": "draft", "status_label": "Черновик"},
        )

        dashboard = self.client.get("/team-manager/")
        events = self.client.get("/team-manager/api/task-events/")

        self.assertEqual(dashboard.status_code, 200)
        self.assertNotContains(dashboard, "draft-manager-hidden")
        self.assertNotContains(dashboard, "Черновик заявки на приемку")
        self.assertEqual(dashboard.context["manager_stats"]["client_drafts"], 0)
        self.assertEqual(events.status_code, 200)
        self.assertNotContains(events, "draft-manager-hidden")

    def test_latest_request_entries_preserves_status_history_with_scalar_payloads(self):
        submitted = OrderAuditEntry.objects.create(
            order_id="scalar-history-visible",
            order_type="receiving",
            action="status",
            agency=self.agency,
            user=self.user,
            description="Заявка отправлена",
            payload={"status": "submitted", "status_label": "На согласовании"},
        )
        technical = OrderAuditEntry.objects.create(
            order_id="scalar-history-visible",
            order_type="receiving",
            action="status",
            agency=self.agency,
            user=self.user,
            description="Техническое событие без статуса",
            payload={"act_sent": False, "act": {}},
        )
        OrderAuditEntry.objects.create(
            order_id="scalar-history-hidden",
            order_type="receiving",
            action="status",
            agency=self.agency,
            user=self.user,
            description="Заявка отправлена",
            payload={"status": "submitted", "status_label": "На согласовании"},
        )
        OrderAuditEntry.objects.create(
            order_id="scalar-history-hidden",
            order_type="receiving",
            action="update",
            agency=self.agency,
            user=self.user,
            description="Черновик со строковым признаком акта",
            payload={
                "status": "draft",
                "status_label": "Черновик",
                "act_sent": "false",
            },
        )

        from teammanager.views import _latest_request_entries

        entries = _latest_request_entries(limit=20, order_type="receiving")
        visible = next(
            entry for entry in entries if entry.order_id == "scalar-history-visible"
        )

        self.assertEqual(visible.id, technical.id)
        self.assertEqual(visible._manager_status_entry_cache.id, submitted.id)
        self.assertEqual(visible._manager_submitted_at, submitted.created_at)
        self.assertNotIn(
            "scalar-history-hidden",
            {entry.order_id for entry in entries},
        )


    def test_manager_can_create_and_edit_clients(self):
        create_page = self.client.get("/client/new/", {"next": "/team-manager/clients/"})
        self.assertEqual(create_page.status_code, 200)
        self.assertContains(create_page, 'name="next"')
        self.assertContains(create_page, "/team-manager/clients/")

        edit_page = self.client.get(f"/client/{self.agency.id}/edit/", {"next": "/team-manager/clients/"})
        self.assertEqual(edit_page.status_code, 200)
        self.assertContains(edit_page, "Редактирование клиента")
        self.assertContains(edit_page, "/team-manager/clients/")

    def test_manager_cannot_create_client_with_existing_inn(self):
        response = self.client.post(
            "/client/new/",
            {
                "agn_name": "ООО Дубль менеджера",
                "short_name": "Дубль",
                "inn": self.agency.inn,
                "phone": "+7 (900) 111-22-33",
                "fio_agn": "Иванов",
                "email": "double@example.com",
                "portal_login": "double_client",
                "portal_password": "pwd12345",
                "next": "/team-manager/clients/",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Такой ИНН уже есть в системе")
        self.assertEqual(Agency.objects.filter(inn=self.agency.inn).count(), 1)

    def test_clients_search(self):
        Agency.objects.create(agn_name="Другой клиент", inn="7800000000")
        page = self.client.get("/team-manager/clients/", {"q": "менеджера"})
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "Клиент для менеджера")
        self.assertNotContains(page, "Другой клиент")

    def test_manager_orders_fills_pickup_meta_and_status_from_shipping_order(self):
        """Без МП/склада в audit — берём delivery_type и get_status_display из ShippingOrder."""
        shipping_order = ShippingOrder.objects.create(
            number="SO-000059",
            agency=self.agency,
            created_by=self.user,
            status=ShippingOrder.STATUS_SHIPPED,
            delivery_type=ShippingOrder.DELIVERY_PICKUP,
            marketplace=None,
            destination_warehouse="",
        )
        OrderAuditEntry.objects.create(
            order_id="SO-000059",
            order_type="shipping",
            action="update",
            agency=self.agency,
            user=self.user,
            description="Заявка отредактирована менеджером",
            payload={
                "status": "shipped",
                "marketplace": "",
                "marketplace_id": None,
                "warehouse": "",
            },
        )
        # marketplace_id без имени в payload — должно резолвиться в OZON.
        shipping_with_id = ShippingOrder.objects.create(
            number="SO-000088",
            agency=self.agency,
            created_by=self.user,
            status=ShippingOrder.STATUS_PACKED,
            delivery_type=ShippingOrder.DELIVERY_MARKETPLACE,
            marketplace=self.ozon,
            destination_warehouse="Саратул",
        )
        OrderAuditEntry.objects.create(
            order_id="SO-000088",
            order_type="shipping",
            action="status",
            agency=self.agency,
            user=self.user,
            description="Подготовлена складом",
            payload={"status": "packed", "marketplace_id": self.ozon.id},
        )

        page = self.client.get("/team-manager/orders/")
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "№SO-000059")
        self.assertContains(page, "Самовывоз")
        self.assertContains(page, "Отгружена")
        self.assertContains(page, "№SO-000088")
        self.assertContains(page, "OZON")
        self.assertContains(page, "Саратул")
        self.assertIsNotNone(shipping_order.pk)
        self.assertIsNotNone(shipping_with_id.pk)

    def test_manager_orders_shows_open_act_for_receiving_and_shipping(self):
        OrderAuditEntry.objects.create(
            order_id="803",
            order_type="receiving",
            action="status",
            agency=self.agency,
            user=self.user,
            description="Акт приёмки отправлен менеджеру",
            payload={
                "status": "warehouse",
                "status_label": "Принято складом, акт приемки отправлен менеджеру",
                "act_storekeeper_signed": True,
            },
        )
        shipping_order = ShippingOrder.objects.create(
            number="SO-ACT-001",
            agency=self.agency,
            created_by=self.user,
            status=ShippingOrder.STATUS_PACKED,
            delivery_type=ShippingOrder.DELIVERY_MARKETPLACE,
            marketplace=self.ozon,
            destination_warehouse="Щербинка",
        )
        OrderAuditEntry.objects.create(
            order_id="SO-ACT-001",
            order_type="shipping",
            action="status",
            agency=self.agency,
            user=self.user,
            description="Акт отгрузки готов",
            payload={
                "status": "packed",
                "shipping_state": "packed",
                "status_label": "Упакована",
                "act": "shipping_dispatch_act",
                "act_logistician_signed": True,
                "act_sent": True,
            },
        )

        page = self.client.get("/team-manager/orders/")
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "Открыть акт")
        self.assertContains(page, f'href="/orders/receiving/803/act/print/"')
        self.assertContains(page, f'href="/shipping/{shipping_order.id}/act/"')
        self.assertContains(page, f'href="/shipping/{shipping_order.id}/boxes.xlsx"')
        self.assertContains(page, "Состав коробов")
        self.assertContains(page, "Подписать акт")
        self.assertContains(page, "№803_PR")
        self.assertContains(page, "№SO-ACT-001")
    def test_shipped_order_not_operationally_overdue(self):
        """Завершённая отгрузка: операционный SLA закрыт, фокус — документы/фин.действие."""
        from types import SimpleNamespace

        from teammanager.views import _assign_manager_category, _finance_sla, _ops_complete

        shipped = SimpleNamespace(status="shipped")
        self.assertTrue(
            _ops_complete(task_type="shipping", shipping_order=shipped, status_bucket="")
        )
        self.assertFalse(
            _ops_complete(
                task_type="shipping",
                shipping_order=SimpleNamespace(status="submitted"),
                status_bucket="",
            )
        )
        sla = _finance_sla("Выставить счёт")
        self.assertFalse(sla["is_overdue"])
        self.assertEqual(sla["css"], "orange")
        self.assertEqual(
            _assign_manager_category(
                {
                    "is_overdue": False,
                    "executor": "Менеджер",
                    "stuck_reason": "",
                    "next_step": "Выставить счёт",
                    "deadline": timezone.now() - timedelta(hours=5),
                }
            ),
            "docs",
        )
        # Просроченный due_date не должен давать overdue, если ops завершён и SLA уже фин.
        self.assertNotEqual(
            _assign_manager_category(
                {
                    "is_overdue": sla["is_overdue"],
                    "executor": "Менеджер",
                    "stuck_reason": "",
                    "next_step": "Выставить счёт",
                    "deadline": timezone.now() - timedelta(hours=5),
                }
            ),
            "overdue",
        )

    def test_focus_categories_are_mutually_exclusive(self):
        from teammanager.views import _assign_manager_category

        row = {
            "is_overdue": True,
            "executor": "—",
            "stuck_reason": "Зависло",
            "next_step": "Выставить счёт",
            "deadline": timezone.now(),
        }
        self.assertEqual(_assign_manager_category(row), "overdue")
        row["is_overdue"] = False
        self.assertEqual(_assign_manager_category(row), "no_owner")
        row["executor"] = "Иван"
        self.assertEqual(_assign_manager_category(row), "stuck")
        row["stuck_reason"] = ""
        self.assertEqual(_assign_manager_category(row), "docs")


class TeamCabinetUnifiedAccessTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.manager_user = User.objects.create_user(username="cab_mgr", password="pwd")
        self.logist_user = User.objects.create_user(username="cab_log", password="pwd")
        self.manager = Employee.objects.create(
            full_name="Каб Менеджер", user=self.manager_user, role="manager", is_active=True
        )
        self.logist = Employee.objects.create(
            full_name="Каб Логист", user=self.logist_user, role="logistician", is_active=True
        )

    def test_logistician_opens_team_manager_cabinet(self):
        self.client.force_login(self.logist_user)
        dash = self.client.get("/team-manager/")
        self.assertEqual(dash.status_code, 200)
        self.assertContains(dash, "Рейсы")
        self.assertContains(dash, "Личный кабинет менеджера FULLBOX")
        html = dash.content.decode("utf-8")
        aside = html.split("<aside", 1)[-1].split("</aside>", 1)[0]
        self.assertIn("/logistics/", aside)
        self.assertNotIn("Биллинг", aside)

    def test_manager_sees_billing_and_trips(self):
        self.client.force_login(self.manager_user)
        dash = self.client.get("/team-manager/")
        self.assertEqual(dash.status_code, 200)
        self.assertContains(dash, "Биллинг")
        self.assertContains(dash, "Рейсы")

    def test_manager_with_additional_roles_sees_both_cabinet_links(self):
        self.manager.access_roles = ["head_manager", "accountant"]
        self.manager.save(update_fields=["access_roles"])
        self.client.force_login(self.manager_user)

        dash = self.client.get("/team-manager/")

        self.assertEqual(dash.status_code, 200)
        self.assertContains(dash, 'href="/head-manager/"', html=False)
        self.assertContains(dash, 'href="/accountant/"', html=False)

    def test_claim_unassigned_task(self):
        task = Task.objects.create(title="Свободная логистика", route="/shipping/1/", status="backlog")
        self.client.force_login(self.logist_user)
        response = self.client.post(
            f"/team-manager/api/tasks/{task.id}/claim/",
            data='{"reason":"test"}',
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"])
        task.refresh_from_db()
        self.assertEqual(task.assigned_to_id, self.logist.id)
        from teammanager.models import TaskHandoff

        self.assertEqual(TaskHandoff.objects.filter(task=task).count(), 1)

    def test_manager_can_edit_deadline_for_every_service_type_with_audit(self):
        from todo.models import TaskComment

        self.client.force_login(self.manager_user)
        routes = (
            "/orders/receiving/SLA-RECEIVING/",
            "/orders/processing/SLA-PROCESSING/",
            "/orders/other/SLA-OTHER/",
            "/shipping/900001/",
            "/logistics/trips/900001/",
        )
        new_due_date = timezone.localtime() + timedelta(days=3)
        for index, route in enumerate(routes):
            task = Task.objects.create(
                title=f"SLA service {index}",
                route=route,
                status="in_progress",
                assigned_to=self.manager,
                due_date=timezone.localtime() + timedelta(hours=1),
            )
            response = self.client.post(
                f"/team-manager/api/tasks/{task.id}/deadline/",
                data=(
                    '{"due_date":"'
                    + new_due_date.strftime("%Y-%m-%dT%H:%M")
                    + '","reason":"Перенос согласован с клиентом"}'
                ),
                content_type="application/json",
            )

            self.assertEqual(response.status_code, 200, response.content)
            self.assertTrue(response.json()["ok"])
            task.refresh_from_db()
            self.assertEqual(
                timezone.localtime(task.due_date).replace(second=0, microsecond=0),
                new_due_date.replace(second=0, microsecond=0),
            )
            self.assertTrue(
                TaskComment.objects.filter(
                    task=task,
                    body__contains="Перенос согласован с клиентом",
                ).exists()
            )

    def test_logistician_cannot_edit_manager_deadline(self):
        task = Task.objects.create(
            title="SLA shipping",
            route="/shipping/900002/",
            status="in_progress",
            assigned_to=self.logist,
        )
        self.client.force_login(self.logist_user)

        response = self.client.post(
            f"/team-manager/api/tasks/{task.id}/deadline/",
            data=(
                '{"due_date":"'
                + (timezone.localtime() + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M")
                + '","reason":"Перенос согласован"}'
            ),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 403)

    def test_manager_dashboard_shows_deadline_editor_and_separate_stuck_warning(self):
        task = Task.objects.create(
            title="SLA processing dashboard",
            route="/orders/processing/SLA-DASHBOARD/",
            status="in_progress",
            assigned_to=self.manager,
            due_date=timezone.localtime() + timedelta(hours=20),
        )
        Task.objects.filter(pk=task.pk).update(
            updated_at=timezone.localtime() - timedelta(hours=7),
        )
        self.client.force_login(self.manager_user)

        response = self.client.get("/team-manager/", {"section": "tasks"})

        self.assertEqual(response.status_code, 200)
        row = next(item for item in response.context["manager_task_rows"] if item["id"] == task.id)
        self.assertFalse(row["sla"].startswith("Зависло"))
        self.assertTrue(row["stuck_reason"].startswith("Зависло"))
        self.assertContains(response, "Изменить срок")
        self.assertContains(response, 'id="deadline-modal"', html=False)

    def test_resolve_cabinet_url_logistician_to_team_manager(self):
        from employees.access import resolve_cabinet_url

        self.assertEqual(resolve_cabinet_url("logistician"), "/team-manager/")


class TeamCabinetHandoffStage2Tests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.u1 = User.objects.create_user(username="h_mgr", password="pwd")
        self.u2 = User.objects.create_user(username="h_log", password="pwd")
        self.mgr = Employee.objects.create(full_name="H Менеджер", user=self.u1, role="manager", is_active=True)
        self.log = Employee.objects.create(full_name="H Логист", user=self.u2, role="logistician", is_active=True)

    def test_settings_page_and_coverage(self):
        self.client.force_login(self.u1)
        page = self.client.get("/team-manager/settings/")
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "Временное замещение")
        self.assertContains(page, "Массовая передача")
        today = timezone.localdate().isoformat()
        resp = self.client.post(
            "/team-manager/settings/",
            {
                "action": "create_coverage",
                "principal_id": self.mgr.id,
                "substitute_id": self.log.id,
                "valid_from": today,
                "note": "Отпуск",
            },
        )
        self.assertEqual(resp.status_code, 302)
        from teammanager.models import EmployeeCoverage

        cov = EmployeeCoverage.objects.filter(principal=self.mgr, substitute=self.log, is_active=True).first()
        self.assertIsNotNone(cov)

    def test_transfer_and_bulk(self):
        t1 = Task.objects.create(title="T1", route="/shipping/1/", status="in_progress", assigned_to=self.mgr)
        t2 = Task.objects.create(title="T2", route="/shipping/2/", status="backlog", assigned_to=self.mgr)
        self.client.force_login(self.u1)
        transfer = self.client.post(
            f"/team-manager/api/tasks/{t1.id}/transfer/",
            data=f'{{"to_employee_id": {self.log.id}, "reason": "Отпуск", "comment": "на неделю"}}',
            content_type="application/json",
        )
        self.assertEqual(transfer.status_code, 200)
        self.assertTrue(transfer.json()["ok"])
        t1.refresh_from_db()
        self.assertEqual(t1.assigned_to_id, self.log.id)

        bulk = self.client.post(
            "/team-manager/settings/",
            {
                "action": "bulk_transfer",
                "from_employee_id": self.mgr.id,
                "to_employee_id": self.log.id,
                "reason": "vacation",
                "comment": "bulk",
                "task_types": "shipping",
            },
        )
        self.assertEqual(bulk.status_code, 302)
        t2.refresh_from_db()
        self.assertEqual(t2.assigned_to_id, self.log.id)
        from teammanager.models import TaskHandoff

        self.assertGreaterEqual(TaskHandoff.objects.filter(task=t2).count(), 1)

    def test_dashboard_has_transfer_modal(self):
        self.client.force_login(self.u1)
        dash = self.client.get("/team-manager/")
        self.assertEqual(dash.status_code, 200)
        self.assertContains(dash, "xfer-modal")
        self.assertContains(dash, "Передать задачу")
        self.assertContains(dash, 'href="/team-manager/settings/"')


class ShippingRoutingClarifyTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.mgr_user = User.objects.create_user(username="r_mgr", password="pwd")
        self.log_user = User.objects.create_user(username="r_log", password="pwd")
        self.mgr = Employee.objects.create(full_name="R Менеджер", user=self.mgr_user, role="manager", is_active=True)
        self.log = Employee.objects.create(full_name="R Логист", user=self.log_user, role="logistician", is_active=True)
        self.agency = Agency.objects.create(agn_name="R Client", inn="7700999000", mened_user_id=self.mgr_user.id)
        self.order = ShippingOrder.objects.create(
            number="SO-CLARIFY-1",
            agency=self.agency,
            created_by=self.mgr_user,
            status=ShippingOrder.STATUS_PACKED,
            delivery_type=ShippingOrder.DELIVERY_MARKETPLACE,
            destination_warehouse="Склад",
        )
        self.order_id = self.order.pk

    def test_return_and_resolve_clarification(self):
        from logistics.models import ShippingRoutingState
        from logistics.routing_services import (
            resolve_clarification_and_return_to_logistics,
            return_to_manager_for_clarification,
            routing_snapshot,
        )
        from shipping.services import ensure_logistician_task

        ensure_logistician_task(self.order, user=self.log_user)
        snap = routing_snapshot(self.order)
        self.assertEqual(snap["status"], ShippingRoutingState.STATUS_READY)

        return_to_manager_for_clarification(
            order=self.order,
            employee=self.log,
            reason_code=ShippingRoutingState.REASON_NO_ADDRESS,
            reason_text="",
            user=self.log_user,
        )
        snap = routing_snapshot(self.order)
        self.assertTrue(snap["needs_clarification"])
        self.assertEqual(
            Task.objects.filter(route=f"/shipping/{self.order.pk}/", assigned_to=self.mgr)
            .exclude(status="done")
            .count(),
            1,
        )
        self.assertEqual(
            Task.objects.filter(route=f"/shipping/{self.order.pk}/", assigned_to__role="logistician")
            .exclude(status="done")
            .count(),
            0,
        )

        resolve_clarification_and_return_to_logistics(
            order=self.order,
            employee=self.mgr,
            comment="Адрес добавлен",
            user=self.mgr_user,
        )
        snap = routing_snapshot(self.order)
        self.assertEqual(snap["status"], ShippingRoutingState.STATUS_READY)
        self.assertFalse(snap["needs_clarification"])
        self.assertEqual(
            Task.objects.filter(route=f"/shipping/{self.order.pk}/", assigned_to__role="logistician")
            .exclude(status="done")
            .count(),
            1,
        )

    def test_clarify_excluded_from_ready_trips(self):
        from logistics.models import ShippingRoutingState
        from logistics.routing_services import return_to_manager_for_clarification
        from logistics.services import _ready_orders_for_trip
        from shipping.services import ensure_logistician_task

        ensure_logistician_task(self.order, user=self.log_user)
        ids_before = {row["id"] for row in _ready_orders_for_trip() if row.get("id")}
        # row may use order id key differently — check numbers
        numbers_before = {row.get("number") or row.get("order_number") for row in _ready_orders_for_trip()}
        return_to_manager_for_clarification(
            order=self.order,
            employee=self.log,
            reason_code=ShippingRoutingState.REASON_NO_WEIGHT,
            user=self.log_user,
        )
        numbers_after = {row.get("number") or row.get("order_number") for row in _ready_orders_for_trip()}
        self.assertIn("SO-CLARIFY-1", numbers_before | {self.order.number})
        # After clarify must not be in ready list
        ready_numbers = set()
        for row in _ready_orders_for_trip():
            ready_numbers.add(str(row.get("number") or row.get("order_number") or ""))
        self.assertNotIn("SO-CLARIFY-1", ready_numbers)

    def test_packed_order_in_draft_trip_shows_route_control_not_send_to_warehouse(self):
        from logistics.models import LogisticsTrip, LogisticsTripOrder, ShippingRoutingState
        from logistics.routing_services import routing_snapshot
        from teammanager.views import (
            _attach_shipping_task_orders,
            _entry_key,
            _preload_task_row_caches,
            _task_row,
        )

        ShippingRoutingState.objects.create(
            shipping_order=self.order,
            status=ShippingRoutingState.STATUS_READY,
        )
        trip = LogisticsTrip.objects.create(
            number="DRAFT-TESTROUTED001",
            status=LogisticsTrip.STATUS_DRAFT,
            assigned_logistician=self.log,
        )
        LogisticsTripOrder.objects.create(
            trip=trip,
            shipping_order=self.order,
            loading_sequence=1,
            delivery_sequence=1,
        )
        task = Task.objects.create(
            title=f"Заявка на отгрузку №{self.order.number}",
            route=f"/shipping/{self.order.pk}/",
            status="backlog",
            assigned_to=self.log,
        )
        entry = OrderAuditEntry.objects.create(
            order_id=self.order.number,
            order_type="shipping",
            action="status",
            agency=self.agency,
            user=self.log_user,
            description="Кладовщик подготовил заявку к отгрузке",
            payload={"shipping_state": ShippingOrder.STATUS_PACKED, "status": ShippingOrder.STATUS_PACKED},
        )

        snap = routing_snapshot(self.order)
        self.assertEqual(snap["status"], ShippingRoutingState.STATUS_ROUTED)
        self.assertEqual(snap["status_label"], "Рейс назначен")
        self.assertEqual(snap["trip_number"], trip.number)

        tasks = [task]
        latest = {_entry_key(entry): entry}
        _attach_shipping_task_orders(tasks, latest)
        _preload_task_row_caches(tasks)
        row = _task_row(task, latest, timezone.localtime(), live_status=True)

        self.assertEqual(row["next_step"], "Контроль рейса")
        self.assertEqual(row["logistics_status"], "Рейс назначен")
        self.assertNotEqual(row["next_step"], "Передать на склад")


class TeamManagerOtherRequestDetailRedesignTests(TestCase):
    def setUp(self):
        from client_cabinet.models import OtherRequest
        from client_cabinet.other_request_workflow import (
            accept_by_warehouse,
            approve_and_send_to_department,
            seed_default_categories,
            take_in_progress,
        )
        from client_cabinet.other_requests import create_other_request

        self.OtherRequest = OtherRequest
        self.manager_user = get_user_model().objects.create_user(username="or_mgr", password="pwd")
        self.store_user = get_user_model().objects.create_user(username="or_sk", password="pwd")
        self.manager = Employee.objects.create(
            full_name="Менеджер OR", user=self.manager_user, role="manager", is_active=True
        )
        self.storekeeper = Employee.objects.create(
            full_name="Кладовщик OR", user=self.store_user, role="storekeeper", is_active=True
        )
        self.agency = Agency.objects.create(agn_name="Клиент OR", mened_user_id=self.manager_user.id)
        seed_default_categories()

        created = create_other_request(
            agency=self.agency,
            user=self.manager_user,
            category="measure_size",
            description="Замерить короб перед отправкой",
        )
        self.order_id = created["order_id"]
        obj = OtherRequest.objects.get(public_number=self.order_id)
        # Менеджер входит и в EXECUTOR_ROLES, и в MANAGER_ROLES — а кабинет /team-manager/
        # доступен только ролям из CABINET_ROLES (кладовщик работает через /orders/other/).
        obj = approve_and_send_to_department(obj, user=self.manager_user)
        obj = accept_by_warehouse(obj, user=self.store_user)
        self.obj = take_in_progress(obj, user=self.store_user)

    def _use_custom_category(self):
        from client_cabinet.other_request_workflow import resolve_category

        category = resolve_category("custom")
        self.obj.category = category
        self.obj.category_code = category.code
        self.obj.save(update_fields=["category", "category_code"])

    def _add_other_service_fact(self):
        from billing.models import (
            BillingService,
            ClientTariffItem,
            ClientTariffVersion,
            TariffCategory,
            TariffUnit,
        )
        from billing.service_catalog import ensure_service_catalog
        from billing.warehouse_services import replace_warehouse_facts

        ensure_service_catalog()
        service = BillingService.objects.get(code="extra_warehouse_operation")
        category, _created = TariffCategory.objects.get_or_create(
            code="extra",
            defaults={"name": "Дополнительные услуги", "sort_order": 120, "is_active": True},
        )
        unit, _created = TariffUnit.objects.get_or_create(
            code="pcs",
            defaults={"name": "Штука", "short_name": "шт", "is_active": True},
        )
        version = ClientTariffVersion.objects.create(
            client=self.agency,
            name="Тариф других заявок",
            version_number=1,
            status=ClientTariffVersion.STATUS_DRAFT,
            valid_from=timezone.localdate(),
        )
        ClientTariffItem.objects.create(
            tariff_version=version,
            category=category,
            service=service,
            service_name=service.name,
            unit=unit,
            price=Decimal("25.0000"),
            is_active=True,
        )
        version.status = ClientTariffVersion.STATUS_ACTIVE
        version.save(update_fields=["status"])
        replace_warehouse_facts(
            client=self.agency,
            order_type="other",
            order_id=self.order_id,
            lines=[{"service_id": service.pk, "quantity": "2", "source": "other_manual"}],
            user=self.store_user,
        )
        return service

    def test_detail_page_renders_redesigned_blocks(self):
        self.client.force_login(self.manager_user)
        response = self.client.get(f"/team-manager/other-requests/{self.order_id}/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Выполнение задачи")
        self.assertContains(response, "Фото и файлы")
        self.assertContains(response, "Комментарии")
        self.assertContains(response, "История")
        self.assertContains(response, "Сводка")
        self.assertNotContains(response, "measure_length_cm")

    def test_manager_cannot_complete_warehouse_work(self):
        self.client.force_login(self.manager_user)
        response = self.client.post(
            f"/team-manager/other-requests/{self.order_id}/",
            {
                "action": "complete_work",
                "measure_length_cm": "40",
                "measure_width_cm": "30",
                "measure_height_cm": "20",
                "measure_weight_kg": "8.5",
                "measure_package_type": "короб",
                "result_comment": "Замер снят на складе",
            },
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Работу по заявке завершает склад")
        self.obj.refresh_from_db()
        self.assertEqual(self.obj.status, self.OtherRequest.STATUS_IN_PROGRESS)
        self.assertFalse(self.obj.result_comment)

    def test_manager_cannot_bypass_storekeeper_result_validation(self):
        from client_cabinet.other_request_workflow import resolve_category

        category = resolve_category("recount")
        self.obj.category = category
        self.obj.category_code = category.code
        self.obj.save(update_fields=["category", "category_code"])

        self.client.force_login(self.manager_user)
        response = self.client.post(
            f"/team-manager/other-requests/{self.order_id}/",
            {"action": "complete_work", "result_comment": "Без количества"},
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Работу по заявке завершает склад")
        self.obj.refresh_from_db()
        self.assertEqual(self.obj.status, self.OtherRequest.STATUS_IN_PROGRESS)

    def test_executor_cannot_complete_without_agreed_service_fact(self):
        from client_cabinet.other_request_workflow import OtherRequestError, complete_by_executor

        self._use_custom_category()
        with self.assertRaisesMessage(OtherRequestError, "Перед завершением укажите"):
            complete_by_executor(
                self.obj,
                user=self.store_user,
                result_comment="Работа выполнена",
                result_qty=2,
                result_unit="шт",
            )
        self.obj.refresh_from_db()
        self.assertEqual(self.obj.status, self.OtherRequest.STATUS_IN_PROGRESS)

    def test_other_services_are_visible_and_close_without_auto_calculation(self):
        from billing.models import BillingApplication
        from client_cabinet.other_request_workflow import complete_by_executor

        self._use_custom_category()
        service = self._add_other_service_fact()
        self.obj = complete_by_executor(
            self.obj,
            user=self.store_user,
            result_comment="Работа выполнена",
            result_qty=2,
            result_unit="шт",
        )
        self.client.force_login(self.manager_user)
        detail = self.client.get(f"/team-manager/other-requests/{self.order_id}/")
        self.assertEqual(detail.status_code, 200)
        self.assertContains(detail, service.name)
        self.assertContains(detail, "warehouse-services-modal")

        closed = self.client.post(
            f"/team-manager/other-requests/{self.order_id}/",
            {"action": "approve_close"},
            follow=True,
        )
        self.assertEqual(closed.status_code, 200)
        self.obj.refresh_from_db()
        self.assertEqual(self.obj.status, self.OtherRequest.STATUS_CLOSED)
        application = BillingApplication.objects.get(
            application_type=BillingApplication.TYPE_OTHER,
            application_id=self.order_id,
            client=self.agency,
        )
        self.assertTrue(application.is_operations_completed)
        self.assertEqual(application.charges.count(), 0)

    def test_storekeeper_page_uses_tariff_service_modal(self):
        self.client.force_login(self.store_user)
        response = self.client.get(f"/orders/other/{self.order_id}/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "warehouse-services-modal")
        self.assertNotContains(response, 'name="service_name"')

    def test_manager_can_update_summary_meta(self):
        self.client.force_login(self.manager_user)
        response = self.client.post(
            f"/team-manager/other-requests/{self.order_id}/",
            {
                "action": "update_meta",
                "department": self.obj.department,
                "priority": self.OtherRequest.PRIORITY_URGENT,
                "assignee_id": str(self.storekeeper.id),
                "due_at": "",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.obj.refresh_from_db()
        self.assertEqual(self.obj.priority, self.OtherRequest.PRIORITY_URGENT)
        self.assertEqual(self.obj.assignee_id, self.storekeeper.id)

    def test_manager_cannot_cancel_when_warehouse_took_in_work(self):
        self.client.force_login(self.manager_user)
        response = self.client.get(f"/team-manager/other-requests/{self.order_id}/")
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'name="action" value="cancel"')

        response = self.client.post(
            f"/team-manager/other-requests/{self.order_id}/",
            {"action": "cancel", "reason": "Клиент передумал"},
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Менеджер не может отменить")
        self.obj.refresh_from_db()
        self.assertEqual(self.obj.status, self.OtherRequest.STATUS_IN_PROGRESS)

    def test_list_page_has_category_filter(self):
        self.client.force_login(self.manager_user)
        response = self.client.get("/team-manager/other-requests/", {"category": "measure_size"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.order_id)
        self.assertContains(response, "Все категории")


class TeamManagerLogisticsReferenceTests(TestCase):
    def setUp(self):
        from head_manager.models import Carrier

        self.user = get_user_model().objects.create_user(username="manager_logistics_ref", password="pwd")
        Employee.objects.create(
            full_name="Менеджер Логистика",
            user=self.user,
            role="manager",
            is_active=True,
        )
        self.carrier = Carrier.objects.create(
            name="ООО Тестовый перевозчик",
            inn="7701234567",
            phone="+7 495 000-00-00",
            is_active=True,
        )
        self.client.force_login(self.user)

    def _create_vehicle(self, *, number: str, driver: str, default: bool = False):
        return self.client.post(
            "/team-manager/references/logistics/",
            {
                "action": "save_vehicle",
                "carrier": str(self.carrier.pk),
                "vehicle_name": "Газель",
                "vehicle_number": number,
                "driver_name": driver,
                "driver_phone": "8 (900) 555-44-33",
                "max_weight_kg": "1500",
                "max_volume_m3": "12.5",
                "max_pallets": "8",
                "is_active": "on",
                "is_default": "on" if default else "",
                "comment": "Рабочая машина",
            },
        )

    def test_reference_page_creates_vehicle_without_changing_carrier(self):
        from logistics.models import CarrierVehicle

        page = self.client.get("/team-manager/references/logistics/")
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "Справочники")
        self.assertContains(page, "Логистика")
        self.assertContains(page, "ООО Тестовый перевозчик")
        self.assertContains(page, 'href="/team-manager/references/logistics/"', html=False)

        response = self._create_vehicle(number="a123bc777", driver="Иванов Иван", default=False)

        self.assertEqual(response.status_code, 302)
        vehicle = CarrierVehicle.objects.get(carrier=self.carrier)
        self.assertEqual(vehicle.vehicle_number, "А123ВС777")
        self.assertEqual(vehicle.driver_phone, "+7 900 555-44-33")
        self.assertTrue(vehicle.is_active)
        self.assertTrue(vehicle.is_default)
        self.assertEqual(vehicle.created_by, self.user)
        self.carrier.refresh_from_db()
        self.assertEqual(self.carrier.inn, "7701234567")
        self.assertEqual(self.carrier.phone, "+7 495 000-00-00")

    def test_new_default_and_deactivation_promote_remaining_vehicle(self):
        from logistics.models import CarrierVehicle

        first_response = self._create_vehicle(number="а111аа777", driver="Первый водитель", default=True)
        self.assertEqual(first_response.status_code, 302, first_response.content.decode("utf-8"))
        first = CarrierVehicle.objects.get(vehicle_number="А111АА777")
        second_response = self._create_vehicle(number="а222аа777", driver="Второй водитель", default=True)
        self.assertEqual(second_response.status_code, 302, second_response.content.decode("utf-8"))
        first.refresh_from_db()
        second = CarrierVehicle.objects.get(vehicle_number="А222АА777")
        self.assertFalse(first.is_default)
        self.assertTrue(second.is_default)

        response = self.client.post(
            "/team-manager/references/logistics/",
            {"action": "toggle_active", "vehicle_id": str(second.pk)},
        )

        self.assertEqual(response.status_code, 302)
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertFalse(second.is_active)
        self.assertFalse(second.is_default)
        self.assertTrue(first.is_default)


class TeamManagerDashboardIntegrityTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(username="manager_integrity", password="pwd")
        self.other_user = User.objects.create_user(username="manager_integrity_other", password="pwd")
        self.manager = Employee.objects.create(
            full_name="Менеджер Свой", user=self.user, role="manager", is_active=True
        )
        self.other_manager = Employee.objects.create(
            full_name="Менеджер Чужой", user=self.other_user, role="manager", is_active=True
        )
        self.agency = Agency.objects.create(
            agn_name="Клиент целостности",
            inn="7700123499",
            mened_user_id=self.user.id,
        )

    def _create_receiving_task(self, order_id: str, assignee: Employee):
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="receiving",
            action="status",
            agency=self.agency,
            user=self.user,
            description="Заявка отправлена менеджеру",
            payload={"status": "sent_unconfirmed", "status_label": "Ждет подтверждения"},
        )
        return Task.objects.create(
            title=f"Заявка на приемку №{order_id}",
            route=f"/orders/receiving/{order_id}/",
            assigned_to=assignee,
            status="backlog",
        )

    def test_workdesk_defaults_to_all_operational_tasks(self):
        self._create_receiving_task("PR-SCOPE-OWN", self.manager)
        self._create_receiving_task("PR-SCOPE-OTHER", self.other_manager)
        self._create_receiving_task("PR-SCOPE-FREE", None)
        self.client.force_login(self.user)

        default_response = self.client.get("/team-manager/")

        self.assertEqual(default_response.status_code, 200)
        self.assertEqual(default_response.context["manager_section"], "desk")
        self.assertEqual(default_response.context["manager_filters"]["scope"], "all")
        self.assertEqual(default_response.context["manager_task_total"], 3)
        self.assertContains(default_response, "Рабочий стол")

        response = self.client.get("/team-manager/", {"section": "tasks"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["manager_filters"]["scope"], "all")
        self.assertEqual(response.context["manager_task_total"], 3)

    def test_workdesk_today_uses_task_submission_time_without_due_date(self):
        task = self._create_receiving_task("PR-TODAY", self.manager)
        Task.objects.filter(pk=task.pk).update(
            created_at=timezone.now() - timedelta(days=5),
        )
        self.client.force_login(self.user)

        response = self.client.get(
            "/team-manager/",
            {"section": "desk", "focus": "today"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["manager_stats"]["today"], 1)
        self.assertEqual(response.context["manager_task_filtered_total"], 1)
        self.assertEqual(
            response.context["manager_task_rows"][0]["submitted_at"].date(),
            timezone.localdate(),
        )
        self.assertContains(response, "PR-TODAY")

    def test_workdesk_hides_billing_rows_but_tasks_registry_keeps_them(self):
        from unittest.mock import patch

        self._create_receiving_task("PR-BILLING", self.manager)
        self.client.force_login(self.user)

        with patch("teammanager.views._next_step", return_value="Рассчитать начисления"):
            desk = self.client.get("/team-manager/", {"section": "desk"})
            tasks = self.client.get("/team-manager/", {"section": "tasks"})

        self.assertEqual(desk.status_code, 200)
        self.assertEqual(desk.context["manager_task_total"], 0)
        self.assertEqual(desk.context["manager_billing_attention_count"], 1)
        self.assertEqual(desk.context["manager_task_rows"], [])
        self.assertContains(desk, "Открыть биллинг")
        self.assertEqual(tasks.status_code, 200)
        self.assertEqual(tasks.context["manager_task_total"], 1)
        self.assertContains(tasks, "PR-BILLING")

    def test_empty_technical_status_does_not_override_real_status(self):
        from teammanager.views import _is_manager_status_source

        entry = OrderAuditEntry.objects.create(
            order_id="PR-EMPTY-STATUS",
            order_type="receiving",
            action="status",
            agency=self.agency,
            user=self.user,
            description="Техническое событие",
            payload={"description": "Обновлены служебные данные"},
        )

        self.assertFalse(_is_manager_status_source(entry))

    def test_processing_in_work_belongs_to_warehouse_bucket(self):
        from teammanager.views import _quick_entry_status

        entry = OrderAuditEntry.objects.create(
            order_id="OBR-WAREHOUSE",
            order_type="processing",
            action="status",
            agency=self.agency,
            user=self.user,
            description="Склад начал обработку",
            payload={"status": "processing_in_work", "status_label": "Открыта раскоробовка"},
        )

        status = _quick_entry_status(entry)
        self.assertEqual(status["bucket"], "warehouse")
        self.assertEqual(status["filter_status"], "processing")
