from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

from cardforge.doctor import collect
from cardforge.smoke import preflight

app = typer.Typer(no_args_is_help=True, help="CardForge environment and reference catalog tools.")


def emit(payload: str, output: Path | None) -> None:
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(payload + "\n", encoding="utf-8")
    typer.echo(payload)


@app.command()
def doctor(
    root: Annotated[Path, typer.Option(help="Project folder")] = Path("."),
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable report")] = False,
    output: Annotated[Path | None, typer.Option(help="Save the same report to a file")] = None,
    offline: Annotated[
        bool, typer.Option("--offline", help="Skip the OpenRouter route probes (no network)")
    ] = False,
) -> None:
    """Check the environment; with a key and `[doctor] route_probe`, probe the OpenRouter routes
    (one ≈ $0 request each, journaled). No GPU initialization."""
    report = collect(root, probe=False if offline else None)
    if as_json:
        payload = report.model_dump_json(indent=2)
    else:
        plugin = next(
            (c.detail.split(":")[0] for c in report.checks if c.name == "plugin_mode"), "?"
        )
        errors = sum(c.status == "error" for c in report.checks)
        rows = [
            f"CardForge doctor: plugin mode {plugin}; "
            + (f"{errors} error(s) — fix the [ERROR] lines" if errors else "no errors")
        ]
        from cardforge.mcp.tools import DEV_CHECKS

        rows.extend(f"[{c.status.upper()}] {c.name}: {c.detail}" for c in report.checks
                    if c.name not in DEV_CHECKS)
        dev = [c for c in report.checks if c.name in DEV_CHECKS]
        if dev:
            rows.append(f"(development checks, not used by the plugin: {report.mode}; "
                        + ", ".join(f"{c.name} {c.status}" for c in dev) + ")")
        payload = "\n".join(rows)
    emit(payload, output)
    raise typer.Exit(1 if report.has_errors else 0)


@app.command()
def smoke_preflight(
    root: Annotated[Path, typer.Option(help="Project folder")] = Path("."),
    output: Annotated[Path | None, typer.Option()] = None,
) -> None:
    """List smoke prerequisites. Does not execute generation or download weights."""
    payload = preflight(root)
    emit(json.dumps(payload, ensure_ascii=False, indent=2), output)
    raise typer.Exit(1 if any(c["status"] == "blocked" for c in payload["checks"]) else 0)


catalog_app = typer.Typer(no_args_is_help=True, help="Reference catalog and human review")
app.add_typer(catalog_app, name="catalog")

eval_app = typer.Typer(no_args_is_help=True, help="Offline evaluation and judge calibration")
app.add_typer(eval_app, name="eval")


