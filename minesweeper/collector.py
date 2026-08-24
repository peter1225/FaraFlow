"""Deterministic local trajectory collector for the first Grounding Gate batch."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from .engine import BoardConfig, GameStatus, MinesweeperBoard
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
class CollectedExample:
    record: CanonicalRecord
    image: bytes


@dataclass(frozen=True)
class CollectionStats:
    requested_records: int
    records_written: int
    trajectories_attempted: int
    trajectories_with_actions: int


def _observation(image_path: str) -> ObservationMetadata:
    return ObservationMetadata(
        image_path=image_path,
        viewport_css=(1440, 900),
        capture_rect_css=CaptureRectCss(x=0, y=0, width=1440, height=900),
        input_image_size_px=(1440, 900),
        device_scale_factor=1.0,
        screenshot_scale="css",
    )


def collect_trajectory(
    *,
    seed: int,
    config: BoardConfig = BoardConfig(),
    max_steps: int = 100,
) -> list[CollectedExample]:
    """Collect forced-action examples until the board has no deterministic move."""

    board = MinesweeperBoard(config, seed=seed)
    first_click = (config.height // 2, config.width // 2)
    board.click(first_click)
    board_rect = BoardRectCss(360, 160, 630, 560, config.height, config.width)
    examples: list[CollectedExample] = []

    for step_id in range(max_steps):
        if board.status in {GameStatus.WON, GameStatus.LOST}:
            break
        actions = infer_forced_actions(board)
        decision = choose_teacher_action(actions)
        if decision is None:
            break
        trajectory_id = f"seed-{seed:08d}"
        image_path = f"images/{trajectory_id}/step_{step_id:04d}.png"
        legal_actions = [
            LegalAction(action=item.action, target_cell=item.target_cell)
            for item in decision.legal_actions
        ]
        record = CanonicalRecord.from_target(
            trajectory_id=trajectory_id,
            step_id=step_id,
            seed=seed,
            difficulty=(
                "beginner"
                if config.width == 9 and config.height == 9
                else f"{config.width}x{config.height}"
            ),
            observation=_observation(image_path),
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
        examples.append(
            CollectedExample(
                record=record,
                image=render_board_png(board, board_rect),
            )
        )
        if decision.action.action == "left_click":
            board.click(decision.action.target_cell)
        else:
            board.toggle_flag(decision.action.target_cell)
    return examples


def collect_dataset(
    root: Path,
    *,
    requested_records: int = 100,
    seed_start: int = 0,
    max_attempts: int | None = None,
    max_steps: int = 100,
    config: BoardConfig = BoardConfig(),
) -> CollectionStats:
    """Write deterministic PNG + JSONL data and return an auditable manifest."""

    if requested_records <= 0:
        raise ValueError("requested_records must be positive")
    attempts_limit = max_attempts or requested_records * 20
    records_path = root / "records.jsonl"
    records_path.parent.mkdir(parents=True, exist_ok=True)
    records_path.write_text("", encoding="utf-8")
    records_written = 0
    trajectories_with_actions = 0
    attempted = 0
    with records_path.open("a", encoding="utf-8") as records_file:
        while records_written < requested_records and attempted < attempts_limit:
            seed = seed_start + attempted
            attempted += 1
            examples = collect_trajectory(seed=seed, config=config, max_steps=max_steps)
            if not examples:
                continue
            trajectories_with_actions += 1
            for example in examples:
                if records_written >= requested_records:
                    break
                image_path = root / example.record.observation.image_path
                image_path.parent.mkdir(parents=True, exist_ok=True)
                image_path.write_bytes(example.image)
                records_file.write(
                    json.dumps(example.record.model_dump(mode="json"), ensure_ascii=False)
                    + "\n"
                )
                records_written += 1
    if records_written < requested_records:
        raise RuntimeError(
            f"only collected {records_written}/{requested_records} records after "
            f"{attempted} trajectory attempts"
        )
    manifest = {
        "schema_version": "fara-mines-v3",
        "data_source": "deterministic_synthetic_engine",
        "render_backend": "pillow",
        "generator": "minesweeper.collector.collect_dataset",
        "generator_version": "1",
        "requested_records": requested_records,
        "records_written": records_written,
        "trajectories_attempted": attempted,
        "trajectories_with_actions": trajectories_with_actions,
        "seed_start": seed_start,
        "board_config": asdict(config),
        "hidden_state_policy": "mine_map_only_in_supervision_only",
        "max_steps_per_trajectory": max_steps,
        "viewport_css": [1440, 900],
        "input_image_size_px": [1440, 900],
        "device_scale_factor": 1.0,
        "screenshot_scale": "css",
    }
    (root / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return CollectionStats(
        requested_records=requested_records,
        records_written=records_written,
        trajectories_attempted=attempted,
        trajectories_with_actions=trajectories_with_actions,
    )
