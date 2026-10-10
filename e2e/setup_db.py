"""Create a fresh E2E database and migrate it (runs before the E2E API starts)."""
import os
import subprocess
import sys

import psycopg

url = os.environ["DATABASE_URL"]
name = url.rsplit("/", 1)[1]
with psycopg.connect(url.rsplit("/", 1)[0] + "/annotation", autocommit=True) as conn:
    conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    conn.execute(f'CREATE DATABASE "{name}"')
subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], check=True)
