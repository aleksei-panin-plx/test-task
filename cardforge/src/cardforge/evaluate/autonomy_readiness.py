"""Stage 11: are the conditions for autonomous mode (TA §12.3) met by the HITL data?

`cardforge eval autonomy-readiness` reads, in place and without network:
- labelled shadow campaigns of `config/autonomy.toml` (`[[readiness.campaigns]]`): shadow
  decisions of the core + human labels (local files; only counts and item ids reach the report);
- every run under `[readiness.scan]` (manifest + costs) for the cost of the accepted set and the
  first-attempt share;
- blind-test scores (`cardforge eval score-blind-test --output`), if any;
- the real auto-pass journal (`auto_pass` / `auto_pass_returned` events, breaker state).

Per generator and decision type (auto-pass, auto-reject, policies) it returns a verdict in roadmap
terms: G2 = flagged / none + audit / not ready, with the failed conditions spelled out. In-sample
labels (τ_a fitted on them) are reported but never used for a verdict.
"""

from __future__ import annotations

import glob
import json
import math
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path

from cardforge.autonomy import load_autonomy

FORMAT = "cardforge-autonomy-readiness-v1"
CRITERIA = {
    "false_pass_max": 0.20,
    "false_pass_upper_max": 0.25,
    "false_reject_max": 0.10,
    "coverage_min": 0.30,
    "min_shadow_images": 150,
    "min_briefs": 3,
    "mandatory_reasons": ["wrong_object", "text", "artifacts"],
    "blind_max_recall": 0.40,
    "blind_min_sheets": 10,
    "max_usd_per_accepted_card": 0.10,
    "first_attempt_min": 0.70,
    "none_g3_return_max": 0.10,
    "none_g3_min_cards": 300,
}
# Reasons of TA §12.3 «обязательные правила»: must_not_have (text) and the spec (wrong object).
# The rest of `mandatory_reasons` (e.g. artifacts) is the stricter fable-review §3.4 list.
TA_MANDATORY = ("wrong_object", "text")


# ---------------------------------------------------------------- statistics (pure Python)


def _binom_cdf(k: int, n: int, p: float) -> float:
    if p <= 0:
        return 1.0
    if p >= 1:
        return 1.0 if k >= n else 0.0
    lp, lq = math.log(p), math.log1p(-p)
    lg = math.lgamma
    return sum(
        math.exp(lg(n + 1) - lg(i + 1) - lg(n - i + 1) + i * lp + (n - i) * lq)
        for i in range(k + 1)
    )


def clopper_pearson(k: int, n: int, alpha: float = 0.05) -> list[float] | None:
    """Exact two-sided 95 % interval of k/n; None when n = 0."""
    if n == 0:
        return None

    def solve(f, target):
        lo, hi = 0.0, 1.0
        for _ in range(60):
            mid = (lo + hi) / 2
            lo, hi = (mid, hi) if f(mid) > target else (lo, mid)
        return (lo + hi) / 2

    upper = 1.0 if k == n else solve(lambda p: _binom_cdf(k, n, p), alpha / 2)
    lower = 0.0 if k == 0 else 1 - solve(lambda p: _binom_cdf(n - k, n, p), alpha / 2)
    return [round(lower, 4), round(upper, 4)]


def rate(k: int, n: int) -> dict:
    return {"k": k, "n": n, "rate": round(k / n, 4) if n else None, "ci95": clopper_pearson(k, n)}


def kappa(a: list[bool], b: list[bool]) -> float | None:
    if not a or len(a) != len(b):
        return None
    n = len(a)
    po = sum(x == y for x, y in zip(a, b, strict=True)) / n
    pa, pb = sum(a) / n, sum(b) / n
    pe = pa * pb + (1 - pa) * (1 - pb)
    return None if pe >= 1 else round((po - pe) / (1 - pe), 4)


# ---------------------------------------------------------------- inputs


