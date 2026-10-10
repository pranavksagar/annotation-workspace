"""Atomic batches of human annotation operations.

Lock order is always drawing row, lease row, annotation entities. Operations are applied to an in-memory
copy of the drawing's document, the whole final state is validated, and the resulting diff is written in
foreign-key-safe order (parents before children, references removed before deletion). All or nothing.
"""
from __future__ import annotations

import copy
import json
from datetime import timezone
from typing import Any

from sqlalchemy import Connection, text

from ..config import settings
from ..database import transaction
from ..errors import ApiError
from ..repository import get_drawing, load_document_state
from ..schemas import MutationEnvelope
from ..validation import draft_issues, issue, normalize_angle, normalize_polygon
from .leases import verify_lease
from .receipts import find_receipt, get_receipt, insert_receipt, request_hash


def semantic_operations(envelope: MutationEnvelope) -> list[dict[str, Any]]:
    return [op.model_dump(mode="json", exclude_unset=True) for op in envelope.operations]


def _load_pages(conn: Connection, drawing_id: str) -> dict[str, dict[str, Any]]:
    return {str(r["id"]): dict(r) for r in conn.execute(text(
        "SELECT id, page_number, width, height FROM annotation.pages WHERE drawing_id = :id"),
        {"id": drawing_id}).mappings()}


def _invalid(message: str, index: int, **where: Any) -> ApiError:
    return ApiError(422, "INVALID_OPERATION", message,
                    details=[issue("INVALID_OPERATION", message, operation_index=index, **where)])


