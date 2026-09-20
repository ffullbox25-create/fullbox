from types import SimpleNamespace
from unittest.mock import patch

from django.db.models.expressions import OrderBy
from django.test import SimpleTestCase

from fbs.services.picking import (
    _physical_location_sort_key,
    pick_route_ordering,
)


class FbsPickRouteOrderTests(SimpleTestCase):
    @staticmethod
    def _allocation(*, code, tier, section, row, cell):
        location = SimpleNamespace(
            tier_no=tier,
            section_no=section,
            row_no=row,
            cell_no=cell,
        )
        box = SimpleNamespace(box_code=code, route_location=location)
        return SimpleNamespace(balance=SimpleNamespace(box=box))

    @patch(
        "fbs.services.picking.fbs_box_physical_location",
        side_effect=lambda box: box.route_location,
    )
    def test_physical_route_is_tier_then_line_then_rack_then_cell(self, _resolver):
        allocations = [
            self._allocation(code="T2-L0", tier=2, section=1, row=1, cell=1),
            self._allocation(code="T1-LD", tier=1, section=5, row=1, cell=1),
            self._allocation(code="T1-LA-R2", tier=1, section=2, row=2, cell=1),
            self._allocation(code="T1-LA-R1-C2", tier=1, section=2, row=1, cell=2),
            self._allocation(code="T1-L0", tier=1, section=1, row=9, cell=1),
            self._allocation(code="NO-ADDRESS", tier=0, section=0, row=0, cell=0),
        ]

        ordered_codes = [
            allocation.balance.box.box_code
            for allocation in sorted(allocations, key=_physical_location_sort_key)
        ]

        self.assertEqual(
            ordered_codes,
            [
                "T1-L0",
                "T1-LA-R1-C2",
                "T1-LA-R2",
                "T1-LD",
                "T2-L0",
                "NO-ADDRESS",
            ],
        )

    def test_tsd_queryset_uses_the_same_coordinate_precedence(self):
        ordering = pick_route_ordering("location__")

        self.assertEqual(len(ordering), 5)
        self.assertEqual(
            [
                expression.expression.name
                for expression in ordering[1:]
                if isinstance(expression, OrderBy)
            ],
            [
                "location__tier_no",
                "location__section_no",
                "location__row_no",
                "location__cell_no",
            ],
        )
        self.assertTrue(all(expression.nulls_last for expression in ordering[1:]))
