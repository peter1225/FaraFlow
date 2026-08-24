from __future__ import annotations

import json

import pytest
from faraflow.model.coordinate_adapter import ObservationGeometry, css_to_image_px
from faraflow.model.fara_protocol import RawComputerAction
from PIL import Image

from minesweeper.browser_html import visible_state_to_html
from minesweeper.collector import collect_dataset, collect_trajectory
from minesweeper.engine import BoardConfig, MinesweeperBoard
from minesweeper.evaluate import css_to_cell, evaluate_raw_action
from minesweeper.geometry import BoardRectCss
from minesweeper.render import render_board_png
from minesweeper.schema import (
    CanonicalRecord,
    CaptureRectCss,
    LegalAction,
    ObservationMetadata,
    TeacherMetadata,
)
from minesweeper.smoke import make_smoke_sample, write_smoke_sample
from minesweeper.solver import choose_teacher_action, infer_forced_actions
from scripts.export_fara_sft import export_dataset, serialize_tool_call
from scripts.split_minesweeper_dataset import choose_validation_trajectories


def test_first_click_is_safe_and_seed_is_reproducible() -> None:
    config = BoardConfig()
    first = MinesweeperBoard(config, seed=42)
    second = MinesweeperBoard(config, seed=42)

    first.click((0, 0))
    second.click((0, 0))

    assert (0, 0) not in first.mine_map
    assert first.mine_map == second.mine_map
    assert first.visible_snapshot() == second.visible_snapshot()


def test_visible_snapshot_hides_mine_truth() -> None:
    board = MinesweeperBoard.from_truth(
        BoardConfig(width=3, height=3, mines=1),
        {(0, 0)},
        revealed={(1, 1)},
        flags={(0, 0)},
    )

    snapshot = board.visible_snapshot()

    assert snapshot[0][0] == {"state": "flagged", "adjacent_mines": None}
    assert all("mine" not in cell for row in snapshot for cell in row)


def test_solver_infers_safe_actions_and_deterministic_teacher_choice() -> None:
    board = MinesweeperBoard.from_truth(
        BoardConfig(width=3, height=3, mines=1),
        {(0, 0)},
        revealed={(1, 1)},
        flags={(0, 0)},
    )

    actions = infer_forced_actions(board)
    decision = choose_teacher_action(actions)

    assert decision is not None
    assert decision.action.action == "left_click"
    assert decision.action.target_cell == (0, 1)
    assert len(decision.legal_actions) == 7


def test_solver_infers_a_forced_mine() -> None:
    board = MinesweeperBoard.from_truth(
        BoardConfig(width=3, height=3, mines=3),
        {(0, 1), (1, 0), (1, 1)},
        revealed={(0, 0)},
        flags={(1, 0), (1, 1)},
    )

    actions = infer_forced_actions(board)

    assert len(actions) == 1
    assert actions[0].action == "right_click"
    assert actions[0].target_cell == (0, 1)


def test_canonical_coordinates_keep_css_image_and_normalized_spaces() -> None:
    sample = make_smoke_sample()
    target = sample.record.target

    assert target.target_cell == (0, 0)
    assert target.coordinate_css == pytest.approx((395.0, 191.1111111111))
    assert target.coordinate_image_px == pytest.approx(target.coordinate_css)
    assert target.coordinate_norm_1000 == pytest.approx(
        (274.3055555556, 212.3456790123)
    )
    assert sample.record.target.legal_actions


def test_cropped_geometry_round_trip_matches_css_point() -> None:
    geometry = ObservationGeometry(
        viewport_css_width=1440,
        viewport_css_height=900,
        image_width_px=2880,
        image_height_px=1800,
        capture_x_css=120,
        capture_y_css=40,
        capture_width_css=720,
        capture_height_css=450,
        device_scale_factor=2.0,
        screenshot_scale="device",
    )
    css = (480.0, 265.0)
    image = css_to_image_px(css, geometry)

    assert image == pytest.approx((1440.0, 900.0))


def test_grounding_evaluator_accepts_any_legal_cell() -> None:
    sample = make_smoke_sample()
    board_rect = BoardRectCss(360, 160, 630, 560, 9, 9)
    runtime_coordinate = board_rect.cell_center_css((0, 2))
    raw = RawComputerAction(action="left_click", coordinate=runtime_coordinate)
    result = evaluate_raw_action(
        raw,
        mode="pixel",
        geometry=ObservationGeometry.full_viewport(1440, 900),
        target=sample.record.target,
        board_rect=board_rect,
    )

    assert css_to_cell(runtime_coordinate, board_rect) == (0, 2)
    assert result.target_cell_hit is False
    assert result.legal_action is True


