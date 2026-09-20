"""Pure quantity-to-box-multiplicity allocation for client shipping requests."""

from __future__ import annotations

from dataclasses import dataclass
from math import gcd
from time import perf_counter
from typing import Iterable, Mapping


@dataclass(frozen=True)
class BoxPickLine:
    multiplicity: int
    box_count: int

    @property
    def qty(self) -> int:
        return self.multiplicity * self.box_count


@dataclass(frozen=True)
class BoxPickPlan:
    lines: tuple[BoxPickLine, ...]
    piece_qty: int
    missing_qty: int
    unattainable: bool
    tie_break_fallback_used: bool

    @property
    def whole_box_qty(self) -> int:
        return sum(line.qty for line in self.lines)

    @property
    def planned_qty(self) -> int:
        return self.whole_box_qty + self.piece_qty

    @property
    def fulfilled_qty(self) -> int:
        return max(self.planned_qty - self.missing_qty, 0)


def _nonnegative_int(value: object) -> int:
    try:
        return max(int(value or 0), 0)
    except (TypeError, ValueError, OverflowError):
        return 0


def _normalized_availability(
    available: Iterable[tuple[object, object]] | Mapping[object, object] | None,
) -> list[tuple[int, int]]:
    if available is None:
        return []
    values = available.items() if isinstance(available, Mapping) else available
    merged: dict[int, int] = {}
    for raw_multiplicity, raw_box_count in values:
        multiplicity = _nonnegative_int(raw_multiplicity)
        box_count = _nonnegative_int(raw_box_count)
        if multiplicity <= 0 or box_count <= 0:
            continue
        merged[multiplicity] = merged.get(multiplicity, 0) + box_count
    return sorted(merged.items(), reverse=True)