class _Working:
    def __init__(self, state: dict[str, dict], pages: dict[str, dict], next_number: int) -> None:
        self.state = copy.deepcopy(state)
        self.pages = pages
        self.next_number = next_number
        self.touched_pages: list[str] = []

    def page(self, page_id: str, index: int) -> dict[str, Any]:
        page = self.pages.get(page_id)
        if page is None:
            raise _invalid("Page does not belong to this drawing.", index, page_id=page_id)
        return page

    def apply(self, index: int, op: dict[str, Any]) -> None:
        kind = op["type"]
        regions, groups, members = self.state["regions"], self.state["groups"], self.state["members"]
        if kind == "region.create":
            data = op["region"]
            region_id = data["id"]
            if region_id in regions:
                raise _invalid("A region with this ID already exists.", index, region_id=region_id)
            page = self.page(data["page_id"], index)
            region = {
                "id": region_id, "page_id": data["page_id"], "kind": data["kind"],
                "polygon": normalize_polygon(data["polygon"], page, operation_index=index, region_id=region_id),
                "transcription": data.get("transcription", ""), "symbol_code": data.get("symbol_code"),
                "legibility": data.get("legibility", "READABLE"),
                "reading_angle_degrees": normalize_angle(data.get("reading_angle_degrees", 0.0)),
                "semantic_disposition": data.get("semantic_disposition", "UNCLASSIFIED"),
                "origin": "MANUAL", "origin_suggestion_id": None,
            }
            regions[region_id] = region
            self.touched_pages.append(region["page_id"])
        elif kind == "region.update":
            region = regions.get(op["region_id"])
            if region is None:
                raise _invalid("The region to update does not exist.", index, region_id=op["region_id"])
            for field, value in op["changes"].items():
                if value is None and field != "symbol_code":
                    raise _invalid(f"{field} cannot be null.", index, region_id=region["id"], field=field)
                if field == "polygon":
                    value = normalize_polygon(value, self.pages[region["page_id"]], operation_index=index,
                                              region_id=region["id"])
                elif field == "reading_angle_degrees":
                    value = normalize_angle(value)
                region[field] = value
            self.touched_pages.append(region["page_id"])
        elif kind == "region.delete":
            region = regions.pop(op["region_id"], None)
            if region is None:
                raise _invalid("The region to delete does not exist.", index, region_id=op["region_id"])
            self.touched_pages.append(region["page_id"])
        elif kind == "group.create":
            data = op["group"]
            if data["id"] in groups:
                raise _invalid("A requirement with this ID already exists.", index, group_id=data["id"])
            self.page(data["primary_page_id"], index)
            groups[data["id"]] = {
                "id": data["id"], "primary_page_id": data["primary_page_id"], "display_number": self.next_number,
                "requirement_type": data["requirement_type"], "description": data.get("description"),
                "structured_value": data.get("structured_value"),
            }
            self.next_number += 1
            self.touched_pages.append(data["primary_page_id"])
        elif kind == "group.update":
            group = groups.get(op["group_id"])
            if group is None:
                raise _invalid("The requirement to update does not exist.", index, group_id=op["group_id"])
            for field, value in op["changes"].items():
                if value is None and field in ("primary_page_id", "requirement_type"):
                    raise _invalid(f"{field} cannot be null.", index, group_id=group["id"], field=field)
                if field == "primary_page_id":
                    self.page(value, index)
                group[field] = value
            self.touched_pages.append(group["primary_page_id"])
        elif kind == "group.delete":
            group = groups.pop(op["group_id"], None)
            if group is None:
                raise _invalid("The requirement to delete does not exist.", index, group_id=op["group_id"])
            for key in [k for k in members if k[0] == group["id"]]:
                del members[key]  # removes membership rows only, never the source regions
            self.touched_pages.append(group["primary_page_id"])
        elif kind == "member.add":
            key = (op["group_id"], op["region_id"], op["role"])
            if op["group_id"] not in groups or op["region_id"] not in regions:
                raise _invalid("The requirement or region to link does not exist.", index,
                               group_id=op["group_id"], region_id=op["region_id"])
            if key in members:
                raise _invalid("This region already has this role in the requirement.", index,
                               group_id=op["group_id"], region_id=op["region_id"])
            members[key] = {"group_id": key[0], "region_id": key[1], "role": key[2],
                            "order_index": op.get("order_index", 0)}
            region = regions[op["region_id"]]
            if op["role"] != "CONTEXT" and region["semantic_disposition"] == "NON_REQUIREMENT":
                region["semantic_disposition"] = "UNCLASSIFIED"
            self.touched_pages.append(groups[op["group_id"]]["primary_page_id"])
        elif kind == "member.update":
            key = (op["group_id"], op["region_id"], op["role"])
            if key not in members:
                raise _invalid("The link to update does not exist.", index, group_id=key[0], region_id=key[1])
            members[key]["order_index"] = op["changes"]["order_index"]
        elif kind == "member.remove":
            key = (op["group_id"], op["region_id"], op["role"])
            if members.pop(key, None) is None:
                raise _invalid("The link to remove does not exist.", index, group_id=key[0], region_id=key[1])
        else:  # pragma: no cover - the schema rejects unknown operation types
            raise _invalid(f"Unknown operation {kind}.", index)


def _diff(before: dict[str, dict], after: dict[str, dict]) -> tuple[list, list, list]:
    inserted = [after[k] for k in after if k not in before]
    updated = [after[k] for k in after if k in before and after[k] != before[k]]
    deleted = [before[k] for k in before if k not in after]
    return inserted, updated, deleted


