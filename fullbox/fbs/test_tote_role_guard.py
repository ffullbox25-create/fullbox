from datetime import timedelta
from types import SimpleNamespace
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from employees.models import Employee
from fbs import controller_views, tsd_views
from fbs.exceptions import FbsPickingError
from fbs.models import (
    FbsControllerCheckTote,
    FbsControllerPickTote,
    FbsControllerSession,
    FbsHandoverBatch,
    FbsIntegrationProfile,
    FbsOrder,
    FbsPickBatch,
    FbsPickScanEvent,
    FbsPickTask,
    FbsProblemToteItem,
    FbsPickingCart,
    FbsToteBinding,
    FbsToteMovement,
    FbsToteZone,
    FbsWorkstation,
)
from fbs.services.totes import (
    SERVICE_TOTE_CANCELED,
    SERVICE_TOTE_PROBLEM,
    add_controller_check_tote,
    attach_pick_tote_to_available_check_tote,
    bind_controller_service_totes,
    bind_pick_tote_to_picker,
    close_controller_session,
    controller_service_tote_for_actor,
    record_extra_problem_tote_item,
    start_controller_session,
    transfer_controller_pick_tote,
)
from fbs.services.controller_shift import start_controller_shift
from fbs.services.picking import (
    _assert_task_actor,
    claim_next_pick_batch,
    claim_pick_batch_verification,
)
from sku.models import Agency


urlpatterns = []


