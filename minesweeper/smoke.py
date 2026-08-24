"""Deterministic one-step sample for the local Grounding Gate."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .engine import BoardConfig, MinesweeperBoard
from .geometry import BoardRectCss
from .render import render_board_png
from .schema import (
    CanonicalRecord,
    CaptureRectCss,
    LegalAction,
    ObservationMetadata,
    TeacherMetadata,
)
from .solver import choose_teacher_action, infer_forced_actions


@dataclass(frozen=True)
class SmokeSample:
    board: MinesweeperBoard
    image: bytes
    record: CanonicalRecord


def make_smoke_sample() -> SmokeSample:
    """Build a visible state with a forced safe click and deterministic labels."""

    config = BoardConfig(width=9, height=9, mines=10)
    mines = {
        (0, 1),
        (4, 4),
        (5, 5),
        (5, 6),
        (6, 5),
        (6, 6),
        (7, 7),
        (8, 8),
        (0, 8),
        (8, 0),
    }
    board = MinesweeperBoard.from_truth(
        config,
        mines,
        revealed={(1, 1)},
        flags={(0, 1)},
        seed=20260818,
    )
    actions = infer_forced_actions(board)
    decision = choose_teacher_action(actions)
    if decision is None:
        raise RuntimeError("smoke fixture did not produce a forced action")

    board_rect = BoardRectCss(360, 160, 630, 560, config.height, config.width)
    observation = ObservationMetadata(
        image_path="images/smoke/step_0001.png",
        viewport_css=(1440, 900),
        capture_rect_css=CaptureRectCss(x=0, y=0, width=1440, height=900),
        input_image_size_px=(1440, 900),
        device_scale_factor=1.0,
        screenshot_scale="css",
    )
    legal_actions = [
        LegalAction(action=item.action, target_cell=item.target_cell)
        for item in decision.legal_actions
    ]
    record = CanonicalRecord.from_target(
        trajectory_id="smoke-20260818-0001",
        step_id=1,
        seed=board.seed,
        difficulty="beginner",
        observation=observation,
        board_rect=board_rect,
        target_cell=decision.action.target_cell,
        teacher_action=decision.action.action,
        legal_actions=legal_actions,
        teacher=TeacherMetadata(
            rule_id=decision.action.rule_id,
            reasoning=decision.action.reasoning,
        ),
        supervision_only={
            "visible_state": board.visible_snapshot(),
            "mine_map": sorted(board.mine_map),
            "board_rect_css": {
                "x": board_rect.x,
                "y": board_rect.y,
                "width": board_rect.width,
                "height": board_rect.height,
                "rows": board_rect.rows,
                "cols": board_rect.cols,
            },
        },
    )
    return SmokeSample(
        board=board,
        image=render_board_png(board, board_rect),
        record=record,
    )


def write_smoke_sample(root: Path) -> CanonicalRecord:
    """Write one PNG + JSON record under an ignored local data directory."""

    sample = make_smoke_sample()
    image_path = root / "images" / "smoke" / "step_0001.png"
    record_path = root / "records" / "smoke-20260818-0001.json"
    image_path.parent.mkdir(parents=True, exist_ok=True)
    record_path.parent.mkdir(parents=True, exist_ok=True)
    image_path.write_bytes(sample.image)
    record_path.write_text(
        sample.record.model_dump_json(indent=2),
        encoding="utf-8",
    )
    return sample.record
