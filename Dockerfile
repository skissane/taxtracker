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

WORKDIR /app

ENV PATH="/app/.venv/bin:${PATH}"
ENV PYTHONUNBUFFERED=1
ENV LIFETRACKER_STATIC_ROOT=/app/staticfiles
# Keep the database and secret key on a volume so they survive container
# restarts and image rebuilds.
ENV LIFETRACKER_DB_PATH=/data/db.sqlite3
ENV LIFETRACKER_SECRET_KEY_FILE=/data/secret_key

COPY --from=builder /app /app
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

RUN chmod +x /usr/local/bin/docker-entrypoint.sh

VOLUME /data

EXPOSE 8000

ENTRYPOINT ["docker-entrypoint.sh"]
