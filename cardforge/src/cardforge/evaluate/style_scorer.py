"""Layer 1 «style scorer» of the judge architecture (docs/research/fable-review.md §3.2–3.3).

A linear probe (L2 logistic regression) on frozen CSD ViT-L embeddings (`models/csd-vit-l`,
local CPU, $0) predicts the probability that a person rejects the card for style /
materials / dullness (`p_reject`). Trained offline by `scripts/stage04_style_scorer.py` on the
project's own human labels; the artifact (coefficients, zones, training-set hash, embedding
weights hash) is pinned by its sha256 in `config/style_scorer.toml` and verified on load.

Risk zones (§3.3, selective prediction): on cross-fitted (out-of-fold) scores of the
calibration labels, `tau_a` is the largest score with a false-pass share among the cards at or
below it ≤ R_a and the exact (Clopper–Pearson) 95 % upper bound ≤ R_a + slack; `tau_r` is the
smallest score with a false-reject share among the cards at or above it ≤ R_r (+ slack on the
upper bound). Between them — the grey zone (a person decides). R_a = 20 % (user, 2026-11-06).

Runtime: the entry `style_score` of `HardResult.checks` is **signal only** (`passed=None`,
`required=False`), like `look`. Mode `shadow` (default) only records the zone the automaton
would choose. `auto_pass` is refused in code until the shadow-mode exit criteria of §3.4
step 2 are met and a person changes this module; `auto_reject` by score can be switched on in
the config, but stays off by default.

Ranking (`rank_candidates`) is the exported function for best-of-N (fable-review Q2): style
score + hard-check failures + veto-judge penalty, lower = better. API in README.

No label, image or embedding leaves the machine: nothing here makes a network call.
"""

from __future__ import annotations

import json
import math
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from cardforge.evaluate.common import sha256

CONFIG = "config/style_scorer.toml"
SIGNAL = "style_score"
VERSION = "style-probe-v1"
MODES = ("off", "shadow", "auto_reject")
ZONES = ("auto_reject", "auto_pass", "grey")
_PACKAGE_ROOT = Path(__file__).resolve().parents[3]


class ScorerError(ValueError):
    pass


# ---------------------------------------------------------------- statistics


def fit_logreg(
    X: np.ndarray,
    y: np.ndarray,
    C: float = 1.0,
    sample_weight: np.ndarray | None = None,
    standardize: bool = True,
    max_iter: int = 100,
    tol: float = 1e-8,
    pairs: tuple | None = None,
) -> dict:
    """L2 logistic regression by Newton–Raphson (IRLS); objective of scikit-learn
    `LogisticRegression(C)`: C·Σ wᵢ·logloss + ½‖β‖², intercept not penalised.
    `standardize` z-scores the features with the training mean/std (kept in the model).

    `pairs = (D, t, w)` adds a Bradley–Terry term on the same coefficients: for a comparison
    with D = x_right − x_left, P(left is closer to the game style) = σ(β·D) (a lower p_reject
    logit wins), target t = 1 / 0 / 0.5 (tie), weight w. No intercept in the pair rows."""
    X = np.asarray(X, dtype=np.float64)
    y = np.asarray(y, dtype=np.float64)
    sw = np.ones(len(y)) if sample_weight is None else np.asarray(sample_weight, float)
    mean = X.mean(0) if standardize else np.zeros(X.shape[1])
    std = X.std(0) if standardize else np.ones(X.shape[1])
    std = np.where(std > 1e-12, std, 1.0)
    Z = np.hstack([(X - mean) / std, np.ones((len(X), 1))])
    if pairs is not None and len(pairs[0]):
        D, t, w = (np.asarray(a, dtype=np.float64) for a in pairs)
        Z = np.vstack([Z, np.hstack([D / std, np.zeros((len(D), 1))])])
        y = np.concatenate([y, t])
        sw = np.concatenate([sw, w])
    beta = np.zeros(Z.shape[1])
    reg = np.full(Z.shape[1], 1.0 / C)
    reg[-1] = 0.0
    for _ in range(max_iter):
        p = 1 / (1 + np.exp(-np.clip(Z @ beta, -35, 35)))
        grad = Z.T @ (sw * (p - y)) + reg * beta
        H = (Z * (sw * p * (1 - p))[:, None]).T @ Z + np.diag(reg) + 1e-10 * np.eye(len(beta))
        step = np.linalg.solve(H, grad)
        beta -= step
        if np.max(np.abs(step)) < tol:
            break
    return {
        "coef": beta[:-1].tolist(),
        "intercept": float(beta[-1]),
        "mean": mean.tolist(),
        "std": std.tolist(),
        "C": C,
        "standardize": standardize,
    }


