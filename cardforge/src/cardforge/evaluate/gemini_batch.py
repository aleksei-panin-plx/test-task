"""Resumable, budget-reserved offline judging through Google's native Batch API."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

from cardforge.evaluate.api_budget import token_cost
from cardforge.evaluate.common import dump, sha256
from cardforge.evaluate.gemini_judge import settings
from cardforge.evaluate.judge_pack import Submission, pack_asset
from cardforge.evaluate.local_batch import fingerprint
from cardforge.evaluate.models import JudgeAnswer


def native_response_schema() -> dict:
    """Resolve Pydantic refs into the native Schema subset for Batch responseSchema."""
    source = JudgeAnswer.model_json_schema()

    def resolve(value):
        if isinstance(value, list):
            return [resolve(x) for x in value]
        if not isinstance(value, dict):
            return value
        if "$ref" in value:
            return resolve(source["$defs"][value["$ref"].split("/")[-1]])
        if "anyOf" in value:
            branches = value["anyOf"]
            nonnull = [x for x in branches if x.get("type") != "null"]
            if len(nonnull) == 1 and len(branches) == 2:
                return {**resolve(nonnull[0]), "nullable": True}
        return {
            key: resolve(item)
            for key, item in value.items()
            if key not in ("$defs", "title", "additionalProperties")
        }

    return resolve(source)


class BatchJudge:
    """A fresh, fixed campaign. Completed answers and accepted jobs are never resubmitted."""

    def __init__(self, root: Path, pack: Path, client):
        from google.genai import types

        self.root, self.pack, self.client, self.types = root, pack, client, types
        self.cfg, self.ledger = settings(root)
        if self.cfg["model"] not in ("gemini-3.8-flash", "gemini-3.6-flash") or (
            self.cfg["input_usd_per_million"],
            self.cfg["output_usd_per_million"],
        ) != (0.75, 3.75):
            raise ValueError("Batch pricing/model contract requires explicit revalidation")
        self.public = json.loads((pack / "pack.json").read_text())
        self.key = json.loads(
            (
                root / ("dataset/analysis/evaluation/keys/" + self.public["pack_id"] + ".json")
            ).read_text()
        )
        if Path(self.key["output"]) != pack.resolve():
            raise ValueError("Unregistered batch pack")
        if self.public.get("teacher_examples"):
            raise ValueError("This batch campaign does not transmit teaching labels")
        frozen_settings = dict(self.public.get("run_settings", {}))
        selected_model = frozen_settings.pop("model", self.cfg["model"])
        if selected_model not in ("gemini-3.8-flash", "gemini-3.6-flash"):
            raise ValueError("Unsupported frozen Batch model/pricing contract")
        self.cfg = {**self.cfg, "model": selected_model}
        schema_mode = frozen_settings.pop("schema_mode", "json-schema")
        if schema_mode not in ("json-schema", "native-schema"):
            raise ValueError("Unknown frozen batch schema mode")
        thinking_level = frozen_settings.pop("thinking_level", None)
        if thinking_level not in ("LOW", "MEDIUM", "HIGH"):
            raise ValueError("Unexpected frozen batch thinking level")
        if frozen_settings != {
            "service_tier": "batch",
            "max_output_tokens": 1024,
            "max_total_generated_tokens": 2048,
        }:
            raise ValueError("Unexpected frozen batch generation settings")
        for name, digest in self.key["files"].items():
            if sha256(pack_asset(root, pack, name)) != digest:
                raise ValueError("Changed frozen batch input")
        self.items = {x["item_id"]: x for x in self.public["items"]}
        if len(self.items) != len(self.public["items"]):
            raise ValueError("Duplicate batch item")
        self.output = pack / "gemini-responses"
        self.jobs = pack / "gemini-batch-jobs"
        self.output.mkdir(exist_ok=True)
        self.jobs.mkdir(exist_ok=True)
        self.prepared = {}
        schema_config = (
            {"response_json_schema": JudgeAnswer.model_json_schema()}
            if schema_mode == "json-schema"
            else {"response_schema": types.Schema.model_validate(native_response_schema())}
        )
        self.config = types.GenerateContentConfig(
            temperature=0.0,
            max_output_tokens=1024,
            thinking_config=types.ThinkingConfig(thinking_level=thinking_level),
            response_mime_type="application/json",
            **schema_config,
            response_modalities=["TEXT"],
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            automatic_continuation=False,
        )

    def prompt(self, item: dict) -> str:
        for name in ("README.txt", "pack.json"):
            if sha256(self.pack / name) != self.key["files"][name]:
                raise ValueError("Changed frozen batch prompt or pack")
        return (
            (self.pack / "README.txt").read_text()
            + "\nВерните только JudgeAnswer для одной карты. Пояснения краткие, конкретные, "
            "на русском. Не видите человеческих меток; не пытайтесь их угадать.\n"
            + json.dumps(
                {k: item[k] for k in ("item_id", "image_sha256", "spec")}, ensure_ascii=False
            )
        )

    def request_fingerprint(self, identifier: str) -> str:
        item = self.items[identifier]
        names = [item["image"], item["thumbnail"], *item["refs"]]
        labels = ["Оцениваемое изображение", "Миниатюра 215x240"] + [
            f"Reference: {x['role']}, category {x.get('category', 'unspecified')}"
            for x in item["reference_context"]
        ]
        return fingerprint(
            {
                "model": self.cfg["model"],
                "prompt": self.prompt(item),
                "config": self.config.model_dump(mode="json"),
                "files": {name: self.key["files"][name] for name in names},
                "image_labels": labels,
                "mode": "batch",
            }
        )

    def prepare(self, identifier: str) -> dict:
        if identifier in self.prepared:
            return self.prepared[identifier]
        item = self.items[identifier]
        prompt = self.prompt(item)
        names = [item["image"], item["thumbnail"], *item["refs"]]
        labels = ["Оцениваемое изображение", "Миниатюра 215x240"] + [
            f"Reference: {x['role']}, category {x.get('category', 'unspecified')}"
            for x in item["reference_context"]
        ]
        if len(names) != len(labels):
            raise ValueError("Reference role count mismatch")
        parts = [self.types.Part.from_text(text=prompt)]
        hashes = {}
        size = len(prompt.encode()) + len(json.dumps(self.config.model_dump(mode="json"))) + 4096
        for label, name in zip(labels, names, strict=True):
            path = pack_asset(self.root, self.pack, name)
            digest = sha256(path)
            if digest != self.key["files"][name]:
                raise ValueError("Changed batch image")
            data = path.read_bytes()
            hashes[name] = digest
            size += 4 * ((len(data) + 2) // 3) + len(label.encode()) + 512
            parts.extend(
                [
                    self.types.Part.from_text(text=label),
                    self.types.Part.from_bytes(data=data, mime_type="image/png"),
                ]
            )
        count = self.client.models.count_tokens(
            model=self.cfg["model"], contents=parts
        ).total_tokens
        if not count or not 0 < count <= self.cfg["max_input_tokens"]:
            raise ValueError("Unexpected batch input count; no generation submitted")
        # Published Batch rates, with the same doubled input/schema margin and
        # doubled output allowance as the synchronous driver's conservative reserve.
        ceiling = token_cost(count * 2 + 4096, 2048, 0.375, 1.875)
        request_id = self.request_fingerprint(identifier)
        value = {
            "item_id": identifier,
            "request_id": request_id,
            "ceiling": str(ceiling),
            "payload_upper_bytes": size,
            "request": self.types.InlinedRequest(
                contents=[self.types.Content(role="user", parts=parts)],
                config=self.config,
                metadata={"key": identifier},
            ),
        }
        self.prepared[identifier] = value
        return value

    def submit(self, prepared: list[dict]) -> dict:
        if not prepared or sum(x["payload_upper_bytes"] for x in prepared) > 15_000_000:
            raise ValueError("Batch payload exceeds conservative inline limit")
        identifiers = [x["item_id"] for x in prepared]
        if len(set(identifiers)) != len(identifiers):
            raise ValueError("Duplicate requested batch item")
        # A failed/ambiguous operation is deliberately not retried by this method.
        assigned = set()
        for existing in self.jobs.glob("job-*.json"):
            assigned.update(x["item_id"] for x in json.loads(existing.read_text())["items"])
        if assigned.intersection(identifiers) or self.completed().intersection(identifiers):
            raise ValueError("Batch item already assigned; resume saved job")
        number = (
            max((int(p.stem.removeprefix("job-")) for p in self.jobs.glob("job-*.json")), default=0)
            + 1
        )
        billing_id = fingerprint(
            {
                "pack_id": self.public["pack_id"],
                "batch_number": number,
                "requests": [x["request_id"] for x in prepared],
            }
        )
        state = {
            "state": "prepared",
            "billing_id": billing_id,
            "display_name": f"cardforge-v6-{number:04}-{billing_id[:16]}",
            "items": [{k: x[k] for k in ("item_id", "request_id", "ceiling")} for x in prepared],
        }
        path = self.jobs / f"job-{number:04}.json"
        ceiling = sum((Decimal(x["ceiling"]) for x in prepared), Decimal(0))
        # Reserve atomically as one operation; no remote job exists before this succeeds.
        self.ledger.reserve(
            billing_id,
            ceiling,
            {
                "provider": "gemini",
                "model": self.cfg["model"],
                "pack_id": self.public["pack_id"],
                "execution_mode": "batch",
                "item_ids": [x["item_id"] for x in prepared],
            },
        )
        state.update(state="submitting", ceiling_usd=str(ceiling))
        dump(path, state)
        try:
            remote = self.client.batches.create(
                model=self.cfg["model"],
                src=[x["request"] for x in prepared],
                config={"display_name": state["display_name"]},
            )
        except Exception:
            state["state"] = "submission_unknown"
            dump(path, state)
            raise  # Keep the complete reservation; never blindly create another job.
        if not remote.name:
            raise ValueError("Batch job name missing; keep reservation and inspect")
        state.update(state="submitted", job_name=remote.name)
        dump(path, state)
        for x in prepared:
            self.prepared.pop(x["item_id"], None)
        return {"path": path, "state": state}

    def collect(self, path: Path, state: dict, remote) -> bool:
        if remote.name != state["job_name"]:
            raise ValueError("Remote batch job identity mismatch; reservation retained")
        name = getattr(remote.state, "name", str(remote.state).split(".")[-1])
        if name in ("JOB_STATE_PENDING", "JOB_STATE_RUNNING", "JOB_STATE_QUEUED"):
            state["remote_state"] = name
            dump(path, state)
            return False
        if name != "JOB_STATE_SUCCEEDED":
            state.update(state="remote_failure", remote_state=name)
            dump(path, state)
            raise ValueError("Batch did not succeed; reservation retained for inspection")
        responses = getattr(getattr(remote, "dest", None), "inlined_responses", None)
        if responses is None or len(responses) != len(state["items"]):
            raise ValueError("Missing batch responses; reservation retained")
        expected = {x["item_id"]: x for x in state["items"]}
        for identifier, saved in expected.items():
            if saved["request_id"] != self.request_fingerprint(identifier):
                raise ValueError("Batch request fingerprint changed; reservation retained")
        seen, errors, receipts, records, payloads = set(), [], {}, {}, {}
        total = Decimal(0)
        # Billing/envelope validation is independent of the model's JSON shape.
        # Nothing is settled until every entry has a trusted job/key/usage binding.
        for entry in responses:
            identifier = (entry.metadata or {}).get("key")
            if identifier not in expected or identifier in seen:
                raise ValueError("Missing or duplicate batch metadata binding")
            if entry.error is not None:
                if entry.response is not None:
                    raise ValueError("Conflicting batch error/response; reservation retained")
                code = getattr(entry.error, "code", None)
                if code not in (3, 8, 13, 14, 400, 429, 500, 503):
                    raise ValueError("Unknown batch failure billing; reservation retained")
                receipts[identifier] = {
                    "provider_error_code": code,
                    "billed_upper_usd": "0",
                    "billing_reference": "https://ai.google.dev/gemini-api/docs/billing",
                }
                records[identifier] = {
                    "request_id": expected[identifier]["request_id"],
                    **receipts[identifier],
                    "job_name": state["job_name"],
                    "execution_mode": "batch",
                }
                errors.append(identifier)
                seen.add(identifier)
                continue
            response = entry.response
            if response is None or response.usage_metadata is None:
                raise ValueError("Batch usage missing; reservation retained")
            if response.model_version != self.cfg["model"]:
                raise ValueError("Batch returned an unknown model version; reservation retained")
            usage = response.usage_metadata.model_dump(mode="json")
            prompt_tokens = usage.get("prompt_token_count") or 0
            generated = max(
                (usage.get("candidates_token_count") or 0)
                + (usage.get("thoughts_token_count") or 0),
                (usage.get("total_token_count") or 0) - prompt_tokens,
            )
            if prompt_tokens <= 0 or generated <= 0:
                raise ValueError("Incomplete token usage; reservation retained")
            cost = token_cost(prompt_tokens, generated, 0.375, 1.875)
            total += cost
            records[identifier] = {
                "request_id": expected[identifier]["request_id"],
                "text": response.text,
                "usage": usage,
                "accounted_upper_usd": str(cost),
                "model_version": response.model_version,
                "job_name": state["job_name"],
                "execution_mode": "batch",
            }
            receipts[identifier] = usage
            payloads[identifier] = response.text
            seen.add(identifier)
        if seen != set(expected):
            raise ValueError("Incomplete batch bindings")
        # Check all old receipts before writing any new ones; frozen evidence wins.
        for identifier, record in records.items():
            raw_path = self.output / f"{identifier}.raw.json"
            if raw_path.exists() and json.loads(raw_path.read_text()) != record:
                raise ValueError("Refusing to overwrite frozen batch response")
        for identifier, record in records.items():
            raw_path = self.output / f"{identifier}.raw.json"
            if not raw_path.exists():
                dump(raw_path, record)
        self.ledger.settle(
            state["billing_id"],
            total,
            {
                "execution_mode": "batch",
                "job_name": state["job_name"],
                "requests": receipts,
            },
        )

        # All raw receipts and known billing are durable before semantic parsing.
        # One malformed answer prevents promotion of every answer in this job.
        valid, invalid = [], {}
        for identifier, text in payloads.items():
            try:
                answer = JudgeAnswer.model_validate_json(text or "")
                if answer.item_id != identifier:
                    raise ValueError("Batch metadata/answer identity mismatch")
                if answer.image_sha256 != self.items[identifier]["image_sha256"]:
                    raise ValueError("Batch answer image SHA mismatch")
                valid.append(
                    {
                        "request_id": expected[identifier]["request_id"],
                        "answer": answer.model_dump(),
                    }
                )
            except ValueError as exc:
                invalid[identifier] = {"error_type": type(exc).__name__, "error": str(exc)[:300]}
        if invalid:
            state.update(
                state="response_invalid",
                charged_upper_usd=str(total),
                failed_ids=errors,
                invalid_ids=list(invalid),
                response_errors=invalid,
            )
            dump(path, state)
            raise ValueError(
                "Batch response invalid; raw receipts and known billing saved; "
                "no judgments promoted"
            )
        for value in valid:
            target = self.output / (value["answer"]["item_id"] + ".json")
            if target.exists() and json.loads(target.read_text()) != value:
                raise ValueError("Refusing to overwrite frozen judgment")
        for value in valid:
            target = self.output / (value["answer"]["item_id"] + ".json")
            if not target.exists():
                dump(target, value)
        state.update(state="collected", charged_upper_usd=str(total), failed_ids=errors)
        dump(path, state)
        return True

    def completed(self) -> set[str]:
        return {i for i in self.items if (self.output / f"{i}.json").exists()}

    def assemble(self) -> dict:
        if self.completed() != set(self.items):
            raise ValueError("Batch campaign incomplete")
        answers = []
        for item in self.public["items"]:
            saved = json.loads((self.output / (item["item_id"] + ".json")).read_text())
            if saved["request_id"] != self.request_fingerprint(item["item_id"]):
                raise ValueError("Stale cached batch request fingerprint")
            answer = JudgeAnswer.model_validate(saved["answer"])
            raw = json.loads((self.output / (item["item_id"] + ".raw.json")).read_text())
            if (
                raw["request_id"] != saved["request_id"]
                or JudgeAnswer.model_validate_json(raw["text"]).model_dump() != answer.model_dump()
            ):
                raise ValueError("Raw receipt contradicts cached batch request or answer")
            if answer.item_id != item["item_id"] or answer.image_sha256 != item["image_sha256"]:
                raise ValueError("Stale cached batch response")
            answers.append(answer.model_dump())
        data = {
            "pack_id": self.public["pack_id"],
            "judge": {
                "provider": "gemini",
                "host": "gemini-api",
                "model": self.cfg["model"],
                "family": "Google",
                "role": "working",
            },
            "answers": answers,
        }
        Submission.model_validate(data)
        path = self.pack / "answers.gemini.json"
        if path.exists() and json.loads(path.read_text()) != data:
            raise ValueError("Refusing to overwrite final batch submission")
        if not path.exists():
            dump(path, data)
        return {
            "complete": True,
            "n": len(answers),
            "path": str(path),
            "budget": self.ledger.summary(),
        }
