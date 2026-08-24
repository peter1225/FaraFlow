"""Run a bounded, no-side-effect coordinate grounding batch against Fara."""

from __future__ import annotations

import argparse
import base64
import json
from typing import Any, cast

import httpx
from faraflow.config import Settings
from faraflow.model.coordinate_adapter import CoordinateMode, ObservationGeometry
from faraflow.model.fara_protocol import build_system_prompt, parse_raw_tool_call

from minesweeper.evaluate import evaluate_raw_action
from minesweeper.geometry import BoardRectCss
from minesweeper.render import render_board_png
from minesweeper.schema import CanonicalRecord
from minesweeper.smoke import make_smoke_sample

TARGET_CELLS: tuple[tuple[int, int], ...] = (
    (0, 0),
    (0, 2),
    (1, 0),
    (1, 2),
    (2, 0),
    (2, 1),
    (2, 2),
)


def _target_record(
    sample: Any,
    board_rect: BoardRectCss,
    target_cell: tuple[int, int],
) -> CanonicalRecord:
    """Reuse one visible board while changing only the explicit target label."""

    return CanonicalRecord.from_target(
        trajectory_id=f"grounding-target-{target_cell[0]}-{target_cell[1]}",
        step_id=0,
        seed=sample.board.seed,
        difficulty="beginner",
        observation=sample.record.observation,
        board_rect=board_rect,
        target_cell=target_cell,
        teacher_action="left_click",
        legal_actions=sample.record.target.legal_actions,
        teacher=sample.record.teacher,
        supervision_only=sample.record.supervision_only,
    )


def _instruction(mode: CoordinateMode, target_cell: tuple[int, int]) -> str:
    row, col = target_cell
    return (
        "This is a coordinate-only Minesweeper Grounding Gate test. Ignore game deduction. "
        f"Return exactly one computer_use tool call with action left_click on row {row}, "
        f"column {col}; it is outlined in green. Use the visual center of that highlighted "
        "cell. Do not click any other cell, "
        f"and use {mode.value} coordinates. Do not terminate."
    )


def run(args: argparse.Namespace) -> dict[str, Any]:
    settings = cast(Any, Settings)(_env_file=args.env_file)
    mode = CoordinateMode(args.mode)
    sample = make_smoke_sample()
    board_rect = BoardRectCss(360, 160, 630, 560, 9, 9)
    geometry = ObservationGeometry.full_viewport(1440, 900)
    system_prompt = build_system_prompt(
        1440 if mode is CoordinateMode.PIXEL else 1000,
        900 if mode is CoordinateMode.PIXEL else 1000,
        coordinate_mode=mode.value,
        screenshot_width=1440,
        screenshot_height=900,
    )
    base_url = (args.base_url or settings.fara_base_url).rstrip("/")
    rows: list[dict[str, Any]] = []
    with httpx.Client(timeout=args.timeout) as client:
        for target_cell in TARGET_CELLS:
            target_record = _target_record(sample, board_rect, target_cell)
            encoded = base64.b64encode(
                render_board_png(sample.board, board_rect, highlight_cell=target_cell)
            ).decode("ascii")
            payload = {
                "model": args.model or settings.fara_model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image_url",
                                "image_url": {"url": f"data:image/png;base64,{encoded}"},
                            },
                            {
                                "type": "text",
                                "text": _instruction(mode, target_cell),
                            },
                        ],
                    },
                ],
                "temperature": 0.0,
                "max_tokens": 256,
            }
            row: dict[str, Any] = {"target_cell": target_cell}
            try:
                response = client.post(f"{base_url}/chat/completions", json=payload)
                response.raise_for_status()
                response_data = response.json()
                content = response_data["choices"][0]["message"]["content"]
                raw = parse_raw_tool_call(content)
                evaluation = evaluate_raw_action(
                    raw.action,
                    mode=mode,
                    geometry=geometry,
                    target=target_record.target,
                    board_rect=board_rect,
                )
                row.update(
                    {
                        "http_status": response.status_code,
                        "raw_action": raw.action.model_dump(mode="json"),
                        "predicted_cell": evaluation.predicted_cell,
                        "target_cell_hit": evaluation.target_cell_hit,
                        "legal_action": evaluation.legal_action,
                        "pixel_error": evaluation.pixel_error,
                    }
                )
            except Exception as exc:  # noqa: BLE001 - retain per-case evidence
                row.update({"error_type": type(exc).__name__, "error": str(exc)})
            rows.append(row)

    completed = [row for row in rows if "error" not in row]
    legal_count = sum(bool(row.get("legal_action")) for row in completed)
    hit_count = sum(bool(row.get("target_cell_hit")) for row in completed)
    return {
        "mode": mode.value,
        "model": args.model or settings.fara_model,
        "targets": list(TARGET_CELLS),
        "summary": {
            "requested": len(rows),
            "completed": len(completed),
            "errors": len(rows) - len(completed),
            "legal_action_rate": legal_count / len(completed) if completed else 0.0,
            "target_cell_hit_rate": hit_count / len(completed) if completed else 0.0,
        },
        "results": rows,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mode",
        choices=[mode.value for mode in CoordinateMode],
        default="normalized_1000",
    )
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--model", default=None)
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--timeout", type=float, default=180.0)
    args = parser.parse_args()
    print(json.dumps(run(args), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
