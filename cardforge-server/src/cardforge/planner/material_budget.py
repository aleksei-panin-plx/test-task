"""Set material budget: few cards with bare metal as the main material (fable-review §2 Q1).

Image models draw bare metal (chrome, steel, silver, brass, ...) photographically, and prompt
text does not fix it; the cheapest lever is not to order it. The budget is data in
`config/material_budget.toml`. It never edits descriptions (that is `materials.py`); it only
counts each variant's bare-metal main materials and, above the budget, asks the planner to
swap the excess for painted metal or naturally coloured objects (`repair`), or warns.
"""

from __future__ import annotations

import math
import re
import tomllib
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from cardforge.contracts.request import CardDraft, PlanDraft

CONFIG = "config/material_budget.toml"
WORD = re.compile(r"[a-z]+")


@dataclass(frozen=True)
class MaterialBudget:
    version: str
    enabled: bool
    max_share: float
    max_cards: int
    action: str
    bare_metal: re.Pattern
    head_end: re.Pattern
    painted: frozenset[str]
    neutral: frozenset[str]
    hints: tuple[str, ...]

    def limit(self, n_cards: int) -> int:
        return min(self.max_cards, math.floor(self.max_share * n_cards + 1e-9))


def parse_budget(data: dict) -> MaterialBudget:
    if data["action"] not in ("repair", "warn"):
        raise ValueError(f"material budget: unknown action {data['action']!r}")
    if not 0 <= float(data["max_share"]) <= 1:
        raise ValueError("material budget: max_share must be within [0, 1]")
    words = sorted(data["bare_metal"], key=len, reverse=True)
    return MaterialBudget(
        version=data["version"],
        enabled=bool(data.get("enabled", True)),
        max_share=float(data["max_share"]),
        max_cards=int(data["max_cards"]),
        action=data["action"],
        bare_metal=re.compile(r"\b(?:" + "|".join(map(re.escape, words)) + r")\b", re.I),
        head_end=re.compile(data["head_end"], re.I),
        painted=frozenset(w.lower() for w in data["painted_colours"]),
        neutral=frozenset(w.lower() for w in data["neutral_colours"]),
        hints=tuple(data.get("prompt", {}).get("hints", [])),
    )


@lru_cache(maxsize=8)
def _load(path: Path, mtime: float) -> MaterialBudget:
    return parse_budget(tomllib.loads(path.read_text(encoding="utf-8")))


def load_budget(root: Path) -> MaterialBudget:
    path = (root / CONFIG).resolve()
    return _load(path, path.stat().st_mtime)


def head(budget: MaterialBudget, text: str) -> str:
    m = budget.head_end.search(text)
    return text[: m.start()] if m else text


def bare_metal_word(
    budget: MaterialBudget, text: str | None, materials: list[str] | tuple = ()
) -> str | None:
    """The bare metal named as the main material of a description, or None.

    With a materials list, only metal cards count (no "golden hay bale"). A metal-first card
    whose head is only grey or black ("long black blacksmith tongs") is bare dark metal.
    """
    if not text or (materials and "metal" not in materials):
        return None
    h = head(budget, text)
    words = WORD.findall
    for m in budget.bare_metal.finditer(h):
        if h[m.end() :].startswith("-"):  # colour compound: copper-green, silver-grey
            continue
        if set(words(h[: m.start()].lower())) & budget.painted:  # green cast-iron, red-and-gold
            continue
        return m.group(0).lower()
    if list(materials[:1]) == ["metal"]:
        tokens = set(words(h.lower()))
        dark = tokens & budget.neutral
        if dark and not tokens & budget.painted:
            return f"{sorted(dark)[0]} metal"
    return None


def is_bare_metal(budget: MaterialBudget, card: CardDraft) -> bool:
    return bare_metal_word(budget, card.object_description, card.materials) is not None


def check_variant(budget: MaterialBudget, plan: PlanDraft) -> dict:
    """Bare-metal cards of one variant and the budget finding (None when within budget)."""
    hits = []
    for no, card in enumerate(plan.cards, 1):
        word = bare_metal_word(budget, card.object_description, card.materials)
        if word:
            hits.append({"card": f"{plan.variant_id}_c{no:02d}", "metal": word})
    limit = budget.limit(len(plan.cards))
    message = None
    if len(hits) > limit:
        listed = ", ".join(f"{h['card']} ({h['metal']})" for h in hits)
        message = (
            f"{plan.variant_id}: {len(hits)} cards with bare metal as the main material "
            f"({listed}); the set budget is {limit} of {len(plan.cards)}. Keep the "
            f"{limit} most important and replace the others with painted metal ('red painted "
            "metal ...') or objects that are naturally painted, wooden or fabric"
        )
    return {"variant": plan.variant_id, "cards": hits, "limit": limit, "message": message}


def check_plans(budget: MaterialBudget, plans: list[PlanDraft], last_round: bool = False) -> dict:
    """Budget findings for every variant: errors (repair) or warnings."""
    if not budget.enabled:
        return {"variants": [], "errors": [], "warnings": []}
    variants = [check_variant(budget, p) for p in plans]
    messages = [v["message"] for v in variants if v["message"]]
    as_error = budget.action == "repair" and not last_round
    return {
        "variants": variants,
        "errors": messages if as_error else [],
        "warnings": [] if as_error else messages,
    }
