"""Draft-state and completion validation for the human annotation document."""
from __future__ import annotations

from typing import Any

from packages.annotation_domain.geometry import GeometryError, normalize_quad
from packages.annotation_domain.vocabulary import symbol_codes

Issue = dict[str, Any]


def issue(code: str, message: str, **where: Any) -> Issue:
    return {"code": code, "message": message, **{k: v for k, v in where.items() if v is not None}}


def normalize_polygon(polygon: Any, page: dict[str, Any], **where: Any) -> list[list[float]]:
    from .errors import ApiError
    try:
        return normalize_quad(polygon, float(page["width"]), float(page["height"]))
    except GeometryError as exc:
        raise ApiError(422, "INVALID_GEOMETRY", str(exc),
                       details=[issue("INVALID_GEOMETRY", str(exc), field="polygon", **where)]) from exc


def normalize_angle(value: float) -> float:
    angle = float(value) % 360.0
    if angle > 180:
        angle -= 360
    return 180.0 if angle == -180 else round(angle, 6)


def draft_issues(state: dict[str, dict], pages: dict[str, dict]) -> list[Issue]:
    """Rules that always hold, even for incomplete drafts."""
    issues: list[Issue] = []
    codes = symbol_codes()
    regions, groups, members = state["regions"], state["groups"], state["members"]
    for region in regions.values():
        where = {"region_id": region["id"], "page_id": region["page_id"]}
        if region["page_id"] not in pages:
            issues.append(issue("INVALID_REFERENCE", "Region refers to a page outside this drawing.", **where))
        if region["kind"] == "IGNORE" and region["legibility"] != "UNREADABLE":
            issues.append(issue("INVALID_LABEL", "Unreadable/ignore regions must be marked unreadable.",
                                field="legibility", **where))
        if region["symbol_code"] is not None:
            if region["kind"] != "SYMBOL":
                issues.append(issue("INVALID_LABEL", "Only symbol regions can have a symbol code.",
                                    field="symbol_code", **where))
            elif region["symbol_code"] not in codes:
                issues.append(issue("INVALID_LABEL", "Unknown symbol code.", field="symbol_code", **where))
    positive_regions: set[str] = set()
    for member in members.values():
        where = {"group_id": member["group_id"], "region_id": member["region_id"]}
        group, region = groups.get(member["group_id"]), regions.get(member["region_id"])
        if group is None or region is None:
            issues.append(issue("REGION_HAS_REFERENCES" if group is not None else "INVALID_REFERENCE",
                                "A requirement links to a text region that no longer exists. Remove the link first."
                                if group is not None else "Membership refers to a missing requirement.", **where))
            continue
        if member["role"] != "CONTEXT":
            positive_regions.add(region["id"])
            if region["page_id"] != group["primary_page_id"]:
                issues.append(issue("INVALID_MEMBERSHIP",
                                    "Only CONTEXT links may refer to a different page than the requirement.",
                                    field="role", page_id=region["page_id"], **where))
            if region["kind"] == "IGNORE":
                issues.append(issue("INVALID_MEMBERSHIP", "Unreadable/ignore regions can only be CONTEXT links.",
                                    field="role", **where))
    for region_id in positive_regions:
        if regions[region_id]["semantic_disposition"] == "NON_REQUIREMENT":
            issues.append(issue("INVALID_LABEL", "A region that is part of a requirement cannot be 'Not a requirement'.",
                                field="semantic_disposition", region_id=region_id))
    for group in groups.values():
        if group["primary_page_id"] not in pages:
            issues.append(issue("INVALID_REFERENCE", "Requirement refers to a page outside this drawing.",
                                group_id=group["id"]))
    return issues


def completion_issues(state: dict[str, dict], pages: dict[str, dict], coverage: list[dict[str, str]],
                      labeling_policy_version: str, supported_policy: str) -> list[Issue]:
    """Everything that must be true before a drawing can be training-ready."""
    issues = draft_issues(state, pages)
    codes = symbol_codes()
    if labeling_policy_version != supported_policy:
        issues.append(issue("UNSUPPORTED_POLICY", f"Labeling policy must be {supported_policy}."))
    declared = {entry["page_id"]: entry["status"] for entry in coverage}
    if len(declared) != len(coverage) or set(declared) != set(pages):
        issues.append(issue("COVERAGE_MISMATCH", "The coverage declaration must list every page exactly once."))
    region_count: dict[str, int] = {page_id: 0 for page_id in pages}
    for region in state["regions"].values():
        region_count[region["page_id"]] = region_count.get(region["page_id"], 0) + 1
        where = {"region_id": region["id"], "page_id": region["page_id"]}
        try:
            page = pages[region["page_id"]]
            normalize_quad(region["polygon"], float(page["width"]), float(page["height"]))
        except (GeometryError, KeyError):
            issues.append(issue("INVALID_GEOMETRY", "Region shape is invalid.", field="polygon", **where))
        kind, legible = region["kind"], region["legibility"] == "READABLE"
        text_value = region["transcription"]
        if kind == "TEXT" and legible and not text_value.strip():
            issues.append(issue("INCOMPLETE_LABEL", "Type exactly what this text region shows, or mark it unreadable.",
                                field="transcription", **where))
        if kind == "SYMBOL" and legible and not text_value.strip() and region["symbol_code"] not in codes:
            issues.append(issue("INCOMPLETE_LABEL", "Choose a symbol code or type the symbol, or mark it unreadable.",
                                field="symbol_code", **where))
        if (kind == "IGNORE" or not legible) and (text_value or region["symbol_code"]):
            issues.append(issue("INVALID_LABEL",
                                "Unreadable and ignore regions must not contain a transcription or symbol code.",
                                field="transcription", **where))
    for page_id, status in declared.items():
        if page_id not in pages:
            continue
        count = region_count.get(page_id, 0)
        if count == 0 and status != "NO_LABELABLE_CONTENT":
            issues.append(issue("EMPTY_PAGE_UNCONFIRMED",
                                "This page has no labels. Confirm it contains no labelable text or symbols.",
                                page_id=page_id))
        if count > 0 and status == "NO_LABELABLE_CONTENT":
            issues.append(issue("COVERAGE_MISMATCH", "A page with labels cannot be declared empty.", page_id=page_id))
    non_context: dict[str, int] = {group_id: 0 for group_id in state["groups"]}
    for member in state["members"].values():
        if member["role"] != "CONTEXT" and member["group_id"] in non_context:
            non_context[member["group_id"]] += 1
    for group in state["groups"].values():
        if non_context[group["id"]] == 0:
            issues.append(issue("INCOMPLETE_REQUIREMENT",
                                f"Requirement {group['display_number']} needs at least one linked region that is not "
                                "just context.", group_id=group["id"], page_id=group["primary_page_id"]))
    return issues
