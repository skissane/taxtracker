FROM ghcr.io/astral-sh/uv:0.10.10 AS uv

FROM python:3.14-slim AS builder

COPY --from=uv /uv /uvx /bin/

WORKDIR /app

COPY pyproject.toml uv.lock manage.py README.md ./
COPY src ./src

RUN uv sync --locked --no-dev

ENV LIFETRACKER_STATIC_ROOT=/app/staticfiles

# Throwaway secret key: settings.py needs one to import, but collectstatic
# doesn't use it, and it mustn't be baked into the image.
RUN LIFETRACKER_SECRET_KEY_FILE=/tmp/secret_key \
    .venv/bin/python manage.py collectstatic --noinput \
    && rm /tmp/secret_key

FROM python:3.14-slim

# Links the image CI pushes to GHCR to this repository.
LABEL org.opencontainers.image.source="https://github.com/skissane/taxtracker"

ARG LIFETRACKER_COMMIT_ID=""

WORKDIR /app

ENV PATH="/app/.venv/bin:${PATH}"
ENV PYTHONUNBUFFERED=1
ENV LIFETRACKER_STATIC_ROOT=/app/staticfiles
ENV LIFETRACKER_COMMIT_ID="${LIFETRACKER_COMMIT_ID}"
# Keep the database and secret key on a volume so they survive container
# restarts and image rebuilds.
ENV LIFETRACKER_DATA_DIR=/data
# Production-safe by default: deployments must set LIFETRACKER_ALLOWED_HOSTS
# (settings.py refuses to start without it). `just docker-run` sets
# LIFETRACKER_DEBUG=1 for local use instead.
ENV LIFETRACKER_DEBUG=0

# The lifetracker user/group the app runs as. Created with these IDs at build
# time; set LIFETRACKER_UID/LIFETRACKER_GID at runtime (e.g. to match the owner
# of a bind-mounted directory) and docker-entrypoint.sh changes them to match
# before dropping root. The defaults are above 10000 to stay clear of typical
# host user/group IDs. dialout is deleted because its GID, 20, is macOS's
# staff group: nothing in the image uses it, and with it gone a lifetracker
# group renumbered to 20 shows by name instead of as dialout.
ENV LIFETRACKER_UID=10001
ENV LIFETRACKER_GID=10001
RUN groupdel dialout \
    && groupadd --system --gid "${LIFETRACKER_GID}" lifetracker \
    && useradd --system --uid "${LIFETRACKER_UID}" --gid lifetracker \
       --create-home --home-dir /home/lifetracker --shell /usr/sbin/nologin \
       lifetracker

COPY --from=builder /app /app
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

RUN chmod +x /usr/local/bin/docker-entrypoint.sh

RUN mkdir /data && chown lifetracker:lifetracker /data
VOLUME /data

EXPOSE 8000

ENTRYPOINT ["docker-entrypoint.sh"]
