"""A small deterministic Minesweeper engine for trajectory generation.

The engine deliberately separates hidden truth from the visible board state.
Only ``visible_snapshot`` belongs in a model observation; ``mine_map`` is
supervision-only data for verification and evaluation.
"""

from __future__ import annotations

import random
from collections import deque
from dataclasses import dataclass
from enum import Enum
from typing import Iterable, Iterator, Literal

Cell = tuple[int, int]
VisibleState = Literal["covered", "flagged", "revealed"]


class GameStatus(str, Enum):
    READY = "ready"
    ACTIVE = "active"
    WON = "won"
    LOST = "lost"


class BoardActionError(ValueError):
    """Raised when an action cannot be applied to the current board state."""


@dataclass(frozen=True)
class BoardConfig:
    width: int = 9
    height: int = 9
    mines: int = 10

    def __post_init__(self) -> None:
        if self.width <= 0 or self.height <= 0:
            raise ValueError("board dimensions must be positive")
        if self.mines < 0 or self.mines >= self.width * self.height:
            raise ValueError("mines must be between 0 and board_cells - 1")


@dataclass(frozen=True)
class VisibleCell:
    state: VisibleState
    adjacent_mines: int | None = None


class MinesweeperBoard:
    """Deterministic, first-click-safe Minesweeper state machine."""

    def __init__(self, config: BoardConfig = BoardConfig(), seed: int = 0) -> None:
        self.config = config
        self.seed = seed
        self._rng = random.Random(seed)
        self._mines: set[Cell] = set()
        self._revealed: set[Cell] = set()
        self._flags: set[Cell] = set()
        self._generated = False
        self._status = GameStatus.READY
        self._exploded: Cell | None = None

    @classmethod
    def from_truth(
        cls,
        config: BoardConfig,
        mines: Iterable[Cell],
        *,
        revealed: Iterable[Cell] = (),
        flags: Iterable[Cell] = (),
        status: GameStatus = GameStatus.ACTIVE,
        seed: int = 0,
    ) -> MinesweeperBoard:
        """Create a controlled state for solver tests and verifier fixtures."""

        board = cls(config, seed=seed)
        board._mines = set(mines)
        board._revealed = set(revealed)
        board._flags = set(flags)
        board._generated = True
        board._status = status
        board._validate_cells(board._mines | board._revealed | board._flags)
        if board._mines & board._revealed:
            raise ValueError("revealed cells cannot contain mines")
        if board._revealed & board._flags:
            raise ValueError("a cell cannot be both revealed and flagged")
        return board

    @property
    def status(self) -> GameStatus:
        return self._status

    @property
    def exploded(self) -> Cell | None:
        return self._exploded

    @property
    def revealed_cells(self) -> frozenset[Cell]:
        return frozenset(self._revealed)

    @property
    def flagged_cells(self) -> frozenset[Cell]:
        return frozenset(self._flags)

    @property
    def mine_map(self) -> frozenset[Cell]:
        """Hidden truth; never include this in model-facing observations."""

        return frozenset(self._mines)

    def _validate_cell(self, cell: Cell) -> None:
        row, col = cell
        if not (0 <= row < self.config.height and 0 <= col < self.config.width):
            raise BoardActionError(f"cell outside board: {cell}")

    def _validate_cells(self, cells: Iterable[Cell]) -> None:
        for cell in cells:
            self._validate_cell(cell)

    def cells(self) -> Iterator[Cell]:
        for row in range(self.config.height):
            for col in range(self.config.width):
                yield row, col

    def neighbors(self, cell: Cell) -> tuple[Cell, ...]:
        row, col = cell
        result = []
        for row_delta in (-1, 0, 1):
            for col_delta in (-1, 0, 1):
                if row_delta == 0 and col_delta == 0:
                    continue
                candidate = (row + row_delta, col + col_delta)
                if 0 <= candidate[0] < self.config.height and 0 <= candidate[1] < self.config.width:
                    result.append(candidate)
        return tuple(result)

    def adjacent_mines(self, cell: Cell) -> int:
        self._validate_cell(cell)
        return sum(neighbor in self._mines for neighbor in self.neighbors(cell))

    def _generate(self, first_cell: Cell) -> None:
        candidates = [cell for cell in self.cells() if cell != first_cell]
        self._mines = set(self._rng.sample(candidates, self.config.mines))
        self._generated = True

    def _check_finished(self) -> None:
        safe_cells = self.config.width * self.config.height - len(self._mines)
        if len(self._revealed) == safe_cells:
            self._status = GameStatus.WON

    def click(self, cell: Cell) -> tuple[Cell, ...]:
        """Left-click a cell and return newly revealed cells in row-major order."""

        self._validate_cell(cell)
        if self._status in {GameStatus.WON, GameStatus.LOST}:
            raise BoardActionError("game is already finished")
        if cell in self._flags:
            raise BoardActionError("cannot click a flagged cell")
        if cell in self._revealed:
            return ()
        if not self._generated:
            self._generate(cell)
        self._status = GameStatus.ACTIVE
        if cell in self._mines:
            self._exploded = cell
            self._status = GameStatus.LOST
            return ()

        revealed_now: set[Cell] = set()
        queue: deque[Cell] = deque([cell])
        while queue:
            current = queue.popleft()
            if current in revealed_now or current in self._flags or current in self._mines:
                continue
            revealed_now.add(current)
            if self.adjacent_mines(current) == 0:
                queue.extend(
                    neighbor
                    for neighbor in self.neighbors(current)
                    if neighbor not in revealed_now and neighbor not in self._flags
                )
        self._revealed.update(revealed_now)
        self._check_finished()
        return tuple(sorted(revealed_now))

    def toggle_flag(self, cell: Cell) -> bool:
        """Right-click a covered cell; return whether it is now flagged."""

        self._validate_cell(cell)
        if self._status in {GameStatus.WON, GameStatus.LOST}:
            raise BoardActionError("game is already finished")
        if cell in self._revealed:
            raise BoardActionError("cannot flag a revealed cell")
        if cell in self._flags:
            self._flags.remove(cell)
            return False
        self._flags.add(cell)
        return True

    def chord(self, cell: Cell) -> tuple[Cell, ...]:
        """Reveal covered neighbors when the visible flag count satisfies a number."""

        self._validate_cell(cell)
        if cell not in self._revealed:
            raise BoardActionError("chord requires a revealed cell")
        if len(self._flags.intersection(self.neighbors(cell))) != self.adjacent_mines(cell):
            return ()
        revealed: list[Cell] = []
        for neighbor in self.neighbors(cell):
            if neighbor not in self._revealed and neighbor not in self._flags:
                revealed.extend(self.click(neighbor))
                if self._status is GameStatus.LOST:
                    break
        return tuple(sorted(set(revealed)))

    def visible_cell(self, cell: Cell) -> VisibleCell:
        self._validate_cell(cell)
        if cell in self._flags:
            return VisibleCell("flagged")
        if cell in self._revealed:
            return VisibleCell("revealed", self.adjacent_mines(cell))
        return VisibleCell("covered")

    def visible_snapshot(self) -> list[list[dict[str, int | str | None]]]:
        """Return JSON-compatible state without hidden mine locations."""

        return [
            [
                {
                    "state": visible.state,
                    "adjacent_mines": visible.adjacent_mines,
                }
                for visible in (self.visible_cell((row, col)) for col in range(self.config.width))
            ]
            for row in range(self.config.height)
        ]

    def is_solved(self) -> bool:
        return self._status is GameStatus.WON
