"""Second «restyle» pass through image edit (fable-review Q3, 2026-11-06).

A finished card (shape and composition already accepted) is sent back to an image model
together with the game's reference cards and one instruction: repaint materials and light as
in the references, keep the form and composition. Two arms:

- Nano Banana 2.1 (chat endpoint): the card first, then the labelled references.
- GPT Image 2.5 (images endpoint): the card is the first `input_references` entry, the
  references follow. OpenRouter documents no separate edit endpoint and no `input_fidelity`
  for this model; the experiment script records whether the provider accepts it.

Same guards as generation (`OpenRouterImageBackend`): project ledger with reserve/settle,
pinned provider (`provider.only`), `allow_fallbacks = false`, `data_collection = deny`.
Only images leave the machine (the card and the customer references the user allowed
for all generators on 2026-11-03); no human labels are sent.
"""

from __future__ import annotations

import base64
import hashlib
import io
from pathlib import Path

from PIL import Image

from cardforge.contracts.generation import PromptBundle, Reference, RenderParams, RenderResult
from cardforge.render.openrouter_image import OpenRouterImageBackend

# restyle-v1: the wording Q3 measured (2026-11-06, GPT 2.5 edit 20:5); kept for the records of
# the experiment scripts. It asks for «crisp bright specular highlights» and a «soft rim light»,
# which the render template ref-v6 of the same cards forbids (user's off-style reasons: sharp
# highlights, off-style light) — the runtime uses restyle-v2 with the ref-v6 light and metal.
INSTRUCTION_V1_VERSION = "restyle-v1"
INSTRUCTION_V1 = (
    "Edit the first image, the game card to edit. Repaint only how its surfaces and light are "
    "rendered so that they match the art style of the reference cards that follow: smooth "
    "hand-painted 3D look, softly rounded bevelled edges, clean gradients on every surface, soft "
    "broad highlights, matte smooth base materials, polished metal with crisp bright specular "
    "highlights, painted wood grain, bright frontal top light with gentle ambient occlusion and a "
    "soft rim light, cheerful clean saturated colours; no photographic textures, no grunge, no "
    "noise, no outlines, no harsh shadows.\n"
    "Keep everything else exactly as it is in the first image: the same object, silhouette, "
    "shape, proportions, parts and their count, pose, camera angle, perspective, composition, "
    "framing, position and size in the frame, the same backdrop design and its colours. Do not "
    "add, remove, move or redraw any element and do not add text. Use the reference cards only "
    "for how materials and light are painted; never copy their objects, backdrops or layout. "
    "Return the edited card in the same aspect ratio."
)
# restyle-v2 (2026-11-12): restyle-v1 with the light, highlight and metal wording of ref-v6
# (`light`, materials `metal_chrome`); everything about keeping the card is unchanged.
# Not measured on paid edits yet: Q3 measured restyle-v1.
INSTRUCTION_VERSION = "restyle-v2"
INSTRUCTION = (
    "Edit the first image, the game card to edit. Repaint only how its surfaces and light are "
    "rendered so that they match the art style of the reference cards that follow: smooth "
    "hand-painted 3D look, softly rounded bevelled edges, clean gradients on every surface, "
    "matte smooth base materials, metal as a smooth light-to-mid gradient with one soft "
    "highlight, painted wood grain, one soft key light from the upper front: each large form gets "
    "one smooth light-to-dark gradient and at most one or two broad, soft-edged highlights, "
    "gentle ambient occlusion in creases, cheerful clean saturated colours; no rim light, no "
    "sparkles, no glints, no mirror reflections, no photographic textures, no grunge, no noise, "
    "no outlines, no harsh shadows.\n"
    "Keep everything else exactly as it is in the first image: the same object, silhouette, "
    "shape, proportions, parts and their count, pose, camera angle, perspective, composition, "
    "framing, position and size in the frame, the same backdrop design and its colours. Do not "
    "add, remove, move or redraw any element and do not add text. Use the reference cards only "
    "for how materials and light are painted; never copy their objects, backdrops or layout. "
    "Return the edited card in the same aspect ratio."
)
EDIT_ROLES = ("style", "material", "category")  # the background reference is not about materials