def _read(root: Path, rel: str) -> dict:
    path = root / rel
    if not path.exists():
        raise ValueError(f"autonomy readiness: missing input {rel}")
    return json.loads(path.read_text(encoding="utf-8"))


def _dig(data: dict, dotted: str):
    for part in dotted.split("."):
        data = data[part]
    return data


def _labels(root: Path, spec: dict) -> tuple[dict, dict]:
    """(item_id → {y, reasons, unsure}, label quality) for one campaign."""
    kind = spec.get("kind")
    if kind == "y3":
        items = _read(root, spec["path"])["items"]
        labels = {
            i["item_id"]: {
                "y": int(i["y3"]),
                "reasons": i.get("reasons", []),
                "unsure": bool(i.get("unsure")),
            }
            for i in items
        }
        return labels, {"source": "y3 file", "note": "controls / retest: see the rubric-05 README"}
    if kind != "blind":
        raise ValueError(f"autonomy readiness: unknown label kind {kind!r}")
    mapping = _read(root, spec["mapping"])
    ratings = _read(root, spec["ratings"])["ratings"]
    labels, first, controls, repeats, known = {}, {}, defaultdict(lambda: [0, 0]), [], []
    for card in mapping["cards"]:
        r = ratings.get(card["card"])
        if r is None:
            continue
        y = int(r["decision"] == "reject")
        if card["kind"] == "first":
            labels[card["item_id"]] = {
                "y": y,
                "reasons": r.get("reasons", []),
                "unsure": bool(r.get("unsure")),
            }
            first[card["item_id"]] = y
        elif card["kind"] == "control" and card.get("expected"):
            controls[card["session"]][0] += 1
            controls[card["session"]][1] += int(r["decision"] != card["expected"])
        elif card["kind"] == "repeat":
            repeats.append((card["item_id"], y))
        elif card["kind"] == "known_reject":
            known.append(y)
    pairs = [(first[i], y) for i, y in repeats if i in first]
    quality = {
        "controls": {str(s): {"n": n, "miss": m} for s, (n, m) in sorted(controls.items())},
        "drift_sessions": [str(s) for s, (n, m) in sorted(controls.items()) if m > 2],
        "retest": {
            "pairs": len(pairs),
            "agree": sum(a == b for a, b in pairs),
            "kappa": kappa([bool(a) for a, _ in pairs], [bool(b) for _, b in pairs]),
        },
        "unsure": sum(v["unsure"] for v in labels.values()),
        "known_rejects_rejected_again": rate(sum(known), len(known)) if known else None,
    }
    return labels, quality


def _policy_flag(root: Path, image: str | None) -> bool | None:
    """True if a policy rule did not pass on this attempt (eval.json next to the image)."""
    if not image:
        return None
    path = (root / image).parent / "eval.json"
    if not path.exists():
        return None
    verdicts = json.loads(path.read_text(encoding="utf-8")).get("policy_verdicts") or []
    return any(v.get("verdict") != "pass" for v in verdicts)


def load_campaign(root: Path, spec: dict) -> tuple[list[dict], dict]:
    decisions = _read(root, spec["decisions"])["items"]
    labels, quality = _labels(root, spec["labels"])
    extra = set()
    if spec.get("extra_auto_pass"):
        extra = set(
            _dig(_read(root, spec["extra_auto_pass"]["path"]), spec["extra_auto_pass"]["key"])
        )
    rule = spec.get("auto_pass_rule")
    rows = []
    for d in decisions:
        lab = labels.get(d["item_id"])
        if lab is None:
            continue
        shadow = d["shadow"]
        ar = bool(shadow["auto_reject"])
        if "auto_pass" in shadow:
            ap = bool(shadow["auto_pass"])
        elif rule:
            ap = (
                d["profile"] == rule["profile"]
                and not ar
                and d.get("style_p") is not None
                and d["style_p"] <= rule["tau_a"]
            )
        else:
            ap = False
        rows.append(
            {
                "campaign": spec["id"],
                "sample": spec["sample"],
                "run": d["run"],
                "item_id": d["item_id"],
                "profile": d["profile"],
                "y": lab["y"],
                "reasons": lab["reasons"],
                "unsure": lab["unsure"],
                "ar": ar,
                "layers": shadow.get("layers", {}),
                "ap": ap,
                "ap2": ap or d["item_id"] in extra,
                "geometry": d.get("geometry_would") or [],
                "policy_flag": _policy_flag(root, d.get("image")),
            }
        )
        # Runtime rule (service._real_auto_pass): a policy flag is a reason left → card goes to G2.
        rows[-1]["ap_rt"] = rows[-1]["ap2"] and not rows[-1]["policy_flag"]
    info = {
        "id": spec["id"],
        "sample": spec["sample"],
        "labeler": spec["labels"].get("labeler"),
        "question": spec["labels"].get("question"),
        "decisions": len(decisions),
        "labelled": len(rows),
        "runs": sorted({r["run"] for r in rows}),
        "label_quality": quality,
        "extra_auto_pass": len(extra),
    }
    return rows, info


