from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path

import pytest
from PIL import Image
from typer.testing import CliRunner

from cardforge.cli import app
from cardforge.knowledge.catalog import (
    Card,
    initialize,
    load_cards,
    read_jsonl,
    validate,
    write_jsonl,
)
from cardforge.knowledge.grammar import check_set
from cardforge.knowledge.review import import_reviews

PROJECT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def catalog_root(tmp_path):
    (tmp_path / "dataset").mkdir()
    folder = tmp_path / "knowledge/catalog"
    folder.mkdir(parents=True)
    vocab = (PROJECT / "knowledge/catalog/vocabulary.yaml").read_text()
    (folder / "vocabulary.yaml").write_text(vocab)
    buf = io.BytesIO()
    Image.new("RGB", (430, 480), "blue").save(buf, format="PNG")
    pixels = buf.getvalue()
    rows = read_jsonl(PROJECT / "knowledge/catalog/cards.jsonl")
    for r in rows:
        (tmp_path / f"dataset/{r['card_ref']}.png").write_bytes(pixels)
        r.update(
            source_sha256=hashlib.sha256(pixels).hexdigest(),
            has_alpha=False,
            alpha_meaning="absent",
        )
    write_jsonl(folder / "cards.jsonl", rows)
    (folder / "human_review.jsonl").write_text("")
    return tmp_path


def test_numeric_initialization_and_no_clobber(catalog_root):
    output = catalog_root / "draft.jsonl"
    assert initialize(catalog_root, output) == 160
    rows = read_jsonl(output)
    assert [r["card_ref"] for r in rows][8:11] == [
        "CardC6_card_09",
        "CardC6_card_10",
        "CardC6_card_11",
    ]
    assert rows[99]["position"] == 10 and rows[99]["set_index"] == 10
    before = output.read_bytes()
    with pytest.raises(ValueError, match="overwrite"):
        initialize(catalog_root, output)
    assert output.read_bytes() == before


def test_missing_and_numeric_alias_rejected(catalog_root):
    p = catalog_root / "dataset/CardC6_card_01.png"
    p.rename(catalog_root / "dataset/CardC6_card_001.png")
    # One canonical numeric source is acceptable in init; duplicates are not.
    p.write_bytes((catalog_root / "dataset/CardC6_card_001.png").read_bytes())
    with pytest.raises(ValueError, match="exactly once"):
        initialize(catalog_root, catalog_root / "draft.jsonl")


def test_human_confirmation_not_inferred(catalog_root):
    assert validate(catalog_root)["valid"]
    result = validate(catalog_root, require_human=True)
    assert not result["valid"] and result["human_confirmed"] == 0


def test_duplicate_unknown_vocabulary_and_changed_source(catalog_root):
    path = catalog_root / "knowledge/catalog/cards.jsonl"
    rows = read_jsonl(path)
    original = list(rows)
    write_jsonl(path, rows + [rows[0]])
    assert not validate(catalog_root)["valid"]
    rows = original
    rows[0]["background"]["color"] = "ultraviolet"
    write_jsonl(path, rows)
    assert any("outside vocabulary" in e for e in validate(catalog_root)["errors"])
    (catalog_root / "dataset/CardC6_card_01.png").write_bytes(b"changed")
    assert any("changed source" in e for e in validate(catalog_root)["errors"])


def review_row(card, **changes):
    return {
        "card_ref": card.card_ref,
        "source_sha256": card.source_sha256,
        "reviewer": "Test reviewer",
        "reviewed_at": "2026-11-01T00:00:00Z",
        "confirmed_fields": ["category", "set_theme"],
        "corrections": changes,
        "note": "Synthetic test, not a real approval",
        "label_source": "human",
    }


def test_human_overlay_does_not_use_crosscheck(catalog_root):
    card = load_cards(catalog_root)[0]
    folder = catalog_root / "knowledge/catalog"
    write_jsonl(folder / "crosscheck.jsonl", [{"card_ref": card.card_ref, "category": 5}])
    assert load_cards(catalog_root)[0].category == card.category
    incoming = catalog_root / "review.jsonl"
    write_jsonl(incoming, [review_row(card, category=3)])
    assert import_reviews(catalog_root, incoming) == 1
    assert load_cards(catalog_root)[0].category == 3
    assert load_cards(catalog_root, apply_human=False)[0].category == card.category


