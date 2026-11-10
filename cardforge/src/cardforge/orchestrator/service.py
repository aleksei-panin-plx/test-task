"""Service API of the core (TA §13.4): `create_run`, `plan`, `advance`, `decide_gate`,
`status`, `export` over `RunStore`.

CLI, MCP, HTTP and Gradio only call these functions. Nothing here prints or prompts.
`advance` moves a run to its next wait (G1, G2, host agent) or to the end and is
idempotent: calling it again after a crash continues from the files on disk.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import time
import uuid
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from contextlib import nullcontext
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from PIL import Image, ImageDraw

import cardforge
from cardforge.contracts.generation import (
    CardEvaluation,
    GenerationAttempt,
    PromptBundle,
    Reference,
)
from cardforge.contracts.policy import PolicyPack, PolicyVerdict
from cardforge.contracts.request import CollectionRequest, PlanDraft, SetPlan
from cardforge.contracts.run import RunManifest
from cardforge.evaluate import (
    auto_reject,
    batch_judges,
    card_qa,
    set_qa,
    sheet_review,
    style_scorer,
    veto_judge,
)
from cardforge.evaluate.batch_judges import BatchJudge
from cardforge.evaluate.common import sha256
from cardforge.evaluate.object_mask import Masker, fake_masker, rembg_masker
from cardforge.evaluate.set_qa import check_set
from cardforge.export import g2_labels
from cardforge.export.postprocess import dehaze_params_for, write_final
from cardforge.export.report import render_report, to_html
from cardforge.export.sheets import write_framed, write_sheet
from cardforge.knowledge.backgrounds import PLATES
from cardforge.knowledge.references import background_guide
from cardforge.llm.fake import FakeLLM
from cardforge.llm.provider import HostAgentProvider, LLMProvider, NoProvider, Pending, seal
from cardforge.orchestrator import states
from cardforge.orchestrator.budget import BudgetLedger
from cardforge.orchestrator.calls import BudgetExhausted, ask
from cardforge.orchestrator.execution import (
    Limiter,
    SharedSlots,
    TransientError,
    stop_requested,
    with_retries,
)
from cardforge.orchestrator.store import RunStore, now, write_atomic_bytes
from cardforge.orchestrator.workspace import Workspace, judge_profile, render_profile
from cardforge.planner import planner
from cardforge.planner.validators import load_grammar
from cardforge.policy import actions, ip_check
from cardforge.policy.loader import resolve_active
from cardforge.policy.prompt_guard import PolicyPromptError
from cardforge.prompting import fixes
from cardforge.prompting.assemble import RenderProfile, Template, build_bundle, load_template
from cardforge.render import edit_runtime
from cardforge.render.base import RenderBackend
from cardforge.render.fake import FakeBackend

RENDER_TIMEOUT_S = 180
MAX_STEPS = 200


class ServiceError(ValueError):
    pass


def fake_ocr(path: Path) -> dict:
    return {"status": "ok", "engine": "fake", "observations": []}


def system_ocr(root: Path) -> Callable[[Path], dict]:
    from cardforge.evaluate.ocr import recognize

    return lambda path: {"engine": "unknown", **recognize([path], root)[str(path)]}


@dataclass
class RenderSlot:
    profile: RenderProfile
    backend: RenderBackend | None
    estimate_usd: float


@dataclass
class Runtime:
    """Providers for one `advance` call; rebuilt from the manifest on resume."""

    llm: LLMProvider
    judge: card_qa.JudgeProfile  # the deciding judge (may be `none`)
    profile: RenderProfile
    backend: RenderBackend | None
    ocr: Callable[[Path], dict]
    render_estimate_usd: float = 0.0
    # Judge panel: (profile, provider); the decider first when it is a real judge. Every
    # member gets the same task in parallel; only `judge` decides, the rest is logged.
    panel: list[tuple[card_qa.JudgeProfile, LLMProvider]] = field(default_factory=list)
    judge_concurrency: int = 4
    # Several render models: profile id → RenderSlot for every profile except `profile`.
    slots: dict[str, RenderSlot] = field(default_factory=dict)
    # Candidate mode: batch judges (deciding first, then logged) and the combining rule.
    batch: list[tuple[card_qa.JudgeProfile, BatchJudge]] = field(default_factory=list)
    deciders: list[str] = field(default_factory=list)
    rule: str | None = None
    sleep: Callable[[float], None] = time.sleep
    retry_base_delay_s: float = 1.0
    concurrency: int = 4
    # Machine-wide request slots per provider backend (debt T2), shared by parallel runs.
    shared_slots: dict[str, SharedSlots] = field(default_factory=dict)
    # Object mask for the `crop` hard check: rembg from the local model, a synthetic mask
    # for FakeBackend runs, or None (the check is then `not_run`, never a pass).
    mask: Masker | None = None
    mask_source: str = "none"
    # Set QA (stage 4): style embedder for set consistency (None → `not_run`, e.g. FakeBackend
    # runs) and the VLM reviewer of the 5×2 sheet (None → not asked).
    set_embedder: set_qa.Embedder | None = None
    set_embed_source: str = "none"
    sheet_llm: LLMProvider | None = None
    # Layer-1 style scorer (fable-review §3.2): signal `style_score` per card, shadow by
    # default (config/style_scorer.toml). None → no entry (FakeBackend runs) unless injected.
    style: style_scorer.Scorer | None = None
    style_note: str | None = None
    # Edit pass (config/edit_pass.toml, fable-review Q3): one image edit instead of the first
    # redraw of a card fixed only for material / style. None → off (FakeBackend runs unless
    # a test injects `fake_edit`).
    edit: edit_runtime.EditPass | None = None


OFFLINE_PROVIDERS = ("fake", "host-agent", "none")
# Per-card judges (N = 1). `veto` is shadow-only: its answer is logged, it never decides.
PER_CARD_KINDS = ("card", "veto")
VETO_PROVIDERS = ("fake", "openrouter")


def _llm(name: str, options: dict) -> LLMProvider:
    if name == "fake":
        return FakeLLM(**options)
    if name == "host-agent":
        return HostAgentProvider()
    if name == "none":
        return NoProvider()
    raise ServiceError(f"provider {name!r} has no adapter in this build (gemini/openrouter: later)")


def _judge_llm(
    judge: card_qa.JudgeProfile, opts: dict, *, decider: bool = False, root: Path | None = None
) -> LLMProvider:
    """FakeLLM knobs: config `fake = {...}`, then runtime_options `fake_judges[<id>]`, then
    `fake_judge` for the deciding judge. A veto judge on OpenRouter gets its own adapter."""
    if judge.kind == "veto" and judge.provider == "openrouter":
        if root is None:
            raise ServiceError(f"judge {judge.id!r}: the OpenRouter adapter needs the project root")
        limits = {
            k: getattr(judge, k)
            for k in ("reasoning_effort", "max_input_tokens_ceiling", "max_output_tokens")
            if getattr(judge, k) is not None
        }
        return veto_judge.OpenRouterVetoProvider(
            root,
            judge_id=judge.id,
            candidate=judge.candidate or "kimi-k3.1-pro",
            route=judge.route,
            **limits,
        )
    options = {
        "model": judge.model,
        **judge.fake,
        **opts.get("fake_judges", {}).get(judge.id, {}),
        **(opts.get("fake_judge", {}) if decider else {}),
    }
    return _llm(judge.provider, options if judge.provider == "fake" else {})


def _backend(ws: Workspace, profile: RenderProfile, opts: dict) -> RenderSlot:
    estimate = profile.cost_estimate_usd
    backend: RenderBackend | None = None  # not implemented: rendering raises a clear error
    if profile.backend == "fake":
        fake = {**opts.get("fake_backend", {}), **opts.get("fake_backends", {}).get(profile.id, {})}
        backend = FakeBackend(root=ws.root, **fake)
        estimate = fake.get("cost_per_image_usd", estimate)
    elif profile.backend == "mflux":
        from cardforge.render.mflux_backend import MfluxBackend

        backend = MfluxBackend(ws.root, profile.id)
    elif profile.backend == "codex":
        from cardforge.render.codex_image import CodexImageBackend

        backend = CodexImageBackend(
            ws.root,
            profile.codex_model,
            effort=profile.codex_effort,
            send_references=profile.send_references,
            max_refs=profile.max_refs,
        )
    elif profile.backend == "openrouter-image":
        from cardforge.render.openrouter_image import OpenRouterImageBackend

        backend = OpenRouterImageBackend(
            ws.root,
            profile.model_id,
            estimate_usd=profile.cost_estimate_usd,
            aspect_ratio=profile.aspect_ratio,
            zdr=profile.zdr,
            send_references=profile.send_references,
            endpoint=profile.endpoint,
            send_seed=profile.send_seed,
            max_refs=profile.max_refs,
            extra=profile.extra,
            allow_openai=profile.allow_openai,
            **opts.get("openrouter_image", {}),
        )
    return RenderSlot(profile, backend, estimate)


def _batch_judge(ws: Workspace, jp: card_qa.JudgeProfile, opts: dict) -> BatchJudge:
    fake = {**jp.fake, **opts.get("fake_judges", {}).get(jp.id, {})}
    if jp.provider == "fake":
        cls = batch_judges.FakeGroupJudge if jp.kind == "group" else batch_judges.FakeAbsoluteJudge
        return cls(jp.id, **fake)
    if jp.provider == "openrouter" and jp.kind == "group":
        return batch_judges.KimiG1Judge(
            ws.root,
            id=jp.id,
            ship_floor=jp.ship_floor or 4.0,
            estimate_per_item_usd=jp.estimate_per_item_usd,
        )
    if jp.provider == "host-cli" and jp.kind == "absolute":
        return batch_judges.CodexCliJudge(ws.root, id=jp.id, decision_policy=jp.decision_policy)
    raise ServiceError(f"judge {jp.id!r}: no batch adapter for provider {jp.provider!r}")


def build_runtime(ws: Workspace, manifest: RunManifest) -> Runtime:
    opts = manifest.runtime_options
    main = _backend(ws, render_profile(ws, manifest.render_profile or ""), opts)
    profile, backend, estimate = main.profile, main.backend, main.estimate_usd
    slots = {
        pid: _backend(ws, render_profile(ws, pid), opts)
        for pid in dict.fromkeys([*manifest.render_profiles, *manifest.render_fallbacks.values()])
        if pid != profile.id
    }
    judge = judge_profile(ws, manifest.judge_profile or "none")
    batch = []
    for name in [*manifest.deciders, *manifest.shadow_judges]:
        jp = judge_profile(ws, name)
        if jp.kind not in PER_CARD_KINDS and (jp.enabled_in_config or name in manifest.deciders):
            batch.append((jp, _batch_judge(ws, jp, opts)))
    card_mode = manifest.candidates_per_card == 1
    panel = (
        [(judge, _judge_llm(judge, opts, decider=True))]
        if judge.enabled and card_mode and judge.kind == "card"
        else []
    )
    for name in manifest.shadow_judges:
        shadow = judge_profile(ws, name)
        # switched off in config → skipped on the next advance
        if shadow.enabled_in_config and shadow.kind in PER_CARD_KINDS and card_mode:
            panel.append((shadow, _judge_llm(shadow, opts, root=ws.root)))
    defaults = ws.config()["defaults"]
    fake = profile.backend == "fake"
    embedder, embed_source, sheet_llm = _set_qa_runtime(ws, opts, fake)
    style, style_note = _style_runtime(ws, opts, fake, embedder)
    return Runtime(
        llm=_llm(manifest.llm_provider["name"], opts.get("fake_llm", {})),
        panel=panel,
        judge_concurrency=int(defaults.get("judge_concurrency", 4)),
        judge=judge,
        profile=profile,
        backend=backend,
        ocr=fake_ocr if profile.backend == "fake" else system_ocr(ws.root),
        render_estimate_usd=estimate,
        concurrency=int(ws.config()["defaults"].get("api_concurrency", 4)),
        shared_slots=_shared_slots(ws, opts),
        slots=slots,
        batch=batch,
        deciders=list(manifest.deciders),
        rule=manifest.decision_rule,
        mask=fake_masker(opts.get("fake_mask")) if fake else rembg_masker(ws.root),
        mask_source="fake (synthetic)" if fake else "rembg-u2net",
        set_embedder=embedder,
        set_embed_source=embed_source,
        sheet_llm=sheet_llm,
        style=style,
        style_note=style_note,
        edit=edit_runtime.runtime(ws.root, opts, fake),
    )


def _shared_slots(ws: Workspace, opts: dict) -> dict[str, SharedSlots]:
    """T2: one set of request slots per paid provider for all runs on this machine
    (`[defaults] provider_concurrency`, folder `runs/_api/slots/<backend>`)."""
    n = int(ws.config()["defaults"].get("provider_concurrency", 4))
    if opts.get("fake_backend") is not None or n <= 0:
        return {}
    return {"openrouter-image": SharedSlots(ws.root / "runs/_api/slots/openrouter", n)}


def _style_runtime(ws: Workspace, opts: dict, fake: bool, set_embedder):
    """(scorer, note). Real runs: the pinned artifact + the local CSD embedder (the set-QA one
    when its metric is CSD). FakeBackend runs: none, unless a test injects `fake_style`
    ({"model": artifact dict, "mode": ...}: a deterministic embedder of the image bytes)."""
    cfg = style_scorer.load_config(ws.root)
    if fake:
        spec = opts.get("fake_style")
        if not spec:
            return None, None
        mode = "shadow" if opts.get("shadow") else spec.get("mode", "shadow")
        return style_scorer.Scorer(spec["model"], mode, style_scorer.fake_embedder(spec["model"]),
                                   overrides=spec.get("overrides", {})), "fake"  # fmt: skip
    if not cfg or not cfg.get("enabled", False):
        return None, None
    if not (ws.root / cfg["model"]).is_file():
        return None, f"artifact missing: {cfg['model']}"
    metric = ws.config().get("set_qa", {}).get("style_metric", "csd")
    embed = set_embedder if (set_embedder is not None and metric == "csd") else (
        set_qa.local_embedder(ws.root, "csd")
    )  # fmt: skip
    return style_scorer.runtime_scorer(
        ws.root, embed, cfg, force_shadow=bool(opts.get("shadow"))
    ), None


def _set_qa_runtime(ws: Workspace, opts: dict, fake: bool):
    """(style embedder, its source, sheet reviewer). FakeBackend images carry no style, so
    fake runs skip the embedding and use the fake sheet reviewer; real runs use the local
    CPU embedder (already downloaded weights) and the configured OpenRouter reviewer."""
    cfg = ws.config().get("set_qa", {})
    sheet = cfg.get("sheet", {})
    choice = opts.get("set_sheet") or ("fake" if fake else sheet.get("provider", "none"))
    if not sheet.get("enabled", False) and not fake:
        choice = "none"
    if choice == "fake":
        reviewer = FakeLLM(sheet_outliers=opts.get("fake_sheet_outliers"))
    elif choice == "openrouter":
        reviewer = sheet_review.OpenRouterSheetProvider(
            ws.root,
            candidate=sheet.get("candidate", "kimi-k3.1-pro"),
            route=sheet.get("route"),
            reasoning_effort=sheet.get("reasoning_effort", "low"),
            max_input_tokens_ceiling=int(sheet.get("max_input_tokens_ceiling", 6000)),
            max_output_tokens=int(sheet.get("max_output_tokens", 6000)),
        )
    elif choice == "none":
        reviewer = None
    else:
        raise ServiceError(f"unknown set sheet reviewer {choice!r}; choose fake|openrouter|none")
    if fake:
        return None, "none (FakeBackend)", reviewer
    metric = cfg.get("style_metric", "csd")
    return set_qa.local_embedder(ws.root, metric), f"{metric} (local CPU)", reviewer


@dataclass
class Ctx:
    ws: Workspace
    store: RunStore
    rt: Runtime
    ledger: BudgetLedger
    request: CollectionRequest
    packs: list[PolicyPack]
    template: Template
    pending: list[Pending] = field(default_factory=list)
    cache: dict = field(default_factory=dict)

    @property
    def manifest(self) -> RunManifest:
        return self.store.manifest()

    def template_for(self, profile: RenderProfile) -> Template:
        key = f"template:{profile.template}"
        if key not in self.cache:
            self.cache[key] = load_template(self.ws.root, profile.template)
        return self.cache[key]

    def slot(self, variant_id: str) -> RenderSlot:
        pid = self.manifest.variant_profiles.get(variant_id, self.rt.profile.id)
        return self._slot_of(pid)

    def _slot_of(self, pid: str) -> RenderSlot:
        if pid == self.rt.profile.id:
            return RenderSlot(self.rt.profile, self.rt.backend, self.rt.render_estimate_usd)
        return self.rt.slots[pid]

    def card_profile(self, card_id: str) -> str:
        """The profile that renders the card now: its variant's, or the reserve (G1) once the
        card was moved there (`counters["fallback"]`)."""
        moved = self.manifest.counters.get("fallback", {}).get(card_id)
        if moved:
            return moved["profile"]
        variant = card_id.split("_")[0]
        return self.manifest.variant_profiles.get(variant, self.rt.profile.id)

    def card_slot(self, card_id: str) -> RenderSlot:
        return self._slot_of(self.card_profile(card_id))

    @property
    def fix_library(self) -> fixes.FixLibrary:
        if "fix_library" not in self.cache:
            self.cache["fix_library"] = fixes.load_library(self.ws.root)
        return self.cache["fix_library"]

    def plan_ctx(self) -> planner.PlanContext:
        return planner.PlanContext(self.ws.root, self.store, self.ledger, self.rt.llm, self.packs)


# ---------------------------------------------------------------- create / plan / advance


def _config_hash(ws: Workspace) -> str:
    h = hashlib.sha256()
    for rel in ("config/cardforge.toml", "config/runtime.toml", "config/models.toml"):
        h.update((ws.root / rel).read_bytes())
    return h.hexdigest()[:16]


def create_run(
    ws: Workspace,
    request: CollectionRequest,
    *,
    llm: str = "host-agent",
    render: str | list[str] = "fake",
    judge: str | None = None,
    deciders: list[str] | None = None,
    shadow_judges: list[str] | None = None,
    candidates: int | None = None,
    render_variant_limit: int | None = None,
    compare_models: bool = False,
    run_id: str | None = None,
    client: str = "cli",
    owner: str = "local",
    deployment: str = "local",
    runtime_options: dict | None = None,
) -> str:
    """`render`: one profile or several (each set variant gets exactly one, round-robin).
    `deciders`: batch judges for candidate mode, combined by the configured rule; without
    them the run uses one per-card `judge` (N = 1)."""
    cfg = ws.config()
    allowed = cfg["deployment"][deployment]
    renders = [render] if isinstance(render, str) else list(render)
    # `primary` = the main generators chosen by the user ([defaults] primary_render_profiles).
    primary = list(cfg["defaults"].get("primary_render_profiles", []))
    renders = list(dict.fromkeys(x for r in renders for x in (primary if r == "primary" else [r])))
    if not renders:
        raise ServiceError("choose at least one render profile")
    profiles = [render_profile(ws, r) for r in renders]
    # G1 (user 2026-11-09): a main profile's reserve (`fallback`), unless switched off for
    # this run or the run compares models (a reserve would mix models inside one variant).
    use_fallback = (runtime_options or {}).get("render_fallback", True) and not compare_models
    fallbacks = {
        p.id: p.fallback
        for p in profiles
        if use_fallback and p.fallback and p.fallback != p.id and p.fallback not in renders
    }
    reserves = [render_profile(ws, f) for f in dict.fromkeys(fallbacks.values())]
    for profile in [*profiles, *reserves]:
        if not profile.enabled:
            raise ServiceError(f"render profile {profile.id!r} is switched off in config")
        if not profile.available:
            raise ServiceError(f"render profile {profile.id!r} is not available in this build")
        if profile.backend not in allowed["backends"]:
            raise ServiceError(f"deployment {deployment!r} does not allow {profile.backend!r}")
    if llm not in allowed["llm_providers"]:
        raise ServiceError(f"deployment {deployment!r} does not allow llm_providers {llm!r}")
    test_run = all(p.test_only for p in profiles)
    if test_run and any(not r.test_only for r in reserves):
        raise ServiceError("a test render profile cannot fall back to a real generator")
    deciders = list(dict.fromkeys(deciders or []))
    rule = None
    if deciders:
        jps = [judge_profile(ws, d) for d in deciders]
        if any(j.kind == "card" for j in jps):
            raise ServiceError("candidate mode needs batch judges (kind group/absolute)")
        kinds = sorted(j.kind for j in jps)
        if len(jps) == 1:
            rule = "single"
        elif len(jps) == 2 and kinds == ["absolute", "group"]:
            rule = cfg["decision"]["rule"]
        else:
            raise ServiceError(f"no decision rule for deciders {deciders} ({kinds})")
        group_n = {j.candidates for j in jps if j.kind == "group"}
        calibrated = group_n.pop() if group_n else None
        candidates = candidates or calibrated or 4
        if calibrated and candidates != calibrated:
            raise ServiceError(
                f"the group judge is calibrated on {calibrated} candidates; {candidates} "
                "would be an uncalibrated profile"
            )
        if candidates < 2:
            raise ServiceError("candidate mode needs at least 2 candidates per position")
        judge = deciders[0]
    else:
        judge = judge or cfg["defaults"]["judge_profile"]
        candidates = 1
    names = deciders or [judge]
    if shadow_judges is None:
        shadow_judges = default_panel(
            ws,
            names,
            kinds={"card"} if candidates == 1 else {"group", "absolute"},
            test_run=test_run,
        )
    shadow_judges = list(dict.fromkeys(shadow_judges))
    for name in [*names, *shadow_judges]:
        jp = judge_profile(ws, name)
        if jp.provider not in allowed["judge_providers"]:
            raise ServiceError(f"deployment {deployment!r} does not allow judge {name!r}")
        if not jp.enabled_in_config:
            raise ServiceError(f"judge {name!r} is switched off in config/runtime.toml")
        if jp.test_only and not test_run:
            raise ServiceError(f"test judge {name!r} cannot judge a real render")
        if jp.kind == "card" and jp.provider not in OFFLINE_PROVIDERS:
            raise ServiceError(f"judge {name!r}: provider {jp.provider!r} has no adapter yet")
        if jp.kind == "veto" and name in names:
            raise ServiceError(f"veto judge {name!r} is shadow-only: it never decides")
        if jp.kind == "veto" and jp.provider not in VETO_PROVIDERS:
            raise ServiceError(f"judge {name!r}: provider {jp.provider!r} has no veto adapter")
        if jp.kind not in PER_CARD_KINDS and candidates == 1:
            raise ServiceError(f"batch judge {name!r} needs candidate mode (deciders)")
        if jp.kind in PER_CARD_KINDS and candidates > 1:
            raise ServiceError(f"per-card judge {name!r} cannot judge candidate groups")
        if jp.kind not in PER_CARD_KINDS and jp.provider not in ("fake", "openrouter", "host-cli"):
            raise ServiceError(f"judge {name!r}: provider {jp.provider!r} has no adapter yet")
    if set(names) & set(shadow_judges):
        raise ServiceError("a deciding judge cannot also be a shadow (logged) judge")
    if any(not judge_profile(ws, j).enabled for j in shadow_judges):
        raise ServiceError("`none` is not a judge to log")
    g3_marks = (runtime_options or {}).get("g3_unconfirmed")
    if not judge_profile(ws, judge).enabled and request.hitl.g2 == "none" and not g3_marks:
        raise ServiceError("judge profile `none` needs a human G2 gate (g2: all or flagged)")
    paid = [j for j in [*names, *shadow_judges] if judge_profile(ws, j).provider == "openrouter"]
    paid += [p.id for p in [*profiles, *reserves] if p.backend == "openrouter-image"]
    if paid:
        remaining = float(batch_judges.KimiG1Judge(ws.root).ledger().summary()["remaining_usd"])
        if not 0 < request.budget.max_cost_usd <= remaining:
            raise ServiceError(
                f"paid judges {paid} need a run budget > 0 and <= the OpenRouter ledger "
                f"remainder ${remaining:.2f}; got ${request.budget.max_cost_usd:.2f}"
            )
    if compare_models and len(renders) < 2:
        raise ServiceError("comparison mode needs at least two render profiles")
    if render_variant_limit is not None and not 1 <= render_variant_limit <= request.variants_count:
        raise ServiceError("render_variant_limit must be between 1 and variants_count")
    run_id = run_id or datetime.now(UTC).strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
    request = request.model_copy(update={"request_id": run_id, "render_profile": renders[0]})
    packs = resolve_active(ws.root, request.markets, request.policy_packs)
    grammar = load_grammar(ws.root)
    store = RunStore.create(
        ws.run_dir(run_id),
        gate_modes={"g1": request.hitl.g1, "g2": request.hitl.g2},
        versions={
            "code": f"cardforge {cardforge.__version__} (no git repository)",
            "prompt_template": {p.id: load_template(ws.root, p.template).version for p in profiles},
            "set_grammar": grammar.get("version"),
            "style_profile": planner.style_profile(ws.root).get("version"),
            "policies": [
                {"id": p.policy_id, "version": p.policy_version, "sha256": p.file_sha256}
                for p in packs
            ],
            "render_model": [
                {"profile": p.id, "backend": p.backend, "id": p.model_id} for p in profiles
            ],
            "judges": {
                n: {k: getattr(judge_profile(ws, n), k) for k in ("provider", "model", "profile")}
                for n in [*names, *shadow_judges]
            },
            "config_sha256": _config_hash(ws),
        },
        llm_provider={"name": llm},
        render_profile=renders[0],
        render_profiles=renders,
        render_fallbacks=fallbacks,
        fallback_after_failures=int(cfg["defaults"].get("fallback_after_failures", 2)),
        judge_profile=judge,
        deciders=deciders,
        decision_rule=rule,
        candidates_per_card=candidates,
        render_variant_limit=render_variant_limit,
        compare_models=compare_models,
        shadow_judges=shadow_judges,
        client=client,
        owner=owner,
        deployment=deployment,
        runtime_options=runtime_options or {},
    )
    store.write("request.json", request)
    store.emit("state_changed", {"from": None, "to": "created"})
    return run_id


def default_panel(
    ws: Workspace, deciders: str | list[str], *, test_run: bool, kinds: set[str] | None = None
) -> list[str]:
    """Judges that join a run as logged ones without being named: `panel = true`, enabled,
    of a kind that fits the mode; test-only judges join only FakeBackend runs."""
    deciders = [deciders] if isinstance(deciders, str) else deciders
    kinds = kinds or {"card"}
    out = []
    for name in ws.config()["judges"]:
        jp = judge_profile(ws, name)
        if name in deciders or not jp.enabled or not jp.enabled_in_config or not jp.panel:
            continue
        if jp.kind not in kinds or (jp.test_only and not test_run):
            continue
        out.append(name)
    return out


def _context(ws: Workspace, run_id: str, runtime: Runtime | None) -> Ctx:
    store = RunStore(ws.run_dir(run_id))
    if not store.exists("manifest.json"):
        raise ServiceError(f"no run {run_id} in {ws.runs_dir}")
    manifest = store.manifest()
    request = store.read_model("request.json", CollectionRequest)
    return Ctx(
        ws=ws,
        store=store,
        rt=runtime or build_runtime(ws, manifest),
        ledger=BudgetLedger(store, request.budget.max_cost_usd),
        request=request,
        packs=resolve_active(ws.root, request.markets, request.policy_packs),
        template=load_template(ws.root),
    )


def plan(ws: Workspace, run_id: str, runtime: Runtime | None = None) -> dict:
    """Advance only through planning: stops at `planned`, G1 or a host-agent wait."""
    return advance(ws, run_id, runtime, stop_before={"rendering"})


RUN_STOP = "STOP"  # per-run stop file (`stop_run`, stage 7); `runs/STOP` stops every run


def advance(
    ws: Workspace,
    run_id: str,
    runtime: Runtime | None = None,
    *,
    stop_before: set[str] | None = None,
) -> dict:
    ctx = _context(ws, run_id, runtime)
    handlers = {
        "created": _normalize,
        "normalized": _plan,
        "planning_repair": _plan,
        "planned": _gate1_open,
        "awaiting_g1": _gate1_apply,
        "rendering": _render,
        "awaiting_g2": _gate2_apply,
        "exported": _finish,
    }
    for _ in range(MAX_STEPS):
        manifest = ctx.manifest
        if manifest.state in states.RUN_TERMINAL:
            break
        if stop_requested(ws.runs_dir):
            ctx.store.emit("error", {"kind": "stopped", "detail": "kill switch"})
            break
        if ctx.store.exists(RUN_STOP):
            ctx.store.emit("error", {"kind": "stopped", "detail": "run stop (stop_run)"})
            break
        state = manifest.state
        if state == "awaiting_agent":
            state = manifest.counters.get("resume_state", "created")
            # Working again: show the step; a still-missing answer sets awaiting_agent back.
            states.set_run_state(ctx.store, state, "resumed")
        if stop_before and state in stop_before:
            break
        ctx.pending = []
        if handlers[state](ctx) == "wait":
            break
    return status(ws, run_id)


def _wait_for_agent(ctx: Ctx, resume_state: str) -> str:
    def change(m: RunManifest) -> None:
        m.counters["resume_state"] = resume_state
        m.counters["pending_tasks"] = sorted({p.task_path for p in ctx.pending})

    ctx.store.update_manifest(change)
    states.set_run_state(ctx.store, "awaiting_agent")
    return "wait"


def _clear_pending(ctx: Ctx) -> None:
    def change(m: RunManifest) -> None:
        m.counters.pop("resume_state", None)
        m.counters.pop("pending_tasks", None)

    ctx.store.update_manifest(change)


# ---------------------------------------------------------------- planning


def _normalize(ctx: Ctx) -> str:
    try:
        result = planner.normalize(ctx.plan_ctx(), ctx.request)
    except BudgetExhausted:
        return _fail(ctx, "budget_exhausted")
    if isinstance(result, Pending):
        ctx.pending.append(result)
        return _wait_for_agent(ctx, "created")
    ctx.request = result
    ctx.store.write("request.json", result)
    _clear_pending(ctx)
    states.set_run_state(ctx.store, "normalized")
    return "next"


def _plan(ctx: Ctx) -> str:
    round_no = ctx.manifest.counters.get("plan_round", 0)
    repair = None
    edits = ctx.manifest.counters.get("plan_edits", [])
    if round_no:
        previous = ctx.store.read_json(f"plan/rounds/round_{round_no - 1}.json")
        repair = {"plan": previous["raw"], "errors": previous["errors"],
                  "collection": previous.get("collection")}  # fmt: skip
        if edits:
            repair["human_edits"] = [e["text"] for e in edits]
    resume = "planning_repair" if round_no else "normalized"
    try:
        result = planner.compose(ctx.plan_ctx(), ctx.request, round_no, repair)
    except BudgetExhausted:
        return _fail(ctx, "budget_exhausted")
    if isinstance(result, Pending):
        ctx.pending.append(result)
        return _wait_for_agent(ctx, resume)
    _clear_pending(ctx)
    verdicts: list[PolicyVerdict] = result["verdicts"]
    ctx.store.write(
        f"plan/rounds/round_{round_no}.json",
        {
            "raw": result["raw"],
            "collection": result.get("collection"),
            "errors": result["errors"],
            "warnings": result["warnings"],
            "verdicts": [v.model_dump() for v in verdicts],
        },
    )
    if result["errors"]:
        # A G1 edit starts a new repair series: the limit counts rounds since the last edit.
        since = round_no - ctx.manifest.counters.get("plan_edit_base", 0)
        if since >= planner.MAX_REPAIR_ROUNDS:
            ctx.store.write(
                "plan/planning_failed.json",
                {"rounds": round_no + 1, "unmet_rules": result["errors"]},
            )
            return _fail(ctx, "planning_failed")
        ctx.store.update_manifest(lambda m: m.counters.update(plan_round=round_no + 1))
        states.set_run_state(ctx.store, "planning_repair", f"{len(result['errors'])} errors")
        return "next"
    drafts: list[PlanDraft] = result["drafts"]
    plans = planner.freeze(ctx.ws.root, ctx.ws.refs_dir, ctx.request, drafts, verdicts)
    renders = ctx.manifest.render_profiles or [ctx.rt.profile.id]
    profiles = {p.variant_id: renders[i % len(renders)] for i, p in enumerate(plans)}
    if ctx.manifest.compare_models:
        plans, verdicts, profiles = _model_clones(plans, verdicts, renders)
    ctx.store.write("plan/variants.json", plans)
    if ctx.request.plan_mode == "collection":
        from cardforge.planner.collection import layout as collection_layout

        ctx.store.write("plan/collection.json", {
            "collection_theme": ctx.request.collection_theme,
            **(result.get("collection") or {}),
            "layout": collection_layout(ctx.ws.root, ctx.request.sets_total),
        })  # fmt: skip
    ctx.store.write(
        "plan/policy_screen.json", {"verdicts": [v.model_dump() for v in verdicts], "errors": []}
    )
    ctx.store.write(
        "plan/validation.json",
        {"valid": True, "rounds": round_no + 1, "warnings": result["warnings"]},
    )
    for v in verdicts:
        if v.verdict != "pass" and v.target != "request":
            ctx.store.emit("policy_verdict", v.model_dump(), card_id=v.target)
    states.register_cards(ctx.store, {c.card_id: p.variant_id for p in plans for c in p.cards})

    def assign(m: RunManifest) -> None:
        # TA §7.3: one render profile per variant, fixed here (round-robin, or one clone of
        # the first variant per model in comparison mode).
        m.variant_profiles = profiles
        if m.compare_models:
            base = plans[0].variant_id
            m.compare_variants = [base] + [
                p.variant_id for p in plans if p.variant_id.startswith(base + "m")
            ]

    ctx.store.update_manifest(assign)
    ip_check.init_pending(ctx.store, [p.variant_id for p in plans])
    states.set_run_state(ctx.store, "planned")
    return "next"


def _model_clones(plans: list[SetPlan], verdicts: list[PolicyVerdict], renders: list[str]):
    """Comparison mode: the first variant is drawn once per model (`v01`, `v01m2`, ...),
    so the models are compared on identical specs; each clone still has one model."""
    base = plans[0]
    clones, extra = [], []
    for n, _ in enumerate(renders[1:], 2):
        vid = f"{base.variant_id}m{n}"
        cards = []
        for c in base.cards:
            cid = c.card_id.replace(base.variant_id, vid, 1)
            flags = [f.model_copy(update={"target": cid}) for f in c.policy_flags]
            cards.append(c.model_copy(update={"card_id": cid, "policy_flags": flags}))
            extra += [
                v.model_copy(update={"target": cid}) for v in verdicts if v.target == c.card_id
            ]
        clones.append(base.model_copy(update={"variant_id": vid, "cards": cards}))
    profiles = {p.variant_id: renders[0] for p in plans}
    profiles.update({c.variant_id: renders[n] for n, c in enumerate(clones, 1)})
    return plans + clones, verdicts + extra, profiles


def _fail(ctx: Ctx, reason: str) -> str:
    states.set_run_state(ctx.store, "failed", reason)
    return "wait"


def _plans(ctx: Ctx) -> list[SetPlan]:
    if ctx.cache.get("plans") is None:
        rows = ctx.store.read_json("plan/variants.json")
        ctx.cache["plans"] = [SetPlan.model_validate(x) for x in rows]
    return ctx.cache["plans"]


def _plan_verdicts(ctx: Ctx) -> list[PolicyVerdict]:
    # The frozen plan never changes after `planned`; read it once per advance.
    if ctx.cache.get("plan_verdicts") is None:
        if not ctx.store.exists("plan/policy_screen.json"):
            return []
        rows = ctx.store.read_json("plan/policy_screen.json")["verdicts"]
        ctx.cache["plan_verdicts"] = [PolicyVerdict.model_validate(v) for v in rows]
    return ctx.cache["plan_verdicts"]


# ---------------------------------------------------------------- gates


G1 = "review/g1.json"
G2 = "review/g2.json"


def _gate1_open(ctx: Ctx) -> str:
    plans = _plans(ctx)
    verdicts = _plan_verdicts(ctx)
    mode = ctx.request.hitl.g1
    flagged = {
        p.variant_id
        for p in plans
        for v in verdicts
        if v.action == actions.FLAG_G1 and v.target.startswith(p.variant_id + "_")
    }
    if mode == "none" or (mode == "flagged" and not flagged and not ctx.request.conflicts):
        ctx.store.write(
            G1,
            {
                "gate": "G1",
                "mode": mode,
                "items": [],
                "decisions": {
                    p.variant_id: {"decision": "approve", "decided_by": "auto"} for p in plans
                },
            },
        )
        return _start_rendering(ctx)
    warnings = ctx.store.read_json("plan/validation.json")["warnings"]
    items = [
        {
            "variant_id": p.variant_id,
            "title": p.title,
            "concept": p.concept,
            "set_index": p.set_index,
            "gold_slots": p.gold_slots,
            "flagged": p.variant_id in flagged,
            "cards": [
                {
                    "card_id": c.card_id,
                    "object": c.object_name,
                    "category": c.category,
                    "background": c.background.model_dump() if c.background else None,
                    "policy_flags": [f"{f.rule_id}:{f.verdict}:{f.action}" for f in c.policy_flags],
                }
                for c in p.cards
            ],
            "policy_notes": p.policy_notes,
            "warnings": [w for w in warnings if w.startswith(p.variant_id)],
        }
        for p in plans
    ]
    ctx.store.write(
        G1,
        {
            "gate": "G1",
            "mode": mode,
            "conflicts": [c.model_dump() for c in ctx.request.conflicts],
            "items": items,
            "allowed": ["approve", "reject"],
            "decisions": {},
        },
    )
    states.set_run_state(ctx.store, "awaiting_g1")
    return "wait"


def _gate1_apply(ctx: Ctx) -> str:
    gate = ctx.store.read_json(G1)
    variants = [i["variant_id"] for i in gate["items"]]
    if any(v not in gate["decisions"] for v in variants):
        return "wait"
    if ctx.manifest.runtime_options.get("plan_only"):
        return "wait"  # stage 7: no render key → plans only, never rendered
    return _start_rendering(ctx)


def _approved_at_g1(ctx: Ctx) -> set[str]:
    if not ctx.store.exists(G1):
        return set()
    return {
        v for v, d in ctx.store.read_json(G1)["decisions"].items() if d["decision"] == "approve"
    }


def _approved(ctx: Ctx) -> set[str]:
    """Variants that are rendered: approved at G1, cut to `render_variant_limit` if set."""
    approved = _approved_at_g1(ctx)
    if ctx.manifest.compare_variants:
        return approved & set(ctx.manifest.compare_variants)
    limit = ctx.manifest.render_variant_limit
    return set(sorted(approved)[:limit]) if limit else approved


def _start_rendering(ctx: Ctx) -> str:
    approved = _approved(ctx)
    if not approved:
        return _fail(ctx, "all_variants_rejected_at_g1")
    for card_id, card in ctx.manifest.cards.items():
        if card.state != "planned":
            continue
        if card.variant_id in approved:
            states.set_card_state(ctx.store, card_id, "spec_ready")
        elif card.variant_id in _approved_at_g1(ctx):
            states.set_card_state(
                ctx.store, card_id, "rejected", "not rendered: render_variant_limit (test limit)"
            )
        else:
            states.set_card_state(ctx.store, card_id, "rejected", "variant rejected at G1")
    states.set_run_state(ctx.store, "rendering")
    return "next"


def decide_gate(
    ws: Workspace,
    run_id: str,
    gate: str,
    decisions: dict[str, str],
    reviewer: str,
    notes: dict[str, str] | None = None,
    reasons: dict[str, list[str]] | None = None,
    blind: bool = False,
) -> dict:
    """Record human decisions verbatim. G1: approve|reject per variant;
    G2: accept|reject|fix: <text> per card. Never sets `ip_check` (that is G3, §9.8).

    G2 only: `reasons` adds reason codes per card (prompts/render/fixes.toml); a fix may
    then come from codes alone (`fix` without text). Every G2 decision is also saved as a
    label record (`cardforge.export.g2_labels`); `blind` says it came from the blind page."""
    if not reviewer.strip():
        raise ServiceError("a gate decision needs the reviewer's name")
    store = RunStore(ws.run_dir(run_id))
    rel = {"g1": G1, "g2": G2}.get(gate.lower())
    if rel is None or not store.exists(rel):
        raise ServiceError(f"gate {gate} is not open for {run_id}")
    data = store.read_json(rel)
    items = {i["variant_id" if gate.lower() == "g1" else "card_id"]: i for i in data["items"]}
    reasons = reasons or {}
    if reasons and gate.lower() != "g2":
        raise ServiceError("reason codes are recorded at G2 only")
    library = fixes.load_library(ws.root)
    for target, raw in decisions.items():
        if target not in items:
            raise ServiceError(f"{target} is not waiting at {gate}")
        text = raw.strip()
        kind = text.split(":", 1)[0].strip().lower()
        allowed = ("approve", "reject") if gate.lower() == "g1" else ("accept", "reject", "fix")
        if kind not in allowed:
            raise ServiceError(f"{target}: decision must be one of {allowed}")
        try:
            codes = library.check(list(reasons.get(target) or []))
        except ValueError as exc:
            raise ServiceError(f"{target}: {exc}") from None
        if (
            kind == "fix"
            and not "".join(text.split(":", 1)[1:]).strip()
            and not library.prompt_codes(codes)
        ):
            raise ServiceError(f"{target}: fix needs an instruction, `fix: <text>`")
        if kind == "accept" and gate.lower() == "g2":
            chosen = "".join(text.split(":", 1)[1:]).strip()
            if items[target].get("candidates"):
                if chosen and chosen not in items[target]["candidates"]:
                    raise ServiceError(f"{target}: no candidate {chosen}")
                if not chosen and not items[target].get("suggested_candidate"):
                    raise ServiceError(f"{target}: name the candidate, `accept: <k>`")
            elif not items[target].get("image"):
                raise ServiceError(f"{target}: there is no image to accept")
        payload = {
            "gate": gate.upper(),
            "subject": "variant" if gate.lower() == "g1" else "card",
            "decision": kind,
            "human_answer": text,
            "decided_by": "human",
            "reviewer": reviewer,
            **({"note": notes[target]} if notes and notes.get(target) else {}),
            **({"reasons": codes} if codes else {}),
            **({"blind": True} if blind else {}),
        }
        judge = items[target].get("judge_decision")
        if gate.lower() == "g2" and judge:
            payload["judge_decision"] = judge
            payload["agree_with_judge"] = (judge == "accept") == (kind == "accept")
        event = store.emit(
            "human_decision",
            payload,
            variant_id=target if gate.lower() == "g1" else items[target]["variant_id"],
            card_id=None if gate.lower() == "g1" else target,
        )
        data["decisions"][target] = {
            "decision": kind,
            "text": text,
            "decided_by": "human",
            "event_id": event.event_id,
            "reviewer": reviewer,
            **({"reasons": codes} if codes else {}),
        }
        if gate.lower() == "g2":
            item = items[target]
            fix_note = "".join(text.split(":", 1)[1:]).strip() if kind == "fix" else ""
            g2_labels.record(
                store,
                reviewer=reviewer,
                card_id=target,
                image_rel=item.get("image"),
                attempts=store.manifest().cards[target].attempts,
                decision=kind,
                reasons=codes,
                note=fix_note or (notes or {}).get(target),
                blind=blind,
                g2_round=len(data.get("history", [])),
                event_id=event.event_id,
            )
    store.write(rel, data)
    return status(ws, run_id)


def retry_failed(ws: Workspace, run_id: str, reason: str) -> dict:
    """Send cards that failed on a backend error back to rendering (a new attempt).

    For failures whose cause was fixed (an adapter bug, a refilled quota). The reason is
    journaled. An attempt that produced no image at all (the request was refused or never
    sent) does not use up one of the card's attempts: its records are kept as
    `attempt.failed-N.json` and the attempt number is reused. Otherwise the counter grows."""
    if not reason.strip():
        raise ServiceError("a retry needs a reason")
    store = RunStore(ws.run_dir(run_id))
    failed = [cid for cid, c in store.manifest().cards.items() if c.state == "failed"]
    for cid in failed:
        card = store.manifest().cards[cid]
        if card.attempts and _release_imageless_attempt(store, cid, card.variant_id, card.attempts):
            store.update_manifest(
                lambda m, c=cid: setattr(m.cards[c], "attempts", m.cards[c].attempts - 1)
            )
            store.emit(
                "error",
                {"kind": "attempt_released", "attempt_no": card.attempts,
                 "detail": "no image was produced; the attempt number is reused"},
                card_id=cid,
            )  # fmt: skip
        states.set_card_state(store, cid, "needs_fix", f"operator retry: {reason}")
    if failed and store.manifest().state not in states.RUN_TERMINAL:
        store.emit("error", {"kind": "operator_retry", "cards": failed, "reason": reason})
    return {"retried": failed, **status(ws, run_id)}


def _release_imageless_attempt(store: RunStore, cid: str, variant: str, attempt_no: int) -> bool:
    root = store.path(_attempt_dir(cid, variant, attempt_no))
    records = sorted(root.rglob("attempt.json")) if root.exists() else []
    if not records or any(r.with_name("raw.png").exists() for r in records):
        return False
    for record in records:
        n = 1
        while record.with_name(f"attempt.failed-{n}.json").exists():
            n += 1
        record.rename(record.with_name(f"attempt.failed-{n}.json"))
    return True


REOPENABLE = ("budget_exhausted", "incomplete_variants")


def reopen(ws: Workspace, run_id: str, reason: str) -> dict:
    """Continue a `partial` run whose cause is gone (more budget, fixes still pending)."""
    if not reason.strip():
        raise ServiceError("reopening a run needs a reason")
    store = RunStore(ws.run_dir(run_id))
    m = store.manifest()
    pending = [c for c, s in m.cards.items() if s.state in ("needs_fix", "spec_ready")]
    waiting_with_work = m.state == "awaiting_g2" and pending
    if not waiting_with_work and (m.state != "partial" or m.status_reason not in REOPENABLE):
        raise ServiceError(
            f"only partial runs ({', '.join(REOPENABLE)}) or a G2 wait with cards still to "
            "render can be reopened"
        )
    store.update_manifest(lambda mm: mm.counters.pop("budget_exhausted", None))
    states.set_run_state(store, "rendering", f"reopened: {reason}")
    return status(ws, run_id)


def set_budget(
    ws: Workspace,
    run_id: str,
    max_cost_usd: float,
    reason: str,
    max_attempts_per_card: int | None = None,
) -> dict:
    """Change a run's ceiling (journaled). Paid runs stay within the OpenRouter ledger."""
    if not reason.strip():
        raise ServiceError("a budget change needs a reason")
    store = RunStore(ws.run_dir(run_id))
    request = store.read_model("request.json", CollectionRequest)
    m = store.manifest()
    renders = [render_profile(ws, p) for p in (m.render_profiles or [m.render_profile])]
    paid = any(p.backend == "openrouter-image" for p in renders) or any(
        judge_profile(ws, j).provider == "openrouter" for j in [*m.deciders, *m.shadow_judges]
    )
    if paid:
        from cardforge.render.openrouter_image import openrouter_ledger

        ledger = openrouter_ledger(ws.root)[1].summary()
        spent = BudgetLedger(store, request.budget.max_cost_usd).committed()
        if max_cost_usd - spent > float(ledger["remaining_usd"]) + 1e-9:
            raise ServiceError(
                f"the run may spend ${max_cost_usd - spent:.2f} more, the OpenRouter ledger "
                f"has ${float(ledger['remaining_usd']):.2f}"
            )
    old = request.budget.max_cost_usd
    changes: dict = {"max_cost_usd": max_cost_usd}
    if max_attempts_per_card is not None:
        changes["max_attempts_per_card"] = max_attempts_per_card
    budget = request.budget.model_copy(update=changes)
    store.write("request.json", request.model_copy(update={"budget": budget}))
    store.emit("budget_warning", {"kind": "budget_changed", "from": old, "to": max_cost_usd,
                                  "max_attempts_per_card": budget.max_attempts_per_card,
                                  "reason": reason})  # fmt: skip
    store.update_manifest(lambda mm: mm.counters.pop("budget_exhausted", None))
    return status(ws, run_id)


