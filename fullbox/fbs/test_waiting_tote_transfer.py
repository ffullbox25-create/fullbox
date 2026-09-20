from django.test import TestCase, override_settings
from django.urls import reverse
from fbs.test_tote_role_guard import FbsControllerToteTransferTests as Fixtures
from fbs.models import FbsPickBatch,FbsPickingCart,FbsToteBinding,FbsToteMovement,FbsControllerPickTote
from fbs.exceptions import FbsPickingError
from fbs.services.controller_tote_transfer import transfer_waiting_controller_tote

@override_settings(FBS_MODULE_ENABLED=True,FBS_WAREHOUSE_WRITES_ENABLED=True)
class WaitingToteTransferTests(TestCase):
    setUp = Fixtures.setUp

    def waiting(self):
        cart=FbsPickingCart.objects.create(barcode='FBS-CART-000023',name='Тележка 23')
        from django.utils import timezone
        batch=FbsPickBatch.objects.create(agency=self.agency,cart=cart,
            workstation=self.source_workstation,status='verification',planned_qty=11,picked_qty=11,
            assigned_to=self.picker,picking_completed_at=timezone.now())
        binding=FbsToteBinding.objects.create(tote=cart,pick_batch=batch,
            workstation=self.source_workstation,state='waiting_control')
        return batch,binding

    def transfer(self,binding,**kwargs):
        args=dict(binding_id=binding.id,expected_workstation_id=self.source_workstation.id,
            target_session_id=self.target_session.id,performed_by=self.storekeeper)
        args.update(kwargs)
        return transfer_waiting_controller_tote(**args)

    def test_transfer_preserves_contents_and_waits_for_scan(self):
        batch,binding=self.waiting()
        self.transfer(binding)
        batch.refresh_from_db();binding.refresh_from_db()
        self.assertEqual(binding.workstation_id,self.target_workstation.id)
        self.assertEqual(batch.workstation_id,self.target_workstation.id)
        self.assertEqual((batch.planned_qty,batch.picked_qty),(11,11))
        self.assertIsNone(batch.verification_assigned_to_id)
        self.assertIsNone(batch.verification_started_at)
        self.assertIsNone(binding.controller_session_id)
        self.assertEqual(binding.state,'waiting_control')
        self.assertFalse(FbsControllerPickTote.objects.filter(pick_batch=batch).exists())
        event=FbsToteMovement.objects.get(tote=binding.tote)
        self.assertEqual(event.details['target_session_id'],self.target_session.id)
        self.assertEqual(event.details['source_session_id'],self.source_session.id)

    def test_stale_request_cannot_transfer_twice(self):
        batch,binding=self.waiting();self.transfer(binding)
        with self.assertRaises(FbsPickingError):self.transfer(binding)
        self.assertEqual(FbsToteMovement.objects.filter(tote=binding.tote).count(),1)

    def test_started_verification_rejects_without_mutation(self):
        batch,binding=self.waiting();batch.verification_assigned_to=self.source_controller
        batch.save(update_fields=['verification_assigned_to'])
        with self.assertRaises(FbsPickingError):self.transfer(binding)
        binding.refresh_from_db();self.assertEqual(binding.workstation_id,self.source_workstation.id)
        self.assertFalse(FbsToteMovement.objects.filter(tote=binding.tote).exists())

    def test_outsider_and_closed_target_rejected(self):
        batch,binding=self.waiting()
        with self.assertRaises(FbsPickingError):self.transfer(binding,performed_by=self.outsider)
        self.target_session.status='closed';self.target_session.save(update_fields=['status'])
        with self.assertRaises(FbsPickingError):self.transfer(binding)
        self.assertFalse(FbsToteMovement.objects.filter(tote=binding.tote).exists())

    def test_full_target_rejects(self):
        batch,binding=self.waiting()
        self.target_workstation.max_parallel_waves=1;self.target_workstation.save()
        from django.utils import timezone
        FbsPickBatch.objects.create(agency=self.agency,workstation=self.target_workstation,
            cart=self.target_service_tote,status='verification',picking_completed_at=timezone.now())
        with self.assertRaises(FbsPickingError):self.transfer(binding)
        batch.refresh_from_db();self.assertEqual(batch.workstation_id,self.source_workstation.id)

    def test_waiting_tote_visible_and_transferable_in_active_filter(self):
        batch,binding=self.waiting();self.client.force_login(self.storekeeper)
        response=self.client.get(reverse('fbs:tsd_controller_settings'),{'status':'active','q':'FBS-CART-000023'})
        self.assertContains(response,'FBS-CART-000023')
        self.assertContains(response,'transfer_waiting_controller_tote')
        response=self.client.post(reverse('fbs:tsd_controller_settings'),{
            'action':'transfer_waiting_controller_tote','binding_id':binding.id,
            'source_workstation_id':self.source_workstation.id,'target_session_id':self.target_session.id})
        self.assertEqual(response.status_code,302)
        binding.refresh_from_db();self.assertEqual(binding.workstation_id,self.target_workstation.id)
        response=self.client.get(reverse('fbs:tsd_controller_settings'))
        self.assertEqual(response.status_code,200)
        self.assertContains(response,'controller_tote_transfer_target')