def test_invalid_review_is_atomic(catalog_root):
    cards = load_cards(catalog_root)
    incoming = catalog_root / "review.jsonl"
    valid = review_row(cards[0], category=3)
    invalid = review_row(cards[1], category=99)
    write_jsonl(incoming, [valid, invalid])
    with pytest.raises(ValueError):
        import_reviews(catalog_root, incoming)
    assert (catalog_root / "knowledge/catalog/human_review.jsonl").read_text() == ""
    valid["source_sha256"] = "0" * 64
    write_jsonl(incoming, [valid])
    with pytest.raises(ValueError, match="hash mismatch"):
        import_reviews(catalog_root, incoming)


def test_schema_rejects_metadata_mutation(catalog_root):
    row = load_cards(catalog_root)[0].model_dump()
    row["position"] = 9
    with pytest.raises(ValueError):
        Card.model_validate(row)
    row["position"] = 1
    row["measured"]["fill_ratio"] = 1.2
    with pytest.raises(ValueError):
        Card.model_validate(row)


def test_reference_exceptions_do_not_leak_to_production():
    cards = load_cards(PROJECT)
    grammar = json.loads((PROJECT / "knowledge/set_grammar/v1.yaml").read_text())
    for s in range(1, 16):
        subset = [c for c in cards if c.set_index == s]
        assert check_set(subset, grammar)["valid"]
    sea = [c for c in cards if c.set_index == 7]
    assert check_set(sea, grammar)["applied_exceptions"]
    production = check_set(sea, grammar, mode="production")
    assert not production["valid"]
    assert any("missing categories" in e for e in production["errors"])
    assert any("gold" in e for e in production["errors"])


def test_monotonous_and_duplicate_set_fails():
    cards = load_cards(PROJECT)[:10]
    grammar = json.loads((PROJECT / "knowledge/set_grammar/v1.yaml").read_text())
    assert not check_set([cards[0]] * 10, grammar)["valid"]
    changed = [c.model_copy(deep=True) for c in cards]
    for c in changed:
        c.background.color = "blue"
        c.scale_class = "handheld"
        c.object_type = "tool"
    result = check_set(changed, grammar)
    assert not result["valid"]
    assert len(result["errors"]) >= 3


def test_rule_examples_and_no_fake_centroid():
    ids = {c.card_ref for c in load_cards(PROJECT)}
    for name in [
        "knowledge/set_grammar/v1.yaml",
        "knowledge/style_profile/reference_cards_v1.yaml",
    ]:
        rules = json.loads((PROJECT / name).read_text())["rules"]
        assert all(len(set(r["examples"])) >= 2 and set(r["examples"]) <= ids for r in rules)
    profile = json.loads((PROJECT / "knowledge/style_profile/reference_cards_v1.yaml").read_text())
    assert profile["hard_style_rejection_enabled"] is False


def test_cli_missing_human_exit(catalog_root):
    result = CliRunner().invoke(
        app, ["catalog", "validate", "--root", str(catalog_root), "--require-human"]
    )
    assert result.exit_code == 1
    assert "0/160" in result.output


def test_base_catalog_import_does_not_initialize_ml():
    import subprocess
    import sys

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import cardforge.knowledge.catalog; import cardforge.knowledge.review; "
            'assert not any(k.startswith(("mlx", "rembg", "onnxruntime")) for k in sys.modules)',
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_review_keeps_prior_corrections_on_reexport(catalog_root):
    from cardforge.knowledge.review import build_review

    # Reexport must compare edits to immutable agent labels, not erase an earlier
    # correction by comparing the displayed effective labels with themselves.
    card = load_cards(catalog_root)[0]
    incoming = catalog_root / "review.jsonl"
    write_jsonl(incoming, [review_row(card, category=3)])
    import_reviews(catalog_root, incoming)
    folder = catalog_root / "knowledge/catalog"
    write_jsonl(folder / "disagreements.jsonl", [])
    output = catalog_root / "review.html"
    build_review(catalog_root, output)
    text = output.read_text()
    original = json.loads(text.split("const original=", 1)[1].split(";\nconst byId", 1)[0])
    assert original[0]["labels"]["category"] == card.category
    assert "Категория 3" in text
    assert 'checked="' not in text
    assert "{{DATA}}" not in text


def test_adjudication_requires_confirmation_of_the_disputed_field(catalog_root):
    from cardforge.knowledge.review import adjudicate_disagreements

    card = load_cards(catalog_root)[0]
    row = review_row(card)
    differences = [
        {"card_ref": card.card_ref, "field": "category", "first_pass": 1, "crosscheck": 4},
        {
            "card_ref": card.card_ref,
            "field": "materials",
            "first_pass": ["wood"],
            "crosscheck": ["metal"],
        },
    ]
    result = adjudicate_disagreements(differences, [card], [row])
    assert result[0]["status"] == "resolved_by_human"
    assert result[0]["resolution"] == card.category
    assert result[1]["status"] == "awaiting_human"
    assert differences[0]["first_pass"] == 1
    row["source_sha256"] = "0" * 64
    assert all(
        d["status"] == "awaiting_human"
        for d in adjudicate_disagreements(differences, [card], [row])
    )