PROFILES = {
    # GPT Image 2.5 Sunburst, as or-gpt-image-2-5 (config/runtime.toml), provider pinned.
    "gpt": {
        "model": "openai/gpt-image-2.5-sunburst",
        "endpoint": "images",
        "aspect_ratio": "3:4",
        "send_seed": False,
        "extra": {"quality": "medium"},
        "provider_only": ["openai"],
        "allow_openai": True,
        "estimate_usd": 0.08,
    },
    # Nano Banana 2.1, as or-nano-banana-v2; 3:4 keeps the GPT card's frame.
    "nano": {
        "model": "google/gemini-nano-banana-2.1",
        "endpoint": "chat",
        "aspect_ratio": "3:4",
        "send_seed": True,
        "extra": {},
        "provider_only": ["google-ai-studio"],
        "allow_openai": False,
        "estimate_usd": 0.05,
    },
}


def _data_url(png: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(png).decode()


class EditBackend(OpenRouterImageBackend):
    """`OpenRouterImageBackend` with the source card prepended to the image inputs."""

    def __init__(
        self, root: Path, model: str, *, source_png: bytes, provider_options: dict | None = None,
        **kw,
    ):  # fmt: skip
        super().__init__(root, model, send_references=True, **kw)
        self.source_png = source_png
        self.provider_options = provider_options

    def request_body(self, bundle: PromptBundle, cfg: dict) -> dict:
        body = super().request_body(bundle, cfg)
        src = {"type": "image_url", "image_url": {"url": _data_url(self.source_png)}}
        if self.endpoint == "images":
            body["input_references"] = [src, *body.get("input_references", [])]
        else:
            content = body["messages"][0]["content"]
            content[1:1] = [{"type": "text", "text": "Image to edit (the card)"}, src]
        if self.provider_options:
            body["provider"]["options"] = self.provider_options
        return body


def edit_bundle(arm: str, refs: list[Reference], seed: int, instruction: str = INSTRUCTION):
    p = PROFILES[arm]
    bundle = PromptBundle(
        backend="openrouter-image-edit",
        model_id=p["model"],
        prompt_text=instruction,
        references=[r for r in refs if r.role in EDIT_ROLES],
        params=RenderParams(width=864, height=960, seed=seed),
        template_version=INSTRUCTION_VERSION,
    )
    return bundle.model_copy(update={"hash": bundle.compute_hash()})


def make_backend(
    root: Path, arm: str, source_png: bytes, provider_options: dict | None = None
) -> EditBackend:
    p = PROFILES[arm]
    return EditBackend(
        root,
        p["model"],
        source_png=source_png,
        provider_options=provider_options,
        estimate_usd=p["estimate_usd"],
        aspect_ratio=p["aspect_ratio"],
        provider_only=p["provider_only"],
        endpoint=p["endpoint"],
        send_seed=p["send_seed"],
        extra=p["extra"],
        allow_openai=p["allow_openai"],
        max_refs=len(EDIT_ROLES) + 2,
    )


def source_key(source_png: bytes, bundle: PromptBundle) -> str:
    return hashlib.sha256(source_png + bundle.hash.encode()).hexdigest()


def match_frame(edited_png: bytes, size: tuple[int, int]) -> Image.Image:
    """The edited raw resized to the source raw size so the source crop window applies."""
    with Image.open(io.BytesIO(edited_png)) as im:
        img = im.convert("RGB")
    if img.size != size:
        img = img.resize(size, Image.Resampling.LANCZOS)
    return img


def run_edit(
    root: Path, arm: str, source_png: bytes, refs: list[Reference], seed: int, *,
    provider_options: dict | None = None, timeout_s: int = 180,
) -> tuple[PromptBundle, RenderResult]:  # fmt: skip
    bundle = edit_bundle(arm, refs, seed)
    backend = make_backend(root, arm, source_png, provider_options)
    return bundle, backend.render(bundle, timeout_s)
