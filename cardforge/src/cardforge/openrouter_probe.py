# ruff: noqa: E501  (Russian fix texts)
"""OpenRouter route probes for `cardforge doctor` (stage 7, user «Да, поручи», 2026-11-10).

A designer may paste a key of another OpenRouter account. The pipeline needs two account settings
(`docs/decisions.md`, «GPT-судья: маршрутизация OpenRouter», 2026-11-04):
1. Guardrails of the account or the key allow the OpenAI provider (GPT Image) — else «Filter by Guardrails»;
2. Privacy allows requests without ZDR (GPT Image has no ZDR endpoint) — else «Filter by Data Policy».

For every pinned route the full mode needs (the main render profile, its reserve, the shadow judges)
one minimal text request is sent with the same `provider` settings as the real calls (`max_tokens`
1, cost ≈ $0). Image models would draw a picture, so an image profile is probed through a text model
of the same provider (`probe_model` in config/runtime.toml) — guardrails and privacy filter by provider
and data policy, not by the model. Each probe is reserved and settled in the OpenRouter ledger under
the shared paid lock, like any paid call. Runs only with a key and not offline.
"""

from __future__ import annotations

import os
import tomllib
import uuid
from decimal import Decimal
from pathlib import Path

from dotenv import dotenv_values

GUARDRAILS_URL = "https://openrouter.ai/settings/guardrails"
PRIVACY_URL = "https://openrouter.ai/settings/privacy"
KEYS_URL = "https://openrouter.ai/settings/keys"
CREDITS_URL = "https://openrouter.ai/settings/credits"
PROBE_ESTIMATE_USD = Decimal("0.002")
PROMPT = "ping"


def openrouter_key(root: Path) -> str | None:
    return os.environ.get("OPENROUTER_API_KEY") or dotenv_values(
        Path(root) / ".env", interpolate=False
    ).get("OPENROUTER_API_KEY")


def _toml(root: Path, rel: str) -> dict:
    return tomllib.loads((Path(root) / rel).read_text(encoding="utf-8"))


def routes(root: Path) -> list[dict]:
    """Pinned routes of the full mode, with the provider settings of the real calls."""
    runtime = _toml(root, "config/runtime.toml")
    evaluation = _toml(root, "config/evaluation.toml")["openrouter"]
    forbidden = list(evaluation["forbidden_providers"])
    profiles = runtime["render_profiles"]
    names: list[str] = []
    for name in runtime["defaults"].get("primary_render_profiles", []):
        names.append(name)
        if profiles[name].get("fallback"):
            names.append(profiles[name]["fallback"])
    out = []
    for name in dict.fromkeys(names):
        p = profiles[name]
        if p.get("backend") != "openrouter-image":
            continue
        model = p["model_id"]
        allow_openai = p.get("allow_openai", False)
        provider = {
            "allow_fallbacks": False,
            "data_collection": "deny",
            "ignore": [f for f in forbidden if not (allow_openai and model.startswith(f"{f}/"))],
            **({"only": p["provider_only"]} if p.get("provider_only") else {}),
            **({"zdr": True} if p.get("zdr") else {}),
        }
        out.append({"id": name, "purpose": "рендер" if name in runtime["defaults"].get(
            "primary_render_profiles", []) else "резервный рендер", "model": p.get("probe_model", model),
            "real_model": model, "provider": provider, "openai": allow_openai})  # fmt: skip
    designer = _toml(root, "config/cardforge.toml").get("designer", {})
    candidates = {c["name"]: c for c in evaluation.get("candidates", [])}
    for judge in designer.get("shadow_judges", []):
        j = runtime["judges"].get(judge, {})
        c = candidates.get(j.get("candidate", ""))
        if j.get("provider") != "openrouter" or c is None:
            continue
        out.append({"id": judge, "purpose": "судья-вето и проверка листа", "model": c["model"],
                    "real_model": c["model"], "openai": False,
                    "provider": {"only": [c["provider"]], "allow_fallbacks": False,
                                 "require_parameters": True, "zdr": True, "data_collection": "deny",
                                 "ignore": forbidden}})  # fmt: skip
    return out


