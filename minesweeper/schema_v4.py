"""Canonical V4 schema for certainty-first Minesweeper supervision.

V4 adds an explicit ``terminate`` target for positions that have no logically
forced move.  Coordinate-bearing actions remain compatible with FaraFlow's
normalized-1000 computer-use protocol.
"""

from __future__ import annotations

from typing import Any, Literal

from faraflow.model.coordinate_adapter import css_to_image_px, css_to_normalized_1000
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .engine import Cell
from .geometry import BoardRectCss
from .schema import ObservationMetadata

MinesweeperActionV4 = Literal["left_click", "right_click", "terminate"]
CertaintyV4 = Literal["safe", "mine", "ambiguous", "first_click"]


class BoardMetadataV4(BaseModel):
    model_config = ConfigDict(extra="forbid")

    width: int = Field(gt=0)
    height: int = Field(gt=0)
    mines: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_mine_count(self) -> BoardMetadataV4:
        if self.mines >= self.width * self.height:
            raise ValueError("mines must be smaller than the number of cells")
        return self


class LegalActionV4(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["left_click", "right_click"]
    target_cell: Cell
    rule_id: str = Field(min_length=1)


class TargetMetadataV4(BaseModel):
    model_config = ConfigDict(extra="forbid")

    teacher_action: MinesweeperActionV4
    target_cell: Cell | None = None
    coordinate_css: tuple[float, float] | None = None
    coordinate_image_px: tuple[float, float] | None = None
    coordinate_norm_1000: tuple[float, float] | None = None
    answer: str | None = None
    legal_actions: list[LegalActionV4] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_target_contract(self) -> TargetMetadataV4:
        if self.teacher_action == "terminate":
            if any(
                value is not None
                for value in (
                    self.target_cell,
                    self.coordinate_css,
                    self.coordinate_image_px,
                    self.coordinate_norm_1000,
                )
            ):
                raise ValueError("terminate must not contain a target coordinate")
            if not self.answer or not self.answer.strip():
                raise ValueError("terminate requires a non-empty answer")
            if self.legal_actions:
                raise ValueError("terminate is valid only when legal_actions is empty")
            return self

        if self.answer is not None:
            raise ValueError("coordinate actions must not contain an answer")
        if any(
            value is None
            for value in (
                self.target_cell,
                self.coordinate_css,
                self.coordinate_image_px,
                self.coordinate_norm_1000,
            )
        ):
            raise ValueError("coordinate actions require cell and coordinate fields")
        if not any(
            action.action == self.teacher_action and action.target_cell == self.target_cell
            for action in self.legal_actions
        ):
            raise ValueError("teacher target must be present in legal_actions")
        return self


class TeacherMetadataV4(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rule_id: str = Field(min_length=1)
    certainty: CertaintyV4
    reasoning: str = Field(min_length=1)
    proof: dict[str, Any] = Field(default_factory=dict)


class CanonicalRecordV4(BaseModel):
    """One visible board state paired with one certainty-first teacher action."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["fara-mines-v4"] = "fara-mines-v4"
    trajectory_id: str = Field(min_length=1)
    step_id: int = Field(ge=0)
    seed: int
    difficulty: str = Field(min_length=1)
    category: Literal[
        "forced_safe",
        "forced_mine",
        "constraint",
        "ambiguous",
        "edge",
    ]
    board: BoardMetadataV4
    observation: ObservationMetadata
    target: TargetMetadataV4
    teacher: TeacherMetadataV4
    supervision_only: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_decision(
        cls,
        *,
        trajectory_id: str,
        step_id: int,
        seed: int,
        difficulty: str,
        category: Literal[
            "forced_safe",
            "forced_mine",
            "constraint",
            "ambiguous",
            "edge",
        ],
        board: BoardMetadataV4,
        observation: ObservationMetadata,
        board_rect: BoardRectCss,
        teacher_action: MinesweeperActionV4,
        target_cell: Cell | None,
        answer: str | None,
        legal_actions: list[LegalActionV4],
        teacher: TeacherMetadataV4,
        supervision_only: dict[str, Any] | None = None,
    ) -> CanonicalRecordV4:
        if teacher_action == "terminate":
            target = TargetMetadataV4(
                teacher_action="terminate",
                answer=answer,
                legal_actions=[],
            )
        else:
            if target_cell is None:
                raise ValueError("coordinate action requires target_cell")
            geometry = observation.to_geometry()
            coordinate_css = board_rect.cell_center_css(target_cell)
            target = TargetMetadataV4(
                teacher_action=teacher_action,
                target_cell=target_cell,
                coordinate_css=coordinate_css,
                coordinate_image_px=css_to_image_px(coordinate_css, geometry),
                coordinate_norm_1000=css_to_normalized_1000(coordinate_css, geometry),
                legal_actions=legal_actions,
            )
        return cls(
            trajectory_id=trajectory_id,
            step_id=step_id,
            seed=seed,
            difficulty=difficulty,
            category=category,
            board=board,
            observation=observation,
            target=target,
            teacher=teacher,
            supervision_only=supervision_only or {},
        )

    def with_layout(
        self,
        *,
        observation: ObservationMetadata,
        board_rect: BoardRectCss,
        layout_metadata: dict[str, Any],
    ) -> CanonicalRecordV4:
        """Return a copy with coordinates recomputed for a rendered layout."""

        supervision = dict(self.supervision_only)
        supervision["board_rect_css"] = {
            "x": board_rect.x,
            "y": board_rect.y,
            "width": board_rect.width,
            "height": board_rect.height,
            "rows": board_rect.rows,
            "cols": board_rect.cols,
        }
        supervision["layout"] = layout_metadata
        if self.target.teacher_action == "terminate":
            target = self.target.model_copy(deep=True)
        else:
            if self.target.target_cell is None:
                raise ValueError("coordinate action has no target cell")
            geometry = observation.to_geometry()
            coordinate_css = board_rect.cell_center_css(self.target.target_cell)
            target = self.target.model_copy(
                update={
                    "coordinate_css": coordinate_css,
                    "coordinate_image_px": css_to_image_px(coordinate_css, geometry),
                    "coordinate_norm_1000": css_to_normalized_1000(
                        coordinate_css, geometry
                    ),
                }
            )
        return self.model_copy(
            update={
                "observation": observation,
                "target": target,
                "supervision_only": supervision,
            }
        )