def _bounded_reachability(
    goal: int,
    denominations: list[tuple[int, int]],
) -> tuple[int, list[tuple[int, int, int]], list[int]]:
    """Return reachable bitset plus chunk metadata and pre-chunk bitsets."""
    mask = (1 << (goal + 1)) - 1
    reachable = 1
    chunks: list[tuple[int, int, int]] = []
    before_chunks: list[int] = []
    for denomination_index, (multiplicity, box_count) in enumerate(denominations):
        remaining = min(box_count, goal // multiplicity)
        chunk_size = 1
        while remaining:
            take = min(chunk_size, remaining)
            shift = multiplicity * take
            before_chunks.append(reachable)
            chunks.append((denomination_index, shift, take))
            reachable = (reachable | (reachable << shift)) & mask
            remaining -= take
            chunk_size <<= 1
    return reachable, chunks, before_chunks


def _reconstruct_chunks(
    target_sum: int,
    denomination_count: int,
    chunks: list[tuple[int, int, int]],
    before_chunks: list[int],
) -> list[int]:
    counts = [0] * denomination_count
    remainder = target_sum
    for chunk_index in range(len(chunks) - 1, -1, -1):
        denomination_index, shift, take = chunks[chunk_index]
        before = before_chunks[chunk_index]
        if (before >> remainder) & 1:
            continue
        if remainder >= shift and ((before >> (remainder - shift)) & 1):
            counts[denomination_index] += take
            remainder -= shift
    if remainder:
        raise RuntimeError("reachable quantity could not be reconstructed")
    return counts


def _suffix_reachability(goal: int, denominations: list[tuple[int, int]]) -> list[int]:
    mask = (1 << (goal + 1)) - 1
    suffix = [1] * (len(denominations) + 1)
    for index in range(len(denominations) - 1, -1, -1):
        multiplicity, box_count = denominations[index]
        reachable = suffix[index + 1]
        remaining = min(box_count, goal // multiplicity)
        chunk_size = 1
        while remaining:
            take = min(chunk_size, remaining)
            reachable = (reachable | (reachable << (multiplicity * take))) & mask
            remaining -= take
            chunk_size <<= 1
        suffix[index] = reachable
    return suffix


def _minimum_box_lower_bound(
    denominations: list[tuple[int, int]],
    start: int,
    quantity: int,
) -> int:
    if quantity <= 0:
        return 0
    boxes = 0
    remaining = quantity
    for multiplicity, available_boxes in denominations[start:]:
        capacity = multiplicity * available_boxes
        if capacity >= remaining:
            return boxes + (remaining + multiplicity - 1) // multiplicity
        boxes += available_boxes
        remaining -= capacity
    return 1 << 60


def _ceil_div(numerator: int, denominator: int) -> int:
    return -(-numerator // denominator)


def _solve_last_two(
    quantity: int,
    first: tuple[int, int],
    second: tuple[int, int],
) -> tuple[int, int] | None:
    first_value, first_limit = first
    second_value, second_limit = second
    common = gcd(first_value, second_value)
    if quantity % common:
        return None

    a = first_value // common
    b = second_value // common
    total = quantity // common
    low = max(0, _ceil_div(total - b * second_limit, a))
    high = min(first_limit, total // a)
    if low > high:
        return None

    residue = 0 if b == 1 else (total * pow(a, -1, b)) % b
    first_count = high - ((high - residue) % b)
    if first_count < low:
        return None
    second_count = (total - a * first_count) // b
    if second_count < 0 or second_count > second_limit:
        return None
    return first_count, second_count


def _optimize_ties(
    target_sum: int,
    denominations: list[tuple[int, int]],
    initial_counts: list[int],
    *,
    budget_ms: float,
) -> tuple[list[int], bool]:
    """Minimize box count, then prefer counts of larger multiplicities."""
    if target_sum <= 0 or not denominations:
        return initial_counts, False

    suffix_reachable = _suffix_reachability(target_sum, denominations)
    suffix_capacity = [0] * (len(denominations) + 1)
    suffix_gcd = [0] * (len(denominations) + 1)
    for index in range(len(denominations) - 1, -1, -1):
        multiplicity, box_count = denominations[index]
        suffix_capacity[index] = suffix_capacity[index + 1] + multiplicity * box_count
        suffix_gcd[index] = gcd(multiplicity, suffix_gcd[index + 1])

    best = list(initial_counts)
    best_key = (sum(best), tuple(-count for count in best))
    current = [0] * len(denominations)
    deadline = perf_counter() + max(float(budget_ms), 0.0) / 1000.0
    timed_out = False
    visited = 0

    def consider() -> None:
        nonlocal best, best_key
        key = (sum(current), tuple(-count for count in current))
        if key < best_key:
            best = list(current)
            best_key = key

    def search(index: int, remaining: int, used_boxes: int) -> None:
        nonlocal timed_out, visited
        if timed_out:
            return
        visited += 1
        if not (visited & 63) and perf_counter() >= deadline:
            timed_out = True
            return
        if remaining == 0:
            consider()
            return
        if index >= len(denominations) or remaining < 0:
            return
        if remaining > suffix_capacity[index]:
            return
        common = suffix_gcd[index]
        if common and remaining % common:
            return
        if not ((suffix_reachable[index] >> remaining) & 1):
            return
        lower_bound = _minimum_box_lower_bound(denominations, index, remaining)
        if used_boxes + lower_bound > best_key[0]:
            return

        remaining_types = len(denominations) - index
        if remaining_types == 1:
            value, limit = denominations[index]
            if remaining % value:
                return
            count = remaining // value
            if count <= limit:
                current[index] = count
                consider()
                current[index] = 0
            return
        if remaining_types == 2:
            solution = _solve_last_two(
                remaining,
                denominations[index],
                denominations[index + 1],
            )
            if solution is None:
                return
            first_count, second_count = solution
            current[index] = first_count
            current[index + 1] = second_count
            consider()
            current[index] = 0
            current[index + 1] = 0
            return

        value, limit = denominations[index]
        next_capacity = suffix_capacity[index + 1]
        minimum_count = max(0, _ceil_div(remaining - next_capacity, value))
        maximum_count = min(limit, remaining // value, best_key[0] - used_boxes)
        for count in range(maximum_count, minimum_count - 1, -1):
            next_remaining = remaining - count * value
            if not ((suffix_reachable[index + 1] >> next_remaining) & 1):
                continue
            next_lower_bound = _minimum_box_lower_bound(
                denominations,
                index + 1,
                next_remaining,
            )
            if used_boxes + count + next_lower_bound > best_key[0]:
                continue
            current[index] = count
            search(index + 1, next_remaining, used_boxes + count)
            current[index] = 0
            if timed_out:
                return

    search(0, target_sum, 0)
    if perf_counter() >= deadline and visited:
        timed_out = True
    return best, timed_out


def pick_boxes_for_quantity(
    target_qty: object,
    available: Iterable[tuple[object, object]] | Mapping[object, object] | None,
    *,
    unit_containers_are_pieces: bool,
    tie_break_budget_ms: float = 20.0,
) -> BoxPickPlan:
    """Build a deterministic quantity plan without selecting physical box codes.

    ``unit_containers_are_pieces`` is deliberately required: product owners must
    decide whether a container with multiplicity 1 is a whole box or a piece.
    """
    requested = _nonnegative_int(target_qty)
    normalized = _normalized_availability(available)
    total_available = sum(multiplicity * count for multiplicity, count in normalized)
    fulfilled_target = min(requested, total_available)
    missing_qty = requested - fulfilled_target
    if fulfilled_target <= 0:
        return BoxPickPlan(
            lines=(),
            piece_qty=requested,
            missing_qty=missing_qty,
            unattainable=bool(missing_qty),
            tie_break_fallback_used=False,
        )

    denominations = [
        (multiplicity, count)
        for multiplicity, count in normalized
        if not (unit_containers_are_pieces and multiplicity == 1)
    ]
    if not denominations:
        return BoxPickPlan(
            lines=(),
            piece_qty=requested,
            missing_qty=missing_qty,
            unattainable=bool(missing_qty),
            tie_break_fallback_used=False,
        )

    reachable, chunks, before_chunks = _bounded_reachability(fulfilled_target, denominations)
    best_whole_qty = reachable.bit_length() - 1
    initial_counts = _reconstruct_chunks(
        best_whole_qty,
        len(denominations),
        chunks,
        before_chunks,
    )
    counts, timed_out = _optimize_ties(
        best_whole_qty,
        denominations,
        initial_counts,
        budget_ms=tie_break_budget_ms,
    )
    lines = tuple(
        BoxPickLine(multiplicity=multiplicity, box_count=count)
        for (multiplicity, _available_count), count in zip(denominations, counts)
        if count > 0
    )
    return BoxPickPlan(
        lines=lines,
        piece_qty=requested - best_whole_qty,
        missing_qty=missing_qty,
        unattainable=bool(missing_qty),
        tie_break_fallback_used=timed_out,
    )
