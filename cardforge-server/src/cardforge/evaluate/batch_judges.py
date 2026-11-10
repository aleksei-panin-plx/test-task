"""Batch judges for candidate groups (stage 4, user decision 2026-11-03).

Two deciding judges run side by side over all candidates of a judging round:
- a *group* judge ranks the N candidates of one card position (Kimi K3.1 Pro G1,
  `pairwise-group-v1`: best within-group win rate AND mean `ship_as_is` >= 4 over >= 2 valid
  v6-scored passes; calibrated on groups of 4);
- an *absolute* judge gives every candidate its own verdict (Codex CLI gpt-6.1-sol, rubric v5
  with `definite-failure-first-v1`).

The real adapters reuse the stage-2 calibrated pipeline unchanged (`export_pack`,
`openrouter_judge.run`, `pairwise_style.run_group`, the Codex CLI contract of runs 23/24);
their input contract must stay identical to calibration. Fakes stand in for tests.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
import tomllib
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

from cardforge.contracts.request import CardSpec

QUALITY_PRIORITY = (
    "Game references and the quality rubric govern acceptance. Past generator wording "
    "cannot waive a quality defect."
)
CRISP = (
    "A chosen graphic pattern must have legible graphic boundaries at thumbnail size; "
    "diffuse light is not the requested pattern."
)


@dataclass(frozen=True)
class JudgeItem:
    item_id: str  # "<card_id>.a01.k2"
    card_id: str
    position: str  # card_id + attempt: one group of candidates
    candidate: int
    image: str  # path relative to the project root (stage-2 packs require it)
    image_sha256: str
    spec: dict


@dataclass
class BatchResult:
    """`verdicts[item_id]`: {"accept": bool, "decision": str, ...}; `pending` = wait."""

    verdicts: dict[str, dict] = field(default_factory=dict)
    cost_usd: float = 0.0
    elapsed_s: float = 0.0
    meta: dict[str, Any] = field(default_factory=dict)
    pending: str | None = None


class BatchJudge(Protocol):
    id: str
    kind: str  # "group" | "absolute"

    def estimate_usd(self, items: list[JudgeItem]) -> float: ...

    def judge(self, round_dir: Path, items: list[JudgeItem]) -> BatchResult: ...


def judge_spec(spec: CardSpec, set_theme: str) -> dict:
    """The stage-2 judge spec shape (runs 27 and 48) built from a stage-4 CardSpec."""
    motif = spec.background.motif if spec.background else "unspecified"
    return {
        "object_name": spec.object_name,
        "category": spec.category,
        "set_theme": set_theme,
        "must_have": spec.must_have,
        "reference_tags": {
            "scale_class": spec.scale_class,
            "object_type": spec.object_type,
            "materials": spec.materials,
            "surface": spec.surface or "none",
        },
        "object_description": spec.object_description or spec.object_name,
        "functional_context": {
            "requirement": "Functional details need a plausible use/mounting context; "
            "a souvenir pedestal alone does not supply it.",
            "reference_surface": spec.surface or "none",
            "reference_environment": spec.environment or "none",
            "scope": "Context example, not permission to turn the subject into a toy.",
        },
        "background": {
            "source": "stage-4 collection plan",
            "motif": motif,
            **({"quality_requirement": CRISP} if motif not in ("gradient", "scene") else {}),
        },
        "quality_priority": QUALITY_PRIORITY,
    }


def _roll(*parts: object) -> int:
    return int(hashlib.sha256(":".join(map(str, parts)).encode()).hexdigest(), 16) % 100


def groups_of(items: list[JudgeItem]) -> dict[str, list[JudgeItem]]:
    out: dict[str, list[JudgeItem]] = {}
    for item in items:
        out.setdefault(item.position, []).append(item)
    return out


# ---------------------------------------------------------------- fakes (tests, demo)


class FakeGroupJudge:
    """Picks one candidate per position. `script[position]`: candidate number or None."""

    kind = "group"

    def __init__(
        self,
        id: str = "fake-g1",
        *,
        script: dict | None = None,
        none_rate: int = 0,
        salt: str = "g1",
        cost_per_item_usd: float = 0.0,
    ):
        self.id = id
        self.script = script or {}
        self.none_rate = none_rate
        self.salt = salt
        self.cost_per_item_usd = cost_per_item_usd
        self.calls = 0

    def estimate_usd(self, items: list[JudgeItem]) -> float:
        return self.cost_per_item_usd * len(items)

    def judge(self, round_dir: Path, items: list[JudgeItem]) -> BatchResult:
        self.calls += 1
        result = BatchResult(cost_usd=self.estimate_usd(items))
        for position, members in groups_of(items).items():
            if position in self.script:
                pick = self.script[position]
            elif _roll(self.salt, position) < self.none_rate:
                pick = None
            else:
                pick = members[_roll(self.salt, position, "pick") % len(members)].candidate
            for item in members:
                win = 1.0 if item.candidate == pick else 0.25
                ship = 4.5 if item.candidate == pick else 3.0
                accept = item.candidate == pick
                result.verdicts[item.item_id] = {
                    "accept": accept,
                    "decision": "accept" if accept else "reject",
                    "win": win,
                    "ship": ship,
                    "n_valid": 3,
                    "top": item.candidate == pick or pick is None,
                }
        return result


class FakeAbsoluteJudge:
    """Judges every candidate. `script[item_id or position]`: decision or list per candidate."""

    kind = "absolute"

    def __init__(
        self,
        id: str = "fake-sol",
        *,
        script: dict | None = None,
        fix_rate: int = 20,
        salt: str = "sol",
        cost_per_item_usd: float = 0.0,
    ):
        self.id = id
        self.script = script or {}
        self.fix_rate = fix_rate
        self.salt = salt
        self.cost_per_item_usd = cost_per_item_usd
        self.calls = 0

    def estimate_usd(self, items: list[JudgeItem]) -> float:
        return self.cost_per_item_usd * len(items)

    def judge(self, round_dir: Path, items: list[JudgeItem]) -> BatchResult:
        self.calls += 1
        result = BatchResult(cost_usd=self.estimate_usd(items))
        for item in items:
            scripted = self.script.get(item.item_id, self.script.get(item.position))
            if isinstance(scripted, list):
                scripted = scripted[item.candidate - 1]
            decision = scripted or (
                "fix" if _roll(self.salt, item.item_id) < self.fix_rate else "accept"
            )
            result.verdicts[item.item_id] = {
                "accept": decision == "accept",
                "decision": decision,
                "fix_instruction": None if decision == "accept" else "Сделать фон чище.",
            }
        return result


# ---------------------------------------------------------------- shared pack helpers


def _pack_manifest(root: Path, folder: Path, items: list[JudgeItem]) -> Path:
    rows = [
        {
            "item_id": i.item_id,
            "image": i.image,
            "image_sha256": i.image_sha256,
            "cohort": "borderline",
            "generator_family": "stage-4 run",
            "reference_group": i.position,
            "variant": f"candidate-{i.candidate}",
            "spec": i.spec,
        }
        for i in items
    ]
    path = folder / "manifest.private.json"
    data = {"version": 1, "items": rows}
    if path.exists():
        if json.loads(path.read_text()) != data:
            raise ValueError(f"{path}: frozen judging round differs; start a new round")
    else:
        folder.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    return path


def _export(
    root: Path,
    folder: Path,
    items: list[JudgeItem],
    *,
    seed: int,
    rubric: str,
    contract: str,
    readme_from: Path,
    run_settings: dict | None = None,
) -> tuple[Path, dict]:
    """export_pack with roles-v1, then the calibrated rubric text and contract versions."""
    from cardforge.evaluate.common import dump, sha256
    from cardforge.evaluate.judge_pack import export_pack

    pack = folder / "pack"
    manifest = _pack_manifest(root, folder, items)
    if not (pack / "pack.json").exists():
        export_pack(root, manifest, pack, seed=seed, reference_policy="roles-v1")
        public = json.loads((pack / "pack.json").read_text())
        public.update(rubric_version=rubric, input_contract_version=contract)
        if run_settings is not None:
            public["run_settings"] = run_settings
        dump(pack / "pack.json", public)
        shutil.copyfile(readme_from, pack / "README.txt")
        keypath = root / f"dataset/analysis/evaluation/keys/{public['pack_id']}.json"
        key = json.loads(keypath.read_text())
        key.update(rubric_version=rubric, input_contract_version=contract)
        for name in ("pack.json", "README.txt"):
            key["files"][name] = sha256(pack / name)
        dump(keypath, key)
    public = json.loads((pack / "pack.json").read_text())
    key = json.loads(
        (root / f"dataset/analysis/evaluation/keys/{public['pack_id']}.json").read_text()
    )
    return pack, {"public": public, "key": key}


# ---------------------------------------------------------------- Kimi K3.1 Pro G1


class KimiG1Judge:
    """OpenRouter Kimi K3.1 Pro, profile pairwise-group-v1 G1, s = 4 (stage-2 calibration)."""

    kind = "group"
    candidate = "kimi-k3.1-pro"
    rubric = "rubric7-severity-v6"
    contract = "judge-inputs-severity-v6"
    dev_pack = "runs/stage02/27-claude-severity-v6/pack"
    passes = ("high-scored", "high-scored-r2", "high-scored-r3")
    group_size = 4

    def __init__(
        self,
        root: Path,
        *,
        id: str = "kimi-g1",
        ship_floor: float = 4.0,
        estimate_per_item_usd: float = 0.09,
        workers: int = 4,
        max_429_rounds: int = 40,
        sleep=time.sleep,
    ):
        self.root = root
        self.id = id
        self.ship_floor = ship_floor
        self.estimate_per_item_usd = estimate_per_item_usd
        self.workers = workers
        self.max_429_rounds = max_429_rounds
        self.sleep = sleep

    def estimate_usd(self, items: list[JudgeItem]) -> float:
        return self.estimate_per_item_usd * len(items)

    def ledger(self):
        from cardforge.evaluate.openrouter_judge import settings

        return settings(self.root, self.candidate)[2]

    def _until_done(self, call) -> dict:
        for _ in range(self.max_429_rounds):
            result = call()
            if not result["errors"]:
                return result
            if not all("HTTP 429" in e for e in result["errors"]):
                raise RuntimeError(f"{self.id}: non-429 error: {result['errors'][0][:300]}")
            self.sleep(65)
        raise RuntimeError(f"{self.id}: still rate-limited after {self.max_429_rounds} rounds")

    def judge(self, round_dir: Path, items: list[JudgeItem]) -> BatchResult:
        from cardforge.evaluate.openrouter_judge import run
        from cardforge.evaluate.pairwise_style import group_win_rates, run_group

        started = time.monotonic()
        result = BatchResult()
        groups = groups_of(items)
        calibrated = [i for g in groups.values() if len(g) == self.group_size for i in g]
        for g in groups.values():
            if len(g) != self.group_size:
                for i in g:  # another N is an uncalibrated profile: never accepted by G1
                    result.verdicts[i.item_id] = {"accept": False, "decision": "uncalibrated_n"}
        if not calibrated:
            return result
        dev = json.loads((self.root / self.dev_pack / "pack.json").read_text())
        folder = round_dir / self.id
        pack, info = _export(
            self.root,
            folder,
            calibrated,
            seed=20261106,
            rubric=self.rubric,
            contract=self.contract,
            readme_from=self.root / self.dev_pack / "README.txt",
            run_settings=dev.get("run_settings", {}),
        )
        before = Decimal(self.ledger().summary()["accounted_upper_usd"])
        for n, name in enumerate(self.passes):
            out = folder / "judges" / name
            out.mkdir(parents=True, exist_ok=True)
            self._until_done(
                lambda o=out, r=(0 if n == 0 else n + 1): run(
                    self.root,
                    pack,
                    o,
                    self.candidate,
                    expected_rubric=self.rubric,
                    workers=self.workers,
                    reasoning_effort="high",
                    scored=True,
                    repeat=r,
                )
            )
        mapping = info["key"]["mapping"]
        pack_groups: dict[str, list[dict]] = {}
        for item in info["public"]["items"]:
            pack_groups.setdefault(mapping[item["item_id"]]["reference_group"], []).append(item)
        group_dir = folder / "judges" / "group"
        group_dir.mkdir(parents=True, exist_ok=True)
        self._until_done(
            lambda: run_group(
                self.root,
                pack,
                group_dir,
                self.candidate,
                pack_groups,
                expected_rubric=self.rubric,
                workers=self.workers,
                reasoning_effort="high",
            )
        )
        wins = group_win_rates(group_dir, pack_groups) or {}
        for members in pack_groups.values():
            top = max(wins.get(m["item_id"], 0.0) for m in members)
            for m in members:
                ships = []
                for name in self.passes:
                    path = folder / "judges" / name / "responses" / f"{m['item_id']}.json"
                    if path.exists():
                        ships.append(json.loads(path.read_text())["answer"]["scores"]["ship_as_is"])
                ship = sum(ships) / len(ships) if len(ships) >= 2 else None
                win = wins.get(m["item_id"])
                accept = (
                    win is not None
                    and win == top
                    and ship is not None
                    and (ship >= self.ship_floor)
                )
                result.verdicts[mapping[m["item_id"]]["item_id"]] = {
                    "accept": accept,
                    "decision": "accept" if accept else ("invalid" if ship is None else "reject"),
                    "win": win,
                    "top": win == top,
                    "ship": ship,
                    "n_valid": len(ships),
                    "pack_item": m["item_id"],
                }
        after = Decimal(self.ledger().summary()["accounted_upper_usd"])
        result.cost_usd = float(after - before)
        result.elapsed_s = round(time.monotonic() - started, 1)
        result.meta = {
            "pack_id": info["public"]["pack_id"],
            "pack": str(pack),
            "rule": f"top win rate (ties) AND mean ship_as_is >= {self.ship_floor} "
            "over >= 2 valid v6-scored passes",
            "ledger": self.ledger().summary(),
        }
        return result


# ---------------------------------------------------------------- Codex CLI gpt-6.1-sol


CODEX_TASK = (
    "Рабочая оценка CardForge v5 (этап 4). Оцени карты по рубрике в {pack}/README.txt. "
    "Используй выбранные batches в {batches} и связанные изображения, миниатюры, референсы "
    "в {pack}. Просмотри каждый полный размер, миниатюру и нужные refs. Все семь passed/note "
    "обязательны; notes короткие на русском. При провале нужна fix_instruction, при "
    "неопределённости стиля style_uncertain=true. Не угадывай происхождение. Не читай "
    "manifest.private.json, человеческие метки, старые ответы, ключи экспорта, env или "
    "конфиги. Обучающих меток в этом пакете нет. Оригиналы не меняй, не генерируй, не вызывай "
    "внешние API или других агентов. Новые quality-требования имеют приоритет над "
    "историческими словами генератора; не требуй паттерн там, где выбран gradient. Запиши "
    "Submission для каждого batch согласно {pack}/answer.schema.json. Родитель отдельно "
    "проверит и соберёт результаты."
)
CODEX_LAUNCH = (
    "\n\nУточнение запуска: это изолированная CLI-сессия судьи. Рабочий каталог {scratch}. "
    "Оцени только batches {batches}. Сохраняй каждый законченный batch в текущий каталог как "
    "batch-NN.json. Не запускай других агентов, внешние API, генерацию или поиск в интернете. "
    "В модели судьи укажи {model}. Финальное сообщение короткое: какие batches записаны и что "
    "не удалось.\n"
)


class CodexCliJudge:
    """Codex CLI `gpt-6.1-sol` on the ChatGPT subscription (no paid OpenAI API), rubric v5,
    decision `definite-failure-first-v1` (stage-2 runs 23 + 24, pack_ids in evaluation.toml)."""

    kind = "absolute"
    rubric = "rubric7-quality-priority-v5"
    contract = "judge-inputs-quality-priority-v5"
    readme = "runs/stage02/23-quality-priority/pack/README.txt"
    model = "gpt-6.1-sol"
    batch_size = 20

    def __init__(
        self,
        root: Path,
        *,
        id: str = "codex-sol",
        timeout_s: int = 3 * 3600,
        decision_policy: str = "definite-failure-first-v1",
    ):
        self.root = root
        self.id = id
        self.timeout_s = timeout_s
        self.decision_policy = decision_policy

    def estimate_usd(self, items: list[JudgeItem]) -> float:
        return 0.0  # subscription; time is reported instead

    def judge(self, round_dir: Path, items: list[JudgeItem]) -> BatchResult:
        from cardforge.evaluate.common import dump, sha256
        from cardforge.evaluate.judge_pack import Submission

        started = time.monotonic()
        folder = round_dir / self.id
        pack, info = _export(
            self.root,
            folder,
            items,
            seed=20261107,
            rubric=self.rubric,
            contract=self.contract,
            readme_from=self.root / self.readme,
        )
        public = info["public"]
        batches_dir = folder / "batches"
        if not batches_dir.exists():
            batches_dir.mkdir()
            for start in range(0, len(public["items"]), self.batch_size):
                dump(
                    batches_dir / f"batch-{start // self.batch_size + 1:02}.json",
                    {
                        "pack_id": public["pack_id"],
                        "items": public["items"][start : start + self.batch_size],
                    },
                )
        results = folder / "results"
        results.mkdir(exist_ok=True)
        wanted = {p.name: json.loads(p.read_text()) for p in sorted(batches_dir.glob("*.json"))}
        missing = [n for n in wanted if not (results / n).exists()]
        if missing:
            self._launch(folder, pack, batches_dir, missing, wanted, Submission, public, sha256)
            still = [n for n in wanted if not (results / n).exists()]
            if still:
                # An unavailable deciding judge must stop the run, not count as "invalid".
                raise RuntimeError(
                    f"{self.id}: batches not judged {still}; CLI: {self._last_cli_error(folder)}"
                )
        answers = {}
        for name in wanted:
            path = results / name
            if path.exists():
                for a in json.loads(path.read_text())["answers"]:
                    answers[a["item_id"]] = a
        from cardforge.evaluate.models import JudgeAnswer

        mapping = info["key"]["mapping"]
        result = BatchResult(elapsed_s=round(time.monotonic() - started, 1))
        for item in public["items"]:
            raw = answers.get(item["item_id"])
            original = mapping[item["item_id"]]["item_id"]
            if raw is None:
                result.verdicts[original] = {"accept": False, "decision": "invalid"}
                continue
            answer = JudgeAnswer.model_validate(raw)
            decision = answer.decision_for(self.decision_policy)
            result.verdicts[original] = {
                "accept": decision == "accept",
                "decision": decision,
                "fix_instruction": answer.fix_instruction,
                "issues": answer.issues,
                "pack_item": item["item_id"],
            }
        result.meta = {
            "pack_id": public["pack_id"],
            "model_requested": self.model,
            "decision_policy": self.decision_policy,
            "auth": "ChatGPT subscription",
        }
        return result

    @staticmethod
    def _last_cli_error(folder: Path) -> str:
        runs = sorted((folder / "cli-runs").glob("*/stream.jsonl"))
        for line in reversed(runs[-1].read_text().splitlines() if runs else []):
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get("type") in ("error", "turn.failed"):
                return str(event.get("message") or event.get("error", {}).get("message"))[:300]
        return "no error reported"

    def _launch(self, folder, pack, batches_dir, selected, wanted, Submission, public, sha256):
        from cardforge.evaluate.common import dump

        codex = tomllib.loads((self.root / "config/tools.toml").read_text())["host_cli"]["codex"]
        scratch = Path(tempfile.mkdtemp(prefix="cardforge-codex-", dir="/private/tmp"))
        prompt = CODEX_TASK.format(pack=pack.resolve(), batches=batches_dir.resolve()) + (
            CODEX_LAUNCH.format(scratch=scratch, batches=", ".join(selected), model=self.model)
        )
        argv = [
            codex,
            "exec",
            "--skip-git-repo-check",
            "--ephemeral",
            "--ignore-user-config",
            "--model",
            self.model,
            "-c",
            'model_reasoning_effort="xhigh"',
            "--sandbox",
            "workspace-write",
            "--json",
            "--color",
            "never",
            "-",
        ]
        env = {
            k: v
            for k, v in os.environ.items()
            if k not in ("OPENAI_API_KEY", "OPENAI_BASE_URL", "ANTHROPIC_API_KEY")
        }
        run = folder / "cli-runs" / time.strftime("%Y%m%d-%H%M%S")
        run.mkdir(parents=True)
        dump(
            run / "launch.json",
            {
                "argv": argv,
                "scratch": str(scratch),
                "batches": selected,
                "auth_basis": "ChatGPT subscription",
                "api_key_environment_removed": True,
            },
        )
        (run / "prompt.txt").write_text(prompt)
        with (run / "stream.jsonl").open("w") as out, (run / "stderr.log").open("w") as err:
            proc = subprocess.run(
                argv,
                input=prompt,
                cwd=scratch,
                env=env,
                stdout=out,
                stderr=err,
                text=True,
                timeout=self.timeout_s,
            )
        collected, errors = [], []
        for name in selected:
            path = scratch / name
            if not path.exists():
                errors.append(f"{name}: not written")
                continue
            try:
                data = json.loads(path.read_text())
                data["judge"] = {
                    "provider": "host-agent",
                    "host": "codex",
                    "model": self.model,
                    "family": "OpenAI",
                    "role": "working",
                }
                sub = Submission.model_validate(data)
                expected = {x["item_id"]: x for x in wanted[name]["items"]}
                if {a.item_id for a in sub.answers} != set(expected) or any(
                    a.image_sha256 != expected[a.item_id]["image_sha256"] for a in sub.answers
                ):
                    raise ValueError("missing, extra or wrong-SHA answers")
                target = folder / "results" / name
                if target.exists():
                    raise ValueError("refusing to overwrite a frozen judge result")
                dump(target, sub.model_dump())
                collected.append(name)
            except (ValueError, OSError) as exc:
                errors.append(f"{name}: {str(exc)[:200]}")
        dump(
            run / "status.json",
            {"exit_code": proc.returncode, "collected": collected, "errors": errors},
        )


# ---------------------------------------------------------------- GPT via OpenRouter


class OpenRouterAbsoluteJudge:
    """GPT (e.g. openai/gpt-6.1-sol) through OpenRouter on the calibrated Codex Sol input
    contract: rubric v5 pack, role references, `definite-failure-first-v1`.

    User decision 2026-11-04 («можешь использовать GPT через опенроутер в качестве судьи»).
    A provider change is a new judge: until it is calibrated, its verdicts are compared
    with human labels, not trusted on their own. Accounting uses the stage-2 ledger and
    `accounted_call` unchanged; only the `openai` exclusion is lifted for this judge."""

    kind = "absolute"
    rubric = CodexCliJudge.rubric
    contract = CodexCliJudge.contract
    readme = CodexCliJudge.readme

    def __init__(
        self,
        root: Path,
        *,
        id: str = "gpt-sol-or",
        model: str = "openai/gpt-6.1-sol",
        input_usd_per_token: float = 2e-6,
        output_usd_per_token: float = 1e-5,
        reasoning_effort: str = "high",
        workers: int = 4,
        decision_policy: str = "definite-failure-first-v1",
        estimate_per_item_usd: float = 0.05,
        zdr: bool = True,
    ):
        self.root = root
        self.id = id
        # Project rule: judges require zero data retention. OpenAI has no ZDR endpoint on
        # OpenRouter; the user allowed this judge without it (2026-11-04, «Разрешить»).
        self.zdr = zdr
        self.candidate = {
            "name": id,
            "model": model,
            "provider": model.split("/")[0],
            "input_usd_per_token": input_usd_per_token,
            "output_usd_per_token": output_usd_per_token,
        }
        self.reasoning_effort = reasoning_effort
        self.workers = workers
        self.decision_policy = decision_policy
        self.estimate_per_item_usd = estimate_per_item_usd

    def estimate_usd(self, items: list[JudgeItem]) -> float:
        return self.estimate_per_item_usd * len(items)

    def provider_settings(self, body: dict) -> dict:
        """Let this judge's own provider through; drop ZDR only when told to."""
        provider = body["provider"]
        provider["ignore"] = [p for p in provider["ignore"] if p != self.candidate["provider"]]
        if not self.zdr:
            provider.pop("zdr", None)
        return body

    def judge(self, round_dir: Path, items: list[JudgeItem]) -> BatchResult:
        import os
        import threading
        from concurrent.futures import ThreadPoolExecutor

        import httpx
        from dotenv import dotenv_values

        from cardforge.evaluate.common import dump
        from cardforge.evaluate.local_batch import fingerprint
        from cardforge.evaluate.models import JudgeAnswer
        from cardforge.evaluate.openrouter_judge import (
            _parse_answer,
            accounted_call,
            build_content,
            request_body,
        )
        from cardforge.render.openrouter_image import openrouter_ledger

        started = time.monotonic()
        cfg, ledger = openrouter_ledger(self.root)
        cfg = {**cfg, "reasoning_effort": self.reasoning_effort}
        folder = round_dir / self.id
        pack, info = _export(
            self.root,
            folder,
            items,
            seed=20261108,
            rubric=self.rubric,
            contract=self.contract,
            readme_from=self.root / self.readme,
        )
        public, mapping = info["public"], info["key"]["mapping"]
        responses = folder / "responses"
        responses.mkdir(parents=True, exist_ok=True)
        key = os.getenv("OPENROUTER_API_KEY") or dotenv_values(
            self.root / ".env", interpolate=False
        ).get("OPENROUTER_API_KEY")
        if not key:
            raise ValueError("missing OPENROUTER_API_KEY")
        before = Decimal(ledger.summary()["accounted_upper_usd"])
        stop, errors = threading.Event(), []

        def one(client, item: dict) -> None:
            saved = responses / f"{item['item_id']}.json"
            if saved.exists() or stop.is_set():
                return
            content, identity = build_content(self.root, pack, public, item)
            body = request_body(cfg, self.candidate, content, JudgeAnswer)
            body = self.provider_settings(body)
            request_id = fingerprint(
                {
                    "endpoint": "openrouter/chat/completions",
                    "settings": {k: v for k, v in body.items() if k != "messages"},
                    **identity,
                }
            )
            raw, message, cost = accounted_call(
                client,
                cfg,
                self.candidate,
                ledger,
                body,
                request_id,
                responses,
                item["item_id"],
                {"pack_id": public["pack_id"], "item_id": item["item_id"]},
                stop,
            )
            try:
                answer = _parse_answer(message.get("content") or "", JudgeAnswer)
                if answer.item_id != item["item_id"] or answer.image_sha256 != item["image_sha256"]:
                    raise ValueError("answer ID/SHA mismatch")
            except ValueError as exc:  # invalid answers are kept, never re-asked
                dump(responses / f"{item['item_id']}.invalid.json", {**raw, "invalid": str(exc)})
                return
            dump(saved, {"request_id": request_id, "answer": answer.model_dump()})

        with httpx.Client(
            base_url=cfg["base_url"],
            timeout=httpx.Timeout(600.0, connect=30.0),
            headers={"Authorization": f"Bearer {key}", "X-Title": "CardForge stage 4 judge"},
        ) as client:
            with ThreadPoolExecutor(max_workers=self.workers) as pool:
                for future in [pool.submit(one, client, i) for i in public["items"]]:
                    try:
                        future.result()
                    except Exception as exc:  # noqa: BLE001 - reported, run continues
                        errors.append(str(exc)[:300])
        result = BatchResult(elapsed_s=round(time.monotonic() - started, 1))
        for item in public["items"]:
            original = mapping[item["item_id"]]["item_id"]
            path = responses / f"{item['item_id']}.json"
            if not path.exists():
                result.verdicts[original] = {"accept": False, "decision": "invalid"}
                continue
            answer = JudgeAnswer.model_validate(json.loads(path.read_text())["answer"])
            decision = answer.decision_for(self.decision_policy)
            result.verdicts[original] = {
                "accept": decision == "accept",
                "decision": decision,
                "fix_instruction": answer.fix_instruction,
                "issues": answer.issues,
                "pack_item": item["item_id"],
            }
        result.cost_usd = float(Decimal(ledger.summary()["accounted_upper_usd"]) - before)
        result.meta = {
            "pack_id": public["pack_id"],
            "model": self.candidate["model"],
            "rubric": self.rubric,
            "decision_policy": self.decision_policy,
            "errors": errors[:5],
            "calibrated": False,
            "zdr": self.zdr,
        }
        return result


