"""Stage 4: blind review package of the current generators' cards for several human reviewers.

User decision 2026-11-04 («Да, собери»): the old held-out package 48 holds only local FLUX
renders, which are switched off; judge calibration needs blind labels on the current
generators. No new images: every card a human already decided at G2 in kino-western-02/03
(GPT Image, Nano Banana, FLUX 3, Seedream, all attempts incl. fixes) plus the 6 GPT Image 2.5
examples. The page hides the model, shuffles the order (no two neighbours of the same object)
and asks for a decision plus reason codes (taxonomy: docs/research/judges-quality.md §1.1).

    .venv/bin/python scripts/stage04_blind_review.py items     # list, no files written
    .venv/bin/python scripts/stage04_blind_review.py package   # once; never reshuffled
    .venv/bin/python scripts/stage04_blind_review.py import path/to/export.json
    .venv/bin/python scripts/stage04_blind_review.py agreement
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import shutil
from datetime import UTC, datetime
from pathlib import Path

from PIL import Image

from cardforge.evaluate.calibration import cohen_kappa
from cardforge.evaluate.common import dump, sha256

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "runs/stage04/blind-review-01"
NAME = "CardForge-review-kino-01"
FORMAT = "cardforge-review-kino-01"
SEED = 20261104
RUNS = ("kino-western-02", "kino-western-03")
SMOKE = ROOT / "runs/stage04/smoke-gpt25"
SMOKE_PROFILE = "or-gpt-image-2-5"
LABEL_REFS = ("17", "45", "58", "91")  # the same four style references as packages 46 and 48
DECISIONS = ("accept", "fix", "reject", "uncertain")
REASONS = {
    "style": "Стиль или «выделка» не как в игре",
    "props": "Лишний реквизит, перегруз деталями",
    "support": "Нелогичная опора: вторая линия, обрыв рельсов, странная платформа",
    "crop": "Главный объект обрезан или выходит из безопасной зоны",
    "material": "Материал не узнаётся (ткань, плёнка, металл…)",
    "background": "Фон: не контрастирует, слишком яркий или паттерн не тот",
    "category": "Категория карты не читается",
    "direction": "Объект направлен вниз",
    "artifact": "Артефакты, искажённые или лишние детали",
    "text": "Надписи, буквы, логотипы",
    "theme": "Не читается связь с темой «Кино · Вестерн»",
}
PAGE = Path(__file__).with_name("stage04_blind_review.html")
README = """CardForge — разметка карт коллекции «Кино», сет «Вестерн»

1. Распакуйте архив и откройте index.html в браузере (Chrome, Safari, Firefox).
   Интернет и установка не нужны.
2. Введите имя. Для каждой карты решите, можно ли взять её в игровой набор:
   1 — принимаю как есть, 2 — нужна небольшая правка, 3 — отклоняю, 4 — не уверен(а).
   Если не «принимаю» — отметьте причины галочками; комментарий необязателен, но полезен.
3. Решения сохраняются в браузере автоматически; можно закрыть и продолжить позже
   в том же браузере на этом же компьютере.
4. Когда все карты оценены, нажмите «Скачать разметку» и отправьте файл .json обратно.

