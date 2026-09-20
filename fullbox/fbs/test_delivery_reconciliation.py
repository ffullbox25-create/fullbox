from unittest.mock import patch
from django.test import TestCase, TransactionTestCase, override_settings
from audit.models import OrderAuditEntry
from sku.models import Agency
from fbs.models import (
    FbsOrder, FbsOrderItem, FbsOrderStockAllocation, FbsPickBatch,
    FbsPickRestockRequest, FbsPickRestockLine,
)
from fbs.test_status_reconciliation import FbsDuplicateStatusReconciliationTests
from fbs.services.delivery_reconciliation import inspect_delivered_order, reconcile_delivered_order
from fbs.services.sync import _ingest_status


@override_settings(FBS_MODULE_ENABLED=True, FBS_WAREHOUSE_WRITES_ENABLED=True)
class FbsDeliveryReconciliationTests(TestCase):
    setUp = FbsDuplicateStatusReconciliationTests.setUp

    def fixture(self, *, marketplace="wb", status="ready_for_handover", quantity=1, picked=1):
        self.profile.marketplace = marketplace
        self.profile.save(update_fields=["marketplace"])
        self.order = FbsOrder.objects.create(
            profile=self.profile, external_order_id="7700", internal_status=status,
            marketplace_status="complete" if marketplace == "wb" else "delivered",
            marketplace_substatus="sold" if marketplace == "wb" else "posting_received",
        )
        self.item = FbsOrderItem.objects.create(order=self.order, external_line_id="1",
            external_sku="1", sku=self.sku, barcode=self.barcode, quantity=quantity)
        self.allocation = FbsOrderStockAllocation.objects.create(order_item=self.item,
            balance=self.balance, qty_reserved=quantity, qty_picked=picked,
            status="picked" if picked == quantity else "released")
        self.confirmation = dict(profile_id=self.profile.pk, external_order_id="7700",
            marketplace_status=self.order.marketplace_status,
            marketplace_substatus=self.order.marketplace_substatus,
            raw_payload={"id":7700,"status":self.order.marketplace_status}, source="test")
        return self.order

    def apply(self):
        return reconcile_delivered_order(order_id=self.order.pk, confirmation=self.confirmation)

    def stock(self):
        self.balance.refresh_from_db()
        return self.balance.qty, self.balance.available_qty, self.balance.reserved_qty

    def test_delivery_changes_status_not_stock_and_replay_is_noop(self):
        self.fixture()
        before=self.stock()
        self.assertEqual(self.apply()["action"], "reconciled")
        self.assertEqual(self.stock(), before)
        self.assertEqual(self.apply()["action"], "unchanged")
        self.assertEqual(self.stock(), before)
        entries=OrderAuditEntry.objects.filter(payload__source="marketplace_delivery_reconciliation")
        self.assertEqual(entries.count(), 1)
        self.assertEqual(entries.get().payload["debit_qty"],0)
        self.assertEqual(entries.get().payload["allocation_ids"],[self.allocation.pk])

    def test_ozon_picked_delivery_is_reconciled(self):
        self.fixture(marketplace="ozon",status="picked")
        self.assertEqual(self.apply()["action"],"reconciled")

    def test_warehouse_handed_over_advances_to_delivered(self):
        self.fixture(status="handed_over")
        self.assertEqual(self.apply()["action"],"reconciled")

    def test_incomplete_debit_cannot_take_other_stock(self):
        self.fixture(quantity=2,picked=1)
        before=self.stock()
        self.assertEqual(self.apply()["action"],"blocked")
        self.assertEqual(self.stock(),before)

    def test_no_source_allocation_is_blocked(self):
        self.fixture(picked=0)
        self.allocation.delete()
        self.assertEqual(self.apply()["reason"],"no_source_allocations")

    def test_cancellation_is_not_overridden(self):
        self.fixture(status="cancelled")
        self.assertEqual(self.apply()["reason"],"local_status_excluded")

    def test_wave_return_without_order_link_blocks(self):
        self.fixture()
        batch=FbsPickBatch.objects.create(agency=self.agency,planned_qty=1)
        request=FbsPickRestockRequest.objects.create(batch=batch,planned_qty=1,
            returned_qty=1,status="completed",reason="Return")
        FbsPickRestockLine.objects.create(request=request,allocation=self.allocation,
            source_balance=self.balance,source_box=self.balance.box,
            source_cell=self.balance.box.pallet.cell,planned_qty=1,returned_qty=1,status="completed")
        self.assertEqual(self.apply()["reason"],"return_conflict")

    def test_canceled_return_with_actual_quantity_still_blocks(self):
        self.fixture()
        batch=FbsPickBatch.objects.create(agency=self.agency,planned_qty=1)
        FbsPickRestockRequest.objects.create(batch=batch,order=self.order,planned_qty=1,
            returned_qty=1,status="canceled",reason="Return")
        self.assertEqual(self.apply()["reason"],"return_conflict")

    def test_client_mismatch_blocks(self):
        self.fixture()
        self.balance.agency=Agency.objects.create(agn_name="Other")
        self.balance.save(update_fields=["agency"])
        self.assertEqual(self.apply()["reason"],"client_mismatch")

    def test_active_reserve_is_untouched(self):
        self.fixture(picked=0)
        self.allocation.status="reserved"
        self.allocation.save(update_fields=["status"])
        self.assertEqual(self.apply()["reason"],"active_reservation")
        self.allocation.refresh_from_db()
        self.assertEqual(self.allocation.status,"reserved")

    def test_item_quantities_must_match_individually(self):
        self.fixture()
        FbsOrderItem.objects.create(order=self.order,external_line_id="2",external_sku="2",quantity=1)
        self.assertEqual(self.apply()["reason"],"debit_not_proven")

    def test_stale_or_wrong_profile_evidence_blocks(self):
        self.fixture()
        self.confirmation["profile_id"]+=1
        self.assertEqual(self.apply()["reason"],"confirmation_mismatch")
        self.confirmation["profile_id"]-=1
        self.confirmation["marketplace_substatus"]="waiting"
        self.assertEqual(self.apply()["reason"],"confirmation_mismatch")

    def test_wb_complete_sorted_is_not_delivery(self):
        self.fixture()
        self.order.marketplace_substatus="sorted"
        self.order.save(update_fields=["marketplace_substatus"])
        self.confirmation["marketplace_substatus"]="sorted"
        self.assertEqual(self.apply()["reason"],"marketplace_not_delivered")

    def test_dry_inspection_has_no_writes(self):
        self.fixture()
        before=OrderAuditEntry.objects.count()
        self.assertEqual(inspect_delivered_order(self.order.pk)["action"],"reconcile_status")
        self.order.refresh_from_db()
        self.assertEqual(self.order.internal_status,"ready_for_handover")
        self.assertEqual(OrderAuditEntry.objects.count(),before)

    @override_settings(FBS_WAREHOUSE_WRITES_ENABLED=False)
    def test_disabled_writes_block(self):
        self.fixture()
        self.assertEqual(self.apply()["reason"],"writes_disabled")

    def test_failed_audit_rolls_back_status(self):
        self.fixture()
        with patch("fbs.services.delivery_reconciliation.log_order_snapshot",side_effect=RuntimeError("audit failed")):
            with self.assertRaises(RuntimeError): self.apply()
        self.order.refresh_from_db()
        self.assertEqual(self.order.internal_status,"ready_for_handover")

    def test_status_ingestion_and_duplicate_repair(self):
        self.fixture()
        _ingest_status(self.profile,self.confirmation)
        self.order.refresh_from_db()
        self.assertEqual(self.order.internal_status,"delivered")
        FbsOrder.objects.filter(pk=self.order.pk).update(internal_status="ready_for_handover")
        self.assertEqual(_ingest_status(self.profile,self.confirmation),"duplicate")
        self.order.refresh_from_db()
        self.assertEqual(self.order.internal_status,"delivered")

    def test_delivered_import_does_not_release_unconsumed_reserved_stock(self):
        self.fixture(status="reserved",picked=0)
        self.allocation.status="reserved"
        self.allocation.save(update_fields=["status"])
        self.balance.available_qty=0
        self.balance.reserved_qty=1
        self.balance.save(update_fields=["available_qty","reserved_qty"])
        _ingest_status(self.profile,self.confirmation)
        self.order.refresh_from_db()
        self.allocation.refresh_from_db()
        self.assertEqual(self.order.internal_status,"reserved")
        self.assertEqual(self.allocation.status,"reserved")
        self.assertEqual(self.stock(),(1,0,1))

    def test_command_requires_explicit_apply_scope(self):
        from django.core.management import call_command, CommandError
        with self.assertRaises(CommandError):
            call_command("reconcile_fbs_deliveries",apply=True)

    def test_command_refreshes_marketplace_before_apply(self):
        from django.core.management import call_command
        from io import StringIO
        import json
        from fbs.integrations.http import MarketplaceHttpResponse
        self.fixture()
        response=MarketplaceHttpResponse(status_code=200,headers={},content=b"",
            json_payload={"orders":[{"id":7700,"supplierStatus":"complete","wbStatus":"sold"}]})
        output=StringIO()
        with patch("fbs.management.commands.reconcile_fbs_deliveries.RequestsMarketplaceReadTransport") as transport:
            transport.return_value.send.return_value=response
            call_command("reconcile_fbs_deliveries",apply=True,order_id=[self.order.pk],stdout=output,stderr=StringIO())
        self.assertEqual(json.loads(output.getvalue())["results"][0]["action"],"reconciled")


