from __future__ import annotations

from itertools import product
from random import Random
from time import perf_counter
from unittest import TestCase

from .box_picking import pick_boxes_for_quantity


class BoxPickingTests(TestCase):
    def test_matches_bruteforce_objective_on_small_inputs(self):
        random = Random(20260903)
        for _case in range(200):
            multiplicities = sorted(random.sample(range(1, 13), random.randint(1, 4)), reverse=True)
            availability = [(value, random.randint(1, 4)) for value in multiplicities]
            total_available = sum(value * count for value, count in availability)
            target = random.randint(0, total_available)

            candidates = []
            for counts in product(*(range(limit + 1) for _value, limit in availability)):
                whole_qty = sum(count * value for count, (value, _limit) in zip(counts, availability))
                if whole_qty <= target:
                    candidates.append(
                        (
                            target - whole_qty,
                            sum(counts),
                            tuple(-count for count in counts),
                            counts,
                        )
                    )
            expected = min(candidates)
            actual = pick_boxes_for_quantity(
                target,
                availability,
                unit_containers_are_pieces=False,
            )
            actual_counts = tuple(
                next((line.box_count for line in actual.lines if line.multiplicity == value), 0)
                for value, _limit in availability
            )
            self.assertEqual(
                (actual.piece_qty, sum(actual_counts), tuple(-count for count in actual_counts)),
                expected[:3],
                (target, availability, expected, actual),
            )

    def test_avoids_greedy_piece_remainder(self):
        plan = pick_boxes_for_quantity(
            540,
            [(240, 10), (180, 10)],
            unit_containers_are_pieces=False,
        )
        self.assertEqual([(line.multiplicity, line.box_count) for line in plan.lines], [(180, 3)])
        self.assertEqual(plan.piece_qty, 0)

    def test_minimizes_piece_remainder_for_1000(self):
        plan = pick_boxes_for_quantity(
            1000,
            [(360, 10), (100, 20)],
            unit_containers_are_pieces=False,
        )
        self.assertEqual([(line.multiplicity, line.box_count) for line in plan.lines], [(100, 10)])
        self.assertEqual(plan.piece_qty, 0)

    def test_smaller_than_one_box_is_all_piece_pick(self):
        plan = pick_boxes_for_quantity(
            100,
            [(180, 10)],
            unit_containers_are_pieces=False,
        )
        self.assertEqual(plan.lines, ())
        self.assertEqual(plan.piece_qty, 100)

    def test_limited_box_count_is_respected(self):
        plan = pick_boxes_for_quantity(
            360,
            [(180, 1)],
            unit_containers_are_pieces=False,
        )
        self.assertEqual([(line.multiplicity, line.box_count) for line in plan.lines], [(180, 1)])
        self.assertEqual(plan.piece_qty, 180)

    def test_zero_target_is_empty(self):
        plan = pick_boxes_for_quantity(
            0,
            [(180, 2)],
            unit_containers_are_pieces=False,
        )
        self.assertEqual(plan.lines, ())
        self.assertEqual(plan.piece_qty, 0)
        self.assertFalse(plan.unattainable)

    def test_empty_availability_is_unattainable(self):
        plan = pick_boxes_for_quantity(
            10,
            [],
            unit_containers_are_pieces=False,
        )
        self.assertEqual(plan.lines, ())
        self.assertEqual(plan.piece_qty, 10)
        self.assertEqual(plan.missing_qty, 10)
        self.assertTrue(plan.unattainable)

    def test_invalid_multiplicities_do_not_raise(self):
        plan = pick_boxes_for_quantity(
            7,
            [(0, 12), (-2, 5), (3, -1), (2, 4)],
            unit_containers_are_pieces=False,
        )
        self.assertEqual([(line.multiplicity, line.box_count) for line in plan.lines], [(2, 3)])
        self.assertEqual(plan.piece_qty, 1)

    def test_fewer_boxes_wins_for_equal_piece_remainder(self):
        plan = pick_boxes_for_quantity(
            600,
            [(300, 2), (200, 3)],
            unit_containers_are_pieces=False,
        )
        self.assertEqual([(line.multiplicity, line.box_count) for line in plan.lines], [(300, 2)])

    def test_larger_multiplicity_wins_after_equal_box_count(self):
        plan = pick_boxes_for_quantity(
            10,
            [(8, 1), (7, 1), (3, 1), (2, 1)],
            unit_containers_are_pieces=False,
        )
        self.assertEqual(
            [(line.multiplicity, line.box_count) for line in plan.lines],
            [(8, 1), (2, 1)],
        )

    def test_unit_container_policy_is_explicit(self):
        whole = pick_boxes_for_quantity(
            3,
            [(2, 1), (1, 3)],
            unit_containers_are_pieces=False,
        )
        pieces = pick_boxes_for_quantity(
            3,
            [(2, 1), (1, 3)],
            unit_containers_are_pieces=True,
        )
        self.assertEqual([(line.multiplicity, line.box_count) for line in whole.lines], [(2, 1), (1, 1)])
        self.assertEqual(whole.piece_qty, 0)
        self.assertEqual([(line.multiplicity, line.box_count) for line in pieces.lines], [(2, 1)])
        self.assertEqual(pieces.piece_qty, 1)

    def test_shortage_is_reported_separately(self):
        plan = pick_boxes_for_quantity(
            500,
            [(180, 2)],
            unit_containers_are_pieces=False,
        )
        self.assertEqual(plan.fulfilled_qty, 360)
        self.assertEqual(plan.piece_qty, 140)
        self.assertEqual(plan.missing_qty, 140)
        self.assertTrue(plan.unattainable)

    def test_result_is_deterministic(self):
        first = pick_boxes_for_quantity(
            9876,
            [(360, 20), (240, 40), (180, 40), (100, 100)],
            unit_containers_are_pieces=False,
        )
        second = pick_boxes_for_quantity(
            9876,
            [(100, 100), (180, 40), (360, 20), (240, 40)],
            unit_containers_are_pieces=False,
        )
        self.assertEqual(first, second)

    def test_large_input_stays_within_budget(self):
        started = perf_counter()
        plan = pick_boxes_for_quantity(
            100_000,
            [
                (997, 200),
                (499, 300),
                (251, 500),
                (127, 1000),
                (61, 2000),
                (31, 4000),
                (7, 20_000),
                (1, 100_000),
            ],
            unit_containers_are_pieces=False,
        )
        elapsed_ms = (perf_counter() - started) * 1000
        self.assertEqual(plan.piece_qty, 0)
        self.assertLess(elapsed_ms, 50.0, f"allocation took {elapsed_ms:.3f} ms")

    def test_piece_remainder_is_never_worse_than_descending_greedy(self):
        random = Random(42)
        for _case in range(200):
            availability = sorted(
                [(value, random.randint(1, 40)) for value in random.sample(range(2, 500), 8)],
                reverse=True,
            )
            total_available = sum(value * count for value, count in availability)
            target = random.randint(1, min(total_available, 100_000))
            greedy_remaining = target
            for multiplicity, available_boxes in availability:
                greedy_remaining -= min(available_boxes, greedy_remaining // multiplicity) * multiplicity
            plan = pick_boxes_for_quantity(
                target,
                availability,
                unit_containers_are_pieces=True,
            )
            self.assertLessEqual(plan.piece_qty, greedy_remaining)