def predict(model: dict, X) -> np.ndarray:
    X = np.atleast_2d(np.asarray(X, dtype=np.float64))
    z = ((X - np.asarray(model["mean"])) / np.asarray(model["std"])) @ np.asarray(model["coef"])
    return 1 / (1 + np.exp(-np.clip(z + model["intercept"], -35, 35)))


def group_folds(groups: list[str], k: int, seed: int) -> np.ndarray:
    """Fold id per row; all rows of one group (planned position) share a fold."""
    uniq = sorted(set(groups))
    rng = np.random.default_rng(seed)
    order = rng.permutation(len(uniq))
    fold_of = {uniq[i]: n % k for n, i in enumerate(order)}
    return np.array([fold_of[g] for g in groups])


def cross_val(
    X,
    y,
    groups: list[str],
    *,
    C: float,
    k: int = 5,
    seed: int = 0,
    sample_weight=None,
    standardize: bool = True,
) -> np.ndarray:
    """Out-of-fold p_reject (cross-fitting by position groups)."""
    X, y = np.asarray(X), np.asarray(y)
    folds = group_folds(groups, k, seed)
    out = np.zeros(len(y))
    for f in range(k):
        te = folds == f
        tr = ~te
        sw = None if sample_weight is None else np.asarray(sample_weight)[tr]
        m = fit_logreg(X[tr], y[tr], C=C, sample_weight=sw, standardize=standardize)
        out[te] = predict(m, X[te])
    return out


def auc(scores, y) -> float | None:
    """ROC AUC of `scores` for the positive class y=1 (ties count half)."""
    s, y = np.asarray(scores, float), np.asarray(y).astype(bool)
    pos, neg = s[y], s[~y]
    if not len(pos) or not len(neg):
        return None
    order = np.argsort(np.concatenate([pos, neg]), kind="mergesort")
    allv = np.concatenate([pos, neg])[order]
    ranks = np.empty(len(allv))
    i = 0
    while i < len(allv):
        j = i
        while j + 1 < len(allv) and allv[j + 1] == allv[i]:
            j += 1
        ranks[i : j + 1] = (i + j) / 2 + 1
        i = j + 1
    r = np.empty(len(allv))
    r[order] = ranks
    rp = r[: len(pos)].sum()
    return float((rp - len(pos) * (len(pos) + 1) / 2) / (len(pos) * len(neg)))


def kappa(pred, y) -> float | None:
    a, b = np.asarray(pred).astype(bool), np.asarray(y).astype(bool)
    if not len(a):
        return None
    po = float((a == b).mean())
    pa, pb = a.mean(), b.mean()
    pe = pa * pb + (1 - pa) * (1 - pb)
    return None if pe >= 1 else float((po - pe) / (1 - pe))


def _binom_cdf(k: int, n: int, p: float) -> float:
    if p <= 0:
        return 1.0
    if p >= 1:
        return 1.0 if k >= n else 0.0
    lp, lq = math.log(p), math.log1p(-p)
    return float(
        sum(
            math.exp(math.lgamma(n + 1) - math.lgamma(i + 1) - math.lgamma(n - i + 1)
                     + i * lp + (n - i) * lq)
            for i in range(k + 1)
        )
    )  # fmt: skip


