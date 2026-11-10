"""OpenRouter judging of a frozen exchange pack: one pinned provider, own ledger, resumable.

Mirrors the Gemini few-shot request (same README, prompt, teaching parts and image labels)
so a candidate is measured on the identical frozen pack. Each call pins a single provider
without fallbacks, with ZDR and data_collection=deny; a provider change is a new judge.
"""

from __future__ import annotations

import base64
import json
import os
import re
import threading
import tomllib
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import dotenv_values
from pydantic import Field, ValidationError

from cardforge.evaluate.api_budget import Ledger, token_cost
from cardforge.evaluate.common import dump, sha256
from cardforge.evaluate.judge_pack import pack_asset
from cardforge.evaluate.local_batch import fingerprint
from cardforge.evaluate.models import JudgeAnswer, Strict

SUPPORTED_RUBRICS = ("rubric7-human-guidance-v4-fewshot", "rubric7-severity-v6")

SCORED_ADDENDUM = """
Дополнение v6-scored (не меняет семь критериев выше). Дополнительно поставьте целые
оценки 1–5 в поле scores, сравнивая с refs этой игры, а не с абстрактным «хорошим 3D»:
- object_style: форма, упрощение и рендер самого предмета;
- material_style: материалы, блики, шероховатость, прозрачность;
- environment_style: фон/опора/окружение и свет сцены, включая читаемость мотива;
- ship_as_is: художник по этой игре добавил бы карту в набор без доработки.
5 — неотличимо от карт игры; 4 — мелкие пожелания; 3 — заметное расхождение, нужна
доработка; 2 — существенно чужая манера или ошибка; 1 — непригодно. Ставьте оценки
независимо от passed; не завышайте за узнаваемость предмета.
"""


class StyleScores(Strict):
    object_style: int = Field(ge=1, le=5)
    material_style: int = Field(ge=1, le=5)
    environment_style: int = Field(ge=1, le=5)
    ship_as_is: int = Field(ge=1, le=5)


class ScoredJudgeAnswer(JudgeAnswer):
    scores: StyleScores


def settings(root: Path, name: str) -> tuple[dict, dict, Ledger]:
    cfg = tomllib.loads((root / "config/evaluation.toml").read_text())["openrouter"]
    today = datetime.now(ZoneInfo("Europe/Belgrade")).date().isoformat()
    enforce = cfg.get("enforce_price_dates", True)  # false in folder 1 (user 2026-11-10)
    if enforce and not cfg["price_verified_date"] <= today <= cfg["price_valid_through"]:
        raise ValueError("reverify OpenRouter endpoint pricing before API calls")
    # $176.2: user top-up of 2026-11-09 (AGENTS.md)
    capped = cfg.get("enforce_ledger_cap", True)  # false in folder 1 (user 2026-11-10)
    if capped and not 0 < cfg["budget_usd"] <= cfg["authorized_max_usd"] <= 176.2:
        raise ValueError("OpenRouter calls require the explicitly authorized $176.2 ceiling")
    matches = [c for c in cfg["candidates"] if c["name"] == name]
    if len(matches) != 1:
        raise ValueError(f"unknown OpenRouter candidate {name!r}")
    candidate = matches[0]
    provider_slug = candidate["provider"].split("/")[0].lower()
    if provider_slug in cfg["forbidden_providers"] or candidate["model"].startswith("openai/"):
        raise ValueError("OpenAI models/providers are not authorized")
    ledger = Ledger(root / cfg["ledger"], cfg["budget_usd"], cfg["authorized_max_usd"])
    return cfg, candidate, ledger


def verify_pack(root: Path, pack: Path, expected_rubric: str) -> tuple[dict, dict]:
    """Same integrity and teaching-approval gate as the Gemini client, before any request."""
    if expected_rubric not in SUPPORTED_RUBRICS:
        raise ValueError("unsupported judge rubric")
    public = json.loads((pack / "pack.json").read_text())
    key = json.loads(
        (root / f"dataset/analysis/evaluation/keys/{public['pack_id']}.json").read_text()
    )
    if Path(key["output"]) != pack.resolve() or public.get("rubric_version") != expected_rubric:
        raise ValueError("unregistered pack or unexpected evaluation rubric")
    for name, digest in key["files"].items():
        if sha256(pack_asset(root, pack, name)) != digest:
            raise ValueError("changed judge input")
    if public.get("teacher_examples"):
        from cardforge.evaluate.judge_inputs import validate_teacher_examples

        approval_path = pack.parent / "approval-status.json"
        preview_path = pack.parent / "approval-preview.json"
        if not approval_path.exists() or not preview_path.exists():
            raise ValueError("explicit teaching-data approval required before any API request")
        approval = json.loads(approval_path.read_text())
        if (
            approval.get("state") != "approved"
            or approval.get("pack_id") != public["pack_id"]
            or approval.get("preview_sha256") != sha256(preview_path)
            or json.loads(preview_path.read_text()) != public["teacher_examples"]
            or approval.get("teaching_image_sha256")
            != {
                e["image"]: sha256(pack_asset(root, pack, e["image"]))
                for e in public["teacher_examples"]
            }
        ):
            raise ValueError("teaching-data approval does not cover the exact frozen payload")
        validate_teacher_examples(key["teacher_examples"], list(key["mapping"].values()))
        for example in key["teacher_examples"]:
            source = example["item"]
            if sha256(root / source["image"]) != source["image_sha256"]:
                raise ValueError("teaching source image changed")
    return public, key


