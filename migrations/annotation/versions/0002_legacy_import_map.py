"""Traceability map for the repeatable legacy import.

Revision ID: 0002_legacy_import_map
Revises: 0001_annotation_schema
Create Date: 2026-10-01
"""
from alembic import op

revision = "0002_legacy_import_map"
down_revision = "0001_annotation_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    CREATE TABLE annotation.legacy_import_map (
        legacy_kind TEXT NOT NULL CHECK (legacy_kind IN ('REVISION', 'REQUIREMENT', 'EVIDENCE')),
        legacy_id TEXT NOT NULL,
        entity_id UUID NOT NULL,
        drawing_id UUID NOT NULL REFERENCES annotation.drawings(id),
        details JSONB NOT NULL DEFAULT '{}'::jsonb,
        imported_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (legacy_kind, legacy_id)
    );
    CREATE INDEX legacy_import_map_drawing_idx ON annotation.legacy_import_map (drawing_id);
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS annotation.legacy_import_map")
