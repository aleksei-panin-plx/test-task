# ruff: noqa: E501
"""Deterministic offline planner for tests and `cardforge plan --fake` (no network).

It answers the same tasks as the host agent with the same envelope, so the core runs its normal
validation and repair. Its plans come from a fixed pool of neutral film-set props. It is not
evidence of quality: it only shows that the plan flow and the content tables work.
"""

from __future__ import annotations

import threading
from typing import Any

from cardforge.llm.provider import LLMReply, Pending
from cardforge.orchestrator.store import RunStore

PROVIDER = "fake"
MODEL = "fake-llm-v1"

# (name, scale_class, object_type, materials); categories follow the catalog rules.
POOL: dict[int, list[tuple[str, str, str, list[str]]]] = {
    1: [
        ("Хлопушка", "handheld", "tool", ["wood", "plastic"]),
        ("Кинокамера", "tabletop", "device", ["metal", "plastic"]),
        ("Мегафон режиссёра", "handheld", "device", ["plastic", "metal"]),
        ("Ведро попкорна", "handheld", "food", ["paper", "food"]),
        ("Кинобилет", "handheld", "other", ["paper"]),
        ("Катушка плёнки", "handheld", "other", ["metal", "plastic"]),
        ("Стерео-очки", "handheld", "clothing", ["plastic", "paper"]),
        ("Статуэтка премии", "tabletop", "decor", ["metal"]),
        ("Сценарий в папке", "handheld", "other", ["paper"]),
        ("Берет режиссёра", "handheld", "clothing", ["fabric"]),
        ("Объектив", "handheld", "device", ["glass", "metal"]),
        ("Стакан газировки", "handheld", "food", ["paper", "plastic"]),
        ("Микрофон-пушка", "handheld", "device", ["metal", "fabric"]),
        ("Пульт монтажёра", "tabletop", "device", ["plastic"]),
        ("Коробка конфет кинозала", "handheld", "food", ["paper", "food"]),
        ("Видоискатель", "handheld", "device", ["metal", "glass"]),
    ],
    2: [
        ("Кресло режиссёра", "furniture", "decor", ["wood", "fabric"]),
        ("Прожектор на штативе", "furniture", "device", ["metal", "glass"]),
        ("Кинопроектор", "tabletop", "device", ["metal"]),
        ("Стойка с костюмами", "furniture", "clothing", ["fabric", "metal"]),
        ("Рулон ковровой дорожки", "furniture", "decor", ["fabric"]),
        ("Кофр с плёнкой", "tabletop", "tool", ["metal"]),
        ("Софтбокс", "furniture", "device", ["fabric", "metal"]),
        ("Операторская тележка", "vehicle", "vehicle", ["metal", "rubber"]),
        ("Гримёрный кейс", "tabletop", "tool", ["plastic", "metal"]),
        ("Табурет осветителя", "furniture", "decor", ["wood"]),
        ("Монитор режиссёра", "tabletop", "device", ["plastic", "glass"]),
    ],
    3: [
        ("Попкорн-машина", "furniture", "device", ["metal", "glass"]),
        ("Монтажный стол", "furniture", "device", ["wood", "metal"]),
        ("Камера на рельсах", "furniture", "device", ["metal"]),
        ("Афишная тумба", "structure", "structure", ["stone", "paper"]),
        ("Автомат с газировкой", "furniture", "device", ["metal", "glass"]),
        ("Гримёрное зеркало с лампами", "furniture", "decor", ["wood", "glass"]),
        ("Звукорежиссёрский пульт", "furniture", "device", ["plastic", "metal"]),
        ("Стол с наградами", "furniture", "decor", ["wood", "metal"]),
        ("Экран для проекций", "structure", "structure", ["fabric", "metal"]),
        ("Билетный автомат", "furniture", "device", ["metal", "plastic"]),
        ("Вешалка с плащами", "furniture", "clothing", ["wood", "fabric"]),
    ],
    4: [
        ("Кинотеатр под открытым небом", "structure", "structure", ["fabric", "metal"]),
        ("Трейлер артиста", "vehicle", "vehicle", ["metal", "glass"]),
        ("Кинотеатр с афишей", "structure", "structure", ["stone", "neon"]),
        ("Съёмочный кран", "structure", "tool", ["metal"]),
        ("Ретро-автомобиль на премьере", "vehicle", "vehicle", ["metal", "glass"]),
        ("Павильон киностудии", "structure", "structure", ["metal", "stone"]),
        ("Фургон с аппаратурой", "vehicle", "vehicle", ["metal"]),
        ("Билетная касса", "structure", "structure", ["wood", "glass"]),
        ("Зал кинотеатра", "structure", "structure", ["fabric", "wood"]),
        ("Электрокар студии", "vehicle", "vehicle", ["plastic", "metal"]),
        ("Прожекторы премьеры", "structure", "device", ["metal", "glass"]),
        ("Бассейн для съёмок", "structure", "structure", ["stone", "water"]),
        ("Декорация городской улицы", "structure", "structure", ["wood", "paper"]),
        ("Будка киномеханика", "structure", "structure", ["wood", "metal"]),
        ("Велосипед курьера плёнок", "vehicle", "vehicle", ["metal", "rubber"]),
        ("Шатёр кейтеринга", "structure", "structure", ["fabric", "metal"]),
    ],
}
LAYOUT = [1, 2, 3, 4, 1, 3, 4, 2, 4, 1]
COLORS = ["red", "blue", "yellow", "purple", "green", "orange", "cyan", "pink", "brown", "neutral"]
MOTIFS = ["rays", "stripes", "circles", "gradient", "icons", "diamonds", "waves"]
SURFACES = {2: ["plain"], 3: ["wood", "stone", "fabric"]}
SHAPES = ["компактный, округлый", "вытянутый вверх, узкий", "низкий и широкий", "прямоугольный, плотный"]
# The object's main colour, opposite to its background colour (prompts/planner/card_rules.md).
OBJECT_COLORS = {
    "red": "основной цвет зелёный", "blue": "основной цвет оранжевый",
    "yellow": "основной цвет фиолетовый", "purple": "основной цвет жёлтый",
    "green": "основной цвет красный", "orange": "основной цвет синий",
    "cyan": "основной цвет красный", "pink": "основной цвет зелёный",
    "brown": "основной цвет голубой", "neutral": "основной цвет красный",
}  # fmt: skip
ENVIRONMENTS = ["площадка под открытым небом, вечер", "павильон студии с софитами", "улица у кинотеатра"]


