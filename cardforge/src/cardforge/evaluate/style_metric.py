"""DINOv2 CLS embeddings and leave-one-out reference distributions, on CPU."""

from __future__ import annotations

import json
import time
import tomllib
from pathlib import Path

from cardforge.evaluate.common import dump, sha256
from cardforge.knowledge.catalog import load_cards


def normalize(matrix):
    import numpy as np

    matrix = np.asarray(matrix, dtype=float)
    if not np.isfinite(matrix).all() or matrix.ndim != 2:
        raise ValueError("expected finite embedding matrix")
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    if (norms == 0).any():
        raise ValueError("zero embedding")
    return matrix / norms


def fit(vectors, ids: list[str], hashes: list[str]) -> dict:
    import numpy as np

    x = normalize(vectors)
    if len(x) < 7 or len(x) != len(ids) or len(hashes) != len(ids):
        raise ValueError("need at least seven uniquely identified reference embeddings")
    if len(set(ids)) != len(ids) or len(set(hashes)) != len(hashes):
        raise ValueError("duplicate reference IDs or source images")
    centroid = normalize(x.mean(axis=0, keepdims=True))[0]
    loo = normalize(x.sum(axis=0, keepdims=True) - x)
    distances = np.clip(1 - (x * loo).sum(axis=1), 0, 2)
    pairwise = np.clip(1 - x @ x.T, 0, 2)
    np.fill_diagonal(pairwise, np.inf)
    knn = np.sort(pairwise, axis=1)[:, :5].mean(axis=1)
    return {
        "ids": ids,
        "hashes": hashes,
        "vectors": x.tolist(),
        "centroid": centroid.tolist(),
        "reference_centroid_distances_loo": distances.tolist(),
        "reference_knn_distances_loo": knn.tolist(),
        "k": 5,
    }


def score(vector, index: dict, *, image_sha256: str | None = None) -> dict:
    import numpy as np

    x = normalize([vector])[0]
    refs = np.asarray(index["vectors"])
    keep = [i for i, h in enumerate(index["hashes"]) if h != image_sha256]
    if len(keep) < 5:
        raise ValueError("insufficient neighbours after self-exclusion")
    refs = refs[keep]
    centroid = normalize(refs.mean(axis=0, keepdims=True))[0]
    distance = float(np.clip(1 - x @ centroid, 0, 2))
    pairwise = np.clip(1 - refs @ x, 0, 2)
    nearest = np.argsort(pairwise)[:5]
    knn = float(pairwise[nearest].mean())

    def percentile(value, distribution):
        d = np.asarray(distribution)
        return float(100 * ((d < value).sum() + 0.5 * (d == value).sum()) / len(d))

    return {
        "centroid_distance": distance,
        "centroid_percentile": percentile(distance, index["reference_centroid_distances_loo"]),
        "knn_distance": knn,
        "knn_percentile": percentile(knn, index["reference_knn_distances_loo"]),
        "nearest": [
            {"item_id": index["ids"][keep[i]], "distance": float(pairwise[i])} for i in nearest
        ],
        "interpretation": "Lower distance/percentile is closer; "
        "not an acceptance score. Reference self excluded by source SHA.",
    }


def embed(paths: list[Path], model_path: Path):
    import torch
    from PIL import Image
    from transformers import AutoImageProcessor, AutoModel

    torch.set_num_threads(4)
    processor = AutoImageProcessor.from_pretrained(model_path, local_files_only=True)
    model = AutoModel.from_pretrained(model_path, local_files_only=True).eval().to("cpu")
    vectors = []
    with torch.inference_mode():
        for start in range(0, len(paths), 8):
            images = [Image.open(p).convert("RGB") for p in paths[start : start + 8]]
            inputs = processor(images=images, return_tensors="pt")
            vectors.extend(model(**inputs).last_hidden_state[:, 0, :].cpu().tolist())
    return vectors


