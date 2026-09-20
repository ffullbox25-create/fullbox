from unittest.mock import patch

from django.test import TestCase, override_settings, RequestFactory

from audit.models import OrderAuditEntry
from fbs.exceptions import FbsReplenishmentError, FbsStorageError
from fbs.models import FbsClientMovementRequest, FbsPallet, FbsStorageCell, FbsBox
from fbs.test_ready_processing_movement import ReadyProcessingMovementTests as Fixtures
from fbs.services.receiving_destinations import (
    allocate_selected_receiving_fbs_pallet, receiving_fbs_destination_options,
)
from fbs.services.receiving_movements import auto_route_receiving_pallet_to_fbs
from orders.test_receiving_allow_materializes_stock import ReceivingAllowMaterializesStockTests, ORDER_ID
from orders.services import ReceivingWorkflowService
from sklad.models import WarehouseContainer, WarehouseLocation, WarehouseReserve
from sku.models import Agency


@override_settings(FBS_MODULE_ENABLED=True, FBS_WAREHOUSE_WRITES_ENABLED=True,
                   FBS_ZONE_CODE="FBS", FBS_CLIENT_FULL_PALLET_LOGICAL_POST_ON_ACCEPT=False)
class ManualReceivingDestinationTests(TestCase):
    _source_box = Fixtures._source_box
    _snapshot = Fixtures._snapshot
    ready_box = Fixtures.ready_box
    receiving_box = Fixtures.receiving_box
    receiving_permission = Fixtures.receiving_permission

    def setUp(self):
        Fixtures.setUp(self)
        self.first = self.make_cell("AA-FIRST", 71)
        self.selected = self.make_cell("ZZ-SELECTED", 72)

    def make_cell(self, code, row):
        location = WarehouseLocation.objects.create(
            warehouse_code="MSK", zone_code="OS", zone_kind="storage",
            location_code=code, row_no=row, section_no=1, tier_no=1, cell_no=1,
            is_active=True, is_storage=True,
        )
        return FbsStorageCell.objects.create(cell_code=code, location=location)

    def route(self, cell_id=None):
        return auto_route_receiving_pallet_to_fbs(
            agency_id=self.agency.id, order_id="PR-FBS-RELEASE",
            pallet_code=self.ready_pallet.container_code,
            fbs_cell_id=cell_id, user=self.storekeeper,
        )

    def test_without_selection_has_no_reservation_or_movement(self):
        self.receiving_box("NO-SELECTION")
        for value in (None, "", 0, "invalid"):
            with self.assertRaises(FbsReplenishmentError):
                self.route(value)
        self.assertFalse(FbsClientMovementRequest.objects.exists())
        self.assertFalse(WarehouseReserve.objects.exists())

    def test_selected_cell_not_first_free_is_used_and_repeat_is_idempotent(self):
        source = self.receiving_box("SELECTED")
        result = self.route(self.selected.id)
        self.assertEqual(result["status"], "assigned")
        self.assertEqual(result["targets"][0]["cell_id"], self.selected.id)
        self.assertFalse(FbsPallet.objects.filter(cell=self.first).exists())
        again = self.route(self.selected.id)
        self.assertEqual(result, again)
        self.assertEqual(FbsClientMovementRequest.objects.count(), 1)
        source.refresh_from_db()
        self.assertEqual(source.location_id, self.ready_location.id)

    def test_existing_movement_is_not_silently_retargeted(self):
        self.receiving_box("EXISTING")
        self.route(self.selected.id)
        with self.assertRaisesMessage(FbsReplenishmentError, "уже создано перемещение"):
            self.route(self.first.id)
        self.assertFalse(FbsPallet.objects.filter(cell=self.first).exists())

    def test_occupied_cell_does_not_fall_back_and_rolls_back_reserve(self):
        source = self.receiving_box("OCCUPIED")
        WarehouseContainer.objects.create(
            agency=self.agency, container_type="pallet", container_code="OCCUPANT",
            current_location=self.selected.location, status="active",
        )
        with self.assertRaises(FbsStorageError):
            self.route(self.selected.id)
        self.assertFalse(FbsClientMovementRequest.objects.exists())
        self.assertFalse(WarehouseReserve.objects.exists())
        self.assertFalse(FbsPallet.objects.filter(cell=self.first).exists())
        source.refresh_from_db()
        self.assertEqual(source.available_qty, 5)

    def test_foreign_inactive_virtual_and_missing_cells_are_rejected(self):
        self.receiving_box("BAD-CELLS")
        other = Agency.objects.create(agn_name="Other owner")
        self.selected.client_cluster = other.id
        self.selected.save()
        with self.assertRaises(FbsStorageError):
            self.route(self.selected.id)
        self.selected.client_cluster = 0
        self.selected.is_active = False
        self.selected.save()
        with self.assertRaises(FbsStorageError):
            self.route(self.selected.id)
        self.selected.is_active = True
        self.selected.save()
        self.selected.location.zone_kind = WarehouseLocation.ZONE_KIND_VIRTUAL
        self.selected.location.save()
        with self.assertRaises(FbsStorageError):
            self.route(self.selected.id)
        with self.assertRaises(FbsStorageError):
            self.route(999999)
        self.assertFalse(FbsClientMovementRequest.objects.exists())

    def test_capacity_is_rechecked_without_another_cell_fallback(self):
        pallet = FbsPallet.objects.create(
            agency=self.agency, cell=self.selected, pallet_code="FULL", max_boxes=1,
        )
        FbsBox.objects.create(agency=self.agency, pallet=pallet, box_code="FULL-BOX")
        with self.assertRaisesMessage(FbsStorageError, "нужно 1"):
            allocate_selected_receiving_fbs_pallet(
                agency=self.agency, cell_id=self.selected.id, required_box_slots=1,
            )
        self.assertFalse(FbsPallet.objects.filter(cell=self.first).exists())

    def test_available_own_pallet_is_reused(self):
        pallet = FbsPallet.objects.create(
            agency=self.agency, cell=self.selected, pallet_code="OWN", max_boxes=3,
        )
        allocated = allocate_selected_receiving_fbs_pallet(
            agency=self.agency, cell_id=self.selected.id, required_box_slots=2,
        )
        self.assertEqual(allocated.id, pallet.id)

    def test_options_are_read_only_and_exclude_occupied_and_other_client(self):
        other = Agency.objects.create(agn_name="Foreign")
        FbsPallet.objects.create(agency=other, cell=self.first, pallet_code="FOREIGN")
        before = (FbsPallet.objects.count(), WarehouseContainer.objects.count())
        ids = {row["cell_id"] for row in receiving_fbs_destination_options(agency_id=self.agency.id)}
        self.assertIn(self.selected.id, ids)
        self.assertNotIn(self.first.id, ids)
        self.assertEqual(before, (FbsPallet.objects.count(), WarehouseContainer.objects.count()))


