# syntax=docker/dockerfile:1
# One image runs everything: migrations, the preparation worker and the API, which also serves the built frontend.
# Same image for `docker compose up` and for Render (deploy/start.sh is the entrypoint).

FROM node:22-bookworm-slim AS frontend
WORKDIR /src
RUN corepack enable
COPY package.json pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile
COPY tsconfig.json tsconfig.app.json vite.config.ts ./
COPY apps/review ./apps/review
RUN pnpm run build

FROM python:3.12-slim-bookworm AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 FRONTEND_DIST=/app/dist/review
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY alembic.ini pyproject.toml ./
COPY apps ./apps
COPY packages ./packages
COPY workers ./workers
COPY migrations ./migrations
COPY --from=frontend /src/dist/review ./dist/review
COPY --chmod=755 deploy/start.sh ./deploy/start.sh
RUN useradd --system --uid 10001 app && chown -R app /app
USER app
EXPOSE 8000
ENTRYPOINT ["/app/deploy/start.sh"]
