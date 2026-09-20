from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings

from employees.models import Employee
from sklad.models import (
    WarehouseContainer,
    WarehouseLocation,
    WarehouseOperation,
    WarehouseReserve,
    WarehouseStockSnapshot,
)
from sku.models import Agency, SKU, SKUBarcode

from .client_portal import create_client_movement_request
from .exceptions import FbsReplenishmentError
from .models import (
    FbsBox,
    FbsClientMovementRequest,
    FbsPallet,
    FbsReplenishmentPlan,
    FbsReplenishmentPreparedBox,
    FbsStorageCell,
)
from .services.client_movements import accept_client_movement_request


@override_settings(
    FBS_MODULE_ENABLED=True,
    FBS_WAREHOUSE_WRITES_ENABLED=True,
    FBS_ZONE_CODE="FBS",
    FBS_PREP_ZONE_CODE="FBS-PREP",
)
class FbsClientMovementItemFallbackTests(TestCase):
    def setUp(self):
        users = get_user_model()
        self.storekeeper = users.objects.create_user(username="fallback-storekeeper")
        Employee.objects.create(
            user=self.storekeeper,
            full_name="Fallback storekeeper",
            role="storekeeper",
        )
        self.agency = Agency.objects.create(agn_name="Fallback client")
        self.exact_sku = self._sku("FALLBACK-EXACT", "4600000000100")
        self.fallback_sku = self._sku("FALLBACK-ITEM", "4600000000001")
        self.source_location = WarehouseLocation.objects.create(
            warehouse_code="MSK",
            zone_code="STORAGE",
            zone_kind=WarehouseLocation.ZONE_KIND_STORAGE,
            row_no=61,
            section_no=1,
            tier_no=1,
            cell_no=1,
            location_code="FALLBACK-SOURCE",
            is_storage=True,
        )
        WarehouseLocation.objects.create(
            warehouse_code="MSK",
            zone_code="FBS-PREP",
            zone_kind=WarehouseLocation.ZONE_KIND_TRANSIT,
            row_no=61,
            section_no=1,
            tier_no=0,
            cell_no=1,
            location_code="FALLBACK-PREP",
        )
        fbs_location = WarehouseLocation.objects.create(
            warehouse_code="MSK",
            zone_code="FBS",
            zone_kind=WarehouseLocation.ZONE_KIND_STORAGE,
            row_no=61,
            section_no=2,
            tier_no=1,
            cell_no=1,
            location_code="FALLBACK-FBS",
            is_storage=True,
        )
        self.pallet = FbsPallet.objects.create(
            agency=self.agency,
            cell=FbsStorageCell.objects.create(
                cell_code="FALLBACK-FBS",
                location=fbs_location,
            ),
            pallet_code="FALLBACK-PALLET",
            max_boxes=10,
        )
        self.exact_snapshot = self._snapshot(
            sku=self.exact_sku,
            barcode="4600000000100",
            code="FALLBACK-EXACT-BOX",
            qty=100,
        )
        self.fallback_snapshot = self._snapshot(
            sku=self.fallback_sku,
            barcode="4600000000001",
            code="FALLBACK-50-BOX",
            qty=50,
        )
        self.initial_exact_snapshot = self._snapshot(
            sku=self.fallback_sku,
            barcode="4600000000001",
            code="FALLBACK-1-BOX",
            qty=1,
        )
        self.request_row = create_client_movement_request(
            agency=self.agency,
            mode=FbsClientMovementRequest.MODE_BOX,
            raw_lines=[
                {
                    "barcode": "4600000000100",
                    "qty": 100,
                    "units_per_box": 100,
                },
                {
                    "barcode": "4600000000001",
                    "qty": 1,
                    "units_per_box": 1,
                },
            ],
        )
        self.initial_request_status = self.request_row.status
        # The client submitted while an exact box was free. Before warehouse
        # acceptance, another request claimed it, reproducing request 13.
        WarehouseStockSnapshot.objects.filter(pk=self.initial_exact_snapshot.pk).update(
            available_qty=0,
            other_reserved_qty=1,
        )
        self.initial_exact_snapshot.refresh_from_db()

    def _sku(self, code, barcode):
        sku = SKU.objects.create(
            agency=self.agency,
            sku_code=code,
            name=code,
        )
        SKUBarcode.objects.create(sku=sku, value=barcode, is_primary=True)
        return sku

    def _snapshot(self, *, sku, barcode, code, qty):
        box = WarehouseContainer.objects.create(
            agency=self.agency,
            container_type=WarehouseContainer.TYPE_BOX,
            container_code=code,
            current_location=self.source_location,
        )
        return WarehouseStockSnapshot.objects.create(
            agency=self.agency,
            stock_unit_type="box",
            source_context_type="receiving",
            source_context_id=code,
            sku_ref=sku,
            sku_code=sku.sku_code,
            name=sku.name,
            barcode=barcode,
            goods_type="gv",
            qty=qty,
            available_qty=qty,
            container=box,
            container_code=box.container_code,
            location=self.source_location,
            zone_code="STORAGE",
            zone_kind=WarehouseLocation.ZONE_KIND_STORAGE,
        )

    def test_fallback_requires_explicit_confirmation_and_keeps_other_box_whole(self):
        with self.assertRaisesMessage(FbsReplenishmentError, "0 из 1"):
            accept_client_movement_request(
                request_id=self.request_row.id,
                accepted_by=self.storekeeper,
            )

        self.request_row.refresh_from_db()
        self.exact_snapshot.refresh_from_db()
        self.fallback_snapshot.refresh_from_db()
        self.assertEqual(self.request_row.status, self.initial_request_status)
        self.assertFalse(FbsReplenishmentPlan.objects.exists())
        self.assertEqual(self.exact_snapshot.available_qty, 100)
        self.assertEqual(self.fallback_snapshot.available_qty, 50)

        result = accept_client_movement_request(
            request_id=self.request_row.id,
            accepted_by=self.storekeeper,
            allow_item_fallback=True,
        )

        self.request_row.refresh_from_db()
        self.exact_snapshot.refresh_from_db()
        self.fallback_snapshot.refresh_from_db()
        self.assertEqual([plan.mode for plan in result.plans], ["box", "item"])
        self.assertEqual(self.request_row.status, FbsClientMovementRequest.STATUS_WAREHOUSE_ACCEPTED)
        self.assertEqual(
            (self.exact_snapshot.available_qty, self.exact_snapshot.other_reserved_qty),
            (0, 100),
        )
        self.assertEqual(
            (self.fallback_snapshot.available_qty, self.fallback_snapshot.other_reserved_qty),
            (49, 1),
        )
        fallback_plan = result.plans[1]
        self.assertTrue(
            fallback_plan.comment.startswith(
                FbsReplenishmentPlan.CLIENT_BOX_ITEM_FALLBACK_MARKER
            )
        )
        self.assertEqual(
            FbsReplenishmentPreparedBox.objects.filter(plan=fallback_plan).count(),
            1,
        )
        self.assertFalse(
            FbsBox.objects.filter(source_container=self.fallback_snapshot.container).exists()
        )
        self.assertEqual(WarehouseOperation.objects.count(), 2)
        self.assertEqual(WarehouseReserve.objects.count(), 2)

        repeated = accept_client_movement_request(
            request_id=self.request_row.id,
            accepted_by=self.storekeeper,
        )
        self.assertEqual(
            [plan.id for plan in repeated.plans],
            [plan.id for plan in result.plans],
        )

    def test_unmarked_item_plan_cannot_be_linked_to_box_request(self):
        plan = FbsReplenishmentPlan(
            agency=self.agency,
            client_movement_request=self.request_row,
            mode=FbsReplenishmentPlan.MODE_ITEM,
            target_cell=self.pallet.cell,
            target_pallet=self.pallet,
            staging_location=WarehouseLocation.objects.get(location_code="FALLBACK-PREP"),
            comment="ordinary item plan",
        )
        with self.assertRaises(ValidationError):
            plan.full_clean()

    def test_operator_shows_and_executes_explicit_fallback_action(self):
        self.client.force_login(self.storekeeper)
        detail_url = f"/fbs/operator/movements/{self.request_row.id}/"
        response = self.client.get(detail_url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Принять с поштучным добором")
        self.assertContains(response, "Поштучно добрать 1 шт.")

        approved = self.client.post(
            f"/fbs/operator/movements/{self.request_row.id}/approve/",
            {"allow_item_fallback": "1"},
        )

        self.assertEqual(approved.status_code, 302)
        self.request_row.refresh_from_db()
        self.assertEqual(
            self.request_row.status,
            FbsClientMovementRequest.STATUS_WAREHOUSE_ACCEPTED,
        )
        self.assertEqual(
            set(self.request_row.replenishment_plans.values_list("mode", flat=True)),
            {"box", "item"},
        )