class ReceivingManualGuardTests(TestCase):
    _box = ReceivingAllowMaterializesStockTests._box
    _pallet = ReceivingAllowMaterializesStockTests._pallet
    _flow_state = ReceivingAllowMaterializesStockTests._flow_state
    snapshots = ReceivingAllowMaterializesStockTests.snapshots

    def setUp(self):
        ReceivingAllowMaterializesStockTests.setUp(self)
        OrderAuditEntry.objects.create(
            agency=self.agency, order_id=ORDER_ID, order_type="receiving", action="update",
            payload={"receiving_route": "fbs"},
        )

    def allow(self, cell_id=None):
        return ReceivingWorkflowService.allow_receiving_flow_pallet_placement(
            order_id=ORDER_ID, pallet_code="PAL-MAT-1-gv", fbs_cell_id=cell_id, user=self.user,
        )

    def test_missing_choice_rejected_before_materialization(self):
        before = OrderAuditEntry.objects.count()
        self.assertEqual(self.allow()["status"], "fbs_destination_required")
        self.assertFalse(self.snapshots().exists())
        self.assertEqual(before, OrderAuditEntry.objects.count())

    def test_unavailable_choice_rolls_back_permission_and_stock(self):
        before = OrderAuditEntry.objects.count()
        with patch.object(ReceivingWorkflowService, "_auto_route_receiving_pallet_to_fbs", return_value={"status": "blocked", "message": "Занято"}):
            result = self.allow(123)
        self.assertEqual(result["status"], "fbs_destination_invalid")
        self.assertFalse(self.snapshots().exists())
        self.assertEqual(before, OrderAuditEntry.objects.count())

    def test_permission_passes_and_audits_explicit_selection(self):
        with patch.object(ReceivingWorkflowService, "_auto_route_receiving_pallet_to_fbs", return_value={"status": "assigned"}) as route:
            result = self.allow(123)
        self.assertEqual(result["status"], "allowed")
        self.assertEqual(route.call_args.kwargs["fbs_cell_id"], 123)
        entry = OrderAuditEntry.objects.get(payload__event="receiving_pallet_placement_permission")
        self.assertEqual(entry.payload["fbs_cell_id"], 123)

    def test_http_api_requires_selection_and_rejects_non_storekeeper(self):
        from orders.web_ui import receiving_flow_pallet_placement_permission
        factory = RequestFactory()
        request = factory.post("/", {"pallet_code": "PAL-MAT-1-gv"}, content_type="application/json")
        request.user = self.user
        with patch("orders.web_ui.get_request_role", return_value="storekeeper"), patch("orders.web_ui._receiving_owner_lock_response", return_value=None):
            self.assertEqual(receiving_flow_pallet_placement_permission(request, ORDER_ID).status_code, 409)
        with patch("orders.web_ui.get_request_role", return_value="manager"):
            self.assertEqual(receiving_flow_pallet_placement_permission(request, ORDER_ID).status_code, 403)
