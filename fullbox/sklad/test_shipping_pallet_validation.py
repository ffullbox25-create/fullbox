from types import SimpleNamespace

from django.test import TestCase
from django.utils import timezone

from logistics.services import _shipping_trip_pallet_error
from sklad.models import (
    WarehouseContainer,
    WarehouseEvent,
    WarehouseLocation,
    WarehouseStockSnapshot,
)
from sklad.services import WarehouseStateCode, WarehouseWritePathService
from sku.models import Agency


class ShippingPalletOwnershipValidationTests(TestCase):
    order_id = "OTG-TEST-157"

    def setUp(self):
        self.agency = Agency.objects.create(agn_name="Клиент проверки рейса")
        location = WarehouseLocation.objects.create(
            warehouse_code="MSK",
            zone_code="OTG",
            zone_kind=WarehouseLocation.ZONE_KIND_SHIPPING,
            row_no=1,
            section_no=1,
            tier_no=1,
            cell_no=1,
            location_code="OTG-TEST-1-1-1-1",
            display_name="OTG · Тестовая ячейка",
        )
        self.pallet = WarehouseContainer.objects.create(
            agency=self.agency,
            container_type=WarehouseContainer.TYPE_MIXED_PALLET,
            container_code="PAL-OTG-TEST-157",
            current_location=location,
            source_context_type="shipping",
            source_context_id=self.order_id,
        )
        self.box = WarehouseContainer.objects.create(
            agency=self.agency,
            container_type=WarehouseContainer.TYPE_BOX,
            container_code="BOX-OTG-TEST-157",
            parent_container=self.pallet,
            current_location=location,
            source_context_type="receiving",
            source_context_id="PR-TEST-157",
        )
        last_event = WarehouseEvent.objects.create(
            agency=self.agency,
            event_type="palletization_completed",
            stock_context_type="shipping",
            stock_context_id=self.order_id,
            container=self.box,
            to_location=location,
            to_zone_code="OTG",
            qty=10,
            occurred_at=timezone.now(),
        )
        self.snapshot = WarehouseStockSnapshot.objects.create(
            agency=self.agency,
            source_context_type="receiving",
            source_context_id="PR-TEST-157",
            sku_code="SKU-TEST-157",
            name="Товар проверки",
            qty=10,
            available_qty=0,
            shipping_reserved_qty=0,
            container=self.box,
            container_code=self.box.container_code,
            parent_container=self.pallet,
            location=location,
            zone_code="OTG",
            zone_kind=location.zone_kind,
            warehouse_state_code=WarehouseStateCode.READY_FOR_LOADING.value,
            last_event=last_event,
        )

    def test_ready_for_loading_snapshot_with_zero_reserve_is_valid(self):
        result = WarehouseWritePathService.validate_shipping_pallet_ownership(
            agency=self.agency,
            order_id=self.order_id,
        )

        self.assertEqual(result["snapshot_count"], 1)
        self.assertEqual(result["pallet_count"], 1)
        self.assertEqual(result["pallet_codes"], ["PAL-OTG-TEST-157"])

    def test_logistics_accepts_ready_snapshot_with_zero_reserve(self):
        error = _shipping_trip_pallet_error(
            SimpleNamespace(agency=self.agency, number=self.order_id),
            {"pallets": [{"code": self.pallet.container_code}]},
        )

        self.assertEqual(error, "")

    def test_snapshot_from_another_shipping_order_is_rejected(self):
        with self.assertRaisesMessage(
            ValueError,
            "Заявка заблокирована: складские остатки заявки не найдены.",
        ):
            WarehouseWritePathService.validate_shipping_pallet_ownership(
                agency=self.agency,
                order_id="OTG-TEST-OTHER",
            )

    def test_snapshot_without_parent_pallet_is_rejected(self):
        self.snapshot.parent_container = None
        self.snapshot.save(update_fields=["parent_container", "updated_at"])
        self.box.parent_container = None
        self.box.save(update_fields=["parent_container", "updated_at"])

        with self.assertRaisesMessage(
            ValueError,
            "Заявка заблокирована: товар не размещен на паллетах.",
        ):
            WarehouseWritePathService.validate_shipping_pallet_ownership(
                agency=self.agency,
                order_id=self.order_id,
            )

    def test_pallet_owned_by_another_shipping_order_is_rejected(self):
        self.pallet.source_context_id = "OTG-TEST-OTHER"
        self.pallet.save(update_fields=["source_context_id", "updated_at"])

        with self.assertRaisesMessage(
            ValueError,
            "Заявка заблокирована: паллета PAL-OTG-TEST-157 принадлежит другой заявке.",
        ):
            WarehouseWritePathService.validate_shipping_pallet_ownership(
                agency=self.agency,
                order_id=self.order_id,
            )
