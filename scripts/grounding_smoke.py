"""Run one no-side-effect Base Fara grounding request against an OpenAI endpoint."""

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
from minesweeper.smoke import make_smoke_sample


def run(args: argparse.Namespace) -> dict[str, Any]:
    settings = cast(Any, Settings)(_env_file=args.env_file)
    mode = CoordinateMode(args.mode)
    sample = make_smoke_sample()
    board_rect = BoardRectCss(360, 160, 630, 560, 9, 9)
    geometry = ObservationGeometry.full_viewport(1440, 900)
    encoded = base64.b64encode(sample.image).decode("ascii")
    system_prompt = build_system_prompt(
        1440 if mode is CoordinateMode.PIXEL else 1000,
        900 if mode is CoordinateMode.PIXEL else 1000,
        coordinate_mode=mode.value,
        screenshot_width=1440,
        screenshot_height=900,
    )
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
                        "text": (
                            "This is a coordinate-only Minesweeper Grounding Gate smoke test. "
                            "Ignore Minesweeper deduction. Return exactly one valid computer_use "
                            "tool call with action left_click on row 0, column 0: the unflagged "
                            "covered cell at the board's top-left. The red flagged cell at row 0, "
                            "column 1 is not the target. Use the visual center of the requested "
                            f"cell and use {mode.value} coordinates. Do not terminate."
                        ),
                    },
                ],
            },
        ],
        "temperature": 0.0,
        "max_tokens": 256,
    }
    base_url = (args.base_url or settings.fara_base_url).rstrip("/")
    with httpx.Client(timeout=args.timeout) as client:
        response = client.post(f"{base_url}/chat/completions", json=payload)
        response.raise_for_status()
    response_data = response.json()
    content = response_data["choices"][0]["message"]["content"]
    raw = parse_raw_tool_call(content)
    evaluation = evaluate_raw_action(
        raw.action,
        mode=mode,
        geometry=geometry,
        target=sample.record.target,
        board_rect=board_rect,
    )
    return {
        "mode": mode.value,
        "http_status": response.status_code,
        "model": response_data.get("model"),
        "raw_action": raw.action.model_dump(mode="json"),
        "evaluation": {
            "predicted_action": evaluation.predicted_action,
            "predicted_coordinate_css": evaluation.predicted_coordinate_css,
            "predicted_cell": evaluation.predicted_cell,
            "target_cell_hit": evaluation.target_cell_hit,
            "legal_action": evaluation.legal_action,
            "pixel_error": evaluation.pixel_error,
        },
        "target": sample.record.target.model_dump(mode="json"),
        "usage": response_data.get("usage"),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=[mode.value for mode in CoordinateMode], required=True)
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--model", default=None)
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--timeout", type=float, default=180.0)
    args = parser.parse_args()
    print(json.dumps(run(args), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
