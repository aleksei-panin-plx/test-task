"""Stage 4: preliminary target κ for AI judges from the blind human labels (package kino-01).

Offline, no API calls. Re-run after every new import: it reads all reviewers present.

Raters: every blind reviewer imported by `stage04_blind_review.py`, plus the earlier open G2
decisions of the same images (`panin (open G2)`: model and run were visible, not blind).
Binary labels: strict = accept; lenient = accept or fix. `uncertain` is left out of a pair.
The lenient scale ("reject or not") is the primary one: user decision 2026-11-04, "Да, мягкая
шкала" (docs/decisions.md). Strict is kept for reference.

Targets (requested 2026-11-04, preliminary while 2-3 raters exist):
- floor   = median pairwise κ between people;
- target  = a judge's κ against the majority of people >= the mean κ of a person against the
            majority of the other people (leave-one-rater-out);
- ceiling = the maximum pairwise κ observed between people.

Run labels (phase 2, 2026-11-04): blind G2 decisions saved by the core in
`runs/stage04/<run>/review/labels/*.json` (`cardforge-g2-labels-v1`) are read as their own
section `runs`: they label other images than package kino-01, so they do not enter the
targets above. Shadow judges logged on the same attempts (`<attempt>/judges/<id>.json` with a
`veto` block, e.g. `kimi-veto`) are scored against them (κ with bootstrap CI, agreement, n),
next to the targets.

Saved judge answers on the same images (no new calls) are scored against the same majority:
Kimi G1 and GPT-sol post hoc on kino-western-03, and the judge-research experiment 1
(Qwen, Kimi, GPT-6 Luna) on kino-western-02/03.

    .venv/bin/python -I scripts/stage04_blind_targets.py
"""

from __future__ import annotations

import json
import random
import statistics
from datetime import UTC, datetime
from pathlib import Path

from cardforge.evaluate.calibration import cohen_kappa
from cardforge.evaluate.common import dump

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "runs/stage04/blind-review-01"
OPEN = "panin (open G2)"
BOOT = 2000
SEED = 20261104
MODES = {"strict": ("accept",), "lenient": ("accept", "fix")}
PRIMARY_MODE = "lenient"
GPT_IMAGE = {"or-gpt-image-2", "or-gpt-image-2-5", "codex-gpt-image"}


def raters() -> dict[str, dict[str, str]]:
    people: dict[str, dict[str, str]] = {}
    for path in sorted((OUT / "reviewers").glob("*.json")):
        record = json.loads(path.read_text())
        people[record["reviewer"]] = {i: r["decision"] for i, r in record["ratings"].items()}
    mapping = json.loads((OUT / "mapping.private.json").read_text())["cards"]
    people[OPEN] = {m["item_id"]: m["open_g2"]["decision"] for m in mapping if m["open_g2"]}
    return people


def binary(labels: dict[str, str], mode: str) -> dict[str, bool]:
    return {i: d in MODES[mode] for i, d in labels.items() if d != "uncertain"}


def kappa(x: list[bool], y: list[bool]) -> float | None:
    if not x or (len(set(x)) == 1 and len(set(y)) == 1):
        return None
    return cohen_kappa(x, y)


def compare(a: dict[str, bool], b: dict[str, bool], rng: random.Random | None = None) -> dict:
    shared = sorted(set(a) & set(b))
    x, y = [a[k] for k in shared], [b[k] for k in shared]
    out = {
        "n": len(shared),
        "kappa": kappa(x, y),
        "agreement": round(sum(p == q for p, q in zip(x, y, strict=True)) / len(shared), 4)
        if shared
        else None,
    }
    if rng and shared:
        draws = []
        for _ in range(BOOT):
            idx = [rng.randrange(len(shared)) for _ in shared]
            k = kappa([x[i] for i in idx], [y[i] for i in idx])
            if k is not None:
                draws.append(k)
        draws.sort()
        out["kappa_ci95"] = [
            round(draws[int(0.025 * len(draws))], 3),
            round(draws[int(0.975 * len(draws)) - 1], 3),
        ]
    return out


