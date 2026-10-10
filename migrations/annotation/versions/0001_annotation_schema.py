"""Annotation workspace schema.

Revision ID: 0001_annotation_schema
Revises:
Create Date: 2026-10-01
"""
from alembic import op

revision = "0001_annotation_schema"
down_revision = None
branch_labels = None
depends_on = None

UPGRADE = r"""
CREATE SCHEMA IF NOT EXISTS annotation;

-- Immutable binary storage. Source files, page renders and thumbnails all live here.
CREATE TABLE annotation.assets (
    id UUID PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('SOURCE', 'PAGE_RENDER', 'THUMBNAIL')),
    mime_type TEXT NOT NULL,
    byte_count BIGINT NOT NULL CHECK (byte_count >= 0),
    sha256 CHAR(64) NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    bytes BYTEA NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT assets_kind_sha256_key UNIQUE (kind, sha256),
    CONSTRAINT assets_byte_count_matches CHECK (octet_length(bytes) = byte_count)
);

CREATE TABLE annotation.drawings (
    id UUID PRIMARY KEY,
    filename TEXT NOT NULL CHECK (length(filename) BETWEEN 1 AND 255),
    display_title TEXT,
    source_asset_id UUID NOT NULL REFERENCES annotation.assets(id),
    source_sha256 CHAR(64) NOT NULL,
    source_format TEXT NOT NULL CHECK (source_format IN ('PDF', 'PNG', 'JPEG')),
    annotation_status TEXT NOT NULL DEFAULT 'TO_DO'
        CHECK (annotation_status IN ('TO_DO', 'IN_PROGRESS', 'COMPLETED')),
    preparation_state TEXT NOT NULL DEFAULT 'QUEUED'
        CHECK (preparation_state IN ('QUEUED', 'RUNNING', 'READY', 'FAILED')),
    ocr_state TEXT NOT NULL DEFAULT 'NOT_REQUESTED'
        CHECK (ocr_state IN ('NOT_REQUESTED', 'QUEUED', 'RUNNING', 'READY', 'FAILED')),
    annotation_version BIGINT NOT NULL DEFAULT 0 CHECK (annotation_version >= 0),
    next_requirement_number INTEGER NOT NULL DEFAULT 1 CHECK (next_requirement_number >= 1),
    page_count INTEGER CHECK (page_count IS NULL OR page_count >= 1),
    active_training_snapshot_id UUID,
    last_annotation_page_id UUID,
    lineage_key TEXT NOT NULL,
    preparation_error_code TEXT,
    preparation_error_message TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Initialised to created_at so library ordering is "most recent annotation activity or
    -- creation". Only acknowledged annotation mutations, completion and reopening change it.
    annotation_updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ,
    CONSTRAINT drawings_source_sha256_key UNIQUE (source_sha256),
    CONSTRAINT drawings_completed_consistency CHECK (
        (annotation_status = 'COMPLETED' AND active_training_snapshot_id IS NOT NULL AND completed_at IS NOT NULL)
        OR (annotation_status <> 'COMPLETED' AND active_training_snapshot_id IS NULL AND completed_at IS NULL)
    )
);
CREATE INDEX drawings_source_asset_idx ON annotation.drawings (source_asset_id);
CREATE INDEX drawings_status_activity_idx ON annotation.drawings (annotation_status, annotation_updated_at DESC, id);
CREATE INDEX drawings_activity_idx ON annotation.drawings (annotation_updated_at DESC, id DESC);

CREATE TABLE annotation.pages (
    id UUID PRIMARY KEY,
    drawing_id UUID NOT NULL REFERENCES annotation.drawings(id),
    page_number INTEGER NOT NULL CHECK (page_number >= 1),
    coordinate_unit TEXT NOT NULL CHECK (coordinate_unit IN ('PT', 'PX')),
    coordinate_space_version INTEGER NOT NULL DEFAULT 1,
    width DOUBLE PRECISION NOT NULL CHECK (width > 0),
    height DOUBLE PRECISION NOT NULL CHECK (height > 0),
    source_geometry JSONB NOT NULL,
    render_asset_id UUID NOT NULL REFERENCES annotation.assets(id),
    thumbnail_asset_id UUID NOT NULL REFERENCES annotation.assets(id),
    render_width_px INTEGER NOT NULL CHECK (render_width_px > 0),
    render_height_px INTEGER NOT NULL CHECK (render_height_px > 0),
    canonical_to_render_matrix JSONB NOT NULL,
    render_profile JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT pages_drawing_page_number_key UNIQUE (drawing_id, page_number),
    CONSTRAINT pages_drawing_id_key UNIQUE (drawing_id, id)
);
CREATE INDEX pages_render_asset_idx ON annotation.pages (render_asset_id);
CREATE INDEX pages_thumbnail_asset_idx ON annotation.pages (thumbnail_asset_id);

CREATE TABLE annotation.regions (
    id UUID PRIMARY KEY,
    drawing_id UUID NOT NULL REFERENCES annotation.drawings(id),
    page_id UUID NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('TEXT', 'SYMBOL', 'IGNORE')),
    polygon JSONB NOT NULL CHECK (jsonb_typeof(polygon) = 'array' AND jsonb_array_length(polygon) = 4),
    transcription TEXT NOT NULL DEFAULT '',
    symbol_code TEXT,
    legibility TEXT NOT NULL DEFAULT 'READABLE' CHECK (legibility IN ('READABLE', 'UNREADABLE')),
    reading_angle_degrees DOUBLE PRECISION NOT NULL DEFAULT 0
        CHECK (reading_angle_degrees > -180 AND reading_angle_degrees <= 180),
    semantic_disposition TEXT NOT NULL DEFAULT 'UNCLASSIFIED'
        CHECK (semantic_disposition IN ('UNCLASSIFIED', 'NON_REQUIREMENT', 'SUPPORTING_CONTEXT')),
    origin TEXT NOT NULL DEFAULT 'MANUAL' CHECK (origin IN ('MANUAL', 'OCR_ACCEPTED', 'LEGACY_IMPORT')),
    origin_suggestion_id UUID,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT regions_drawing_id_key UNIQUE (drawing_id, id),
    CONSTRAINT regions_page_fk FOREIGN KEY (drawing_id, page_id) REFERENCES annotation.pages(drawing_id, id),
    CONSTRAINT regions_ignore_unreadable CHECK (kind <> 'IGNORE' OR legibility = 'UNREADABLE')
);
CREATE INDEX regions_drawing_page_idx ON annotation.regions (drawing_id, page_id);

CREATE TABLE annotation.requirement_groups (
    id UUID PRIMARY KEY,
    drawing_id UUID NOT NULL REFERENCES annotation.drawings(id),
    primary_page_id UUID NOT NULL,
    display_number INTEGER NOT NULL CHECK (display_number >= 1),
    requirement_type TEXT NOT NULL CHECK (requirement_type IN (
        'DIMENSION', 'GEOMETRIC_TOLERANCE', 'SURFACE_FINISH', 'THREAD', 'MATERIAL',
        'HEAT_TREATMENT', 'COATING', 'GEAR_SPLINE', 'MANUFACTURING_NOTE', 'OTHER')),
    description TEXT,
    structured_value JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT groups_drawing_number_key UNIQUE (drawing_id, display_number),
    CONSTRAINT groups_drawing_id_key UNIQUE (drawing_id, id),
    CONSTRAINT groups_page_fk FOREIGN KEY (drawing_id, primary_page_id) REFERENCES annotation.pages(drawing_id, id)
);
CREATE INDEX groups_drawing_page_idx ON annotation.requirement_groups (drawing_id, primary_page_id);

CREATE TABLE annotation.group_members (
    drawing_id UUID NOT NULL,
    group_id UUID NOT NULL,
    region_id UUID NOT NULL,
    role TEXT NOT NULL CHECK (role IN (
        'PRIMARY', 'NOMINAL', 'UPPER_TOLERANCE', 'LOWER_TOLERANCE', 'UNIT', 'DATUM', 'SYMBOL', 'CONTEXT')),
    order_index INTEGER NOT NULL DEFAULT 0 CHECK (order_index >= 0),
    PRIMARY KEY (group_id, region_id, role),
    CONSTRAINT members_group_fk FOREIGN KEY (drawing_id, group_id) REFERENCES annotation.requirement_groups(drawing_id, id),
    CONSTRAINT members_region_fk FOREIGN KEY (drawing_id, region_id) REFERENCES annotation.regions(drawing_id, id)
);
CREATE INDEX members_region_idx ON annotation.group_members (region_id);
CREATE INDEX members_drawing_group_idx ON annotation.group_members (drawing_id, group_id);

-- Anonymous per-browser navigation. No names, emails or users table.
CREATE TABLE annotation.browser_progress (
    browser_id UUID NOT NULL,
    drawing_id UUID NOT NULL REFERENCES annotation.drawings(id),
    page_id UUID NOT NULL,
    center_x DOUBLE PRECISION NOT NULL,
    center_y DOUBLE PRECISION NOT NULL,
    zoom_relative_to_fit DOUBLE PRECISION NOT NULL CHECK (zoom_relative_to_fit > 0),
    selected_region_id UUID,
    selected_group_id UUID,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (browser_id, drawing_id),
    CONSTRAINT progress_page_fk FOREIGN KEY (drawing_id, page_id) REFERENCES annotation.pages(drawing_id, id)
);
CREATE INDEX progress_drawing_page_idx ON annotation.browser_progress (drawing_id, page_id);

-- Collision-prevention lease. Rows are retained on release so generation never decreases.
CREATE TABLE annotation.editing_leases (
    drawing_id UUID PRIMARY KEY REFERENCES annotation.drawings(id),
    browser_id UUID NOT NULL,
    tab_id UUID NOT NULL,
    token_hash TEXT NOT NULL,
    generation BIGINT NOT NULL CHECK (generation >= 0),
    expires_at TIMESTAMPTZ NOT NULL,
    last_heartbeat_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE annotation.mutation_receipts (
    drawing_id UUID NOT NULL REFERENCES annotation.drawings(id),
    mutation_id UUID NOT NULL,
    operation_kind TEXT NOT NULL CHECK (operation_kind IN ('EDIT', 'COMPLETE', 'REOPEN')),
    request_sha256 CHAR(64) NOT NULL,
    expected_version BIGINT NOT NULL,
    resulting_version BIGINT NOT NULL,
    request_payload JSONB NOT NULL,
    result_payload JSONB NOT NULL,
    browser_id UUID,
    tab_id UUID,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (drawing_id, mutation_id)
);

CREATE TABLE annotation.training_snapshots (
    id UUID PRIMARY KEY,
    drawing_id UUID NOT NULL REFERENCES annotation.drawings(id),
    sequence INTEGER NOT NULL CHECK (sequence >= 1),
    annotation_version BIGINT NOT NULL,
    schema_version TEXT NOT NULL,
    payload JSONB NOT NULL,
    payload_sha256 CHAR(64) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT snapshots_drawing_sequence_key UNIQUE (drawing_id, sequence),
    CONSTRAINT snapshots_drawing_id_key UNIQUE (drawing_id, id)
);
CREATE INDEX snapshots_drawing_sequence_idx ON annotation.training_snapshots (drawing_id, sequence DESC);

ALTER TABLE annotation.drawings
    ADD CONSTRAINT drawings_active_snapshot_fk FOREIGN KEY (id, active_training_snapshot_id)
        REFERENCES annotation.training_snapshots(drawing_id, id),
    ADD CONSTRAINT drawings_last_annotation_page_fk FOREIGN KEY (id, last_annotation_page_id)
        REFERENCES annotation.pages(drawing_id, id);

CREATE TABLE annotation.preparation_jobs (
    id UUID PRIMARY KEY,
    drawing_id UUID NOT NULL REFERENCES annotation.drawings(id),
    job_type TEXT NOT NULL CHECK (job_type IN ('RENDER', 'OCR')),
    state TEXT NOT NULL CHECK (state IN ('QUEUED', 'RUNNING', 'SUCCEEDED', 'FAILED')),
    attempt_count INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    lease_token TEXT,
    lease_generation BIGINT NOT NULL DEFAULT 0,
    leased_until TIMESTAMPTZ,
    heartbeat_at TIMESTAMPTZ,
    progress JSONB NOT NULL DEFAULT '{}'::jsonb,
    run_metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    error_code TEXT,
    error_message TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ
);
CREATE UNIQUE INDEX jobs_one_active_per_drawing_type ON annotation.preparation_jobs (drawing_id, job_type)
    WHERE state IN ('QUEUED', 'RUNNING');
CREATE INDEX jobs_queue_idx ON annotation.preparation_jobs (state, next_attempt_at, created_at);
CREATE INDEX jobs_drawing_idx ON annotation.preparation_jobs (drawing_id, job_type, created_at DESC);

-- Immutability guards that hold regardless of which database role connects.
CREATE FUNCTION annotation.reject_modification() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '% rows are immutable', TG_TABLE_NAME USING ERRCODE = 'integrity_constraint_violation';
END;
$$;
CREATE TRIGGER assets_immutable BEFORE UPDATE OR DELETE ON annotation.assets
    FOR EACH ROW EXECUTE FUNCTION annotation.reject_modification();
CREATE TRIGGER snapshots_immutable BEFORE UPDATE OR DELETE ON annotation.training_snapshots
    FOR EACH ROW EXECUTE FUNCTION annotation.reject_modification();

CREATE FUNCTION annotation.protect_page_frame() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.width IS DISTINCT FROM OLD.width OR NEW.height IS DISTINCT FROM OLD.height
       OR NEW.coordinate_unit IS DISTINCT FROM OLD.coordinate_unit
       OR NEW.render_asset_id IS DISTINCT FROM OLD.render_asset_id
       OR NEW.canonical_to_render_matrix IS DISTINCT FROM OLD.canonical_to_render_matrix
       OR NEW.drawing_id IS DISTINCT FROM OLD.drawing_id OR NEW.page_number IS DISTINCT FROM OLD.page_number THEN
        RAISE EXCEPTION 'published page frames are immutable' USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER pages_frame_immutable BEFORE UPDATE ON annotation.pages
    FOR EACH ROW EXECUTE FUNCTION annotation.protect_page_frame();

-- Current training-ready set: completed drawings joined to their active snapshot.
CREATE VIEW annotation.ready_training_snapshots AS
SELECT s.id AS snapshot_id, s.drawing_id, s.sequence, s.annotation_version, s.schema_version,
       s.payload_sha256, s.created_at, d.lineage_key, d.completed_at
FROM annotation.drawings d
JOIN annotation.training_snapshots s
  ON s.id = d.active_training_snapshot_id AND s.drawing_id = d.id
WHERE d.annotation_status = 'COMPLETED';
"""