def clopper_pearson(k: int, n: int, alpha: float = 0.05) -> tuple[float, float]:
    """Exact two-sided (1 − alpha) interval of a binomial share k/n."""
    if n == 0:
        return (0.0, 1.0)

    def solve(f, target):
        lo, hi = 0.0, 1.0
        for _ in range(60):
            mid = (lo + hi) / 2
            if f(mid) > target:
                lo = mid
            else:
                hi = mid
        return (lo + hi) / 2

    upper = 1.0 if k == n else solve(lambda p: _binom_cdf(k, n, p), alpha / 2)
    lower = 0.0 if k == 0 else 1 - solve(lambda p: _binom_cdf(n - k, n, p), alpha / 2)
    return (round(lower, 4), round(upper, 4))


# ---------------------------------------------------------------- risk zones (§3.3)


def calibrate_zones(
    scores,
    y,
    *,
    r_a: float = 0.20,
    r_r: float = 0.10,
    slack: float = 0.05,
    min_n: int = 20,
) -> dict:
    """Thresholds on p_reject from calibration scores `scores` and labels `y` (1 = reject).

    auto_pass: p ≤ tau_a; the largest tau_a with share of y=1 at or below ≤ r_a and its
    Clopper–Pearson 95 % upper bound ≤ r_a + slack, on ≥ min_n cards.
    auto_reject: p ≥ tau_r; the smallest tau_r with share of y=0 at or above ≤ r_r and its
    upper bound ≤ r_r + slack, on ≥ min_n cards. No feasible threshold → None (zone empty)."""
    s, y = np.asarray(scores, float), np.asarray(y).astype(int)
    n = len(s)
    order = np.argsort(s, kind="mergesort")
    ss, yy = s[order], y[order]
    tau_a = pass_stats = None
    for i in range(min_n, n + 1):
        if i < n and ss[i] == ss[i - 1]:
            continue  # a threshold must not split tied scores
        bad = int(yy[:i].sum())
        lo, hi = clopper_pearson(bad, i)
        if bad / i <= r_a and hi <= r_a + slack:
            tau_a = float(ss[i - 1])
            pass_stats = {"n": i, "false_pass": bad, "rate": round(bad / i, 4), "ci95": [lo, hi]}
    tau_r = rej_stats = None
    for i in range(n - min_n, -1, -1):
        if i > 0 and ss[i] == ss[i - 1]:
            continue
        m = n - i
        bad = int((1 - yy[i:]).sum())
        lo, hi = clopper_pearson(bad, m)
        if bad / m <= r_r and hi <= r_r + slack:
            tau_r = float(ss[i])
            rej_stats = {"n": m, "false_reject": bad, "rate": round(bad / m, 4), "ci95": [lo, hi]}
    if tau_a is not None and tau_r is not None and tau_a >= tau_r:
        raise ScorerError("auto-pass and auto-reject zones overlap: check the labels")
    return {
        "r_a": r_a,
        "r_r": r_r,
        "slack": slack,
        "min_n": min_n,
        "tau_a": tau_a,
        "tau_r": tau_r,
        "auto_pass": pass_stats,
        "auto_reject": rej_stats,
        "n": n,
    }


def zone_of(p: float, zones: dict) -> str:
    if zones.get("tau_r") is not None and p >= zones["tau_r"]:
        return "auto_reject"
    if zones.get("tau_a") is not None and p <= zones["tau_a"]:
        return "auto_pass"
    return "grey"


