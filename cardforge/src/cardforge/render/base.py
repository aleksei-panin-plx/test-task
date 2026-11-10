"""Render backend interface (TA §10.1)."""

from __future__ import annotations

from typing import Protocol

from cardforge.contracts.generation import BackendCaps, PromptBundle, RenderResult


class RenderBackend(Protocol):
    id: str

    def capabilities(self) -> BackendCaps: ...

    def render(self, bundle: PromptBundle, timeout_s: int) -> RenderResult:
        """Return the image or a provider block; raise TransientError for 429/5xx/timeouts."""
        ...

    def close(self) -> None: ...
