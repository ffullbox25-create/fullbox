from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import include, path, reverse

from employees.models import Employee
from sklad.models import WarehouseContainer, WarehouseLocation
from sku.models import Agency, SKU

from .exceptions import FbsError, FbsPickingError
from .models import (
    FbsBox,
    FbsIntegrationProfile,
    FbsInventorySession,
    FbsOrder,
    FbsOrderItem,
    FbsOrderStockAllocation,
    FbsOrderTraceability,
    FbsPallet,
    FbsPickBatch,
    FbsPickScanEvent,
    FbsPickTask,
    FbsRack,
    FbsRackCell,
    FbsRackCellBinding,
    FbsStockBalance,
    FbsStorageCell,
)
from .services.inventory import create_inventory_session
from .services.physical_locations import fbs_box_physical_location_code
from .services.picking import (
    complete_pick_allocation,
    pick_requires_box_scan,
    validate_pick_box_scan,
    validate_pick_cell_scan,
)
from .tsd_views import _pick_box_session_key, _pick_cell_session_key


urlpatterns = [path("fbs/", include("fbs.urls"))]


@override_settings(
    ROOT_URLCONF="fbs.test_pick_scan_flow",
    FBS_MODULE_ENABLED=True,
    FBS_WAREHOUSE_WRITES_ENABLED=True,
    FBS_STATUS_PULL_ENABLED=False,
    FBS_OUTBOX_ENABLED=False,
)
class FbsPickScanFlowTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="scan-flow-picker")
        Employee.objects.create(user=self.user, role="picker", full_name="Сборщик")
        self.agency = Agency.objects.create(agn_name="Scan flow client")
        self.profile = FbsIntegrationProfile.objects.create(
            agency=self.agency,
            marketplace=FbsIntegrationProfile.MARKETPLACE_OZON,
            name="Scan flow profile",
            external_warehouse_id="scan-flow-warehouse",
            is_active=True,
        )
        self.sku = SKU.objects.create(
            agency=self.agency, sku_code="SCAN-SKU", name="Товар для отбора"
        )
        self.barcode = "4600000000911"
        self.cell = self.make_cell("SCAN-LOCATION")
        self.box = self.make_box("SCAN-BOX", self.cell)
        self.batch = FbsPickBatch.objects.create(
            agency=self.agency,
            status=FbsPickBatch.STATUS_IN_PROGRESS,
            assigned_to=self.user,
            planned_qty=1,
        )
        self.order = FbsOrder.objects.create(
            profile=self.profile,
            external_order_id="SCAN-ORDER",
            internal_status=FbsOrder.STATUS_PICKING,
        )
        self.task = FbsPickTask.objects.create(
            batch=self.batch,
            order=self.order,
            status=FbsPickTask.STATUS_IN_PROGRESS,
            assigned_to=self.user,
            planned_qty=1,
        )
        self.allocation = self.add_allocation(self.box)
        self.balance = self.allocation.balance
        self.url = self.allocation_url(self.allocation)
        self.client.force_login(self.user)

    def make_cell(self, code):
        location = WarehouseLocation.objects.create(
            warehouse_code="MSK",
            zone_code="PR",
            zone_kind=WarehouseLocation.ZONE_KIND_STORAGE,
            location_code=code,
            is_active=True,
            is_storage=True,
            is_pickable=True,
            is_fbs_visible=True,
            is_topology_visible=False,
        )
        return FbsStorageCell.objects.create(
            cell_code=code, location=location, purpose=FbsStorageCell.PURPOSE_PICK
        )

    def make_box(self, code, cell):
        pallet, _ = FbsPallet.objects.get_or_create(
            cell=cell,
            status=FbsPallet.STATUS_ACTIVE,
            defaults={"agency": self.agency, "pallet_code": f"{code}-PALLET"},
        )
        return FbsBox.objects.create(
            agency=self.agency, box_code=code, pallet=pallet,
            status=FbsBox.STATUS_ACTIVE,
        )

    def bind_rack(self, box):
        cell = box.pallet.cell
        rack, _ = FbsRack.objects.get_or_create(location=cell.location)
        rack_cell = FbsRackCell.objects.create(
            rack=rack, storage_cell=cell, position=1
        )
        box.pallet.is_rack_binding = True
        box.pallet.save(update_fields=["is_rack_binding", "updated_at"])
        box.source_container = WarehouseContainer.objects.create(
            agency=self.agency,
            container_type=WarehouseContainer.TYPE_BOX,
            container_code=box.box_code,
            current_location=cell.location,
            source_context_type="fbs_rack_cell",
            source_context_id=str(rack_cell.id),
        )
        box.save(update_fields=["source_container", "updated_at"])
        binding = FbsRackCellBinding.objects.create(
            rack_cell=rack_cell, agency=self.agency, pallet=box.pallet, box=box
        )
        self.assertFalse(pick_requires_box_scan(box))
        return binding

    def add_allocation(self, box, *, balance=None, quantity=1):
        number = self.task.allocations.count() + 1
        if balance is None:
            balance = FbsStockBalance.objects.create(
                agency=self.agency, box=box, sku_ref=self.sku,
                identity_key=f"scan-{number}", sku_code=self.sku.sku_code,
                name=self.sku.name, barcode=self.barcode,
                qty=quantity + 1, available_qty=1, reserved_qty=quantity,
            )
        else:
            balance.qty += quantity
            balance.reserved_qty += quantity
            balance.save(update_fields=["qty", "reserved_qty", "updated_at"])
        item = FbsOrderItem.objects.create(
            order=self.order, external_line_id=f"SCAN-LINE-{number}",
            external_sku=self.sku.sku_code, sku=self.sku,
            barcode=self.barcode, product_name=self.sku.name, quantity=quantity,
        )
        allocation = FbsOrderStockAllocation.objects.create(
            order_item=item, balance=balance, pick_task=self.task,
            qty_reserved=quantity, status=FbsOrderStockAllocation.STATUS_PICKING,
        )
        FbsOrderTraceability.objects.create(
            allocation=allocation, qty=quantity,
            status=FbsOrderTraceability.STATUS_RESERVED,
        )
        self.task.planned_qty = sum(self.task.allocations.values_list("qty_reserved", flat=True))
        self.task.save(update_fields=["planned_qty", "updated_at"])
        self.batch.planned_qty = self.task.planned_qty
        self.batch.save(update_fields=["planned_qty", "updated_at"])
        return allocation

    def allocation_url(self, allocation):
        return reverse("fbs:tsd_pick_allocation", args=[allocation.id])

    def pick(self, **overrides):
        args = dict(
            allocation_id=self.allocation.id, cell_scan="",
            box_scan=self.box.box_code, item_scan=self.barcode,
            performed_by=self.user,
        )
        args.update(overrides)
        return complete_pick_allocation(**args)

    def assert_unchanged(self):
        self.balance.refresh_from_db()
        self.allocation.refresh_from_db()
        self.assertEqual((self.balance.qty, self.balance.reserved_qty), (2, 1))
        self.assertEqual(self.allocation.qty_picked, 0)
        self.assertEqual(self.allocation.status, FbsOrderStockAllocation.STATUS_PICKING)

    def confirm_source(self, box=None):
        box = box or self.box
        if pick_requires_box_scan(box):
            data = {"action": "scan_box", "box_scan": box.box_code}
        else:
            data = {"action": "scan_cell", "cell_scan": fbs_box_physical_location_code(box)}
        response = self.client.post(self.url, data)
        self.assertContains(response, 'name="item_scan"')

    def complete_http(self, url=None):
        return self.client.post(url or self.url, {"action": "complete", "item_scan": self.barcode})

    def test_box_then_item_without_cell_picks_exactly_one_unit(self):
        validate_pick_box_scan(
            allocation_id=self.allocation.id, box_scan=self.box.box_code,
            performed_by=self.user,
        )
        result = self.pick()
        self.assertEqual(result.status, FbsOrderStockAllocation.STATUS_PICKED)
        self.assertEqual(result.qty_picked, 1)
        self.balance.refresh_from_db()
        self.assertEqual((self.balance.qty, self.balance.reserved_qty), (1, 0))
        self.assertFalse(FbsPickScanEvent.objects.filter(stage=FbsPickScanEvent.STAGE_CELL).exists())
        event = FbsPickScanEvent.objects.get(
            allocation=self.allocation, stage=FbsPickScanEvent.STAGE_BOX,
            result=FbsPickScanEvent.RESULT_SUCCESS,
        )
        self.assertIn(self.cell.cell_code, event.message)
        with self.assertRaisesMessage(FbsPickingError, "Позиция уже отобрана."):
            self.pick()
        self.balance.refresh_from_db()
        self.assertEqual((self.balance.qty, self.balance.reserved_qty), (1, 0))

    def test_box_pick_does_not_validate_obsolete_cell_scan(self):
        self.assertEqual(self.pick(cell_scan="OLD-CELL").qty_picked, 1)

    def test_box_pick_requires_box_even_with_valid_cell(self):
        for cell_scan in ("", fbs_box_physical_location_code(self.box)):
            with self.subTest(cell_scan=cell_scan):
                with self.assertRaisesMessage(FbsPickingError, "Скан короба FBS не совпадает с заданием."):
                    self.pick(box_scan="", cell_scan=cell_scan)
                self.assert_unchanged()

    def test_wrong_box_is_rejected_and_logged_even_in_same_cell(self):
        other_box = self.make_box("OTHER-BOX", self.cell)
        with self.assertRaisesMessage(FbsPickingError, "Скан короба FBS не совпадает с заданием."):
            validate_pick_box_scan(
                allocation_id=self.allocation.id, box_scan=other_box.box_code,
                performed_by=self.user,
            )
        event = FbsPickScanEvent.objects.get(
            allocation=self.allocation, stage=FbsPickScanEvent.STAGE_BOX,
            result=FbsPickScanEvent.RESULT_ERROR,
        )
        self.assertEqual(event.scan_value, other_box.box_code)
        self.assertEqual(event.expected_value, self.box.box_code)
        with self.assertRaisesMessage(FbsPickingError, "Скан короба FBS не совпадает с заданием."):
            self.pick(box_scan=other_box.box_code)
        self.assert_unchanged()

    def test_box_event_records_physical_container_address_instead_of_plan_cell(self):
        physical_cell = self.make_cell("ACTUAL-BOX-LOCATION")
        self.box.source_container = WarehouseContainer.objects.create(
            agency=self.agency, container_type=WarehouseContainer.TYPE_BOX,
            container_code=self.box.box_code, current_location=physical_cell.location,
        )
        self.box.save(update_fields=["source_container", "updated_at"])
        validate_pick_box_scan(
            allocation_id=self.allocation.id, box_scan=self.box.box_code,
            performed_by=self.user,
        )
        event = FbsPickScanEvent.objects.get(
            allocation=self.allocation, stage=FbsPickScanEvent.STAGE_BOX,
            result=FbsPickScanEvent.RESULT_SUCCESS,
        )
        self.assertIn("ACTUAL-BOX-LOCATION", event.message)
        self.assertNotIn(self.cell.cell_code, event.message)

    def test_rack_requires_correct_cell_and_accepts_item_without_box(self):
        self.bind_rack(self.box)
        for cell_scan in ("", "WRONG-CELL"):
            with self.subTest(cell_scan=cell_scan):
                with self.assertRaisesMessage(FbsPickingError, "Скан ячейки не совпадает с физическим адресом маршрута."):
                    self.pick(box_scan="", cell_scan=cell_scan)
                self.assert_unchanged()
        cell_scan = fbs_box_physical_location_code(self.box)
        validate_pick_cell_scan(
            allocation_id=self.allocation.id, cell_scan=cell_scan, performed_by=self.user,
        )
        self.assertEqual(self.pick(box_scan="", cell_scan=cell_scan).status, FbsOrderStockAllocation.STATUS_PICKED)
        self.balance.refresh_from_db()
        self.assertEqual((self.balance.qty, self.balance.reserved_qty), (1, 0))

    def test_unverified_rack_flag_still_requires_box(self):
        binding = self.bind_rack(self.box)
        binding.delete()
        self.assertTrue(pick_requires_box_scan(self.box))
        with self.assertRaisesMessage(FbsPickingError, "Скан короба FBS не совпадает с заданием."):
            self.pick(box_scan="", cell_scan=fbs_box_physical_location_code(self.box))
        self.assert_unchanged()

    def test_item_scan_remains_required_for_both_sources(self):
        for direct_cell in (False, True):
            if direct_cell:
                self.bind_rack(self.box)
            for item_scan in ("", "WRONG-ITEM"):
                with self.subTest(direct_cell=direct_cell, item_scan=item_scan):
                    with self.assertRaisesMessage(FbsPickingError, "Штрихкод товара не совпадает с заданием."):
                        self.pick(
                            box_scan="" if direct_cell else self.box.box_code,
                            cell_scan=fbs_box_physical_location_code(self.box) if direct_cell else "",
                            item_scan=item_scan,
                        )
                    self.assert_unchanged()

    def test_box_pick_honors_inventory_lock(self):
        create_inventory_session(
            scope_type="box", mode=FbsInventorySession.MODE_IMMEDIATE,
            box=self.box, agency=self.agency, created_by=self.user,
        )
        with self.assertRaises(FbsError):
            self.pick()
        self.assert_unchanged()

    def test_box_pick_rejects_other_picker(self):
        other = get_user_model().objects.create_user(username="other-scan-picker")
        with self.assertRaisesMessage(FbsPickingError, "Задание назначено другому сборщику."):
            self.pick(performed_by=other)
        self.assert_unchanged()

    def test_http_box_is_step_one_and_item_is_step_two(self):
        response = self.client.get(self.url)
        self.assertTrue(response.context["requires_box_scan"])
        self.assertFalse(response.context["requires_cell_scan"])
        self.assertContains(response, self.cell.cell_code)
        self.assertContains(response, 'name="box_scan"')
        self.assertContains(response, 'picker-action-number">1</span>')
        self.assertNotContains(response, 'name="cell_scan"')
        response = self.client.post(self.url, {"action": "scan_box", "box_scan": self.box.box_code})
        self.assertContains(response, 'name="item_scan"')
        self.assertContains(response, 'picker-action-number">2</span>')
        self.assertNotIn(_pick_cell_session_key(self.allocation.id), self.client.session)
        self.assertEqual(self.complete_http().status_code, 302)
        self.allocation.refresh_from_db()
        self.assertEqual(self.allocation.status, FbsOrderStockAllocation.STATUS_PICKED)

    def test_http_box_rejects_cell_step_and_cannot_skip_box(self):
        response = self.client.post(self.url, {"action": "scan_cell", "cell_scan": self.cell.cell_code})
        self.assertContains(response, "Отсканируйте короб", status_code=400)
        session = self.client.session
        session[_pick_cell_session_key(self.allocation.id)] = self.cell.cell_code
        session.save()
        response = self.client.post(self.url, {
            "action": "complete", "item_scan": self.barcode,
            "box_scan": self.box.box_code, "requires_box_scan": "0",
        })
        self.assertContains(response, "Сначала подтвердите короб.", status_code=400)
        self.assert_unchanged()

    def test_http_multiple_units_keep_box_confirmation_until_position_is_complete(self):
        FbsOrderStockAllocation.objects.filter(pk=self.allocation.id).update(qty_reserved=2)
        FbsOrderItem.objects.filter(pk=self.allocation.order_item_id).update(quantity=2)
        FbsOrderTraceability.objects.filter(allocation=self.allocation).update(qty=2)
        FbsStockBalance.objects.filter(pk=self.balance.id).update(qty=3, reserved_qty=2)
        FbsPickTask.objects.filter(pk=self.task.id).update(planned_qty=2)
        FbsPickBatch.objects.filter(pk=self.batch.id).update(planned_qty=2)
        self.confirm_source()
        self.assertRedirects(self.complete_http(), self.url, fetch_redirect_response=False)
        self.assertEqual(self.client.session[_pick_box_session_key(self.allocation.id)], self.box.box_code)
        self.assertNotIn(_pick_cell_session_key(self.allocation.id), self.client.session)
        self.allocation.refresh_from_db()
        self.assertEqual(self.allocation.status, FbsOrderStockAllocation.STATUS_PICKING)
        self.assertEqual(self.allocation.qty_picked, 1)
        self.assertEqual(self.complete_http().status_code, 302)
        self.allocation.refresh_from_db()
        self.balance.refresh_from_db()
        self.assertEqual(self.allocation.status, FbsOrderStockAllocation.STATUS_PICKED)
        self.assertEqual((self.balance.qty, self.balance.reserved_qty), (1, 0))
        self.assertNotIn(_pick_box_session_key(self.allocation.id), self.client.session)

    def test_http_rack_still_uses_cell_then_item(self):
        self.bind_rack(self.box)
        response = self.client.get(self.url)
        self.assertTrue(response.context["requires_cell_scan"])
        self.assertContains(response, 'name="cell_scan"')
        self.assertNotContains(response, 'name="box_scan"')
        response = self.complete_http()
        self.assertContains(response, "Сначала подтвердите ячейку.", status_code=400)
        self.assert_unchanged()
        self.confirm_source()
        self.assertEqual(self.complete_http().status_code, 302)
        self.allocation.refresh_from_db()
        self.assertEqual(self.allocation.status, FbsOrderStockAllocation.STATUS_PICKED)

    def test_same_box_carries_only_box_confirmation(self):
        next_allocation = self.add_allocation(self.box, balance=self.balance)
        self.confirm_source()
        self.assertRedirects(self.complete_http(), self.allocation_url(next_allocation), fetch_redirect_response=False)
        session = self.client.session
        self.assertEqual(session[_pick_box_session_key(next_allocation.id)], self.box.box_code)
        self.assertNotIn(_pick_cell_session_key(next_allocation.id), session)
        self.assertEqual(self.complete_http(self.allocation_url(next_allocation)).status_code, 302)
        next_allocation.refresh_from_db()
        self.assertEqual(next_allocation.status, FbsOrderStockAllocation.STATUS_PICKED)

    def test_other_box_in_same_cell_needs_new_box_scan(self):
        next_box = self.make_box("NEXT-BOX", self.cell)
        next_allocation = self.add_allocation(next_box)
        self.confirm_source()
        self.assertRedirects(self.complete_http(), self.allocation_url(next_allocation), fetch_redirect_response=False)
        self.assertNotIn(_pick_box_session_key(next_allocation.id), self.client.session)
        self.assertNotIn(_pick_cell_session_key(next_allocation.id), self.client.session)
        response = self.complete_http(self.allocation_url(next_allocation))
        self.assertContains(response, "Сначала подтвердите короб.", status_code=400)

    def test_same_rack_carries_only_cell_confirmation(self):
        self.bind_rack(self.box)
        next_allocation = self.add_allocation(self.box, balance=self.balance)
        self.confirm_source()
        self.assertRedirects(self.complete_http(), self.allocation_url(next_allocation), fetch_redirect_response=False)
        session = self.client.session
        self.assertEqual(session[_pick_cell_session_key(next_allocation.id)], self.cell.cell_code)
        self.assertNotIn(_pick_box_session_key(next_allocation.id), session)
        self.assertEqual(self.complete_http(self.allocation_url(next_allocation)).status_code, 302)
        next_allocation.refresh_from_db()
        self.assertEqual(next_allocation.status, FbsOrderStockAllocation.STATUS_PICKED)

    def test_box_to_rack_in_same_cell_requires_cell_scan(self):
        next_box = self.make_box("NEXT-RACK", self.cell)
        self.bind_rack(next_box)
        next_allocation = self.add_allocation(next_box)
        self.confirm_source()
        self.assertRedirects(self.complete_http(), self.allocation_url(next_allocation), fetch_redirect_response=False)
        self.assertNotIn(_pick_cell_session_key(next_allocation.id), self.client.session)
        response = self.complete_http(self.allocation_url(next_allocation))
        self.assertContains(response, "Сначала подтвердите ячейку.", status_code=400)

    def test_rack_to_box_in_same_cell_requires_box_scan(self):
        self.bind_rack(self.box)
        next_box = self.make_box("NEXT-PHYSICAL-BOX", self.cell)
        next_allocation = self.add_allocation(next_box)
        self.confirm_source()
        self.assertRedirects(self.complete_http(), self.allocation_url(next_allocation), fetch_redirect_response=False)
        self.assertNotIn(_pick_cell_session_key(next_allocation.id), self.client.session)
        self.assertNotIn(_pick_box_session_key(next_allocation.id), self.client.session)
        response = self.complete_http(self.allocation_url(next_allocation))
        self.assertContains(response, "Сначала подтвердите короб.", status_code=400)

    def test_other_rack_cell_requires_new_cell_scan(self):
        self.bind_rack(self.box)
        next_box = self.make_box("OTHER-RACK", self.make_cell("OTHER-RACK-CELL"))
        self.bind_rack(next_box)
        next_allocation = self.add_allocation(next_box)
        self.confirm_source()
        self.assertRedirects(self.complete_http(), self.allocation_url(next_allocation), fetch_redirect_response=False)
        self.assertNotIn(_pick_cell_session_key(next_allocation.id), self.client.session)
        response = self.complete_http(self.allocation_url(next_allocation))
        self.assertContains(response, "Сначала подтвердите ячейку.", status_code=400)
