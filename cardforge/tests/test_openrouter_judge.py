from decimal import Decimal
from pathlib import Path

import pytest

from cardforge.evaluate.api_budget import Ledger
from cardforge.evaluate.openrouter_judge import request_body, settings

ROOT = Path(__file__).resolve().parents[1]


def test_every_candidate_pins_one_zdr_provider_without_fallbacks():
    cfg, _, _ = settings(ROOT, "qwen3.8-27b")
    for candidate in cfg["candidates"]:
        body = request_body(cfg, candidate, [{"type": "text", "text": "x"}])
        assert body["provider"] == {
            "only": [candidate["provider"]],
            "allow_fallbacks": False,
            "require_parameters": True,
            "zdr": True,
            "data_collection": "deny",
            "ignore": ["openai", "azure"],
        }
        assert body["response_format"]["json_schema"]["strict"] is True
        assert not candidate["model"].startswith("openai/")


def test_unknown_candidate_is_refused():
    with pytest.raises(ValueError, match="unknown OpenRouter candidate"):
        settings(ROOT, "gpt-anything")


def test_openrouter_ledger_is_separate_and_capped(tmp_path):
    with pytest.raises(ValueError):
        Ledger(tmp_path / "or.json", 16, 15)
    ledger = Ledger(tmp_path / "or.json", 15, 15)
    ledger.reserve("a", Decimal("14.9"), {})
    with pytest.raises(ValueError, match="exhausted"):
        ledger.reserve("b", Decimal("0.2"), {})
    with pytest.raises(ValueError):
        Ledger(tmp_path / "gemini.json", 9)
