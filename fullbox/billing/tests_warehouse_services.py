"""Факты услуг кладовщика → биллинг без цен на складе."""
import json
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import Client, TestCase
from django.utils import timezone

from audit.models import OrderAuditEntry
from accountant.models import ClientLifecycle
from employees.models import Employee
from logistics.models import LogisticsExternalTrip, LogisticsTrip, LogisticsTripOrder
from shipping.models import ShippingOrder
from sklad.models import WarehouseContainer, WarehouseEvent
from sku.models import Agency

from billing.calculators.engine import calculate_application
from billing.models import (
    BillingApplication,
    BillingStaffNotification,
    BillingService,
    ClientTariffItem,
    ClientTariffVersion,
    TariffCategory,
    TariffUnit,
    WarehouseServiceFact,
)
from billing.service_catalog import ensure_service_catalog
from billing.statuses import BillingStatus
from billing.warehouse_services import (
    candidates_from_warehouse_facts,
    link_facts_to_charges,
    list_agreed_services_for_storekeeper,
    list_facts,
    require_logistics_trip_completion_facts,
    require_completion_facts,
    replace_warehouse_facts,
    review_unlisted_warehouse_fact,
    resolve_canonical_warehouse_order,
    shipping_pick_auto_quantities,
    sync_processing_facts_to_billing,
    sync_receiving_facts_to_billing,
    sync_shipping_facts_to_billing,
    sync_logistics_trip_facts_to_billing,
    sync_other_request_facts_to_billing,
)


User = get_user_model()


