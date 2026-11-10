# ruff: noqa: E501  (JSON-RPC fixtures)
"""Stage 7 plugin flow (docs/stages/07-plugin.md): designer service and MCP stdio, offline on
FakeBackend with the fake LLM. No network, no paid calls."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from cardforge.mcp import server, tools
from cardforge.orchestrator import product, service
from cardforge.orchestrator.store import RunStore
from cardforge.orchestrator.workspace import Workspace

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def ws(tmp_path, monkeypatch) -> Workspace:
    monkeypatch.setenv("CARDFORGE_WORKER", "inline")
    return Workspace(root=ROOT, runs_dir=tmp_path / "runs", refs_dir=Path("dataset"))


def start(ws, rid="p1", **kw):
    """Development flow: 3 variants of one set, G2 = flagged (per-run overrides of the defaults)."""
    kw.setdefault("g2", "flagged")
    kw.setdefault("plan_mode", "variants")
    kw.setdefault("set_theme", "Вестерн")
    return product.start(ws, "Кино", wishes="побольше ретро", run_id=rid, llm="fake",
                         render="fake", **kw)  # fmt: skip


def accept_all(ws, rid):
    for _ in range(5):
        v = product.view(ws, rid)
        if v["next"] != "show_flags":
            return v
        cards = product.flags(ws, rid)["cards"]
        product.decide_cards(ws, rid, "R1", {c["card_id"]: "accept" for c in cards})
    return product.view(ws, rid)


def test_g1_is_shown_without_cost_and_edit_goes_through_repair(ws, monkeypatch):
    from cardforge.planner import planner

    seen: list[dict] = []
    real = planner.plan_task

    def spy(*args, **kw):
        seen.append(real(*args, **kw))
        return seen[-1]

    monkeypatch.setattr(planner, "plan_task", spy)
    v = start(ws)
    assert v["state"] == "awaiting_g1" and v["next"] == "show_plan"
    assert "cost_usd" not in v and "ip_check" not in v
    plan = product.plan_view(ws, "p1")
    assert len(plan["variants"]) == 3 and len(plan["variants"][0]["cards"]) == 10
    assert "cost" not in json.dumps(plan)
    store = RunStore(ws.run_dir("p1"))
    rounds = len(list(store.path("plan/rounds").glob("round_*.json")))
    v = product.edit_plan(ws, "p1", "в v02 вместо торта бокалы", "R1")
    assert v["state"] == "awaiting_g1"  # the fake planner answered the repair at once
    assert len(list(store.path("plan/rounds").glob("round_*.json"))) == rounds + 1
    task = seen[-1]
    assert task["step"] == "repair" and task["human_edits"] == ["в v02 вместо торта бокалы"]
    assert "human_edits" in task["instruction"]
    assert product.plan_view(ws, "p1")["edits_so_far"] == ["в v02 вместо торта бокалы"]
    edit = [e for e in store.events() if e.type == "human_decision"][-1].payload
    assert edit["decision"] == "edit" and edit["human_answer"] == "в v02 вместо торта бокалы"


def test_plan_only_without_render_key_never_renders(ws, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setattr(product, "render_key_present", lambda root: False)
    v = product.start(ws, "Лето", sets=2, run_id="po", llm="fake")
    assert v["plan_only"] and v["state"] == "awaiting_g1"
    out = product.approve_plan(ws, "po", "R1", "запускай")
    assert "Картинок не будет" in out["message"]
    store = RunStore(ws.run_dir("po"))
    assert not any(e.type == "render_started" for e in store.events())


def test_full_flow_select_delivery_and_regenerate(ws):
    start(ws)
    v = product.approve_plan(ws, "p1", "R1", "запускай")
    assert v["next"] == "show_flags" and not v["worker_running"]
    flags = product.flags(ws, "p1")
    assert flags["cards"] and all(c["why"] for c in flags["cards"])
    v = accept_all(ws, "p1")
    assert v["state"] == "completed" and v["next"] == "choose_variant"
    sheets = product.sheets(ws, "p1")
    assert [s["complete"] for s in sheets["variants"]] == [True, True, True]
    with pytest.raises(service.ServiceError, match="слова"):
        product.select_variants(ws, "p1", ["v02"], "R1", " ")
    out = product.select_variants(ws, "p1", ["v02"], "R1", "берём второй")
    current = Path(out["delivery"])
    assert (current / "index.html").exists() and len(list((current / "v02/png").glob("*.png"))) == 10
    assert (current / "v02/sheet-5x2.png").exists()
    store = RunStore(ws.run_dir("p1"))
    so = store.read_json("screened_out.json")
    assert so["final_variants"] == ["v02"] and len(so["screened_out"]) >= 20
    assert "## v02" in store.path("alternatives.md").read_text(encoding="utf-8")
    assert product.view(ws, "p1")["next"] == "done"
    # regenerate after export: one attempt above the limit, always back to the person
    card = store.manifest().cards["v02_c03"]
    before = card.attempts
    v = product.regenerate(ws, "p1", ["v02_c03"], "без надписей, ручка короче", "R1")
    m = store.manifest()
    assert m.cards["v02_c03"].attempts == before + 1
    assert "v02_c03" in m.counters["human_only"]
    assert v["next"] == "show_flags"
    item = next(c for c in product.flags(ws, "p1")["cards"] if c["card_id"] == "v02_c03")
    assert item["regenerated_on_request"]
    entries = m.counters["fix"]["v02_c03"]["entries"]
    assert entries[-1]["text"] == "без надписей, ручка короче"
    out = product.export_run(ws, "p1")
    assert out["waiting_variants"] == ["v02"]
    accept_all(ws, "p1")
    out = product.export_run(ws, "p1")
    assert out["previous_version"] == "v01" and (current.parent / "v01/index.html").exists()
    cards = json.loads((current / "v02/cards.json").read_text(encoding="utf-8"))["cards"]
    assert cards[2]["source"].endswith(f"attempt_{before + 1:02d}/final.png")


def test_stop_and_resume(ws, monkeypatch):
    start(ws)
    monkeypatch.setenv("CARDFORGE_WORKER", "none")  # not inline: we call work() ourselves
    monkeypatch.setattr(product, "_start_worker", lambda ws, rid: None)
    product.approve_plan(ws, "p1", "R1", "запускай")
    product.stop(ws, "p1")
    store = RunStore(ws.run_dir("p1"))
    result = product.work(ws, "p1")
    assert result["state"] in ("awaiting_g1", "rendering")
    assert not any(e.type == "render_started" for e in store.events())
    assert product.view(ws, "p1")["next"] == "stopped"
    product.resume(ws, "p1")
    assert not store.exists("STOP")
    assert product.work(ws, "p1")["state"] == "awaiting_g2"


def test_regenerate_refuses_while_planning(ws):
    start(ws)
    with pytest.raises(service.ServiceError, match="дождись"):
        product.regenerate(ws, "p1", ["v01_c01"], "x", "R1")


def rpc(ws, *msgs) -> list[dict]:
    lines = "\n".join(json.dumps(m, ensure_ascii=False) for m in msgs) + "\n"
    out = io.StringIO()
    server.serve_stdio(ws, io.StringIO(lines), out)
    return [json.loads(x) for x in out.getvalue().splitlines()]


def test_mcp_stdio_initialize_list_and_flow(ws, monkeypatch):
    real = product.start
    monkeypatch.setattr(product, "start", lambda ws, theme, **kw: real(
        ws, theme, run_id="p1", llm="fake", render="fake", g2="flagged", sets=3))
    replies = rpc(
        ws,
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "start_run", "arguments": {"collection": "Лето"}}},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "approve_plan", "arguments": {"run_id": "p1", "reviewer": "R1", "human_answer": "запускай"}}},
        {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "get_flags", "arguments": {"run_id": "p1"}}},
        {"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": {"name": "select_variant", "arguments": {"run_id": "p1", "variants": ["v01"], "reviewer": "R1", "human_answer": "v01"}}},
        {"jsonrpc": "2.0", "id": 7, "method": "nope"},
    )  # fmt: skip
    assert [r["id"] for r in replies] == [1, 2, 3, 4, 5, 6, 7]
    assert replies[0]["result"]["protocolVersion"] == "2025-06-18"
    names = {t["name"] for t in replies[1]["result"]["tools"]}
    assert {"start_run", "edit_plan", "get_flags", "select_variant", "regenerate_cards"} <= names
    assert not names & {"set_budget", "confirm_ip"}
    assert replies[2]["result"]["structuredContent"]["next"] == "show_plan"
    flags = replies[4]["result"]
    images = [c for c in flags["content"] if c["type"] == "image"]
    assert flags["structuredContent"]["in_chat"] is False or images
    assert replies[5]["result"]["isError"] is True  # v01 still has cards at G2
    assert replies[6]["error"]["code"] == -32601


def test_tool_images_are_small_pngs(ws):
    start(ws)
    product.approve_plan(ws, "p1", "R1", "запускай")
    accept_all(ws, "p1")
    res = tools.call(ws, "get_sheets", {"run_id": "p1"})
    imgs = [c for c in res["content"] if c["type"] == "image"]
    assert len(imgs) == 3 and all(c["mimeType"] == "image/png" for c in imgs)


# ---------------------------------------------------------------- packaging


PLUGIN = ROOT / "plugin"


def test_plugin_manifests_and_skill():
    for rel in (".claude-plugin/plugin.json", ".codex-plugin/plugin.json", ".mcp.json", ".mcp.codex.json"):
        data = json.loads((PLUGIN / rel).read_text(encoding="utf-8"))
        assert data
    market = json.loads((ROOT / ".claude-plugin/marketplace.json").read_text(encoding="utf-8"))
    assert market["plugins"][0]["source"] == "./plugin"
    # Codex: marketplace in .agents/plugins, local source object; MCP command inside the plugin
    # (Codex runs it from its plugin cache), long tool timeout for exports and redraws.
    codex_market = json.loads((ROOT / ".agents/plugins/marketplace.json").read_text(encoding="utf-8"))
    assert codex_market["plugins"][0]["source"] == {"source": "local", "path": "./plugin"}
    manifest = json.loads((PLUGIN / ".codex-plugin/plugin.json").read_text(encoding="utf-8"))
    assert manifest["skills"] == "./skills/" and manifest["mcpServers"] == "./.mcp.codex.json"
    server = json.loads((PLUGIN / ".mcp.codex.json").read_text(encoding="utf-8"))["mcpServers"]["cardforge"]
    assert server["command"] == "./bin/cardforge-mcp" and server["cwd"] == "."
    assert server["tool_timeout_sec"] >= 600
    skill = (PLUGIN / "skills/cardforge/SKILL.md").read_text(encoding="utf-8")
    assert skill.startswith("---\nname: cardforge\n")
    for word in ("Не пишешь промпты рендера", "Не говоришь о стоимости", "Не показываешь чек-лист"):
        assert word in skill
    names = {t.name for t in tools.TOOLS}
    used = {n for n in names if f"`{n}" in skill}
    dev_only = {"select_variant"}  # variants mode (development); the collection plan has no choice
    assert used | dev_only | {"doctor"} >= names
    commands = {p.stem for p in (PLUGIN / "commands").glob("*.md")}
    assert commands == {"new", "status", "review", "finish", "redo", "doctor"}
    assert (PLUGIN / "bin/cardforge-mcp").stat().st_mode & 0o111


def test_codex_and_claude_agent_adapters_are_in_sync():
    """One compliance-reviewer for both agents: the plugin copy equals agents/ (TA §13.1)."""
    src = (ROOT / "agents/compliance-reviewer.md").read_text(encoding="utf-8")
    assert (PLUGIN / "agents/compliance-reviewer.md").read_text(encoding="utf-8") == src


def test_doctor_reports_plugin_mode():
    from cardforge.doctor import plugin_check

    assert plugin_check(True).status == "ok"
    off = plugin_check(False)
    assert off.status == "warning" and "plan-only" in off.detail and "OPENROUTER_API_KEY" in off.detail


def test_g3_choice_in_core_unblocks_completed(ws):
    """Debt T3: a variant left incomplete does not block `completed` once the person chose."""
    start(ws)
    product.approve_plan(ws, "p1", "R1", "запускай")
    cards = product.flags(ws, "p1")["cards"]
    decisions = {c["card_id"]: "accept" for c in cards}
    decisions["v03_c02"] = "reject"
    v = product.decide_cards(ws, "p1", "R1", decisions)
    v = accept_all(ws, "p1")
    assert v["state"] == "partial" and v["reason"] == "incomplete_variants"
    with pytest.raises(service.ServiceError, match="неполные"):
        product.select_variants(ws, "p1", ["v03"], "R1", "третий")
    out = product.select_variants(ws, "p1", ["v01", "v02"], "R1", "берём первый и второй")
    assert out["state"] == "completed"
    store = RunStore(ws.run_dir("p1"))
    assert store.manifest().status_reason is None
    sel = [e.payload for e in store.events() if e.type == "human_decision" and e.payload.get("gate") == "G3"]
    assert sel[-1]["variants"] == ["v01", "v02"] and sel[-1]["human_answer"] == "берём первый и второй"
    assert sorted(p.name for p in Path(out["delivery"]).iterdir() if p.is_dir()) == ["v01", "v02"]


# ---------------------------------------------------------------- folder 1, assets, installer


def test_build_dist_has_no_customer_data_and_is_rebuildable(tmp_path):
    import importlib.util

    spec = importlib.util.spec_from_file_location("build_dist", ROOT / "scripts/build_dist.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    repo = tmp_path / "repo"  # dist/ is a git repository (stage 10): its own files stay untouched
    (repo / ".git").mkdir(parents=True)
    (repo / ".git/HEAD").write_text("ref: refs/heads/main\n")
    (repo / "README.md").write_text("repo readme\n")
    (repo / ".gitignore").write_text("repo ignore\n")
    out = repo / "cardforge"
    meta = mod.build(out)
    assert (out / "DIST.json").exists() and meta["core_sha256"]
    for rel in ("plugin/skills/cardforge/SKILL.md", "plugin/AGENTS.md", "plugin/.mcp.json",
                ".agents/plugins/marketplace.json", "plugin/.mcp.codex.json",
                "installer/install.sh", "installer/install.ps1", "uv.lock", "AGENTS.md",
                "models/csd-vit-l/source.json", "models/rembg/source.json", "src/cardforge/mcp/server.py"):
        assert (out / rel).exists(), rel
    files = [p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file()]
    assert not [f for f in files if f.startswith("dataset/")]
    assert [f for f in files if f.startswith("runs/")] == mod.RUN_ARTIFACTS
    assert not [f for f in files if f.endswith((".bin", ".onnx", ".safetensors")) or ".private" in f]
    assert not [f for f in files if f.startswith("knowledge/refs") or f in mod.DATA_FILES]
    assert "/Users/" not in (out / "DIST.json").read_text(encoding="utf-8")
    readme = (out / "README.md").read_text(encoding="utf-8")
    assert "Установка — двойной щелчок" in readme and "install.command" in readme
    ignored = (out / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "assets/" in ignored and "knowledge/frames/occlusion_v2.png" in ignored  # unpacked frames
    ev = (out / "config/evaluation.toml").read_text(encoding="utf-8")
    assert "enforce_price_dates = false" in ev and "enforce_ledger_cap = false" in ev
    assert "budget_usd = 1000000.0" in ev and "176.2" not in ev.split("[openrouter]")[1].split("[")[0]
    assert "route_probe = true" in (out / "config/cardforge.toml").read_text(encoding="utf-8")
    home = str(Path.home())
    assert home not in readme and not (out / "README.ru.md").exists()
    leaks = [f for f in files if f.endswith((".toml", ".md", ".json")) and not f.startswith("tests/")
             and home in (out / f).read_text(errors="ignore")]  # fmt: skip
    assert not leaks, leaks  # no developer machine paths in the git folder
    assert "Windows — не проверено" in readme and (out / "README.en.md").exists()
    mod.build(out)  # an earlier build is replaced
    (out / "DIST.json").unlink()
    with pytest.raises(SystemExit, match="not an earlier build"):
        mod.build(out)
    with pytest.raises(SystemExit, match="inside the source"):
        mod.build(ROOT / "src/cardforge/x")
    (out / "DIST.json").write_text("{}")
    mod.build(out)
    assert (repo / ".git/HEAD").read_text() == "ref: refs/heads/main\n"
    assert (repo / "README.md").read_text() == "repo readme\n"
    assert (repo / ".gitignore").read_text() == "repo ignore\n"
    (repo / "DIST.json").write_text("{}")
    with pytest.raises(SystemExit, match="git repository"):
        mod.build(repo)  # never over the repository root, even with a DIST.json
    assert (repo / ".git/HEAD").exists()


def test_fetch_assets_checks_sha256(tmp_path):
    import hashlib

    from cardforge.assets import fetch

    blob = tmp_path / "u2net.onnx"
    blob.write_bytes(b"weights")
    good = hashlib.sha256(b"weights").hexdigest()
    root = tmp_path / "root"
    (root / "models/rembg").mkdir(parents=True)
    src = {"files": {"models/u2net/u2net.onnx": {"url": blob.as_uri(), "sha256": good}}}
    (root / "models/rembg/source.json").write_text(json.dumps(src))
    assert fetch(root, names=("rembg",), dry_run=True)["planned"] == ["models/rembg/models/u2net/u2net.onnx"]
    rep = fetch(root, names=("rembg",), log=lambda m: None)
    assert rep["ok"] and rep["downloaded"] == ["models/rembg/models/u2net/u2net.onnx"]
    assert fetch(root, names=("rembg",))["kept"] == ["models/rembg/models/u2net/u2net.onnx"]
    src["files"]["models/u2net/u2net.onnx"]["sha256"] = "0" * 64
    (root / "models/rembg/source.json").write_text(json.dumps(src))
    bad = fetch(root, names=("rembg",), log=lambda m: None)
    assert not bad["ok"] and "sha256" in bad["failed"][0]["error"]
    assert not (root / "models/rembg/models/u2net/u2net.onnx").exists()  # refused file removed


def test_installer_dry_run(tmp_path):
    import subprocess

    refused = subprocess.run(["sh", str(ROOT / "installer/install.sh"), "--dry-run", "--plugin", "none"],
                             env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path),
                                  "CARDFORGE_INSTALL_HOME": str(tmp_path / "cf")},
                             capture_output=True, text=True, timeout=60)  # fmt: skip
    if list((ROOT / "assets").glob("cardforge-data-*.tar.gz")):  # folder 1 with its assets: found
        assert refused.returncode == 0, refused.stderr
    else:
        assert refused.returncode == 1 and "--data" in refused.stderr  # no data archive: stop early
    out = subprocess.run(["sh", str(ROOT / "installer/install.sh"), "--dry-run", "--no-data",
                          "--plugin", "none"],
                         env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path),
                              "CARDFORGE_INSTALL_HOME": str(tmp_path / "cf")},
                         capture_output=True, text=True, timeout=60)  # fmt: skip
    assert out.returncode == 0, out.stderr
    for step in ("uv sync --frozen", "fetch-assets", "cardforge doctor", "uv python install 3.12"):
        assert step in out.stdout


def test_background_worker_runs_in_its_own_process(ws, monkeypatch):
    """The real worker: `cardforge work` in a separate process renders to G2 (FakeBackend)."""
    import time

    start(ws)
    monkeypatch.delenv("CARDFORGE_WORKER", raising=False)
    v = product.approve_plan(ws, "p1", "R1", "запускай")
    store = RunStore(ws.run_dir("p1"))
    info = store.read_json("worker.json")
    assert not info.get("inline") and info["pid"] != __import__("os").getpid()
    for _ in range(240):
        v = product.view(ws, "p1")
        if not v["worker_running"]:
            break
        time.sleep(0.5)
    assert v["next"] == "show_flags", (v, store.path("worker.log").read_text())
    assert store.read_json("worker.json")["state"] == "awaiting_g2"



# ---------------------------------------------------------------- gates (user 2026-11-10)


def test_default_gates_are_g1_human_g2_none(ws):
    from cardforge.autonomy import gate_modes

    assert gate_modes(ROOT) == ("all", "none")
    assert gate_modes(ROOT, "auto", "flagged") == ("none", "flagged")
    with pytest.raises(ValueError):
        gate_modes(ROOT, g2="sometimes")
    v = product.start(ws, "Лето", sets=2, run_id="d1", llm="fake", render="fake")
    req = RunStore(ws.run_dir("d1")).read_json("request.json")["hitl"]
    assert (req["g1"], req["g2"]) == ("all", "none") and v["next"] == "show_plan"


def test_g2_none_puts_best_attempt_into_the_set_marked_for_g3(ws):
    """No human G2: non-auto-passed cards are redrawn within the limit; the best attempt still
    reaches the set, marked «не подтверждена судьями» on the sheet, in delivery and report."""
    product.start(ws, "Лето", sets=3, run_id="d2", llm="fake", render="fake")
    v = product.approve_plan(ws, "d2", "R1", "запускай")
    store = RunStore(ws.run_dir("d2"))
    m = store.manifest()
    assert v["state"] == "completed" and v["next"] == "export"
    assert not store.exists("review/g2.json")
    unconfirmed = service.unconfirmed_cards(m)
    assert unconfirmed and all(c.state == "accepted" for c in m.cards.values())
    cid, rec = sorted(unconfirmed.items())[0]
    assert m.cards[cid].attempts == 3  # redrawn up to the limit first
    assert m.cards[cid].accepted_attempt == rec["attempt_no"]
    sheets = product.sheets(ws, "d2")
    marked = {u["card_id"] for s in sheets["sets"] for u in s["unconfirmed"]}
    assert marked == set(unconfirmed) and all(s["complete"] for s in sheets["sets"])
    assert all(u["why"] for s in sheets["sets"] for u in s["unconfirmed"])
    g3 = store.read_json("review/g3.json")
    assert {u["card_id"] for v in g3["variants"].values() for u in v.get("unconfirmed", [])} == marked
    assert "Не подтверждены судьями" in store.path("report.md").read_text(encoding="utf-8")
    vid = cid.split("_")[0]
    out = product.export_run(ws, "d2")
    cards = json.loads((Path(out["delivery"]) / vid / "cards.json").read_text(encoding="utf-8"))["cards"]
    assert any(c["accepted_by"] == "не подтверждена судьями" for c in cards)
    assert "Не подтверждены судьями" in (Path(out["delivery"]) / "index.html").read_text(encoding="utf-8")
    # G3 choice: redraw the unconfirmed card with an edit → accepted at once, marked, delivery stale
    product.regenerate(ws, "d2", [cid], "без надписей", "R1")
    m = store.manifest()
    assert m.cards[cid].attempts == 4 and m.cards[cid].state == "accepted"
    assert service.unconfirmed_cards(m)[cid]["why"] == "regenerated on request"
    assert product.view(ws, "d2")["next"] == "export"
    product.export_run(ws, "d2")
    assert product.view(ws, "d2")["next"] == "review_collection"


def test_g2_none_without_marks_keeps_the_old_rejection(ws):
    """Runs without the G3 marking option (tests, old runs) still reject at the limit."""
    from cardforge.contracts.request import CollectionRequest, Hitl

    req = CollectionRequest(request_id="t", collection_theme="Кино", set_theme="Вестерн",
                            producer_wishes_raw="", render_profile="fake",
                            hitl=Hitl(g1="none", g2="none"))  # fmt: skip
    with pytest.raises(service.ServiceError, match="needs a human G2"):
        service.create_run(ws, req, llm="fake", render="fake", judge="none", shadow_judges=[])


def test_cli_gate_override_per_run(ws):
    from typer.testing import CliRunner

    from cardforge.cli import app

    base = ["plan", "--collection", "Кино", "--set-theme", "Вестерн", "--llm", "fake",
            "--render", "fake", "--runs-dir", str(ws.runs_dir)]  # fmt: skip
    out = CliRunner().invoke(app, [*base, "--run-id", "c1", "--judge", "fake"])
    assert out.exit_code in (0, 2), out.output
    hitl = RunStore(ws.run_dir("c1")).read_json("request.json")["hitl"]
    opts = RunStore(ws.run_dir("c1")).manifest().runtime_options
    assert (hitl["g1"], hitl["g2"]) == ("all", "none") and opts["g3_unconfirmed"]
    out = CliRunner().invoke(app, [*base, "--run-id", "c2", "--g1", "auto", "--g2", "flagged"])
    assert out.exit_code in (0, 2), out.output
    hitl = RunStore(ws.run_dir("c2")).read_json("request.json")["hitl"]
    assert (hitl["g1"], hitl["g2"]) == ("none", "flagged")
    assert "g3_unconfirmed" not in RunStore(ws.run_dir("c2")).manifest().runtime_options


# ---------------------------------------------------------------- collection plan (user 2026-11-10)


def test_collection_is_required_never_defaulted(ws):
    with pytest.raises(service.ServiceError, match="тема коллекции"):
        product.start(ws, " ", run_id="nc", llm="fake", render="fake")
    tool = tools.BY_NAME["start_run"]
    assert tool.schema["required"] == ["collection"]
    assert "collection" not in product.designer_config(ROOT)


def test_whole_collection_plan_at_g1(ws):
    """Default: 16 set themes, sets 1–15 with one list of object cards each (gold slots by the
    reference pattern 0…4, 130 object cards), set 16 = grand set theme only."""
    v = product.start(ws, "Лето", run_id="leto", llm="fake", render="fake")
    assert v["next"] == "show_plan"
    plan = product.plan_view(ws, "leto")
    assert plan["brief"]["plan_mode"] == "collection" and plan["brief"]["sets"] == 16
    assert len(plan["collection"]["set_themes"]) == 16 and plan["collection"]["grand_set_theme"]
    assert [s["set_index"] for s in plan["variants"]] == list(range(1, 16))
    assert [s["gold_slots"] for s in plan["variants"]] == [0] * 6 + [1, 1, 2, 2, 2, 2, 3, 3, 4]
    assert [len(s["cards"]) for s in plan["variants"]] == [10 - g for g in
                                                          [0] * 6 + [1, 1, 2, 2, 2, 2, 3, 3, 4]]  # fmt: skip
    assert plan["collection"]["object_cards"] == 130 and plan["collection"]["gold_slots"] == 20
    titles = [s["title"] for s in plan["variants"]]
    assert titles == plan["collection"]["set_themes"][:15] and len(set(titles)) == 15
    g1 = RunStore(ws.run_dir("leto")).read_json("review/g1.json")
    assert len(g1["items"]) == 15 and g1["items"][14]["gold_slots"] == 4


def test_number_of_sets_from_the_request(ws):
    """«сделай 4 набора коллекции Лето»: only 4 sets planned and drawn, no grand set; gold slots by
    the set's position (sets 1–4 have none)."""
    product.start(ws, "Лето", sets=4, run_id="l4", llm="fake", render="fake")
    plan = product.plan_view(ws, "l4")
    assert len(plan["collection"]["set_themes"]) == 4 and plan["collection"]["grand_set_theme"] is None
    assert [len(s["cards"]) for s in plan["variants"]] == [10, 10, 10, 10]
    product.start(ws, "Лето", sets=9, run_id="l9", llm="fake", render="fake")
    plan = product.plan_view(ws, "l9")
    assert [s["gold_slots"] for s in plan["variants"]][6:] == [1, 1, 2]
    with pytest.raises(service.ServiceError, match="1 до 16"):
        product.start(ws, "Лето", sets=17, run_id="l17", llm="fake", render="fake")


