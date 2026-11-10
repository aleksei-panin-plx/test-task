# CardForge server (folder 2): Linux, CPU-only torch, RapidOCR (Apple Vision is macOS only).
# Code and the core copy are in the image; customer data, weights and runs are volumes
# (docker-compose.yml), never baked in. No GPU, no deployment target is implied.
FROM python:3.12.13-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_PYTHON_DOWNLOADS=never \
    PATH=/opt/venv/bin:$PATH

# DejaVu: Cyrillic captions on the 5×2 sheets (cardforge/export/sheets.py font list);
# libglib for opencv-headless / onnxruntime.
RUN apt-get update \
 && apt-get install -y --no-install-recommends fonts-dejavu-core libglib2.0-0 \
 && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.13.6 /uv /usr/local/bin/uv

WORKDIR /srv/cardforge
# Dependencies first (cache layer), exactly as uv.lock pins them: torch from the CPU index
# (pyproject [tool.uv.sources]), rapidocr-onnxruntime, rembg[cpu].
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-install-project --no-dev --extra analysis --extra evaluation --extra api

COPY . .
RUN uv sync --frozen --no-dev --extra analysis --extra evaluation --extra api \
 && mkdir -p /srv/cardforge-image && cp -R models /srv/cardforge-image/models \
 && chmod +x server/entrypoint.sh \
 && useradd --uid 10001 --create-home cardforge \
 && mkdir -p /srv/cardforge/runs /srv/cardforge/models \
 && chown -R cardforge /srv/cardforge

USER cardforge
ENV CARDFORGE_HOME=/srv/cardforge \
    CARDFORGE_RUNS_DIR=/srv/cardforge/runs \
    CARDFORGE_SERVER_HOST=0.0.0.0 \
    CARDFORGE_SERVER_PORT=8006
EXPOSE 8006
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8006/health', timeout=4)"
ENTRYPOINT ["/srv/cardforge/server/entrypoint.sh"]
CMD ["python", "-m", "server.app"]
