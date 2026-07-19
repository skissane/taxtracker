FROM ghcr.io/astral-sh/uv:0.10.10 AS uv

FROM python:3.14-slim AS builder

COPY --from=uv /uv /uvx /bin/

WORKDIR /app

COPY pyproject.toml uv.lock manage.py README.md ./
COPY src ./src

RUN uv sync --locked --no-dev

FROM python:3.14-slim

WORKDIR /app

ENV PATH="/app/.venv/bin:${PATH}"
ENV PYTHONUNBUFFERED=1

COPY --from=builder /app /app
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

RUN chmod +x /usr/local/bin/docker-entrypoint.sh

EXPOSE 8000

ENTRYPOINT ["docker-entrypoint.sh"]