@override_settings(
    FBS_MODULE_ENABLED=True,
    FBS_WAREHOUSE_WRITES_ENABLED=True,
    ROOT_URLCONF="fbs.test_tote_role_guard",
)
class FbsToteRoleGuardTests(TestCase):
    def setUp(self):
        users = get_user_model()
        self.controller = users.objects.create_user(username="tote_role_controller")
        self.picker = users.objects.create_user(username="tote_role_picker")
        self.agency = Agency.objects.create(agn_name="Tote role client")
        self.workstation = FbsWorkstation.objects.create(
            barcode="FBS-WS-TOTE-ROLE",
            name="Tote role workstation",
            printer_name="TEST-PRINTER",
        )
        self.tote = FbsPickingCart.objects.create(
            barcode="FBS-CART-TOTE-ROLE",
            name="Tote role cart",
        )
        self.first_check_tote = FbsPickingCart.objects.create(
            barcode="FBS-CART-TOTE-CHECK-FIRST",
            name="First check tote",
        )
        self.profile = FbsIntegrationProfile.objects.create(
            agency=self.agency,
            marketplace=FbsIntegrationProfile.MARKETPLACE_OZON,
            name="Tote role profile",
            external_warehouse_id="tote-role-warehouse",
        )

    def _unfinished_picker_wave(self):
        finished_order = FbsOrder.objects.create(
            profile=self.profile,
            external_order_id="TOTE-TAKEOVER-FINISHED",
        )
        open_order = FbsOrder.objects.create(
            profile=self.profile,
            external_order_id="TOTE-TAKEOVER-OPEN",
        )
        batch = FbsPickBatch.objects.create(
            agency=self.agency,
            status=FbsPickBatch.STATUS_IN_PROGRESS,
            planned_qty=2,
            picked_qty=1,
            assigned_to=self.picker,
            cart=self.tote,
        )
        finished_task = FbsPickTask.objects.create(
            batch=batch,
            order=finished_order,
            status=FbsPickTask.STATUS_PICKED,
            assigned_to=self.picker,
            planned_qty=1,
            picked_qty=1,
            sort_order=1,
        )
        open_task = FbsPickTask.objects.create(
            batch=batch,
            order=open_order,
            status=FbsPickTask.STATUS_IN_PROGRESS,
            assigned_to=self.picker,
            planned_qty=1,
            picked_qty=0,
            sort_order=2,
        )
        bind_pick_tote_to_picker(batch_id=batch.id, performed_by=self.picker)
        return batch, finished_task, open_task

    def test_picker_takes_over_unfinished_wave_by_scanning_its_tote(self):
        replacement = get_user_model().objects.create_user(
            username="tote_role_replacement_picker"
        )
        batch, finished_task, open_task = self._unfinished_picker_wave()

        claimed = claim_next_pick_batch(
            assigned_to=replacement,
            workstation_scan="",
            cart_scan=self.tote.barcode,
        )

        batch.refresh_from_db()
        finished_task.refresh_from_db()
        open_task.refresh_from_db()
        binding = FbsToteBinding.objects.get(tote=self.tote)
        self.assertEqual(claimed.id, batch.id)
        self.assertEqual(batch.assigned_to_id, replacement.id)
        self.assertEqual((batch.planned_qty, batch.picked_qty), (2, 1))
        self.assertEqual(finished_task.assigned_to_id, self.picker.id)
        self.assertEqual(open_task.assigned_to_id, replacement.id)
        self.assertEqual(
            (open_task.status, open_task.picked_qty),
            (FbsPickTask.STATUS_IN_PROGRESS, 0),
        )
        self.assertEqual(binding.employee_id, replacement.id)
        self.assertEqual(binding.pick_batch_id, batch.id)
        self.assertTrue(
            FbsPickScanEvent.objects.filter(
                batch=batch,
                stage=FbsPickScanEvent.STAGE_CART,
                result=FbsPickScanEvent.RESULT_SUCCESS,
                created_by=replacement,
                message__contains="Незавершённая волна передана",
            ).exists()
        )
        with self.assertRaisesMessage(FbsPickingError, "другому сборщику"):
            _assert_task_actor(open_task, self.picker)

    def test_picker_with_active_wave_cannot_take_over_another_tote(self):
        replacement = get_user_model().objects.create_user(
            username="tote_role_busy_replacement_picker"
        )
        batch, _finished_task, open_task = self._unfinished_picker_wave()
        other_tote = FbsPickingCart.objects.create(
            barcode="FBS-CART-TOTE-ROLE-BUSY",
            name="Busy replacement cart",
        )
        FbsPickBatch.objects.create(
            agency=self.agency,
            status=FbsPickBatch.STATUS_IN_PROGRESS,
            planned_qty=1,
            picked_qty=0,
            assigned_to=replacement,
            cart=other_tote,
        )

        with self.assertRaisesMessage(FbsPickingError, "уже привязана к таре"):
            claim_next_pick_batch(
                assigned_to=replacement,
                workstation_scan="",
                cart_scan=self.tote.barcode,
            )

        batch.refresh_from_db()
        open_task.refresh_from_db()
        self.assertEqual(batch.assigned_to_id, self.picker.id)
        self.assertEqual(open_task.assigned_to_id, self.picker.id)

    def test_tote_cannot_be_rebound_without_wave_assignment(self):
        outsider = get_user_model().objects.create_user(
            username="tote_role_unassigned_picker"
        )
        batch, _finished_task, _open_task = self._unfinished_picker_wave()

        with self.assertRaisesMessage(FbsPickingError, "только к сборщику этой волны"):
            bind_pick_tote_to_picker(
                batch_id=batch.id,
                performed_by=outsider,
            )

        binding = FbsToteBinding.objects.get(tote=self.tote)
        self.assertEqual(binding.employee_id, self.picker.id)
        self.assertEqual(binding.pick_batch_id, batch.id)

    def test_free_tote_still_claims_oldest_queued_wave(self):
        order = FbsOrder.objects.create(
            profile=self.profile,
            external_order_id="TOTE-NORMAL-CLAIM",
        )
        batch = FbsPickBatch.objects.create(
            agency=self.agency,
            status=FbsPickBatch.STATUS_QUEUED,
            planned_qty=1,
        )
        task = FbsPickTask.objects.create(
            batch=batch,
            order=order,
            status=FbsPickTask.STATUS_QUEUED,
            planned_qty=1,
        )

        claimed = claim_next_pick_batch(
            assigned_to=self.picker,
            workstation_scan="",
            cart_scan=self.tote.barcode,
        )

        batch.refresh_from_db()
        task.refresh_from_db()
        binding = FbsToteBinding.objects.get(tote=self.tote)
        self.assertEqual(claimed.id, batch.id)
        self.assertEqual(batch.status, FbsPickBatch.STATUS_IN_PROGRESS)
        self.assertEqual(batch.assigned_to_id, self.picker.id)
        self.assertEqual(task.status, FbsPickTask.STATUS_IN_PROGRESS)
        self.assertEqual(task.assigned_to_id, self.picker.id)
        self.assertEqual(binding.employee_id, self.picker.id)

    def test_controller_session_starts_without_physical_check_tote(self):
        session = start_controller_session(
            workstation_id=self.workstation.id,
            controller=self.controller,
            unknown_tote_scan=self.tote.barcode,
        )

        self.assertEqual(session.check_totes.count(), 0)

    def test_controller_can_add_four_physical_check_totes(self):
        session = start_controller_session(
            workstation_id=self.workstation.id,
            controller=self.controller,
            unknown_tote_scan=self.tote.barcode,
        )
        for index in range(1, 5):
            check_tote = FbsPickingCart.objects.create(
                barcode=f"FBS-CART-TOTE-PHYSICAL-{index}",
                name=f"Physical check tote {index}",
            )
            add_controller_check_tote(
                session_id=session.id,
                tote_scan=check_tote.barcode,
                performed_by=self.controller,
            )

        self.assertEqual(session.check_totes.count(), 4)

    def test_controller_can_hold_three_unfinished_waves_but_not_four(self):
        batches = [
            FbsPickBatch.objects.create(
                agency=self.agency,
                status=FbsPickBatch.STATUS_VERIFICATION,
                picking_completed_at=timezone.now(),
            )
            for _index in range(4)
        ]

        for batch in batches[:3]:
            claimed = claim_pick_batch_verification(
                batch_id=batch.id,
                assigned_to=self.controller,
            )
            self.assertEqual(claimed.verification_assigned_to_id, self.controller.id)

        with self.assertRaisesMessage(
            FbsPickingError,
            "У контроллера уже открыты три проверки волн. "
            "Завершите одну из них, чтобы принять следующую тару.",
        ):
            claim_pick_batch_verification(
                batch_id=batches[3].id,
                assigned_to=self.controller,
            )

    def test_new_operator_takes_over_desk_session_without_moving_desk_work(self):
        second_controller = get_user_model().objects.create_user(
            username="tote_role_controller_second"
        )
        session = start_controller_session(
            workstation_id=self.workstation.id,
            controller=self.controller,
            unknown_tote_scan=self.tote.barcode,
            first_check_tote_scan=self.first_check_tote.barcode,
        )
        start_controller_shift(
            workstation_id=self.workstation.id,
            controller=self.controller,
        )

        selected = start_controller_shift(
            workstation_id=self.workstation.id,
            controller=second_controller,
        )

        session.refresh_from_db()
        self.workstation.refresh_from_db()
        self.assertEqual(selected.id, self.workstation.id)
        self.assertEqual(self.workstation.shift_controller_id, second_controller.id)
        self.assertEqual(session.controller_id, second_controller.id)
        self.assertEqual(session.workstation_id, self.workstation.id)
        self.assertEqual(session.unknown_tote_id, self.tote.id)
        self.assertEqual(session.check_totes.count(), 0)

    def test_new_operator_takes_over_unfinished_verification_at_desk(self):
        second_controller = get_user_model().objects.create_user(
            username="tote_role_controller_wave_takeover"
        )
        session = start_controller_session(
            workstation_id=self.workstation.id,
            controller=self.controller,
            unknown_tote_scan=self.tote.barcode,
        )
        check_tote = FbsControllerCheckTote.objects.create(
            session=session,
            agency=self.agency,
            profile=self.profile,
            status=FbsControllerCheckTote.STATUS_OPEN,
            item_qty=1,
            opened_by=self.controller,
        )
        pick_tote = FbsPickingCart.objects.create(
            barcode="FBS-CART-TOTE-TAKEOVER",
            name="Takeover pick tote",
        )
        batch = FbsPickBatch.objects.create(
            agency=self.agency,
            status=FbsPickBatch.STATUS_VERIFICATION,
            planned_qty=1,
            picked_qty=1,
            assigned_to=self.picker,
            workstation=self.workstation,
            cart=pick_tote,
            verification_assigned_to=self.controller,
            picking_completed_at=timezone.now(),
            verification_started_at=timezone.now(),
        )
        FbsControllerPickTote.objects.create(
            session=session,
            check_tote=check_tote,
            pick_batch=batch,
            tote=pick_tote,
            planned_qty=1,
        )
        start_controller_shift(
            workstation_id=self.workstation.id,
            controller=self.controller,
        )

        start_controller_shift(
            workstation_id=self.workstation.id,
            controller=second_controller,
        )

        session.refresh_from_db()
        batch.refresh_from_db()
        self.assertEqual(session.controller_id, second_controller.id)
        self.assertEqual(batch.verification_assigned_to_id, second_controller.id)

    def test_switching_operator_releases_only_previous_desk_operator_binding(self):
        second_workstation = FbsWorkstation.objects.create(
            barcode="FBS-WS-TOTE-ROLE-SECOND",
            name="Second tote role workstation",
            printer_name="SECOND-PRINTER",
        )
        start_controller_shift(
            workstation_id=self.workstation.id,
            controller=self.controller,
        )

        start_controller_shift(
            workstation_id=second_workstation.id,
            controller=self.controller,
        )

        self.workstation.refresh_from_db()
        second_workstation.refresh_from_db()
        self.assertEqual(self.workstation.shift_status, FbsWorkstation.SHIFT_CLOSED)
        self.assertIsNone(self.workstation.shift_controller_id)
        self.assertEqual(self.workstation.printer_name, "TEST-PRINTER")
        self.assertEqual(second_workstation.shift_controller_id, self.controller.id)
        self.assertEqual(second_workstation.printer_name, "SECOND-PRINTER")

    def test_fourth_check_tote_is_created_when_accepting_new_pick_tote(self):
        session = start_controller_session(
            workstation_id=self.workstation.id,
            controller=self.controller,
            unknown_tote_scan=self.tote.barcode,
            first_check_tote_scan=self.first_check_tote.barcode,
        )
        for index in (1, 2, 3):
            check_tote = FbsPickingCart.objects.create(
                barcode=f"FBS-CART-TOTE-CHECK-{index}",
                name=f"Check tote {index}",
            )
            add_controller_check_tote(
                session_id=session.id,
                tote_scan=check_tote.barcode,
                performed_by=self.controller,
            )
        FbsControllerCheckTote.objects.filter(session=session).update(
            status=FbsControllerCheckTote.STATUS_COMPOSITION
        )
        pick_tote = FbsPickingCart.objects.create(
            barcode="FBS-CART-TOTE-PICK-FOURTH",
            name="Fourth flow pick tote",
        )
        expected_result = SimpleNamespace(id=401)

        with mock.patch(
            "fbs.services.totes._picked_batch_for_control",
            return_value=SimpleNamespace(id=101),
        ), mock.patch(
            "fbs.services.totes._batch_profile",
            return_value=self.profile,
        ), mock.patch(
            "fbs.services.totes._attach_pick_tote_context",
            return_value=expected_result,
        ) as attach_context:
            result = attach_pick_tote_to_available_check_tote(
                session_id=session.id,
                pick_tote_scan=pick_tote.barcode,
                performed_by=self.controller,
            )

        self.assertEqual(result.id, expected_result.id)
        self.assertEqual(
            FbsControllerCheckTote.objects.filter(session=session).count(),
            4,
        )
        created_flow = FbsControllerCheckTote.objects.filter(session=session).latest("id")
        self.assertIsNone(created_flow.tote_id)
        self.assertEqual(created_flow.profile_id, self.profile.id)
        self.assertEqual(
            attach_context.call_args.kwargs["check_tote"].id,
            created_flow.id,
        )

    def test_legacy_session_with_four_check_totes_uses_existing_tote(self):
        free_zone = FbsToteZone.objects.create(
            barcode="FBS-TOTE-ZONE-LEGACY-FOUR",
            name="Legacy four free zone",
            kind=FbsToteZone.KIND_FREE,
        )
        session = FbsControllerSession.objects.create(
            workstation=self.workstation,
            controller=self.controller,
            unknown_tote=self.tote,
            free_zone=free_zone,
        )
        check_totes = []
        for index in range(1, 5):
            check_totes.append(
                FbsControllerCheckTote.objects.create(
                    session=session,
                    tote=FbsPickingCart.objects.create(
                        barcode=f"FBS-CART-TOTE-LEGACY-CHECK-{index}",
                        name=f"Legacy check tote {index}",
                    ),
                    opened_by=self.controller,
                )
            )
        pick_tote = FbsPickingCart.objects.create(
            barcode="FBS-CART-TOTE-LEGACY-PICK",
            name="Legacy flow pick tote",
        )
        expected_result = SimpleNamespace(id=501)

        with mock.patch(
            "fbs.services.totes._picked_batch_for_control",
            return_value=SimpleNamespace(id=102),
        ), mock.patch(
            "fbs.services.totes._batch_profile",
            return_value=self.profile,
        ), mock.patch(
            "fbs.services.totes._attach_pick_tote_context",
            return_value=expected_result,
        ) as attach_context:
            result = attach_pick_tote_to_available_check_tote(
                session_id=session.id,
                pick_tote_scan=pick_tote.barcode,
                performed_by=self.controller,
            )

        self.assertEqual(result.id, expected_result.id)
        self.assertEqual(
            attach_context.call_args.kwargs["check_tote"].id,
            check_totes[0].id,
        )
        self.assertEqual(
            FbsControllerCheckTote.objects.filter(session=session).count(),
            4,
        )

    def test_closed_marketplace_flow_is_not_reused_for_new_pick_tote(self):
        session = start_controller_session(
            workstation_id=self.workstation.id,
            controller=self.controller,
            unknown_tote_scan=self.tote.barcode,
            first_check_tote_scan=self.first_check_tote.barcode,
        )
        old_check_tote = FbsControllerCheckTote.objects.create(
            session=session,
            tote=None,
            agency=self.agency,
            profile=self.profile,
            opened_by=self.controller,
        )
        closed_batch = FbsHandoverBatch.objects.create(
            profile=self.profile,
            external_supply_id="CLOSED-SUPPLY",
            status=FbsHandoverBatch.STATUS_OPEN,
            marketplace_state=FbsHandoverBatch.MARKETPLACE_COMPLETE,
        )
        old_check_tote.handover_batch = closed_batch
        old_check_tote.save(
            update_fields=["handover_batch", "updated_at"]
        )
        pick_tote = FbsPickingCart.objects.create(
            barcode="FBS-CART-TOTE-NEW-FLOW",
            name="New flow pick tote",
        )
        expected_result = SimpleNamespace(id=601)

        with mock.patch(
            "fbs.services.totes._picked_batch_for_control",
            return_value=SimpleNamespace(id=103),
        ), mock.patch(
            "fbs.services.totes._batch_profile",
            return_value=self.profile,
        ), mock.patch(
            "fbs.services.totes._attach_pick_tote_context",
            return_value=expected_result,
        ) as attach_context:
            result = attach_pick_tote_to_available_check_tote(
                session_id=session.id,
                pick_tote_scan=pick_tote.barcode,
                performed_by=self.controller,
            )

        self.assertEqual(result.id, expected_result.id)
        selected = attach_context.call_args.kwargs["check_tote"]
        self.assertNotEqual(selected.id, old_check_tote.id)
        self.assertIsNone(selected.handover_batch_id)

    def test_active_wave_tote_cannot_become_unknown_service_tote(self):
        batch = FbsPickBatch.objects.create(
            agency=self.agency,
            status=FbsPickBatch.STATUS_VERIFICATION,
            planned_qty=1,
            picked_qty=1,
            cart=self.tote,
            workstation=self.workstation,
            picking_completed_at=timezone.now(),
        )

        with self.assertRaisesMessage(FbsPickingError, f"волне #{batch.id}"):
            start_controller_session(
                workstation_id=self.workstation.id,
                controller=self.controller,
                unknown_tote_scan=self.tote.barcode,
                first_check_tote_scan=self.first_check_tote.barcode,
            )

    def test_unknown_service_tote_cannot_become_pick_tote(self):
        session = start_controller_session(
            workstation_id=self.workstation.id,
            controller=self.controller,
            unknown_tote_scan=self.tote.barcode,
            first_check_tote_scan=self.first_check_tote.barcode,
        )
        batch = FbsPickBatch.objects.create(
            agency=self.agency,
            status=FbsPickBatch.STATUS_IN_PROGRESS,
            planned_qty=1,
            picked_qty=0,
            cart=self.tote,
            assigned_to=self.picker,
        )

        with self.assertRaisesMessage(FbsPickingError, "служебная тара"):
            bind_pick_tote_to_picker(
                batch_id=batch.id,
                performed_by=self.picker,
            )

        session.refresh_from_db()
        self.assertEqual(session.unknown_tote_id, self.tote.id)

    def test_shift_reserves_one_shared_service_tote_and_ignores_legacy_scans(self):
        problem_tote = FbsPickingCart.objects.create(
            barcode="FBS-CART-TOTE-PROBLEM",
            name="Problem tote",
        )
        canceled_tote = FbsPickingCart.objects.create(
            barcode="FBS-CART-TOTE-CANCELED",
            name="Canceled tote",
        )

        session = start_controller_session(
            workstation_id=self.workstation.id,
            controller=self.controller,
            unknown_tote_scan=self.tote.barcode,
            problem_tote_scan=problem_tote.barcode,
            canceled_tote_scan=canceled_tote.barcode,
            first_check_tote_scan=self.first_check_tote.barcode,
        )

        self.assertEqual(session.unknown_tote_id, self.tote.id)
        self.assertEqual(session.problem_tote_id, self.tote.id)
        self.assertEqual(session.canceled_tote_id, self.tote.id)
        binding = FbsToteBinding.objects.get(tote=self.tote)
        self.assertEqual(binding.state, FbsToteBinding.STATE_UNKNOWN)
        self.assertTrue(
            tsd_views._controller_service_binding_ready(
                session=session,
                binding=binding,
                tote_id=session.problem_tote_id,
            )
        )
        self.assertEqual(binding.workstation_id, self.workstation.id)
        self.assertFalse(
            FbsToteBinding.objects.filter(
                tote_id__in=(problem_tote.id, canceled_tote.id)
            ).exists()
        )
        self.assertEqual(
            FbsToteMovement.objects.filter(
                action=FbsToteMovement.ACTION_ASSIGN,
                controller_session=session,
            ).count(),
            1,
        )

    def test_shift_routes_problem_and_canceled_to_shared_tote(self):
        problem_tote = FbsPickingCart.objects.create(
            barcode="FBS-CART-TOTE-SHARED-PROBLEM",
            name="Shared flow problem tote",
        )

        session = start_controller_session(
            workstation_id=self.workstation.id,
            controller=self.controller,
            unknown_tote_scan=self.tote.barcode,
            problem_tote_scan=problem_tote.barcode,
            first_check_tote_scan=self.first_check_tote.barcode,
        )

        self.assertEqual(session.problem_tote_id, self.tote.id)
        self.assertEqual(session.canceled_tote_id, self.tote.id)
        binding = FbsToteBinding.objects.get(tote=self.tote)
        self.assertEqual(binding.state, FbsToteBinding.STATE_UNKNOWN)
        loaded_problem_session, loaded_problem_tote = (
            controller_service_tote_for_actor(
                actor=self.controller,
                purpose=SERVICE_TOTE_PROBLEM,
            )
        )
        loaded_session, canceled_tote = controller_service_tote_for_actor(
            actor=self.controller,
            purpose=SERVICE_TOTE_CANCELED,
        )
        self.assertEqual(loaded_problem_session.id, session.id)
        self.assertEqual(loaded_problem_tote.id, self.tote.id)
        self.assertEqual(loaded_session.id, session.id)
        self.assertEqual(canceled_tote.id, self.tote.id)

    def test_extra_problem_item_is_recorded_in_shared_service_tote(self):
        session = start_controller_session(
            workstation_id=self.workstation.id,
            controller=self.controller,
            unknown_tote_scan=self.tote.barcode,
        )
        check_tote = FbsControllerCheckTote.objects.create(
            session=session,
            agency=self.agency,
            profile=self.profile,
            opened_by=self.controller,
        )
        pick_tote = FbsPickingCart.objects.create(
            barcode="FBS-CART-TOTE-SHARED-PICK",
            name="Shared service pick tote",
        )
        batch = FbsPickBatch.objects.create(
            agency=self.agency,
            status=FbsPickBatch.STATUS_VERIFICATION,
            cart=pick_tote,
            workstation=self.workstation,
            verification_assigned_to=self.controller,
            picking_completed_at=timezone.now(),
        )
        FbsControllerPickTote.objects.create(
            session=session,
            check_tote=check_tote,
            pick_batch=batch,
            tote=pick_tote,
            planned_qty=1,
        )

        item = record_extra_problem_tote_item(
            pick_batch_id=batch.id,
            scanned_value="4600999999999",
            performed_by=self.controller,
        )

        self.assertEqual(item.problem_tote_id, self.tote.id)
        self.assertTrue(
            FbsToteMovement.objects.filter(
                tote=self.tote,
                target_kind="problem_tote",
                details__problem_item_id=item.id,
            ).exists()
        )

    def test_shared_unknown_and_canceled_tote_is_released_once(self):
        session = start_controller_session(
            workstation_id=self.workstation.id,
            controller=self.controller,
            unknown_tote_scan=self.tote.barcode,
            first_check_tote_scan=self.first_check_tote.barcode,
        )
        session.check_totes.update(status=FbsControllerCheckTote.STATUS_CLOSED)

        close_controller_session(
            session_id=session.id,
            performed_by=self.controller,
        )

        self.assertEqual(
            FbsToteMovement.objects.filter(
                tote=self.tote,
                action=FbsToteMovement.ACTION_RELEASE,
                controller_session=session,
            ).count(),
            1,
        )

    def test_closed_session_offers_its_shared_service_tote_for_reuse(self):
        session = start_controller_session(
            workstation_id=self.workstation.id,
            controller=self.controller,
            unknown_tote_scan=self.tote.barcode,
        )
        close_controller_session(
            session_id=session.id,
            performed_by=self.controller,
        )

        previous = controller_views._previous_controller_tote_session(
            self.workstation
        )

        self.assertIsNotNone(previous)
        self.assertEqual(previous.id, session.id)
        self.assertEqual(previous.unknown_tote_id, self.tote.id)

    def test_legacy_duplicate_service_scans_still_create_one_shared_tote(self):
        problem_tote = FbsPickingCart.objects.create(
            barcode="FBS-CART-TOTE-DUPLICATE",
            name="Duplicate tote",
        )

        session = start_controller_session(
            workstation_id=self.workstation.id,
            controller=self.controller,
            unknown_tote_scan=self.tote.barcode,
            problem_tote_scan=problem_tote.barcode,
            canceled_tote_scan=problem_tote.barcode,
            first_check_tote_scan=self.first_check_tote.barcode,
        )

        self.assertEqual(session.problem_tote_id, self.tote.id)
        self.assertEqual(session.canceled_tote_id, self.tote.id)
        self.assertFalse(FbsToteBinding.objects.filter(tote=problem_tote).exists())

    def test_existing_session_can_bind_missing_problem_and_canceled_totes(self):
        session = start_controller_session(
            workstation_id=self.workstation.id,
            controller=self.controller,
            unknown_tote_scan=self.tote.barcode,
            first_check_tote_scan=self.first_check_tote.barcode,
        )
        session.problem_tote = None
        session.canceled_tote = None
        session.save(update_fields=["problem_tote", "canceled_tote"])
        problem_tote = FbsPickingCart.objects.create(
            barcode="FBS-CART-TOTE-LEGACY-PROBLEM",
            name="Legacy problem tote",
        )
        canceled_tote = FbsPickingCart.objects.create(
            barcode="FBS-CART-TOTE-LEGACY-CANCELED",
            name="Legacy canceled tote",
        )

        bind_controller_service_totes(
            session_id=session.id,
            problem_tote_scan=problem_tote.barcode,
            canceled_tote_scan=canceled_tote.barcode,
            performed_by=self.controller,
        )

        session.refresh_from_db()
        self.assertEqual(session.problem_tote_id, self.tote.id)
        self.assertEqual(session.canceled_tote_id, self.tote.id)
        self.assertFalse(
            FbsToteBinding.objects.filter(
                tote_id__in=(problem_tote.id, canceled_tote.id)
            ).exists()
        )

    def test_existing_session_can_restore_shared_roles_without_an_extra_scan(self):
        session = start_controller_session(
            workstation_id=self.workstation.id,
            controller=self.controller,
            unknown_tote_scan=self.tote.barcode,
            first_check_tote_scan=self.first_check_tote.barcode,
        )
        session.problem_tote = None
        session.canceled_tote = None
        session.save(update_fields=["problem_tote", "canceled_tote"])

        bind_controller_service_totes(
            session_id=session.id,
            problem_tote_scan="",
            canceled_tote_scan="",
            performed_by=self.controller,
        )

        session.refresh_from_db()
        self.assertEqual(session.problem_tote_id, self.tote.id)
        self.assertEqual(session.canceled_tote_id, self.tote.id)

    def test_problem_tote_cannot_become_pick_tote(self):
        problem_tote = FbsPickingCart.objects.create(
            barcode="FBS-CART-TOTE-RESERVED-PROBLEM",
            name="Reserved problem tote",
        )
        canceled_tote = FbsPickingCart.objects.create(
            barcode="FBS-CART-TOTE-RESERVED-CANCELED",
            name="Reserved canceled tote",
        )
        start_controller_session(
            workstation_id=self.workstation.id,
            controller=self.controller,
            unknown_tote_scan=self.tote.barcode,
            problem_tote_scan=problem_tote.barcode,
            canceled_tote_scan=canceled_tote.barcode,
            first_check_tote_scan=self.first_check_tote.barcode,
        )
        batch = FbsPickBatch.objects.create(
            agency=self.agency,
            status=FbsPickBatch.STATUS_IN_PROGRESS,
            planned_qty=1,
            picked_qty=0,
            cart=self.tote,
            assigned_to=self.picker,
        )

        with self.assertRaisesMessage(FbsPickingError, "служебная тара"):
            bind_pick_tote_to_picker(
                batch_id=batch.id,
                performed_by=self.picker,
            )


