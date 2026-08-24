"""Coordinate conversion between Fara model space and browser CSS pixels.

The model may emit either screenshot pixels or FaraFlow's legacy 0..1000
normalized coordinates.  The browser executor should receive CSS pixels only.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum

from .fara_protocol import ComputerAction, RawComputerAction


class CoordinateMode(str, Enum):
    PIXEL = "pixel"
    NORMALIZED_1000 = "normalized_1000"


@dataclass(frozen=True)
class ObservationGeometry:
    """Geometry of the screenshot presented to the model.

    ``capture_rect_css`` describes where the screenshot lies in the browser
    viewport.  For a full-page viewport screenshot it is simply (0, 0, W, H).
    ``image_width_px`` and ``image_height_px`` refer to the actual PNG/JPEG
    dimensions sent to the model, not an internal processor tensor.
    """

    viewport_css_width: int
    viewport_css_height: int
    image_width_px: int
    image_height_px: int
    capture_x_css: float = 0.0
    capture_y_css: float = 0.0
    capture_width_css: float | None = None
    capture_height_css: float | None = None
    device_scale_factor: float = 1.0
    screenshot_scale: str = "css"

    def __post_init__(self) -> None:
        if self.viewport_css_width <= 0 or self.viewport_css_height <= 0:
            raise ValueError("viewport dimensions must be positive")
        if self.image_width_px <= 0 or self.image_height_px <= 0:
            raise ValueError("image dimensions must be positive")
        if self.device_scale_factor <= 0 or not math.isfinite(self.device_scale_factor):
            raise ValueError("device_scale_factor must be a positive finite number")
        capture_width = self.capture_width_css
        capture_height = self.capture_height_css
        if capture_width is None:
            capture_width = self.viewport_css_width
            object.__setattr__(self, "capture_width_css", capture_width)
        if capture_height is None:
            capture_height = self.viewport_css_height
            object.__setattr__(self, "capture_height_css", capture_height)
        if capture_width <= 0 or capture_height <= 0:
            raise ValueError("capture dimensions must be positive")
        if self.screenshot_scale not in {"css", "device"}:
            raise ValueError("screenshot_scale must be 'css' or 'device'")

    @classmethod
    def full_viewport(cls, width: int, height: int) -> ObservationGeometry:
        return cls(
            viewport_css_width=width,
            viewport_css_height=height,
            image_width_px=width,
            image_height_px=height,
            capture_width_css=width,
            capture_height_css=height,
        )


def _validate_finite_point(point: tuple[float, float]) -> None:
    if not all(math.isfinite(float(value)) for value in point):
        raise ValueError("coordinates must contain finite numbers")


def model_to_css(
    coordinate: tuple[float, float],
    mode: CoordinateMode | str,
    geometry: ObservationGeometry,
) -> tuple[float, float]:
    """Convert a model coordinate to a Playwright CSS-pixel coordinate."""

    mode = CoordinateMode(mode)
    x, y = (float(coordinate[0]), float(coordinate[1]))
    _validate_finite_point((x, y))

    if mode is CoordinateMode.NORMALIZED_1000:
        if not (0.0 <= x <= 1000.0 and 0.0 <= y <= 1000.0):
            raise ValueError("normalized coordinates must be in the 0..1000 range")
        image_x = x / 1000.0 * geometry.image_width_px
        image_y = y / 1000.0 * geometry.image_height_px
    else:
        if not (0.0 <= x < geometry.image_width_px and 0.0 <= y < geometry.image_height_px):
            raise ValueError(
                "pixel coordinates must be inside the submitted screenshot dimensions"
            )
        image_x = x
        image_y = y

    capture_width = geometry.capture_width_css
    capture_height = geometry.capture_height_css
    assert capture_width is not None
    assert capture_height is not None
    css_x = geometry.capture_x_css + (
        image_x / geometry.image_width_px * capture_width
    )
    css_y = geometry.capture_y_css + (
        image_y / geometry.image_height_px * capture_height
    )
    if not (
        0.0 <= css_x <= geometry.viewport_css_width
        and 0.0 <= css_y <= geometry.viewport_css_height
    ):
        raise ValueError("converted CSS coordinate is outside the viewport")
    return css_x, css_y


def css_to_image_px(
    coordinate_css: tuple[float, float],
    geometry: ObservationGeometry,
) -> tuple[float, float]:
    """Convert a browser CSS-pixel point into submitted-image pixels."""

    x_css, y_css = (float(coordinate_css[0]), float(coordinate_css[1]))
    _validate_finite_point((x_css, y_css))
    capture_width = geometry.capture_width_css
    capture_height = geometry.capture_height_css
    assert capture_width is not None
    assert capture_height is not None
    if not (
        geometry.capture_x_css <= x_css <= geometry.capture_x_css + capture_width
        and geometry.capture_y_css <= y_css <= geometry.capture_y_css + capture_height
    ):
        raise ValueError("CSS coordinate is outside the submitted capture rectangle")
    image_x = (
        (x_css - geometry.capture_x_css)
        / capture_width
        * geometry.image_width_px
    )
    image_y = (
        (y_css - geometry.capture_y_css)
        / capture_height
        * geometry.image_height_px
    )
    return image_x, image_y


def css_to_normalized_1000(
    coordinate_css: tuple[float, float],
    geometry: ObservationGeometry,
) -> tuple[float, float]:
    """Convert a browser CSS-pixel point into Fara's normalized coordinates."""

    image_x, image_y = css_to_image_px(coordinate_css, geometry)
    return (
        image_x / geometry.image_width_px * 1000.0,
        image_y / geometry.image_height_px * 1000.0,
    )


def image_px_to_normalized_1000(
    coordinate_image_px: tuple[float, float],
    geometry: ObservationGeometry,
) -> tuple[float, float]:
    """Convert submitted-image pixels into Fara's normalized coordinates."""

    x, y = (float(coordinate_image_px[0]), float(coordinate_image_px[1]))
    _validate_finite_point((x, y))
    if not (
        0.0 <= x <= geometry.image_width_px
        and 0.0 <= y <= geometry.image_height_px
    ):
        raise ValueError("image coordinate is outside the submitted image dimensions")
    return (
        x / geometry.image_width_px * 1000.0,
        y / geometry.image_height_px * 1000.0,
    )


def adapt_action(
    raw_action: RawComputerAction,
    mode: CoordinateMode | str,
    geometry: ObservationGeometry,
) -> ComputerAction:
    """Convert a raw model action into the runtime CSS-pixel action model."""

    values = raw_action.model_dump(mode="python", exclude_none=True)
    if raw_action.coordinate is not None:
        values["coordinate"] = model_to_css(raw_action.coordinate, mode, geometry)
    return ComputerAction.model_validate(values)
