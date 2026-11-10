"""Model weights the core needs on a designer's machine (stage 7 installer).

Each `models/<name>/source.json` names its files with sha256: CSD ViT-L from Hugging Face at a
pinned revision (`sha256` + `model_id` + `revision`), u2net from its release URL (`files`). The
installer calls `cardforge fetch-assets`; a file already present with the right sha256 is kept,
a download with another sha256 is deleted and refused. No other weights are fetched.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import urllib.request
from pathlib import Path

# Weights the plugin needs (LoRA and local FLUX are out of scope, user 2026-11-09).
REQUIRED = ("csd-vit-l", "rembg")
CHUNK = 1 << 20


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while block := fh.read(CHUNK):
            h.update(block)
    return h.hexdigest()


def planned_files(root: Path, names=REQUIRED) -> list[dict]:
    """[{model, path, url, sha256}] from the source.json files."""
    out = []
    for name in names:
        src = json.loads((root / "models" / name / "source.json").read_text(encoding="utf-8"))
        if "files" in src:
            for rel, f in src["files"].items():
                out.append({"model": name, "path": f"models/{name}/{rel}", "url": f["url"],
                            "sha256": f["sha256"]})  # fmt: skip
        else:
            base = f"https://huggingface.co/{src['model_id']}/resolve/{src['revision']}"
            for rel, digest in src["sha256"].items():
                out.append({"model": name, "path": f"models/{name}/{rel}", "url": f"{base}/{rel}",
                            "sha256": digest})  # fmt: skip
    return out


def _download(url: str, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "cardforge-installer"})
        with urllib.request.urlopen(req, timeout=60) as reply, os.fdopen(fd, "wb") as out:
            shutil.copyfileobj(reply, out, CHUNK)
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def fetch(
    root: Path, *, names=REQUIRED, dry_run: bool = False, source: Path | None = None, log=print
) -> dict:
    """Download missing or wrong files; verify every file's sha256. `source`: a folder laid out
    like `models/` (e.g. another install's `models/`) to copy matching files from first."""
    report = {"kept": [], "downloaded": [], "copied": [], "planned": [], "failed": []}
    for f in planned_files(root, names):
        path = root / f["path"]
        if path.exists() and sha256_file(path) == f["sha256"]:
            report["kept"].append(f["path"])
            continue
        local = Path(source) / Path(f["path"]).relative_to("models") if source else None
        if local is not None and local.is_file() and not dry_run:
            if sha256_file(local) == f["sha256"]:
                path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(local, path)
                report["copied"].append(f["path"])
                continue
            log(f"{local}: sha256 differs, downloading instead")
        if dry_run:
            report["planned"].append(f["path"])
            continue
        log(f"downloading {f['path']} ← {f['url']}")
        try:
            _download(f["url"], path)
        except OSError as exc:
            report["failed"].append({"path": f["path"], "error": str(exc)})
            continue
        got = sha256_file(path)
        if got != f["sha256"]:
            path.unlink(missing_ok=True)
            report["failed"].append({"path": f["path"], "error": f"sha256 {got} != {f['sha256']}"})
            continue
        report["downloaded"].append(f["path"])
    report["ok"] = not report["failed"] and not report["planned"]
    return report
