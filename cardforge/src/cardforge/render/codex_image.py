"""GPT image generation through the Codex CLI built-in image tool (TA §10.5).

Runs on the user's ChatGPT/Codex subscription: no API key, no paid OpenAI API. The image
model behind the tool is not exposed (stage 0: `verified_image_model: null`), so the profile
records the requested Codex model only. No seed; strictly sequential (`local=True` in the
caps puts it under the global lock); about 1.5 min per image. Text-only unless reference
images are allowed for this provider.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
import tomllib
from pathlib import Path

from PIL import Image

from cardforge.contracts.generation import BackendCaps, PromptBundle, RenderResult

INSTRUCTION = (
    "\n\nTask for this isolated session: generate exactly one image for the description above "
    "with your built-in image generation tool, portrait orientation. Save the generated PNG "
    "as output.png in the current working directory (copy it from where the tool stores it). "
    "Do not edit, crop or draw the image yourself, do not run other agents, web searches or "
    "external APIs. Final message: the path of output.png or the error."
)


class CodexImageBackend:
    id = "codex"

    def __init__(
        self,
        root: Path,
        model: str,
        *,
        effort: str = "medium",
        timeout_s: int = 600,
        send_references: bool = False,
        max_refs: int = 6,
    ):
        self.root = root
        # User consent 2026-11-03: reference images may be sent to all generators.
        self.send_references = send_references
        self.max_refs = max_refs
        self.model = model
        self.effort = effort
        self.timeout_s = timeout_s
        self.binary = tomllib.loads((root / "config/tools.toml").read_text())["host_cli"]["codex"]

    def capabilities(self) -> BackendCaps:
        return BackendCaps(
            max_refs=self.max_refs if self.send_references else 0,
            supports_seed=False,
            supports_negative=False,
            supports_img2img=False,
            supports_edit=False,
            supports_lora=False,
            native_sizes=None,
            local=True,  # one at a time: parallel runs mix up the generated files
            license_class="commercial_api",
        )

    @staticmethod
    def cli_error(stream: str) -> str | None:
        for line in reversed(stream.splitlines()):
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get("type") in ("error", "turn.failed"):
                return str(event.get("message") or (event.get("error") or {}).get("message"))
        return None

    def render(self, bundle: PromptBundle, timeout_s: int) -> RenderResult:
        scratch = Path(tempfile.mkdtemp(prefix="cardforge-codex-image-", dir="/private/tmp"))
        argv = [
            self.binary, "exec", "--skip-git-repo-check", "--ephemeral", "-m", self.model,
            "-c", f'model_reasoning_effort="{self.effort}"', "-s", "workspace-write",
            "-C", str(scratch), "--json", "--color", "never",
        ]  # fmt: skip
        for ref in bundle.references if self.send_references else []:
            # Attached in prompt order: "Image 1", "Image 2", ... match the role labels.
            argv += ["-i", str((self.root / ref.path).resolve())]
        argv.append("-")  # the prompt comes on stdin because -i takes positional values
        env = {
            k: v for k, v in os.environ.items() if k not in ("OPENAI_API_KEY", "OPENAI_BASE_URL")
        }
        started = time.monotonic()
        try:
            proc = subprocess.run(
                argv,
                input=bundle.prompt_text + INSTRUCTION,
                cwd=scratch,
                env=env,
                capture_output=True,
                text=True,
                timeout=min(self.timeout_s, max(timeout_s, 60)),
            )
        except subprocess.TimeoutExpired:
            return RenderResult(status="timeout", error=f"codex image: no result in {timeout_s}s")
        (scratch / "stream.jsonl").write_text(proc.stdout)
        output = scratch / "output.png"
        if not output.exists():
            error = self.cli_error(proc.stdout) or (proc.stderr or "")[-300:] or "no output.png"
            limit = "credits" in error.lower() or "limit" in error.lower()
            return RenderResult(
                status="error",
                error=("provider_limit: " if limit else "") + error[:300],
                model_revision=f"codex:{self.model}",
            )
        with Image.open(output) as im:
            png = scratch / "output.normalized.png"
            im.convert("RGB").save(png, format="PNG")
        data = png.read_bytes()
        shutil.rmtree(scratch, ignore_errors=True)
        return RenderResult(
            status="ok",
            image_png=data,
            cost_usd=0.0,
            model_revision=f"codex:{self.model} (image model not exposed)",
            provider_request_id=f"codex-{int(started)}",
        )

    def close(self) -> None:
        pass
