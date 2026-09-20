from types import SimpleNamespace

from django.test import SimpleTestCase

from .models import FbsReplenishmentAllocation, FbsReplenishmentPlan
from .services.reachtruck_bridge import (
    _current_prepared_flow_allocation,
    _prepared_flow_location_locked,
    _task_specs,
)


class FbsReachtruckTaskCompactionTests(SimpleTestCase):
    @staticmethod
    def _allocation(*, allocation_id, container_id, line_id, status):
        return SimpleNamespace(
            id=allocation_id,
            status=status,
            line_id=line_id,
            source_snapshot=SimpleNamespace(container_id=container_id),
        )

    def test_reserved_allocations_are_grouped_by_physical_box_and_line(self):
        plan = SimpleNamespace(mode=FbsReplenishmentPlan.MODE_ITEM)
        rows = [
            self._allocation(
                allocation_id=1,
                container_id=10,
                line_id=100,
                status=FbsReplenishmentAllocation.STATUS_RESERVED,
            ),
            self._allocation(
                allocation_id=2,
                container_id=10,
                line_id=100,
                status=FbsReplenishmentAllocation.STATUS_RESERVED,
            ),
            self._allocation(
                allocation_id=3,
                container_id=11,
                line_id=100,
                status=FbsReplenishmentAllocation.STATUS_RESERVED,
            ),
            self._allocation(
                allocation_id=4,
                container_id=10,
                line_id=101,
                status=FbsReplenishmentAllocation.STATUS_RESERVED,
            ),
        ]

        specs = _task_specs(plan, rows)

        self.assertEqual(
            [[allocation.id for allocation in group] for group in specs],
            [[1, 2], [3], [4]],
        )

    def test_completed_allocations_remain_separate_from_open_group(self):
        plan = SimpleNamespace(mode=FbsReplenishmentPlan.MODE_ITEM)
        rows = [
            self._allocation(
                allocation_id=1,
                container_id=10,
                line_id=100,
                status=FbsReplenishmentAllocation.STATUS_STAGED,
            ),
            self._allocation(
                allocation_id=2,
                container_id=10,
                line_id=100,
                status=FbsReplenishmentAllocation.STATUS_RESERVED,
            ),
            self._allocation(
                allocation_id=3,
                container_id=10,
                line_id=100,
                status=FbsReplenishmentAllocation.STATUS_RESERVED,
            ),
        ]

        specs = _task_specs(plan, rows)

        self.assertEqual(
            [[allocation.id for allocation in group] for group in specs],
            [[1], [2, 3]],
        )

    def test_grouped_prepared_flow_uses_first_open_allocation(self):
        rows = [
            SimpleNamespace(
                id=1,
                status=FbsReplenishmentAllocation.STATUS_STAGED,
            ),
            SimpleNamespace(
                id=2,
                status=FbsReplenishmentAllocation.STATUS_IN_PROGRESS,
            ),
            SimpleNamespace(
                id=3,
                status=FbsReplenishmentAllocation.STATUS_RESERVED,
            ),
        ]

        allocation = _current_prepared_flow_allocation(rows)

        self.assertEqual(allocation.id, 2)

    def test_prepared_flow_location_locks_after_first_scan(self):
        self.assertFalse(
            _prepared_flow_location_locked(
                [
                    SimpleNamespace(
                        scanned_qty=0,
                        status="printed",
                    )
                ]
            )
        )
        self.assertTrue(
            _prepared_flow_location_locked(
                [
                    SimpleNamespace(
                        scanned_qty=1,
                        status="filling",
                    )
                ]
            )
        )