def zone_report(scores, y, zones: dict, keys: list[str] | None = None) -> dict:
    """Coverage and error shares of the zones, overall and per key (e.g. generator)."""
    s, y = np.asarray(scores, float), np.asarray(y).astype(int)
    z = np.array([zone_of(v, zones) for v in s])

    def block(mask):
        n = int(mask.sum())
        out = {"n": n, "reject_rate": round(float(y[mask].mean()), 4) if n else None}
        for name, bad_label in (("auto_pass", 1), ("auto_reject", 0)):
            m = mask & (z == name)
            k = int((y[m] == bad_label).sum())
            out[name] = {
                "n": int(m.sum()),
                "coverage": round(int(m.sum()) / n, 4) if n else None,
                "errors": k,
                "error_rate": round(k / int(m.sum()), 4) if m.sum() else None,
                "ci95": list(clopper_pearson(k, int(m.sum()))) if m.sum() else None,
            }
        out["grey"] = int((mask & (z == "grey")).sum())
        out["coverage_total"] = (
            round((out["auto_pass"]["n"] + out["auto_reject"]["n"]) / n, 4) if n else None
        )
        return out

    report = {"all": block(np.ones(len(s), bool))}
    if keys is not None:
        k = np.asarray(keys)
        for key in sorted(set(keys)):
            report[key] = block(k == key)
    return report


# ---------------------------------------------------------------- artifact and config


def load_config(root: Path | None = None) -> dict | None:
    path = (root or _PACKAGE_ROOT) / CONFIG
    return tomllib.loads(path.read_text()) if path.is_file() else None


def load_model(root: Path, cfg: dict) -> dict:
    """The pinned artifact; a changed file (sha256 ≠ config) is refused."""
    path = root / cfg["model"]
    if not path.is_file():
        raise ScorerError(f"style scorer artifact missing: {cfg['model']}")
    digest = sha256(path)
    if cfg.get("model_sha256") and digest != cfg["model_sha256"]:
        raise ScorerError(f"style scorer artifact changed: {digest} ≠ config model_sha256")
    model = json.loads(path.read_text())
    if model.get("version") != VERSION:
        raise ScorerError(f"unknown style scorer version {model.get('version')!r}")
    if len(model["probe"]["coef"]) != model["embedding"]["dim"]:
        raise ScorerError("style scorer: coefficient length ≠ embedding dim")
    model["_sha256"] = digest
    return model


def check_mode(cfg: dict) -> str:
    """`auto_pass` is not a mode: it needs the shadow-mode exit criteria (§3.4 step 2)."""
    mode = cfg.get("mode", "shadow")
    if mode not in MODES:
        raise ScorerError(
            f"style scorer mode {mode!r}: allowed {MODES}; auto-pass stays forbidden until the "
            "shadow-mode criteria of docs/research/fable-review.md §3.4 step 2 are met"
        )
    return mode


def score_vector(model: dict, vector) -> float:
    return float(predict(model["probe"], np.asarray(vector, float)[None, :])[0])


def zones_for(model: dict, profile: str | None, overrides: dict | None) -> tuple[dict, str]:
    """The artifact's zones, with a per-generator override of the config
    (`[zones_by_profile.<render profile>]`, e.g. a stricter `tau_r` for GPT) when one is set.
    Returns (zones, source)."""
    zones = dict(model["zones"])
    over = (overrides or {}).get(profile or "", {})
    if not over:
        return zones, "artifact"
    for key in ("tau_a", "tau_r"):
        if key in over:
            zones[key] = over[key]
    return zones, f"config zones_by_profile.{profile}"


