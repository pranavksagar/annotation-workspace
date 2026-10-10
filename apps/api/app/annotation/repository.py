"""Row loading and JSON serialization. Queries never select asset bytes unless asked to."""
from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text

from .errors import not_found

DRAWING_COLUMNS = """d.id, d.filename, d.display_title, d.source_asset_id, d.source_sha256, d.source_format,
    d.annotation_status, d.preparation_state, d.ocr_state, d.annotation_version, d.next_requirement_number,
    d.page_count, d.active_training_snapshot_id, d.last_annotation_page_id, d.lineage_key,
    d.preparation_error_code, d.preparation_error_message, d.created_at, d.annotation_updated_at, d.completed_at"""


def iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def sid(value: UUID | str | None) -> str | None:
    return str(value) if value is not None else None


def get_drawing(conn: Connection, drawing_id: UUID | str, *, lock: bool = False) -> dict[str, Any]:
    sql = f"SELECT {DRAWING_COLUMNS} FROM annotation.drawings d WHERE d.id = :id"
    if lock:
        sql += " FOR UPDATE"
    row = conn.execute(text(sql), {"id": str(drawing_id)}).mappings().first()
    if row is None:
        raise not_found()
    return dict(row)


def drawing_json(d: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": sid(d["id"]), "filename": d["filename"], "display_title": d["display_title"],
        "source_format": d["source_format"], "source_asset_id": sid(d["source_asset_id"]),
        "source_sha256": d["source_sha256"].strip(), "annotation_status": d["annotation_status"],
        "preparation_state": d["preparation_state"], "ocr_state": d["ocr_state"],
        "annotation_version": str(d["annotation_version"]), "page_count": d["page_count"],
        "active_training_snapshot_id": sid(d["active_training_snapshot_id"]),
        "last_annotation_page_id": sid(d["last_annotation_page_id"]),
        "preparation_error_code": d["preparation_error_code"],
        "preparation_error_message": d["preparation_error_message"],
        "created_at": iso(d["created_at"]), "annotation_updated_at": iso(d["annotation_updated_at"]),
        "completed_at": iso(d["completed_at"]),
    }


PAGE_COLUMNS = """p.id, p.drawing_id, p.page_number, p.coordinate_unit, p.coordinate_space_version, p.width, p.height,
    p.source_geometry, p.render_asset_id, p.thumbnail_asset_id, p.render_width_px, p.render_height_px,
    p.canonical_to_render_matrix, p.render_profile"""


def load_pages(conn: Connection, drawing_id: UUID | str) -> list[dict[str, Any]]:
    rows = conn.execute(text(f"SELECT {PAGE_COLUMNS}, a.sha256 AS render_sha256 FROM annotation.pages p "
                             "JOIN annotation.assets a ON a.id = p.render_asset_id "
                             "WHERE p.drawing_id = :id ORDER BY p.page_number"), {"id": str(drawing_id)})
    return [dict(r) for r in rows.mappings()]


def page_json(p: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": sid(p["id"]), "page_number": p["page_number"], "coordinate_unit": p["coordinate_unit"],
        "coordinate_space_version": p["coordinate_space_version"], "width": p["width"], "height": p["height"],
        "render_asset_id": sid(p["render_asset_id"]), "thumbnail_asset_id": sid(p["thumbnail_asset_id"]),
        "render_width_px": p["render_width_px"], "render_height_px": p["render_height_px"],
        "canonical_to_render_matrix": p["canonical_to_render_matrix"],
    }


REGION_FIELDS = ("id", "page_id", "kind", "polygon", "transcription", "symbol_code", "legibility",
                 "reading_angle_degrees", "semantic_disposition", "origin", "origin_suggestion_id")
GROUP_FIELDS = ("id", "primary_page_id", "display_number", "requirement_type", "description", "structured_value")
MEMBER_FIELDS = ("group_id", "region_id", "role", "order_index")


def _normalize(row: dict[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    out = {}
    for name in fields:
        value = row[name]
        out[name] = str(value) if isinstance(value, UUID) else value
    return out


def load_document_state(conn: Connection, drawing_id: UUID | str) -> dict[str, dict]:
    """Human annotation entities as plain dicts keyed by identity."""
    params = {"id": str(drawing_id)}
    regions = {str(r["id"]): _normalize(dict(r), REGION_FIELDS) for r in conn.execute(text(
        f"SELECT {', '.join(REGION_FIELDS)} FROM annotation.regions WHERE drawing_id = :id"), params).mappings()}
    groups = {str(g["id"]): _normalize(dict(g), GROUP_FIELDS) for g in conn.execute(text(
        f"SELECT {', '.join(GROUP_FIELDS)} FROM annotation.requirement_groups WHERE drawing_id = :id"), params).mappings()}
    members = {}
    for m in conn.execute(text(f"SELECT {', '.join(MEMBER_FIELDS)} FROM annotation.group_members WHERE drawing_id = :id"),
                          params).mappings():
        item = _normalize(dict(m), MEMBER_FIELDS)
        members[member_key(item)] = item
    return {"regions": regions, "groups": groups, "members": members}


def member_key(member: dict[str, Any]) -> tuple[str, str, str]:
    return (str(member["group_id"]), str(member["region_id"]), member["role"])


def sorted_document(state: dict[str, dict], page_numbers: dict[str, int]) -> dict[str, list]:
    regions = sorted(state["regions"].values(), key=lambda r: (page_numbers.get(r["page_id"], 0), r["id"]))
    groups = sorted(state["groups"].values(), key=lambda g: g["display_number"])
    members = sorted(state["members"].values(), key=lambda m: (m["group_id"], m["order_index"], m["region_id"], m["role"]))
    return {"regions": regions, "groups": groups, "members": members}


def lease_row(conn: Connection, drawing_id: UUID | str, *, lock: bool = False) -> dict[str, Any] | None:
    sql = ("SELECT drawing_id, browser_id, tab_id, token_hash, generation, expires_at, last_heartbeat_at, "
           "expires_at > now() AS active, GREATEST(0, EXTRACT(EPOCH FROM expires_at - now())) AS remaining "
           "FROM annotation.editing_leases WHERE drawing_id = :id")
    if lock:
        sql += " FOR UPDATE"
    row = conn.execute(text(sql), {"id": str(drawing_id)}).mappings().first()
    return dict(row) if row else None


def lease_availability(conn: Connection, drawing_id: UUID | str) -> dict[str, Any]:
    """Non-sensitive lease status: never exposes browser or tab identifiers."""
    row = lease_row(conn, drawing_id)
    if row and row["active"] and row["token_hash"]:
        return {"available": False, "retry_after_seconds": int(float(row["remaining"])) + 1}
    return {"available": True, "retry_after_seconds": 0}


def render_job(conn: Connection, drawing_id: UUID | str) -> dict[str, Any] | None:
    row = conn.execute(text(
        "SELECT id, state, attempt_count, progress, error_code, error_message, next_attempt_at, finished_at "
        "FROM annotation.preparation_jobs WHERE drawing_id = :id AND job_type = 'RENDER' "
        "ORDER BY created_at DESC LIMIT 1"), {"id": str(drawing_id)}).mappings().first()
    return dict(row) if row else None
