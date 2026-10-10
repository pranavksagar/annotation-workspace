#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
exec npx vite --host 127.0.0.1
