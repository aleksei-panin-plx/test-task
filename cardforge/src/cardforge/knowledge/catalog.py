"""Reference catalog validation and explicit human-review overlays; no ML imports."""

from __future__ import annotations

import colorsys
import hashlib
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any, Literal

from PIL import Image
from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Background(StrictModel):
    motif: str
    color: str


class PaletteColor(StrictModel):
    rgb: tuple[int, int, int]
    fraction: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def valid_rgb(self) -> PaletteColor:
        if any(not 0 <= x <= 255 for x in self.rgb):
            raise ValueError("RGB channels must be 0..255")
        return self


class Measurements(StrictModel):
    fill_ratio: float = Field(ge=0, le=1)
    bg_palette: list[PaletteColor]
    object_bbox: tuple[int, int, int, int] | None
    mask_method: str
    mask_path: str
    mask_quality: Literal["unreviewed", "usable", "suspect"] = "unreviewed"
    mask_quality_note: str = ""


class Card(StrictModel):
    card_ref: str = Field(pattern=r"^CardC6_card_\d{2,3}$")
    set_index: int = Field(ge=1, le=16)
    position: int = Field(ge=1, le=10)
    set_theme: str
    category: int = Field(ge=1, le=5)
    object_name: str
    scale_class: str
    object_type: str
    background: Background
    surface: str
    environment: str
    materials: list[str] = Field(min_length=1)
    has_alpha: bool
    alpha_meaning: Literal["absent", "fully_opaque", "non_opaque"]
    source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    notes: list[str]
    measured: Measurements
    label_source: Literal["agent", "human"]

    @model_validator(mode="after")
    def consistent_index(self) -> Card:
        n = int(self.card_ref.rsplit("_", 1)[1])
        if (n - 1) // 10 + 1 != self.set_index or (n - 1) % 10 + 1 != self.position:
            raise ValueError("card_ref and numeric set/position disagree")
        if self.has_alpha == (self.alpha_meaning == "absent"):
            raise ValueError("has_alpha and alpha_meaning disagree")
        if len(set(self.materials)) != len(self.materials):
            raise ValueError("duplicate material")
        return self


LABEL_FIELDS = (
    "set_theme",
    "category",
    "object_name",
    "scale_class",
    "object_type",
    "background",
    "surface",
    "environment",
    "materials",
)


class HumanReview(StrictModel):
    card_ref: str
    source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    reviewer: str = Field(min_length=1)
    reviewed_at: str = Field(min_length=1)
    confirmed_fields: list[str]
    corrections: dict[str, Any] = Field(default_factory=dict)
    note: str = ""
    label_source: Literal["human"] = "human"

    @model_validator(mode="after")
    def scope(self) -> HumanReview:
        if not {"category", "set_theme"}.issubset(self.confirmed_fields):
            raise ValueError("human review must confirm category and set_theme")
        if set(self.confirmed_fields) - set(LABEL_FIELDS):
            raise ValueError("unknown confirmation field")
        if set(self.corrections) - set(self.confirmed_fields):
            raise ValueError("all corrections must be explicitly confirmed")
        return self


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(s) for s in path.read_text().splitlines() if s.strip()]


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))


def load_cards(root: Path, *, apply_human: bool = True, apply_masks: bool = True) -> list[Card]:
    folder = root / "knowledge/catalog"
    cards = [Card.model_validate(r) for r in read_jsonl(folder / "cards.jsonl")]
    by_id = {c.card_ref: c for c in cards}
    if len(by_id) != len(cards):
        raise ValueError("duplicate card_ref")
    if apply_human and (folder / "human_review.jsonl").exists():
        seen = set()
        for row in read_jsonl(folder / "human_review.jsonl"):
            review = HumanReview.model_validate(row)
            if review.card_ref in seen or review.card_ref not in by_id:
                raise ValueError("duplicate or unknown review card")
            seen.add(review.card_ref)
            card = by_id[review.card_ref]
            if review.source_sha256 != card.source_sha256:
                raise ValueError("review references stale source hash")
            data = card.model_dump()
            data.update(review.corrections)
            data["label_source"] = "human"
            by_id[card.card_ref] = Card.model_validate(data)
    result = [by_id[c.card_ref] for c in cards]
    if apply_masks:
        from cardforge.knowledge.mask_review import apply_reviews

        result = apply_reviews(root, result)
    return result


def vocabulary_errors(card: Card, vocab: dict) -> list[str]:
    fields = {
        k: getattr(card, k)
        for k in (
            "set_theme",
            "object_name",
            "scale_class",
            "object_type",
            "surface",
            "environment",
        )
    }
    fields.update(motif=card.background.motif, color=card.background.color)
    errors = [
        f"{card.card_ref}: {k}={v!r} outside vocabulary"
        for k, v in fields.items()
        if v not in vocab[k]
    ]
    errors += [
        f"{card.card_ref}: material {m!r} outside vocabulary"
        for m in card.materials
        if m not in vocab["materials"]
    ]
    return errors


