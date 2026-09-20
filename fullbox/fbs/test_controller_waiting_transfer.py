from django.test import TestCase, override_settings
from django.urls import reverse
from employees.models import Employee
from fbs.models import FbsToteBinding,FbsPickBatch,FbsPickingCart,FbsToteMovement
from fbs.test_waiting_tote_transfer import WaitingToteTransferTests as Fixtures
from fbs.exceptions import FbsPickingError

@override_settings(FBS_MODULE_ENABLED=True,FBS_WAREHOUSE_WRITES_ENABLED=True)
class ControllerWaitingTransferTests(TestCase):
    waiting=Fixtures.waiting
    transfer=Fixtures.transfer

    def setUp(self):
        Fixtures.setUp(self)
        Employee.objects.create(user=self.source_controller,full_name='Первый контролер',role='fbs_controller',is_active=True)
        Employee.objects.create(user=self.target_controller,full_name='Второй контролер',role='fbs_controller',is_active=True)

    def test_controller_can_transfer_unclaimed_tote(self):
        batch,binding=self.waiting()
        self.transfer(binding,performed_by=self.source_controller)
        binding.refresh_from_db();batch.refresh_from_db()
        self.assertEqual(binding.workstation_id,self.target_workstation.id)
        self.assertIsNone(batch.verification_started_at)
        self.assertEqual(FbsToteMovement.objects.get(tote=binding.tote).performed_by_id,self.source_controller.id)

    def test_any_controller_sees_and_moves_tote_from_other_desk(self):
        batch,binding=self.waiting()
        self.client.force_login(self.target_controller)
        url=reverse('fbs:controller_tote_transfers')
        response=self.client.get(url,{'q':'FBS-CART-000023'})
        self.assertContains(response,'FBS-CART-000023')
        self.assertContains(response,'Второй контролер')
        response=self.client.post(url,{'binding_id':binding.id,
            'source_workstation_id':self.source_workstation.id,'target_session_id':self.target_session.id})
        self.assertEqual(response.status_code,302)
        batch.refresh_from_db();self.assertEqual(batch.workstation_id,self.target_workstation.id)

    def test_started_tote_hidden_and_cannot_be_transferred(self):
        batch,binding=self.waiting();batch.verification_assigned_to=self.source_controller
        batch.save(update_fields=['verification_assigned_to'])
        self.client.force_login(self.target_controller)
        url=reverse('fbs:controller_tote_transfers')
        self.assertNotContains(self.client.get(url),'FBS-CART-000023')
        response=self.client.post(url,{'binding_id':binding.id,
            'source_workstation_id':self.source_workstation.id,'target_session_id':self.target_session.id})
        self.assertEqual(response.status_code,400)
        binding.refresh_from_db();self.assertEqual(binding.workstation_id,self.source_workstation.id)
        self.assertFalse(FbsToteMovement.objects.filter(tote=binding.tote).exists())

    def test_outsider_denied_by_endpoint_and_service(self):
        batch,binding=self.waiting();self.client.force_login(self.outsider)
        response=self.client.post(reverse('fbs:controller_tote_transfers'),{'binding_id':binding.id,
            'source_workstation_id':self.source_workstation.id,'target_session_id':self.target_session.id})
        self.assertIn(response.status_code,(302,403))
        with self.assertRaises(FbsPickingError):self.transfer(binding,performed_by=self.outsider)
        self.assertFalse(FbsToteMovement.objects.filter(tote=binding.tote).exists())
