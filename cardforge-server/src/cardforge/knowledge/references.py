"""Reference selection by role from catalog tags (TA §6.5).

1-2 images per role, 3-6 in total, deterministic for a seed. Gold character cards
(category 5) are never references for object cards. Paths point into the ignored
reference folder; nothing is copied.

Material atlas (H5, `knowledge/materials-atlas.toml`): with the flag on, the "material" role
comes from curated cards for the card's main material instead of a random catalog match.
Off by default; with it off the selection is byte-identical to before.

Material swatches and matched references (Q4/Q5, `knowledge/material-swatches.toml`): with
their flags on, the material role becomes one swatch sheet (role "swatch") for the atlas
material, and style/category references prefer the same scale and support. Both off by default.
"""

from __future__ import annotations

import json
import os
import random
import re
import tomllib
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from cardforge.contracts.generation import Reference
from cardforge.contracts.request import CardDraft
from cardforge.knowledge.catalog import Card

SCALES = ["handheld", "tabletop", "furniture", "vehicle", "structure"]


def _pick(pool: list[Card], k: int, rng: random.Random) -> list[Card]:
    pool = sorted(pool, key=lambda c: c.card_ref)
    return rng.sample(pool, min(k, len(pool)))


MATERIALS_ATLAS = "knowledge/materials-atlas.toml"
ATLAS_ENV = "CARDFORGE_MATERIAL_ATLAS"


@dataclass(frozen=True)
class AtlasEntry:
    key: str
    label: str
    aliases: tuple[str, ...]  # spec material tokens that mean this entry
    refines: tuple[str, ...]  # generic tokens ("metal", "organic") its keywords refine
    keywords: tuple[str, ...]  # words in the description/name used for that refinement
    subjects: tuple[str, ...]  # words naming the subject (car, sky); they beat materials
    refs: tuple[str, ...]


def _earliest(entries: list[AtlasEntry], words: str, text: str) -> str | None:
    hits = [
        (m.start(), e.key)
        for e in entries
        for w in getattr(e, words)
        if (m := re.search(rf"\b{re.escape(w)}\b", text))
    ]
    return min(hits)[1] if hits else None


@dataclass(frozen=True)
class MaterialAtlas:
    version: str
    enabled: bool
    per_card: int
    entries: dict[str, AtlasEntry]

    def active(self) -> bool:
        env = os.environ.get(ATLAS_ENV, "").strip().lower()
        if env in ("1", "true", "on", "yes"):
            return True
        if env in ("0", "false", "off", "no"):
            return False
        return self.enabled

    def main_material(self, spec: CardDraft) -> str | None:
        """Atlas key for the card.

        1. A subject word in the description or name (car, taxi, clouds) wins, earliest first.
        2. Otherwise the first spec material the atlas knows. A generic token ("metal",
           "organic") is refined by the earliest keyword of the entries that refine it
           (brass -> gold, roses -> flowers); with no keyword it falls back to its alias,
           if any ("metal" -> painted metal), else the next material is tried.
        """
        text = f"{spec.object_description or ''} {spec.object_name}".lower()
        entries = list(self.entries.values())
        if key := _earliest(entries, "subjects", text):
            return key
        aliases = {a: e.key for e in entries for a in e.aliases}
        for raw in spec.materials:
            token = raw.strip().lower()
            refiners = [e for e in entries if token in e.refines]
            if key := (_earliest(refiners, "keywords", text) or aliases.get(token)):
                return key
        return None


def parse_atlas(data: dict) -> MaterialAtlas:
    def words(v: dict, name: str) -> tuple[str, ...]:
        return tuple(w.strip().lower() for w in v.get(name, []))

    entries = {
        k: AtlasEntry(
            key=k,
            label=v.get("label", k),
            aliases=words(v, "aliases"),
            refines=words(v, "refines"),
            keywords=words(v, "keywords"),
            subjects=words(v, "subjects"),
            refs=tuple(v["refs"]),
        )
        for k, v in data.get("materials", {}).items()
    }
    seen: dict[str, str] = {}
    for e in entries.values():
        if not e.refs:
            raise ValueError(f"atlas entry {e.key} has no refs")
        if e.refines and not e.keywords:
            raise ValueError(f"atlas entry {e.key} refines {e.refines} but has no keywords")
        for a in e.aliases:
            if seen.setdefault(a, e.key) != e.key:
                raise ValueError(f"alias {a!r} in both {seen[a]} and {e.key}")
    return MaterialAtlas(
        version=data.get("version", ""),
        enabled=bool(data.get("enabled", False)),
        per_card=int(data.get("per_card", 1)),
        entries=entries,
    )


