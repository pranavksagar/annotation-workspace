#!/bin/sh
# Deployment step: apply schema migrations. The API never migrates at startup.
set -eu
cd "$(dirname "$0")/.."
exec .venv/bin/alembic upgrade head
