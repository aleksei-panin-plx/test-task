"""PromptGuard: positive constraints in, forbidden terms out (TA §9.3, §9.4)."""

from __future__ import annotations

from cardforge.contracts.policy import PolicyPack, PolicyRule, PolicyVerdict
from cardforge.policy.matching import find_terms


class PolicyPromptError(ValueError):
    def __init__(self, hits: list[tuple[str, str, str]]):
        self.hits = hits
        super().__init__(
            "forbidden terms in prompt: "
            + "; ".join(f"{rule} ({sev}): {term!r}" for rule, sev, term in hits)
        )


def _prompt_rules(packs: list[PolicyPack]) -> list[PolicyRule]:
    return [r for p in packs for r in p.rules if "prompt" in r.stages]


def applicable_rules(
    packs: list[PolicyPack], verdicts: list[PolicyVerdict], targets: set[str]
) -> list[PolicyRule]:
    """Rules whose constraint belongs in this card's prompt.

    Pure text rules and IP text rules always apply (any image may grow lettering); other
    rules apply when they fired on the request or on this card at plan stage. Keeps
    prompts short (TA §9.4).
    """
    fired = {v.rule_id for v in verdicts if v.verdict != "pass" and v.target in targets}
    return [
        r
        for p in packs
        for r in p.rules
        if "prompt" in r.stages
        and (
            r.id in fired
            or set(r.applies_to) == {"text"}
            or (p.policy_kind == "ip" and "text" in r.applies_to)
        )
    ]


def constraint_lines(rules: list[PolicyRule]) -> list[str]:
    return list(dict.fromkeys(r.prompt_constraint for r in rules if r.prompt_constraint))


def guard(
    prompt_text: str, packs: list[PolicyPack], waived: set[str] | frozenset[str] = frozenset()
) -> None:
    """Raise with the rule id when a forbidden term reaches the final prompt.

    `waived` holds review-level rules a human accepted at G1; block rules are never waivable.
    """
    hits: list[tuple[str, str, str]] = []
    for rule in _prompt_rules(packs):
        if rule.id in waived and rule.severity != "block":
            continue
        hits += [(rule.id, rule.severity, t) for t in find_terms(prompt_text, rule.match.keywords)]
    if hits:
        raise PolicyPromptError(hits)
