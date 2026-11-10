"""Material language rules for planner descriptions (H4, docs/research/generation-defects.md).

The rules are data in `config/material_rules.toml`: ordered regex rewrites of the English
`object_description` (painted metal by default, soft stylised chrome, slate blue-grey iron,
honey-orange wood, ...) and the sole-neutral check (grey or black is never an object's only
colour). Rewrites are deterministic and idempotent; the planner applies them after parsing
the drafts, before validation, so the frozen spec carries the rewritten text.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from cardforge.contracts.request import CardDraft, PlanDraft

CONFIG = "config/material_rules.toml"
WORD = re.compile(r"[a-z]+")


@dataclass(frozen=True)
class Rewrite:
    id: str
    pattern: re.Pattern
    replace: str
    unless_before: re.Pattern | None = None
    unless_after: re.Pattern | None = None
    requires_before: re.Pattern | None = None
    when_materials: frozenset[str] = frozenset()
    max_count: int = 0  # 0 = unlimited


@dataclass(frozen=True)
class MaterialRules:
    version: str
    enabled: bool
    rewrites: tuple[Rewrite, ...]
    neutral: frozenset[str]
    chromatic: frozenset[str]
    sole_neutral_action: str
    metal_dark: re.Pattern
    metal_dark_replace: str
    accent_template: str
    accent_for_background: dict[str, str]
    hints: tuple[str, ...]


@dataclass
class Outcome:
    before: str
    after: str
    rules: list[str] = field(default_factory=list)
    sole_neutral: bool = False  # still true after rewrites (and after the fix, if applied)
    fixed: bool = False

    @property
    def changed(self) -> bool:
        return self.before != self.after


def _compile(raw: str | None, chromatic: str, flags=re.IGNORECASE) -> re.Pattern | None:
    if raw is None:
        return None
    return re.compile(raw.replace("<chromatic>", chromatic), flags)


def parse_rules(data: dict) -> MaterialRules:
    colours = data["colours"]
    chromatic = r"\b(?:" + "|".join(sorted(colours["chromatic"], key=len, reverse=True)) + ")"
    rewrites = []
    for r in data.get("rewrite", []):
        rewrites.append(
            Rewrite(
                id=r["id"],
                pattern=_compile(r["pattern"], chromatic),
                replace=r["replace"],
                unless_before=_compile(r.get("unless_before"), chromatic),
                unless_after=_compile(r.get("unless_after"), chromatic),
                requires_before=_compile(r.get("requires_before"), chromatic),
                when_materials=frozenset(r.get("when_materials", [])),
                max_count=int(r.get("max_count", 0)),
            )
        )
    ids = [r.id for r in rewrites]
    if len(set(ids)) != len(ids):
        raise ValueError(f"material rules: rewrite ids repeat: {ids}")
    sole = data["sole_neutral"]
    if sole["action"] not in ("repair", "fix", "warn"):
        raise ValueError(f"material rules: unknown sole_neutral.action {sole['action']!r}")
    return MaterialRules(
        version=data["version"],
        enabled=bool(data.get("enabled", True)),
        rewrites=tuple(rewrites),
        neutral=frozenset(colours["neutral"]),
        chromatic=frozenset(colours["chromatic"]),
        sole_neutral_action=sole["action"],
        metal_dark=_compile(sole["metal_dark_pattern"], chromatic),
        metal_dark_replace=sole["metal_dark_replace"],
        accent_template=sole["accent_template"],
        accent_for_background=dict(sole["accent_for_background"]),
        hints=tuple(data.get("prompt", {}).get("hints", [])),
    )


@lru_cache(maxsize=8)
def _load(path: Path, mtime: float) -> MaterialRules:
    return parse_rules(tomllib.loads(path.read_text(encoding="utf-8")))


def load_rules(root: Path) -> MaterialRules:
    path = (root / CONFIG).resolve()
    return _load(path, path.stat().st_mtime)


# A rewrite that lands next to a word it already contains doubles it: "honey-brown wooden" →
# "honey-honey-orange wooden", "caramel brown leather" → "caramel caramel-brown leather", "warm
# golden medal" → "warm warm yellow-orange golden medal" (debt T7). Collapse the repeat.
DOUBLED = re.compile(r"\b(\w+)(?:[ -]\1\b)+", re.IGNORECASE)


def _tidy(text: str) -> str:
    text = DOUBLED.sub(r"\1", text)
    text = re.sub(r"\s{2,}", " ", text)
    return re.sub(r"\s+([,.;])", r"\1", text).strip()


def apply_rewrite(rule: Rewrite, text: str, materials: set[str]) -> tuple[str, int]:
    if rule.when_materials and not rule.when_materials & materials:
        return text, 0
    count = 0

    def sub(m: re.Match) -> str:
        nonlocal count
        before, after = m.string[: m.start()], m.string[m.end() :]
        if rule.max_count and count >= rule.max_count:
            return m.group(0)
        if rule.requires_before and not rule.requires_before.search(before):
            return m.group(0)
        if rule.unless_before and rule.unless_before.search(before):
            return m.group(0)
        if rule.unless_after and rule.unless_after.match(after):
            return m.group(0)
        count += 1
        return m.expand(rule.replace)

    out = rule.pattern.sub(sub, text)
    return (_tidy(out), count) if count else (text, 0)


def colour_words(rules: MaterialRules, text: str) -> set[str]:
    return set(WORD.findall(text.lower())) & (rules.neutral | rules.chromatic)


def is_sole_neutral(rules: MaterialRules, text: str) -> bool:
    found = colour_words(rules, text)
    return bool(found) and found <= rules.neutral


def fix_sole_neutral(rules: MaterialRules, text: str, materials: set[str], background: str | None):
    """Deterministic fallback: metal black → slate blue-grey, else add a complementary accent."""
    if "metal" in materials:
        text = _tidy(rules.metal_dark.sub(rules.metal_dark_replace, text))
        if not is_sole_neutral(rules, text):
            return text
    accent = rules.accent_for_background.get(background or "neutral", "red")
    word = re.compile(
        r"\b(?:soft stylised chrome|"
        + "|".join(sorted(rules.neutral, key=len, reverse=True))
        + r")\b",
        re.IGNORECASE,
    )
    m = word.search(text)
    if not m:
        return text
    new = rules.accent_template.format(accent=accent, word=m.group(0))
    return text[: m.start()] + new + text[m.end() :]


def normalize_text(
    rules: MaterialRules,
    text: str,
    materials: list[str] | set[str] = (),
    background: str | None = None,
    fix: bool | None = None,
) -> Outcome:
    """Apply every rewrite in order; then the sole-neutral check (and fix when asked)."""
    mats = set(materials)
    out = Outcome(before=text, after=text)
    for rule in rules.rewrites:
        out.after, n = apply_rewrite(rule, out.after, mats)
        if n:
            out.rules.append(rule.id)
    out.sole_neutral = is_sole_neutral(rules, out.after)
    if fix is None:
        fix = rules.sole_neutral_action == "fix"
    if out.sole_neutral and fix:
        out.after = fix_sole_neutral(rules, out.after, mats, background)
        out.fixed = True
        out.rules.append("sole_neutral_fix")
        out.sole_neutral = is_sole_neutral(rules, out.after)
    return out


def normalize_card(rules: MaterialRules, card: CardDraft, fix: bool | None = None):
    if not card.object_description:
        return card, None
    outcome = normalize_text(
        rules,
        card.object_description,
        card.materials,
        card.background.color if card.background else None,
        fix,
    )
    if not outcome.changed:
        return card, outcome
    return card.model_copy(update={"object_description": outcome.after}), outcome


def normalize_plans(rules: MaterialRules, plans: list[PlanDraft]) -> dict:
    """Rewrite every draft's descriptions. Returns plans, the change log and findings."""
    if not rules.enabled:
        return {"plans": plans, "changes": [], "errors": [], "warnings": []}
    new_plans, changes, errors, warnings = [], [], [], []
    for plan in plans:
        cards = []
        for no, card in enumerate(plan.cards, 1):
            cid = f"{plan.variant_id}_c{no:02d}"
            card, outcome = normalize_card(rules, card)
            cards.append(card)
            if outcome is None:
                continue
            if outcome.changed:
                changes.append(
                    {
                        "card": cid,
                        "before": outcome.before,
                        "after": outcome.after,
                        "rules": outcome.rules,
                    }
                )
            if outcome.sole_neutral:
                msg = (
                    f"{cid}: grey/black is the only colour of {outcome.after!r}; give the "
                    "object a saturated main colour (grey, black and chrome only for small parts)"
                )
                (warnings if rules.sole_neutral_action == "warn" else errors).append(msg)
        new_plans.append(plan.model_copy(update={"cards": cards}))
    return {"plans": new_plans, "changes": changes, "errors": errors, "warnings": warnings}
