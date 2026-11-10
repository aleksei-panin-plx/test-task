"""Copyright confirmation status (TA §9.8). The only writer of `ip_check: confirmed`."""

from __future__ import annotations

from cardforge.contracts.run import IpCheck
from cardforge.orchestrator.store import RunStore


def init_pending(store: RunStore, variant_ids: list[str]) -> None:
    """Every variant starts pending; gate mode `none` and autonomy never change this."""
    manifest = store.manifest()
    for v in variant_ids:
        manifest.ip_check.setdefault(v, IpCheck())
    store.save_manifest(manifest)


def reset_pending(store: RunStore, variant_id: str, reason: str) -> bool:
    """The variant's card set changed after G3: a confirmation no longer covers it. Back to
    `pending` (journaled with the old event id); a new human answer is needed."""
    manifest = store.manifest()
    old = manifest.ip_check.get(variant_id)
    if old is None or old.status != "confirmed":
        return False
    store.emit(
        "ip_check_reset",
        {"from_event_id": old.event_id, "reason": reason},
        variant_id=variant_id,
    )
    manifest.ip_check[variant_id] = IpCheck()
    store.save_manifest(manifest)
    return True


def record_human_decision(
    store: RunStore, variant_id: str, *, confirmed: bool, human_answer: str, reviewer: str
) -> IpCheck:
    """Record the G3 answer verbatim, then derive `ip_check` from that event."""
    if not human_answer.strip():
        raise ValueError("ip_check needs the human's own answer, quoted verbatim")
    manifest = store.manifest()
    if variant_id not in manifest.ip_check:
        raise ValueError(f"unknown variant {variant_id}")
    event = store.emit(
        "human_decision",
        {
            "gate": "G3",
            "subject": "ip_check",
            "decision": "confirmed" if confirmed else "rejected",
            "decided_by": "human",
            "reviewer": reviewer,
            "human_answer": human_answer,
            "checklist": "policies/ip/baseline.md#Чек-лист авторских прав",
        },
        variant_id=variant_id,
    )
    check = IpCheck(status="confirmed" if confirmed else "pending", event_id=event.event_id)
    manifest.ip_check[variant_id] = check
    store.save_manifest(manifest)
    return check