def test_collection_run_draws_all_sets_and_delivers_by_set(ws):
    product.start(ws, "Лето", sets=8, run_id="l8", llm="fake", render="fake", g2="flagged")
    product.approve_plan(ws, "l8", "R1", "запускай")
    v = accept_all(ws, "l8")
    assert v["state"] == "completed" and v["next"] == "export"
    with pytest.raises(service.ServiceError, match="выбора варианта нет"):
        product.select_variants(ws, "l8", ["v01"], "R1", "первый")
    out = product.export_run(ws, "l8")
    current = Path(out["delivery"])
    assert sorted(p.name for p in current.iterdir() if p.is_dir()) == [f"v{i:02d}" for i in range(1, 9)]
    assert len(list((current / "v08/png").glob("*.png"))) == 9  # set 8: 9 object cards + 1 gold slot
    index = (current / "index.html").read_text(encoding="utf-8")
    assert "Коллекция «Лето»" in index and "Сет 8" in index and "Золотых позиций: 1" in index
    assert product.view(ws, "l8")["next"] == "review_collection"
    sheets = product.sheets(ws, "l8")
    assert len(sheets["sets"]) == 8 and sheets["sets"][7]["gold_slots"] == 1


def test_collection_validator_catches_repeats_counts_and_grand_theme():
    from cardforge.contracts.request import CollectionRequest, PlanDraft
    from cardforge.llm.fake import fake_plan
    from cardforge.planner.collection import layout
    from cardforge.planner.validators import validate_collection

    req = CollectionRequest(request_id="t", collection_theme="Лето", set_theme="",
                            producer_wishes_raw="", plan_mode="collection", sets_total=16,
                            variants_count=15)  # fmt: skip
    lay = layout(ROOT, 16)
    plans = []
    for slot in lay["drawn"]:
        d = fake_plan(slot["set_index"] - 1, "x")
        d["cards"] = d["cards"][: slot["object_cards"]]
        d.update(variant_id=slot["variant_id"], set_index=slot["set_index"],
                 gold_slots=slot["gold_slots"], title=f"t{slot['set_index']}")  # fmt: skip
        plans.append(PlanDraft.model_validate(d))
    meta = {"set_themes": [f"t{i}" for i in range(1, 17)], "grand_set_theme": "t16"}
    errs = validate_collection(meta, plans, req, lay)["errors"]
    assert any("repeats set" in e for e in errs)  # the fake pool repeats objects across sets
    plans[14] = plans[14].model_copy(update={"cards": plans[14].cards[:5]})
    errs = validate_collection({"set_themes": meta["set_themes"][:15]}, plans, req, lay)["errors"]
    assert any("16 set themes" in e for e in errs) and any("grand_set_theme" in e for e in errs)
    assert any("has 6 object cards" in e for e in errs)


