"""Render Canonical visible states through a local Playwright browser."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from minesweeper.browser_html import visible_state_to_html
from minesweeper.geometry import BoardRectCss
from minesweeper.schema import CanonicalRecord


def _board_rect(record: CanonicalRecord) -> BoardRectCss:
    raw = record.supervision_only.get("board_rect_css")
    if isinstance(raw, dict):
        return BoardRectCss(
            x=float(raw["x"]),
            y=float(raw["y"]),
            width=float(raw["width"]),
            height=float(raw["height"]),
            rows=int(raw["rows"]),
            cols=int(raw["cols"]),
        )
    visible_state = record.supervision_only.get("visible_state")
    if not isinstance(visible_state, list) or not visible_state:
        raise ValueError(f"record {record.trajectory_id}/{record.step_id} has no visible_state")
    rows = len(visible_state)
    cols = len(visible_state[0])
    return BoardRectCss(360, 160, 630, 560, rows, cols)


def render_dataset(
    records_path: Path,
    output_root: Path,
    *,
    max_records: int | None = None,
) -> dict[str, Any]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - environment-dependent
        raise RuntimeError("Playwright is required for browser rendering") from exc

    rows: list[CanonicalRecord] = []
    for line_number, line in enumerate(
        records_path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        if not line.strip():
            continue
        try:
            rows.append(CanonicalRecord.model_validate(json.loads(line)))
        except Exception as exc:  # noqa: BLE001 - annotate bad source lines
            raise ValueError(f"invalid Canonical record at line {line_number}") from exc
        if max_records is not None and len(rows) >= max_records:
            break
    if not rows:
        raise ValueError("records file contains no records")

    output_root.mkdir(parents=True, exist_ok=True)
    output_records = output_root / "records.jsonl"
    rewritten: list[CanonicalRecord] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 900}, device_scale_factor=1)
        try:
            for record in rows:
                visible_state = record.supervision_only.get("visible_state")
                if not isinstance(visible_state, list):
                    raise ValueError(
                        f"record {record.trajectory_id}/{record.step_id} has no visible_state"
                    )
                board_rect = _board_rect(record)
                page.set_content(
                    visible_state_to_html(visible_state, board_rect),
                    wait_until="load",
                )
                image_path = Path(record.observation.image_path)
                destination = output_root / image_path
                destination.parent.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(destination), full_page=False)
                rewritten.append(record)
        finally:
            browser.close()
    output_records.write_text(
        "".join(record.model_dump_json() + "\n" for record in rewritten),
        encoding="utf-8",
    )
    manifest = {
        "schema_version": "fara-mines-v3",
        "data_source": "browser_synthetic_local",
        "render_backend": "playwright-chromium",
        "records": len(rewritten),
        "records_path": str(output_records.resolve()),
        "viewport_css": [1440, 900],
        "device_scale_factor": 1.0,
        "hidden_state_policy": "mine_map_not_read_by_renderer",
    }
    (output_root / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("records_path", type=Path)
    parser.add_argument("output_root", type=Path)
    parser.add_argument("--max-records", type=int, default=None)
    args = parser.parse_args()
    print(
        json.dumps(
            render_dataset(
                args.records_path,
                args.output_root,
                max_records=args.max_records,
            ),
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