# ---------------------------------------------------------------- metrics


def metrics(rows: list[dict], crit: dict) -> dict:
    n = len(rows)
    out = {
        "n": n,
        "briefs": len({r["run"] for r in rows}),
        "human_reject": rate(sum(r["y"] for r in rows), n),
    }
    mand = set(crit["mandatory_reasons"])
    for key, name in (
        ("ap", "auto_pass_journal"),
        ("ap2", "auto_pass_journal_v2"),
        ("ap_rt", "auto_pass"),
    ):
        sel = [r for r in rows if r[key]]
        bad = [r for r in sel if r["y"]]
        missed = [
            {"item_id": r["item_id"], "reasons": sorted(set(r["reasons"]) & mand)}
            for r in bad
            if set(r["reasons"]) & mand
        ]
        out[name] = {
            "share": rate(len(sel), n),
            "false_pass": rate(len(bad), len(sel)),
            "false_pass_reasons": dict(Counter(x for r in bad for x in r["reasons"])),
            "mandatory_missed": missed,
            "ta_mandatory_missed": [m for m in missed if set(m["reasons"]) & set(TA_MANDATORY)],
            "policy_flag_passed": sum(bool(r["policy_flag"]) for r in sel),
            "kappa_vs_human": kappa([r[key] for r in rows], [not r["y"] for r in rows]),
            "human_review_share": rate(n - len(sel), n),
        }
    ar = [r for r in rows if r["ar"]]
    layers = sorted({k for r in rows for k in r["layers"]})
    out["auto_reject"] = {
        "share": rate(len(ar), n),
        "false_reject": rate(sum(1 - r["y"] for r in ar), len(ar)),
        "by_layer": {
            k: rate(
                sum(1 - r["y"] for r in rows if r["layers"].get(k)),
                sum(1 for r in rows if r["layers"].get(k)),
            )
            for k in layers
        },
        "kappa_vs_human": kappa([r["ar"] for r in rows], [bool(r["y"]) for r in rows]),
    }
    geo = [r for r in rows if r["geometry"]]
    if geo:
        out["geometry_flags"] = {
            "share": rate(len(geo), n),
            "human_reject": rate(sum(r["y"] for r in geo), len(geo)),
        }
    out["coverage_shadow"] = rate(sum(r["ar"] or r["ap_rt"] for r in rows), n)
    out["policy_flags"] = sum(bool(r["policy_flag"]) for r in rows)
    out["all_pass_baseline"] = rate(
        sum(r["y"] for r in rows if not r["ar"]), sum(not r["ar"] for r in rows)
    )
    return out


# ---------------------------------------------------------------- runs: cost and first attempt


