"""Policy pack loader, resolver and linter (TA §9.1, §9.2)."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import yaml
from pydantic import ValidationError

from cardforge.contracts.policy import PolicyPack
from cardforge.policy.matching import find_terms

BASELINE = "ip.baseline"
CHECKLIST_HEADING = "## Чек-лист авторских прав"
_BLOCK = re.compile(r"^```yaml policy\n(.*?)^```", re.S | re.M)
_FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n", re.S)
_PACK_FIELDS = (
    "policy_id",
    "policy_kind",
    "region",
    "policy_version",
    "includes",
    "verified",
    "review_after",
)


class PolicyError(ValueError):
    pass


def parse_checklist(text: str) -> list[str]:
    if CHECKLIST_HEADING not in text:
        return []
    section = text.split(CHECKLIST_HEADING, 1)[1].split("\n## ", 1)[0]
    return [m.group(1).strip() for m in re.finditer(r"^\d+\.\s+(.+)$", section, re.M)]


def parse_pack(path: Path) -> PolicyPack:
    raw = path.read_bytes()
    text = raw.decode("utf-8")
    head = _FRONTMATTER.match(text)
    if not head:
        raise PolicyError(f"{path}: missing frontmatter")
    meta = yaml.safe_load(head.group(1)) or {}
    blocks = _BLOCK.findall(text)
    if len(blocks) != 1:
        raise PolicyError(f"{path}: expected exactly one ```yaml policy block, got {len(blocks)}")
    body = yaml.safe_load(blocks[0]) or {}
    if set(body) != {"rules"}:
        raise PolicyError(f"{path}: policy block must contain only `rules:`")
    try:
        return PolicyPack(
            **{k: meta[k] for k in _PACK_FIELDS if k in meta},
            rules=body["rules"],
            file_sha256=hashlib.sha256(raw).hexdigest(),
            path=str(path),
            checklist=parse_checklist(text),
        )
    except ValidationError as exc:
        raise PolicyError(f"{path}: {exc}") from exc


def pack_files(root: Path) -> list[Path]:
    return sorted(p for p in (root / "policies").rglob("*.md") if p.name != "README.md")


def load_packs(root: Path) -> dict[str, PolicyPack]:
    packs: dict[str, PolicyPack] = {}
    for path in pack_files(root):
        pack = parse_pack(path)
        if pack.policy_id in packs:
            raise PolicyError(f"duplicate policy_id {pack.policy_id}")
        packs[pack.policy_id] = pack
    return packs


def _pack_ref(ref: str, packs: dict[str, PolicyPack], root: Path) -> PolicyPack:
    """`topics/gambling` (path form from TA §5.1) or `topic.gambling` (policy_id)."""
    if ref in packs:
        return packs[ref]
    for pack in packs.values():
        rel = Path(pack.path).resolve().relative_to((root / "policies").resolve())
        if str(rel.with_suffix("")) == ref:
            return pack
    raise PolicyError(f"unknown policy pack {ref!r}")


def resolve_active(root: Path, markets: list[str], policy_packs: list[str]) -> list[PolicyPack]:
    """IP baseline is always active; it cannot be removed by a request."""
    packs = load_packs(root)
    if BASELINE not in packs:
        raise PolicyError("policies/ip/baseline.md is mandatory")
    wanted = [BASELINE]
    regions = {m.upper() for m in markets}
    wanted += [p.policy_id for p in packs.values() if p.region and p.region.upper() in regions]
    wanted += [_pack_ref(ref, packs, root).policy_id for ref in policy_packs]
    active: list[PolicyPack] = []
    stack = list(reversed(wanted))
    while stack:
        pack = _pack_ref(stack.pop(), packs, root)
        if pack in active:
            continue
        active.append(pack)
        stack.extend(reversed(pack.includes))
    return active


def lint_pack(pack: PolicyPack, others: list[PolicyPack]) -> list[str]:
    issues: list[str] = []
    seen: set[str] = set()
    prompt_rules = [r for p in others for r in p.rules if "prompt" in r.stages]
    for rule in pack.rules:
        where = f"{pack.policy_id}/{rule.id}"
        if rule.id in seen:
            issues.append(f"{where}: duplicate id")
        seen.add(rule.id)
        if "qa" in rule.stages and not rule.qa_question:
            issues.append(f"{where}: stage qa needs qa_question")
        if "prompt" in rule.stages and not rule.prompt_constraint:
            issues.append(f"{where}: stage prompt needs prompt_constraint")
        if rule.severity == "block" and not rule.alternatives:
            issues.append(f"{where}: block rule needs alternatives")
        if "plan" in rule.stages and not rule.match.keywords and not rule.match.semantic:
            issues.append(f"{where}: plan rule needs keywords or semantic match")
        if rule.prompt_constraint:
            # A constraint must not name a forbidden thing, or PromptGuard rejects its own text.
            for other in prompt_rules:
                for term in find_terms(rule.prompt_constraint, other.match.keywords):
                    issues.append(f"{where}: prompt_constraint names {term!r} of {other.id}")
    if pack.policy_kind == "ip" and not pack.checklist:
        issues.append(f"{pack.policy_id}: missing section «Чек-лист авторских прав»")
    if pack.review_after is None:
        issues.append(f"{pack.policy_id}: missing review_after")
    return issues


def lint(root: Path, paths: list[Path] | None = None) -> dict:
    report: dict = {"packs": [], "issues": []}
    loaded: list[PolicyPack] = []
    for path in paths or pack_files(root):
        try:
            loaded.append(parse_pack(path))
        except (PolicyError, yaml.YAMLError, OSError) as exc:
            report["issues"].append(str(exc))
    ids = [p.policy_id for p in loaded]
    rule_ids = [r.id for p in loaded for r in p.rules]
    for dup in sorted({i for i in rule_ids if rule_ids.count(i) > 1}):
        report["issues"].append(f"rule id {dup} is used by more than one pack")
    for pack in loaded:
        report["issues"].extend(lint_pack(pack, loaded))
        for inc in pack.includes:
            if inc not in ids and not any(
                str(Path(p.path).with_suffix("")).endswith(inc) for p in loaded
            ):
                report["issues"].append(f"{pack.policy_id}: unknown include {inc!r}")
        report["packs"].append(
            {
                "policy_id": pack.policy_id,
                "version": pack.policy_version,
                "rules": len(pack.rules),
                "sha256": pack.file_sha256,
                "path": pack.path,
            }
        )
    report["ok"] = not report["issues"]
    return report