DOWNGRADE = r"""
DROP VIEW IF EXISTS annotation.ready_training_snapshots;
DROP TABLE IF EXISTS annotation.preparation_jobs;
ALTER TABLE annotation.drawings DROP CONSTRAINT IF EXISTS drawings_active_snapshot_fk;
ALTER TABLE annotation.drawings DROP CONSTRAINT IF EXISTS drawings_last_annotation_page_fk;
DROP TABLE IF EXISTS annotation.training_snapshots;
DROP TABLE IF EXISTS annotation.mutation_receipts;
DROP TABLE IF EXISTS annotation.editing_leases;
DROP TABLE IF EXISTS annotation.browser_progress;
DROP TABLE IF EXISTS annotation.group_members;
DROP TABLE IF EXISTS annotation.requirement_groups;
DROP TABLE IF EXISTS annotation.regions;
DROP TABLE IF EXISTS annotation.pages;
DROP TABLE IF EXISTS annotation.drawings;
DROP TABLE IF EXISTS annotation.assets;
DROP FUNCTION IF EXISTS annotation.protect_page_frame();
DROP FUNCTION IF EXISTS annotation.reject_modification();
"""


def upgrade() -> None:
    op.execute(UPGRADE)


def downgrade() -> None:
    op.execute(DOWNGRADE)
