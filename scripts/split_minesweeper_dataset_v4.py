"""Create exact train/validation/test splits without trajectory leakage."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from collections import Counter, defaultdict
from pathlib import Path
from typing import Iterable

from minesweeper.schema_v4 import CanonicalRecordV4

CATEGORIES = ("forced_safe", "forced_mine", "constraint", "ambiguous", "edge")


def load_records_v4(root: Path) -> list[CanonicalRecordV4]:
    records_path = root / "records.jsonl"
    if not records_path.exists():
        raise FileNotFoundError(records_path)
    records: list[CanonicalRecordV4] = []
    for line_number, line in enumerate(
        records_path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        if not line.strip():
            continue
        try:
            record = CanonicalRecordV4.model_validate(json.loads(line))
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"invalid Canonical V4 record at line {line_number}") from exc
        image_path = root / record.observation.image_path
        if not image_path.is_file():
            raise FileNotFoundError(image_path)
        records.append(record)
    if not records:
        raise ValueError("records.jsonl contains no records")
    return records


def _stable_group_order(trajectory_ids: Iterable[str], salt: str) -> list[str]:
    return sorted(
        trajectory_ids,
        key=lambda value: hashlib.sha256(f"{salt}:{value}".encode()).digest(),
    )


def choose_exact_trajectories(
    grouped_counts: dict[str, int],
    target_records: int,
    *,
    excluded: set[str] | None = None,
    salt: str,
    grouped_categories: dict[str, Counter[str]] | None = None,
) -> set[str]:
    """Find an exact whole-trajectory subset while preserving category balance."""

    if target_records < 0:
        raise ValueError("target record count must be non-negative")
    if target_records == 0:
        return set()
    excluded = excluded or set()
    available = {
        trajectory_id: count
        for trajectory_id, count in grouped_counts.items()
        if trajectory_id not in excluded
    }
    if sum(available.values()) < target_records:
        raise ValueError(
            f"only {sum(available.values())} records are available for target {target_records}"
        )
    category_totals = Counter[str]()
    if grouped_categories is not None:
        for trajectory_id in available:
            category_totals.update(grouped_categories[trajectory_id])
    available_total = sum(available.values())

    def score(category_counts: tuple[int, ...], total: int) -> float:
        if grouped_categories is None or total == 0 or available_total == 0:
            return 0.0
        return sum(
            abs(
                category_counts[index]
                - total * category_totals[category] / available_total
            )
            for index, category in enumerate(CATEGORIES)
        )

    empty_categories = (0,) * len(CATEGORIES)
    states: dict[int, tuple[tuple[str, ...], tuple[int, ...]]] = {
        0: ((), empty_categories)
    }
    for trajectory_id in _stable_group_order(available, salt):
        count = available[trajectory_id]
        next_states = dict(states)
        addition = tuple(
            grouped_categories[trajectory_id][category]
            if grouped_categories is not None
            else 0
            for category in CATEGORIES
        )
        for total, (selected, selected_categories) in states.items():
            candidate = total + count
            if candidate > target_records:
                continue
            candidate_categories = tuple(
                selected_categories[index] + addition[index]
                for index in range(len(CATEGORIES))
            )
            candidate_value = (selected + (trajectory_id,), candidate_categories)
            existing = next_states.get(candidate)
            if existing is None or (
                score(candidate_categories, candidate),
                len(candidate_value[0]),
                candidate_value[0],
            ) < (
                score(existing[1], candidate),
                len(existing[0]),
                existing[0],
            ):
                next_states[candidate] = candidate_value
        states = next_states
    if target_records in states:
        return set(states[target_records][0])
    reachable = max(states)
    raise ValueError(
        f"cannot select whole trajectories totaling exactly {target_records} records; "
        f"largest reachable count is {reachable}. Generate more trajectories or adjust targets."
    )


def _copy_record(
    record: CanonicalRecordV4,
    *,
    source_root: Path,
    split_root: Path,
) -> CanonicalRecordV4:
    relative = Path(record.observation.image_path)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"unsafe image path: {relative}")
    source = source_root / relative
    destination = split_root / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    observation = record.observation.model_copy(update={"image_path": relative.as_posix()})
    return record.model_copy(update={"observation": observation})


def split_dataset_v4(
    source_root: Path,
    output_root: Path,
    *,
    train_records: int,
    validation_records: int,
    test_records: int,
) -> dict[str, object]:
    records = load_records_v4(source_root)
    requested_total = train_records + validation_records + test_records
    if min(train_records, validation_records, test_records) < 0:
        raise ValueError("split record counts must be non-negative")
    if requested_total != len(records):
        raise ValueError(
            f"split targets total {requested_total}, but source contains {len(records)} records"
        )
    grouped: dict[str, int] = defaultdict(int)
    grouped_categories: dict[str, Counter[str]] = defaultdict(Counter)
    for record in records:
        grouped[record.trajectory_id] += 1
        grouped_categories[record.trajectory_id][record.category] += 1

    test_ids = choose_exact_trajectories(
        dict(grouped),
        test_records,
        salt="test-v4",
        grouped_categories=dict(grouped_categories),
    )
    validation_ids = choose_exact_trajectories(
        dict(grouped),
        validation_records,
        excluded=test_ids,
        salt="validation-v4",
        grouped_categories=dict(grouped_categories),
    )
    train_ids = set(grouped) - test_ids - validation_ids
    buckets = {
        "train": [record for record in records if record.trajectory_id in train_ids],
        "validation": [
            record for record in records if record.trajectory_id in validation_ids
        ],
        "test": [record for record in records if record.trajectory_id in test_ids],
    }
    expected = {
        "train": train_records,
        "validation": validation_records,
        "test": test_records,
    }
    for name, bucket in buckets.items():
        if len(bucket) != expected[name]:
            raise RuntimeError(f"{name} split has {len(bucket)} records, expected {expected[name]}")

    output_root.mkdir(parents=True, exist_ok=True)
    split_manifest: dict[str, object] = {}
    for split_name, split_records_list in buckets.items():
        split_root = output_root / split_name
        split_root.mkdir(parents=True, exist_ok=True)
        rewritten = [
            _copy_record(record, source_root=source_root, split_root=split_root)
            for record in split_records_list
        ]
        records_path = split_root / "records.jsonl"
        records_path.write_text(
            "".join(record.model_dump_json() + "\n" for record in rewritten),
            encoding="utf-8",
        )
        category_counts: dict[str, int] = defaultdict(int)
        for record in rewritten:
            category_counts[record.category] += 1
        split_manifest[split_name] = {
            "records": len(rewritten),
            "trajectories": sorted({record.trajectory_id for record in rewritten}),
            "category_counts": dict(category_counts),
            "records_path": records_path.relative_to(output_root).as_posix(),
        }

    manifest = {
        "schema_version": "fara-mines-v4",
        "source_root": str(source_root.resolve()),
        "split_policy": "exact_record_counts_whole_trajectories",
        "splits": split_manifest,
    }
    (output_root / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source_root", type=Path)
    parser.add_argument("output_root", type=Path)
    parser.add_argument("--train-records", type=int, required=True)
    parser.add_argument("--validation-records", type=int, required=True)
    parser.add_argument("--test-records", type=int, required=True)
    args = parser.parse_args()
    print(
        json.dumps(
            split_dataset_v4(
                args.source_root,
                args.output_root,
                train_records=args.train_records,
                validation_records=args.validation_records,
                test_records=args.test_records,
            ),
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