def classify(status: int | None, text: str, route: dict) -> dict:
    """OpenRouter refusal → which account setting to change; anything the provider answered → ok."""
    low = (text or "").lower()
    if status is None:
        return {"status": "warning", "setting": "network",
                "detail": f"OpenRouter недоступен ({text[:120]}): маршрут не проверен"}  # fmt: skip
    if status == 200:
        return {"status": "ok", "setting": None, "detail": "маршрут разрешён"}
    if status == 401:
        return {"status": "error", "setting": "key",
                "detail": f"ключ OpenRouter не принят — проверьте ключ ({KEYS_URL})"}  # fmt: skip
    if status == 402:
        return {"status": "error", "setting": "credits",
                "detail": f"на счёте OpenRouter нет средств ({CREDITS_URL})"}  # fmt: skip
    if "guardrail" in low:
        who = "OpenAI" if route.get("openai") else ", ".join(route["provider"].get("only", [])) or "провайдер модели"
        return {"status": "error", "setting": "guardrails",
                "detail": f"в Guardrails аккаунта или ключа не разрешён провайдер {who} — разрешите его: {GUARDRAILS_URL}"}  # fmt: skip
    if "data policy" in low or "zdr" in low or "data_collection" in low or "privacy" in low:
        need = "разрешите запросы к провайдерам без ZDR (Zero Data Retention)" if not route["provider"].get("zdr") \
            else "разрешите провайдеров с ZDR и политикой «data collection: deny»"
        return {"status": "error", "setting": "privacy",
                "detail": f"настройки Privacy аккаунта не пропускают маршрут — {need}: {PRIVACY_URL}"}  # fmt: skip
    if status in (400, 404) and ("model" in low and ("not" in low or "invalid" in low)):
        return {"status": "warning", "setting": "probe_model",
                "detail": f"модель пробы {route['model']} не найдена — проверка этого маршрута не настроена (config/runtime.toml probe_model)"}  # fmt: skip
    if status == 404 and "no endpoints" in low:
        return {"status": "error", "setting": "routing",
                "detail": f"нет доступного эндпоинта для {route['real_model']} с нужными настройками; проверьте Guardrails ({GUARDRAILS_URL}) и Privacy ({PRIVACY_URL})"}  # fmt: skip
    # 400/5xx from the provider itself: the request passed OpenRouter's filters.
    return {"status": "ok", "setting": None, "detail": f"маршрут разрешён (провайдер ответил {status})"}


def probe(root: Path, *, transport=None, lock: bool = True) -> list[dict]:
    """One tiny request per route; results with the setting to fix. No key → []."""
    import httpx

    from cardforge.render.edit_runtime import paid_lock
    from cardforge.render.openrouter_image import openrouter_ledger

    key = openrouter_key(root)
    if not key:
        return []
    try:
        cfg, ledger = openrouter_ledger(root)
    except (ValueError, OSError) as exc:
        return [{"id": "ledger", "purpose": "журнал расходов", "status": "warning", "setting": "ledger",
                 "detail": f"журнал OpenRouter недоступен ({exc}); маршруты не проверены"}]  # fmt: skip
    lock_path = Path(root) / "runs/_api/paid-run.lock"
    if lock and lock_path.is_dir():
        return [{"id": "lock", "purpose": "проверка маршрутов", "status": "warning", "setting": "busy",
                 "detail": "идёт генерация: проверка маршрутов OpenRouter пропущена, повторите doctor позже"}]  # fmt: skip
    results = []
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    guard = paid_lock(lock_path, wait_s=1.0) if lock else _nolock()
    with guard, httpx.Client(base_url=cfg["base_url"], timeout=30.0, transport=transport,
                             headers={"Authorization": f"Bearer {key}", "X-Title": "CardForge doctor"}) as client:  # fmt: skip
        for route in routes(root):
            rid = f"probe:{route['id']}:{uuid.uuid4().hex[:8]}"
            ledger.reserve(rid, PROBE_ESTIMATE_USD, {"provider": "openrouter", "model": route["model"],
                                                     "kind": "doctor_probe"})  # fmt: skip
            body = {"model": route["model"], "messages": [{"role": "user", "content": PROMPT}],
                    "max_tokens": 1, "provider": route["provider"], "usage": {"include": True}}  # fmt: skip
            try:
                reply = client.post("/chat/completions", json=body)
                status, text = reply.status_code, reply.text
                usage = (reply.json().get("usage") or {}) if "json" in reply.headers.get("content-type", "") else {}
            except httpx.HTTPError as exc:
                status, text, usage = None, str(exc), {}
            cost = Decimal(str(usage["cost"])) if usage.get("cost") is not None else (
                PROBE_ESTIMATE_USD if status == 200 else Decimal(0))  # fmt: skip
            ledger.settle(rid, cost, {"http_status": status, **usage})
            results.append({"id": route["id"], "purpose": route["purpose"], "model": route["model"],
                            **classify(status, text, route)})  # fmt: skip
    return results


class _nolock:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False
