FROM python:3.12-slim

# ffmpeg/ffprobe are required by the downloader (remux + transcode).
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

ENV UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# Dependency layer: only invalidated when the manifests change.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Source layer, then install the project itself.
COPY tgdl ./tgdl
RUN uv sync --frozen --no-dev

# Non-root runtime. `data` is the volume mount point for the SQLite DB + workdirs.
RUN useradd --create-home --uid 10001 tgdl \
    && mkdir -p /app/data \
    && chown -R tgdl:tgdl /app
USER tgdl

VOLUME ["/app/data"]

ENTRYPOINT ["uv", "run", "--no-sync", "tgdl-bot"]
