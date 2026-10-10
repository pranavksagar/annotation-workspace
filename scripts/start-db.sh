#!/bin/sh
# Local PostgreSQL 18 in Docker on port 55432 (same major version as production and integration tests).
set -eu
if docker ps -a --format '{{.Names}}' | grep -qx annotation-workspace-pg; then
  docker start annotation-workspace-pg >/dev/null
else
  docker run -d --name annotation-workspace-pg -e POSTGRES_USER=annotation -e POSTGRES_PASSWORD=annotation \
    -e POSTGRES_DB=annotation -p 55432:5432 -v annotation-workspace-pg:/var/lib/postgresql postgres:18 >/dev/null
fi
echo "PostgreSQL: postgresql://annotation:annotation@127.0.0.1:55432/annotation"