def color_family(rgb: tuple[int, int, int]) -> str:
    h, s, v = colorsys.rgb_to_hsv(*(x / 255 for x in rgb))
    h *= 360
    if s < 0.18 or v < 0.12:
        return "neutral"
    if 15 <= h < 65 and v < 0.6:
        return "brown"
    for upper, name in [
        (15, "red"),
        (45, "orange"),
        (70, "yellow"),
        (165, "green"),
        (195, "cyan"),
        (255, "blue"),
        (290, "purple"),
        (345, "pink"),
    ]:
        if h < upper:
            return name
    return "red"


def palette_agrees(card: Card) -> bool:
    # Background labels describe the back wall/sky. A textured foreground surface
    # can dominate the measured palette; mismatch is a review warning, not relabeling.
    support = sum(
        p.fraction for p in card.measured.bg_palette if color_family(p.rgb) == card.background.color
    )
    return support >= 0.15


def validate(root: Path, *, require_human: bool = False) -> dict:
    errors, warnings = [], []
    try:
        cards = load_cards(root)
    except (ValueError, OSError) as exc:
        return {"valid": False, "errors": [str(exc)], "warnings": []}
    expected = {f"CardC6_card_{n:02}" for n in range(1, 161)}
    if {c.card_ref for c in cards} != expected:
        errors.append("catalog must contain exactly source IDs 1..160")
    vocab = json.loads((root / "knowledge/catalog/vocabulary.yaml").read_text())
    for card in cards:
        errors.extend(vocabulary_errors(card, vocab))
        p = root / f"dataset/{card.card_ref}.png"
        if not p.exists() or hashlib.sha256(p.read_bytes()).hexdigest() != card.source_sha256:
            errors.append(f"{card.card_ref}: missing or changed source")
        elif Image.open(p).size != (430, 480):
            errors.append(f"{card.card_ref}: wrong source dimensions")
        if not palette_agrees(card):
            warnings.append(f"{card.card_ref}: background palette mismatch; inspect mask/surface")
    confirmed = sum(c.label_source == "human" for c in cards)
    review_path = root / "knowledge/catalog/human_review.jsonl"
    reviews = {r["card_ref"]: r for r in read_jsonl(review_path)} if review_path.exists() else {}
    full_sample = [c for c in cards if c.position in (2, 4, 6, 8)]
    full_confirmed = sum(
        set(LABEL_FIELDS).issubset(reviews.get(c.card_ref, {}).get("confirmed_fields", []))
        for c in full_sample
    )
    if require_human and confirmed != 160:
        errors.append(f"human-confirmed category/theme: {confirmed}/160")
    if require_human and full_confirmed != 64:
        errors.append(f"same-model-family full-field review: {full_confirmed}/64")
    return {
        "valid": not errors,
        "cards": len(cards),
        "human_confirmed": confirmed,
        "full_sample_confirmed": full_confirmed,
        "category_counts": dict(sorted(Counter(c.category for c in cards).items())),
        "errors": errors,
        "warnings": warnings,
    }


def initialize(root: Path, output: Path) -> int:
    if output.exists():
        raise ValueError(f"refusing to overwrite {output}")
    files = list((root / "dataset").glob("CardC6_card_*.png"))
    numbers = []
    for p in files:
        match = re.fullmatch(r"CardC6_card_(\d+)\.png", p.name)
        if not match:
            raise ValueError(f"invalid name: {p.name}")
        numbers.append((int(match[1]), p))
    if sorted(n for n, _ in numbers) != list(range(1, 161)):
        raise ValueError("expected each numeric source 1..160 exactly once")
    measured_path = root / "knowledge/catalog/measurements.json"
    measurements = (
        {r["card_ref"]: r for r in json.loads(measured_path.read_text())["rows"]}
        if measured_path.exists()
        else {}
    )
    rows = []
    for n, p in sorted(numbers):
        with Image.open(p) as im:
            has_alpha = "A" in im.getbands()
            meaning = (
                ("fully_opaque" if im.getchannel("A").getextrema() == (255, 255) else "non_opaque")
                if has_alpha
                else "absent"
            )
        rows.append(
            {
                "card_ref": p.stem,
                "set_index": (n - 1) // 10 + 1,
                "position": (n - 1) % 10 + 1,
                "has_alpha": has_alpha,
                "alpha_meaning": meaning,
                "source_sha256": hashlib.sha256(p.read_bytes()).hexdigest(),
                "measured": measurements.get(p.stem),
                "annotation_status": "pending",
            }
        )
    write_jsonl(output, rows)
    return len(rows)
