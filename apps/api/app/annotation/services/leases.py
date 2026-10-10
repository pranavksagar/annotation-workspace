"""Short editing leases that stop two tabs editing one drawing. Not access control or ownership."""
from __future__ import annotations

import hashlib
import hmac
import secrets
from typing import Any

from sqlalchemy import Connection, text

from ..config import settings
from ..database import transaction
from ..errors import ApiError
from ..repository import get_drawing, lease_row

HELD_MESSAGE = "Someone is editing this drawing. You can view it or try again shortly."


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _lease_lost(message: str = "Your editing session ended. Reload the drawing to continue editing.") -> ApiError:
    return ApiError(423, "LEASE_LOST", message)


def verify_lease(conn: Connection, drawing_id: str, token: str, generation: str) -> dict[str, Any]:
    """Lock and verify the caller's lease. The drawing row must already be locked by the caller."""
    row = lease_row(conn, drawing_id, lock=True)
    if (row is None or not row["token_hash"] or str(row["generation"]) != str(generation)
            or not hmac.compare_digest(row["token_hash"], token_hash(token))):
        raise _lease_lost()
    if not row["active"]:
        raise _lease_lost("Your editing session expired. Editing will resume after it is renewed.")
    return row


def invalidate_lease(conn: Connection, drawing_id: str) -> None:
    conn.execute(text(
        "UPDATE annotation.editing_leases SET generation = generation + 1, token_hash = '', expires_at = now() "
        "WHERE drawing_id = :id"), {"id": drawing_id})


def acquire(drawing_id: str, browser_id: str, tab_id: str) -> dict[str, Any]:
    with transaction() as conn:
        drawing = get_drawing(conn, drawing_id, lock=True)
        if drawing["annotation_status"] == "COMPLETED":
            raise ApiError(409, "DRAWING_COMPLETED", "This drawing is completed. Reopen it to make corrections.")
        if drawing["preparation_state"] != "READY":
            raise ApiError(409, "PREPARATION_NOT_READY", "This drawing is still being prepared.")
        conn.execute(text(
            "INSERT INTO annotation.editing_leases (drawing_id, browser_id, tab_id, token_hash, generation, expires_at, "
            "last_heartbeat_at) VALUES (:id, :browser, :tab, '', 0, now(), now()) ON CONFLICT (drawing_id) DO NOTHING"),
            {"id": drawing_id, "browser": browser_id, "tab": tab_id})
        row = lease_row(conn, drawing_id, lock=True)
        assert row is not None
        if row["active"] and row["token_hash"]:
            # No force takeover in version one, including from the same browser or a duplicated tab.
            raise ApiError(423, "LEASE_HELD", HELD_MESSAGE,
                           retry_after_seconds=int(float(row["remaining"])) + 1)
        token = secrets.token_urlsafe(32)
        updated = conn.execute(text(
            "UPDATE annotation.editing_leases SET browser_id = :browser, tab_id = :tab, token_hash = :hash, "
            "generation = generation + 1, expires_at = now() + make_interval(secs => :secs), last_heartbeat_at = now() "
            "WHERE drawing_id = :id RETURNING generation, expires_at"),
            {"id": drawing_id, "browser": browser_id, "tab": tab_id, "hash": token_hash(token),
             "secs": settings.edit_lease_seconds}).mappings().one()
    return {"lease_token": token, "lease_generation": str(updated["generation"]),
            "expires_at": updated["expires_at"].isoformat(), "lease_seconds": settings.edit_lease_seconds,
            "heartbeat_seconds": settings.edit_heartbeat_seconds,
            "annotation_version": str(drawing["annotation_version"])}


def heartbeat(drawing_id: str, token: str, generation: str) -> dict[str, Any]:
    with transaction() as conn:
        row = conn.execute(text(
            "UPDATE annotation.editing_leases SET expires_at = now() + make_interval(secs => :secs), "
            "last_heartbeat_at = now() WHERE drawing_id = :id AND generation = CAST(:gen AS bigint) "
            "AND token_hash = :hash AND token_hash <> '' AND expires_at > now() RETURNING expires_at"),
            {"id": drawing_id, "gen": generation, "hash": token_hash(token),
             "secs": settings.edit_lease_seconds}).first()
        if row is None:
            get_drawing(conn, drawing_id)  # 404 for unknown drawings
            raise _lease_lost()
        version = conn.execute(text("SELECT annotation_version FROM annotation.drawings WHERE id = :id"),
                               {"id": drawing_id}).scalar_one()
    return {"lease_generation": str(generation), "expires_at": row[0].isoformat(), "annotation_version": str(version)}


def release(drawing_id: str, token: str, generation: str) -> dict[str, Any]:
    """Release only the caller's matching lease; a stale release cannot end a newer editor's lease."""
    with transaction() as conn:
        result = conn.execute(text(
            "UPDATE annotation.editing_leases SET token_hash = '', expires_at = now() "
            "WHERE drawing_id = :id AND generation = CAST(:gen AS bigint) AND token_hash = :hash AND token_hash <> ''"),
            {"id": drawing_id, "gen": generation, "hash": token_hash(token)})
    return {"released": result.rowcount == 1}
