"""Fix library and fix history (stage 4, phase 1 of the judge autonomy strategy).

A fix reaches the next attempt as reason codes and/or free text:

- codes (`prompts/render/fixes.toml`) become short positive English prompt lines, so a fix
  can come from codes alone (hard checks, a veto-only judge, G2 reason checkboxes);
- free text (a judge's `fix_instruction`, a policy rule's `prompt_constraint`, a human G2
  comment) is used as written, except human comments, which are first rewritten into one
  English constraint by the run's LLM provider (`fix_normalize` task); the original stays.

`manifest.counters["fix"][card_id]` keeps the whole de-duplicated history of a card
(`entries`), not only the last fix. Every still-relevant entry goes into the next prompt.
The old single-dict format `{instruction, same_seed, from_human}` of earlier runs is read
as a one-entry history whose text is used verbatim, so old runs give the same prompt.
"""

from __future__ import annotations

import hashlib
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

LIBRARY = "prompts/render/fixes.toml"
FORMAT = "fix-history-v1"
OTHER = "other"
# At most this many lines go into one prompt; the newest win (a card has at most
# `max_fix_rounds` + human rounds of fixes, so this is rarely reached).
MAX_LINES = 6
MAX_DESCRIPTION = 300
CYRILLIC = re.compile(r"[Ѐ-ӿ]")


@dataclass(frozen=True)
class FixCode:
    code: str
    label: str
    prompt: str | None
    question: str | None


@dataclass(frozen=True)
class FixLibrary:
    version: str
    codes: dict[str, FixCode]

    @property
    def labels(self) -> dict[str, str]:
        return {c: f.label for c, f in self.codes.items()}

    def check(self, codes: list[str] | tuple[str, ...]) -> list[str]:
        """Known codes, order kept, duplicates dropped; an unknown code is an error."""
        unknown = [c for c in codes if c not in self.codes]
        if unknown:
            raise ValueError(f"unknown reason code(s) {unknown}; known: {sorted(self.codes)}")
        return list(dict.fromkeys(codes))

    def prompt_codes(self, codes) -> list[str]:
        return [c for c in codes if c in self.codes and self.codes[c].prompt]

    def question(self, code: str) -> str | None:
        fix = self.codes.get(code)
        return fix.question if fix else None


def load_library(root: Path, rel: str = LIBRARY) -> FixLibrary:
    raw = (root / rel).read_bytes()
    data = tomllib.loads(raw.decode("utf-8"))
    codes = {
        code: FixCode(code, v["label"], v.get("prompt"), v.get("question"))
        for code, v in data["codes"].items()
    }
    if OTHER not in codes or codes[OTHER].prompt:
        raise ValueError("the fix library needs `other` without a prompt line")
    return FixLibrary(f"{data['version']}@{hashlib.sha256(raw).hexdigest()[:12]}", codes)


def entries(record: dict | None) -> list[dict]:
    """History of one card; the legacy single-dict record becomes one entry."""
    if not record:
        return []
    if record.get("format") == FORMAT:
        return [dict(e) for e in record["entries"]]
    if not record.get("instruction"):
        return []  # legacy "regenerate" without text: nothing to carry over
    return [
        {
            "id": "legacy-1",
            "source": "human" if record.get("from_human") else "auto",
            "attempt": None,
            "codes": [],
            "text": record["instruction"],
            # Legacy text went into the prompt verbatim; keep it that way on resume.
            "prompt_text": record["instruction"],
            "normalized_by": "legacy (not normalized)",
            "legacy": True,
        }
    ]


def _norm(text: str | None) -> str:
    return " ".join((text or "").casefold().split()).rstrip(".!")


def add(history: list[dict], entry: dict) -> dict:
    """Append `entry` unless the same codes + text are already there (then only its
    `attempts` grows). Returns the stored entry."""
    key = (tuple(sorted(entry.get("codes") or [])), _norm(entry.get("text")))
    for old in history:
        if (tuple(sorted(old.get("codes") or [])), _norm(old.get("text"))) == key:
            seen = old.setdefault("attempts", [old.get("attempt")])
            if entry.get("attempt") not in seen:
                seen.append(entry.get("attempt"))
            return old
    stored = {
        "id": f"{entry.get('source', 'auto').split(':')[0]}-{len(history) + 1}",
        "codes": [],
        "text": None,
        "prompt_text": None,
        **entry,
    }
    stored["attempts"] = [stored.get("attempt")]
    history.append(stored)
    return stored


def needs_normalizing(entry: dict) -> bool:
    return (
        entry.get("source") == "human"
        and bool((entry.get("text") or "").strip())
        and entry.get("prompt_text") is None
        and not entry.get("normalized_by")
    )


