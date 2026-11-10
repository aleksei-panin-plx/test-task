"""Local FLUX.2 klein 4B q4 through mflux (TA §10.6), the stage-2 pipeline settings.

864×960, 4 steps, guidance 1.0, `flow_match_euler_discrete`; the core then crops to 430×480.
MLX is imported only when the first image is requested (base imports never touch MLX);
the model stays loaded for the run. The weights must match the authorized checkpoint
(`docs/stages/evidence/02/local-36-weights.json`). Strictly sequential under the GPU lock.
"""

from __future__ import annotations

import io
import json
import os
import sys
import time
import tomllib
from pathlib import Path

from cardforge.contracts.generation import BackendCaps, PromptBundle, RenderResult

SIZE = (864, 960)


class MfluxBackend:
    id = "mflux"

    def __init__(self, root: Path, profile: str = "local-klein4b"):
        self.root = root
        self.profile = profile
        self.config = tomllib.loads((root / "config/models.toml").read_text())["models"][profile]
        self.folder = root / f"models/{self.config['id']}-q{self.config['quantize']}"
        self._model = None
        self.load_s: float | None = None

    def capabilities(self) -> BackendCaps:
        return BackendCaps(
            max_refs=0,
            supports_seed=True,
            supports_negative=False,
            supports_img2img=False,
            supports_edit=False,
            supports_lora=True,
            native_sizes=[SIZE],
            local=True,
            license_class="open",
        )

    def verify_weights(self) -> dict:
        from cardforge.evaluate.common import sha256

        weights = {
            "revision": self.config["download_revision"],
            "files": {
                str(p.relative_to(self.folder)): sha256(p)
                for p in sorted(self.folder.rglob("*"))
                if p.is_file()
            },
        }
        authorized = json.loads(
            (self.root / "docs/stages/evidence/02/local-36-weights.json").read_text()
        )[self.profile]
        if weights != authorized:
            raise ValueError("weights differ from the authorized local checkpoint")
        return weights

    def _load(self):
        if self._model is not None:
            return self._model
        os.environ.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1")
        self.verify_weights()
        started = time.monotonic()
        # Same construction as scripts/local_calibration.worker (stage 2).
        from mflux.callbacks.instances.memory_saver import MemorySaver
        from mflux.models.common.compute_precision import ComputePrecision
        from mflux.models.common.resolution.config_resolution import ConfigResolution
        from mflux.models.common.vae.vae_decode_options import VAEDecodeOptions
        from mflux.models.flux2.cli.flux2_generate import build_parser
        from mflux.models.flux2.variants import Flux2Klein

        # mflux's parser reads sys.argv only (as in the stage-2 worker).
        saved = sys.argv
        sys.argv = [
            self.profile, "--model", str(self.folder), "--base-model", self.config["id"],
            "--prompt", "batch prompts supplied per call", "--seed", "42", "--low-ram",
        ]  # fmt: skip
        try:
            args = build_parser().parse_args()
        finally:
            sys.argv = saved
        model = Flux2Klein(
            model_config=ConfigResolution.resolve_restricted(
                args.model,
                "flux2-klein-4b",
                model_path=args.model_path,
                base_model=args.base_model,
            ),
            quantize=args.quantize,
            model_path=args.model_path,
            compute_precision=ComputePrecision.dtype_for(args.compute_precision),
        )
        VAEDecodeOptions.apply(args, model)
        model.callbacks.register(
            MemorySaver(
                model=model,
                keep_transformer=True,
                keep_text_encoder=True,
                cache_limit_bytes=1000**3,
                args=args,
                num_seeds=10**6,
            )
        )
        self._model = model
        self.load_s = round(time.monotonic() - started, 1)
        return self._model

    def render(self, bundle: PromptBundle, timeout_s: int) -> RenderResult:
        import mlx.core as mx

        model = self._load()
        mx.reset_peak_memory()
        generated = model.generate_image(
            seed=bundle.params.seed,
            prompt=bundle.prompt_text,
            width=bundle.params.width,
            height=bundle.params.height,
            num_inference_steps=bundle.params.steps or self.config["steps"],
            guidance=bundle.params.guidance if bundle.params.guidance is not None else 1.0,
            scheduler="flow_match_euler_discrete",
        )
        image = generated.image.convert("RGB")
        if image.size != (bundle.params.width, bundle.params.height):
            return RenderResult(status="error", error=f"unexpected size {image.size}")
        buf = io.BytesIO()
        image.save(buf, format="PNG")
        return RenderResult(
            status="ok",
            image_png=buf.getvalue(),
            cost_usd=0.0,
            model_revision=self.config["revision"],
            peak_memory_gb=round(mx.get_peak_memory() / 1e9, 2),
        )

    def close(self) -> None:
        self._model = None
