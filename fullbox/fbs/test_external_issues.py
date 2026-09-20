import uuid
import os
from pathlib import Path
from datetime import timedelta
from unittest.mock import patch
from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from employees.models import Employee
from fbs import test_inventory as fixtures
from fbs.exceptions import FbsError
from fbs.models import FbsBox, FbsPallet, FbsExternalIssue, FbsExternalIssueEvent, FbsStockBalance, FbsInventorySession
from fbs.services.external_issues import create_issue, issue_command


@override_settings(FBS_MODULE_ENABLED=True, FBS_WAREHOUSE_WRITES_ENABLED=True, FBS_STOCK_PUSH_ENABLED=False)
class ExternalIssueTests(TestCase):
    def setUp(self):
        fixtures.FbsInventoryScanModeTests.setUp(self)
        self.user = get_user_model().objects.create_user(username="issue-keeper")
        self.employee = Employee.objects.create(user=self.user, full_name="Кладовщик выдачи", role="storekeeper", is_active=True)
        self.pallet.status = FbsPallet.STATUS_ACTIVE
        self.pallet.save(update_fields=["status"])
        self.box.status = FbsBox.STATUS_ACTIVE
        self.box.save(update_fields=["status"])
        self.balance = FbsStockBalance.objects.create(agency=self.agency, box=self.box, sku_ref=self.sku,
            identity_key="plain", sku_code=self.sku.sku_code, name=self.sku.name, barcode="4600000000001",
            qty=100, available_qty=70, reserved_qty=30)
        self.client.force_login(self.user)

    def create(self, qty=70, **changes):
        args = dict(user=self.user, agency_id=self.agency.id, reference=str(uuid.uuid4()), recipient="Представитель клиента",
            purpose="owner", basis="Заявление клиента", selections=[{"balance_id":self.balance.pk,"qty":qty}], request_key=str(uuid.uuid4()))
        args.update(changes)
        return create_issue(**args)

    def command(self, doc, action, **changes):
        args = dict(user=self.user, issue_id=doc.pk, action=action, request_key=str(uuid.uuid4()),
            line_id=doc.lines.first().pk, cell_scan=self.cell.cell_code, box_scan=self.box.box_code,
            item_scan=self.balance.barcode, confirmed=True,
            expected_qty=sum(line.in_hand_qty for line in doc.lines.all()) if action == "complete" else None)
        args.update(changes)
        return issue_command(**args)

    def assertStock(self, qty, free, market=30, external=0):
        self.balance.refresh_from_db()
        self.assertEqual((self.balance.qty,self.balance.available_qty,self.balance.reserved_qty,self.balance.external_reserved_qty),
                         (qty,free,market,external))

    def test_100_with_30_reserved_allows_only_70(self):
        self.create()
        self.assertStock(100,0,external=70)

    def test_71_rejected_atomically(self):
        with self.assertRaisesMessage(FbsError,"свободно 70"):
            self.create(71)
        self.assertStock(100,70)
        self.assertFalse(FbsExternalIssue.objects.exists())

    def test_marketplace_can_reserve_remaining_free_units_not_external_hold(self):
        from fbs.models import FbsIntegrationProfile, FbsOrder, FbsOrderItem
        from fbs.services.picking import reserve_order_stock
        from sku.models import SKUBarcode
        SKUBarcode.objects.create(sku=self.sku,value=self.balance.barcode,is_primary=True)
        self.create(60)
        profile=FbsIntegrationProfile.objects.create(agency=self.agency,marketplace="wb",external_warehouse_id="issue-test")
        order=FbsOrder.objects.create(profile=profile,external_order_id="external-coexist")
        FbsOrderItem.objects.create(order=order,external_line_id="1",external_sku=self.sku.sku_code,
            sku=self.sku,product_name=self.sku.name,barcode=self.balance.barcode,quantity=10)
        result=reserve_order_stock(order_id=order.id,reserved_by=self.user)
        self.assertTrue(result.reserved, result)
        self.assertStock(100,0,market=40,external=60)
        # Even a stale counter must not hide the underlying marketplace allocation.
        FbsStockBalance.objects.filter(pk=self.balance.pk).update(reserved_qty=0,available_qty=40)
        with self.assertRaisesMessage(FbsError,"Резервы заказов"):
            self.create(1)

    def test_stale_handover_total_rejected(self):
        doc=self.create(2); self.command(doc,"pick"); self.command(doc,"pick")
        with self.assertRaisesMessage(FbsError,"количество изменилось"):
            self.command(doc,"complete",expected_qty=1)
        self.assertStock(98,68)

    def test_second_request_cannot_consume_first_hold(self):
        self.create(65)
        with self.assertRaises(FbsError): self.create(6)
        self.assertStock(100,5,external=65)

    def test_invalid_quantities(self):
        for value in (0,-1,True,"1.5",None,"",1.5):
            with self.subTest(value=value), self.assertRaises(FbsError): self.create(value)
        self.assertStock(100,70)

    def test_multirow_failure_rolls_back_all(self):
        with self.assertRaises(FbsError):
            self.create(selections=[{"balance_id":self.balance.pk,"qty":1},{"balance_id":999999,"qty":1}])
        self.assertStock(100,70)

    def test_other_client_rejected(self):
        from sku.models import Agency
        other = Agency.objects.create(agn_name="Другой клиент")
        with self.assertRaises(FbsError): self.create(1,agency_id=other.pk)

    def test_duplicate_document_key_is_idempotent(self):
        key,ref=str(uuid.uuid4()),str(uuid.uuid4())
        doc=self.create(3,request_key=key,reference=ref)
        self.assertEqual(doc.pk,self.create(3,request_key=key,reference=ref).pk)
        self.assertStock(100,67,external=3)
        self.assertEqual(FbsExternalIssue.objects.count(),1)

    def test_reused_key_with_changed_quantity_rejected(self):
        key,ref=str(uuid.uuid4()),str(uuid.uuid4())
        self.create(3,request_key=key,reference=ref)
        with self.assertRaises(FbsError): self.create(4,request_key=key,reference=ref)

    def test_duplicate_reference_rejected(self):
        self.create(3,reference="ACT-1")
        with self.assertRaises(FbsError): self.create(3,reference="ACT-1")

    def test_pick_then_complete_does_not_subtract_twice(self):
        doc=self.create(2)
        self.command(doc,"pick")
        self.assertStock(99,68,external=1)
        self.command(doc,"complete")
        self.assertStock(99,69)
        doc.refresh_from_db()
        self.assertEqual(doc.status,"completed")
        self.assertEqual(doc.lines.get().shipped_qty,1)

    def test_duplicate_scan_not_repeated(self):
        doc=self.create(2); key=str(uuid.uuid4())
        self.command(doc,"pick",request_key=key)
        self.command(doc,"pick",request_key=key)
        self.assertStock(99,68,external=1)

    def test_complete_replay_is_idempotent_and_new_request_rejected(self):
        doc=self.create(1); self.command(doc,"pick"); key=str(uuid.uuid4())
        self.command(doc,"complete",request_key=key,expected_qty=1)
        self.command(doc,"complete",request_key=key,expected_qty=1)
        with self.assertRaises(FbsError): self.command(doc,"complete")
        self.assertStock(99,69)

    def test_wrong_scans_rejected(self):
        doc=self.create(1)
        for field in ("cell_scan","box_scan","item_scan"):
            with self.subTest(field=field), self.assertRaises(FbsError): self.command(doc,"pick",**{field:"WRONG"})
        self.assertStock(100,69,external=1)

    def test_other_document_line_rejected(self):
        one=self.create(1); two=self.create(1)
        with self.assertRaises(FbsError): self.command(one,"pick",line_id=two.lines.get().pk)

    def test_cancel_unpicked_releases_only_external_reserve(self):
        doc=self.create(70); self.command(doc,"cancel")
        self.assertStock(100,70)

    def test_cancel_after_pick_requires_physical_return(self):
        doc=self.create(2); self.command(doc,"pick")
        with self.assertRaises(FbsError): self.command(doc,"cancel")
        self.command(doc,"return"); self.command(doc,"cancel")
        self.assertStock(100,70)

    def test_return_cannot_be_repeated(self):
        doc=self.create(1); self.command(doc,"pick"); self.command(doc,"return")
        with self.assertRaises(FbsError): self.command(doc,"return")
        self.assertStock(100,70)

    def test_complete_without_pick_or_confirmation_rejected(self):
        doc=self.create(1)
        with self.assertRaises(FbsError): self.command(doc,"complete")
        self.command(doc,"pick")
        with self.assertRaises(FbsError): self.command(doc,"complete",confirmed=False)

    def test_corrupted_external_hold_rejected(self):
        doc=self.create(1)
        FbsStockBalance.objects.filter(pk=self.balance.pk).update(external_reserved_qty=0)
        with self.assertRaises(FbsError): self.command(doc,"pick")

    def test_db_constraint_rejects_overcommit(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            FbsStockBalance.objects.filter(pk=self.balance.pk).update(external_reserved_qty=1)

    def test_stock_flag_and_role_enforced_in_command(self):
        with self.settings(FBS_WAREHOUSE_WRITES_ENABLED=False), self.assertRaises(FbsError): self.create(1)
        self.employee.role="picker";self.employee.save(update_fields=["role"])
        with self.assertRaises(FbsError): self.create(1)

    def test_immediate_inventory_rejected_with_open_issue(self):
        from fbs.services.inventory import create_inventory_session
        self.create(1)
        with self.assertRaises(FbsError):
            create_inventory_session(scope_type="box", mode="immediate",box=self.box,created_by=self.user)

    def test_drain_inventory_waits_for_external_issue(self):
        from fbs.services.inventory import create_inventory_session, _active_scope_operations
        doc=self.create(1)
        session=create_inventory_session(scope_type="box", mode="drain",box=self.box,created_by=self.user)
        self.assertTrue(_active_scope_operations(session))
        self.command(doc,"cancel")
        self.assertFalse(_active_scope_operations(session))

    def test_rack_merge_rejected_with_open_issue(self):
        from fbs.services.racks import _assert_source_free
        doc=self.create(1);self.command(doc,"pick")
        with self.assertRaises(FbsError): _assert_source_free(self.box,[self.balance])

    def test_marked_requires_exact_code(self):
        mark=fixtures.FbsInventoryScanModeTests._kiz("0460000000000","SERIAL1234567")
        self.balance.qty=1;self.balance.available_qty=1;self.balance.reserved_qty=0;self.balance.marking_code=mark
        self.balance.save()
        doc=self.create(1)
        with self.assertRaises((FbsError,ValueError)): self.command(doc,"pick")
        self.command(doc,"pick",item_scan=mark)
        self.command(doc,"complete")
        self.assertStock(0,0,market=0)

    def test_historical_requires_supervisor_and_explicit_confirmation(self):
        occurred=timezone.now()-timedelta(days=1)
        with self.assertRaises(FbsError): self.create(2,historical=True,occurred_at=occurred,historical_confirmed=True)
        self.employee.role="head_manager";self.employee.save(update_fields=["role"])
        with self.assertRaises(FbsError): self.create(2,historical=True,occurred_at=occurred)
        doc=self.create(2,historical=True,occurred_at=occurred,historical_confirmed=True)
        self.assertStock(98,68)
        self.assertEqual(doc.status,"completed")
        self.assertEqual(doc.events.get().action,"historical")

    def test_document_and_quantity_audit(self):
        doc=self.create(1);self.command(doc,"pick");self.command(doc,"complete")
        self.assertEqual(list(doc.events.values_list("action",flat=True)),["created","pick","complete"])
        self.assertEqual(doc.events.last().payload["shipped_qty"],1)
        self.assertEqual(doc.events.last().performed_by_id,self.user.pk)

    def test_views_render_and_post_create(self):
        response=self.client.get(reverse("fbs:external_issues"),{"client":self.agency.pk})
        self.assertContains(response,"Выдача из ФБС")
        self.save_ui_artifact("issue-list.html", response)
        response=self.client.post(reverse("fbs:external_issues"),dict(client=self.agency.pk,
            reference="UI-1",recipient="Клиент",purpose="owner",basis="Заявление",request_key=str(uuid.uuid4()),
            **{f"qty_{self.balance.pk}":"2"}))
        self.assertEqual(response.status_code,302,response.content)
        response=self.client.get(response.url)
        self.assertContains(response,"Отобрать 1 шт.")
        self.assertContains(response,"Резервы WB/Ozon останутся без изменений")
        self.save_ui_artifact("issue-detail.html", response)
        doc=FbsExternalIssue.objects.get(reference="UI-1")
        self.command(doc,"pick")
        self.save_ui_artifact("issue-picked.html",self.client.get(reverse("fbs:external_issue_detail",args=[doc.pk])))

    @staticmethod
    def save_ui_artifact(name, response):
        folder=os.environ.get("FBS_ISSUE_TEST_ARTIFACT_DIR")
        if folder:
            Path(folder,name).write_bytes(response.content)

    def test_stock_page_shows_separate_external_reserve(self):
        self.create(2)
        response=self.client.get(reverse("fbs:tsd_storekeeper_stock"))
        self.assertContains(response,"Резерв выдачи")

    def test_anonymous_and_picker_cannot_use_views(self):
        self.client.logout()
        self.assertEqual(self.client.get(reverse("fbs:external_issues")).status_code,302)
        self.client.force_login(self.user)
        self.employee.role="picker";self.employee.save(update_fields=["role"])
        self.assertEqual(self.client.get(reverse("fbs:external_issues")).status_code,403)

    def test_create_queues_stock_export_after_commit(self):
        with patch("fbs.services.stock_sync.queue_agency_stock_exports") as queue:
            with self.captureOnCommitCallbacks(execute=True): self.create(1)
            queue.assert_called_once_with(agency_id=self.agency.id)
