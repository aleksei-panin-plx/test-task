"""Validation and image preparation for explicitly authorized local calibration batches.

No ML imports or provider requests occur on import or validation.
"""

from __future__ import annotations

import hashlib
import json
import math
import tomllib
from collections import Counter
from pathlib import Path

from PIL import Image

from cardforge.evaluate.common import private_path, sha256

ALLOWED = {"local-klein4b", "local-zimage-turbo"}
STYLES = {"photorealism", "flat_2d", "other_3d"}


def fingerprint(data: object) -> str:
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def validate_plan(root: Path, path: Path) -> dict:
    plan = json.loads(path.read_text())
    if plan.get("status") != "authorized_local_batch" or not plan.get("authorization"):
        raise ValueError("explicitly authorized local batch required")
    if plan.get("api_cost_usd") != 0 or plan.get("weights_downloads") is not False:
        raise ValueError("local calibration requires $0 and no new weights")
    rows = plan["items"]
    if len(rows) != 36 or plan["count"] != 36:
        raise ValueError("authorization covers the concrete 36-image plan")
    if len({r["item_id"] for r in rows}) != 36 or len({r["output"] for r in rows}) != 36:
        raise ValueError("duplicate item IDs or output paths")
    if Counter(r["profile"] for r in rows) != dict.fromkeys(ALLOWED, 18):
        raise ValueError("only 18 images per authorized model are allowed")
    references = {r["matched_reference_id"] for r in rows}
    if len(references) != 12 or Counter(r["foreign_style"] for r in rows) != dict.fromkeys(
        STYLES, 12
    ):
        raise ValueError("need 12 matched objects in all three foreign styles")
    models = tomllib.loads((root / "config/models.toml").read_text())["models"]
    for row in rows:
        if (
            row["references"]
            or row["render_size"] != [864, 960]
            or row["evaluation_size"] != [430, 480]
        ):
            raise ValueError("unexpected references or dimensions")
        if (
            not row["prompt"].strip()
            or type(row["seed"]) is not int
            or not 0 <= row["seed"] < 2**32
            or row["category"] not in (1, 2, 3, 4)
        ):
            raise ValueError("invalid prompt, seed or category")
        image = private_path(root, row["output"])
        if (
            not image.is_relative_to((root / "runs/stage02/01-local36").resolve())
            or image.suffix != ".png"
        ):
            raise ValueError("generated art must stay in runs/stage02/01-local36/")
    for ref in references:
        if {r["foreign_style"] for r in rows if r["matched_reference_id"] == ref} != STYLES:
            raise ValueError("each object needs each foreign style exactly once")
    for profile in ALLOWED:
        model = models[profile]
        if model["license"] != "Apache-2.0" or not model["license_accepted"]:
            raise ValueError("only the authorized Apache 2.0 models may run")
        folder = root / f"models/{model['id']}-q{model['quantize']}"
        saved = json.loads((folder / "mflux_model.json").read_text())
        if (
            saved["base_model"] != model["id"]
            or saved["quantize"] != model["quantize"]
            or saved["upstream"]["revision"] != model["download_revision"]
        ):
            raise ValueError("local weights do not match pinned model configuration")
    return plan


def card_image(image: Image.Image) -> Image.Image:
    scale = max(430 / image.width, 480 / image.height)
    width, height = math.ceil(image.width * scale), math.ceil(image.height * scale)
    resized = image.convert("RGB").resize((width, height), Image.Resampling.LANCZOS)
    left, top = (width - 430) // 2, (height - 480) // 2
    return resized.crop((left, top, left + 430, top + 480))


def completed(root: Path, row: dict, job_hash: str) -> bool:
    output = private_path(root, row["output"])
    metrics = output.with_suffix(".metrics.json")
    raw = output.with_name(output.stem + ".raw.png")
    if not any(p.exists() for p in (output, raw, metrics)):
        return False
    if not all(p.exists() for p in (output, raw, metrics)):
        raise ValueError(f"incomplete prior job requires inspection: {row['item_id']}")
    data = json.loads(metrics.read_text())
    if (
        data["status"] != "ok"
        or data["job_sha256"] != job_hash
        or data["image_sha256"] != sha256(output)
        or data["raw_sha256"] != sha256(raw)
    ):
        raise ValueError(f"changed/failed prior job: {row['item_id']}")
    for path, dimensions in ((output, (430, 480)), (raw, tuple(row["render_size"]))):
        with Image.open(path) as image:
            if image.size != dimensions:
                raise ValueError("prior output dimensions differ")
            image.verify()
    return True
