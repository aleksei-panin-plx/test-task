"""Hash-bound technical mask overlays. Never change human semantic annotations."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Literal

from PIL import Image
from pydantic import BaseModel, ConfigDict, Field

from cardforge.evaluate.common import private_path, sha256

if TYPE_CHECKING:
    from cardforge.knowledge.catalog import Card


class MaskReview(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    card_ref: str
    source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    original_mask_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    mask_path: str
    mask_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    reviewer: str = Field(min_length=1)
    review_source: Literal["agent-visual", "human"]
    review_note: str = Field(min_length=1)
    method: str = Field(min_length=1)
    decision: Literal["usable", "suspect"]
    evidence: str
    evidence_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    measurements: dict


def apply_reviews(root: Path, cards: list[Card]) -> list[Card]:
    from cardforge.knowledge.catalog import Measurements, read_jsonl

    path = root / "knowledge/catalog/mask_review.jsonl"
    if not path.exists():
        return cards
    reviews = [MaskReview.model_validate(row) for row in read_jsonl(path)]
    by_id = {card.card_ref: card for card in cards}
    if len({r.card_ref for r in reviews}) != len(reviews):
        raise ValueError("duplicate technical mask review")
    for review in reviews:
        card = by_id.get(review.card_ref)
        if card is None or card.source_sha256 != review.source_sha256:
            raise ValueError("mask review references unknown/stale source")
        source = root / f"dataset/{card.card_ref}.png"
        original = private_path(root, card.measured.mask_path)
        mask = private_path(root, review.mask_path)
        if not mask.is_relative_to((root / "dataset").resolve()):
            raise ValueError("reference mask must remain in private dataset")
        if (
            sha256(source) != review.source_sha256
            or sha256(original) != review.original_mask_sha256
            or sha256(mask) != review.mask_sha256
            or sha256(private_path(root, review.evidence)) != review.evidence_sha256
        ):
            raise ValueError("mask review input/evidence bytes changed")
        with Image.open(mask) as image, Image.open(source) as art:
            if image.mode != "L" or image.size != art.size or image.getextrema() != (0, 255):
                raise ValueError("reviewed mask must be a nonempty source-sized L mask")
            colors = image.getcolors(maxcolors=3)
            if colors is None or {v for _, v in colors} != {0, 255}:
                raise ValueError("reviewed mask must be binary")
            filled = next(n for n, value in colors if value == 255)
            expected_area = filled / (image.width * image.height)
            expected_bbox = image.getbbox()
        if review.decision == "usable":
            measured = Measurements.model_validate(review.measurements)
            if measured.mask_path != review.mask_path or measured.mask_quality != "usable":
                raise ValueError("reviewed measurements contradict technical decision")
            if measured.fill_ratio != expected_area or measured.object_bbox != expected_bbox:
                raise ValueError("reviewed geometry contradicts mask pixels")
            # The semantic/human layer and the raw source measurements remain intact.
            by_id[card.card_ref] = card.model_copy(update={"measured": measured})
    return [by_id[card.card_ref] for card in cards]