def _write(conn: Connection, drawing_id: str, before: dict[str, dict], after: dict[str, dict]) -> dict[str, Any]:
    r_ins, r_upd, r_del = _diff(before["regions"], after["regions"])
    g_ins, g_upd, g_del = _diff(before["groups"], after["groups"])
    m_ins, m_upd, m_del = _diff(before["members"], after["members"])

    new_ids = [r["id"] for r in r_ins] + [g["id"] for g in g_ins]
    if new_ids:
        taken = conn.execute(text(
            "SELECT id FROM annotation.regions WHERE id = ANY(CAST(:ids AS uuid[])) UNION ALL "
            "SELECT id FROM annotation.requirement_groups WHERE id = ANY(CAST(:ids AS uuid[]))"),
            {"ids": new_ids}).first()
        if taken:
            raise ApiError(422, "ENTITY_ID_CONFLICT", "A new label reused an ID that already exists.",
                           details=[issue("ENTITY_ID_CONFLICT", "ID already in use.", entity_id=str(taken[0]))])

    region_params = lambda r: {**r, "drawing": drawing_id, "polygon": json.dumps(r["polygon"])}  # noqa: E731
    group_params = lambda g: {**g, "drawing": drawing_id,  # noqa: E731
                              "structured_value": json.dumps(g["structured_value"]) if g["structured_value"] else None}
    if r_ins:
        conn.execute(text(
            "INSERT INTO annotation.regions (id, drawing_id, page_id, kind, polygon, transcription, symbol_code, "
            "legibility, reading_angle_degrees, semantic_disposition, origin, origin_suggestion_id) VALUES "
            "(:id, :drawing, :page_id, :kind, CAST(:polygon AS jsonb), :transcription, :symbol_code, :legibility, "
            ":reading_angle_degrees, :semantic_disposition, :origin, :origin_suggestion_id)"),
            [region_params(r) for r in r_ins])
    if r_upd:
        conn.execute(text(
            "UPDATE annotation.regions SET kind = :kind, polygon = CAST(:polygon AS jsonb), "
            "transcription = :transcription, symbol_code = :symbol_code, legibility = :legibility, "
            "reading_angle_degrees = :reading_angle_degrees, semantic_disposition = :semantic_disposition, "
            "updated_at = now() WHERE id = :id AND drawing_id = :drawing"), [region_params(r) for r in r_upd])
    if g_ins:
        conn.execute(text(
            "INSERT INTO annotation.requirement_groups (id, drawing_id, primary_page_id, display_number, "
            "requirement_type, description, structured_value) VALUES (:id, :drawing, :primary_page_id, "
            ":display_number, :requirement_type, :description, CAST(:structured_value AS jsonb))"),
            [group_params(g) for g in g_ins])
    if g_upd:
        conn.execute(text(
            "UPDATE annotation.requirement_groups SET primary_page_id = :primary_page_id, "
            "display_number = :display_number, requirement_type = :requirement_type, description = :description, "
            "structured_value = CAST(:structured_value AS jsonb), updated_at = now() "
            "WHERE id = :id AND drawing_id = :drawing"), [group_params(g) for g in g_upd])
    if m_del:
        conn.execute(text(
            "DELETE FROM annotation.group_members WHERE group_id = :group_id AND region_id = :region_id "
            "AND role = :role"), m_del)
    if m_upd:
        conn.execute(text(
            "UPDATE annotation.group_members SET order_index = :order_index WHERE group_id = :group_id "
            "AND region_id = :region_id AND role = :role"), m_upd)
    if m_ins:
        conn.execute(text(
            "INSERT INTO annotation.group_members (drawing_id, group_id, region_id, role, order_index) VALUES "
            "(:drawing, :group_id, :region_id, :role, :order_index)"), [{**m, "drawing": drawing_id} for m in m_ins])
    if g_del:
        conn.execute(text("DELETE FROM annotation.requirement_groups WHERE id = :id AND drawing_id = :drawing"),
                     [{"id": g["id"], "drawing": drawing_id} for g in g_del])
    if r_del:
        conn.execute(text("DELETE FROM annotation.regions WHERE id = :id AND drawing_id = :drawing"),
                     [{"id": r["id"], "drawing": drawing_id} for r in r_del])
    return {
        "changed": {"regions": r_ins + r_upd, "groups": g_ins + g_upd, "members": m_ins + m_upd},
        "deleted": {"region_ids": [r["id"] for r in r_del], "group_ids": [g["id"] for g in g_del],
                    "members": [{"group_id": m["group_id"], "region_id": m["region_id"], "role": m["role"]}
                                for m in m_del]},
    }