def build_index(root: Path) -> dict:
    import importlib.metadata

    import numpy as np

    manifest_path = root / "models/dinov2-base/source.json"
    manifest = json.loads(manifest_path.read_text())
    for name, expected in manifest["sha256"].items():
        if sha256(root / "models/dinov2-base" / name) != expected:
            raise ValueError(f"changed DINOv2 file: {name}")
    cards = [c for c in load_cards(root) if c.category <= 4]
    paths = [root / f"dataset/{c.card_ref}.png" for c in cards]
    hashes = [sha256(p) for p in paths]
    if hashes != [c.source_sha256 for c in cards]:
        raise ValueError("changed catalog image")
    started = time.monotonic()
    vectors = embed(paths, root / "models/dinov2-base")
    index = fit(vectors, [c.card_ref for c in cards], hashes)
    index["model"] = {
        "id": manifest["model_id"],
        "revision": manifest["revision"],
        "weights_sha256": manifest["sha256"]["model.safetensors"],
        "embedding": "last_hidden_state CLS; L2 normalized; CPU float32",
        "processor_sha256": manifest["sha256"]["preprocessor_config.json"],
        "torch": importlib.metadata.version("torch"),
        "transformers": importlib.metadata.version("transformers"),
    }
    dump(root / "dataset/analysis/evaluation/style-index.json", index)
    x = np.asarray(index["vectors"])
    # Content confounding cannot be established without matched foreign objects.
    by_category = {
        str(cat): {
            "n": sum(c.category == cat for c in cards),
            "median_loo_distance": float(
                np.median(
                    [
                        d
                        for c, d in zip(
                            cards, index["reference_centroid_distances_loo"], strict=True
                        )
                        if c.category == cat
                    ]
                )
            ),
        }
        for cat in range(1, 5)
    }
    by_set = []
    for set_index in sorted({c.set_index for c in cards}):
        held = [i for i, c in enumerate(cards) if c.set_index == set_index]
        train = [i for i, c in enumerate(cards) if c.set_index != set_index]
        centroid = normalize(x[train].mean(axis=0, keepdims=True))[0]
        by_set.append(
            {"set_index": set_index, "median_distance": float(np.median(1 - x[held] @ centroid))}
        )
    distances = 1 - x @ np.asarray(index["centroid"])
    anchors = [cards[i].card_ref for i in np.argsort(distances)[:4]]
    report = {
        "n": len(cards),
        "elapsed_s": time.monotonic() - started,
        "model": index["model"],
        "by_category": by_category,
        "leave_one_set_out": by_set,
        "centroid_nearest_candidates": anchors,
        "auc": None,
        "style_content_separation_validated": False,
        "reason": "30-50 matched-object foreign samples missing; CSD decision pending",
    }
    dump(root / "docs/stages/evidence/02/style-baseline.json", report)
    return report


def verify_model(root: Path, index: dict) -> None:
    manifest = json.loads((root / "models/dinov2-base/source.json").read_text())
    for name, expected in manifest["sha256"].items():
        if sha256(root / "models/dinov2-base" / name) != expected:
            raise ValueError(f"changed DINOv2 file: {name}")
    if index["model"]["revision"] != manifest["revision"] or index["model"][
        "weights_sha256"
    ] != sha256(root / "models/dinov2-base/model.safetensors"):
        raise ValueError("style index and embedding model differ")
    if index["model"]["processor_sha256"] != manifest["sha256"]["preprocessor_config.json"]:
        raise ValueError("style index and image processor differ")


def score_image(root: Path, image: Path, metric: str | None = None) -> dict:
    if metric is None:
        config = root / "config/evaluation.toml"
        metric = (
            tomllib.loads(config.read_text())["style"]["primary"] if config.exists() else "dinov2"
        )
    if metric == "csd":
        from cardforge.evaluate.csd_metric import embed as csd_embed

        index = json.loads((root / "dataset/analysis/evaluation/csd-style-index.json").read_text())
        source = json.loads((root / "models/csd-vit-l/source.json").read_text())
        if index["model"] != source:
            raise ValueError("CSD index and model differ")
        vector = csd_embed([image], root / "models/csd-vit-l")[0]
        return {"metric": "csd", **score(vector, index, image_sha256=sha256(image))}
    if metric != "dinov2":
        raise ValueError("unknown style metric; choose csd or dinov2")
    index = json.loads((root / "dataset/analysis/evaluation/style-index.json").read_text())
    verify_model(root, index)
    vector = embed([image], root / "models/dinov2-base")[0]
    return {"metric": "dinov2", **score(vector, index, image_sha256=sha256(image))}


