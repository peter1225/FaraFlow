"""Create a leakage-safe train/validation split from Canonical V3 records."""

from __future__ import annotations

import argparse
import json
import shutil
from collections import defaultdict
from pathlib import Path
from typing import Iterable

from minesweeper.schema import CanonicalRecord


def load_records(root: Path) -> list[CanonicalRecord]:
    records_path = root / "records.jsonl"
    if not records_path.exists():
        raise FileNotFoundError(records_path)
    records: list[CanonicalRecord] = []
    for line_number, line in enumerate(
        records_path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        if not line.strip():
            continue
        try:
            record = CanonicalRecord.model_validate(json.loads(line))
        except Exception as exc:  # noqa: BLE001 - add the source line to the error
            raise ValueError(f"invalid Canonical record at line {line_number}") from exc
        image_path = root / record.observation.image_path
        if not image_path.exists():
            raise FileNotFoundError(image_path)
        records.append(record)
    if not records:
        raise ValueError("records.jsonl contains no records")
    return records


def choose_validation_trajectories(
    records: Iterable[CanonicalRecord],
    validation_fraction: float,
) -> set[str]:
    if not 0.0 < validation_fraction < 1.0:
        raise ValueError("validation_fraction must be between 0 and 1")
    grouped: dict[str, int] = defaultdict(int)
    for record in records:
        grouped[record.trajectory_id] += 1
    if len(grouped) < 2:
        raise ValueError("at least two trajectories are required for a split")

    target = sum(grouped.values()) * validation_fraction
    states: dict[int, tuple[str, ...]] = {0: ()}
    for trajectory_id, count in sorted(grouped.items()):
        next_states = dict(states)
        for total, selected in states.items():
            next_states.setdefault(total + count, selected + (trajectory_id,))
        states = next_states
    total_records = sum(grouped.values())
    candidates = [
        (total, selected)
        for total, selected in states.items()
        if 0 < total < total_records
    ]
    _, selected = min(
        candidates,
        key=lambda item: (abs(item[0] - target), len(item[1]), item[1]),
    )
    return set(selected)


def _copy_record(
    record: CanonicalRecord,
    *,
    source_root: Path,
    split_root: Path,
) -> CanonicalRecord:
    source_image = source_root / record.observation.image_path
    relative_image = Path(record.observation.image_path)
    destination_image = split_root / relative_image
    destination_image.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source_image, destination_image)
    observation = record.observation.model_copy(
        update={"image_path": relative_image.as_posix()}
    )
    return record.model_copy(update={"observation": observation})


def split_dataset(
    source_root: Path,
    output_root: Path,
    *,
    validation_fraction: float = 0.2,
) -> dict[str, object]:
    records = load_records(source_root)
    validation_trajectories = choose_validation_trajectories(records, validation_fraction)
    buckets = {
        "train": [
            record for record in records if record.trajectory_id not in validation_trajectories
        ],
        "validation": [
            record for record in records if record.trajectory_id in validation_trajectories
        ],
    }
    output_root.mkdir(parents=True, exist_ok=True)
    manifest_buckets: dict[str, object] = {}
    for split_name, split_records in buckets.items():
        split_root = output_root / split_name
        records_path = split_root / "records.jsonl"
        records_path.parent.mkdir(parents=True, exist_ok=True)
        rewritten = [
            _copy_record(record, source_root=source_root, split_root=split_root)
            for record in split_records
        ]
        records_path.write_text(
            "".join(record.model_dump_json() + "\n" for record in rewritten),
            encoding="utf-8",
        )
        manifest_buckets[split_name] = {
            "records": len(rewritten),
            "trajectories": sorted({record.trajectory_id for record in rewritten}),
            "records_path": records_path.relative_to(output_root).as_posix(),
        }

    manifest = {
        "schema_version": "fara-mines-v3",
        "source_root": str(source_root.resolve()),
        "validation_fraction_requested": validation_fraction,
        "validation_trajectories": sorted(validation_trajectories),
        "splits": manifest_buckets,
    }
    (output_root / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source_root", type=Path)
    parser.add_argument("output_root", type=Path)
    parser.add_argument("--validation-fraction", type=float, default=0.2)
    args = parser.parse_args()
    print(
        json.dumps(
            split_dataset(
                args.source_root,
                args.output_root,
                validation_fraction=args.validation_fraction,
            ),
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
