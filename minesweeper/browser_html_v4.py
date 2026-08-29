"""Theme and scroll-aware browser renderer for Canonical V4 boards."""

from __future__ import annotations

from html import escape
from typing import Any, Literal

from .geometry import BoardRectCss

ThemeV4 = Literal["light", "dark"]


def visible_state_to_html_v4(
    visible_state: list[list[dict[str, Any]]],
    board_rect: BoardRectCss,
    *,
    viewport: tuple[int, int] = (1440, 900),
    theme: ThemeV4 = "light",
    scroll_offset: int = 0,
) -> str:
    rows = len(visible_state)
    cols = len(visible_state[0]) if rows else 0
    if rows <= 0 or cols <= 0 or any(len(row) != cols for row in visible_state):
        raise ValueError("visible_state must be a non-empty rectangular grid")
    if theme not in {"light", "dark"}:
        raise ValueError(f"unsupported theme: {theme}")
    viewport_width, viewport_height = viewport
    if scroll_offset < 0:
        raise ValueError("scroll_offset must be non-negative")

    palette = (
        {
            "page": "#f7f2e8",
            "panel": "#ffffff",
            "topbar": "#31566f",
            "border": "#4b5563",
            "covered": "#c7ccd1",
            "revealed": "#f9fafb",
            "flagged": "#f5c2c2",
            "number": "#174ea6",
            "flag": "#b42318",
            "text": "#17212b",
        }
        if theme == "light"
        else {
            "page": "#111827",
            "panel": "#1f2937",
            "topbar": "#030712",
            "border": "#6b7280",
            "covered": "#4b5563",
            "revealed": "#d1d5db",
            "flagged": "#7f1d1d",
            "number": "#1d4ed8",
            "flag": "#fee2e2",
            "text": "#f9fafb",
        }
    )
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
                f"{escape(text)}</div>"
            )

    document_top = board_rect.y + scroll_offset
    page_height = max(
        viewport_height + scroll_offset + 80,
        int(document_top + board_rect.height + 80),
    )
    font_size = max(
        13,
        min(30, int(min(board_rect.cell_width, board_rect.cell_height) * 0.48)),
    )
    flag_size = max(16, min(36, int(font_size * 1.35)))
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
html, body {{ margin: 0; width: {viewport_width}px; min-height: {page_height}px; }}
body {{ background: {palette['page']}; color: {palette['text']}; font-family: Arial, sans-serif; }}
.topbar {{ position: absolute; inset: 0 0 auto 0; height: 64px; background: {palette['topbar']}; }}
.title {{ position: absolute; left: 28px; top: 20px; font-weight: 700; color: #ffffff; }}
.panel {{
  position: absolute; left: 20px; right: 20px; top: {84 + scroll_offset}px;
  height: {max(100, int(board_rect.height + 110))}px; border-radius: 12px;
  background: {palette['panel']}; box-shadow: 0 8px 24px rgba(0,0,0,.12);
}}
.board {{
  position: absolute; left: {board_rect.x}px; top: {document_top}px;
  width: {board_rect.width}px; height: {board_rect.height}px;
  display: grid; grid-template-columns: repeat({cols}, 1fr);
  grid-template-rows: repeat({rows}, 1fr); box-sizing: border-box;
  border: 4px solid {palette['border']};
}}
.cell {{
  box-sizing: border-box; display: flex; align-items: center; justify-content: center;
  border: 1px solid {palette['border']}; font-size: {font_size}px;
  font-weight: 700; line-height: 1; user-select: none;
}}
.covered {{ background: {palette['covered']}; }}
.revealed {{ background: {palette['revealed']}; color: {palette['number']}; }}
.flagged {{ background: {palette['flagged']}; color: {palette['flag']}; }}
.flagged::before {{ content: "⚑"; font-size: {flag_size}px; }}
</style>
</head>
<body>
<div class="topbar"></div><div class="title">Minesweeper</div><div class="panel"></div>
<main class="board">{"".join(cells)}</main>
</body>
</html>"""