Оценивайте самостоятельно, не обсуждая карты с другими проверяющими. Правильных ответов
заранее нет: одни и те же объекты нарисованы по-разному, порядок перемешан.
Внутренние материалы: изображения не публикуйте и не пересылайте дальше.
"""


def _plans(run: Path) -> dict[str, dict]:
    return {
        c["card_id"]: c
        for p in json.loads((run / "plan/variants.json").read_text())
        for c in p["cards"]
    }


def items(root: Path = ROOT) -> list[dict]:
    """Every attempt a human decided at G2 (the decision that applied to it), plus GPT 2.5."""
    rows: list[dict] = []
    for name in RUNS:
        run = root / "runs/stage04" / name
        manifest = json.loads((run / "manifest.json").read_text())
        theme = json.loads((run / "request.json").read_text())
        plans = _plans(run)
        current: dict[str, int] = {}
        labels: dict[tuple[str, int], dict] = {}
        for line in (run / "events.jsonl").read_text().splitlines():
            e = json.loads(line)
            if e["type"] == "render_started":
                current[e["card_id"]] = e["payload"]["attempt_no"]
            elif e["type"] == "human_decision" and e["payload"].get("gate") == "G2":
                p = e["payload"]
                if p.get("subject") == "card" and e["card_id"] in current:
                    labels[(e["card_id"], current[e["card_id"]])] = {
                        "decision": p["decision"],
                        "answer": p.get("human_answer"),
                        "note": p.get("note"),
                        "reviewer": p.get("reviewer"),
                    }
        for (cid, attempt), label in sorted(labels.items()):
            variant = manifest["cards"][cid]["variant_id"]
            image = run / f"cards/{variant}/{cid}/attempt_{attempt:02d}/final.png"
            if not image.exists():
                raise FileNotFoundError(image)
            rows.append(
                {
                    "item_id": f"{name}/{cid}.a{attempt:02d}",
                    "run": name,
                    "card_id": cid,
                    "attempt": attempt,
                    "position": cid.split("_")[1],
                    "model": manifest["variant_profiles"][variant],
                    "image": str(image.relative_to(root)),
                    "image_sha256": sha256(image),
                    "spec": _spec(plans[cid], theme),
                    "open_g2": label,
                }
            )
    run = root / "runs/stage04/kino-western-03"
    plans, theme = _plans(run), json.loads((run / "request.json").read_text())
    for result in sorted(SMOKE.glob("*.result.json")) if root == ROOT else []:
        cid = result.name.split(".")[0]
        image = SMOKE / f"{cid}.final.png"
        if json.loads(result.read_text())["status"] != "ok" or not image.exists():
            continue
        rows.append(
            {
                "item_id": f"smoke-gpt25/{cid}.a01",
                "run": "smoke-gpt25",
                "card_id": cid,
                "attempt": 1,
                "position": cid.split("_")[1],
                "model": SMOKE_PROFILE,
                "image": str(image.relative_to(root)),
                "image_sha256": sha256(image),
                "spec": _spec(plans[cid], theme),
                "open_g2": None,
            }
        )
    return rows


def _spec(card: dict, request: dict) -> dict:
    return {
        "object_name": card["object_name"],
        "category": card["category"],
        "set_theme": f"{request['collection_theme']} · {request['set_theme']}",
        "must_not_have": card.get("must_not_have", []),
    }


def shuffled(rows: list[dict], seed: int = SEED) -> list[dict]:
    """Seeded shuffle with no two neighbours showing the same object (same spec position)."""
    rng = random.Random(seed)
    for _ in range(10_000):
        order = rows[:]
        rng.shuffle(order)
        keys = [r["spec"]["object_name"] for r in order]
        if all(a != b for a, b in zip(keys, keys[1:], strict=False)):
            return order
    raise RuntimeError("could not separate neighbours of the same object")


def package(out: Path = OUT, rows: list[dict] | None = None, root: Path = ROOT) -> dict:
    mapping_path = out / "mapping.private.json"
    if mapping_path.exists():
        raise SystemExit("package already built; do not reshuffle a handed-out package")
    rows = shuffled(rows if rows is not None else items(root))
    pkg = out / NAME
    (pkg / "cards").mkdir(parents=True)
    (pkg / "refs").mkdir()
    cards, mapping = [], []
    for n, item in enumerate(rows, 1):
        card = f"card-{n:03d}"
        # Re-encode pixels only: no generation metadata (model, prompt) travels with the image.
        with Image.open(root / item["image"]) as image:
            image.convert("RGB").save(pkg / "cards" / f"{card}.png")
        digest = sha256(pkg / "cards" / f"{card}.png")
        cards.append({"card": card, "image_sha256": digest, "spec": item["spec"]})
        mapping.append({"card": card, "package_sha256": digest, **item})
    for name in LABEL_REFS:
        shutil.copyfile(root / f"dataset/CardC6_card_{name}.png", pkg / f"refs/ref-{name}.png")
    page = PAGE.read_text()
    (pkg / "index.html").write_text(
        page.replace("cardforge-review-kino-v0", FORMAT)
        .replace("/*CARDS*/[]", json.dumps(cards, ensure_ascii=False))
        .replace("/*REFS*/[]", json.dumps([f"refs/ref-{n}.png" for n in LABEL_REFS]))
        .replace("/*REASONS*/{}", json.dumps(REASONS, ensure_ascii=False))
    )
    (pkg / "README.txt").write_text(README)
    archive = Path(shutil.make_archive(str(pkg), "zip", out, pkg.name))
    dump(
        mapping_path,
        {
            "seed": SEED,
            "format": FORMAT,
            "built_at": datetime.now(UTC).isoformat(),
            "reasons": REASONS,
            "cards": mapping,
            "zip": archive.name,
            "zip_sha256": sha256(archive),
        },
    )
    by_model: dict[str, int] = {}
    for item in rows:
        by_model[item["model"]] = by_model.get(item["model"], 0) + 1
    return {"cards": len(cards), "by_model": by_model, "zip": str(archive),
            "zip_bytes": archive.stat().st_size}  # fmt: skip


def import_export(path: Path, out: Path = OUT) -> dict:
    """Store one reviewer's export as its own file (never overwritten); recompute agreement."""
    data = json.loads(path.read_text())
    mapping = {
        m["card"]: m for m in json.loads((out / "mapping.private.json").read_text())["cards"]
    }
    if data.get("format") != FORMAT:
        raise ValueError("unexpected export format (is this the Кино blind package?)")
    reviewer = (data.get("reviewer") or "").strip()
    if not reviewer:
        raise ValueError("reviewer name required")
    rows = {}
    for card, rating in data["ratings"].items():
        source = mapping.get(card)
        if not source or rating["image_sha256"] != source["package_sha256"]:
            raise ValueError(f"{card}: export does not match the handed-out image")
        if rating["decision"] not in DECISIONS:
            raise ValueError(f"{card}: explicit decision required")
        unknown = set(rating.get("reasons", [])) - set(REASONS) - {"other"}
        if unknown:
            raise ValueError(f"{card}: unknown reason codes {sorted(unknown)}")
        rows[source["item_id"]] = {"card": card, **rating}
    reviewers = out / "reviewers"
    (reviewers / "sources").mkdir(parents=True, exist_ok=True)
    digest = sha256(path)
    target = reviewers / f"{hashlib.sha256(reviewer.encode()).hexdigest()[:12]}-{digest[:8]}.json"
    previous = [json.loads(p.read_text()) for p in reviewers.glob("*.json")]
    if any(p["source_sha256"] == digest for p in previous):
        return {"skipped": "identical export already imported", "agreement": agreement(out)}
    if any(p["reviewer"] == reviewer for p in previous):
        raise ValueError(
            f"{reviewer}: a different export is already imported; keep both "
            "source files and resolve explicitly (e.g. rename the reviewer)"
        )
    # Exclusive create: a second file never replaces an earlier reviewer's labels.
    with target.open("x", encoding="utf-8") as stream:
        json.dump({"reviewer": reviewer, "source_sha256": digest,
                   "imported_at": datetime.now(UTC).isoformat(), "rated": len(rows),
                   "ratings": rows}, stream, ensure_ascii=False, indent=2)  # fmt: skip
    shutil.copyfile(path, reviewers / "sources" / target.name)
    return {"imported": len(rows), "file": f"reviewers/{target.name}", "agreement": agreement(out)}


