# ruff: noqa: E501  (report templates)
"""Carry the CardForge core from one autonomous copy to another (user 2026-11-09 «Автономные копии»).

    python -I scripts/sync_core.py <source> <target> [--dry-run]

Copies the core trees (`src/cardforge/`, `knowledge/`, `policies/`, `prompts/`, `config/`, plus the
shared core tests `tests/` and `agents/`) from `<source>` into `<target>`, prints a diff report
(added / changed / removed by sha256) and writes `<target>/docs/core-sync.md` with the sha256 of the
core, the date and the list of intentional divergences.

Intentional divergences of the target live in `<target>/core-sync.toml`:

- `keep = [...]` — paths (globs) the sync never overwrites or deletes in the target;
- `exclude = [...]` — source paths never copied (and removed from the target if present);
- `[[patch]] path, old, new, why` — a text replacement applied after the copy. A patch whose
  `old` text is gone from the new source and whose `new` text is not there either fails the
  sync (exit code 2): the divergence must be re-decided, never silently lost.

When the *source* carries a `core-sync.toml` (syncing back from the server folder), its patches
are reversed on the copied files and its `keep` paths are not carried, so server-only
divergences never leak into the other copy.

Never copied (and never deleted in the target), whatever the source holds: `*.private*`,
`labels.private/`, `dataset/`, `runs/`, `.env*`, caches and the frame images of the data asset. Nothing is deleted outside the synced trees.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import shutil
import sys
import tomllib
from datetime import UTC, datetime
from pathlib import Path

TREES = ["src/cardforge", "knowledge", "policies", "prompts", "config", "tests", "agents"]
# Scripts the shared core tests import (blind G2 package); copied like the trees.
FILES = ["scripts/stage04_blind_review.py", "scripts/stage04_blind_review.html",
         "scripts/stage04_blind_targets.py"]  # fmt: skip
SKIP_PARTS = {"__pycache__", ".pytest_cache", ".ruff_cache", ".DS_Store", "labels.private",
              "dataset", "runs", ".venv"}  # fmt: skip
# Customer-derived data (the card frame images) travels in the data asset, never in a copy of the
# core: never copied and never deleted by the sync (same list as `scripts/build_dist.py`).
DATA_FILES = {"knowledge/frames/frame_preview.png", "knowledge/frames/occlusion_v2.png"}
CONFIG = "core-sync.toml"
REPORT = "docs/core-sync.md"


def _private(rel: str) -> bool:
    parts = Path(rel).parts
    name = parts[-1]
    return (
        bool(set(parts) & SKIP_PARTS)
        or ".private" in name
        or name.startswith(".env")
        or rel in DATA_FILES
        or rel.startswith(("knowledge/references", "knowledge/refs", "tests/snapshots/private"))
    )


def _match(rel: str, patterns: list[str]) -> bool:
    return any(rel == p or fnmatch.fnmatch(rel, p) or rel.startswith(p.rstrip("/") + "/") for p in patterns)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _files(root: Path) -> dict[str, Path]:
    out: dict[str, Path] = {}
    for tree in TREES:
        base = root / tree
        if not base.exists():
            continue
        for path in sorted(base.rglob("*")):
            if path.is_file():
                rel = path.relative_to(root).as_posix()
                if not _private(rel):
                    out[rel] = path
    for rel in FILES:
        if (root / rel).is_file():
            out[rel] = root / rel
    return out


def load_config(folder: Path) -> dict:
    path = folder / CONFIG
    data = tomllib.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    return {"keep": list(data.get("keep", [])), "exclude": list(data.get("exclude", [])),
            "patch": list(data.get("patch", []))}  # fmt: skip


def core_sha256(root: Path) -> str:
    """Same digest as `DIST.json` `core_sha256` of `scripts/build_dist.py`: sorted
    `src/cardforge/` paths with their sha256 (caches excluded)."""
    h = hashlib.sha256()
    base = root / "src/cardforge"
    for path in sorted(base.rglob("*")):
        rel = path.relative_to(root).as_posix()
        if path.is_file() and not _private(rel):
            h.update(rel.encode() + b"\0" + _sha(path).encode())
    return h.hexdigest()


def _apply(text: str, old: str, new: str) -> tuple[str, str]:
    if old in text:
        return text.replace(old, new), "applied"
    if new in text:
        return text, "already in source"
    return text, "FAILED"


def sync(source: Path, target: Path, *, dry_run: bool = False, now: datetime | None = None) -> dict:
    source, target = source.resolve(), target.resolve()
    if source == target:
        raise SystemExit("source and target are the same folder")
    if not (source / "src/cardforge/__init__.py").exists():
        raise SystemExit(f"{source} has no src/cardforge/")
    cfg = load_config(target)
    back = load_config(source)  # divergences of the source copy (server → local direction)
    src_files = {
        rel: p for rel, p in _files(source).items()
        if not _match(rel, cfg["exclude"]) and not _match(rel, back["keep"])
    }  # fmt: skip
    dst_files = _files(target)
    # Expected content of every target file after the sync (before writing anything).
    expected: dict[str, bytes] = {rel: p.read_bytes() for rel, p in src_files.items()}
    patches = []
    for p in back["patch"]:  # reverse the source copy's own divergences
        rel = p["path"]
        if rel in expected:
            text, status = _apply(expected[rel].decode("utf-8"), p["new"], p["old"])
            expected[rel] = text.encode("utf-8")
            patches.append({"path": rel, "why": p.get("why", ""), "status": f"reversed: {status}",
                            "direction": "source"})  # fmt: skip
    for p in cfg["patch"]:
        rel = p["path"]
        if rel not in expected:
            patches.append({"path": rel, "why": p.get("why", ""), "status": "FAILED: file absent in source"})
            continue
        text, status = _apply(expected[rel].decode("utf-8"), p["old"], p["new"])
        expected[rel] = text.encode("utf-8")
        patches.append({"path": rel, "why": p.get("why", ""), "status": status})
    added, changed, removed, kept = [], [], [], []
    for rel, data in sorted(expected.items()):
        if _match(rel, cfg["keep"]) and rel in dst_files:
            kept.append(rel)
            continue
        if rel not in dst_files:
            added.append(rel)
        elif hashlib.sha256(data).hexdigest() != _sha(dst_files[rel]):
            changed.append(rel)
    for rel in sorted(dst_files):
        if rel not in expected and not _match(rel, cfg["keep"]):
            removed.append(rel)
    failed = [p for p in patches if "FAILED" in p["status"]]
    result = {
        "date": (now or datetime.now(UTC)).isoformat(timespec="seconds"),
        "source": str(source),
        "target": str(target),
        "source_core_sha256": core_sha256(source),
        "added": added, "changed": changed, "removed": removed, "kept": kept,
        "patches": patches,
        "keep": cfg["keep"], "exclude": cfg["exclude"],
        "dry_run": dry_run,
        "ok": not failed,
    }  # fmt: skip
    if dry_run or failed:
        return result
    for rel in added + changed:
        dst = target / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(expected[rel])
        shutil.copystat(src_files[rel], dst)
    for rel in removed:
        (target / rel).unlink()
    for tree in TREES:  # drop directories the removals emptied
        base = target / tree
        if base.exists():
            for d in sorted((p for p in base.rglob("*") if p.is_dir()), reverse=True):
                if not any(d.iterdir()):
                    d.rmdir()
    result["target_core_sha256"] = core_sha256(target)
    write_report(target, result)
    return result


def _rel(path: str, base: Path) -> str:
    """The source as seen from the target folder (`../cardforge`), never an absolute home path."""
    p = Path(path)
    try:
        return "../" + p.relative_to(base.parent).as_posix()
    except ValueError:
        return p.name


def write_report(target: Path, r: dict) -> Path:
    """`docs/core-sync.md`: the latest sync on top, earlier ones kept under «История»."""
    path = target / REPORT
    entries: list[dict] = []
    if path.exists():
        last = path.read_text(encoding="utf-8").split("<!-- sync-json:", 1)
        if len(last) == 2:
            prev = json.loads(last[1].split("-->", 1)[0])
            entries = [{k: prev[k] for k in ("date", "target_core_sha256", "source_name", "n")},
                       *prev.get("history", [])][:50]  # fmt: skip
    history = "\n".join(
        f"- {e['date']}: ядро `{e['target_core_sha256'][:16]}…` из `{e['source_name']}` "
        f"(+{e['n'][0]} ~{e['n'][1]} −{e['n'][2]})"
        for e in entries
    )

    def block(title: str, items: list[str]) -> str:
        if not items:
            return f"- {title}: нет\n"
        shown = "".join(f"  - `{i}`\n" for i in items[:200])
        more = f"  - … и ещё {len(items) - 200}\n" if len(items) > 200 else ""
        return f"- {title} ({len(items)}):\n{shown}{more}"

    src_name = _rel(r["source"], target)
    meta = {"date": r["date"], "target_core_sha256": r["target_core_sha256"], "source_name": src_name,
            "n": [len(r["added"]), len(r["changed"]), len(r["removed"])], "history": entries}  # fmt: skip
    lines = [
        "# Синхронизация ядра (core-sync)",
        "",
        "Эта папка несёт **автономную копию** ядра CardForge (решение пользователя 2026-11-09 «Автономные",
        "копии»). Ядро не правится здесь и в другой копии независимо: правка делается в одной копии и",
        "переносится в другую только `scripts/sync_core.py <источник> <папка>`. Расхождения, которые",
        "синхронизация не затирает, перечислены в `core-sync.toml` и ниже.",
        "",
        "## Последняя синхронизация",
        "",
        f"- дата (UTC): {r['date']}",
        f"- источник: `{src_name}`",
        f"- sha256 ядра источника (`src/cardforge/`, алгоритм `DIST.json` `core_sha256`): `{r['source_core_sha256']}`",
        f"- sha256 ядра этой папки после синхронизации и правок: `{r['target_core_sha256']}`",
        f"- деревья: {', '.join(f'`{t}/`' for t in TREES)}; файлы: {', '.join(f'`{f}`' for f in FILES)}",
        "",
        "### Изменения",
        "",
        block("добавлено", r["added"]),
        block("изменено", r["changed"]),
        block("удалено", r["removed"]),
        block("сохранено по `keep`", r["kept"]),
        "",
        "## Намеренные расхождения",
        "",
        "| Файл | Почему | Статус |",
        "|---|---|---|",
        *[f"| `{p['path']}` | {p['why']} | {p['status']} |" for p in r["patches"]],
        "",
        f"- `keep` (не перезаписываются): {', '.join(f'`{k}`' for k in r['keep']) or 'нет'}",
        f"- `exclude` (не копируются из источника): {', '.join(f'`{k}`' for k in r['exclude']) or 'нет'}",
        "",
        "Никогда не копируются и не удаляются: `*.private*`, `labels.private/`, `dataset/`, `runs/`, `.env*`, кэши, "
        "картинки рамки из архива данных (`knowledge/frames/frame_preview.png`, `occlusion_v2.png`).",
        "",
        "## История",
        "",
        history or "- (первая синхронизация)",
        "",
        f"<!-- sync-json:{json.dumps(meta, ensure_ascii=False)}-->",
        "",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def print_report(r: dict) -> None:
    print(f"sync {r['source']} → {r['target']}{' (dry run)' if r['dry_run'] else ''}")
    for key in ("added", "changed", "removed", "kept"):
        print(f"  {key}: {len(r[key])}")
        for rel in r[key][:40]:
            print(f"    {rel}")
        if len(r[key]) > 40:
            print(f"    … {len(r[key]) - 40} more")
    for p in r["patches"]:
        print(f"  patch {p['path']}: {p['status']}")
    print(f"  source core sha256 {r['source_core_sha256']}")
    if r.get("target_core_sha256"):
        print(f"  target core sha256 {r['target_core_sha256']}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("source", type=Path)
    ap.add_argument("target", type=Path)
    ap.add_argument("--dry-run", action="store_true", help="only print the diff")
    a = ap.parse_args()
    r = sync(a.source, a.target, dry_run=a.dry_run)
    print_report(r)
    if not r["ok"]:
        print("a divergence patch no longer applies: re-decide it in core-sync.toml", file=sys.stderr)
        raise SystemExit(2)


if __name__ == "__main__":
    main()