def card_signal(
    p: float | None, model: dict | None, mode: str, note: str | None = None, *,
    profile: str | None = None, overrides: dict | None = None,
) -> dict:  # fmt: skip
    """The `style_score` entry of `HardResult.checks`: always signal-only, never fails a card.
    `would` = what the automaton would do (shadow); `acts` = True only for an enabled
    score-based auto-reject (mode `auto_reject`). `profile` selects a per-generator zone
    override (`overrides` = config `zones_by_profile`)."""
    base = {
        "passed": None,
        "required": False,
        "signal_only": True,
        "code": None,
        "mode": mode,
        "threshold": "zones of config/style_scorer.toml (fable-review §3.3)",
    }
    if p is None or model is None:
        return {**base, "value": "not_run", "note": note or "no style scorer"}
    zones, source = zones_for(model, profile, overrides)
    zone = zone_of(p, zones)
    entry = {
        **base,
        "value": round(p, 4),
        "zone": zone,
        "would": {"auto_reject": "reject", "auto_pass": "pass", "grey": "human"}[zone],
        "acts": mode == "auto_reject" and zone == "auto_reject",
        "tau_a": zones.get("tau_a"),
        "tau_r": zones.get("tau_r"),
        "zones_source": source,
        "profile": profile,
        "model": model["name"],
        "model_sha256": model.get("_sha256"),
    }
    if entry["acts"]:
        entry["code"] = "style"
    if note:
        entry["note"] = note
    return entry


# ---------------------------------------------------------------- reference distance (J7)

REF_SIGNAL = "ref_distance"
REF_INDEX = "dataset/analysis/evaluation/csd-style-index.json"  # CSD of the 130 customer cards
STYLE_REFS_FILE = "knowledge/style_profile/reference_cards_v1.yaml"  # JSON despite the suffix
REF_KEYS = ("style_mean", "bundle_mean", "bundle_max", "all_mean", "all_max", "all_knn5")


def load_ref_index(root: Path, rel: str = REF_INDEX) -> dict | None:
    """{ids, R (unit rows), pos, style, sha256} or None when the local index is absent."""
    path = root / rel
    if not path.is_file():
        return None
    idx = json.loads(path.read_text())
    R = np.asarray(idx["vectors"], dtype=np.float64)
    R /= np.linalg.norm(R, axis=1, keepdims=True)
    style_path = root / STYLE_REFS_FILE
    style = json.loads(style_path.read_text())["style_references"] if style_path.is_file() else []
    pos = {rid: n for n, rid in enumerate(idx["ids"])}
    return {
        "ids": list(idx["ids"]),
        "R": R,
        "pos": pos,
        "style": [pos[s] for s in style if s in pos],
        "sha256": sha256(path),
        "path": rel,
    }


def ref_similarity(vector, index: dict, bundle_ids: list[str] | tuple = (), k: int = 5) -> dict:
    """CSD cosine of one card to reference groups (E-M7 step A definitions): the game's style
    references (mean), the card's own bundle references (mean / max), all customer cards
    (mean / max / mean of the k nearest). Higher = closer to the references. `nearest` = id."""
    v = np.asarray(vector, dtype=np.float64)
    v = v / np.linalg.norm(v)
    sims = index["R"] @ v
    own = [index["pos"][r] for r in bundle_ids if r in index["pos"]]
    out = {
        "style_mean": float(sims[index["style"]].mean()) if index["style"] else None,
        "bundle_mean": float(sims[own].mean()) if own else None,
        "bundle_max": float(sims[own].max()) if own else None,
        "all_mean": float(sims.mean()),
        "all_max": float(sims.max()),
        "all_knn5": float(np.sort(sims)[-k:].mean()),
    }
    out["nearest"] = index["ids"][int(np.argmax(sims))]
    return out


def ref_signal(sim: dict | None, index: dict | None, note: str | None = None) -> dict:
    """The `ref_distance` entry of `HardResult.checks`: signal only (J7), never fails a card.
    `value` = 1 − kNN-5 cosine to the customer cards (larger = farther from the game)."""
    base = {"passed": None, "required": False, "signal_only": True, "code": None,
            "threshold": "none (signal only; autonomy-readiness J7)"}  # fmt: skip
    if sim is None or index is None:
        return {**base, "value": "not_run", "note": note or "no reference index"}
    return {
        **base,
        "value": round(1.0 - sim["all_knn5"], 4),
        "cosine": {k: (None if sim[k] is None else round(sim[k], 4)) for k in REF_KEYS},
        "nearest": sim["nearest"],
        "index": index["path"],
        "index_sha256": index["sha256"],
    }


