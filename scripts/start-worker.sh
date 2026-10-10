#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
exec .venv/bin/python -m workers.annotation_worker.main
