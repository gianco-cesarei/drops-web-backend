
# Official, upstream-maintained image for the free PO token HTTP server
# (bgutil-ytdlp-pot-provider) - the compiled server + its own node_modules.
# We only copy files out of it below; nothing from this stage runs directly.
FROM brainicism/bgutil-ytdlp-pot-provider:latest AS bgutil

# Node runtime, taken from a Debian/glibc image compatible with python:3.12-slim
# below - NOT from the bgutil image above, whose base isn't guaranteed
# glibc-compatible with python:3.12-slim (a node binary from an incompatible
# base fails to exec at all, degrading straight to "PO token disabled" with
# no build-time signal). node:22-slim is bookworm-based, matching python:3.12-slim.
FROM node:22-slim AS nodert

FROM python:3.12-slim AS api

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000 \
    DROPS_WEB_STATE_DIR=/data \
    DROPS_YTDLP_BGUTIL_HTTP_BASE_URL=http://127.0.0.1:4416

RUN apt-get update \
    && apt-get install --no-install-recommends -y ca-certificates ffmpeg \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system drops \
    && useradd --system --gid drops --home-dir /app drops \
    && mkdir -p /app /data \
    && chown -R drops:drops /app /data

# Node runtime + the bgutil HTTP server itself (free PO token provider,
# helps yt-dlp dodge YouTube's bot-check - see run_web.py, which starts this
# as a background process, and media_core.py, which wires yt-dlp to it once
# it's confirmed up). The compiled server (JS + its node_modules) comes from
# the upstream bgutil image; the node binary that runs it comes from a
# glibc-compatible stage instead (see the nodert stage above).
COPY --from=nodert /usr/local/bin/node /usr/local/bin/node
COPY --from=bgutil --chown=drops:drops /app /opt/bgutil-server

# Build fails loudly here if node can't run on this base, instead of the
# app silently degrading to "PO token disabled" at container startup.
RUN node --version

WORKDIR /app

COPY backend/requirements-web.txt backend/requirements-web.txt
RUN pip install --no-cache-dir -r backend/requirements-web.txt

COPY --chown=drops:drops \
    backend/discogs_agent.py \
    backend/bpm_analyzer.py \
    backend/bpm_jobs.py \
    backend/download_engine.py \
    backend/media_core.py \
    backend/run_web.py \
    backend/spotify_agent.py \
    backend/web_app.py \
    backend/web_settings.py \
    backend/web_store.py \
    backend/

# Fail image build if runtime import graph is incomplete. Backend runs with
# /app/backend on sys.path because run_web.py is executed as backend/run_web.py.
RUN python -c "import sys; sys.path.insert(0, '/app/backend'); import web_app; import spotify_agent; import discogs_agent"

USER drops

EXPOSE 8000
VOLUME ["/data"]

HEALTHCHECK --interval=10s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('PORT', '8000') + '/health', timeout=3).read()"

CMD ["python", "backend/run_web.py"]
