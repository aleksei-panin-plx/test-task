"""Bounded local target-style candidate plan, kept separate from negative controls."""

from __future__ import annotations

import json
import tomllib
from collections import Counter
from pathlib import Path

from cardforge.evaluate.common import private_path, sha256

VARIANTS = {"game_3d", "soft_materials", "product_boundary", "reference_edit"}
OUTPUT = "runs/stage02/10-target-style-pool"


def validate(root: Path, source: Path, campaign: str = OUTPUT) -> dict:
    plan = json.loads(source.read_text())
    if (
        plan.get("status") != "authorized_local_target_candidates"
        or not plan.get("authorization")
        or plan.get("api_cost_usd") != 0
        or plan.get("weights_downloads") is not False
    ):
        raise ValueError("authorized local zero-cost target plan required")
    rows = plan["items"]
    if len(rows) != 48 or plan["count"] != 48:
        raise ValueError("this campaign covers exactly 48 candidates")
    if len({r["item_id"] for r in rows}) != 48 or len({r["output"] for r in rows}) != 48:
        raise ValueError("duplicate candidate ID or output")
    if Counter(r["variant"] for r in rows) != dict.fromkeys(VARIANTS, 12):
        raise ValueError("12 candidates per target-style variant required")
    refs = {r["matched_reference_id"] for r in rows}
    if len(refs) != 12:
        raise ValueError("12 source objects required")
    for ref in refs:
        if {r["variant"] for r in rows if r["matched_reference_id"] == ref} != VARIANTS:
            raise ValueError("each object requires all four variants")
    for row in rows:
        if (
            row["profile"] != "local-klein4b"
            or row["category"] not in (1, 2, 3, 4)
            or row["render_size"] != [864, 960]
            or row["evaluation_size"] != [430, 480]
            or not row["prompt"].strip()
            or type(row["seed"]) is not int
            or not 0 <= row["seed"] < 2**32
        ):
            raise ValueError("unexpected model, dimensions, prompt, seed or category")
        output = private_path(root, row["output"])
        if not output.is_relative_to((root / campaign).resolve()) or output.name != "image.png":
            raise ValueError("candidate output escapes campaign")
        expected = root / f"dataset/{row['matched_reference_id']}.png"
        if row["variant"] == "reference_edit":
            if len(row["references"]) != 1:
                raise ValueError("exactly one local matched reference required")
            reference = row["references"][0]
            path = private_path(root, reference["path"])
            if path != expected.resolve() or sha256(path) != reference["sha256"]:
                raise ValueError("changed or mismatched conditioning reference")
        elif row["references"]:
            raise ValueError("text-only variants cannot receive images")
    model = tomllib.loads((root / "config/models.toml").read_text())["models"]["local-klein4b"]
    if model["license"] != "Apache-2.0" or not model["license_accepted"]:
        raise ValueError("approved Apache-2.0 model required")
    metadata = json.loads(
        (root / f"models/{model['id']}-q{model['quantize']}/mflux_model.json").read_text()
    )
    if (
        metadata["base_model"] != model["id"]
        or metadata["quantize"] != model["quantize"]
        or metadata["upstream"]["revision"] != model["download_revision"]
    ):
        raise ValueError("pinned local weights required")
    return plan
