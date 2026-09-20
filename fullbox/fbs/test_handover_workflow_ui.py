from types import SimpleNamespace

from django.test import SimpleTestCase

from fbs.models import (
    FbsHandoverBatch,
    FbsHandoverBox,
    FbsIntegrationProfile,
    FbsPickRestockRequest,
)
from fbs.tsd_views import (
    _handover_assignment_display_priority,
    _handover_exclusion_check_copy,
    _handover_next_action,
    _handover_relevant_commands,
    _handover_restock_blocks_handover,
)


class HandoverRelevantCommandTests(SimpleTestCase):
    def test_ignores_failed_command_for_order_moved_to_another_handover(self):
        batch_command = SimpleNamespace(order_id=None, status="confirmed")
        current_order_command = SimpleNamespace(order_id=101, status="confirmed")
        moved_order_command = SimpleNamespace(order_id=202, status="failed")

        relevant = _handover_relevant_commands(
            [batch_command, current_order_command, moved_order_command],
            order_ids={101},
        )

        self.assertEqual(relevant, [batch_command, current_order_command])

    def test_keeps_failed_command_for_current_order(self):
        current_order_command = SimpleNamespace(order_id=101, status="failed")

        relevant = _handover_relevant_commands(
            [current_order_command],
            order_ids={101},
        )

        self.assertEqual(relevant, [current_order_command])


