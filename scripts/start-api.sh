#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
exec .venv/bin/uvicorn apps.api.app.annotation_main:app --host 127.0.0.1 --port "${API_PORT:-8010}"
