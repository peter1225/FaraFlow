"""Canonical Minesweeper trajectory schema (v3)."""

from __future__ import annotations

from typing import Any, Literal

from faraflow.model.coordinate_adapter import (
    ObservationGeometry,
    css_to_image_px,
    css_to_normalized_1000,
)
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .engine import Cell
from .geometry import BoardRectCss


class CaptureRectCss(BaseModel):
    model_config = ConfigDict(extra="forbid")

    x: float
    y: float
    width: float
    height: float


class ObservationMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    image_path: str
    viewport_css: tuple[int, int]
    capture_rect_css: CaptureRectCss
    input_image_size_px: tuple[int, int]
    device_scale_factor: float = Field(gt=0)
    screenshot_scale: Literal["css", "device"]

    def to_geometry(self) -> ObservationGeometry:
        return ObservationGeometry(
            viewport_css_width=self.viewport_css[0],
            viewport_css_height=self.viewport_css[1],
            image_width_px=self.input_image_size_px[0],
            image_height_px=self.input_image_size_px[1],
            capture_x_css=self.capture_rect_css.x,
            capture_y_css=self.capture_rect_css.y,
            capture_width_css=self.capture_rect_css.width,
            capture_height_css=self.capture_rect_css.height,
            device_scale_factor=self.device_scale_factor,
            screenshot_scale=self.screenshot_scale,
        )


class LegalAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["left_click", "right_click"]
    target_cell: tuple[int, int]


class TargetMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_cell: tuple[int, int]
    coordinate_css: tuple[float, float]
    coordinate_image_px: tuple[float, float]
    coordinate_norm_1000: tuple[float, float]
    teacher_action: Literal["left_click", "right_click"]
    legal_actions: list[LegalAction] = Field(min_length=1)

    @model_validator(mode="after")
    def target_is_legal(self) -> TargetMetadata:
        if not any(
            action.action == self.teacher_action and action.target_cell == self.target_cell
            for action in self.legal_actions
        ):
            raise ValueError("teacher target must be present in legal_actions")
        return self


class TeacherMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rule_id: str = Field(min_length=1)
    reasoning: str = Field(min_length=1)


class CanonicalRecord(BaseModel):
    """One screenshot/action pair suitable for validation and conversion."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["fara-mines-v3"] = "fara-mines-v3"
    trajectory_id: str = Field(min_length=1)
    step_id: int = Field(ge=0)
    seed: int
    difficulty: str = Field(min_length=1)
    observation: ObservationMetadata
    target: TargetMetadata
    teacher: TeacherMetadata
    supervision_only: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_target(
        cls,
        *,
        trajectory_id: str,
        step_id: int,
        seed: int,
        difficulty: str,
        observation: ObservationMetadata,
        board_rect: BoardRectCss,
        target_cell: Cell,
        teacher_action: Literal["left_click", "right_click"],
        legal_actions: list[LegalAction],
        teacher: TeacherMetadata,
        supervision_only: dict[str, Any] | None = None,
    ) -> CanonicalRecord:
        geometry = observation.to_geometry()
        coordinate_css = board_rect.cell_center_css(target_cell)
        coordinate_image_px = css_to_image_px(coordinate_css, geometry)
        coordinate_norm_1000 = css_to_normalized_1000(coordinate_css, geometry)
        target = TargetMetadata(
            target_cell=target_cell,
            coordinate_css=coordinate_css,
            coordinate_image_px=coordinate_image_px,
            coordinate_norm_1000=coordinate_norm_1000,
            teacher_action=teacher_action,
            legal_actions=legal_actions,
        )
        return cls(
            trajectory_id=trajectory_id,
            step_id=step_id,
            seed=seed,
            difficulty=difficulty,
            observation=observation,
            target=target,
            teacher=teacher,
            supervision_only=supervision_only or {},
        )
