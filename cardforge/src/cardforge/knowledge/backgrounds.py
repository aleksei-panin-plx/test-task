"""Background plates: reference cards of categories 1-2 with the object removed.

User request 2026-11-03: pass background references so generated patterns match the game.
A plate is the reference card with its object mask (dilated) filled by OpenCV inpainting,
so a generator sees the motif, its colours and contrast but no object to copy. Plates are
derived customer art and stay in the ignored `dataset/` folder. OpenCV comes from the
`analysis` extra and is imported only here.
"""

from __future__ import annotations

import json
from pathlib import Path

from PIL import Image

from cardforge.knowledge.catalog import Card, load_cards

PLATES = "dataset/analysis/backgrounds"


def plate_path(root: Path, card_ref: str) -> Path:
    return root / PLATES / f"{card_ref}.png"


def build_plates(root: Path, *, dilate_px: int = 9) -> dict:
    import cv2
    import numpy as np

    out = root / PLATES
    out.mkdir(parents=True, exist_ok=True)
    made, skipped = [], []
    for card in load_cards(root, apply_masks=False):
        if card.category not in (1, 2):
            continue
        mask_rel = card.measured.mask_path if card.measured else None
        if card.measured is None or card.measured.mask_quality != "usable" or not mask_rel:
            skipped.append(card.card_ref)
            continue
        image = cv2.imread(str(root / f"dataset/{card.card_ref}.png"), cv2.IMREAD_COLOR)
        mask = cv2.imread(str(root / mask_rel), cv2.IMREAD_GRAYSCALE)
        mask = (mask >= 128).astype("uint8") * 255
        kernel = np.ones((dilate_px, dilate_px), np.uint8)
        mask = cv2.dilate(mask, kernel, iterations=2)
        plate = cv2.inpaint(image, mask, 9, cv2.INPAINT_TELEA)
        cv2.imwrite(str(plate_path(root, card.card_ref)), plate)
        made.append(card.card_ref)
    index = {"method": "rembg mask dilated, OpenCV TELEA inpaint", "plates": made,
             "skipped_unusable_mask": skipped}  # fmt: skip
    (out / "index.json").write_text(json.dumps(index, indent=2) + "\n")
    return {"plates": len(made), "skipped": len(skipped)}


def plates_by_motif(root: Path) -> dict[str, list[Card]]:
    out: dict[str, list[Card]] = {}
    for card in load_cards(root, apply_masks=False):
        if card.category in (1, 2) and plate_path(root, card.card_ref).exists():
            out.setdefault(card.background.motif, []).append(card)
    return out


def contact_sheet(root: Path, target: Path) -> Path:
    from PIL import ImageDraw

    groups = plates_by_motif(root)
    w, h, label = 172, 192, 110
    rows = sorted(groups.items(), key=lambda kv: -len(kv[1]))
    width = label + w * max(len(v) for _, v in rows)
    sheet = Image.new("RGB", (width, h * len(rows)), "white")
    draw = ImageDraw.Draw(sheet)
    for r, (motif, cards) in enumerate(rows):
        draw.text((6, r * h + 90), motif, fill="black")
        for i, card in enumerate(cards):
            with Image.open(plate_path(root, card.card_ref)) as im:
                sheet.paste(im.convert("RGB").resize((w, h)), (label + i * w, r * h))
            draw.text((label + i * w + 4, r * h + 4), card.card_ref[-3:].lstrip("_"), fill="black")
    target.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(target)
    return target
