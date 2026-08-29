from __future__ import annotations

import json

from faraflow.model.fara_protocol import parse_raw_tool_call

from minesweeper.collector_v4 import category_targets, collect_dataset_v4
from minesweeper.schema_v4 import CanonicalRecordV4
from scripts.export_llamafactory_v4 import export_dataset_v4
from scripts.render_browser_dataset_v4 import layout_for_record
from scripts.split_minesweeper_dataset_v4 import split_dataset_v4
from scripts.validate_minesweeper_dataset_v4 import validate_dataset_v4


def test_v4_category_targets_are_exact_and_balanced() -> None:
    targets = category_targets(6500, include_ambiguous=True)

    assert sum(targets.values()) == 6500
    assert targets == {
        "forced_safe": 1625,
        "forced_mine": 1625,
        "constraint": 1625,
        "ambiguous": 1300,
        "edge": 325,
    }


def test_v4_end_to_end_collection_split_validation_and_export(tmp_path) -> None:
    canonical = tmp_path / "canonical"
    stats = collect_dataset_v4(
        canonical,
        requested_records=20,
        seed_start=100_000,
        max_attempts=500,
        include_ambiguous=True,
        randomize_layout=True,
    )
    assert stats.records_written == 20
    assert stats.category_counts == {
        "edge": 1,
        "ambiguous": 4,
        "forced_mine": 5,
        "forced_safe": 5,
        "constraint": 5,
    }

    split_root = tmp_path / "split"
    manifest = split_dataset_v4(
        canonical,
        split_root,
        train_records=15,
        validation_records=3,
        test_records=2,
    )
    assert manifest["splits"]["train"]["records"] == 15
    validation = validate_dataset_v4(split_root)
    assert validation["status"] == "ok"
    assert validation["unsafe_clicks"] == 0
    assert validation["false_flags"] == 0
    assert validation["ambiguous_actions"] == 0
    assert validation["trajectory_overlap"] == 0

    output = tmp_path / "llamafactory"
    exported = export_dataset_v4(
        split_root / "train" / "records.jsonl",
        split_root / "validation" / "records.jsonl",
        output,
    )
    assert exported["records"] == {"train": 15, "validation": 3, "test": 2}
    rows = json.loads((output / "validation.json").read_text(encoding="utf-8"))
    for row in rows:
        decision = parse_raw_tool_call(row["output"])
        if decision.action.action == "terminate":
            assert decision.action.answer
            assert decision.action.coordinate is None
    assert "mine_map" not in json.dumps(rows, ensure_ascii=False)


def test_v4_layout_is_deterministic_and_recomputes_coordinates(tmp_path) -> None:
    canonical = tmp_path / "canonical"
    collect_dataset_v4(
        canonical,
        requested_records=5,
        seed_start=200_000,
        max_attempts=200,
        include_ambiguous=True,
        randomize_layout=True,
    )
    record = CanonicalRecordV4.model_validate_json(
        (canonical / "records.jsonl").read_text(encoding="utf-8").splitlines()[0]
    )
    first = layout_for_record(record, 6)
    second = layout_for_record(record, 6)

    assert first == second
    relaid = record.with_layout(
        observation=record.observation,
        board_rect=first.board_rect,
        layout_metadata={"index": first.index},
    )
    if relaid.target.target_cell is not None:
        assert relaid.target.coordinate_css == first.board_rect.cell_center_css(
            relaid.target.target_cell
        )
