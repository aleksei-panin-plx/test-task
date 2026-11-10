"""Loopback-only image review, with explicit human decisions and no model judgements."""

from __future__ import annotations

import json
import secrets
import threading
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from PIL import Image

from cardforge.evaluate.common import dump, private_path, sha256
from cardforge.evaluate.judge_pack import load_manifest


def prepare(root: Path, folder: Path, manifest_path: Path | None = None) -> dict:
    items = load_manifest(root, manifest_path or root / "calibration/manifest.json")
    for profile in () if manifest_path else ("local-klein4b", "local-zimage-turbo"):
        image = root / f"runs/stage02/02-existing-target-smokes/{profile}/image.png"
        items.append(
            {
                "item_id": f"existing-smoke-{profile}",
                "image": str(image.relative_to(root)),
                "image_sha256": sha256(image),
                "cohort": "candidate",
                "spec": {
                    "object_name": "Латунный кинопрожектор",
                    "category": 2,
                    "set_theme": "Кино",
                    "must_have": [],
                },
            }
        )
    urgent = {"foreign-036", "existing-smoke-local-klein4b", "existing-smoke-local-zimage-turbo"}
    # Keep IDs of existing human decisions stable when the main campaign expands.
    previous = folder / "session.json"
    if manifest_path is None and previous.exists():
        order = {r["item_id"]: i for i, r in enumerate(json.loads(previous.read_text())["items"])}
        items.sort(key=lambda row: order.get(row["item_id"], len(order)))
    public = []
    assets = folder / "thumbnails"
    assets.mkdir(parents=True, exist_ok=True)
    for i, item in enumerate(items, 1):
        identifier = f"review-{i:03}"
        source = private_path(root, item["image"])
        storage = (
            root / "dataset/analysis/evaluation/human-review-assets"
            if item["cohort"] == "real"
            else assets
        )
        storage.mkdir(parents=True, exist_ok=True)
        thumb = storage / f"{item['image_sha256']}.png"
        if not thumb.exists():
            with Image.open(source) as image:
                image.convert("RGB").resize((215, 240), Image.Resampling.LANCZOS).save(thumb)
        public.append(
            {
                **item,
                "review_id": identifier,
                "priority": bool(item.get("review_priority", manifest_path is not None))
                or item["item_id"] in urgent,
                "generated": item["cohort"] != "real",
                "thumbnail": str(thumb.resolve()),
            }
        )
    manifest = {
        "version": 1,
        "items": public,
        "review_only": manifest_path is not None,
        "notice": (
            f"Новая очередь: {len(public)} попыток целевого стиля. "
            "Оцените готовность картинки для игрового набора, включая стиль, объект, "
            "категорию, композицию и артефакты. Прошлые оценки и метрики скрыты. "
            "Предыдущие три решения сохранены отдельно; повторять их не требуется."
            if manifest_path
            else f"В этой исходной выборке {len(public)} изображений; приоритетных — 3. "
            "Название модели, прошлые оценки и метрики скрыты. "
            "Решения сохраняются локально после нажатия кнопки."
        ),
    }
    dump(folder / "session.json", manifest)
    return manifest


def validate_decision(row: dict, item: dict) -> dict:
    if not isinstance(row, dict) or set(row) != {
        "review_id",
        "image_sha256",
        "reviewer",
        "decision",
        "note",
    }:
        raise ValueError("unexpected review fields")
    if row["review_id"] != item["review_id"] or row["image_sha256"] != item["image_sha256"]:
        raise ValueError("stale or unknown image")
    if row["decision"] not in ("accept", "reject", "uncertain"):
        raise ValueError("explicit human decision required")
    if (
        not isinstance(row["reviewer"], str)
        or not row["reviewer"].strip()
        or len(row["reviewer"]) > 128
    ):
        raise ValueError("reviewer name required")
    if not isinstance(row["note"], str) or len(row["note"]) > 10000:
        raise ValueError("invalid note")
    return {
        **row,
        "item_id": item["item_id"],
        "source": "human",
        "accepted": {"accept": True, "reject": False, "uncertain": None}[row["decision"]],
        "reviewed_at": datetime.now(UTC).isoformat(),
    }


