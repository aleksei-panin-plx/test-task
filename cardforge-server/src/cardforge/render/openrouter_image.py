"""Image models through OpenRouter (user request 2026-11-03: try them against local FLUX).

Same guards as the OpenRouter judge: the project ledger (`config/evaluation.toml
[openrouter]`), the price window, no OpenAI models or providers, no fallbacks,
`data_collection = deny`. Each call is reserved at an upper estimate before it is sent and
settled at the cost OpenRouter reports. Text-only by default: customer reference images are
sent only with `send_references = true`, which needs the user's explicit consent.
"""

from __future__ import annotations

import base64
import io
import os
import tomllib
import uuid
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import dotenv_values
from PIL import Image

from cardforge.contracts.generation import BackendCaps, PromptBundle, RenderResult
from cardforge.orchestrator.execution import TransientError


def openrouter_ledger(root: Path, ledger_path: Path | None = None):
    from cardforge.evaluate.api_budget import Ledger

    cfg = tomllib.loads((root / "config/evaluation.toml").read_text())["openrouter"]
    today = datetime.now(ZoneInfo("Europe/Belgrade")).date().isoformat()
    # Development: prices are re-verified by hand before paid campaigns. Folder 1 (designers) sets
    # `enforce_price_dates = false` (user 2026-11-10), so generation never stops on a calendar date.
    enforce = cfg.get("enforce_price_dates", True)
    if enforce and not cfg["price_verified_date"] <= today <= cfg["price_valid_through"]:
        raise ValueError("reverify OpenRouter pricing before API calls")
    path = ledger_path or root / cfg["ledger"]
    return cfg, Ledger(path, cfg["budget_usd"], cfg["authorized_max_usd"])