def calibrate_style(root: Path, metric: str = "dinov2") -> dict:
    from cardforge.evaluate.judge_pack import load_manifest

    if metric == "csd":
        report = json.loads((root / "docs/stages/evidence/02/csd-calibration.json").read_text())
        load_manifest(root, root / "calibration/manifest.json")
        if report.get("manifest_sha256") != sha256(root / "calibration/manifest.json"):
            raise ValueError("CSD input changed; rerun scripts/evaluate_csd.py")
        source = json.loads((root / "models/csd-vit-l/source.json").read_text())
        if report["model"] != source:
            raise ValueError("CSD model changed; recalibrate")
        for name, expected in source["sha256"].items():
            if sha256(root / "models/csd-vit-l" / name) != expected:
                raise ValueError(f"changed CSD model file: {name}")
        return report
    if metric != "dinov2":
        raise ValueError("unknown style metric; choose csd or dinov2")
    items = load_manifest(root, root / "calibration/manifest.json")
    index = json.loads((root / "dataset/analysis/evaluation/style-index.json").read_text())
    verify_model(root, index)
    real = {r["item_id"]: r for r in items if r["cohort"] == "real"}
    foreign_all = [r for r in items if r["cohort"] == "foreign"]
    foreign = [r for r in foreign_all if r.get("style_calibration_eligible", True)]
    if not foreign:
        return {
            "auc": None,
            "foreign_n": 0,
            "acceptance_auc_09": False,
            "style_content_separation_validated": False,
            "reason": "Matched-object foreign samples absent; CSD experiment pending",
        }
    for row in foreign:
        matched = row.get("matched_reference_id")
        if (
            matched not in index["ids"]
            or matched not in real
            or row["spec"]["object_name"] != real[matched]["spec"]["object_name"]
        ):
            raise ValueError("foreign sample requires a matching object and matched_reference_id")
    from cardforge.evaluate.local_batch import fingerprint

    cache_path = root / "runs/stage02/01-local36/evaluation/dinov2-embeddings.json"
    cache_key = fingerprint(
        {"model": index["model"], "images": [[r["item_id"], r["image_sha256"]] for r in foreign]}
    )
    cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}
    if cache.get("key") == cache_key:
        vectors = normalize(cache["vectors"])
    else:
        vectors = normalize(
            embed([root / r["image"] for r in foreign], root / "models/dinov2-base")
        )
        dump(cache_path, {"key": cache_key, "vectors": vectors.tolist()})
    return {
        **compare_embeddings(index, foreign, vectors, real),
        "manifest_sha256": sha256(root / "calibration/manifest.json"),
        "excluded_object_mismatch_ids": [
            r["item_id"] for r in foreign_all if not r.get("style_calibration_eligible", True)
        ],
    }


def compare_embeddings(index: dict, foreign: list[dict], vectors, real: dict) -> dict:
    import numpy as np

    from cardforge.evaluate.calibration import roc_auc

    for identifier, expected in zip(index["ids"], index["hashes"], strict=True):
        if real.get(identifier, {}).get("image_sha256") != expected:
            raise ValueError("stale reference style embedding")
    vectors = normalize(vectors)
    refs = np.asarray(index["vectors"])
    positives, negatives, scores = [], [], []
    for row, vector in zip(foreign, vectors, strict=True):
        i = index["ids"].index(row["matched_reference_id"])
        if real[row["matched_reference_id"]]["image_sha256"] != index["hashes"][i]:
            raise ValueError("stale reference style embedding")
        # The same centroid, with the matched real object excluded, scores both members.
        centroid = normalize((refs.sum(axis=0) - refs[i])[None, :])[0]
        pos, neg = float(refs[i] @ centroid), float(vector @ centroid)
        positives.append(pos)
        negatives.append(neg)
        scores.append(
            {
                "item_id": row["item_id"],
                "matched_reference_id": row["matched_reference_id"],
                "positive_similarity": pos,
                "foreign_similarity": neg,
            }
        )
    matched_auc = roc_auc(positives, negatives)
    centroid = np.asarray(index["centroid"])
    # Primary reference-vs-foreign AUC uses all 130 distinct real controls, not repeated pairs.
    real_similarities = [1 - d for d in index["reference_centroid_distances_loo"]]
    foreign_similarities = [float(v @ centroid) for v in vectors]
    auc = roc_auc(real_similarities, foreign_similarities)
    by_style = {
        style: {
            "n": sum(r.get("foreign_style") == style for r in foreign),
            "auc": roc_auc(
                real_similarities,
                [
                    s
                    for r, s in zip(foreign, foreign_similarities, strict=True)
                    if r.get("foreign_style") == style
                ],
            ),
        }
        for style in sorted({r.get("foreign_style") for r in foreign})
    }
    groups = {r.get("foreign_style") for r in foreign}
    all_styles = {"photorealism", "flat_2d", "other_3d"}.issubset(groups)
    validated = 30 <= len(foreign) <= 50 and all_styles and auc >= 0.9
    return {
        "auc": auc,
        "foreign_n": len(foreign),
        "real_n": len(real_similarities),
        "matched_object_auc": matched_auc,
        "by_style": by_style,
        "acceptance_auc_09": auc >= 0.9,
        "required_style_groups_present": all_styles,
        "matched_scores": scores,
        "style_content_separation_validated": validated,
        "note": "Exploratory matched-object calibration, not held-out generalization. "
        "If AUC < .9, evaluate CSD before accepting a style metric.",
    }
