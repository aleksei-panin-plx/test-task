"""Two checklist items about how the object is *painted* (stage 4, work 6 of
`docs/research/generation-defects.md`, 2026-11-06): «материал нарисован как в референсах?» and
«блики и свет как в референсах?». The user rejects cards for materials, highlights and dullness,
while the v1/v2 checklists ask only whether a material is *recognisable* (`material`) and almost
never flag it (0-1 of 268 rejects in blind-labels-02).

Same contract as the checklist they extend (`veto_judge` v1 for the runtime Kimi veto, phase-3
`defect-checklist-v2` for Kimi v2 and DeepSeek): the base prompt is kept byte for byte and the
two questions are appended at the end of its question list; each answer is the same
`{defect: bool, note: str}` object; image order, references and pinned routes are unchanged.
Behind a flag: the extended checklist has its own profile id `<base>+look-v1` (a new judge for
calibration); without it nothing changes.

Until calibrated on human reasons (relabel-03), the two items are **signal only**: they are
recorded and mapped to fix codes but never veto (`LOOK_VETO` is empty).
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import create_model

SUFFIX = "+look-v1"

LOOK_ITEMS = {
    "material_style": "Нарисованы ли материалы предмета иначе, чем в референсах игры? В "
    "референсах металл чаще окрашен насыщенной эмалью, как гладкий пластик; золото и латунь — "
    "тёплые жёлто-оранжевые с одним мягким бликом, не зеркало; хром и сталь — мягкий "
    "светло-серый градиент с одним мягким бликом, обычно на мелких частях; дерево — тёплое "
    "медовое, гладкое, без лака; ткань — матовая, крупные складки. Дефект — фотореалистичный или "
    "зеркальный металл, голая серая сталь как основной цвет предмета, лакированное или "
    "фотографическое дерево, микротекстура, материал, который в референсах так не рисуют.",
    "highlights_light": "Отличаются ли блики и свет от референсов? В референсах один мягкий "
    "ключевой свет сверху-спереди, на крупной форме один-два широких блика с мягким краем, острая "
    "искра — только на стекле или линзе. Дефект — много мелких или резких бликов, искры, "
    "резкие светлые полосы, контровой свет, зеркальные отражения окружения.",
}
# Fix codes of prompts/render/fixes.toml (fixes-v1 has no separate «light» code).
LOOK_CODES = {"material_style": "material", "highlights_light": "style"}
LOOK_VETO: list[str] = []  # signal only until calibrated (relabel-03 reasons)


@dataclass(frozen=True)
class Checklist:
    profile: str
    prompt: str
    schema: type
    defects: dict[str, str]
    codes: dict[str, str]
    look: tuple[str, ...] = ()


def look_lines() -> str:
    return "\n".join(f"- {k}: {v}" for k, v in LOOK_ITEMS.items())


def extend(base: Checklist) -> Checklist:
    """The base checklist plus the two look items at the end (prompt prefix unchanged)."""
    if base.look:
        raise ValueError(f"{base.profile} already has the look items")
    clash = set(LOOK_ITEMS) & set(base.defects)
    if clash:
        raise ValueError(f"look items clash with {sorted(clash)}")
    defect = base.schema.model_fields[next(iter(base.defects))].annotation
    schema = create_model(
        base.schema.__name__ + "Look",
        __base__=base.schema,
        **{k: (defect, ...) for k in LOOK_ITEMS},
    )
    return Checklist(
        profile=base.profile + SUFFIX,
        prompt=base.prompt + "\n" + look_lines(),
        schema=schema,
        defects={**base.defects, **LOOK_ITEMS},
        codes={**base.codes, **LOOK_CODES},
        look=tuple(LOOK_ITEMS),
    )


def is_look(profile: str | None) -> bool:
    return bool(profile) and profile.endswith(SUFFIX)