@override_settings(FBS_MODULE_ENABLED=True,FBS_WAREHOUSE_WRITES_ENABLED=True)
class FbsDeliveryConcurrencyTests(TransactionTestCase):
    setUp = FbsDuplicateStatusReconciliationTests.setUp
    fixture = FbsDeliveryReconciliationTests.fixture

    def test_parallel_reconciliation_records_one_transition(self):
        from concurrent.futures import ThreadPoolExecutor
        from django.db import connection, connections
        if connection.vendor != "postgresql":
            self.skipTest("PostgreSQL row locking test")
        self.fixture()
        def run():
            try:
                return reconcile_delivered_order(order_id=self.order.pk,confirmation=self.confirmation)["action"]
            finally:
                connections.close_all()
        with ThreadPoolExecutor(max_workers=2) as pool:
            result=list(pool.map(lambda _:run(),range(2)))
        self.assertCountEqual(result,["reconciled","unchanged"])
        self.assertEqual(OrderAuditEntry.objects.filter(payload__source="marketplace_delivery_reconciliation").count(),1)


@override_settings(FBS_MODULE_ENABLED=True, FBS_WAREHOUSE_WRITES_ENABLED=True)
class FbsQuarantinedReplacementDeliveryTests(TestCase):
    setUp = FbsDuplicateStatusReconciliationTests.setUp
    fixture = FbsDeliveryReconciliationTests.fixture
    apply = FbsDeliveryReconciliationTests.apply
    stock = FbsDeliveryReconciliationTests.stock

    def replacement_fixture(self):
        from datetime import timedelta
        from django.contrib.auth import get_user_model
        from django.utils import timezone
        from fbs.models import (FbsPickTask, FbsWorkstation, FbsPickingCart,
            FbsToteZone, FbsControllerSession, FbsProblemToteItem,
            FbsToteMovement, FbsOrderTraceability, FbsPickException,
            FbsPickVerificationProgress, FbsHandoverBatch, FbsHandoverBox, FbsHandoverOrder)
        self.fixture(status="handed_over")
        now = timezone.now()
        actor = get_user_model().objects.create_user(username="quarantine-controller")
        batch = FbsPickBatch.objects.create(agency=self.agency, planned_qty=1)
        old_task = FbsPickTask.objects.create(batch=batch, order=self.order,
            status="exception", planned_qty=1, picked_qty=1)
        new_batch = FbsPickBatch.objects.create(agency=self.agency, planned_qty=1)
        new_task = FbsPickTask.objects.create(batch=new_batch, order=self.order,
            status="picked", planned_qty=1, picked_qty=1)
        self.allocation.pick_task=old_task
        self.allocation.picked_at=now-timedelta(hours=1)
        self.allocation.save(update_fields=["pick_task","picked_at"])
        self.replacement=FbsOrderStockAllocation.objects.create(order_item=self.item,
            balance=self.balance, pick_task=new_task, qty_reserved=1,qty_picked=1,
            status="picked",picked_at=now+timedelta(minutes=1))
        FbsOrderTraceability.objects.create(allocation=self.allocation,marking_code="OLD-KIZ",qty=1,status="picked")
        self.replacement_trace=FbsOrderTraceability.objects.create(
            allocation=self.replacement,marking_code="NEW-KIZ",qty=1,status="picked")
        self.issue=FbsPickException.objects.create(task=old_task,allocation=self.allocation,
            status="open",exception_type="other",reason="Invalid KIZ")
        ws=FbsWorkstation.objects.create(barcode="FBS-WS-000001",name="Controller")
        cart=FbsPickingCart.objects.create(barcode="FBS-CART-000001",name="Problem")
        zone=FbsToteZone.objects.create(barcode="FREE",name="Free",kind="free")
        session=FbsControllerSession.objects.create(workstation=ws,controller=actor,
            unknown_tote=cart,problem_tote=cart,free_zone=zone)
        self.problem=FbsProblemToteItem.objects.create(session=session,problem_tote=cart,
            order=self.order,order_item=self.item,scanned_value="OLD-KIZ",reason="Invalid KIZ",
            severity="critical",quantity=1,status="in_tote",reported_by=actor)
        self.movement=FbsToteMovement.objects.create(tote=cart,controller_session=session,
            action="place",target_kind="problem_tote",pick_batch=batch,quantity=1,
            performed_by=actor,details={"problem_item_id":self.problem.pk,
                "allocation_id":self.allocation.pk,"order_id":self.order.pk,
                "order_item_id":self.item.pk,"source_balance_id":self.balance.pk,
                "physically_confirmed":True,"route":"rewave","scan":"OLD-KIZ"})
        self.verification=FbsPickVerificationProgress.objects.create(allocation=self.replacement,
            qty_verified=1,completed_at=now+timedelta(minutes=2))
        handover=FbsHandoverBatch.objects.create(profile=self.profile,status="dispatched",
            dispatched_at=now+timedelta(minutes=3))
        box=FbsHandoverBox.objects.create(batch=handover,qr_code="SHIP-1")
        self.link=FbsHandoverOrder.objects.create(order=self.order,box=box,status="active")

    def test_verified_replacement_only_advances_status_and_preserves_problem(self):
        self.replacement_fixture()
        before=self.stock()
        result=self.apply()
        self.assertEqual(result["action"],"reconciled")
        self.assertEqual(result["already_debited_qty"],1)
        self.assertEqual(result["quarantined_qty"],1)
        self.assertEqual(result["allocation_ids"],[self.replacement.pk])
        self.assertEqual(result["quarantined_picks"][0]["allocation_id"],self.allocation.pk)
        self.assertEqual(self.stock(),before)
        self.problem.refresh_from_db()
        self.allocation.refresh_from_db()
        self.assertEqual(self.problem.status,"in_tote")
        self.assertEqual(self.allocation.qty_picked,1)
        self.assertEqual(self.apply()["action"],"unchanged")
        self.assertEqual(OrderAuditEntry.objects.filter(payload__source="marketplace_delivery_reconciliation").count(),1)

    def test_unconfirmed_placement_blocks(self):
        from fbs.models import FbsToteMovement
        self.replacement_fixture()
        details={**self.movement.details,"physically_confirmed":False}
        FbsToteMovement.objects.filter(pk=self.movement.pk).update(details=details)
        self.assertEqual(self.apply()["reason"],"quarantine_evidence_incomplete")

    def test_wrong_source_balance_blocks(self):
        from fbs.models import FbsToteMovement
        self.replacement_fixture()
        FbsToteMovement.objects.filter(pk=self.movement.pk).update(
            details={**self.movement.details,"source_balance_id":self.balance.pk+100})
        self.assertEqual(self.apply()["reason"],"quarantine_evidence_incomplete")

    def test_duplicate_movement_blocks(self):
        self.replacement_fixture()
        self.movement.pk=None
        self.movement.save()
        self.assertEqual(self.apply()["reason"],"quarantine_evidence_incomplete")

    def test_same_kiz_replacement_blocks(self):
        self.replacement_fixture()
        self.replacement_trace.marking_code="OLD-KIZ"
        self.replacement_trace.save(update_fields=["marking_code"])
        self.assertEqual(self.apply()["reason"],"replacement_delivery_unproven")

    def test_missing_verification_blocks(self):
        self.replacement_fixture()
        self.verification.delete()
        self.assertEqual(self.apply()["reason"],"replacement_delivery_unproven")

    def test_missing_dispatch_blocks(self):
        self.replacement_fixture()
        self.link.status="excluded"
        self.link.save(update_fields=["status"])
        self.assertEqual(self.apply()["reason"],"replacement_delivery_unproven")

    def test_problem_returned_cannot_hide_overpick(self):
        self.replacement_fixture()
        self.problem.status="returned"
        self.problem.save(update_fields=["status"])
        self.assertEqual(self.apply()["reason"],"debit_not_proven")

    def test_quarantine_without_replacement_is_not_a_shipment(self):
        self.replacement_fixture()
        self.replacement.qty_picked=0
        self.replacement.status="released"
        self.replacement.save(update_fields=["qty_picked","status"])
        self.assertEqual(self.apply()["reason"],"replacement_delivery_unproven")

    def test_partial_quarantine_cannot_hide_overpick(self):
        self.replacement_fixture()
        self.allocation.qty_reserved=2
        self.allocation.qty_picked=2
        self.allocation.save(update_fields=["qty_reserved","qty_picked"])
        self.assertEqual(self.apply()["reason"],"quarantine_evidence_incomplete")