def _project_root() -> Path:
    return Path(os.environ.get("CARDFORGE_HOME") or Path(__file__).resolve().parents[3])


@lru_cache(maxsize=4)
def _load_atlas_cached(path: str) -> MaterialAtlas | None:
    p = Path(path)
    if not p.exists():
        return None
    return parse_atlas(tomllib.loads(p.read_text(encoding="utf-8")))


def load_atlas(root: Path | None = None) -> MaterialAtlas | None:
    return _load_atlas_cached(str((root or _project_root()) / MATERIALS_ATLAS))


def atlas_problems(atlas: MaterialAtlas, catalog: list[Card]) -> list[str]:
    """Refs must exist in the catalog and never be gold character cards (category 5)."""
    by_id = {c.card_ref: c for c in catalog}
    out = []
    for e in atlas.entries.values():
        for r in e.refs:
            if r not in by_id:
                out.append(f"{e.key}: {r} not in catalog")
            elif by_id[r].category == 5:
                out.append(f"{e.key}: {r} is a category-5 character card")
    return out


def select_references(
    catalog: list[Card],
    spec: CardDraft,
    style_ids: list[str],
    seed: int,
    refs_dir: Path,
    atlas: MaterialAtlas | None = None,
    swatches: SwatchBook | None = None,
) -> list[Reference]:
    """`atlas=None` / `swatches=None` load the project files; each acts only when its flag is on."""
    rng = random.Random(seed)
    objects = [c for c in catalog if c.category != 5]
    by_id = {c.card_ref: c for c in objects}
    book = swatches or load_swatches()
    matched = book is not None and book.matched_active()
    style_pool = [by_id[i] for i in style_ids if i in by_id]
    if matched:
        style = _pick_tiers(_style_tiers(style_pool, spec), 1, rng)
    else:
        style = _pick(style_pool, 1, rng)
    used = {c.card_ref for c in style}
    near = {spec.scale_class}
    i = SCALES.index(spec.scale_class)
    near |= set(SCALES[max(0, i - 1) : i + 2])
    near_pool = [
        c
        for c in objects
        if c.category == spec.category and c.scale_class in near and c.card_ref not in used
    ]
    if matched:
        category = _pick_tiers(_category_tiers(objects, spec, used) + [near_pool], 2, rng)
    else:
        category = _pick(near_pool, 2, rng)
    used |= {c.card_ref for c in category}
    atlas = atlas or load_atlas()
    if book is not None and book.active() and (sheet := book.sheet_for(atlas, spec)):
        return [
            Reference(id=c.card_ref, role=role, path=str(refs_dir / f"{c.card_ref}.png"))
            for role, cards in (("style", style), ("category", category))
            for c in cards
        ] + [
            Reference(
                id=f"swatch:{sheet}", role="swatch", path=str(book.sheet_path(refs_dir, sheet))
            )
        ]
    material = _atlas_pick(atlas, spec, by_id, used, rng)
    materials = set(spec.materials)
    material = material or _pick(
        [
            c
            for c in objects
            if c.card_ref not in used
            and (materials & set(c.materials) or c.object_type == spec.object_type)
        ],
        1,
        rng,
    )
    return [
        Reference(id=c.card_ref, role=role, path=str(refs_dir / f"{c.card_ref}.png"))
        for role, cards in (("style", style), ("category", category), ("material", material))
        for c in cards
    ]


def _atlas_pick(
    atlas: MaterialAtlas | None,
    spec: CardDraft,
    by_id: dict[str, Card],
    used: set[str],
    rng: random.Random,
) -> list[Card]:
    """Curated cards for the main material, best first; [] falls back to the random match."""
    if atlas is None or not atlas.active():
        return []
    key = atlas.main_material(spec)
    if key is None:
        return []
    pool = [by_id[r] for r in atlas.entries[key].refs if r in by_id and r not in used]
    if not pool:
        return []
    # Deterministic per seed, but not always the first ref: rotate the curated order.
    start = rng.randrange(len(pool))
    pool = pool[start:] + pool[:start]
    return pool[: max(1, atlas.per_card)]


# ---------------------------------------------------------------- Q4 swatches / Q5 matched refs

MATERIAL_SWATCHES = "knowledge/material-swatches.toml"
SWATCH_ENV = "CARDFORGE_MATERIAL_SWATCHES"
MATCHED_ENV = "CARDFORGE_MATCHED_REFS"


def _flag(env: str, default: bool) -> bool:
    v = os.environ.get(env, "").strip().lower()
    if v in ("1", "true", "on", "yes"):
        return True
    if v in ("0", "false", "off", "no"):
        return False
    return default


