#!/bin/sh
# Container entrypoint: migrate, then run the preparation worker and the API (which serves the frontend).
set -eu

# On Render, derive the origin and host checks from the platform URL unless they were set explicitly.
if [ -n "${RENDER_EXTERNAL_URL:-}" ]; then
  : "${APP_PUBLIC_ORIGIN:=$RENDER_EXTERNAL_URL}"
  : "${TRUSTED_HOSTS:=${RENDER_EXTERNAL_HOSTNAME:-}}"
  export APP_PUBLIC_ORIGIN TRUSTED_HOSTS
fi

alembic upgrade head   # a deployment step: it finishes before the API accepts traffic

python -m workers.annotation_worker.main &
WORKER=$!
uvicorn apps.api.app.annotation_main:app --host 0.0.0.0 --port "${PORT:-8000}" \
  --proxy-headers --forwarded-allow-ips '*' &
API=$!

# Forward SIGTERM/SIGINT to both. The worker finishes or abandons its job; an abandoned job lease expires and is re-claimed.
trap 'kill -TERM "$API" "$WORKER" 2>/dev/null || true' TERM INT

# Exit as soon as either process dies so the platform restarts the container.
while kill -0 "$API" 2>/dev/null && kill -0 "$WORKER" 2>/dev/null; do sleep 1; done
kill -TERM "$API" "$WORKER" 2>/dev/null || true
wait "$API" 2>/dev/null; API_STATUS=$?
wait "$WORKER" 2>/dev/null || true
exit "$API_STATUS"
