# syntax=docker/dockerfile:1

# 3.13 exactly: pyrainbird 6.4+ needs it, and on an older Python pip quietly
# installs 6.0.2, which speaks only HTTP and can't reach the LNK2 (HTTPS only).
FROM python:3.13-slim AS base
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
# The system zone database, so TZ (set in docker-compose.yml) gives log lines in
# Phoenix time rather than UTC. Plan times never depend on it: they use the
# service's own clock, set from RAINBIRD_TZ.
RUN apt-get update && apt-get install -y --no-install-recommends tzdata && rm -rf /var/lib/apt/lists/*
COPY requirements.txt ./
# tzdata: zoneinfo's fallback, so the plan time zone never depends on whether
# the slim image happens to ship /usr/share/zoneinfo.
RUN pip install -r requirements.txt tzdata

# ---- tests ------------------------------------------------------------------
# Whole runs against the simulated controller. The runtime stage copies the
# marker, so BuildKit can't skip this stage and a failing test fails the build.
FROM base AS test
COPY rbservice ./rbservice
COPY tests ./tests
RUN python -m pytest -q -p no:cacheprovider tests/ && touch /tests-passed

# ---- runtime ----------------------------------------------------------------
FROM base AS runtime
COPY --from=test /tests-passed /tests-passed
COPY rbservice ./rbservice
# uid 1000 is scott on the NUC, who owns ~/rainbird/data. Any other uid writes
# the WAL and shm files as a stranger, and the host-side backup can't read them.
RUN useradd --uid 1000 --no-create-home --shell /usr/sbin/nologin rainbird \
    && mkdir -p /data && chown rainbird:rainbird /data
USER rainbird
CMD ["python", "-m", "rbservice"]