def _pair(a: dict[str, bool], b: dict[str, bool]) -> dict:
    shared = sorted(set(a) & set(b))
    x, y = [a[k] for k in shared], [b[k] for k in shared]
    return {
        "n": len(shared),
        "kappa": cohen_kappa(x, y) if shared and (len(set(x)) > 1 or len(set(y)) > 1) else None,
        "agreement": sum(p == q for p, q in zip(x, y, strict=True)) / len(shared)
        if shared
        else None,
    }


def agreement(out: Path = OUT) -> dict:
    """Pairwise κ between blind reviewers (strict: accept; lenient: accept or fix), and each
    reviewer against the earlier open (model-labelled) G2 decisions of the same images."""
    mapping = json.loads((out / "mapping.private.json").read_text())["cards"]
    people: dict[str, dict[str, str]] = {}
    for path in sorted((out / "reviewers").glob("*.json")) if (out / "reviewers").exists() else []:
        record = json.loads(path.read_text())
        people[record["reviewer"]] = {
            i: r["decision"] for i, r in record["ratings"].items() if r["decision"] != "uncertain"
        }
    open_g2 = {m["item_id"]: m["open_g2"]["decision"] for m in mapping if m["open_g2"]}
    modes = {"strict": ("accept",), "lenient": ("accept", "fix")}
    report: dict = {
        "scope": "Blind review of the Кино·Вестерн cards of the current generators. Strict = "
        "accept; lenient = accept or fix. 'open G2' = the earlier decisions with models shown.",
        "reviewers": {
            n: {"rated": len(v), "accept": sum(d == "accept" for d in v.values())}
            for n, v in people.items()
        },  # fmt: skip
        "pairs": {},
        "vs_open_g2": {},
        "computed_at": datetime.now(UTC).isoformat(),
    }
    names = list(people)
    for mode, yes in modes.items():
        binary = {n: {i: d in yes for i, d in v.items()} for n, v in people.items()}
        opened = {i: d in yes for i, d in open_g2.items()}
        for i, a in enumerate(names):
            for b in names[i + 1 :]:
                report["pairs"].setdefault(f"{a} vs {b}", {})[mode] = _pair(binary[a], binary[b])
            report["vs_open_g2"].setdefault(a, {})[mode] = _pair(binary[a], opened)
    dump(out / "agreement.json", report)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("step", choices=("items", "package", "import", "agreement"))
    parser.add_argument("path", nargs="?", type=Path)
    args = parser.parse_args()
    if args.step == "items":
        rows = items()
        summary: dict = {"items": len(rows), "by_model": {}, "open_g2": {}}
        for r in rows:
            summary["by_model"][r["model"]] = summary["by_model"].get(r["model"], 0) + 1
            d = r["open_g2"]["decision"] if r["open_g2"] else "none"
            summary["open_g2"][d] = summary["open_g2"].get(d, 0) + 1
        result = summary
    elif args.step == "package":
        result = package()
    elif args.step == "import":
        if not args.path:
            raise SystemExit("import needs the path of a reviewer's export")
        result = import_export(args.path)
    else:
        result = agreement()
    print(json.dumps(result, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
