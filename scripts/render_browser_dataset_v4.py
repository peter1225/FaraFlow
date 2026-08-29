"""Render Canonical V4 records across deterministic browser layout variants."""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from minesweeper.browser_html_v4 import ThemeV4, visible_state_to_html_v4
from minesweeper.geometry import BoardRectCss
from minesweeper.schema import CaptureRectCss, ObservationMetadata
from minesweeper.schema_v4 import CanonicalRecordV4

VIEWPORT = (1440, 900)


@dataclass(frozen=True)
class LayoutV4:
    index: int
    board_rect: BoardRectCss
    theme: ThemeV4
    scroll_offset: int
    visual_scale: float
    edge_padding: int


def _stable_index(record: CanonicalRecordV4, layouts: int) -> int:
    payload = f"{record.trajectory_id}:{record.step_id}:{record.category}".encode()
    return int.from_bytes(hashlib.sha256(payload).digest()[:8], "big") % layouts


def layout_for_record(record: CanonicalRecordV4, layouts: int) -> LayoutV4:
    if layouts <= 0:
        raise ValueError("layouts must be positive")
    index = _stable_index(record, layouts)
    scales = (0.72, 0.84, 0.96, 1.08, 1.18, 0.90)
    x_ratios = (0.08, 0.50, 0.26, 0.68, 0.42, 0.15)
    y_ratios = (0.10, 0.18, 0.46, 0.32, 0.62, 0.25)
    scrolls = (0, 120, 240, 80, 180, 320)
    paddings = (24, 48, 72, 36, 60, 90)
    scale = scales[index % len(scales)]
    max_cell = min(
        (VIEWPORT[0] - 2 * paddings[index % len(paddings)]) / record.board.width,
        (VIEWPORT[1] - 150) / record.board.height,
        62,
    )
    cell_size = max(20.0, max_cell * scale)
    width = cell_size * record.board.width
    height = cell_size * record.board.height
    padding = paddings[index % len(paddings)]
    available_x = max(0.0, VIEWPORT[0] - width - 2 * padding)
    available_y = max(0.0, VIEWPORT[1] - height - 120)
    x = padding + available_x * x_ratios[index % len(x_ratios)]
    y = 82 + available_y * y_ratios[index % len(y_ratios)]
    return LayoutV4(
        index=index,
        board_rect=BoardRectCss(
            x=x,
            y=y,
            width=width,
            height=height,
            rows=record.board.height,
            cols=record.board.width,
        ),
        theme="dark" if index % 2 else "light",
        scroll_offset=scrolls[index % len(scrolls)],
        visual_scale=scale,
        edge_padding=padding,
    )


def _observation(image_path: str) -> ObservationMetadata:
    return ObservationMetadata(
        image_path=image_path,
        viewport_css=VIEWPORT,
        capture_rect_css=CaptureRectCss(x=0, y=0, width=VIEWPORT[0], height=VIEWPORT[1]),
        input_image_size_px=VIEWPORT,
        device_scale_factor=1.0,
        screenshot_scale="css",
    )


def _load_records(records_path: Path, max_records: int | None) -> list[CanonicalRecordV4]:
    rows: list[CanonicalRecordV4] = []
    for line_number, line in enumerate(
        records_path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        if not line.strip():
            continue
        try:
            rows.append(CanonicalRecordV4.model_validate(json.loads(line)))
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"invalid Canonical V4 record at line {line_number}") from exc
        if max_records is not None and len(rows) >= max_records:
            break
    if not rows:
        raise ValueError("records file contains no records")
    return rows


def render_dataset_v4(
    records_path: Path,
    output_root: Path,
    *,
    layouts: int = 6,
    max_records: int | None = None,
) -> dict[str, Any]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("Playwright is required for browser rendering") from exc

    rows = _load_records(records_path, max_records)
    output_root.mkdir(parents=True, exist_ok=True)
    rewritten: list[CanonicalRecordV4] = []
    layout_counts: dict[int, int] = {}
    theme_counts: dict[str, int] = {}
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": VIEWPORT[0], "height": VIEWPORT[1]})
        try:
            for record in rows:
                visible_state = record.supervision_only.get("visible_state")
                if not isinstance(visible_state, list):
                    raise ValueError(
                        f"record {record.trajectory_id}/{record.step_id} has no visible_state"
                    )
                layout = layout_for_record(record, layouts)
                page.set_content(
                    visible_state_to_html_v4(
                        visible_state,
                        layout.board_rect,
                        viewport=VIEWPORT,
                        theme=layout.theme,
                        scroll_offset=layout.scroll_offset,
                    ),
                    wait_until="load",
                )
                page.evaluate("offset => window.scrollTo(0, offset)", layout.scroll_offset)
                destination = output_root / record.observation.image_path
                destination.parent.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(destination), full_page=False)
                rewritten_record = record.with_layout(
                    observation=_observation(record.observation.image_path),
                    board_rect=layout.board_rect,
                    layout_metadata={
                        "index": layout.index,
                        "theme": layout.theme,
                        "scroll_offset": layout.scroll_offset,
                        "visual_scale": layout.visual_scale,
                        "edge_padding": layout.edge_padding,
                    },
                )
                rewritten.append(rewritten_record)
                layout_counts[layout.index] = layout_counts.get(layout.index, 0) + 1
                theme_counts[layout.theme] = theme_counts.get(layout.theme, 0) + 1
        finally:
            browser.close()

    output_records = output_root / "records.jsonl"
    output_records.write_text(
        "".join(record.model_dump_json() + "\n" for record in rewritten),
        encoding="utf-8",
    )
    manifest = {
        "schema_version": "fara-mines-v4",
        "data_source": "browser_synthetic_local_randomized",
        "render_backend": "playwright-chromium",
        "records": len(rewritten),
        "records_path": str(output_records.resolve()),
        "layouts_requested": layouts,
        "layout_counts": layout_counts,
        "theme_counts": theme_counts,
        "viewport_css": list(VIEWPORT),
        "coordinate_policy": "recomputed_after_layout",
        "hidden_state_policy": "mine_map_not_read_by_renderer",
    }
    (output_root / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("records_path", type=Path)
    parser.add_argument("output_root", type=Path)
    parser.add_argument("--layouts", type=int, default=6)
    parser.add_argument("--max-records", type=int, default=None)
    args = parser.parse_args()
    print(
        json.dumps(
            render_dataset_v4(
                args.records_path,
                args.output_root,
                layouts=args.layouts,
                max_records=args.max_records,
            ),
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
