"""Immutable, deduplicated binary assets stored in PostgreSQL BYTEA."""
from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from sqlalchemy import Connection, text


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def insert_asset(conn: Connection, kind: str, mime_type: str, data: bytes,
                 metadata: dict[str, Any] | None = None, digest: str | None = None) -> str:
    """Insert or reuse an asset with the same (kind, sha256). Returns its id."""
    digest = digest or sha256_hex(data)
    new_id = str(uuid.uuid4())
    row = conn.execute(text(
        "INSERT INTO annotation.assets (id, kind, mime_type, byte_count, sha256, bytes, metadata) "
        "VALUES (:id, :kind, :mime, :count, :sha, :bytes, CAST(:meta AS jsonb)) "
        "ON CONFLICT (kind, sha256) DO NOTHING RETURNING id"),
        {"id": new_id, "kind": kind, "mime": mime_type, "count": len(data), "sha": digest, "bytes": data,
         "meta": json.dumps(metadata or {})}).first()
    if row:
        return str(row[0])
    return str(conn.execute(text("SELECT id FROM annotation.assets WHERE kind = :kind AND sha256 = :sha"),
                            {"kind": kind, "sha": digest}).scalar_one())


def asset_meta(conn: Connection, asset_id: str) -> dict[str, Any] | None:
    row = conn.execute(text("SELECT id, kind, mime_type, byte_count, sha256 FROM annotation.assets WHERE id = :id"),
                       {"id": asset_id}).mappings().first()
    return dict(row) if row else None


def asset_bytes(conn: Connection, asset_id: str) -> bytes | None:
    value = conn.execute(text("SELECT bytes FROM annotation.assets WHERE id = :id"), {"id": asset_id}).scalar()
    return bytes(value) if value is not None else None
