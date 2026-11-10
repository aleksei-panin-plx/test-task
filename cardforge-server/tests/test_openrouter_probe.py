# ruff: noqa: E501  (mock payloads)
"""OpenRouter route probes in doctor (stage 7, user «Да, поручи», 2026-11-10). Mock HTTP only."""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import httpx
import pytest

from cardforge import doctor, openrouter_probe

ROOT = Path(__file__).resolve().parents[1]


def _set(text: str, key: str, value: str) -> str:
    """`key = …` line → `key = value` (the comment after it dropped); the key must exist."""
    out, n = re.subn(rf"(?m)^{key} = .*$", f"{key} = {value}", text)
    assert n == 1, key
    return out


@pytest.fixture
def root(tmp_path) -> Path:
    """The development project's settings, whatever copy runs the test (the project, folder 1 or
    folder 2 carry different `route_probe`, ledger cap and price-date switches)."""
    (tmp_path / "config").mkdir()
    shutil.copy(ROOT / "config/runtime.toml", tmp_path / "config/runtime.toml")
    cfg = (ROOT / "config/cardforge.toml").read_text(encoding="utf-8")
    (tmp_path / "config/cardforge.toml").write_text(_set(cfg, "route_probe", "false"), encoding="utf-8")
    ev = (ROOT / "config/evaluation.toml").read_text(encoding="utf-8")
    ev = re.sub(r'price_verified_date = "[^"]+"', 'price_verified_date = "2000-01-01"', ev)
    ev = re.sub(r'price_valid_through = "[^"]+"', 'price_valid_through = "2099-12-31"', ev)
    for key, value in (("budget_usd", "176.2"), ("authorized_max_usd", "176.2"),
                       ("enforce_price_dates", "true"), ("enforce_ledger_cap", "true")):  # fmt: skip
        ev = _set(ev, key, value)
    (tmp_path / "config/evaluation.toml").write_text(ev, encoding="utf-8")
    (tmp_path / ".env").write_text("OPENROUTER_API_KEY=sk-test\n")
    return tmp_path


