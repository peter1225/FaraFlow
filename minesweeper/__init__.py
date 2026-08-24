"""Deterministic Minesweeper environment and Canonical V3 data helpers."""

from .engine import BoardConfig, GameStatus, MinesweeperBoard
from .geometry import BoardRectCss
from .solver import TeacherDecision, choose_teacher_action, infer_forced_actions

__all__ = [
    "BoardConfig",
    "BoardRectCss",
    "GameStatus",
    "MinesweeperBoard",
    "TeacherDecision",
    "choose_teacher_action",
    "infer_forced_actions",
]