def test_collection_plan_through_host_agent_tasks_and_g1_edit(ws):
    """The real plugin path: the host agent answers normalize / plan / policy_screen task files,
    G1 opens; an edit in words goes back as a repair task with the previous collection."""
    from cardforge.llm.fake import FakeLLM

    product.start(ws, "Лето", sets=2, run_id="h2", llm="host-agent", render="fake")
    fake = FakeLLM()
    for _ in range(8):
        v = product.view(ws, "h2")
        if v["next"] != "answer_tasks":
            break
        for t in product.agent_tasks(ws, "h2")["tasks"]:
            body = t["body"]
            if body["step"] in ("plan", "repair"):
                assert "collection_layout" in body and "The object cards of a set tell" in body["instruction"]
                answer = fake._collection_plan(body)
                if body["step"] == "repair":
                    assert body["human_edits"] == ["тему сета 1 назови «Пикник»"]
                    assert body["previous_collection"]["set_themes"][0] == "Лето: подтема 1"
                    answer["collection"]["set_themes"][0] = "Пикник"
                    answer["variants"][0]["title"] = "Пикник"
            else:
                answer = getattr(fake, "_" + body["step"])(body)
            product.answer_task(ws, "h2", t["task"], answer, "claude-code", "test")
        v = product.resume(ws, "h2")
        if v["next"] == "show_plan" and not product.plan_view(ws, "h2")["edits_so_far"]:
            product.edit_plan(ws, "h2", "тему сета 1 назови «Пикник»", "R1")
    assert v["next"] == "show_plan"
    assert product.plan_view(ws, "h2")["collection"]["set_themes"] == ["Пикник", "Лето: подтема 2"]
    store = RunStore(ws.run_dir("h2"))
    rounds = [e.payload["round"] for e in store.events() if e.type == "material_rules"]
    assert len(rounds) == len(set(rounds))  # journaled once per round


