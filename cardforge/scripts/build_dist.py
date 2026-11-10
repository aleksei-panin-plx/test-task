# ruff: noqa: E501  (file lists and templates)
"""Build folder 1 `cardforge/` (distribution-options-2026-11-09 §5) from this project, without git.

The working project is only read. The output is a separate folder, git-ready: core, knowledge,
policies, prompts, config, the plugin, the installer and the tests — no customer art, no runs, no
weights (the installer downloads weights by sha256 and unpacks the data asset). A second, optional
output is the data asset `cardforge-data-<version>.tar.gz` with `dataset/` (customer references,
allowed in the installer, never in git; user 2026-11-09).

    .venv/bin/python -I scripts/build_dist.py --out dist/cardforge [--data-asset dist/assets]

`--out` is refused inside a source folder; an existing output is replaced only if it carries the
`DIST.json` marker of an earlier build.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import tarfile
import tomllib
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TREES = ["src/cardforge", "config", "knowledge", "policies", "prompts", "agents", "plugin", "installer",
         "tests"]  # fmt: skip
FILES = ["pyproject.toml", "uv.lock", ".claude-plugin/marketplace.json", ".agents/plugins/marketplace.json",
         ".python-version"]  # fmt: skip
# The folder's own README (install guide for a designer / tester); the development README stays out.
READMES = {"installer/README.dist.md": "README.md", "installer/README.dist.en.md": "README.en.md"}
# Model folders: only their source.json (weights come from the installer).
MODELS = ["csd-vit-l", "rembg"]
# Small core artifacts that still live under runs/ (debt: move to a shipped folder). Same paths.
RUN_ARTIFACTS: list[str] = []  # the style scorer moved to knowledge/style_scorer/ (stage 7)
SKIP_PARTS = {"__pycache__", ".pytest_cache", ".ruff_cache", ".DS_Store"}
# Tests of the development history (research campaigns over runs/, local FLUX smoke guards,
# Gemini drivers): the scripts and runs they check stay in the development project.
DEV_ONLY_TESTS = (
    "tests/test_borderline_batch.py",
    "tests/test_judge_campaigns.py",
    "tests/test_local_calibration.py",
    "tests/test_gemini_driver.py",
    "tests/test_smoke_guards.py",
)
# Scripts kept tests need (blind G2 review package, the build itself).
SCRIPTS = ["scripts/stage04_blind_review.py", "scripts/stage04_blind_review.html",
           "scripts/stage04_blind_targets.py", "scripts/build_dist.py"]
# Private or customer data that must never enter the git folder.
SKIP_PREFIXES = ("knowledge/references", "knowledge/refs", "tests/snapshots/private")
DIST_GITIGNORE = """assets/
.venv/
__pycache__/
*.py[cod]
.pytest_cache/
.ruff_cache/
.DS_Store
.env
.env.*
!.env.example
dataset/
models/**
!models/*/
!models/*/source.json
runs/
"""
DIST_AGENTS = """# CardForge (folder 1: core + local plugin + installer)

Built from the CardForge development project by `scripts/build_dist.py` (see `DIST.json`). Do not edit
the core here and in the development project independently: changes go through one of them and are
carried over by a new build (the server folder will use `scripts/sync_core.py`, `docs/core-sync.md`).

- Designer flow: `plugin/skills/cardforge/SKILL.md`; scenario and decisions: `docs/07-plugin.md`.
- Install: `installer/install.sh` (macOS); `installer/install.ps1` (Windows, not verified yet).
- Checks after code changes: `.venv/bin/pytest -m "not api and not gpu"`, `.venv/bin/ruff check src tests`,
  `.venv/bin/cardforge doctor`.
- Policies: `policies/ip/baseline.md` is always active (its section «Чек-лист авторских прав» drives the
  planner and the IP QA; show it from that file, never copy it;
  designers are not asked to confirm it, user 2026-11-10); culture packs attach by `markets`.
- Never: write render prompts by hand, lift a `block` policy rule, publish customer art or runs,
  commit `dataset/`, weights, `.env` or `runs/`.
"""


def _version() -> str:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]


# Images derived from the customer's UI (the card frame) travel in the data asset, not in git.
DATA_FILES = (
    "knowledge/frames/frame_preview.png",
    "knowledge/frames/occlusion_v2.png",
    # Evidence of the technical mask review of the references (sha256-checked with dataset/ masks
    # by knowledge.mask_review): travels with the data, not in git.
    "runs/stage02/17-mask-refinement/review.frozen.json",
    "runs/stage02/17-mask-refinement/review.round2.frozen.json",
)


def _skip(rel: str) -> bool:
    parts = set(Path(rel).parts)
    return (
        bool(parts & SKIP_PARTS)
        or rel.startswith(SKIP_PREFIXES)
        or rel.endswith(".private.json")
        or rel in DATA_FILES
        or rel in DEV_ONLY_TESTS
    )


def _copy_tree(src: Path, dst: Path, rel_root: Path) -> list[str]:
    out = []
    for path in sorted(src.rglob("*")):
        rel = str(path.relative_to(rel_root))
        if path.is_dir() or _skip(rel):
            continue
        target = dst / path.relative_to(rel_root)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        out.append(rel)
    return out


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build(
    out: Path, data_asset: Path | None = None, *, root: Path = ROOT, with_assets: bool = False
) -> dict:
    out = out.resolve()
    for name in ("src", "config", "knowledge", "policies", "dataset", "runs", "plugin"):
        if out.is_relative_to((root / name).resolve()):
            raise SystemExit(f"refusing to build inside the source folder {name}/")
    if out == root.resolve():
        raise SystemExit("refusing to build over the project")
    if out.exists():
        if not (out / "DIST.json").exists():
            raise SystemExit(f"{out} exists and is not an earlier build (no DIST.json)")
        shutil.rmtree(out)
    out.mkdir(parents=True)
    files: list[str] = []
    for tree in TREES:
        if (root / tree).exists():
            files += _copy_tree(root / tree, out, root)
    for rel in FILES:
        if (root / rel).exists():
            (out / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(root / rel, out / rel)
            files.append(rel)
    for src_rel, dst_rel in READMES.items():
        shutil.copy2(root / src_rel, out / dst_rel)
        files.append(dst_rel)
    for rel in SCRIPTS:
        (out / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(root / rel, out / rel)
        files.append(rel)
    for name in MODELS:
        rel = f"models/{name}/source.json"
        (out / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(root / rel, out / rel)
        files.append(rel)
    for rel in RUN_ARTIFACTS:
        (out / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(root / rel, out / rel)
        files.append(rel)
    # The development config carries this machine's path and the developer's disk threshold.
    cfg = out / "config/cardforge.toml"
    text = cfg.read_text(encoding="utf-8")
    text = "\n".join(
        'workspace = "~/.cardforge/current"' if line.startswith("workspace = ") else line
        for line in text.splitlines()
    )
    text = text.replace("minimum_disk_gb = 100", "minimum_disk_gb = 6  # designer install (dist)")
    text = text.replace("route_probe = false", "route_probe = true  # designer install: probe OpenRouter routes")
    cfg.write_text(text + "\n", encoding="utf-8")
    # User 2026-11-10: the price-verification date never stops a designer's generation.
    ev = out / "config/evaluation.toml"
    ev_text = ev.read_text(encoding="utf-8")
    if "enforce_price_dates = true" not in ev_text:
        raise SystemExit("config/evaluation.toml: enforce_price_dates = true expected")
    ev_text = ev_text.replace("enforce_price_dates = true", "enforce_price_dates = false")
    # User 2026-11-10 «Да, убрать»: no per-machine ledger ceiling for designers (the ledger still
    # journals every call; the OpenRouter key limit caps the spend).
    cap = "budget_usd = 176.2\nauthorized_max_usd = 176.2\n"
    if cap not in ev_text or "enforce_ledger_cap = true" not in ev_text:
        raise SystemExit("config/evaluation.toml: [openrouter] ledger cap lines changed; update build_dist.py")
    ev_text = ev_text.replace(cap, "budget_usd = 1000000.0\nauthorized_max_usd = 1000000.0\n")
    ev_text = ev_text.replace("enforce_ledger_cap = true", "enforce_ledger_cap = false")
    ev.write_text(ev_text, encoding="utf-8")
    # Host CLI paths of the developer's machine → bare commands found on PATH.
    tools = out / "config/tools.toml"
    tools.write_text(
        tools.read_text(encoding="utf-8").replace(str(Path.home()) + "/.local/bin/", ""),
        encoding="utf-8",
    )
    docs = out / "docs"
    docs.mkdir()
    stage = root / "docs/stages/07-plugin.md"  # a rebuild from folder 1 itself has docs/07-plugin.md
    shutil.copy2(stage if stage.exists() else root / "docs/07-plugin.md", docs / "07-plugin.md")
    files.append("docs/07-plugin.md")
    (out / ".gitignore").write_text(DIST_GITIGNORE, encoding="utf-8")
    (out / "AGENTS.md").write_text(DIST_AGENTS, encoding="utf-8")
    (out / "CLAUDE.md").write_text("Read and follow [AGENTS.md](AGENTS.md).\n", encoding="utf-8")
    (out / ".env.example").write_text("OPENROUTER_API_KEY=\n", encoding="utf-8")
    files += [".gitignore", "AGENTS.md", "CLAUDE.md", ".env.example"]
    home = str(Path.home())
    personal = [
        f for f in files
        if f.endswith((".toml", ".md", ".json", ".sh", ".ps1")) and not f.startswith("tests/")
        and home in (out / f).read_text(encoding="utf-8", errors="ignore")
    ]  # fmt: skip
    if personal:
        raise SystemExit(f"developer machine paths would leak: {personal[:5]}")
    leaked = [f for f in files if f.startswith(("dataset/", "runs/")) and f not in RUN_ARTIFACTS]
    if leaked:
        raise SystemExit(f"customer data or runs would leak: {leaked[:5]}")
    core = hashlib.sha256()
    for rel in sorted(f for f in files if f.startswith("src/cardforge/")):
        core.update(rel.encode() + b"\0" + _sha(out / rel).encode())
    meta = {
        "name": "cardforge",
        "version": _version(),
        "built_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "source": "CardForge development project (scripts/build_dist.py)",
        "core_sha256": core.hexdigest(),
        "files": len(files),
        "data_asset": None,
    }
    if with_assets:
        data_asset = out / "assets"
    if data_asset is not None:
        meta["data_asset"] = build_data_asset(data_asset, root=root)
        meta["data_asset"]["path"] = str(Path(meta["data_asset"]["path"]).relative_to(out)) if with_assets else meta["data_asset"]["path"]
    if with_assets:
        meta["weights"] = copy_weights(out / "assets/models", root=root)
    (out / "DIST.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return meta


def build_data_asset(folder: Path, *, root: Path = ROOT) -> dict:
    """`dataset/` and the frame images as a tarball for the installer (customer art: never into git or a public place)."""
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"cardforge-data-{_version()}.tar.gz"
    with tarfile.open(target, "w:gz") as tar:
        tar.add(root / "dataset", arcname="dataset",
                filter=lambda t: None if Path(t.name).name in SKIP_PARTS else t)  # fmt: skip
        for rel in DATA_FILES:
            tar.add(root / rel, arcname=rel)
    return {"path": str(target), "sha256": _sha(target), "bytes": target.stat().st_size}


def copy_weights(folder: Path, *, root: Path = ROOT) -> list[str]:
    """Model weights for an offline, double-click install (`assets/models/<model>/…`), copied only
    when their sha256 matches source.json; the installer passes them to `fetch-assets --from`."""
    sys.path.insert(0, str(root / "src"))
    from cardforge.assets import planned_files, sha256_file

    copied = []
    for f in planned_files(root):
        src = root / f["path"]
        if not src.is_file() or sha256_file(src) != f["sha256"]:
            raise SystemExit(f"weights missing or changed: {f['path']}")
        dst = folder / Path(f["path"]).relative_to("models")
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        copied.append(f["path"])
    return copied


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=ROOT / "dist/cardforge")
    ap.add_argument("--data-asset", type=Path, default=None, help="folder for the dataset tarball")
    ap.add_argument("--with-assets", action="store_true",
                    help="put the data archive and the model weights into <out>/assets (double-click install)")
    a = ap.parse_args()
    meta = build(a.out, a.data_asset, with_assets=a.with_assets)
    json.dump(meta, sys.stdout, ensure_ascii=False, indent=1)
    print()


if __name__ == "__main__":
    main()
