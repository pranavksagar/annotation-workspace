"""Anonymous per-browser navigation state. Never touches annotation version or workflow status."""
from __future__ import annotations

from typing import Any

from sqlalchemy import text

from ..database import snapshot_read, transaction
from ..errors import ApiError
from ..repository import get_drawing, iso, sid
from ..schemas import BrowserProgressIn


def read_progress(drawing_id: str, browser_id: str) -> dict[str, Any]:
    with snapshot_read() as conn:
        drawing = get_drawing(conn, drawing_id)
        row = conn.execute(text(
            "SELECT bp.page_id, bp.center_x, bp.center_y, bp.zoom_relative_to_fit, bp.updated_at, "
            "(SELECT r.id FROM annotation.regions r WHERE r.id = bp.selected_region_id AND r.drawing_id = bp.drawing_id) "
            "  AS selected_region_id, "
            "(SELECT g.id FROM annotation.requirement_groups g WHERE g.id = bp.selected_group_id "
            "  AND g.drawing_id = bp.drawing_id) AS selected_group_id "
            "FROM annotation.browser_progress bp WHERE bp.browser_id = :b AND bp.drawing_id = :d"),
            {"b": browser_id, "d": drawing_id}).mappings().first()
    progress = None
    if row:
        # A deleted selection falls back to no selection.
        progress = {"page_id": sid(row["page_id"]), "center_x": row["center_x"], "center_y": row["center_y"],
                    "zoom_relative_to_fit": row["zoom_relative_to_fit"],
                    "selected_region_id": sid(row["selected_region_id"]),
                    "selected_group_id": sid(row["selected_group_id"]), "updated_at": iso(row["updated_at"])}
    return {"progress": progress, "last_annotation_page_id": sid(drawing["last_annotation_page_id"])}


def write_progress(drawing_id: str, body: BrowserProgressIn) -> dict[str, Any]:
    with transaction() as conn:
        get_drawing(conn, drawing_id)
        page = conn.execute(text("SELECT 1 FROM annotation.pages WHERE id = :p AND drawing_id = :d"),
                            {"p": str(body.page_id), "d": drawing_id}).first()
        if page is None:
            raise ApiError(422, "INVALID_REFERENCE", "The page does not belong to this drawing.")
        # Selections that are not (yet) saved in this drawing are stored as no selection.
        region = conn.execute(text("SELECT id FROM annotation.regions WHERE id = :r AND drawing_id = :d"),
                              {"r": str(body.selected_region_id), "d": drawing_id}).scalar() \
            if body.selected_region_id else None
        group = conn.execute(text("SELECT id FROM annotation.requirement_groups WHERE id = :g AND drawing_id = :d"),
                             {"g": str(body.selected_group_id), "d": drawing_id}).scalar() \
            if body.selected_group_id else None
        conn.execute(text(
            "INSERT INTO annotation.browser_progress (browser_id, drawing_id, page_id, center_x, center_y, "
            "zoom_relative_to_fit, selected_region_id, selected_group_id, updated_at) VALUES "
            "(:b, :d, :p, :x, :y, :z, :r, :g, now()) ON CONFLICT (browser_id, drawing_id) DO UPDATE SET "
            "page_id = EXCLUDED.page_id, center_x = EXCLUDED.center_x, center_y = EXCLUDED.center_y, "
            "zoom_relative_to_fit = EXCLUDED.zoom_relative_to_fit, selected_region_id = EXCLUDED.selected_region_id, "
            "selected_group_id = EXCLUDED.selected_group_id, updated_at = now()"),
            {"b": str(body.browser_id), "d": drawing_id, "p": str(body.page_id), "x": body.center_x,
             "y": body.center_y, "z": body.zoom_relative_to_fit, "r": sid(region), "g": sid(group)})
    return {"saved": True}
