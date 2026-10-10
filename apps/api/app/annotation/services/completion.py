"""Finish drawing (validate + immutable snapshot + COMPLETED in one transaction) and reopening."""
from __future__ import annotations

import uuid
from datetime import timezone
from typing import Any

from sqlalchemy import text

from packages.annotation_domain.canonical import canonical_dumps
from packages.annotation_domain.vocabulary import LABELING_POLICY_VERSION

from ..database import transaction
from ..errors import ApiError
from ..repository import get_drawing, load_document_state, load_pages
from ..schemas import CompleteRequest, ReopenRequest
from ..snapshot import build_snapshot_payload, snapshot_checksum
from ..validation import completion_issues
from .leases import invalidate_lease, verify_lease
from .receipts import find_receipt, insert_receipt, request_hash


def complete(drawing_id: str, body: CompleteRequest) -> dict[str, Any]:
    mutation_id = str(body.mutation_id)
    coverage = [entry.model_dump(mode="json") for entry in body.coverage]
    semantic = {"labeling_policy_version": body.labeling_policy_version,
                "coverage": sorted(coverage, key=lambda c: c["page_id"])}
    digest = request_hash("COMPLETE", drawing_id, mutation_id, body.expected_annotation_version, semantic)
    with transaction() as conn:
        drawing = get_drawing(conn, drawing_id, lock=True)
        prior = find_receipt(conn, drawing_id, mutation_id, "COMPLETE", digest)
        if prior is not None:
            return prior
        current = int(drawing["annotation_version"])
        if drawing["annotation_status"] == "COMPLETED":
            raise ApiError(409, "DRAWING_COMPLETED", "This drawing is already completed.",
                           current_annotation_version=current)
        if drawing["preparation_state"] != "READY":
            raise ApiError(409, "PREPARATION_NOT_READY", "Every page must be prepared before finishing.",
                           current_annotation_version=current)
        verify_lease(conn, drawing_id, body.lease_token, body.lease_generation)
        if int(body.expected_annotation_version) != current:
            raise ApiError(409, "VERSION_CONFLICT", "This drawing changed before it could be finished.",
                           current_annotation_version=current)
        pages = load_pages(conn, drawing_id)
        if not pages or len(pages) != drawing["page_count"]:
            raise ApiError(409, "PREPARATION_NOT_READY", "Every page must be prepared before finishing.",
                           current_annotation_version=current)
        pages_by_id = {str(p["id"]): p for p in pages}
        state = load_document_state(conn, drawing_id)
        problems = completion_issues(state, pages_by_id, coverage, body.labeling_policy_version,
                                     LABELING_POLICY_VERSION)
        if problems:
            raise ApiError(422, "COMPLETION_INVALID",
                           "Some labels need attention before this drawing can be finished.",
                           details=problems, current_annotation_version=current)

        new_version = current + 1
        sequence = int(conn.execute(text(
            "SELECT COALESCE(MAX(sequence), 0) + 1 FROM annotation.training_snapshots WHERE drawing_id = :id"),
            {"id": drawing_id}).scalar_one())
        payload = build_snapshot_payload(drawing, pages, state, new_version, sequence, coverage)
        checksum = snapshot_checksum(payload)
        snapshot_id = str(uuid.uuid4())
        conn.execute(text(
            "INSERT INTO annotation.training_snapshots (id, drawing_id, sequence, annotation_version, schema_version, "
            "payload, payload_sha256) VALUES (:id, :drawing, :seq, :version, :schema, CAST(:payload AS jsonb), :sha)"),
            {"id": snapshot_id, "drawing": drawing_id, "seq": sequence, "version": new_version,
             "schema": payload["schema_version"], "payload": canonical_dumps(payload), "sha": checksum})
        row = conn.execute(text(
            "UPDATE annotation.drawings SET annotation_status = 'COMPLETED', annotation_version = :v, "
            "completed_at = now(), annotation_updated_at = now(), active_training_snapshot_id = :snap "
            "WHERE id = :id RETURNING completed_at"), {"v": new_version, "snap": snapshot_id, "id": drawing_id}).one()
        invalidate_lease(conn, drawing_id)
        result = {
            "mutation_id": mutation_id, "operation_kind": "COMPLETE", "previous_annotation_version": str(current),
            "annotation_version": str(new_version), "annotation_status": "COMPLETED",
            "saved_at": row[0].astimezone(timezone.utc).isoformat(), "snapshot_id": snapshot_id,
            "snapshot_sequence": sequence, "payload_sha256": checksum,
        }
        insert_receipt(conn, drawing_id=drawing_id, mutation_id=mutation_id, operation_kind="COMPLETE", digest=digest,
                       expected_version=current, resulting_version=new_version,
                       request_payload={"expected_annotation_version": body.expected_annotation_version, **semantic},
                       result_payload=result, browser_id=str(body.browser_id), tab_id=str(body.tab_id))
    return result


def reopen(drawing_id: str, body: ReopenRequest) -> dict[str, Any]:
    """Reopen for corrections. No lease: completed drawings never have an active editing lease."""
    mutation_id = str(body.mutation_id)
    digest = request_hash("REOPEN", drawing_id, mutation_id, body.expected_annotation_version, {})
    with transaction() as conn:
        drawing = get_drawing(conn, drawing_id, lock=True)
        prior = find_receipt(conn, drawing_id, mutation_id, "REOPEN", digest)
        if prior is not None:
            return prior
        current = int(drawing["annotation_version"])
        if drawing["annotation_status"] != "COMPLETED":
            raise ApiError(409, "DRAWING_NOT_COMPLETED", "Only completed drawings can be reopened.",
                           current_annotation_version=current)
        if int(body.expected_annotation_version) != current:
            raise ApiError(409, "VERSION_CONFLICT", "This drawing changed before it could be reopened.",
                           current_annotation_version=current)
        new_version = current + 1
        row = conn.execute(text(
            "UPDATE annotation.drawings SET annotation_status = 'IN_PROGRESS', annotation_version = :v, "
            "completed_at = NULL, active_training_snapshot_id = NULL, annotation_updated_at = now() "
            "WHERE id = :id RETURNING annotation_updated_at"), {"v": new_version, "id": drawing_id}).one()
        result = {
            "mutation_id": mutation_id, "operation_kind": "REOPEN", "previous_annotation_version": str(current),
            "annotation_version": str(new_version), "annotation_status": "IN_PROGRESS",
            "saved_at": row[0].astimezone(timezone.utc).isoformat(),
        }
        insert_receipt(conn, drawing_id=drawing_id, mutation_id=mutation_id, operation_kind="REOPEN", digest=digest,
                       expected_version=current, resulting_version=new_version,
                       request_payload={"expected_annotation_version": body.expected_annotation_version},
                       result_payload=result, browser_id=str(body.browser_id), tab_id=str(body.tab_id))
    return result