def eval_run(operation, output: Path | None = None, *, require_pass: bool = False) -> None:
    try:
        report = operation()
    except (ValueError, OSError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    emit(json.dumps(report, ensure_ascii=False, indent=2), output)
    if require_pass and not report.get("runtime_allowed"):
        raise typer.Exit(1)


@eval_app.command("calibrate-checks")
def eval_checks(root: Path = Path(".")) -> None:
    """Measure all 130 object references; never tune thresholds to acceptance."""
    from cardforge.evaluate.hard_checks import calibrate_checks

    eval_run(lambda: calibrate_checks(root))


@eval_app.command("init-calibration")
def eval_init(root: Path = Path(".")) -> None:
    """Create reference controls only; foreign and human borderline labels remain pending."""
    from cardforge.evaluate.judge_pack import initialize_calibration

    eval_run(lambda: initialize_calibration(root))


@eval_app.command("check")
def eval_check(
    image: Path,
    mask: Path,
    category: int,
    root: Path = Path("."),
    mask_quality: str = "unreviewed",
    allowed_text: list[str] | None = None,
) -> None:
    """Check one image with an explicit segmentation mask; unreviewed masks need review."""
    from cardforge.evaluate.hard_checks import check_image

    def operation():
        result = check_image(root, image, mask, category, mask_quality, allowed_text)
        result["runtime_allowed"] = result["decision"] == "pass"
        return result

    eval_run(operation, require_pass=True)


@eval_app.command("score-style")
def eval_score_style(image: Path, root: Path = Path("."), metric: str | None = None) -> None:
    """Measure centroid and five-neighbour distances; never decides acceptance alone."""
    from cardforge.evaluate.style_metric import score_image

    eval_run(lambda: score_image(root, image, metric))


@eval_app.command("build-style-index")
def eval_style(root: Path = Path(".")) -> None:
    """Run DINOv2 on CPU using previously downloaded, pinned local files."""
    from cardforge.evaluate.style_metric import build_index

    eval_run(lambda: build_index(root))


@eval_app.command("calibrate-style")
def eval_style_calibrate(
    root: Path = Path("."), output: Path | None = None, metric: str | None = None
) -> None:
    """Compute DINO calibration or validate the cached CSD experiment against current inputs."""
    import tomllib

    from cardforge.evaluate.style_metric import calibrate_style

    config = root / "config/evaluation.toml"
    selected = metric or (
        tomllib.loads(config.read_text())["style"]["primary"] if config.exists() else "dinov2"
    )
    eval_run(lambda: calibrate_style(root, selected), output)


@eval_app.command("export-judge-pack")
def eval_export(
    manifest: Path,
    output: Path,
    root: Path = Path("."),
    seed: int = 42,
    limit: int | None = None,
    working_family: str | None = None,
    reference_policy: str = "roles-v1",
) -> None:
    """Export anonymized images/specs and references for style, category and materials."""
    from cardforge.evaluate.judge_pack import export_pack

    eval_run(
        lambda: export_pack(
            root,
            manifest,
            output,
            seed=seed,
            limit=limit,
            working_family=working_family,
            reference_policy=reference_policy,
        )
    )


@eval_app.command("import-judge-pack")
def eval_import(pack: Path, answers: Path, root: Path = Path(".")) -> None:
    """Require complete answers and unchanged inputs; record the actual judge identity."""
    from cardforge.evaluate.judge_pack import import_pack

    eval_run(lambda: import_pack(root, pack, answers))


@eval_app.command("import-labels")
def eval_labels(source: Path, root: Path = Path(".")) -> None:
    """Import explicitly supplied human calibration labels with source SHA validation."""
    from cardforge.evaluate.judge_pack import import_labels

    eval_run(lambda: import_labels(root, source))


@eval_app.command("calibrate")
def eval_calibrate(
    judge: Annotated[str, typer.Option(help="provider:host:model")],
    root: Path = Path("."),
    output: Path | None = None,
    pack_id: Annotated[
        list[str] | None,
        typer.Option(help="Frozen pack ID; repeat for disjoint packs of the same campaign"),
    ] = None,
    decision_policy: Annotated[
        str | None, typer.Option(help="Override decision policy for an explicit comparison")
    ] = None,
) -> None:
    """Compute judge metrics. Exit nonzero until every runtime calibration gate passes."""
    from cardforge.evaluate.calibration import calibrate, select_working_judges

    def selected_report():
        if pack_id:
            selection = {"pack_id": pack_id[0]} if len(pack_id) == 1 else {"pack_ids": pack_id}
            if decision_policy is not None:
                selection["decision_policy"] = decision_policy
            return calibrate(root, judge, **selection)
        profiles = [p for p in select_working_judges(root)["profiles"] if p["judge_id"] == judge]
        if len(profiles) == 1:
            selected = dict(profiles[0])
            if decision_policy is not None:
                selected["decision_policy"] = decision_policy
            return calibrate(root, **selected)
        return calibrate(root, judge, decision_policy=decision_policy or "flag-first")

    eval_run(selected_report, output, require_pass=True)


@eval_app.command("export-blind-test")
def eval_blind_export(
    manifest: Path,
    output: Path,
    root: Path = Path("."),
    seed: int = 20261101,
    all_real: bool = True,
    sheets: int = 10,
    working_family: str | None = None,
) -> None:
    """Create ten 5x2 sheets; all-real control by default, mixed via --no-all-real."""
    from cardforge.evaluate.blind_test import export_blind

    eval_run(
        lambda: export_blind(
            root,
            manifest,
            output,
            seed=seed,
            all_real=all_real,
            sheets=sheets,
            working_family=working_family,
        )
    )


@eval_app.command("score-blind-test")
def eval_blind_score(
    pack: Path, response: Path, root: Path = Path("."), output: Path | None = None
) -> None:
    """Score a completed blind control against the exact hypergeometric null."""
    from cardforge.evaluate.blind_test import score_blind

    eval_run(lambda: score_blind(root, pack, response), output)


@eval_app.command("autonomy-readiness")
def eval_autonomy_readiness(root: Path = Path("."), output: Path | None = None) -> None:
    """Stage 11: autonomy conditions (TA §12.3) from all labelled runs. Local, no network, $0."""
    from cardforge.evaluate.autonomy_readiness import autonomy_readiness

    eval_run(lambda: autonomy_readiness(root), output)


@catalog_app.command("init")
def catalog_init(root: Path = Path("."), output: Path | None = None) -> None:
    """Create numeric-order drafts; never overwrites existing annotations."""
    from cardforge.knowledge.catalog import initialize

    try:
        n = initialize(root, output or root / "knowledge/catalog/cards.jsonl")
    except (ValueError, OSError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"Created {n} draft records; semantic annotation still required.")


@catalog_app.command("validate")
def catalog_validate(
    root: Path = Path("."),
    require_human: bool = False,
    output: Path | None = None,
) -> None:
    """Validate data; --require-human also checks all category/theme confirmations."""
    from cardforge.knowledge.catalog import validate

    report = validate(root, require_human=require_human)
    emit(json.dumps(report, ensure_ascii=False, indent=2), output)
    raise typer.Exit(0 if report["valid"] else 1)


@catalog_app.command("review")
def catalog_review(root: Path = Path("."), output: Path | None = None) -> None:
    """Build a private offline contact-sheet review form; does not approve cards."""
    from cardforge.knowledge.review import build_review

    path = output or root / "dataset/analysis/review.html"
    build_review(root, path)
    typer.echo(str(path.resolve()))


@catalog_app.command("import-review")
def catalog_import_review(path: Path, root: Path = Path(".")) -> None:
    """Validate and import explicitly supplied human decisions."""
    from cardforge.knowledge.review import import_reviews

    try:
        n = import_reviews(root, path)
    except (ValueError, OSError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"Imported {n} human decisions.")


@catalog_app.command("check-grammar")
def catalog_check_grammar(root: Path = Path(".")) -> None:
    """Run the versioned grammar on all historical reference sets."""
    from cardforge.knowledge.catalog import load_cards
    from cardforge.knowledge.grammar import check_set

    cards = load_cards(root)
    grammar = json.loads((root / "knowledge/set_grammar/v1.yaml").read_text())
    results = [
        {"set_index": s, **check_set([c for c in cards if c.set_index == s], grammar)}
        for s in range(1, 17)
    ]
    typer.echo(json.dumps(results, ensure_ascii=False, indent=2))
    raise typer.Exit(0 if all(r["valid"] for r in results) else 1)


policy_app = typer.Typer(no_args_is_help=True, help="Policy packs: IP baseline and culture")
app.add_typer(policy_app, name="policy")


@policy_app.command("lint")
def policy_lint(
    paths: Annotated[list[Path] | None, typer.Argument(help="Pack files; default all")] = None,
    root: Path = Path("."),
) -> None:
    """Validate pack format and rule invariants (TA §9.2)."""
    from cardforge.policy.loader import lint

    report = lint(root, paths)
    emit(json.dumps(report, ensure_ascii=False, indent=2), None)
    raise typer.Exit(0 if report["ok"] else 1)


@policy_app.command("active")
def policy_active(
    markets: Annotated[list[str] | None, typer.Option("--market")] = None,
    packs: Annotated[list[str] | None, typer.Option("--pack")] = None,
    root: Path = Path("."),
) -> None:
    """Show packs a request would activate; the IP baseline is always first."""
    from cardforge.policy.loader import resolve_active

    active = resolve_active(root, markets or [], packs or [])
    rows = [
        {"policy_id": p.policy_id, "version": p.policy_version, "rules": len(p.rules)}
        for p in active
    ]
    emit(json.dumps(rows, ensure_ascii=False, indent=2), None)


@policy_app.command("start")
def policy_start(
    run: Annotated[Path, typer.Argument(help="New run folder")],
    collection: Annotated[str, typer.Option("--collection")],
    set_theme: Annotated[str, typer.Option("--set-theme")],
    wishes: Annotated[str, typer.Option("--wishes")] = "",
    markets: Annotated[list[str] | None, typer.Option("--market")] = None,
    packs: Annotated[list[str] | None, typer.Option("--pack")] = None,
    variants: Annotated[int, typer.Option(min=3, max=5)] = 3,
    g1: str = "all",
    g2: str = "flagged",
    seed: int = 0,
    root: Path = Path("."),
) -> None:
    """Write request.json with policy conflicts and the host-agent planning task."""
    from cardforge.contracts.request import CollectionRequest, Hitl
    from cardforge.policy.service import start

    request = CollectionRequest(
        request_id=run.name,
        collection_theme=collection,
        set_theme=set_theme,
        producer_wishes_raw=wishes,
        variants_count=variants,
        markets=[m.upper() for m in markets or []],
        policy_packs=packs or [],
        hitl=Hitl(g1=g1, g2=g2),
        seed=seed,
    )
    emit(json.dumps(start(root, run, request), ensure_ascii=False, indent=2), None)


@policy_app.command("screen")
def policy_screen(run: Path, root: Path = Path(".")) -> None:
    """Screen the host-agent plan; exit 2 while waiting for an agent answer, 1 on errors."""
    from cardforge.policy.service import screen

    try:
        result = screen(root, run)
    except (ValueError, OSError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    emit(json.dumps(result, ensure_ascii=False, indent=2), None)
    raise typer.Exit({"planned": 0, "awaiting_agent": 2}.get(result["state"], 1))


@policy_app.command("report")
def policy_report(run: Path, root: Path = Path(".")) -> None:
    """Re-render report.md with the «Политики» and «Авторские права» sections."""
    from cardforge.policy.service import report

    typer.echo(str(report(root, run)))


@policy_app.command("confirm-ip")
def policy_confirm_ip(
    run: Path,
    variant: Annotated[str, typer.Option("--variant")],
    reviewer: Annotated[str, typer.Option("--reviewer", help="Who answered")],
    human_answer: Annotated[
        str | None, typer.Option("--human-answer", help="The human's own words, verbatim")
    ] = None,
    reject: bool = False,
    root: Path = Path("."),
) -> None:
    """Record the human G3 answer on the copyright checklist (TA §9.8)."""
    from cardforge.orchestrator.store import RunStore
    from cardforge.policy.ip_check import record_human_decision
    from cardforge.policy.service import report

    if human_answer is None:
        human_answer = typer.prompt(
            f"Ответ человека по чек-листу policies/ip/baseline.md для {variant}"
        )
    store = RunStore(run)
    check = record_human_decision(
        store, variant, confirmed=not reject, human_answer=human_answer, reviewer=reviewer
    )
    if store.manifest().render_profile:  # stage-4 run: the core owns the report
        from cardforge.orchestrator.service import export

        export(_workspace(root, run.parent), run.name)
    else:
        report(root, run)
    emit(check.model_dump_json(), None)


# ---------------------------------------------------------------- stage 4: run lifecycle
# These commands only call the core service API (TA §13.4); the core never prints.

GateMode = Annotated[
    str | None,
    typer.Option(help="G1 human|auto, G2 none|flagged|all; default config/autonomy.toml [gates]"),
]


def _workspace(root: Path, runs_dir: Path | None = None):
    from dataclasses import replace

    from cardforge.orchestrator.workspace import Workspace

    ws = Workspace.from_env(root)
    return replace(ws, runs_dir=runs_dir) if runs_dir else ws


def _run_ref(run: str, root: Path, runs_dir: Path | None):
    """Accept a run id or a run folder path."""
    path = Path(run)
    if (path / "manifest.json").exists():
        return _workspace(root, path.parent), path.name
    return _workspace(root, runs_dir), run


def _service_call(operation) -> None:
    from cardforge.orchestrator.service import ServiceError, dumps

    try:
        result = operation()
    except (ServiceError, ValueError, OSError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    emit(dumps(result), None)
    state = result.get("state")
    # 0 done or planned, 2 waiting for a human or the host agent, 1 failed.
    if state in ("awaiting_agent", "awaiting_g1", "awaiting_g2"):
        raise typer.Exit(2)
    if state == "failed":
        raise typer.Exit(1)


def _new_run(
    *,
    collection: str,
    set_theme: str,
    wishes: str,
    markets: list[str] | None,
    packs: list[str] | None,
    variants: int,
    g1: str,
    sets: int | None = None,
    g2: str,
    review_policy: str,
    budget: float,
    max_attempts: int,
    seed: int,
    render: list[str] | None,
    llm: str,
    judge: str | None,
    decider: list[str] | None,
    candidates: int | None,
    render_variants: int | None,
    compare_models: bool,
    shadow_judge: list[str] | None,
    run_id: str | None,
    root: Path,
    runs_dir: Path | None,
    shadow: bool = False,
    render_fallback: bool = True,
):
    from cardforge.autonomy import gate_modes
    from cardforge.contracts.request import Budget, CollectionRequest, Hitl
    from cardforge.orchestrator.service import create_run

    ws = _workspace(root, runs_dir)
    g1, g2 = gate_modes(ws.root, g1, g2)
    request = CollectionRequest(
        request_id=run_id or "new",
        collection_theme=collection,
        set_theme=set_theme,
        producer_wishes_raw=wishes,
        variants_count=min(sets, 15) if sets else variants,
        plan_mode="collection" if sets else "variants",
        sets_total=sets or 16,
        markets=[m.upper() for m in markets or []],
        policy_packs=packs or [],
        render_profile=(render or ["fake"])[0],
        hitl=Hitl(g1=g1, g2=g2, review_policy=review_policy),
        budget=Budget(max_cost_usd=budget, max_attempts_per_card=max_attempts),
        seed=seed,
    )
    return ws, create_run(
        ws,
        request,
        llm=llm,
        render=render or ["fake"],
        judge=judge,
        deciders=decider,
        candidates=candidates,
        render_variant_limit=render_variants,
        compare_models=compare_models,
        shadow_judges=shadow_judge,
        run_id=run_id,
        runtime_options={
            **({"shadow": True} if shadow else {}),
            # G2 = none (user 2026-11-10): unconfirmed best attempts go to G3 with a mark.
            **({"g3_unconfirmed": True} if g2 == "none" else {}),
            **({} if render_fallback else {"render_fallback": False}),
        }
        or None,
    )


@app.command("plan")
def run_plan(
    collection: Annotated[str, typer.Option("--collection", help="Collection theme, e.g. Кино")],
    set_theme: Annotated[str, typer.Option("--set-theme", help="Set theme, e.g. Вестерн")],
    wishes: Annotated[str, typer.Option("--wishes")] = "",
    markets: Annotated[list[str] | None, typer.Option("--market")] = None,
    packs: Annotated[list[str] | None, typer.Option("--pack")] = None,
    variants: Annotated[int, typer.Option(min=3, max=5)] = 3,
    sets: Annotated[
        int | None,
        typer.Option(min=1, max=16, help="Collection plan: number of sets (16 = whole collection)"),
    ] = None,
    g1: GateMode = None,
    g2: GateMode = None,
    review_policy: str = "replace",
    budget: Annotated[float, typer.Option(help="Run ceiling, USD")] = 0.0,
    max_attempts: int = 3,
    seed: int = 0,
    render: Annotated[
        list[str] | None,
        typer.Option(help="Render profile(s), repeatable: one model per set variant"),
    ] = None,
    llm: Annotated[str, typer.Option(help="host-agent | fake")] = "host-agent",
    judge: Annotated[str | None, typer.Option(help="Deciding judge profile; default none")] = None,
    decider: Annotated[
        list[str] | None,
        typer.Option(
            "--decider", help="Deciding batch judge, repeatable (candidate mode, e.g. two)"
        ),
    ] = None,
    candidates: Annotated[int | None, typer.Option(help="Candidates per card position")] = None,
    render_variants: Annotated[
        int | None, typer.Option(help="Render only the first N planned variants (test limit)")
    ] = None,
    compare_models: Annotated[
        bool, typer.Option(help="Draw the first variant once per --render model (comparison)")
    ] = False,
    shadow_judge: Annotated[
        list[str] | None,
        typer.Option("--shadow-judge", help="Extra judge whose answers are only logged"),
    ] = None,
    shadow: Annotated[
        bool,
        typer.Option(
            "--shadow",
            help="Shadow-mode run: auto-reject and GPT auto-pass are only journaled, never act",
        ),
    ] = False,
    render_fallback: Annotated[
        bool,
        typer.Option(
            "--render-fallback/--no-render-fallback",
            help="Move a card to its profile's reserve generator after repeated failures (G1)",
        ),
    ] = True,
    run_id: Annotated[str | None, typer.Option("--run-id")] = None,
    root: Path = Path("."),
    runs_dir: Path | None = None,
) -> None:
    """Create a run and plan it: stops at G1, at a host-agent task or before rendering."""
    from cardforge.orchestrator.service import plan

    def operation():
        ws, rid = _new_run(**locals_)
        return plan(ws, rid)

    locals_ = dict(
        collection=collection, set_theme=set_theme, wishes=wishes, markets=markets, packs=packs,
        variants=variants, sets=sets, g1=g1, g2=g2, review_policy=review_policy, budget=budget,
        max_attempts=max_attempts, seed=seed, render=render, llm=llm, judge=judge,
        decider=decider, candidates=candidates, render_variants=render_variants,
        compare_models=compare_models,
        shadow_judge=shadow_judge, run_id=run_id,
        root=root, runs_dir=runs_dir, shadow=shadow, render_fallback=render_fallback,
    )  # fmt: skip
    _service_call(operation)


@app.command("run")
def run_full(
    collection: Annotated[str, typer.Option("--collection", help="Collection theme, e.g. Кино")],
    set_theme: Annotated[str, typer.Option("--set-theme", help="Set theme, e.g. Вестерн")],
    wishes: Annotated[str, typer.Option("--wishes")] = "",
    markets: Annotated[list[str] | None, typer.Option("--market")] = None,
    packs: Annotated[list[str] | None, typer.Option("--pack")] = None,
    variants: Annotated[int, typer.Option(min=3, max=5)] = 3,
    sets: Annotated[
        int | None,
        typer.Option(min=1, max=16, help="Collection plan: number of sets (16 = whole collection)"),
    ] = None,
    g1: GateMode = None,
    g2: GateMode = None,
    review_policy: str = "replace",
    budget: Annotated[float, typer.Option(help="Run ceiling, USD")] = 0.0,
    max_attempts: int = 3,
    seed: int = 0,
    render: Annotated[
        list[str] | None,
        typer.Option(help="Render profile(s), repeatable: one model per set variant"),
    ] = None,
    llm: Annotated[str, typer.Option(help="host-agent | fake")] = "host-agent",
    judge: Annotated[str | None, typer.Option(help="Deciding judge profile; default none")] = None,
    decider: Annotated[
        list[str] | None,
        typer.Option(
            "--decider", help="Deciding batch judge, repeatable (candidate mode, e.g. two)"
        ),
    ] = None,
    candidates: Annotated[int | None, typer.Option(help="Candidates per card position")] = None,
    render_variants: Annotated[
        int | None, typer.Option(help="Render only the first N planned variants (test limit)")
    ] = None,
    compare_models: Annotated[
        bool, typer.Option(help="Draw the first variant once per --render model (comparison)")
    ] = False,
    shadow_judge: Annotated[
        list[str] | None,
        typer.Option("--shadow-judge", help="Extra judge whose answers are only logged"),
    ] = None,
    shadow: Annotated[
        bool,
        typer.Option(
            "--shadow",
            help="Shadow-mode run: auto-reject and GPT auto-pass are only journaled, never act",
        ),
    ] = False,
    render_fallback: Annotated[
        bool,
        typer.Option(
            "--render-fallback/--no-render-fallback",
            help="Move a card to its profile's reserve generator after repeated failures (G1)",
        ),
    ] = True,
    run_id: Annotated[str | None, typer.Option("--run-id")] = None,
    root: Path = Path("."),
    runs_dir: Path | None = None,
) -> None:
    """Create a run and advance it to the next wait (G1, G2, host agent) or to the end."""
    from cardforge.orchestrator.service import advance

    def operation():
        ws, rid = _new_run(**locals_)
        return advance(ws, rid)

    locals_ = dict(
        collection=collection, set_theme=set_theme, wishes=wishes, markets=markets, packs=packs,
        variants=variants, sets=sets, g1=g1, g2=g2, review_policy=review_policy, budget=budget,
        max_attempts=max_attempts, seed=seed, render=render, llm=llm, judge=judge,
        decider=decider, candidates=candidates, render_variants=render_variants,
        compare_models=compare_models,
        shadow_judge=shadow_judge, run_id=run_id,
        root=root, runs_dir=runs_dir, shadow=shadow, render_fallback=render_fallback,
    )  # fmt: skip
    _service_call(operation)


@app.command("status")
def run_status(run: str, root: Path = Path("."), runs_dir: Path | None = None) -> None:
    """Run and card states, what the run waits for, cost so far."""
    from cardforge.orchestrator.service import status

    ws, rid = _run_ref(run, root, runs_dir)
    _service_call(lambda: status(ws, rid))


@app.command("resume")
def run_resume(run: str, root: Path = Path("."), runs_dir: Path | None = None) -> None:
    """Continue a run after a gate decision, a host-agent answer or an interruption."""
    from cardforge.orchestrator.service import advance

    ws, rid = _run_ref(run, root, runs_dir)
    _service_call(lambda: advance(ws, rid))


@app.command("review")
def run_review(
    run: str,
    gate: Annotated[str, typer.Option("--gate", help="g1 | g2")],
    decide: Annotated[
        list[str] | None,
        typer.Option(
            "--decide",
            help="target=decision; G1: v01=approve|reject; G2: v01_c03=accept|reject|fix: text",
        ),
    ] = None,
    reviewer: Annotated[str | None, typer.Option("--reviewer")] = None,
    from_file: Annotated[
        Path | None, typer.Option("--from-file", help="JSON exported by the review page")
    ] = None,
    root: Path = Path("."),
    runs_dir: Path | None = None,
) -> None:
    """Show what waits at a gate, or record human decisions. Run `resume` afterwards."""
    from cardforge.orchestrator.service import decide_gate

    ws, rid = _run_ref(run, root, runs_dir)
    gate_file = ws.run_dir(rid) / "review" / f"{gate.lower()}.json"
    if from_file:
        data = json.loads(from_file.read_text(encoding="utf-8"))
        who = reviewer or data.get("reviewer") or ""
        if data.get("page_id"):  # blind page: card-NN keys, mapped back on the operator side
            from cardforge.export.review_page import from_blind_export
            from cardforge.orchestrator.store import RunStore

            if data.get("gate", "").lower() != gate.lower():
                typer.echo(f"{from_file} is for gate {data.get('gate')}", err=True)
                raise typer.Exit(1)
            try:
                mapped = from_blind_export(RunStore(ws.run_dir(rid)), data)
            except ValueError as exc:
                typer.echo(f"{from_file}: {exc}", err=True)
                raise typer.Exit(1) from None
            _service_call(
                lambda: decide_gate(
                    ws, rid, gate, mapped["decisions"], who, mapped["notes"], mapped["reasons"],
                    blind=True,
                )
            )  # fmt: skip
            return
        if data.get("run_id") != rid or data.get("gate", "").lower() != gate.lower():
            typer.echo(f"{from_file} is for {data.get('run_id')} {data.get('gate')}", err=True)
            raise typer.Exit(1)
        _service_call(
            lambda: decide_gate(
                ws, rid, gate, data["decisions"], who, data.get("notes"), data.get("reasons")
            )
        )
        return
    if not decide:
        if not gate_file.exists():
            typer.echo(f"gate {gate} is not open", err=True)
            raise typer.Exit(1)
        emit(gate_file.read_text(encoding="utf-8").rstrip(), None)
        return
    if not reviewer:
        typer.echo("--reviewer is required with --decide", err=True)
        raise typer.Exit(1)
    pairs = {}
    for item in decide:
        target, sep, decision = item.partition("=")
        if not sep:
            typer.echo(f"bad --decide {item!r}; expected target=decision", err=True)
            raise typer.Exit(1)
        pairs[target.strip()] = decision
    _service_call(lambda: decide_gate(ws, rid, gate, pairs, reviewer))


@app.command("export")
def run_export(run: str, root: Path = Path("."), runs_dir: Path | None = None) -> None:
    """(Re)write sheets 5×2, framed previews, report.md/html, screened_out and alternatives;
    after the G3 choice (`select`) also the delivery folder `delivery/current/` (versioned)."""
    from cardforge.orchestrator.product import export_run

    ws, rid = _run_ref(run, root, runs_dir)
    _service_call(lambda: export_run(ws, rid))


@app.command("work")
def run_work(run: str, root: Path = Path("."), runs_dir: Path | None = None) -> None:
    """Background worker of the plugin: advance the run to its next wait under the paid lock."""
    from cardforge.orchestrator.product import work

    ws, rid = _run_ref(run, root, runs_dir)
    _service_call(lambda: work(ws, rid))


@app.command("stop")
def run_stop(run: str, root: Path = Path("."), runs_dir: Path | None = None) -> None:
    """Stop this run after the current call (file STOP in the run folder); `resume` continues."""
    from cardforge.orchestrator.product import stop

    ws, rid = _run_ref(run, root, runs_dir)
    _service_call(lambda: stop(ws, rid))


@app.command("edit-plan")
def run_edit_plan(
    run: str,
    note: Annotated[str, typer.Option("--note", help="The person's plan edit, verbatim")],
    reviewer: Annotated[str, typer.Option("--reviewer")],
    root: Path = Path("."),
    runs_dir: Path | None = None,
) -> None:
    """G1: send the plan back to repair with the person's edit; G1 opens again afterwards."""
    from cardforge.orchestrator.product import edit_plan

    ws, rid = _run_ref(run, root, runs_dir)
    _service_call(lambda: edit_plan(ws, rid, note, reviewer))


@app.command("select")
def run_select(
    run: str,
    variant: Annotated[list[str], typer.Option("--variant", help="Chosen variant, repeatable")],
    reviewer: Annotated[str, typer.Option("--reviewer")],
    human_answer: Annotated[str, typer.Option("--human-answer", help="The person's words")],
    root: Path = Path("."),
    runs_dir: Path | None = None,
) -> None:
    """G3: record the person's choice of the final variant(s) and build the delivery folder."""
    from cardforge.orchestrator.product import select_variants

    ws, rid = _run_ref(run, root, runs_dir)
    _service_call(lambda: select_variants(ws, rid, variant, reviewer, human_answer))


@app.command("regenerate")
def run_regenerate(
    run: str,
    card: Annotated[list[str], typer.Option("--card", help="Card to redraw, e.g. v03_c04")],
    note: Annotated[str, typer.Option("--note", help="The person's edit, verbatim")],
    reviewer: Annotated[str, typer.Option("--reviewer")],
    root: Path = Path("."),
    runs_dir: Path | None = None,
) -> None:
    """Redraw cards with the person's edit (also after export); the redraw goes to the person
    at G2; run `export` after the decision to rebuild the delivery."""
    from cardforge.orchestrator.product import regenerate

    ws, rid = _run_ref(run, root, runs_dir)
    _service_call(lambda: regenerate(ws, rid, card, note, reviewer))


@app.command("fetch-assets")
def run_fetch_assets(
    root: Path = Path("."),
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Only list what is missing")] = False,
    source: Annotated[
        Path | None, typer.Option("--from", help="Copy matching files from this models/ folder")
    ] = None,
) -> None:
    """Download the core's weights (CSD ViT-L, u2net) by models/*/source.json, sha256-checked."""
    from cardforge.assets import fetch

    report = fetch(root, dry_run=dry_run, source=source, log=lambda m: typer.echo(m, err=True))
    emit(json.dumps(report, ensure_ascii=False, indent=2), None)
    if report["failed"]:
        raise typer.Exit(1)


@app.command("mcp")
def run_mcp(
    root: Path = Path("."),
    runs_dir: Path | None = None,
) -> None:
    """MCP server over stdio for Claude Code and Codex (the CardForge plugin)."""
    from cardforge.mcp.server import serve_stdio

    serve_stdio(_workspace(root, runs_dir))


@app.command("demo")
def run_demo(
    root: Path = Path("."),
    runs_dir: Path | None = None,
    run_id: Annotated[str | None, typer.Option("--run-id")] = None,
    source: Annotated[str, typer.Option(help="synthetic | dataset | auto")] = "synthetic",
) -> None:
    """Full offline cycle on FakeBackend with the fake LLM and fake judge (no network)."""
    from cardforge.orchestrator.service import run_demo as demo

    ws = _workspace(root, runs_dir)
    _service_call(lambda: demo(ws, run_id=run_id, source=source))


@app.command("retry-failed")
def run_retry_failed(
    run: str,
    reason: Annotated[str, typer.Option("--reason", help="Why the cause is fixed now")],
    root: Path = Path("."),
    runs_dir: Path | None = None,
) -> None:
    """Send backend-failed cards back to rendering; run `resume` afterwards."""
    from cardforge.orchestrator.service import retry_failed

    ws, rid = _run_ref(run, root, runs_dir)
    _service_call(lambda: retry_failed(ws, rid, reason))


@app.command("review-page")
def run_review_page(
    run: str,
    open_page: Annotated[
        bool, typer.Option("--open", help="show model, run and card ids (operator page)")
    ] = False,
    root: Path = Path("."),
    runs_dir: Path | None = None,
) -> None:
    """Write a local HTML page for the G2 review (stays next to the run, never published).
    Blind by default: no model, run or card ids; reason-code checkboxes per card."""
    from cardforge.export.review_page import write_review_page
    from cardforge.orchestrator.store import RunStore

    ws, rid = _run_ref(run, root, runs_dir)
    store = RunStore(ws.run_dir(rid))
    if not store.exists("review/g2.json"):
        typer.echo("gate G2 is not open for this run", err=True)
        raise typer.Exit(1)
    emit(str(write_review_page(store, blind=not open_page, root=ws.root)), None)


@app.command("g3-return")
def g3_return_cmd(
    run: Annotated[str, typer.Argument(help="Run id")],
    card: Annotated[str, typer.Option("--card", help="Auto-passed card to return, e.g. v01_c03")],
    reason: Annotated[str, typer.Option("--reason", help="Why the card is returned")],
    reviewer: Annotated[str, typer.Option("--reviewer", help="Who returns it")],
    root: Path = Path("."),
    runs_dir: Path | None = None,
) -> None:
    """G3: return an auto-passed card from the set sheet; it goes back to the human G2."""
    from cardforge.orchestrator.service import g3_return

    ws = _workspace(root, runs_dir)
    _service_call(lambda: g3_return(ws, run, card, reason, reviewer))


@app.command("card-reopen")
def card_reopen_cmd(
    run: Annotated[str, typer.Argument(help="Run id or run folder")],
    card: Annotated[str, typer.Option("--card", help="Rejected or accepted card, e.g. v02_c04")],
    reason: Annotated[str, typer.Option("--reason", help="Why the card is reopened")],
    reviewer: Annotated[str, typer.Option("--reviewer", help="Who reopens it")],
    root: Path = Path("."),
    runs_dir: Path | None = None,
) -> None:
    """Bring a rejected or accepted card back to G2 with its last image (no new call); run
    `resume`, then decide it at G2. A confirmed ip_check of its variant goes back to pending."""
    from cardforge.orchestrator.service import card_reopen

    ws, rid = _run_ref(run, root, runs_dir)
    _service_call(lambda: card_reopen(ws, rid, card, reason, reviewer))


@app.command("reopen")
def run_reopen(
    run: str,
    reason: Annotated[str, typer.Option("--reason", help="What changed since it stopped")],
    root: Path = Path("."),
    runs_dir: Path | None = None,
) -> None:
    """Continue a partial run (budget raised, fixes pending); run `resume` afterwards."""
    from cardforge.orchestrator.service import reopen

    ws, rid = _run_ref(run, root, runs_dir)
    _service_call(lambda: reopen(ws, rid, reason))


@app.command("budget")
def run_budget(
    run: str,
    max_cost: Annotated[float, typer.Option("--max-cost", help="New run ceiling, USD")],
    reason: Annotated[str, typer.Option("--reason")],
    max_attempts: Annotated[
        int | None, typer.Option("--max-attempts", help="New attempt limit per card")
    ] = None,
    root: Path = Path("."),
    runs_dir: Path | None = None,
) -> None:
    """Change a run's budget ceiling (journaled; paid runs stay within the API ledger)."""
    from cardforge.orchestrator.service import set_budget

    ws, rid = _run_ref(run, root, runs_dir)
    _service_call(lambda: set_budget(ws, rid, max_cost, reason, max_attempts))
