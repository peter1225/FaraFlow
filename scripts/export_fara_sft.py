"""Export Canonical V3 records to an auditable Fara ChatML-style SFT JSONL."""

from __future__ import annotations

import argparse
import base64
import json
from pathlib import Path
from typing import Any, Literal

from faraflow.model.fara_protocol import (
    RawComputerAction,
    build_system_prompt,
    parse_raw_tool_call,
)

from minesweeper.schema import CanonicalRecord

ImageFormat = Literal["hf", "openai"]


def _integer_coordinate(point: tuple[float, float]) -> list[int]:
    values = [int(round(value)) for value in point]
    if not all(0 <= value <= 1000 for value in values):
        raise ValueError(f"normalized coordinate outside 0..1000: {values}")
    return values


def serialize_tool_call(arguments: dict[str, Any]) -> str:
    """Serialize one Fara tool call after applying the runtime protocol validator."""

    action = RawComputerAction.model_validate(arguments)
    call = {
        "name": "computer_use",
        "arguments": action.model_dump(mode="json", exclude_none=True),
    }
    return (
        "<tool_call>\n"
        + json.dumps(call, ensure_ascii=False, separators=(",", ":"))
        + "\n</tool_call>"
    )


def _image_content(
    record: CanonicalRecord,
    *,
    image_root: Path,
    image_format: ImageFormat,
    embed_images: bool,
) -> tuple[dict[str, Any], str]:
    image_path = image_root / record.observation.image_path
    if not image_path.exists():
        raise FileNotFoundError(image_path)
    relative_path = Path(record.observation.image_path).as_posix()
    if embed_images:
        encoded = base64.b64encode(image_path.read_bytes()).decode("ascii")
        image_url = f"data:image/png;base64,{encoded}"
        if image_format == "openai":
            return {"type": "image_url", "image_url": {"url": image_url}}, relative_path
        return {"type": "image", "url": image_url}, relative_path
    if image_format == "openai":
        raise ValueError("--image-format openai requires --embed-images")
    return {"type": "image", "url": relative_path}, relative_path


def export_record(
    record: CanonicalRecord,
    *,
    image_root: Path,
    image_format: ImageFormat = "hf",
    embed_images: bool = False,
    include_metadata: bool = True,
) -> dict[str, Any]:
    """Convert one Canonical record without exposing supervision-only mine truth."""

    image, relative_image_path = _image_content(
        record,
        image_root=image_root,
        image_format=image_format,
        embed_images=embed_images,
    )
    image_width, image_height = record.observation.input_image_size_px
    system = build_system_prompt(
        1000,
        1000,
        coordinate_mode="normalized_1000",
        screenshot_width=image_width,
        screenshot_height=image_height,
    )
    user_text = (
        "Solve the Minesweeper board one step at a time. Use the latest screenshot and "
        "return exactly one computer_use action for the next move. Coordinates must use "
        "the normalized_1000 model space."
    )
    arguments = {
        "action": record.target.teacher_action,
        "coordinate": _integer_coordinate(record.target.coordinate_norm_1000),
    }
    reasoning = record.teacher.reasoning.strip()
    assistant = ""
    if reasoning:
        assistant += f"<think>\n{reasoning}\n</think>\n"
    assistant += serialize_tool_call(arguments)
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": [image, {"type": "text", "text": user_text}]},
        {"role": "assistant", "content": assistant},
    ]
    row: dict[str, Any] = {"messages": messages}
    if include_metadata:
        row["metadata"] = {
            "schema_version": record.schema_version,
            "trajectory_id": record.trajectory_id,
            "step_id": record.step_id,
            "seed": record.seed,
            "image_path": relative_image_path,
            "target_cell": list(record.target.target_cell),
            "legal_actions": [item.model_dump(mode="json") for item in record.target.legal_actions],
            "coordinate_mode": "normalized_1000",
            "hidden_state_excluded": True,
        }
    return row


def validate_export_row(row: dict[str, Any]) -> None:
    """Validate the exported row with the same raw parser used at inference time."""

    messages = row.get("messages")
    if not isinstance(messages, list) or len(messages) != 3:
        raise ValueError("exported row must contain system/user/assistant messages")
    assistant = messages[-1].get("content")
    if not isinstance(assistant, str):
        raise ValueError("assistant content must be a string")
    parsed = parse_raw_tool_call(assistant)
    if parsed.action.coordinate is not None and not all(
        0 <= value <= 1000 for value in parsed.action.coordinate
    ):
        raise ValueError("assistant coordinate is outside normalized_1000 bounds")
    if "mine_map" in json.dumps(messages, ensure_ascii=False):
        raise ValueError("hidden mine truth leaked into messages")


def export_dataset(
    records_path: Path,
    output_path: Path,
    *,
    image_root: Path | None = None,
    image_format: ImageFormat = "hf",
    embed_images: bool = False,
    include_metadata: bool = True,
) -> dict[str, Any]:
    root = image_root or records_path.parent
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(
        records_path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        if not line.strip():
            continue
        try:
            record = CanonicalRecord.model_validate(json.loads(line))
            row = export_record(
                record,
                image_root=root,
                image_format=image_format,
                embed_images=embed_images,
                include_metadata=include_metadata,
            )
            validate_export_row(row)
        except Exception as exc:  # noqa: BLE001 - annotate the bad source line
            raise ValueError(f"failed to export record at line {line_number}") from exc
        rows.append(row)
    if not rows:
        raise ValueError("records file contains no records")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )
    manifest = {
        "format": "fara-chatml-style-sft-v1",
        "records_path": str(records_path.resolve()),
        "output_path": str(output_path.resolve()),
        "records": len(rows),
        "image_format": image_format,
        "images_embedded": embed_images,
        "coordinate_mode": "normalized_1000",
        "metadata_included": include_metadata,
        "tool_call_contract": "computer_use XML; terminate requires answer",
    }
    output_path.with_suffix(".manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("records_path", type=Path)
    parser.add_argument("output_path", type=Path)
    parser.add_argument("--image-root", type=Path, default=None)
    parser.add_argument("--image-format", choices=["hf", "openai"], default="hf")
    parser.add_argument("--embed-images", action="store_true")
    parser.add_argument("--minimal", action="store_true")
    args = parser.parse_args()
    print(
        json.dumps(
            export_dataset(
                args.records_path,
                args.output_path,
                image_root=args.image_root,
                image_format=args.image_format,
                embed_images=args.embed_images,
                include_metadata=not args.minimal,
            ),
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