def test_adjudication_preserves_blind_values_after_a_third_choice(catalog_root):
    from cardforge.knowledge.review import adjudicate_disagreements

    card = load_cards(catalog_root)[0]
    row = review_row(card)
    row["confirmed_fields"].append("materials")
    differences = [
        {
            "card_ref": card.card_ref,
            "field": "materials",
            "first_pass": ["metal"],
            "crosscheck": ["plastic"],
        }
    ]
    result = adjudicate_disagreements(differences, [card], [row])[0]
    assert result["resolution"] == card.materials
    assert result["first_pass"] == ["metal"]
    assert result["crosscheck"] == ["plastic"]


@pytest.fixture()
def acceptance_root(catalog_root):
    from cardforge.knowledge.catalog import LABEL_FIELDS

    cards = load_cards(catalog_root)
    folder = catalog_root / "knowledge/catalog"
    reviews = [review_row(c) for c in cards]
    for row in reviews:
        row["confirmed_fields"] = list(LABEL_FIELDS)
    write_jsonl(folder / "human_review.jsonl", reviews)
    for name in [
        "knowledge/set_grammar/v1.yaml",
        "knowledge/style_profile/reference_cards_v1.yaml",
        "knowledge/catalog/acceptance_policy.json",
    ]:
        path = catalog_root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((PROJECT / name).read_bytes())
    disputes = [
        {
            "card_ref": cards[0].card_ref,
            "field": "category",
            "status": "resolved_by_human",
            "resolution": cards[0].category,
        }
    ]
    for name in ["disagreements.jsonl", "disagreements.final-blind.jsonl"]:
        write_jsonl(folder / name, disputes)
    shot = catalog_root / "dataset/synthetic-window.png"
    shot.write_bytes(b"synthetic test evidence")
    sha = hashlib.sha256(shot.read_bytes()).hexdigest()
    frame = catalog_root / "knowledge/frames"
    frame.mkdir(parents=True)
    write_jsonl(
        frame / "semantic_review.jsonl",
        [
            {
                "card_ref": c.card_ref,
                "source_sha256": c.source_sha256,
                "screenshot": "dataset/synthetic-window.png",
                "screenshot_sha256": sha,
                "assessment": "clear",
                "note": "Synthetic test; not real visual evidence",
                "evidence": ["dataset/synthetic-window.png"],
                "critical_identity_occluded": False,
                "noticeable_noncritical_contact": False,
            }
            for c in cards
        ],
    )
    (frame / "safe_zone.json").write_text(
        json.dumps({"method": "synthetic test", "semantic_audit": {"test": True}})
    )
    return catalog_root


def test_stage_acceptance_requires_explicit_scoped_criterion_change(acceptance_root):
    from cardforge.knowledge.acceptance import stage1_acceptance

    assert stage1_acceptance(acceptance_root)["complete"]
    path = acceptance_root / "knowledge/catalog/acceptance_policy.json"
    policy = json.loads(path.read_text())
    policy["source"] = "agent_inference"
    path.write_text(json.dumps(policy))
    report = stage1_acceptance(acceptance_root)
    assert not report["complete"]
    assert not report["checks"]["crosscheck_replaced_by_explicit_human_gate"]


def test_stage_acceptance_rejects_stale_visual_evidence(acceptance_root):
    from cardforge.knowledge.acceptance import stage1_acceptance

    (acceptance_root / "dataset/synthetic-window.png").write_bytes(b"changed")
    report = stage1_acceptance(acceptance_root)
    assert not report["complete"]
    assert not report["checks"]["safe_zone_method_and_all_card_visual_audit"]


def test_stage_acceptance_rejects_fake_adjudication_and_incomplete_coverage(acceptance_root):
    from cardforge.knowledge.acceptance import stage1_acceptance

    path = acceptance_root / "knowledge/catalog/disagreements.jsonl"
    rows = read_jsonl(path)
    rows[0]["resolution"] = 99
    write_jsonl(path, rows)
    assert not stage1_acceptance(acceptance_root)["complete"]
    rows[0]["resolution"] = load_cards(acceptance_root)[0].category
    write_jsonl(path, rows)
    review = acceptance_root / "knowledge/catalog/human_review.jsonl"
    write_jsonl(review, read_jsonl(review)[:-1])
    assert not stage1_acceptance(acceptance_root)["complete"]
