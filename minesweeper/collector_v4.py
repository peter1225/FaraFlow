"""Balanced Canonical V4 collection for certainty-first Minesweeper SFT."""

from __future__ import annotations

import json
import random
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

from .engine import BoardConfig, GameStatus, MinesweeperBoard
from .geometry import BoardRectCss
from .render import render_board_png
from .schema import CaptureRectCss, ObservationMetadata
from .schema_v4 import (
    BoardMetadataV4,
    CanonicalRecordV4,
    LegalActionV4,
    TeacherMetadataV4,
)
from .solver_v4 import ConstraintAnalysisV4, ForcedActionV4, analyze_forced_actions

CategoryV4 = Literal[
    "forced_safe",
    "forced_mine",
    "constraint",
    "ambiguous",
    "edge",
]

TERMINATE_ANSWER = "No logically forced move remains; guessing would be required."
VIEWPORT = (1440, 900)


@dataclass(frozen=True)
class CollectionStatsV4:
    requested_records: int
    records_written: int
    trajectories_attempted: int
    category_counts: dict[str, int]
    category_targets: dict[str, int]
    incomplete_analyses: int
    inconsistent_states: int


def category_targets(
    records: int,
    *,
    include_ambiguous: bool = True,
) -> dict[CategoryV4, int]:
    """Allocate exact record counts using the documented V4 mixture."""

    if records <= 0:
        raise ValueError("records must be positive")
    weights: dict[CategoryV4, float]
    if include_ambiguous:
        weights = {
            "forced_safe": 0.25,
            "forced_mine": 0.25,
            "constraint": 0.25,
            "ambiguous": 0.20,
            "edge": 0.05,
        }
    else:
        weights = {
            "forced_safe": 0.30,
            "forced_mine": 0.30,
            "constraint": 0.35,
            "ambiguous": 0.00,
            "edge": 0.05,
        }
    targets = {category: int(records * weight) for category, weight in weights.items()}
    remainder = records - sum(targets.values())
    order: tuple[CategoryV4, ...] = (
        "constraint",
        "forced_mine",
        "forced_safe",
        "ambiguous",
        "edge",
    )
    for category in order:
        if remainder <= 0:
            break
        if weights[category] > 0:
            targets[category] += 1
            remainder -= 1
    return targets


def _observation(image_path: str) -> ObservationMetadata:
    return ObservationMetadata(
        image_path=image_path,
        viewport_css=VIEWPORT,
        capture_rect_css=CaptureRectCss(x=0, y=0, width=VIEWPORT[0], height=VIEWPORT[1]),
        input_image_size_px=VIEWPORT,
        device_scale_factor=1.0,
        screenshot_scale="css",
    )


def _layout_for_seed(
    seed: int,
    config: BoardConfig,
    *,
    randomize: bool,
) -> BoardRectCss:
    if not randomize:
        return BoardRectCss(360, 160, 630, 560, config.height, config.width)
    rng = random.Random(seed ^ 0xF4A027)
    max_cell = min(
        62,
        (VIEWPORT[0] - 160) // config.width,
        (VIEWPORT[1] - 180) // config.height,
    )
    min_cell = min(30, max_cell)
    cell_size = rng.randint(min_cell, max_cell)
    width = cell_size * config.width
    height = cell_size * config.height
    x = rng.randint(40, max(40, VIEWPORT[0] - width - 40))
    y = rng.randint(90, max(90, VIEWPORT[1] - height - 40))
    return BoardRectCss(x, y, width, height, config.height, config.width)


def _board_metadata(config: BoardConfig) -> BoardMetadataV4:
    return BoardMetadataV4(width=config.width, height=config.height, mines=config.mines)


def _difficulty(config: BoardConfig) -> str:
    if (config.width, config.height, config.mines) == (9, 9, 10):
        return "beginner"
    return f"{config.width}x{config.height}-{config.mines}"


def _supervision(
    board: MinesweeperBoard,
    board_rect: BoardRectCss,
    *,
    analysis: ConstraintAnalysisV4 | None,
) -> dict[str, object]:
    return {
        "visible_state": board.visible_snapshot(),
        "mine_map": [list(cell) for cell in sorted(board.mine_map)],
        "board_rect_css": {
            "x": board_rect.x,
            "y": board_rect.y,
            "width": board_rect.width,
            "height": board_rect.height,
            "rows": board_rect.rows,
            "cols": board_rect.cols,
        },
        "analysis": {
            "complete": analysis.complete if analysis is not None else True,
            "consistent": analysis.consistent if analysis is not None else True,
            "constraint_count": len(analysis.constraints) if analysis is not None else 0,
            "component_solutions": analysis.component_solutions if analysis is not None else 0,
        },
    }