def _gate2_apply(ctx: Ctx) -> str:
    gate = ctx.store.read_json(G2)
    pending = [i["card_id"] for i in gate["items"] if i["card_id"] not in gate["decisions"]]
    if pending:
        return "wait"
    budget = ctx.request.budget
    for item in gate["items"]:
        cid = item["card_id"]
        decision = gate["decisions"][cid]
        if decision.get("applied"):
            continue
        card = ctx.manifest.cards[cid]
        kind = decision["decision"]
        if kind == "accept":
            if card.state == "flagged":
                states.set_card_state(ctx.store, cid, "accepted", "human accept at G2")
                _mark_accepted(ctx, cid)
            if item.get("candidates"):
                chosen = "".join(decision["text"].split(":", 1)[1:]).strip()
                k = int(chosen) if chosen else item["suggested_candidate"]
                ctx.store.update_manifest(
                    lambda m, c=cid, k=k: setattr(m.cards[c], "accepted_candidate", k)
                )
        elif kind == "reject":
            states.set_card_state(ctx.store, cid, "rejected", "human reject at G2")
        else:
            text = "".join(decision["text"].split(":", 1)[1:]).strip()
            if card.attempts >= max_attempts(ctx.manifest, budget, cid):
                states.set_card_state(ctx.store, cid, "rejected", "fix requested, no attempts left")
            else:
                entry = {
                    "source": "human",
                    "codes": [c for c in decision.get("reasons", []) if c != fixes.OTHER],
                    "text": text or None,
                    "reviewer": decision.get("reviewer"),
                    "event_id": decision.get("event_id"),
                }
                _needs_fix(ctx, cid, [entry], same_seed=True)
        decision["applied"] = True
    # A requested fix is done with this image: keep it in the history so the redrawn card
    # comes back to G2 as a new item.
    for cid, decision in list(gate["decisions"].items()):
        if decision["decision"] == "fix" and decision.get("applied"):
            gate.setdefault("history", []).append({"card_id": cid, **decision})
            del gate["decisions"][cid]
    ctx.store.write(G2, gate)
    states.set_run_state(ctx.store, "rendering")
    return "next"