class WarehouseServiceFactsTests(TestCase):
    def setUp(self):
        ensure_service_catalog()
        self.user = User.objects.create_user(username="wsf_storekeeper", password="pwd")
        Employee.objects.create(user=self.user, full_name="Кладовщик WSF", role="storekeeper", is_active=True)
        self.client_agency = Agency.objects.create(agn_name="Клиент WSF")
        self.service = BillingService.objects.filter(code="receiving_goods").first()
        self.assertIsNotNone(self.service)
        self.category, _ = TariffCategory.objects.get_or_create(
            code="receiving",
            defaults={"name": "Приемка", "sort_order": 10, "is_active": True},
        )
        self.unit, _ = TariffUnit.objects.get_or_create(
            code="pcs",
            defaults={"name": "Штука", "short_name": "шт", "is_active": True},
        )
        self.version = ClientTariffVersion.objects.create(
            client=self.client_agency,
            name="Тариф WSF",
            version_number=1,
            status=ClientTariffVersion.STATUS_ACTIVE,
            valid_from=timezone.localdate(),
        )
        # Temporarily allow item create on active version via draft->activate pattern:
        # ClientTariffItem.clean blocks non-draft; create as draft then activate.
        self.version.status = ClientTariffVersion.STATUS_DRAFT
        self.version.save(update_fields=["status"])
        ClientTariffItem.objects.create(
            tariff_version=self.version,
            category=self.category,
            service=self.service,
            service_name=self.service.name,
            unit=self.unit,
            price=Decimal("6.0000"),
            is_active=True,
        )
        self.version.status = ClientTariffVersion.STATUS_ACTIVE
        self.version.save(update_fields=["status"])

    def _audit_order(self, order_id, *, order_type="receiving", agency=None):
        return OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type=order_type,
            action="create",
            agency=agency or self.client_agency,
            user=self.user,
            payload={"status": "created"},
        )

    def _agree_processing_service(self) -> BillingService:
        service, _created = BillingService.objects.get_or_create(
            code="processing_phase2_test",
            defaults={"name": "Тестовая обработка", "unit": "шт", "is_active": True},
        )
        category, _created = TariffCategory.objects.get_or_create(
            code="processing",
            defaults={"name": "Обработка", "sort_order": 30, "is_active": True},
        )
        self.version.status = ClientTariffVersion.STATUS_DRAFT
        self.version.save(update_fields=["status"])
        ClientTariffItem.objects.get_or_create(
            tariff_version=self.version,
            service=service,
            defaults={
                "category": category,
                "service_name": service.name,
                "unit": self.unit,
                "price": Decimal("10.0000"),
                "is_active": True,
            },
        )
        self.version.status = ClientTariffVersion.STATUS_ACTIVE
        self.version.save(update_fields=["status"])
        return service

    def _agree_shipping_service(self) -> BillingService:
        service, _created = BillingService.objects.get_or_create(
            code="shipping_phase3_test",
            defaults={"name": "Тестовая отгрузка", "unit": "шт", "is_active": True},
        )
        category, _created = TariffCategory.objects.get_or_create(
            code="shipping",
            defaults={"name": "Отгрузка", "sort_order": 80, "is_active": True},
        )
        self.version.status = ClientTariffVersion.STATUS_DRAFT
        self.version.save(update_fields=["status"])
        ClientTariffItem.objects.get_or_create(
            tariff_version=self.version,
            service=service,
            defaults={
                "category": category,
                "service_name": service.name,
                "unit": self.unit,
                "price": Decimal("12.0000"),
                "is_active": True,
            },
        )
        self.version.status = ClientTariffVersion.STATUS_ACTIVE
        self.version.save(update_fields=["status"])
        return service

    def _agree_logistics_service(self) -> BillingService:
        service = BillingService.objects.get(code="logistics_external_trip")
        category, _created = TariffCategory.objects.get_or_create(
            code="logistics",
            defaults={"name": "Логистика", "sort_order": 110, "is_active": True},
        )
        self.version.status = ClientTariffVersion.STATUS_DRAFT
        self.version.save(update_fields=["status"])
        ClientTariffItem.objects.get_or_create(
            tariff_version=self.version,
            service=service,
            defaults={
                "category": category,
                "service_name": service.name,
                "unit": self.unit,
                "price": Decimal("500.0000"),
                "is_active": True,
            },
        )
        self.version.status = ClientTariffVersion.STATUS_ACTIVE
        self.version.save(update_fields=["status"])
        return service

    def _agree_other_service(self) -> BillingService:
        service = BillingService.objects.get(code="extra_warehouse_operation")
        category, _created = TariffCategory.objects.get_or_create(
            code="extra",
            defaults={"name": "Дополнительные услуги", "sort_order": 120, "is_active": True},
        )
        self.version.status = ClientTariffVersion.STATUS_DRAFT
        self.version.save(update_fields=["status"])
        ClientTariffItem.objects.get_or_create(
            tariff_version=self.version,
            service=service,
            defaults={
                "category": category,
                "service_name": service.name,
                "unit": self.unit,
                "price": Decimal("25.0000"),
                "is_active": True,
            },
        )
        self.version.status = ClientTariffVersion.STATUS_ACTIVE
        self.version.save(update_fields=["status"])
        return service

    def test_catalog_has_no_prices(self):
        rows = list_agreed_services_for_storekeeper(self.client_agency, process_type="receiving")
        self.assertTrue(rows)
        row = next(r for r in rows if r["code"] == "receiving_goods")
        self.assertEqual(row["name"], self.service.name)
        self.assertNotIn("price", row)
        self.assertNotIn("tariff", row)
        blob = str(row)
        self.assertNotIn("6.0000", blob)

    def test_replace_facts_and_calculate_uses_facts(self):
        self._audit_order("RCV-WSF-1")
        facts = replace_warehouse_facts(
            client=self.client_agency,
            order_type="receiving",
            order_id="RCV-WSF-1",
            lines=[{"service_id": self.service.id, "quantity": "12"}],
            user=self.user,
        )
        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0].quantity, Decimal("12"))

        app = BillingApplication.objects.create(
            application_type=BillingApplication.TYPE_RECEIVING,
            application_id="RCV-WSF-1",
            client=self.client_agency,
            legal_entity=self.client_agency,
            operational_status="done",
            is_operations_completed=True,
            operations_completed_at=timezone.now(),
            billing_status=BillingStatus.NOT_CALCULATED,
            created_at_source=timezone.now(),
            source_payload={"items": [{"qty": 999}]},
        )
        result = calculate_application(app, user=self.user)
        self.assertTrue(result.get("from_warehouse_facts"))
        charge = app.charges.get(service=self.service)
        self.assertEqual(charge.quantity, Decimal("12"))
        self.assertEqual(charge.tariff, Decimal("6.0000"))
        fact = WarehouseServiceFact.objects.get(pk=facts[0].pk)
        self.assertEqual(fact.charge_id, charge.id)

    def test_completion_requires_positive_receiving_fact(self):
        self._audit_order("RCV-WSF-REQUIRED")

        with self.assertRaisesMessage(ValidationError, "Перед завершением"):
            require_completion_facts(
                client=self.client_agency,
                order_type="receiving",
                order_id="RCV-WSF-REQUIRED",
            )

        saved = replace_warehouse_facts(
            client=self.client_agency,
            order_type="receiving",
            order_id="RCV-WSF-REQUIRED",
            lines=[{"service_id": self.service.id, "quantity": "4"}],
            user=self.user,
        )
        self.assertEqual(
            require_completion_facts(
                client=self.client_agency,
                order_type="receiving",
                order_id="RCV-WSF-REQUIRED",
            ),
            saved,
        )

    def test_plan_fact_difference_requires_reason(self):
        self._audit_order("RCV-WSF-DIFFERENCE")

        with self.assertRaisesMessage(ValidationError, "причину расхождения"):
            replace_warehouse_facts(
                client=self.client_agency,
                order_type="receiving",
                order_id="RCV-WSF-DIFFERENCE",
                lines=[
                    {
                        "service_id": self.service.id,
                        "planned_quantity": "5",
                        "quantity": "4",
                    }
                ],
                user=self.user,
            )

    def test_processing_plan_fact_difference_requires_reason(self):
        service = self._agree_processing_service()
        self._audit_order("PROC-WSF-DIFFERENCE", order_type="processing")

        with self.assertRaisesMessage(ValidationError, "причину расхождения"):
            replace_warehouse_facts(
                client=self.client_agency,
                order_type="processing",
                order_id="PROC-WSF-DIFFERENCE",
                lines=[
                    {
                        "service_id": service.id,
                        "planned_quantity": "5",
                        "auto_quantity": "4",
                        "quantity": "4",
                        "source": "processing_auto",
                    }
                ],
                user=self.user,
            )

    def test_processing_close_requires_facts_and_notifies_manager_without_calculating(self):
        manager_user = User.objects.create_user(username="wsf_processing_manager", password="pwd")
        manager = Employee.objects.create(
            user=manager_user,
            full_name="Менеджер обработки WSF",
            role="manager",
            is_active=True,
        )
        self.client_agency.mened_user_id = manager_user.id
        self.client_agency.save(update_fields=["mened_user_id"])
        self._audit_order("PROC-WSF-EMPTY", order_type="processing")

        with self.assertRaisesMessage(ValidationError, "Перед завершением"):
            sync_processing_facts_to_billing(
                client=self.client_agency,
                order_id="PROC-WSF-EMPTY",
                user=self.user,
            )

        service = self._agree_processing_service()
        audit_entry = self._audit_order("PROC-WSF-SYNC", order_type="processing")
        facts = replace_warehouse_facts(
            client=self.client_agency,
            order_type="processing",
            order_id="PROC-WSF-SYNC",
            lines=[
                {
                    "service_id": service.id,
                    "planned_quantity": "7",
                    "auto_quantity": "7",
                    "quantity": "7",
                    "source": "processing_auto",
                }
            ],
            user=self.user,
        )

        application, synced = sync_processing_facts_to_billing(
            client=self.client_agency,
            order_id="PROC-WSF-SYNC",
            completed_at=timezone.now(),
            user=self.user,
            source_payload={"processed_qty": 7, "boxes_count": 1},
        )

        self.assertEqual(application.application_type, BillingApplication.TYPE_PROCESSING)
        self.assertEqual(application.manager_id, manager.id)
        self.assertEqual(application.created_at_source, audit_entry.created_at)
        self.assertTrue(application.is_operations_completed)
        self.assertEqual(application.charges.count(), 0)
        self.assertEqual([fact.pk for fact in synced], [fact.pk for fact in facts])
        facts[0].refresh_from_db()
        self.assertEqual(facts[0].application_id, application.id)
        notification = BillingStaffNotification.objects.get(recipient=manager_user)
        self.assertIn("PROC-WSF-SYNC", notification.title)
        self.assertEqual(notification.link_url, f"/team-manager/billing/applications/{application.pk}/")

        repeated_application, repeated_facts = sync_processing_facts_to_billing(
            client=self.client_agency,
            order_id="PROC-WSF-SYNC",
            completed_at=timezone.now(),
            user=self.user,
        )
        self.assertEqual(repeated_application.pk, application.pk)
        self.assertEqual([fact.pk for fact in repeated_facts], [fact.pk for fact in facts])
        self.assertEqual(repeated_application.charges.count(), 0)
        self.assertEqual(BillingStaffNotification.objects.filter(recipient=manager_user).count(), 1)

    def test_receiving_close_syncs_facts_and_notifies_manager_without_calculating(self):
        manager_user = User.objects.create_user(username="wsf_receiving_manager", password="pwd")
        manager = Employee.objects.create(
            user=manager_user,
            full_name="Менеджер приемки WSF",
            role="manager",
            is_active=True,
        )
        self.client_agency.mened_user_id = manager_user.id
        self.client_agency.save(update_fields=["mened_user_id"])
        audit_entry = self._audit_order("RCV-WSF-SYNC")
        facts = replace_warehouse_facts(
            client=self.client_agency,
            order_type="receiving",
            order_id="RCV-WSF-SYNC",
            lines=[{"service_id": self.service.id, "quantity": "7"}],
            user=self.user,
        )

        application, synced = sync_receiving_facts_to_billing(
            client=self.client_agency,
            order_id="RCV-WSF-SYNC",
            completed_at=timezone.now(),
            user=self.user,
            source_payload={"box_count": 2, "pallet_count": 1},
        )

        self.assertEqual(application.application_type, BillingApplication.TYPE_RECEIVING)
        self.assertEqual(application.manager_id, manager.id)
        self.assertEqual(application.created_at_source, audit_entry.created_at)
        self.assertTrue(application.is_operations_completed)
        self.assertEqual(application.charges.count(), 0)
        self.assertEqual([fact.pk for fact in synced], [fact.pk for fact in facts])
        facts[0].refresh_from_db()
        self.assertEqual(facts[0].application_id, application.id)
        notification = BillingStaffNotification.objects.get(recipient=manager_user)
        self.assertIn("RCV-WSF-SYNC", notification.title)
        self.assertEqual(notification.link_url, f"/team-manager/billing/applications/{application.pk}/")

    def test_shipping_close_requires_facts_and_notifies_manager_without_calculating(self):
        manager_user = User.objects.create_user(username="wsf_shipping_manager", password="pwd")
        manager = Employee.objects.create(
            user=manager_user,
            full_name="Менеджер отгрузки WSF",
            role="manager",
            is_active=True,
        )
        self.client_agency.mened_user_id = manager_user.id
        self.client_agency.save(update_fields=["mened_user_id"])
        empty_order = ShippingOrder.objects.create(
            number="OTG-WSF-EMPTY",
            agency=self.client_agency,
            created_by=self.user,
            status=ShippingOrder.STATUS_PICKING,
        )

        with self.assertRaisesMessage(ValidationError, "Перед завершением"):
            sync_shipping_facts_to_billing(
                client=self.client_agency,
                order_id=empty_order.number,
                user=self.user,
            )

        service = self._agree_shipping_service()
        order = ShippingOrder.objects.create(
            number="OTG-WSF-SYNC",
            agency=self.client_agency,
            created_by=self.user,
            status=ShippingOrder.STATUS_PICKING,
        )
        with self.assertRaisesMessage(ValidationError, "причину расхождения"):
            replace_warehouse_facts(
                client=self.client_agency,
                order_type="shipping",
                order_id=order.number,
                lines=[
                    {
                        "service_id": service.id,
                        "planned_quantity": "8",
                        "quantity": "7",
                        "source": "shipping_manual",
                    }
                ],
                user=self.user,
            )
        facts = replace_warehouse_facts(
            client=self.client_agency,
            order_type="shipping",
            order_id=order.number,
            lines=[
                {
                    "service_id": service.id,
                    "planned_quantity": "8",
                    "quantity": "7",
                    "discrepancy_reason": "Один короб исключен после проверки",
                    "source": "shipping_manual",
                }
            ],
            user=self.user,
        )

        application, synced = sync_shipping_facts_to_billing(
            client=self.client_agency,
            order_id=order.number,
            completed_at=timezone.now(),
            user=self.user,
            source_payload={"items_count": 70, "box_count": 7, "pallet_count": 2},
        )

        self.assertEqual(application.application_type, BillingApplication.TYPE_SHIPPING)
        self.assertEqual(application.manager_id, manager.id)
        self.assertEqual(application.created_at_source, order.created_at)
        self.assertTrue(application.is_operations_completed)
        self.assertEqual(application.charges.count(), 0)
        self.assertEqual(application.source_payload["box_count"], 7)
        self.assertEqual([fact.pk for fact in synced], [fact.pk for fact in facts])
        facts[0].refresh_from_db()
        self.assertEqual(facts[0].application_id, application.id)
        notification = BillingStaffNotification.objects.get(recipient=manager_user)
        self.assertIn(order.number, notification.title)
        self.assertEqual(notification.link_url, f"/team-manager/billing/applications/{application.pk}/")

    def test_logistics_trip_requires_facts_and_syncs_without_calculating(self):
        manager_user = User.objects.create_user(username="wsf_logistics_manager", password="pwd")
        manager = Employee.objects.create(
            user=manager_user,
            full_name="Менеджер логистики WSF",
            role="manager",
            is_active=True,
        )
        self.client_agency.mened_user_id = manager_user.id
        self.client_agency.save(update_fields=["mened_user_id"])
        order = ShippingOrder.objects.create(
            number="OTG-WSF-TRIP",
            agency=self.client_agency,
            created_by=self.user,
            status=ShippingOrder.STATUS_PACKED,
            expected_boxes=5,
        )
        trip = LogisticsTrip.objects.create(
            number="TRIP-WSF-BILLING",
            status=LogisticsTrip.STATUS_COMPLETED,
            closed_at=timezone.now(),
            created_by=self.user,
        )
        LogisticsTripOrder.objects.create(trip=trip, shipping_order=order)

        with self.assertRaisesMessage(ValidationError, "Перед завершением рейса"):
            require_logistics_trip_completion_facts(trip)

        service = self._agree_logistics_service()
        facts = replace_warehouse_facts(
            client=self.client_agency,
            order_type="logistics",
            order_id=trip.number,
            lines=[{"service_id": service.id, "quantity": "1", "source": "logistics_manual"}],
            user=self.user,
        )
        applications = sync_logistics_trip_facts_to_billing(trip=trip, user=self.user)

        self.assertEqual(len(applications), 1)
        application = applications[0]
        self.assertEqual(application.application_type, BillingApplication.TYPE_LOGISTICS)
        self.assertEqual(application.manager_id, manager.id)
        self.assertTrue(application.is_operations_completed)
        self.assertEqual(application.charges.count(), 0)
        self.assertEqual(application.source_payload["order_numbers"], [order.number])
        facts[0].refresh_from_db()
        self.assertEqual(facts[0].application_id, application.id)
        notification = BillingStaffNotification.objects.get(recipient=manager_user)
        self.assertIn(trip.number, notification.title)

    def test_completed_external_trip_uses_saved_facts_without_auto_charge(self):
        from billing.external_trip import sync_completed_external_trip

        service = self._agree_logistics_service()
        trip = LogisticsTrip.objects.create(
            number="TRIP-WSF-EXTERNAL",
            trip_kind=LogisticsTrip.KIND_EXTERNAL,
            status=LogisticsTrip.STATUS_COMPLETED,
            driver_status=LogisticsTrip.DRIVER_STATUS_DELIVERED,
            closed_at=timezone.now(),
            created_by=self.user,
        )
        LogisticsExternalTrip.objects.create(
            trip=trip,
            client=self.client_agency,
            pickup_address="Точка А",
            delivery_address="Точка Б",
        )
        replace_warehouse_facts(
            client=self.client_agency,
            order_type="logistics",
            order_id=trip.number,
            lines=[{"service_id": service.id, "quantity": "1", "source": "logistics_manual"}],
            user=self.user,
        )

        first = sync_completed_external_trip(trip, user=self.user)
        second = sync_completed_external_trip(trip.pk, user=self.user)

        self.assertEqual(first.pk, second.pk)
        self.assertEqual(first.charges.count(), 0)
        self.assertTrue(first.is_operations_completed)

    def test_driver_can_save_logistics_facts_without_receiving_prices(self):
        driver_user = User.objects.create_user(username="wsf_driver", password="pwd")
        driver = Employee.objects.create(
            user=driver_user,
            full_name="Водитель WSF",
            role="driver",
            is_active=True,
        )
        service = self._agree_logistics_service()
        trip = LogisticsTrip.objects.create(
            number="TRIP-WSF-DRIVER-API",
            status=LogisticsTrip.STATUS_DEPARTED,
            assigned_driver=driver,
            created_by=self.user,
        )
        order = ShippingOrder.objects.create(
            number="OTG-WSF-DRIVER-API",
            agency=self.client_agency,
            created_by=self.user,
            status=ShippingOrder.STATUS_PACKED,
        )
        LogisticsTripOrder.objects.create(trip=trip, shipping_order=order)
        http = Client()
        http.force_login(driver_user)

        catalog = http.get(
            f"/api/warehouse-billing/catalog/?client={self.client_agency.pk}"
            f"&process=logistics&order_id={trip.number}"
        )
        saved = http.put(
            "/api/warehouse-billing/facts/",
            data=json.dumps(
                {
                    "client_id": self.client_agency.pk,
                    "order_type": "logistics",
                    "order_id": trip.number,
                    "lines": [{"service_id": service.pk, "quantity": "1"}],
                }
            ),
            content_type="application/json",
        )

        self.assertEqual(catalog.status_code, 200, catalog.content)
        self.assertEqual(saved.status_code, 200, saved.content)
        self.assertEqual(saved.json()["data"]["saved"], 1)
        self.assertNotIn("price", str(catalog.json()))

    def test_other_request_facts_sync_without_automatic_charges(self):
        from client_cabinet.models import OtherRequest

        manager_user = User.objects.create_user(username="wsf_other_manager", password="pwd")
        Employee.objects.create(
            user=manager_user,
            full_name="Менеджер другой заявки",
            role="manager",
            is_active=True,
        )
        self.client_agency.mened_user_id = manager_user.id
        self.client_agency.save(update_fields=["mened_user_id"])
        service = self._agree_other_service()
        request_obj = OtherRequest.objects.create(
            public_number="OTH-WSF-1",
            agency=self.client_agency,
            title="Проверить товар",
            description="Проверить товар и сделать фото",
            status=OtherRequest.STATUS_CLOSED,
            created_by=self.user,
            closed_at=timezone.now(),
        )
        catalog = list_agreed_services_for_storekeeper(
            self.client_agency,
            process_type="other",
        )
        self.assertIn(service.id, {row["service_id"] for row in catalog})
        saved = replace_warehouse_facts(
            client=self.client_agency,
            order_type="other",
            order_id=request_obj.public_number,
            lines=[{"service_id": service.id, "quantity": "3", "source": "other_manual"}],
            user=self.user,
        )

        application, facts = sync_other_request_facts_to_billing(
            request_obj=request_obj,
            user=self.user,
        )

        self.assertEqual(application.application_type, BillingApplication.TYPE_OTHER)
        self.assertTrue(application.is_operations_completed)
        self.assertEqual(application.charges.count(), 0)
        self.assertEqual([fact.pk for fact in facts], [saved[0].pk])
        saved[0].refresh_from_db()
        self.assertEqual(saved[0].application_id, application.pk)
        self.assertTrue(
            BillingStaffNotification.objects.filter(
                recipient=manager_user,
                source_key__startswith="warehouse-facts:other:"
            ).exists()
        )

    def test_other_request_resolver_rejects_foreign_client(self):
        from client_cabinet.models import OtherRequest

        foreign_client = Agency.objects.create(agn_name="Чужой клиент другой заявки")
        request_obj = OtherRequest.objects.create(
            public_number="OTH-WSF-FOREIGN",
            agency=foreign_client,
            status=OtherRequest.STATUS_IN_PROGRESS,
            created_by=self.user,
        )

        with self.assertRaisesMessage(ValidationError, "Заявка клиента не найдена."):
            resolve_canonical_warehouse_order(
                client=self.client_agency,
                order_type="other",
                order_id=request_obj.public_number,
            )

    def test_rejects_service_outside_tariff(self):
        self._audit_order("RCV-WSF-2")
        other, _ = BillingService.objects.get_or_create(
            code="wsf_not_agreed",
            defaults={"name": "Несогласованная тестовая услуга", "unit": "шт"},
        )
        with self.assertRaises(ValidationError):
            replace_warehouse_facts(
                client=self.client_agency,
                order_type="receiving",
                order_id="RCV-WSF-2",
                lines=[{"service_id": other.id, "quantity": "1"}],
                user=self.user,
            )

    def test_api_catalog_and_facts_for_storekeeper(self):
        self._audit_order("RCV-WSF-API")
        http = Client()
        http.force_login(self.user)
        cat = http.get(
            f"/api/warehouse-billing/catalog/?client={self.client_agency.id}&process=receiving"
        )
        self.assertEqual(cat.status_code, 200)
        body = cat.json()
        self.assertTrue(body["ok"])
        self.assertTrue(body["data"]["services"])
        self.assertNotIn("price", str(body["data"]["services"]))

        save = http.put(
            "/api/warehouse-billing/facts/",
            data=json.dumps(
                {
                    "client_id": self.client_agency.id,
                    "order_type": "receiving",
                    "order_id": "RCV-WSF-API",
                    "lines": [{"service_id": self.service.id, "quantity": "3", "tariff": "999"}],
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(save.status_code, 200)
        self.assertTrue(save.json()["ok"])
        fact = WarehouseServiceFact.objects.get(order_id="RCV-WSF-API")
        self.assertEqual(fact.quantity, Decimal("3"))
        # Цена из запроса не должна попасть в модель.
        self.assertFalse(hasattr(fact, "tariff"))

    def test_rejects_missing_and_foreign_client_orders_without_creating_facts(self):
        foreign_client = Agency.objects.create(agn_name="Чужой клиент WSF")
        self._audit_order("RCV-FOREIGN", agency=foreign_client)

        for order_id in ("RCV-MISSING", "RCV-FOREIGN"):
            with self.subTest(order_id=order_id), self.assertRaisesMessage(
                ValidationError,
                "Заявка клиента не найдена.",
            ):
                replace_warehouse_facts(
                    client=self.client_agency,
                    order_type="receiving",
                    order_id=order_id,
                    lines=[{"service_id": self.service.id, "quantity": "1"}],
                    user=self.user,
                )
        self.assertFalse(WarehouseServiceFact.objects.filter(order_id__in={"RCV-MISSING", "RCV-FOREIGN"}).exists())

    def test_shipping_resolver_requires_shipping_order_owner(self):
        foreign_client = Agency.objects.create(agn_name="Чужой клиент отгрузки WSF")
        ShippingOrder.objects.create(number="OTG-WSF-1", agency=foreign_client, created_by=self.user)

        with self.assertRaisesMessage(ValidationError, "Заявка клиента не найдена."):
            resolve_canonical_warehouse_order(
                client=self.client_agency,
                order_type="shipping",
                order_id="OTG-WSF-1",
            )
        self.assertEqual(
            resolve_canonical_warehouse_order(
                client=foreign_client,
                order_type="shipping",
                order_id="OTG-WSF-1",
            ),
            (WarehouseServiceFact.ORDER_SHIPPING, "OTG-WSF-1"),
        )

    def test_ambiguous_audit_ownership_is_blocked_for_both_clients(self):
        foreign_client = Agency.objects.create(agn_name="Второй клиент WSF")
        self._audit_order("RCV-AMBIGUOUS")
        self._audit_order("RCV-AMBIGUOUS", agency=foreign_client)

        for agency in (self.client_agency, foreign_client):
            with self.subTest(agency=agency.pk), self.assertRaisesMessage(
                ValidationError,
                "Заявка клиента не найдена.",
            ):
                resolve_canonical_warehouse_order(
                    client=agency,
                    order_type="receiving",
                    order_id="RCV-AMBIGUOUS",
                )

    def test_cross_type_number_does_not_supply_facts_to_other_application(self):
        self._audit_order("WSF-SAME", order_type="receiving")
        self._audit_order("WSF-SAME", order_type="processing")
        fact = WarehouseServiceFact.objects.create(
            client=self.client_agency,
            order_type=WarehouseServiceFact.ORDER_RECEIVING,
            order_id="WSF-SAME",
            service=self.service,
            service_name_snapshot=self.service.name,
            quantity=Decimal("2"),
            unit="шт",
            status=WarehouseServiceFact.STATUS_SENT_TO_BILLING,
        )
        processing_app = BillingApplication.objects.create(
            application_type=BillingApplication.TYPE_PROCESSING,
            application_id="WSF-SAME",
            client=self.client_agency,
            legal_entity=self.client_agency,
            created_at_source=timezone.now(),
        )

        self.assertIsNone(candidates_from_warehouse_facts(processing_app))
        self.assertEqual(link_facts_to_charges(processing_app), 0)
        fact.refresh_from_db()
        self.assertIsNone(fact.application_id)
        self.assertEqual(list_facts(client=self.client_agency, order_type="receiving", order_id="WSF-SAME"), [fact])

    def test_non_warehouse_application_has_no_warehouse_facts(self):
        application = BillingApplication.objects.create(
            application_type=BillingApplication.TYPE_OTHER,
            application_id="OTHER-WSF-1",
            client=self.client_agency,
            legal_entity=self.client_agency,
            created_at_source=timezone.now(),
        )

        self.assertIsNone(candidates_from_warehouse_facts(application))
        self.assertEqual(link_facts_to_charges(application), 0)

    def test_existing_cross_type_application_link_blocks_editing(self):
        self._audit_order("WSF-CONFLICT", order_type="processing")
        wrong_app = BillingApplication.objects.create(
            application_type=BillingApplication.TYPE_RECEIVING,
            application_id="WSF-CONFLICT",
            client=self.client_agency,
            legal_entity=self.client_agency,
            created_at_source=timezone.now(),
        )
        fact = WarehouseServiceFact.objects.create(
            client=self.client_agency,
            order_type=WarehouseServiceFact.ORDER_PROCESSING,
            order_id="WSF-CONFLICT",
            service=self.service,
            service_name_snapshot=self.service.name,
            quantity=Decimal("1"),
            unit="шт",
            application=wrong_app,
        )

        with self.assertRaisesMessage(ValidationError, "Редактирование заблокировано"):
            replace_warehouse_facts(
                client=self.client_agency,
                order_type="processing",
                order_id="WSF-CONFLICT",
                lines=[],
                user=self.user,
            )
        fact.refresh_from_db()
        self.assertEqual(fact.application_id, wrong_app.id)
        self.assertEqual(fact.quantity, Decimal("1"))

    def test_manager_cannot_use_client_outside_portfolio(self):
        manager_user = User.objects.create_user(username="wsf_manager", password="pwd")
        Employee.objects.create(user=manager_user, full_name="Менеджер WSF", role="manager", is_active=True)
        self._audit_order("RCV-MANAGER-DENIED")
        http = Client()
        http.force_login(manager_user)

        response = http.get(
            "/api/warehouse-billing/facts/",
            {
                "client": self.client_agency.id,
                "order_type": "receiving",
                "order_id": "RCV-MANAGER-DENIED",
            },
        )

        self.assertEqual(response.status_code, 403)
        self.assertFalse(response.json()["ok"])

    def _agree_shipping_pick_services(self) -> dict[str, BillingService]:
        services_by_code = {
            "shipping_pick_box_1_15kg": "Сборка короба по листу подбора, от 1 до 15 кг",
            "shipping_pick_box_15_30kg": "Сборка короба по листу подбора, от 15 до 30 кг",
            "shipping_pick_box_30_50kg": "Сборка короба по листу подбора, от 30 до 50 кг",
            "shipping_pick_storage_item": "Сборка товара по листу подбора с хранения, 1 ед.",
        }
        category, _created = TariffCategory.objects.get_or_create(
            code="shipping",
            defaults={"name": "Отгрузка", "sort_order": 80, "is_active": True},
        )
        self.version.status = ClientTariffVersion.STATUS_DRAFT
        self.version.save(update_fields=["status"])
        services = {}
        for code, name in services_by_code.items():
            service, _created = BillingService.objects.get_or_create(
                code=code,
                defaults={"name": name, "unit": "шт", "is_active": True},
            )
            ClientTariffItem.objects.get_or_create(
                tariff_version=self.version,
                service=service,
                defaults={
                    "category": category,
                    "service_name": service.name,
                    "unit": self.unit,
                    "price": Decimal("10.0000"),
                    "is_active": True,
                },
            )
            services[code] = service
        self.version.status = ClientTariffVersion.STATUS_ACTIVE
        self.version.save(update_fields=["status"])
        return services

    def _shipping_pick_event(self, order, *, container=None, qty=1, payload=None):
        return WarehouseEvent.objects.create(
            agency=self.client_agency,
            event_type="otg_arrived",
            stock_context_type="shipping",
            stock_context_id=order.number,
            container=container,
            qty=qty,
            payload=payload or {},
            occurred_at=timezone.now(),
        )

    def test_shipping_pick_auto_quantities_use_unique_boxes_and_piece_events(self):
        self._agree_shipping_pick_services()
        order = ShippingOrder.objects.create(
            number="OTG-WSF-AUTO-COUNT",
            agency=self.client_agency,
            created_by=self.user,
        )
        weights = (15_000, 30_000, 50_000, 50_001, None)
        boxes = []
        for index, weight in enumerate(weights, start=1):
            box = WarehouseContainer.objects.create(
                agency=self.client_agency,
                container_type=WarehouseContainer.TYPE_BOX,
                container_code=f"AUTO-BOX-{index}",
                gross_weight_g=weight,
            )
            boxes.append(box)
            self._shipping_pick_event(
                order,
                container=box,
                payload={"whole_box_shipping_pick": True, "box_code": box.container_code},
            )
        # Повтор того же подтвержденного события и обычное событие перемещения
        # не должны повторно начислять подбор короба.
        self._shipping_pick_event(
            order,
            container=boxes[0],
            payload={"whole_box_shipping_pick": True, "box_code": boxes[0].container_code},
        )
        self._shipping_pick_event(order, container=boxes[0], payload={"box_code": boxes[0].container_code})
        self._shipping_pick_event(order, qty=4, payload={"partial_shipping_pick": True})
        self._shipping_pick_event(order, qty=3, payload={"partial_shipping_pick": True})

        result = shipping_pick_auto_quantities(client=self.client_agency, order_id=order.number)

        self.assertEqual(result["total_boxes"], 5)
        self.assertEqual(result["classified_boxes"], 3)
        self.assertEqual(result["unclassified_boxes"], 2)
        self.assertEqual(result["piece_quantity"], 7)
        self.assertEqual(result["quantities"]["shipping_pick_box_1_15kg"], 1)
        self.assertEqual(result["quantities"]["shipping_pick_box_15_30kg"], 1)
        self.assertEqual(result["quantities"]["shipping_pick_box_30_50kg"], 1)
        self.assertEqual(result["quantities"]["shipping_pick_storage_item"], 7)
        self.assertTrue(result["warnings"])

    def test_shipping_auto_facts_override_submitted_quantities_and_keep_manual_services(self):
        auto_services = self._agree_shipping_pick_services()
        manual_service = self._agree_shipping_service()
        order = ShippingOrder.objects.create(
            number="OTG-WSF-AUTO-SAVE",
            agency=self.client_agency,
            created_by=self.user,
        )
        box = WarehouseContainer.objects.create(
            agency=self.client_agency,
            container_type=WarehouseContainer.TYPE_BOX,
            container_code="AUTO-SAVE-BOX",
            gross_weight_g=12_000,
        )
        self._shipping_pick_event(
            order,
            container=box,
            payload={"whole_box_shipping_pick": True, "box_code": box.container_code},
        )
        self._shipping_pick_event(order, qty=4, payload={"partial_shipping_pick": True})

        facts = replace_warehouse_facts(
            client=self.client_agency,
            order_type="shipping",
            order_id=order.number,
            lines=[
                {"service_id": auto_services["shipping_pick_box_1_15kg"].id, "quantity": "999"},
                {"service_id": auto_services["shipping_pick_storage_item"].id, "quantity": "999"},
                {"service_id": manual_service.id, "quantity": "2"},
            ],
            user=self.user,
        )
        facts_by_code = {fact.service.code: fact for fact in facts}

        self.assertEqual(facts_by_code["shipping_pick_box_1_15kg"].quantity, Decimal("1"))
        self.assertEqual(facts_by_code["shipping_pick_storage_item"].quantity, Decimal("4"))
        self.assertFalse(facts_by_code["shipping_pick_box_1_15kg"].is_manual)
        self.assertFalse(facts_by_code["shipping_pick_storage_item"].is_manual)
        self.assertEqual(facts_by_code[manual_service.code].quantity, Decimal("2"))
        self.assertTrue(facts_by_code[manual_service.code].is_manual)

    def test_shipping_facts_api_returns_auto_suggestions_without_writing(self):
        self._agree_shipping_pick_services()
        order = ShippingOrder.objects.create(
            number="OTG-WSF-AUTO-API",
            agency=self.client_agency,
            created_by=self.user,
        )
        box = WarehouseContainer.objects.create(
            agency=self.client_agency,
            container_type=WarehouseContainer.TYPE_BOX,
            container_code="AUTO-API-BOX",
            gross_weight_g=20_000,
        )
        self._shipping_pick_event(
            order,
            container=box,
            payload={"whole_box_shipping_pick": True, "box_code": box.container_code},
        )
        self._shipping_pick_event(order, qty=6, payload={"partial_shipping_pick": True})
        self.assertFalse(WarehouseServiceFact.objects.filter(order_id=order.number).exists())

        http = Client()
        http.force_login(self.user)
        response = http.get(
            "/api/warehouse-billing/facts/",
            {"client": self.client_agency.id, "order_type": "shipping", "order_id": order.number},
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        quantities = {row["code"]: row["quantity"] for row in data["automatic_facts"]}
        self.assertEqual(quantities["shipping_pick_box_15_30kg"], "1")
        self.assertEqual(quantities["shipping_pick_storage_item"], "6")
        self.assertEqual(data["automatic_summary"]["total_boxes"], 1)
        self.assertFalse(WarehouseServiceFact.objects.filter(order_id=order.number).exists())

    def test_unlisted_receiving_service_waits_for_manager_and_is_idempotent(self):
        self._audit_order("RCV-WSF-UNLISTED")
        line = {
            "service_id": None,
            "custom_name": "Пересчет вложений вручную",
            "custom_key": "custom-test-1",
            "quantity": "3",
            "unit": "короб",
            "discrepancy_reason": "Услуги нет в согласованном списке клиента",
            "source": "warehouse_unlisted",
        }

        first = replace_warehouse_facts(
            client=self.client_agency,
            order_type="receiving",
            order_id="RCV-WSF-UNLISTED",
            lines=[line],
            user=self.user,
        )
        second = replace_warehouse_facts(
            client=self.client_agency,
            order_type="receiving",
            order_id="RCV-WSF-UNLISTED",
            lines=[line],
            user=self.user,
        )

        self.assertEqual(first[0].pk, second[0].pk)
        self.assertEqual(WarehouseServiceFact.objects.filter(order_id="RCV-WSF-UNLISTED").count(), 1)
        fact = second[0]
        self.assertIsNone(fact.service_id)
        self.assertEqual(fact.status, WarehouseServiceFact.STATUS_WAITING_MANAGER_REVIEW)
        self.assertTrue(fact.metadata["unlisted_service"])
        self.assertEqual(
            require_completion_facts(
                client=self.client_agency,
                order_type="receiving",
                order_id="RCV-WSF-UNLISTED",
            ),
            [fact],
        )

        application = BillingApplication.objects.create(
            application_type=BillingApplication.TYPE_RECEIVING,
            application_id="RCV-WSF-UNLISTED",
            client=self.client_agency,
            legal_entity=self.client_agency,
            created_at_source=timezone.now(),
        )
        self.assertEqual(candidates_from_warehouse_facts(application), [])
        self.assertEqual(application.charges.count(), 0)

    def test_unlisted_service_is_rejected_outside_receiving(self):
        self._audit_order("PROC-WSF-UNLISTED", order_type="processing")

        with self.assertRaisesMessage(ValidationError, "только на приемке"):
            replace_warehouse_facts(
                client=self.client_agency,
                order_type="processing",
                order_id="PROC-WSF-UNLISTED",
                lines=[
                    {
                        "custom_name": "Новая операция",
                        "custom_key": "custom-processing",
                        "quantity": "1",
                        "unit": "шт",
                        "discrepancy_reason": "Нет в списке",
                    }
                ],
                user=self.user,
            )

    def test_manager_review_maps_unlisted_service_without_automatic_charge(self):
        self._audit_order("RCV-WSF-REVIEW")
        fact = replace_warehouse_facts(
            client=self.client_agency,
            order_type="receiving",
            order_id="RCV-WSF-REVIEW",
            lines=[
                {
                    "custom_name": "Пересчет вложений вручную",
                    "custom_key": "custom-review",
                    "quantity": "3",
                    "unit": "короб",
                    "discrepancy_reason": "Нет в списке",
                }
            ],
            user=self.user,
        )[0]
        application = BillingApplication.objects.create(
            application_type=BillingApplication.TYPE_RECEIVING,
            application_id="RCV-WSF-REVIEW",
            client=self.client_agency,
            legal_entity=self.client_agency,
            created_at_source=timezone.now(),
        )

        reviewed = review_unlisted_warehouse_fact(
            fact_id=fact.pk,
            action="approve",
            service_id=self.service.pk,
            quantity="4",
            unit="короб",
            manager_comment="Сопоставлено с приемкой товара",
            user=self.user,
        )

        self.assertEqual(reviewed.service_id, self.service.pk)
        self.assertEqual(reviewed.quantity, Decimal("4"))
        self.assertEqual(reviewed.status, WarehouseServiceFact.STATUS_APPROVED)
        self.assertEqual(application.charges.count(), 0)
        candidates = candidates_from_warehouse_facts(application)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].service_code, self.service.code)

    def test_only_manager_can_review_unlisted_service_api(self):
        manager_user = User.objects.create_user(username="wsf_unlisted_manager", password="pwd")
        Employee.objects.create(
            user=manager_user,
            full_name="Менеджер новых услуг",
            role="manager",
            is_active=True,
        )
        self.client_agency.mened_user_id = manager_user.pk
        self.client_agency.save(update_fields=["mened_user_id"])
        ClientLifecycle.objects.create(
            agency=self.client_agency,
            status=ClientLifecycle.STATUS_ACTIVE,
        )
        self._audit_order("RCV-WSF-REVIEW-API")
        fact = replace_warehouse_facts(
            client=self.client_agency,
            order_type="receiving",
            order_id="RCV-WSF-REVIEW-API",
            lines=[
                {
                    "custom_name": "Проверка коробов вручную",
                    "custom_key": "custom-review-api",
                    "quantity": "2",
                    "unit": "короб",
                    "discrepancy_reason": "Нет в списке",
                }
            ],
            user=self.user,
        )[0]
        path = f"/team-manager/billing/api/warehouse-services/facts/{fact.pk}/review/"
        payload = json.dumps(
            {
                "action": "approve",
                "service_id": self.service.pk,
                "quantity": "2",
                "unit": "короб",
                "manager_comment": "Проверено",
            }
        )

        storekeeper_http = Client()
        storekeeper_http.force_login(self.user)
        denied = storekeeper_http.post(path, data=payload, content_type="application/json")
        self.assertEqual(denied.status_code, 403)

        manager_http = Client()
        manager_http.force_login(manager_user)
        approved = manager_http.post(path, data=payload, content_type="application/json")
        self.assertEqual(approved.status_code, 200)
        self.assertTrue(approved.json()["ok"])
        fact.refresh_from_db()
        self.assertEqual(fact.status, WarehouseServiceFact.STATUS_APPROVED)

    def test_manager_executed_services_page_renders_unlisted_review(self):
        manager_user = User.objects.create_user(username="wsf_unlisted_page_manager", password="pwd")
        Employee.objects.create(
            user=manager_user,
            full_name="Менеджер реестра услуг",
            role="manager",
            is_active=True,
        )
        self.client_agency.mened_user_id = manager_user.pk
        self.client_agency.save(update_fields=["mened_user_id"])
        ClientLifecycle.objects.create(
            agency=self.client_agency,
            status=ClientLifecycle.STATUS_ACTIVE,
        )
        self._audit_order("RCV-WSF-REVIEW-PAGE")
        replace_warehouse_facts(
            client=self.client_agency,
            order_type="receiving",
            order_id="RCV-WSF-REVIEW-PAGE",
            lines=[
                {
                    "custom_name": "Новая услуга для проверки",
                    "custom_key": "custom-review-page",
                    "quantity": "1",
                    "unit": "шт",
                    "discrepancy_reason": "Нет в списке",
                }
            ],
            user=self.user,
        )

        http = Client()
        http.force_login(manager_user)
        response = http.get("/team-manager/billing/executed-services/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Новая услуга для проверки")
        self.assertContains(response, "Проверить")
        self.assertContains(response, "Сопоставить и одобрить")