def majority(labels: list[dict[str, bool]]) -> dict[str, bool]:
    """Strict majority of the given raters per item; ties (e.g. 1 vs 1) are left out."""
    votes: dict[str, list[bool]] = {}
    for one in labels:
        for item, v in one.items():
            votes.setdefault(item, []).append(v)
    return {i: sum(v) * 2 > len(v) for i, v in votes.items() if sum(v) * 2 != len(v)}


def judges() -> dict[str, dict[str, dict[str, bool]]]:
    """Saved judge answers mapped to package item ids, per mode."""
    out: dict[str, dict[str, dict[str, bool]]] = {}
    post = ROOT / "runs/stage04/kino-western-03/judging-posthoc"
    kimi = json.loads((post / "kimi-g1.result.json").read_text())["items"]
    gpt = json.loads((post / "gpt-sol-or.result.json").read_text())["items"]
    pre = "kino-western-03/"
    for mode in MODES:
        out.setdefault("kimi-g1 (post hoc, 03)", {})[mode] = {
            pre + i: bool(v["accept"]) for i, v in kimi.items() if v.get("accept") is not None
        }
        out.setdefault("gpt-6.1-sol OpenRouter (post hoc, 03)", {})[mode] = {
            pre + i: v["decision"] in MODES[mode]
            for i, v in gpt.items()
            if v["decision"] != "invalid"
        }
    exp = json.loads((ROOT / "runs/stage04/judge-research/exp1/score.json").read_text())
    # exp1 ids are "kw03:<card>.aNN" or "kw02:<card>"; the attempt comes from the image path.
    items = json.loads((ROOT / "runs/stage04/judge-research/exp1/items.json").read_text())
    items = items if isinstance(items, list) else items.get("items", list(items.values()))
    ids = {x["id"]: f"{x['run']}/{x['card']}.a{x['image'].split('attempt_')[1][:2]}" for x in items}
    for name, data in exp["judges"].items():
        for rule in ("primary", "veto_only", "floor_only"):
            rows = {
                ids[r["id"]]: bool(r["pred"][rule])
                for r in data["rows"]
                if r.get("pred") and r["pred"].get(rule) is not None
            }
            out[f"{name} exp1 {rule}"] = {m: rows for m in MODES}
        verdicts = {ids[r["id"]]: r["verdict"] for r in data["rows"] if r.get("verdict")}
        if verdicts:
            out[f"{name} exp1 verdict"] = {
                m: {i: v in MODES[m] for i, v in verdicts.items()} for m in MODES
            }
    return out


def run_labels(runs: Path = ROOT / "runs/stage04") -> dict[str, dict[str, dict]]:
    """{run: {reviewer: {item: rating}}}: blind G2 decisions written by the core."""
    out: dict[str, dict[str, dict]] = {}
    for path in sorted(runs.glob("*/review/labels/*.json")):
        record = json.loads(path.read_text())
        if record.get("format") != "cardforge-g2-labels-v1":
            continue
        ratings = {i: r for i, r in record["ratings"].items() if r.get("blind")}
        if ratings:
            out.setdefault(record["run_id"], {})[record["reviewer"]] = ratings
    return out


def shadow_vetoes(run_dir: Path) -> dict[str, dict[str, dict]]:
    """{judge: {item: record}} for veto-kind shadow answers saved per attempt."""
    out: dict[str, dict[str, dict]] = {}
    for path in sorted(run_dir.glob("cards/*/*/attempt_*/judges/*.json")):
        rec = json.loads(path.read_text())
        veto = rec.get("veto")
        if not veto or not veto.get("valid"):
            continue
        item = f"{run_dir.name}/{rec['card_id']}.a{rec['attempt_no']:02d}"
        out.setdefault(rec["judge_id"], {})[item] = rec
    return out