class HandoverNextActionTests(SimpleTestCase):
    def batch(self, *, status=FbsHandoverBatch.STATUS_OPEN, marketplace_state="open"):
        return SimpleNamespace(
            status=status,
            marketplace_state=marketplace_state,
            compatibility_key="test:destination:pickup_point",
            profile=SimpleNamespace(marketplace=FbsIntegrationProfile.MARKETPLACE_WB),
        )

    def box(self, *, status, order_count, box_id=1):
        return SimpleNamespace(
            id=box_id,
            status=status,
            order_count=order_count,
            qr_code=f"BOX-{box_id}",
        )

    def action(self, batch, boxes, *, assignments=1, missing=0):
        return _handover_next_action(
            batch,
            boxes=boxes,
            assignment_count=assignments,
            missing_order_count=missing,
            all_assignments_confirmed=True,
            all_orders_ready=True,
            composition_ready=True,
        )

    def test_verified_filled_box_opens_marketplace_delivery(self):
        action = self.action(
            self.batch(),
            [self.box(status=FbsHandoverBox.STATUS_OPEN, order_count=1)],
        )

        self.assertEqual(action.code, "deliver_marketplace")

    def test_verified_closed_box_opens_marketplace_delivery(self):
        action = self.action(
            self.batch(),
            [self.box(status=FbsHandoverBox.STATUS_CLOSED, order_count=1)],
        )

        self.assertEqual(action.code, "deliver_marketplace")

    def test_missing_order_returns_to_packing(self):
        action = self.action(
            self.batch(),
            [self.box(status=FbsHandoverBox.STATUS_OPEN, order_count=1)],
            assignments=2,
            missing=1,
        )

        self.assertEqual(action.code, "pack_orders")

    def test_unverified_wb_order_opens_composition_scan(self):
        action = _handover_next_action(
            self.batch(),
            boxes=[self.box(status=FbsHandoverBox.STATUS_OPEN, order_count=1)],
            assignment_count=1,
            missing_order_count=0,
            unverified_order_count=1,
            all_assignments_confirmed=True,
            all_orders_ready=True,
            composition_ready=True,
        )

        self.assertEqual(action.code, "verify_orders")

    def test_empty_extra_box_is_explicit_blocker(self):
        action = self.action(
            self.batch(),
            [self.box(status=FbsHandoverBox.STATUS_OPEN, order_count=0)],
        )

        self.assertEqual(action.code, "blocked_empty_box")
        self.assertTrue(action.blocked)

    def test_ready_wb_supply_opens_marketplace_delivery(self):
        action = self.action(
            self.batch(status=FbsHandoverBatch.STATUS_READY),
            [self.box(status=FbsHandoverBox.STATUS_SCANNED, order_count=1)],
        )

        self.assertEqual(action.code, "deliver_marketplace")

    def test_marketplace_complete_opens_driver_dispatch(self):
        action = self.action(
            self.batch(
                status=FbsHandoverBatch.STATUS_READY,
                marketplace_state=FbsHandoverBatch.MARKETPLACE_COMPLETE,
            ),
            [self.box(status=FbsHandoverBox.STATUS_SCANNED, order_count=1)],
        )

        self.assertEqual(action.code, "dispatch")

    def test_accepted_supply_has_no_next_operation(self):
        action = self.action(
            self.batch(status=FbsHandoverBatch.STATUS_ACCEPTED),
            [self.box(status=FbsHandoverBox.STATUS_ACCEPTED, order_count=1)],
        )

        self.assertEqual(action.code, "complete")

    def test_blocking_return_names_the_problem_order(self):
        action = _handover_next_action(
            self.batch(),
            boxes=[self.box(status=FbsHandoverBox.STATUS_OPEN, order_count=1)],
            assignment_count=1,
            missing_order_count=0,
            all_assignments_confirmed=True,
            all_orders_ready=True,
            composition_ready=False,
            blocking_return_count=1,
            blocking_return_order_numbers=("0144248053-0164-1",),
        )

        self.assertEqual(action.code, "await_return")
        self.assertEqual(
            action.title,
            "Завершить возврат заказа 0144248053-0164-1",
        )
        self.assertIn("заказа 0144248053-0164-1", action.description)

    def test_failed_wb_exclusion_tells_controller_what_to_do(self):
        action = _handover_next_action(
            self.batch(status=FbsHandoverBatch.STATUS_READY),
            boxes=[self.box(status=FbsHandoverBox.STATUS_SCANNED, order_count=1)],
            assignment_count=1,
            missing_order_count=0,
            all_assignments_confirmed=True,
            all_orders_ready=False,
            composition_ready=False,
            blocking_return_count=1,
            blocking_return_order_numbers=("5652752066",),
            blocking_return_statuses=(FbsPickRestockRequest.STATUS_FAILED,),
        )

        self.assertEqual(action.code, "await_return")
        self.assertEqual(action.title, "Снимите заказ 5652752066")
        self.assertIn("назначенную тару", action.description)
        self.assertIn("Финальная отправка", action.description)

    def test_blocking_return_assignment_is_shown_first(self):
        normal_assignment = SimpleNamespace(id=1, order_id=101)
        problem_assignment = SimpleNamespace(id=2, order_id=202)
        exclusion_by_order_id = {
            202: SimpleNamespace(
                id=9,
                status=FbsPickRestockRequest.STATUS_FAILED,
                source_tote_id=None,
            )
        }

        assignments = [normal_assignment, problem_assignment]
        assignments.sort(
            key=lambda assignment: _handover_assignment_display_priority(
                assignment,
                exclusion_by_order_id,
            )
        )

        self.assertEqual(assignments, [problem_assignment, normal_assignment])

    def test_failed_exclusion_row_explains_controller_action(self):
        exclusion_request = SimpleNamespace(
            status=FbsPickRestockRequest.STATUS_FAILED,
            source_tote_id=None,
            reason="WB пока показывает заказ в поставке.",
        )

        label, detail = _handover_exclusion_check_copy(exclusion_request)

        self.assertEqual(label, "Снимите заказ и освободите проверку")
        self.assertIn("отсканировать", detail)
        self.assertIn("финальным барьером", detail)

    def test_staged_return_no_longer_blocks_controller_actions(self):
        staged_request = SimpleNamespace(
            status=FbsPickRestockRequest.STATUS_WAITING_MARKETPLACE,
            source_tote_id=77,
        )

        self.assertFalse(_handover_restock_blocks_handover(staged_request))

    def test_unstaged_return_still_requires_controller_tote_scan(self):
        unstaged_request = SimpleNamespace(
            status=FbsPickRestockRequest.STATUS_WAITING_MARKETPLACE,
            source_tote_id=None,
        )

        self.assertTrue(_handover_restock_blocks_handover(unstaged_request))

    def test_staged_return_allows_remaining_order_scan(self):
        action = _handover_next_action(
            self.batch(),
            boxes=[self.box(status=FbsHandoverBox.STATUS_OPEN, order_count=1)],
            assignment_count=2,
            missing_order_count=0,
            unverified_order_count=1,
            all_assignments_confirmed=True,
            all_orders_ready=True,
            composition_ready=False,
            background_return_count=1,
            background_return_order_numbers=("5652752066",),
        )

        self.assertEqual(action.code, "verify_orders")

    def test_staged_return_blocks_only_final_wb_step(self):
        action = _handover_next_action(
            self.batch(status=FbsHandoverBatch.STATUS_READY),
            boxes=[self.box(status=FbsHandoverBox.STATUS_SCANNED, order_count=1)],
            assignment_count=1,
            missing_order_count=0,
            all_assignments_confirmed=True,
            all_orders_ready=True,
            composition_ready=False,
            background_return_count=1,
            background_return_order_numbers=("5652752066",),
        )

        self.assertEqual(action.code, "wait_marketplace_exclusion")
        self.assertIn("следующей работе", action.description)
        self.assertIn("5652752066", action.description)