def _image(root: Path, pack: Path, name: str) -> dict:
    data = base64.b64encode(pack_asset(root, pack, name).read_bytes()).decode()
    return {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{data}"}}


def build_content(
    root: Path,
    pack: Path,
    public: dict,
    item: dict,
    teacher: tuple[Path, dict] | None = None,
    addendum: str | None = None,
) -> tuple[list, dict]:
    """Same text and image order as gemini_judge.run; returns parts and hashed identity.

    `teacher` borrows the approved teaching examples of another verified pack.
    """
    teacher_pack, teacher_public = teacher or (pack, public)
    prompt = (
        (pack / "README.txt").read_text() + (addendum or "") + "\nВерните только JudgeAnswer для "
        "одной карты. Пояснения краткие, конкретные, на русском. Не видите "
        "человеческих меток; не пытайтесь их угадать.\n"
        + json.dumps({k: item[k] for k in ("item_id", "image_sha256", "spec")}, ensure_ascii=False)
    )
    names = [item["image"], item["thumbnail"], *item["refs"]]
    content: list[dict] = [{"type": "text", "text": prompt}]
    teaching_names = []
    for example in teacher_public.get("teacher_examples", []):
        teaching_names.append(example["image"])
        content += [
            {
                "type": "text",
                "text": "Обучающий пример, не оцениваемая карта: "
                + json.dumps(
                    {k: v for k, v in example.items() if k != "image"}, ensure_ascii=False
                ),
            },
            _image(root, teacher_pack, example["image"]),
        ]
    labels = ["Оцениваемое изображение", "Миниатюра 215x240"] + [
        f"Reference: {context['role']}, category {context.get('category', 'unspecified')}"
        for context in item.get("reference_context", [])
    ]
    if "reference_context" not in item:
        labels += [f"Style reference {n + 1}" for n in range(len(item["refs"]))]
    if len(labels) != len(names):
        raise ValueError("reference role/image count mismatch")
    for label, name in zip(labels, names, strict=True):
        content += [{"type": "text", "text": label}, _image(root, pack, name)]
    identity = {
        "prompt": prompt,
        "files": {
            n: sha256(pack_asset(root, pack, n))
            for n in names + (teaching_names if teacher is None else [])
        },
        **(
            {
                "teaching_files": {
                    n: sha256(pack_asset(root, teacher_pack, n)) for n in teaching_names
                }
            }
            if teacher is not None
            else {}
        ),
        "image_labels": labels,
        "teacher_examples": teacher_public.get("teacher_examples", []),
    }
    return content, identity


def request_body(
    cfg: dict, candidate: dict, content: list, answer_model: type[JudgeAnswer] = JudgeAnswer
) -> dict:
    body = {
        "model": candidate["model"],
        "messages": [{"role": "user", "content": content}],
        "max_tokens": cfg["max_output_tokens"],
        "reasoning": {"effort": cfg["reasoning_effort"], "exclude": False},
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "JudgeAnswer",
                "strict": True,
                "schema": answer_model.model_json_schema(),
            },
        },
        "provider": {
            "only": [candidate["provider"]],
            "allow_fallbacks": False,
            "require_parameters": True,
            "zdr": True,
            "data_collection": "deny",
            "ignore": cfg["forbidden_providers"],
        },
        "usage": {"include": True},
        "stream": False,
    }
    if "temperature" in candidate:
        body["temperature"] = candidate["temperature"]
    return body


def _parse_answer(text: str, answer_model: type[JudgeAnswer] = JudgeAnswer) -> JudgeAnswer:
    text = text.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
    return answer_model.model_validate_json(fenced.group(1) if fenced else text)