# ---------------------------------------------------------------- rendering


def _attempt_dir(card_id: str, variant: str, attempt_no: int) -> str:
    return f"cards/{variant}/{card_id}/attempt_{attempt_no:02d}"


def _needs_fix(ctx: Ctx, card_id: str, asked: list[dict], *, same_seed: bool) -> None:
    """Add this round's fixes to the card's history (de-duplicated, never overwritten).

    `same_seed` is only claimed when the render profile sends a seed: GPT Image and Nano
    Banana on OpenRouter do not (`send_seed = false`), so their retry is a new image."""
    card = ctx.manifest.cards[card_id]
    profile = ctx.slot(card.variant_id).profile
    seed_sent = profile.send_seed
    keep_seed = same_seed and seed_sent
    retry = "same_seed" if keep_seed else "new_seed" if seed_sent else "new_image"
    library = ctx.fix_library
    stored: list[dict] = []
    edit_cfg = ctx.rt.edit.cfg if ctx.rt.edit is not None else {}
    edit_codes = edit_runtime.eligible_codes(asked, edit_cfg) if edit_cfg else []

    def change(m: RunManifest) -> None:
        nonlocal stored
        m.cards[card_id].fix_rounds += 1
        # G1 (user 2026-11-09 «Да, так»): why this attempt failed, by source, kept apart from
        # the de-duplicated fix history (a human `crop` merges into a `hard_check:crop` entry).
        failures = m.counters.setdefault("failures", {}).setdefault(card_id, {})
        key = str(m.cards[card_id].attempts)
        failures[key] = sorted({*failures.get(key, []), *(e.get("source") or "?" for e in asked)})
        history = fixes.entries(m.counters.get("fix", {}).get(card_id))
        stored = [
            fixes.add(history, {**e, "attempt": m.cards[card_id].attempts, "codes":
                                library.check(e.get("codes") or [])})
            for e in asked
        ]  # fmt: skip
        m.counters.setdefault("fix", {})[card_id] = {
            "format": fixes.FORMAT,
            "library": library.version,
            "entries": history,
            # Kept for readers of the old format: the text the next prompt will get.
            "instruction": fixes.compose(history, library),
            "same_seed": keep_seed,
            "from_human": any(e.get("source") == "human" for e in asked),
            "retry": retry,
            "seed_sent": seed_sent,
            # Codes of this round when it asks only for material / style (edit pass).
            "edit_codes": edit_codes,
        }

    ctx.store.update_manifest(change)
    record = ctx.manifest.counters["fix"][card_id]
    for e in stored:
        ctx.store.emit(
            "fix_recorded",
            {"kind": "requested", "entry_id": e["id"], "source": e.get("source"),
             "attempt_no": e.get("attempt"), "codes": e.get("codes", []),
             "text": e.get("text"), "retry": retry, "seed_sent": seed_sent,
             "library": library.version},
            card_id=card_id,
        )  # fmt: skip
    states.set_card_state(ctx.store, card_id, "needs_fix", record["instruction"] or "regenerate")


def max_attempts(manifest: RunManifest, budget, card_id: str) -> int:
    """The run's attempt limit plus attempts a person granted the card (`regenerate`)."""
    return budget.max_attempts_per_card + manifest.counters.get("extra_attempts", {}).get(
        card_id, 0
    )


def _mark_accepted(ctx: Ctx, card_id: str) -> None:
    def change(m: RunManifest) -> None:
        card = m.cards[card_id]
        card.accepted_attempt = card.attempts
        card.first_attempt_accepted = card.attempts == 1
        m.counters.get("unconfirmed", {}).pop(card_id, None)

    ctx.store.update_manifest(change)


@dataclass
class Job:
    card_id: str
    variant_id: str
    attempt_no: int
    bundle: PromptBundle
    rel: str
    reservation: str | None
    slot: RenderSlot | None = None
    candidate: int | None = None
    limiter: Limiter | None = None
    edit: dict | None = None  # edit pass: {source, source_sha256, codes}


def _bundle_for(
    ctx: Ctx, spec, attempt_no: int, approved_flags, *, slot: RenderSlot, candidate: int | None
) -> PromptBundle:
    fix = ctx.manifest.counters.get("fix", {}).get(spec.card_id)
    fix_text = _fix_text(ctx, spec.card_id) if attempt_no > 1 else None
    if candidate is not None:
        # Candidate groups are regenerated whole: a fresh seed per candidate and attempt.
        seed = planner.card_seed(ctx.request.seed, f"{spec.card_id}.k{candidate}", attempt_no)
    elif fix and fix["same_seed"] and attempt_no > 1:
        previous = ctx.store.read_model(
            _attempt_dir(spec.card_id, spec.card_id.split("_")[0], attempt_no - 1) + "/bundle.json",
            PromptBundle,
        )
        seed = previous.params.seed  # TA §11.1: fix = same seed plus the instruction
    else:
        seed = planner.card_seed(ctx.request.seed, spec.card_id, attempt_no)

    def ref(role: str, rid: str) -> Reference:
        # Background plates: the reference card with its object removed (knowledge/backgrounds).
        path = (
            f"{PLATES}/{rid}.png" if role == "background" else str(ctx.ws.refs_dir / f"{rid}.png")
        )
        return Reference(id=rid, role=role, path=path)

    roles = spec.reference_roles
    # Priority when a model takes few images: style, background, one category, material.
    ordered = (
        [("style", r) for r in roles.get("style", [])]
        + [("background", r) for r in roles.get("background", [])]
        + [("category", r) for r in roles.get("category", [])[:1]]
        + [("material", r) for r in roles.get("material", [])]
        + [("category", r) for r in roles.get("category", [])[1:]]
    )
    refs = [ref(role, rid) for role, rid in ordered]
    if "motif_texts" not in ctx.cache:
        ctx.cache["motif_texts"] = background_guide(ctx.ws.root)["motifs"]
    caps = slot.backend.capabilities() if slot.backend else None
    return build_bundle(
        ctx.template_for(slot.profile),
        slot.profile,
        spec,
        ctx.packs,
        _plan_verdicts(ctx),
        seed=seed,
        references=refs,
        max_refs=caps.max_refs if caps else 6,
        fix_instruction=fix_text,
        waived=approved_flags,
        candidate=candidate,
        motif_texts=ctx.cache["motif_texts"],
    )


def _fix_text(ctx: Ctx, card_id: str) -> str | None:
    """Every still-relevant fix of the card, composed from its history (old runs: the one
    stored instruction, verbatim)."""
    record = ctx.manifest.counters.get("fix", {}).get(card_id)
    return fixes.compose(fixes.entries(record), ctx.fix_library)


def _effective_spec(ctx: Ctx, spec):
    """The plan's spec, with the object description a human G2 fix rewrote (phase 3,
    2026-11-04): otherwise «without the booth» fights «with a small projection booth»."""
    record = ctx.manifest.counters.get("fix", {}).get(spec.card_id)
    description = fixes.current_description(fixes.entries(record))
    if not description or description == spec.object_description:
        return spec
    return spec.model_copy(update={"object_description": description})


