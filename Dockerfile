FROM python:3.12-slim

# ffmpeg/ffprobe are required by the downloader (remux + transcode).
# gosu lets the entrypoint fix volume ownership as root, then drop privileges.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates gosu \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# deno is yt-dlp's JavaScript runtime for YouTube's signature / proof-of-origin
# challenges. Without one, yt-dlp 2026+ deprecates YouTube extraction, formats go
# missing, and datacenter IPs escalate straight to "confirm you're not a bot".
COPY --from=denoland/deno:bin /deno /usr/local/bin/deno

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
# The container starts as root only so the entrypoint can chown mounted volumes
# (hosted platforms create them root-owned), then drops to tgdl via gosu.
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN useradd --create-home --uid 10001 tgdl \
    && mkdir -p /app/data \
    && chown -R tgdl:tgdl /app \
    && chmod +x /usr/local/bin/entrypoint.sh

VOLUME ["/app/data"]

# Liveness: getMe against Telegram confirms token + network are working.
HEALTHCHECK --interval=60s --timeout=15s --start-period=20s --retries=3 \
    CMD ["/app/.venv/bin/python", "-m", "tgdl.main", "--healthcheck"]

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["uv", "run", "--no-sync", "tgdl-bot"]
