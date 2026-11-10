"""Deterministic offline LLM and judge for tests and `cardforge demo` (no network).

It answers the same tasks as the real providers with the same envelope, so the core runs
its normal validation and repair. Its plans come from a fixed pool of neutral film-set
props; its judge verdicts follow an explicit script. It is not evidence of quality.
"""

from __future__ import annotations

import hashlib
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
            card["environment"] = "outdoor" if scale == "vehicle" else "indoor"
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
    """`judge_script` maps card_id → decisions per attempt: accept|fix|flag|reject.
    `bad_plans` makes the first N plan answers invalid to exercise repair."""

    name = PROVIDER

    def __init__(
        self,
        *,
        judge_script: dict[str, list[str]] | None = None,
        bad_plans: int = 0,
        cost_per_call_usd: float = 0.0,
        pending_steps: set[str] | None = None,
        model: str = MODEL,
        fix_rate: int = 0,
        flag_rate: int = 0,
        salt: str = "",
        unresolved_fixes: dict[str, list[int]] | None = None,
        veto_rate: int = 0,
        sheet_outliers: list[int] | None = None,
    ):
        self.judge_script = judge_script or {}
        self.bad_plans = bad_plans
        self.cost_per_call_usd = cost_per_call_usd
        self.pending_steps = pending_steps or set()
        self.model = model
        # Unscripted cards: a seeded share of fix / flag verdicts, so fake judges disagree.
        self.fix_rate = fix_rate
        self.flag_rate = flag_rate
        self.salt = salt
        # card_id -> attempts on which the inspector still sees every earlier fix defect.
        self.unresolved_fixes = unresolved_fixes or {}
        # Veto checklist: a seeded share of attempts gets one defect (phase-2 shadow judge).
        self.veto_rate = veto_rate
        # Set sheet review: sheet numbers the fake reviewer names as outliers.
        self.sheet_outliers = sheet_outliers or []
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

    def _collection_plan(self, task: dict) -> dict:
        """Collection mode: N numbered set themes, one card list per drawn set (object cards only),
        object names tagged with the set so nothing repeats across the collection."""
        request = task["request"]
        layout = task["collection_layout"]
        theme = request["collection_theme"]
        themes = [f"{theme}: подтема {i}" for i in range(1, request["sets_total"] + 1)]
        if layout["grand_set"]:
            themes[-1] = f"{theme}: кульминация"
        variants = []
        for slot in layout["drawn"]:
            i = slot["set_index"]
            v = fake_plan(i - 1, themes[i - 1])
            v["cards"] = _varied(v["cards"], slot["object_cards"])
            for c in v["cards"]:
                c["object_name"] = f"{c['object_name']} (сет {i})"
            v.update(variant_id=slot["variant_id"], set_index=i, gold_slots=slot["gold_slots"],
                     title=themes[i - 1], color_plan=[c["background"]["color"] for c in v["cards"]],
                     concept=f"Фейковый план сета {i} коллекции «{theme}».")  # fmt: skip
            variants.append(v)
        collection = {"set_themes": themes,
                      "grand_set_theme": themes[-1] if layout["grand_set"] else None}  # fmt: skip
        return {"collection": collection, "variants": variants}

    def _plan(self, task: dict) -> dict:
        request = task["request"]
        if request.get("plan_mode") == "collection":
            return self._collection_plan(task)
        variants = [fake_plan(i, request["set_theme"]) for i in range(request["variants_count"])]
        earlier = sum(k.startswith("plan") for k in self.calls[:-1])
        if earlier < self.bad_plans:  # a repeated object: the validator must catch it
            variants[0]["cards"][1]["object_name"] = variants[0]["cards"][0]["object_name"]
        for v in variants:
            v["policy_notes"] = [
                f"{c['rule_id']}: пожелание заменено жанровой альтернативой — {c['resolution']}"
                for c in request["conflicts"]
                if c.get("rule_id")
            ]
        return {"variants": variants}

    def _repair(self, task: dict) -> dict:
        return self._plan(task)

    def _policy_screen(self, task: dict) -> dict:
        return {"findings": []}

    def _policy_inspect(self, task: dict) -> dict:
        still = task.get("attempt_no") in self.unresolved_fixes.get(task["card_id"], [])
        return {
            "answers": {
                q["rule_id"]: {"answer": "yes", "evidence": "fake inspector: still visible"}
                if still and q["rule_id"].startswith("fix:")
                else {"answer": "no", "evidence": "fake inspector: not observed"}
                for q in task["questions"]
            }
        }

    # Keyword stand-in for the host agent's rewrite of a human G2 comment (not a translator).
    FIX_KEYWORDS = {
        "crop": ("обрез", "край", "выше", "safe", "crop"),
        "props": ("лишн", "деталей", "детали", "прожектор", "убрать", "props"),
        "background": ("фон", "background"),
        "support": ("опор", "лини", "рельс", "support"),
    }

    def _fix_normalize(self, task: dict) -> dict:
        comment = task["comment"].lower()
        codes = [c for c, words in self.FIX_KEYWORDS.items() if any(w in comment for w in words)]
        digest = hashlib.sha256(task["comment"].encode()).hexdigest()[:6]
        what = ", ".join(codes) if codes else "general"
        return {
            "constraint": f"Apply the reviewer's {what} correction to the main object "
            f"(fake normalizer, comment {digest}).",
            "codes": codes,
        }

    def _policy_inspect_batch(self, task: dict) -> dict:
        return {
            "answers": {
                item["item_id"]: {
                    q["rule_id"]: {"answer": "no", "evidence": "fake inspector: not observed"}
                    for q in item["questions"]
                }
                for item in task["items"]
            }
        }

    def _judge(self, task: dict) -> dict:
        card_id, attempt = task["card_id"], task["attempt_no"]
        script = self.judge_script.get(card_id, [])
        if attempt - 1 < len(script):
            verdict = script[attempt - 1]
        else:
            roll = int(hashlib.sha256(f"{self.salt}:{card_id}:{attempt}".encode()).hexdigest(), 16)
            roll %= 100
            verdict = (
                "fix"
                if roll < self.fix_rate
                else "flag"
                if roll < self.fix_rate + self.flag_rate
                else "accept"
            )
        failed = {"fix": {"composition"}, "reject": {"object_match", "category", "theme"}}.get(
            verdict, set()
        )
        criteria = {
            name: {
                "passed": name not in failed,
                "note": "fake judge: scripted failure" if name in failed else "fake judge: ok",
            }
            for name in task["criteria"]
        }
        return {
            "item_id": card_id,
            "image_sha256": task["image_sha256"],
            "criteria": criteria,
            "style_uncertain": verdict == "flag",
            "issues": ["scripted issue"] if failed else [],
            "fix_instruction": "Поднять объект выше и освободить нижнюю зону кадра."
            if failed
            else None,
        }

    VETO_DEFECTS = ("extra_props", "cropped", "style_mismatch", "low_contrast")

    def _veto_checklist(self, task: dict) -> dict:
        card_id, attempt = task["card_id"], task["attempt_no"]
        roll = int(hashlib.sha256(f"{self.salt}:{card_id}:{attempt}".encode()).hexdigest(), 16)
        hit = (
            self.VETO_DEFECTS[roll % len(self.VETO_DEFECTS)]
            if roll % 100 < self.veto_rate
            else None
        )
        names = list(task["answer_schema"]["properties"])[1:]
        answer: dict = {"extra_items": ["fake prop"] if hit == "extra_props" else []}
        for name in names:
            answer[name] = {
                "defect": name == hit,
                "note": "fake veto: scripted defect" if name == hit else "fake veto: not seen",
            }
        return answer

    def _set_sheet_review(self, task: dict) -> dict:
        from cardforge.evaluate.sheet_review import fake_answer

        return fake_answer(task, self.sheet_outliers)

    def _set_review(self, task: dict) -> dict:
        return {
            "consistent_style": True,
            "outlier_cards": [],
            "reads_as_one_scene": True,
            "note": "fake set review",
        }