def runs_section(rng: random.Random, targets: dict) -> dict:
    section: dict = {}
    for run_id, reviewers in run_labels().items():
        run_dir = ROOT / "runs/stage04" / run_id
        vetoes = shadow_vetoes(run_dir)
        entry: dict = {"reviewers": {}, "judges": {}}
        for name, ratings in reviewers.items():
            labels = {i: r["decision"] for i, r in ratings.items()}
            entry["reviewers"][name] = {
                "rated": len(labels),
                **{d: sum(v == d for v in labels.values()) for d in ("accept", "fix", "reject")},
            }
            for judge, recs in vetoes.items():
                per_mode = {}
                for mode in MODES:
                    pred = {i: bool(r["veto"]["not_reject"]) for i, r in recs.items()}
                    res = compare(pred, binary(labels, mode), rng)
                    if mode == PRIMARY_MODE and res["kappa"] is not None:
                        t = targets.get(mode, {})
                        res["vs_targets"] = {
                            "floor": t.get("floor_median_pair_kappa"),
                            "target": t.get("target_mean_person_vs_others"),
                            "ceiling": t.get("ceiling_max_pair_kappa"),
                        }
                    per_mode[mode] = res
                shared = sorted(set(recs) & set(labels))
                per_mode["vetoes"] = sum(recs[i]["veto"]["veto"] for i in shared)
                per_mode["human_rejects"] = sum(labels[i] == "reject" for i in shared)
                entry["judges"].setdefault(judge, {})[name] = per_mode
        section[run_id] = entry
    return section


def main() -> dict:
    rng = random.Random(SEED)
    people = raters()
    names = list(people)
    blind = [n for n in names if n != OPEN]
    model_of = {
        m["item_id"]: m["model"]
        for m in json.loads((OUT / "mapping.private.json").read_text())["cards"]
    }
    report: dict = {
        "scope": "Preliminary: blind reviewers of package kino-01 plus the earlier open G2 "
        "decisions (not blind). Strict = accept; lenient = accept or fix.",
        "primary_mode": PRIMARY_MODE,
        "raters": {},
        "pairs": {},
        "leave_one_out": {},
        "targets": {},
        "judges": {},
        "baseline_gpt_image_rule": {},
        "computed_at": datetime.now(UTC).isoformat(),
    }
    for n, labels in people.items():
        counts = {
            d: sum(v == d for v in labels.values())
            for d in ("accept", "fix", "reject", "uncertain")
        }
        report["raters"][n] = {"rated": len(labels), **counts,
                               "blind": n != OPEN}  # fmt: skip
    for mode in MODES:
        b = {n: binary(v, mode) for n, v in people.items()}
        for i, x in enumerate(names):
            for y in names[i + 1 :]:
                report["pairs"].setdefault(f"{x} vs {y}", {})[mode] = compare(b[x], b[y], rng)
        loo = {}
        for n in names:
            others = majority([b[o] for o in names if o != n])
            res = compare(b[n], others, rng)
            res["accept_rate"] = round(sum(b[n].values()) / len(b[n]), 3)
            loo[n] = res
        report["leave_one_out"][mode] = loo
        pair_k = [report["pairs"][p][mode]["kappa"] for p in report["pairs"]]
        pair_k = [k for k in pair_k if k is not None]
        blind_pairs = [p for p in report["pairs"] if OPEN not in p]
        loo_k = [v["kappa"] for v in loo.values() if v["kappa"] is not None]
        report["targets"][mode] = {
            "floor_median_pair_kappa": round(statistics.median(pair_k), 3) if pair_k else None,
            "target_mean_person_vs_others": round(statistics.mean(loo_k), 3) if loo_k else None,
            "ceiling_max_pair_kappa": round(max(pair_k), 3) if pair_k else None,
            "blind_only_pair_kappa": [report["pairs"][p][mode]["kappa"] for p in blind_pairs],
        }
        everyone = majority(list(b.values()))
        blind_major = majority([b[n] for n in blind])
        for j, per_mode in judges().items():
            report["judges"].setdefault(j, {})[mode] = {
                "vs_majority_all": compare(per_mode[mode], everyone, rng),
                "vs_majority_blind": compare(per_mode[mode], blind_major, rng),
            }
        rule = {i: model_of.get(i) in GPT_IMAGE for i in model_of}
        report["baseline_gpt_image_rule"][mode] = {
            "vs_majority_all": compare(rule, everyone, rng),
            "vs_majority_blind": compare(rule, blind_major, rng),
        }
    report["runs"] = runs_section(rng, report["targets"])
    dump(OUT / "targets.json", report)
    return report


if __name__ == "__main__":
    r = main()
    short = {
        k: r[k] for k in ("primary_mode", "raters", "pairs", "leave_one_out", "targets", "runs")
    }
    print(json.dumps(short, ensure_ascii=False, indent=1))