def _category_for_action(action: ForcedActionV4) -> CategoryV4:
    if action.rule_id == "constraint_enumeration":
        return "constraint"
    return "forced_mine" if action.action == "right_click" else "forced_safe"


def _legal_actions(analysis: ConstraintAnalysisV4) -> list[LegalActionV4]:
    return [
        LegalActionV4(
            action=action.action,
            target_cell=action.target_cell,
            rule_id=action.rule_id,
        )
        for action in analysis.legal_actions
    ]


def _action_record(
    *,
    board: MinesweeperBoard,
    board_rect: BoardRectCss,
    trajectory_id: str,
    step_id: int,
    action: ForcedActionV4,
    analysis: ConstraintAnalysisV4,
) -> CanonicalRecordV4:
    category = _category_for_action(action)
    image_path = f"images/{trajectory_id}/step_{step_id:04d}.png"
    certainty = "mine" if action.action == "right_click" else "safe"
    return CanonicalRecordV4.from_decision(
        trajectory_id=trajectory_id,
        step_id=step_id,
        seed=board.seed,
        difficulty=_difficulty(board.config),
        category=category,
        board=_board_metadata(board.config),
        observation=_observation(image_path),
        board_rect=board_rect,
        teacher_action=action.action,
        target_cell=action.target_cell,
        answer=None,
        legal_actions=_legal_actions(analysis),
        teacher=TeacherMetadataV4(
            rule_id=action.rule_id,
            certainty=certainty,
            reasoning=action.reasoning,
            proof=action.proof,
        ),
        supervision_only=_supervision(board, board_rect, analysis=analysis),
    )


def _ambiguous_record(
    *,
    board: MinesweeperBoard,
    board_rect: BoardRectCss,
    trajectory_id: str,
    step_id: int,
    analysis: ConstraintAnalysisV4,
) -> CanonicalRecordV4:
    image_path = f"images/{trajectory_id}/step_{step_id:04d}.png"
    return CanonicalRecordV4.from_decision(
        trajectory_id=trajectory_id,
        step_id=step_id,
        seed=board.seed,
        difficulty=_difficulty(board.config),
        category="ambiguous",
        board=_board_metadata(board.config),
        observation=_observation(image_path),
        board_rect=board_rect,
        teacher_action="terminate",
        target_cell=None,
        answer=TERMINATE_ANSWER,
        legal_actions=[],
        teacher=TeacherMetadataV4(
            rule_id="no_forced_move",
            certainty="ambiguous",
            reasoning=(
                "The visible clues admit multiple consistent mine assignments and no covered "
                "cell is certainly safe or certainly a mine. Do not guess."
            ),
            proof={
                "method": "complete_constraint_enumeration",
                "result": "no_forced_move",
                "constraint_count": len(analysis.constraints),
            },
        ),
        supervision_only=_supervision(board, board_rect, analysis=analysis),
    )


def _first_click_record(
    *,
    board: MinesweeperBoard,
    board_rect: BoardRectCss,
    trajectory_id: str,
    target_cell: tuple[int, int],
) -> CanonicalRecordV4:
    image_path = f"images/{trajectory_id}/step_0000.png"
    legal = [
        LegalActionV4(action="left_click", target_cell=cell, rule_id="first_click_safe")
        for cell in board.cells()
    ]
    return CanonicalRecordV4.from_decision(
        trajectory_id=trajectory_id,
        step_id=0,
        seed=board.seed,
        difficulty=_difficulty(board.config),
        category="edge",
        board=_board_metadata(board.config),
        observation=_observation(image_path),
        board_rect=board_rect,
        teacher_action="left_click",
        target_cell=target_cell,
        answer=None,
        legal_actions=legal,
        teacher=TeacherMetadataV4(
            rule_id="first_click_safe",
            certainty="first_click",
            reasoning=(
                "The board is unopened and this game guarantees that the first click is safe; "
                "choose the center cell."
            ),
            proof={"method": "game_rule", "rule": "first_click_safe"},
        ),
        supervision_only=_supervision(board, board_rect, analysis=None),
    )


def _write_record(
    root: Path,
    records_file: object,
    record: CanonicalRecordV4,
    board: MinesweeperBoard,
    board_rect: BoardRectCss,
) -> None:
    image_path = root / record.observation.image_path
    image_path.parent.mkdir(parents=True, exist_ok=True)
    image_path.write_bytes(render_board_png(board, board_rect, image_size=VIEWPORT))
    records_file.write(record.model_dump_json() + "\n")


