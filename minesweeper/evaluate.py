"""Grounding metrics for one Canonical V3 action."""

from __future__ import annotations

from dataclasses import dataclass
from math import hypot

from faraflow.model.coordinate_adapter import (
    CoordinateMode,
    ObservationGeometry,
    adapt_action,
)
from faraflow.model.fara_protocol import ComputerAction, RawComputerAction

from .engine import Cell
from .geometry import BoardRectCss
from .schema import TargetMetadata


@dataclass(frozen=True)
class GroundingEvaluation:
    predicted_action: str
    predicted_coordinate_css: tuple[float, float] | None
    predicted_cell: Cell | None
    target_cell_hit: bool
    legal_action: bool
    pixel_error: float | None


def css_to_cell(coordinate_css: tuple[float, float], board_rect: BoardRectCss) -> Cell | None:
    x, y = coordinate_css
    if not (
        board_rect.x <= x < board_rect.x + board_rect.width
        and board_rect.y <= y < board_rect.y + board_rect.height
    ):
        return None
    col = int((x - board_rect.x) / board_rect.cell_width)
    row = int((y - board_rect.y) / board_rect.cell_height)
    return row, col


def evaluate_runtime_action(
    action: ComputerAction,
    *,
    target: TargetMetadata,
    board_rect: BoardRectCss,
) -> GroundingEvaluation:
    coordinate_css = action.coordinate
    predicted_cell = css_to_cell(coordinate_css, board_rect) if coordinate_css else None
    legal_action = any(
        item.action == action.action and item.target_cell == predicted_cell
        for item in target.legal_actions
    )
    pixel_error = (
        hypot(
            coordinate_css[0] - target.coordinate_css[0],
            coordinate_css[1] - target.coordinate_css[1],
        )
        if coordinate_css is not None
        else None
    )
    return GroundingEvaluation(
        predicted_action=action.action,
        predicted_coordinate_css=coordinate_css,
        predicted_cell=predicted_cell,
        target_cell_hit=predicted_cell == target.target_cell,
        legal_action=legal_action,
        pixel_error=pixel_error,
    )


def evaluate_raw_action(
    raw_action: RawComputerAction,
    *,
    mode: CoordinateMode | str,
    geometry: ObservationGeometry,
    target: TargetMetadata,
    board_rect: BoardRectCss,
) -> GroundingEvaluation:
    runtime_action = adapt_action(raw_action, mode, geometry)
    return evaluate_runtime_action(runtime_action, target=target, board_rect=board_rect)
