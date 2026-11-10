"""Integrity-checked import of a completed human review against a frozen working judge."""

from __future__ import annotations

import json
import re
import shutil
from collections import Counter
from datetime import datetime
from pathlib import Path

from cardforge.evaluate.calibration import cohen_kappa
from cardforge.evaluate.common import dump, private_path, sha256
from cardforge.evaluate.human_review import validate_decision
from cardforge.evaluate.judge_pack import import_labels, load_manifest
from cardforge.evaluate.models import JudgeAnswer, JudgeIdentity, Label
from cardforge.knowledge.catalog import write_jsonl


def validate_campaign(
    root: Path, manifest: Path, session: Path, frozen: Path, expected_frozen_sha256: str
) -> dict:
    if sha256(frozen) != expected_frozen_sha256:
        raise ValueError("working judge changed after the review was frozen")
    items = load_manifest(root, manifest)
    if any(r["cohort"] != "borderline" for r in items):
        raise ValueError("human campaign requires explicit borderline candidates")
    by_id = {r["item_id"]: r for r in items}
    public = json.loads((session / "session.json").read_text())["items"]
    by_review = {r["review_id"]: r for r in public}
    ratings = json.loads((session / "ratings.json").read_text())
    if len(by_review) != len(items) or set(ratings) != set(by_review):
        raise ValueError("human review must cover the complete campaign exactly once")
    frozen_data = json.loads(frozen.read_text())
    identity = JudgeIdentity.model_validate(frozen_data["judge"])
    records = frozen_data["records"]
    if len(records) != len(items) or {r["item_id"] for r in records} != set(by_id):
        raise ValueError("frozen judge coverage mismatch")
    judged = {}
    for row in records:
        answer = JudgeAnswer.model_validate(row["answer"])
        item = by_id[row["item_id"]]
        if (
            row["judge"] != identity.model_dump()
            or answer.item_id != row["item_id"]
            or answer.image_sha256 != item["image_sha256"]
            or row["image_sha256"] != item["image_sha256"]
            or row["cohort"] != item["cohort"]
            or row["decision"] != answer.decision
        ):
            raise ValueError("frozen judge/image provenance mismatch")
        judged[row["item_id"]] = row
    labels, pairs, disagreements, raw = [], [], [], []
    seen = set()
    for review_id, row in ratings.items():
        item = by_review[review_id]
        checked = validate_decision(
            {k: row[k] for k in ("review_id", "image_sha256", "reviewer", "decision", "note")}, item
        )
        canonical = by_id.get(row["item_id"])
        if (
            row["review_id"] != review_id
            or row["source"] != "human"
            or row["item_id"] != checked["item_id"]
            or canonical is None
            or canonical["image_sha256"] != row["image_sha256"]
            or canonical["image"] != item["image"]
            or canonical["spec"] != item["spec"]
            or type(row["accepted"]) is not type(checked["accepted"])
            or row["accepted"] != checked["accepted"]
            or datetime.fromisoformat(row["reviewed_at"]).tzinfo is None
            or row["item_id"] in seen
        ):
            raise ValueError("human label/image provenance mismatch")
        seen.add(row["item_id"])
        raw.append(row)
        if row["accepted"] is None:
            continue
        labels.append(
            Label(
                item_id=row["item_id"],
                image_sha256=row["image_sha256"],
                cohort="borderline",
                accepted=row["accepted"],
                source="human",
                reviewer=row["reviewer"],
                note=row["note"],
            ).model_dump()
        )
        prediction = judged[row["item_id"]]
        if prediction["decision"] != "flag":
            accepted = prediction["decision"] == "accept"
            pairs.append((row["accepted"], accepted))
            if row["accepted"] != accepted:
                disagreements.append(
                    {
                        "item_id": row["item_id"],
                        "human": row["decision"],
                        "judge": prediction["decision"],
                        "human_note": row["note"],
                    }
                )
    if seen != set(by_id):
        raise ValueError("human item coverage mismatch")
    kappa = cohen_kappa([a for a, _ in pairs], [b for _, b in pairs]) if pairs else None
    summary = {
        "human_n": len(raw),
        "human_decisions": dict(Counter(r["decision"] for r in raw)),
        "judge_decisions": dict(Counter(r["decision"] for r in records)),
        "binary_paired_n": len(pairs),
        "cohen_kappa": kappa,
        "observed_agreement": sum(a == b for a, b in pairs) / len(pairs) if pairs else None,
        "confusion": {
            name: sum(a == truth and b == prediction for a, b in pairs)
            for name, truth, prediction in (
                ("TP", True, True),
                ("TN", False, False),
                ("FP", False, True),
                ("FN", True, False),
            )
        },
        "kappa_criterion_met": len(pairs) >= 40 and kappa is not None and kappa >= 0.6,
        "disagreements": disagreements,
        "judge": identity.model_dump(),
        "note": "Flags and uncertain human labels excluded from binary pairing. "
        "Existing human accept decisions are preserved even with suggested edits.",
    }
    return {"items": items, "raw": raw, "labels": labels, "records": records, "summary": summary}


