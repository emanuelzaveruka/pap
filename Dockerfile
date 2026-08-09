# Multi-stage: wheels are built with a compiler present, the runtime image has none.
#
# Playwright is deliberately NOT here. It adds ~1.5 GB and may prove unnecessary —
# Phase 1 discovery decides whether Studeo needs a browser at all. If it does, that
# goes in Dockerfile.browser and is used only by the jobs that require it.
FROM python:3.12-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY requirements.txt ./
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --upgrade pip \
 && /opt/venv/bin/pip install -r requirements.txt

COPY pyproject.toml ./
COPY src ./src
RUN /opt/venv/bin/pip install --no-deps .


FROM python:3.12-slim AS runtime

# postgresql-client provides pg_dump and psql, which `pap backup db` and
# `pap backup verify` shell out to. Without them backups fail at runtime with a
# confusing FileNotFoundError rather than at build time.
RUN apt-get update \
 && apt-get install -y --no-install-recommends postgresql-client ca-certificates tini \
 && rm -rf /var/lib/apt/lists/*

# Non-root: the container writes only to /var/lib/pap, which is a mounted volume.
RUN useradd --system --create-home --uid 10001 pap

COPY --from=builder /opt/venv /opt/venv
WORKDIR /app
COPY migrations ./migrations
COPY patterns ./patterns

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PAP_MIGRATIONS_DIR=/app/migrations \
    LOG_DIR=/var/lib/pap/logs \
    PAP_STATE_DIR=/var/lib/pap/secrets

# Stamped into every GlitchTip event so a regression can be traced to a release.
ARG PAP_RELEASE=dev
ENV PAP_RELEASE=${PAP_RELEASE}

RUN mkdir -p /var/lib/pap/logs /var/lib/pap/secrets /var/lib/pap/backups \
 && chown -R pap:pap /var/lib/pap \
 && chmod 700 /var/lib/pap/secrets

USER pap

# tini reaps the pg_dump/psql subprocesses backups spawn; without an init, a
# `docker compose run` container leaves zombies behind.
ENTRYPOINT ["/usr/bin/tini", "--", "pap"]
CMD ["doctor"]