@dataclass
class Scorer:
    """Runtime scorer: config + artifact + an embedder (paths → CSD vectors)."""

    model: dict
    mode: str
    embed: object  # Callable[[list[Path]], list[list[float]]]
    cache: dict = field(default_factory=dict)  # image sha256 → vector
    overrides: dict = field(default_factory=dict)  # config zones_by_profile
    ref_index: dict | None = None  # load_ref_index (J7); None → no `ref_distance` entry

    def score(self, image: Path, image_sha256: str | None = None) -> float:
        key = image_sha256 or sha256(image)
        if key not in self.cache:
            self.cache[key] = self.embed([image])[0]
        return score_vector(self.model, self.cache[key])

    def signal(
        self, image: Path, image_sha256: str | None = None, profile: str | None = None
    ) -> dict:
        try:
            p = self.score(image, image_sha256)
        except Exception as exc:  # noqa: BLE001 - a signal never breaks a card
            return card_signal(None, None, self.mode, f"error: {str(exc)[:200]}")
        return card_signal(p, self.model, self.mode, profile=profile, overrides=self.overrides)

    def ref_signal(
        self, image: Path, image_sha256: str | None = None, bundle_ids: list[str] | tuple = ()
    ) -> dict | None:
        """`ref_distance` of the card (reuses the cached CSD vector); None without an index."""
        if self.ref_index is None:
            return None
        try:
            key = image_sha256 or sha256(image)
            if key not in self.cache:
                self.cache[key] = self.embed([image])[0]
            return ref_signal(ref_similarity(self.cache[key], self.ref_index, bundle_ids),
                              self.ref_index)  # fmt: skip
        except Exception as exc:  # noqa: BLE001 - a signal never breaks a card
            return ref_signal(None, None, f"error: {str(exc)[:200]}")


def fake_embedder(model: dict):
    """Deterministic stand-in for tests and FakeBackend runs: a unit vector from the image
    bytes' sha256 (no model weights)."""
    import hashlib

    dim = int(model["embedding"]["dim"])

    def embed(paths: list[Path]) -> list[list[float]]:
        out = []
        for p in paths:
            seed = int(hashlib.sha256(Path(p).read_bytes()).hexdigest()[:16], 16)
            v = np.random.default_rng(seed).normal(size=dim)
            out.append((v / np.linalg.norm(v)).tolist())
        return out

    return embed


def runtime_scorer(
    root: Path, embed, cfg: dict | None = None, *, force_shadow: bool = False
) -> Scorer | None:
    """None when switched off or not configured; errors in the artifact are fatal (a pinned
    model that does not match must be noticed, not silently skipped). `force_shadow` (a
    shadow-mode run, fable-review §3.4 step 2) records the zone but never acts."""
    cfg = cfg if cfg is not None else load_config(root)
    if not cfg or not cfg.get("enabled", False):
        return None
    mode = check_mode(cfg)
    if mode == "off":
        return None
    if force_shadow:
        mode = "shadow"
    return Scorer(
        load_model(root, cfg), mode, embed, overrides=check_overrides(cfg),
        ref_index=load_ref_index(root, cfg.get("ref_index", REF_INDEX)),
    )  # fmt: skip


def check_overrides(cfg: dict) -> dict:
    """`[zones_by_profile.<profile>]`: only `tau_r` / `tau_a` in [0, 1]; auto-pass stays
    forbidden like the mode (a `tau_a` override is refused)."""
    out = {}
    for pid, over in (cfg.get("zones_by_profile") or {}).items():
        unknown = set(over) - {"tau_r", "tau_a", "note"}
        if unknown:
            raise ScorerError(f"zones_by_profile.{pid}: unknown keys {sorted(unknown)}")
        if over.get("tau_a") is not None:
            raise ScorerError(
                f"zones_by_profile.{pid}: tau_a (auto-pass) stays forbidden until the "
                "shadow-mode criteria of docs/research/fable-review.md §3.4 step 2 are met"
            )
        tau = over.get("tau_r")
        if tau is not None and not 0.0 <= float(tau) <= 1.0:
            raise ScorerError(f"zones_by_profile.{pid}: tau_r {tau} outside [0, 1]")
        out[pid] = {k: float(v) for k, v in over.items() if k in ("tau_r", "tau_a")}
    return out