def transport(answers: dict[str, tuple[int, dict]], seen: list | None = None):
    """model → (status, json); default 200 with a tiny cost."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if seen is not None:
            seen.append(body)
        status, data = answers.get(body["model"], (200, {"usage": {"cost": 0.00001}}))
        return httpx.Response(status, json=data)

    return httpx.MockTransport(handler)


def test_routes_use_the_real_provider_settings(root):
    r = {x["id"]: x for x in openrouter_probe.routes(root)}
    gpt = r["or-gpt-image-2-5"]
    assert gpt["openai"] and "openai" not in gpt["provider"]["ignore"]
    assert "zdr" not in gpt["provider"] and gpt["provider"]["data_collection"] == "deny"
    assert gpt["model"] != gpt["real_model"]  # an image model would draw: a text model of the provider
    kimi = r["kimi-veto-v2"]["provider"]
    assert kimi["only"] == ["moonshotai/mxfp4"] and kimi["zdr"] and kimi["require_parameters"]


def test_all_routes_allowed_are_journaled(root):
    seen: list = []
    res = openrouter_probe.probe(root, transport=transport({}, seen), lock=False)
    assert {x["status"] for x in res} == {"ok"} and len(res) == 3
    assert all(b["max_tokens"] == 1 for b in seen)
    ledger = json.loads((root / "runs/_api/openrouter-ledger.json").read_text())
    probes = [e for e in ledger["entries"].values() if e.get("metadata", e).get("kind", "") == "doctor_probe"
              or "probe:" in json.dumps(e)]  # fmt: skip
    assert len(probes) >= 3


def test_guardrails_and_privacy_refusals_block_full_mode(root, monkeypatch):
    answers = {
        "openai/gpt-6.1-sol": (404, {"error": {"message": "No endpoints found matching your guardrail restrictions (Filter by Guardrails)"}}),
        "moonshotai/kimi-k3.1-pro": (404, {"error": {"message": "No endpoints found matching your data policy (Filter by Data Policy)"}}),
    }  # fmt: skip
    res = {x["id"]: x for x in openrouter_probe.probe(root, transport=transport(answers), lock=False)}
    assert res["or-gpt-image-2-5"]["setting"] == "guardrails" and "OpenAI" in res["or-gpt-image-2-5"]["detail"]
    assert openrouter_probe.GUARDRAILS_URL in res["or-gpt-image-2-5"]["detail"]
    assert res["kimi-veto-v2"]["setting"] == "privacy" and openrouter_probe.PRIVACY_URL in res["kimi-veto-v2"]["detail"]
    assert res["or-nano-banana-v2"]["status"] == "ok"
    check = doctor.plugin_check(True, list(res.values()), probed=True)
    assert check.status == "error" and check.detail.startswith("blocked")


@pytest.mark.parametrize(
    "status, message, setting, level",
    [
        (401, "No auth credentials found", "key", "error"),
        (402, "Insufficient credits", "credits", "error"),
        (400, "openai/gpt-6.1-sol is not a valid model ID", "probe_model", "warning"),
        (400, "max_tokens is too small for this model", None, "ok"),  # the provider answered
    ],
)  # fmt: skip
def test_classify(status, message, setting, level):
    route = {"model": "m", "real_model": "m", "openai": True, "provider": {}}
    got = openrouter_probe.classify(status, json.dumps({"error": {"message": message}}), route)
    assert got["status"] == level and got["setting"] == setting


def test_busy_paid_lock_and_no_key_skip_the_probe(root, monkeypatch):
    (root / "runs/_api/paid-run.lock").mkdir(parents=True)
    res = openrouter_probe.probe(root, transport=transport({}))
    assert res[0]["setting"] == "busy" and res[0]["status"] == "warning"
    (root / ".env").unlink()
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    assert openrouter_probe.probe(root, transport=transport({})) == []


def test_doctor_probe_switch(root, monkeypatch):
    """Development project: `route_probe = false` — doctor never makes a paid call by itself."""
    called = []
    monkeypatch.setattr(openrouter_probe, "probe", lambda r, **kw: called.append(1) or [])
    doctor.collect(root)
    assert called == []
    doctor.collect(root, probe=True)
    assert called == [1]


def test_price_date_check_is_off_in_folder_1(root, monkeypatch):
    """User 2026-11-10 «Убрать проверку в папке 1»: an expired price-verification date stops paid
    calls in the development project, never in folder 1 (`enforce_price_dates = false`)."""
    from cardforge.evaluate import openrouter_judge
    from cardforge.render.openrouter_image import openrouter_ledger

    ev = root / "config/evaluation.toml"
    expired = re.sub(r'price_valid_through = "[^"]+"', 'price_valid_through = "2001-01-01"', ev.read_text())
    ev.write_text(expired)
    with pytest.raises(ValueError, match="reverify"):
        openrouter_ledger(root)
    with pytest.raises(ValueError, match="reverify"):
        openrouter_judge.settings(root, "kimi-k3.1-pro")
    ev.write_text(expired.replace("enforce_price_dates = true", "enforce_price_dates = false"))
    assert openrouter_ledger(root)[0]["enforce_price_dates"] is False
    openrouter_judge.settings(root, "kimi-k3.1-pro")
    build_dist = ROOT / "scripts/build_dist.py"  # folder 2 does not carry it
    if build_dist.exists():
        assert "enforce_price_dates = false" in build_dist.read_text(encoding="utf-8")


def test_ledger_cap_is_off_in_folder_1(root):
    """User 2026-11-10 «Да, убрать»: folder 1 has no per-machine $176.2 ledger ceiling; the ledger
    still journals every call. The development project keeps the ceiling."""
    from decimal import Decimal

    from cardforge.evaluate import openrouter_judge
    from cardforge.render.openrouter_image import openrouter_ledger

    ev = root / "config/evaluation.toml"
    text = ev.read_text()
    assert "budget_usd = 176.2\nauthorized_max_usd = 176.2\n" in text
    big = text.replace("budget_usd = 176.2\nauthorized_max_usd = 176.2\n",
                       "budget_usd = 1000000.0\nauthorized_max_usd = 1000000.0\n")  # fmt: skip
    ev.write_text(big)
    with pytest.raises(ValueError, match="176.2"):
        openrouter_judge.settings(root, "kimi-k3.1-pro")  # the cap rule is still on
    ev.write_text(big.replace("enforce_ledger_cap = true", "enforce_ledger_cap = false"))
    openrouter_judge.settings(root, "kimi-k3.1-pro")
    _, ledger = openrouter_ledger(root)
    ledger.reserve("big", Decimal("500"), {"kind": "test"})  # above the old ceiling
    assert Decimal(ledger.summary()["remaining_usd"]) > Decimal("999000")