def apply_mutation(drawing_id: str, envelope: MutationEnvelope) -> dict[str, Any]:
    if len(envelope.operations) > settings.max_batch_operations:
        raise ApiError(413, "BATCH_TOO_LARGE",
                       f"A save can contain at most {settings.max_batch_operations} changes. Select fewer items.")
    mutation_id = str(envelope.mutation_id)
    operations = semantic_operations(envelope)
    digest = request_hash("EDIT", drawing_id, mutation_id, envelope.expected_annotation_version, operations)
    with transaction() as conn:
        drawing = get_drawing(conn, drawing_id, lock=True)
        # Receipt lookup precedes every mutable-state check so a lost response is always recoverable.
        prior = find_receipt(conn, drawing_id, mutation_id, "EDIT", digest)
        if prior is not None:
            return prior
        current = int(drawing["annotation_version"])
        if drawing["preparation_state"] != "READY":
            raise ApiError(409, "PREPARATION_NOT_READY", "This drawing is still being prepared.",
                           current_annotation_version=current)
        if drawing["annotation_status"] == "COMPLETED":
            raise ApiError(409, "DRAWING_COMPLETED", "This drawing is completed. Reopen it to make corrections.",
                           current_annotation_version=current)
        verify_lease(conn, drawing_id, envelope.lease_token, envelope.lease_generation)
        if int(envelope.expected_annotation_version) != current:
            raise ApiError(409, "VERSION_CONFLICT", "This drawing changed before your edit was saved.",
                           current_annotation_version=current)

        pages = _load_pages(conn, drawing_id)
        before = load_document_state(conn, drawing_id)
        working = _Working(before, pages, int(drawing["next_requirement_number"]))
        for index, op in enumerate(operations):
            working.apply(index, op)
        after = working.state
        if len(after["regions"]) > settings.max_regions_per_drawing:
            raise ApiError(422, "DRAWING_LIMIT", f"A drawing can hold at most {settings.max_regions_per_drawing} regions.")
        if len(after["groups"]) > settings.max_groups_per_drawing:
            raise ApiError(422, "DRAWING_LIMIT", f"A drawing can hold at most {settings.max_groups_per_drawing} requirements.")
        problems = draft_issues(after, pages)
        if problems:
            code = problems[0]["code"]
            raise ApiError(422, code if code in ("REGION_HAS_REFERENCES", "INVALID_MEMBERSHIP") else "INVALID_LABEL",
                           problems[0]["message"], details=problems)

        changes = _write(conn, drawing_id, before, after)
        new_version = current + 1
        last_page = working.touched_pages[-1] if working.touched_pages else None
        row = conn.execute(text(
            "UPDATE annotation.drawings SET annotation_version = :v, annotation_status = 'IN_PROGRESS', "
            "next_requirement_number = :n, annotation_updated_at = now(), "
            "last_annotation_page_id = COALESCE(CAST(:page AS uuid), last_annotation_page_id) "
            "WHERE id = :id RETURNING annotation_updated_at"),
            {"v": new_version, "n": working.next_number, "page": last_page, "id": drawing_id}).one()
        result = {
            "mutation_id": mutation_id, "operation_kind": "EDIT", "previous_annotation_version": str(current),
            "annotation_version": str(new_version), "annotation_status": "IN_PROGRESS",
            "saved_at": row[0].astimezone(timezone.utc).isoformat(), **changes,
        }
        insert_receipt(conn, drawing_id=drawing_id, mutation_id=mutation_id, operation_kind="EDIT", digest=digest,
                       expected_version=current, resulting_version=new_version,
                       request_payload={"expected_annotation_version": envelope.expected_annotation_version,
                                        "operations": operations},
                       result_payload=result, browser_id=str(envelope.browser_id), tab_id=str(envelope.tab_id))
    return result


def receipt(drawing_id: str, mutation_id: str) -> dict[str, Any] | None:
    from ..database import snapshot_read
    with snapshot_read() as conn:
        get_drawing(conn, drawing_id)
        return get_receipt(conn, drawing_id, mutation_id)