def _normalize_fixes(ctx: Ctx, spec, attempt_no: int) -> bool:
    """Rewrite new human G2 comments into English constraints before the prompt is built.

    Uses the run's LLM provider through `ask()` (host agent: a pending task; FakeLLM in
    tests); never OpenRouter. The original comment stays in the entry and the events.
    Returns False while an answer is pending."""
    card_id = spec.card_id
    record = ctx.manifest.counters.get("fix", {}).get(card_id)
    todo = [e for e in fixes.entries(record) if fixes.needs_normalizing(e)]
    if not todo:
        return True
    library = ctx.fix_library
    results: dict[str, dict] = {}
    for entry in todo:
        if ctx.rt.llm.name == "none":
            results[entry["id"]] = {"prompt_text": entry["text"],
                                    "normalized_by": "none (no LLM provider)"}  # fmt: skip
            continue
        task = seal(fixes.normalize_task(entry, spec, attempt_no, library))
        try:
            reply = ask(
                ctx.store,
                ctx.ledger,
                ctx.rt.llm,
                f"fixnorm.{card_id}.{entry['id']}",
                task,
                card_id=card_id,
            )
        except BudgetExhausted:
            results[entry["id"]] = {"prompt_text": entry["text"],
                                    "normalized_by": "none (budget exhausted)"}  # fmt: skip
            continue
        if isinstance(reply, Pending):
            ctx.pending.append(reply)
            continue
        try:
            text, codes = fixes.check_normalized(reply.data, library)
            results[entry["id"]] = {"prompt_text": text, "inferred_codes": codes,
                                    "normalized_by": f"{reply.provider}:{reply.model}"}  # fmt: skip
        except ValueError as exc:
            results[entry["id"]] = {"prompt_text": entry["text"],
                                    "normalized_by": f"rejected answer: {exc}"}  # fmt: skip
            continue
        try:  # phase 3: the fix may rewrite the object description it conflicts with
            description = fixes.check_description(reply.data)
        except ValueError as exc:
            results[entry["id"]]["description_rejected"] = str(exc)
            description = None
        if description:
            results[entry["id"]]["object_description"] = description
    if results:

        def change(m: RunManifest) -> None:
            rec = m.counters["fix"][card_id]
            for e in rec["entries"]:
                e.update(results.get(e["id"], {}))
            rec["instruction"] = fixes.compose(rec["entries"], library)

        ctx.store.update_manifest(change)
        for entry in todo:
            if entry["id"] in results:
                ctx.store.emit(
                    "fix_recorded",
                    {"kind": "normalized", "entry_id": entry["id"], "original": entry["text"],
                     **results[entry["id"]]},
                    card_id=card_id,
                )  # fmt: skip
    return all(e["id"] in results for e in todo)


def _waived(ctx: Ctx) -> frozenset[str]:
    """Review-level plan flags a human approved at G1 (block rules are never waivable)."""
    if not ctx.store.exists(G1):
        return frozenset()
    gate = ctx.store.read_json(G1)
    human = {v for v, d in gate["decisions"].items() if d.get("decided_by") == "human"}
    return frozenset(
        v.rule_id
        for v in _plan_verdicts(ctx)
        if v.severity == "review"
        and v.action == actions.FLAG_G1
        and v.target.split("_")[0] in human
    )


def _render_job(ctx: Ctx, job: Job, limiter: Limiter) -> GenerationAttempt | None:
    """One paid render. Returns None — no call, reservation released — when the kill switch
    is set before the job starts: a batch submitted at once must not run past `runs/STOP`
    (shadow-02 2026-11-09: 18 queued redraws ran after STOP). The card stays `rendering`, so
    `resume` renders that attempt later."""
    if stop_requested(ctx.ws.runs_dir):
        if job.reservation:
            ctx.ledger.settle(job.reservation, 0.0)
        ctx.store.emit("error", {"kind": "stopped", "detail": "kill switch before a queued render",
                                 "attempt_no": job.attempt_no},
                       card_id=job.card_id, variant_id=job.variant_id)  # fmt: skip
        return None
    backend = job.slot.backend
    started_at, t0 = now(), time.monotonic()

    def on_retry(n: int, exc: TransientError) -> None:
        ctx.store.emit(
            "error",
            {"kind": "transient", "retry": n, "detail": str(exc), "status": exc.status},
            card_id=job.card_id,
            variant_id=job.variant_id,
        )

    unbilled = False
    with (job.limiter or limiter).slot():
        try:
            if job.edit is not None:
                source_png = ctx.store.path(job.edit["source"]).read_bytes()
                call = lambda: ctx.rt.edit.render(job.bundle, source_png, RENDER_TIMEOUT_S)  # noqa: E731
            else:
                call = lambda: backend.render(job.bundle, RENDER_TIMEOUT_S)  # noqa: E731
            result = with_retries(
                call,
                sleep=ctx.rt.sleep,
                base_delay_s=ctx.rt.retry_base_delay_s,
                on_retry=on_retry,
            )
            status, error = result.status, result.error
        except TransientError as exc:
            result, status, error = None, "timeout" if exc.timeout else "error", str(exc)
            # T2: a 429 / 5xx answer generated nothing; only an unknown outcome keeps the estimate.
            unbilled = exc.answered
        except Exception as exc:  # a backend bug must not kill the other renders
            result, status, error = None, "error", f"{type(exc).__name__}: {exc}"
            unbilled = False
    duration = round(time.monotonic() - t0, 3)
    raw_rel = f"{job.rel}/raw.png"
    if result is not None and result.image_png:
        write_atomic_bytes(ctx.store.path(raw_rel), result.image_png)
    if result is not None:
        cost = result.cost_usd
    else:
        cost = 0.0 if unbilled else job.slot.estimate_usd
    if job.reservation:
        ctx.ledger.settle(job.reservation, cost, duration)
    attempt = GenerationAttempt(
        attempt_no=job.attempt_no,
        card_id=job.card_id,
        bundle_hash=job.bundle.hash,
        backend=job.bundle.backend,
        model_id=job.bundle.model_id,
        model_revision=result.model_revision if result else "",
        started_at=started_at,
        finished_at=now(),
        duration_s=duration,
        peak_memory_gb=result.peak_memory_gb if result else None,
        cost_usd=cost,
        provider_request_id=result.provider_request_id if result else None,
        status=status,
        raw_path=raw_rel,
        error=error,
        fix_instruction=_fix_text(ctx, job.card_id) if job.attempt_no > 1 else None,
    )
    ctx.store.write(f"{job.rel}/attempt.json", attempt)
    if job.edit is not None:
        ctx.store.emit(
            edit_runtime.EVENT,
            {"attempt_no": job.attempt_no, "from_attempt": job.edit["from_attempt"],
             "codes": job.edit["codes"], "status": status, "cost_usd": cost, "error": error,
             "bundle_hash": job.bundle.hash, "source": job.edit["source"],
             "source_sha256": job.edit["source_sha256"],
             "raw_sha256": sha256(ctx.store.path(raw_rel)) if result and result.image_png else None,
             "model_id": job.bundle.model_id, "model_revision": attempt.model_revision,
             "instruction": job.bundle.template_version,
             "references": [r.id for r in job.bundle.references],
             "config": ctx.rt.edit.cfg.get("version")},
            card_id=job.card_id,
            variant_id=job.variant_id,
        )  # fmt: skip
    ctx.store.emit(
        "render_finished",
        {
            "attempt_no": job.attempt_no,
            "bundle_hash": job.bundle.hash,
            "status": status,
            "duration_s": duration,
            "cost_usd": cost,
            "error": error,
        },
        card_id=job.card_id,
        variant_id=job.variant_id,
    )
    return attempt


def _reusable(ctx: Ctx, rel: str, bundle: PromptBundle) -> GenerationAttempt | None:
    """TA §7.3: an `ok` attempt with the same bundle hash is reused, not regenerated."""
    if not ctx.store.exists(f"{rel}/attempt.json"):
        return None
    attempt = ctx.store.read_model(f"{rel}/attempt.json", GenerationAttempt)
    if (
        attempt.status == "ok"
        and attempt.bundle_hash == bundle.hash
        and ctx.store.exists(attempt.raw_path)
    ):
        return attempt
    return None


def _candidate_rel(rel: str, candidate: int | None) -> str:
    return rel if candidate is None else f"{rel}/cand_{candidate}"


def _edit_plan(ctx: Ctx, cid: str, variant: str, attempt_no: int, slot: RenderSlot):
    """One edit of the previous image instead of a redraw (config/edit_pass.toml): the fix
    round asked only for material / style, the card's profile is listed, and the card had no
    edit yet. The choice is stored in `counters["edit"]`, so a resumed attempt keeps it and a
    card rejected after its edit gets the ordinary redraw."""
    done = ctx.manifest.counters.get("edit", {}).get(cid)
    if done is not None:
        return done if done["attempt_no"] == attempt_no else None
    ep = ctx.rt.edit
    record = ctx.manifest.counters.get("fix", {}).get(cid) or {}
    if ep is None or attempt_no < 2 or not record.get("edit_codes"):
        return None
    if slot.profile.id not in ep.profiles or int(ep.cfg.get("max_per_card", 1)) < 1:
        return None
    prev = _attempt_dir(cid, variant, attempt_no - 1)
    if not ctx.store.exists(f"{prev}/raw.png") or not ctx.store.exists(f"{prev}/bundle.json"):
        return None
    refs = [
        r
        for r in ctx.store.read_model(f"{prev}/bundle.json", PromptBundle).references
        if r.role in edit_runtime.EDIT_ROLES
    ]
    plan = {
        "attempt_no": attempt_no,
        "from_attempt": attempt_no - 1,
        "source": f"{prev}/raw.png",
        "source_sha256": sha256(ctx.store.path(f"{prev}/raw.png")),
        "codes": list(record["edit_codes"]),
        "references": [r.model_dump() for r in refs],
    }
    ctx.store.update_manifest(lambda m: m.counters.setdefault("edit", {}).update({cid: plan}))
    return plan


def _render(ctx: Ctx) -> str:
    approved = _approved(ctx)
    specs = {c.card_id: c for p in _plans(ctx) if p.variant_id in approved for c in p.cards}
    for variant in approved:
        slot = ctx.slot(variant)
        if slot.backend is None:
            raise ServiceError(
                f"render backend {slot.profile.backend!r} is not implemented in this build"
            )
    budget = ctx.request.budget
    n = ctx.manifest.candidates_per_card
    candidates = [None] if n == 1 else list(range(1, n + 1))
    limiters: dict[str, Limiter] = {}
    waived = _waived(ctx)
    for _ in range(MAX_STEPS):
        if stop_requested(ctx.ws.runs_dir) or ctx.store.exists(RUN_STOP):
            ctx.store.emit("error", {"kind": "stopped", "detail": "stop during rendering"})
            return "wait"
        progressed = False
        # 1. cards whose evaluation is due (fresh renders, resumed or waiting for the agent)
        if n == 1:
            for cid, card in sorted(ctx.manifest.cards.items()):
                if card.state == "evaluating" and cid in specs:
                    progressed |= _evaluate(ctx, _effective_spec(ctx, specs[cid]), card.attempts)
        else:
            progressed |= _evaluate_round(ctx, specs)
        # 2. cards to render
        jobs: list[Job] = []
        exhausted = ctx.manifest.counters.get("budget_exhausted", False)
        for cid, card in sorted(ctx.manifest.cards.items()):
            if cid not in specs or card.state not in ("spec_ready", "needs_fix", "rendering"):
                continue
            resumed = card.state == "rendering"
            attempt_no = card.attempts if resumed else card.attempts + 1
            if not resumed and attempt_no > max_attempts(ctx.manifest, budget, cid):
                states.set_card_state(ctx.store, cid, "rejected", "max_attempts_per_card")
                progressed = True
                continue
            if exhausted and not resumed:
                continue
            if not resumed and n == 1:
                _maybe_fallback(ctx, cid, card.variant_id, attempt_no)
            spec, slot = specs[cid], ctx.card_slot(cid)
            if attempt_no > 1 and not _normalize_fixes(ctx, spec, attempt_no):
                continue  # waits for the host agent's rewrite of a G2 comment
            spec = _effective_spec(ctx, spec)
            limiter = limiters.setdefault(
                slot.profile.id,
                Limiter(
                    local=slot.backend.capabilities().local,
                    concurrency=ctx.rt.concurrency,
                    shared=ctx.rt.shared_slots.get(slot.profile.backend),
                ),
            )
            rel = _attempt_dir(cid, card.variant_id, attempt_no)
            todo: list[tuple[int | None, PromptBundle]] = []
            edit = _edit_plan(ctx, cid, card.variant_id, attempt_no, slot) if n == 1 else None
            try:
                if edit is not None:
                    bundle = ctx.rt.edit.bundle(
                        edit["references"],
                        planner.card_seed(ctx.request.seed, cid, attempt_no),
                        edit["source_sha256"],
                    )
                    ctx.store.write(f"{rel}/bundle.json", bundle)
                    if not _reusable(ctx, rel, bundle):
                        todo.append((None, bundle))
                for k in candidates if edit is None else []:
                    bundle = _bundle_for(ctx, spec, attempt_no, waived, slot=slot, candidate=k)
                    crel = _candidate_rel(rel, k)
                    ctx.store.write(f"{crel}/bundle.json", bundle)
                    if not _reusable(ctx, crel, bundle):
                        todo.append((k, bundle))
            except PolicyPromptError as exc:
                ctx.store.emit("error", {"kind": "prompt_guard", "detail": str(exc)}, card_id=cid)
                states.set_card_state(ctx.store, cid, "rejected", f"prompt_guard: {exc}")
                progressed = True
                continue
            if not todo:  # every image of this attempt already exists: reuse, no new call
                if not resumed:
                    _start_attempt(ctx, cid, attempt_no, None, emit=False)
                _after_attempt(ctx, spec, rel, candidates)
                progressed = True
                continue
            reservations = []
            estimate = ctx.rt.edit.estimate_usd if edit is not None else slot.estimate_usd
            for _k, _bundle in todo:
                rid = ctx.ledger.reserve(estimate, "render", cid, variant_id=card.variant_id)
                if rid is None:
                    break
                reservations.append(rid)
            if len(reservations) < len(todo):
                for rid in reservations:  # a group is rendered whole or not at all
                    ctx.ledger.settle(rid, 0.0)
                ctx.store.update_manifest(lambda m: m.counters.update(budget_exhausted=True))
                exhausted = True
                continue
            _start_attempt(ctx, cid, attempt_no, None, emit=False, resumed=resumed)
            for (k, bundle), rid in zip(todo, reservations, strict=True):
                _emit_render_started(ctx, cid, attempt_no, bundle, k, resumed)
                jobs.append(
                    Job(
                        cid,
                        card.variant_id,
                        attempt_no,
                        bundle,
                        _candidate_rel(rel, k),
                        rid,
                        slot,
                        k,
                        limiter,
                        edit,
                    )
                )
        if jobs:
            progressed = True
            if _run_jobs(ctx, jobs, specs, candidates):
                return "wait"  # kill switch hit inside the batch: nothing more this call
        if ctx.pending or not progressed:
            break
    if ctx.pending:
        return _wait_for_agent(ctx, "rendering")
    _clear_pending(ctx)
    return _after_rendering(ctx, specs)


# G1 «failure» sources (user 2026-11-09 «Да, так»): a person (G2), a judge, or the automaton's
# auto-reject (style scorer zone, step-0 veto). Hard checks (crop, safe_zone, fill, text, …),
# policy and fix-check rounds are neutral: they neither count nor break a run of failures.
FALLBACK_FAILURE_SOURCES = frozenset({"human", "judge", "auto_reject"})


def fallback_failure(sources: list[str] | None) -> bool | None:
    """True: the attempt failed for G1; None: neutral (only hard checks / policy / fix checks)."""
    if not sources:
        return None
    return True if FALLBACK_FAILURE_SOURCES & set(sources) else None


def _maybe_fallback(ctx: Ctx, cid: str, variant: str, attempt_no: int) -> None:
    """G1 (user 2026-11-09): the card's main profile failed `fallback_after_failures`
    attempts in a row → the next attempt and the rest go to the profile's reserve. A failure
    is a reject by a person, a judge or the style scorer / step-0 auto-reject («Да, так»);
    an attempt sent back only by hard checks (geometry, text), policy or fix checks is
    neutral and skipped. Journaled once (`render_fallback`) and stored in
    `counters["fallback"]`, so resume keeps it."""
    m = ctx.manifest
    if cid in m.counters.get("fallback", {}):
        return
    main = m.variant_profiles.get(variant, ctx.rt.profile.id)
    reserve = m.render_fallbacks.get(main)
    after = m.fallback_after_failures
    if not reserve or attempt_no - 1 < after:
        return
    sources = m.counters.get("failures", {}).get(cid, {})
    failed = []
    for n in range(attempt_no - 1, 0, -1):
        rel = _attempt_dir(cid, variant, n)
        record = ctx.store.read_json(f"{rel}/attempt.json") if ctx.store.exists(
            f"{rel}/attempt.json") else None  # fmt: skip
        if record is None or record.get("model_id") != ctx._slot_of(main).profile.model_id:
            break  # an attempt of another model (or none) breaks the run of failures
        if fallback_failure(sources.get(str(n))) is None:
            continue  # hard checks only: neutral
        evaluation = ctx.store.read_json(f"{rel}/eval.json") if ctx.store.exists(
            f"{rel}/eval.json") else {}  # fmt: skip
        failed.append({"attempt_no": n, "status": record.get("status"),
                       "decision": evaluation.get("decision"), "sources": sources.get(str(n)),
                       "reasons": evaluation.get("reasons", [])})  # fmt: skip
        if len(failed) == after:
            break
    if len(failed) < after:
        return
    entry = {"profile": reserve, "from_profile": main, "at_attempt": attempt_no,
             "after": sorted(failed, key=lambda f: f["attempt_no"])}  # fmt: skip
    ctx.store.update_manifest(
        lambda mm: mm.counters.setdefault("fallback", {}).update({cid: entry})
    )
    ctx.store.emit("render_fallback", entry, card_id=cid, variant_id=variant)


def _start_attempt(
    ctx: Ctx, cid: str, attempt_no: int, bundle: PromptBundle | None, *, emit: bool, resumed=False
) -> None:
    if not resumed:
        ctx.store.update_manifest(lambda m: setattr(m.cards[cid], "attempts", attempt_no))
        states.set_card_state(ctx.store, cid, "rendering", f"attempt {attempt_no}")
    if emit and bundle is not None:
        _emit_render_started(ctx, cid, attempt_no, bundle, None, resumed)


def _emit_render_started(ctx, cid, attempt_no, bundle, candidate, resumed) -> None:
    ctx.store.emit(
        "render_started",
        {
            "attempt_no": attempt_no,
            "bundle_hash": bundle.hash,
            "resumed": resumed,
            **({"candidate": candidate} if candidate is not None else {}),
        },
        card_id=cid,
        variant_id=cid.split("_")[0],
    )


def _run_jobs(ctx: Ctx, jobs: list[Job], specs, candidates) -> bool:
    """Run a batch of renders; True when the kill switch stopped some of them."""
    stopped = False
    workers = max(j.limiter.concurrency for j in jobs)
    pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="render")
    try:
        futures: dict[Future, Job] = {pool.submit(_render_job, ctx, j, j.limiter): j for j in jobs}
        for future in as_completed(futures):
            job = futures[future]
            attempt = future.result()  # SimulatedCrash and other BaseExceptions propagate
            if attempt is None:
                stopped = True
                continue  # stopped before the call: resume renders it
            spec = _effective_spec(ctx, specs[job.card_id])
            if job.candidate is not None:
                if attempt.status == "ok":
                    _postprocess(ctx, spec, job.rel)
                rel = _attempt_dir(job.card_id, job.variant_id, job.attempt_no)
                _after_attempt(ctx, spec, rel, candidates)
            elif attempt.status == "ok":
                _after_render(ctx, spec, job.rel)
            elif attempt.status == "blocked_by_provider":
                # The prompt is never weakened automatically (TA §15.1).
                states.set_card_state(ctx.store, job.card_id, "flagged", "blocked_by_provider")
            else:
                states.set_card_state(ctx.store, job.card_id, "failed", attempt.error)
    except BaseException:
        pool.shutdown(wait=True, cancel_futures=True)
        raise
    pool.shutdown(wait=True)
    return stopped


