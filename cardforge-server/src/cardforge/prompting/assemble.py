"""CardSpec → PromptBundle from versioned blocks and backend dialects (TA §10.9).

No render prompt is written by hand: the text comes only from `prompts/render/<v>.toml`,
the spec and PromptGuard constraint lines. The template version (name + sha256 of the
file) is part of every bundle and therefore of its idempotency hash.
"""

from __future__ import annotations

import hashlib
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from cardforge.contracts.generation import PromptBundle, Reference, RenderParams
from cardforge.contracts.policy import PolicyPack, PolicyVerdict
from cardforge.contracts.request import CardSpec
from cardforge.policy.prompt_guard import applicable_rules, constraint_lines, guard

TEMPLATE = "prompts/render/v1.toml"
FUNCTIONAL_TYPES = {"tool", "device", "vehicle"}


@dataclass(frozen=True)
class RenderProfile:
    id: str
    backend: str
    model_id: str
    dialect: str
    width: int
    height: int
    cost_estimate_usd: float = 0.0
    steps: int | None = None
    guidance: float | None = None
    quantize: int | None = None
    available: bool = True
    enabled: bool = True
    test_only: bool = False
    template: str = TEMPLATE
    # OpenRouter image models
    aspect_ratio: str = "4:5"
    send_references: bool = False
    zdr: bool = False
    endpoint: str = "chat"
    send_seed: bool = True
    max_refs: int = 6
    extra: dict | None = None
    allow_openai: bool = False
    # Codex CLI image tool
    codex_model: str = ""
    codex_effort: str = "medium"
    # G1 (2026-11-09): reserve profile for cards this profile fails `fallback_after_failures`
    # times in a row ([defaults] of config/runtime.toml); None = no reserve.
    fallback: str | None = None


@dataclass(frozen=True)
class Template:
    data: dict
    version: str


def load_template(root: Path, rel: str = TEMPLATE) -> Template:
    raw = (root / rel).read_bytes()
    data = tomllib.loads(raw.decode("utf-8"))
    return Template(data, f"{data['version']}@{hashlib.sha256(raw).hexdigest()[:12]}")


def material_lines(data: dict, spec: CardSpec) -> list[str]:
    """ref-v6: one line per material the card has, from the template's `[materials]` table.

    `[material_variants.<material>.<variant>]` adds a variant line (gold, chrome, iron for
    metal) when the object description names one of its words. Templates without the table
    (ref-v5 and older) give no lines, so their prompts are unchanged.
    """
    table = data.get("materials", {})
    variants = data.get("material_variants", {})
    text = (spec.object_description or spec.object_name).lower()
    lines: list[str] = []
    for material in dict.fromkeys(m.strip().lower() for m in spec.materials):
        if material in table:
            lines.append(table[material])
        for v in variants.get(material, {}).values():
            if any(re.search(rf"\b{re.escape(w.lower())}\b", text) for w in v["words"]):
                lines.append(v["text"])
    return [x for x in dict.fromkeys(lines) if x]


def _setting(blocks: dict, spec: CardSpec, motif_texts: dict[str, str]) -> str:
    bg = spec.background
    if bg is None:
        return ""
    kind = bg.motif if bg.motif in ("gradient", "scene") else "background"
    text = motif_texts.get(bg.motif, f"a {bg.motif} pattern")
    return blocks["setting"][kind].format(color=bg.color, motif=bg.motif, motif_text=text)


def render_blocks(
    template: Template,
    spec: CardSpec,
    constraints: list[str],
    references: list[Reference],
    fix_instruction: str | None,
    candidate: int | None = None,
    motif_texts: dict[str, str] | None = None,
) -> dict[str, str]:
    b = template.data["blocks"]
    lines = material_lines(template.data, spec)
    prefixes = template.data.get("candidates", {})
    must_have = f"Must show: {', '.join(spec.must_have)}. " if spec.must_have else ""
    roles = [
        r
        for r in ("style", "category", "material", "swatch", "background")
        if any(x.role == r for x in references)
    ]
    # Image numbers per role, in the order the images are sent.
    numbers = {
        r: ", ".join(str(i) for i, x in enumerate(references, 1) if x.role == r) for r in roles
    }
    return {
        "candidate": prefixes[str(candidate or 1)].strip() if prefixes else "",
        "tail": b.get("tail", ""),
        "detail": b.get("detail", ""),
        "clean": b.get("clean", ""),
        "logic": b.get("logic", ""),
        "dominant": b.get("dominant", ""),  # ref-v4
        "form": b.get("form", ""),  # ref-v4
        "light": b.get("light", ""),  # ref-v6
        "colour": b.get("colour", ""),  # ref-v6
        "materials": b["materials"].format(lines=" ".join(lines))
        if lines and "materials" in b
        else "",  # ref-v6
        "style": b["style"],
        "category": b["category"][str(spec.category)].format(
            surface=spec.surface or "", environment=spec.environment or ""
        ),
        "object": b["object"].format(
            object_name=spec.object_name,
            object_description=spec.object_description or spec.object_name,
            materials=", ".join(spec.materials) or "as appropriate",
            must_have=must_have,
            theme_rationale=spec.theme_rationale,
        ),
        "functional": b["functional"]
        if spec.category in (3, 4) or spec.object_type in FUNCTIONAL_TYPES
        else "",
        "setting": _setting(b, spec, motif_texts or {}),
        "composition": b["composition"].format(
            top_pct=round(spec.safe_zone.top_pct * 100),
            bottom_pct=round(spec.safe_zone.bottom_pct * 100),
            side_pct=round(spec.safe_zone.side_pct * 100),
        ),
        "constraints": b["constraints"].format(rules="; ".join(constraints)) if constraints else "",
        "references": " ".join(
            b["references"][r].format(n=numbers[r]) for r in roles if b["references"].get(r)
        ),
        "fix": b["fix"].format(fix_instruction=fix_instruction) if fix_instruction else "",
    }


def build_bundle(
    template: Template,
    profile: RenderProfile,
    spec: CardSpec,
    packs: list[PolicyPack],
    plan_verdicts: list[PolicyVerdict],
    *,
    seed: int,
    references: list[Reference],
    max_refs: int,
    fix_instruction: str | None = None,
    waived: frozenset[str] = frozenset(),
    candidate: int | None = None,
    motif_texts: dict[str, str] | None = None,
) -> PromptBundle:
    """Assemble, guard and seal. Raises PolicyPromptError on a forbidden term."""
    dialect = template.data["dialects"][profile.dialect]
    refs = references[:max_refs] if "references" in dialect["order"] else []
    rules = applicable_rules(packs, plan_verdicts, {spec.card_id, "request"})
    blocks = render_blocks(
        template, spec, constraint_lines(rules), refs, fix_instruction, candidate, motif_texts
    )
    text = dialect["joiner"].join(blocks[k] for k in dialect["order"] if blocks[k])
    guard(text, packs, waived)
    return PromptBundle(
        backend=profile.backend,
        model_id=profile.model_id,
        prompt_text=text,
        references=refs,
        params=RenderParams(
            width=profile.width,
            height=profile.height,
            steps=profile.steps,
            guidance=profile.guidance,
            seed=seed,
            quantize=profile.quantize,
        ),
        template_version=template.version,
    ).sealed()
