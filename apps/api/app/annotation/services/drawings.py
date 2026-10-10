"""Drawing library: upload registration, listing, detail, documents and preparation retry."""
from __future__ import annotations

import base64
import json
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Connection, text
from sqlalchemy.exc import IntegrityError

from ..config import settings
from ..database import get_engine, snapshot_read, transaction
from ..errors import ApiError
from ..repository import (drawing_json, get_drawing, iso, lease_availability, load_document_state, load_pages,
                          page_json, render_job, sid, sorted_document)
from .assets import insert_asset, sha256_hex
from .uploads import sanitize_filename, validate_source

STATUSES = ("TO_DO", "IN_PROGRESS", "COMPLETED")


def _existing_by_hash(conn: Connection, digest: str) -> dict[str, Any] | None:
    row = conn.execute(text("SELECT id FROM annotation.drawings WHERE source_sha256 = :sha"), {"sha": digest}).first()
    return get_drawing(conn, row[0]) if row else None


def register_upload(filename: str | None, data: bytes) -> tuple[dict[str, Any], bool]:
    """Validate and store a source. Returns (drawing, duplicate). Commits before returning."""
    validated = validate_source(data)
    digest = sha256_hex(data)
    name = sanitize_filename(filename)
    with transaction() as conn:
        existing = _existing_by_hash(conn, digest)
        if existing:
            return existing, True
    try:
        with transaction() as conn:
            asset_id = insert_asset(conn, "SOURCE", validated.mime_type, data,
                                    {"original_filename": name, "declared_page_count": validated.page_count}, digest)
            drawing_id = str(uuid.uuid4())
            conn.execute(text(
                "INSERT INTO annotation.drawings (id, filename, source_asset_id, source_sha256, source_format, lineage_key) "
                "VALUES (:id, :name, :asset, :sha, :fmt, :lineage)"),
                {"id": drawing_id, "name": name, "asset": asset_id, "sha": digest, "fmt": validated.source_format,
                 "lineage": digest})
            conn.execute(text(
                "INSERT INTO annotation.preparation_jobs (id, drawing_id, job_type, state, progress) "
                "VALUES (:id, :drawing, 'RENDER', 'QUEUED', CAST(:progress AS jsonb))"),
                {"id": str(uuid.uuid4()), "drawing": drawing_id,
                 "progress": json.dumps({"pages_done": 0, "page_count": validated.page_count})})
            return get_drawing(conn, drawing_id), False
    except IntegrityError:
        # A simultaneous upload of the same bytes won the unique constraint race.
        with transaction() as conn:
            existing = _existing_by_hash(conn, digest)
        if existing:
            return existing, True
        raise


def _encode_cursor(ts: datetime, drawing_id: str) -> str:
    return base64.urlsafe_b64encode(json.dumps([ts.isoformat(), drawing_id]).encode()).decode()


def _decode_cursor(cursor: str) -> tuple[str, str]:
    try:
        ts, drawing_id = json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
        uuid.UUID(drawing_id)
        datetime.fromisoformat(ts)
        return ts, drawing_id
    except Exception as exc:
        raise ApiError(422, "INVALID_CURSOR", "The page cursor is invalid.") from exc


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def list_drawings(q: str | None, status: str | None, cursor: str | None, limit: int) -> dict[str, Any]:
    if status and status not in STATUSES:
        raise ApiError(422, "INVALID_FILTER", "Unknown workflow status filter.")
    limit = max(1, min(limit, 100))
    params: dict[str, Any] = {"limit": limit + 1}
    search = ""
    if q and q.strip():
        search = " AND d.filename ILIKE :q ESCAPE '\\'"
        params["q"] = f"%{_escape_like(q.strip())}%"
    where = "WHERE TRUE" + search
    if status:
        where += " AND d.annotation_status = :status"
        params["status"] = status
    if cursor:
        ts, cursor_id = _decode_cursor(cursor)
        where += " AND (d.annotation_updated_at, d.id) < (CAST(:cts AS timestamptz), CAST(:cid AS uuid))"
        params.update(cts=ts, cid=cursor_id)
    with snapshot_read() as conn:
        rows = [dict(r) for r in conn.execute(text(f"""
            SELECT d.id, d.filename, d.page_count, d.annotation_status, d.annotation_version, d.preparation_state,
                   d.preparation_error_message, d.created_at, d.annotation_updated_at, d.completed_at,
                   p.thumbnail_asset_id,
                   EXISTS (SELECT 1 FROM annotation.editing_leases l WHERE l.drawing_id = d.id
                           AND l.expires_at > now() AND l.token_hash <> '') AS being_edited,
                   (SELECT j.progress FROM annotation.preparation_jobs j WHERE j.drawing_id = d.id
                      AND j.job_type = 'RENDER' ORDER BY j.created_at DESC LIMIT 1) AS preparation_progress
            FROM annotation.drawings d
            LEFT JOIN annotation.pages p ON p.drawing_id = d.id AND p.page_number = 1
            {where}
            ORDER BY d.annotation_updated_at DESC, d.id DESC
            LIMIT :limit"""), params).mappings()]
        count_params = {k: v for k, v in params.items() if k == "q"}
        counts = {s: 0 for s in STATUSES}
        for status_name, count in conn.execute(text(
                f"SELECT annotation_status, count(*) FROM annotation.drawings d WHERE TRUE{search} "
                "GROUP BY annotation_status"), count_params):
            counts[status_name] = count
    has_more = len(rows) > limit
    rows = rows[:limit]
    items = [{
        "id": sid(r["id"]), "filename": r["filename"], "page_count": r["page_count"],
        "annotation_status": r["annotation_status"], "annotation_version": str(r["annotation_version"]),
        "preparation_state": r["preparation_state"], "preparation_error_message": r["preparation_error_message"],
        "preparation_progress": r["preparation_progress"] or {},
        "thumbnail_asset_id": sid(r["thumbnail_asset_id"]), "being_edited": r["being_edited"],
        "created_at": iso(r["created_at"]), "annotation_updated_at": iso(r["annotation_updated_at"]),
        "completed_at": iso(r["completed_at"]),
    } for r in rows]
    next_cursor = _encode_cursor(rows[-1]["annotation_updated_at"], str(rows[-1]["id"])) if has_more and rows else None
    return {"items": items, "counts": {**counts, "ALL": sum(counts.values())}, "next_cursor": next_cursor}


