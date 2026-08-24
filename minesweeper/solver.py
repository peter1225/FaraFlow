"""Visible-state Minesweeper rules and deterministic teacher tie-breaking."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .engine import Cell, MinesweeperBoard

SolverActionName = Literal["left_click", "right_click"]


@dataclass(frozen=True)
class SolverAction:
    action: SolverActionName
    target_cell: Cell
    rule_id: str
    reasoning: str


@dataclass(frozen=True)
class TeacherDecision:
    action: SolverAction
    legal_actions: tuple[SolverAction, ...]


def infer_forced_actions(board: MinesweeperBoard) -> tuple[SolverAction, ...]:
    """Infer safe clicks and guaranteed mines from visible numbers and flags."""

    candidates: dict[tuple[SolverActionName, Cell], SolverAction] = {}
    for cell in sorted(board.revealed_cells):
        number = board.adjacent_mines(cell)
        neighbors = board.neighbors(cell)
        flagged = [neighbor for neighbor in neighbors if neighbor in board.flagged_cells]
        covered = [
            neighbor
            for neighbor in neighbors
            if neighbor not in board.revealed_cells and neighbor not in board.flagged_cells
        ]
        remaining = number - len(flagged)
        if not covered:
            continue
        if remaining == 0:
            for target in covered:
                action = SolverAction(
                    action="left_click",
                    target_cell=target,
                    rule_id="number_satisfied",
                    reasoning=(
                        f"Cell {cell} has {number} adjacent mines and all are already flagged; "
                        f"{target} is safe."
                    ),
                )
                candidates[(action.action, target)] = action
        elif remaining == len(covered):
            for target in covered:
                action = SolverAction(
                    action="right_click",
                    target_cell=target,
                    rule_id="number_forces_mine",
                    reasoning=(
                        f"Cell {cell} still needs mines in every one of its {len(covered)} "
                        f"covered neighbors; {target} is a mine."
                    ),
                )
                candidates[(action.action, target)] = action
    return tuple(
        sorted(
            candidates.values(),
            key=lambda item: (
                0 if item.action == "left_click" else 1,
                item.target_cell[0],
                item.target_cell[1],
                item.rule_id,
            ),
        )
    )


def choose_teacher_action(actions: tuple[SolverAction, ...]) -> TeacherDecision | None:
    """Choose one deterministic action while preserving all legal alternatives."""

    if not actions:
        return None
    return TeacherDecision(action=actions[0], legal_actions=actions)