class OpenRouterImageBackend:
    id = "openrouter-image"

    def __init__(
        self,
        root: Path,
        model: str,
        *,
        estimate_usd: float,
        aspect_ratio: str = "4:5",
        provider_only: list[str] | None = None,
        zdr: bool = False,
        send_references: bool = False,
        endpoint: str = "chat",
        send_seed: bool = True,
        max_refs: int = 6,
        extra: dict | None = None,
        allow_openai: bool = False,
        ledger_path: Path | None = None,
        transport=None,
    ):
        # OpenAI image models only for generation tests the user allowed (2026-11-03,
        # docs/decisions.md); judges never use OpenAI.
        if model.startswith("openai/") and not allow_openai:
            raise ValueError("OpenAI models are not authorized")
        self.allow_openai = allow_openai
        self.max_refs = max_refs
        self.extra = extra or {}
        self.root = root
        self.model = model
        self.estimate_usd = estimate_usd
        self.aspect_ratio = aspect_ratio
        self.provider_only = provider_only
        self.zdr = zdr
        self.send_references = send_references
        if endpoint not in ("chat", "images"):
            raise ValueError("endpoint must be chat or images")
        # Pure image models (FLUX 3) are served only by /api/v1/images.
        self.endpoint = endpoint
        self.send_seed = send_seed  # FLUX 3 on OpenRouter rejects `seed`
        self.ledger_path = ledger_path
        self.transport = transport

    def capabilities(self) -> BackendCaps:
        return BackendCaps(
            max_refs=self.max_refs if self.send_references else 0,
            supports_seed=True,
            supports_negative=False,
            supports_img2img=False,
            supports_edit=False,
            supports_lora=False,
            native_sizes=None,
            local=False,
            license_class="commercial_api",
        )

    def request_body(self, bundle: PromptBundle, cfg: dict) -> dict:
        content: list[dict] = [{"type": "text", "text": bundle.prompt_text}]
        for ref in bundle.references if self.send_references else []:
            data = base64.b64encode((self.root / ref.path).read_bytes()).decode()
            content += [
                {"type": "text", "text": f"Reference ({ref.role})"},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{data}"}},
            ]
        provider = {
            "allow_fallbacks": False,
            "data_collection": "deny",
            "ignore": [
                p
                for p in cfg["forbidden_providers"]
                if not (self.allow_openai and self.model.startswith(f"{p}/"))
            ],
            **({"only": self.provider_only} if self.provider_only else {}),
            **({"zdr": True} if self.zdr else {}),
        }
        # Google rejects seeds above int32; some models take no seed at all.
        seed = {"seed": bundle.params.seed % 2**31} if self.send_seed else {}
        if self.endpoint == "images":
            # OpenRouter docs: {"type": "image_url", "image_url": {"url": <URL or data URL>}}.
            refs = [
                {
                    "type": "image_url",
                    "image_url": {
                        "url": "data:image/png;base64,"
                        + base64.b64encode((self.root / ref.path).read_bytes()).decode()
                    },
                }
                for ref in (bundle.references if self.send_references else [])
            ]
            return {
                "model": self.model,
                "prompt": bundle.prompt_text,
                "n": 1,
                "aspect_ratio": self.aspect_ratio,
                **({"input_references": refs} if refs else {}),
                **self.extra,
                **seed,
                "provider": provider,
                "usage": {"include": True},
            }
        return {
            "model": self.model,
            "messages": [{"role": "user", "content": content}],
            "modalities": ["image", "text"],
            "image_config": {"aspect_ratio": self.aspect_ratio},
            **seed,
            "provider": provider,
            "usage": {"include": True},
            "stream": False,
        }

    def render(self, bundle: PromptBundle, timeout_s: int) -> RenderResult:
        import httpx

        cfg, ledger = openrouter_ledger(self.root, self.ledger_path)
        key = os.getenv("OPENROUTER_API_KEY") or dotenv_values(
            self.root / ".env", interpolate=False
        ).get("OPENROUTER_API_KEY")
        if not key:
            raise ValueError("missing OPENROUTER_API_KEY")
        body = self.request_body(bundle, cfg)
        rid = f"image:{bundle.hash[:16]}:{uuid.uuid4().hex[:8]}"
        # Upper bound: three times the profile estimate; settled at the reported cost.
        try:
            ledger.reserve(
                rid,
                Decimal(str(self.estimate_usd * 3)),
                {"provider": "openrouter", "model": self.model, "kind": "image",
                 "bundle": bundle.hash},
            )  # fmt: skip
        except ValueError as exc:
            # The project ledger refused: nothing was sent, nothing was spent.
            return RenderResult(status="error", cost_usd=0.0, error=f"provider_budget: {exc}")
        try:
            with httpx.Client(
                base_url=cfg["base_url"],
                headers={"Authorization": f"Bearer {key}", "X-Title": "CardForge stage 4 render"},
                timeout=httpx.Timeout(timeout_s, connect=30.0),
                transport=self.transport,
            ) as client:
                path = "/images" if self.endpoint == "images" else "/chat/completions"
                reply = client.post(path, json=body)
        except httpx.HTTPError as exc:
            # Unknown outcome: the reservation stays (it may have been billed).
            raise TransientError(f"{self.model}: transport error, reservation kept: {exc}") from exc
        data = reply.json() if "json" in reply.headers.get("content-type", "") else {}
        usage = data.get("usage") or {}
        cost = Decimal(str(usage["cost"])) if usage.get("cost") is not None else None
        if reply.status_code != 200:
            # Rejected before generation costs nothing; a reported cost is always settled.
            ledger.settle(rid, cost or Decimal(0), {"http_status": reply.status_code, **usage})
            if reply.status_code == 429 or reply.status_code >= 500:
                try:
                    after = float(reply.headers.get("retry-after") or "")
                except ValueError:
                    after = None
                raise TransientError(
                    f"{self.model}: HTTP {reply.status_code}",
                    status=reply.status_code,
                    retry_after_s=after,
                )
            return RenderResult(
                status="error", error=f"HTTP {reply.status_code}: {str(data)[:300]}"
            )
        ledger.settle(rid, cost if cost is not None else Decimal(str(self.estimate_usd * 3)), usage)
        if self.endpoint == "images":
            item = (data.get("data") or [{}])[0]
            data = {
                **data,
                "choices": [
                    {
                        "message": {
                            "images": [
                                {"image_url": {"url": "data:image/png;base64," + item["b64_json"]}}
                            ]
                            if item.get("b64_json")
                            else []
                        }
                    }
                ],
            }
        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        images = message.get("images") or []
        meta = {
            "cost_usd": float(cost) if cost is not None else self.estimate_usd * 3,
            "model_revision": str(data.get("model") or self.model),
            "provider_request_id": data.get("id"),
        }
        if not images:
            blocked = choice.get("finish_reason") in ("content_filter", "safety")
            return RenderResult(
                status="blocked_by_provider" if blocked else "error",
                error=f"no image returned: {str(message.get('content') or choice)[:300]}",
                **meta,
            )
        url = images[0]["image_url"]["url"]
        raw = base64.b64decode(url.split(",", 1)[1])
        with Image.open(io.BytesIO(raw)) as im:
            buf = io.BytesIO()
            im.convert("RGB").save(buf, format="PNG")
        return RenderResult(status="ok", image_png=buf.getvalue(), **meta)

    def close(self) -> None:
        pass