def _preparation_json(drawing: dict[str, Any], job: dict[str, Any] | None) -> dict[str, Any]:
    return {
        "state": drawing["preparation_state"],
        "progress": (job or {}).get("progress") or {},
        "error_code": drawing["preparation_error_code"],
        "error_message": drawing["preparation_error_message"],
        "job_state": (job or {}).get("state"),
        "retryable": drawing["preparation_state"] == "FAILED",
    }


def drawing_detail(drawing_id: str) -> dict[str, Any]:
    with snapshot_read() as conn:
        drawing = get_drawing(conn, drawing_id)
        pages = load_pages(conn, drawing_id)
        job = render_job(conn, drawing_id)
        lease = lease_availability(conn, drawing_id)
    return {"drawing": drawing_json(drawing), "pages": [page_json(p) for p in pages],
            "preparation": _preparation_json(drawing, job), "lease": lease,
            "ocr_enabled": settings.enable_ocr_suggestions}


def read_document(drawing_id: str) -> dict[str, Any]:
    """The full human annotation document from one consistent snapshot."""
    with snapshot_read() as conn:
        drawing = get_drawing(conn, drawing_id)
        pages = load_pages(conn, drawing_id)
        state = load_document_state(conn, drawing_id)
    ordered = sorted_document(state, {str(p["id"]): p["page_number"] for p in pages})
    return {"drawing_id": str(drawing["id"]), "annotation_version": str(drawing["annotation_version"]),
            "annotation_status": drawing["annotation_status"], **ordered}


def retry_preparation(drawing_id: str) -> dict[str, Any]:
    with transaction() as conn:
        drawing = get_drawing(conn, drawing_id, lock=True)
        if drawing["preparation_state"] != "FAILED":
            raise ApiError(409, "PREPARATION_NOT_FAILED", "Only failed preparation can be retried.")
        job = conn.execute(text(
            "SELECT id FROM annotation.preparation_jobs WHERE drawing_id = :id AND job_type = 'RENDER' "
            "AND state = 'FAILED' ORDER BY created_at DESC LIMIT 1 FOR UPDATE"), {"id": drawing_id}).first()
        if job is None:
            raise ApiError(409, "PREPARATION_NOT_FAILED", "There is no failed preparation job to retry.")
        # Requeue the same job; published pages are kept and never re-framed.
        conn.execute(text(
            "UPDATE annotation.preparation_jobs SET state = 'QUEUED', attempt_count = 0, next_attempt_at = now(), "
            "lease_generation = lease_generation + 1, lease_token = NULL, leased_until = NULL, error_code = NULL, "
            "error_message = NULL, finished_at = NULL WHERE id = :id"), {"id": job[0]})
        conn.execute(text(
            "UPDATE annotation.drawings SET preparation_state = 'QUEUED', preparation_error_code = NULL, "
            "preparation_error_message = NULL WHERE id = :id"), {"id": drawing_id})
    return drawing_detail(drawing_id)


def engine_ready() -> bool:
    with get_engine().connect() as conn:
        return conn.execute(text("SELECT 1")).scalar() == 1
