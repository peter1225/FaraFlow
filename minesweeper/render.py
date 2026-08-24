"""Simple deterministic PNG renderer for local Grounding Gate smoke data."""

from __future__ import annotations

from io import BytesIO

from PIL import Image, ImageDraw

from .engine import Cell, MinesweeperBoard
from .geometry import BoardRectCss


def render_board_png(
    board: MinesweeperBoard,
    board_rect: BoardRectCss,
    *,
    image_size: tuple[int, int] = (1440, 900),
    highlight_cell: Cell | None = None,
) -> bytes:
    """Render only the visible state; mine truth is never drawn."""

    image = Image.new("RGB", image_size, "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, image_size[0] - 1, 64), fill="#1f2937")
    draw.rectangle(
        (
            board_rect.x,
            board_rect.y,
            board_rect.x + board_rect.width,
            board_rect.y + board_rect.height,
        ),
        outline="#111827",
        width=4,
    )
    for row in range(board.config.height):
        for col in range(board.config.width):
            cell = (row, col)
            left = board_rect.x + col * board_rect.cell_width
            top = board_rect.y + row * board_rect.cell_height
            right = left + board_rect.cell_width
            bottom = top + board_rect.cell_height
            visible = board.visible_cell(cell)
            if visible.state == "covered":
                fill = "#d1d5db"
            elif visible.state == "flagged":
                fill = "#fecaca"
            else:
                fill = "#f9fafb"
            draw.rectangle((left, top, right, bottom), fill=fill, outline="#9ca3af", width=2)
            if visible.state == "revealed" and visible.adjacent_mines:
                draw.text(
                    (left + board_rect.cell_width * 0.42, top + board_rect.cell_height * 0.3),
                    str(visible.adjacent_mines),
                    fill="#1d4ed8",
                )
            elif visible.state == "flagged":
                draw.polygon(
                    [
                        (
                            left + board_rect.cell_width * 0.35,
                            bottom - board_rect.cell_height * 0.25,
                        ),
                        (left + board_rect.cell_width * 0.35, top + board_rect.cell_height * 0.25),
                        (left + board_rect.cell_width * 0.75, top + board_rect.cell_height * 0.45),
                    ],
                    fill="#b91c1c",
                )
    if highlight_cell is not None:
        row, col = highlight_cell
        if not (0 <= row < board.config.height and 0 <= col < board.config.width):
            raise ValueError(f"highlight cell outside board: {highlight_cell}")
        left = board_rect.x + col * board_rect.cell_width
        top = board_rect.y + row * board_rect.cell_height
        right = left + board_rect.cell_width
        bottom = top + board_rect.cell_height
        draw.rectangle(
            (left + 4, top + 4, right - 4, bottom - 4),
            outline="#16a34a",
            width=6,
        )
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()
