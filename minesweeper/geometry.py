"""Board layout geometry used by rendering and Canonical V3 records."""

from __future__ import annotations

from dataclasses import dataclass

from .engine import Cell


@dataclass(frozen=True)
class BoardRectCss:
    x: float
    y: float
    width: float
    height: float
    rows: int
    cols: int

    def __post_init__(self) -> None:
        if self.width <= 0 or self.height <= 0:
            raise ValueError("board rectangle dimensions must be positive")
        if self.rows <= 0 or self.cols <= 0:
            raise ValueError("board rows and columns must be positive")

    @property
    def cell_width(self) -> float:
        return self.width / self.cols

    @property
    def cell_height(self) -> float:
        return self.height / self.rows

    def cell_center_css(self, cell: Cell) -> tuple[float, float]:
        row, col = cell
        if not (0 <= row < self.rows and 0 <= col < self.cols):
            raise ValueError(f"cell outside board rectangle: {cell}")
        return (
            self.x + (col + 0.5) * self.cell_width,
            self.y + (row + 0.5) * self.cell_height,
        )
