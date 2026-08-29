"""Replay and validate every Canonical V4 label before training."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from math import hypot
from pathlib import Path
from typing import Any

from faraflow.model.coordinate_adapter import css_to_normalized_1000

from minesweeper.engine import BoardConfig, GameStatus, MinesweeperBoard
from minesweeper.geometry import BoardRectCss
from minesweeper.schema_v4 import CanonicalRecordV4
from minesweeper.solver_v4 import analyze_forced_actions


def _load_split(root: Path, split: str) -> list[CanonicalRecordV4]:
    records_path = root / split / "records.jsonl"
    if not records_path.is_file():
        raise FileNotFoundError(records_path)
    records: list[CanonicalRecordV4] = []
    for line_number, line in enumerate(
        records_path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        if not line.strip():
            continue
        try:
            records.append(CanonicalRecordV4.model_validate(json.loads(line)))
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"invalid {split} record at line {line_number}") from exc
    if not records:
        raise ValueError(f"{split} split contains no records")
    return records


def _board_rect(record: CanonicalRecordV4) -> BoardRectCss:
    raw = record.supervision_only.get("board_rect_css")
    if not isinstance(raw, dict):
        raise ValueError("record has no board_rect_css")
    return BoardRectCss(
        x=float(raw["x"]),
        y=float(raw["y"]),
        width=float(raw["width"]),
        height=float(raw["height"]),
        rows=int(raw["rows"]),
        cols=int(raw["cols"]),
    )


def _replay_board(record: CanonicalRecordV4) -> MinesweeperBoard:
    visible = record.supervision_only.get("visible_state")
    mine_map = record.supervision_only.get("mine_map")
    if not isinstance(visible, list) or not isinstance(mine_map, list):
        raise ValueError("record has no replayable visible_state/mine_map")
    revealed: list[tuple[int, int]] = []
    flags: list[tuple[int, int]] = []
    for row_index, row in enumerate(visible):
        if not isinstance(row, list):
            raise ValueError("visible_state rows must be lists")
        for col_index, cell in enumerate(row):
            if not isinstance(cell, dict):
                raise ValueError("visible_state cells must be objects")
            state = cell.get("state")
            if state == "revealed":
                revealed.append((row_index, col_index))
            elif state == "flagged":
                flags.append((row_index, col_index))
            elif state != "covered":
                raise ValueError(f"unsupported cell state: {state}")
    mines = [tuple(int(value) for value in cell) for cell in mine_map]
    if record.teacher.rule_id != "first_click_safe" and len(mines) != record.board.mines:
        raise ValueError(
            f"mine_map has {len(mines)} mines, expected {record.board.mines}"
        )
    board = MinesweeperBoard.from_truth(
        BoardConfig(
            width=record.board.width,
            height=record.board.height,
            mines=record.board.mines,
        ),
        mines,
        revealed=revealed,
        flags=flags,
        status=(
            GameStatus.READY
            if record.teacher.rule_id == "first_click_safe"
            else GameStatus.ACTIVE
        ),
        seed=record.seed,
    )
    if board.visible_snapshot() != visible:
        raise ValueError("visible_state numbers do not match supervision-only mine truth")
    return board


def _validate_coordinate(record: CanonicalRecordV4, board_rect: BoardRectCss) -> bool:
    if record.target.teacher_action == "terminate":
        return record.target.coordinate_norm_1000 is None
    if record.target.target_cell is None or record.target.coordinate_css is None:
        return False
    expected_css = board_rect.cell_center_css(record.target.target_cell)
    if hypot(
        expected_css[0] - record.target.coordinate_css[0],
        expected_css[1] - record.target.coordinate_css[1],
    ) > 1e-6:
        return False
    expected_norm = css_to_normalized_1000(expected_css, record.observation.to_geometry())
    actual_norm = record.target.coordinate_norm_1000
    return actual_norm is not None and hypot(
        expected_norm[0] - actual_norm[0], expected_norm[1] - actual_norm[1]
    ) <= 1e-6


def _validate_split(root: Path, split: str) -> dict[str, Any]:
    records = _load_split(root, split)
    unsafe_clicks = 0
    false_flags = 0
    ambiguous_actions = 0
    hidden_truth_leaks = 0
    coordinate_errors = 0
    missing_images = 0
    action_counts: Counter[str] = Counter()
    category_counts: Counter[str] = Counter()

    for record in records:
        if not (root / split / record.observation.image_path).is_file():
            missing_images += 1
        model_facing = record.model_dump(mode="json", exclude={"supervision_only"})
        if "mine_map" in json.dumps(model_facing, ensure_ascii=False):
            hidden_truth_leaks += 1
        board_rect = _board_rect(record)
        if not _validate_coordinate(record, board_rect):
            coordinate_errors += 1
        board = _replay_board(record)
        action_counts[record.target.teacher_action] += 1
        category_counts[record.category] += 1

        if record.teacher.rule_id == "first_click_safe":
            if (
                record.target.teacher_action != "left_click"
                or record.target.target_cell is None
                or board.status is not GameStatus.READY
            ):
                ambiguous_actions += 1
            continue

        analysis = analyze_forced_actions(board)
        legal = {
            (action.action, action.target_cell) for action in analysis.legal_actions
        }
        if not analysis.complete or not analysis.consistent:
            ambiguous_actions += 1
            continue
        if record.target.teacher_action == "terminate":
            if legal or record.target.answer is None:
                ambiguous_actions += 1
            continue

        target_cell = record.target.target_cell
        if target_cell is None:
            ambiguous_actions += 1
            continue
        if (record.target.teacher_action, target_cell) not in legal:
            ambiguous_actions += 1
        if record.target.teacher_action == "left_click" and target_cell in board.mine_map:
            unsafe_clicks += 1
        if record.target.teacher_action == "right_click" and target_cell not in board.mine_map:
            false_flags += 1

    return {
        "records": len(records),
        "trajectories": sorted({record.trajectory_id for record in records}),
        "actions": dict(action_counts),
        "categories": dict(category_counts),
        "unsafe_clicks": unsafe_clicks,
        "false_flags": false_flags,
        "ambiguous_actions": ambiguous_actions,
        "hidden_truth_leaks": hidden_truth_leaks,
        "coordinate_errors": coordinate_errors,
        "missing_images": missing_images,
    }


def validate_dataset_v4(root: Path) -> dict[str, Any]:
    splits = {
        name: _validate_split(root, name) for name in ("train", "validation", "test")
    }
    trajectory_sets = {
        name: set(report["trajectories"]) for name, report in splits.items()
    }
    overlap_ids = sorted(
        (trajectory_sets["train"] & trajectory_sets["validation"])
        | (trajectory_sets["train"] & trajectory_sets["test"])
        | (trajectory_sets["validation"] & trajectory_sets["test"])
    )
    totals = {
        key: sum(int(report[key]) for report in splits.values())
        for key in (
            "unsafe_clicks",
            "false_flags",
            "ambiguous_actions",
            "hidden_truth_leaks",
            "coordinate_errors",
            "missing_images",
        )
    }
    report: dict[str, Any] = {
        "root": str(root.resolve()),
        **splits,
        **totals,
        "trajectory_overlap": len(overlap_ids),
        "trajectory_overlap_ids": overlap_ids,
    }
    failures = {key: value for key, value in totals.items() if value}
    if overlap_ids:
        failures["trajectory_overlap"] = len(overlap_ids)
    if failures:
        report["status"] = "failed"
        raise ValueError(json.dumps(report, ensure_ascii=False, indent=2))
    report["status"] = "ok"
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset_root", type=Path)
    args = parser.parse_args()
    print(json.dumps(validate_dataset_v4(args.dataset_root), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