@dataclass(frozen=True)
class SwatchBook:
    version: str
    enabled: bool
    matched_refs: bool
    tile: int
    gap: int
    crops: dict[str, tuple[tuple[str, int, int, int], ...]]  # atlas key -> (card no, x, y, size)

    def active(self) -> bool:
        return _flag(SWATCH_ENV, self.enabled)

    def matched_active(self) -> bool:
        return _flag(MATCHED_ENV, self.matched_refs)

    def sheet_for(self, atlas: MaterialAtlas | None, spec: CardDraft) -> str | None:
        """Atlas key of the card's main material when a swatch sheet exists for it."""
        if atlas is None:
            return None
        key = atlas.main_material(spec)
        return key if key in self.crops else None

    @staticmethod
    def sheet_path(refs_dir: Path, key: str) -> Path:
        return refs_dir / "swatches" / f"{key}.png"


def parse_swatches(data: dict) -> SwatchBook:
    crops = {}
    for k, v in data.get("swatches", {}).items():
        rows = tuple((str(n), int(x), int(y), int(s)) for n, x, y, s in v["crops"])
        if not 1 <= len(rows) <= 3:
            raise ValueError(f"swatch {k}: 1-3 crops expected")
        crops[k] = rows
    return SwatchBook(
        version=data.get("version", ""),
        enabled=bool(data.get("enabled", False)),
        matched_refs=bool(data.get("matched_refs", False)),
        tile=int(data.get("tile", 320)),
        gap=int(data.get("gap", 8)),
        crops=crops,
    )


@lru_cache(maxsize=4)
def _load_swatches_cached(path: str) -> SwatchBook | None:
    p = Path(path)
    if not p.exists():
        return None
    return parse_swatches(tomllib.loads(p.read_text(encoding="utf-8")))


def load_swatches(root: Path | None = None) -> SwatchBook | None:
    return _load_swatches_cached(str((root or _project_root()) / MATERIAL_SWATCHES))


def build_sheet(book: SwatchBook, key: str, cards_dir: Path):
    """The swatch sheet image for one material (PIL, RGB): crops side by side, no backdrop."""
    from PIL import Image

    rows = book.crops[key]
    t, g = book.tile, book.gap
    sheet = Image.new("RGB", (len(rows) * t + (len(rows) - 1) * g, t), "white")
    for i, (n, x, y, s) in enumerate(rows):
        with Image.open(cards_dir / f"CardC6_card_{n}.png") as im:
            tile = im.convert("RGB").crop((x, y, x + s, y + s))
        sheet.paste(tile.resize((t, t), Image.Resampling.LANCZOS), (i * (t + g), 0))
    return sheet


def _support(c) -> str:
    """Support type: nothing for category 1, the surface for 2-3, the environment for 4."""
    if c.category == 4:
        return c.environment or "none"
    return (c.surface or "none") if c.category in (2, 3) else "none"


def _style_tiers(pool: list[Card], spec: CardDraft) -> list[list[Card]]:
    return [
        [c for c in pool if c.scale_class == spec.scale_class and _support(c) == _support(spec)],
        [c for c in pool if c.scale_class == spec.scale_class],
        pool,
    ]


def _category_tiers(objects: list[Card], spec: CardDraft, used: set[str]) -> list[list[Card]]:
    same = [c for c in objects if c.category == spec.category and c.card_ref not in used]
    return [
        [c for c in same if c.scale_class == spec.scale_class and _support(c) == _support(spec)],
        [c for c in same if c.scale_class == spec.scale_class],
    ]


def _pick_tiers(tiers: list[list[Card]], k: int, rng: random.Random) -> list[Card]:
    """k cards, from the strictest tier first; each tier is a seeded sample of what is left."""
    out: list[Card] = []
    for tier in tiers:
        left = [c for c in tier if c.card_ref not in {x.card_ref for x in out}]
        out += _pick(left, k - len(out), rng)
        if len(out) == k:
            break
    return out


BACKGROUND_REFS = "knowledge/style_profile/background_refs_v1.json"


def background_guide(root: Path) -> dict:
    return json.loads((root / BACKGROUND_REFS).read_text(encoding="utf-8"))


def select_background(catalog: list[Card], spec: CardDraft, guide: dict) -> str | None:
    """A curated background plate (object removed) with the card's motif, same colour first."""
    if spec.background is None or spec.category == 4:
        return None
    plates = guide["plates"].get(spec.background.motif, [])
    colors = {c.card_ref: c.background.color for c in catalog}
    same = [p for p in plates if colors.get(p) == spec.background.color]
    return (same or plates or [None])[0]