def test_double_click_install_finds_assets_and_agents(tmp_path):
    """User 2026-11-10: install by double-click, assets inside the installer folder."""
    import shutil
    import stat
    import subprocess

    command = ROOT / "installer/install.command"
    assert command.stat().st_mode & stat.S_IXUSR
    assert "installer/install.sh --gui" in command.read_text(encoding="utf-8")
    assert "install.ps1" in (ROOT / "installer/install.bat").read_text(encoding="utf-8")
    folder = tmp_path / "cardforge"
    (folder / "installer").mkdir(parents=True)
    shutil.copy(ROOT / "installer/install.sh", folder / "installer/install.sh")
    shutil.copy(ROOT / "pyproject.toml", folder / "pyproject.toml")
    (folder / "assets/models/csd-vit-l").mkdir(parents=True)
    (folder / "assets/cardforge-data-0.0.1.tar.gz").write_bytes(b"")
    home = tmp_path / "home"
    (home / ".local/bin").mkdir(parents=True)
    (home / ".local/bin/codex").write_text("#!/bin/sh\n")
    (home / ".local/bin/codex").chmod(0o755)
    env = {"PATH": "/usr/bin:/bin", "HOME": str(home), "CARDFORGE_INSTALL_HOME": str(tmp_path / "cf"),
           "CARDFORGE_AGENT_SEARCH": "path"}  # this machine's own apps must not count
    out = subprocess.run(["sh", str(folder / "installer/install.sh"), "--dry-run"], env=env,
                         capture_output=True, text=True, timeout=60)  # fmt: skip
    assert out.returncode == 0, out.stderr
    assert "unpacking cardforge-data-0.0.1.tar.gz" in out.stdout
    assert f"--from {folder}/assets/models" in out.stdout
    assert "codex plugin add cardforge@cardforge" in out.stdout and "claude plugin" not in out.stdout
    (home / ".local/bin/codex").unlink()
    none = subprocess.run(["sh", str(folder / "installer/install.sh"), "--dry-run"], env=env,
                          capture_output=True, text=True, timeout=60)  # fmt: skip
    assert none.returncode == 1 and "ни Claude Code" in none.stderr