def accounted_call(
    client,
    cfg: dict,
    candidate: dict,
    ledger: Ledger,
    body: dict,
    request_id: str,
    responses: Path,
    stem: str,
    meta: dict,
    stop: threading.Event,
) -> tuple[dict, dict, Decimal]:
    """Reserve, send once, settle and save `<stem>.raw.json`; never retries blindly."""
    import httpx

    ceiling = token_cost(
        cfg["max_input_tokens_ceiling"],
        cfg["max_output_tokens"],
        Decimal(str(candidate["input_usd_per_token"])) * 1_000_000,
        Decimal(str(candidate["output_usd_per_token"])) * 1_000_000,
    )
    # Only receipts of explicit pre-generation rejections (settled at $0) allow a new
    # attempt; an unknown outcome keeps its reservation and blocks the item.
    attempt = sum(
        json.loads(p.read_text()).get("settled_zero") is True
        for p in responses.glob(f"{stem}.*.error.json")
    )
    billing_id = (
        fingerprint({"content_request_id": request_id, "attempt": attempt})
        if attempt
        else request_id
    )
    ledger.reserve(
        billing_id,
        ceiling,
        {
            "provider": "openrouter",
            "model": candidate["model"],
            "pinned_provider": candidate["provider"],
            **meta,
            "input_ceiling": cfg["max_input_tokens_ceiling"],
        },
    )
    try:
        reply = client.post("/chat/completions", json=body)
    except httpx.HTTPError as exc:
        # Unknown network outcome: the reservation stays in full; never retried blindly.
        stop.set()
        raise ValueError(f"{stem}: transport error, reservation kept") from exc
    data = (
        reply.json()
        if reply.headers.get("content-type", "").startswith("application/json")
        else {"text": reply.text}
    )
    if reply.status_code in (400, 401, 402, 403, 404, 422, 429) and "usage" not in data:
        # Rejected before generation (bad parameters, no endpoint, upstream rate limit);
        # OpenRouter does not bill these. Any response carrying usage is billed below.
        receipt = {
            "request_id": request_id,
            "billing_request_id": billing_id,
            "http_status": reply.status_code,
            "body": data,
            "settled_zero": True,
        }
        dump(responses / f"{stem}.{billing_id[:12]}.error.json", receipt)
        ledger.settle(billing_id, Decimal(0), receipt)
        stop.set()
        raise ValueError(f"{stem}: HTTP {reply.status_code} {data}")
    if reply.status_code != 200 or "usage" not in data or not data.get("choices"):
        dump(
            responses / f"{stem}.{billing_id[:12]}.error.json",
            {
                "request_id": request_id,
                "billing_request_id": billing_id,
                "http_status": reply.status_code,
                "body": data,
            },
        )
        stop.set()
        raise ValueError(f"{stem}: HTTP {reply.status_code}; reservation kept")
    usage = data["usage"]
    prompt_tokens = usage.get("prompt_tokens") or 0
    completion_tokens = usage.get("completion_tokens") or 0
    computed = token_cost(
        prompt_tokens,
        completion_tokens,
        Decimal(str(candidate["input_usd_per_token"])) * 1_000_000,
        Decimal(str(candidate["output_usd_per_token"])) * 1_000_000,
    )
    reported = Decimal(str(usage["cost"])) if usage.get("cost") is not None else None
    cost = max(computed, reported) if reported is not None else computed
    message = data["choices"][0]["message"]
    raw = {
        "request_id": request_id,
        "billing_request_id": billing_id,
        "generation_id": data.get("id"),
        "model_requested": candidate["model"],
        "model_reported": data.get("model"),
        "provider_pinned": candidate["provider"],
        "provider_reported": data.get("provider"),
        "system_fingerprint": data.get("system_fingerprint"),
        "finish_reason": data["choices"][0].get("finish_reason"),
        "native_finish_reason": data["choices"][0].get("native_finish_reason"),
        "text": message.get("content"),
        "reasoning_chars": len(message.get("reasoning") or ""),
        "usage": usage,
        "accounted_upper_usd": str(cost),
        "settings": {k: v for k, v in body.items() if k not in ("messages",)},
    }
    dump(responses / f"{stem}.raw.json", raw)
    ledger.settle(billing_id, cost, usage)
    if not str(data.get("model", "")).startswith(candidate["model"]):
        stop.set()
        raise ValueError(f"{stem}: unexpected model {data.get('model')}")
    return raw, message, cost


