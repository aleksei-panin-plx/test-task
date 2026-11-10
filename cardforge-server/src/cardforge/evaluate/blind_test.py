"""Blind 7+3 sheets, including an all-real control with hidden random pseudo-targets."""

from __future__ import annotations

import json
import math
import random
import secrets
from pathlib import Path

from PIL import Image, ImageDraw

from cardforge.evaluate.common import dump, private_path, sha256
from cardforge.evaluate.judge_pack import load_manifest


def chance_distribution(sheets: int) -> list[float]:
    # Each sheet: choose three positions, three hidden targets among ten.
    single = [math.comb(3, k) * math.comb(7, 3 - k) / math.comb(10, 3) for k in range(4)]
    distribution = [1.0]
    for _ in range(sheets):
        updated = [0.0] * (len(distribution) + 3)
        for i, a in enumerate(distribution):
            for j, b in enumerate(single):
                updated[i + j] += a * b
        distribution = updated
    return distribution


def export_blind(
    root: Path,
    manifest: Path,
    output: Path,
    *,
    seed: int,
    all_real: bool = True,
    sheets: int = 10,
    working_family: str | None = None,
) -> dict:
    if sheets < 10:
        raise ValueError("minimum ten sheets per profile")
    if (
        not any(
            output.resolve().is_relative_to((root / folder).resolve())
            for folder in ("dataset", "runs")
        )
        or output.exists()
    ):
        raise ValueError("use a new private directory under dataset/ or runs/")
    if not all_real and not output.resolve().is_relative_to((root / "runs").resolve()):
        raise ValueError("mixed sheets with generated art must stay in runs/")
    items = load_manifest(root, manifest)
    real = [r for r in items if r["cohort"] == "real"]
    foreign = [
        r for r in items if r["cohort"] == "foreign" and r.get("style_calibration_eligible", True)
    ]
    if len({r["spec"]["set_theme"] for r in real}) < (10 if all_real else 7):
        raise ValueError("not enough distinct real sets")
    if not all_real and len(foreign) < 3:
        raise ValueError("need foreign samples for mixed blind sheets")
    output.mkdir(parents=True)
    rng = random.Random(seed)
    pack_id = secrets.token_hex(12)
    key, files = [], {}
    for n in range(sheets):
        shuffled = rng.sample(real, len(real))
        chosen, themes = [], set()
        for row in shuffled:
            if row["spec"]["set_theme"] not in themes:
                chosen.append(row)
                themes.add(row["spec"]["set_theme"])
            if len(chosen) == (10 if all_real else 7):
                break
        if not all_real:
            chosen.extend(rng.sample(foreign, 3))
        rng.shuffle(chosen)
        targets = (
            sorted(rng.sample(range(1, 11), 3))
            if all_real
            else [p for p, row in enumerate(chosen, 1) if row["cohort"] == "foreign"]
        )
        sheet = Image.new("RGB", (215 * 5, 265 * 2), "white")
        draw = ImageDraw.Draw(sheet)
        for pos, row in enumerate(chosen):
            x, y = pos % 5 * 215, pos // 5 * 265
            image = Image.open(private_path(root, row["image"])).convert("RGB")
            sheet.paste(image.resize((215, 240), Image.Resampling.LANCZOS), (x, y))
            draw.text((x + 8, y + 243), str(pos + 1), fill="black")
        name = f"sheet-{n + 1:02}.png"
        sheet.save(output / name)
        files[name] = sha256(output / name)
        key.append({"sheet": name, "targets": targets, "items": [r["item_id"] for r in chosen]})
    dump(
        output / "response.json",
        {"pack_id": pack_id, "reviewer": "FILL", "family": "FILL", "answers": []},
    )
    (output / "README.txt").write_text(
        "В каждом листе выберите ровно 3 наиболее чужие карты. "
        "Запишите answers: [{sheet: sheet-01.png, positions: [1,2,3]}, ...]. "
        "Заполните reviewer и family фактическими значениями. Все 10 листов обязательны.\n"
    )
    files["README.txt"] = sha256(output / "README.txt")
    dump(
        root / f"dataset/analysis/evaluation/blind-keys/{pack_id}.json",
        {
            "output": str(output.resolve()),
            "all_real": all_real,
            "sheets": key,
            "files": files,
            "seed": seed,
            "working_judge_family": working_family,
            "pack_id": pack_id,
        },
    )
    return {"pack_id": pack_id, "sheets": sheets, "path": str(output), "status": "awaiting_judge"}


def score_blind(root: Path, pack: Path, response: Path) -> dict:
    data = json.loads(response.read_text())
    if (
        not data.get("reviewer")
        or not data.get("family")
        or "FILL" in (data["reviewer"], data["family"])
    ):
        raise ValueError("actual reviewer and family required")
    key = json.loads(
        private_path(
            root, f"dataset/analysis/evaluation/blind-keys/{data['pack_id']}.json"
        ).read_text()
    )
    if Path(key["output"]) != pack.resolve():
        raise ValueError("unregistered pack path")
    for name, expected in key["files"].items():
        if sha256(private_path(pack, name)) != expected:
            raise ValueError("changed blind-test input")
    answers = {r["sheet"]: r["positions"] for r in data["answers"]}
    expected = {r["sheet"] for r in key["sheets"]}
    if set(answers) != expected or len(answers) != len(data["answers"]):
        raise ValueError("all sheets required exactly once")
    hits = 0
    for row in key["sheets"]:
        selected = answers[row["sheet"]]
        if (
            len(selected) != 3
            or len(set(selected)) != 3
            or any(type(p) is not int or not 1 <= p <= 10 for p in selected)
        ):
            raise ValueError("select three distinct integer positions 1..10")
        hits += len(set(selected) & set(row["targets"]))
    distribution = chance_distribution(len(answers))
    p = min(1.0, 2 * min(sum(distribution[: hits + 1]), sum(distribution[hits:])))
    return {
        "pack_id": data["pack_id"],
        "reviewer": data["reviewer"],
        "family": data["family"],
        "control_all_real": key["all_real"],
        "hits": hits,
        "trials": len(answers) * 3,
        "target_recall": hits / (len(answers) * 3),
        "chance_recall": 0.3,
        "exact_two_sided_p": p,
        "consistent_with_chance": p >= 0.05,
        "independent_family_verified": bool(key.get("working_judge_family"))
        and key["working_judge_family"].casefold() != data["family"].casefold(),
        "note": "All-real targets are independent hidden random pseudo-targets. "
        "Failure to reject chance is a control check, not proof of an unbiased judge.",
    }
