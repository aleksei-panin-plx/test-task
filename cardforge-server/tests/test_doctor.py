import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from cardforge.cli import app
from cardforge.doctor import collect, select_mode
from cardforge.smoke import preflight


@pytest.mark.parametrize(
    ("codex", "auth", "local", "key", "paid", "expected"),
    [
        (True, True, False, False, False, ("keyless", "host-agent", "closed-gpt-codex")),
        (True, False, False, False, False, ("plan-only", "host-agent", None)),
        (False, False, True, False, False, ("keyless", "host-agent", "local-klein4b-lora")),
        (False, False, False, True, False, ("plan-only", "host-agent", None)),
        (False, False, False, True, True, ("developer-api", "gemini", "closed-nano")),
        (True, True, True, True, True, ("keyless", "host-agent", "closed-gpt-codex")),
    ],
)
def test_mode_selection(codex, auth, local, key, paid, expected):
    assert (
        select_mode(
            codex_present=codex,
            codex_auth_present=auth,
            local_lora_present=local,
            gemini_present=key,
            paid_api_enabled=paid,
        )
        == expected
    )


@pytest.mark.parametrize("present", [True, False])
def test_local_only_never_selects_cloud_renderer_even_with_auth_and_keys(present):
    mode, llm, render = select_mode(
        codex_present=True,
        codex_auth_present=True,
        local_lora_present=False,
        gemini_present=True,
        paid_api_enabled=True,
        local_only=True,
        local_base_present=present,
    )
    assert render == ("local-klein4b" if present else None)
    assert llm == "host-agent"
    assert mode == ("keyless" if present else "plan-only")


def test_secrets_never_appear_in_report(monkeypatch, tmp_path):
    secret = "secret-for-test-do-not-emit"
    monkeypatch.setenv("GEMINI_API_KEY", secret)
    monkeypatch.delenv("HF_TOKEN", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    (tmp_path / ".env").write_text("HF_TOKEN=dotenv-private-token\n")
    report = collect(tmp_path)
    serialized = report.model_dump_json()
    assert secret not in serialized and "dotenv-private-token" not in serialized
    assert report.credentials == {
        "GEMINI_API_KEY": True, "HF_TOKEN": True, "OPENROUTER_API_KEY": False
    }  # fmt: skip
    assert report.paid_api_enabled is False


def test_zero_budget_disables_api_even_when_switch_is_on(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config/cardforge.toml").write_text(
        "[development]\napi_budget_usd=0.0\npaid_api_enabled=true\n"
    )
    assert collect(tmp_path).paid_api_enabled is False


def test_low_disk_is_error(monkeypatch, tmp_path):
    from collections import namedtuple

    usage = namedtuple("usage", "total used free")
    monkeypatch.setattr("cardforge.doctor.shutil.disk_usage", lambda _: usage(200, 199, 1))
    assert collect(tmp_path).has_errors


def test_expired_policy_warns(tmp_path):
    policy = tmp_path / "policies/culture/old.md"
    policy.parent.mkdir(parents=True)
    policy.write_text("---\nreview_after: 2000-01-01\n---\n")
    assert any(
        c.name == "policy_review" and c.status == "warning" for c in collect(tmp_path).checks
    )


def test_cli_json_and_saved_report_agree(tmp_path):
    target = tmp_path / "doctor.json"
    result = CliRunner().invoke(app, ["doctor", "--json", "--output", str(target)])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == json.loads(target.read_text())
    assert json.loads(result.output)["render_access_verified"] is False


def test_preflight_never_calls_api_and_is_explicit_about_unexecuted_checks(monkeypatch, tmp_path):
    monkeypatch.setenv("GEMINI_API_KEY", "present-but-spend-is-forbidden")
    (tmp_path / "config").mkdir()
    (tmp_path / "config/models.toml").write_text((Path.cwd() / "config/models.toml").read_text())
    (tmp_path / "config/cardforge.toml").write_text(
        "[development]\napi_budget_usd=0\npaid_api_enabled=false\nlocal_generation_only=true\n"
    )
    result = preflight(tmp_path)
    assert result["api_spent_usd"] == 0
    assert all(c["executed"] is False for c in result["checks"])
    api = next(c for c in result["checks"] if c["profile"] == "closed-nano")
    assert "paid_api_disabled" in api["reasons"]


def test_funded_evaluation_does_not_enable_closed_image_generation(monkeypatch, tmp_path):
    monkeypatch.setenv("GEMINI_API_KEY", "offline-presence-fixture")
    (tmp_path / "config").mkdir()
    (tmp_path / "config/models.toml").write_text((Path.cwd() / "config/models.toml").read_text())
    (tmp_path / "config/cardforge.toml").write_text(
        "[development]\napi_budget_usd=5\npaid_api_enabled=true\nlocal_generation_only=true\n"
    )
    result = preflight(tmp_path)
    gemini = next(c for c in result["checks"] if c["profile"] == "gemini")
    images = next(c for c in result["checks"] if c["profile"] == "closed-nano")
    assert "paid_api_disabled" not in gemini["reasons"]
    assert images["status"] == "blocked" and "local_generation_only" in images["reasons"]
    assert result["api_spent_usd"] == 0