@override_settings(
    FBS_MODULE_ENABLED=True,
    FBS_WAREHOUSE_WRITES_ENABLED=True,
)
class FbsControllerToteTransferTests(TestCase):
    def setUp(self):
        users = get_user_model()
        self.storekeeper = users.objects.create_user(
            username="controller_tote_transfer_storekeeper"
        )
        Employee.objects.create(
            full_name="Кладовщик передачи тары",
            user=self.storekeeper,
            role="storekeeper",
            is_active=True,
        )
        self.source_controller = users.objects.create_user(
            username="controller_tote_transfer_source"
        )
        self.target_controller = users.objects.create_user(
            username="controller_tote_transfer_target"
        )
        self.outsider = users.objects.create_user(
            username="controller_tote_transfer_outsider"
        )
        self.picker = users.objects.create_user(
            username="controller_tote_transfer_picker"
        )
        self.agency = Agency.objects.create(agn_name="Controller tote transfer")
        self.profile = FbsIntegrationProfile.objects.create(
            agency=self.agency,
            marketplace=FbsIntegrationProfile.MARKETPLACE_OZON,
            name="Controller tote transfer profile",
            external_warehouse_id="controller-transfer",
        )
        self.source_workstation = FbsWorkstation.objects.create(
            barcode="FBS-WS-TRANSFER-SOURCE",
            name="Стол контролера 1",
        )
        self.target_workstation = FbsWorkstation.objects.create(
            barcode="FBS-WS-TRANSFER-TARGET",
            name="Стол контролера 2",
        )
        self.source_service_tote = FbsPickingCart.objects.create(
            barcode="FBS-CART-TRANSFER-SERVICE-SOURCE",
            name="Служебная тара первого стола",
        )
        self.target_service_tote = FbsPickingCart.objects.create(
            barcode="FBS-CART-TRANSFER-SERVICE-TARGET",
            name="Служебная тара второго стола",
        )
        self.source_session = start_controller_session(
            workstation_id=self.source_workstation.id,
            controller=self.source_controller,
            unknown_tote_scan=self.source_service_tote.barcode,
        )
        self.target_session = start_controller_session(
            workstation_id=self.target_workstation.id,
            controller=self.target_controller,
            unknown_tote_scan=self.target_service_tote.barcode,
        )
        start_controller_shift(
            workstation_id=self.source_workstation.id,
            controller=self.source_controller,
        )
        start_controller_shift(
            workstation_id=self.target_workstation.id,
            controller=self.target_controller,
        )

    def _active_pick_context(self, *, physical_check_tote=False):
        pick_tote = FbsPickingCart.objects.create(
            barcode=f"FBS-CART-TRANSFER-PICK-{FbsPickingCart.objects.count()}",
            name="Переносимая тара",
        )
        check_tote_cart = None
        if physical_check_tote:
            check_tote_cart = FbsPickingCart.objects.create(
                barcode=(
                    "FBS-CART-TRANSFER-CHECK-"
                    f"{FbsPickingCart.objects.count()}"
                ),
                name="Физическая тара проверки",
            )
        check_tote = FbsControllerCheckTote.objects.create(
            session=self.source_session,
            tote=check_tote_cart,
            agency=self.agency,
            profile=self.profile,
            status=FbsControllerCheckTote.STATUS_OPEN,
            item_qty=5,
            labeled_qty=2,
            composition_qty=1,
            opened_by=self.source_controller,
        )
        batch = FbsPickBatch.objects.create(
            agency=self.agency,
            status=FbsPickBatch.STATUS_VERIFICATION,
            planned_qty=5,
            picked_qty=5,
            assigned_to=self.picker,
            workstation=self.source_workstation,
            cart=pick_tote,
            verification_assigned_to=self.source_controller,
            picking_completed_at=timezone.now(),
            verification_started_at=timezone.now(),
        )
        pick_context = FbsControllerPickTote.objects.create(
            session=self.source_session,
            check_tote=check_tote,
            pick_batch=batch,
            tote=pick_tote,
            status=FbsControllerPickTote.STATUS_PROCESSING,
            planned_qty=5,
            processed_qty=2,
        )
        FbsToteBinding.objects.create(
            tote=pick_tote,
            state=FbsToteBinding.STATE_AT_CONTROL,
            workstation=self.source_workstation,
            pick_batch=batch,
            controller_session=self.source_session,
            updated_by=self.source_controller,
        )
        if check_tote_cart is not None:
            FbsToteBinding.objects.create(
                tote=check_tote_cart,
                state=FbsToteBinding.STATE_CHECKING,
                workstation=self.source_workstation,
                controller_session=self.source_session,
                updated_by=self.source_controller,
            )
        return pick_context, check_tote, batch, pick_tote, check_tote_cart

    def test_storekeeper_transfers_tote_and_preserves_verification_progress(self):
        pick_context, check_tote, batch, pick_tote, _ = self._active_pick_context()
        started_at = batch.verification_started_at

        transferred = transfer_controller_pick_tote(
            pick_tote_id=pick_context.id,
            target_session_id=self.target_session.id,
            performed_by=self.storekeeper,
        )

        transferred.refresh_from_db()
        check_tote.refresh_from_db()
        batch.refresh_from_db()
        binding = FbsToteBinding.objects.get(tote=pick_tote)
        source_service_binding = FbsToteBinding.objects.get(
            tote=self.source_service_tote
        )
        self.assertEqual(transferred.session_id, self.target_session.id)
        self.assertEqual(transferred.processed_qty, 2)
        self.assertEqual(check_tote.session_id, self.target_session.id)
        self.assertEqual(check_tote.labeled_qty, 2)
        self.assertEqual(check_tote.composition_qty, 1)
        self.assertEqual(batch.workstation_id, self.target_workstation.id)
        self.assertEqual(
            batch.verification_assigned_to_id,
            self.target_controller.id,
        )
        self.assertEqual(batch.verification_started_at, started_at)
        self.assertEqual(binding.workstation_id, self.target_workstation.id)
        self.assertEqual(binding.controller_session_id, self.target_session.id)
        self.assertEqual(binding.pick_batch_id, batch.id)
        self.assertEqual(
            source_service_binding.workstation_id,
            self.source_workstation.id,
        )
        movement = FbsToteMovement.objects.get(
            tote=pick_tote,
            details__operation="controller_tote_transfer",
        )
        self.assertEqual(movement.source_code, self.source_workstation.barcode)
        self.assertEqual(movement.target_code, self.target_workstation.barcode)
        self.assertEqual(movement.performed_by_id, self.storekeeper.id)

    def test_storekeeper_page_lists_tote_and_live_target_desk(self):
        pick_context, _check_tote, _batch, _pick_tote, _ = (
            self._active_pick_context()
        )
        self.client.force_login(self.storekeeper)

        response = self.client.get(reverse("fbs:tsd_storekeeper"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, pick_context.tote.barcode)
        self.assertContains(response, self.source_workstation.name)
        self.assertContains(response, self.target_workstation.name)
        self.assertContains(response, "Передать тару")

    def test_storekeeper_controller_settings_show_wave_people_controller_and_tote(self):
        pick_context, _check_tote, batch, _pick_tote, _ = self._active_pick_context()
        batch.created_by = self.storekeeper
        batch.save(update_fields=["created_by", "updated_at"])
        self.client.force_login(self.storekeeper)

        response = self.client.get(reverse("fbs:tsd_controller_settings"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "FBS-контроллеры")
        self.assertContains(response, f"Волна #{batch.id}")
        self.assertContains(response, "Создал: Кладовщик передачи тары")
        self.assertContains(response, "Сборщик: controller_tote_transfer_picker")
        self.assertContains(response, "controller_tote_transfer_source")
        self.assertContains(response, pick_context.tote.barcode)
        self.assertContains(response, self.source_workstation.name)
        self.assertContains(response, self.target_workstation.name)

    def test_storekeeper_controller_settings_post_transfers_tote(self):
        pick_context, _check_tote, batch, _pick_tote, _ = self._active_pick_context()
        self.client.force_login(self.storekeeper)

        response = self.client.post(
            reverse("fbs:tsd_controller_settings"),
            {
                "action": "transfer_controller_tote",
                "pick_tote_id": pick_context.id,
                "target_session_id": self.target_session.id,
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response["Location"],
            f"{reverse('fbs:tsd_controller_settings')}#controller-work",
        )
        batch.refresh_from_db()
        self.assertEqual(batch.workstation_id, self.target_workstation.id)
        self.assertEqual(batch.verification_assigned_to_id, self.target_controller.id)
        self.assertTrue(
            FbsToteMovement.objects.filter(
                tote=pick_context.tote,
                details__operation="controller_tote_transfer",
            ).exists()
        )

    def test_storekeeper_transfers_tote_from_closed_source_shift(self):
        pick_context, check_tote, batch, pick_tote, _ = self._active_pick_context()
        self.source_session.status = FbsControllerSession.STATUS_CLOSED
        self.source_session.closed_at = timezone.now()
        self.source_session.save(update_fields=["status", "closed_at"])
        self.client.force_login(self.storekeeper)

        page = self.client.get(reverse("fbs:tsd_controller_settings"))

        self.assertEqual(page.status_code, 200)
        self.assertContains(page, pick_context.tote.barcode)
        self.assertContains(page, self.target_workstation.name)

        response = self.client.post(
            reverse("fbs:tsd_controller_settings"),
            {
                "action": "transfer_controller_tote",
                "pick_tote_id": pick_context.id,
                "target_session_id": self.target_session.id,
            },
        )

        self.assertEqual(response.status_code, 302)
        pick_context.refresh_from_db()
        check_tote.refresh_from_db()
        batch.refresh_from_db()
        binding = FbsToteBinding.objects.get(tote=pick_tote)
        self.assertEqual(pick_context.session_id, self.target_session.id)
        self.assertEqual(check_tote.session_id, self.target_session.id)
        self.assertEqual(batch.workstation_id, self.target_workstation.id)
        self.assertEqual(
            batch.verification_assigned_to_id,
            self.target_controller.id,
        )
        self.assertEqual(binding.workstation_id, self.target_workstation.id)
        self.assertEqual(binding.controller_session_id, self.target_session.id)

    def test_storekeeper_post_transfers_tote_to_selected_desk(self):
        pick_context, _check_tote, batch, _pick_tote, _ = (
            self._active_pick_context()
        )
        self.client.force_login(self.storekeeper)

        response = self.client.post(
            reverse("fbs:tsd_storekeeper"),
            {
                "action": "transfer_controller_tote",
                "pick_tote_id": pick_context.id,
                "target_session_id": self.target_session.id,
            },
        )

        self.assertRedirects(response, reverse("fbs:tsd_storekeeper"))
        batch.refresh_from_db()
        self.assertEqual(batch.workstation_id, self.target_workstation.id)
        self.assertEqual(
            batch.verification_assigned_to_id,
            self.target_controller.id,
        )

    def test_transfer_moves_legacy_physical_check_tote_with_its_flow(self):
        pick_context, check_tote, _batch, _pick_tote, check_tote_cart = (
            self._active_pick_context(physical_check_tote=True)
        )

        transfer_controller_pick_tote(
            pick_tote_id=pick_context.id,
            target_session_id=self.target_session.id,
            performed_by=self.storekeeper,
        )

        check_tote.refresh_from_db()
        check_binding = FbsToteBinding.objects.get(tote=check_tote_cart)
        self.assertEqual(check_tote.session_id, self.target_session.id)
        self.assertEqual(
            check_binding.workstation_id,
            self.target_workstation.id,
        )
        self.assertEqual(
            check_binding.controller_session_id,
            self.target_session.id,
        )

    def test_transfer_rolls_back_every_change_when_tote_move_fails(self):
        pick_context, check_tote, batch, pick_tote, check_tote_cart = (
            self._active_pick_context(physical_check_tote=True)
        )
        from fbs.services import totes as tote_service

        original_move_tote = tote_service._move_tote
        move_count = 0

        def fail_second_move(*args, **kwargs):
            nonlocal move_count
            move_count += 1
            if move_count == 2:
                raise FbsPickingError("Тестовый отказ второй записи")
            return original_move_tote(*args, **kwargs)

        with mock.patch.object(
            tote_service,
            "_move_tote",
            side_effect=fail_second_move,
        ):
            with self.assertRaisesMessage(
                FbsPickingError,
                "Тестовый отказ второй записи",
            ):
                transfer_controller_pick_tote(
                    pick_tote_id=pick_context.id,
                    target_session_id=self.target_session.id,
                    performed_by=self.storekeeper,
                )

        pick_context.refresh_from_db()
        check_tote.refresh_from_db()
        batch.refresh_from_db()
        pick_binding = FbsToteBinding.objects.get(tote=pick_tote)
        check_binding = FbsToteBinding.objects.get(tote=check_tote_cart)
        self.assertEqual(pick_context.session_id, self.source_session.id)
        self.assertEqual(check_tote.session_id, self.source_session.id)
        self.assertEqual(batch.workstation_id, self.source_workstation.id)
        self.assertEqual(
            batch.verification_assigned_to_id,
            self.source_controller.id,
        )
        self.assertEqual(pick_binding.workstation_id, self.source_workstation.id)
        self.assertEqual(check_binding.workstation_id, self.source_workstation.id)
        self.assertFalse(
            FbsToteMovement.objects.filter(
                tote__in=[pick_tote, check_tote_cart],
                details__operation="controller_tote_transfer",
            ).exists()
        )

    def test_non_storekeeper_cannot_transfer_controller_tote(self):
        pick_context, _check_tote, _batch, _pick_tote, _ = (
            self._active_pick_context()
        )

        with self.assertRaisesMessage(
            FbsPickingError,
            "может только кладовщик",
        ):
            transfer_controller_pick_tote(
                pick_tote_id=pick_context.id,
                target_session_id=self.target_session.id,
                performed_by=self.outsider,
            )

    def test_transfer_rejects_target_without_live_controller(self):
        pick_context, _check_tote, batch, _pick_tote, _ = (
            self._active_pick_context()
        )
        FbsWorkstation.objects.filter(pk=self.target_workstation.id).update(
            shift_heartbeat_at=timezone.now() - timedelta(minutes=2)
        )

        with self.assertRaisesMessage(
            FbsPickingError,
            "нет активного контролера",
        ):
            transfer_controller_pick_tote(
                pick_tote_id=pick_context.id,
                target_session_id=self.target_session.id,
                performed_by=self.storekeeper,
            )

        batch.refresh_from_db()
        self.assertEqual(batch.workstation_id, self.source_workstation.id)
        self.assertEqual(
            batch.verification_assigned_to_id,
            self.source_controller.id,
        )

    def test_transfer_rejects_tote_with_unresolved_problem_item(self):
        pick_context, check_tote, batch, _pick_tote, _ = (
            self._active_pick_context()
        )
        FbsProblemToteItem.objects.create(
            session=self.source_session,
            problem_tote=self.source_service_tote,
            source_pick_tote=pick_context,
            source_check_tote=check_tote,
            scanned_value="UNKNOWN-ITEM",
            reason="Товар отложен на исходном столе",
            quantity=1,
            reported_by=self.source_controller,
        )

        with self.assertRaisesMessage(
            FbsPickingError,
            "отложен товар в служебную тару исходного стола",
        ):
            transfer_controller_pick_tote(
                pick_tote_id=pick_context.id,
                target_session_id=self.target_session.id,
                performed_by=self.storekeeper,
            )

        batch.refresh_from_db()
        self.assertEqual(batch.workstation_id, self.source_workstation.id)


class FbsControllerAjaxErrorTests(SimpleTestCase):
    def test_bind_service_totes_ajax_returns_exact_scan_error(self):
        expected_error = (
            "Тара FBS-CART-TOTE-ROLE уже используется в волне #21. "
            "Отсканируйте другую свободную тару."
        )
        request = RequestFactory().post(
            "/fbs/controller/",
            {
                "action": "bind_service_totes",
                "session_id": "1",
                "problem_tote_scan": "FBS-CART-TOTE-ROLE",
                "canceled_tote_scan": "FBS-CART-TOTE-CANCELED",
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        request.user = SimpleNamespace(id=90, is_authenticated=True)
        view = controller_views.controller_home
        while hasattr(view, "__wrapped__"):
            view = view.__wrapped__

        with mock.patch(
            "fbs.controller_views.bind_controller_service_totes",
            side_effect=FbsPickingError(expected_error),
        ), mock.patch(
            "employees.access.is_developer_login",
            return_value=True,
        ):
            response = view(request)

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.headers["Content-Type"], "application/json")
        self.assertJSONEqual(
            response.content.decode("utf-8"),
            {"ok": False, "error": expected_error},
        )
