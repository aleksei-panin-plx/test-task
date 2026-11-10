"""G2 decisions as labels for judge calibration (stage 4, phase 1, 2026-11-04).

Every human G2 decision is also written to `review/labels/<reviewer-hash>.json` in the
same shape as a blind reviewer record of package kino-01
(`runs/stage04/blind-review-01/reviewers/*.json`), so
`scripts/stage04_blind_targets.py` can read it as one more rater:

    {
      "format": "cardforge-g2-labels-v1",
      "reviewer": "<name>",
      "run_id": "<run>",
      "ratings": {
        "<run_id>/<card_id>.a<NN>": {
          "decision": "accept" | "fix" | "reject",      # lenient scale: accept or fix
          "reasons": ["crop", "props", ...],            # codes of prompts/render/fixes.toml
          "note": "<comment, original wording>",
          "image": "cards/<v>/<card>/attempt_NN[/cand_K]/final.png",
          "image_sha256": "...",
          "rated_at": "<ISO time>",
          "blind": true,             # decided on the blind review page
          "g2_round": 0,             # how many fix rounds the run's G2 had before
          "event_id": "<human_decision event>"
        }
      }
    }

The item id matches the blind package (`kino-western-03/v01_c03.a02`), so the same image
labelled in both places lines up. Model, render profile and run are not in the record:
they stay in the run (`manifest.json`), private to the analysis.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

from cardforge.evaluate.common import sha256
from cardforge.orchestrator.store import RunStore

FORMAT = "cardforge-g2-labels-v1"
DIR = "review/labels"


def item_id(run_id: str, card_id: str, image_rel: str | None, attempts: int) -> str:
    attempt = attempts
    if image_rel and "/attempt_" in image_rel:
        attempt = int(image_rel.split("/attempt_")[1][:2])
    return f"{run_id}/{card_id}.a{attempt:02d}"


def path_for(reviewer: str) -> str:
    return f"{DIR}/{hashlib.sha256(reviewer.encode()).hexdigest()[:12]}.json"


def record(
    store: RunStore,
    *,
    reviewer: str,
    card_id: str,
    image_rel: str | None,
    attempts: int,
    decision: str,
    reasons: list[str],
    note: str | None,
    blind: bool,
    g2_round: int,
    event_id: str,
) -> str:
    rel = path_for(reviewer)
    data = (
        store.read_json(rel)
        if store.exists(rel)
        else {"format": FORMAT, "reviewer": reviewer, "run_id": store.run_id, "ratings": {}}
    )
    key = item_id(store.run_id, card_id, image_rel, attempts)
    image = store.path(image_rel) if image_rel else None
    data["ratings"][key] = {
        "decision": decision,
        "reasons": reasons,
        "note": note or "",
        "image": image_rel,
        "image_sha256": sha256(image) if image is not None and image.exists() else None,
        "rated_at": datetime.now(UTC).isoformat(),
        "blind": blind,
        "g2_round": g2_round,
        "event_id": event_id,
    }
    store.write(rel, data)
    return key