def test_installer_finds_the_claude_desktop_app_cli(tmp_path):
    """The other Mac (2026-11-10): Claude Code and Codex installed as apps, no `claude` / `codex` on
    PATH. The CLI inside the Claude desktop app is found, newest version first."""
    import subprocess

    home = tmp_path / "home"
    base = home / "Library/Application Support/Claude/claude-code"
    for ver, build in (("2.1.9", "aaa"), ("2.1.10", "bbb")):
        f = base / ver / build / "claude.app/Contents/MacOS/claude"
        f.parent.mkdir(parents=True)
        f.write_text("#!/bin/sh\n")
        f.chmod(0o755)
    env = {"PATH": "/usr/bin:/bin", "HOME": str(home), "CARDFORGE_INSTALL_HOME": str(tmp_path / "cf")}
    out = subprocess.run(["sh", str(ROOT / "installer/install.sh"), "--dry-run", "--no-data",
                          "--plugin", "claude"], env=env, capture_output=True, text=True, timeout=60)  # fmt: skip
    assert out.returncode == 0, out.stderr
    assert "2.1.10/bbb/claude.app/Contents/MacOS/claude plugin install cardforge@cardforge" in out.stdout


def test_doctor_tool_hides_development_checks():
    """The agent read «full mode» + «no eligible renderer» as a contradiction (user test 2026-11-10):
    development checks (local FLUX/MLX, Codex rendering) never reach the plugin's answer."""
    from cardforge.orchestrator.workspace import Workspace

    out = tools._doctor(Workspace.from_env(ROOT), {})
    assert not [w for w in out["warnings"] if w.split(":")[0] in tools.DEV_CHECKS]
    assert not any("renderer" in w for w in out["warnings"])


