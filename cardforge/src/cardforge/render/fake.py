"""FakeBackend: offline images for e2e tests and `cardforge demo` (TA §10.1).

`dataset` mode returns the category reference from the ignored reference folder (a real
card of the needed category); `synthetic` draws a deterministic placeholder from the seed.
`auto` uses the dataset when the file exists. Failure knobs exercise retries, provider
blocks, budget accounting and crash/resume without any network.
"""

from __future__ import annotations

import hashlib
import io
import random
import threading
import time
from pathlib import Path

from PIL import Image, ImageDraw

from cardforge.contracts.generation import BackendCaps, PromptBundle, RenderResult
from cardforge.orchestrator.execution import TransientError


class SimulatedCrash(BaseException):
    """Stands in for a killed process: not an Exception, so nothing in the core catches it."""


class FakeBackend:
    id = "fake"

    def __init__(
        self,
        *,
        source: str = "synthetic",
        root: Path | None = None,
        cost_per_image_usd: float = 0.0,
        transient_failures: int = 0,
        blocked_objects: tuple[str, ...] = (),
        crash_after: int | None = None,
        latency_s: float = 0.0,
    ):
        if source not in ("synthetic", "dataset", "auto"):
            raise ValueError("source must be synthetic, dataset or auto")
        self.source = source
        self.root = root
        self.cost_per_image_usd = cost_per_image_usd
        self.transient_left = transient_failures
        self.blocked_objects = blocked_objects
        self.crash_after = crash_after
        self.latency_s = latency_s
        self.calls = 0
        self.images = 0
        self._lock = threading.Lock()

    def capabilities(self) -> BackendCaps:
        return BackendCaps(
            max_refs=6,
            supports_seed=True,
            supports_negative=False,
            supports_img2img=False,
            supports_edit=False,
            supports_lora=False,
            native_sizes=None,
            local=False,
            license_class="test",
        )

    def render(self, bundle: PromptBundle, timeout_s: int) -> RenderResult:
        with self._lock:
            self.calls += 1
            if self.crash_after is not None and self.images >= self.crash_after:
                raise SimulatedCrash(f"fake crash after {self.images} images")
            if self.transient_left > 0:
                self.transient_left -= 1
                raise TransientError("fake 503", status=503)
            self.images += 1
        if self.latency_s:
            time.sleep(self.latency_s)
        rid = f"fake-{bundle.hash[:12]}"
        if any(name in bundle.prompt_text for name in self.blocked_objects):
            return RenderResult(
                status="blocked_by_provider",
                cost_usd=self.cost_per_image_usd,
                provider_request_id=rid,
                error="fake safety block",
            )
        image = self._dataset(bundle) or self._synthetic(bundle)
        buf = io.BytesIO()
        image.save(buf, format="PNG")
        return RenderResult(
            status="ok",
            image_png=buf.getvalue(),
            cost_usd=self.cost_per_image_usd,
            model_revision="fake-v1",
            provider_request_id=rid,
        )

    def _dataset(self, bundle: PromptBundle) -> Image.Image | None:
        if self.source == "synthetic":
            return None
        for ref in bundle.references:
            path = Path(ref.path)
            if self.root and not path.is_absolute():
                path = self.root / path
            if ref.role == "category" and path.is_file():
                with Image.open(path) as im:
                    return im.convert("RGB").resize(
                        (bundle.params.width, bundle.params.height), Image.Resampling.LANCZOS
                    )
        if self.source == "dataset":
            raise FileNotFoundError("dataset mode needs an existing category reference")
        return None

    def _synthetic(self, bundle: PromptBundle) -> Image.Image:
        seed = int(hashlib.sha256(bundle.hash.encode()).hexdigest()[:8], 16)
        rng = random.Random(seed)
        w, h = bundle.params.width, bundle.params.height
        bg = tuple(rng.randint(120, 255) for _ in range(3))
        image = Image.new("RGB", (w, h), bg)
        draw = ImageDraw.Draw(image)
        for i in range(0, w + h, max(8, w // 12)):  # a graphic motif
            draw.line([(i, 0), (i - h, h)], fill=tuple(max(0, c - 25) for c in bg), width=6)
        # A few seeded shapes in different places keep placeholder images distinct for dHash.
        for n in range(3):
            fg = tuple(rng.randint(20, 220) for _ in range(3))
            cx, cy = rng.randint(w // 4, 3 * w // 4), rng.randint(h // 4, 2 * h // 3)
            rx = rng.randint(w // 10, w // (4 if n else 3))
            ry = rng.randint(h // 10, h // (4 if n else 3))
            box = [cx - rx, cy - ry, cx + rx, cy + ry]
            shape = rng.choice(("rect", "ellipse", "triangle"))
            if shape == "rect":
                draw.rounded_rectangle(box, radius=rx // 3, fill=fg)
            elif shape == "ellipse":
                draw.ellipse(box, fill=fg)
            else:
                draw.polygon([(cx, cy - ry), (cx + rx, cy + ry), (cx - rx, cy + ry)], fill=fg)
        return image

    def close(self) -> None:
        pass
