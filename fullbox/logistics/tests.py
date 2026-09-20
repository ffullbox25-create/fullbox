from datetime import date, timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from agent.models import DeviceAgent
from audit.models import OrderAuditEntry
from billing.models import BillingApplication, BillingService, WarehouseServiceFact
from employees.models import Employee
from fullbox.container_codes import issue_container_code, issue_pallet_code
from head_manager.models import Carrier, OwnCompany
from shipping.models import ShippingOrder, ShippingOrderItem
from sklad.models import WarehouseContainer, WarehouseLocation, WarehouseReserve, WarehouseStockSnapshot
from sklad.services.warehouse_write_path import WarehouseWritePathService
from sku.models import Agency
from todo.models import Task

from .models import CarrierVehicle, LogisticsTrip, LogisticsTripOrder, ShippingRoutingState, next_draft_trip_number


class LogisticsDashboardTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.logistician_user = user_model.objects.create_user(username="logistician_test", password="pwd")
        self.logistician = Employee.objects.create(
            full_name="Логистов Сергей",
            role="logistician",
            user=self.logistician_user,
            is_active=True,
        )
        self.storekeeper_user = user_model.objects.create_user(username="storekeeper_test", password="pwd")
        self.storekeeper = Employee.objects.create(
            full_name="Кладовщик Петр",
            role="storekeeper",
            user=self.storekeeper_user,
            is_active=True,
        )
        self.agency = Agency.objects.create(agn_name="Индивидуальный предприниматель Талеев Денис Толибаевич")
        self.own_company = OwnCompany.objects.create(
            name='Общество с ограниченной ответственностью "ФуллБокс"',
            is_default=True,
            is_active=True,
        )
        self.carrier = Carrier.objects.create(
            name="ИП Касаев Абдурашид Метханович",
            is_active=True,
        )

    def _create_packed_order(self, number: str, *, pallet_count: int = 2, box_count: int = 16) -> ShippingOrder:
        order = ShippingOrder.objects.create(
            number=number,
            agency=self.agency,
            created_by=self.logistician_user,
            status=ShippingOrder.STATUS_PACKED,
            slot_date=date(2026, 4, 2),
            destination_warehouse="Склад WB Тюмень",
            expected_boxes=box_count,
        )
        OrderAuditEntry.objects.create(
            order_id=order.number,
            order_type="shipping",
            action="status",
            agency=self.agency,
            user=self.logistician_user,
            description="Паллетизация завершена",
            payload={
                "act": "shipping_packing",
                "pallet_count": pallet_count,
                "delivered_box_count": box_count,
            },
        )
        return order

    def _add_logistics_fact(self, trip: LogisticsTrip, *, agency=None):
        service, _created = BillingService.objects.get_or_create(
            code="logistics_trip_test",
            defaults={"name": "Тестовая услуга рейса", "unit": "рейс", "is_active": True},
        )
        return WarehouseServiceFact.objects.create(
            client=agency or self.agency,
            order_type="logistics",
            order_id=trip.number,
            service=service,
            service_name_snapshot=service.name,
            quantity=1,
            unit="рейс",
            status=WarehouseServiceFact.STATUS_SENT_TO_BILLING,
            source="logistics_manual",
            reported_by=self.logistician_user,
        )

    def _add_shipping_fact(self, order: ShippingOrder):
        service, _created = BillingService.objects.get_or_create(
            code="shipping_loading_test",
            defaults={"name": "Погрузка в машину", "unit": "шт", "is_active": True},
        )
        return WarehouseServiceFact.objects.create(
            client=order.agency,
            order_type=WarehouseServiceFact.ORDER_SHIPPING,
            order_id=order.number,
            service=service,
            service_name_snapshot=service.name,
            quantity=1,
            unit="шт",
            status=WarehouseServiceFact.STATUS_SENT_TO_BILLING,
            source="shipping_loading_manual",
            reported_by=self.storekeeper_user,
        )

    def _scan_trip_pallet(self, trip: LogisticsTrip, pallet_code: str):
        OrderAuditEntry.objects.filter(
            order_id=str(trip.pk),
            order_type="logistics_trip",
            payload__act="trip_loading_progress",
        ).delete()
        response = self.client.post(
            reverse("logistics:trip-loading", args=[trip.pk]),
            data={"scan_value": pallet_code},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(response.status_code, 200, response.content.decode())
        payload = response.json()
        self.assertTrue(payload["result"]["ok"], payload)
        self.assertTrue(payload["all_complete"], payload)
        return response

    def test_order_logistician_task_closes_only_after_trip_enters_loading(self):
        from logistics.routing_services import sync_routing_from_trip
        from shipping.services import ensure_logistician_task

        order = self._create_packed_order("OTG-TASK-LIFECYCLE")
        task = ensure_logistician_task(order, user=self.logistician_user)
        self.assertEqual(
            ShippingRoutingState.objects.get(shipping_order=order).status,
            ShippingRoutingState.STATUS_READY,
        )
        trip = LogisticsTrip.objects.create(
            number="TRIP-TASK-LIFECYCLE",
            trip_date=date(2026, 4, 2),
            status=LogisticsTrip.STATUS_DRAFT,
            assigned_logistician=self.logistician,
            created_by=self.logistician_user,
        )
        LogisticsTripOrder.objects.create(
            trip=trip,
            shipping_order=order,
            loading_sequence=1,
            delivery_sequence=1,
        )

        sync_routing_from_trip(order)
        task.refresh_from_db()
        self.assertNotEqual(task.status, "done")

        trip.status = LogisticsTrip.STATUS_LOADING
        trip.save(update_fields=["status", "updated_at"])
        sync_routing_from_trip(order)
        task.refresh_from_db()
        self.assertEqual(task.status, "done")

    def _create_packed_order_with_pallets(self, number: str, pallets: list[dict]) -> ShippingOrder:
        order = ShippingOrder.objects.create(
            number=number,
            agency=self.agency,
            created_by=self.logistician_user,
            status=ShippingOrder.STATUS_PACKED,
            slot_date=date(2026, 4, 2),
            destination_warehouse=f"Склад {number}",
            expected_boxes=sum(len(list(pallet.get("boxes") or [])) for pallet in pallets),
        )
        act_boxes = []
        act_pallets = []
        total_boxes = 0
        for pallet in pallets:
            box_refs = []
            for index, box in enumerate(list(pallet.get("boxes") or []), start=1):
                code = str(box.get("code") or "").strip()
                if not code:
                    continue
                row_key = f"{code}::{index}"
                total_boxes += 1
                box_refs.append(row_key)
                act_boxes.append(
                    {
                        "row_key": row_key,
                        "code": code,
                        "qty": int(box.get("qty") or 1),
                        "items": [],
                        "barcode_preview": str(box.get("barcode_preview") or ""),
                        "pallet_label": str(pallet.get("label") or ""),
                    }
                )
            act_pallets.append(
                {
                    "label": str(pallet.get("label") or ""),
                    "code": str(pallet.get("code") or ""),
                    "boxes": box_refs,
                    "qty": sum(int(box.get("qty") or 1) for box in list(pallet.get("boxes") or [])),
                }
            )
        OrderAuditEntry.objects.create(
            order_id=order.number,
            order_type="shipping",
            action="status",
            agency=self.agency,
            user=self.logistician_user,
            description="Паллетизация завершена",
            payload={
                "act": "shipping_packing",
                "pallet_count": len(act_pallets),
                "delivered_box_count": total_boxes,
                "act_pallets": act_pallets,
                "act_boxes": act_boxes,
            },
        )
        return order

    def _create_packed_order_with_warehouse_pallet(
        self,
        number: str,
        *,
        pallet_code: str,
        agency: Agency | None = None,
    ) -> ShippingOrder:
        agency = agency or self.agency
        order = ShippingOrder.objects.create(
            number=number,
            agency=agency,
            created_by=self.logistician_user,
            status=ShippingOrder.STATUS_PACKED,
            slot_date=date(2026, 4, 2),
            destination_warehouse=f"Склад {number}",
            expected_boxes=1,
        )
        ShippingOrderItem.objects.create(
            order=order,
            sku_code="SKU-LOAD-1",
            name="Товар погрузки",
            size="44",
            barcode="355500000001",
            goods_type="Готовый",
            qty_requested=10,
            qty_reserved=10,
        )
        location, _ = WarehouseLocation.objects.get_or_create(
            location_code="OTG-1-1-1-1",
            defaults={
                "warehouse_code": "MSK",
                "zone_code": "OTG",
                "zone_kind": WarehouseLocation.ZONE_KIND_SHIPPING,
                "row_no": 1,
                "section_no": 1,
                "tier_no": 1,
                "cell_no": 1,
                "display_name": "OTG · Зона отгрузки",
            },
        )
        shipping_pallet = WarehouseContainer.objects.create(
            agency=agency,
            container_type=WarehouseContainer.TYPE_MIXED_PALLET,
            container_code=pallet_code,
            current_location=location,
            source_context_type="shipping",
            source_context_id=order.number,
            created_by=self.logistician_user,
        )
        box = WarehouseContainer.objects.create(
            agency=agency,
            container_type=WarehouseContainer.TYPE_BOX,
            container_code=f"{pallet_code}-BOX-1",
            parent_container=shipping_pallet,
            current_location=location,
            source_context_type="receiving",
            source_context_id="R-LOAD-1",
            created_by=self.logistician_user,
        )
        WarehouseReserve.objects.create(
            agency=agency,
            reserve_type=WarehouseReserve.TYPE_SHIPPING,
            context_type="shipping",
            context_id=order.number,
            sku_code="SKU-LOAD-1",
            size="44",
            barcode="355500000001",
            goods_type="Готовый",
            qty_reserved=10,
            qty_allocated=10,
            status=WarehouseReserve.STATUS_ALLOCATED,
            created_by=self.logistician_user,
        )
        WarehouseStockSnapshot.objects.create(
            agency=agency,
            source_context_type="receiving",
            source_context_id="R-LOAD-1",
            sku_code="SKU-LOAD-1",
            name="Товар погрузки",
            size="44",
            barcode="355500000001",
            goods_type="Готовый",
            qty=10,
            available_qty=0,
            shipping_reserved_qty=10,
            container=box,
            container_code=box.container_code,
            parent_container=shipping_pallet,
            location=location,
            zone_code="OTG",
            zone_kind=location.zone_kind,
            warehouse_state_code="ready_for_loading",
        )
        OrderAuditEntry.objects.create(
            order_id=order.number,
            order_type="shipping",
            action="status",
            agency=agency,
            user=self.logistician_user,
            description="Паллетизация завершена",
            payload={
                "act": "shipping_packing",
                "pallet_count": 1,
                "delivered_box_count": 1,
                "act_pallets": [{"label": "Паллета 1", "code": pallet_code, "boxes": [f"{pallet_code}-BOX-1::1"], "qty": 10}],
                "act_boxes": [{"row_key": f"{pallet_code}-BOX-1::1", "code": f"{pallet_code}-BOX-1", "qty": 10, "items": [], "pallet_label": "Паллета 1", "pallet_code": pallet_code}],
            },
        )
        return order

    def test_logistician_can_open_dashboard(self):
        order = self._create_packed_order("SO-000001")
        self.client.force_login(self.logistician_user)

        response = self.client.get(reverse("logistics:dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Логистический контур")
        self.assertContains(response, "1_OTG")
        self.assertNotContains(response, order.number)
        self.assertContains(response, "Сборка подтверждена")

    def test_dashboard_displays_transit_warehouse_in_direction(self):
        order = self._create_packed_order("SO-TRANSIT-DIRECTION")
        order.destination_warehouse = "Казань_РФЦ_НОВЫЙ"
        order.wb_transit_warehouse = True
        order.transit_address = "СТАРАЯ_КУПАВНА_28"
        order.save(
            update_fields=[
                "destination_warehouse",
                "wb_transit_warehouse",
                "transit_address",
                "updated_at",
            ]
        )
        self.client.force_login(self.logistician_user)

        response = self.client.get(reverse("logistics:dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Назначение: Казань_РФЦ_НОВЫЙ")
        self.assertContains(response, "Транзит: СТАРАЯ_КУПАВНА_28")
        self.assertContains(response, "старая_купавна_28", html=False)

    def test_dashboard_batches_shipping_movement_state_without_changing_labels(self):
        first_order = self._create_packed_order("SO-BATCH-001")
        second_order = self._create_packed_order("SO-BATCH-002")

        from .services import build_logistics_dashboard_context

        with CaptureQueriesContext(connection) as captured:
            context = build_logistics_dashboard_context(role="manager")

        move_task_queries = [
            query
            for query in captured.captured_queries
            if "reachtruck_movetask" in str(query.get("sql") or "").lower()
        ]
        self.assertEqual(len(move_task_queries), 1)
        labels_by_order_id = {
            row["order"].pk: row["status_label"]
            for row in context["ready_orders"]
        }
        self.assertEqual(labels_by_order_id[first_order.pk], "Подготовлена складом, ожидает логиста")
        self.assertEqual(labels_by_order_id[second_order.pk], "Подготовлена складом, ожидает логиста")

    def test_dashboard_excludes_transfer_orders(self):
        transfer_order = self._create_packed_order("OTG-TRANSFER-DASHBOARD")
        transfer_order.delivery_type = ShippingOrder.DELIVERY_TRANSFER
        transfer_order.save(update_fields=["delivery_type", "updated_at"])
        regular_order = self._create_packed_order("OTG-REGULAR-DASHBOARD")

        from .services import build_logistics_dashboard_context

        context = build_logistics_dashboard_context(role="logistician", employee=self.logistician)
        ready_order_ids = {row["order"].pk for row in context["ready_orders"]}

        self.assertNotIn(transfer_order.pk, ready_order_ids)
        self.assertIn(regular_order.pk, ready_order_ids)

    def test_dashboard_marks_trip_orders_as_preparing_for_trip(self):
        order = self._create_packed_order("SO-000003")
        trip = LogisticsTrip.objects.create(
            number="TRIP-000001",
            trip_date=date(2026, 4, 3),
            status=LogisticsTrip.STATUS_PLANNED,
            assigned_logistician=self.logistician,
            created_by=self.logistician_user,
        )
        LogisticsTripOrder.objects.create(
            trip=trip,
            shipping_order=order,
            loading_sequence=1,
            delivery_sequence=1,
        )
        self.client.force_login(self.logistician_user)

        response = self.client.get(reverse("logistics:dashboard"))

        self.assertContains(response, ">Р<", html=False)
        self.assertContains(response, "Подготовка к рейсу")
        self.assertContains(response, f"/logistics/trips/{trip.pk}/")
        self.assertContains(response, "Открыть рейс")
        self.assertContains(response, "/logistics/trips/")

    def test_dashboard_marks_departed_trip_orders_as_loaded(self):
        order = self._create_packed_order("SO-000003A")
        trip = LogisticsTrip.objects.create(
            number="TRIP-000001A",
            trip_date=date(2026, 4, 3),
            status=LogisticsTrip.STATUS_DEPARTED,
            assigned_logistician=self.logistician,
            created_by=self.logistician_user,
        )
        LogisticsTripOrder.objects.create(
            trip=trip,
            shipping_order=order,
            loading_sequence=1,
            delivery_sequence=1,
        )
        self.client.force_login(self.logistician_user)

        response = self.client.get(reverse("logistics:dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Загружено в машину")
        self.assertNotContains(response, "Подготовка к рейсу")
        self.assertContains(response, 'data-stage="shipped"', html=False)
        self.assertContains(response, "Отгружено")
        self.assertContains(response, f"/logistics/trips/{trip.pk}/")

    def test_dashboard_groups_trip_orders_together_before_free_ready_orders(self):
        first_order = self._create_packed_order("SO-000011")
        second_order = self._create_packed_order("SO-000012")
        free_order = self._create_packed_order("SO-000013")
        first_order.slot_date = date(2026, 4, 7)
        first_order.save(update_fields=["slot_date", "updated_at"])
        second_order.slot_date = None
        second_order.save(update_fields=["slot_date", "updated_at"])
        free_order.slot_date = date(2026, 4, 5)
        free_order.save(update_fields=["slot_date", "updated_at"])
        trip = LogisticsTrip.objects.create(
            number="TRIP-000001B",
            trip_date=date(2026, 4, 3),
            status=LogisticsTrip.STATUS_DEPARTED,
            assigned_logistician=self.logistician,
            created_by=self.logistician_user,
        )
        LogisticsTripOrder.objects.create(
            trip=trip,
            shipping_order=first_order,
            loading_sequence=1,
            delivery_sequence=1,
        )
        LogisticsTripOrder.objects.create(
            trip=trip,
            shipping_order=second_order,
            loading_sequence=2,
            delivery_sequence=2,
        )
        self.client.force_login(self.logistician_user)

        response = self.client.get(reverse("logistics:dashboard"))

        self.assertEqual(response.status_code, 200)
        content = response.content.decode("utf-8")
        self.assertLess(content.index("11_OTG"), content.index("12_OTG"))
        self.assertLess(content.index("12_OTG"), content.index("13_OTG"))

    def test_dashboard_sorts_ready_rows_by_latest_changes_while_keeping_trip_grouped(self):
        grouped_first_order = self._create_packed_order("SO-000021")
        grouped_second_order = self._create_packed_order("SO-000022")
        free_order = self._create_packed_order("SO-000023")
        grouped_trip = LogisticsTrip.objects.create(
            number="TRIP-000021A",
            trip_date=date(2026, 4, 3),
            status=LogisticsTrip.STATUS_DEPARTED,
            assigned_logistician=self.logistician,
            created_by=self.logistician_user,
        )
        LogisticsTripOrder.objects.create(
            trip=grouped_trip,
            shipping_order=grouped_first_order,
            loading_sequence=1,
            delivery_sequence=1,
        )
        LogisticsTripOrder.objects.create(
            trip=grouped_trip,
            shipping_order=grouped_second_order,
            loading_sequence=2,
            delivery_sequence=2,
        )
        now = timezone.now()
        LogisticsTrip.objects.filter(pk=grouped_trip.pk).update(updated_at=now)
        ShippingOrder.objects.filter(pk=free_order.pk).update(updated_at=now - timedelta(minutes=5))
        self.client.force_login(self.logistician_user)

        response = self.client.get(reverse("logistics:dashboard"))

        self.assertEqual(response.status_code, 200)
        content = response.content.decode("utf-8")
        self.assertLess(content.index("21_OTG"), content.index("22_OTG"))
        self.assertLess(content.index("22_OTG"), content.index("23_OTG"))

    def test_logistician_can_open_trip_list(self):
        order = self._create_packed_order("SO-TRIP-LIST", pallet_count=3, box_count=21)
        trip = LogisticsTrip.objects.create(
            number="TRIP-000111",
            trip_date=date(2026, 4, 7),
            status=LogisticsTrip.STATUS_LOADING,
            vehicle_type=LogisticsTrip.VEHICLE_FULFILLMENT,
            vehicle_name="Транспорт Fullbox",
            vehicle_number="А123ВС777",
            driver_name="Сидоров Сидор Сидорович",
            driver_phone="+7 900 222-11-00",
            assigned_logistician=self.logistician,
            created_by=self.logistician_user,
        )
        LogisticsTripOrder.objects.create(
            trip=trip,
            shipping_order=order,
            loading_sequence=1,
            delivery_sequence=1,
        )
        self.client.force_login(self.logistician_user)

        response = self.client.get(reverse("logistics:trip-list"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Все рейсы")
        self.assertContains(response, "111_RS")
        self.assertContains(response, "Погрузка")
        self.assertContains(response, "Транспорт Fullbox")
        self.assertContains(response, "А 123 ВС 777")
        self.assertContains(response, "Сидоров Сидор Сидорович")
        self.assertContains(response, "ИП Талеев Денис Толибаевич")
        self.assertContains(response, "Склад WB Тюмень")
        self.assertContains(response, f"/logistics/trips/{trip.pk}/")

    def test_logistician_can_create_trip_from_packed_order(self):
        order = self._create_packed_order("SO-000002")
        self.client.force_login(self.logistician_user)

        response = self.client.post(
            reverse("logistics:dashboard"),
            data={
                "action": "create_trip",
                "trip_date": "2026-04-03",
                "vehicle_type": LogisticsTrip.VEHICLE_FULFILLMENT,
                "vehicle_name": "Газель логистики",
                "vehicle_number": "A111BC72",
                "driver_name": "Сергей Водитель",
                "driver_phone": "+7 900 100-00-00",
                "route_comment": "Погрузить в первой волне",
                "order_ids": [str(order.id)],
            },
        )

        self.assertEqual(response.status_code, 302)
        trip = LogisticsTrip.objects.get()
        self.assertEqual(trip.status, LogisticsTrip.STATUS_DRAFT)
        self.assertEqual(trip.assigned_logistician, self.logistician)
        self.assertEqual(trip.vehicle_name, "Газель логистики")
        self.assertTrue(trip.number.startswith("DRAFT-"))
        self.assertLessEqual(len(trip.number), 32)
        self.assertEqual(trip.vehicle_number, "A111BC72")
        self.assertEqual(trip.driver_phone, "+7 900 100-00-00")
        link = LogisticsTripOrder.objects.get()
        self.assertEqual(link.trip, trip)
        self.assertEqual(link.shipping_order, order)
        storekeeper_task = Task.objects.get(
            route=f"/logistics/trips/{trip.pk}/",
            assigned_to=self.storekeeper,
        )
        self.assertEqual(storekeeper_task.title, "Рейс · Черновик")
        self.assertIn("только для просмотра", storekeeper_task.description)
        self.client.force_login(self.storekeeper_user)
        storekeeper_dashboard = self.client.get(
            "/sklad/",
            {
                "todo_filters_applied": "1",
                "todo_filter_type": "logistics",
            },
        )
        self.assertEqual(storekeeper_dashboard.status_code, 200)
        self.assertContains(storekeeper_dashboard, "Рейс · Черновик")
        self.assertContains(storekeeper_dashboard, "Черновик")
        self.assertContains(storekeeper_dashboard, f"/logistics/trips/{trip.pk}/")
        self.client.force_login(self.logistician_user)
        detail_response = self.client.get(reverse("logistics:trip-detail", args=[trip.pk]))
        self.assertContains(detail_response, "Заявки в рейсе")
        self.assertContains(detail_response, "Добавить заявки для планирования")
        self.assertContains(detail_response, "Маршрут")
        self.assertNotContains(detail_response, "Погрузка в машину")
        self.assertContains(detail_response, "2_OTG")
        self.assertNotContains(detail_response, order.number)
        self.assertContains(detail_response, "ИП Талеев Денис Толибаевич")
        self.assertContains(detail_response, "Сборка подтверждена")
        self.assertContains(detail_response, "Склад подтвердил сборку: 1 из 1")
        self.assertContains(detail_response, "Нет дополнительных согласованных заявок для планирования.")
        self.assertContains(detail_response, "Параметры загрузки")
        self.assertContains(detail_response, "Сформировать рейс")
        self.assertContains(detail_response, "Черновик")
        self.assertContains(detail_response, "Количество паллет")
        self.assertContains(detail_response, "Количество коробов")
        self.assertContains(detail_response, ">2<", html=False)
        self.assertContains(detail_response, ">16<", html=False)

    def test_trip_detail_displays_transit_warehouse(self):
        order = self._create_packed_order("SO-TRIP-TRANSIT")
        order.destination_warehouse = "Казань_РФЦ_НОВЫЙ"
        order.wb_transit_warehouse = True
        order.transit_address = "СТАРАЯ_КУПАВНА_28"
        order.save(
            update_fields=[
                "destination_warehouse",
                "wb_transit_warehouse",
                "transit_address",
                "updated_at",
            ]
        )
        trip = LogisticsTrip.objects.create(
            number="DRAFT-TRIP-TRANSIT",
            status=LogisticsTrip.STATUS_DRAFT,
            assigned_logistician=self.logistician,
            created_by=self.logistician_user,
        )
        LogisticsTripOrder.objects.create(trip=trip, shipping_order=order)
        self.client.force_login(self.logistician_user)

        response = self.client.get(reverse("logistics:trip-detail", args=[trip.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Транзитный склад")
        self.assertContains(response, "СТАРАЯ_КУПАВНА_28")

    def test_logistician_can_preplan_reserved_order_without_changing_shipping_state(self):
        order = self._create_packed_order("SO-PREPLAN-RESERVED")
        order.status = ShippingOrder.STATUS_RESERVED
        order.save(update_fields=["status", "updated_at"])
        self.client.force_login(self.logistician_user)

        dashboard = self.client.get(reverse("logistics:dashboard"))
        self.assertContains(dashboard, "Можно планировать")
        self.assertContains(dashboard, f'name="order_ids" value="{order.pk}"', html=False)

        response = self.client.post(
            reverse("logistics:dashboard"),
            data={
                "action": "create_trip",
                "trip_date": "2026-04-03",
                "order_ids": [str(order.pk)],
            },
        )

        self.assertEqual(response.status_code, 302)
        trip = LogisticsTrip.objects.get()
        order.refresh_from_db()
        self.assertEqual(trip.status, LogisticsTrip.STATUS_DRAFT)
        self.assertEqual(order.status, ShippingOrder.STATUS_RESERVED)
        self.assertFalse(ShippingRoutingState.objects.filter(shipping_order=order).exists())
        detail = self.client.get(reverse("logistics:trip-detail", args=[trip.pk]))
        self.assertContains(detail, "Склад подтвердил сборку: 0 из 1")
        self.assertContains(detail, "Ожидает склад")
        self.assertContains(detail, 'id="finalize-trip-btn"', html=False)
        self.assertContains(detail, "disabled", html=False)

    def test_submitted_order_cannot_be_added_to_trip_draft(self):
        order = self._create_packed_order("SO-PREPLAN-SUBMITTED")
        order.status = ShippingOrder.STATUS_SUBMITTED
        order.save(update_fields=["status", "updated_at"])
        self.client.force_login(self.logistician_user)

        response = self.client.post(
            reverse("logistics:dashboard"),
            data={"action": "create_trip", "order_ids": [str(order.pk)]},
        )

        self.assertEqual(response.status_code, 302)
        self.assertFalse(LogisticsTrip.objects.exists())
        self.assertFalse(LogisticsTripOrder.objects.exists())

    def test_add_to_draft_uses_explicit_owned_draft(self):
        user_model = get_user_model()
        other_user = user_model.objects.create_user(username="other_logistician", password="pwd")
        other_logistician = Employee.objects.create(
            full_name="Другой Логист",
            role="logistician",
            user=other_user,
            is_active=True,
        )
        own_draft = LogisticsTrip.objects.create(
            number="DRAFT-OWN",
            status=LogisticsTrip.STATUS_DRAFT,
            assigned_logistician=self.logistician,
            created_by=self.logistician_user,
        )
        other_draft = LogisticsTrip.objects.create(
            number="DRAFT-OTHER",
            status=LogisticsTrip.STATUS_DRAFT,
            assigned_logistician=other_logistician,
            created_by=other_user,
        )
        own_order = self._create_packed_order("SO-PREPLAN-OWN")
        own_order.status = ShippingOrder.STATUS_PICKING
        own_order.save(update_fields=["status", "updated_at"])
        self.client.force_login(self.logistician_user)

        response = self.client.post(
            reverse("logistics:dashboard"),
            data={
                "action": "add_to_draft",
                "draft_trip_id": str(own_draft.pk),
                "order_ids": [str(own_order.pk)],
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertTrue(
            LogisticsTripOrder.objects.filter(
                trip=own_draft,
                shipping_order=own_order,
            ).exists()
        )
        self.assertFalse(LogisticsTripOrder.objects.filter(trip=other_draft).exists())

        foreign_order = self._create_packed_order("SO-PREPLAN-FOREIGN")
        foreign_order.status = ShippingOrder.STATUS_RESERVED
        foreign_order.save(update_fields=["status", "updated_at"])
        forbidden = self.client.post(
            reverse("logistics:dashboard"),
            data={
                "action": "add_to_draft",
                "draft_trip_id": str(other_draft.pk),
                "order_ids": [str(foreign_order.pk)],
            },
        )
        self.assertEqual(forbidden.status_code, 403)
        self.assertFalse(
            LogisticsTripOrder.objects.filter(
                trip=other_draft,
                shipping_order=foreign_order,
            ).exists()
        )

    def test_trip_detail_adds_picking_order_without_syncing_routing(self):
        trip = LogisticsTrip.objects.create(
            number="DRAFT-DETAIL-PREPLAN",
            status=LogisticsTrip.STATUS_DRAFT,
            assigned_logistician=self.logistician,
            created_by=self.logistician_user,
        )
        order = self._create_packed_order("SO-PREPLAN-DETAIL")
        order.status = ShippingOrder.STATUS_PICKING
        order.save(update_fields=["status", "updated_at"])
        self.client.force_login(self.logistician_user)

        response = self.client.post(
            reverse("logistics:trip-detail", args=[trip.pk]),
            data={"action": "add_orders", "order_ids": [str(order.pk)]},
        )

        self.assertEqual(response.status_code, 302)
        self.assertTrue(
            LogisticsTripOrder.objects.filter(trip=trip, shipping_order=order).exists()
        )
        order.refresh_from_db()
        self.assertEqual(order.status, ShippingOrder.STATUS_PICKING)
        self.assertFalse(ShippingRoutingState.objects.filter(shipping_order=order).exists())

    def test_logistician_cannot_create_trip_from_transfer_order(self):
        order = self._create_packed_order("OTG-TRANSFER-NO-TRIP")
        order.delivery_type = ShippingOrder.DELIVERY_TRANSFER
        order.save(update_fields=["delivery_type", "updated_at"])
        self.client.force_login(self.logistician_user)

        response = self.client.post(
            reverse("logistics:dashboard"),
            data={
                "action": "create_trip",
                "trip_date": "2026-04-03",
                "vehicle_type": LogisticsTrip.VEHICLE_FULFILLMENT,
                "vehicle_name": "Газель логистики",
                "vehicle_number": "A111BC72",
                "driver_name": "Сергей Водитель",
                "driver_phone": "+7 900 100-00-00",
                "order_ids": [str(order.id)],
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertFalse(LogisticsTrip.objects.exists())
        self.assertFalse(LogisticsTripOrder.objects.filter(shipping_order=order).exists())

    def test_marketplace_order_with_warehouse_pallet_remains_eligible_for_trip(self):
        order = self._create_packed_order_with_warehouse_pallet(
            "OTG-REGULAR-WITH-PALLET",
            pallet_code="TDT-REGULAR-OTG-000001-gv",
        )
        self.assertEqual(order.delivery_type, ShippingOrder.DELIVERY_MARKETPLACE)
        self.client.force_login(self.logistician_user)

        response = self.client.post(
            reverse("logistics:dashboard"),
            data={
                "action": "create_trip",
                "trip_date": "2026-04-03",
                "vehicle_type": LogisticsTrip.VEHICLE_FULFILLMENT,
                "vehicle_name": "Газель логистики",
                "vehicle_number": "A111BC72",
                "driver_name": "Сергей Водитель",
                "driver_phone": "+7 900 100-00-00",
                "order_ids": [str(order.id)],
            },
        )

        self.assertEqual(response.status_code, 302)
        link = LogisticsTripOrder.objects.get(shipping_order=order)
        self.assertEqual(link.trip.status, LogisticsTrip.STATUS_DRAFT)

    def test_next_draft_trip_number_fits_database_field(self):
        value = next_draft_trip_number()
        self.assertTrue(value.startswith("DRAFT-"))
        self.assertLessEqual(len(value), 32)

    def test_trip_detail_updates_sequences(self):
        first_order = self._create_packed_order("SO-LOG-0003", pallet_count=1, box_count=8)
        second_order = self._create_packed_order("SO-LOG-0004", pallet_count=3, box_count=24)
        trip = LogisticsTrip.objects.create(
            number="TRIP-000001",
            trip_date=date(2026, 4, 3),
            status=LogisticsTrip.STATUS_PLANNED,
            assigned_logistician=self.logistician,
            created_by=self.logistician_user,
        )
        first_link = LogisticsTripOrder.objects.create(
            trip=trip,
            shipping_order=first_order,
            loading_sequence=1,
            delivery_sequence=1,
        )
        second_link = LogisticsTripOrder.objects.create(
            trip=trip,
            shipping_order=second_order,
            loading_sequence=2,
            delivery_sequence=2,
        )
        self.client.force_login(self.logistician_user)

        response = self.client.post(
            reverse("logistics:trip-detail", args=[trip.pk]),
            data={
                "action": "update_sequences",
                "ordered_trip_item_ids": [str(second_link.id), str(first_link.id)],
                f"comment_{first_link.id}": "Погрузить после второй заявки",
                f"comment_{second_link.id}": "Первая в погрузке",
            },
        )

        self.assertEqual(response.status_code, 302)
        first_link.refresh_from_db()
        second_link.refresh_from_db()
        self.assertEqual(first_link.loading_sequence, 2)
        self.assertEqual(first_link.delivery_sequence, 2)
        self.assertEqual(first_link.comment, "Погрузить после второй заявки")
        self.assertEqual(second_link.loading_sequence, 1)
        self.assertEqual(second_link.delivery_sequence, 1)
        self.assertEqual(second_link.comment, "Первая в погрузке")

    def test_trip_detail_updates_loading_params(self):
        order = self._create_packed_order("SO-LOAD-0001", pallet_count=1, box_count=8)
        trip = LogisticsTrip.objects.create(
            number="TRIP-000002",
            trip_date=date(2026, 4, 3),
            status=LogisticsTrip.STATUS_PLANNED,
            assigned_logistician=self.logistician,
            created_by=self.logistician_user,
        )
        LogisticsTripOrder.objects.create(
            trip=trip,
            shipping_order=order,
            loading_sequence=1,
            delivery_sequence=1,
        )
        self.client.force_login(self.logistician_user)

        response = self.client.post(
            reverse("logistics:trip-detail", args=[trip.pk]),
            data={
                "action": "update_loading_params",
                "carrier_id": str(self.carrier.id),
                "vehicle_number": "a123bc777",
                "driver_name": "Иванов Иван Иванович",
                "driver_phone": "8 (900) 555-44-33",
            },
        )

        self.assertEqual(response.status_code, 302)
        trip.refresh_from_db()
        self.assertEqual(trip.carrier, self.carrier)
        self.assertEqual(trip.vehicle_number, "А123ВС777")
        self.assertEqual(trip.driver_name, "Иванов Иван Иванович")
        self.assertEqual(trip.driver_phone, "+7 900 555-44-33")

        detail_response = self.client.get(reverse("logistics:trip-detail", args=[trip.pk]))
        self.assertContains(detail_response, "Параметры сохраняются автоматически после выхода из блока.")
        self.assertNotContains(detail_response, "Сохранить параметры")
        self.assertNotContains(detail_response, "Сохранить порядок")
        self.assertContains(detail_response, 'name="vehicle_number"', html=False)
        self.assertContains(detail_response, 'value="А123ВС777"', html=False)
        self.assertContains(detail_response, 'id="vehicle-number-editor"', html=False)
        self.assertContains(detail_response, 'name="carrier_id"', html=False)
        self.assertContains(detail_response, "ИП Касаев Абдурашид Метханович")
        self.assertContains(detail_response, "ФуллБокс")
        self.assertContains(detail_response, "ИП Талеев Денис Толибаевич")
        self.assertContains(detail_response, "Склад WB Тюмень")
        self.assertContains(detail_response, 'data-placeholder="А 123 АА 777"', html=False)
        self.assertContains(detail_response, 'id="carrier-selected-label"', html=False)
        self.assertContains(detail_response, "Иванов Иван Иванович")
        self.assertContains(detail_response, "+7 900 555-44-33")

    def test_trip_detail_exposes_latest_carrier_transport_defaults(self):
        LogisticsTrip.objects.create(
            number="TRIP-CARRIER-HISTORY",
            trip_date=date(2026, 4, 1),
            status=LogisticsTrip.STATUS_COMPLETED,
            carrier=self.carrier,
            vehicle_number="А777АА777",
            driver_name="Исторический Водитель",
            driver_phone="+7 900 111-22-33",
            assigned_logistician=self.logistician,
            created_by=self.logistician_user,
        )
        LogisticsTrip.objects.create(
            number="TRIP-CARRIER-CANCELED",
            trip_date=date(2026, 4, 2),
            status=LogisticsTrip.STATUS_CANCELED,
            carrier=self.carrier,
            vehicle_number="А000АА777",
            driver_name="Отмененный Водитель",
            driver_phone="+7 900 000-00-00",
            assigned_logistician=self.logistician,
            created_by=self.logistician_user,
        )
        trip = LogisticsTrip.objects.create(
            number="TRIP-CARRIER-CURRENT",
            trip_date=date(2026, 4, 3),
            status=LogisticsTrip.STATUS_DRAFT,
            assigned_logistician=self.logistician,
            created_by=self.logistician_user,
        )
        self.client.force_login(self.logistician_user)

        response = self.client.get(reverse("logistics:trip-detail", args=[trip.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-default-vehicle="А777АА777"', html=False)
        self.assertContains(response, 'data-default-driver="Исторический Водитель"', html=False)
        self.assertContains(response, 'data-default-phone="+7 900 111-22-33"', html=False)
        self.assertNotContains(response, 'data-default-driver="Отмененный Водитель"', html=False)
        self.assertContains(response, "applyCarrierTransportDefaults")
        self.assertContains(response, "applyCarrierTransportDefaults({ onlyBlank: true })")

    def test_trip_detail_prefers_default_carrier_vehicle_over_trip_history(self):
        LogisticsTrip.objects.create(
            number="TRIP-CARRIER-OLD-DEFAULT",
            trip_date=date(2026, 4, 1),
            status=LogisticsTrip.STATUS_COMPLETED,
            carrier=self.carrier,
            vehicle_number="А111АА777",
            driver_name="Старый Водитель",
            driver_phone="+7 900 111-11-11",
            assigned_logistician=self.logistician,
            created_by=self.logistician_user,
        )
        CarrierVehicle.objects.create(
            carrier=self.carrier,
            vehicle_name="Газель",
            vehicle_number="А222АА777",
            driver_name="Основной Водитель",
            driver_phone="+7 900 222-22-22",
            max_weight_kg=1500,
            max_volume_m3=12,
            max_pallets=8,
            is_default=True,
            is_active=True,
            created_by=self.logistician_user,
        )
        trip = LogisticsTrip.objects.create(
            number="TRIP-CARRIER-REGISTRY",
            trip_date=date(2026, 4, 3),
            status=LogisticsTrip.STATUS_DRAFT,
            assigned_logistician=self.logistician,
            created_by=self.logistician_user,
        )
        from .services import _carrier_options_queryset

        option = _carrier_options_queryset(exclude_trip_id=trip.pk).get(pk=self.carrier.pk)

        self.assertEqual(option.default_vehicle_number, "А222АА777")
        self.assertEqual(option.default_driver_name, "Основной Водитель")
        self.assertEqual(option.default_driver_phone, "+7 900 222-22-22")
        self.assertNotEqual(option.default_driver_name, "Старый Водитель")

    def test_trip_detail_update_trip_normalizes_vehicle_and_phone(self):
        trip = LogisticsTrip.objects.create(
            number="TRIP-000003",
            trip_date=date(2026, 4, 3),
            status=LogisticsTrip.STATUS_DRAFT,
            assigned_logistician=self.logistician,
            created_by=self.logistician_user,
        )
        self.client.force_login(self.logistician_user)

        response = self.client.post(
            reverse("logistics:trip-detail", args=[trip.pk]),
            data={
                "action": "update_trip",
                "trip_date": "2026-04-05",
                "status": LogisticsTrip.STATUS_DRAFT,
                "vehicle_type": LogisticsTrip.VEHICLE_FULFILLMENT,
                "vehicle_name": "Газель 2",
                "carrier_id": str(self.carrier.id),
                "vehicle_number": "a321bc777",
                "driver_name": "Петров Петр Петрович",
                "driver_phone": "8 999 222-33-44",
                "route_comment": "Маршрут обновлен",
                "loading_comment": "Грузить с задней рампы",
            },
        )

        self.assertEqual(response.status_code, 302)
        trip.refresh_from_db()
        self.assertEqual(trip.trip_date, date(2026, 4, 5))
        self.assertEqual(trip.status, LogisticsTrip.STATUS_DRAFT)
        self.assertEqual(trip.vehicle_type, LogisticsTrip.VEHICLE_FULFILLMENT)
        self.assertEqual(trip.vehicle_name, "Газель 2")
        self.assertEqual(trip.carrier, self.carrier)
        self.assertEqual(trip.vehicle_number, "А321ВС777")
        self.assertEqual(trip.driver_name, "Петров Петр Петрович")
        self.assertEqual(trip.driver_phone, "+7 999 222-33-44")
        self.assertEqual(trip.route_comment, "Маршрут обновлен")
        self.assertEqual(trip.loading_comment, "Грузить с задней рампы")

    def test_update_trip_cannot_bypass_finalize_status_validation(self):
        trip = LogisticsTrip.objects.create(
            number="DRAFT-STATUS-GUARD",
            trip_date=date(2026, 4, 3),
            status=LogisticsTrip.STATUS_DRAFT,
            assigned_logistician=self.logistician,
            created_by=self.logistician_user,
        )
        self.client.force_login(self.logistician_user)

        response = self.client.post(
            reverse("logistics:trip-detail", args=[trip.pk]),
            data={
                "action": "update_trip",
                "status": LogisticsTrip.STATUS_LOADING,
                "trip_date": "2026-04-05",
            },
        )

        self.assertEqual(response.status_code, 302)
        trip.refresh_from_db()
        self.assertEqual(trip.status, LogisticsTrip.STATUS_DRAFT)
        self.assertEqual(trip.trip_date, date(2026, 4, 3))

    def test_finalize_trip_requires_driver_data_and_creates_storekeeper_task(self):
        order = self._create_packed_order_with_warehouse_pallet(
            "SO-FINALIZE-1",
            pallet_code="SO-FINALIZE-1-PAL-1",
        )
        trip = LogisticsTrip.objects.create(
            number="TRIP-000001",
            trip_date=date(2026, 4, 3),
            status=LogisticsTrip.STATUS_PLANNED,
            assigned_logistician=self.logistician,
            created_by=self.logistician_user,
        )
        LogisticsTripOrder.objects.create(
            trip=trip,
            shipping_order=order,
            loading_sequence=1,
            delivery_sequence=1,
        )
        self.client.force_login(self.logistician_user)

        response = self.client.post(
            reverse("logistics:trip-detail", args=[trip.pk]),
            data={
                "action": "finalize_trip",
                "carrier_id": str(self.carrier.id),
                "trip_date": "2026-04-03",
                "vehicle_number": "a123bc777",
                "driver_name": "Иванов Иван Иванович",
                "driver_phone": "8 900 555-44-33",
            },
        )

        self.assertEqual(response.status_code, 302)
        trip.refresh_from_db()
        self.assertEqual(trip.status, LogisticsTrip.STATUS_LOADING)
        self.assertEqual(trip.carrier, self.carrier)
        self.assertEqual(trip.vehicle_number, "А123ВС777")
        task = Task.objects.get(route=f"/logistics/trips/{trip.pk}/", assigned_to=self.storekeeper)
        self.assertEqual(task.title, "Рейс №1_RS")
        self.assertIn("Иванов Иван Иванович", task.description)
        self.assertIn("+7 900 555-44-33", task.description)

    def test_finalize_waits_for_warehouse_packed_status(self):
        order = self._create_packed_order_with_warehouse_pallet(
            "SO-PREPLAN-FINALIZE",
            pallet_code="SO-PREPLAN-FINALIZE-PAL-1",
        )
        order.status = ShippingOrder.STATUS_PICKING
        order.save(update_fields=["status", "updated_at"])
        trip = LogisticsTrip.objects.create(
            number="DRAFT-PREPLAN-FINALIZE",
            trip_date=date(2026, 4, 3),
            status=LogisticsTrip.STATUS_DRAFT,
            carrier=self.carrier,
            vehicle_number="А123ВС777",
            driver_name="Иванов Иван Иванович",
            driver_phone="+7 900 555-44-33",
            assigned_logistician=self.logistician,
            created_by=self.logistician_user,
        )
        LogisticsTripOrder.objects.create(
            trip=trip,
            shipping_order=order,
            loading_sequence=1,
            delivery_sequence=1,
        )
        self.client.force_login(self.logistician_user)

        blocked = self.client.post(
            reverse("logistics:trip-detail", args=[trip.pk]),
            data={
                "action": "finalize_trip",
                "carrier_id": str(self.carrier.pk),
                "trip_date": "2026-04-03",
                "vehicle_number": "А123ВС777",
                "driver_name": "Иванов Иван Иванович",
                "driver_phone": "+7 900 555-44-33",
            },
        )

        self.assertEqual(blocked.status_code, 302)
        trip.refresh_from_db()
        self.assertEqual(trip.status, LogisticsTrip.STATUS_DRAFT)
        self.assertFalse(Task.objects.filter(route=f"/logistics/trips/{trip.pk}/", assigned_to=self.storekeeper).exists())

        order.status = ShippingOrder.STATUS_PACKED
        order.save(update_fields=["status", "updated_at"])
        finalized = self.client.post(
            reverse("logistics:trip-detail", args=[trip.pk]),
            data={
                "action": "finalize_trip",
                "carrier_id": str(self.carrier.pk),
                "trip_date": "2026-04-03",
                "vehicle_number": "А123ВС777",
                "driver_name": "Иванов Иван Иванович",
                "driver_phone": "+7 900 555-44-33",
            },
        )
        self.assertEqual(finalized.status_code, 302)
        trip.refresh_from_db()
        self.assertEqual(trip.status, LogisticsTrip.STATUS_LOADING)

    def test_finalize_blocks_packed_order_without_valid_warehouse_pallets(self):
        order = self._create_packed_order("SO-FINALIZE-NO-PALLETS")
        trip = LogisticsTrip.objects.create(
            number="DRAFT-NO-PALLETS",
            trip_date=date(2026, 4, 3),
            status=LogisticsTrip.STATUS_DRAFT,
            carrier=self.carrier,
            vehicle_number="А123ВС777",
            driver_name="Иванов Иван Иванович",
            driver_phone="+7 900 555-44-33",
            assigned_logistician=self.logistician,
            created_by=self.logistician_user,
        )
        LogisticsTripOrder.objects.create(
            trip=trip,
            shipping_order=order,
            loading_sequence=1,
            delivery_sequence=1,
        )
        self.client.force_login(self.logistician_user)

        response = self.client.post(
            reverse("logistics:trip-detail", args=[trip.pk]),
            data={
                "action": "finalize_trip",
                "carrier_id": str(self.carrier.pk),
                "trip_date": "2026-04-03",
                "vehicle_number": "А123ВС777",
                "driver_name": "Иванов Иван Иванович",
                "driver_phone": "+7 900 555-44-33",
            },
        )

        self.assertEqual(response.status_code, 302)
        trip.refresh_from_db()
        self.assertEqual(trip.status, LogisticsTrip.STATUS_DRAFT)
        self.assertFalse(Task.objects.filter(route=f"/logistics/trips/{trip.pk}/", assigned_to=self.storekeeper).exists())

    def test_logistician_cannot_edit_trip_after_transfer_to_storekeeper(self):
        order = self._create_packed_order("SO-LOCK-1", pallet_count=1, box_count=6)
        trip = LogisticsTrip.objects.create(
            number="3_RS",
            trip_date=date(2026, 4, 4),
            status=LogisticsTrip.STATUS_LOADING,
            vehicle_number="А123ВС777",
            driver_name="Иванов Иван Иванович",
            driver_phone="+7 900 555-44-33",
            assigned_logistician=self.logistician,
            created_by=self.logistician_user,
        )
        LogisticsTripOrder.objects.create(
            trip=trip,
            shipping_order=order,
            loading_sequence=1,
            delivery_sequence=1,
        )
        self.client.force_login(self.logistician_user)

        get_response = self.client.get(reverse("logistics:trip-detail", args=[trip.pk]))
        self.assertEqual(get_response.status_code, 200)
        self.assertContains(
            get_response,
            "Рейс передан в складской контур: параметры закрыты, но можно добавить готовые заявки ниже",
        )
        self.assertNotContains(get_response, "Сформировать рейс")
        self.assertNotContains(get_response, "Вернуть логисту на доработку")

        post_response = self.client.post(
            reverse("logistics:trip-detail", args=[trip.pk]),
            data={
                "action": "update_loading_params",
                "carrier_id": str(self.carrier.id),
                "vehicle_number": "а999аа777",
                "driver_name": "Новый Водитель",
                "driver_phone": "8 900 777-66-55",
            },
        )

        self.assertEqual(post_response.status_code, 403)
        trip.refresh_from_db()
        self.assertEqual(trip.vehicle_number, "А123ВС777")
        self.assertEqual(trip.driver_name, "Иванов Иван Иванович")

    def test_logistician_can_fill_only_missing_transport_after_transfer_to_storekeeper(self):
        trip = LogisticsTrip.objects.create(
            number="62_RS",
            trip_date=date(2026, 7, 28),
            status=LogisticsTrip.STATUS_LOADING,
            carrier=self.carrier,
            max_weight_kg=1500,
            assigned_logistician=self.logistician,
            created_by=self.logistician_user,
        )
        self.client.force_login(self.logistician_user)

        detail_response = self.client.get(reverse("logistics:trip-detail", args=[trip.pk]))
        self.assertEqual(detail_response.status_code, 200)
        self.assertFalse(detail_response.context["can_edit_trip"])
        self.assertTrue(detail_response.context["can_fill_missing_transport"])
        self.assertTrue(detail_response.context["can_edit_vehicle_number"])
        self.assertContains(detail_response, "Можно заполнить только отсутствующие данные машины и водителя")

        post_response = self.client.post(
            reverse("logistics:trip-detail", args=[trip.pk]),
            data={
                "action": "update_loading_params",
                "trip_date": "2026-08-10",
                "carrier_id": "",
                "vehicle_number": "a123bc777",
                "driver_name": "Иванов Иван Иванович",
                "driver_phone": "8 900 555-44-33",
                "max_weight_kg": "1",
            },
        )

        self.assertEqual(post_response.status_code, 302)
        trip.refresh_from_db()
        self.assertEqual(trip.status, LogisticsTrip.STATUS_LOADING)
        self.assertEqual(trip.trip_date, date(2026, 7, 28))
        self.assertEqual(trip.carrier, self.carrier)
        self.assertEqual(trip.max_weight_kg, 1500)
        self.assertEqual(trip.vehicle_number, "А123ВС777")
        self.assertEqual(trip.driver_name, "Иванов Иван Иванович")
        self.assertEqual(trip.driver_phone, "+7 900 555-44-33")

        overwrite_response = self.client.post(
            reverse("logistics:trip-detail", args=[trip.pk]),
            data={
                "action": "update_loading_params",
                "vehicle_number": "а999аа777",
                "driver_name": "Другой Водитель",
                "driver_phone": "8 900 777-66-55",
            },
        )
        self.assertEqual(overwrite_response.status_code, 403)
        trip.refresh_from_db()
        self.assertEqual(trip.vehicle_number, "А123ВС777")
        self.assertEqual(trip.driver_name, "Иванов Иван Иванович")

    def test_storekeeper_can_return_trip_to_logistician_for_rework(self):
        order = self._create_packed_order("SO-RETURN-1", pallet_count=1, box_count=6)
        trip = LogisticsTrip.objects.create(
            number="3_RS",
            trip_date=date(2026, 4, 4),
            status=LogisticsTrip.STATUS_LOADING,
            vehicle_number="А123ВС777",
            driver_name="Иванов Иван Иванович",
            driver_phone="+7 900 555-44-33",
            assigned_logistician=self.logistician,
            created_by=self.logistician_user,
        )
        LogisticsTripOrder.objects.create(
            trip=trip,
            shipping_order=order,
            loading_sequence=1,
            delivery_sequence=1,
        )
        storekeeper_task = Task.objects.create(
            title="Рейс №3_RS",
            description="Ожидает машину",
            route=f"/logistics/trips/{trip.pk}/",
            assigned_to=self.storekeeper,
            created_by=self.logistician_user,
        )
        self.client.force_login(self.storekeeper_user)

        get_response = self.client.get(reverse("logistics:trip-detail", args=[trip.pk]))
        self.assertEqual(get_response.status_code, 200)
        self.assertContains(get_response, "Вернуть логисту на доработку")
        self.assertContains(
            get_response,
            "Если в составе или маршруте нужны изменения, сначала верни его логисту на доработку.",
        )

        post_response = self.client.post(
            reverse("logistics:trip-detail", args=[trip.pk]),
            data={"action": "return_to_logistic"},
        )

        self.assertEqual(post_response.status_code, 302)
        trip.refresh_from_db()
        storekeeper_task.refresh_from_db()
        self.assertEqual(trip.status, LogisticsTrip.STATUS_PLANNED)
        self.assertEqual(storekeeper_task.status, "done")

        logistician_task = Task.objects.exclude(pk=storekeeper_task.pk).get(
            route=f"/logistics/trips/{trip.pk}/",
            assigned_to=self.logistician,
        )
        self.assertEqual(logistician_task.status, "backlog")
        self.assertIn("вернул рейс 3_RS логисту на доработку", logistician_task.description)

        self.client.force_login(self.logistician_user)
        detail_response = self.client.get(reverse("logistics:trip-detail", args=[trip.pk]))
        self.assertContains(detail_response, "Сформировать рейс")
        self.assertContains(detail_response, "Добавить готовые заявки")
        self.assertContains(detail_response, "<title>3_RS | Логистика | FULLBOX</title>", html=False)

    def test_storekeeper_can_open_trip_detail_readonly(self):
        order = self._create_packed_order("SO-FINALIZE-2", pallet_count=1, box_count=6)
        trip = LogisticsTrip.objects.create(
            number="TRIP-000002",
            trip_date=date(2026, 4, 4),
            status=LogisticsTrip.STATUS_LOADING,
            vehicle_number="А123ВС777",
            driver_name="Иванов Иван Иванович",
            driver_phone="+7 900 555-44-33",
            assigned_logistician=self.logistician,
            created_by=self.logistician_user,
        )
        LogisticsTripOrder.objects.create(
            trip=trip,
            shipping_order=order,
            loading_sequence=1,
            delivery_sequence=1,
        )
        self.client.force_login(self.storekeeper_user)

        response = self.client.get(reverse("logistics:trip-detail", args=[trip.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Вернуть логисту на доработку")
        self.assertNotContains(response, "Сформировать рейс")
        self.assertContains(response, 'href="/sklad/"', html=False)
        self.assertContains(response, "В кабинет")
        self.assertContains(response, f'/shipping/{order.pk}/documents/')
        self.assertNotContains(response, f'/shipping/{order.pk}/act/')

    def test_storekeeper_trip_detail_shows_loading_button(self):
        order = self._create_packed_order_with_pallets(
            "SO-LOAD-BTN-1",
            [
                {"label": "Паллета 1", "code": "SHIP-1-PAL-1", "boxes": [{"code": "BOX-1", "qty": 2}]},
            ],
        )
        trip = LogisticsTrip.objects.create(
            number="TRIP-000020",
            trip_date=date(2026, 4, 4),
            status=LogisticsTrip.STATUS_LOADING,
            assigned_logistician=self.logistician,
            created_by=self.logistician_user,
        )
        LogisticsTripOrder.objects.create(
            trip=trip,
            shipping_order=order,
            loading_sequence=1,
            delivery_sequence=1,
        )
        self.client.force_login(self.storekeeper_user)

        response = self.client.get(reverse("logistics:trip-detail", args=[trip.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Приступить к погрузке")
        self.assertContains(response, reverse("logistics:trip-loading", args=[trip.pk]))

    def test_trip_loading_page_shows_orders_in_reverse_delivery_order(self):
        first_order = self._create_packed_order_with_pallets(
            "SO-LOAD-ORDER-1",
            [{"label": "Паллета 1", "code": "SO-LOAD-ORDER-1-PAL-1", "boxes": [{"code": "BOX-1", "qty": 1}]}],
        )
        second_order = self._create_packed_order_with_pallets(
            "SO-LOAD-ORDER-2",
            [{"label": "Паллета 1", "code": "SO-LOAD-ORDER-2-PAL-1", "boxes": [{"code": "BOX-2", "qty": 1}]}],
        )
        trip = LogisticsTrip.objects.create(
            number="TRIP-000021",
            trip_date=date(2026, 4, 4),
            status=LogisticsTrip.STATUS_LOADING,
            assigned_logistician=self.logistician,
            created_by=self.logistician_user,
        )
        LogisticsTripOrder.objects.create(
            trip=trip,
            shipping_order=first_order,
            loading_sequence=1,
            delivery_sequence=1,
        )
        LogisticsTripOrder.objects.create(
            trip=trip,
            shipping_order=second_order,
            loading_sequence=2,
            delivery_sequence=2,
        )
        self.client.force_login(self.storekeeper_user)

        response = self.client.get(reverse("logistics:trip-loading", args=[trip.pk]))

        self.assertEqual(response.status_code, 200)
        content = response.content.decode("utf-8")
        self.assertIn("Погрузка рейса", content)
        self.assertLess(content.index("2_OTG"), content.index("1_OTG"))

    def test_storekeeper_cannot_start_planned_trip_before_logistician_finalize(self):
        order = self._create_packed_order_with_pallets(
            "SO-LOAD-START-1",
            [{"label": "Паллета 1", "code": "SO-LOAD-START-1-PAL-1", "boxes": [{"code": "BOX-1", "qty": 1}]}],
        )
        trip = LogisticsTrip.objects.create(
            number="TRIP-000021A",
            trip_date=date(2026, 4, 4),
            status=LogisticsTrip.STATUS_PLANNED,
            assigned_logistician=self.logistician,
            created_by=self.logistician_user,
        )
        LogisticsTripOrder.objects.create(
            trip=trip,
            shipping_order=order,
            loading_sequence=1,
            delivery_sequence=1,
        )
        self.client.force_login(self.storekeeper_user)

        response = self.client.post(
            reverse("logistics:trip-detail", args=[trip.pk]),
            data={"action": "start_loading"},
        )

        self.assertEqual(response.status_code, 403)
        trip.refresh_from_db()
        self.assertEqual(trip.status, LogisticsTrip.STATUS_PLANNED)

    def test_trip_loading_scan_marks_wrong_order_red_and_correct_order_green(self):
        first_order = self._create_packed_order_with_pallets(
            "SO-LOAD-SCAN-1",
            [{"label": "Паллета 1", "code": "SO-LOAD-SCAN-1-PAL-1", "boxes": [{"code": "BOX-1", "qty": 1}]}],
        )
        second_order = self._create_packed_order_with_pallets(
            "SO-LOAD-SCAN-2",
            [{"label": "Паллета 1", "code": "SO-LOAD-SCAN-2-PAL-1", "boxes": [{"code": "BOX-2", "qty": 1}]}],
        )
        trip = LogisticsTrip.objects.create(
            number="TRIP-000022",
            trip_date=date(2026, 4, 4),
            status=LogisticsTrip.STATUS_LOADING,
            assigned_logistician=self.logistician,
            created_by=self.logistician_user,
        )
        LogisticsTripOrder.objects.create(
            trip=trip,
            shipping_order=first_order,
            loading_sequence=1,
            delivery_sequence=1,
        )
        LogisticsTripOrder.objects.create(
            trip=trip,
            shipping_order=second_order,
            loading_sequence=2,
            delivery_sequence=2,
        )
        self.client.force_login(self.storekeeper_user)

        wrong_response = self.client.post(
            reverse("logistics:trip-loading", args=[trip.pk]),
            data={"scan_value": "SO-LOAD-SCAN-1::SO-LOAD-SCAN-1-PAL-1"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(wrong_response.status_code, 409)
        wrong_payload = wrong_response.json()
        self.assertFalse(wrong_payload["result"]["ok"])
        self.assertEqual(wrong_payload["result"]["tone"], "error")
        self.assertIn("Сейчас очередь заявки LOAD-SCAN-2_OTG", wrong_payload["result"]["message"])

        ok_response = self.client.post(
            reverse("logistics:trip-loading", args=[trip.pk]),
            data={"scan_value": "SO-LOAD-SCAN-2::SO-LOAD-SCAN-2-PAL-1"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(ok_response.status_code, 200)
        ok_payload = ok_response.json()
        self.assertTrue(ok_payload["result"]["ok"])
        self.assertEqual(ok_payload["result"]["tone"], "success")
        self.assertEqual(ok_payload["loaded_total"], 1)
        self.assertEqual(ok_payload["current_order_number"], "LOAD-SCAN-1_OTG")

        audit_entry = OrderAuditEntry.objects.filter(
            order_id=str(trip.pk),
            order_type="logistics_trip",
            payload__act="trip_loading_progress",
        ).first()
        self.assertIsNotNone(audit_entry)
        self.assertEqual(audit_entry.payload["loaded_pallet_keys"], ["SO-LOAD-SCAN-2-PAL-1"])

    def test_trip_loading_keeps_duplicate_legacy_pallet_codes_separate_by_order(self):
        second_agency = Agency.objects.create(
            agn_name="Второй клиент погрузки",
            pref="SECOND",
        )
        first_order = self._create_packed_order_with_warehouse_pallet(
            "SO-LOAD-DUP-1",
            pallet_code="PAL-1",
        )
        second_order = self._create_packed_order_with_warehouse_pallet(
            "SO-LOAD-DUP-2",
            pallet_code="PAL-1",
            agency=second_agency,
        )
        trip = LogisticsTrip.objects.create(
            number="TRIP-000022-DUP",
            trip_date=date(2026, 4, 4),
            status=LogisticsTrip.STATUS_LOADING,
            assigned_logistician=self.logistician,
            created_by=self.logistician_user,
        )
        LogisticsTripOrder.objects.create(
            trip=trip,
            shipping_order=first_order,
            loading_sequence=1,
            delivery_sequence=1,
        )
        LogisticsTripOrder.objects.create(
            trip=trip,
            shipping_order=second_order,
            loading_sequence=2,
            delivery_sequence=2,
        )
        WarehouseWritePathService.assign_to_trip(
            agency=second_agency,
            order_id=second_order.number,
            trip_id=trip.number,
            assigned_by=self.logistician_user,
        )
        loading = WarehouseWritePathService.start_loading(
            agency=second_agency,
            order_id=second_order.number,
            trip_id=trip.number,
            started_by=self.storekeeper_user,
        )
        WarehouseWritePathService.complete_loading(
            operation=loading,
            performed_by=self.storekeeper_user,
        )
        OrderAuditEntry.objects.create(
            order_id=str(trip.pk),
            order_type="logistics_trip",
            action="update",
            agency=second_agency,
            user=self.storekeeper_user,
            description="Старый формат аудита погрузки",
            payload={
                "act": "trip_loading_progress",
                "trip_pk": trip.pk,
                "loaded_pallet_keys": ["PAL-1"],
                "last_loaded_key": "PAL-1",
            },
        )
        self.client.force_login(self.storekeeper_user)

        page_response = self.client.get(reverse("logistics:trip-loading", args=[trip.pk]))

        self.assertEqual(page_response.status_code, 200)
        self.assertEqual(page_response.context["loading_state_payload"]["loaded_total"], 1)
        self.assertFalse(page_response.context["loading_state_payload"]["all_complete"])
        self.assertEqual(
            page_response.context["loading_state_payload"]["current_order_id"],
            first_order.pk,
        )

        scan_response = self.client.post(
            reverse("logistics:trip-loading", args=[trip.pk]),
            data={"scan_value": "PAL-1"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(scan_response.status_code, 200)
        scan_payload = scan_response.json()
        self.assertTrue(scan_payload["result"]["ok"])
        self.assertEqual(scan_payload["loaded_total"], 2)
        self.assertTrue(scan_payload["all_complete"])
        latest_progress = OrderAuditEntry.objects.filter(
            order_id=str(trip.pk),
            order_type="logistics_trip",
            payload__act="trip_loading_progress",
        ).order_by("-created_at").first()
        self.assertEqual(
            latest_progress.payload["loaded_pallet_keys"],
            ["PAL-1", f"{first_order.pk}::PAL-1"],
        )
        first_snapshot = WarehouseStockSnapshot.objects.get(
            agency=self.agency,
            parent_container__container_code="PAL-1",
        )
        self.assertEqual(first_snapshot.current_trip_id, trip.number)
        self.assertEqual(first_snapshot.warehouse_state_code, "loaded_to_vehicle")

    def test_trip_loading_keeps_unique_pallet_scan_compatible(self):
        pallet_code = "TDT-PAL-0001"
        order = self._create_packed_order_with_warehouse_pallet(
            "SO-LOAD-UNIQUE-1",
            pallet_code=pallet_code,
        )
        trip = LogisticsTrip.objects.create(
            number="TRIP-000022-UNIQUE",
            trip_date=date(2026, 4, 4),
            status=LogisticsTrip.STATUS_LOADING,
            assigned_logistician=self.logistician,
            created_by=self.logistician_user,
        )
        LogisticsTripOrder.objects.create(
            trip=trip,
            shipping_order=order,
            loading_sequence=1,
            delivery_sequence=1,
        )
        self.client.force_login(self.storekeeper_user)

        response = self.client.post(
            reverse("logistics:trip-loading", args=[trip.pk]),
            data={"scan_value": pallet_code},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["result"]["ok"])
        self.assertEqual(payload["loaded_total"], 1)
        self.assertTrue(payload["all_complete"])
        progress_entry = OrderAuditEntry.objects.filter(
            order_id=str(trip.pk),
            order_type="logistics_trip",
            payload__act="trip_loading_progress",
        ).order_by("-created_at").first()
        self.assertEqual(progress_entry.payload["loaded_pallet_keys"], [pallet_code])
        snapshot = WarehouseStockSnapshot.objects.get(
            agency=self.agency,
            parent_container__container_code=pallet_code,
        )
        self.assertEqual(snapshot.current_trip_id, trip.number)
        self.assertEqual(snapshot.warehouse_state_code, "loaded_to_vehicle")

    def test_pallet_codes_use_client_prefix_and_independent_four_digit_sequence(self):
        first_agency = Agency.objects.create(agn_name="Клиент TDT", pref="TDT")
        second_agency = Agency.objects.create(agn_name="Клиент EGR", pref="EGR")

        with patch(
            "fullbox.container_codes._issue_container_sequence_number",
            side_effect=[1, 2, 1],
        ):
            first_code = issue_pallet_code(
                agency=first_agency,
                goods_type="Готовый",
                order_type="shipping",
                order_id="SO-100",
            )
            second_code = issue_pallet_code(
                agency=first_agency,
                goods_type="Готовый",
                order_type="shipping",
                order_id="SO-101",
            )
            other_client_code = issue_pallet_code(
                agency=second_agency,
                goods_type="Готовый",
                order_type="shipping",
                order_id="SO-200",
            )

        self.assertEqual(first_code["code"], "TDT-OTG-0001")
        self.assertEqual(second_code["code"], "TDT-OTG-0002")
        self.assertEqual(other_client_code["code"], "EGR-OTG-0001")

    def test_box_code_format_is_unchanged(self):
        agency = Agency.objects.create(agn_name="Клиент коробов", pref="BOX")

        with patch("fullbox.container_codes._issue_container_sequence_number", return_value=1):
            issued = issue_container_code(agency=agency, goods_type="Готовый")

        self.assertRegex(issued["code"], r"^BOX-\d{4}-000001-gv$")

    def test_trip_loading_page_renders_scanner_agent_block(self):
        order = self._create_packed_order_with_pallets(
            "SO-LOAD-AGENT-1",
            [{"label": "Паллета 1", "code": "SO-LOAD-AGENT-1-PAL-1", "boxes": [{"code": "BOX-1", "qty": 1}]}],
        )
        trip = LogisticsTrip.objects.create(
            number="TRIP-000023",
            trip_date=date(2026, 4, 4),
            status=LogisticsTrip.STATUS_LOADING,
            assigned_logistician=self.logistician,
            created_by=self.logistician_user,
        )
        LogisticsTripOrder.objects.create(
            trip=trip,
            shipping_order=order,
            loading_sequence=1,
            delivery_sequence=1,
        )
        DeviceAgent.objects.create(
            agent_id="trip-loading-agent-1",
            name="Тестовый агент",
            host="workstation-1",
            version="1.0.0",
            last_seen=timezone.now(),
            meta={"com_status": {"connected": True, "enabled": True, "port": "COM7"}},
        )
        self.client.force_login(self.storekeeper_user)

        response = self.client.get(reverse("logistics:trip-loading", args=[trip.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Сканер")
        self.assertContains(response, "trip-loading-agent-1")
        self.assertContains(response, "Перехватить сканер")

    def test_trip_loading_page_shows_documents_and_finish_button_after_full_loading(self):
        pallet_code = "SO-LOAD-DOCS-1-PAL-1"
        order = self._create_packed_order_with_warehouse_pallet(
            "SO-LOAD-DOCS-1",
            pallet_code=pallet_code,
        )
        trip = LogisticsTrip.objects.create(
            number="TRIP-000024",
            trip_date=date(2026, 4, 4),
            status=LogisticsTrip.STATUS_LOADING,
            assigned_logistician=self.logistician,
            created_by=self.logistician_user,
        )
        LogisticsTripOrder.objects.create(
            trip=trip,
            shipping_order=order,
            loading_sequence=1,
            delivery_sequence=1,
        )
        OrderAuditEntry.objects.create(
            order_id=str(trip.pk),
            order_type="logistics_trip",
            action="update",
            agency=self.agency,
            user=self.storekeeper_user,
            description="Все паллеты погружены",
            payload={
                "act": "trip_loading_progress",
                "trip_pk": trip.pk,
                "loaded_pallet_keys": [pallet_code],
                "last_loaded_key": pallet_code,
            },
        )
        self.client.force_login(self.storekeeper_user)
        self._scan_trip_pallet(trip, pallet_code)

        response = self.client.get(reverse("logistics:trip-loading", args=[trip.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Погрузка завершена")
        self.assertContains(response, "Транспортная накладная")
        self.assertContains(response, "Акт возврата")
        self.assertContains(response, "Завершить погрузку")
        self.assertContains(response, "Фактически оказанные услуги")
        self.assertContains(response, 'id="shipping-service-targets"', html=False)
        self.assertContains(response, order.number)
        self.assertContains(response, f'/shipping/{order.pk}/transport-note/docx/')
        self.assertContains(response, f'/shipping/{order.pk}/return-act/doc/')

    def test_trip_detail_shows_loaded_status_for_fully_loaded_order(self):
        order = self._create_packed_order_with_pallets(
            "SO-LOAD-STATUS-1",
            [{"label": "Паллета 1", "code": "SO-LOAD-STATUS-1-PAL-1", "boxes": [{"code": "BOX-1", "qty": 1}]}],
        )
        trip = LogisticsTrip.objects.create(
            number="TRIP-000024A",
            trip_date=date(2026, 4, 4),
            status=LogisticsTrip.STATUS_LOADING,
            assigned_logistician=self.logistician,
            created_by=self.logistician_user,
        )
        LogisticsTripOrder.objects.create(
            trip=trip,
            shipping_order=order,
            loading_sequence=1,
            delivery_sequence=1,
        )
        OrderAuditEntry.objects.create(
            order_id=str(trip.pk),
            order_type="logistics_trip",
            action="update",
            agency=self.agency,
            user=self.storekeeper_user,
            description="Все паллеты погружены",
            payload={
                "act": "trip_loading_progress",
                "trip_pk": trip.pk,
                "loaded_pallet_keys": ["SO-LOAD-STATUS-1-PAL-1"],
                "last_loaded_key": "SO-LOAD-STATUS-1-PAL-1",
            },
        )
        self.client.force_login(self.storekeeper_user)

        response = self.client.get(reverse("logistics:trip-detail", args=[trip.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Загружено в машину")
        self.assertNotContains(response, "Подготовка к рейсу")

    def test_trip_detail_shows_loaded_status_from_warehouse_without_audit_progress(self):
        pallet_code = "SO-LOAD-STATUS-WH-1-PAL-1"
        order = self._create_packed_order_with_warehouse_pallet("SO-LOAD-STATUS-WH-1", pallet_code=pallet_code)
        trip = LogisticsTrip.objects.create(
            number="TRIP-000024WH",
            trip_date=date(2026, 4, 4),
            status=LogisticsTrip.STATUS_LOADING,
            assigned_logistician=self.logistician,
            created_by=self.logistician_user,
        )
        LogisticsTripOrder.objects.create(
            trip=trip,
            shipping_order=order,
            loading_sequence=1,
            delivery_sequence=1,
        )
        WarehouseWritePathService.assign_to_trip(
            agency=self.agency,
            order_id=order.number,
            trip_id=trip.number,
            assigned_by=self.logistician_user,
        )
        loading = WarehouseWritePathService.start_loading(
            agency=self.agency,
            order_id=order.number,
            trip_id=trip.number,
            started_by=self.storekeeper_user,
        )
        WarehouseWritePathService.complete_loading(operation=loading, performed_by=self.storekeeper_user)
        self.client.force_login(self.storekeeper_user)

        response = self.client.get(reverse("logistics:trip-detail", args=[trip.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Загружено в машину")
        self.assertNotContains(response, "Подготовка к рейсу")

    def test_departed_trip_detail_hides_loading_button_and_shows_view_banner(self):
        order = self._create_packed_order_with_pallets(
            "SO-LOAD-DEPARTED-1",
            [{"label": "Паллета 1", "code": "SO-LOAD-DEPARTED-1-PAL-1", "boxes": [{"code": "BOX-1", "qty": 1}]}],
        )
        trip = LogisticsTrip.objects.create(
            number="TRIP-000024B",
            trip_date=date(2026, 4, 4),
            status=LogisticsTrip.STATUS_DEPARTED,
            assigned_logistician=self.logistician,
            created_by=self.logistician_user,
        )
        LogisticsTripOrder.objects.create(
            trip=trip,
            shipping_order=order,
            loading_sequence=1,
            delivery_sequence=1,
        )
        self.client.force_login(self.storekeeper_user)

        response = self.client.get(reverse("logistics:trip-detail", args=[trip.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Погрузка завершена")
        self.assertContains(response, "Отметьте сдачу каждой заявки на маркетплейс")
        self.assertNotContains(response, "Приступить к погрузке")

    def test_storekeeper_can_finish_loading_and_trip_moves_to_departed(self):
        pallet_code = "SO-LOAD-FINISH-1-PAL-1"
        order = self._create_packed_order_with_warehouse_pallet(
            "SO-LOAD-FINISH-1",
            pallet_code=pallet_code,
        )
        trip = LogisticsTrip.objects.create(
            number="TRIP-000025",
            trip_date=date(2026, 4, 4),
            status=LogisticsTrip.STATUS_LOADING,
            assigned_logistician=self.logistician,
            created_by=self.logistician_user,
        )
        LogisticsTripOrder.objects.create(
            trip=trip,
            shipping_order=order,
            loading_sequence=1,
            delivery_sequence=1,
        )
        storekeeper_task = Task.objects.create(
            title="Рейс №25_RS",
            description="Погрузка рейса",
            route=f"/logistics/trips/{trip.pk}/",
            assigned_to=self.storekeeper,
            created_by=self.logistician_user,
        )
        OrderAuditEntry.objects.create(
            order_id=str(trip.pk),
            order_type="logistics_trip",
            action="update",
            agency=self.agency,
            user=self.storekeeper_user,
            description="Все паллеты погружены",
            payload={
                "act": "trip_loading_progress",
                "trip_pk": trip.pk,
                "loaded_pallet_keys": [pallet_code],
                "last_loaded_key": pallet_code,
            },
        )
        self._add_shipping_fact(order)
        self.client.force_login(self.storekeeper_user)
        self._scan_trip_pallet(trip, pallet_code)

        response = self.client.post(
            reverse("logistics:trip-loading", args=[trip.pk]),
            data={"action": "finish_loading"},
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], reverse("logistics:trip-detail", args=[trip.pk]))
        trip.refresh_from_db()
        storekeeper_task.refresh_from_db()
        self.assertEqual(trip.status, LogisticsTrip.STATUS_DEPARTED)
        self.assertEqual(storekeeper_task.status, "done")
        application = BillingApplication.objects.get(
            application_type=BillingApplication.TYPE_SHIPPING,
            application_id=order.number,
            client=order.agency,
        )
        self.assertEqual(application.operational_status, "loaded")
        self.assertEqual(application.operational_status_label, "Погрузка завершена складом")

    def test_finish_loading_without_shipping_facts_keeps_trip_in_loading(self):
        pallet_code = "SO-LOAD-NO-SERVICES-1-PAL-1"
        order = self._create_packed_order_with_warehouse_pallet(
            "SO-LOAD-NO-SERVICES-1",
            pallet_code=pallet_code,
        )
        trip = LogisticsTrip.objects.create(
            number="TRIP-000025-NO-SERVICES",
            trip_date=date(2026, 4, 4),
            status=LogisticsTrip.STATUS_LOADING,
            assigned_logistician=self.logistician,
            created_by=self.logistician_user,
        )
        LogisticsTripOrder.objects.create(
            trip=trip,
            shipping_order=order,
            loading_sequence=1,
            delivery_sequence=1,
        )
        OrderAuditEntry.objects.create(
            order_id=str(trip.pk),
            order_type="logistics_trip",
            action="update",
            agency=self.agency,
            user=self.storekeeper_user,
            description="Все паллеты погружены",
            payload={
                "act": "trip_loading_progress",
                "trip_pk": trip.pk,
                "loaded_pallet_keys": [pallet_code],
                "last_loaded_key": pallet_code,
            },
        )
        self.client.force_login(self.storekeeper_user)
        self._scan_trip_pallet(trip, pallet_code)

        response = self.client.post(
            reverse("logistics:trip-loading", args=[trip.pk]),
            data={"action": "finish_loading"},
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], reverse("logistics:trip-loading", args=[trip.pk]))
        trip.refresh_from_db()
        self.assertEqual(trip.status, LogisticsTrip.STATUS_LOADING)
        self.assertFalse(
            BillingApplication.objects.filter(
                application_type=BillingApplication.TYPE_SHIPPING,
                application_id=order.number,
                client=order.agency,
            ).exists()
        )

    def test_finish_loading_syncs_warehouse_snapshots_to_loaded_vehicle(self):
        pallet_code = "SO-LOAD-WH-1-PAL-1"
        order = self._create_packed_order_with_warehouse_pallet("SO-LOAD-WH-1", pallet_code=pallet_code)
        trip = LogisticsTrip.objects.create(
            number="TRIP-000026",
            trip_date=date(2026, 4, 4),
            status=LogisticsTrip.STATUS_LOADING,
            assigned_logistician=self.logistician,
            created_by=self.logistician_user,
        )
        LogisticsTripOrder.objects.create(
            trip=trip,
            shipping_order=order,
            loading_sequence=1,
            delivery_sequence=1,
        )
        storekeeper_task = Task.objects.create(
            title="Рейс №26_RS",
            description="Погрузка рейса",
            route=f"/logistics/trips/{trip.pk}/",
            assigned_to=self.storekeeper,
            created_by=self.logistician_user,
        )
        OrderAuditEntry.objects.create(
            order_id=str(trip.pk),
            order_type="logistics_trip",
            action="update",
            agency=self.agency,
            user=self.storekeeper_user,
            description="Все паллеты погружены",
            payload={
                "act": "trip_loading_progress",
                "trip_pk": trip.pk,
                "loaded_pallet_keys": [pallet_code],
                "last_loaded_key": pallet_code,
            },
        )
        self._add_shipping_fact(order)
        self.client.force_login(self.storekeeper_user)
        self._scan_trip_pallet(trip, pallet_code)

        response = self.client.post(
            reverse("logistics:trip-loading", args=[trip.pk]),
            data={"action": "finish_loading"},
        )

        self.assertEqual(response.status_code, 302)
        trip.refresh_from_db()
        storekeeper_task.refresh_from_db()
        snapshot = WarehouseStockSnapshot.objects.get(
            agency=self.agency,
            parent_container__container_code=pallet_code,
        )
        response_messages = [str(message) for message in response.wsgi_request._messages]
        self.assertEqual(trip.status, LogisticsTrip.STATUS_DEPARTED, response_messages)
        self.assertEqual(storekeeper_task.status, "done")
        self.assertEqual(snapshot.current_trip_id, trip.number)
        self.assertEqual(snapshot.warehouse_state_code, "loaded_to_vehicle")
        self.assertTrue(snapshot.is_in_vehicle)

    def test_departure_reconcile_uses_loaded_barcode_fact_and_creates_pickup_act(self):
        from logistics.services import _ship_trip_orders_after_departure

        pallet_code = "SO-LOAD-ACT-PICKUP-PAL-1"
        order = self._create_packed_order_with_warehouse_pallet(
            "SO-LOAD-ACT-PICKUP",
            pallet_code=pallet_code,
        )
        order.delivery_type = ShippingOrder.DELIVERY_PICKUP
        order.save(update_fields=["delivery_type", "updated_at"])
        item = order.items.get()
        item.qty_requested = 12
        item.qty_reserved = 12
        item.save(update_fields=["qty_requested", "qty_reserved", "updated_at"])
        reserve = WarehouseReserve.objects.get(
            agency=self.agency,
            context_type="shipping",
            context_id=order.number,
        )
        reserve.qty_satisfied = 10
        reserve.status = WarehouseReserve.STATUS_SATISFIED
        reserve.save(update_fields=["qty_satisfied", "status", "updated_at"])
        trip = LogisticsTrip.objects.create(
            number="TRIP-LOAD-ACT-PICKUP",
            trip_date=date(2026, 4, 4),
            status=LogisticsTrip.STATUS_LOADING,
            assigned_logistician=self.logistician,
            created_by=self.logistician_user,
        )
        LogisticsTripOrder.objects.create(
            trip=trip,
            shipping_order=order,
            loading_sequence=1,
            delivery_sequence=1,
        )
        self.client.force_login(self.storekeeper_user)
        self._scan_trip_pallet(trip, pallet_code)

        _ship_trip_orders_after_departure(trip, user=self.storekeeper_user)

        order.refresh_from_db()
        item.refresh_from_db()
        snapshot = WarehouseStockSnapshot.objects.get(
            agency=self.agency,
            parent_container__container_code=pallet_code,
        )
        self.assertEqual(order.status, ShippingOrder.STATUS_PARTIAL)
        self.assertEqual(item.qty_shipped, 10)
        self.assertEqual(item.qty_reserved, 2)
        self.assertEqual(snapshot.warehouse_state_code, "shipped")
        dispatch_entry = OrderAuditEntry.objects.filter(
            order_type="shipping",
            order_id=order.number,
            payload__act="shipping_dispatch_act",
        ).latest("created_at")
        self.assertTrue(dispatch_entry.payload["act_warehouse_confirmed"])
        self.assertEqual(dispatch_entry.payload["act_items"][0]["qty_requested"], 12)
        self.assertEqual(dispatch_entry.payload["act_items"][0]["qty_shipped"], 10)
        self.assertTrue(
            Task.objects.filter(
                route=f"/shipping/{order.pk}/act/",
                assigned_to=self.logistician,
                status="backlog",
            ).exists()
        )

    def test_departure_reconcile_does_not_create_act_or_ship_transfer(self):
        from logistics.services import _ship_trip_orders_after_departure

        pallet_code = "SO-LOAD-ACT-TRANSFER-PAL-1"
        order = self._create_packed_order_with_warehouse_pallet(
            "SO-LOAD-ACT-TRANSFER",
            pallet_code=pallet_code,
        )
        order.delivery_type = ShippingOrder.DELIVERY_TRANSFER
        order.save(update_fields=["delivery_type", "updated_at"])
        trip = LogisticsTrip.objects.create(
            number="TRIP-LOAD-ACT-TRANSFER",
            trip_date=date(2026, 4, 4),
            status=LogisticsTrip.STATUS_LOADING,
            assigned_logistician=self.logistician,
            created_by=self.logistician_user,
        )
        LogisticsTripOrder.objects.create(
            trip=trip,
            shipping_order=order,
            loading_sequence=1,
            delivery_sequence=1,
        )
        self.client.force_login(self.storekeeper_user)
        self._scan_trip_pallet(trip, pallet_code)

        _ship_trip_orders_after_departure(trip, user=self.storekeeper_user)

        order.refresh_from_db()
        snapshot = WarehouseStockSnapshot.objects.get(
            agency=self.agency,
            parent_container__container_code=pallet_code,
        )
        self.assertEqual(order.status, ShippingOrder.STATUS_PACKED)
        self.assertEqual(snapshot.warehouse_state_code, "loaded_to_vehicle")
        self.assertFalse(
            OrderAuditEntry.objects.filter(
                order_type="shipping",
                order_id=order.number,
                payload__act="shipping_dispatch_act",
            ).exists()
        )

    def test_finish_loading_syncs_routing_to_in_transit(self):
        pallet_code = "SO-LOAD-ROUTE-1-PAL-1"
        order = self._create_packed_order_with_warehouse_pallet(
            "SO-LOAD-ROUTE-1",
            pallet_code=pallet_code,
        )
        trip = LogisticsTrip.objects.create(
            number="TRIP-000027",
            trip_date=date(2026, 4, 4),
            status=LogisticsTrip.STATUS_LOADING,
            assigned_logistician=self.logistician,
            created_by=self.logistician_user,
        )
        LogisticsTripOrder.objects.create(
            trip=trip,
            shipping_order=order,
            loading_sequence=1,
            delivery_sequence=1,
        )
        OrderAuditEntry.objects.create(
            order_id=str(trip.pk),
            order_type="logistics_trip",
            action="update",
            agency=self.agency,
            user=self.storekeeper_user,
            description="Все паллеты погружены",
            payload={
                "act": "trip_loading_progress",
                "trip_pk": trip.pk,
                "loaded_pallet_keys": [pallet_code],
                "last_loaded_key": pallet_code,
            },
        )
        self._add_shipping_fact(order)
        self.client.force_login(self.storekeeper_user)
        self._scan_trip_pallet(trip, pallet_code)

        response = self.client.post(
            reverse("logistics:trip-loading", args=[trip.pk]),
            data={"action": "finish_loading"},
        )

        self.assertEqual(response.status_code, 302)
        trip.refresh_from_db()
        self.assertEqual(trip.status, LogisticsTrip.STATUS_DEPARTED)
        routing = ShippingRoutingState.objects.get(shipping_order=order)
        self.assertEqual(routing.status, ShippingRoutingState.STATUS_IN_TRANSIT)

    def test_logistician_mark_delivered_completes_trip_on_last_order(self):
        order_a = self._create_packed_order_with_pallets(
            "SO-MP-DEL-A",
            [{"label": "Паллета A", "code": "SO-MP-DEL-A-PAL-1", "boxes": [{"code": "BOX-A1", "qty": 1}]}],
        )
        order_b = self._create_packed_order_with_pallets(
            "SO-MP-DEL-B",
            [{"label": "Паллета B", "code": "SO-MP-DEL-B-PAL-1", "boxes": [{"code": "BOX-B1", "qty": 1}]}],
        )
        trip = LogisticsTrip.objects.create(
            number="TRIP-000028",
            trip_date=date(2026, 4, 5),
            status=LogisticsTrip.STATUS_DEPARTED,
            assigned_logistician=self.logistician,
            created_by=self.logistician_user,
        )
        LogisticsTripOrder.objects.create(
            trip=trip, shipping_order=order_a, loading_sequence=1, delivery_sequence=1
        )
        LogisticsTripOrder.objects.create(
            trip=trip, shipping_order=order_b, loading_sequence=2, delivery_sequence=2
        )
        self._add_logistics_fact(trip)
        self.client.force_login(self.logistician_user)

        first = self.client.post(
            reverse("logistics:trip-detail", args=[trip.pk]),
            data={"action": "mark_delivered", "shipping_order_id": order_a.pk},
        )
        self.assertEqual(first.status_code, 302)
        trip.refresh_from_db()
        self.assertEqual(trip.status, LogisticsTrip.STATUS_DEPARTED)
        self.assertEqual(
            ShippingRoutingState.objects.get(shipping_order=order_a).status,
            ShippingRoutingState.STATUS_DELIVERED,
        )

        second = self.client.post(
            reverse("logistics:trip-detail", args=[trip.pk]),
            data={"action": "mark_delivered", "shipping_order_id": order_b.pk},
        )
        self.assertEqual(second.status_code, 302)
        trip.refresh_from_db()
        self.assertEqual(trip.status, LogisticsTrip.STATUS_COMPLETED)
        self.assertEqual(
            ShippingRoutingState.objects.get(shipping_order=order_b).status,
            ShippingRoutingState.STATUS_DELIVERED,
        )

    def test_logistician_mark_not_delivered_sets_delivery_failed(self):
        order = self._create_packed_order_with_pallets(
            "SO-MP-FAIL-1",
            [{"label": "Паллета 1", "code": "SO-MP-FAIL-1-PAL-1", "boxes": [{"code": "BOX-F1", "qty": 1}]}],
        )
        trip = LogisticsTrip.objects.create(
            number="TRIP-000029",
            trip_date=date(2026, 4, 5),
            status=LogisticsTrip.STATUS_DEPARTED,
            assigned_logistician=self.logistician,
            created_by=self.logistician_user,
        )
        LogisticsTripOrder.objects.create(
            trip=trip, shipping_order=order, loading_sequence=1, delivery_sequence=1
        )
        self._add_logistics_fact(trip)
        self.client.force_login(self.logistician_user)

        response = self.client.post(
            reverse("logistics:trip-detail", args=[trip.pk]),
            data={"action": "mark_not_delivered", "shipping_order_id": order.pk},
        )

        self.assertEqual(response.status_code, 302)
        trip.refresh_from_db()
        routing = ShippingRoutingState.objects.get(shipping_order=order)
        self.assertEqual(routing.status, ShippingRoutingState.STATUS_FAILED)
        self.assertEqual(trip.status, LogisticsTrip.STATUS_COMPLETED)

        from client_cabinet.client_shipping_view import resolve_client_shipping_stage

        stage_key, stage_label = resolve_client_shipping_stage(
            order_status=order.status,
            trip_status=trip.status,
            routing_status=routing.status,
        )
        self.assertEqual(stage_key, "not_delivered")
        self.assertEqual(stage_label, "Не сдана на МП")

    def test_last_delivery_outcome_is_blocked_until_logistics_services_are_saved(self):
        order = self._create_packed_order_with_pallets(
            "SO-MP-NO-SERVICES",
            [{"label": "Паллета 1", "code": "SO-MP-NO-SERVICES-PAL-1", "boxes": [{"code": "BOX-NS1", "qty": 1}]}],
        )
        trip = LogisticsTrip.objects.create(
            number="TRIP-NO-LOGISTICS-SERVICES",
            trip_date=date(2026, 4, 5),
            status=LogisticsTrip.STATUS_DEPARTED,
            assigned_logistician=self.logistician,
            created_by=self.logistician_user,
        )
        LogisticsTripOrder.objects.create(trip=trip, shipping_order=order)
        self.client.force_login(self.logistician_user)

        response = self.client.post(
            reverse("logistics:trip-detail", args=[trip.pk]),
            data={"action": "mark_not_delivered", "shipping_order_id": order.pk},
        )

        self.assertEqual(response.status_code, 302)
        trip.refresh_from_db()
        self.assertEqual(trip.status, LogisticsTrip.STATUS_DEPARTED)
        self.assertFalse(
            ShippingRoutingState.objects.filter(
                shipping_order=order,
                status=ShippingRoutingState.STATUS_FAILED,
            ).exists()
        )

    def test_departed_trip_detail_shows_mp_delivery_buttons_for_logistician(self):
        order = self._create_packed_order_with_pallets(
            "SO-MP-UI-1",
            [{"label": "Паллета 1", "code": "SO-MP-UI-1-PAL-1", "boxes": [{"code": "BOX-U1", "qty": 1}]}],
        )
        trip = LogisticsTrip.objects.create(
            number="TRIP-000030",
            trip_date=date(2026, 4, 5),
            status=LogisticsTrip.STATUS_DEPARTED,
            assigned_logistician=self.logistician,
            created_by=self.logistician_user,
        )
        LogisticsTripOrder.objects.create(
            trip=trip, shipping_order=order, loading_sequence=1, delivery_sequence=1
        )
        self.client.force_login(self.logistician_user)

        response = self.client.get(reverse("logistics:trip-detail", args=[trip.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Сдача на МП")
        self.assertContains(response, "Сдано на МП")
        self.assertContains(response, "Не сдано")
        self.assertContains(response, 'name="action" value="mark_delivered"')


class TripFinalizeValidationTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.logistician_user = User.objects.create_user(username="val_log", password="pass")
        self.logistician = Employee.objects.create(
            user=self.logistician_user, full_name="Val Log", role="logistician", is_active=True
        )
        self.agency = Agency.objects.create(agn_name="Val Agency", inn="7700111222")
        self.carrier = Carrier.objects.create(name="Val Carrier", is_active=True)

    def _insert_packed_order(self, number: str, *, destination="Склад WB"):
        from django.db import connection

        now = timezone.now()
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO shipping_shippingorder (
                    number, status, delivery_type, destination_address, comment,
                    created_at, updated_at, agency_id, created_by_id, marketplace_id,
                    slot_date, vehicle_type, wb_supply_barcode, wb_transit_warehouse,
                    driver_phone, expected_boxes, place_type, vehicle_number,
                    destination_warehouse, shipping_barcode, supply_type, transit_address,
                    shipping_discrepancy_payload, shipping_discrepancy_status,
                    distribution_status, supply_number
                ) VALUES (
                    %s, %s, %s, '', '',
                    %s, %s, %s, %s, NULL,
                    %s, '', '', false,
                    '', 4, '', '',
                    %s, '', '', '',
                    '{}'::jsonb, '',
                    '', ''
                )
                RETURNING id
                """,
                [
                    number,
                    ShippingOrder.STATUS_PACKED,
                    ShippingOrder.DELIVERY_MARKETPLACE,
                    now,
                    now,
                    self.agency.id,
                    self.logistician_user.id,
                    date(2026, 4, 2),
                    destination,
                ],
            )
            return ShippingOrder.objects.get(pk=cursor.fetchone()[0])

    def test_pallets_over_capacity_blocks_finalize(self):
        from logistics.trip_validation import validate_trip_for_finalize

        order = self._insert_packed_order("SO-VAL-P1")
        trip = LogisticsTrip.objects.create(
            number="TRIP-VAL-P",
            trip_date=date(2026, 4, 10),
            status=LogisticsTrip.STATUS_PLANNED,
            vehicle_number="А123ВС777",
            driver_name="Водитель Тест",
            carrier=self.carrier,
            max_pallets=2,
            assigned_logistician=self.logistician,
            created_by=self.logistician_user,
        )
        LogisticsTripOrder.objects.create(trip=trip, shipping_order=order, loading_sequence=1, delivery_sequence=1)
        result = validate_trip_for_finalize(
            trip,
            packing_payloads={order.number: {"pallet_count": 5, "delivered_box_count": 20}},
        )
        self.assertFalse(result.ok)
        self.assertTrue(any("паллет превышает вместимость" in m for m in result.blocking_messages))

    def test_same_vehicle_can_run_multiple_trips_in_one_day(self):
        from logistics.trip_validation import validate_trip_for_finalize

        order = self._insert_packed_order("SO-VAL-V2")
        LogisticsTrip.objects.create(
            number="TRIP-VAL-A",
            trip_date=date(2026, 4, 11),
            status=LogisticsTrip.STATUS_LOADING,
            vehicle_number="А555АА777",
            driver_name="Иван Первый",
            assigned_logistician=self.logistician,
            created_by=self.logistician_user,
        )
        trip = LogisticsTrip.objects.create(
            number="TRIP-VAL-B",
            trip_date=date(2026, 4, 11),
            status=LogisticsTrip.STATUS_PLANNED,
            vehicle_number="А555АА777",
            driver_name="Пётр Второй",
            carrier=self.carrier,
            assigned_logistician=self.logistician,
            created_by=self.logistician_user,
        )
        LogisticsTripOrder.objects.create(trip=trip, shipping_order=order, loading_sequence=1, delivery_sequence=1)
        result = validate_trip_for_finalize(trip)
        self.assertTrue(result.ok, result.blocking_messages)
        self.assertFalse(any("автомобиль" in m.lower() for m in result.blocking_messages))
        self.assertTrue(any("автомобиль уже назначен" in m.lower() for m in result.warning_messages))

    def test_same_driver_can_run_multiple_trips_in_one_day(self):
        from logistics.trip_validation import validate_trip_for_finalize

        order = self._insert_packed_order("SO-VAL-D2")
        LogisticsTrip.objects.create(
            number="TRIP-VAL-DRIVER-A",
            trip_date=date(2026, 4, 11),
            status=LogisticsTrip.STATUS_DEPARTED,
            vehicle_number="А111АА777",
            driver_name="Иван Многорейсов",
            assigned_logistician=self.logistician,
            created_by=self.logistician_user,
        )
        trip = LogisticsTrip.objects.create(
            number="TRIP-VAL-DRIVER-B",
            trip_date=date(2026, 4, 11),
            status=LogisticsTrip.STATUS_PLANNED,
            vehicle_number="А222АА777",
            driver_name="Иван Многорейсов",
            carrier=self.carrier,
            assigned_logistician=self.logistician,
            created_by=self.logistician_user,
        )
        LogisticsTripOrder.objects.create(trip=trip, shipping_order=order, loading_sequence=1, delivery_sequence=1)

        result = validate_trip_for_finalize(trip)

        self.assertTrue(result.ok, result.blocking_messages)
        self.assertFalse(any("водитель" in m.lower() for m in result.blocking_messages))
        self.assertTrue(any("водитель уже назначен" in m.lower() for m in result.warning_messages))

    def test_missing_address_blocks_finalize(self):
        from logistics.trip_validation import validate_trip_for_finalize

        order = self._insert_packed_order("SO-VAL-A1", destination="")
        trip = LogisticsTrip.objects.create(
            number="TRIP-VAL-ADDR",
            trip_date=date(2026, 4, 12),
            status=LogisticsTrip.STATUS_PLANNED,
            vehicle_number="А321ВС777",
            driver_name="Водитель Адрес",
            carrier=self.carrier,
            assigned_logistician=self.logistician,
            created_by=self.logistician_user,
        )
        LogisticsTripOrder.objects.create(trip=trip, shipping_order=order, loading_sequence=1, delivery_sequence=1)
        result = validate_trip_for_finalize(trip)
        self.assertFalse(result.ok)
        self.assertTrue(any("не заполнен адрес" in m for m in result.blocking_messages))
        self.assertIn("orders", result.highlight_fields)

    def test_transit_address_satisfies_marketplace_route_address(self):
        from logistics.services import _order_route_destination
        from logistics.trip_validation import validate_trip_for_finalize

        order = self._insert_packed_order("SO-VAL-TRANSIT", destination="")
        order.transit_address = "СТАРАЯ_КУПАВНА_28, Московская область"
        order.save(update_fields=["transit_address"])
        trip = LogisticsTrip.objects.create(
            number="TRIP-VAL-TRANSIT",
            trip_date=date(2026, 4, 12),
            status=LogisticsTrip.STATUS_PLANNED,
            vehicle_number="А322ВС777",
            driver_name="Водитель Транзит",
            carrier=self.carrier,
            assigned_logistician=self.logistician,
            created_by=self.logistician_user,
        )
        LogisticsTripOrder.objects.create(
            trip=trip,
            shipping_order=order,
            loading_sequence=1,
            delivery_sequence=1,
        )

        result = validate_trip_for_finalize(trip)

        self.assertTrue(result.ok, result.blocking_messages)
        self.assertFalse(any("не заполнен адрес" in m for m in result.blocking_messages))
        self.assertEqual(_order_route_destination(order), order.transit_address)

    def test_missing_required_fields_map_to_highlights(self):
        from logistics.trip_validation import validate_trip_for_finalize

        order = self._insert_packed_order("SO-VAL-EMPTY")
        trip = LogisticsTrip.objects.create(
            number="TRIP-VAL-EMPTY",
            trip_date=None,
            status=LogisticsTrip.STATUS_PLANNED,
            assigned_logistician=self.logistician,
            created_by=self.logistician_user,
        )
        LogisticsTripOrder.objects.create(trip=trip, shipping_order=order, loading_sequence=1, delivery_sequence=1)
        result = validate_trip_for_finalize(trip)
        self.assertFalse(result.ok)
        for field_name in ("driver_name", "vehicle_number", "trip_date", "carrier_id"):
            self.assertIn(field_name, result.highlight_fields)

    def test_departed_trip_detail_hides_stale_finalize_errors(self):
        order = self._insert_packed_order("SO-VAL-DEP")
        # Mark order as shipped so finalize validation would fail if shown.
        order.status = ShippingOrder.STATUS_SHIPPED
        order.save(update_fields=["status"])
        trip = LogisticsTrip.objects.create(
            number="34_RS",
            trip_date=date(2026, 4, 12),
            status=LogisticsTrip.STATUS_DEPARTED,
            vehicle_number="Н434КХ550",
            driver_name="Скрипник Андрей",
            carrier=self.carrier,
            assigned_logistician=self.logistician,
            created_by=self.logistician_user,
        )
        LogisticsTripOrder.objects.create(trip=trip, shipping_order=order, loading_sequence=1, delivery_sequence=1)
        self.client.force_login(self.logistician_user)

        response = self.client.get(reverse("logistics:trip-detail", args=[trip.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Погрузка завершена")
        self.assertNotContains(response, "Невозможно утвердить рейс")
        self.assertNotContains(response, "Сформировать рейс")


class TripOptimisticLockTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user_a = User.objects.create_user(username="lock_a", password="pass")
        self.user_b = User.objects.create_user(username="lock_b", password="pass")
        self.emp_a = Employee.objects.create(
            user=self.user_a, full_name="Lock A", role="logistician", is_active=True
        )
        self.emp_b = Employee.objects.create(
            user=self.user_b, full_name="Lock B", role="logistician", is_active=True
        )
        self.agency = Agency.objects.create(agn_name="Lock Agency", inn="7700999888")

    def _insert_packed_order(self, number: str):
        from django.db import connection

        now = timezone.now()
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO shipping_shippingorder (
                    number, status, delivery_type, destination_address, comment,
                    created_at, updated_at, agency_id, created_by_id, marketplace_id,
                    slot_date, vehicle_type, wb_supply_barcode, wb_transit_warehouse,
                    driver_phone, expected_boxes, place_type, vehicle_number,
                    destination_warehouse, shipping_barcode, supply_type, transit_address,
                    shipping_discrepancy_payload, shipping_discrepancy_status,
                    distribution_status, supply_number
                ) VALUES (
                    %s, %s, %s, '', '',
                    %s, %s, %s, %s, NULL,
                    %s, '', '', false,
                    '', 4, '', '',
                    %s, '', '', '',
                    '{}'::jsonb, '',
                    '', ''
                )
                RETURNING id
                """,
                [
                    number,
                    ShippingOrder.STATUS_PACKED,
                    ShippingOrder.DELIVERY_MARKETPLACE,
                    now,
                    now,
                    self.agency.id,
                    self.user_a.id,
                    date(2026, 4, 20),
                    "Склад WB",
                ],
            )
            order_id = cursor.fetchone()[0]
        return ShippingOrder.objects.get(pk=order_id)

    def test_stale_version_blocks_update_loading_params(self):
        order = self._insert_packed_order("SO-LOCK-VER-1")
        trip = LogisticsTrip.objects.create(
            number="TRIP-LOCK-1",
            trip_date=date(2026, 4, 20),
            status=LogisticsTrip.STATUS_PLANNED,
            vehicle_number="А111АА777",
            driver_name="Водитель Один",
            assigned_logistician=self.emp_a,
            created_by=self.user_a,
            edit_version=3,
        )
        LogisticsTripOrder.objects.create(trip=trip, shipping_order=order, loading_sequence=1, delivery_sequence=1)

        self.client.force_login(self.user_a)
        response = self.client.post(
            reverse("logistics:trip-detail", args=[trip.pk]),
            data={
                "action": "update_loading_params",
                "edit_version": "2",
                "vehicle_number": "А222АА777",
                "driver_name": "Водитель Два",
                "driver_phone": "+79001112233",
            },
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "изменён другим сотрудником")
        trip.refresh_from_db()
        self.assertEqual(trip.edit_version, 3)
        self.assertEqual(trip.vehicle_number, "А111АА777")

    def test_matching_version_bumps_and_saves(self):
        order = self._insert_packed_order("SO-LOCK-VER-2")
        trip = LogisticsTrip.objects.create(
            number="TRIP-LOCK-2",
            trip_date=date(2026, 4, 21),
            status=LogisticsTrip.STATUS_PLANNED,
            vehicle_number="А333АА777",
            driver_name="Водитель Три",
            assigned_logistician=self.emp_a,
            created_by=self.user_a,
            edit_version=1,
        )
        LogisticsTripOrder.objects.create(trip=trip, shipping_order=order, loading_sequence=1, delivery_sequence=1)

        self.client.force_login(self.user_b)
        response = self.client.post(
            reverse("logistics:trip-detail", args=[trip.pk]),
            data={
                "action": "update_loading_params",
                "edit_version": "1",
                "vehicle_number": "А444АА777",
                "driver_name": "Водитель Четыре",
                "driver_phone": "+79004445566",
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["edit_version"], 2)
        trip.refresh_from_db()
        self.assertEqual(trip.edit_version, 2)
        self.assertEqual(trip.vehicle_number, "А444АА777")
        self.assertEqual(trip.last_edited_by_id, self.emp_b.id)

    def test_ajax_conflict_returns_409(self):
        order = self._insert_packed_order("SO-LOCK-VER-3")
        trip = LogisticsTrip.objects.create(
            number="TRIP-LOCK-3",
            trip_date=date(2026, 4, 22),
            status=LogisticsTrip.STATUS_PLANNED,
            vehicle_number="А555АА777",
            driver_name="Водитель Пять",
            assigned_logistician=self.emp_a,
            created_by=self.user_a,
            edit_version=5,
        )
        LogisticsTripOrder.objects.create(trip=trip, shipping_order=order, loading_sequence=1, delivery_sequence=1)

        self.client.force_login(self.user_a)
        response = self.client.post(
            reverse("logistics:trip-detail", args=[trip.pk]),
            data={
                "action": "update_sequences",
                "edit_version": "4",
                "ordered_trip_item_ids": [trip.orders.first().id],
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(response.status_code, 409)
        payload = response.json()
        self.assertTrue(payload["conflict"])
        trip.refresh_from_db()
        self.assertEqual(trip.edit_version, 5)

    def test_detail_shows_concurrent_editor(self):
        from logistics.trip_concurrency import touch_trip_presence

        order = self._insert_packed_order("SO-LOCK-VER-4")
        trip = LogisticsTrip.objects.create(
            number="TRIP-LOCK-4",
            trip_date=date(2026, 4, 23),
            status=LogisticsTrip.STATUS_PLANNED,
            assigned_logistician=self.emp_a,
            created_by=self.user_a,
        )
        LogisticsTripOrder.objects.create(trip=trip, shipping_order=order, loading_sequence=1, delivery_sequence=1)
        touch_trip_presence(trip, self.emp_a)

        self.client.force_login(self.user_b)
        response = self.client.get(reverse("logistics:trip-detail", args=[trip.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Сейчас на карточке")
        self.assertContains(response, "Lock A")


class LogisticsCabinetShellTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(username="shell_log", password="pass")
        self.emp = Employee.objects.create(
            user=self.user, full_name="Shell Log", role="logistician", is_active=True
        )
        self.store_user = User.objects.create_user(username="shell_sk", password="pass")
        self.store = Employee.objects.create(
            user=self.store_user, full_name="Shell SK", role="storekeeper", is_active=True
        )
        self.agency = Agency.objects.create(agn_name="Shell Agency", inn="7700555444")

    def _insert_packed_order(self, number: str):
        from django.db import connection

        now = timezone.now()
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO shipping_shippingorder (
                    number, status, delivery_type, destination_address, comment,
                    created_at, updated_at, agency_id, created_by_id, marketplace_id,
                    slot_date, vehicle_type, wb_supply_barcode, wb_transit_warehouse,
                    driver_phone, expected_boxes, place_type, vehicle_number,
                    destination_warehouse, shipping_barcode, supply_type, transit_address,
                    shipping_discrepancy_payload, shipping_discrepancy_status,
                    distribution_status, supply_number
                ) VALUES (
                    %s, %s, %s, '', '',
                    %s, %s, %s, %s, NULL,
                    %s, '', '', false,
                    '', 2, '', '',
                    %s, '', '', '',
                    '{}'::jsonb, '',
                    '', ''
                )
                RETURNING id
                """,
                [
                    number,
                    ShippingOrder.STATUS_PACKED,
                    ShippingOrder.DELIVERY_MARKETPLACE,
                    now,
                    now,
                    self.agency.id,
                    self.user.id,
                    date(2026, 5, 1),
                    "Склад",
                ],
            )
            return ShippingOrder.objects.get(pk=cursor.fetchone()[0])

    def test_logistician_dashboard_uses_teammanager_shell(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse("logistics:dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Личный кабинет менеджера FULLBOX")
        self.assertContains(response, "Подбор заявок")
        self.assertContains(response, "Все рейсы")
        self.assertContains(response, 'href="/team-manager/"')

    def test_storekeeper_trip_detail_uses_standalone_shell(self):
        order = self._insert_packed_order("SO-SHELL-1")
        trip = LogisticsTrip.objects.create(
            number="TRIP-SHELL-1",
            trip_date=date(2026, 5, 1),
            status=LogisticsTrip.STATUS_LOADING,
            assigned_logistician=self.emp,
            created_by=self.user,
        )
        LogisticsTripOrder.objects.create(trip=trip, shipping_order=order, loading_sequence=1, delivery_sequence=1)
        self.client.force_login(self.store_user)
        response = self.client.get(reverse("logistics:trip-detail", args=[trip.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Личный кабинет менеджера FULLBOX")
        self.assertContains(response, "Подбор для маршрута")