def test_variants_on_request_main_by_judges_and_alternatives(ws):
    """User 2026-11-10: variants on request — 1 + N variants of one set; the judges pick the main
    one, the others are alternatives; at G3 the person can make another variant the main one."""
    with pytest.raises(service.ServiceError, match="тема набора"):
        product.start(ws, "Лето", alternatives=3, run_id="vx", llm="fake", render="fake")
    product.start(ws, "Лето", set_theme="Пикник", alternatives=3, run_id="va", llm="fake", render="fake")
    store = RunStore(ws.run_dir("va"))
    req = store.read_json("request.json")
    assert req["plan_mode"] == "variants" and req["variants_count"] == 4
    v = product.approve_plan(ws, "va", "R1", "запускай")
    assert v["state"] == "completed" and v["next"] == "export"
    out = product.export_run(ws, "va")
    assert out["main_variant"]["by"] == "judges"
    main = out["main_variant"]["variant"]
    current = Path(out["delivery"])
    assert len([p for p in current.iterdir() if p.is_dir()]) == 4
    index = (current / "index.html").read_text(encoding="utf-8")
    assert f"Основной вариант (выбор судей) — {main}" in index and "Альтернатива 3" in index
    assert json.loads((current / main / "cards.json").read_text())["role"] == "main"
    sheets = product.sheets(ws, "va")
    assert sheets["variants"][0]["main"] and sheets["variants"][0]["variant_id"] == main
    assert [r["judges"]["rank"] for r in sheets["variants"]] == [1, 2, 3, 4]
    assert product.view(ws, "va")["next"] == "review_variants"
    other = next(r["variant_id"] for r in sheets["variants"] if not r["main"])
    out = product.select_variants(ws, "va", [other], "R1", "основным берём этот")
    assert out["main_variant"] == {"variant": other, "by": "human"} and out["previous_version"] == "v01"
    rec = store.manifest().counters["main_variant"]
    assert rec["judges_pick"] == main and rec["human_answer"] == "основным берём этот"
    assert "Основной вариант (выбор человека)" in (Path(out["delivery"]) / "index.html").read_text(encoding="utf-8")
