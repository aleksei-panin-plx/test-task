"""Private, portable review package. No checkbox is pre-approved."""

from __future__ import annotations

import base64
import hashlib
import html
import io
import json
from pathlib import Path

from PIL import Image

from cardforge.knowledge.catalog import (
    LABEL_FIELDS,
    Card,
    HumanReview,
    load_cards,
    read_jsonl,
    vocabulary_errors,
    write_jsonl,
)


def import_reviews(root: Path, path: Path) -> int:
    cards = {c.card_ref: c for c in load_cards(root, apply_human=False)}
    vocab = json.loads((root / "knowledge/catalog/vocabulary.yaml").read_text())
    destination = root / "knowledge/catalog/human_review.jsonl"
    existing = read_jsonl(destination) if destination.exists() else []
    merged = {r["card_ref"]: r for r in existing}
    incoming = read_jsonl(path)
    seen = set()
    for row in incoming:
        r = HumanReview.model_validate(row)
        if r.card_ref in seen or r.card_ref not in cards:
            raise ValueError("duplicate or unknown review card")
        seen.add(r.card_ref)
        c = cards[r.card_ref]
        if (
            c.source_sha256 != r.source_sha256
            or hashlib.sha256((root / f"dataset/{c.card_ref}.png").read_bytes()).hexdigest()
            != r.source_sha256
        ):
            raise ValueError("review source hash mismatch")
        updated = Card.model_validate({**c.model_dump(), **r.corrections, "label_source": "human"})
        errors = vocabulary_errors(updated, vocab)
        if errors:
            raise ValueError("; ".join(errors))
        merged[r.card_ref] = r.model_dump()
    # Validate everything before touching the existing human layer.
    temp = destination.with_suffix(".jsonl.tmp")
    write_jsonl(temp, list(merged.values()))
    temp.replace(destination)
    return len(incoming)


def adjudicate_disagreements(
    differences: list[dict],
    cards: list[Card],
    reviews: list[dict],
) -> list[dict]:
    """Record human decisions without changing the historical blind comparison."""
    by_id = {c.card_ref: c for c in cards}
    approved = {r.card_ref: r for r in map(HumanReview.model_validate, reviews)}
    result = []
    for original in differences:
        row = dict(original)
        card = by_id.get(row["card_ref"])
        review = approved.get(row["card_ref"])
        field = row["field"]
        if (
            card is not None
            and review is not None
            and review.source_sha256 == card.source_sha256
            and field.split(".")[0] in review.confirmed_fields
        ):
            value = card.model_dump()
            for part in field.split("."):
                value = value[part]
            row.update(
                status="resolved_by_human",
                resolution=value,
                reviewer=review.reviewer,
                reviewed_at=review.reviewed_at,
                resolution_note=review.note,
            )
        else:
            row.update(status="awaiting_human", resolution=None)
            for key in ("reviewer", "reviewed_at", "resolution_note"):
                row.pop(key, None)
        result.append(row)
    return result


def build_review(root: Path, output: Path) -> None:
    cards = load_cards(root)
    base = {c.card_ref: c.model_dump() for c in load_cards(root, apply_human=False)}
    vocab = json.loads((root / "knowledge/catalog/vocabulary.yaml").read_text())
    disagreements = read_jsonl(root / "knowledge/catalog/disagreements.jsonl")
    pieces = []
    payload = []
    for c in cards:
        if c.position == 1:
            if pieces:
                pieces.append("</div></section>")
            pieces.append(
                f'<section><h2>{c.set_index:02}. {html.escape(c.set_theme)}</h2><div class="cards">'
            )
        im = Image.open(root / f"dataset/{c.card_ref}.png").convert("RGB")
        im.thumbnail((215, 240))
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=87)
        uri = "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
        data = c.model_dump()
        labels = {k: data[k] for k in LABEL_FIELDS}
        payload.append(
            {
                "card_ref": c.card_ref,
                "source_sha256": c.source_sha256,
                "labels": {k: base[c.card_ref][k] for k in LABEL_FIELDS},
            }
        )
        disputes = [d for d in disagreements if d["card_ref"] == c.card_ref]
        detail = "\n".join(f"{d['field']}: {d['first_pass']} ↔ {d['crosscheck']}" for d in disputes)
        selected = c.position in [2, 4, 6, 8]
        flag = "Проверить ВСЕ поля: выборка 40%" if selected else "Проверить категорию и тему"
        labels_html = html.escape(json.dumps(labels, ensure_ascii=False, indent=2))
        dispute_html = (
            (
                "<details open><summary>Расхождения</summary><pre>"
                + html.escape(detail)
                + "</pre></details>"
            )
            if detail
            else ""
        )
        pieces.append(f'''<article data-id="{c.card_ref}">
<img src="{uri}" alt="{html.escape(c.object_name)}" width="215" height="240">
<h3>{c.card_ref.rsplit("_", 1)[1]} · {html.escape(c.object_name)}</h3>
<p class="tag">Категория {c.category} · {html.escape(c.background.color)}
 · маска: {c.measured.mask_quality}</p>
<p>{html.escape(" ".join(c.notes))}</p>
<p class="flag">{flag}</p>
<label><input class="confirm" type="checkbox"> Категорию и тему проверил(а)</label>
<label><input class="all" type="checkbox"> Все смысловые поля проверил(а)</label>
<details><summary>Метки и правки</summary>
<textarea spellcheck="false">{labels_html}</textarea></details>
{dispute_html}
<textarea class="note" placeholder="Комментарий проверяющего" rows="2"></textarea>
</article>''')
    pieces.append("</div></section>")
    data_json = json.dumps(payload, ensure_ascii=False).replace("<", "\\u003c")
    definitions = "".join(
        f"<li><b>{k}</b> — {html.escape(v)}</li>" for k, v in vocab["category"].items()
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    template = Path(__file__).with_name("review_template.html").read_text()
    rendered = template.replace("{{DEFINITIONS}}", definitions)
    rendered = rendered.replace("{{CARDS}}", "".join(pieces)).replace("{{DATA}}", data_json)
    output.write_text(rendered, encoding="utf-8")
