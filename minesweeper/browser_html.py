"""Render a visible Minesweeper state as a local browser page."""

from __future__ import annotations

from html import escape
from typing import Any

from .geometry import BoardRectCss


def visible_state_to_html(
    visible_state: list[list[dict[str, Any]]],
    board_rect: BoardRectCss,
    *,
    viewport: tuple[int, int] = (1440, 900),
) -> str:
    """Build a self-contained, network-free page from visible cells only."""

    rows = len(visible_state)
    cols = len(visible_state[0]) if rows else 0
    if rows <= 0 or cols <= 0 or any(len(row) != cols for row in visible_state):
        raise ValueError("visible_state must be a non-empty rectangular grid")
    viewport_width, viewport_height = viewport
    if viewport_width <= 0 or viewport_height <= 0:
        raise ValueError("viewport dimensions must be positive")
    cells: list[str] = []
    for row_index, row in enumerate(visible_state):
        for col_index, cell in enumerate(row):
            state = str(cell.get("state", "covered"))
            if state not in {"covered", "revealed", "flagged"}:
                raise ValueError(f"unsupported visible cell state: {state}")
            number = cell.get("adjacent_mines")
            text = str(number) if state == "revealed" and number else ""
            cells.append(
                "<div class=\"cell "
                f"{escape(state)}\" data-cell=\"{row_index},{col_index}\">"
                f"{escape(text)}"
                "</div>"
            )
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
html, body {{
  margin: 0; width: {viewport_width}px; height: {viewport_height}px;
  overflow: hidden; background: #ffffff;
}}
body {{ font-family: Arial, sans-serif; }}
.topbar {{ height: 64px; background: #1f2937; }}
.board {{
  position: absolute; left: {board_rect.x}px; top: {board_rect.y}px;
  width: {board_rect.width}px; height: {board_rect.height}px;
  display: grid; grid-template-columns: repeat({cols}, 1fr);
  grid-template-rows: repeat({rows}, 1fr); box-sizing: border-box;
  border: 4px solid #111827;
}}
.cell {{
  box-sizing: border-box; display: flex; align-items: center; justify-content: center;
  border: 2px solid #9ca3af; font-size: 24px; line-height: 1; user-select: none;
}}
.covered {{ background: #d1d5db; color: #111827; }}
.revealed {{ background: #f9fafb; color: #1d4ed8; }}
.flagged {{ background: #fecaca; color: #b91c1c; }}
.flagged::before {{ content: "⚑"; font-size: 34px; }}
</style>
</head>
<body><div class="topbar"></div><main class="board">{"".join(cells)}</main></body>
</html>"""
