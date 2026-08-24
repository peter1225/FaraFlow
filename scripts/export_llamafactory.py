"""Export Minesweeper records to LLaMA-Factory's multimodal Alpaca format.

The existing ``export_fara_sft`` command produces a ChatML-style intermediate
file.  LLaMA-Factory's multimodal Alpaca loader expects one JSON object per
example with ``instruction``, ``input``, ``output`` and ``images`` fields, and
an ``<image>`` token in the prompt for each image.  This command creates a
self-contained dataset directory containing train/validation JSON files,
copied screenshots, and a ``dataset_info.json`` mapping.
"""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path
from typing import Any

from faraflow.model.fara_protocol import (
    RawComputerAction,
    build_system_prompt,
    parse_raw_tool_call,
)

from minesweeper.schema import CanonicalRecord


def _integer_coordinate(point: tuple[float, float]) -> list[int]:
    values = [int(round(value)) for value in point]
    if not all(0 <= value <= 1000 for value in values):
        raise ValueError(f"normalized coordinate outside 0..1000: {values}")
    return values


def _serialize_tool_call(record: CanonicalRecord) -> str:
    """Build the exact Fara XML tool-call contract used at inference time."""

    action = RawComputerAction.model_validate(
        {
            "action": record.target.teacher_action,
            "coordinate": _integer_coordinate(record.target.coordinate_norm_1000),
        }
    )
    call = {
        "name": "computer_use",
        "arguments": action.model_dump(mode="json", exclude_none=True),
    }
    tool_call = (
        "<tool_call>\n"
        + json.dumps(call, ensure_ascii=False, separators=(",", ":"))
        + "\n</tool_call>"
    )
    reasoning = record.teacher.reasoning.strip()
    if reasoning:
        return f"<think>\n{reasoning}\n</think>\n{tool_call}"
    return tool_call


def _copy_image(record: CanonicalRecord, source_root: Path, output_root: Path) -> str:
    """Copy one screenshot and return its POSIX path relative to dataset_dir."""

    relative = Path(record.observation.image_path)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"image path must be relative and contained: {relative}")
    source = (source_root / relative).resolve()
    source_root_resolved = source_root.resolve()
    if source_root_resolved not in source.parents:
        raise ValueError(f"image path escapes source root: {relative}")
    if not source.is_file():
        raise FileNotFoundError(source)
    destination = output_root / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not destination.exists() or destination.stat().st_size != source.stat().st_size:
        shutil.copy2(source, destination)
    return relative.as_posix()


def _export_split(records_path: Path, output_root: Path) -> list[dict[str, Any]]:
    source_root = records_path.parent
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(
        records_path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        if not line.strip():
            continue
        try:
            record = CanonicalRecord.model_validate(json.loads(line))
            image_path = _copy_image(record, source_root, output_root)
            image_width, image_height = record.observation.input_image_size_px
            system = build_system_prompt(
                1000,
                1000,
                coordinate_mode="normalized_1000",
                screenshot_width=image_width,
                screenshot_height=image_height,
            )
            row = {
                "instruction": (
                    "<image>\nSolve the Minesweeper board one step at a time. "
                    "Use the screenshot and return exactly one computer_use action "
                    "for the next move. Coordinates must use normalized_1000 model space."
                ),
                "input": "",
                "output": _serialize_tool_call(record),
                "system": system,
                "images": [image_path],
            }
            _validate_row(row)
        except Exception as exc:  # noqa: BLE001 - annotate the source line
            raise ValueError(f"failed to export {records_path}:{line_number}") from exc
        rows.append(row)
    if not rows:
        raise ValueError(f"records file contains no records: {records_path}")
    return rows


def _validate_row(row: dict[str, Any]) -> None:
    images = row.get("images")
    instruction = row.get("instruction")
    if not isinstance(images, list) or len(images) != 1:
        raise ValueError("LLaMA-Factory row must contain exactly one image")
    if not isinstance(instruction, str) or instruction.count("<image>") != len(images):
        raise ValueError("image count does not match <image> token count")
    output = row.get("output")
    if not isinstance(output, str):
        raise ValueError("output must be a string")
    parsed = parse_raw_tool_call(output)
    if parsed.action.coordinate is not None and not all(
        0 <= value <= 1000 for value in parsed.action.coordinate
    ):
        raise ValueError("assistant coordinate is outside normalized_1000 bounds")
    # Supervision-only fields (including mine_map) are never copied into rows.
    if "mine_map" in json.dumps(row, ensure_ascii=False):
        raise ValueError("hidden mine truth leaked into LLaMA-Factory row")


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def export_dataset(
    train_records: Path,
    validation_records: Path,
    output_dir: Path,
) -> dict[str, Any]:
    """Create a complete LLaMA-Factory dataset directory."""

    output_dir.mkdir(parents=True, exist_ok=True)
    train_rows = _export_split(train_records, output_dir)
    validation_rows = _export_split(validation_records, output_dir)
    _write_json(output_dir / "train.json", train_rows)
    _write_json(output_dir / "validation.json", validation_rows)
    dataset_info = {
        "fara_mines_train": {
            "file_name": "train.json",
            "formatting": "alpaca",
            "columns": {
                "prompt": "instruction",
                "query": "input",
                "response": "output",
                "system": "system",
                "images": "images",
            },
        },
        "fara_mines_validation": {
            "file_name": "validation.json",
            "formatting": "alpaca",
            "columns": {
                "prompt": "instruction",
                "query": "input",
                "response": "output",
                "system": "system",
                "images": "images",
            },
        },
    }
    _write_json(output_dir / "dataset_info.json", dataset_info)
    manifest = {
        "format": "llamafactory-multimodal-alpaca-v1",
        "train_records_path": str(train_records.resolve()),
        "validation_records_path": str(validation_records.resolve()),
        "output_dir": str(output_dir.resolve()),
        "train_records": len(train_rows),
        "validation_records": len(validation_rows),
        "coordinate_mode": "normalized_1000",
        "images_copied": True,
        "hidden_state_excluded": True,
        "tool_call_contract": "computer_use XML; terminate requires answer",
    }
    _write_json(output_dir / "manifest.json", manifest)
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("train_records", type=Path)
    parser.add_argument("validation_records", type=Path)
    parser.add_argument("output_dir", type=Path)
    args = parser.parse_args()
    print(
        json.dumps(
            export_dataset(args.train_records, args.validation_records, args.output_dir),
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
