"""Deterministic, immutable training snapshots (schema annotation-training-v1)."""
from __future__ import annotations

from typing import Any

from packages.annotation_domain.canonical import canonical_sha256, round_coordinate
from packages.annotation_domain.vocabulary import (LABELING_POLICY_VERSION, SNAPSHOT_SCHEMA_VERSION,
                                                   VOCABULARY_VERSION)

from .repository import sid


def _poly(polygon: list[list[float]]) -> list[list[float]]:
    return [[round_coordinate(x), round_coordinate(y)] for x, y in polygon]


def build_snapshot_payload(drawing: dict[str, Any], pages: list[dict[str, Any]], state: dict[str, dict],
                           annotation_version: int, sequence: int, coverage: list[dict[str, str]]) -> dict[str, Any]:
    """Build the canonical snapshot. Excludes viewport state, lease tokens and unaccepted predictions."""
    page_numbers = {str(p["id"]): p["page_number"] for p in pages}
    coverage_by_page = {entry["page_id"]: entry["status"] for entry in coverage}
    members_by_group: dict[str, list[dict[str, Any]]] = {}
    for member in state["members"].values():
        members_by_group.setdefault(member["group_id"], []).append(
            {"region_id": member["region_id"], "role": member["role"], "order_index": member["order_index"]})
    return {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "labeling_policy_version": LABELING_POLICY_VERSION,
        "vocabulary_version": VOCABULARY_VERSION,
        "sequence": sequence,
        "annotation_version": str(annotation_version),
        "drawing": {
            "id": sid(drawing["id"]), "filename": drawing["filename"], "source_format": drawing["source_format"],
            "source_asset_id": sid(drawing["source_asset_id"]), "source_sha256": drawing["source_sha256"].strip(),
            "lineage_key": drawing["lineage_key"], "page_count": len(pages),
        },
        "coverage": [{"page_id": str(p["id"]), "page_number": p["page_number"],
                      "status": coverage_by_page.get(str(p["id"]))} for p in pages],
        "pages": [{
            "id": str(p["id"]), "page_number": p["page_number"], "coordinate_unit": p["coordinate_unit"],
            "coordinate_space_version": p["coordinate_space_version"], "width": float(p["width"]),
            "height": float(p["height"]), "source_geometry": p["source_geometry"],
            "render_asset_id": str(p["render_asset_id"]), "render_sha256": p["render_sha256"].strip(),
            "render_width_px": p["render_width_px"], "render_height_px": p["render_height_px"],
            "canonical_to_render_matrix": p["canonical_to_render_matrix"], "render_profile": p["render_profile"],
        } for p in sorted(pages, key=lambda p: p["page_number"])],
        "regions": [{
            "id": r["id"], "page_id": r["page_id"], "kind": r["kind"], "polygon": _poly(r["polygon"]),
            "transcription": r["transcription"], "symbol_code": r["symbol_code"], "legibility": r["legibility"],
            "reading_angle_degrees": float(r["reading_angle_degrees"]),
            "semantic_disposition": r["semantic_disposition"], "origin": r["origin"],
            "origin_suggestion_id": r["origin_suggestion_id"],
        } for r in sorted(state["regions"].values(), key=lambda r: (page_numbers[r["page_id"]], r["id"]))],
        "groups": [{
            "id": g["id"], "display_number": g["display_number"], "primary_page_id": g["primary_page_id"],
            "requirement_type": g["requirement_type"], "description": g["description"],
            "structured_value": g["structured_value"],
            "members": sorted(members_by_group.get(g["id"], []),
                              key=lambda m: (m["order_index"], m["region_id"], m["role"])),
        } for g in sorted(state["groups"].values(), key=lambda g: g["display_number"])],
    }


def snapshot_checksum(payload: dict[str, Any]) -> str:
    return canonical_sha256(payload)
