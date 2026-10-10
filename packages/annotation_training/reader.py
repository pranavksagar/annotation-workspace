"""Internal, read-only access to training-ready snapshots. Not part of the public HTTP surface.

A training run selects snapshot IDs from `annotation.ready_training_snapshots`, pins them in its own
manifest, and then loads exactly those snapshots. Pinned historical snapshots remain loadable even if the
drawing is later reopened; new selections only see currently completed drawings.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text

from packages.annotation_domain.canonical import canonical_sha256
from packages.annotation_domain.vocabulary import SNAPSHOT_SCHEMA_VERSION

SUPPORTED_SCHEMAS = {SNAPSHOT_SCHEMA_VERSION}


class SnapshotIntegrityError(Exception):
    pass


@dataclass(frozen=True)
class TrainingSnapshot:
    id: UUID
    drawing_id: UUID
    sequence: int
    annotation_version: int
    schema_version: str
    payload_sha256: str
    payload: dict[str, Any] = field(repr=False)

    @property
    def pages(self) -> list[dict[str, Any]]:
        return self.payload["pages"]

    @property
    def regions(self) -> list[dict[str, Any]]:
        return self.payload["regions"]

    @property
    def groups(self) -> list[dict[str, Any]]:
        return self.payload["groups"]

    @property
    def lineage_key(self) -> str:
        return self.payload["drawing"]["lineage_key"]


def list_ready_snapshot_ids(connection: Connection) -> list[UUID]:
    rows = connection.execute(text(
        "SELECT snapshot_id FROM annotation.ready_training_snapshots ORDER BY completed_at, snapshot_id"))
    return [UUID(str(r[0])) for r in rows]


def load_snapshot(connection: Connection, snapshot_id: UUID | str) -> TrainingSnapshot:
    row = connection.execute(text(
        "SELECT id, drawing_id, sequence, annotation_version, schema_version, payload, payload_sha256 "
        "FROM annotation.training_snapshots WHERE id = :id"), {"id": str(snapshot_id)}).mappings().first()
    if row is None:
        raise KeyError(f"snapshot {snapshot_id} not found")
    if row["schema_version"] not in SUPPORTED_SCHEMAS:
        raise SnapshotIntegrityError(f"unsupported snapshot schema {row['schema_version']}")
    expected = row["payload_sha256"].strip()
    if canonical_sha256(row["payload"]) != expected:
        raise SnapshotIntegrityError(f"snapshot {snapshot_id} checksum mismatch")
    return TrainingSnapshot(UUID(str(row["id"])), UUID(str(row["drawing_id"])), row["sequence"],
                            int(row["annotation_version"]), row["schema_version"], expected, row["payload"])


def load_asset_bytes(connection: Connection, asset_id: UUID | str, expected_sha256: str | None = None) -> bytes:
    row = connection.execute(text("SELECT bytes, sha256 FROM annotation.assets WHERE id = :id"),
                             {"id": str(asset_id)}).first()
    if row is None:
        raise KeyError(f"asset {asset_id} not found")
    data, stored = bytes(row[0]), row[1].strip()
    actual = hashlib.sha256(data).hexdigest()
    if actual != stored or (expected_sha256 and actual != expected_sha256):
        raise SnapshotIntegrityError(f"asset {asset_id} checksum mismatch")
    return data
