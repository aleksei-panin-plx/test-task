"""Exercise real HTTP handlers with in-memory sockets: no external service or labels."""

import io
import json
from pathlib import Path

import pytest

from cardforge.evaluate import human_review
from cardforge.evaluate.common import sha256


def test_separate_campaign_does_not_append_old_smokes_or_create_labels(tmp_path):
    from PIL import Image

    from cardforge.evaluate.common import dump

    image = tmp_path / "runs/new/image.png"
    image.parent.mkdir(parents=True)
    Image.new("RGB", (430, 480), "blue").save(image)
    manifest = tmp_path / "runs/new/manifest.json"
    dump(
        manifest,
        {
            "items": [
                {
                    "item_id": "new-001",
                    "image": "runs/new/image.png",
                    "image_sha256": sha256(image),
                    "cohort": "borderline",
                    "spec": {"object_name": "new", "category": 2},
                }
            ]
        },
    )
    result = human_review.prepare(tmp_path, tmp_path / "runs/new-review", manifest)
    assert len(result["items"]) == 1 and result["review_only"]
    assert result["items"][0]["priority"]
    assert not (tmp_path / "runs/new-review/ratings.json").exists()
    assert not (tmp_path / "calibration/labels.jsonl").exists()


def test_expanding_main_manifest_preserves_existing_review_ids(tmp_path):
    from PIL import Image

    from cardforge.evaluate.common import dump

    image = tmp_path / "dataset/reference.png"
    image.parent.mkdir()
    Image.new("RGB", (430, 480), "blue").save(image)
    for profile in ("local-klein4b", "local-zimage-turbo"):
        smoke = tmp_path / f"runs/stage02/02-existing-target-smokes/{profile}/image.png"
        smoke.parent.mkdir(parents=True)
        smoke.write_bytes(image.read_bytes())
    item = {
        "item_id": "old-reference",
        "image": "dataset/reference.png",
        "image_sha256": sha256(image),
        "cohort": "real",
        "spec": {"category": 2},
    }
    manifest = tmp_path / "calibration/manifest.json"
    dump(manifest, {"items": [item]})
    folder = tmp_path / "runs/old-session"
    original = human_review.prepare(tmp_path, folder)
    dump(manifest, {"items": [item, {**item, "item_id": "new-candidate", "cohort": "borderline"}]})
    expanded = human_review.prepare(tmp_path, folder)
    old_ids = {r["item_id"]: r["review_id"] for r in original["items"]}
    assert all(
        r["review_id"] == old_ids[r["item_id"]]
        for r in expanded["items"]
        if r["item_id"] in old_ids
    )
    assert expanded["items"][-1]["item_id"] == "new-candidate"


@pytest.fixture
def review(tmp_path, monkeypatch):
    source = tmp_path / "dataset/image.png"
    source.parent.mkdir()
    source.write_bytes(b"image fixture")
    folder = tmp_path / "runs/review"
    folder.mkdir(parents=True)
    item = {
        "review_id": "review-001",
        "image_sha256": sha256(source),
        "image": "dataset/image.png",
        "thumbnail": str(source),
        "item_id": "secret-source-id",
        "cohort": "real",
        "priority": True,
        "generated": False,
        "spec": {"object_name": "fixture", "category": 2, "set_theme": "fixture"},
    }
    monkeypatch.setattr(human_review, "prepare", lambda *_: {"items": [item]})

    class Server:
        def __init__(self, address, handler):
            self.server_port = address[1]
            self.handler = handler

    monkeypatch.setattr(human_review, "ThreadingHTTPServer", Server)
    server = human_review.make_server(tmp_path, folder)

    def request(path, data=None, token=None):
        body = json.dumps(data).encode() if data is not None else b""
        method = "POST" if data is not None else "GET"
        headers = (
            f"{method} {path} HTTP/1.0\r\nHost: 127.0.0.1:8766\r\nContent-Length: {len(body)}\r\n"
        )
        if token:
            headers += f"X-Review-Token: {token}\r\n"

        class Socket:
            output = b""

            def makefile(self, *_args):
                return io.BytesIO(headers.encode() + b"\r\n" + body)

            def sendall(self, value):
                self.output += value

        sock = Socket()
        server.handler(sock, ("127.0.0.1", 12345), server)
        head, response = sock.output.split(b"\r\n\r\n", 1)
        return int(head.split()[1]), response

    return tmp_path, folder, item, request


def test_no_labels_or_model_answers_exposed_by_default(review):
    _root, folder, _item, request = review
    status, body = request("/api/session")
    data = json.loads(body)
    assert status == 200 and data["ratings"] == {}
    assert set(data["items"][0]) == {"review_id", "image_sha256", "spec", "priority", "generated"}
    assert b"secret-source-id" not in body and b"cohort" not in body
    assert not (folder / "ratings.json").exists()
    assert request("/.env")[0] == 404
    assert request("/asset/../.env/full")[0] == 404


def test_explicit_human_decision_requires_token_and_preserves_history(review):
    root, folder, item, request = review
    row = {k: item[k] for k in ("review_id", "image_sha256")}
    row.update(reviewer="Test human", decision="uncertain", note="Needs discussion")
    assert request("/api/review", row)[0] == 403
    token = json.loads(request("/api/session")[1])["token"]
    status, body = request("/api/review", row, token)
    assert status == 200 and json.loads(body)["accepted"] is None
    row["decision"] = "reject"
    assert request("/api/review", row, token)[0] == 200
    saved = json.loads((folder / "ratings.json").read_text())[item["review_id"]]
    assert saved["accepted"] is False and saved["source"] == "human"
    assert len(list((folder / "history").glob("*.json"))) == 2
    assert not (root / "calibration/labels.jsonl").exists()
    exported = json.loads(request("/api/export")[1])
    assert exported == saved


def test_stale_image_and_incomplete_decisions_cannot_be_saved(review):
    root, folder, item, request = review
    token = json.loads(request("/api/session")[1])["token"]
    row = {k: item[k] for k in ("review_id", "image_sha256")}
    row.update(reviewer="", decision="accept", note="")
    assert request("/api/review", row, token)[0] == 400
    row["reviewer"] = "Test human"
    row["decision"] = ""
    assert request("/api/review", row, token)[0] == 400
    row["decision"] = "accept"
    Path(root / item["image"]).write_bytes(b"changed image")
    assert request("/api/review", row, token)[0] == 400
    assert not (folder / "ratings.json").exists()