def fake_plan(variant_no: int, set_theme: str) -> dict:
    """One valid PlanDraft; variants draw disjoint objects so Jaccard stays at 0."""
    taken = {c: 0 for c in POOL}
    cards = []
    for slot, category in enumerate(LAYOUT):
        per_variant = LAYOUT.count(category)
        pool = POOL[category]
        name, scale, otype, materials = pool[
            (variant_no * per_variant + taken[category]) % len(pool)
        ]
        taken[category] += 1
        color = COLORS[(slot * 3 + variant_no) % len(COLORS)]
        card: dict[str, Any] = {
            "object_name": name,
            "main_object": f"{name}: {SHAPES[slot % len(SHAPES)]}, {OBJECT_COLORS[color]}",
            "theme_rationale": f"Узнаваемый предмет подтемы «{set_theme}» на съёмочной площадке.",
            "category": category,
            "scale_class": scale,
            "object_type": otype,
            "composition": "ракурс 3/4, объект в верхне-центральной зоне",
            "background": {
                "motif": "scene" if category == 4 else MOTIFS[(slot + variant_no) % len(MOTIFS)],
                "color": color,
            },
            "materials": materials,
            "must_have": [],
            "must_not_have": ["надписи"],
        }
        if category in SURFACES:
            options = SURFACES[category]
            card["surface"] = options[(slot + variant_no) % len(options)]
        if category == 4:
            card["environment"] = ENVIRONMENTS[(slot + variant_no) % len(ENVIRONMENTS)]
        cards.append(card)
    return {
        "variant_id": f"v{variant_no + 1:02d}",
        "title": f"Съёмочный день {variant_no + 1}",
        "concept": "Один день на съёмочной площадке: от подготовки до премьеры.",
        "cards": cards,
        "color_plan": [c["background"]["color"] for c in cards],
        "diversity_notes": "Масштабы от реквизита в руке до павильона.",
        "policy_notes": [],
    }


def _varied(cards: list[dict], n: int) -> list[dict]:
    """`n` of the 10 fake cards keeping every category and as many object types as possible."""
    if n >= len(cards):
        return cards
    chosen: list[int] = []
    for cat in (1, 2, 3, 4):
        if len(chosen) < n:
            types = {cards[j]["object_type"] for j in chosen}
            same = [i for i, c in enumerate(cards) if c["category"] == cat and i not in chosen]
            fresh = [i for i in same if cards[i]["object_type"] not in types]
            chosen.append((fresh or same)[0])
    for i, c in enumerate(cards):
        if len(chosen) >= n:
            break
        if i not in chosen and c["object_type"] not in {cards[j]["object_type"] for j in chosen}:
            chosen.append(i)
    chosen += [i for i in range(len(cards)) if i not in chosen][: n - len(chosen)]
    return [cards[i] for i in sorted(chosen)]


