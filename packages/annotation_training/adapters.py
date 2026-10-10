"""Turn snapshots into in-memory training examples. No export bundles are produced.

Policies (mechanical-labeling-v1):
- Detection: whole completed pages; TEXT/SYMBOL regions are positives, IGNORE and unreadable regions are
  explicit ignore areas.
- Recognition: oriented crops of readable TEXT and supported SYMBOL regions, cut from the exact immutable
  render via the canonical-to-render matrix. Never substitute an axis-aligned bounding box.
- Requirement selection: non-CONTEXT group members are positives; only explicit NON_REQUIREMENT regions are
  negatives. UNCLASSIFIED text and CONTEXT-only links are never negatives.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator

from packages.annotation_domain.geometry import apply_affine, region_crop_transform
from packages.annotation_domain.vocabulary import symbol_codes

from .reader import TrainingSnapshot


@dataclass(frozen=True)
class DetectionExample:
    snapshot_id: str
    page_id: str
    render_asset_id: str
    render_sha256: str
    width_px: int
    height_px: int
    positives: list[dict]       # {"region_id", "class": TEXT|SYMBOL, "polygon_px"}
    ignore_polygons_px: list[list[list[float]]]


@dataclass(frozen=True)
class RecognitionExample:
    snapshot_id: str
    region_id: str
    page_id: str
    render_asset_id: str
    render_sha256: str
    corners_px: list[list[float]]  # reading-order first corner, clockwise
    crop_width: int
    crop_height: int
    kind: str
    target_text: str | None        # exact transcription or the symbol's Unicode character
    symbol_code: str | None        # set for symbol-class targets


@dataclass(frozen=True)
class RequirementExample:
    snapshot_id: str
    region_id: str
    page_id: str
    label: str                     # POSITIVE or NEGATIVE
    memberships: list[dict]        # {"group_id", "display_number", "requirement_type", "role", "order_index"}
    context_links: list[dict]      # CONTEXT memberships, preserved for relation models


def _pages(snapshot: TrainingSnapshot) -> dict[str, dict]:
    return {p["id"]: p for p in snapshot.pages}


def _to_px(page: dict, polygon: list[list[float]]) -> list[list[float]]:
    return [list(apply_affine(page["canonical_to_render_matrix"], p)) for p in polygon]


def iter_detection_examples(snapshot: TrainingSnapshot) -> Iterator[DetectionExample]:
    by_page: dict[str, list[dict]] = {}
    for region in snapshot.regions:
        by_page.setdefault(region["page_id"], []).append(region)
    for page in snapshot.pages:
        positives, ignores = [], []
        for region in by_page.get(page["id"], []):
            polygon_px = _to_px(page, region["polygon"])
            if region["kind"] == "IGNORE" or region["legibility"] == "UNREADABLE":
                ignores.append(polygon_px)
            else:
                positives.append({"region_id": region["id"], "class": region["kind"], "polygon_px": polygon_px})
        yield DetectionExample(str(snapshot.id), page["id"], page["render_asset_id"], page["render_sha256"],
                               page["render_width_px"], page["render_height_px"], positives, ignores)


def iter_recognition_examples(snapshot: TrainingSnapshot) -> Iterator[RecognitionExample]:
    pages, codes = _pages(snapshot), symbol_codes()
    for region in snapshot.regions:
        if region["kind"] == "IGNORE" or region["legibility"] != "READABLE":
            continue
        target, code = None, None
        if region["transcription"]:
            target = region["transcription"]
        elif region["kind"] == "SYMBOL" and region["symbol_code"] in codes:
            code = region["symbol_code"]
            target = codes[code]["unicode"]  # None for glyphs with no Unicode form: class-only target
        else:
            continue
        page = pages[region["page_id"]]
        crop = region_crop_transform(region["polygon"], page["canonical_to_render_matrix"])
        yield RecognitionExample(str(snapshot.id), region["id"], page["id"], page["render_asset_id"],
                                 page["render_sha256"], crop["corners"], crop["width"], crop["height"],
                                 region["kind"], target, code)


def iter_requirement_examples(snapshot: TrainingSnapshot) -> Iterator[RequirementExample]:
    memberships: dict[str, list[dict]] = {}
    contexts: dict[str, list[dict]] = {}
    for group in snapshot.groups:
        for member in group["members"]:
            entry = {"group_id": group["id"], "display_number": group["display_number"],
                     "requirement_type": group["requirement_type"], "role": member["role"],
                     "order_index": member["order_index"]}
            (contexts if member["role"] == "CONTEXT" else memberships).setdefault(member["region_id"], []).append(entry)
    for region in snapshot.regions:
        rid = region["id"]
        if rid in memberships:
            label = "POSITIVE"
        elif region["semantic_disposition"] == "NON_REQUIREMENT":
            label = "NEGATIVE"
        else:
            continue  # UNCLASSIFIED / SUPPORTING_CONTEXT / CONTEXT-only are not semantic negatives
        yield RequirementExample(str(snapshot.id), rid, region["page_id"], label, memberships.get(rid, []),
                                 contexts.get(rid, []))


def crop_region(render_png: bytes, example: RecognitionExample):
    """Rectify the oriented quad from the render. Returns a PIL image of (crop_width, crop_height)."""
    from packages.annotation_domain.crop import crop_quad

    return crop_quad(render_png, example.corners_px, example.crop_width, example.crop_height)
