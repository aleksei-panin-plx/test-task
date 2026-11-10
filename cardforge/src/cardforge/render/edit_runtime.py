"""Edit pass in the core (fable-review Q3 → user 2026-11-06 «все согласовываю»).

A card whose fix round asks only for material / style (G2 reason codes, auto-reject step 0,
style-scorer zone) gets ONE image edit of its previous picture (GPT Image 2.5, instruction
`restyle-v1`, references and request shape of `edit_pass`, as measured in
runs/stage04/edit-restyle) instead of a redraw. Config: config/edit_pass.toml. The request
builder `edit_pass` stays byte-identical to the Q3 experiment (its hash is frozen there).
"""

from __future__ import annotations

import contextlib
import io
import os
import time
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

from cardforge.contracts.generation import PromptBundle, Reference, RenderResult
from cardforge.render.edit_pass import (
    EDIT_ROLES,
    INSTRUCTION_VERSION,
    edit_bundle,
    make_backend,
    match_frame,
)


def png_bytes(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# ---------------------------------------------------------------- core integration

CONFIG = "config/edit_pass.toml"
EVENT = "edit_pass"


def load_config(root: Path) -> dict | None:
    import tomllib

    path = root / CONFIG
    return tomllib.loads(path.read_text()) if path.is_file() else None


def eligible_codes(asked: list[dict], cfg: dict) -> list[str]:
    """Codes of one fix round when every entry asks only for material / style; else []."""
    allowed = set(cfg.get("codes", ()))
    codes: list[str] = []
    for e in asked:
        own = [c for c in e.get("codes") or [] if c != "other"]
        if not own or not set(own) <= allowed:
            return []
        if e.get("text") and not cfg.get("allow_text", False):
            return []
        codes += own
    return list(dict.fromkeys(codes))


@contextlib.contextmanager
def paid_lock(path: Path, *, wait_s: float = 30.0, sleep=time.sleep, log=print):
    """`mkdir` lock shared by paid OpenRouter runs of several sessions; re-entrant inside one
    process (a script that already holds it can run the core)."""
    owner = path / "owner.txt"
    mine = f"pid {os.getpid()}"
    if path.is_dir() and owner.is_file() and owner.read_text().startswith(mine):
        yield
        return
    while True:
        try:
            path.mkdir()
            break
        except FileExistsError:
            log(f"{path.name} busy, waiting {wait_s:.0f} s")
            sleep(wait_s)
    try:
        owner.write_text(f"{mine} edit-pass {time.time():.0f}\n")
        yield
    finally:
        owner.unlink(missing_ok=True)
        path.rmdir()


class FakeEditBackend:
    """Offline stand-in: the source picture, a little more saturated (tests, FakeBackend runs)."""

    def __init__(self, source_png: bytes, cost_usd: float = 0.05):
        self.source_png = source_png
        self.cost_usd = cost_usd

    def render(self, bundle: PromptBundle, timeout_s: int) -> RenderResult:
        from PIL import ImageEnhance

        with Image.open(io.BytesIO(self.source_png)) as im:
            out = ImageEnhance.Color(im.convert("RGB")).enhance(1.15)
        return RenderResult(
            status="ok", image_png=png_bytes(out), cost_usd=self.cost_usd,
            model_revision="fake-edit",
        )  # fmt: skip


@dataclass
class EditPass:
    """Runtime of the edit pass: which cards qualify and how one edit is made."""

    root: Path
    cfg: dict
    fake: bool = False
    fake_cost_usd: float = 0.05

    @property
    def profiles(self) -> set[str]:
        return set(self.cfg.get("profiles", ()))

    @property
    def estimate_usd(self) -> float:
        return self.fake_cost_usd if self.fake else float(self.cfg.get("estimate_usd", 0.08))

    def bundle(self, refs: list, seed: int, source_sha256: str) -> PromptBundle:
        refs = [r if isinstance(r, Reference) else Reference(**r) for r in refs]
        b = edit_bundle(self.cfg.get("arm", "gpt"), refs, seed)
        b = b.model_copy(
            update={"template_version": f"{INSTRUCTION_VERSION}+src:{source_sha256[:16]}"}
        )
        return b.model_copy(update={"hash": b.compute_hash()})

    def render(self, bundle: PromptBundle, source_png: bytes, timeout_s: int) -> RenderResult:
        if self.fake:
            res = FakeEditBackend(source_png, self.fake_cost_usd).render(bundle, timeout_s)
        else:
            backend = make_backend(
                self.root, self.cfg.get("arm", "gpt"), source_png, self.cfg.get("provider_options")
            )
            with paid_lock(self.root / self.cfg.get("lock", "runs/_api/paid-run.lock")):
                res = backend.render(bundle, timeout_s)
        if res.status == "ok" and res.image_png:
            with Image.open(io.BytesIO(source_png)) as im:
                size = im.size
            res = res.model_copy(update={"image_png": png_bytes(match_frame(res.image_png, size))})
        return res


def runtime(root: Path, opts: dict, fake: bool) -> EditPass | None:
    """Real runs: config/edit_pass.toml. FakeBackend runs: only when a test injects
    `fake_edit` ({"profiles": [...], "cost_usd": ...})."""
    cfg = load_config(root)
    if fake:
        spec = opts.get("fake_edit")
        if not spec or not cfg:
            return None
        return EditPass(root, {**cfg, "enabled": True, "profiles": spec.get("profiles", ["fake"])},
                        fake=True, fake_cost_usd=spec.get("cost_usd", 0.05))  # fmt: skip
    if not cfg or not cfg.get("enabled", False):
        return None
    return EditPass(root, cfg)


__all__ = ["EDIT_ROLES", "EVENT", "EditPass", "eligible_codes", "paid_lock", "runtime"]