def import_campaign(
    root: Path,
    manifest: Path,
    session: Path,
    frozen: Path,
    expected_frozen_sha256: str,
    output: Path,
) -> dict:
    validated = validate_campaign(root, manifest, session, frozen, expected_frozen_sha256)
    if output.exists():
        raise ValueError("review snapshot exists; refusing to overwrite")
    main_path = root / "calibration/manifest.json"
    main = json.loads(main_path.read_text())
    if {r["item_id"] for r in main["items"]} & {r["item_id"] for r in validated["items"]}:
        raise ValueError("campaign already registered or item IDs overlap")
    pack_ids = {r["pack_id"] for r in validated["records"]}
    if len(pack_ids) != 1:
        raise ValueError("one frozen working campaign required")
    pack_id = next(iter(pack_ids))
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,95}", pack_id):
        raise ValueError("invalid frozen campaign ID")
    output_records = root / "calibration/judgements" / f"{pack_id}.jsonl"
    if output_records.exists():
        raise ValueError("working judge records already imported")
    output.mkdir(parents=True)
    before = output / "before"
    before.mkdir()
    for relative in (
        "calibration/manifest.json",
        "calibration/labels.jsonl",
        "docs/stages/evidence/02/acceptance.json",
        "docs/stages/evidence/02/csd-calibration.json",
    ):
        path = root / relative
        if path.exists():
            shutil.copy2(path, before / (relative.replace("/", "__")))
    shutil.copy2(session / "ratings.json", output / "ratings.original.json")
    shutil.copy2(session / "session.json", output / "session.original.json")
    shutil.copy2(frozen, output / "codex.frozen.original.json")
    write_jsonl(output / "human.original.jsonl", validated["raw"])
    write_jsonl(output / "labels.jsonl", validated["labels"])
    main["items"].extend(
        {k: v for k, v in r.items() if k != "review_priority"} for r in validated["items"]
    )
    dump(main_path, main)
    result = import_labels(root, output / "labels.jsonl")
    write_jsonl(output_records, validated["records"])
    summary = {
        **validated["summary"],
        "import": result,
        "source_ratings_sha256": sha256(output / "ratings.original.json"),
        "frozen_judge_sha256": expected_frozen_sha256,
        "new_main_items": len(validated["items"]),
        "working_output": str(output_records),
    }
    dump(output / "summary.json", summary)
    dump(root / "docs/stages/evidence/02/target-human-import.json", summary)
    for row in validated["raw"]:
        folder = output / row["item_id"]
        folder.mkdir()
        item = next(r for r in validated["items"] if r["item_id"] == row["item_id"])
        image = private_path(root, item["image"])
        for name in ("image.png", "thumbnail.png"):
            (folder / name).symlink_to((image.parent / name).resolve())
        shutil.copy2(image.parent / "judges.json", folder / "judges.before-human.json")
        dump(folder / "human.json", row)
        dump(folder / "spec.json", item["spec"])
    return summary
