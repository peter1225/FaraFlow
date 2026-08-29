"""Export validated Canonical V4 data to LLaMA-Factory multimodal Alpaca."""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path
from typing import Any

from faraflow.model.fara_protocol import RawComputerAction, build_system_prompt, parse_raw_tool_call

from minesweeper.schema_v4 import CanonicalRecordV4


def _integer_coordinate(point: tuple[float, float]) -> list[int]:
    values = [int(round(value)) for value in point]
    if not all(0 <= value <= 1000 for value in values):
        raise ValueError(f"normalized coordinate outside 0..1000: {values}")
    return values


def serialize_tool_call_v4(record: CanonicalRecordV4) -> str:
    if record.target.teacher_action == "terminate":
        arguments: dict[str, Any] = {
            "action": "terminate",
            "answer": record.target.answer,
        }
    else:
        coordinate = record.target.coordinate_norm_1000
        if coordinate is None:
            raise ValueError("coordinate action has no normalized coordinate")
        arguments = {
            "action": record.target.teacher_action,
            "coordinate": _integer_coordinate(coordinate),
        }
    action = RawComputerAction.model_validate(arguments)
    call = {
        "name": "computer_use",
        "arguments": action.model_dump(
            mode="json",
            exclude_none=True,
            exclude_defaults=True,
        ),
    }
    tool_call = (
        "<tool_call>\n"
        + json.dumps(call, ensure_ascii=False, separators=(",", ":"))
        + "\n</tool_call>"
    )
    return f"<think>\n{record.teacher.reasoning.strip()}\n</think>\n{tool_call}"


def _copy_image(record: CanonicalRecordV4, source_root: Path, output_root: Path) -> str:
    relative = Path(record.observation.image_path)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"unsafe image path: {relative}")
    source = (source_root / relative).resolve()
    root = source_root.resolve()
    if root not in source.parents or not source.is_file():
        raise FileNotFoundError(source)
    destination = output_root / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not destination.exists() or destination.stat().st_size != source.stat().st_size:
        shutil.copy2(source, destination)
    return relative.as_posix()


def _validate_row(row: dict[str, Any]) -> None:
    if not isinstance(row.get("images"), list) or len(row["images"]) != 1:
        raise ValueError("each row must contain exactly one image")
    if row.get("instruction", "").count("<image>") != 1:
        raise ValueError("instruction must contain exactly one <image> token")
    output = row.get("output")
    if not isinstance(output, str):
        raise ValueError("output must be a string")
    parsed = parse_raw_tool_call(output)
    if parsed.action.action == "terminate":
        if not parsed.action.answer or parsed.action.coordinate is not None:
            raise ValueError("terminate output has an invalid contract")
    elif parsed.action.coordinate is None or not all(
        0 <= value <= 1000 for value in parsed.action.coordinate
    ):
        raise ValueError("coordinate output is outside normalized_1000")
    if "mine_map" in json.dumps(row, ensure_ascii=False):
        raise ValueError("hidden mine truth leaked into exported row")


def _export_split(records_path: Path, output_root: Path) -> list[dict[str, Any]]:
    source_root = records_path.parent
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(
        records_path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        if not line.strip():
            continue
        try:
            record = CanonicalRecordV4.model_validate(json.loads(line))
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
                    "<image>\nSolve exactly one Minesweeper step using only visible clues and "
                    "flags. Right-click only a cell proven to be a mine. Left-click only a cell "
                    "proven to be safe. If no move is logically forced, terminate and state that "
                    "guessing would be required. Never guess. Return exactly one computer_use "
                    "tool call. Coordinates use normalized_1000 model space."
                ),
                "input": "",
                "output": serialize_tool_call_v4(record),
                "system": system,
                "images": [image_path],
            }
            _validate_row(row)
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"failed to export {records_path}:{line_number}") from exc
        rows.append(row)
    if not rows:
        raise ValueError(f"records file contains no records: {records_path}")
    return rows


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def export_dataset_v4(
    train_records: Path,
    validation_records: Path,
    output_dir: Path,
    *,
    test_records: Path | None = None,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    sources: dict[str, Path] = {
        "train": train_records,
        "validation": validation_records,
    }
    inferred_test = train_records.parent.parent / "test" / "records.jsonl"
    if test_records is not None:
        sources["test"] = test_records
    elif inferred_test.is_file():
        sources["test"] = inferred_test

    exported = {name: _export_split(path, output_dir) for name, path in sources.items()}
    for name, rows in exported.items():
        _write_json(output_dir / f"{name}.json", rows)

    dataset_info: dict[str, Any] = {}
    for name in exported:
        dataset_info[f"fara_mines_v2_{name}"] = {
            "file_name": f"{name}.json",
            "formatting": "alpaca",
            "columns": {
                "prompt": "instruction",
                "query": "input",
                "response": "output",
                "system": "system",
                "images": "images",
            },
        }
    _write_json(output_dir / "dataset_info.json", dataset_info)
    manifest = {
        "format": "llamafactory-multimodal-alpaca-v2",
        "schema_version": "fara-mines-v4",
        "output_dir": str(output_dir.resolve()),
        "records": {name: len(rows) for name, rows in exported.items()},
        "source_paths": {name: str(path.resolve()) for name, path in sources.items()},
        "coordinate_mode": "normalized_1000",
        "images_copied": True,
        "hidden_state_excluded": True,
        "teacher_policy": "certain_mine_then_certain_safe_else_terminate",
        "tool_call_contract": "computer_use XML; terminate requires answer",
    }
    _write_json(output_dir / "manifest.json", manifest)
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("train_records", type=Path)
    parser.add_argument("validation_records", type=Path)
    parser.add_argument("output_dir", type=Path)
    parser.add_argument("--test-records", type=Path, default=None)
    args = parser.parse_args()
    print(
        json.dumps(
            export_dataset_v4(
                args.train_records,
                args.validation_records,
                args.output_dir,
                test_records=args.test_records,
            ),
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