def _pick_action(
    analysis: ConstraintAnalysisV4,
    counts: Counter[str],
    targets: dict[CategoryV4, int],
) -> ForcedActionV4:
    under_target = [
        action
        for action in analysis.legal_actions
        if counts[_category_for_action(action)] < targets[_category_for_action(action)]
    ]
    if not under_target:
        return analysis.legal_actions[0]

    def key(action: ForcedActionV4) -> tuple[float, int, tuple[int, int]]:
        category = _category_for_action(action)
        target = max(targets[category], 1)
        remaining_ratio = (target - counts[category]) / target
        action_priority = 0 if action.action == "right_click" else 1
        return (-remaining_ratio, action_priority, action.target_cell)

    return min(under_target, key=key)


def collect_dataset_v4(
    root: Path,
    *,
    requested_records: int = 6500,
    seed_start: int = 100_000,
    max_attempts: int | None = None,
    max_steps: int = 100,
    config: BoardConfig = BoardConfig(),
    include_ambiguous: bool = True,
    randomize_layout: bool = True,
    max_component_solutions: int = 200_000,
) -> CollectionStatsV4:
    """Write a quota-balanced V4 dataset without hidden-truth teacher actions."""

    targets = category_targets(requested_records, include_ambiguous=include_ambiguous)
    counts: Counter[str] = Counter()
    attempts_limit = max_attempts or max(requested_records * 20, 2_000)
    root.mkdir(parents=True, exist_ok=True)
    records_path = root / "records.jsonl"
    incomplete_analyses = 0
    inconsistent_states = 0
    attempted = 0

    with records_path.open("w", encoding="utf-8") as records_file:
        while sum(counts.values()) < requested_records and attempted < attempts_limit:
            seed = seed_start + attempted
            attempted += 1
            board = MinesweeperBoard(config, seed=seed)
            board_rect = _layout_for_seed(seed, config, randomize=randomize_layout)
            trajectory_id = f"v4-seed-{seed:08d}"
            first_click = (config.height // 2, config.width // 2)
            if counts["edge"] < targets["edge"]:
                record = _first_click_record(
                    board=board,
                    board_rect=board_rect,
                    trajectory_id=trajectory_id,
                    target_cell=first_click,
                )
                _write_record(root, records_file, record, board, board_rect)
                counts["edge"] += 1
            board.click(first_click)

            for step_id in range(1, max_steps + 1):
                if board.status in {GameStatus.WON, GameStatus.LOST}:
                    break
                analysis = analyze_forced_actions(
                    board,
                    max_component_solutions=max_component_solutions,
                )
                if not analysis.complete:
                    incomplete_analyses += 1
                    break
                if not analysis.consistent:
                    inconsistent_states += 1
                    break
                if not analysis.legal_actions:
                    if counts["ambiguous"] < targets["ambiguous"]:
                        record = _ambiguous_record(
                            board=board,
                            board_rect=board_rect,
                            trajectory_id=trajectory_id,
                            step_id=step_id,
                            analysis=analysis,
                        )
                        _write_record(root, records_file, record, board, board_rect)
                        counts["ambiguous"] += 1
                    break

                action = _pick_action(analysis, counts, targets)
                category = _category_for_action(action)
                if counts[category] < targets[category]:
                    record = _action_record(
                        board=board,
                        board_rect=board_rect,
                        trajectory_id=trajectory_id,
                        step_id=step_id,
                        action=action,
                        analysis=analysis,
                    )
                    _write_record(root, records_file, record, board, board_rect)
                    counts[category] += 1
                if action.action == "left_click":
                    board.click(action.target_cell)
                else:
                    board.toggle_flag(action.target_cell)

                if sum(counts.values()) >= requested_records:
                    break

    if sum(counts.values()) != requested_records:
        missing = {
            category: targets[category] - counts[category]
            for category in targets
            if counts[category] < targets[category]
        }
        raise RuntimeError(
            f"collected {sum(counts.values())}/{requested_records} records after "
            f"{attempted} trajectories; missing quotas: {missing}"
        )

    manifest = {
        "schema_version": "fara-mines-v4",
        "data_source": "deterministic_constraint_engine",
        "generator": "minesweeper.collector_v4.collect_dataset_v4",
        "requested_records": requested_records,
        "records_written": sum(counts.values()),
        "category_targets": targets,
        "category_counts": dict(counts),
        "trajectories_attempted": attempted,
        "seed_start": seed_start,
        "board_config": asdict(config),
        "include_ambiguous": include_ambiguous,
        "randomize_layout": randomize_layout,
        "hidden_state_policy": "mine_map_only_in_supervision_only",
        "teacher_policy": "certain_mine_then_certain_safe_else_terminate",
        "max_component_solutions": max_component_solutions,
        "viewport_css": list(VIEWPORT),
    }
    (root / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return CollectionStatsV4(
        requested_records=requested_records,
        records_written=sum(counts.values()),
        trajectories_attempted=attempted,
        category_counts=dict(counts),
        category_targets=dict(targets),
        incomplete_analyses=incomplete_analyses,
        inconsistent_states=inconsistent_states,
    )