def _sentence(line: str) -> str:
    line = line.strip()
    return line if line[-1:] in ".!?" else line + "."


def lines(history: list[dict], library: FixLibrary) -> list[str]:
    """Prompt lines of every still-relevant entry: each code once, each text once.

    All entries of a card stay relevant (a fixed defect can come back; only a human fix's
    rewritten `object_description` changes the spec, see `current_description`). An entry
    uses its normalized `prompt_text` when it has one, else its own text (judge and policy
    texts, or a comment no LLM could rewrite); the render step
    normalizes human comments before it builds the prompt."""
    out: list[str] = []
    seen: set[str] = set()
    for e in history:
        for code in library.prompt_codes(e.get("codes") or []):
            if code not in seen:
                seen.add(code)
                out.append(library.codes[code].prompt or "")
        text = e.get("prompt_text") or e.get("text")
        if text and _norm(text) not in seen:
            seen.add(_norm(text))
            out.append(text.strip())
    return out[-MAX_LINES:]


def compose(history: list[dict], library: FixLibrary) -> str | None:
    """The text after "Correction for this attempt:"; one legacy line stays verbatim."""
    found = lines(history, library)
    if not found:
        return None
    if len(found) == 1:
        return found[0]
    return " ".join(_sentence(x) for x in found)


def verify_codes(history: list[dict], attempt_no: int, library: FixLibrary) -> dict[str, int]:
    """Codes to check on `attempt_no`: code → attempt the fix was first asked for.

    Explicit codes and codes the normalizer inferred from a human comment both count;
    only fixes requested for an earlier attempt are checked."""
    out: dict[str, int] = {}
    for e in history:
        asked = e.get("attempt")
        if asked is None or asked >= attempt_no:
            continue
        for code in [*(e.get("codes") or []), *(e.get("inferred_codes") or [])]:
            if library.question(code) and code not in out:
                out[code] = asked
    return out


def normalize_task(entry: dict, spec, attempt_no: int, library: FixLibrary) -> dict:
    """`ask()` task: one human comment → one short positive English constraint."""
    return {
        "step": "fix_normalize",
        "schema_version": "fix_normalize.v2",
        "instruction": (
            "A reviewer commented on a generated game-card image. Rewrite the comment as ONE "
            "short positive English instruction for an image model (imperative, at most 40 "
            "words, concrete and visual, describe what to show rather than what to avoid). "
            "Keep the reviewer's intent exactly; add no new requirements, names or brands. "
            "In `codes`, list the reason codes the comment matches (may be empty). "
            "If the comment changes what the main object shows (adds, removes or replaces a "
            "part or a supporting element), also return `object_description`: the given "
            "description rewritten to agree with the comment (English, same style, at most "
            f"{MAX_DESCRIPTION} characters), so the description and the correction never "
            "conflict; otherwise return an empty string."
        ),
        "card_id": spec.card_id,
        "attempt_no": attempt_no,
        "object": spec.object_name,
        "object_description": spec.object_description or spec.object_name,
        "category": spec.category,
        "comment": entry["text"],
        "reason_codes": {c: f.label for c, f in library.codes.items() if c != OTHER},
        "answer_schema": {
            "type": "object",
            "required": ["constraint", "codes"],
            "properties": {
                "constraint": {"type": "string", "maxLength": 400},
                "codes": {"type": "array", "items": {"type": "string"}},
                "object_description": {"type": "string", "maxLength": MAX_DESCRIPTION},
            },
        },
    }


def check_normalized(data: dict, library: FixLibrary) -> tuple[str, list[str]]:
    """Validated (constraint, codes) from a `fix_normalize` answer; ValueError otherwise."""
    text = str(data.get("constraint") or "").strip()
    if not text:
        raise ValueError("empty constraint")
    if len(text) > 400:
        raise ValueError("constraint longer than 400 characters")
    if CYRILLIC.search(text):
        raise ValueError("constraint is not in English")
    codes = [c for c in library.check(list(data.get("codes") or [])) if c != OTHER]
    return text, codes


def check_description(data: dict) -> str | None:
    """Optional rewritten `object_description` of a `fix_normalize` answer (phase 3,
    2026-11-04): None when absent or empty; ValueError when not usable."""
    text = " ".join(str(data.get("object_description") or "").split())
    if not text:
        return None
    if len(text) > MAX_DESCRIPTION:
        raise ValueError(f"object_description longer than {MAX_DESCRIPTION} characters")
    if CYRILLIC.search(text):
        raise ValueError("object_description is not in English")
    return text


def current_description(history: list[dict]) -> str | None:
    """The newest `object_description` a human fix rewrote, if any: it replaces the plan's
    description for the next attempts, so the fix text and the description agree."""
    for e in reversed(history):
        if e.get("object_description"):
            return e["object_description"]
    return None
