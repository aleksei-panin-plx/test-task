"""Gemini text-only judging of an integrity-checked exchange pack; bounded and resumable."""

from __future__ import annotations

import json
import os
import re
import tomllib
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import dotenv_values

from cardforge.evaluate.api_budget import Ledger, token_cost
from cardforge.evaluate.common import dump, sha256
from cardforge.evaluate.judge_pack import pack_asset
from cardforge.evaluate.local_batch import fingerprint
from cardforge.evaluate.models import JudgeAnswer


class FlexCapacityUnavailable(ValueError):
    """A received HTTP 503, distinct from an ambiguous timeout or lost response."""

    code = 503


def settings(root: Path) -> tuple[dict, Ledger]:
    dev = tomllib.loads((root / "config/cardforge.toml").read_text())["development"]
    cfg = tomllib.loads((root / "config/evaluation.toml").read_text())["gemini"]
    if not dev.get("paid_api_enabled") or not 0 < dev.get("api_budget_usd", 0) <= 8:
        raise ValueError("Gemini evaluation requires explicitly authorized budget")
    today = datetime.now(ZoneInfo("Europe/Belgrade")).date().isoformat()
    if not cfg["price_verified_date"] <= today <= cfg["price_valid_through"]:
        raise ValueError("reverify pricing before API calls")
    return cfg, Ledger(root / "runs/_api/ledger.json", dev["api_budget_usd"])


