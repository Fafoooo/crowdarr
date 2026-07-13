# syntax=docker/dockerfile:1.7

ARG NODE_VERSION=22.17.0
ARG PYTHON_VERSION=3.12.11

FROM node:${NODE_VERSION}-bookworm-slim AS frontend-builder
WORKDIR /build/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN --mount=type=cache,target=/root/.npm npm ci
COPY frontend/ ./
RUN npm run build

FROM python:${PYTHON_VERSION}-slim-bookworm AS python-builder
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    VIRTUAL_ENV=/opt/venv
RUN python -m venv "${VIRTUAL_ENV}"
ENV PATH="${VIRTUAL_ENV}/bin:${PATH}"
WORKDIR /build
COPY pyproject.toml README.md ./
COPY backend/ ./backend/
RUN --mount=type=cache,target=/root/.cache/pip pip install .

FROM python:${PYTHON_VERSION}-slim-bookworm AS runtime
LABEL org.opencontainers.image.title="crowdarr" \
      org.opencontainers.image.description="Self-hosted CrowdNFO companion for download clients and media libraries" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:${PATH}" \
    PUID=1000 \
    PGID=1000 \
    UMASK=0022 \
    TZ=Etc/UTC

RUN DEBIAN_FRONTEND=noninteractive apt-get update \
    && apt-get install --no-install-recommends -y ca-certificates gosu mediainfo tini tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 1000 crowdarr \
    && useradd --uid 1000 --gid crowdarr --home-dir /config --no-create-home --shell /usr/sbin/nologin crowdarr \
    && mkdir -p /app/frontend/dist /config \
    && chown -R crowdarr:crowdarr /config

WORKDIR /app
COPY --from=python-builder /opt/venv /opt/venv
COPY --from=python-builder /build/backend ./backend
COPY --from=frontend-builder /build/frontend/dist ./frontend/dist
COPY --chmod=0755 docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

EXPOSE 8000
STOPSIGNAL SIGTERM
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import os, urllib.request; p=os.environ.get('CROWDARR_PORT', os.environ.get('CROWDARRR_PORT', '8000')); urllib.request.urlopen(f'http://127.0.0.1:{p}/api/health', timeout=3).read()"]

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/docker-entrypoint.sh"]
CMD ["serve"]