def run(
    root: Path,
    pack: Path,
    output: Path,
    candidate_name: str,
    *,
    expected_rubric: str = "rubric7-human-guidance-v4-fewshot",
    ids: set[str] | None = None,
    limit: int | None = None,
    workers: int = 1,
    reasoning_effort: str | None = None,
    teacher_pack: Path | None = None,
    scored: bool = False,
    repeat: int = 0,
    transport=None,
) -> dict:
    import httpx

    cfg, candidate, ledger = settings(root, candidate_name)
    public, _ = verify_pack(root, pack, expected_rubric)
    if reasoning_effort is not None:
        if reasoning_effort not in ("low", "medium", "high"):
            raise ValueError("unsupported reasoning effort")
        cfg = {**cfg, "reasoning_effort": reasoning_effort}
    teacher = None
    if teacher_pack is not None:
        if public.get("teacher_examples"):
            raise ValueError("pack already carries its own teaching examples")
        # Re-checks the user's approval binding for the exact frozen teaching payload.
        teacher = (teacher_pack, verify_pack(root, teacher_pack, SUPPORTED_RUBRICS[0])[0])
    api_key = os.getenv("OPENROUTER_API_KEY") or dotenv_values(
        root / ".env", interpolate=False
    ).get("OPENROUTER_API_KEY")
    if not api_key:
        raise ValueError("missing OPENROUTER_API_KEY")
    responses = output / "responses"
    responses.mkdir(parents=True, exist_ok=True)
    selected = [i for i in public["items"] if ids is None or i["item_id"] in ids]
    if limit is not None:
        selected = selected[:limit]
    stop = threading.Event()
    lock = threading.Lock()
    stats = {"new": 0, "invalid": 0, "errors": []}

    def judge(client: httpx.Client, item: dict):
        if stop.is_set():
            return
        content, identity = build_content(
            root, pack, public, item, teacher, SCORED_ADDENDUM if scored else None
        )
        if repeat:
            # Deliberate independent resample of an unchanged request (sampling noise study).
            identity = {**identity, "repeat": repeat}
        answer_model = ScoredJudgeAnswer if scored else JudgeAnswer
        body = request_body(cfg, candidate, content, answer_model)
        request_id = fingerprint(
            {
                "endpoint": "openrouter/chat/completions",
                "settings": {k: v for k, v in body.items() if k != "messages"},
                **identity,
            }
        )
        response_path = responses / f"{item['item_id']}.json"
        raw_path = responses / f"{item['item_id']}.raw.json"
        if response_path.exists() or raw_path.exists():
            saved = json.loads((response_path if response_path.exists() else raw_path).read_text())
            if saved["request_id"] != request_id:
                raise ValueError("saved response belongs to a different frozen request")
            return
        raw, message, cost = accounted_call(
            client,
            cfg,
            candidate,
            ledger,
            body,
            request_id,
            responses,
            item["item_id"],
            {"pack_id": public["pack_id"], "item_id": item["item_id"]},
            stop,
        )
        try:
            answer = _parse_answer(message.get("content") or "", answer_model)
            if answer.item_id != item["item_id"] or answer.image_sha256 != item["image_sha256"]:
                raise ValueError("answer ID/SHA mismatch")
        except (ValidationError, ValueError) as exc:
            with lock:
                stats["invalid"] += 1
            dump(raw_path, {**raw, "invalid": str(exc)[:2000]})
            print(f"INVALID {item['item_id']} upper_usd={cost}: {str(exc)[:200]}", flush=True)
            return
        dump(response_path, {"request_id": request_id, "answer": answer.model_dump()})
        with lock:
            stats["new"] += 1
        print(f"DONE {item['item_id']} decision={answer.decision} upper_usd={cost}", flush=True)

    with httpx.Client(
        base_url=cfg["base_url"],
        headers={"Authorization": f"Bearer {api_key}", "X-Title": "CardForge stage 2 judge"},
        timeout=httpx.Timeout(600.0, connect=30.0),
        transport=transport,
    ) as client:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(judge, client, item) for item in selected]
            for future in futures:
                try:
                    future.result()
                except Exception as exc:  # noqa: BLE001 - collected and reported, run stops
                    stop.set()
                    stats["errors"].append(str(exc)[:500])
    paths = [responses / f"{r['item_id']}.json" for r in public["items"]]
    complete = all(p.exists() for p in paths)
    if complete:
        dump(
            output / "answers.json",
            {
                "pack_id": public["pack_id"],
                "judge": {
                    "provider": "openrouter",
                    "host": candidate["provider"],
                    "model": candidate["model"],
                    "role": "working",
                },
                "answers": [json.loads(p.read_text())["answer"] for p in paths],
            },
        )
    return {
        "candidate": candidate_name,
        "new_answers": stats["new"],
        "invalid_new": stats["invalid"],
        "errors": stats["errors"],
        "saved_answers": sum(p.exists() for p in paths),
        "n": len(paths),
        "complete": complete,
        "budget": ledger.summary(),
    }