def scan_runs(root: Path, scan: dict) -> dict:
    seen, runs = set(), []
    skip = set(scan.get("skip_profiles", ["fake"]))
    for pattern in scan.get("runs_glob", []):
        for path in sorted(glob.glob(str(root / pattern), recursive=True)):
            run_dir = Path(path).parent
            if run_dir in seen or not (run_dir / "costs.json").exists():
                continue
            seen.add(run_dir)
            m = json.loads(Path(path).read_text(encoding="utf-8"))
            if m.get("render_profile") in skip:
                continue
            calls = json.loads((run_dir / "costs.json").read_text(encoding="utf-8")).get(
                "calls", []
            )
            cost = sum(c.get("actual_usd") or 0 for c in calls)
            est_only = sum(
                c.get("actual_usd") or 0
                for c in calls
                if c.get("kind") == "render"
                and c.get("actual_usd")
                and c.get("actual_usd") == c.get("estimate_usd")
            )
            cards = [c for c in (m.get("cards") or {}).values() if isinstance(c, dict)]
            acc = [c for c in cards if c.get("state") == "accepted"]
            first = sum(bool(c.get("first_attempt_accepted")) for c in acc)
            runs.append(
                {
                    "run": str(run_dir.relative_to(root)),
                    "profile": m.get("render_profile"),
                    "created_at": m.get("created_at"),
                    "state": m.get("state"),
                    "positions": len(cards),
                    "accepted": len(acc),
                    "first_attempt_accepted": first,
                    "cost_usd": round(cost, 4),
                    "estimate_only_usd": round(est_only, 4),
                    "usd_per_accepted": round(cost / len(acc), 4) if acc else None,
                    "usd_per_accepted_excl_estimates": round((cost - est_only) / len(acc), 4)
                    if acc
                    else None,
                }
            )
    since = str(scan.get("cost_since") or "")
    return {
        "since": since or None,
        "runs": runs,
        "by_profile": _by_profile([r for r in runs if (r["created_at"] or "") >= since]),
        "by_profile_all_runs": _by_profile(runs),
    }


def _by_profile(runs: list[dict]) -> dict:
    by_profile = {}
    for prof in sorted({r["profile"] for r in runs if r["accepted"]}, key=str):
        sel = [r for r in runs if r["profile"] == prof and r["accepted"]]
        acc, pos = sum(r["accepted"] for r in sel), sum(r["positions"] for r in sel)
        cost = sum(r["cost_usd"] for r in sel)
        est = sum(r["estimate_only_usd"] for r in sel)
        first = sum(r["first_attempt_accepted"] for r in sel)
        by_profile[prof] = {
            "runs": len(sel),
            "positions": pos,
            "accepted": acc,
            "usd_per_accepted": round(cost / acc, 4),
            "usd_per_10_card_set": round(10 * cost / acc, 3),
            "usd_per_accepted_excl_estimates": round((cost - est) / acc, 4),
            "first_attempt_of_accepted": rate(first, acc),
            "first_attempt_of_positions": rate(first, pos),
            "run_ids": [r["run"] for r in sel],
        }
    return by_profile


