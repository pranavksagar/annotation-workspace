"""Mutation receipts: resolve uncertain network outcomes without replaying writes."""
from __future__ import annotations

import json
from typing import Any

from sqlalchemy import Connection, text

from packages.annotation_domain.canonical import canonical_dumps, canonical_sha256

from ..errors import ApiError


def request_hash(operation_kind: str, drawing_id: str, mutation_id: str, expected_version: str,
                 semantic_payload: Any) -> str:
    """Hash of the logical request. Lease token/generation and browser/tab ids are excluded so the same
    request can be retried after reacquiring a lease."""
    return canonical_sha256({"operation_kind": operation_kind, "drawing_id": drawing_id, "mutation_id": mutation_id,
                             "expected_version": str(expected_version), "payload": semantic_payload})


def find_receipt(conn: Connection, drawing_id: str, mutation_id: str, operation_kind: str,
                 digest: str) -> dict[str, Any] | None:
    row = conn.execute(text(
        "SELECT operation_kind, request_sha256, result_payload FROM annotation.mutation_receipts "
        "WHERE drawing_id = :d AND mutation_id = :m"), {"d": drawing_id, "m": mutation_id}).mappings().first()
    if row is None:
        return None
    if row["operation_kind"] != operation_kind or row["request_sha256"].strip() != digest:
        raise ApiError(409, "IDEMPOTENCY_KEY_REUSED",
                       "This mutation ID was already used for a different change. Submit changes with a new ID.")
    return {**row["result_payload"], "replayed": True}


def insert_receipt(conn: Connection, *, drawing_id: str, mutation_id: str, operation_kind: str, digest: str,
                   expected_version: int, resulting_version: int, request_payload: Any, result_payload: Any,
                   browser_id: str | None, tab_id: str | None) -> None:
    conn.execute(text(
        "INSERT INTO annotation.mutation_receipts (drawing_id, mutation_id, operation_kind, request_sha256, "
        "expected_version, resulting_version, request_payload, result_payload, browser_id, tab_id) VALUES "
        "(:d, :m, :kind, :sha, :expected, :resulting, CAST(:req AS jsonb), CAST(:res AS jsonb), :browser, :tab)"),
        {"d": drawing_id, "m": mutation_id, "kind": operation_kind, "sha": digest, "expected": expected_version,
         "resulting": resulting_version, "req": canonical_dumps(request_payload), "res": json.dumps(result_payload),
         "browser": browser_id, "tab": tab_id})


def get_receipt(conn: Connection, drawing_id: str, mutation_id: str) -> dict[str, Any] | None:
    row = conn.execute(text(
        "SELECT operation_kind, resulting_version, result_payload, created_at FROM annotation.mutation_receipts "
        "WHERE drawing_id = :d AND mutation_id = :m"), {"d": drawing_id, "m": mutation_id}).mappings().first()
    if row is None:
        return None
    return {"mutation_id": mutation_id, "operation_kind": row["operation_kind"],
            "resulting_version": str(row["resulting_version"]), "result": row["result_payload"],
            "created_at": row["created_at"].isoformat()}