def _after_attempt(ctx: Ctx, spec, rel: str, candidates: list[int | None]) -> None:
    """Card mode: QA now. Candidate mode: once every candidate has its attempt record,
    the card waits (`evaluating`) for the next judging round."""
    if candidates == [None]:
        _after_render(ctx, spec, rel)
        return
    records = []
    for k in candidates:
        crel = _candidate_rel(rel, k)
        if not ctx.store.exists(f"{crel}/attempt.json"):
            return  # other candidates still rendering
        record = ctx.store.read_model(f"{crel}/attempt.json", GenerationAttempt)
        if record.status == "ok" and not ctx.store.exists(f"{crel}/final.png"):
            _postprocess(ctx, spec, crel)
        records.append(record.status)
    if ctx.manifest.cards[spec.card_id].state != "rendering":
        return
    if any(r in ("error", "timeout") for r in records):
        states.set_card_state(ctx.store, spec.card_id, "failed", f"candidate render {records}")
    elif "ok" not in records:
        states.set_card_state(ctx.store, spec.card_id, "flagged", "blocked_by_provider")
    else:
        states.set_card_state(ctx.store, spec.card_id, "evaluating", "waiting for judges")


def _postprocess(ctx: Ctx, spec, rel: str) -> None:
    # «Снять фильтр» is off unless config/postprocess.toml lists this render profile (E1).
    profile_id = ctx.card_profile(spec.card_id)
    info = write_final(
        ctx.store.path(f"{rel}/raw.png"),
        ctx.store.path(f"{rel}/final.png"),
        spec.safe_zone,
        dehaze_params_for(ctx.ws.root, profile_id),
    )
    ctx.store.write(f"{rel}/postprocess.json", info)
    with Image.open(ctx.store.path(f"{rel}/final.png")) as im:
        thumb = im.resize((215, 240), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    thumb.save(buf, format="PNG")
    write_atomic_bytes(ctx.store.path(f"{rel}/thumb.png"), buf.getvalue())


def _after_render(ctx: Ctx, spec, rel: str) -> None:
    _postprocess(ctx, spec, rel)
    states.set_card_state(ctx.store, spec.card_id, "evaluating")
    _evaluate(ctx, spec, ctx.manifest.cards[spec.card_id].attempts)


def _evaluate(ctx: Ctx, spec, attempt_no: int) -> bool:
    """Run card QA for the current attempt. Returns False while waiting for the agent."""
    card_id = spec.card_id
    rel = _attempt_dir(card_id, card_id.split("_")[0], attempt_no)
    final = ctx.store.path(f"{rel}/final.png")
    hitl = ctx.request.hitl
    ocr = ctx.rt.ocr(final)
    hard = card_qa.hard_checks(final, spec, ocr, _mask(ctx, final), ctx.rt.mask_source)
    image_sha = sha256(final)
    if ctx.rt.style is not None:  # signal only unless mode auto_reject (config/style_scorer.toml)
        profile = ctx.card_profile(card_id)
        hard.checks[style_scorer.SIGNAL] = ctx.rt.style.signal(final, image_sha, profile)
        ref = ctx.rt.style.ref_signal(final, image_sha, _bundle_ref_ids(ctx.store.path(rel)))
        if ref is not None:  # J7: CSD closeness to the customer references, signal only
            hard.checks[style_scorer.REF_SIGNAL] = ref
    elif ctx.rt.style_note:
        hard.checks[style_scorer.SIGNAL] = style_scorer.card_signal(
            None, None, "shadow", ctx.rt.style_note
        )
    verdicts: list[PolicyVerdict] = []
    judge = None
    shadows: dict = {}
    checked: dict[str, dict] = {}
    if not hard.failed:
        rules = card_qa.applicable_inspector_rules(ctx.packs, _plan_verdicts(ctx), spec)
        # Phase 1: every fix code of an earlier attempt gets a targeted question.
        history = fixes.entries(ctx.manifest.counters.get("fix", {}).get(card_id))
        asked = fixes.verify_codes(history, attempt_no, ctx.fix_library)
        questions = {c: ctx.fix_library.question(c) or "" for c in asked}
        answers: dict = {}
        if rules or questions:
            reply = ask(
                ctx.store,
                ctx.ledger,
                ctx.rt.llm,
                f"inspect.{card_id}.a{attempt_no:02d}",
                seal(card_qa.inspect_task(spec, f"{rel}/final.png", rules, questions, attempt_no)),
                card_id=card_id,
            )
            if isinstance(reply, Pending):
                ctx.pending.append(reply)
                return False
            answers = reply.data.get("answers", {})
        verdicts = card_qa.policy_verdicts(spec, rules, answers, ocr, ctx.packs, hitl)
        checked = card_qa.fix_checks(answers, asked)
        unresolved = any(r["resolved"] is False for r in checked.values())
        if not unresolved and not any(
            v.action in (actions.FIX, actions.REGENERATE) for v in verdicts
        ):
            judge, shadows, waiting = _ask_judges(
                ctx, spec, attempt_no, rel, final, hard.checks.get(style_scorer.SIGNAL)
            )
            if waiting is not None:
                ctx.pending.append(waiting)
                return False
    # Journal the checks only once the evaluation is complete, not on every agent poll.
    for name, check in hard.checks.items():
        ctx.store.emit(
            "check_result",
            {"check": name, "passed": check["passed"], "attempt_no": attempt_no},
            card_id=card_id,
        )
    for code, result in checked.items():
        ctx.store.emit(
            "check_result",
            {"check": f"fix:{code}", "passed": result["resolved"], "attempt_no": attempt_no,
             "answer": result["answer"]},
            card_id=card_id,
        )  # fmt: skip
    for v in verdicts:
        if v.verdict != "pass":
            ctx.store.emit("policy_verdict", v.model_dump(), card_id=card_id)
    acted, would = _auto_reject(
        ctx, card_id, attempt_no, f"{rel}/final.png", image_sha, hard, shadows
    )
    core_ok = (
        not hard.failed
        and not hard.flagged  # crop / safe_zone flags send the card to the person (2026-11-09)
        and not any(r["resolved"] is False for r in checked.values())
        and not any(v.action in (actions.FIX, actions.REGENERATE) for v in verdicts)
        and not would
    )
    zone = _shadow_auto_pass(ctx, card_id, attempt_no, image_sha, hard, core_ok)
    constraints = {r.id: r.prompt_constraint for p in ctx.packs for r in p.rules}
    evaluation = card_qa.decide(
        hard, verdicts, judge, ctx.rt.judge, constraints, checked, auto_rejects=acted
    )
    evaluation.shadow_judges = shadows
    _real_auto_pass(ctx, card_id, attempt_no, image_sha, hard, zone, evaluation)
    ctx.store.write(f"{rel}/eval.json", evaluation)
    _journal_hard_check_redraw(ctx, card_id, attempt_no, rel, evaluation)
    _apply_decision(ctx, card_id, evaluation)
    return True


def hard_check_reasons(reasons: list[str]) -> list[str]:
    """The hard checks that acted (`hard_check:<name>`); `:unknown` reasons are flags."""
    return sorted(
        {r.split(":", 1)[1] for r in reasons
         if r.startswith("hard_check:") and not r.endswith(":unknown")}
    )  # fmt: skip


def _journal_hard_check_redraw(ctx: Ctx, card_id, attempt_no, rel, evaluation) -> None:
    """G7 / §4.4 (user 2026-11-09): what a redraw caused by a hard check (`crop`,
    `safe_zone`, …) gave — the checks of the previous attempt, the automatic outcome of this
    one, the checks still failing, the redraw's cost and both images. The person's verdict on
    the redraw is joined later (`hard_check_redraws`, written on export)."""
    if attempt_no < 2:
        return
    variant = card_id.split("_")[0]
    prev = _attempt_dir(card_id, variant, attempt_no - 1)
    if not ctx.store.exists(f"{prev}/eval.json"):
        return
    before = hard_check_reasons(ctx.store.read_json(f"{prev}/eval.json").get("reasons", []))
    if not before:
        return
    now_failing = hard_check_reasons(evaluation.reasons)
    record = ctx.store.read_json(f"{rel}/attempt.json") if ctx.store.exists(
        f"{rel}/attempt.json") else {}  # fmt: skip
    ctx.store.emit(
        "hard_check_redraw",
        {"attempt_no": attempt_no, "from_attempt": attempt_no - 1, "checks": before,
         "decision": evaluation.decision, "still_failing": [c for c in before if c in now_failing],
         "new_failing": [c for c in now_failing if c not in before],
         "cost_usd": record.get("cost_usd"), "model_id": record.get("model_id"),
         "before": f"{prev}/final.png", "after": f"{rel}/final.png"},
        card_id=card_id,
        variant_id=variant,
    )  # fmt: skip


def hard_check_redraws(store: RunStore) -> dict:
    """Cost of the hard checks (G7): every `hard_check_redraw` event joined with the card's
    end — its state, the attempt that was accepted, and the person's G2 decision on that
    attempt when there is one. Per check: redraws, cost, automatic outcome, end states.
    «Перерисовка хуже исходной» needs a person comparing `before`/`after`; the pairs are listed."""
    manifest = store.manifest()
    gate = store.read_json(G2) if store.exists(G2) else {}
    human: dict[str, list] = {}
    for d in [*gate.get("history", []), *({"card_id": c, **v} for c, v in
              gate.get("decisions", {}).items())]:  # fmt: skip
        human.setdefault(d["card_id"], []).append(d.get("decision"))
    rows = []
    for e in store.events():
        if e.type != "hard_check_redraw":
            continue
        card = manifest.cards.get(e.card_id or "")
        last = card is not None and card.attempts == e.payload["attempt_no"]
        rows.append({
            "card_id": e.card_id, **e.payload,
            "card_state": card.state if card else None,
            "redraw_accepted": bool(card and card.accepted_attempt == e.payload["attempt_no"]),
            "is_last_attempt": last,
            "human": human.get(e.card_id or "", []) if last else [],
        })  # fmt: skip
    per_check: dict[str, dict] = {}
    for r in rows:
        for check in r["checks"]:
            c = per_check.setdefault(check, {"redraws": 0, "cost_usd": 0.0, "decision": {},
                                             "still_failing": 0, "card_state": {}})  # fmt: skip
            c["redraws"] += 1
            c["cost_usd"] = round(c["cost_usd"] + float(r.get("cost_usd") or 0.0), 4)
            c["decision"][r["decision"]] = c["decision"].get(r["decision"], 0) + 1
            c["still_failing"] += check in r["still_failing"]
            c["card_state"][r["card_state"]] = c["card_state"].get(r["card_state"], 0) + 1
    return {"format": "cardforge-hard-check-redraws-v1", "per_check": per_check, "rows": rows}


def _auto_reject(ctx: Ctx, card_id, attempt_no, image_rel, image_sha, hard, shadows):
    """Step 0 of auto-reject (config/auto_reject.toml) and the score zone of the style scorer
    in mode `auto_reject`. Every firing is journaled (`check_result`, check `auto_reject`);
    returns (the firings that change the decision, what the automaton would reject on:
    admitted firings and the style auto-reject zone, acting or not)."""
    if "auto_reject_cfg" not in ctx.cache:
        ctx.cache["auto_reject_cfg"] = auto_reject.load_config(ctx.ws.root)
    cfg = ctx.cache["auto_reject_cfg"]
    profiles = {p.id: p.profile for p, _ in ctx.rt.panel}
    firings = auto_reject.veto_firings(shadows, profiles, cfg)
    is_active = auto_reject.active(ctx.ws.root, cfg)
    # Shadow-mode run (fable-review §3.4 step 2, `--shadow`): every firing is journaled with
    # what the automaton would do, nothing changes the decision; every card goes to a person.
    shadow_run = bool(ctx.manifest.runtime_options.get("shadow"))
    would = auto_reject.acting(firings, is_active)
    acted = [] if shadow_run else would
    style = hard.checks.get(style_scorer.SIGNAL, {})
    if style.get("zone") == "auto_reject":
        row = {"source": style_scorer.SIGNAL, "item": "zone", "code": "style", "admitted": True}
        if style.get("acts") or shadow_run:
            firings.append(row)
        if shadow_run:
            would.append(row)
        elif style.get("acts"):
            acted.append(row)
    if firings:
        payload = auto_reject.event_payload(
            firings, acted, attempt_no=attempt_no, image=image_rel, image_sha256=image_sha,
            cfg=cfg or {}, is_active=is_active,
        )  # fmt: skip
        if shadow_run:
            payload["shadow_run"] = True
            payload["would_veto"] = [f["source"] for f in would]
        ctx.store.emit(auto_reject.EVENT, payload, card_id=card_id)
    would_reject = [f for f in firings if f.get("admitted")]
    return acted, would_reject


def _shadow_auto_pass(ctx: Ctx, card_id, attempt_no, image_sha, hard, core_ok: bool) -> None:
    """§4.4 (user 2026-11-09): the auto-pass zone is journaled next to auto-reject, never
    acts; the decision stays with the person. A card is in the zone when its profile has
    `[shadow_auto_pass.<profile>]` in config/style_scorer.toml, p ≤ tau_a, the runtime model
    is the pinned `model_sha256` (when set) and the core is ok (hard checks, policy, fix
    checks, no auto-reject firing). `stub = true` marks a temporary threshold, `basis` its
    label target (the final one comes from the retrained scorer, J2). Written to
    `style_score.shadow_auto_pass` in eval.json and as an `auto_pass_shadow` event."""
    style = hard.checks.get(style_scorer.SIGNAL)
    if not style or not isinstance(style.get("value"), int | float):
        return
    if "shadow_auto_pass_cfg" not in ctx.cache:
        ctx.cache["shadow_auto_pass_cfg"] = shadow_auto_pass_config(ctx.ws.root)
    entry = ctx.cache["shadow_auto_pass_cfg"].get(style.get("profile") or "")
    if not entry:
        return
    p = float(style["value"])
    pinned = entry.get("model_sha256")
    model_ok = pinned is None or style.get("model_sha256") == pinned
    in_zone = model_ok and p <= entry["tau_a"] and core_ok
    record = {"tau_a": entry["tau_a"], "stub": entry["stub"], "basis": entry.get("basis"),
              "p": p, "core_ok": core_ok, "model_ok": model_ok, "in_zone": in_zone,
              "would": "pass" if in_zone else None, "acts": False}  # fmt: skip
    style["shadow_auto_pass"] = record
    if in_zone:
        ctx.store.emit(
            "auto_pass_shadow",
            {"attempt_no": attempt_no, "profile": style.get("profile"), "image_sha256": image_sha,
             "model_sha256": style.get("model_sha256"), **record},
            card_id=card_id,
        )  # fmt: skip
    return record


# ---------------------------------------------------------------- real auto-pass (2026-11-09)
# User «Да, все три» (AGENTS.md): GPT auto-pass leaves shadow mode, variant (a′). Config:
# `[auto_pass]` of config/style_scorer.toml. Only reasons that leave the card «for a person
# because nobody judged it» may remain; anything else (flags, unknown masks, policy, judges)
# keeps the card on G2.
AUTO_PASS_ALLOWED_REASONS = frozenset({"judge:not_run (no judge profile)", "judge:not_run"})


def auto_pass_config(root: Path) -> dict:
    cfg = (style_scorer.load_config(root) or {}).get("auto_pass") or {}
    return {
        "enabled": bool(cfg.get("enabled", False)),
        "profiles": list(cfg.get("profiles", [])),
        "window": int(cfg.get("window", 100)),
        "max_return_share": float(cfg.get("max_return_share", 0.2)),
        "state": str(cfg.get("state", "runs/_autopass/state.json")),
    }


def _auto_pass_state_path(ws: Workspace, cfg: dict) -> Path:
    return ws.root / cfg["state"]  # one breaker for every runs directory


def auto_pass_state(ws: Workspace, cfg: dict | None = None) -> dict:
    """Shared across runs: every auto-passed card (run, card, returned) and the breaker."""
    cfg = cfg or auto_pass_config(ws.root)
    path = _auto_pass_state_path(ws, cfg)
    if not path.is_file():
        return {"format": "cardforge-auto-pass-state-v1", "passed": [], "disabled": None}
    return json.loads(path.read_text(encoding="utf-8"))


def _save_auto_pass_state(ws: Workspace, cfg: dict, state: dict) -> None:
    path = _auto_pass_state_path(ws, cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_atomic_bytes(path, json.dumps(state, ensure_ascii=False, indent=1).encode("utf-8"))


def auto_pass_status(ws: Workspace, ctx_runtime_options: dict | None = None) -> tuple[bool, str]:
    """(acts, why): real auto-pass is on unless the config, the breaker, runs/STOP or a
    `--shadow` run turn it back into the shadow journal."""
    cfg = auto_pass_config(ws.root)
    if not cfg["enabled"]:
        return False, "config [auto_pass] enabled = false"
    if (ctx_runtime_options or {}).get("shadow"):
        return False, "shadow run (--shadow)"
    if (ctx_runtime_options or {}).get("auto_pass") is False:
        return False, "run option auto_pass = false"
    if stop_requested(ws.runs_dir):
        return False, "kill switch runs/STOP"
    disabled = auto_pass_state(ws, cfg).get("disabled")
    if disabled:
        return False, f"breaker: {disabled.get('reason')}"
    return True, "on"


def _real_auto_pass(ctx: Ctx, card_id, attempt_no, image_sha, hard, zone, evaluation) -> None:
    if not zone or not zone.get("in_zone"):
        return
    if card_id in ctx.manifest.counters.get("human_only", {}):
        # Stage 7 (user 2026-11-10): a card redrawn on a person's request always goes to them.
        zone["real"] = {"acts": False, "why": "regenerated on request: the person reviews it"}
        return
    cfg = auto_pass_config(ctx.ws.root)
    style = hard.checks.get(style_scorer.SIGNAL, {})
    if style.get("profile") not in cfg["profiles"]:
        return
    acts, why = auto_pass_status(ctx.ws, ctx.manifest.runtime_options)
    rest = [r for r in evaluation.reasons if r not in AUTO_PASS_ALLOWED_REASONS]
    if not acts or rest or evaluation.decision not in ("flag", "accept"):
        left = rest or evaluation.decision
        zone["real"] = {"acts": False, "why": why if not acts else f"reasons left: {left}"}
        return
    zone["acts"] = True
    zone["real"] = {"acts": True, "why": "auto_pass"}
    evaluation.decision = "accept"
    evaluation.reasons = ["auto_pass"]
    payload = {"attempt_no": attempt_no, "profile": style.get("profile"), "p": zone["p"],
               "tau_a": zone["tau_a"], "basis": zone.get("basis"), "image_sha256": image_sha,
               "model_sha256": style.get("model_sha256")}  # fmt: skip
    ctx.store.emit("auto_pass", payload, card_id=card_id)
    entry = {**payload, "returned": None}
    ctx.store.update_manifest(
        lambda m: m.counters.setdefault("auto_pass", {}).update({card_id: entry})
    )
    state = auto_pass_state(ctx.ws, cfg)
    state["passed"].append({"run": ctx.store.run_id, "card": card_id, "attempt_no": attempt_no,
                            "at": now(), "returned": False})  # fmt: skip
    _save_auto_pass_state(ctx.ws, cfg, state)


REDRAW_HOW = "cardforge regenerate <run> --card <id> --note <text> --reviewer <name>"


def unconfirmed_cards(manifest: RunManifest) -> dict[str, dict]:
    """Accepted cards that went into the set without a judge's confirmation (G2 = none)."""
    out = {}
    for cid, rec in manifest.counters.get("unconfirmed", {}).items():
        card = manifest.cards.get(cid)
        if card and card.state == "accepted" and card.accepted_attempt == rec["attempt_no"]:
            out[cid] = rec
    return out


def _auto_passed(manifest: RunManifest) -> set[str]:
    """Auto-passed and not returned at G3: these cards skip G2."""
    return {c for c, r in manifest.counters.get("auto_pass", {}).items() if not r.get("returned")}


def g3_sheet(ctx: Ctx) -> dict:
    """review/g3.json: per variant the 5×2 sheet and its auto-passed cards (marked АВТО on the
    sheet); returning one: `cardforge g3-return RUN --card <id> --reason … --reviewer …`."""
    m = ctx.manifest
    auto = m.counters.get("auto_pass", {})
    acts, why = auto_pass_status(ctx.ws, m.runtime_options)
    variants = {}
    for cid, rec in sorted(auto.items()):
        v = m.cards[cid].variant_id
        slot = variants.setdefault(v, {"sheet": f"sheets/{v}.png", "auto_passed": []})
        slot["auto_passed"].append({"card_id": cid, "attempt_no": rec["attempt_no"], "p": rec["p"],
                                    "returned": rec.get("returned")})  # fmt: skip
    for cid, rec in sorted(unconfirmed_cards(m).items()):
        v = m.cards[cid].variant_id
        slot = variants.setdefault(v, {"sheet": f"sheets/{v}.png", "auto_passed": []})
        slot.setdefault("unconfirmed", []).append({"card_id": cid, **rec})
    how = "cardforge g3-return <run> --card <card_id> --reason <text> --reviewer <name>"
    return {"gate": "G3", "auto_pass": {"acts": acts, "why": why}, "variants": variants,
            "how_to_return": how,
            "how_to_redraw": REDRAW_HOW}  # fmt: skip


def g3_return(ws: Workspace, run_id: str, card_id: str, reason: str, reviewer: str) -> dict:
    """G3 (user 2026-11-09): the person returns an auto-passed card from the set sheet. The card
    goes back to G2 (flagged), the return is journaled (`auto_pass_returned`) and counted; more
    than `max_return_share` of the last `window` auto-passed cards returned → the breaker turns
    auto-pass off (`auto_pass_disabled`, state file) until a person switches it back on."""
    if not reason.strip() or not reviewer.strip():
        raise ServiceError("a G3 return needs a reason and a reviewer")
    store = RunStore(ws.run_dir(run_id))
    m = store.manifest()
    rec = m.counters.get("auto_pass", {}).get(card_id)
    if rec is None:
        raise ServiceError(f"{card_id} was not auto-passed in {run_id}")
    if rec.get("returned"):
        raise ServiceError(f"{card_id} was already returned")
    if m.cards[card_id].state != "accepted":
        raise ServiceError(f"{card_id} is {m.cards[card_id].state}, not accepted")
    returned = {"at": now(), "reason": reason, "reviewer": reviewer}
    store.update_manifest(lambda mm: mm.counters["auto_pass"][card_id].update(returned=returned))
    store.emit("auto_pass_returned", {"attempt_no": rec["attempt_no"], **returned}, card_id=card_id)
    states.set_card_state(store, card_id, "flagged", f"g3_return: {reason}")
    store.update_manifest(lambda mm: setattr(mm.cards[card_id], "accepted_attempt", None))
    cfg = auto_pass_config(ws.root)
    state = auto_pass_state(ws, cfg)
    for r in state["passed"]:
        if r["run"] == run_id and r["card"] == card_id:
            r["returned"] = True
    window = state["passed"][-cfg["window"] :]
    n_ret = sum(r["returned"] for r in window)
    if state.get("disabled") is None and n_ret > cfg["max_return_share"] * cfg["window"]:
        why = (
            f"{n_ret} of the last {len(window)} auto-passed cards returned at G3 "
            f"(> {cfg['max_return_share']:.0%} of {cfg['window']})"
        )
        state["disabled"] = {"at": now(), "returned": n_ret, "window": len(window), "reason": why}
        store.emit("auto_pass_disabled", state["disabled"])
    _save_auto_pass_state(ws, cfg, state)
    if m.state in states.RUN_TERMINAL or m.state == "exported":
        states.set_run_state(store, "rendering", f"g3 return of {card_id}")
    return status(ws, run_id)


def select_final(
    ws: Workspace, run_id: str, variants: list[str], reviewer: str, human_answer: str
) -> dict:
    """G3 (debt T3, stage 7): the person chooses the final variant(s), verbatim. The chosen ones
    must be complete; cards of the other variants that still wait (G2, fix, not drawn) are
    rejected as «not chosen at G3» and stay alternatives; the run finishes `completed`."""
    if not variants or not reviewer.strip() or not human_answer.strip():
        raise ServiceError("a G3 choice needs variants, the reviewer and the person's words")
    store = RunStore(ws.run_dir(run_id))
    if not store.exists("manifest.json"):
        raise ServiceError(f"no run {run_id} in {ws.runs_dir}")
    m = store.manifest()
    if m.state not in ("completed", "partial", "awaiting_g2"):
        raise ServiceError(f"{run_id} is {m.state}: choose variants after rendering")
    plans = {p["variant_id"]: p for p in store.read_json("plan/variants.json")}
    unknown = [v for v in variants if v not in plans]
    if unknown:
        raise ServiceError(f"no variant {', '.join(unknown)} in {run_id}")
    incomplete = [
        v for v in variants
        if any(m.cards[c["card_id"]].state != "accepted" for c in plans[v]["cards"])
    ]  # fmt: skip
    if incomplete:
        raise ServiceError(f"incomplete variants: {', '.join(incomplete)}")
    event = store.emit("human_decision", {
        "gate": "G3", "subject": "variant", "decision": "select", "variants": list(variants),
        "human_answer": human_answer, "decided_by": "human", "reviewer": reviewer,
    })  # fmt: skip
    selection = {"variants": list(variants), "reviewer": reviewer, "human_answer": human_answer,
                 "event_id": event.event_id, "at": now()}  # fmt: skip
    store.update_manifest(lambda mm: mm.counters.update(g3_selection=selection))
    dropped = []
    for cid, card in sorted(store.manifest().cards.items()):
        if card.variant_id in variants or card.state in ("accepted", "rejected", "failed"):
            continue
        if card.state in ("planned", "spec_ready", "needs_fix", "flagged"):
            states.set_card_state(store, cid, "rejected", "variant not chosen at G3")
            dropped.append(cid)
    if store.exists(G2):
        gate = store.read_json(G2)
        for item in gate["items"]:
            if item["card_id"] in dropped and item["card_id"] not in gate["decisions"]:
                gate["decisions"][item["card_id"]] = {
                    "decision": "reject", "text": "variant not chosen at G3",
                    "decided_by": "g3_selection", "event_id": event.event_id, "applied": True,
                }  # fmt: skip
        store.write(G2, gate)
    if store.manifest().state != "completed":
        states.set_run_state(store, "rendering", "G3 choice")
        advance(ws, run_id)
    out = status(ws, run_id)
    out["dropped"] = dropped
    return out


def card_reopen(ws: Workspace, run_id: str, card_id: str, reason: str, reviewer: str) -> dict:
    """A person brings a rejected or accepted card back to G2 with its last image (no new
    call). For a card the core rejected at a limit, or an accepted card the person wants to
    see again; at G2 the person decides as usual (`accept`, `reject`, `fix`: a fix needs an
    attempt left, see `budget --max-attempts`). The card's previous G2 decision moves to the
    gate history; a confirmed `ip_check` of its variant goes back to `pending` (the set
    changed). Auto-passed accepted cards are returned with `g3_return` instead."""
    if not reason.strip() or not reviewer.strip():
        raise ServiceError("reopening a card needs a reason and a reviewer")
    store = RunStore(ws.run_dir(run_id))
    if not store.exists("manifest.json"):
        raise ServiceError(f"no run {run_id} in {ws.runs_dir}")
    m = store.manifest()
    card = m.cards.get(card_id)
    if card is None:
        raise ServiceError(f"no card {card_id} in {run_id}")
    if card.state not in ("rejected", "accepted"):
        raise ServiceError(f"{card_id} is {card.state}: only rejected or accepted cards reopen")
    auto = m.counters.get("auto_pass", {}).get(card_id)
    if card.state == "accepted" and auto and not auto.get("returned"):
        raise ServiceError(f"{card_id} was auto-passed: return it with `g3-return`")
    request = store.read_model("request.json", CollectionRequest)
    if request.hitl.g2 == "none":
        raise ServiceError(f"{run_id} has no human G2 gate")
    image = f"{_attempt_dir(card_id, card.variant_id, card.attempts)}/final.png"
    if not card.attempts or not store.exists(image):
        raise ServiceError(f"{card_id} has no image to show at G2")
    if m.state not in (*states.RUN_TERMINAL, "awaiting_g2") or m.state == "failed":
        raise ServiceError(f"{run_id} is {m.state}: reopen a card at G2 or after the run ends")
    if store.exists(G2):
        gate = store.read_json(G2)
        old = gate["decisions"].pop(card_id, None)
        if old is not None:
            gate.setdefault("history", []).append(
                {"card_id": card_id, **old, "superseded": f"card_reopen: {reason}"}
            )
            store.write(G2, gate)
    store.emit(
        "card_reopened",
        {"from": card.state, "attempt_no": card.attempts, "reason": reason, "reviewer": reviewer},
        card_id=card_id,
        variant_id=card.variant_id,
    )
    states.set_card_state(store, card_id, "flagged", f"card_reopen: {reason}")
    store.update_manifest(lambda mm: setattr(mm.cards[card_id], "accepted_attempt", None))
    ip_check.reset_pending(store, card.variant_id, f"card {card_id} reopened: {reason}")
    # The G2 gate is rebuilt from the flagged cards on the next `resume` (no render call).
    states.set_run_state(store, "rendering", f"card_reopen of {card_id}")
    return status(ws, run_id)


def shadow_auto_pass_config(root: Path) -> dict:
    """`[shadow_auto_pass.<profile>]` of config/style_scorer.toml: {profile: {tau_a, stub}}.
    Journal only: it never changes a decision, so it is allowed while auto-pass is not."""
    cfg = style_scorer.load_config(root) or {}
    out = {}
    for pid, entry in (cfg.get("shadow_auto_pass") or {}).items():
        unknown = set(entry) - {"tau_a", "stub", "basis", "model_sha256", "note"}
        if unknown:
            raise ServiceError(f"shadow_auto_pass.{pid}: unknown keys {sorted(unknown)}")
        tau = float(entry["tau_a"])
        if not 0.0 <= tau <= 1.0:
            raise ServiceError(f"shadow_auto_pass.{pid}: tau_a {tau} outside [0, 1]")
        out[pid] = {"tau_a": tau, "stub": bool(entry.get("stub", False))}
        for key in ("basis", "model_sha256"):
            if entry.get(key):
                out[pid][key] = str(entry[key])
    return out


def _bundle_ref_ids(attempt_dir: Path) -> list[str]:
    """Reference ids of the attempt's render bundle (background plates excluded)."""
    try:
        bundle = json.loads((attempt_dir / "bundle.json").read_text())
    except (OSError, ValueError):
        return []
    return [r["id"] for r in bundle.get("references", []) if r.get("role") != "background"]


def _mask(ctx: Ctx, final: Path):
    if ctx.rt.mask is None:
        return None
    try:
        return ctx.rt.mask(final)
    except Exception as exc:  # a mask failure leaves the check unknown, never passed
        ctx.store.emit("error", {"kind": "object_mask", "detail": f"{type(exc).__name__}: {exc}"})
        return None


# ---------------------------------------------------------------- candidate mode


def _round_items(ctx: Ctx, specs, cards: list[str]) -> list[batch_judges.JudgeItem]:
    items = []
    n = ctx.manifest.candidates_per_card
    for cid in cards:
        card = ctx.manifest.cards[cid]
        rel = _attempt_dir(cid, card.variant_id, card.attempts)
        for k in range(1, n + 1):
            final = ctx.store.path(f"{rel}/cand_{k}/final.png")
            if not final.exists():
                continue  # blocked by the provider: no image to judge
            items.append(
                batch_judges.JudgeItem(
                    item_id=f"{cid}.a{card.attempts:02d}.k{k}",
                    card_id=cid,
                    position=f"{cid}.a{card.attempts:02d}",
                    candidate=k,
                    image=os.path.relpath(final, ctx.ws.root),
                    image_sha256=sha256(final),
                    spec=batch_judges.judge_spec(specs[cid], ctx.request.set_theme),
                )
            )
    return items


def _open_round(ctx: Ctx, items: list[batch_judges.JudgeItem]) -> str:
    """A judging round is frozen by its item list; a resumed round keeps its number."""
    rows = [i.__dict__ for i in items]
    counters = ctx.manifest.counters
    current = counters.get("open_round")
    if current and ctx.store.exists(f"judging/{current}/items.json"):
        if ctx.store.read_json(f"judging/{current}/items.json") == rows:
            return current
    number = counters.get("judge_rounds", 0) + 1
    name = f"round_{number:02d}"
    ctx.store.write(f"judging/{name}/items.json", rows)
    ctx.store.update_manifest(lambda m: m.counters.update(judge_rounds=number, open_round=name))
    return name


def _contact_sheets(ctx: Ctx, round_name: str, items) -> list[str]:
    out = []
    for start in range(0, len(items), 20):
        sheet = Image.new("RGB", (215 * 5, 260 * 4), "white")
        draw = ImageDraw.Draw(sheet)
        for pos, item in enumerate(items[start : start + 20]):
            x, y = pos % 5 * 215, pos // 5 * 260
            with Image.open(ctx.ws.root / item.image) as im:
                sheet.paste(im.convert("RGB").resize((215, 240)), (x, y))
            draw.text((x + 4, y + 243), item.item_id, fill="black")
        rel = f"judging/{round_name}/inspect-sheet-{start // 20 + 1:02}.png"
        buf = io.BytesIO()
        sheet.save(buf, format="PNG")
        write_atomic_bytes(ctx.store.path(rel), buf.getvalue())
        out.append(rel)
    return out


def _inspect_round(ctx: Ctx, specs, round_name: str, items, ocr: dict) -> dict | None:
    """PolicyInspector for the whole round in one task (host agent: one answer file)."""
    rules = {
        cid: card_qa.applicable_inspector_rules(ctx.packs, _plan_verdicts(ctx), specs[cid])
        for cid in {i.card_id for i in items}
    }
    questions = [
        {
            "item_id": i.item_id,
            "image": os.path.relpath(ctx.ws.root / i.image, ctx.store.root),
            "questions": [{"rule_id": r.id, "question": r.qa_question} for r in rules[i.card_id]],
        }
        for i in items
        if rules[i.card_id]
    ]
    answers: dict = {}
    if questions:
        task = {
            "step": "policy_inspect_batch",
            "schema_version": "policy_inspect_batch.v1",
            "instruction": "For every item answer each question about its image with yes, no or "
            "uncertain and one sentence of visual evidence. Contact sheets show all items.",
            "items": questions,
            "contact_sheets": _contact_sheets(ctx, round_name, items),
            "answer_schema": {
                "answers": {
                    "<item_id>": {"<rule_id>": {"answer": "yes|no|uncertain", "evidence": "str"}}
                }
            },
        }
        reply = ask(ctx.store, ctx.ledger, ctx.rt.llm, f"inspect.{round_name}", seal(task))
        if isinstance(reply, Pending):
            ctx.pending.append(reply)
            return None
        answers = reply.data.get("answers", {})
    hitl = ctx.request.hitl
    return {
        i.item_id: card_qa.policy_verdicts(
            specs[i.card_id],
            rules[i.card_id],
            answers.get(i.item_id, {}),
            ocr[i.item_id],
            ctx.packs,
            hitl,
        )
        for i in items
    }


def _run_batch_judge(ctx: Ctx, round_name: str, jp, judge, items) -> dict:
    record_rel = f"judging/{round_name}/{jp.id}.result.json"
    if ctx.store.exists(record_rel):  # never re-asked, never rewritten
        return ctx.store.read_json(record_rel)
    estimate = judge.estimate_usd(items)
    rid = ctx.ledger.reserve(estimate, f"judge:{jp.id}", None, cap_usd=jp.max_cost_usd)
    if rid is None:
        return {"judge_id": jp.id, "status": "skipped: budget_exhausted", "verdicts": {}}
    try:
        result = judge.judge(ctx.store.path(f"judging/{round_name}"), items)
    except Exception as exc:
        ctx.ledger.settle(rid, estimate)  # unknown spend: keep the estimate
        ctx.store.emit("error", {"kind": "judge", "judge": jp.id, "detail": str(exc)[:500]})
        return {"judge_id": jp.id, "status": f"error: {str(exc)[:300]}", "verdicts": {}}
    ctx.ledger.settle(rid, result.cost_usd, result.elapsed_s)
    role = "decider" if jp.id in ctx.rt.deciders else "shadow"
    record = {
        "judge_id": jp.id,
        "role": role,
        "kind": jp.kind,
        "provider": jp.provider,
        "model": jp.model,
        "profile": jp.profile,
        "decision_policy": jp.decision_policy,
        "status": "ok",
        "cost_usd": result.cost_usd,
        "elapsed_s": result.elapsed_s,
        "answered_at": now(),
        "meta": result.meta,
        "verdicts": result.verdicts,
    }
    ctx.store.write(record_rel, record)
    for item in items:
        verdict = result.verdicts.get(item.item_id)
        if verdict is None:
            continue
        rel = f"{_attempt_dir(item.card_id, item.card_id.split('_')[0], int(item.position[-2:]))}"
        crel = f"{rel}/cand_{item.candidate}/judges/{jp.id}.json"
        if not ctx.store.exists(crel):
            ctx.store.write(
                crel,
                {
                    "judge_id": jp.id,
                    "role": role,
                    "round": round_name,
                    "item_id": item.item_id,
                    **verdict,
                },
            )
        ctx.store.emit(
            "judge_result",
            {
                "role": role,
                "profile": jp.id,
                "item_id": item.item_id,
                "candidate": item.candidate,
                "attempt_no": int(item.position[-2:]),
                "decision": verdict.get("decision"),
            },
            card_id=item.card_id,
        )
    return record


def _evaluate_round(ctx: Ctx, specs) -> bool:
    """Candidate mode QA for every card waiting in `evaluating` (TA §11.1 order:
    hard checks → PolicyInspector → judges → rule)."""
    cards = [c for c, s in sorted(ctx.manifest.cards.items()) if s.state == "evaluating"]
    cards = [c for c in cards if c in specs]
    if not cards:
        return False
    items = _round_items(ctx, specs, cards)
    round_name = _open_round(ctx, items)
    ocr, hard = {}, {}
    for item in items:
        final = ctx.ws.root / item.image
        ocr[item.item_id] = ctx.rt.ocr(final)
        hard[item.item_id] = card_qa.hard_checks(
            final, specs[item.card_id], ocr[item.item_id], _mask(ctx, final), ctx.rt.mask_source
        )
    verdicts = _inspect_round(ctx, specs, round_name, items, ocr)
    if verdicts is None:
        return False
    with ThreadPoolExecutor(
        max_workers=max(1, len(ctx.rt.batch)), thread_name_prefix="judge"
    ) as pool:
        records = dict(
            zip(
                [jp.id for jp, _ in ctx.rt.batch],
                pool.map(lambda m: _run_batch_judge(ctx, round_name, *m, items), ctx.rt.batch),
                strict=True,
            )
        )
    errors = [
        r["status"]
        for jid, r in records.items()
        if jid in ctx.rt.deciders and r["status"].startswith("error")
    ]
    if errors:
        raise ServiceError(f"deciding judge failed in {round_name}: {errors[0]}; fix and resume")
    by_card: dict[str, list] = {}
    for item in items:
        by_card.setdefault(item.card_id, []).append(item)
    for cid in cards:
        _decide_position(ctx, specs[cid], by_card.get(cid, []), hard, verdicts, records)
    ctx.store.update_manifest(lambda m: m.counters.pop("open_round", None))
    return True


def _decide_position(ctx: Ctx, spec, members, hard, verdicts, records) -> None:
    cid = spec.card_id
    card = ctx.manifest.cards[cid]
    eligible = {
        i.item_id
        for i in members
        if not hard[i.item_id].failed
        and not any(v.action in (actions.FIX, actions.REGENERATE) for v in verdicts[i.item_id])
    }
    deciders = [records[d] for d in ctx.rt.deciders]
    reasons: list[str] = []
    missing = [r["judge_id"] for r in deciders if r["status"] != "ok"]
    if missing:
        outcome = {"decision": "flag", "item_id": None, "reason": f"judges unavailable {missing}"}
    elif ctx.rt.rule == "single":
        v = deciders[0]["verdicts"]
        ok = [
            i.item_id
            for i in members
            if i.item_id in eligible and v.get(i.item_id, {}).get("accept")
        ]
        outcome = (
            {"decision": "accept", "item_id": ok[0], "reason": "single judge accepts"}
            if ok
            else {"decision": "fix", "item_id": None, "reason": "no candidate accepted"}
        )
    else:
        kinds = {r["kind"]: r["verdicts"] for r in deciders}
        outcome = batch_judges.pick_and_confirm(
            kinds["group"], kinds["absolute"], members, eligible
        )
    decision = outcome["decision"]
    reasons.append(f"rule {ctx.rt.rule}: {outcome['reason']}")
    chosen = next((i for i in members if i.item_id == outcome.get("item_id")), None)
    if chosen is not None:
        flags = [v for v in verdicts[chosen.item_id] if v.action == actions.FLAG_G2]
        unknown = hard[chosen.item_id].unknown
        if decision == "accept" and (flags or unknown):
            decision = "flag"
            reasons += [f"policy:{v.rule_id}:flag_g2" for v in flags]
            reasons += [f"hard_check:{u}:unknown" for u in unknown]
    if not eligible:
        reasons.append("no candidate passed hard checks and policy")
    candidates = {
        str(i.candidate): {
            "item_id": i.item_id,
            "hard_failed": hard[i.item_id].failed,
            "policy": [
                f"{v.rule_id}:{v.verdict}:{v.action}"
                for v in verdicts[i.item_id]
                if v.verdict != "pass"
            ],
            "verdicts": {r["judge_id"]: r["verdicts"].get(i.item_id) for r in records.values()},
        }
        for i in members
    }
    evaluation = CardEvaluation(
        hard_checks={str(i.candidate): hard[i.item_id].checks for i in members},
        policy_verdicts=[v for i in members for v in verdicts[i.item_id]],
        decision=decision,
        reasons=reasons,
        fix_instruction=outcome.get("fix_instruction"),
        candidates=candidates,
        chosen_candidate=chosen.candidate if chosen else None,
        rule={"name": ctx.rt.rule, **outcome},
    )
    for i in members:
        for v in verdicts[i.item_id]:
            if v.verdict != "pass":
                ctx.store.emit("policy_verdict", v.model_dump(), card_id=cid)
    rel = _attempt_dir(cid, card.variant_id, card.attempts)
    ctx.store.write(f"{rel}/eval.json", evaluation)
    _apply_decision(ctx, cid, evaluation)
    if ctx.manifest.cards[cid].state == "accepted":
        ctx.store.update_manifest(
            lambda m: setattr(m.cards[cid], "accepted_candidate", evaluation.chosen_candidate)
        )


def style_gate_skip(gate: dict, style: dict | None, shadow_run: bool) -> str | None:
    """J4 (config `style_gate` of a judge): why the judge is not asked on this card, or None.
    Asked only in the grey zone `min_p` ≤ p < τ_r; a card without a numeric score is asked."""
    if not gate or (gate.get("runs", "shadow") == "shadow" and not shadow_run):
        return None
    p = (style or {}).get("value")
    if not isinstance(p, int | float):
        return None
    lo, hi = float(gate.get("min_p", 0.0)), (style or {}).get("tau_r")
    if p < lo:
        return f"skipped: style_gate (p={p:.3f} < {lo})"
    if hi is not None and p >= hi:
        return f"skipped: style_gate (p={p:.3f} >= tau_r {hi})"
    return None


def _ask_judges(ctx: Ctx, spec, attempt_no: int, rel: str, final: Path, style=None):
    """Judge panel: the same task to every member in parallel (semaphore = thread pool).

    Each answer is stored once in `<attempt>/judges/<id>.json` with judge id, profile,
    model, cost and the raw answer, and is reused (never rewritten) on resume. Returns
    (decider outcome or None, {other judge: outcome | status}, Pending of the decider).
    A logged judge with a `style_gate` (J4) outside the grey zone is not asked: its entry is
    a `skipped: style_gate …` status and the skip is journaled."""
    shadow_run = bool(ctx.manifest.runtime_options.get("shadow"))
    skipped: dict[str, str] = {}
    members = []
    for member in ctx.rt.panel:
        profile = member[0]
        why = None
        if profile.id != ctx.rt.judge.id:
            why = style_gate_skip(profile.style_gate, style, shadow_run)
        if why and not ctx.store.exists(f"{rel}/judges/{profile.id}.json"):
            skipped[profile.id] = why
        else:
            members.append(member)
    for jid, why in skipped.items():
        ctx.store.emit(
            "check_result",
            {"check": f"style_gate:{jid}", "passed": None, "attempt_no": attempt_no,
             "detail": why, "style_p": (style or {}).get("value")},
            card_id=spec.card_id,
        )  # fmt: skip
    if not members:
        return None, dict(skipped), None
    image_sha = sha256(final)
    with ThreadPoolExecutor(
        max_workers=max(1, min(ctx.rt.judge_concurrency, len(members))),
        thread_name_prefix="judge",
    ) as pool:
        results = list(
            pool.map(lambda m: _ask_judge(ctx, spec, attempt_no, rel, image_sha, *m), members)
        )
    decider, waiting, others = None, None, {}
    for (profile, _), result in zip(members, results):
        if profile.id == ctx.rt.judge.id:
            if isinstance(result, Pending):
                waiting = result
            elif isinstance(result, card_qa.JudgeOutcome):
                decider = result
        else:
            others[profile.id] = (
                f"pending: {result.task_path}" if isinstance(result, Pending) else result
            )
    return decider, {**others, **skipped}, waiting


def _veto_task(ctx: Ctx, spec, attempt_no: int, rel: str, image_sha: str, profile) -> dict:
    """Experiment-1 checklist task: stage-2 judge spec, roles-v1 references (phase 2)."""
    js = batch_judges.judge_spec(spec, ctx.request.set_theme)
    refs = veto_judge.references(ctx.ws.root, f"{spec.card_id}.a{attempt_no:02d}", image_sha, js)
    return veto_judge.veto_task(
        card_id=spec.card_id,
        attempt_no=attempt_no,
        image=f"{rel}/final.png",
        image_sha256=image_sha,
        spec=js,
        refs=refs,
        ref_sha256=[sha256(ctx.ws.root / r) for r in refs],
        judge_profile=profile.profile,
    )


def _ask_judge(ctx: Ctx, spec, attempt_no, rel, image_sha, profile, provider):
    record_rel = f"{rel}/judges/{profile.id}.json"
    if ctx.store.exists(record_rel):  # answered before: never asked or rewritten again
        return card_qa.JudgeOutcome.model_validate(ctx.store.read_json(record_rel)["outcome"])
    role = "decider" if profile.id == ctx.rt.judge.id else "shadow"
    veto = profile.kind == "veto"
    if veto:
        task = seal(_veto_task(ctx, spec, attempt_no, rel, image_sha, profile))
    else:
        task = seal(
            card_qa.judge_task(
                spec,
                set_theme=ctx.request.set_theme,
                attempt_no=attempt_no,
                image_rel=f"{rel}/final.png",
                thumb_rel=f"{rel}/thumb.png",
                image_sha256=image_sha,
                profile=profile,
            )
        )
    key = f"judge.{profile.id}.{spec.card_id}.a{attempt_no:02d}"
    # T2: OpenRouter judges take the same machine-wide request slots as the renders.
    shared = (
        ctx.rt.shared_slots.get("openrouter-image") if profile.provider == "openrouter" else None
    )
    try:
        with shared.slot() if shared else nullcontext():
            reply = ask(
                ctx.store,
                ctx.ledger,
                provider,
                key,
                task,
                card_id=spec.card_id,
                kind=f"judge:{profile.id}",
                cap_usd=profile.max_cost_usd,
            )
    except BudgetExhausted:
        return "skipped: budget_exhausted"
    except Exception as exc:
        if role == "decider":
            raise
        # A logged judge never stops the run; no record is written, so `resume` asks again
        # (the API ledger refuses a request whose outcome is unknown).
        detail = f"{type(exc).__name__}: {str(exc)[:300]}"
        ctx.store.emit(
            "error", {"kind": "judge", "judge": profile.id, "detail": detail}, card_id=spec.card_id
        )
        return f"error: {detail}"
    if isinstance(reply, Pending):
        return reply
    extra: dict = {}
    if veto:
        meta = reply.data.get("_meta")
        outcome, extra["veto"] = veto_judge.veto_outcome(
            reply.data, reply.provider, reply.model, veto_judge.RULE
        )
        if meta:
            extra["request"] = meta
        extra["refs"] = task["refs"]
    else:
        outcome = card_qa.judge_outcome(reply.data, reply.provider, reply.model, profile)
    ctx.store.write(
        record_rel,
        {
            "judge_id": profile.id,
            "role": role,
            "provider": reply.provider,
            "model": reply.model,
            "configured_model": profile.model,
            "profile": profile.profile,
            "decision_policy": profile.decision_policy,
            "cost_usd": reply.cost_usd,
            "card_id": spec.card_id,
            "attempt_no": attempt_no,
            "task_sha256": task["task_sha256"],
            "answered_at": now(),
            "raw_answer": {k: v for k, v in reply.data.items() if k != "_meta"},
            "outcome": outcome.model_dump(mode="json"),
            **extra,
        },
    )
    ctx.store.emit(
        "judge_result",
        {"attempt_no": attempt_no, "role": role, "profile": profile.id, **outcome.model_dump()},
        card_id=spec.card_id,
    )
    return outcome


CROP_LIMIT_REASON = "hard_check:crop"
# Geometric mask checks behave like `crop` (object position/size from a raw u2net mask, a
# borderline card can still be fine): stopped only by these at the limit → G2, not rejected.
# `background_simplicity` is not geometric and keeps the old rule.
# 2026-11-09: `crop` is a flag for the person (top only), so an unresolved crop fix check at the
# limit also goes to G2 instead of a silent rejection.
CROP_FIX_UNRESOLVED = "fix_check:crop:unresolved"
GEOMETRY_LIMIT_REASONS = frozenset(
    {CROP_LIMIT_REASON, CROP_FIX_UNRESOLVED, "hard_check:safe_zone", "hard_check:fill"}
)


def _g3_marks(ctx: Ctx) -> bool:
    """G2 = none with the person at G3 (user 2026-11-10): never reject silently; the best attempt
    goes into the set marked «не подтверждена судьями»."""
    opts = ctx.manifest.runtime_options
    return ctx.request.hitl.g2 == "none" and bool(opts.get("g3_unconfirmed"))


def _attempt_score(ctx: Ctx, card_id: str, attempt_no: int) -> tuple[int, int, int]:
    """Lower is better: hard failures, then other reasons, then older attempts last."""
    card = ctx.manifest.cards[card_id]
    rel = _attempt_dir(card_id, card.variant_id, attempt_no) + "/eval.json"
    if not ctx.store.exists(rel) or not ctx.store.exists(rel.replace("eval.json", "final.png")):
        return (99, 99, -attempt_no)
    reasons = ctx.store.read_model(rel, CardEvaluation).reasons
    hard = sum(r.startswith("hard_check:") and not r.endswith(":flag") for r in reasons)
    return (hard, len(reasons), -attempt_no)


def _accept_unconfirmed(ctx: Ctx, card_id: str, why: str) -> None:
    """The best attempt into the set, marked for the G3 sheet, delivery and report."""
    card = ctx.manifest.cards[card_id]
    best = min(range(1, card.attempts + 1), key=lambda n: _attempt_score(ctx, card_id, n))
    rel = _attempt_dir(card_id, card.variant_id, best) + "/eval.json"
    ev = ctx.store.read_model(rel, CardEvaluation) if ctx.store.exists(rel) else None
    reasons = ev.reasons if ev else []
    notes = [
        f"{v.rule_id} ({v.verdict}): {v.evidence}"
        for v in (ev.policy_verdicts if ev else [])
        if v.verdict != "pass"
    ]
    record = {"attempt_no": best, "reasons": reasons, "policy_notes": notes, "why": why,
              "at": now()}  # fmt: skip
    reason = "unconfirmed: " + ("; ".join(reasons) or why)
    states.set_card_state(ctx.store, card_id, "accepted", reason)

    def change(m: RunManifest) -> None:
        m.cards[card_id].accepted_attempt = best
        m.cards[card_id].first_attempt_accepted = False
        m.counters.setdefault("unconfirmed", {})[card_id] = record

    ctx.store.update_manifest(change)
    ctx.store.emit("check_result", {"check": "unconfirmed_accept", "passed": None, **record},
                   card_id=card_id)  # fmt: skip


def _apply_decision(ctx: Ctx, card_id: str, evaluation: CardEvaluation) -> None:
    budget = ctx.request.budget
    card = ctx.manifest.cards[card_id]
    decision = evaluation.decision
    g3 = _g3_marks(ctx)
    if g3 and decision != "accept" and card_id in ctx.manifest.counters.get("human_only", {}):
        # A redraw the person asked for is not redrawn again: they see it at G3.
        _accept_unconfirmed(ctx, card_id, "regenerated on request")
        return
    if g3 and decision == "reject":
        decision = "fix"  # redraw within the limit; the best attempt still reaches G3
    if decision == "flag" and ctx.request.hitl.g2 == "none":
        # TA §9.5: without a human, review findings are conservatively regenerated.
        decision = "fix" if ctx.request.hitl.review_policy == "replace" else "accept"
    if decision == "accept":
        states.set_card_state(ctx.store, card_id, "accepted", "; ".join(evaluation.reasons) or None)
        _mark_accepted(ctx, card_id)
    elif decision == "fix":
        rounds_left = card.fix_rounds < budget.max_fix_rounds
        attempts_left = card.attempts < max_attempts(ctx.manifest, budget, card_id)
        if rounds_left and attempts_left:
            hard_fail = any(r.startswith("hard_check:") for r in evaluation.reasons)
            asked = list(evaluation.fixes)
            if not asked and evaluation.fix_instruction:  # candidate mode: the rule's text
                asked = [{"source": "judge", "codes": [], "text": evaluation.fix_instruction}]
            _needs_fix(ctx, card_id, asked, same_seed=not hard_fail)
        elif g3:
            _accept_unconfirmed(ctx, card_id, "attempt limit")
        elif (
            ctx.request.hitl.g2 != "none"
            and evaluation.reasons
            and set(evaluation.reasons) <= GEOMETRY_LIMIT_REASONS
        ):
            # User 2026-11-04 («Да, на G2 человеку»): a card stopped only by the crop check
            # (the detector flags edge contact; a near-edge object can still be fine) is not
            # rejected silently at the limit: the last attempt goes to G2 for the human.
            # 2026-11-05: the same for the geometric mask checks safe_zone and fill.
            states.set_card_state(
                ctx.store, card_id, "flagged", "crop_limit: " + ", ".join(evaluation.reasons)
            )
        else:
            states.set_card_state(ctx.store, card_id, "rejected", "max_fix_rounds")
    elif decision == "reject":
        states.set_card_state(ctx.store, card_id, "rejected", "; ".join(evaluation.reasons))
    else:
        states.set_card_state(ctx.store, card_id, "flagged", "; ".join(evaluation.reasons))


def _after_rendering(ctx: Ctx, specs) -> str:
    mode = ctx.request.hitl.g2
    cards = ctx.manifest.cards
    # An applied fix is not a final verdict: the redrawn card is reviewed again.
    old_gate = ctx.store.read_json(G2) if ctx.store.exists(G2) else {"decisions": {}}
    reviewed = {
        cid
        for cid, d in old_gate["decisions"].items()
        if not (d["decision"] == "fix" and d.get("applied"))
    }
    auto = _auto_passed(ctx.manifest)  # auto-pass (2026-11-09): straight to the final set
    waiting = [
        cid
        for cid in sorted(specs)
        if cid not in reviewed
        and cid not in auto
        and (cards[cid].state == "flagged" or (mode == "all" and cards[cid].state == "accepted"))
    ]
    if mode != "none" and waiting:
        old = ctx.store.read_json(G2) if ctx.store.exists(G2) else {"items": [], "decisions": {}}
        history = old.get("history", []) + [
            {"card_id": cid, **d} for cid, d in old["decisions"].items() if cid not in reviewed
        ]
        old["decisions"] = {cid: d for cid, d in old["decisions"].items() if cid in reviewed}
        items = [i for i in old["items"] if i["card_id"] in reviewed]
        for cid in waiting:
            last = _last_eval(ctx, cid)
            rel = _attempt_dir(cid, cards[cid].variant_id, cards[cid].attempts)
            item = {
                "card_id": cid,
                "variant_id": cards[cid].variant_id,
                "object": specs[cid].object_name,
                "state": cards[cid].state,
                "image": f"{rel}/final.png" if ctx.store.exists(f"{rel}/final.png") else None,
                "reasons": last.reasons if last else [cards[cid].reason],
                "judge_decision": last.judge.decision if last and last.judge else None,
                # Stage 7: what the policy QA doubts, shown to the person as a comment.
                "policy_notes": [
                    f"{v.rule_id} ({v.verdict}): {v.evidence}"
                    for v in (last.policy_verdicts if last else [])
                    if v.verdict != "pass"
                ],
            }
            if last and last.candidates:
                # Candidate mode: both judges' verdicts on every candidate; `accept` takes
                # the rule's suggestion, `accept: <k>` a specific candidate.
                item["image"] = (
                    f"{rel}/cand_{last.chosen_candidate}/final.png"
                    if last.chosen_candidate
                    else None
                )
                item["suggested_candidate"] = last.chosen_candidate
                item["judge_decision"] = "accept" if last.chosen_candidate else "reject"
                item["candidates"] = {
                    k: {
                        "image": f"{rel}/cand_{k}/final.png",
                        "verdicts": {
                            j: (v or {}).get("decision") for j, v in c["verdicts"].items()
                        },
                        "hard_failed": c["hard_failed"],
                        "policy": c["policy"],
                    }
                    for k, c in last.candidates.items()
                }
            items.append(item)
        ctx.store.write(
            G2,
            {
                "gate": "G2",
                "mode": mode,
                "items": items,
                "allowed": ["accept", "accept: <candidate>", "reject", "fix: <text>"],
                "decisions": old["decisions"],
                "history": history,
            },
        )
        states.set_run_state(ctx.store, "awaiting_g2")
        return "wait"
    export_artifacts(ctx)
    states.set_run_state(ctx.store, "exported")
    return "next"


def _last_eval(ctx: Ctx, card_id: str) -> CardEvaluation | None:
    card = ctx.manifest.cards[card_id]
    rel = _attempt_dir(card_id, card.variant_id, card.attempts) + "/eval.json"
    return ctx.store.read_model(rel, CardEvaluation) if ctx.store.exists(rel) else None


def _final_variants(ctx: Ctx) -> set[str]:
    """Variants the run is finished on: the G3 choice if a person made one, else all approved."""
    selection = ctx.manifest.counters.get("g3_selection")
    return set(selection["variants"]) if selection else _approved(ctx)


def _finish(ctx: Ctx) -> str:
    final = _final_variants(ctx)
    cards = [c for c in ctx.manifest.cards.values() if c.variant_id in final]
    counters = ctx.manifest.counters
    if ctx.manifest.counters.get("g3_selection") and all(c.state == "accepted" for c in cards):
        reason = None  # T3: the chosen variants are complete; the others do not block
    elif counters.get("budget_exhausted"):
        reason = "budget_exhausted"
    elif any(c.state != "accepted" for c in cards):
        reason = "incomplete_variants"
    else:
        reason = None
    states.set_run_state(ctx.store, "partial" if reason else "completed", reason)
    export_artifacts(ctx)  # the report shows the final state
    return "wait"


# ---------------------------------------------------------------- export and status


def export_artifacts(ctx: Ctx) -> dict:
    store = ctx.store
    manifest = ctx.manifest
    plans = _plans(ctx) if store.exists("plan/variants.json") else []
    approved = _approved(ctx)
    grammar = load_grammar(ctx.ws.root)
    frame = ctx.ws.root / "knowledge/frames/frame_preview.png"
    evaluations: dict[str, CardEvaluation] = {}
    sets = {}
    framed: dict[str, list[str]] = {}
    for plan in plans:
        if plan.variant_id not in approved:
            continue
        finals: dict[str, Path] = {}
        tiles = []
        for c in plan.cards:
            card = manifest.cards[c.card_id]
            last = _last_eval(ctx, c.card_id) if card.attempts else None
            if last:
                evaluations[c.card_id] = last
            final = None
            if card.state == "accepted":
                rel = _attempt_dir(c.card_id, plan.variant_id, card.accepted_attempt or 1)
                if card.accepted_candidate:
                    rel += f"/cand_{card.accepted_candidate}"
                final = store.path(rel + "/final.png")
                finals[c.card_id] = final
                if frame.exists():
                    rel = f"framed/{plan.variant_id}/{c.card_id}.png"
                    write_framed(store.path(rel), final, frame)
                    framed.setdefault(plan.variant_id, []).append(rel)
            tiles.append((c.card_id, final, card.state))
        # Collection mode: the set's gold slots close the sheet, drawn separately (not planned).
        gold = range(1, plan.gold_slots + 1)
        tiles += [(f"{plan.variant_id}_g{k}", None, "золотая карта") for k in gold]
        auto = frozenset(_auto_passed(manifest))
        write_sheet(store.path(f"sheets/{plan.variant_id}.png"), tiles, auto,
                    unconfirmed=frozenset(unconfirmed_cards(manifest)))  # fmt: skip
        sets[plan.variant_id] = check_set(
            plan,
            finals,
            grammar,
            sheet_review=_set_sheet_review(ctx, plan, finals),
            style=_set_style(ctx, plan, finals),
        )
        store.write(f"sets/{plan.variant_id}.json", sets[plan.variant_id])
    events = store.events()
    durations = {
        "render_s": sum(
            e.payload.get("duration_s", 0) for e in events if e.type == "render_finished"
        ),
        "llm_s": sum(e.payload.get("duration_s", 0) for e in events if e.type == "llm_call"),
    }
    limitations = []
    if ctx.rt.profile.test_only:
        limitations.append(
            "Картинки сделаны FakeBackend — это не результат генерации и не оценка качества."
        )
    if ctx.rt.judge.test_only:
        limitations.append("Судья — сценарный фейк (`fake`), его решения не калиброваны.")
    if not ctx.rt.judge.enabled:
        limitations.append("Судья не выбран (`none`): все карты без провала проверок идут на G2.")
    limitations.append(
        "Проверки по маске (safe zone, простота фона, заполнение) в рантайме пока не запускаются."
    )
    moved = manifest.counters.get("fallback", {})
    if moved:
        by = sorted({v["profile"] for v in moved.values()})
        limitations.append(
            f"Резервный генератор (G1): {len(moved)} карт(ы) после "
            f"{manifest.fallback_after_failures} неудач подряд перерисованы на {', '.join(by)}: "
            + ", ".join(sorted(moved))
            + "."
        )
    unconf = unconfirmed_cards(manifest)
    if unconf:
        limitations.append(
            f"Не подтверждены судьями (G2 = none, решение 2026-11-10): {len(unconf)} карт(ы) — "
            + "; ".join(
                f"{c}: {', '.join(r['reasons']) or r['why']}" for c, r in sorted(unconf.items())
            )
            + ". Человек решает на G3: принять, перерисовать с правкой или заменить альтернативой."
        )
    if manifest.render_variant_limit:
        limitations.append(
            f"Тестовый лимит пользователя: рендерятся только {manifest.render_variant_limit} "
            "вариант(а) плана; остальные спланированы, но не рисовались."
        )
    text = render_report(
        models=_models_summary(ctx, plans, approved, events),
        deciders=_deciders_summary(ctx),
        run_id=store.run_id,
        request=ctx.request,
        manifest=manifest,
        packs=ctx.packs,
        plans=plans,
        approved=approved,
        plan_verdicts=_plan_verdicts(ctx),
        evaluations=evaluations,
        sets=sets,
        costs=ctx.ledger.totals(),
        durations=durations,
        events=events,
        framed=framed,
        limitations=limitations,
        panel={
            "decider": manifest.judge_profile,
            "members": [p for p, _ in ctx.rt.panel],
            "configured": manifest.shadow_judges,
            "records": [
                json.loads(p.read_text(encoding="utf-8"))
                for p in sorted(store.root.glob("cards/*/*/attempt_*/judges/*.json"))
            ],
        },
    )
    store.write("report.md", text)
    store.write("report.html", to_html(text, f"CardForge {store.run_id}"))
    store.write("hard_check_redraws.json", hard_check_redraws(store))
    store.write("review/g3.json", g3_sheet(ctx))
    return {
        "report": str(store.path("report.md")),
        "html": str(store.path("report.html")),
        "sheets": [str(store.path(f"sheets/{v}.png")) for v in sorted(sets)],
    }


# ---------------------------------------------------------------- set QA (stage 4)


def _set_qa_cfg(ctx: Ctx) -> dict:
    return ctx.ws.config().get("set_qa", {})


def _accepted_in_order(plan: SetPlan, finals: dict[str, Path]) -> list[tuple[str, Path]]:
    return [(c.card_id, finals[c.card_id]) for c in plan.cards if c.card_id in finals]


def _set_style(ctx: Ctx, plan: SetPlan, finals: dict[str, Path]) -> dict | None:
    """CSD style consistency of the accepted cards; embeddings cached by image SHA."""
    if ctx.rt.set_embedder is None:
        return None
    cfg = _set_qa_cfg(ctx)
    metric = cfg.get("style_metric", "csd")
    style_cfg = {"metric": metric, **{k: cfg[k] for k in set_qa.DEFAULT_STYLE if k in cfg}}
    cards = _accepted_in_order(plan, finals)
    if len(cards) < int(style_cfg.get("min_cards", 4)):
        return {"status": "not_run", "reason": f"{len(cards)} accepted cards"}
    try:
        key = f"set_reference:{metric}"
        if key not in ctx.cache:
            ctx.cache[key] = set_qa.load_reference(
                ctx.ws.root, metric, int(style_cfg.get("min_cards", 4))
            )
        rel = f"sets/style-embeddings-{metric}.json"
        cache = ctx.store.read_json(rel) if ctx.store.exists(rel) else {}
        shas = {cid: sha256(p) for cid, p in cards}
        todo = [(cid, p) for cid, p in cards if shas[cid] not in cache]
        if todo:
            vectors = ctx.rt.set_embedder([p for _, p in todo])
            cache.update({shas[cid]: v for (cid, _), v in zip(todo, vectors, strict=True)})
            ctx.store.write(rel, cache)
        result = set_qa.style_consistency(
            {cid: cache[shas[cid]] for cid, _ in cards}, ctx.cache[key], style_cfg
        )
    except Exception as exc:  # noqa: BLE001 - a set-level warning must never break export
        return {"status": "not_run", "reason": f"error: {str(exc)[:200]}"}
    result["embedder"] = ctx.rt.set_embed_source
    return result


def _set_sheet_review(ctx: Ctx, plan: SetPlan, finals: dict[str, Path]) -> dict | None:
    """One VLM look at the numbered 5×2 sheet of accepted cards, after every card of the
    variant has its final state. The answer is cached by task hash: an unchanged set is
    never asked (or billed) twice; a changed set (G2 reopened a card) is a new task."""
    provider = ctx.rt.sheet_llm
    if provider is None:
        return None
    cfg = _set_qa_cfg(ctx).get("sheet", {})
    manifest = ctx.manifest
    if any(manifest.cards[c.card_id].state not in states.CARD_TERMINAL for c in plan.cards):
        return {"status": "not_run", "reason": "variant not finished"}
    cards = _accepted_in_order(plan, finals)
    if len(cards) < int(cfg.get("min_cards", 4)):
        return {"status": "not_run", "reason": f"{len(cards)} accepted cards"}
    sheet_rel = f"sets/{plan.variant_id}.sheet-review.png"
    sheet_review.labeled_sheet(ctx.store.path(sheet_rel), [p for _, p in cards])
    specs = {c.card_id: c for c in plan.cards}
    task = seal(
        sheet_review.sheet_task(
            variant_id=plan.variant_id,
            sheet=sheet_rel,
            sheet_sha256=sha256(ctx.store.path(sheet_rel)),
            cards=[
                {
                    "position": n,
                    "card_id": cid,
                    "object": specs[cid].object_name,
                    "category": int(specs[cid].category),
                }
                for n, (cid, _) in enumerate(cards, 1)
            ],
        )
    )
    saved_rel = f"sets/{plan.variant_id}.sheet-review.json"
    if ctx.store.exists(saved_rel):
        saved = ctx.store.read_json(saved_rel)
        if saved.get("task_sha256") == task["task_sha256"]:
            return saved["result"]
    key = f"set_sheet_{plan.variant_id}_{task['task_sha256'][:12]}"
    try:
        reply = ask(
            ctx.store,
            ctx.ledger,
            provider,
            key,
            task,
            variant_id=plan.variant_id,
            kind="llm:set_sheet_review",
            cap_usd=float(cfg["max_cost_usd"]) if "max_cost_usd" in cfg else None,
        )
    except BudgetExhausted:
        return {"status": "not_run", "reason": "budget"}
    except Exception as exc:  # noqa: BLE001 - reservation kept by the ledger; never re-sent
        return {"status": "not_run", "reason": f"error: {str(exc)[:200]}"}
    if isinstance(reply, Pending):
        return {"status": "not_run", "reason": "waiting for an answer"}
    result = sheet_review.sheet_outcome(reply.data, task, reply.provider, reply.model)
    result["cost_usd"] = reply.cost_usd
    ctx.store.write(saved_rel, {"task_sha256": task["task_sha256"], "result": result})
    ctx.store.emit(
        "check_result",
        {
            "check": "set_sheet_review",
            "status": result["status"],
            "outliers": result.get("outliers", []),
            "provider": reply.provider,
            "model": reply.model,
            "cost_usd": reply.cost_usd,
        },
        variant_id=plan.variant_id,
    )
    return result


def _models_summary(ctx: Ctx, plans, approved, events) -> list[dict]:
    manifest = ctx.manifest
    renders = manifest.render_profiles or [manifest.render_profile]
    costs = ctx.ledger.totals()["by_variant"]
    rows = []
    for pid in renders:
        variants = [
            p.variant_id
            for p in plans
            if p.variant_id in approved
            and manifest.variant_profiles.get(p.variant_id, renders[0]) == pid
        ]
        cards = [c for c in manifest.cards.values() if c.variant_id in variants]
        finished = [e for e in events if e.type == "render_finished" and e.variant_id in variants]
        rows.append(
            {
                "profile": pid,
                "model": render_profile(ctx.ws, pid).model_id,
                "variants": variants,
                "cards": len(cards),
                "accepted": sum(c.state == "accepted" for c in cards),
                "first": sum(bool(c.first_attempt_accepted) for c in cards),
                "images": sum(e.payload.get("status") == "ok" for e in finished),
                "render_s": sum(e.payload.get("duration_s", 0) for e in finished),
                "cost": sum(v for k, v in costs.items() if k in variants),
            }
        )
    return rows


def _deciders_summary(ctx: Ctx) -> dict:
    manifest = ctx.manifest
    if manifest.candidates_per_card == 1 or not manifest.deciders:
        return {}
    from cardforge.export.report import cohen_kappa

    records: dict[str, list[dict]] = {}
    for path in sorted(ctx.store.root.glob("judging/round_*/*.result.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        records.setdefault(data["judge_id"], []).append(data)
    judges = []
    verdicts: dict[str, dict[str, bool]] = {}
    for jid in manifest.deciders:
        rows = records.get(jid, [])
        merged = {k: v for r in rows for k, v in r["verdicts"].items()}
        verdicts[jid] = {k: bool(v.get("accept")) for k, v in merged.items()}
        positions = {k.rsplit(".", 1)[0] for k in merged}
        accepted_positions = {k.rsplit(".", 1)[0] for k, v in merged.items() if v.get("accept")}
        jp = judge_profile(ctx.ws, jid)
        judges.append(
            {
                "id": jid,
                "kind": jp.kind,
                "provider": jp.provider,
                "model": jp.model,
                "profile": jp.profile,
                "items": len(merged),
                "accepted": sum(verdicts[jid].values()),
                "positions": len(positions),
                "positions_accepted": len(accepted_positions),
                "cost": sum(r.get("cost_usd", 0.0) for r in rows),
                "elapsed_s": sum(r.get("elapsed_s", 0.0) for r in rows),
            }
        )
    out = {
        "rule": manifest.decision_rule,
        "candidates": manifest.candidates_per_card,
        "judges": judges,
    }
    if len(manifest.deciders) == 2:
        a, b = manifest.deciders
        shared = sorted(set(verdicts[a]) & set(verdicts[b]))
        x, y = [verdicts[a][i] for i in shared], [verdicts[b][i] for i in shared]
        confirmed = neither = 0
        disagree = []
        for path in sorted(ctx.store.root.glob("cards/*/*/attempt_*/eval.json")):
            ev = json.loads(path.read_text(encoding="utf-8"))
            reason = (ev.get("rule") or {}).get("reason")
            where = f"{path.parent.parent.name} {path.parent.name}"
            if reason == "pick confirmed":
                confirmed += 1
            elif reason == "judges disagree":
                picker = next(j["id"] for j in judges if j["kind"] == "group")
                confirmer = next(j["id"] for j in judges if j["kind"] == "absolute")
                disagree.append(
                    f"{where}: {picker} принимает {ev['rule'].get('picker_accepts')}, "
                    f"{confirmer} принимает {ev['rule'].get('confirmer_accepts')}"
                )
            elif reason == "no candidate accepted":
                neither += 1
        out["pair"] = {
            "n": len(shared),
            "same": sum(p == q for p, q in zip(x, y, strict=True)),
            "kappa": cohen_kappa(x, y),
            "confirmed": confirmed,
            "disagree": disagree,
            "neither": neither,
        }
    return out


def export(ws: Workspace, run_id: str, runtime: Runtime | None = None) -> dict:
    """(Re)write sheets, framed previews and the report for the current state."""
    ctx = _context(ws, run_id, runtime)
    if not ctx.store.exists("plan/variants.json"):
        raise ServiceError(f"{run_id} has no plan yet")
    return export_artifacts(ctx)


def status(ws: Workspace, run_id: str) -> dict:
    store = RunStore(ws.run_dir(run_id))
    m = store.manifest()
    by_state: dict[str, int] = {}
    for card in m.cards.values():
        by_state[card.state] = by_state.get(card.state, 0) + 1
    out = {
        "run_id": run_id,
        "run_dir": str(store.root),
        "state": m.state,
        "reason": m.status_reason,
        "cards": by_state,
        "ip_check": {v: c.status for v, c in m.ip_check.items()},
    }
    if m.state == "awaiting_agent":
        out["pending_tasks"] = m.counters.get("pending_tasks", [])
    if m.state in ("awaiting_g1", "awaiting_g2"):
        rel = G1 if m.state == "awaiting_g1" else G2
        gate = store.read_json(rel)
        key = "variant_id" if rel == G1 else "card_id"
        out["waiting"] = [i[key] for i in gate["items"] if i[key] not in gate["decisions"]]
        out["gate_file"] = str(store.path(rel))
    if store.exists("costs.json"):
        calls = store.read_json("costs.json")["calls"]
        out["cost_usd"] = round(
            sum(
                c["actual_usd"] if c["actual_usd"] is not None else c["estimate_usd"] for c in calls
            ),
            6,
        )
    if store.exists("report.md"):
        out["report"] = str(store.path("report.md"))
    return out


DEMO_JUDGE_SCRIPT = {"v01_c02": ["fix", "accept"], "v02_c05": ["flag", "accept"]}


def run_demo(ws: Workspace, *, run_id: str | None = None, source: str = "synthetic") -> dict:
    """`cardforge demo`: the whole cycle offline on FakeBackend, fake LLM and fake judge.

    Gates run in mode `none` because a demo has no human; `ip_check` therefore stays
    `pending`. One card goes through a judge fix round, one through a style flag.
    """
    started = time.monotonic()
    request = CollectionRequest(
        request_id="demo",
        collection_theme="Кино",
        set_theme="Вестерн",
        producer_wishes_raw="побольше ретро, без оружия",
        render_profile="fake",
        hitl={"g1": "none", "g2": "none"},
    )
    rid = create_run(
        ws,
        request,
        llm="fake",
        render="fake",
        judge="fake",
        run_id=run_id or "demo-" + datetime.now(UTC).strftime("%Y%m%d-%H%M%S"),
        runtime_options={
            "fake_backend": {"source": source},
            "fake_judge": {"judge_script": DEMO_JUDGE_SCRIPT},
        },
    )
    result = advance(ws, rid)
    result["elapsed_s"] = round(time.monotonic() - started, 2)
    return result


def summarize_events(ws: Workspace, run_id: str) -> dict:
    """Attempts per card from the journal (used by resume checks)."""
    counts: dict[str, int] = {}
    for e in RunStore(ws.run_dir(run_id)).events():
        if e.type == "render_started" and e.card_id:
            counts[e.card_id] = counts.get(e.card_id, 0) + 1
    return counts


def dumps(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)
