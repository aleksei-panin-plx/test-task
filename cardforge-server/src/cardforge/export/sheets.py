"""5×2 variant sheets and framed previews (TA §7.2, roadmap stage 4 item 10).

The frame (`knowledge/frames/frame_preview.png`) is for preview only and never enters the
delivered cards.
"""

from __future__ import annotations

import io
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from cardforge.orchestrator.store import write_atomic_bytes

TILE = (430, 480)
COLUMNS, ROWS = 5, 2


# PIL's default font has no Cyrillic (labels came out as boxes): a system font with Cyrillic, else
# the Latin label.
FONTS = (
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/Library/Fonts/Arial Unicode.ttf",
    "C:/Windows/Fonts/arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
)
_FONT: dict[int, tuple[ImageFont.ImageFont, bool]] = {}


def _font(size: int = 22) -> tuple[ImageFont.ImageFont, bool]:
    if size not in _FONT:
        for path in FONTS:
            try:
                _FONT[size] = (ImageFont.truetype(path, size), True)
                break
            except OSError:
                continue
        else:
            _FONT[size] = (ImageFont.load_default(size=size), False)
    return _FONT[size]


def _label(draw: ImageDraw.ImageDraw, xy, ru: str, en: str, fill, size: int = 22) -> None:
    font, cyrillic = _font(size)
    draw.text(xy, ru if cyrillic else en, fill=fill, font=font)


def _png(image: Image.Image) -> bytes:
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


def placeholder(card_id: str, state: str) -> Image.Image:
    if state == "золотая карта":  # collection mode: a gold slot, drawn separately
        tile = Image.new("RGB", TILE, (246, 226, 160))
        draw = ImageDraw.Draw(tile)
        draw.rectangle([4, 4, TILE[0] - 5, TILE[1] - 5], outline=(200, 160, 40), width=6)
        _label(draw, (24, TILE[1] // 2 - 30), "ЗОЛОТАЯ КАРТА", "GOLD CARD", (120, 90, 10), 34)
        brown = (120, 90, 10)
        _label(draw, (24, TILE[1] // 2 + 12), "рисуется отдельно", "drawn separately", brown)
        return tile
    tile = Image.new("RGB", TILE, (225, 225, 225))
    draw = ImageDraw.Draw(tile)
    draw.rectangle([4, 4, TILE[0] - 5, TILE[1] - 5], outline=(160, 160, 160), width=4)
    text = f"{card_id}: {state}"
    _label(draw, (24, TILE[1] // 2 - 10), text, text, (90, 90, 90))
    return tile


def mark_auto_pass(tile: Image.Image, card_id: str) -> Image.Image:
    """G3 (2026-11-09): an auto-passed card — accepted without G2 — gets a green border and a
    label with its id, so the person can return it from the set sheet."""
    tile = tile.copy()
    draw = ImageDraw.Draw(tile)
    draw.rectangle([0, 0, TILE[0] - 1, TILE[1] - 1], outline=(30, 150, 70), width=10)
    draw.rectangle([10, TILE[1] - 48, 250, TILE[1] - 10], fill=(30, 150, 70))
    _label(draw, (20, TILE[1] - 40), f"АВТО · {card_id}", f"AUTO · {card_id}", (255, 255, 255))
    return tile


def mark_unconfirmed(tile: Image.Image, card_id: str) -> Image.Image:
    """G2 = none (2026-11-10): a card in the set without a judge's confirmation — orange border and
    label, so the person checks it at G3 (accept, redraw with an edit or swap an alternative)."""
    tile = tile.copy()
    draw = ImageDraw.Draw(tile)
    draw.rectangle([0, 0, TILE[0] - 1, TILE[1] - 1], outline=(230, 120, 0), width=10)
    draw.rectangle([10, TILE[1] - 48, 400, TILE[1] - 10], fill=(230, 120, 0))
    _label(draw, (20, TILE[1] - 40), f"НЕ ПОДТВЕРЖДЕНА · {card_id}", f"UNCONFIRMED · {card_id}",
           (255, 255, 255))
    return tile


def write_sheet(
    target: Path,
    tiles: list[tuple[str, Path | None, str]],
    marked: frozenset[str] = frozenset(),
    *,
    unconfirmed: frozenset[str] = frozenset(),
) -> Path:
    """`tiles` are (card_id, final image or None, state) in card order; `marked` = auto-passed,
    `unconfirmed` = in the set without a judge's confirmation (G2 = none)."""
    sheet = Image.new("RGB", (TILE[0] * COLUMNS, TILE[1] * ROWS), (255, 255, 255))
    for i, (card_id, path, state) in enumerate(tiles[: COLUMNS * ROWS]):
        if path is not None and path.exists():
            with Image.open(path) as im:
                tile = im.convert("RGB").resize(TILE)
        else:
            tile = placeholder(card_id, state)
        if card_id in marked:
            tile = mark_auto_pass(tile, card_id)
        elif card_id in unconfirmed:
            tile = mark_unconfirmed(tile, card_id)
        sheet.paste(tile, ((i % COLUMNS) * TILE[0], (i // COLUMNS) * TILE[1]))
    write_atomic_bytes(target, _png(sheet))
    return target


def write_framed(target: Path, final: Path, frame: Path) -> Path:
    with Image.open(final) as art, Image.open(frame) as fr:
        card = art.convert("RGBA").resize(fr.size)
        card.alpha_composite(fr.convert("RGBA"))
    write_atomic_bytes(target, _png(card.convert("RGB")))
    return target