# ---------------------------------------------------------------- ranking (best-of-N, Q2)


@dataclass(frozen=True)
class Candidate:
    """One candidate image of a card position. `p_reject` from the style scorer (None = not
    scored); `hard_failed` = names of failed hard checks; `vetoes` = veto items that fired
    (e.g. ["kimi-veto-v2:downward"]); `vetoes_precise` = those of them that are calibrated
    step-0 auto-reject items."""

    id: str
    p_reject: float | None
    hard_failed: tuple[str, ...] = ()
    hard_unknown: tuple[str, ...] = ()
    vetoes: tuple[str, ...] = ()
    vetoes_precise: tuple[str, ...] = ()


RANK_DEFAULTS = {
    "hard_fail_penalty": 10.0,  # any failed hard check ranks below every clean candidate
    "precise_veto_penalty": 5.0,  # a step-0 veto ranks below clean, above hard failures
    "veto_penalty": 0.25,  # other (imprecise) veto items: a mild penalty per item
    "unknown_penalty": 0.1,  # unknown required check: slight penalty, never a pass
    "unscored_p": 0.5,  # no style score: neutral
}


def rank_score(c: Candidate, weights: dict | None = None) -> float:
    w = {**RANK_DEFAULTS, **(weights or {})}
    p = w["unscored_p"] if c.p_reject is None else c.p_reject
    imprecise = [v for v in c.vetoes if v not in c.vetoes_precise]
    return (
        p
        + w["hard_fail_penalty"] * len(c.hard_failed)
        + w["precise_veto_penalty"] * len(c.vetoes_precise)
        + w["veto_penalty"] * len(imprecise)
        + w["unknown_penalty"] * len(c.hard_unknown)
    )


def rank_candidates(candidates: list[Candidate], weights: dict | None = None) -> list[dict]:
    """Candidates best first (lowest rank score; ties keep input order). Each row:
    {id, rank, score, p_reject, hard_failed, vetoes, eligible}; `eligible` = no failed hard
    check and no precise veto (the candidate could be shown to a person)."""
    rows = []
    for i, c in enumerate(candidates):
        rows.append(
            {
                "id": c.id,
                "score": round(rank_score(c, weights), 6),
                "p_reject": None if c.p_reject is None else round(c.p_reject, 4),
                "hard_failed": list(c.hard_failed),
                "hard_unknown": list(c.hard_unknown),
                "vetoes": list(c.vetoes),
                "vetoes_precise": list(c.vetoes_precise),
                "eligible": not c.hard_failed and not c.vetoes_precise,
                "_i": i,
            }
        )
    rows.sort(key=lambda r: (r["score"], r["_i"]))
    for n, r in enumerate(rows, 1):
        r["rank"] = n
        del r["_i"]
    return rows


def candidate_from_eval(
    cid: str, hard_checks: dict, p_reject: float | None, vetoes: list[str], precise: list[str]
) -> Candidate:
    """Build a Candidate from an `eval.json`-like `hard_checks` dict (signal-only entries
    are ignored) and the veto items that fired."""
    failed = tuple(k for k, v in hard_checks.items() if v.get("passed") is False)
    unknown = tuple(
        k for k, v in hard_checks.items() if v.get("passed") is None and v.get("required")
    )
    return Candidate(cid, p_reject, failed, unknown, tuple(vetoes), tuple(precise))