# ---------------------------------------------------------------- decision rule


def pick_and_confirm(
    picker: dict[str, dict],
    confirmer: dict[str, dict],
    members: list[JudgeItem],
    eligible: set[str],
) -> dict:
    """Default rule for two deciding judges (configurable; named in the report):
    the group judge's pick that the absolute judge also accepts → accept;
    only one of them accepts something → flag for G2 with both verdicts;
    neither accepts anything → fix (regenerate the position)."""
    ids = [m.item_id for m in members if m.item_id in eligible]
    picks = [i for i in ids if picker.get(i, {}).get("accept")]
    confirmed = [i for i in ids if confirmer.get(i, {}).get("accept")]
    both = [i for i in picks if i in confirmed]
    if both:
        return {"decision": "accept", "item_id": both[0], "reason": "pick confirmed"}
    if picks or confirmed:
        return {
            "decision": "flag",
            "item_id": (picks or confirmed)[0],
            "reason": "judges disagree",
            "picker_accepts": picks,
            "confirmer_accepts": confirmed,
        }
    fixes = [confirmer.get(i, {}).get("fix_instruction") for i in ids]
    return {
        "decision": "fix",
        "item_id": None,
        "reason": "no candidate accepted",
        "fix_instruction": next((f for f in fixes if f), None),
    }
