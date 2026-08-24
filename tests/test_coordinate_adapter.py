from __future__ import annotations

import pytest
from faraflow.model.coordinate_adapter import (
    CoordinateMode,
    ObservationGeometry,
    adapt_action,
    model_to_css,
)
from faraflow.model.fara_protocol import RawComputerAction


def test_normalized_coordinate_maps_to_css_pixels() -> None:
    geometry = ObservationGeometry.full_viewport(1440, 900)
    assert model_to_css((500, 500), CoordinateMode.NORMALIZED_1000, geometry) == (
        720.0,
        450.0,
    )


def test_pixel_coordinate_maps_to_css_pixels() -> None:
    geometry = ObservationGeometry.full_viewport(1440, 900)
    assert model_to_css((1074, 437), CoordinateMode.PIXEL, geometry) == (
        1074.0,
        437.0,
    )


def test_cropped_device_scale_image_maps_back_to_css() -> None:
    geometry = ObservationGeometry(
        viewport_css_width=1440,
        viewport_css_height=900,
        image_width_px=800,
        image_height_px=400,
        capture_x_css=100,
        capture_y_css=50,
        capture_width_css=400,
        capture_height_css=200,
        device_scale_factor=2.0,
        screenshot_scale="device",
    )
    assert model_to_css((400, 200), CoordinateMode.PIXEL, geometry) == (300.0, 150.0)


@pytest.mark.parametrize(
    ("coordinate", "mode"),
    [
        ((1440, 0), CoordinateMode.PIXEL),
        ((-1, 10), CoordinateMode.PIXEL),
        ((1001, 10), CoordinateMode.NORMALIZED_1000),
        ((500, -1), CoordinateMode.NORMALIZED_1000),
    ],
)
def test_coordinate_bounds_are_checked_after_raw_parse(coordinate, mode) -> None:
    geometry = ObservationGeometry.full_viewport(1440, 900)
    with pytest.raises(ValueError):
        model_to_css(coordinate, mode, geometry)


def test_adapt_action_returns_runtime_css_coordinate() -> None:
    raw = RawComputerAction(action="left_click", coordinate=(500, 500))
    action = adapt_action(
        raw,
        CoordinateMode.NORMALIZED_1000,
        ObservationGeometry.full_viewport(1440, 900),
    )
    assert action.coordinate == (720.0, 450.0)