def make_server(
    root: Path, folder: Path, port: int = 8766, *, manifest_path: Path | None = None
) -> ThreadingHTTPServer:
    manifest = prepare(root, folder, manifest_path) if manifest_path else prepare(root, folder)
    by_id = {r["review_id"]: r for r in manifest["items"]}
    token = secrets.token_urlsafe(24)
    ratings_path = folder / "ratings.json"
    ratings = json.loads(ratings_path.read_text()) if ratings_path.exists() else {}
    for identifier, rating in ratings.items():
        if (
            identifier not in by_id
            or rating["image_sha256"] != by_id[identifier]["image_sha256"]
            or rating["item_id"] != by_id[identifier]["item_id"]
        ):
            raise ValueError("review session changed; use a new review directory")
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def send(self, body: bytes, kind: str = "application/json", status: int = 200):
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def valid_host(self):
            return self.headers.get("Host") in (
                f"127.0.0.1:{self.server.server_port}",
                f"localhost:{self.server.server_port}",
            )

        def do_GET(self):
            if not self.valid_host():
                return self.send(b"{}", status=403)
            path = urlparse(self.path).path
            if path == "/":
                return self.send(
                    Path(__file__).with_name("human_review.html").read_bytes(),
                    "text/html; charset=utf-8",
                )
            if path == "/api/session":
                fields = ("review_id", "image_sha256", "spec", "priority", "generated")
                with lock:
                    data = {
                        "token": token,
                        "ratings": dict(ratings),
                        "items": [{k: r[k] for k in fields} for r in manifest["items"]],
                        "notice": manifest.get("notice", ""),
                        "review_only": manifest.get("review_only", False),
                    }
                return self.send(json.dumps(data, ensure_ascii=False).encode())
            if path == "/api/export":
                with lock:
                    body = "".join(
                        json.dumps(r, ensure_ascii=False) + "\n" for r in ratings.values()
                    )
                return self.send(body.encode(), "application/x-ndjson; charset=utf-8")
            if path.startswith("/asset/"):
                parts = path.split("/")
                if len(parts) != 4 or parts[2] not in by_id or parts[3] not in ("full", "small"):
                    return self.send(b"{}", status=404)
                item = by_id[parts[2]]
                image = (
                    private_path(root, item["image"])
                    if parts[3] == "full"
                    else Path(item["thumbnail"])
                )
                return self.send(image.read_bytes(), "image/png")
            if path in ("/ref/17", "/ref/45", "/ref/58", "/ref/91"):
                name = path.rsplit("/", 1)[1]
                return self.send(
                    (root / f"dataset/CardC6_card_{name}.png").read_bytes(), "image/png"
                )
            return self.send(b"{}", status=404)

        def do_POST(self):
            if (
                not self.valid_host()
                or self.path != "/api/review"
                or self.headers.get("X-Review-Token") != token
            ):
                return self.send(b"{}", status=403)
            origin = self.headers.get("Origin")
            if origin and origin not in (
                f"http://127.0.0.1:{self.server.server_port}",
                f"http://localhost:{self.server.server_port}",
            ):
                return self.send(b"{}", status=403)
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 20000:
                    raise ValueError("invalid request size")
                row = json.loads(self.rfile.read(length))
                if not isinstance(row, dict):
                    raise ValueError("review must be an object")
                item = by_id[row["review_id"]]
                if sha256(private_path(root, item["image"])) != item["image_sha256"]:
                    raise ValueError("image changed; restart review")
                result = validate_decision(row, item)
                # Preserve every explicit decision before updating the convenient latest view.
                history = folder / "history"
                history.mkdir(exist_ok=True)
                with lock:
                    dump(history / f"{secrets.token_hex(12)}.json", result)
                    updated = {**ratings, item["review_id"]: result}
                    temporary = ratings_path.with_suffix(".tmp")
                    dump(temporary, updated)
                    temporary.replace(ratings_path)
                    ratings.update(updated)
                return self.send(json.dumps(result, ensure_ascii=False).encode())
            except (ValueError, KeyError, TypeError) as exc:
                return self.send(json.dumps({"error": str(exc)}).encode(), status=400)

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)
