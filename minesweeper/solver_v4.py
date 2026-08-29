"""Complete visible-state constraint analysis for Minesweeper V4 data.

The solver never consults ``mine_map``.  It enumerates assignments satisfying
the equations exposed by revealed clues, then uses the public total mine count
to reject globally impossible component assignments.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Iterable, Literal

from .engine import Cell, MinesweeperBoard

SolverActionNameV4 = Literal["left_click", "right_click"]


@dataclass(frozen=True)
class VisibleConstraint:
    cells: frozenset[Cell]
    mines: int
    clue_cells: tuple[Cell, ...] = ()


@dataclass(frozen=True)
class ForcedActionV4:
    action: SolverActionNameV4
    target_cell: Cell
    rule_id: str
    reasoning: str
    proof: dict[str, object]


@dataclass(frozen=True)
class ConstraintAnalysisV4:
    legal_actions: tuple[ForcedActionV4, ...]
    complete: bool
    consistent: bool
    component_solutions: int
    constraints: tuple[VisibleConstraint, ...]


@dataclass
class _ComponentSummary:
    cells: tuple[Cell, ...]
    solution_counts: dict[int, int]
    mine_possible: dict[int, set[Cell]]
    safe_possible: dict[int, set[Cell]]

    @property
    def total_solutions(self) -> int:
        return sum(self.solution_counts.values())


class _EnumerationLimit(RuntimeError):
    pass


def _visible_constraints(board: MinesweeperBoard) -> tuple[VisibleConstraint, ...] | None:
    merged: dict[frozenset[Cell], tuple[int, set[Cell]]] = {}
    for clue_cell in sorted(board.revealed_cells):
        visible_clue = board.visible_cell(clue_cell)
        if visible_clue.state != "revealed" or visible_clue.adjacent_mines is None:
            return None
        number = visible_clue.adjacent_mines
        neighbors = board.neighbors(clue_cell)
        flagged = {cell for cell in neighbors if cell in board.flagged_cells}
        covered = frozenset(
            cell
            for cell in neighbors
            if cell not in board.revealed_cells and cell not in board.flagged_cells
        )
        remaining = number - len(flagged)
        if remaining < 0 or remaining > len(covered):
            return None
        if not covered:
            if remaining != 0:
                return None
            continue
        existing = merged.get(covered)
        if existing is not None and existing[0] != remaining:
            return None
        if existing is None:
            merged[covered] = (remaining, {clue_cell})
        else:
            existing[1].add(clue_cell)
    return tuple(
        VisibleConstraint(cells=cells, mines=mines, clue_cells=tuple(sorted(clues)))
        for cells, (mines, clues) in sorted(
            merged.items(),
            key=lambda item: (len(item[0]), tuple(sorted(item[0])), item[1][0]),
        )
    )


def _constraint_components(
    constraints: tuple[VisibleConstraint, ...],
) -> list[tuple[tuple[Cell, ...], tuple[VisibleConstraint, ...]]]:
    by_cell: dict[Cell, list[int]] = defaultdict(list)
    for index, constraint in enumerate(constraints):
        for cell in constraint.cells:
            by_cell[cell].append(index)
    remaining = set(by_cell)
    components: list[tuple[tuple[Cell, ...], tuple[VisibleConstraint, ...]]] = []
    while remaining:
        start = min(remaining)
        queue: deque[Cell] = deque([start])
        component_cells: set[Cell] = set()
        component_constraints: set[int] = set()
        while queue:
            cell = queue.popleft()
            if cell in component_cells:
                continue
            component_cells.add(cell)
            remaining.discard(cell)
            for constraint_index in by_cell[cell]:
                if constraint_index in component_constraints:
                    continue
                component_constraints.add(constraint_index)
                queue.extend(constraints[constraint_index].cells)
        components.append(
            (
                tuple(sorted(component_cells)),
                tuple(constraints[index] for index in sorted(component_constraints)),
            )
        )
    return components


def _enumerate_component(
    cells: tuple[Cell, ...],
    constraints: tuple[VisibleConstraint, ...],
    *,
    max_solutions: int,
) -> _ComponentSummary:
    constraint_indexes_by_cell: dict[Cell, list[int]] = defaultdict(list)
    for constraint_index, constraint in enumerate(constraints):
        for cell in constraint.cells:
            constraint_indexes_by_cell[cell].append(constraint_index)
    ordered_cells = tuple(
        sorted(cells, key=lambda cell: (-len(constraint_indexes_by_cell[cell]), cell))
    )
    assigned_by_constraint = [0] * len(constraints)
    mines_by_constraint = [0] * len(constraints)
    assignment: dict[Cell, bool] = {}
    solution_counts: dict[int, int] = defaultdict(int)
    mine_possible: dict[int, set[Cell]] = defaultdict(set)
    safe_possible: dict[int, set[Cell]] = defaultdict(set)
    solution_total = 0

    def visit(position: int, mine_count: int) -> None:
        nonlocal solution_total
        if position == len(ordered_cells):
            if any(
                mines_by_constraint[index] != constraint.mines
                for index, constraint in enumerate(constraints)
            ):
                return
            solution_total += 1
            if solution_total > max_solutions:
                raise _EnumerationLimit
            solution_counts[mine_count] += 1
            for cell, is_mine in assignment.items():
                if is_mine:
                    mine_possible[mine_count].add(cell)
                else:
                    safe_possible[mine_count].add(cell)
            return

        cell = ordered_cells[position]
        related = constraint_indexes_by_cell[cell]
        for is_mine in (False, True):
            valid = True
            for constraint_index in related:
                assigned_by_constraint[constraint_index] += 1
                if is_mine:
                    mines_by_constraint[constraint_index] += 1
                constraint = constraints[constraint_index]
                assigned = assigned_by_constraint[constraint_index]
                mines = mines_by_constraint[constraint_index]
                slots_left = len(constraint.cells) - assigned
                if mines > constraint.mines or mines + slots_left < constraint.mines:
                    valid = False
            if valid:
                assignment[cell] = is_mine
                visit(position + 1, mine_count + int(is_mine))
                assignment.pop(cell, None)
            for constraint_index in related:
                if is_mine:
                    mines_by_constraint[constraint_index] -= 1
                assigned_by_constraint[constraint_index] -= 1

    visit(0, 0)
    return _ComponentSummary(
        cells=tuple(sorted(cells)),
        solution_counts=dict(solution_counts),
        mine_possible={key: set(value) for key, value in mine_possible.items()},
        safe_possible={key: set(value) for key, value in safe_possible.items()},
    )


def _possible_sums(groups: Iterable[Iterable[int]]) -> set[int]:
    totals = {0}
    for group in groups:
        totals = {left + right for left in totals for right in group}
    return totals


def analyze_forced_actions(
    board: MinesweeperBoard,
    *,
    max_component_solutions: int = 200_000,
) -> ConstraintAnalysisV4:
    """Return every move proven by the visible board and public mine total."""

    constraints = _visible_constraints(board)
    if constraints is None:
        return ConstraintAnalysisV4((), True, False, 0, ())
    covered = {
        cell
        for cell in board.cells()
        if cell not in board.revealed_cells and cell not in board.flagged_cells
    }
    remaining_mines = board.config.mines - len(board.flagged_cells)
    if remaining_mines < 0 or remaining_mines > len(covered):
        return ConstraintAnalysisV4((), True, False, 0, constraints)

    summaries: list[_ComponentSummary] = []
    try:
        for cells, component_constraints in _constraint_components(constraints):
            summary = _enumerate_component(
                cells,
                component_constraints,
                max_solutions=max_component_solutions,
            )
            if not summary.solution_counts:
                return ConstraintAnalysisV4((), True, False, 0, constraints)
            summaries.append(summary)
    except _EnumerationLimit:
        return ConstraintAnalysisV4((), False, True, 0, constraints)

    frontier = {cell for summary in summaries for cell in summary.cells}
    unconstrained = tuple(sorted(covered - frontier))
    count_groups = [summary.solution_counts.keys() for summary in summaries]
    all_component_sums = _possible_sums(count_groups)
    feasible_unconstrained_counts = {
        remaining_mines - component_sum
        for component_sum in all_component_sums
        if 0 <= remaining_mines - component_sum <= len(unconstrained)
    }
    if not feasible_unconstrained_counts:
        return ConstraintAnalysisV4((), True, False, 0, constraints)

    direct: dict[tuple[str, Cell], VisibleConstraint] = {}
    for constraint in constraints:
        if constraint.mines == 0:
            for cell in constraint.cells:
                direct.setdefault(("left_click", cell), constraint)
        elif constraint.mines == len(constraint.cells):
            for cell in constraint.cells:
                direct.setdefault(("right_click", cell), constraint)
    forced_safe: set[Cell] = set()
    forced_mine: set[Cell] = set()
    for index, summary in enumerate(summaries):
        other_groups = [
            other.solution_counts.keys()
            for other_index, other in enumerate(summaries)
            if other_index != index
        ]
        other_sums = _possible_sums(other_groups)
        feasible_counts = {
            count
            for count in summary.solution_counts
            if any(
                0 <= remaining_mines - count - other_sum <= len(unconstrained)
                for other_sum in other_sums
            )
        }
        for cell in summary.cells:
            mine_possible = any(
                cell in summary.mine_possible.get(count, set()) for count in feasible_counts
            )
            safe_possible = any(
                cell in summary.safe_possible.get(count, set()) for count in feasible_counts
            )
            if mine_possible and not safe_possible:
                forced_mine.add(cell)
            elif safe_possible and not mine_possible:
                forced_safe.add(cell)

    if feasible_unconstrained_counts == {0}:
        forced_safe.update(unconstrained)
    elif feasible_unconstrained_counts == {len(unconstrained)} and unconstrained:
        forced_mine.update(unconstrained)

    actions: list[ForcedActionV4] = []
    for action_name, cells in (("right_click", forced_mine), ("left_click", forced_safe)):
        for cell in sorted(cells):
            direct_constraint = direct.get((action_name, cell))
            if direct_constraint is not None:
                if action_name == "right_click":
                    rule_id = "number_forces_mine"
                    reasoning = (
                        f"Visible clue {direct_constraint.clue_cells[0]} requires a mine in "
                        f"every remaining covered neighbor; cell {cell} is a mine."
                    )
                else:
                    rule_id = "number_satisfied"
                    reasoning = (
                        f"Visible clue {direct_constraint.clue_cells[0]} has no unaccounted "
                        f"adjacent mines; cell {cell} is safe."
                    )
                proof: dict[str, object] = {
                    "method": "direct_clue_rule",
                    "clue_cells": [list(value) for value in direct_constraint.clue_cells],
                    "target_cell": list(cell),
                    "result": "mine" if action_name == "right_click" else "safe",
                }
            else:
                rule_id = "constraint_enumeration"
                result = "mine" if action_name == "right_click" else "safe"
                reasoning = (
                    f"Every mine assignment consistent with the visible clues marks cell "
                    f"{cell} as {result}; this move is logically forced."
                )
                proof = {
                    "method": "complete_constraint_enumeration",
                    "target_cell": list(cell),
                    "result": result,
                    "constraint_count": len(constraints),
                }
            actions.append(
                ForcedActionV4(
                    action=action_name,
                    target_cell=cell,
                    rule_id=rule_id,
                    reasoning=reasoning,
                    proof=proof,
                )
            )
    return ConstraintAnalysisV4(
        legal_actions=tuple(actions),
        complete=True,
        consistent=True,
        component_solutions=sum(summary.total_solutions for summary in summaries),
        constraints=constraints,
    )


def choose_teacher_action_v4(
    analysis: ConstraintAnalysisV4,
) -> ForcedActionV4 | None:
    """Prefer certain mines, then certain safe cells, using row-major order."""

    if not analysis.complete or not analysis.consistent or not analysis.legal_actions:
        return None
    return analysis.legal_actions[0]
