from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from employees.models import Employee
from fbs.exceptions import FbsStorageError
from sku.models import Agency, SKU
from sklad.models import WarehouseLocation

from fbs.models import (
    FbsBox,
    FbsClientMovementRequest,
    FbsClientStoragePolicy,
    FbsHandoverBatch,
    FbsHandoverBox,
    FbsHandoverOrder,
    FbsIntegrationProfile,
    FbsOrder,
    FbsOrderItem,
    FbsPallet,
    FbsStockBalance,
    FbsStorageCell,
)
from fbs.services.billing import capture_daily_storage_usage
from fbs.signals import movement_completed, movement_warehouse_confirmed

from .fbs_services import (
    sync_fbs_delivery_to_billing,
    sync_fbs_movement_to_billing,
    sync_fbs_order_to_billing,
)
from .models import BillingApplication, BillingStaffNotification, WarehouseServiceFact


@override_settings(FBS_MODULE_ENABLED=True, FBS_WAREHOUSE_WRITES_ENABLED=True)
class FbsBillingIntegrationTests(TestCase):
    def setUp(self):
        self.agency = Agency.objects.create(agn_name="Клиент FBS-биллинга")
        self.sku = SKU.objects.create(
            agency=self.agency,
            sku_code="FBS-BILL-SKU",
            name="Товар FBS-биллинга",
            length_mm=100,
            width_mm=100,
            height_mm=100,
        )
        location = WarehouseLocation.objects.create(
            warehouse_code="MSK",
            zone_code="FBS",
            zone_kind=WarehouseLocation.ZONE_KIND_STORAGE,
            location_code="FBS-BILL-01",
            row_no=90,
            section_no=1,
            tier_no=1,
            cell_no=1,
        )
        self.cell = FbsStorageCell.objects.create(
            cell_code="FBS-BILL-01",
            location=location,
        )
        self.pallet = FbsPallet.objects.create(
            agency=self.agency,
            pallet_code="FBS-BILL-PALLET",
            cell=self.cell,
            status=FbsPallet.STATUS_ACTIVE,
        )
        self.box = FbsBox.objects.create(
            agency=self.agency,
            pallet=self.pallet,
            box_code="FBS-BILL-BOX",
            status=FbsBox.STATUS_ACTIVE,
        )
        FbsStockBalance.objects.create(
            agency=self.agency,
            box=self.box,
            sku_ref=self.sku,
            identity_key="fbs-billing-stock",
            sku_code=self.sku.sku_code,
            name=self.sku.name,
            barcode="4600000007777",
            qty=3,
            available_qty=3,
        )
        self.profile = FbsIntegrationProfile.objects.create(
            agency=self.agency,
            marketplace=FbsIntegrationProfile.MARKETPLACE_WB,
            name="WB billing",
            external_warehouse_id="1931120",
            is_active=True,
        )

    def test_storage_uses_only_selected_liter_or_pallet_mode(self):
        policy = FbsClientStoragePolicy.objects.create(
            agency=self.agency,
            billing_mode=FbsClientStoragePolicy.BILLING_LITERS,
        )
        first_day = timezone.localdate()

        capture_daily_storage_usage(usage_date=first_day, agency=self.agency)

        liter_fact = WarehouseServiceFact.objects.get(
            order_type=WarehouseServiceFact.ORDER_FBS,
            order_id=f"FBS-STORAGE-{first_day.isoformat()}",
        )
        self.assertEqual(liter_fact.service.code, "fbs_storage_liter_day")
        self.assertEqual(liter_fact.quantity, Decimal("3.000"))

        policy.billing_mode = FbsClientStoragePolicy.BILLING_PALLETS
        policy.save(update_fields=["billing_mode", "updated_at"])
        second_day = first_day + timedelta(days=1)
        capture_daily_storage_usage(usage_date=second_day, agency=self.agency)

        pallet_fact = WarehouseServiceFact.objects.get(
            order_type=WarehouseServiceFact.ORDER_FBS,
            order_id=f"FBS-STORAGE-{second_day.isoformat()}",
        )
        self.assertEqual(pallet_fact.service.code, "fbs_storage_pallet_day")
        self.assertEqual(pallet_fact.quantity, Decimal("1.000"))
        self.assertEqual(
            BillingApplication.objects.filter(
                application_type=BillingApplication.TYPE_FBS,
                client=self.agency,
            ).count(),
            2,
        )

    def test_order_picking_and_shipping_facts_are_idempotent(self):
        order = FbsOrder.objects.create(
            profile=self.profile,
            external_order_id="WB-FBS-BILL-1",
            internal_status=FbsOrder.STATUS_PICKED,
        )
        FbsOrderItem.objects.create(
            order=order,
            external_line_id="line-1",
            external_sku=self.sku.sku_code,
            sku=self.sku,
            quantity=3,
        )

        sync_fbs_order_to_billing(order=order)
        sync_fbs_order_to_billing(order=order)

        self.assertEqual(
            set(
                WarehouseServiceFact.objects.filter(order_id=f"FBS-ORDER-{order.id}")
                .values_list("service__code", flat=True)
            ),
            {"fbs_pick_item", "fbs_pick_order"},
        )

        handover = FbsHandoverBatch.objects.create(profile=self.profile)
        handover_box = FbsHandoverBox.objects.create(
            batch=handover,
            qr_code="FBS-BILL-HANDOVER-BOX",
        )
        FbsHandoverOrder.objects.create(box=handover_box, order=order)
        order.internal_status = FbsOrder.STATUS_HANDED_OVER
        order.save(update_fields=["internal_status", "updated_at"])

        sync_fbs_order_to_billing(order=order)
        sync_fbs_order_to_billing(order=order)

        self.assertEqual(
            set(
                WarehouseServiceFact.objects.filter(order_id=f"FBS-ORDER-{order.id}")
                .values_list("service__code", flat=True)
            ),
            {
                "fbs_pick_item",
                "fbs_pick_order",
                "fbs_shipping_order",
                "fbs_shipping_box",
            },
        )

    def test_marketplace_accepted_batch_creates_one_delivery_fact(self):
        batch = FbsHandoverBatch.objects.create(
            profile=self.profile,
            external_supply_id="WB-SUPPLY-1",
            status=FbsHandoverBatch.STATUS_ACCEPTED,
            accepted_at=timezone.now(),
        )

        sync_fbs_delivery_to_billing(batch=batch)
        sync_fbs_delivery_to_billing(batch=batch)

        facts = WarehouseServiceFact.objects.filter(
            order_type=WarehouseServiceFact.ORDER_FBS,
            order_id=f"FBS-DELIVERY-{batch.id}",
        )
        self.assertEqual(facts.count(), 1)
        self.assertEqual(facts.get().service.code, "fbs_delivery")
        self.assertEqual(facts.get().quantity, Decimal("1.000"))

    def test_movement_is_billed_only_after_manager_confirmation_signal(self):
        manager_user = get_user_model().objects.create_user(username="fbs-billing-manager")
        Employee.objects.create(
            user=manager_user,
            full_name="Менеджер FBS-биллинга",
            role="manager",
        )
        self.agency.mened_user_id = manager_user.id
        self.agency.save(update_fields=["mened_user_id"])
        request_row = FbsClientMovementRequest.objects.create(
            agency=self.agency,
            mode=FbsClientMovementRequest.MODE_ITEM,
            status=FbsClientMovementRequest.STATUS_AWAITING_MANAGER_CONFIRMATION,
            requested_qty=5,
            actual_moved_qty=4,
            warehouse_confirmed_at=timezone.now(),
        )

        movement_warehouse_confirmed.send(
            sender=self.__class__,
            request_row=request_row,
            user=None,
        )
        with self.assertRaisesMessage(FbsStorageError, "не подтверждено складом"):
            sync_fbs_movement_to_billing(request_row=request_row)
        self.assertFalse(
            BillingApplication.objects.filter(application_id=request_row.number).exists()
        )

        request_row.status = FbsClientMovementRequest.STATUS_COMPLETED
        request_row.save(update_fields=["status", "updated_at"])
        movement_completed.send(
            sender=self.__class__,
            request_row=request_row,
            user=manager_user,
        )
        movement_completed.send(
            sender=self.__class__,
            request_row=request_row,
            user=manager_user,
        )

        fact = WarehouseServiceFact.objects.get(
            order_type=WarehouseServiceFact.ORDER_FBS,
            order_id=request_row.number,
            service__code="fbs_receiving_goods",
        )
        self.assertEqual(fact.quantity, Decimal("4.000"))
        self.assertEqual(
            BillingApplication.objects.filter(application_id=request_row.number).count(),
            1,
        )
        self.assertTrue(
            BillingStaffNotification.objects.filter(
                recipient=manager_user,
                source_key__startswith=(
                    f"fbs-movement:warehouse-confirmed:{request_row.id}:"
                ),
            ).exists()
        )

        request_row.warehouse_confirmed_at += timedelta(minutes=1)
        request_row.save(update_fields=["warehouse_confirmed_at", "updated_at"])
        movement_warehouse_confirmed.send(
            sender=self.__class__,
            request_row=request_row,
            user=None,
        )
        self.assertEqual(
            BillingStaffNotification.objects.filter(
                recipient=manager_user,
                source_key__startswith=(
                    f"fbs-movement:warehouse-confirmed:{request_row.id}:"
                ),
            ).count(),
            2,
        )