def test_smoke_renderer_outputs_1440x900_png() -> None:
    sample = make_smoke_sample()
    image = Image.open(__import__("io").BytesIO(sample.image))
    assert image.size == (1440, 900)
    assert image.format == "PNG"


def test_renderer_highlight_is_visual_only() -> None:
    sample = make_smoke_sample()
    board_rect = BoardRectCss(360, 160, 630, 560, 9, 9)
    highlighted = render_board_png(sample.board, board_rect, highlight_cell=(2, 2))

    assert highlighted != sample.image
    assert Image.open(__import__("io").BytesIO(highlighted)).size == (1440, 900)


def test_browser_html_uses_visible_state_only() -> None:
    sample = make_smoke_sample()
    board_rect = BoardRectCss(360, 160, 630, 560, 9, 9)
    html = visible_state_to_html(sample.board.visible_snapshot(), board_rect)

    assert 'data-cell="0,1"' in html
    assert "mine_map" not in html
    assert "⚑" in html


def test_smoke_record_round_trips_as_json(tmp_path) -> None:
    record = write_smoke_sample(tmp_path)
    restored = json.loads(
        (tmp_path / "records" / "smoke-20260818-0001.json").read_text(encoding="utf-8")
    )

    assert restored["schema_version"] == "fara-mines-v3"
    assert restored["target"]["teacher_action"] == record.target.teacher_action
    assert (tmp_path / "images" / "smoke" / "step_0001.png").exists()


def test_canonical_schema_rejects_teacher_target_not_in_legal_set() -> None:
    with pytest.raises(ValueError, match="teacher target"):
        CanonicalRecord(
            trajectory_id="bad",
            step_id=0,
            seed=1,
            difficulty="beginner",
            observation=ObservationMetadata(
                image_path="x.png",
                viewport_css=(1440, 900),
                capture_rect_css=CaptureRectCss(x=0, y=0, width=1440, height=900),
                input_image_size_px=(1440, 900),
                device_scale_factor=1.0,
                screenshot_scale="css",
            ),
            target={
                "target_cell": (0, 0),
                "coordinate_css": (10, 10),
                "coordinate_image_px": (10, 10),
                "coordinate_norm_1000": (6.9, 11.1),
                "teacher_action": "left_click",
                "legal_actions": [LegalAction(action="right_click", target_cell=(1, 1))],
            },
            teacher=TeacherMetadata(rule_id="x", reasoning="x"),
        )


def test_collector_writes_reproducible_jsonl_batch(tmp_path) -> None:
    examples = next(
        (
            candidate
            for candidate in (collect_trajectory(seed=seed) for seed in range(20))
            if candidate
        ),
        [],
    )
    assert examples
    assert examples[0].record.schema_version == "fara-mines-v3"

    stats = collect_dataset(tmp_path / "dataset", requested_records=5, seed_start=0)

    assert stats.records_written == 5
    lines = (tmp_path / "dataset" / "records.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 5
    assert (tmp_path / "dataset" / "manifest.json").exists()


def test_validation_split_selects_whole_trajectories() -> None:
    records = [collect_trajectory(seed=seed)[0].record for seed in (7, 10, 11)]
    validation = choose_validation_trajectories(records, validation_fraction=1 / 3)

    assert len(validation) == 1
    assert validation.isdisjoint(
        {record.trajectory_id for record in records if record.trajectory_id not in validation}
    )


def test_fara_sft_export_is_parser_compatible_and_hides_truth(tmp_path) -> None:
    record = write_smoke_sample(tmp_path)
    records_path = tmp_path / "records.jsonl"
    records_path.write_text(record.model_dump_json() + "\n", encoding="utf-8")
    output_path = tmp_path / "sft.jsonl"
    manifest = export_dataset(
        records_path,
        output_path,
        image_root=tmp_path,
    )
    row = json.loads(output_path.read_text(encoding="utf-8").splitlines()[0])

    assert manifest["coordinate_mode"] == "normalized_1000"
    assert row["messages"][2]["content"].startswith("<think>")
    assert "mine_map" not in json.dumps(row["messages"], ensure_ascii=False)
    assert '"action":"left_click"' in row["messages"][2]["content"]
    assert '"answer":"done"' in serialize_tool_call(
        {"action": "terminate", "answer": "done"}
    )
    with pytest.raises(ValueError):
        serialize_tool_call({"action": "terminate"})