def run(
    root: Path,
    pack: Path,
    *,
    limit: int | None = None,
    ids: set[str] | None = None,
    finalize: bool = True,
    stop_event=None,
    expected_rubric: str | None = None,
    thinking_level: str | None = None,
    service_tier: str = "standard",
    attempt_tag: str | None = None,
) -> dict:
    from google import genai
    from google.genai import types

    cfg, ledger = settings(root)
    if service_tier not in ("standard", "flex"):
        raise ValueError("unsupported service tier; no automatic fallback")
    if attempt_tag is not None and re.fullmatch(r"[A-Za-z0-9_-]{1,80}", attempt_tag) is None:
        raise ValueError("invalid explicit billing attempt tag")
    selected_thinking = thinking_level or cfg.get("thinking_level", "LOW")
    if selected_thinking not in ("LOW", "MEDIUM"):
        raise ValueError("bounded Flash experiments support LOW or MEDIUM thinking only")
    requested_rubric = expected_rubric or cfg["rubric_version"]
    if requested_rubric not in (
        "rubric7-human-guidance-v2",
        "rubric7-human-guidance-v3",
        "rubric7-human-guidance-v4-fewshot",
        "rubric7-quality-priority-v5",
        "rubric7-severity-v6",
    ):
        raise ValueError("unsupported judge rubric")
    if limit is not None and limit <= 0:
        raise ValueError("positive request limit required")
    if (
        cfg["model"] != "gemini-3.8-flash"
        or cfg["input_usd_per_million"] != 0.75
        or cfg["output_usd_per_million"] != 3.75
        or cfg["max_total_generated_tokens"] < cfg["max_output_tokens"]
    ):
        raise ValueError("model/pricing contract requires explicit revalidation")
    public = json.loads((pack / "pack.json").read_text())
    if public.get("run_settings", {}).get("service_tier", service_tier) != service_tier:
        raise ValueError("service tier contradicts the frozen experiment")
    if public.get("run_settings", {}).get("thinking_level", selected_thinking) != selected_thinking:
        raise ValueError("thinking level contradicts the frozen experiment")
    key = json.loads(
        (root / f"dataset/analysis/evaluation/keys/{public['pack_id']}.json").read_text()
    )
    if Path(key["output"]) != pack.resolve() or public.get("rubric_version") != requested_rubric:
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
    api_key = os.getenv("GEMINI_API_KEY") or dotenv_values(root / ".env", interpolate=False).get(
        "GEMINI_API_KEY"
    )
    if not api_key:
        raise ValueError("missing GEMINI_API_KEY")
    output = pack / "gemini-responses"
    output.mkdir(exist_ok=True)
    config = types.GenerateContentConfig(
        **({"service_tier": types.ServiceTier.FLEX} if service_tier == "flex" else {}),
        temperature=0.0,
        max_output_tokens=cfg["max_output_tokens"],
        thinking_config=types.ThinkingConfig(thinking_level=selected_thinking),
        response_mime_type="application/json",
        response_json_schema=JudgeAnswer.model_json_schema(),
        response_modalities=["TEXT"],
        automatic_continuation=False,
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
    )
    identity = {
        "provider": "gemini",
        "host": "gemini-api",
        "model": cfg["model"],
        "family": "Google Gemini",
        "role": "working",
    }
    reviewed = 0
    with genai.Client(
        api_key=api_key,
        vertexai=False,
        http_options=types.HttpOptions(
            timeout=900000 if service_tier == "flex" else 90000,
            retry_options=types.HttpRetryOptions(attempts=1),
        ),
    ) as client:
        for item in public["items"]:
            if stop_event is not None and stop_event.is_set():
                break
            if ids is not None and item["item_id"] not in ids:
                continue
            prompt = (
                (pack / "README.txt").read_text() + "\nВерните только JudgeAnswer для "
                "одной карты. Пояснения краткие, конкретные, на русском. Не видите "
                "человеческих меток; не пытайтесь их угадать.\n"
                + json.dumps(
                    {k: item[k] for k in ("item_id", "image_sha256", "spec")}, ensure_ascii=False
                )
            )
            names = [item["image"], item["thumbnail"], *item["refs"]]
            contents = [types.Part.from_text(text=prompt)]
            teaching_names = []
            for example in public.get("teacher_examples", []):
                name = example["image"]
                teaching_names.append(name)
                contents.extend(
                    [
                        types.Part.from_text(
                            text="Обучающий пример, не оцениваемая карта: "
                            + json.dumps(
                                {k: v for k, v in example.items() if k != "image"},
                                ensure_ascii=False,
                            )
                        ),
                        types.Part.from_bytes(
                            data=pack_asset(root, pack, name).read_bytes(), mime_type="image/png"
                        ),
                    ]
                )
            labels = ["Оцениваемое изображение", "Миниатюра 215x240"] + [
                f"Reference: {context['role']}, category {context.get('category', 'unspecified')}"
                for context in item.get("reference_context", [])
            ]
            if "reference_context" not in item:
                labels += [f"Style reference {n + 1}" for n in range(len(item["refs"]))]
            if len(labels) != len(names):
                raise ValueError("reference role/image count mismatch")
            for label, name in zip(labels, names, strict=True):
                contents.extend(
                    [
                        types.Part.from_text(text=label),
                        types.Part.from_bytes(
                            data=pack_asset(root, pack, name).read_bytes(), mime_type="image/png"
                        ),
                    ]
                )
            request_id = fingerprint(
                {
                    "model": cfg["model"],
                    "config": config.model_dump(mode="json"),
                    "prompt": prompt,
                    "files": {n: sha256(pack_asset(root, pack, n)) for n in names + teaching_names},
                    **({"image_labels": labels} if "reference_context" in item else {}),
                    **({"teacher_examples": public["teacher_examples"]} if teaching_names else {}),
                }
            )
            response_path = output / f"{item['item_id']}.json"
            if response_path.exists():
                saved = json.loads(response_path.read_text())
                if saved["request_id"] != request_id:
                    raise ValueError("response belongs to a different frozen request")
                cached = JudgeAnswer.model_validate(saved["answer"])
                if cached.item_id != item["item_id"] or cached.image_sha256 != item["image_sha256"]:
                    raise ValueError("cached answer ID/SHA mismatch")
                continue
            billing_request_id = (
                fingerprint({"content_request_id": request_id, "attempt_tag": attempt_tag})
                if attempt_tag is not None
                else request_id
            )
            # Response-schema overhead is covered by the conservative reservation margin.
            count = client.models.count_tokens(model=cfg["model"], contents=contents).total_tokens
            if not count or count > cfg["max_input_tokens"]:
                raise ValueError("unexpected input size; no generation request sent")
            # Counted payload plus an extra schema/protocol margin; double output allowance.
            input_ceiling = count * 2 + 4096
            ceiling = token_cost(
                input_ceiling,
                cfg["max_total_generated_tokens"],
                cfg["input_usd_per_million"],
                cfg["output_usd_per_million"],
            )
            ledger.reserve(
                billing_request_id,
                ceiling,
                {
                    "provider": "gemini",
                    "model": cfg["model"],
                    "pack_id": public["pack_id"],
                    "item_id": item["item_id"],
                    "counted_input_tokens": count,
                    "input_ceiling": input_ceiling,
                    **(
                        {"content_request_id": request_id, "attempt_tag": attempt_tag}
                        if attempt_tag is not None
                        else {}
                    ),
                },
            )
            try:
                response = client.models.generate_content(
                    model=cfg["model"], contents=contents, config=config
                )
            except genai.errors.ServerError as exc:
                if service_tier != "flex" or exc.code != 503:
                    raise
                receipt = {
                    "request_id": request_id,
                    "billing_request_id": billing_request_id,
                    "item_id": item["item_id"],
                    "http_status": 503,
                    "known_terminal_provider_error": True,
                    "billing_reference": "https://ai.google.dev/gemini-api/docs/billing",
                    "capacity_reference": "https://ai.google.dev/gemini-api/docs/generate-content/flex-inference",
                    "accounted_upper_usd": "0",
                    "attempt_tag": attempt_tag,
                }
                dump(output / f"{item['item_id']}.{billing_request_id[:12]}.error.json", receipt)
                # Google's billing FAQ excludes failed server requests. Only a received
                # 503 is settled here; timeouts/unknown outcomes retain their full ceiling.
                ledger.settle(billing_request_id, Decimal(0), receipt)
                raise FlexCapacityUnavailable("Flex capacity unavailable; receipt saved") from exc
            usage = (
                response.usage_metadata.model_dump(mode="json") if response.usage_metadata else None
            )
            if usage is None:
                raise ValueError("missing usage; reservation kept, no further requests")
            prompt_tokens = usage.get("prompt_token_count") or 0
            generated = max(
                (usage.get("candidates_token_count") or 0)
                + (usage.get("thoughts_token_count") or 0),
                (usage.get("total_token_count") or 0) - prompt_tokens,
            )
            headers = getattr(getattr(response, "sdk_http_response", None), "headers", None) or {}
            reported_tier = next(
                (
                    str(v).lower()
                    for k, v in headers.items()
                    if k.lower() == "x-gemini-service-tier"
                ),
                None,
            )
            if service_tier == "flex" and reported_tier not in (None, "flex", "standard"):
                dump(
                    output / f"{item['item_id']}.raw.json",
                    {
                        "request_id": request_id,
                        "text": response.text,
                        "usage": usage,
                        "service_tier_requested": service_tier,
                        "service_tier_reported": reported_tier,
                    },
                )
                raise ValueError("unknown billed tier; reservation retained for reconciliation")
            # Flex never falls back in this client. Reserve at Standard prices above;
            # settle at published Flex rates, or Standard if the server explicitly reports it.
            flex_billed = service_tier == "flex" and reported_tier in (None, "flex")
            divisor = 2 if flex_billed else 1
            cost = token_cost(
                prompt_tokens,
                generated,
                cfg["input_usd_per_million"] / divisor,
                cfg["output_usd_per_million"] / divisor,
            )
            # Keep raw output even if schema validation fails; never retry automatically.
            dump(
                output / f"{item['item_id']}.raw.json",
                {
                    "request_id": request_id,
                    **(
                        {"billing_request_id": billing_request_id, "attempt_tag": attempt_tag}
                        if attempt_tag is not None
                        else {}
                    ),
                    "text": response.text,
                    "usage": usage,
                    "accounted_upper_usd": str(cost),
                    "model_version": response.model_version,
                    **(
                        {
                            "service_tier_requested": service_tier,
                            "service_tier_reported": reported_tier,
                        }
                        if service_tier == "flex"
                        else {}
                    ),
                    "finish_reasons": [str(c.finish_reason) for c in response.candidates or []],
                },
            )
            ledger.settle(billing_request_id, cost, usage)
            if service_tier == "flex" and reported_tier not in (None, "flex"):
                raise ValueError("unexpected billed tier; stopped without automatic fallback")
            answer = JudgeAnswer.model_validate_json(response.text or "")
            if answer.item_id != item["item_id"] or answer.image_sha256 != item["image_sha256"]:
                raise ValueError("Gemini answer ID/SHA mismatch")
            dump(response_path, {"request_id": request_id, "answer": answer.model_dump()})
            reviewed += 1
            print(f"DONE {item['item_id']} decision={answer.decision} upper_usd={cost}", flush=True)
            if limit is not None and reviewed >= limit:
                break
    paths = [output / f"{r['item_id']}.json" for r in public["items"]]
    if finalize and all(p.exists() for p in paths):
        dump(
            pack / "answers.gemini.json",
            {
                "pack_id": public["pack_id"],
                "judge": identity,
                "answers": [json.loads(p.read_text())["answer"] for p in paths],
            },
        )
    summary = {
        "new_answers": reviewed,
        "complete": all(p.exists() for p in paths),
        "saved_answers": sum(p.exists() for p in paths),
        "n": len(paths),
        "budget": ledger.summary(),
        "model": cfg["model"],
        "thinking_level": selected_thinking,
        **({"service_tier": service_tier} if service_tier == "flex" else {}),
    }
    if finalize:
        dump(pack / "gemini-run.json", summary)
    return summary
