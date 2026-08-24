"""Validate split Canonical and Fara SFT files before training."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any

from faraflow.model.fara_protocol import parse_raw_tool_call

from minesweeper.schema import CanonicalRecord
from scripts.export_fara_sft import validate_export_row


def _validate_split(root: Path, split: str) -> dict[str, Any]:
    split_root = root / split
    records_path = split_root / "records.jsonl"
    sft_path = split_root / "fara-sft.jsonl"
    records = [
        CanonicalRecord.model_validate(json.loads(line))
        for line in records_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    sft_rows = [
        json.loads(line)
        for line in sft_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if len(records) != len(sft_rows):
        raise ValueError(f"{split}: Canonical/SFT record count mismatch")
    missing_images = [
        record.observation.image_path
        for record in records
        if not (split_root / record.observation.image_path).exists()
    ]
    hidden_truth_rows = 0
    coordinate_errors = 0
    action_counts: Counter[str] = Counter()
    for row in sft_rows:
        validate_export_row(row)
        if "mine_map" in json.dumps(row["messages"], ensure_ascii=False):
            hidden_truth_rows += 1
        parsed = parse_raw_tool_call(row["messages"][-1]["content"])
        action_counts[parsed.action.action] += 1
        if parsed.action.coordinate is not None and not all(
            0 <= value <= 1000 and float(value).is_integer()
            for value in parsed.action.coordinate
        ):
            coordinate_errors += 1
    if missing_images or hidden_truth_rows or coordinate_errors:
        raise ValueError(
            f"{split}: missing_images={len(missing_images)}, "
            f"hidden_truth_rows={hidden_truth_rows}, coordinate_errors={coordinate_errors}"
        )
    return {
        "records": len(records),
        "trajectories": sorted({record.trajectory_id for record in records}),
        "images": len(records) - len(missing_images),
        "actions": dict(action_counts),
        "hidden_truth_rows": hidden_truth_rows,
        "coordinate_errors": coordinate_errors,
    }


def validate_dataset(root: Path) -> dict[str, Any]:
    report: dict[str, Any] = {
        "root": str(root.resolve()),
        "train": _validate_split(root, "train"),
        "validation": _validate_split(root, "validation"),
    }
    train_ids = set(report["train"]["trajectories"])
    validation_ids = set(report["validation"]["trajectories"])
    overlap = sorted(train_ids & validation_ids)
    if overlap:
        raise ValueError(f"trajectory leakage between splits: {overlap}")
    report["trajectory_overlap"] = overlap
    report["status"] = "ok"
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("dataset_root", type=Path)
    args = parser.parse_args()
    print(json.dumps(validate_dataset(args.dataset_root), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