class FakeLLM:
    """`bad_plans` makes the first N plan answers invalid to exercise repair; `pending_steps`
    makes the fake wait like a host agent that has not answered yet."""

    name = PROVIDER

    def __init__(
        self,
        *,
        bad_plans: int = 0,
        cost_per_call_usd: float = 0.0,
        pending_steps: set[str] | None = None,
        model: str = MODEL,
        inject: dict[str, str] | None = None,
    ):
        # card_id → object name planted in every plan answer (a dry run of the policy replacement:
        # «Глобус» meets a culture rule, the repair swaps it for the rule's first alternative).
        self.inject = inject or {}
        self.bad_plans = bad_plans
        self.cost_per_call_usd = cost_per_call_usd
        self.pending_steps = pending_steps or set()
        self.model = model
        self.calls: list[str] = []
        self._lock = threading.Lock()

    def estimate_cost(self, task: dict) -> float:
        return self.cost_per_call_usd

    def ask(self, store: RunStore, key: str, task: dict) -> LLMReply | Pending:
        step = task["step"]
        if step in self.pending_steps:
            return Pending(key, f"tasks/{key}.task.json", "fake provider configured to wait")
        with self._lock:
            self.calls.append(key)
        data = getattr(self, "_" + step.replace("-", "_"))(task)
        return LLMReply(
            data=data, provider=PROVIDER, model=self.model, cost_usd=self.cost_per_call_usd
        )

    def _normalize(self, task: dict) -> dict:
        parts = [p.strip() for p in task["brief"]["producer_wishes_raw"].split(",") if p.strip()]
        negative = ("без ", "no ", "не ")
        return {
            "constraints": [p for p in parts if p.lower().startswith(negative)],
            "preferences": [p for p in parts if not p.lower().startswith(negative)],
            "assumptions": ["Фейковый нормализатор: пожелания разделены по запятым."],
        }

    def _plan(self, task: dict) -> dict:
        """N numbered set themes, one card list per drawn set (object cards only), object names
        tagged with the set so nothing repeats across the collection."""
        request = task["request"]
        layout = task["collection_layout"]
        theme = request["collection_theme"]
        themes = [f"{theme}: подтема {i}" for i in range(1, request["sets_total"] + 1)]
        if layout["grand_set"]:
            themes[-1] = f"{theme}: кульминация"
        edits = " ".join(task.get("human_edits") or [])
        variants = []
        for slot in layout["drawn"]:
            i = slot["set_index"]
            v = fake_plan(i - 1, themes[i - 1])
            v["cards"] = _varied(v["cards"], slot["object_cards"])
            for c in v["cards"]:
                c["object_name"] = f"{c['object_name']} (сет {i})"
            v.update(variant_id=slot["variant_id"], set_index=i, gold_slots=slot["gold_slots"],
                     title=themes[i - 1], color_plan=[c["background"]["color"] for c in v["cards"]],
                     concept=f"Фейковый план сета {i} коллекции «{theme}».",
                     policy_notes=[f"{c['rule_id']}: пожелание заменено жанровой альтернативой — "
                                   f"{c['resolution']}" for c in request["conflicts"] if c.get("rule_id")])  # fmt: skip
            if edits:
                v["concept"] += f" Учтены правки: {edits}"
            variants.append(v)
        for v in variants:
            for no, c in enumerate(v["cards"], 1):
                if name := self.inject.get(f"{v['variant_id']}_c{no:02d}"):
                    c.update(object_name=name, main_object=f"{name}, компактный, основной цвет красный")
        self._replace(task, variants)
        earlier = sum(k.startswith("plan") for k in self.calls[:-1])
        if earlier < self.bad_plans:  # a repeated object: the validator must catch it
            cards = variants[0]["cards"]
            cards[1]["object_name"] = cards[0]["object_name"]
        collection = {"set_themes": themes,
                      "grand_set_theme": themes[-1] if layout["grand_set"] else None}  # fmt: skip
        return {"collection": collection, "variants": variants}

    @staticmethod
    def _replace(task: dict, variants: list[dict]) -> None:
        """Repair of `replaced_objects`: the rule's first alternative, one policy_notes line."""
        rules = {r["rule_id"]: r for r in task.get("plan_rules", [])}
        by_id = {v["variant_id"]: v for v in variants}
        for r in task.get("replaced_objects", []):
            vid, no = r["card_id"].split("_c")
            v, rule = by_id.get(vid), rules.get(r["rule_id"])
            if v is None or rule is None:
                continue
            card = v["cards"][int(no) - 1]
            alt = (rule["alternatives"] or ["нейтральный предмет"])[0]
            name = f"{alt[:1].upper()}{alt[1:]} ({v['title']})"
            card.update(object_name=name, main_object=f"{alt}, компактный, основной цвет красный")
            v["policy_notes"].append(f"{rule['rule_id']}: «{r['object_before']}» → «{name}»: {rule['title']}")

    def _repair(self, task: dict) -> dict:
        return self._plan(task)

    def _policy_screen(self, task: dict) -> dict:
        return {"findings": []}