def scan_blind(root: Path, patterns: list[str]) -> list[dict]:
    found = []
    for pattern in patterns:
        for path in sorted(glob.glob(str(root / pattern), recursive=True)):
            try:
                data = json.loads(Path(path).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(data, dict) or "target_recall" not in data or "trials" not in data:
                continue
            found.append(
                {
                    "file": str(Path(path).relative_to(root)),
                    "family": data.get("family"),
                    "control_all_real": bool(data.get("control_all_real")),
                    "sheets": int(data["trials"]) // 3,
                    "target_recall": data["target_recall"],
                    "chance_recall": data.get("chance_recall"),
                    "p": data.get("exact_two_sided_p"),
                }
            )
    return found


def scan_auto_pass(root: Path, scan: dict) -> dict:
    passed = returned = 0
    seen = set()
    for pattern in scan.get("runs_glob", []):
        for path in glob.glob(str(root / pattern), recursive=True):
            events = Path(path).parent / "events.jsonl"
            if events in seen or not events.exists():
                continue
            seen.add(events)
            for line in events.read_text(encoding="utf-8").splitlines():
                kind = json.loads(line).get("type") if line.strip() else None
                passed += kind == "auto_pass"
                returned += kind == "auto_pass_returned"
    state_path = root / scan.get("auto_pass_state", "runs/_autopass/state.json")
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else None
    return {
        "real_auto_passed": passed,
        "g3_returned": returned,
        "g3_return_rate": rate(returned, passed),
        "breaker_disabled": (state or {}).get("disabled"),
    }


def unlabelled_decisions(root: Path, patterns: list[str], listed: set[str]) -> list[str]:
    out = set()
    for pattern in patterns:
        for path in glob.glob(str(root / pattern), recursive=True):
            rel = str(Path(path).relative_to(root))
            if rel not in listed:
                out.add(rel)
    return sorted(out)


# ---------------------------------------------------------------- verdicts


def _check(name: str, ok: bool | None, value, need) -> dict:
    return {"name": name, "ok": ok, "value": value, "need": need}


def auto_pass_verdict(m: dict | None, crit: dict) -> dict:
    if not m or not m["auto_pass"]["share"]["k"]:
        return {
            "status": "not_ready",
            "why": "нет зоны авто-пропуска (ни одной карты в зоне вне выборки)",
            "checks": [],
        }
    fp = m["auto_pass"]["false_pass"]
    checks = [
        _check(
            "n_images",
            m["n"] >= crit["min_shadow_images"],
            m["n"],
            f">= {crit['min_shadow_images']}",
        ),
        _check(
            "briefs", m["briefs"] >= crit["min_briefs"], m["briefs"], f">= {crit['min_briefs']}"
        ),
        _check(
            "false_pass",
            fp["rate"] <= crit["false_pass_max"],
            fp["rate"],
            f"<= {crit['false_pass_max']}",
        ),
        _check(
            "false_pass_upper",
            fp["ci95"][1] <= crit["false_pass_upper_max"],
            fp["ci95"][1],
            f"<= {crit['false_pass_upper_max']}",
        ),
        _check(
            "coverage",
            m["auto_pass"]["share"]["rate"] >= crit["coverage_min"],
            m["auto_pass"]["share"]["rate"],
            f">= {crit['coverage_min']}",
        ),
        _check(
            "ta_mandatory_missed",
            not m["auto_pass"]["ta_mandatory_missed"],
            len(m["auto_pass"]["ta_mandatory_missed"]),
            "0 (text, wrong_object)",
        ),
    ]
    strict = _check(
        "fable_layer0_missed",
        not m["auto_pass"]["mandatory_missed"],
        len(m["auto_pass"]["mandatory_missed"]),
        f"0 ({', '.join(crit['mandatory_reasons'])})",
    )
    failed = [c["name"] for c in checks if not c["ok"]]
    if failed:
        return {
            "status": "not_ready",
            "why": f"не выполнено: {', '.join(failed)}",
            "checks": [*checks, strict],
        }
    status = "ready_flagged" if strict["ok"] else "ready_flagged_with_deviation"
    missed = m["auto_pass"]["mandatory_missed"]
    why = (
        "условия выхода из тени выполнены"
        if strict["ok"]
        else f"условия TA §12.3 выполнены; нарушено строгое правило fable §3.4: "
        f"{strict['value']} авто-пропуск(а) с отказом человека за "
        f"{', '.join(sorted({x for i in missed for x in i['reasons']}))}"
    )
    return {"status": status, "why": why, "checks": [*checks, strict]}


def auto_reject_verdict(m: dict | None, crit: dict) -> dict:
    if not m or not m["auto_reject"]["share"]["k"]:
        return {"status": "not_ready", "why": "авто-отказ не срабатывал", "checks": []}
    fr = m["auto_reject"]["false_reject"]
    checks = [
        _check(
            "n_images",
            m["n"] >= crit["min_shadow_images"],
            m["n"],
            f">= {crit['min_shadow_images']}",
        ),
        _check(
            "false_reject",
            fr["rate"] <= crit["false_reject_max"],
            fr["rate"],
            f"<= {crit['false_reject_max']}",
        ),
    ]
    failed = [c["name"] for c in checks if not c["ok"]]
    return {
        "status": "not_ready" if failed else "ready_shadow_exit",
        "why": f"не выполнено: {', '.join(failed)}" if failed else "ложных отказов в пределе",
        "checks": checks,
    }


def policy_verdict(root: Path, rows: list[dict]) -> dict:
    fixtures = [
        p
        for p in (
            "tests/policy/cn_objects.yaml",
            "tests/policy/ip_objects.yaml",
            "tests/policy/images",
        )
        if (root / p).exists()
    ]
    return {
        "status": "not_ready",
        "why": (
            "полнота проверок не измерена "
            "(фикстуры tests/policy/ и `cardforge eval policy` — после MVP)"
        ),
        "fixtures_present": fixtures,
        "observed": {
            "policy_flags": sum(bool(r["policy_flag"]) for r in rows),
            "policy_flag_auto_passed": sum(bool(r["policy_flag"]) and r["ap_rt"] for r in rows),
            "policy_flag_in_shadow_journal": sum(bool(r["policy_flag"]) and r["ap2"] for r in rows),
            "eval_json_missing": sum(r["policy_flag"] is None for r in rows),
        },
    }


def blind_verdict(found: list[dict], crit: dict) -> dict:
    real = [b for b in found if not b["control_all_real"]]
    if not real:
        return {
            "status": "not_run",
            "why": "слепого теста на финальных сетах нет (этап 9)",
            "found": found,
        }
    ok = [
        b
        for b in real
        if b["sheets"] >= crit["blind_min_sheets"]
        and b["target_recall"] <= crit["blind_max_recall"]
    ]
    return {
        "status": "pass" if ok else "fail",
        "why": (
            f"{len(ok)} из {len(real)} тестов: обнаружение ≤ {crit['blind_max_recall']:.0%} "
            f"на ≥ {crit['blind_min_sheets']} листах"
        ),
        "found": found,
    }


def cost_verdict(costs: dict, profile: str, crit: dict) -> dict:
    p = costs["by_profile"].get(profile)
    if not p:
        return {"status": "not_measured", "why": "нет прогонов с принятыми картами"}
    ok = p["usd_per_accepted"] <= crit["max_usd_per_accepted_card"]
    fa = p["first_attempt_of_positions"]["rate"]
    return {
        "status": "pass" if ok else "fail",
        "usd_per_accepted": p["usd_per_accepted"],
        "need": f"<= {crit['max_usd_per_accepted_card']}",
        "first_attempt_of_positions": fa,
        "first_attempt_ok": fa is not None and fa >= crit["first_attempt_min"],
        "first_attempt_need": f">= {crit['first_attempt_min']}",
    }


def overall(per_profile: dict, policy: dict, blind: dict, g3: dict, crit: dict) -> dict:
    flagged = [
        p for p, v in per_profile.items() if v["auto_pass"]["status"].startswith("ready_flagged")
    ]
    blockers = []
    for prof, v in per_profile.items():
        if v["auto_reject"]["status"] != "ready_shadow_exit":
            blockers.append(f"{prof}: авто-отказ не готов ({v['auto_reject']['why']})")
        if v["auto_pass"]["status"] == "ready_flagged_with_deviation":
            blockers.append(f"{prof}: {v['auto_pass']['why']}")
        cost = v.get("cost") or {}
        if cost.get("status") == "pass" and not cost.get("first_attempt_ok"):
            blockers.append(
                f"{prof}: доля первой попытки {cost['first_attempt_of_positions']} "
                f"< {crit['first_attempt_min']}"
            )
        elif cost.get("status") != "pass":
            blockers.append(f"{prof}: стоимость принятого набора — {cost.get('status')}")
    if policy["status"] != "ready":
        blockers.append(f"политики: {policy['why']}")
    if blind["status"] != "pass":
        blockers.append(f"слепой тест: {blind['why']}")
    if g3["real_auto_passed"] < crit["none_g3_min_cards"]:
        blockers.append(
            f"возвраты G3: {g3['real_auto_passed']} реально авто-пропущенных карт "
            f"< {crit['none_g3_min_cards']}"
        )
    if flagged:
        g2 = (
            f"готово к G2 = flagged для {', '.join(flagged)} (авто-пропуск); "
            "остальные генераторы — G2 = all"
        )
    else:
        g2 = "не готово: ни у одного генератора нет авто-пропуска в критерии"
    return {
        "g2_flagged": flagged,
        "none_audit": "not_ready",
        "none_audit_blockers": blockers,
        "summary": g2 + "; G1/G2 = none + аудит — не готово",
    }


# ---------------------------------------------------------------- entry point


def autonomy_readiness(root: Path) -> dict:
    root = root.resolve()
    cfg = load_autonomy(root)
    crit = {**CRITERIA, **cfg["readiness"].get("criteria", {})}
    scan = cfg["readiness"].get("scan", {})
    rows, campaigns = [], []
    for spec in cfg["readiness"].get("campaigns", []):
        r, info = load_campaign(root, spec)
        rows += r
        campaigns.append(info)
    profiles = sorted({r["profile"] for r in rows})
    groups = {}
    for camp in campaigns:
        sel = [r for r in rows if r["campaign"] == camp["id"]]
        groups[camp["id"]] = {
            "all": metrics(sel, crit),
            **{
                p: metrics([r for r in sel if r["profile"] == p], crit)
                for p in profiles
                if any(r["profile"] == p for r in sel)
            },
        }
    oos = [r for r in rows if r["sample"] == "out_of_sample"]
    pooled = {
        p: metrics([r for r in oos if r["profile"] == p], crit)
        for p in profiles
        if any(r["profile"] == p for r in oos)
    }
    costs = scan_runs(root, scan)
    blind = blind_verdict(scan_blind(root, scan.get("blind_glob", [])), crit)
    g3 = scan_auto_pass(root, scan)
    per_profile = {}
    for p in profiles:
        m = pooled.get(p)
        per_profile[p] = {
            "auto_pass": auto_pass_verdict(m, crit),
            "auto_reject": auto_reject_verdict(m, crit),
            "cost": cost_verdict(costs, p, crit),
            "out_of_sample_images": m["n"] if m else 0,
        }
    policy = policy_verdict(root, rows)
    labelers = sorted({c["labeler"] for c in campaigns if c["labeler"]})
    caveats = []
    if len(labelers) == 1:
        caveats.append(f"один разметчик ({labelers[0]}): автомат откалиброван на его критерий")
    known = [c["label_quality"].get("known_rejects_rejected_again") for c in campaigns]
    known = [k for k in known if k]
    if known:
        k, n = sum(x["k"] for x in known), sum(x["n"] for x in known)
        caveats.append(
            f"известные отказы повторно отклонены {k}/{n}: критерий (материал) нестабилен"
        )
    listed = {c["decisions"] for c in cfg["readiness"].get("campaigns", [])}
    return {
        "format": FORMAT,
        "generated_at": datetime.now(UTC).isoformat(),
        "network": False,
        "criteria": crit,
        "campaigns": campaigns,
        "by_campaign": groups,
        "out_of_sample": pooled,
        "verdicts": {
            "by_profile": per_profile,
            "policies": policy,
            "blind_test": blind,
            "g3_monitor": g3,
        },
        "costs": costs,
        "overall": overall(per_profile, policy, blind, g3, crit),
        "labelers": labelers,
        "caveats": caveats,
        "unlabelled_decisions": unlabelled_decisions(root, scan.get("decisions_glob", []), listed),
        "autonomy_hooks": {
            "external_actions": cfg["external_actions"],
            "audit": cfg["audit"],
            "stop": cfg["stop"],
            "schedule": cfg["schedule"],
        },
    }
